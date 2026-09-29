# -*- coding: utf-8 -*-
"""双轨引擎编排器：波次调度 + 降级收集 + 护栏 + Jinja 成文。

波次（需求 §5.2）：
    前置：S2（纯规则，快）
    波次 1：S3/L4 探索 Agent（线程池并行）｜ S5 / S6 / L1 / L5（主线程纯规则）
    波次 2：L2 贝叶斯（←L1,S3）
    波次 3：L3 结论（←L1,L2,L5）→ S4 计划（←S2,L2）→ S1 信号（←S2,S3,L3,L5）
    收尾：护栏 override → 契约重解析 → 成文 → 结构校验
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from pydantic import ValidationError

from src.agent.deep_research.explore_agents import (
    run_intel_agent,
    run_supply_chain_agent,
)
from src.deep_research_dims.bayesian_dim import build_bayesian_dim
from src.deep_research_dims.conclusion_dim import build_conclusion_dim
from src.deep_research_dims.context import build_shared_context
from src.deep_research_dims.data_dim import build_data_dim
from src.deep_research_dims.guardrail import apply_guardrails
from src.deep_research_dims.history_dim import build_history_dim
from src.deep_research_dims.narrate import narrate
from src.deep_research_dims.phase_dim import build_phase_dim
from src.deep_research_dims.plan_dim import build_plan_dim
from src.deep_research_dims.render import build_view, render_markdown, validate_structure
from src.deep_research_dims.scenarios_dim import build_scenarios_dim
from src.deep_research_dims.signal_dim import build_signal_dim
from src.deep_research_dims.six_dim import build_six_dim
from src.schemas.bayesian_framework import EvidenceItem
from src.schemas.deep_research_dims import (
    DIM_IDS,
    DimEnvelope,
    GuardrailEvent,
    IntelDim,
    SupplyChainDim,
)
from src.schemas.report_schema import Intelligence
from src.schemas.supply_chain import SupplyChain

logger = logging.getLogger(__name__)

ProgressCb = Optional[Callable[[Dict[str, Any]], None]]


@dataclass
class DualTrackResult:
    """双轨引擎产出（service 层落盘/推送用）。"""

    success: bool = False
    status: str = "failed"  # success | partial | failed
    markdown: str = ""
    dims: Dict[str, DimEnvelope] = field(default_factory=dict)
    dims_payload: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    guardrail_events: List[GuardrailEvent] = field(default_factory=list)
    total_steps: int = 0
    total_tokens: int = 0
    provider: str = ""
    error: Optional[str] = None

    @property
    def degraded_dims(self) -> List[str]:
        return [d for d, m in self.dims.items() if m.status == "degraded"]

    @property
    def quality_score(self) -> int:
        ok = sum(1 for m in self.dims.values() if m.status == "ok")
        return round(ok / max(len(DIM_IDS), 1) * 100)


def _emit(cb: ProgressCb, event: Dict[str, Any]) -> None:
    if cb:
        try:
            cb(event)
        except Exception:  # noqa: BLE001 - 进度回调不得影响主流程
            pass


def _parse_intel(parsed: Dict[str, Any], steps: int) -> tuple[IntelDim, int]:
    evidence: List[EvidenceItem] = []
    for raw in parsed.get("evidence_items") or []:
        try:
            evidence.append(
                EvidenceItem(
                    evidence=str(raw.get("evidence") or "")[:500],
                    strength=raw.get("strength") or "neutral",
                    lr=float(raw.get("lr") or 1.0),
                    posterior_p=0.5,
                    date=str(raw.get("date") or "")[:10],
                )
            )
        except (ValidationError, ValueError, TypeError):
            continue
    intelligence = Intelligence(
        latest_news=parsed.get("latest_news"),
        risk_alerts=list(parsed.get("risk_alerts") or [])[:10],
        positive_catalysts=list(parsed.get("positive_catalysts") or [])[:10],
        earnings_outlook=parsed.get("earnings_outlook"),
        sentiment_summary=parsed.get("sentiment_summary"),
    )
    dim = IntelDim(
        intelligence=intelligence,
        evidence_items=evidence,
        unverified_count=int(parsed.get("unverified_count") or 0),
    )
    return dim, steps


def _parse_supply_chain(
    parsed: Dict[str, Any], steps: int
) -> tuple[SupplyChainDim, int]:
    try:
        model = SupplyChain(
            company_position=str(parsed.get("company_position") or "数据不足"),
            chain_map=list(parsed.get("chain_map") or []),
            chokepoints=list(parsed.get("chokepoints") or []),
            upstream=list(parsed.get("upstream") or []),
            downstream=list(parsed.get("downstream") or []),
            bargaining_power=parsed.get("bargaining_power"),
            us_china_chain=parsed.get("us_china_chain"),
        )
    except ValidationError as exc:
        raise ValueError(f"产业链契约校验失败: {exc}") from exc
    dim = SupplyChainDim(
        supply_chain=model,
        verification_status=str(parsed.get("verification_status") or "unverified"),
    )
    return dim, steps


def run_dual_track(
    stock_code: str,
    stock_name: str,
    llm_adapter: Any,
    progress_callback: ProgressCb = None,
    explore_max_steps: int = 8,
) -> DualTrackResult:
    """执行一次双轨深度投研分析。"""
    result = DualTrackResult()
    dims: Dict[str, DimEnvelope] = {}

    def emit_dim(dim_id: str, status: str) -> None:
        _emit(progress_callback, {"type": "dim_done", "dim": dim_id, "status": status})

    def start_dim(dim_id: str) -> None:
        _emit(progress_callback, {"type": "dim_start", "dim": dim_id})

    # ---- 阶段 0 + 前置 S2 ----
    _emit(progress_callback, {"type": "thinking", "step": 0, "message": "装配共享数据快照（行情/日线/基本面/筹码/历史）..."})
    ctx = build_shared_context(stock_code, stock_name)

    _emit(progress_callback, {"type": "thinking", "step": 0, "message": "计算数据透视（均线/量比/支撑阻力/筹码）..."})
    start_dim("data")
    data_dim = build_data_dim(ctx)
    dims["data"] = data_dim
    emit_dim("data", data_dim.status)

    # ---- 波次 1：探索 Agent 线程池 + 主线程纯规则 ----
    _emit(progress_callback, {"type": "thinking", "step": 1, "message": "并行执行情报/产业链探索与规则维度..."})

    def intel_worker() -> tuple[str, Any, int]:
        out = run_intel_agent(stock_code, stock_name, llm_adapter, progress_callback, explore_max_steps)
        if out.get("ok"):
            return "ok", out["data"], int(out.get("steps") or 0)
        return "err", out.get("error") or "情报探索失败", 0

    def supply_chain_worker() -> tuple[str, Any, int]:
        out = run_supply_chain_agent(stock_code, stock_name, llm_adapter, progress_callback, explore_max_steps)
        if out.get("ok"):
            return "ok", out["data"], int(out.get("steps") or 0)
        return "err", out.get("error") or "产业链探索失败", 0

    steps_total = 0
    with ThreadPoolExecutor(max_workers=2) as pool:
        start_dim("intel")
        fut_intel = pool.submit(intel_worker)
        start_dim("supply_chain")
        fut_sc = pool.submit(supply_chain_worker)

        start_dim("phase")
        phase_dim = build_phase_dim(ctx)
        dims["phase"] = phase_dim
        emit_dim("phase", phase_dim.status)
        start_dim("history")
        history_dim = build_history_dim(ctx)
        dims["history"] = history_dim
        emit_dim("history", history_dim.status)
        start_dim("six_dim")
        six_dim = build_six_dim(ctx, data_dim.model_dump())
        dims["six_dim"] = six_dim
        emit_dim("six_dim", six_dim.status)

        try:
            sc_status, sc_data, sc_steps = fut_sc.result(timeout=600)
            if sc_status == "ok":
                sc_dim, used = _parse_supply_chain(sc_data, sc_steps)
                dims["supply_chain"] = sc_dim
                steps_total += used
            else:
                dims["supply_chain"] = SupplyChainDim(status="degraded", degraded_reason=str(sc_data))
        except Exception as exc:  # noqa: BLE001
            dims["supply_chain"] = SupplyChainDim(status="degraded", degraded_reason=f"产业链维度异常: {exc}")
        emit_dim("supply_chain", dims["supply_chain"].status)

        try:
            intel_status, intel_data, intel_steps = fut_intel.result(timeout=600)
            if intel_status == "ok":
                intel_dim, used = _parse_intel(intel_data, intel_steps)
                dims["intel"] = intel_dim
                steps_total += used
            else:
                dims["intel"] = IntelDim(status="degraded", degraded_reason=str(intel_data))
        except Exception as exc:  # noqa: BLE001
            dims["intel"] = IntelDim(status="degraded", degraded_reason=f"情报维度异常: {exc}")
        emit_dim("intel", dims["intel"].status)

    # ---- 波次 2：L2 贝叶斯 ----
    _emit(progress_callback, {"type": "thinking", "step": 2, "message": "贝叶斯证据链计算（LR 标定 + 后验更新）..."})
    evidence_dicts = [
        e.model_dump() for e in getattr(dims["intel"], "evidence_items", [])
    ]
    start_dim("bayesian")
    bayesian_dim = build_bayesian_dim(
        dims["six_dim"].model_dump(), evidence_dicts
    )
    dims["bayesian"] = bayesian_dim
    emit_dim("bayesian", bayesian_dim.status)

    # ---- 波次 3：L5 → L3 → S4 → S1 ----
    _emit(progress_callback, {"type": "thinking", "step": 3, "message": "合成情景/结论/计划/信号（规则 + 护栏）..."})
    current_price = ctx.quote.get("price")
    start_dim("scenarios")
    scenarios_dim = build_scenarios_dim(ctx, float(current_price) if isinstance(current_price, (int, float)) else None)
    facts_scen = scenarios_dim.model_dump()
    scenarios_dim = scenarios_dim.model_copy(
        update={
            "narrative": narrate(
                llm_adapter, "scenarios", facts_scen, scenarios_dim.narrative
            )
        }
    )
    dims["scenarios"] = scenarios_dim
    emit_dim("scenarios", scenarios_dim.status)

    start_dim("conclusion")
    conclusion_dim = build_conclusion_dim(
        ctx, dims["six_dim"].model_dump(), dims["bayesian"].model_dump(), dims["scenarios"].model_dump()
    )
    facts_conc = conclusion_dim.model_dump()
    conclusion_dim = conclusion_dim.model_copy(
        update={
            "rationale": narrate(llm_adapter, "conclusion", facts_conc, conclusion_dim.narrative)
        }
    )
    dims["conclusion"] = conclusion_dim
    emit_dim("conclusion", conclusion_dim.status)

    position_suggestion = (
        getattr(dims["bayesian"].bayesian, "position_suggestion", None) or "观察"
    )
    start_dim("plan")
    plan_dim = build_plan_dim(ctx, dims["data"].model_dump(), position_suggestion)
    facts_plan = plan_dim.model_dump()
    plan_dim = plan_dim.model_copy(
        update={"narrative": narrate(llm_adapter, "plan", facts_plan, plan_dim.narrative)}
    )
    dims["plan"] = plan_dim
    emit_dim("plan", plan_dim.status)

    start_dim("signal")
    signal_dim = build_signal_dim(
        ctx,
        dims["conclusion"].model_dump(),
        dims["scenarios"].model_dump(),
    )
    facts_sig = signal_dim.model_dump()
    signal_dim = signal_dim.model_copy(
        update={"narrative": narrate(llm_adapter, "signal", facts_sig, signal_dim.narrative)}
    )
    dims["signal"] = signal_dim
    emit_dim("signal", signal_dim.status)

    # ---- 护栏层（override 后叙述修订 + 契约重解析）----
    payloads = {d: m.model_dump() for d, m in dims.items()}
    events, adjusted = apply_guardrails(payloads)
    result.guardrail_events = events
    from src.deep_research_dims.guardrail import append_override_note
    from src.schemas.deep_research_dims import parse_dim

    # A3 修复：结论行动/信号评级被 override 时，叙述字段追加修订说明，避免与旧结论矛盾
    old_conclusion_action = str(
        ((payloads.get("conclusion") or {}).get("conclusion") or {}).get("action") or ""
    )
    new_conclusion_action = str(
        ((adjusted.get("conclusion") or {}).get("conclusion") or {}).get("action") or ""
    )
    if new_conclusion_action and new_conclusion_action != old_conclusion_action:
        adjusted["conclusion"] = append_override_note(
            adjusted.get("conclusion") or {}, "行动", old_conclusion_action, new_conclusion_action
        )
    old_rating = str((payloads.get("signal") or {}).get("rating") or "")
    new_rating = str((adjusted.get("signal") or {}).get("rating") or "")
    if new_rating and new_rating != old_rating:
        adjusted["signal"] = append_override_note(
            adjusted.get("signal") or {}, "评级", old_rating, new_rating
        )

    final_dims: Dict[str, DimEnvelope] = {}
    for dim_id, payload in adjusted.items():
        try:
            final_dims[dim_id] = parse_dim(dim_id, payload)
        except (ValidationError, ValueError) as exc:
            logger.warning("[DualTrack] 护栏后契约重解析失败 %s: %s", dim_id, exc)
            final_dims[dim_id] = dims[dim_id]
    dims = final_dims

    # ---- 成文 ----
    _emit(progress_callback, {"type": "thinking", "step": 4, "message": "生成双轨投研报告（模板直灌）..."})
    view = build_view(stock_name, stock_code, ctx.as_of, ctx.limitations, dims, events)
    markdown = render_markdown(view)
    missing = validate_structure(markdown)
    if missing:
        markdown = (
            f"> ⚠️ 报告结构不完整，缺失章节：{', '.join(missing)}\n\n" + markdown
        )

    degraded = [d for d, m in dims.items() if m.status == "degraded"]
    ok_count = len(dims) - len(degraded)
    status = "success" if not degraded else "partial" if ok_count >= 6 else "failed"

    result.success = status != "failed" and bool(markdown.strip())
    result.status = status
    result.markdown = markdown
    result.dims = dims
    result.dims_payload = {d: m.model_dump() for d, m in dims.items()}
    result.total_steps = steps_total
    result.provider = ""
    result.error = None if result.success else "维度降级过多，报告不可用"
    return result
