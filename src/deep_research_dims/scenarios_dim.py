# -*- coding: utf-8 -*-
"""L5 情景：概率和=100% 机器校验 + EV 强制计算 + 估值口径自动切换。叙述型 LLM。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
import json
from datetime import date
from pydantic import ValidationError

from icontract import require

from src.deep_research_dims.context import SharedContext
from src.deep_research_dims.fundamental_dim import eligible_financial_value
from src.schemas.deep_research_dims import ScenariosDim

from src.schemas.value_scenarios import AnalystTarget, Scenario, ValueHorizons, ValueScenarios


@require(lambda scenarios: len(scenarios) > 0, "至少一个情景")
def normalize_probabilities(scenarios: List[Scenario]) -> List[Scenario]:
    """概率和机器归一化到严格 100%（护栏规则 4 的底层实现）。"""
    total = sum(s.probability for s in scenarios)
    if total <= 0:
        return [s.model_copy(update={"probability": round(1.0 / len(scenarios), 4)}) for s in scenarios]
    normalized = [
        s.model_copy(update={"probability": round(s.probability / total, 4)})
        for s in scenarios
    ]
    # 四舍五入残差补到概率最大的情景上，保证和严格=1
    residual = round(1.0 - sum(s.probability for s in normalized), 4)
    if residual and normalized:
        top = max(normalized, key=lambda s: s.probability)
        idx = normalized.index(top)
        normalized[idx] = top.model_copy(
            update={"probability": round(top.probability + residual, 4)}
        )
    return normalized


def expected_value(scenarios: List[Scenario]) -> Optional[float]:
    """期望目标价 EV=Σ(概率×价值锚)。无价值锚情景不参与。"""
    anchors = [s for s in scenarios if s.value_anchor]
    if not anchors:
        return None
    return round(sum(s.probability * float(s.value_anchor) for s in anchors), 2)  # type: ignore[arg-type]


def build_scenarios_dim(
    ctx: SharedContext,
    current_price: Optional[float],
    llm_scenarios: Optional[Dict[str, Any]] = None,
    llm_adapter: Any = None,
) -> ScenariosDim:
    """L5：LLM 提供情景叙述（可选），概率/EV/口径全部机器计算；时间层级挂条件句。"""
    fund = ctx.fundamental
    pe_ttm = eligible_financial_value(fund, "pe_ratio")

    raw_scenarios: List[Scenario] = []
    raw = (llm_scenarios or {}).get("scenarios") or []
    for item in raw:
        try:
            raw_scenarios.append(
                Scenario(
                    type=item.get("type") or "neutral",
                    probability=float(item.get("probability") or 0),
                    value_anchor=None,  # Model targets have no source/calculation evidence in this contract.
                    description=item.get("description"),
                )
            )
        except (ValueError, TypeError):
            continue

    normalized = normalize_probabilities(raw_scenarios) if raw_scenarios else []

    scenarios = ValueScenarios(
        industry_space=(llm_scenarios or {}).get("industry_space"),
        competitive_evolution=(llm_scenarios or {}).get("competitive_evolution"),
        scenarios=normalized,
        horizons=ValueHorizons(
            horizon_1y=None,
            horizon_3y=None,
            horizon_5y=None,
        ),
        catalysts=list((llm_scenarios or {}).get("catalysts") or []),
        risks=list((llm_scenarios or {}).get("risks") or []),
    )
    prob_sum = round(sum(s.probability for s in normalized), 4)
    research = fund.get("research") or {}
    targets = research.get("targets") or {}
    reference = None
    reason = "缺少可追溯的目标价与估值计算依据，仅保留定性情景"
    if research.get("source") == "choice":
        reason = "Choice 在截至日期的近180日窗口未提供机构目标价；不以现价倍数或模型猜测补齐"
        target_errors = [error for error in research.get("errors") or [] if error.startswith("choice:targets:")]
        if target_errors:
            cause = {
                "authentication_failed": "Choice登录验证暂未通过",
                "permission_denied": "当前Choice权限未覆盖此项数据",
                "quota_exhausted": "Choice调用额度受限",
                "timeout": "Choice查询超时",
                "sdk_unavailable": "Choice数据连接尚未就绪",
            }.get(target_errors[0].rsplit(":", 1)[-1], "Choice查询未完成")
            reason = f"机构目标价尚未取得：{cause}，暂时无法提供估值参考。恢复数据连接后可重新生成。"
        elif targets.get("RATINGTARGETPRICE") is not None:
            try:
                reference = AnalystTarget.model_validate_json(json.dumps({"source": research["source"], "as_of": research.get("as_of"), "window_days": research.get("target_window_days"), "price": targets["RATINGTARGETPRICE"], "low": targets.get("RATINGTARGETPRICEMIN"), "high": targets.get("RATINGTARGETPRICEMAX")}))
                if (date.fromisoformat(ctx.as_of[:10]) - reference.as_of).days not in (0, 1):
                    reference = None
                    reason = "机构目标价快照已过期，需重新获取"
            except (ValidationError, ValueError):
                reference = None
                reason = "机构目标价数据契约异常，不能参与估值"
    return ScenariosDim(
        status="ok" if reference is not None else "degraded",
        degraded_reason=None if reference is not None else reason,
        scenarios=scenarios,
        probability_sum=prob_sum,
        expected_value=None,
        valuation_basis="analyst_consensus" if reference is not None else "none",
        analyst_target=reference,
        target_price=float(reference.price) if reference is not None else None,
        current_pe_ttm=pe_ttm,
        narrative=(f"机构目标价综合值 {reference.price:.2f} 元；Choice {reference.indicator}，截至 {reference.as_of}，统计窗口180日。仅为机构研报参考，不是独立估值或三情景概率加权期望值，窗口不代表目标价期限。" if reference is not None else reason),
    )
