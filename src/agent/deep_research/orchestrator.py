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
import re
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import ValidationError

from src.agent.deep_research.explore_agents import (  # noqa: F401
    run_business_agent,
    run_capital_agent,
    run_intel_agent,
    run_sentiment_agent,
    run_supply_chain_agent,
    run_technical_agent,
    run_us_china_agent,
)
from src.deep_research_dims.bayesian_dim import build_bayesian_dim
from src.deep_research_dims.conclusion_dim import build_conclusion_dim
from src.deep_research_dims.context import SharedContext, build_shared_context
from src.deep_research_dims.data_dim import adopt_concept_membership, build_data_dim
from src.deep_research_dims.dim_cache import quote_market_identity
from src.deep_research_dims.fundamental_dim import build_fundamental_dim
from src.deep_research_dims.manager_writeup import DIRECTIVE_WORDS
from src.deep_research_dims.sector_dim import build_sector_dim
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
    ACTIVE_DIM_IDS,
    DIM_IDS,
    DIM_MODELS,
    DataDim,
    DimEnvelope,
    GuardrailEvent,
    IntelDim,
    SupplyChainDim,
)
from src.schemas.report_schema import Intelligence
from src.schemas.supply_chain import SupplyChain

from src.core.research_run import ResearchRun, ResearchStopped, current_research, research_dimension
from src.llm.attempt import attempts_detail, known_total_tokens, summarize_attempts

logger = logging.getLogger(__name__)

ProgressCb = Optional[Callable[[Dict[str, Any]], None]]

# 维度依赖图（省钱模式 dims 子集自动补依赖闭包，避免产出"无输入的派生维度"）
_DIM_DEPENDENCIES: Dict[str, frozenset] = {
    "data": frozenset(),
    "phase": frozenset(),
    "history": frozenset(),
    "intel": frozenset(),
    "supply_chain": frozenset(),
    "six_dim": frozenset({"data"}),
    "bayesian": frozenset({"six_dim"}),
    "scenarios": frozenset(),
    "conclusion": frozenset({"six_dim", "bayesian", "scenarios"}),
    "plan": frozenset({"data", "bayesian"}),
    "signal": frozenset({"conclusion", "scenarios"}),
}


def expand_dim_selection(dims: Optional[set[str]]) -> set[str]:
    """把用户选择的维度扩展为依赖闭包；None/空 = 当前全部 18 项（issue #64）。

    与 API/服务共用 ``resolve_dim_selection`` 同一解析语义：先校验未知 ID
    （ValueError），再剔除退役 ID；仅退役请求按受理边界同语义拒绝，不得
    静默转成默认全量或带着未知项启动取数。
    """
    from src.schemas.deep_research_dims import (
        ACTIVE_DIM_IDS,
        resolve_dim_selection,
    )

    resolved = resolve_dim_selection(set(dims) if dims else None)
    selected = resolved if resolved is not None else set(ACTIVE_DIM_IDS)
    changed = True
    while changed:
        changed = False
        for dim in list(selected):
            for dep in _DIM_DEPENDENCIES.get(dim, frozenset()):
                if dep not in selected:
                    selected.add(dep)
                    changed = True
    return selected


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
    # 模型尝试账本汇总（issue #55）：total_tokens 只含可确认合计；
    # usage.completeness 区分 complete/partial/unknown/no_outbound，
    # 未知用量不得冒充确认零消耗。
    usage: Dict[str, Any] = field(default_factory=dict)
    # 终读结论（需求 3）：LLM 通读结构化事实写报告级总结；空 = 回退信号一句话
    final_conclusion: str = ""
    # 经理终稿（研报体 v2）：观点式标题/内容概括/分节叙事；空 dict = 模板全降级
    manager_writeup: Dict[str, Any] = field(default_factory=dict)

    @property
    def degraded_dims(self) -> List[str]:
        return [d for d, m in self.dims.items() if m.status == "degraded"]

    @property
    def quality_score(self) -> int:
        executed = [m for m in self.dims.values() if m.status != "skipped"]
        if not executed:
            return 0
        ok = sum(1 for m in executed if m.status == "ok")
        return round(ok / len(executed) * 100)


def _emit(cb: ProgressCb, event: Dict[str, Any]) -> None:
    if cb:
        try:
            cb(event)
        except Exception:  # noqa: BLE001 - 进度回调不得影响主流程
            pass


# 情报正文大界防御（issue #40）：合理长度原文保留，只有极端超长才截断并披露，
# 不再按 200/100/120/80 字符静默硬截半句话。
_ROOT_CAUSE_LIMIT = 2000
_EVIDENCE_LIMIT = 2000
_EVENT_TEXT_LIMIT = 400
_EVENT_DATE_LIMIT = 24  # 与展示层 _clean_event_date 的收口上限一致，避免双层截断吞掉披露
_EVENT_ITEM_LIMIT = 500
_OUTCOMES_MAX = 8
_PLANS_MAX = 8
_CALENDAR_MAX = 10


def _bounded(text: Any, limit: int) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[:limit] + "（内容超长，已截断）"


# issue #68：情报动作/结果字段只允许研究核验/跟踪动作与事实描述。该检测
# 复用经理指令拒绝词表，并加 T1 买卖编号——它们是情报任务旧契约下独立交易
# 预案的标记。只用于 plans/outcomes/evidence_items 结构化条目，不用于事件
# 标题以外的自由叙述（「券商维持增持、目标价29.4」等合法研报引用必须保留）。
# 经理词表中的「增持/减持」（评级引用/股东行为语境）、「跌破/兑现/首批/退出」
# （核验动作常见业务语境）不生效；补充「半仓/满仓」仓位指令词。
# 评级引用豁免与引号风格无关（spec/standards 反例）：评级标签紧邻「评级/
# 评分/rating」语境（之间只允许至多 4 个引号/标点/空白字符）时按引用豁免——
# 维持买入评级、“买入”评级、"买入"评级 语义相同。豁免只遮蔽标签本身：同一
# 文本其他位置的指令（执行"36元买入、仓位3%"）、无评级语境的裸标签
# （若业绩超预期则"买入"）与非评级交易命令原样接受检测。「核验/若…则」
# 等前缀不豁免指令。英文评级词（buy/sell 等）不在中文指令词表内，无需豁免。
_PLAN_MANAGER_ONLY_WORDS = frozenset({"增持", "减持", "跌破", "兑现", "首批", "退出"})
_PLAN_DIRECTIVE_WORDS = (
    *(word for word in DIRECTIVE_WORDS if word not in _PLAN_MANAGER_ONLY_WORDS),
    "半仓", "满仓", "T1-L", "T1-R", "T1-S1", "T1-SL",
)
_DIRECTIVE_PATTERNS = (r"[增减]持\s*[至到]\s*\d",)
_RATING_LABEL_RE = re.compile(
    "买入|卖出|增持|减持|中性|持有|推荐|看多|看空|回避"
)
# 标签与评级语境之间允许的间隔字符：引号/标点/空白（不含其他文字）。
_RATING_CONTEXT_GAP_CHARS = "\"'“”‘’「」『』()（）[]【】《》、，。；：！？,.;:!?\t \n\r—…·-"
_RATING_CONTEXT_MAX_GAP = 4
_MASKED_CHAR = "▢"


def _near_rating_context(value: str, start: int, end: int) -> bool:
    """评级标签出现位置是否紧邻真实评级语境（标签与语境词间只允许间隔字符）。"""
    cut = end
    skipped = 0
    while cut < len(value) and skipped < _RATING_CONTEXT_MAX_GAP and value[cut] in _RATING_CONTEXT_GAP_CHARS:
        cut += 1
        skipped += 1
    tail = value[cut:]
    if tail.startswith(("评级", "评分")) or tail[:6].lower().startswith("rating"):
        return True
    cut = start
    skipped = 0
    while cut > 0 and skipped < _RATING_CONTEXT_MAX_GAP and value[cut - 1] in _RATING_CONTEXT_GAP_CHARS:
        cut -= 1
        skipped += 1
    head = value[:cut]
    return head.endswith(("评级", "评分")) or head[-6:].lower().endswith("rating")


def _mask_rating_citations(value: str) -> str:
    """遮蔽「评级标签紧邻评级语境」的引用字符，供指令检测跳过。

    评级引用是合法研报引用，豁免与引号风格无关；引号本身不是通行证——
    引号内的指令与无评级语境的裸标签不在豁免范围，原样接受词表与模式检测。
    """
    spans = [
        match.span()
        for match in _RATING_LABEL_RE.finditer(value)
        if _near_rating_context(value, match.start(), match.end())
    ]
    if not spans:
        return value
    masked = list(value)
    for start, end in spans:
        for index in range(start, end):
            masked[index] = _MASKED_CHAR
    return "".join(masked)


def _directive_hit(text: Any) -> str:
    """返回条目命中的第一个独立交易指令标记；无命中返回空串。

    唯一引用例外：紧邻评级语境的评级标签（维持“买入”评级/维持买入评级/
    维持"买入"评级）按评级引用豁免；其余全部文本（含引号内）走同一词表
    与模式——引号不是交易指令通行证，不增加数字/语句变体解析规则。
    """
    value = str(text or "")
    if not value:
        return ""
    masked = _mask_rating_citations(value)
    for word in _PLAN_DIRECTIVE_WORDS:
        if word in masked:
            return word
    for pattern in _DIRECTIVE_PATTERNS:
        match = re.search(pattern, masked)
        if match:
            return match.group(0)
    return ""


def _normalize_slot_items(
    raw: Any, slot: str, event_label: str, truncation_notes: List[str]
) -> List[Optional[str]]:
    """归一化事件 outcomes/plans 槽位为条目序列，返回列表中 None 表示该位置
    条目无效（位置计数与原始响应一致）。

    issue #68 真实报告反例（2026-10-30 飞凯材料授权实跑）：槽位契约是字符串
    数组，但真实响应把整个槽位写成单个完整字符串——按序列处理会被按字符
    拆散成伪条目、伪造「超出上限」丢弃披露，指令检测也被字符化绕过。归一化：
    单个字符串是一条完整条目；字符串数组保持原序与既有上限语义；缺失/空
    一致为空；错误容器/标量与非字符串成员不清洗成研究内容、不静默消失，
    拒绝并随截断披露可见，也不拖垮整个研究。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw] if raw else []
    if isinstance(raw, (list, tuple)):
        normalized: List[Optional[str]] = []
        for index, member in enumerate(raw):
            if isinstance(member, str):
                normalized.append(member)
            else:
                truncation_notes.append(
                    f"事件「{event_label}」第 {index + 1} 条{slot}非字符串"
                    f"（{type(member).__name__}），未采用"
                )
                normalized.append(None)
        return normalized
    truncation_notes.append(
        f"事件「{event_label}」{slot}字段不是字符串或字符串数组"
        f"（{type(raw).__name__}），未采用"
    )
    return []


def _parse_intel(parsed: Dict[str, Any], steps: int) -> tuple[IntelDim, int]:
    evidence: List[EvidenceItem] = []
    truncation_notes: List[str] = []
    for raw in parsed.get("evidence_items") or []:
        hit = _directive_hit(raw.get("evidence") if isinstance(raw, dict) else raw)
        if hit:
            # issue #68：夹带独立交易指令的证据条目在贝叶斯消费前拒绝并披露，
            # 合法研报引用（含评级引用，与引号风格无关）保留。
            truncation_notes.append(
                f"证据条目含独立交易指令（命中「{hit}」），未采用，不进入贝叶斯证据链"
            )
            continue
        try:
            evidence.append(
                EvidenceItem(
                    evidence=_bounded(raw.get("evidence"), _EVIDENCE_LIMIT),
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
    rc_raw = parsed.get("root_cause") or {}
    root_cause = None
    if any(rc_raw.get(k) for k in ("event", "mechanism", "magnitude")):
        root_cause = {
            "event": _bounded(rc_raw.get("event"), _ROOT_CAUSE_LIMIT),
            "mechanism": _bounded(rc_raw.get("mechanism"), _ROOT_CAUSE_LIMIT),
            "magnitude": _bounded(rc_raw.get("magnitude"), _ROOT_CAUSE_LIMIT),
            "persistence": _bounded(rc_raw.get("persistence"), _ROOT_CAUSE_LIMIT),
        }
    calendar = []
    dropped_events = 0
    for item in parsed.get("event_calendar") or []:
        if not isinstance(item, dict):
            continue
        if len(calendar) >= _CALENDAR_MAX:
            dropped_events += 1
            continue
        event_label = str(item.get("event") or "未知")[:30]
        # issue #68（root 反例）：事件标题夹带独立交易指令时整个事件条目拒绝，
        # 不清洗标题伪装成合规事件；合法事件标题（披露/解禁/会议等）不受影响。
        event_hit = _directive_hit(item.get("event"))
        if event_hit:
            truncation_notes.append(
                f"事件「{event_label}」标题含独立交易指令（命中「{event_hit}」），整个事件未采用；"
                "操作条件以本报告规则作战计划为准"
            )
            continue
        # issue #68 真实报告反例：槽位先归一化（单字符串=一条完整条目），
        # 再做超限计数与指令检测，避免按字符拆散伪条目与伪造超限披露。
        slot_plans = _normalize_slot_items(
            item.get("plans"), "应对预案", event_label, truncation_notes
        )
        slot_outcomes = _normalize_slot_items(
            item.get("outcomes"), "可能结果", event_label, truncation_notes
        )
        if len(slot_outcomes) > _OUTCOMES_MAX:
            truncation_notes.append(
                f"事件「{event_label}」可能结果超出 {_OUTCOMES_MAX} 条上限，丢弃 {len(slot_outcomes) - _OUTCOMES_MAX} 条"
            )
        if len(slot_plans) > _PLANS_MAX:
            truncation_notes.append(
                f"事件「{event_label}」应对预案超出 {_PLANS_MAX} 条上限，丢弃 {len(slot_plans) - _PLANS_MAX} 条"
            )
        # issue #68：预案槽位只采用研究核验/跟踪动作；结果槽位只采用事实性
        # 可能结果。含独立交易指令（含 T1 买卖编号、仓位/止损类，及「核验」
        # 前缀不豁免的指令）的条目在采用、缓存与派生证据之前拒绝并披露，
        # 不复制进任何可执行字段。有效事件、结果与合规跟踪动作保留。
        plans: List[str] = []
        rejected_plans = 0
        for position, raw_plan in enumerate(slot_plans[:_PLANS_MAX], start=1):
            if raw_plan is None:
                continue
            hit = _directive_hit(raw_plan)
            if hit:
                rejected_plans += 1
                truncation_notes.append(
                    f"事件「{event_label}」第 {position} 条预案含独立交易指令（命中「{hit}」），"
                    "未采用；事件应对为研究核验/跟踪动作，操作条件以本报告规则作战计划为准"
                )
                continue
            plans.append(_bounded(raw_plan, _EVENT_ITEM_LIMIT))
        outcomes: List[str] = []
        rejected_outcomes = 0
        for position, raw_outcome in enumerate(slot_outcomes[:_OUTCOMES_MAX], start=1):
            if raw_outcome is None:
                continue
            hit = _directive_hit(raw_outcome)
            if hit:
                rejected_outcomes += 1
                truncation_notes.append(
                    f"事件「{event_label}」第 {position} 条可能结果含独立交易指令"
                    f"（命中「{hit}」），未采用；仅保留事实性可能结果"
                )
                continue
            outcomes.append(_bounded(raw_outcome, _EVENT_ITEM_LIMIT))
        if rejected_plans or rejected_outcomes:
            logger.warning(
                "[DualTrack] 事件「%s」拒绝 %d 条独立交易预案、%d 条指令式结果（issue #68 契约）",
                event_label, rejected_plans, rejected_outcomes,
            )
        calendar.append(
            {
                "event": _bounded(item.get("event"), _EVENT_TEXT_LIMIT),
                "date": _bounded(item.get("date"), _EVENT_DATE_LIMIT),
                "outcomes": outcomes,
                "plans": plans,
            }
        )
    if dropped_events:
        truncation_notes.append(
            f"未来事件超出 {_CALENDAR_MAX} 项上限，丢弃 {dropped_events} 项"
        )
        logger.warning(
            "[DualTrack] 事件日历超上限，丢弃 %d 项（上限 %d）", dropped_events, _CALENDAR_MAX
        )
    for note in truncation_notes:
        logger.warning("[DualTrack] %s", note)
    dim = IntelDim(
        intelligence=intelligence,
        evidence_items=evidence,
        unverified_count=int(parsed.get("unverified_count") or 0),
        root_cause=root_cause,
        event_calendar=calendar,
        truncation_notes=truncation_notes,
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


def _generate_final_conclusion(
    llm_adapter: Any,
    stock_name: str,
    stock_code: str,
    dims: Dict[str, DimEnvelope],
    guardrail_events: List[GuardrailEvent],
) -> str:
    """终读结论（需求 3）：输入五段结构化事实，LLM 写 3-5 句投资人向总结。

    数字全部以 JSON 事实注入，LLM 禁止修改或新增数字；输出直接进报告段一与
    done 事件（每日通知消费同一字段）。失败回退空串（模板用信号一句话）。
    """
    import json
    import re
    from src.deep_research_dims.manager_writeup import INCOMPLETE_EVIDENCE_NOTE, manager_evidence_complete

    if not manager_evidence_complete(dims):
        return INCOMPLETE_EVIDENCE_NOTE

    signal = dims.get("signal")
    conclusion = dims.get("conclusion")
    plan = dims.get("plan")
    six_dim = dims.get("six_dim")
    intel = dims.get("intel")
    framework = getattr(six_dim, "framework", None)
    sniper = getattr(plan, "sniper_points", None)
    facts: Dict[str, Any] = {
        "stock": f"{stock_name}（{stock_code}）",
        "total_score": getattr(framework, "dimension_total", None),
        "rating": getattr(signal, "rating", None),
        "action": getattr(getattr(conclusion, "conclusion", None), "action", None),
        "ideal_buy": getattr(sniper, "ideal_buy", None),
        "stop_loss": getattr(sniper, "stop_loss", None),
        "take_profit": getattr(sniper, "take_profit", None),
        "dimension_summaries": {
            d.dimension: (d.indicators[0].summary if d.indicators else "")
            for d in (framework.dimensions if framework else [])
        },
        "root_cause_event": (getattr(intel, "root_cause", None).event if getattr(intel, "root_cause", None) else None),
        "guardrail_count": len(guardrail_events),
    }
    system = (
        "你是投研报告的主笔。基于输入的结构化事实，为该股票写一段 3-5 句的中文投资结论，"
        "面向普通投资人，要求：第一句给总评分与建议行动（用给定的行动词）；接着两个最关键论据"
        "（从维度总结中选）；然后一个最大风险；最后一句下一步关注点。只允许使用输入中的数字，"
        "禁止新增或修改任何数字；禁止 markdown 格式与项目符号。"
    )
    from src.llm.attempt import ENTRY_FINAL_CONCLUSION, attempt_scope

    try:
        with attempt_scope(ENTRY_FINAL_CONCLUSION, "final_read_summary"):
            resp = llm_adapter.call_text(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(facts, ensure_ascii=False, default=str)},
                ],
                temperature=0.3,
                timeout=120.0,
            )
        if getattr(resp, "provider", "") == "error" or getattr(resp, "finish_reason", None) in {"length", "content_filter"}:
            return ""
        text = (resp.content or "").strip()
        text = re.sub(
            r"<think(?:ing)?>.*?</think(?:ing)?>", "", text, flags=re.DOTALL | re.IGNORECASE
        ).strip()
        if len(text) < 30:
            return ""
        return text
    except Exception as exc:  # noqa: BLE001 - 结论失败只回退
        logger.warning("[DualTrack] 终读结论生成失败: %s", exc)
        return ""


def _write_reflection_journals(
    stock_code: str,
    stock_name: str,
    dims: Dict[str, DimEnvelope],
    report_id: Optional[str],
    write_cache: bool,
) -> None:
    """自反思日志（方案 v2.1 P2）：操作指令 + 评分快照落库。

    仅 web 全量路径落库（write_cache=True 且 report_id 非空）；批量桥接
    （write_cache=False / report_id=None）不写，避免日报噪音淹没回放样本。
    失败只记日志，绝不影响报告生成。
    """
    if not write_cache or not report_id:
        return
    try:
        import json as _json

        from src.storage import get_db

        signal = dims.get("signal")
        plan = dims.get("plan")
        six_dim = dims.get("six_dim")
        conclusion = dims.get("conclusion")
        sniper = getattr(plan, "sniper_points", None)
        framework = getattr(six_dim, "framework", None)
        get_db().save_recommendation_journal(
            {
                "stock_code": stock_code,
                "stock_name": stock_name,
                "action": str(getattr(getattr(conclusion, "conclusion", None), "action", None) or "观察"),
                "rating": str(getattr(signal, "rating", None) or "中性"),
                "score": float(getattr(framework, "dimension_total", 0.0) or 0.0) or None,
                "ideal_buy": getattr(sniper, "ideal_buy", None),
                "sell_price": getattr(sniper, "take_profit", None),
                "stop_loss": getattr(sniper, "stop_loss", None),
                "take_profit": getattr(sniper, "take_profit", None),
                "report_id": report_id,
                "scoring_version": getattr(six_dim, "scoring_version", None),
            }
        )
        if framework is not None:
            get_db().save_score_journal(
                {
                    "stock_code": stock_code,
                    "total_score": float(framework.dimension_total),
                    "dimensions_json": _json.dumps(
                        {
                            "dimension_total": framework.dimension_total,
                            "dimensions": [
                                {
                                    "dimension": d.dimension,
                                    "score": d.score,
                                    "weight": d.weight,
                                    "indicators": [
                                        {
                                            "name": i.name,
                                            "score": i.score,
                                            "weight": i.weight,
                                            "basis": i.basis,
                                        }
                                        for i in d.indicators
                                    ],
                                }
                                for d in framework.dimensions
                            ],
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                    "scoring_version": framework.scoring_version,
                    "report_id": report_id,
                }
            )
    except Exception as exc:  # noqa: BLE001 - 日志失败绝不影响主流程
        logger.warning("[DualTrack] 自反思日志写入失败: %s", exc)


def _coerce_researcher_value(key: str, value: Any) -> Any:
    """LLM 输出类型矫正：dict → 提取 summary/note 子字段或紧凑 JSON；score 类字段取数值。"""
    if value is None or isinstance(value, (str, int, float, bool, list)):
        return value
    if isinstance(value, dict):
        if key.endswith("score") or key == "score":
            for sub in ("score", "value", "data"):
                if isinstance(value.get(sub), (int, float)):
                    return value[sub]
            return None
        for sub in ("summary", "note", "text", "reason"):
            if isinstance(value.get(sub), str):
                return value[sub]
        import json as _json

        # 结构化字段（如业务画像 products/position）整存 JSON；截断会制造无效
        # JSON（cn 过滤器无法解析、投资人看到裸 JSON），上限给足常规研报粒度。
        return _json.dumps(value, ensure_ascii=False)[:2000]
    return str(value)


def _parse_researcher(dim_id: str, parsed: Dict[str, Any], steps: int) -> tuple[DimEnvelope, int]:
    """5 新研究员通用解析：字段过滤 + 类型矫正进契约（LLM 多余键丢弃）。"""
    model = DIM_MODELS[dim_id]
    payload = {
        k: _coerce_researcher_value(k, v)
        for k, v in (parsed or {}).items()
        if k in set(model.model_fields)
    }
    payload["status"] = "ok"
    return model(**payload), steps


# 研究员注册表驱动（src/deep_research/researchers/）：新增研究员 = 新模块 + registry 记录。
# 注意：注册表在 run_dual_track 内延迟导入（防循环：researchers → explore_agents → 本包 → researchers）


def _run_dual_track(
    stock_code: str,
    stock_name: str,
    llm_adapter: Any,
    progress_callback: ProgressCb = None,
    explore_max_steps: int = 8,
    force_refresh: bool = False,
    dims_filter: Optional[set] = None,
    skip_narrations: bool = False,
    write_cache: bool = True,
    cache_exclude: Optional[frozenset] = None,
    report_id: Optional[str] = None,
) -> DualTrackResult:
    """执行一次双轨深度投研分析。"""
    result = DualTrackResult()
    run = current_research.get()
    dims: Dict[str, DimEnvelope] = run.dimensions if run is not None else {}
    _perf_t0 = time.time()
    _perf_marks: List[Tuple[str, float]] = []

    def _perf_mark(label: str) -> None:
        _perf_marks.append((label, time.time() - _perf_t0))

    # 维度子集（省钱模式）：未选维度标 skipped，不执行、不入缓存
    selected = expand_dim_selection(dims_filter)
    skipped_ids = [d for d in ACTIVE_DIM_IDS if d not in selected]
    for _d in skipped_ids:
        _emit(progress_callback, {"type": "dim_done", "dim": _d, "status": "skipped"})

    def emit_dim(dim_id: str, status: str) -> None:
        _emit(progress_callback, {"type": "dim_done", "dim": dim_id, "status": status})

    def start_dim(dim_id: str) -> None:
        if run is not None:
            run.check()
        _emit(progress_callback, {"type": "dim_start", "dim": dim_id})

    from src.deep_research_dims.dim_cache import (
        CACHEABLE_DIMS,
        load_cached_dim,
        save_cached_dim,
    )
    from src.schemas.deep_research_dims import parse_dim as _parse_dim

    context_dims = {"data", "technical", "capital", "fundamental", "sector", "six_dim", "scenarios"}

    def try_cache(dim_id: str) -> Optional[DimEnvelope]:
        """命中未过期缓存则直接载入该维度（force_refresh 时跳过）。

        issue #68：intel 缓存读取须与本次已采用行情身份核对，变价/跨观测时点/
        无行情混用一律 miss 重算；身份不足（None）不匹配任何记录。
        """
        if force_refresh or dim_id not in CACHEABLE_DIMS or dim_id in context_dims:
            return None
        identity = quote_market_identity(ctx.quote) if dim_id == "intel" else None
        payload = load_cached_dim(stock_code, dim_id, quote_identity=identity)
        if payload is None:
            return None
        try:
            dim = _parse_dim(dim_id, payload)
        except (ValidationError, ValueError, TypeError):
            return None
        _emit(
            progress_callback,
            {"type": "thinking", "step": 1, "message": f"维度「{dim_id}」命中缓存（未过期，跳过计算）"},
        )
        emit_dim(dim_id, dim.status)
        return dim

    def store_cache(dim_id: str, dim: DimEnvelope) -> None:
        """成功（非降级）的缓存类维度落盘，供同票复用。

        write_cache=False（批量桥接场景）：只读缓存不写。
        cache_exclude：按维度排除——如批量场景跳过情景维度（其叙述为空，固化后
        web 端缓存命中会拿到无叙述 payload）；可复用探索资料允许写入，
        行情派生维度每次按当前快照计算。
        issue #68：intel 保存携带本报告已采用行情身份；身份不足时不落盘
        （下次无法核验复用身份，宁重算不伪造），无行情记 absent 身份。
        """
        if not write_cache or run is not None and (run.stop_reason is not None or run.remaining() <= 0):
            return
        if cache_exclude and dim_id in cache_exclude:
            return
        if dim.status == "ok" and dim_id in CACHEABLE_DIMS and dim_id not in context_dims:
            identity = quote_market_identity(ctx.quote) if dim_id == "intel" else None
            if dim_id == "intel" and identity is None:
                logger.warning(
                    "[DualTrack] 情报维度不落缓存：行情身份不足（有价但缺来源/观测时点），无法核验复用身份"
                )
                return
            save_cached_dim(stock_code, dim_id, dim.model_dump(), quote_identity=identity)

    # ---- 阶段 0 + 前置 S2 ----
    _emit(progress_callback, {"type": "thinking", "step": 0, "message": "装配共享数据快照（行情/日线/基本面/历史）..."})
    from data_provider.mx_data_adapter import financial_refresh
    from uuid import uuid4
    refresh_marker = (stock_code, uuid4().hex) if force_refresh else None
    refresh_token = financial_refresh.set(refresh_marker) if refresh_marker else None
    try:
        if run is not None:
            run.check()
        ctx = build_shared_context(stock_code, stock_name)
        if run is not None:
            run.context = ctx
    finally:
        if refresh_token is not None:
            financial_refresh.reset(refresh_token)
    _perf_mark("stage0_context")

    _emit(progress_callback, {"type": "thinking", "step": 0, "message": "计算数据透视（均线/量比/支撑阻力）..."})
    # ---- 波次 1：探索 Agent 线程池 + 主线程纯规则 ----
    _emit(progress_callback, {"type": "thinking", "step": 1, "message": "并行执行情报/产业链探索与规则维度..."})

    if run is not None:
        run.check()
    steps_total = 0

    def _run_rule_dims() -> None:
        if "phase" in selected:
            start_dim("phase")
            phase_dim = build_phase_dim(ctx)
            dims["phase"] = phase_dim
            emit_dim("phase", phase_dim.status)
        if "history" in selected:
            start_dim("history")
            history_dim = build_history_dim(ctx)
            if history_dim.status == "ok" and not skip_narrations:
                history_dim = history_dim.model_copy(
                    update={
                        "narrative": narrate(
                            llm_adapter, "history", history_dim.model_dump(), history_dim.narrative
                        )
                    }
                )
            dims["history"] = history_dim
            emit_dim("history", history_dim.status)
        if "fundamental" in selected:
            f1_dim = try_cache("fundamental")
            if f1_dim is None:
                start_dim("fundamental")
                f1_dim = build_fundamental_dim(ctx)
                store_cache("fundamental", f1_dim)
                dims["fundamental"] = f1_dim
                emit_dim("fundamental", f1_dim.status)
            else:
                dims["fundamental"] = f1_dim

    def _build_data_dim() -> Optional["DataDim | DimEnvelope"]:
        """S2 数据透视：缓存优先；fresh 时 LLM 叙述（与探索 Agent 并行，叙述耗时隐藏）。

        返回类型实际可能是 ``DimEnvelope``（缓存命中时）或 ``DataDim``（fresh 时）。
        类型注解用 Union 让静态检查通过；运行时 isinstance 校验放在调用点。
        """
        if "data" not in selected:
            return None
        dim = try_cache("data")
        if dim is None:
            start_dim("data")
            dim = build_data_dim(ctx)
            if dim.status == "ok" and not skip_narrations:
                dim = dim.model_copy(
                    update={
                        "narrative": narrate(
                            llm_adapter, "data", dim.model_dump(), dim.narrative
                        )
                    }
                )
            store_cache("data", dim)
            dims["data"] = dim
            emit_dim("data", dim.status)
        else:
            dims["data"] = dim
        return dim

    # ---- 波次 1：八大研究员并行（5 线程，全局 LLM 信号量限流）+ 主线程规则维度 ----
    from src.deep_research.researchers import REGISTRY, RUNNER_NAMES, researcher_dim_ids

    active_researchers = [
        (d, globals()[RUNNER_NAMES[d]]) for d in researcher_dim_ids if d in selected
    ]
    if active_researchers:
        def run_researcher(runner: Callable[..., Dict[str, Any]], dim_id: str) -> Dict[str, Any]:
            token = financial_refresh.set(refresh_marker) if refresh_marker else None
            dim_token = research_dimension.set(dim_id)
            try:
                if run is not None:
                    run.check()
                start_dim(dim_id)
                return runner(stock_code, stock_name, llm_adapter, progress_callback, explore_max_steps, **({"research": ctx.fundamental.get("research")} if dim_id == "business" else {}))
            finally:
                research_dimension.reset(dim_token)
                if token is not None:
                    financial_refresh.reset(token)

        def collect_researcher(fut: Future[Dict[str, Any]], dim_id: str) -> None:
            nonlocal steps_total
            research = dict(ctx.fundamental.get("research") or {})
            try:
                out = run.wait_for(fut) if run is not None else fut.result(timeout=600)
                research.update(out.get("vendor_research") or {})
                if out.get("ok"):
                    if dim_id == "intel":
                        dim, used = _parse_intel(out["data"], int(out.get("steps") or 0))
                    elif dim_id == "supply_chain":
                        dim, used = _parse_supply_chain(out["data"], int(out.get("steps") or 0))
                    else:
                        dim, used = REGISTRY[dim_id].parser(out["data"], int(out.get("steps") or 0))
                    dims[dim_id] = dim
                    store_cache(dim_id, dim)
                    steps_total += used
                else:
                    dims[dim_id] = DIM_MODELS[dim_id](
                        status="degraded", degraded_reason=str(out.get("error"))
                    )
            except Exception as exc:  # noqa: BLE001
                dims[dim_id] = DIM_MODELS[dim_id](
                    status="degraded", degraded_reason=f"研究员异常: {exc}"
                )
            if run is not None:
                research.update(run.vendor_research.get(dim_id, {}))
            dims[dim_id] = restore_vendor_facts(dim_id, dims[dim_id], research)
            if run is not None and (run.evidence.get(dim_id) or run.facts.get(dim_id)):
                from src.schemas.deep_research_dims import parse_dim
                payload = dims[dim_id].model_dump()
                payload["retained_evidence"] = list(run.evidence.get(dim_id, []))
                payload["retained_facts"] = list(run.facts.get(dim_id, []))
                dims[dim_id] = parse_dim(dim_id, payload)
            emit_dim(dim_id, dims[dim_id].status)

        pool = ThreadPoolExecutor(max_workers=5)
        futures: Dict[Future[Dict[str, Any]], str] = {}
        try:
            for dim_id, runner in active_researchers:
                cached = try_cache(dim_id)
                if cached is not None:
                    dims[dim_id] = cached
                    continue
                _emit(progress_callback, {"type": "dim_queued", "dim": dim_id})
                futures[
                    pool.submit(
                        copy_context().run, run_researcher, runner, dim_id,
                    )
                ] = dim_id
            if run is not None:
                for future in futures:
                    run.track(future)

            data_dim = _build_data_dim()
            _run_rule_dims()

            for fut, dim_id in futures.items():
                collect_researcher(fut, dim_id)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
            # Preserve work already complete before freezing an interrupted report.
            for fut, dim_id in futures.items():
                if dim_id not in dims and fut.done() and not fut.cancelled():
                    collect_researcher(fut, dim_id)
    else:
        data_dim = _build_data_dim()
        _run_rule_dims()

    if run is not None:
        run.check()
    _perf_mark("wave1_done")
    _reconcile_shared_history(ctx, dims)

    # ---- 波次 2：F2 板块（带产业链定位文本）→ L1 六维 v2 → L2 贝叶斯 ----
    if "sector" in selected:
        f2_dim = try_cache("sector")
        if f2_dim is None:
            start_dim("sector")
            sc_model = getattr(dims.get("supply_chain"), "supply_chain", None)
            f2_dim = build_sector_dim(
                ctx, str(getattr(sc_model, "company_position", "") or "") or None
            )
            store_cache("sector", f2_dim)
            dims["sector"] = f2_dim
            emit_dim("sector", f2_dim.status)
        else:
            dims["sector"] = f2_dim

    if "six_dim" in selected:
        six_dim = try_cache("six_dim")
        if six_dim is None:
            start_dim("six_dim")
            six_dim = build_six_dim(
                ctx,
                (data_dim.model_dump() if data_dim else {}),
                llm_adapter,
                (dims.get("fundamental").model_dump() if "fundamental" in dims else None),
                (dims.get("sector").model_dump() if "sector" in dims else None),
                (dims.get("intel").model_dump() if "intel" in dims else None),
                (dims.get("technical").model_dump() if "technical" in dims else None),
                (dims.get("capital").model_dump() if "capital" in dims else None),
                (dims.get("sentiment").model_dump() if "sentiment" in dims else None),
            )
            store_cache("six_dim", six_dim)
            dims["six_dim"] = six_dim
            emit_dim("six_dim", six_dim.status)
        else:
            dims["six_dim"] = six_dim

    if "bayesian" in selected:
        _emit(progress_callback, {"type": "thinking", "step": 2, "message": "贝叶斯证据链计算（LR 标定 + 行业基率锚点 + 后验更新）..."})
        from src.deep_research_dims.industry_base_rate import lookup_base_rate

        sc_model = getattr(dims.get("supply_chain"), "supply_chain", None)
        hint_text = " ".join(
            part
            for part in (
                str(ctx.fundamental.get("industry_hint") or ""),
                str(getattr(sc_model, "company_position", "") or ""),
                stock_name,
            )
            if part
        )
        market_implied_p, market_implied_basis = lookup_base_rate(hint_text)
        evidence_dicts = [
            e.model_dump() for e in getattr(dims.get("intel"), "evidence_items", [])
        ]
        start_dim("bayesian")
        bayesian_dim = build_bayesian_dim(
            dims["six_dim"].model_dump(),
            evidence_dicts,
            market_implied_p=market_implied_p,
            market_implied_basis=market_implied_basis,
        )
        dims["bayesian"] = bayesian_dim
        emit_dim("bayesian", bayesian_dim.status)

    if run is not None:
        run.check()
    _perf_mark("wave2_done")

    # ---- 波次 3：L5 → L3 → S4 → S1 ----
    _emit(progress_callback, {"type": "thinking", "step": 3, "message": "合成情景/结论/计划/信号（规则 + 护栏）..."})
    current_price = ctx.quote.get("price")

    scenarios_from_cache = False
    scenarios_dim = None
    if "scenarios" in selected:
        scenarios_dim = try_cache("scenarios")
        if scenarios_dim is None:
            start_dim("scenarios")
            # issue #64 §2.2：接入同报告已有证据（规则装配），支撑合法非估值分支；
            # 缓存/无证据时传 None 语义不变。
            scenarios_dim = build_scenarios_dim(
                ctx,
                float(current_price) if isinstance(current_price, (int, float)) else None,
                _qualitative_evidence_pack(dims),
                llm_adapter,
            )
        else:
            scenarios_from_cache = True
        dims["scenarios"] = scenarios_dim  # 先行入册，供 plan/signal 构建读取

    conclusion_dim = None
    if "conclusion" in selected:
        start_dim("conclusion")
        conclusion_dim = build_conclusion_dim(
            ctx,
            dims["six_dim"].model_dump(),
            dims["bayesian"].model_dump(),
            scenarios_dim.model_dump() if scenarios_dim else {},
        )
        dims["conclusion"] = conclusion_dim
        emit_dim("conclusion", conclusion_dim.status)

    plan_dim = None
    if "plan" in selected:
        position_suggestion = (
            getattr(dims["bayesian"].bayesian, "position_suggestion", None) or "观察"
        )
        start_dim("plan")
        plan_dim = build_plan_dim(ctx, dims["data"].model_dump(), position_suggestion)
        dims["plan"] = plan_dim
        emit_dim("plan", plan_dim.status)

    signal_dim = None
    if "signal" in selected:
        start_dim("signal")
        signal_dim = build_signal_dim(
            ctx,
            dims["conclusion"].model_dump(),
            dims["scenarios"].model_dump(),
        )
        dims["signal"] = signal_dim
        emit_dim("signal", signal_dim.status)

    if scenarios_dim is not None and not scenarios_from_cache:
        store_cache("scenarios", scenarios_dim)
        emit_dim("scenarios", scenarios_dim.status)
    if conclusion_dim is not None:
        dims["conclusion"] = conclusion_dim
    if plan_dim is not None:
        dims["plan"] = plan_dim
    if signal_dim is not None:
        dims["signal"] = signal_dim

    # 未选维度（省钱模式）补 skipped 占位，供成文与契约一致
    for _dim_id in skipped_ids:
        if _dim_id not in dims:
            dims[_dim_id] = DIM_MODELS[_dim_id](
                status="skipped", degraded_reason="省钱模式未选择该维度"
            )

    _perf_mark("wave3_done")

    if run is not None:
        run.check()
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

    _perf_mark("guardrail_done")

    if run is not None:
        run.check()
    # ---- 终读结论（需求 3：通读报告级总结；数字注入式，批量/无 LLM 时回退空串）----
    final_conclusion = ""
    if not skip_narrations and llm_adapter is not None:
        final_conclusion = _generate_final_conclusion(
            llm_adapter, stock_name, stock_code, dims, events
        )
    result.final_conclusion = final_conclusion

    # ---- 经理终稿（研报体 v2：观点式标题 + 内容概括 + 分节叙事；3 波独立容错）----
    manager_writeup: Dict[str, Any] = {}
    if not skip_narrations and llm_adapter is not None:
        try:
            from src.deep_research_dims.manager_writeup import generate_manager_writeup

            manager_writeup = generate_manager_writeup(
                llm_adapter, stock_name, stock_code, dims, events
            )
        except Exception as exc:  # noqa: BLE001 - 终稿失败只降级
            logger.warning("[DualTrack] 经理终稿生成失败: %s", exc)
    result.manager_writeup = manager_writeup

    # ---- 成文 ----
    _emit(progress_callback, {"type": "thinking", "step": 4, "message": "生成双轨投研报告（模板直灌）..."})
    view = build_view(
        stock_name, stock_code, ctx.as_of, ctx.limitations, dims, events,
        report_id=report_id or "",
        final_conclusion=final_conclusion,
        manager=manager_writeup,
    )
    markdown = render_markdown(view)
    missing = validate_structure(markdown)
    if missing:
        markdown = (
            f"> ⚠️ 报告结构不完整，缺失章节：{', '.join(missing)}\n\n" + markdown
        )

    executed = [d for d, m in dims.items() if m.status != "skipped"]
    degraded = [d for d, m in dims.items() if m.status == "degraded"]
    ok_count = len(executed) - len(degraded)
    valid = _has_useful_data(dims)
    status = "success" if not degraded else "partial" if valid else "failed"

    result.success = status != "failed" and bool(markdown.strip())
    result.status = status
    result.markdown = markdown
    result.dims = dims
    result.dims_payload = {d: m.model_dump(mode="json") for d, m in dims.items()}
    result.total_steps = steps_total
    result.provider = ""
    result.error = None if result.success else "维度降级过多，报告不可用"
    result.usage = summarize_attempts(
        run.attempts,
        origin_run_id=run.run_id if run is not None else None,
        origin_report_id=report_id,
    )
    result.usage["attempt_records"] = attempts_detail(run.attempts)
    result.total_tokens = known_total_tokens(result.usage)
    _write_reflection_journals(
        stock_code, stock_name, dims, report_id, write_cache
    )
    _perf_mark("render_done")
    _perf_summary = " ".join(
        f"{name}={t:.1f}s" for name, t in _perf_marks
    )
    logger.info(
        "[DualTrack][perf] total=%.1fs stages: %s", time.time() - _perf_t0, _perf_summary
    )
    return result


def _qualitative_evidence_pack(dims: Dict[str, DimEnvelope]) -> Dict[str, Any]:
    """非估值模式的同报告证据包（issue #64 §2.2，规则装配，零新增模型轮次）。

    只从本报告**已完成（status=ok）**的维度提取实际内容（根因/业务画像/事件
    日历/风险提示/资金与技术观察），承载进情景维度的现有 narrative/catalysts/
    risks 字段；降级维度的残留载荷不作为合格证据。无可用研究时返回空包，
    情景维度按真实缺口降级。
    """
    pack: Dict[str, Any] = {}
    # 核心 gate（R7 最短收敛）：fundamental/capital 两个核心研究任一未正常完成
    # 时，非估值证据不充分——返回空包，情景维度按真实缺口降级。
    for core in ("fundamental", "capital"):
        core_dim = dims.get(core)
        if core_dim is None or core_dim.status != "ok":
            return {}
    intel = dims.get("intel")
    intel_ok = intel is not None and intel.status == "ok"
    root_cause = getattr(intel, "root_cause", None) if intel_ok else None
    if root_cause is not None and str(getattr(root_cause, "event", "") or "").strip():
        pack["industry_space"] = (
            f"盘面根因：{getattr(root_cause, 'event', '') or '未提取'}；"
            f"机制：{getattr(root_cause, 'mechanism', '') or '未提取'}；"
            f"量级：{getattr(root_cause, 'magnitude', '') or '未评估'}"
        )
    business = dims.get("business")
    if business is not None and business.status == "ok" and str(getattr(business, "narrative", "") or "").strip():
        pack["competitive_evolution"] = str(business.narrative)[:400]
    catalysts: List[str] = []
    for item in ((getattr(intel, "event_calendar", None) or []) if intel_ok else [])[:5]:
        label = str(getattr(item, "event", "") or "").strip()
        if label:
            catalysts.append(f"{label}（{getattr(item, 'date', '') or '日期未知'}）")
    if catalysts:
        pack["catalysts"] = catalysts
    risks: List[str] = [
        str(risk).strip()
        for risk in (
            (getattr(getattr(intel, "intelligence", None), "risk_alerts", None) or [])[:5]
            if intel_ok
            else []
        )
        if str(risk).strip()
    ]
    for dim_id, label in (("capital", "资金面"), ("technical", "技术面")):
        dim = dims.get(dim_id)
        if dim is not None and dim.status == "ok" and str(getattr(dim, "narrative", "") or "").strip():
            risks.append(f"{label}观察：{str(dim.narrative)[:160]}")
    if risks:
        pack["risks"] = risks
    return pack


def _reconcile_shared_history(ctx: SharedContext, dims: dict[str, Any]) -> None:
    """Adopt a same-session history published during this run before dependent dims."""
    from src.deep_research_dims.context import (
        HISTORY_FAILED_NOTE_PREFIX,
        HISTORY_MISSING_NOTE,
        STAGE_TIMEOUT_NOTE_SUFFIX,
    )
    from src.services.history_loader import adopt_published_history

    if not adopt_published_history(ctx):
        return
    # Adoption only resolves the history notes; quote/chip timeout notes stay.
    history_note = f"history {STAGE_TIMEOUT_NOTE_SUFFIX}"
    ctx.limitations[:] = [
        note for note in ctx.limitations
        if not (note.startswith(history_note) or note.startswith(HISTORY_MISSING_NOTE) or note.startswith(HISTORY_FAILED_NOTE_PREFIX))
    ]
    data = dims.get("data")
    if data is not None and data.status == "degraded":
        dims["data"] = build_data_dim(ctx)


def _has_useful_data(dims: dict[str, Any]) -> bool:
    """Use the same acquired-data criterion for normal and interrupted reports."""
    for dim in dims.values():
        if dim.status == "skipped":
            continue
        if dim.status == "ok" or dim.retained_evidence or dim.retained_facts:
            return True
        # 已验证供应商概念名（issue #51）是已取得事实：足以让部分报告成文留存，
        # 不抬升任何评分或投资结论；仅有错误码、无概念名不算可用数据。
        if getattr(getattr(dim, "concept_membership", None), "concepts", None):
            return True
        if any(getattr(dim, field, None) for field in ("business_model", "main_products")):
            return True
        if any(getattr(getattr(dim, field, None), "items", []) for field in ("product_segments",)):
            return True
        # B6（规格 §1）：业务画像叙述中留存的 Choice 公司原始事实（控制人/带报告
        # 期股东名单）是已取得事实——足以让部分报告成文留存，不抬升评分或结论。
        if "公司资料（Choice 原始字段留存" in str(getattr(dim, "narrative", "") or ""):
            return True
    financial = dims.get("fundamental")
    if financial is None or financial.status == "skipped":
        return False
    return bool(
        financial.data_quality and financial.data_quality.obtained_count > 0
        or any(field.reading is not None for field in financial.financial_snapshot.values())
        or financial.valuation_detail.get("market_cap") is not None
        or financial.financial_safety.get("leverage") is not None
    )


def restore_vendor_facts(dim_id: str, dim: DimEnvelope, research: dict[str, Any]) -> DimEnvelope:
    """Restore only supplier fields after failed research, keeping strict validation.

    B6（规格 §1）：阶段0已取得的公司原始事实（控制人、带报告期十大股东名单）
    在中断/失败路径同样随业务画像资料区留存——与正常路径同一最小投影，不新取
    数、不推断控制链、不评分。
    """
    from src.schemas.deep_research_dims import parse_dim
    if dim_id != "business" or dim.status == "ok":
        return dim
    payload = dim.model_dump()
    company = research.get("company") or {}
    if dim_id == "business":
        for field, key in (("business_model", "MAINBUSINESS"), ("main_products", "MAINPRODUCT")):
            if company.get(key):
                payload[field] = {"value": company[key], "source": f"Choice {key}，截至 {research.get('as_of')}"}
        if research.get("product_segments"):
            payload["product_segments"] = research["product_segments"]
        fact_parts = []
        if company.get("AHOLDER"):
            fact_parts.append(
                f"实际控制人（Choice 原始字段，截至 {research.get('as_of') or '未标注'}）：{company['AHOLDER']}"
            )
        holders = research.get("holders") or {}
        holder_names = holders.get("HOLDNAMER")
        if isinstance(holder_names, str) and holder_names.strip():
            names = "；".join(name.strip() for name in holder_names.split(";") if name.strip())
            fact_parts.append(
                f"十大股东名单（Choice 原始字段，报告期 {holders.get('period') or '未标注'}）：{names}"
            )
        if fact_parts:
            payload["narrative"] = (
                str(payload.get("narrative") or "").rstrip()
                + "\n\n公司资料（Choice 原始字段留存，未核实控制链）："
                + "；".join(fact_parts)
                + "。"
            )
    try:
        return parse_dim(dim_id, payload)
    except (ValidationError, ValueError, TypeError) as exc:
        logger.warning("Retained vendor evidence did not pass %s contract: %s", dim_id, type(exc).__name__)
    return dim


def _interrupted_result(run: ResearchRun, stock_code: str, stock_name: str, selected: set[str], report_id: str | None) -> DualTrackResult:
    """Freeze completed results and render without starting any new model/tool work."""
    from datetime import datetime
    from src.schemas.deep_research_dims import parse_dim
    dims = dict(run.dimensions)
    ctx = run.context or SharedContext(stock_code, stock_name, datetime.now().isoformat(timespec="seconds"))
    # Stage zero can have facts even if cancellation preceded rule dimension assembly.
    if "fundamental" in selected and "fundamental" not in dims:
        dims["fundamental"] = build_fundamental_dim(ctx).model_copy(update={"status": "degraded", "degraded_reason": run.reason_text})
    if "data" in selected and "data" not in dims:
        from src.core.research_run import retained_tool_facts
        facts = retained_tool_facts("get_realtime_quote", ctx.quote)
        facts += retained_tool_facts("get_daily_history", {"data": ctx.history})
        # 中断报告同样保留阶段 0 已取得的概念归属（issue #51）。
        dims["data"] = DIM_MODELS["data"](
            status="degraded",
            degraded_reason=run.reason_text,
            retained_facts=facts,
            concept_membership=adopt_concept_membership(ctx),
        )
    for dim_id in ACTIVE_DIM_IDS:
        if dim_id not in dims:
            dims[dim_id] = DIM_MODELS[dim_id](
                status="degraded" if dim_id in selected else "skipped",
                degraded_reason=run.reason_text if dim_id in selected else "本次分析范围未选择",
                retained_evidence=run.evidence.get(dim_id, []),
                retained_facts=run.facts.get(dim_id, []),
            )
        elif dim_id in selected and (run.evidence.get(dim_id) or run.facts.get(dim_id)):
            payload = dims[dim_id].model_dump()
            payload["retained_evidence"] = run.evidence.get(dim_id, [])
            existing_facts = payload.get("retained_facts", [])
            payload["retained_facts"] = [
                *existing_facts,
                *(fact for fact in run.facts.get(dim_id, []) if fact not in existing_facts),
            ][-30:]
            dims[dim_id] = parse_dim(dim_id, payload)
    for dim_id in selected & {"business"}:
        research = {**(ctx.fundamental.get("research") or {}), **run.vendor_research.get(dim_id, {})}
        dims[dim_id] = restore_vendor_facts(dim_id, dims[dim_id], research)
    events, adjusted = apply_guardrails({key: value.model_dump() for key, value in dims.items()})
    dims = {key: parse_dim(key, value) for key, value in adjusted.items()}
    view = build_view(stock_name, stock_code, ctx.as_of, ctx.limitations, dims, events, report_id=report_id or "")
    markdown = render_markdown(view)
    valid = _has_useful_data(dims)
    interrupted_usage = summarize_attempts(run.attempts, origin_run_id=run.run_id, origin_report_id=report_id)
    interrupted_usage["attempt_records"] = attempts_detail(run.attempts)
    return DualTrackResult(success=bool(valid), status="partial" if valid else "failed", markdown=markdown if valid else "",
                           dims=dims, dims_payload={key: value.model_dump(mode="json") for key, value in dims.items()},
                           guardrail_events=events, error=run.reason_text,
                           usage=interrupted_usage, total_tokens=known_total_tokens(interrupted_usage))


def run_dual_track(
    stock_code: str, stock_name: str, llm_adapter: Any, progress_callback: ProgressCb = None,
    explore_max_steps: int = 8, force_refresh: bool = False, dims_filter: Optional[set[str]] = None,
    skip_narrations: bool = False, write_cache: bool = True,
    cache_exclude: Optional[frozenset[str]] = None, report_id: Optional[str] = None,
) -> DualTrackResult:
    from src.config import get_config
    existing = current_research.get()
    run = existing or ResearchRun(get_config().deep_research_timeout_seconds)
    with run.bind():
        try:
            result = _run_dual_track(stock_code, stock_name, llm_adapter, progress_callback,
                                     explore_max_steps, force_refresh, dims_filter, skip_narrations,
                                     write_cache, cache_exclude, report_id)
            run.check()
            return result
        except ResearchStopped:
            return _interrupted_result(run, stock_code, stock_name, expand_dim_selection(dims_filter), report_id)
        finally:
            if existing is None:
                run.finish()
