# -*- coding: utf-8 -*-
"""
===================================
A股自选股智能分析系统 - AI分析层
===================================

职责：
1. 封装 LLM 调用逻辑（通过 LiteLLM 统一调用 Gemini/Anthropic/OpenAI 等）
2. 结合技术面和消息面生成分析报告
3. 解析 LLM 响应为结构化 AnalysisResult
"""

import json
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Optional, Dict, Any, List, Tuple, Callable, cast

import litellm
from json_repair import repair_json
from litellm.router import Router

from src.llm.concurrency import limited_completion
from src.agent.llm_adapter import (
    get_thinking_extra_body,
    resolve_fallback_litellm_wire_models,
    register_fallback_model_pricing,
)
from src.agent.provider_trace import resolved_model_provider_identity
from src.agent.skills.defaults import CORE_TRADING_SKILL_POLICY_ZH
from src.config import (
    Config,
    extra_litellm_params,
    get_api_keys_for_model,
    get_config,
    get_configured_llm_models,
    normalize_litellm_temperature,
    resolve_litellm_wire_model,
    resolve_news_window_days,
)
from src.llm.generation_params import (
    apply_litellm_generation_params,
    apply_minimax_reasoning_params,
)
from src.llm.errors import call_litellm_with_param_recovery
from src.llm.usage import (
    attach_message_hmacs,
    extract_usage_payload,
    normalize_litellm_usage,
)
from src.storage import persist_llm_usage
from src.data.stock_mapping import STOCK_NAME_MAP
from src.report_language import (
    get_signal_level,
    get_no_data_text,
    get_placeholder_text,
    get_unknown_text,
    get_chip_unavailable_text,
    infer_decision_type_from_advice,
    is_chip_placeholder_value,
    localize_chip_health,
    localize_confidence_level,
    normalize_report_language,
)
from src.schemas.decision_action import build_action_fields
from src.schemas.report_schema import AnalysisReportSchema
from src.market_context import get_market_role, get_market_guidelines
from src.services.daily_market_context import format_daily_market_context_prompt_section
from src.market_phase_prompt import format_market_phase_prompt_section

logger = logging.getLogger(__name__)


def _normalize_risk_warning_values(value: Any) -> List[str]:
    """Normalize arbitrary risk_warning values into a flat list of text alerts."""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (list, tuple, set)):
        normalized: List[str] = []
        for item in value:
            normalized.extend(_normalize_risk_warning_values(item))
        return normalized
    if isinstance(value, dict):
        if not value:
            return []
        try:
            dumped = json.dumps(value, ensure_ascii=False)
            text = dumped.strip()
        except (TypeError, ValueError):
            text = str(value).strip()
        return [text] if text else []
    text = str(value).strip()
    return [text] if text else []


def _today_has_realtime_overlay(today: Any) -> bool:
    if not isinstance(today, dict):
        return False
    data_source = today.get("data_source") or today.get("dataSource")
    if isinstance(data_source, str) and data_source.startswith("realtime:"):
        return True
    if today.get("is_partial_bar") is True or today.get("isPartialBar") is True:
        return True
    if today.get("is_estimated") is True or today.get("isEstimated") is True:
        return True
    return bool(today.get("estimated_fields") or today.get("estimatedFields"))


def _today_looks_complete_daily_bar(
    context: Dict[str, Any],
    phase_context: Dict[str, Any],
) -> bool:
    today = context.get("today")
    if (
        not isinstance(today, dict)
        or today.get("close") in (None, "")
        or _today_has_realtime_overlay(today)
    ):
        return False

    effective_date = phase_context.get("effective_daily_bar_date")
    today_date = today.get("date") or today.get("trade_date") or context.get("date")
    if effective_date and today_date and str(today_date) != str(effective_date):
        return False
    return True


def _phase_aware_quote_labels(context: Dict[str, Any]) -> Tuple[str, str]:
    """Choose Chinese quote-table labels that do not conflict with phase context."""
    phase_context = context.get("market_phase_context")
    if not isinstance(phase_context, dict):
        return "今日行情", "收盘价"

    phase = str(phase_context.get("phase") or "").strip()
    if phase in {"premarket", "non_trading"}:
        today = context.get("today")
        if _today_looks_complete_daily_bar(context, phase_context):
            return "上一完整交易日行情", "上一完整交易日收盘价"
        if _today_has_realtime_overlay(today):
            return "最新行情", "实时估算价"
        if isinstance(today, dict) and today.get("close") not in (None, ""):
            return "最新行情", "最新价"
        return "今日行情", "收盘价"

    if (
        phase in {"intraday", "lunch_break", "closing_auction"}
        and phase_context.get("is_partial_bar") is True
    ):
        return "最新行情", "盘中估算价"

    return "今日行情", "收盘价"


def _should_hide_regular_session_ohlc(context: Dict[str, Any]) -> bool:
    phase_context = context.get("market_phase_context")
    if not isinstance(phase_context, dict):
        return False

    phase = str(phase_context.get("phase") or "").strip()
    return phase in {
        "premarket",
        "non_trading",
    } and not _today_looks_complete_daily_bar(
        context,
        phase_context,
    )


class _LiteLLMStreamError(RuntimeError):
    """Internal error wrapper that records whether any text was streamed."""

    def __init__(self, message: str, *, partial_received: bool = False):
        super().__init__(message)
        self.partial_received = partial_received


class _AllModelsFailedError(Exception):
    """Raised when every model in the fallback chain fails.

    This includes both LLM call errors and JSON parse errors (when a
    ``response_validator`` is provided to :meth:`GeminiAnalyzer._call_litellm`).

    The ``last_response_text`` attribute holds the raw text from the last model
    that *did* return a response (but whose JSON could not be validated), so
    callers can still attempt a best-effort text fallback.

    ``last_model`` and ``last_usage`` record the model name and token usage
    from the last attempt so callers can persist usage even on fallback.
    """

    def __init__(
        self,
        message: str,
        *,
        last_response_text: Optional[str] = None,
        last_model: Optional[str] = None,
        last_usage: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.last_response_text = last_response_text
        self.last_model = last_model
        self.last_usage = last_usage or {}


def check_content_integrity(
    result: "AnalysisResult",
    *,
    require_phase_decision: bool = False,
) -> Tuple[bool, List[str]]:
    """
    Check mandatory fields for report content integrity.
    Returns (pass, missing_fields). Module-level for use by pipeline (agent weak mode).
    """
    missing: List[str] = []

    def _is_blank_text(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, str):
            return not value.strip()
        return True

    def _is_invalid_risk_alerts(value: Any) -> bool:
        return not isinstance(value, list)

    def _is_invalid_stop_loss(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, (list, tuple, dict)):
            return True
        if isinstance(value, str):
            return not value.strip()
        return False

    if result.sentiment_score is None:
        missing.append("sentiment_score")
    advice = result.operation_advice
    if not advice or not isinstance(advice, str) or _is_blank_text(advice):
        missing.append("operation_advice")
    summary = result.analysis_summary
    if not summary or not isinstance(summary, str) or _is_blank_text(summary):
        missing.append("analysis_summary")
    dash = result.dashboard if isinstance(result.dashboard, dict) else {}
    core = dash.get("core_conclusion")
    core = core if isinstance(core, dict) else {}
    if _is_blank_text(core.get("one_sentence")):
        missing.append("dashboard.core_conclusion.one_sentence")
    intel = dash.get("intelligence")
    intel = intel if isinstance(intel, dict) else None
    if intel is None or _is_invalid_risk_alerts(intel.get("risk_alerts")):
        missing.append("dashboard.intelligence.risk_alerts")
    if result.decision_type in ("buy", "hold"):
        battle = dash.get("battle_plan")
        battle = battle if isinstance(battle, dict) else {}
        sp = battle.get("sniper_points")
        sp = sp if isinstance(sp, dict) else {}
        stop_loss = sp.get("stop_loss")
        if _is_invalid_stop_loss(stop_loss):
            missing.append("dashboard.battle_plan.sniper_points.stop_loss")
    if require_phase_decision:
        phase_decision = dash.get("phase_decision")
        phase_decision = phase_decision if isinstance(phase_decision, dict) else {}
        if not isinstance(phase_decision.get("phase_context"), dict):
            missing.append("dashboard.phase_decision.phase_context")
        if _is_blank_text(phase_decision.get("action_window")):
            missing.append("dashboard.phase_decision.action_window")
        if _is_blank_text(phase_decision.get("immediate_action")):
            missing.append("dashboard.phase_decision.immediate_action")
        if not isinstance(phase_decision.get("watch_conditions"), list):
            missing.append("dashboard.phase_decision.watch_conditions")
        if _is_blank_text(phase_decision.get("next_check_time")):
            missing.append("dashboard.phase_decision.next_check_time")
        if _is_blank_text(phase_decision.get("confidence_reason")):
            missing.append("dashboard.phase_decision.confidence_reason")
        if not isinstance(phase_decision.get("data_limitations"), list):
            missing.append("dashboard.phase_decision.data_limitations")
    return len(missing) == 0, missing


def apply_placeholder_fill(result: "AnalysisResult", missing_fields: List[str]) -> None:
    """Fill missing mandatory fields with placeholders (in-place). Module-level for pipeline."""

    def _is_blank_text(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, str):
            return not value.strip()
        return True

    def _is_invalid_risk_alerts(value: Any) -> bool:
        return not isinstance(value, list)

    def _is_invalid_stop_loss(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, (list, tuple, dict)):
            return True
        if isinstance(value, str):
            return not value.strip()
        return False

    report_language = normalize_report_language(
        getattr(result, "report_language", "zh")
    )
    placeholder = get_placeholder_text(report_language)
    phase_decision_placeholders = {
        "dashboard.phase_decision.action_window": (
            "Model did not provide a phase action window"
            if report_language == "en"
            else "模型未提供阶段化行动窗口"
        ),
        "dashboard.phase_decision.immediate_action": (
            "Model did not provide a phase-aware immediate action"
            if report_language == "en"
            else "模型未提供阶段化即时动作"
        ),
        "dashboard.phase_decision.next_check_time": (
            "Model did not provide a next check point"
            if report_language == "en"
            else "模型未提供下一次检查点"
        ),
        "dashboard.phase_decision.confidence_reason": (
            "Model did not provide a phase confidence rationale"
            if report_language == "en"
            else "模型未提供阶段化置信度理由"
        ),
    }
    for field in missing_fields:
        if field == "sentiment_score":
            result.sentiment_score = 50
        elif field == "operation_advice":
            if _is_blank_text(result.operation_advice):
                result.operation_advice = placeholder
        elif field == "analysis_summary":
            if _is_blank_text(result.analysis_summary):
                result.analysis_summary = placeholder
        elif field == "dashboard.core_conclusion.one_sentence":
            if not result.dashboard:
                result.dashboard = {}
            core = result.dashboard.get("core_conclusion")
            if not isinstance(core, dict):
                core = {}
                result.dashboard["core_conclusion"] = core
            fallback_sentence = (
                result.analysis_summary or result.operation_advice or placeholder
            )
            if _is_blank_text(core.get("one_sentence")):
                result.dashboard["core_conclusion"]["one_sentence"] = fallback_sentence
        elif field == "dashboard.intelligence.risk_alerts":
            if not result.dashboard:
                result.dashboard = {}
            intelligence = result.dashboard.get("intelligence")
            if not isinstance(intelligence, dict):
                intelligence = {}
                result.dashboard["intelligence"] = intelligence
            if _is_invalid_risk_alerts(intelligence.get("risk_alerts")):
                risk_warning_values = _normalize_risk_warning_values(
                    result.risk_warning
                )
                intelligence["risk_alerts"] = risk_warning_values
        elif field == "dashboard.battle_plan.sniper_points.stop_loss":
            if not result.dashboard:
                result.dashboard = {}
            battle_plan = result.dashboard.get("battle_plan")
            if not isinstance(battle_plan, dict):
                battle_plan = {}
                result.dashboard["battle_plan"] = battle_plan
            sniper_points = battle_plan.get("sniper_points")
            if not isinstance(sniper_points, dict):
                sniper_points = {}
                battle_plan["sniper_points"] = sniper_points
            if _is_invalid_stop_loss(sniper_points.get("stop_loss")):
                sniper_points["stop_loss"] = placeholder
        elif field.startswith("dashboard.phase_decision."):
            if not result.dashboard:
                result.dashboard = {}
            phase_decision = result.dashboard.get("phase_decision")
            if not isinstance(phase_decision, dict):
                phase_decision = {}
                result.dashboard["phase_decision"] = phase_decision
            if field == "dashboard.phase_decision.phase_context":
                if not isinstance(phase_decision.get("phase_context"), dict):
                    phase_decision["phase_context"] = {}
            elif field == "dashboard.phase_decision.watch_conditions":
                if not isinstance(phase_decision.get("watch_conditions"), list):
                    phase_decision["watch_conditions"] = []
            elif field == "dashboard.phase_decision.data_limitations":
                if not isinstance(phase_decision.get("data_limitations"), list):
                    phase_decision["data_limitations"] = []
            elif field in phase_decision_placeholders:
                if _is_blank_text(phase_decision.get(field.rsplit(".", 1)[-1])):
                    phase_decision[field.rsplit(".", 1)[-1]] = (
                        phase_decision_placeholders[field]
                    )


# ---------- chip_structure fallback (Issue #589) ----------

_CHIP_KEYS: tuple[str, ...] = (
    "profit_ratio",
    "avg_cost",
    "concentration",
    "chip_health",
)


def _is_value_placeholder(v: Any) -> bool:
    """True if value is empty or placeholder (N/A, 数据缺失, etc.)."""
    return is_chip_placeholder_value(v)


_RISK_WARNING_PLACEHOLDER_TEXTS = {
    "",
    "n/a",
    "na",
    "none",
    "null",
    "unknown",
    "tbd",
    "暂无",
    "待补充",
    "数据缺失",
    "未知",
    "无",
}

_STRUCTURAL_RISK_PHRASE_HINTS = (
    "重大利空",
    "重大风险",
    "关键风险",
    "减持",
    "高位减持",
    "退市",
    "退市风险",
    "停牌",
    "重大问询",
    "处罚",
    "限售",
    "违规",
    "违规风险",
    "诉讼",
    "问询",
    "监管",
    "财务",
    "审计",
    "爆雷",
    "暴雷",
    "违约",
    "违约风险",
    "流动性危机",
    "债务",
    "清算",
    "破产",
    "重大变脸",
    "major risk",
    "material adverse",
    "suspension",
    "delisting",
    "regulatory",
    "downgrade",
    "liquidity",
    "default",
)

_CAPITAL_FLOW_UNAVAILABLE_STATUS = {
    "not_supported",
    "not supported",
    "unsupported",
    "unavailable",
    "not_available",
    "not available",
    "none",
    "na",
    "n/a",
    "null",
    "missing",
}


def _is_meaningful_text(value: Any) -> bool:
    text = str(value).strip() if value is not None else ""
    if not text:
        return False
    lowered = text.strip().lower()
    return lowered not in _RISK_WARNING_PLACEHOLDER_TEXTS


def _safe_float(v: Any, default: float = 0.0) -> float:
    """Safely convert to float; return default on failure. Private helper for chip fill."""
    if v is None:
        return default
    if isinstance(v, (int, float)):
        try:
            return default if math.isnan(float(v)) else float(v)
        except (ValueError, TypeError):
            return default
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return default


def _coerce_chip_metric(v: Any) -> Optional[float]:
    """Convert chip metrics while preserving the distinction between missing and zero."""
    if v is None:
        return None
    try:
        numeric = float(v)
    except (TypeError, ValueError):
        try:
            numeric = float(str(v).strip())
        except (TypeError, ValueError):
            return None
    return None if math.isnan(numeric) else numeric


_BULLISH_TREND_HINTS: Tuple[str, ...] = (
    "多头排列",
    "持续上涨",
    "趋势向上",
    "上升趋势",
    "向上发散",
    "bullish",
    "uptrend",
)
_WEAK_BULLISH_TREND_HINTS: Tuple[str, ...] = ("弱势多头",)
_BEARISH_TREND_HINTS: Tuple[str, ...] = (
    "空头排列",
    "持续下跌",
    "趋势向下",
    "下降趋势",
    "向下发散",
    "bearish",
    "downtrend",
)
_WEAK_BEARISH_TREND_HINTS: Tuple[str, ...] = ("弱势空头",)
_NEGATION_TOKENS: Tuple[str, ...] = (
    "不是",
    "并非",
    "并未",
    "没有",
    "尚不",
    "尚未",
    "未",
    "无",
    "不属",
    "非",
    "not ",
    "no ",
)
_NEGATION_BREAK_CHARS: Tuple[str, ...] = (
    ",",
    ".",
    ";",
    ":",
    "!",
    "?",
    "，",
    "。",
    "；",
    "：",
    "！",
    "？",
    "\n",
)
_NEGATION_LOOKBACK_CHARS = 16
_NEGATION_MAX_GAP_CHARS = 8
_NEGATION_SCOPE_BREAK_TOKENS: Tuple[str, ...] = (
    "而是",
    "但是",
    "但",
    "反而",
    "反倒",
    "转为",
    "转成",
    "改为",
    "改成",
    " but ",
    " instead ",
    " rather ",
)
_SINGLE_CHAR_NEGATION_GAP_PREFIXES: Tuple[str, ...] = (
    "形成",
    "出现",
    "进入",
    "转为",
    "转成",
    "构成",
    "呈现",
    "显示",
    "属于",
    "是",
    "有",
    "能",
    "见",
    "站",
    "守",
    "破",
)


def _normalize_prompt_reason_items(items: Any) -> List[str]:
    """Normalize prompt reason/risk items into a clean string list."""
    if not isinstance(items, list):
        return []
    normalized: List[str] = []
    for item in items:
        text = str(item).strip()
        if text:
            normalized.append(text)
    return normalized


def _contains_trend_hint(text: str, hints: Tuple[str, ...]) -> bool:
    """Return True when text contains a non-negated strong trend hint."""
    lowered = text.strip().lower()

    def _has_negation_scope_break(gap: str) -> bool:
        normalized_gap = gap.lower()
        for token in _NEGATION_SCOPE_BREAK_TOKENS:
            token_index = normalized_gap.find(token)
            if token_index > 0:
                return True
        return False

    def _is_valid_negation_gap(token: str, gap: str) -> bool:
        if not gap:
            return True
        if token not in {"未", "无", "非"}:
            return True
        return any(
            gap.startswith(prefix) for prefix in _SINGLE_CHAR_NEGATION_GAP_PREFIXES
        )

    def _is_negated_match(index: int) -> bool:
        prefix = lowered[max(0, index - _NEGATION_LOOKBACK_CHARS) : index]
        for token in _NEGATION_TOKENS:
            token_index = prefix.rfind(token)
            if token_index < 0:
                continue
            gap = prefix[token_index + len(token) :]
            if any(char in gap for char in _NEGATION_BREAK_CHARS):
                continue
            stripped_gap = gap.strip()
            if len(stripped_gap) > _NEGATION_MAX_GAP_CHARS:
                continue
            if _has_negation_scope_break(stripped_gap):
                continue
            if not _is_valid_negation_gap(token, stripped_gap):
                continue
            return True
        return False

    for hint in hints:
        keyword = hint.lower()
        start = 0
        while True:
            index = lowered.find(keyword, start)
            if index < 0:
                break
            if not _is_negated_match(index):
                return True
            start = index + len(keyword)
    return False


def _infer_trend_direction(trend: Dict[str, Any]) -> str:
    """Infer the final trend direction from trend_status and ma_alignment."""
    combined = " ".join(
        str(trend.get(key, "")).strip()
        for key in ("trend_status", "ma_alignment")
        if str(trend.get(key, "")).strip()
    )
    if not combined:
        return "neutral"
    lowered = combined.lower()
    normalized = lowered.replace(" ", "")
    has_bullish = (
        _contains_trend_hint(combined, _BULLISH_TREND_HINTS + _WEAK_BULLISH_TREND_HINTS)
        or "ma5>ma10>ma20" in normalized
        or (
            "ma5>ma10" in normalized
            and any(pattern in normalized for pattern in ("ma10≤ma20", "ma10<=ma20"))
        )
    )
    has_bearish = (
        _contains_trend_hint(combined, _BEARISH_TREND_HINTS + _WEAK_BEARISH_TREND_HINTS)
        or "ma5<ma10<ma20" in normalized
        or (
            "ma5<ma10" in normalized
            and any(pattern in normalized for pattern in ("ma10≥ma20", "ma10>=ma20"))
        )
    )
    if has_bullish and not has_bearish:
        return "bullish"
    if has_bearish and not has_bullish:
        return "bearish"
    return "neutral"


def _candidate_json_open_positions(text: str) -> List[int]:
    """Return indices of ``{`` characters that look like JSON object starts.

    A candidate ``{`` is accepted if it is either:

    * the start of the text, optionally preceded by whitespace;
    * preceded by a JSON-structural character (``,`` ``:`` ``[`` ``{``)
      with only whitespace between;
    * preceded by a newline (a new "line" of thinking).

    Plain prose that mentions braces in mid-sentence (e.g. ``Start with
    { and end with }``) is filtered out so the bracket matcher does not
    latch onto it.
    """

    indices: List[int] = []
    for idx, ch in enumerate(text):
        if ch != "{":
            continue
        j = idx - 1
        while j >= 0 and text[j] in " \t":
            j -= 1
        if j < 0:
            indices.append(idx)
            continue
        prev = text[j]
        # Accept structural separators or new-line (likely a JSON object
        # starting on a fresh line in the LLM response).
        if prev in ",:[\n{":
            indices.append(idx)
    return indices


def _scan_json_object_end(text: str, open_idx: int) -> Optional[int]:
    """Return the index of the closing ``}`` that matches ``text[open_idx]``.

    Tracks nesting depth while ignoring characters inside JSON string
    literals (including escaped quotes). Returns ``None`` if no matching
    close brace is found before end-of-text.
    """

    in_string = False
    escape = False
    depth = 0
    for idx in range(open_idx, len(text)):
        ch = text[idx]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return idx
    return None


# Recognised scalar keys for the six_dimension_inputs schema. The LLM
# typically emits these in markdown bullet form inside its planning
# section before (or instead of) the final JSON.
_SIX_DIM_KEYS: Tuple[str, ...] = (
    "chain_position",
    "moat_type",
    "moat_strength",
    "customer_concentration",
    "us_china_risk",
    "us_china_impact",  # P5-fix: 宏观与地缘维度中美冲击（区别于 us_china_risk 产业链脱钩）
    "regulatory_risk",  # P5-fix: 宏观与地缘维度国内监管风险
    "chokepoint_type",
    "cognitive_difference",
    "recent_catalysts",
    "news_sentiment",
    "chip_concentration",
)

# Enum-like scalar fields whose value is a single token. We strip parenthetical
# comments (e.g. ``midstream (电子材料产业链中游)`` → ``midstream``).
_ENUM_LIKE_KEYS = frozenset(
    {
        "chain_position",
        "moat_type",
        "moat_strength",
        "us_china_risk",
        "us_china_impact",  # P5-fix
        "regulatory_risk",  # P5-fix
        "chokepoint_type",
        "cognitive_difference",
        "news_sentiment",
    }
)


def _coerce_six_dim_value(key: str, raw: str) -> Any:
    """Coerce a recovered six_dimension_inputs scalar to its target type."""

    text = raw.strip()
    if not text or text.lower() in {"null", "none", "n/a"}:
        return None
    if key == "customer_concentration":
        match = re.search(r"[-+]?\d*\.?\d+", text)
        return float(match.group()) if match else None
    if key == "chip_concentration":
        match = re.search(r"\d+(?:\.\d+)?", text)
        return float(match.group()) if match else None
    if key == "recent_catalysts":
        stripped = text.strip()
        # Treat ``[]`` literal as an empty list.
        if stripped in {"[]", "[ ]"}:
            return []
        # Bullets may be a single item or a JSON-ish list. We accept either.
        if stripped.startswith("[") and stripped.endswith("]"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    return [str(item).strip() for item in parsed if item is not None]
            except Exception:  # noqa: BLE001
                pass
        # Treat as a single string (the LLM often writes one event).
        return [text]
    if key in _ENUM_LIKE_KEYS:
        # 1. Strip parenthetical explanations: "midstream (注释)" → "midstream"
        head = text.split("(", 1)[0].strip().strip(",").strip().strip('"').strip()
        # 2. If the head still reads as prose (e.g. "midstream or upstream?
        #    They make infrared detectors"), pick the first recognised enum
        #    token; otherwise drop everything from the first sentence-ending
        #    punctuation.
        first_word = head.split(None, 1)[0].lower() if head else ""
        if first_word in _KNOWN_ENUM_TOKENS:
            return first_word
        tokens = re.findall(r"[a-z_]+", head.lower())
        for token in tokens:
            if token in _KNOWN_ENUM_TOKENS:
                return token
        # Fall back to the head clipped at the first sentence-ending marker.
        for marker in ("?", ".", "。", "!", ";", "\n"):
            idx = head.find(marker)
            if idx > 0:
                head = head[:idx].strip().strip(",").strip()
                break
        return head or None
    return text


# Canonical enum tokens recognised by ``score_supply_chain`` (see
# ``_score_chain_position`` and ``_score_moat`` in scoring/indicators/
# supply_chain.py). The recovery heuristic prefers these tokens when the
# LLM emits reasoning noise around the chosen value.
_KNOWN_ENUM_TOKENS = frozenset(
    {
        # chain_position
        "upstream",
        "bottleneck",
        "midstream",
        "downstream",
        "commodity",
        # moat_type
        "patent",
        "technology",
        "brand",
        "network",
        "switching_cost",
        "license",
        "regulatory",
        "multiple",
        # moat_strength / us_china_risk
        "strong",
        "moderate",
        "weak",
        "none",
        "high",
        "medium",
        "low",
        # chokepoint_type
        "capacity",
        "geo",
        "tech",
        "cert",
        # cognitive_difference
        "market_underestimating",
        "market_fair",
        "market_overestimating",
        # news_sentiment
        "positive",
        "neutral",
        "negative",
        # us_china_impact (P5-fix)
        "minimal",
        "limited",
        "significant",
        "severe",
        # regulatory_risk (P5-fix)
        # 注: low/medium/high/none 已在上面注册
    }
)


def _recover_six_dimension_inputs_from_prose(text: str) -> Optional[Dict[str, Any]]:
    """Reconstruct ``six_dimension_inputs`` from the LLM planning section.

    When the final JSON output is truncated or otherwise unparseable, the
    LLM often lists the same fields as markdown bullets earlier in its
    response (e.g. ``- chain_position: midstream``). This helper harvests
    those bullets and returns a dict compatible with the schema in the
    "长线六维·主观键值" prompt section.

    Returns ``None`` if fewer than two known keys can be recovered (the
    heuristic is conservative — we do not want to inject garbage).
    """

    if not text:
        return None

    # Locate the planning section. The LLM usually labels it
    # "Six Dimension Inputs" (or "6. Six Dimension Inputs:") followed by
    # bullets. We also accept a fenced "six_dimension_inputs: ..." block.
    section_start_candidates = []
    for marker in (
        "Six Dimension Inputs",
        "six_dimension_inputs",
        "**Six Dimension",
        "8. **Six",
    ):
        idx = text.find(marker)
        if idx >= 0:
            section_start_candidates.append(idx)
    if not section_start_candidates:
        return None

    start = min(section_start_candidates)
    section = text[start : start + 2000]
    # Stop at the first fenced JSON block or the first ``` marker — anything
    # after that belongs to the actual JSON payload, not the planning notes.
    fence_idx = section.find("```")
    if fence_idx > 0:
        section = section[:fence_idx]

    recovered: Dict[str, Any] = {}
    bullet_pattern = re.compile(
        r"^\s*[-*]\s*(?P<key>[a-z_]+)\s*[::]\s*(?P<value>.+?)\s*$",
        re.MULTILINE,
    )
    for match in bullet_pattern.finditer(section):
        key = match.group("key").strip()
        if key not in _SIX_DIM_KEYS:
            continue
        recovered[key] = _coerce_six_dim_value(key, match.group("value"))

    if len(recovered) < 2:
        return None
    return recovered


def _wrap_bare_six_dim_dict(
    parsed: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """If *parsed* looks like a bare six-dimension object, wrap it.

    The MiniMax-M3 thinking model occasionally emits the six-dimension
    values as a top-level dict that lacks the ``six_dimension_inputs``
    wrapper, e.g.::

        {"chain_position": "upstream", "moat_type": "technology", ...}

    instead of::

        {"six_dimension_inputs": {"chain_position": "upstream", ...}}

    Downstream code reads ``result.six_dimension_inputs``; without the
    wrapper the framework scoring falls back to the "数据缺失，使用中性分"
    placeholder. This helper detects the bare form (top-level keys all
    belong to the six-dimension schema) and returns a wrapped copy. It
    returns ``None`` when the dict contains only a handful of unrelated
    keys so we never wrap an unrelated sub-object by accident.
    """

    if "six_dimension_inputs" in parsed:
        return None
    known = sum(1 for key in parsed if key in _SIX_DIM_KEYS)
    other = sum(1 for key in parsed if key not in _SIX_DIM_KEYS)
    if known < 4 or other > 0:
        return None
    inner: Dict[str, Any] = {}
    for key in _SIX_DIM_KEYS:
        if key in parsed:
            inner[key] = parsed[key]
    return {
        "six_dimension_inputs": inner,
        "_recovered_from_bare_wrapper": True,
    }


def _find_and_wrap_bare_six_dim(
    text: str,
    best_parsed: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Scan *text* for a separate bare six-dim object and merge it.

    When the LLM emits two distinct JSON objects (the main dashboard plus
    a bare ``{"chain_position": ..., "moat_type": ..., ...}`` at the
    end), :func:`_extract_json_candidate` only returns the dashboard.
    This helper runs a secondary scan for any *other* parse candidate
    that looks like a bare six-dimension dict and merges it into the
    dashboard under the ``six_dimension_inputs`` key.

    Returns ``None`` when no suitable secondary object is found so the
    caller can fall back to its existing behaviour.
    """

    if "six_dimension_inputs" in best_parsed:
        return None

    primary_start, primary_end = _find_primary_object_span(text, best_parsed)
    if primary_start is None or primary_end is None:
        return None

    candidates = _candidate_json_open_positions(text)
    for open_idx in candidates:
        if primary_start <= open_idx <= primary_end:
            # Skip the primary candidate — we already know it does not
            # contain six_dimension_inputs (otherwise we'd not be here).
            continue
        match_end = _scan_json_object_end(text, open_idx)
        if match_end is None:
            continue
        try:
            parsed = json.loads(text[open_idx : match_end + 1])
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(parsed, dict):
            continue
        wrapped = _wrap_bare_six_dim_dict(parsed)
        if wrapped is not None:
            merged = dict(best_parsed)
            merged["six_dimension_inputs"] = wrapped["six_dimension_inputs"]
            merged["_recovered_from_bare_wrapper"] = True
            return merged
    return None


def _find_primary_object_span(
    text: str, parsed: Dict[str, Any]
) -> Tuple[Optional[int], Optional[int]]:
    """Locate the span of the JSON object that *parsed* was decoded from.

    Used by :func:`_find_and_wrap_bare_six_dim` to skip the primary
    candidate when scanning for a bare six-dim sibling object. We
    cannot rely on a single offset (the candidate may have been
    rebuilt by ``repair_json``), so we re-match by finding the longest
    candidate whose parse equals *parsed*.
    """

    candidates = _candidate_json_open_positions(text)
    best_span: Tuple[int, int] = (0, 0)
    for open_idx in candidates:
        match_end = _scan_json_object_end(text, open_idx)
        if match_end is None:
            continue
        span_len = match_end - open_idx + 1
        if span_len <= best_span[1] - best_span[0]:
            continue
        try:
            parsed_candidate = json.loads(text[open_idx : match_end + 1])
        except Exception:  # noqa: BLE001
            continue
        if parsed_candidate == parsed:
            best_span = (open_idx, match_end)
    if best_span == (0, 0):
        return (None, None)
    return best_span


def _filter_conflicting_trend_items(
    items: List[str], conflict_hints: Tuple[str, ...]
) -> List[str]:
    """Drop reasons that directly conflict with the final trend direction."""
    return [item for item in items if not _contains_trend_hint(item, conflict_hints)]


def _sanitize_trend_analysis_for_prompt(
    trend: Any,
    *,
    volume_change_ratio: Any = None,
) -> Dict[str, Any]:
    """Clean prompt-only trend hints on a derived copy without touching runtime/provider config."""
    trend_dict = dict(trend) if isinstance(trend, dict) else {}
    signal_reasons = _normalize_prompt_reason_items(trend_dict.get("signal_reasons"))
    risk_factors = _normalize_prompt_reason_items(trend_dict.get("risk_factors"))
    prompt_notes: List[str] = []
    trend_direction = _infer_trend_direction(trend_dict)

    if trend_direction == "bearish":
        filtered_signal_reasons = _filter_conflicting_trend_items(
            signal_reasons,
            _BULLISH_TREND_HINTS + _WEAK_BULLISH_TREND_HINTS,
        )
        if len(filtered_signal_reasons) != len(signal_reasons):
            prompt_notes.append(
                "当前技术结构偏空，已剔除与空头主判断直接冲突的看多结构理由。"
            )
        signal_reasons = filtered_signal_reasons
        prompt_notes.append(
            "若新闻、业绩或政策催化偏多，只能表述为“事件先行、技术待确认”或“基本面偏多，但技术面尚未确认”，严禁写成确定性买点。"
        )
    elif trend_direction == "bullish":
        filtered_signal_reasons = _filter_conflicting_trend_items(
            signal_reasons,
            _BEARISH_TREND_HINTS + _WEAK_BEARISH_TREND_HINTS,
        )
        if len(filtered_signal_reasons) != len(signal_reasons):
            prompt_notes.append(
                "当前技术结构偏多，已剔除与多头主判断直接冲突的空头结构理由。"
            )
        signal_reasons = filtered_signal_reasons
        filtered_risk_factors = _filter_conflicting_trend_items(
            risk_factors,
            _BEARISH_TREND_HINTS + _WEAK_BEARISH_TREND_HINTS,
        )
        if len(filtered_risk_factors) != len(risk_factors):
            prompt_notes.append(
                "当前技术结构偏多，已剔除与多头主判断直接冲突的空头结构风险表述。"
            )
        risk_factors = filtered_risk_factors

    parsed_volume_change = _safe_float(volume_change_ratio, default=math.nan)
    if math.isfinite(parsed_volume_change) and parsed_volume_change > 10:
        prompt_notes.append(
            f"成交量较昨日变化约 {parsed_volume_change:.2f} 倍，可能存在异常数据或一次性冲量；量能信号必须降权解读，不能机械视为强确认。"
        )

    trend_dict["signal_reasons"] = signal_reasons
    trend_dict["risk_factors"] = risk_factors
    trend_dict["prompt_consistency_notes"] = prompt_notes
    trend_dict["prompt_trend_direction"] = trend_direction
    return trend_dict


def _derive_chip_health(
    profit_ratio: float, concentration_90: float, language: str = "zh"
) -> str:
    """Derive chip_health from profit_ratio and concentration_90."""
    if profit_ratio >= 0.9:
        return localize_chip_health("警惕", language)  # 获利盘极高
    if concentration_90 >= 0.25:
        return localize_chip_health("警惕", language)  # 筹码分散
    if concentration_90 < 0.15 and 0.3 <= profit_ratio < 0.9:
        return localize_chip_health("健康", language)  # 集中且获利比例适中
    return localize_chip_health("一般", language)


def _build_chip_structure_from_data(
    chip_data: Any, language: str = "zh"
) -> Dict[str, Any]:
    """Build chip_structure dict from ChipDistribution or dict."""
    if hasattr(chip_data, "profit_ratio"):
        pr = _safe_float(chip_data.profit_ratio)
        ac = chip_data.avg_cost
        c90 = _safe_float(chip_data.concentration_90)
    else:
        d = chip_data if isinstance(chip_data, dict) else {}
        pr = _safe_float(d.get("profit_ratio"))
        ac = d.get("avg_cost")
        c90 = _safe_float(d.get("concentration_90"))
    chip_health = _derive_chip_health(pr, c90, language=language)
    return {
        "profit_ratio": f"{pr:.1%}",
        "avg_cost": ac if (ac is not None and _safe_float(ac) != 0.0) else "N/A",
        "concentration": f"{c90:.2%}",
        "chip_health": chip_health,
    }


def _has_meaningful_chip_data(chip_data: Any) -> bool:
    """Return True when chip data has the core metrics required for reporting."""
    if not chip_data:
        return False
    if hasattr(chip_data, "avg_cost"):
        avg_cost = _coerce_chip_metric(getattr(chip_data, "avg_cost", None))
        concentration_90 = _coerce_chip_metric(
            getattr(chip_data, "concentration_90", None)
        )
        concentration_70 = _coerce_chip_metric(
            getattr(chip_data, "concentration_70", None)
        )
    else:
        d = chip_data if isinstance(chip_data, dict) else {}
        avg_cost = _coerce_chip_metric(d.get("avg_cost"))
        concentration_90_value = d.get("concentration_90")
        if concentration_90_value is None:
            concentration_90_value = d.get("concentration")
        concentration_90 = _coerce_chip_metric(concentration_90_value)
        concentration_70 = _coerce_chip_metric(d.get("concentration_70"))
    return (
        avg_cost is not None
        and avg_cost > 0
        and (
            (concentration_90 is not None and concentration_90 >= 0)
            or (concentration_70 is not None and concentration_70 >= 0)
        )
    )


def _mark_chip_structure_unavailable(result: "AnalysisResult", language: str) -> None:
    if not result or not isinstance(result.dashboard, dict):
        return
    data_perspective = result.dashboard.get("data_perspective")
    if not isinstance(data_perspective, dict):
        return
    data_perspective["chip_structure"] = {}
    data_perspective["chip_unavailable_reason"] = get_chip_unavailable_text(language)


def normalize_chip_structure_availability(
    result: "AnalysisResult", chip_data: Any
) -> None:
    """Fill valid chip metrics or collapse placeholder-only chip fields to one fallback line."""
    if not result:
        return
    language = getattr(result, "report_language", "zh")
    if _has_meaningful_chip_data(chip_data):
        fill_chip_structure_if_needed(result, chip_data)
        return
    _mark_chip_structure_unavailable(result, language)


def fill_chip_structure_if_needed(result: "AnalysisResult", chip_data: Any) -> None:
    """When chip_data exists, fill chip_structure placeholder fields from chip_data (in-place)."""
    if not result or not _has_meaningful_chip_data(chip_data):
        return
    try:
        if not result.dashboard:
            result.dashboard = {}
        dash = result.dashboard
        # Use `or {}` rather than setdefault so that an explicit `null` from LLM is also replaced
        dp = dash.get("data_perspective") or {}
        dash["data_perspective"] = dp
        cs = dp.get("chip_structure") or {}
        filled = _build_chip_structure_from_data(
            chip_data,
            language=getattr(result, "report_language", "zh"),
        )
        # Start from a copy of cs to preserve any extra keys the LLM may have added
        merged = dict(cs)
        for k in _CHIP_KEYS:
            if _is_value_placeholder(merged.get(k)):
                merged[k] = filled[k]
        if merged != cs:
            dp["chip_structure"] = merged
            logger.info(
                "[chip_structure] Filled placeholder chip fields from data source (Issue #589)"
            )
    except Exception as e:
        logger.warning("[chip_structure] Fill failed, skipping: %s", e)


_PRICE_POS_KEYS = (
    "ma5",
    "ma10",
    "ma20",
    "bias_ma5",
    "bias_status",
    "current_price",
    "support_level",
    "resistance_level",
)


def fill_price_position_if_needed(
    result: "AnalysisResult",
    trend_result: Any = None,
    realtime_quote: Any = None,
) -> None:
    """Fill missing price_position fields from trend_result / realtime data (in-place)."""
    if not result:
        return
    try:
        if not result.dashboard:
            result.dashboard = {}
        dash = result.dashboard
        dp = dash.get("data_perspective") or {}
        dash["data_perspective"] = dp
        pp = dp.get("price_position") or {}

        computed: Dict[str, Any] = {}
        if trend_result:
            tr = (
                trend_result
                if isinstance(trend_result, dict)
                else (
                    trend_result.__dict__ if hasattr(trend_result, "__dict__") else {}
                )
            )
            computed["ma5"] = tr.get("ma5")
            computed["ma10"] = tr.get("ma10")
            computed["ma20"] = tr.get("ma20")
            computed["bias_ma5"] = tr.get("bias_ma5")
            computed["current_price"] = tr.get("current_price")
            support_levels = tr.get("support_levels") or []
            resistance_levels = tr.get("resistance_levels") or []
            if support_levels:
                computed["support_level"] = support_levels[0]
            if resistance_levels:
                computed["resistance_level"] = resistance_levels[0]
        if realtime_quote:
            rq = (
                realtime_quote
                if isinstance(realtime_quote, dict)
                else (
                    realtime_quote.to_dict()
                    if hasattr(realtime_quote, "to_dict")
                    else {}
                )
            )
            if _is_value_placeholder(computed.get("current_price")):
                computed["current_price"] = rq.get("price")

        filled = False
        for k in _PRICE_POS_KEYS:
            if _is_value_placeholder(pp.get(k)) and not _is_value_placeholder(
                computed.get(k)
            ):
                pp[k] = computed[k]
                filled = True
        if filled:
            dp["price_position"] = pp
            logger.info("[price_position] Filled placeholder fields from computed data")
    except Exception as e:
        logger.warning("[price_position] Fill failed, skipping: %s", e)


def stabilize_decision_with_structure(
    result: "AnalysisResult",
    trend_result: Any = None,
    fundamental_context: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Calibrate aggressive buy/sell advice with price levels and capital flow.

    The LLM can overreact to one-day price movement.  This guard keeps the
    public `decision_type` enum stable while allowing richer neutral wording
    such as 震荡/洗盘观察 when support, resistance, and fund flow do not confirm
    an immediate buy/sell action.
    """
    if not result:
        return

    try:
        language = normalize_report_language(getattr(result, "report_language", "zh"))
        dashboard = result.dashboard if isinstance(result.dashboard, dict) else {}
        data_perspective = (
            dashboard.get("data_perspective") if isinstance(dashboard, dict) else {}
        )
        if not isinstance(data_perspective, dict):
            data_perspective = {}
        price_position = data_perspective.get("price_position")
        if not isinstance(price_position, dict):
            price_position = {}

        trend_dict = _as_dict_for_decision_guard(trend_result)
        current_price = _first_numeric_value(
            getattr(result, "current_price", None),
            price_position.get("current_price"),
            trend_dict.get("current_price"),
        )
        support = _first_numeric_value(
            price_position.get("support_level"),
            _first_list_value(trend_dict.get("support_levels")),
        )
        resistance = _first_numeric_value(
            price_position.get("resistance_level"),
            _first_list_value(trend_dict.get("resistance_levels")),
        )
        decision_type = infer_decision_type_from_advice(
            getattr(result, "decision_type", ""),
            default=getattr(result, "decision_type", "hold") or "hold",
        )
        decision_type = (
            decision_type if decision_type in {"buy", "hold", "sell"} else "hold"
        )
        advice_decision_type = infer_decision_type_from_advice(
            getattr(result, "operation_advice", ""),
            default="",
        )

        flow_bias, flow_reason = _capital_flow_bias_with_status(fundamental_context)
        if flow_bias == "unavailable":
            if (
                isinstance(fundamental_context, dict)
                and "capital_flow" in fundamental_context
            ):
                if decision_type == "buy" or advice_decision_type == "buy":
                    _downgrade_buy_without_capital_flow(
                        result,
                        language,
                        current_price=current_price,
                        support=support,
                        resistance=resistance,
                        flow_status=flow_reason,
                    )
                else:
                    _set_decision_stability_unavailable(
                        result,
                        language,
                        current_price=current_price,
                        support=support,
                        resistance=resistance,
                        flow_status=flow_reason,
                    )
            return

        if current_price is None:
            return

        broke_support = support is not None and current_price < support * 0.985
        near_support = (
            support is not None
            and not broke_support
            and current_price <= support * 1.03
        )
        breakout = resistance is not None and current_price > resistance * 1.01
        near_resistance = (
            resistance is not None
            and not breakout
            and current_price >= resistance * 0.97
        )
        mid_range = (
            support is not None
            and resistance is not None
            and support * 1.03 < current_price < resistance * 0.97
        )

        has_significant_risk = _has_structural_risk_alert(result)

        if decision_type == "buy":
            if near_resistance and flow_bias != "inflow":
                _downgrade_to_structural_hold(
                    result,
                    language,
                    advice_key="range",
                    reason_key="buy_near_resistance",
                    current_price=current_price,
                    support=support,
                    resistance=resistance,
                    flow_bias=flow_bias,
                )
            elif flow_bias == "outflow" and not breakout:
                _downgrade_to_structural_hold(
                    result,
                    language,
                    advice_key="range",
                    reason_key="buy_with_outflow",
                    current_price=current_price,
                    support=support,
                    resistance=resistance,
                    flow_bias=flow_bias,
                )
            elif mid_range and flow_bias == "neutral":
                _downgrade_to_structural_hold(
                    result,
                    language,
                    advice_key="range",
                    reason_key="hold_mid_range",
                    current_price=current_price,
                    support=support,
                    resistance=resistance,
                    flow_bias=flow_bias,
                )
        elif decision_type == "sell":
            if near_support and (flow_bias != "outflow") and not has_significant_risk:
                _downgrade_to_structural_hold(
                    result,
                    language,
                    advice_key="shakeout",
                    reason_key="sell_near_support",
                    current_price=current_price,
                    support=support,
                    resistance=resistance,
                    flow_bias=flow_bias,
                )
            elif (
                flow_bias == "inflow" and not broke_support and not has_significant_risk
            ):
                _downgrade_to_structural_hold(
                    result,
                    language,
                    advice_key="hold",
                    reason_key="sell_with_inflow",
                    current_price=current_price,
                    support=support,
                    resistance=resistance,
                    flow_bias=flow_bias,
                )
        elif decision_type == "hold":
            change_pct = _first_numeric_value(getattr(result, "change_pct", None))
            if (
                change_pct is not None
                and change_pct < 0
                and near_support
                and flow_bias != "outflow"
            ):
                _set_structural_hold_wording(
                    result,
                    language,
                    advice_key="shakeout",
                    reason_key="hold_shakeout",
                    current_price=current_price,
                    support=support,
                    resistance=resistance,
                    flow_bias=flow_bias,
                )
            elif mid_range and flow_bias == "neutral":
                _set_structural_hold_wording(
                    result,
                    language,
                    advice_key="range",
                    reason_key="hold_mid_range",
                    current_price=current_price,
                    support=support,
                    resistance=resistance,
                    flow_bias=flow_bias,
                )
        _sync_stability_dashboard_fields(result)
    except Exception as exc:
        logger.warning("[decision_stability] skipped: %s", exc)


def _has_structural_risk_alert(result: "AnalysisResult") -> bool:
    dashboard = result.dashboard if isinstance(result.dashboard, dict) else {}

    risk_text = getattr(result, "risk_warning", "")
    if _is_significant_structural_risk(risk_text):
        return True

    intelligence = (
        dashboard.get("intelligence") if isinstance(dashboard, dict) else None
    )
    if isinstance(intelligence, dict):
        risk_alerts = intelligence.get("risk_alerts")
        if isinstance(risk_alerts, str):
            if _is_significant_structural_risk(risk_alerts):
                return True
        elif isinstance(risk_alerts, (list, tuple, set)):
            if any(_is_significant_structural_risk(item) for item in risk_alerts):
                return True

    core_conclusion = (
        dashboard.get("core_conclusion") if isinstance(dashboard, dict) else None
    )
    if isinstance(core_conclusion, dict):
        signal_type = str(core_conclusion.get("signal_type", "")).strip()
        if _is_significant_structural_risk(signal_type):
            return True
    return False


def _is_significant_structural_risk(value: Any) -> bool:
    text = str(value or "").strip()
    if not _is_meaningful_text(text):
        return False

    normalized = text.lower()
    if any(keyword in normalized for keyword in _STRUCTURAL_RISK_PHRASE_HINTS):
        return True

    return "重大" in text and "风险" in normalized


def _sync_stability_dashboard_fields(result: "AnalysisResult") -> None:
    dashboard = result.dashboard if isinstance(result.dashboard, dict) else {}
    result.dashboard = dashboard
    dashboard["sentiment_score"] = getattr(result, "sentiment_score", None)
    dashboard["operation_advice"] = getattr(result, "operation_advice", None)
    dashboard["decision_type"] = getattr(result, "decision_type", None)


def _as_dict_for_decision_guard(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        try:
            converted = value.to_dict()
            return converted if isinstance(converted, dict) else {}
        except Exception:
            return {}
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {}


def _first_list_value(value: Any) -> Any:
    if isinstance(value, (list, tuple)) and value:
        return value[0]
    return value


def _coerce_numeric_value(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        if math.isfinite(float(value)):
            return float(value)
        return None
    text = str(value).replace(",", "").replace("，", "").strip()
    if not text or text.upper() in {"N/A", "NA", "NONE", "NULL"}:
        return None
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _first_numeric_value(*values: Any) -> Optional[float]:
    for value in values:
        if isinstance(value, (list, tuple)):
            nested = _first_numeric_value(*value)
            if nested is not None:
                return nested
            continue
        numeric = _coerce_numeric_value(value)
        if numeric is not None:
            return numeric
    return None


def _capital_flow_bias(fundamental_context: Optional[Dict[str, Any]]) -> str:
    return _capital_flow_bias_with_status(fundamental_context)[0]


def _capital_flow_bias_with_status(
    fundamental_context: Optional[Dict[str, Any]],
) -> tuple[str, str]:
    if not isinstance(fundamental_context, dict):
        return "unavailable", "invalid_context"
    block = fundamental_context.get("capital_flow")
    if not isinstance(block, dict):
        return "unavailable", "capital_flow_block_missing"
    status = str(block.get("status") or "").strip().lower()
    normalized_status = status.replace("-", " ").replace("_", " ").strip()
    if (
        normalized_status in _CAPITAL_FLOW_UNAVAILABLE_STATUS
        or "not supported" in normalized_status
    ):
        return "unavailable", status or "not_supported"
    data = block.get("data") if isinstance(block.get("data"), dict) else block
    stock_flow = data.get("stock_flow") if isinstance(data, dict) else None
    if not isinstance(stock_flow, dict) or not stock_flow:
        return "unavailable", "empty_stock_flow"

    def _flow_direction(value: Optional[float]) -> Optional[str]:
        if value is None or value == 0:
            return None
        return "inflow" if value > 0 else "outflow"

    numeric_values = [
        _coerce_numeric_value(stock_flow.get("main_net_inflow")),
        _coerce_numeric_value(stock_flow.get("inflow_5d")),
        _coerce_numeric_value(stock_flow.get("inflow_10d")),
    ]
    if all(value is None for value in numeric_values):
        return "unavailable", "missing_or_na_flow_fields"

    ordered_signals = [_flow_direction(value) for value in numeric_values]
    directions = {signal for signal in ordered_signals if signal is not None}
    if not directions or len(directions) > 1:
        return "neutral", "conflict_or_missing"
    for signal in ordered_signals:
        if signal is not None:
            return signal, "ok"
    return "neutral", "neutral"


def _capital_flow_status_for_stability(reason: str, language: str) -> str:
    normalized = str(reason or "").strip().lower()
    if (
        "not_supported" in normalized
        or "unsupported" in normalized
        or "not available" in normalized
    ):
        return (
            "市场资金流服务暂不支持"
            if language == "zh"
            else "Capital flow source unsupported"
        )
    if "empty_stock_flow" in normalized or "missing" in normalized:
        return "资金流数据缺失" if language == "zh" else "capital flow data unavailable"
    return "资金流数据不可用" if language == "zh" else "capital flow unavailable"


def _set_decision_stability_unavailable(
    result: "AnalysisResult",
    language: str,
    *,
    current_price: Optional[float],
    support: Optional[float],
    resistance: Optional[float],
    flow_status: str,
) -> None:
    dashboard = result.dashboard if isinstance(result.dashboard, dict) else {}
    result.dashboard = dashboard
    dashboard["decision_stability"] = {
        "applied": False,
        "reason": "资金流不可用，未使用资金流校准"
        if language == "zh"
        else "Capital flow unavailable; stability calibration not applied",
        "capital_flow_status": _capital_flow_status_for_stability(
            flow_status, language
        ),
        "current_price": current_price,
        "support": support,
        "resistance": resistance,
        "capital_flow_bias": "unavailable",
    }
    _sync_stability_dashboard_fields(result)


def _bound_hold_watch_sentiment_score(result: "AnalysisResult") -> None:
    try:
        score = int(getattr(result, "sentiment_score", 50))
    except (TypeError, ValueError):
        score = 50
    result.sentiment_score = min(59, max(45, score))


def _apply_hold_watch_dashboard(
    result: "AnalysisResult",
    language: str,
    *,
    advice: str,
    reason: str,
    current_price: Optional[float],
    support: Optional[float],
    resistance: Optional[float],
    flow_bias: str,
    no_position: str,
    has_position: str,
    capital_flow_status: Optional[str] = None,
) -> None:
    result.operation_advice = advice

    dashboard = result.dashboard if isinstance(result.dashboard, dict) else {}
    result.dashboard = dashboard
    core = dashboard.get("core_conclusion")
    if not isinstance(core, dict):
        core = {}
        dashboard["core_conclusion"] = core
    core["signal_type"] = "🟡持有观望" if language == "zh" else "🟡 Hold / Watch"
    core["one_sentence"] = (
        f"{advice}：{reason}" if language == "zh" else f"{advice}: {reason}"
    )

    position_advice = core.get("position_advice")
    if not isinstance(position_advice, dict):
        position_advice = {}
        core["position_advice"] = position_advice
    position_advice["no_position"] = no_position
    position_advice["has_position"] = has_position

    stability = {
        "applied": True,
        "reason": reason,
        "current_price": current_price,
        "support": support,
        "resistance": resistance,
        "capital_flow_bias": flow_bias,
    }
    if capital_flow_status is not None:
        stability["capital_flow_status"] = capital_flow_status
    dashboard["decision_stability"] = stability

    if reason and reason not in str(result.risk_warning or ""):
        sep = "；" if language == "zh" else "; "
        result.risk_warning = (
            f"{result.risk_warning}{sep}{reason}" if result.risk_warning else reason
        )
    result.buy_reason = reason or result.buy_reason


def _downgrade_buy_without_capital_flow(
    result: "AnalysisResult",
    language: str,
    *,
    current_price: Optional[float],
    support: Optional[float],
    resistance: Optional[float],
    flow_status: str,
) -> None:
    status_text = _capital_flow_status_for_stability(flow_status, language)
    if language == "zh":
        advice = "持有观察"
        reason = f"{status_text}，买入结论缺少资金面确认，先按观察处理。"
        no_position = "空仓先不追买，等待资金流恢复、支撑确认或有效突破后再行动。"
        has_position = "持仓以关键支撑为风控线，资金流恢复前控制仓位。"
        confidence = "低"
    else:
        advice = "Hold and watch"
        reason = f"{status_text}; the buy call lacks capital-flow confirmation, so treat it as watch-only."
        no_position = "Do not chase; wait for capital-flow recovery, support confirmation, or a valid breakout."
        has_position = "Use key support as the risk line and keep position size controlled until capital flow recovers."
        confidence = "Low"

    result.decision_type = "hold"
    result.confidence_level = confidence
    _bound_hold_watch_sentiment_score(result)
    _apply_hold_watch_dashboard(
        result,
        language,
        advice=advice,
        reason=reason,
        current_price=current_price,
        support=support,
        resistance=resistance,
        flow_bias="unavailable",
        no_position=no_position,
        has_position=has_position,
        capital_flow_status=status_text,
    )
    _sync_stability_dashboard_fields(result)
    logger.info(
        "[decision_stability] Downgraded buy because capital flow is unavailable: %s",
        flow_status,
    )


def _downgrade_to_structural_hold(
    result: "AnalysisResult",
    language: str,
    *,
    advice_key: str,
    reason_key: str,
    current_price: float,
    support: Optional[float],
    resistance: Optional[float],
    flow_bias: str,
) -> None:
    result.decision_type = "hold"
    _bound_hold_watch_sentiment_score(result)
    _set_structural_hold_wording(
        result,
        language,
        advice_key=advice_key,
        reason_key=reason_key,
        current_price=current_price,
        support=support,
        resistance=resistance,
        flow_bias=flow_bias,
    )


def _set_structural_hold_wording(
    result: "AnalysisResult",
    language: str,
    *,
    advice_key: str,
    reason_key: str,
    current_price: float,
    support: Optional[float],
    resistance: Optional[float],
    flow_bias: str,
) -> None:
    advice = {
        "zh": {
            "range": "震荡观望",
            "shakeout": "洗盘观察",
            "hold": "持有观察",
        },
        "en": {
            "range": "Range-bound watch",
            "shakeout": "Shakeout watch",
            "hold": "Hold and watch",
        },
    }[language].get(advice_key, "持有观察" if language == "zh" else "Hold and watch")
    reason_templates = {
        "zh": {
            "buy_near_resistance": "价格接近压力位且主力资金未确认流入，不宜仅因短线反弹追买。",
            "buy_with_outflow": "主力资金流出与买入结论冲突，买点需等待支撑确认或资金回流。",
            "sell_near_support": "价格贴近支撑且未见资金持续流出，不宜仅因单日下跌直接卖出。",
            "sell_with_inflow": "主力资金流入与卖出结论冲突，先按持有观察处理并跟踪支撑失效。",
            "hold_shakeout": "价格回落至支撑附近但资金未确认流出，更适合按洗盘观察处理。",
            "hold_mid_range": "价格处于支撑与压力之间且资金流不明确，维持震荡观望更可操作。",
        },
        "en": {
            "buy_near_resistance": "Price is near resistance without confirmed main-force inflow, so chasing the rebound is not actionable.",
            "buy_with_outflow": "Main-force outflow conflicts with a buy call; wait for support confirmation or capital inflow.",
            "sell_near_support": "Price is near support without sustained outflow, so a one-day drop is not enough to sell.",
            "sell_with_inflow": "Main-force inflow conflicts with a sell call; hold and watch for support failure.",
            "hold_shakeout": "Price pulled back near support without confirmed outflow, which is better treated as a shakeout watch.",
            "hold_mid_range": "Price is between support and resistance with neutral fund flow, so range-bound watch is more actionable.",
        },
    }
    reason = reason_templates[language].get(reason_key, "")
    result.operation_advice = advice
    if (
        language == "zh"
        and "震荡" not in str(result.trend_prediction)
        and advice_key == "range"
    ):
        result.trend_prediction = "震荡"
    elif language == "en" and advice_key == "range":
        result.trend_prediction = "Sideways"

    if language == "zh":
        no_position = "空仓先不追涨杀跌，等待支撑确认、放量突破或资金回流后再行动。"
        has_position = "持仓以关键支撑为风控线，未跌破前以观察和分批控仓为主。"
    else:
        no_position = "Do not chase or panic; wait for support confirmation, breakout, or renewed inflow."
        has_position = "Use key support as the risk line and manage position size unless support fails."
    _apply_hold_watch_dashboard(
        result,
        language,
        advice=advice,
        reason=reason,
        current_price=current_price,
        support=support,
        resistance=resistance,
        flow_bias=flow_bias,
        no_position=no_position,
        has_position=has_position,
    )
    logger.info(
        "[decision_stability] Applied structural hold calibration: %s", reason_key
    )


def get_stock_name_multi_source(
    stock_code: str, context: Optional[Dict[str, Any]] = None, data_manager=None
) -> str:
    """
    多来源获取股票中文名称

    获取策略（按优先级）：
    1. 从传入的 context 中获取（realtime 数据）
    2. 从静态映射表 STOCK_NAME_MAP 获取
    3. 从 DataFetcherManager 获取（各数据源）
    4. 返回默认名称（股票+代码）

    Args:
        stock_code: 股票代码
        context: 分析上下文（可选）
        data_manager: DataFetcherManager 实例（可选）

    Returns:
        股票中文名称
    """
    # 1. 从上下文获取（实时行情数据）
    if context:
        # 优先从 stock_name 字段获取
        if context.get("stock_name"):
            name = context["stock_name"]
            if name and not name.startswith("股票"):
                return cast(str, name)

        # 其次从 realtime 数据获取
        if "realtime" in context and context["realtime"].get("name"):
            return cast(str, context["realtime"]["name"])

    # 2. 从静态映射表获取
    if stock_code in STOCK_NAME_MAP:
        return STOCK_NAME_MAP[stock_code]

    # 3. 从数据源获取
    if data_manager is None:
        try:
            from data_provider.base import DataFetcherManager

            data_manager = DataFetcherManager()
        except Exception as e:
            logger.debug(f"无法初始化 DataFetcherManager: {e}")

    if data_manager:
        try:
            name = data_manager.get_stock_name(stock_code)
            if name:
                # 更新缓存
                STOCK_NAME_MAP[stock_code] = name
                return cast(str, name)
        except Exception as e:
            logger.debug(f"从数据源获取股票名称失败: {e}")

    # 4. 返回默认名称
    return f"股票{stock_code}"


@dataclass
class AnalysisResult:
    """
    AI 分析结果数据类 - 决策仪表盘版

    封装 Gemini 返回的分析结果，包含决策仪表盘和详细分析
    """

    code: str
    name: str

    # ========== 核心指标 ==========
    sentiment_score: int  # 综合评分 0-100 (>70强烈看多, >60看多, 40-60震荡, <40看空)
    trend_prediction: str  # 趋势预测：强烈看多/看多/震荡/看空/强烈看空
    operation_advice: str  # 操作建议：买入/加仓/持有/减仓/卖出/观望
    decision_type: str = "hold"  # 决策类型：buy/hold/sell（用于统计）
    confidence_level: str = "中"  # 置信度：高/中/低
    report_language: str = "zh"  # 报告输出语言：zh/en
    action: Optional[str] = (
        None  # 建议动作 taxonomy：buy/add/hold/reduce/sell/watch/avoid/alert
    )
    action_label: Optional[str] = None  # 本地化建议动作标签

    # ========== 决策仪表盘 (新增) ==========
    dashboard: Optional[Dict[str, Any]] = None  # 完整的决策仪表盘数据

    # ========== 走势分析 ==========
    trend_analysis: str = ""  # 走势形态分析（支撑位、压力位、趋势线等）
    short_term_outlook: str = ""  # 短期展望（1-3日）
    medium_term_outlook: str = ""  # 中期展望（1-2周）

    # ========== 技术面分析 ==========
    technical_analysis: str = ""  # 技术指标综合分析
    ma_analysis: str = ""  # 均线分析（多头/空头排列，金叉/死叉等）
    volume_analysis: str = ""  # 量能分析（放量/缩量，主力动向等）
    pattern_analysis: str = ""  # K线形态分析

    # ========== 基本面分析 ==========
    fundamental_analysis: str = ""  # 基本面综合分析
    sector_position: str = ""  # 板块地位和行业趋势
    company_highlights: str = ""  # 公司亮点/风险点

    # ========== 情绪面/消息面分析 ==========
    news_summary: str = ""  # 近期重要新闻/公告摘要
    market_sentiment: str = ""  # 市场情绪分析
    hot_topics: str = ""  # 相关热点话题

    # ========== 综合分析 ==========
    analysis_summary: str = ""  # 综合分析摘要
    key_points: str = ""  # 核心看点（3-5个要点）
    risk_warning: str = ""  # 风险提示
    buy_reason: str = ""  # 买入/卖出理由

    # ========== 元数据 ==========
    market_snapshot: Optional[Dict[str, Any]] = None  # 当日行情快照（展示用）
    raw_response: Optional[str] = None  # 原始响应（调试用）
    search_performed: bool = False  # 是否执行了联网搜索
    data_sources: str = ""  # 数据来源说明
    success: bool = True
    error_message: Optional[str] = None

    # ========== 价格数据（分析时快照）==========
    current_price: Optional[float] = None  # 分析时的股价
    change_pct: Optional[float] = None  # 分析时的涨跌幅(%)

    # ========== 模型标记（Issue #528）==========
    model_used: Optional[str] = (
        None  # 分析使用的 LLM 模型（完整名，如 gemini/gemini-2.0-flash）
    )

    # ========== 历史对比（Report Engine P0）==========
    query_id: Optional[str] = None  # 本次分析 query_id，用于历史对比时排除本次记录

    # ========== 基本面上下文（仅运行时，用于通知拼装；不持久化到 to_dict）==========
    fundamental_context: Optional[Dict[str, Any]] = None

    # ========== 长线投研框架（P1 新增）==========
    research_framework: Optional[Dict[str, Any]] = None  # 六维投研框架评分结果
    investment_conclusion: Optional[Dict[str, Any]] = None  # 投资结论

    # ========== 五段式长线投研报告（P1-P2 新增）==========
    bayesian_framework: Optional[Dict[str, Any]] = None  # 贝叶斯框架
    supply_chain: Optional[Dict[str, Any]] = None  # 产业链解读
    value_scenarios: Optional[Dict[str, Any]] = None  # 长期价值与情景
    six_dimension_inputs: Optional[Dict[str, Any]] = (
        None  # LLM 输出的长线六维主观键值（P2-fix）
    )

    # ========== 运行时诊断快照（仅运行时，不持久化到 to_dict）==========
    market_phase_summary: Optional[Dict[str, Any]] = None  # 大盘/市场阶段摘要
    analysis_context_pack_overview: Optional[Dict[str, Any]] = None  # 上下文包概览
    diagnostic_context_snapshot: Optional[Dict[str, Any]] = None  # 诊断上下文快照
    midtrend_compass: Optional[Dict[str, Any]] = None  # 中期趋势罗盘评估结果（P2；仅 cron 落盘 context_snapshot）

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "code": self.code,
            "name": self.name,
            "sentiment_score": self.sentiment_score,
            "trend_prediction": self.trend_prediction,
            "operation_advice": self.operation_advice,
            "decision_type": self.decision_type,
            "confidence_level": self.confidence_level,
            "report_language": self.report_language,
            "action": self.action,
            "action_label": self.action_label,
            "dashboard": self.dashboard,  # 决策仪表盘数据
            "trend_analysis": self.trend_analysis,
            "short_term_outlook": self.short_term_outlook,
            "medium_term_outlook": self.medium_term_outlook,
            "technical_analysis": self.technical_analysis,
            "ma_analysis": self.ma_analysis,
            "volume_analysis": self.volume_analysis,
            "pattern_analysis": self.pattern_analysis,
            "fundamental_analysis": self.fundamental_analysis,
            "sector_position": self.sector_position,
            "company_highlights": self.company_highlights,
            "news_summary": self.news_summary,
            "market_sentiment": self.market_sentiment,
            "hot_topics": self.hot_topics,
            "analysis_summary": self.analysis_summary,
            "key_points": self.key_points,
            "risk_warning": self.risk_warning,
            "buy_reason": self.buy_reason,
            "action_reason": self.buy_reason,  # 操作建议与趋势预测的原因
            "market_snapshot": self.market_snapshot,
            "search_performed": self.search_performed,
            "success": self.success,
            "error_message": self.error_message,
            "current_price": self.current_price,
            "change_pct": self.change_pct,
            "model_used": self.model_used,
            "research_framework": self.research_framework,
            "investment_conclusion": self.investment_conclusion,
            # 五段式长线投研报告字段
            "bayesian_framework": self.bayesian_framework,
            "supply_chain": self.supply_chain,
            "value_scenarios": self.value_scenarios,
        }

    def get_core_conclusion(self) -> str:
        """获取核心结论（一句话）"""
        if self.dashboard and "core_conclusion" in self.dashboard:
            return cast(
                str,
                self.dashboard["core_conclusion"].get(
                    "one_sentence", self.analysis_summary
                ),
            )
        return self.analysis_summary

    def get_position_advice(self, has_position: bool = False) -> str:
        """获取持仓建议"""
        if self.dashboard and "core_conclusion" in self.dashboard:
            pos_advice = self.dashboard["core_conclusion"].get("position_advice", {})
            if has_position:
                return cast(str, pos_advice.get("has_position", self.operation_advice))
            return cast(str, pos_advice.get("no_position", self.operation_advice))
        return self.operation_advice

    def get_sniper_points(self) -> Dict[str, str]:
        """获取狙击点位"""

        if self.dashboard and "battle_plan" in self.dashboard:
            return cast(
                Dict[str, str],
                self.dashboard["battle_plan"].get("sniper_points", {}),
            )
        return {}

    def get_checklist(self) -> List[str]:
        """获取检查清单"""
        if self.dashboard and "battle_plan" in self.dashboard:
            return cast(
                List[str],
                self.dashboard["battle_plan"].get("action_checklist", []),
            )
        return []

    def get_risk_alerts(self) -> List[str]:
        """获取风险警报"""
        if self.dashboard and "intelligence" in self.dashboard:
            return cast(
                List[str],
                self.dashboard["intelligence"].get("risk_alerts", []),
            )
        return []

    def get_emoji(self) -> str:
        """根据操作建议返回对应 emoji"""
        _, emoji, _ = get_signal_level(
            self.operation_advice,
            self.sentiment_score,
            self.report_language,
        )
        return emoji

    def get_confidence_stars(self) -> str:
        """返回置信度星级"""
        star_map = {
            "高": "⭐⭐⭐",
            "high": "⭐⭐⭐",
            "中": "⭐⭐",
            "medium": "⭐⭐",
            "低": "⭐",
            "low": "⭐",
        }
        return star_map.get(str(self.confidence_level or "").strip().lower(), "⭐⭐")


def populate_decision_action_fields(
    result: AnalysisResult,
    *,
    explicit_action: Any = None,
    report_type: Any = None,
    use_existing_action: bool = True,
) -> AnalysisResult:
    """Populate optional decision action fields without changing legacy advice."""

    action_source = explicit_action
    if action_source is None and use_existing_action:
        action_source = getattr(result, "action", None)

    fields = build_action_fields(
        operation_advice=getattr(result, "operation_advice", None),
        explicit_action=action_source,
        report_type=report_type,
        report_language=getattr(result, "report_language", "zh"),
    )
    result.action = fields["action"]
    result.action_label = fields["action_label"]
    return result


class GeminiAnalyzer:
    """
    Gemini AI 分析器

    职责：
    1. 调用 Google Gemini API 进行股票分析
    2. 结合预先搜索的新闻和技术面数据生成分析报告
    3. 解析 AI 返回的 JSON 格式结果

    使用方式：
        analyzer = GeminiAnalyzer()
        result = analyzer.analyze(context, news_context)
    """

    # ========================================
    # 系统提示词 - 决策仪表盘 v2.0
    # ========================================
    # 输出格式升级：从简单信号升级为决策仪表盘
    # 核心模块：核心结论 + 数据透视 + 舆情情报 + 作战计划
    # ========================================

    LEGACY_DEFAULT_SYSTEM_PROMPT = (
        """你是一位专注于趋势交易的{market_placeholder}投资分析师，负责生成专业的【决策仪表盘】分析报告。

{guidelines_placeholder}

"""
        + CORE_TRADING_SKILL_POLICY_ZH
        + """

## 输出格式：决策仪表盘 JSON

请严格按照以下 JSON 格式输出，这是一个完整的【决策仪表盘】：

```json
{
    "stock_name": "股票中文名称",
    "sentiment_score": 0-100整数,
    "trend_prediction": "强烈看多/看多/震荡/看空/强烈看空",
    "operation_advice": "买入/加仓/持有/减仓/卖出/观望",
    "decision_type": "buy/hold/sell",
    "confidence_level": "高/中/低",

    "dashboard": {
        "core_conclusion": {
            "one_sentence": "一句话核心结论（30字以内，直接告诉用户做什么）",
            "signal_type": "🟢买入信号/🟡持有观望/🔴卖出信号/⚠️风险警告",
            "time_sensitivity": "立即行动/今日内/本周内/不急",
            "position_advice": {
                "no_position": "空仓者建议：具体操作指引",
                "has_position": "持仓者建议：具体操作指引"
            }
        },

        "data_perspective": {
            "trend_status": {
                "ma_alignment": "均线排列状态描述",
                "is_bullish": true/false,
                "trend_score": 0-100
            },
            "price_position": {
                "current_price": 当前价格数值,
                "ma5": MA5数值,
                "ma10": MA10数值,
                "ma20": MA20数值,
                "bias_ma5": 乖离率百分比数值,
                "bias_status": "安全/警戒/危险",
                "support_level": 支撑位价格,
                "resistance_level": 压力位价格
            },
            "volume_analysis": {
                "volume_ratio": 量比数值,
                "volume_status": "放量/缩量/平量",
                "turnover_rate": 换手率百分比,
                "volume_meaning": "量能含义解读（如：缩量回调表示抛压减轻）"
            },
            "chip_structure": {
                "profit_ratio": 获利比例,
                "avg_cost": 平均成本,
                "concentration": 筹码集中度,
                "chip_health": "健康/一般/警惕"
            }
        },

        "intelligence": {
            "latest_news": "【最新消息】近期重要新闻摘要",
            "risk_alerts": ["风险点1：具体描述", "风险点2：具体描述"],
            "positive_catalysts": ["利好1：具体描述", "利好2：具体描述"],
            "earnings_outlook": "业绩预期分析（基于年报预告、业绩快报等）",
            "sentiment_summary": "舆情情绪一句话总结"
        },

        "battle_plan": {
            "sniper_points": {
                "ideal_buy": "理想买入点：XX元（在MA5附近）",
                "secondary_buy": "次优买入点：XX元（在MA10附近）",
                "stop_loss": "止损位：XX元（跌破MA20或X%）",
                "take_profit": "目标位：XX元（前高/整数关口）"
            },
            "position_strategy": {
                "suggested_position": "建议仓位：X成",
                "entry_plan": "分批建仓策略描述",
                "risk_control": "风控策略描述"
            },
            "action_checklist": [
                "✅/⚠️/❌ 检查项1：多头排列",
                "✅/⚠️/❌ 检查项2：乖离率合理（强势趋势可放宽）",
                "✅/⚠️/❌ 检查项3：量能配合",
                "✅/⚠️/❌ 检查项4：无重大利空",
                "✅/⚠️/❌ 检查项5：筹码健康",
                "✅/⚠️/❌ 检查项6：PE估值合理"
            ]
        },

        "phase_decision": {
            "phase_context": {"phase": "premarket/intraday/lunch_break/closing_auction/postmarket/non_trading/unknown"},
            "action_window": "盘前计划/盘中跟踪/午间确认/收盘前风控/盘后复盘/非交易日观察",
            "immediate_action": "立即行动/等待确认/观察/止损止盈预警/禁止追高/无盘中动作",
            "watch_conditions": ["观察条件1", "观察条件2"],
            "next_check_time": "下一次检查点或市场本地时间",
            "confidence_reason": "置信度理由，说明阶段和数据质量限制",
            "data_limitations": ["阶段或数据质量限制1", "阶段或数据质量限制2"]
        }
    },

    "analysis_summary": "100字综合分析摘要",
    "key_points": "3-5个核心看点，逗号分隔",
    "risk_warning": "风险提示",
    "buy_reason": "操作理由，引用交易理念",

    "trend_analysis": "走势形态分析",
    "short_term_outlook": "短期1-3日展望",
    "medium_term_outlook": "中期1-2周展望",
    "technical_analysis": "技术面综合分析",
    "ma_analysis": "均线系统分析",
    "volume_analysis": "量能分析",
    "pattern_analysis": "K线形态分析",
    "fundamental_analysis": "基本面分析",
    "sector_position": "板块行业分析",
    "company_highlights": "公司亮点/风险",
    "news_summary": "新闻摘要",
    "market_sentiment": "市场情绪",
    "hot_topics": "相关热点",

    "search_performed": true/false,
    "data_sources": "数据来源说明"
}
```

## 评分标准

### 强烈买入（80-100分）：
- ✅ 多头排列：MA5 > MA10 > MA20
- ✅ 低乖离率：<2%，最佳买点
- ✅ 缩量回调或放量突破
- ✅ 筹码集中健康
- ✅ 消息面有利好催化

### 买入（60-79分）：
- ✅ 多头排列或弱势多头
- ✅ 乖离率 <5%
- ✅ 量能正常
- ⚪ 允许一项次要条件不满足

### 观望（40-59分）：
- ⚠️ 乖离率 >5%（追高风险）
- ⚠️ 均线缠绕趋势不明
- ⚠️ 有风险事件

### 卖出/减仓（0-39分）：
- ❌ 空头排列
- ❌ 跌破MA20
- ❌ 放量下跌
- ❌ 重大利空

## 决策仪表盘核心原则

1. **核心结论先行**：一句话说清该买该卖
2. **分持仓建议**：空仓者和持仓者给不同建议
3. **精确狙击点**：必须给出具体价格，不说模糊的话
4. **检查清单可视化**：用 ✅⚠️❌ 明确显示每项检查结果
5. **风险优先级**：舆情中的风险点要醒目标出

## 可操作性与稳定性约束

- 不得仅因为单日涨跌或评分跨线就在“买入/卖出”之间剧烈切换。
- 操作建议必须同时参考价格位置（支撑/压力位）、量能/筹码、主力资金流向和风险事件。
- 股价位于支撑与压力之间、资金流不明确时，优先输出“持有/震荡/观望/洗盘观察”等可执行的中性建议；`decision_type` 仍保持 `hold`。
- 只有在接近支撑确认或有效突破压力，且资金流/量价配合时，才能给出买入；接近压力且资金流出时不得追买。
- 只有在跌破关键支撑、主力资金持续流出或风险显著放大时，才能给出卖出/减仓。
- 必须输出 `dashboard.phase_decision` 七字段；盘中/午休/临近收盘要给出当前动作、观察条件和下一次检查点。
- 盘前、非交易日或未知阶段不得伪造今日盘中走势；quote/daily_bars/technical 存在 stale、fallback、missing、fetch_failed、partial 或 estimated 时，`confidence_level` 不得为高。"""
    )

    SYSTEM_PROMPT = """你是一位{market_placeholder}投资分析师，负责生成专业的【决策仪表盘】分析报告。

{guidelines_placeholder}

{default_skill_policy_section}
{skills_section}

## 输出格式：决策仪表盘 JSON

请严格按照以下 JSON 格式输出，这是一个完整的【决策仪表盘】：

```json
{
    "stock_name": "股票中文名称",
    "sentiment_score": 0-100整数,
    "trend_prediction": "强烈看多/看多/震荡/看空/强烈看空",
    "operation_advice": "买入/加仓/持有/减仓/卖出/观望",
    "decision_type": "buy/hold/sell",
    "confidence_level": "高/中/低",

    "dashboard": {
        "core_conclusion": {
            "one_sentence": "一句话核心结论（30字以内，直接告诉用户做什么）",
            "signal_type": "🟢买入信号/🟡持有观望/🔴卖出信号/⚠️风险警告",
            "time_sensitivity": "立即行动/今日内/本周内/不急",
            "position_advice": {
                "no_position": "空仓者建议：具体操作指引",
                "has_position": "持仓者建议：具体操作指引"
            }
        },

        "data_perspective": {
            "trend_status": {
                "ma_alignment": "均线排列状态描述",
                "is_bullish": true/false,
                "trend_score": 0-100
            },
            "price_position": {
                "current_price": 当前价格数值,
                "ma5": MA5数值,
                "ma10": MA10数值,
                "ma20": MA20数值,
                "bias_ma5": 乖离率百分比数值,
                "bias_status": "安全/警戒/危险",
                "support_level": 支撑位价格,
                "resistance_level": 压力位价格
            },
            "volume_analysis": {
                "volume_ratio": 量比数值,
                "volume_status": "放量/缩量/平量",
                "turnover_rate": 换手率百分比,
                "volume_meaning": "量能含义解读（如：缩量回调表示抛压减轻）"
            },
            "chip_structure": {
                "profit_ratio": 获利比例,
                "avg_cost": 平均成本,
                "concentration": 筹码集中度,
                "chip_health": "健康/一般/警惕"
            }
        },

        "intelligence": {
            "latest_news": "【最新消息】近期重要新闻摘要",
            "risk_alerts": ["风险点1：具体描述", "风险点2：具体描述"],
            "positive_catalysts": ["利好1：具体描述", "利好2：具体描述"],
            "earnings_outlook": "业绩预期分析（基于年报预告、业绩快报等）",
            "sentiment_summary": "舆情情绪一句话总结"
        },

        "battle_plan": {
            "sniper_points": {
                "ideal_buy": "理想入场位：XX元（满足主要技能触发条件）",
                "secondary_buy": "次优入场位：XX元（更保守或确认后执行）",
                "stop_loss": "止损位：XX元（失效条件或X%风险）",
                "take_profit": "目标位：XX元（按阻力位/风险回报比制定）"
            },
            "position_strategy": {
                "suggested_position": "建议仓位：X成",
                "entry_plan": "分批建仓策略描述",
                "risk_control": "风控策略描述"
            },
            "action_checklist": [
                "✅/⚠️/❌ 检查项1：当前结构是否满足激活技能条件",
                "✅/⚠️/❌ 检查项2：入场位置与风险回报是否合理",
                "✅/⚠️/❌ 检查项3：量价/波动/筹码是否支持判断",
                "✅/⚠️/❌ 检查项4：无重大利空",
                "✅/⚠️/❌ 检查项5：仓位与止损计划明确",
                "✅/⚠️/❌ 检查项6：估值/业绩/催化与结论匹配"
            ]
        },

        "phase_decision": {
            "phase_context": {"phase": "premarket/intraday/lunch_break/closing_auction/postmarket/non_trading/unknown"},
            "action_window": "盘前计划/盘中跟踪/午间确认/收盘前风控/盘后复盘/非交易日观察",
            "immediate_action": "立即行动/等待确认/观察/止损止盈预警/禁止追高/无盘中动作",
            "watch_conditions": ["观察条件1", "观察条件2"],
            "next_check_time": "下一次检查点或市场本地时间",
            "confidence_reason": "置信度理由，说明阶段和数据质量限制",
            "data_limitations": ["阶段或数据质量限制1", "阶段或数据质量限制2"]
        }
    },

    "analysis_summary": "100字综合分析摘要",
    "key_points": "3-5个核心看点，逗号分隔",
    "risk_warning": "风险提示",
    "buy_reason": "操作理由，引用激活技能或风险框架",

    "trend_analysis": "走势形态分析",
    "short_term_outlook": "短期1-3日展望",
    "medium_term_outlook": "中期1-2周展望",
    "technical_analysis": "技术面综合分析",
    "ma_analysis": "均线系统分析",
    "volume_analysis": "量能分析",
    "pattern_analysis": "K线形态分析",
    "fundamental_analysis": "基本面分析",
    "sector_position": "板块行业分析",
    "company_highlights": "公司亮点/风险",
    "news_summary": "新闻摘要",
    "market_sentiment": "市场情绪",
    "hot_topics": "相关热点",

    "search_performed": true/false,
    "data_sources": "数据来源说明"
}
```

## 评分标准

### 强烈买入（80-100分）：
- ✅ 多个激活技能同时支持积极结论
- ✅ 上行空间、触发条件与风险回报清晰
- ✅ 关键风险已排查，仓位与止损计划明确
- ✅ 重要数据和情报结论彼此一致

### 买入（60-79分）：
- ✅ 主信号偏积极，但仍有少量待确认项
- ✅ 允许存在可控风险或次优入场点
- ✅ 需要在报告中明确补充观察条件

### 观望（40-59分）：
- ⚠️ 信号分歧较大，或缺乏足够确认
- ⚠️ 风险与机会大致均衡
- ⚠️ 更适合等待触发条件或回避不确定性

### 卖出/减仓（0-39分）：
- ❌ 主要结论转弱，风险明显高于收益
- ❌ 触发了止损/失效条件或重大利空
- ❌ 现有仓位更需要保护而不是进攻

## 决策仪表盘核心原则

1. **核心结论先行**：一句话说清该买该卖
2. **分持仓建议**：空仓者和持仓者给不同建议
3. **精确狙击点**：必须给出具体价格，不说模糊的话
4. **检查清单可视化**：用 ✅⚠️❌ 明确显示每项检查结果
5. **风险优先级**：舆情中的风险点要醒目标出

## 可操作性与稳定性约束

- 不得仅因为单日涨跌或评分跨线就在“买入/卖出”之间剧烈切换。
- 操作建议必须同时参考价格位置（支撑/压力位）、量能/筹码、主力资金流向和风险事件。
- 股价位于支撑与压力之间、资金流不明确时，优先输出“持有/震荡/观望/洗盘观察”等可执行的中性建议；`decision_type` 仍保持 `hold`。
- 只有在接近支撑确认或有效突破压力，且资金流/量价配合时，才能给出买入；接近压力且资金流出时不得追买。
- 只有在跌破关键支撑、主力资金持续流出或风险显著放大时，才能给出卖出/减仓。
- 必须输出 `dashboard.phase_decision` 七字段；盘中/午休/临近收盘要给出当前动作、观察条件和下一次检查点。
- 盘前、非交易日或未知阶段不得伪造今日盘中走势；quote/daily_bars/technical 存在 stale、fallback、missing、fetch_failed、partial 或 estimated 时，`confidence_level` 不得为高。"""

    TEXT_SYSTEM_PROMPT = """你是一位专业的股票分析助手。

- 回答必须基于用户提供的数据与上下文
- 若信息不足，要明确指出不确定性
- 不要编造价格、财报或新闻事实
"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        config: Optional[Config] = None,
        skills: Optional[List[str]] = None,
        skill_instructions: Optional[str] = None,
        default_skill_policy: Optional[str] = None,
        use_legacy_default_prompt: Optional[bool] = None,
    ):
        """Initialize LLM Analyzer via LiteLLM.

        Args:
            api_key: Ignored (kept for backward compatibility). Keys are loaded from config.
        """
        self._config_override = config
        self._requested_skills = list(skills) if skills is not None else None
        self._skill_instructions_override = skill_instructions
        self._default_skill_policy_override = default_skill_policy
        self._use_legacy_default_prompt_override = use_legacy_default_prompt
        self._resolved_prompt_state: Optional[Dict[str, Any]] = None
        self._router: Optional[Router] = None
        self._legacy_router_model_list: List[Dict[str, Any]] = []
        self._litellm_available = False
        self._init_litellm()
        if not self._litellm_available:
            logger.warning(
                "No LLM configured (LITELLM_MODEL / API keys), AI analysis will be unavailable"
            )

    def _get_runtime_config(self) -> Config:
        """Return the runtime config, honoring injected overrides for tests/pipeline."""
        return getattr(self, "_config_override", None) or get_config()

    def _get_skill_prompt_sections(self) -> tuple[str, str, bool]:
        """Resolve skill instructions + default baseline + prompt mode."""
        skill_instructions = getattr(self, "_skill_instructions_override", None)
        default_skill_policy = getattr(self, "_default_skill_policy_override", None)
        use_legacy_default_prompt = getattr(
            self, "_use_legacy_default_prompt_override", None
        )

        if skill_instructions is not None and default_skill_policy is not None:
            return (
                skill_instructions,
                default_skill_policy,
                bool(use_legacy_default_prompt)
                if use_legacy_default_prompt is not None
                else False,
            )

        resolved_state = getattr(self, "_resolved_prompt_state", None)
        if resolved_state is None:
            from src.agent.factory import resolve_skill_prompt_state

            prompt_state = resolve_skill_prompt_state(
                self._get_runtime_config(),
                skills=getattr(self, "_requested_skills", None),
            )
            resolved_state = {
                "skill_instructions": prompt_state.skill_instructions,
                "default_skill_policy": prompt_state.default_skill_policy,
                "use_legacy_default_prompt": bool(
                    getattr(prompt_state, "use_legacy_default_prompt", False)
                ),
            }
            self._resolved_prompt_state = resolved_state

        return (
            skill_instructions
            if skill_instructions is not None
            else resolved_state.get("skill_instructions", ""),
            default_skill_policy
            if default_skill_policy is not None
            else resolved_state.get("default_skill_policy", ""),
            (
                use_legacy_default_prompt
                if use_legacy_default_prompt is not None
                else bool(resolved_state.get("use_legacy_default_prompt", False))
            ),
        )

    def _get_analysis_system_prompt(
        self, report_language: str, stock_code: str = ""
    ) -> str:
        """Build the analyzer system prompt with output-language guidance."""
        lang = normalize_report_language(report_language)
        market_role = get_market_role(stock_code, lang)
        market_guidelines = get_market_guidelines(stock_code, lang)
        skill_instructions, default_skill_policy, use_legacy_default_prompt = (
            self._get_skill_prompt_sections()
        )
        if use_legacy_default_prompt:
            base_prompt = self.LEGACY_DEFAULT_SYSTEM_PROMPT.replace(
                "{market_placeholder}", market_role
            ).replace("{guidelines_placeholder}", market_guidelines)
        else:
            skills_section = ""
            if skill_instructions:
                skills_section = f"## 激活的交易技能\n\n{skill_instructions}\n"
            default_skill_policy_section = ""
            if default_skill_policy:
                default_skill_policy_section = f"{default_skill_policy}\n"
            base_prompt = (
                self.SYSTEM_PROMPT.replace("{market_placeholder}", market_role)
                .replace("{guidelines_placeholder}", market_guidelines)
                .replace("{default_skill_policy_section}", default_skill_policy_section)
                .replace("{skills_section}", skills_section)
            )
        if lang == "en":
            return (
                base_prompt
                + """

## Output Language (highest priority)

- Keep all JSON keys unchanged.
- `decision_type` must remain `buy|hold|sell`.
- All human-readable JSON values must be written in English.
- Use the common English company name when you are confident; otherwise keep the original listed company name instead of inventing one.
- This includes `stock_name`, `trend_prediction`, `operation_advice`, `confidence_level`, nested dashboard text, checklist items, and all narrative summaries.
"""
            )
        return (
            base_prompt
            + """

## 输出语言（最高优先级）

- 所有 JSON 键名保持不变。
- `decision_type` 必须保持为 `buy|hold|sell`。
- 所有面向用户的人类可读文本值必须使用中文。
"""
        )

    def _has_channel_config(self, config: Config) -> bool:
        """Check if multi-channel config (channels / YAML / legacy model_list) is active."""
        return bool(config.llm_model_list) and not all(
            e.get("model_name", "").startswith("__legacy_")
            for e in config.llm_model_list
        )

    @staticmethod
    def _legacy_router_provider_alias(model: str) -> str:
        provider = model.split("/", 1)[0] if "/" in model else "openai"
        return f"__legacy_{provider}__"

    @staticmethod
    def _build_legacy_router_model_list_from_config(
        model: str,
        model_list: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Build legacy-router candidates from configured legacy llm_model_list entries."""
        if not model:
            return []
        target_model = model
        target_legacy_alias = GeminiAnalyzer._legacy_router_provider_alias(model)
        legacy_entries: List[Dict[str, Any]] = []
        for entry in model_list or []:
            if not isinstance(entry, dict):
                continue
            model_name = str(entry.get("model_name") or "").strip()
            if model_name != target_legacy_alias:
                continue

            params = entry.get("litellm_params")
            if not isinstance(params, dict):
                continue

            api_key = str(params.get("api_key") or "").strip()
            if not api_key or len(api_key) < 8:
                continue

            deployed_params = dict(params)
            deployed_params["model"] = target_model
            deployed_params["api_key"] = api_key
            legacy_entries.append(
                {
                    "model_name": target_model,
                    "litellm_params": deployed_params,
                }
            )

        return legacy_entries

    def _init_litellm(self) -> None:
        """Initialize litellm Router from channels / YAML / legacy keys."""
        config = self._get_runtime_config()
        litellm_model = config.litellm_model
        if not litellm_model:
            logger.warning("Analyzer LLM: LITELLM_MODEL not configured")
            return

        self._litellm_available = True

        # --- Channel / YAML path: build Router from pre-built model_list ---
        if self._has_channel_config(config):
            model_list = config.llm_model_list
            try:
                self._router = Router(
                    model_list=model_list,
                    routing_strategy="simple-shuffle",
                    num_retries=2,
                )
            except TypeError:
                logger.debug(
                    "Analyzer LLM: Router constructor signature not compatible; fallback to direct mode"
                )
                self._router = None
            else:
                unique_models = list(
                    dict.fromkeys(e["litellm_params"]["model"] for e in model_list)
                )
                logger.info(
                    f"Analyzer LLM: Router initialized from channels/YAML — "
                    f"{len(model_list)} deployment(s), models: {unique_models}"
                )
                return

        # --- Legacy path: build Router for multi-key, or use single key ---
        keys = get_api_keys_for_model(litellm_model, config)
        legacy_model_list = self._build_legacy_router_model_list_from_config(
            litellm_model,
            config.llm_model_list,
        )
        if len(legacy_model_list) <= 1 and keys:
            extra_params = extra_litellm_params(litellm_model, config)
            configured_model_list = [
                {
                    "model_name": litellm_model,
                    "litellm_params": {
                        "model": litellm_model,
                        "api_key": k,
                        **extra_params,
                    },
                }
                for k in keys
            ]
            if not legacy_model_list:
                legacy_model_list = configured_model_list
            elif len(legacy_model_list) < len(configured_model_list):
                legacy_model_list = configured_model_list

        if len(legacy_model_list) > 1:
            self._legacy_router_model_list = legacy_model_list
            try:
                self._router = Router(
                    model_list=legacy_model_list,
                    routing_strategy="simple-shuffle",
                    num_retries=2,
                )
            except TypeError:
                logger.debug(
                    "Analyzer LLM: Legacy Router constructor signature not compatible; using legacy model_list fallback"
                )
                self._router = None
            else:
                logger.info(
                    f"Analyzer LLM: Legacy Router initialized with {len(legacy_model_list)} keys "
                    f"for {litellm_model}"
                )
                return

        if keys:
            logger.info(f"Analyzer LLM: litellm initialized (model={litellm_model})")
        else:
            logger.info(
                f"Analyzer LLM: litellm initialized (model={litellm_model}, "
                f"API key from environment)"
            )

    def is_available(self) -> bool:
        """Check if LiteLLM is properly configured with at least one API key."""
        return self._router is not None or self._litellm_available

    def _dispatch_litellm_completion(
        self,
        model: str,
        call_kwargs: Dict[str, Any],
        *,
        config: Config,
        use_channel_router: bool,
        router_model_names: set[str],
    ) -> Any:
        """Dispatch a LiteLLM completion through router or direct fallback."""
        wire_models = resolve_fallback_litellm_wire_models(model, config.llm_model_list)
        register_fallback_model_pricing(wire_models)
        effective_kwargs = apply_minimax_reasoning_params(
            call_kwargs,
            wire_models,
            config.minimax_reasoning_effort,
        )
        if use_channel_router and self._router and model in router_model_names:
            return limited_completion(self._router.completion, **effective_kwargs)
        if self._router and model == config.litellm_model and not use_channel_router:
            return limited_completion(self._router.completion, **effective_kwargs)

        keys = get_api_keys_for_model(model, config)
        if keys:
            effective_kwargs["api_key"] = keys[0]
        effective_kwargs.update(extra_litellm_params(model, config))
        return limited_completion(litellm.completion, **effective_kwargs)

    def _normalize_usage(
        self,
        usage_obj: Any,
        *,
        model: str = "",
        provider: Optional[str] = None,
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Normalize usage objects from LiteLLM responses/chunks."""
        if not usage_obj:
            return attach_message_hmacs({}, messages) if messages is not None else {}
        usage = normalize_litellm_usage(usage_obj, model=model, provider=provider)
        if messages is not None:
            usage = attach_message_hmacs(usage, messages)
        return usage

    @staticmethod
    def _get_response_field(obj: Any, key: str) -> Any:
        """Read a field from dict-like or object-like LiteLLM payloads."""
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    def _extract_text_blocks(self, blocks: Any) -> str:
        """Extract text from OpenAI-compatible content block lists."""
        if not blocks:
            return ""

        parts: List[str] = []
        for block in blocks:
            if isinstance(block, str):
                parts.append(block)
                continue

            text = None
            if isinstance(block, dict):
                text = block.get("text")
                if text is None:
                    text = block.get("content")
            else:
                text = getattr(block, "text", None)
                if text is None:
                    text = getattr(block, "content", None)

            if isinstance(text, str) and text:
                parts.append(text)

        return "".join(parts).strip()

    def _extract_completion_text(self, response: Any) -> str:
        """Extract text from non-stream LiteLLM completion responses."""
        choices = self._get_response_field(response, "choices")
        if not choices:
            return ""

        choice = choices[0]
        message = self._get_response_field(choice, "message")

        content_blocks = self._get_response_field(choice, "content_blocks")
        if content_blocks is None and message is not None:
            content_blocks = self._get_response_field(message, "content_blocks")
        block_text = self._extract_text_blocks(content_blocks)
        if block_text:
            return block_text

        content = None
        if message is not None:
            content = self._get_response_field(message, "content")
        if content is None:
            content = self._get_response_field(choice, "content")

        if isinstance(content, list):
            return self._extract_text_blocks(content)
        if isinstance(content, str):
            return content.strip()
        return str(content).strip() if content is not None else ""

    def _extract_stream_text(self, chunk: Any) -> str:
        """Extract provider-agnostic text delta from a LiteLLM streaming chunk."""
        choices = (
            chunk.get("choices")
            if isinstance(chunk, dict)
            else getattr(chunk, "choices", None)
        )
        if not choices:
            return ""

        choice = choices[0]
        delta = (
            choice.get("delta")
            if isinstance(choice, dict)
            else getattr(choice, "delta", None)
        )
        message = (
            choice.get("message")
            if isinstance(choice, dict)
            else getattr(choice, "message", None)
        )

        content: Any = None
        if isinstance(delta, dict):
            content = delta.get("content")
        elif isinstance(delta, str):
            content = delta
        elif delta is not None:
            content = getattr(delta, "content", None)

        if content is None:
            if isinstance(message, dict):
                content = message.get("content")
            elif message is not None:
                content = getattr(message, "content", None)

        if isinstance(content, list):
            parts: List[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "".join(parts)

        return content if isinstance(content, str) else ""

    def _consume_litellm_stream(
        self,
        stream_response: Any,
        *,
        model: str,
        usage_model: Optional[str] = None,
        provider: Optional[str] = None,
        progress_callback: Optional[Callable[[int], None]] = None,
        first_chunk_timeout: Optional[float] = None,
        idle_chunk_timeout: Optional[float] = None,
        min_response_chars: int = 200,
    ) -> Tuple[str, Dict[str, Any]]:
        """Consume a LiteLLM stream into a single text payload.

        Adds two safety rails (P0-2 fix):
        - first-chunk timeout: if no chunk arrives within ``first_chunk_timeout``
          seconds, treat the stream as a fake-SSE and raise so the caller can
          fall back to non-stream.
        - idle-chunk timeout: if the gap between two chunks exceeds
          ``idle_chunk_timeout`` seconds, raise with ``partial_received=True``
          so the caller can use the partial text (or fall back).
        """
        from src.config import get_first_chunk_timeout, get_idle_chunk_timeout

        if first_chunk_timeout is None:
            first_chunk_timeout = get_first_chunk_timeout()
        if idle_chunk_timeout is None:
            idle_chunk_timeout = get_idle_chunk_timeout()

        chunks: List[str] = []
        usage: Dict[str, Any] = {}
        chars_received = 0
        next_emit_at = 1
        # Wrap the blocking stream iterator in a queue + background thread
        # so we can apply first-chunk / idle-chunk timeouts via queue.get(timeout=).
        # Without this, ``for chunk in stream_response`` would block forever
        # on a slow provider (we saw MiniMax-M3 do this for >100s).
        from queue import Empty, Full, Queue
        from threading import Event, Thread

        chunk_queue: "Queue[Any]" = Queue(maxsize=64)
        stream_error: "List[Exception]" = []
        stream_done = object()  # sentinel
        stop_pump = Event()

        def put_chunk(chunk: Any) -> None:
            while not stop_pump.is_set():
                try:
                    chunk_queue.put(chunk, timeout=0.05)
                    return
                except Full:
                    continue

        def _pump() -> None:
            try:
                for chunk in stream_response:
                    if stop_pump.is_set():
                        break
                    put_chunk(chunk)
            except Exception as exc:  # noqa: BLE001
                stream_error.append(exc)
            finally:
                close = getattr(stream_response, "close", None)
                if close is not None:
                    close()
                put_chunk(stream_done)

        pump_thread = Thread(
            target=_pump,
            name=f"llm-stream-pump-{model}",
            daemon=True,
        )
        pump_thread.start()

        first_chunk_deadline = first_chunk_timeout
        last_chunk_at = time.monotonic()
        try:
            while True:
                try:
                    item = chunk_queue.get(timeout=first_chunk_deadline)
                except Empty:
                    # Either the first chunk hasn't arrived yet, or the
                    # gap between chunks exceeded idle_chunk_timeout.
                    if chars_received == 0:
                        raise _LiteLLMStreamError(
                            f"{model} stream first-chunk timeout after "
                            f"{first_chunk_timeout:.1f}s",
                            partial_received=False,
                        ) from None
                    raise _LiteLLMStreamError(
                        f"{model} stream idle for {idle_chunk_timeout:.1f}s "
                        f"after {chars_received} chars",
                        partial_received=True,
                    ) from None

                if item is stream_done:
                    break

                # We have a chunk. Reset deadline for subsequent chunks.
                now = time.monotonic()
                last_chunk_at = now
                first_chunk_deadline = idle_chunk_timeout

                chunk_usage = extract_usage_payload(item)
                normalized_usage = self._normalize_usage(
                    chunk_usage,
                    model=usage_model or model,
                    provider=provider,
                )
                if normalized_usage:
                    usage = normalized_usage

                delta_text = self._extract_stream_text(item)
                if not delta_text:
                    continue

                chunks.append(delta_text)
                chars_received += len(delta_text)
                if progress_callback and chars_received >= next_emit_at:
                    progress_callback(chars_received)
                    next_emit_at = chars_received + 160
        except _LiteLLMStreamError:
            raise
        except Exception as exc:
            raise _LiteLLMStreamError(
                f"{model} stream interrupted: {exc}",
                partial_received=chars_received > 0,
            ) from exc
        finally:
            stop_pump.set()
            close = getattr(stream_response, "close", None)
            if close is not None:
                try:
                    close()
                except (RuntimeError, ValueError):
                    # A generator currently being read closes in the pump's
                    # finally; its permit remains held until that happens.
                    logger.debug("Stream is still being consumed; awaiting transport cleanup")
            pump_thread.join(timeout=1.0)

        if stream_error and chars_received == 0:
            # Propagate the underlying iterator error so the caller can decide
            # whether to fall back.
            raise _LiteLLMStreamError(
                f"{model} stream failed before any chunk: {stream_error[0]!r}",
                partial_received=False,
            )

        # No chunks at all → "fake SSE" pattern: provider returned an
        # empty iterator immediately, which we treat as a failure so the
        # caller can fall back to non-stream.
        if chars_received == 0:
            raise _LiteLLMStreamError(
                f"{model} stream returned 0 chunks "
                f"(first_chunk_timeout={first_chunk_timeout:.0f}s)",
                partial_received=False,
            )

        response_text = "".join(chunks).strip()
        if not response_text:
            raise _LiteLLMStreamError(
                f"{model} stream returned empty response",
                partial_received=False,
            )

        # "Fake success" guard: if the response is too short to plausibly be
        # a complete analysis for a 4k+ char prompt, treat as failure.
        if len(response_text) < min_response_chars:
            raise _LiteLLMStreamError(
                f"{model} stream returned suspiciously short response "
                f"({len(response_text)} chars < {min_response_chars})",
                partial_received=True,
            )

        if progress_callback and chars_received > 0:
            progress_callback(chars_received)

        return response_text, usage

    def _call_litellm(
        self,
        prompt: str,
        generation_config: dict[str, Any],
        *,
        system_prompt: Optional[str] = None,
        stream: bool = False,
        stream_progress_callback: Optional[Callable[[int], None]] = None,
        response_validator: Optional[Callable[[str], None]] = None,
    ) -> Tuple[str, str, Dict[str, Any]]:
        """Call LLM via litellm with fallback across configured models.

        When channels/YAML are configured, every model goes through the Router
        (which handles per-model key selection, load balancing, and retries).
        In legacy mode, the primary model may use the Router while fallback
        models fall back to direct litellm.completion().

        Args:
            prompt: User prompt text.
            generation_config: Dict with optional keys: temperature, max_output_tokens, max_tokens.
            response_validator: Optional callable that accepts the raw response text and raises
                an exception if the response is unacceptable (e.g. not valid JSON).  When it
                raises, the current model is treated as failed and the next fallback model is
                tried.  If all models fail validation, :class:`_AllModelsFailedError` is raised
                with ``last_response_text`` set to the last raw response received.

        Returns:
            Tuple of (response text, model_used, usage). On success model_used is the full model
            name and usage is a dict with prompt_tokens, completion_tokens, total_tokens.
        """
        config = self._get_runtime_config()
        max_tokens = (
            generation_config.get("max_output_tokens")
            or generation_config.get("max_tokens")
            or 8192
        )
        requested_temperature = generation_config.get("temperature", 0.7)

        models_to_try = [config.litellm_model] + (config.litellm_fallback_models or [])
        models_to_try = [m for m in models_to_try if m]

        use_channel_router = self._has_channel_config(config)

        last_error: Optional[Exception] = None
        last_response_text: Optional[str] = None
        last_model: Optional[str] = None
        last_usage: Dict[str, Any] = {}
        effective_system_prompt = system_prompt or self.TEXT_SYSTEM_PROMPT
        router_model_names = set(get_configured_llm_models(config.llm_model_list))
        for model in models_to_try:
            recovery_model_list = config.llm_model_list
            legacy_router_model_list = (
                getattr(self, "_legacy_router_model_list", None) or []
            )
            if (
                legacy_router_model_list
                and model == config.litellm_model
                and not use_channel_router
            ):
                recovery_model_list = legacy_router_model_list
            usage_model, usage_provider = resolved_model_provider_identity(
                model, recovery_model_list
            )

            try:
                model_short = model.split("/")[-1] if "/" in model else model
                extra = get_thinking_extra_body(model_short)
                call_kwargs: Dict[str, Any] = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": effective_system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": max_tokens,
                }
                request_timeout_seconds = generation_config.get("timeout_seconds")
                if request_timeout_seconds is None:
                    request_timeout_seconds = getattr(
                        config, "llm_stream_response_timeout", 90.0
                    )
                call_kwargs["timeout"] = float(request_timeout_seconds)
                if extra:
                    call_kwargs["extra_body"] = extra
                uses_router = (
                    use_channel_router and self._router and model in router_model_names
                ) or (
                    self._router
                    and model == config.litellm_model
                    and not use_channel_router
                )
                if not uses_router:
                    try:
                        keys = get_api_keys_for_model(model, config)
                    except AttributeError:
                        keys = []
                    if keys:
                        call_kwargs["api_key"] = keys[0]
                    try:
                        call_kwargs.update(extra_litellm_params(model, config))
                    except AttributeError:
                        pass
                call_kwargs = apply_litellm_generation_params(
                    call_kwargs,
                    model,
                    requested_temperature,
                    model_list=recovery_model_list,
                )

                _stream_text: Optional[str] = None
                _stream_usage: Dict[str, Any] = {}

                if stream:
                    try:
                        stream_response = call_litellm_with_param_recovery(
                            lambda kwargs: self._dispatch_litellm_completion(
                                model,
                                kwargs,
                                config=config,
                                use_channel_router=use_channel_router,
                                router_model_names=router_model_names,
                            ),
                            model=model,
                            call_kwargs={**call_kwargs, "stream": True},
                            model_list=recovery_model_list,
                            cache_recovery=False,
                            logger=logger,
                        )
                        _stream_text, _stream_usage = self._consume_litellm_stream(
                            stream_response,
                            model=model,
                            usage_model=usage_model,
                            provider=usage_provider,
                            progress_callback=stream_progress_callback,
                        )
                    except _LiteLLMStreamError as exc:
                        if exc.partial_received:
                            logger.warning(
                                "[LiteLLM] %s stream failed after partial output, retrying non-stream for same model: %s",
                                model,
                                exc,
                            )
                        else:
                            logger.warning(
                                "[LiteLLM] %s stream unavailable before first chunk, falling back to non-stream: %s",
                                model,
                                exc,
                            )
                        last_error = exc
                    except Exception as exc:
                        logger.warning(
                            "[LiteLLM] %s stream request failed before first chunk, falling back to non-stream: %s",
                            model,
                            exc,
                        )

                if _stream_text is not None:
                    # P0-2 fix: MiniMax-M3 (and some other providers) return
                    # a "fake SSE" stream that delivers the full payload in
                    # one chunk and never pushes the usage object. To capture
                    # usage without doubling latency, we extract usage from
                    # the chunks we already received (MiniMax's hidden_params
                    # may carry it) and otherwise mark the call as
                    # "usage_unknown" so dashboards can still distinguish
                    # "billable but unaccounted" from "free". A follow-up
                    # call would double the latency, so we don't auto-fire
                    # one here.
                    stream_had_usage = bool(
                        _stream_usage
                        and (
                            _stream_usage.get("prompt_tokens")
                            or _stream_usage.get("completion_tokens")
                            or _stream_usage.get("total_tokens")
                        )
                    )
                    if not stream_had_usage:
                        # P2 fix (2026-07-17): use tiktoken cl100k_base for
                        # accurate token estimation instead of the brittle
                        # chars/4 heuristic (which over-estimated Chinese +
                        # markdown by 25-50%). Falls back to chars/4 only
                        # if tiktoken is unavailable.
                        try:
                            import tiktoken

                            _enc = tiktoken.get_encoding("cl100k_base")
                            approx_prompt = 0
                            _msgs = call_kwargs.get("messages") or []
                            logger.debug(
                                "[P2-debug] %s messages count=%d",
                                model,
                                len(_msgs),
                            )
                            for idx, m in enumerate(_msgs):
                                _content = m.get("content") or ""
                                if isinstance(_content, list):
                                    for _part in _content:
                                        if isinstance(_part, dict):
                                            _content = _part.get("text") or ""
                                            break
                                _str_content = str(_content)
                                _content_tokens = len(_enc.encode(_str_content))
                                approx_prompt += 4 + _content_tokens
                                logger.debug(
                                    "[P2-debug] %s msg[%d] role=%s content_chars=%d content_tokens=%d",
                                    model,
                                    idx,
                                    m.get("role"),
                                    len(_str_content),
                                    _content_tokens,
                                )
                            _stream_text_tokens = len(_enc.encode(_stream_text))
                            logger.debug(
                                "[P2-debug] %s stream_text_chars=%d stream_text_tokens=%d",
                                model,
                                len(_stream_text),
                                _stream_text_tokens,
                            )
                            approx_completion = _stream_text_tokens
                        except Exception:
                            approx_prompt = sum(
                                len(m.get("content") or "")
                                for m in call_kwargs.get("messages", [])
                            )
                            approx_completion = len(_stream_text)
                        _stream_usage = {
                            "prompt_tokens": approx_prompt,
                            "completion_tokens": approx_completion,
                            "total_tokens": approx_prompt + approx_completion,
                            "usage_source": "estimated_from_chars",
                        }
                        logger.info(
                            "[LiteLLM] %s stream had no usage object; "
                            "estimated prompt≈%d completion≈%d (chars/4)",
                            model,
                            _stream_usage["prompt_tokens"],
                            _stream_usage["completion_tokens"],
                        )

                    last_response_text = _stream_text
                    last_model = model
                    _stream_usage = attach_message_hmacs(
                        _stream_usage, call_kwargs["messages"]
                    )
                    last_usage = _stream_usage
                    if response_validator is not None:
                        response_validator(_stream_text)
                    return _stream_text, model, _stream_usage

                response = call_litellm_with_param_recovery(
                    lambda kwargs: self._dispatch_litellm_completion(
                        model,
                        kwargs,
                        config=config,
                        use_channel_router=use_channel_router,
                        router_model_names=router_model_names,
                    ),
                    model=model,
                    call_kwargs=call_kwargs,
                    model_list=recovery_model_list,
                    logger=logger,
                )

                content = self._extract_completion_text(response)
                if content:
                    usage = self._normalize_usage(
                        extract_usage_payload(response),
                        model=usage_model or model,
                        provider=usage_provider,
                        messages=call_kwargs["messages"],
                    )
                    last_response_text = content
                    last_model = model
                    last_usage = usage
                    if response_validator is not None:
                        response_validator(content)
                    return (content, model, usage)
                raise ValueError("LLM returned empty response")

            except Exception as e:
                logger.warning(f"[LiteLLM] {model} failed: {e}")
                last_error = e
                continue

        raise _AllModelsFailedError(
            f"All LLM models failed (tried {len(models_to_try)} model(s)). Last error: {last_error}",
            last_response_text=last_response_text,
            last_model=last_model,
            last_usage=last_usage,
        )

    def generate_text(
        self,
        prompt: str,
        max_tokens: int = 2048,
        temperature: float = 0.7,
    ) -> Optional[str]:
        """Public entry point for free-form text generation.

        External callers (e.g. MarketAnalyzer) must use this method instead of
        calling _call_litellm() directly or accessing private attributes such as
        _litellm_available, _router, _model, _use_openai, or _use_anthropic.

        Args:
            prompt:      Text prompt to send to the LLM.
            max_tokens:  Maximum tokens in the response (default 2048).
            temperature: Sampling temperature (default 0.7).

        Returns:
            Response text, or None if the LLM call fails (error is logged).
        """
        try:
            result = self._call_litellm(
                prompt,
                generation_config={
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
            )
            if isinstance(result, tuple):
                text, model_used, usage = result
                persist_llm_usage(usage, model_used, call_type="market_review")
                return text
            return result
        except Exception as exc:
            logger.error("[generate_text] LLM call failed: %s", exc)
            return None

    def analyze(
        self,
        context: Dict[str, Any],
        news_context: Optional[str] = None,
        progress_callback: Optional[Callable[[int, str], None]] = None,
        stream_progress_callback: Optional[Callable[[int], None]] = None,
        analysis_context_pack_summary: Optional[str] = None,
    ) -> AnalysisResult:
        """
        分析单只股票

        流程：
        1. 格式化输入数据（技术面 + 新闻）
        2. 调用 Gemini API（带重试和模型切换）
        3. 解析 JSON 响应
        4. 返回结构化结果

        Args:
            context: 从 storage.get_analysis_context() 获取的上下文数据
            news_context: 预先搜索的新闻内容（可选）

        Returns:
            AnalysisResult 对象
        """

        def _emit_progress(progress: int, message: str) -> None:
            if progress_callback is None:
                return
            try:
                progress_callback(progress, message)
            except Exception as exc:
                logger.debug("[analyzer] progress callback skipped: %s", exc)

        code = context.get("code", "Unknown")
        config = self._get_runtime_config()
        report_language = normalize_report_language(
            getattr(config, "report_language", "zh")
        )
        system_prompt = self._get_analysis_system_prompt(
            report_language, stock_code=code
        )

        # 请求前增加延时（防止连续请求触发限流）
        request_delay = config.gemini_request_delay
        if request_delay > 0:
            logger.debug(f"[LLM] 请求前等待 {request_delay:.1f} 秒...")
            _emit_progress(65, f"{code}：LLM 请求前等待 {request_delay:.1f} 秒")
            time.sleep(request_delay)

        # 优先从上下文获取股票名称（由 main.py 传入）
        name = context.get("stock_name")
        if not name or name.startswith("股票"):
            # 备选：从 realtime 中获取
            if "realtime" in context and context["realtime"].get("name"):
                name = context["realtime"]["name"]
            else:
                # 最后从映射表获取
                name = STOCK_NAME_MAP.get(code, f"股票{code}")

        # 如果模型不可用，返回默认结果
        if not self.is_available():
            return AnalysisResult(
                code=code,
                name=name,
                sentiment_score=50,
                trend_prediction="Sideways" if report_language == "en" else "震荡",
                operation_advice="Hold" if report_language == "en" else "持有",
                confidence_level="Low" if report_language == "en" else "低",
                analysis_summary="AI analysis is unavailable because no API key is configured."
                if report_language == "en"
                else "AI 分析功能未启用（未配置 API Key）",
                risk_warning="Configure an LLM API key (GEMINI_API_KEY/ANTHROPIC_API_KEY/OPENAI_API_KEY) and retry."
                if report_language == "en"
                else "请配置 LLM API Key（GEMINI_API_KEY/ANTHROPIC_API_KEY/OPENAI_API_KEY）后重试",
                success=False,
                error_message="LLM API key is not configured"
                if report_language == "en"
                else "LLM API Key 未配置",
                model_used=None,
                report_language=report_language,
            )

        try:
            # 格式化输入（包含技术面数据和新闻）
            prompt = self._format_prompt(
                context,
                name,
                news_context,
                report_language=report_language,
                analysis_context_pack_summary=analysis_context_pack_summary,
            )

            config = self._get_runtime_config()
            model_name = config.litellm_model or "unknown"
            logger.info(f"========== AI 分析 {name}({code}) ==========")
            logger.info(f"[LLM配置] 模型: {model_name}")
            logger.info(f"[LLM配置] Prompt 长度: {len(prompt)} 字符")
            logger.info(f"[LLM配置] 是否包含新闻: {'是' if news_context else '否'}")

            # 记录完整 prompt 到日志（INFO级别记录摘要，DEBUG记录完整）
            prompt_preview = prompt[:500] + "..." if len(prompt) > 500 else prompt
            logger.info(f"[LLM Prompt 预览]\n{prompt_preview}")
            logger.debug(
                f"=== 完整 Prompt ({len(prompt)}字符) ===\n{prompt}\n=== End Prompt ==="
            )

            # 设置生成配置
            generation_config = {
                "temperature": config.llm_temperature,
                "max_output_tokens": 8192,
            }

            logger.info(f"[LLM调用] 开始调用 {model_name}...")
            _emit_progress(68, f"{name}：LLM 已接收请求，等待响应")

            # 使用 litellm 调用（支持完整性校验重试）
            current_prompt = prompt
            retry_count = 0
            max_retries = (
                config.report_integrity_retry if config.report_integrity_enabled else 0
            )

            while True:
                start_time = time.time()
                try:
                    response_text, model_used, llm_usage = self._call_litellm(
                        current_prompt,
                        generation_config,
                        system_prompt=system_prompt,
                        stream=True,
                        stream_progress_callback=stream_progress_callback,
                        response_validator=self._validate_json_response,
                    )
                except _AllModelsFailedError as exc:
                    if exc.last_response_text is not None:
                        logger.warning(
                            "[LLM JSON] %s(%s): all models returned invalid JSON, using text fallback",
                            name,
                            code,
                        )
                        response_text = exc.last_response_text
                        model_used = cast(str, exc.last_model)
                        llm_usage = exc.last_usage
                    else:
                        raise
                elapsed = time.time() - start_time

                # 记录响应信息
                logger.info(
                    f"[LLM返回] {model_name} 响应成功, 耗时 {elapsed:.2f}s, 响应长度 {len(response_text)} 字符"
                )
                response_preview = (
                    response_text[:300] + "..."
                    if len(response_text) > 300
                    else response_text
                )
                logger.info(f"[LLM返回 预览]\n{response_preview}")
                logger.debug(
                    f"=== {model_name} 完整响应 ({len(response_text)}字符) ===\n{response_text}\n=== End Response ==="
                )
                # Keep parser/retry progress monotonic so task progress/message never "goes backward".
                parse_progress = min(99, 93 + retry_count * 2)
                _emit_progress(parse_progress, f"{name}：LLM 返回完成，正在解析 JSON")

                # 解析响应
                result = self._parse_response(response_text, code, name)
                result.raw_response = response_text
                result.search_performed = bool(news_context)
                result.market_snapshot = self._build_market_snapshot(context)
                result.model_used = model_used
                result.report_language = report_language
                normalize_chip_structure_availability(result, context.get("chip"))

                # 内容完整性校验（可选）
                if not config.report_integrity_enabled:
                    break
                require_phase_decision = isinstance(
                    context.get("market_phase_context"), dict
                )
                pass_integrity, missing_fields = self._check_content_integrity(
                    result,
                    require_phase_decision=require_phase_decision,
                )
                if pass_integrity:
                    break
                if retry_count < max_retries:
                    current_prompt = self._build_integrity_retry_prompt(
                        prompt,
                        response_text,
                        missing_fields,
                        report_language=report_language,
                    )
                    retry_count += 1
                    logger.info(
                        "[LLM完整性] 必填字段缺失 %s，第 %d 次补全重试",
                        missing_fields,
                        retry_count,
                    )
                    retry_progress = min(99, 92 + retry_count * 2)
                    _emit_progress(
                        retry_progress,
                        f"{name}：报告字段不完整，正在补全重试（{retry_count}/{max_retries}）",
                    )
                else:
                    self._apply_placeholder_fill(result, missing_fields)
                    logger.warning(
                        "[LLM完整性] 必填字段缺失 %s，已占位补全，不阻塞流程",
                        missing_fields,
                    )
                    break

            persist_llm_usage(
                llm_usage,
                model_used or "unknown",
                call_type="analysis",
                stock_code=code,
            )

            logger.info(
                f"[LLM解析] {name}({code}) 分析完成: {result.trend_prediction}, 评分 {result.sentiment_score}"
            )

            return result

        except Exception as e:
            logger.error(f"AI 分析 {name}({code}) 失败: {e}")
            return AnalysisResult(
                code=code,
                name=name,
                sentiment_score=50,
                trend_prediction="Sideways" if report_language == "en" else "震荡",
                operation_advice="Hold" if report_language == "en" else "持有",
                confidence_level="Low" if report_language == "en" else "低",
                analysis_summary=(
                    f"Analysis failed: {str(e)[:100]}"
                    if report_language == "en"
                    else f"分析过程出错: {str(e)[:100]}"
                ),
                risk_warning="Analysis failed. Please retry later or review manually."
                if report_language == "en"
                else "分析失败，请稍后重试或手动分析",
                success=False,
                error_message=str(e),
                model_used=None,
                report_language=report_language,
            )

    def _format_prompt(
        self,
        context: Dict[str, Any],
        name: str,
        news_context: Optional[str] = None,
        report_language: str = "zh",
        analysis_context_pack_summary: Optional[str] = None,
    ) -> str:
        """
        格式化分析提示词（决策仪表盘 v2.0）

        包含：技术指标、实时行情（量比/换手率）、筹码分布、趋势分析、新闻

        Args:
            context: 技术面数据上下文（包含增强数据）
            name: 股票名称（默认值，可能被上下文覆盖）
            news_context: 预先搜索的新闻内容
        """
        code = context.get("code", "Unknown")
        report_language = normalize_report_language(report_language)
        _, _, use_legacy_default_prompt = self._get_skill_prompt_sections()

        # 优先使用上下文中的股票名称（从 realtime_quote 获取）
        stock_name = context.get("stock_name", name)
        if not stock_name or stock_name == f"股票{code}":
            stock_name = STOCK_NAME_MAP.get(code, f"股票{code}")

        today = context.get("today", {})
        unknown_text = get_unknown_text(report_language)
        no_data_text = get_no_data_text(report_language)
        quote_section_title, close_price_label = _phase_aware_quote_labels(context)
        hide_regular_session_ohlc = _should_hide_regular_session_ohlc(context)
        realtime_overlay_quote = (
            hide_regular_session_ohlc and _today_has_realtime_overlay(today)
        )
        pct_chg_label = "实时涨跌幅" if realtime_overlay_quote else "涨跌幅"
        volume_label = "实时成交量" if realtime_overlay_quote else "成交量"
        amount_label = "实时成交额" if realtime_overlay_quote else "成交额"
        quote_rows = [
            f"| {close_price_label} | {today.get('close', 'N/A')} 元 |",
        ]
        if not hide_regular_session_ohlc:
            quote_rows.extend(
                [
                    f"| 开盘价 | {today.get('open', 'N/A')} 元 |",
                    f"| 最高价 | {today.get('high', 'N/A')} 元 |",
                    f"| 最低价 | {today.get('low', 'N/A')} 元 |",
                ]
            )
        quote_rows.extend(
            [
                f"| {pct_chg_label} | {today.get('pct_chg', 'N/A')}% |",
                f"| {volume_label} | {self._format_volume(today.get('volume'))} |",
                f"| {amount_label} | {self._format_amount(today.get('amount'))} |",
            ]
        )
        quote_rows_text = "\n".join(quote_rows)

        # ========== 构建决策仪表盘格式的输入 ==========
        prompt = f"""# 决策仪表盘分析请求

## 📊 股票基础信息
| 项目 | 数据 |
|------|------|
| 股票代码 | **{code}** |
| 股票名称 | **{stock_name}** |
| 分析日期 | {context.get("date", unknown_text)} |

---
"""
        prompt += format_market_phase_prompt_section(
            context.get("market_phase_context"),
            report_language=report_language,
        )
        daily_market_context_section = format_daily_market_context_prompt_section(
            context.get("daily_market_context"),
            report_language=report_language,
        )
        if daily_market_context_section:
            prompt += daily_market_context_section
        if (
            isinstance(analysis_context_pack_summary, str)
            and analysis_context_pack_summary
        ):
            prompt += analysis_context_pack_summary
        prompt += f"""

## 📈 技术面数据

### {quote_section_title}
| 指标 | 数值 |
|------|------|
{quote_rows_text}

### 均线系统（关键判断指标）
| 均线 | 数值 | 说明 |
|------|------|------|
| MA5 | {today.get("ma5", "N/A")} | 短期趋势线 |
| MA10 | {today.get("ma10", "N/A")} | 中短期趋势线 |
| MA20 | {today.get("ma20", "N/A")} | 中期趋势线 |
| 均线形态 | {context.get("ma_status", unknown_text)} | 多头/空头/缠绕 |
"""

        # 添加实时行情数据（量比、换手率等）
        if "realtime" in context:
            rt = context["realtime"]
            prompt += f"""
### 实时行情增强数据
| 指标 | 数值 | 解读 |
|------|------|------|
| 当前价格 | {rt.get("price", "N/A")} 元 | |
| **量比** | **{rt.get("volume_ratio", "N/A")}** | {rt.get("volume_ratio_desc", "")} |
| **换手率** | **{rt.get("turnover_rate", "N/A")}%** | |
| 市盈率(动态) | {rt.get("pe_ratio", "N/A")} | |
| 市净率 | {rt.get("pb_ratio", "N/A")} | |
| 总市值 | {self._format_amount(rt.get("total_mv"))} | |
| 流通市值 | {self._format_amount(rt.get("circ_mv"))} | |
| 60日涨跌幅 | {rt.get("change_60d", "N/A")}% | 中期表现 |
"""

        # 添加财报与分红（价值投资口径）
        fundamental_context = (
            context.get("fundamental_context") if isinstance(context, dict) else None
        )
        earnings_block = (
            fundamental_context.get("earnings", {})
            if isinstance(fundamental_context, dict)
            else {}
        )
        earnings_data = (
            earnings_block.get("data", {}) if isinstance(earnings_block, dict) else {}
        )
        financial_report = (
            earnings_data.get("financial_report", {})
            if isinstance(earnings_data, dict)
            else {}
        )
        dividend_metrics = (
            earnings_data.get("dividend", {}) if isinstance(earnings_data, dict) else {}
        )
        if isinstance(financial_report, dict) or isinstance(dividend_metrics, dict):
            financial_report = (
                financial_report if isinstance(financial_report, dict) else {}
            )
            dividend_metrics = (
                dividend_metrics if isinstance(dividend_metrics, dict) else {}
            )
            ttm_yield = dividend_metrics.get("ttm_dividend_yield_pct", "N/A")
            ttm_cash = dividend_metrics.get("ttm_cash_dividend_per_share", "N/A")
            ttm_count = dividend_metrics.get("ttm_event_count", "N/A")
            report_date = financial_report.get("report_date", "N/A")
            prompt += f"""
### 财报与分红（价值投资口径）
| 指标 | 数值 | 说明 |
|------|------|------|
| 最近报告期 | {report_date} | 来自结构化财报字段 |
| 营业收入 | {financial_report.get("revenue", "N/A")} | |
| 归母净利润 | {financial_report.get("net_profit_parent", "N/A")} | |
| 经营现金流 | {financial_report.get("operating_cash_flow", "N/A")} | |
| ROE | {financial_report.get("roe", "N/A")} | |
| 近12个月每股现金分红 | {ttm_cash} | 仅现金分红、税前口径 |
| TTM 股息率 | {ttm_yield} | 公式：近12个月每股现金分红 / 当前价格 × 100% |
| TTM 分红事件数 | {ttm_count} | |

> 若上述字段为 N/A 或缺失，请明确写“数据缺失，无法判断”，禁止编造。
"""

        # 基本面快照：把 growth 块（revenue_yoy / net_profit_yoy / roe / gross_margin）
        # 透传给 LLM，让分析基于最新结构化数据而不是仅看 K 线。
        # as_of 优先取 fundamental_context["as_of"]（本次修复 1+2 注入的报告期），
        # 缺失时回退 fundamental_context["as_of_date"]（DB 列兼容旧快照）。
        # 仅在有任意非 None 字段或 as_of 已知时才渲染，避免空表。
        growth_block = (
            fundamental_context.get("growth", {})
            if isinstance(fundamental_context, dict)
            and isinstance(fundamental_context.get("growth"), dict)
            else {}
        )
        growth_data = (
            growth_block.get("data", {}) if isinstance(growth_block, dict) else {}
        )
        growth_as_of = None
        if isinstance(fundamental_context, dict):
            growth_as_of = fundamental_context.get("as_of") or fundamental_context.get(
                "as_of_date"
            )
        growth_non_null: Dict[str, Any] = {}
        if isinstance(growth_data, dict):
            growth_non_null = {k: v for k, v in growth_data.items() if v is not None}
        if growth_non_null or growth_as_of:
            from src.report_language import (
                get_fundamental_snapshot_heading,
                get_fundamental_snapshot_disclaimer,
                get_fundamental_snapshot_table_header,
                get_report_labels,
            )

            _labels = get_report_labels(report_language)
            metric_label, value_label = get_fundamental_snapshot_table_header(
                report_language
            )

            def _pct(v: Any) -> str:
                if v is None:
                    return "N/A"
                try:
                    return f"{float(v):.2f}%"
                except (TypeError, ValueError):
                    return "N/A"

            heading = get_fundamental_snapshot_heading(report_language, growth_as_of)
            disclaimer = get_fundamental_snapshot_disclaimer(
                report_language, growth_as_of
            )
            prompt += f"""
### {heading}
| {metric_label} | {value_label} |
|------|------|
| {_labels.get("revenue_yoy_label", "营收同比")} | {_pct(growth_non_null.get("revenue_yoy"))} |
| {_labels.get("net_profit_yoy_label", "净利同比")} | {_pct(growth_non_null.get("net_profit_yoy"))} |
| {_labels.get("roe_label", "ROE")} | {_pct(growth_non_null.get("roe"))} |
| {_labels.get("gross_margin_label", "毛利率")} | {_pct(growth_non_null.get("gross_margin"))} |

> {disclaimer}
"""

        capital_flow_block = (
            fundamental_context.get("capital_flow", {})
            if isinstance(fundamental_context, dict)
            else {}
        )
        capital_flow_data = (
            capital_flow_block.get("data", {})
            if isinstance(capital_flow_block, dict)
            else {}
        )
        stock_flow = (
            capital_flow_data.get("stock_flow", {})
            if isinstance(capital_flow_data, dict)
            else {}
        )
        sector_flow = (
            capital_flow_data.get("sector_rankings", {})
            if isinstance(capital_flow_data, dict)
            else {}
        )
        has_capital_flow = (
            isinstance(stock_flow, dict)
            and any(v is not None for v in stock_flow.values())
        ) or (
            isinstance(sector_flow, dict)
            and (sector_flow.get("top") or sector_flow.get("bottom"))
        )
        if has_capital_flow:
            top_sectors = (
                sector_flow.get("top", []) if isinstance(sector_flow, dict) else []
            )
            bottom_sectors = (
                sector_flow.get("bottom", []) if isinstance(sector_flow, dict) else []
            )
            top_sector_text = (
                "、".join(
                    str(item.get("name", "")).strip()
                    for item in top_sectors[:3]
                    if isinstance(item, dict) and str(item.get("name", "")).strip()
                )
                or "N/A"
            )
            bottom_sector_text = (
                "、".join(
                    str(item.get("name", "")).strip()
                    for item in bottom_sectors[:3]
                    if isinstance(item, dict) and str(item.get("name", "")).strip()
                )
                or "N/A"
            )
            prompt += f"""
### 主力资金流向（操作建议过滤器）
| 指标 | 数值 | 决策含义 |
|------|------|----------|
| 主力净流入 | {stock_flow.get("main_net_inflow", "N/A")} | 正值偏支持，负值偏压制 |
| 5日净流入 | {stock_flow.get("inflow_5d", "N/A")} | 用于判断资金持续性 |
| 10日净流入 | {stock_flow.get("inflow_10d", "N/A")} | 用于判断资金持续性 |
| 资金流入靠前板块 | {top_sector_text} | 板块资金共振参考 |
| 资金流出靠前板块 | {bottom_sector_text} | 板块风险参考 |

> 资金流向只能作为价格位置的过滤器：接近压力且主力流出时不得追买；接近支撑且未放量跌破时，优先判断为持有观察、震荡或洗盘观察。
"""

        # 添加筹码分布数据
        if "chip" in context:
            chip = context["chip"]
            profit_ratio = chip.get("profit_ratio", 0)
            prompt += f"""
### 筹码分布数据（效率指标）
| 指标 | 数值 | 健康标准 |
|------|------|----------|
| **获利比例** | **{profit_ratio:.1%}** | 70-90%时警惕 |
| 平均成本 | {chip.get("avg_cost", "N/A")} 元 | 现价应高于5-15% |
| 90%筹码集中度 | {chip.get("concentration_90", 0):.2%} | <15%为集中 |
| 70%筹码集中度 | {chip.get("concentration_70", 0):.2%} | |
| 筹码状态 | {chip.get("chip_status", unknown_text)} | |
"""
        else:
            chip_unavailable_text = get_chip_unavailable_text(report_language)
            chip_instruction = (
                "Do not fabricate profit ratio, average cost, or concentration. Mention chip data "
                "unavailability only once in the report; do not repeat per-field no-data text in `chip_structure`."
                if report_language == "en"
                else "请勿编造获利比例、平均成本或集中度；报告中只说明一次筹码数据不可用，不要把“数据缺失，无法判断”逐字段重复写入 `chip_structure`。"
            )
            prompt += f"""
### 筹码分布数据（效率指标）
> {chip_unavailable_text}
> {chip_instruction}
"""

        # 添加趋势分析结果（仅隐式内建 bull_trend 默认回退保留旧口径）
        if "trend_analysis" in context:
            trend = _sanitize_trend_analysis_for_prompt(
                context["trend_analysis"],
                volume_change_ratio=context.get("volume_change_ratio"),
            )
            consistency_notes = trend.get("prompt_consistency_notes", [])
            if use_legacy_default_prompt:
                bias_warning = (
                    "🚨 超过5%，严禁追高！"
                    if trend.get("bias_ma5", 0) > 5
                    else "✅ 安全范围"
                )
                prompt += f"""
### 趋势分析预判（基于交易理念）
| 指标 | 数值 | 判定 |
|------|------|------|
| 趋势状态 | {trend.get("trend_status", unknown_text)} | |
| 均线排列 | {trend.get("ma_alignment", unknown_text)} | MA5>MA10>MA20为多头 |
| 趋势强度 | {trend.get("trend_strength", 0)}/100 | |
| **乖离率(MA5)** | **{trend.get("bias_ma5", 0):+.2f}%** | {bias_warning} |
| 乖离率(MA10) | {trend.get("bias_ma10", 0):+.2f}% | |
| 量能状态 | {trend.get("volume_status", unknown_text)} | {trend.get("volume_trend", "")} |
| 系统信号 | {trend.get("buy_signal", unknown_text)} | |
| 系统评分 | {trend.get("signal_score", 0)}/100 | |

#### 系统分析理由
**买入理由**：
{chr(10).join("- " + r for r in trend.get("signal_reasons", ["无"])) if trend.get("signal_reasons") else "- 无"}

**风险因素**：
{chr(10).join("- " + r for r in trend.get("risk_factors", ["无"])) if trend.get("risk_factors") else "- 无"}
"""
                if consistency_notes:
                    prompt += f"""

**一致性约束**：
{chr(10).join("- " + note for note in consistency_notes)}
"""
            else:
                bias_warning = (
                    "🚨 偏离较大，需谨慎评估追高风险"
                    if trend.get("bias_ma5", 0) > 5
                    else "✅ 位置相对可控"
                )
                prompt += f"""
### 技术与结构分析（供激活技能判断参考）
| 指标 | 数值 | 说明 |
|------|------|------|
| 趋势状态 | {trend.get("trend_status", unknown_text)} | |
| 均线排列 | {trend.get("ma_alignment", unknown_text)} | 结合激活技能判断结构强弱 |
| 趋势强度 | {trend.get("trend_strength", 0)}/100 | |
| **价格位置(MA5)** | **{trend.get("bias_ma5", 0):+.2f}%** | {bias_warning} |
| 价格位置(MA10) | {trend.get("bias_ma10", 0):+.2f}% | |
| 量能状态 | {trend.get("volume_status", unknown_text)} | {trend.get("volume_trend", "")} |
| 系统信号 | {trend.get("buy_signal", unknown_text)} | |
| 系统评分 | {trend.get("signal_score", 0)}/100 | |

#### 系统分析理由
**支持因素**：
{chr(10).join("- " + r for r in trend.get("signal_reasons", ["无"])) if trend.get("signal_reasons") else "- 无"}

**风险因素**：
{chr(10).join("- " + r for r in trend.get("risk_factors", ["无"])) if trend.get("risk_factors") else "- 无"}
"""
                if consistency_notes:
                    prompt += f"""

**一致性约束**：
{chr(10).join("- " + note for note in consistency_notes)}
"""

        # 添加昨日对比数据
        if "yesterday" in context:
            volume_change = context.get("volume_change_ratio", "N/A")
            prompt += f"""
### 量价变化
- 成交量较昨日变化：{volume_change}倍
- 价格较昨日变化：{context.get("price_change_ratio", "N/A")}%
"""
            parsed_volume_change = _safe_float(volume_change, default=math.nan)
            if math.isfinite(parsed_volume_change) and parsed_volume_change > 10:
                prompt += """
- ⚠️ 量能异常提示：成交量较昨日放大超过10倍，可能受异常数据或一次性冲量影响，必须降权解读，不能机械视为强确认信号
"""

        # 添加新闻搜索结果（重点区域）
        news_window_days: Optional[int] = None
        context_window = context.get("news_window_days")
        try:
            if context_window is not None:
                parsed_window = int(context_window)
                if parsed_window > 0:
                    news_window_days = parsed_window
        except (TypeError, ValueError):
            news_window_days = None

        if news_window_days is None:
            prompt_config = self._get_runtime_config()
            news_window_days = resolve_news_window_days(
                news_max_age_days=getattr(prompt_config, "news_max_age_days", 3),
                news_strategy_profile=getattr(
                    prompt_config, "news_strategy_profile", "short"
                ),
            )
        prompt += """
---

## 📰 舆情情报
"""
        if news_context:
            prompt += f"""
以下是 **{stock_name}({code})** 近{news_window_days}日的新闻搜索结果，请重点提取：
1. 🚨 **风险警报**：减持、处罚、利空
2. 🎯 **利好催化**：业绩、合同、政策
3. 📊 **业绩预期**：年报预告、业绩快报
4. 🕒 **时间规则（强制）**：
   - 输出到 `risk_alerts` / `positive_catalysts` / `latest_news` 的每一条都必须带具体日期（YYYY-MM-DD）
   - 超出近{news_window_days}日窗口的新闻一律忽略
   - 时间未知、无法确定发布日期的新闻一律忽略

```
{news_context}
```
"""
        else:
            prompt += """
未搜索到该股票近期的相关新闻。请主要依据技术面数据进行分析。
"""

        # 注入缺失数据警告
        if context.get("data_missing"):
            prompt += """
⚠️ **数据缺失警告**
由于接口限制，当前无法获取完整的实时行情和技术指标数据。
请 **忽略上述表格中的 N/A 数据**，重点依据 **【📰 舆情情报】** 中的新闻进行基本面和情绪面分析。
在回答技术面问题（如均线、乖离率）时，请直接说明“数据缺失，无法判断”，**严禁编造数据**。
"""

        # 明确的输出要求
        prompt += f"""
---

## ✅ 分析任务

请为 **{stock_name}({code})** 生成【决策仪表盘】，严格按照 JSON 格式输出。
"""
        if context.get("is_index_etf"):
            prompt += """
> ⚠️ **指数/ETF 分析约束**：该标的为指数跟踪型 ETF 或市场指数。
> - 风险分析仅关注：**指数走势、跟踪误差、市场流动性**
> - 严禁将基金公司的诉讼、声誉、高管变动纳入风险警报
> - 业绩预期基于**指数成分股整体表现**，而非基金公司财报
> - `risk_alerts` 中不得出现基金管理人相关的公司经营风险

"""
        prompt += f"""
### ⚠️ 重要：输出正确的股票名称格式
正确的股票名称格式为“股票名称（股票代码）”，例如“贵州茅台（600519）”。
如果上方显示的股票名称为"股票{code}"或不正确，请在分析开头**明确输出该股票的正确中文全称**。
"""
        if use_legacy_default_prompt:
            prompt += f"""

### 重点关注（必须明确回答）：
1. ❓ 是否满足 MA5>MA10>MA20 多头排列？
2. ❓ 当前乖离率是否在安全范围内（<5%）？—— 超过5%必须标注"严禁追高"
3. ❓ 量能是否配合（缩量回调/放量突破）？
4. ❓ 筹码结构是否健康？
5. ❓ 消息面有无重大利空？（减持、处罚、业绩变脸等）
"""
        else:
            prompt += f"""

### 重点关注（必须明确回答）：
1. ❓ 当前结构是否满足激活技能的关键触发条件？
2. ❓ 当前入场位置与风险回报是否合理？若偏离过大，请明确说明等待条件
3. ❓ 量能、波动与筹码结构是否支持当前结论？
4. ❓ 消息面有无重大利空或与技能结论冲突的信息？
5. ❓ 若结论成立，具体触发条件、止损位、观察点分别是什么？
"""
        prompt += f"""

### 决策仪表盘要求：
- **股票名称**：必须输出正确的中文全称（如"贵州茅台"而非"股票600519"）
- **核心结论**：一句话说清该买/该卖/该等
- **持仓分类建议**：空仓者怎么做 vs 持仓者怎么做
- **具体狙击点位**：买入价、止损价、目标价（精确到分）
- **检查清单**：每项用 ✅/⚠️/❌ 标记
- **消息面时间合规**：`latest_news`、`risk_alerts`、`positive_catalysts` 不得包含超出近{news_window_days}日或时间未知的信息
- **技术面一致性**：严禁把“空头排列”和“多头排列”等互斥结论同时当作有效依据；若基本面/事件面与技术面冲突，必须明确写“事件先行、技术待确认”或“基本面偏多，但技术面尚未确认”
 
请输出完整的 JSON 格式决策仪表盘。"""

        if report_language == "en":
            prompt += """

### Output language requirements (highest priority)
- Keep every JSON key exactly as defined above; do not translate keys.
- `decision_type` must remain `buy`, `hold`, or `sell`.
- All human-readable JSON values must be in English.
- This includes `stock_name`, `trend_prediction`, `operation_advice`, `confidence_level`, all nested dashboard text, checklist items, and every summary field.
- Use the common English company name when you are confident. If not, keep the listed company name rather than inventing one.
- When data is missing, explain it in English instead of Chinese.
"""
        else:
            prompt += f"""

### 输出语言要求（最高优先级）
- 所有 JSON 键名必须保持不变，不要翻译键名。
- `decision_type` 必须保持为 `buy`、`hold`、`sell`。
- 所有面向用户的人类可读文本值必须使用中文。
- 当数据缺失时，请使用中文直接说明“{no_data_text}，无法判断”。

## 输出格式硬约束（最高优先级，B1 修复）
- **只输出 1 段合法 JSON 对象**——不要 markdown 围栏（不要 ```json 或 ``` 包裹）、不要思考过程（不要 <think>...</think> 这类标签）、不要自我重写。
- 输出一旦完成就立刻停止；如果一次回答不完整，宁可字段填空也不要重新生成第 2 段 JSON。
- 禁止使用 ```json ... ``` 包裹；你的最终输出必须以 左花括号 开头、以 右花括号 结尾。
- 不要在 JSON 前面或后面写任何解释、注释、Markdown 标题或第二段 JSON。

## 长线六维·主观键值（P2-fix，2026-07-18 + P5-fix，2026-08-03 扩 2 键）
本系统在做"长线六维详情"打分时，还需要你（LLM）**额外输出**以下键，供评分系统量化补充 ⑤ 六维详情 面板。
所有键都写在与决策仪表盘**并列**的根级 JSON 字段 `six_dimension_inputs` 下，按下面 schema 输出。如果对某只股票无法判断，对应值可设为 `null`（不要瞎编）：
- `chain_position`: `upstream | bottleneck | midstream | downstream | commodity`，无上下游则 `null`
- `moat_type`: `patent | technology | brand | network | switching_cost | license | regulatory | null`
- `moat_strength`: `strong | moderate | weak | null`
- `customer_concentration`: 0.0~1.0 赫希曼指数（HHI，前 N 大客户占比平方和），无法估算则 `null`
- `us_china_risk`: `high | medium | low | none`，出口/制裁敏感度（个股产业链层面）
- `us_china_impact`（P5-fix 新增）: `minimal | limited | significant | severe | null`，宏观与地缘层面中美关系对大盘/板块的整体冲击（**与 `us_china_risk` 不同**：前者描述产业链脱钩风险，后者描述整体宏观地缘冲击；不要混淆）
- `regulatory_risk`（P5-fix 新增）: `low | medium | high | null`，国内监管风险（数据安全/反垄断/医药集采/教育双减等政策事件对个股/行业的整体压制程度）
- `chokepoint_type`: `patent | capacity | geo | tech | cert`，是否卡点
- `cognitive_difference`: `market_underestimating | market_fair | market_overestimating`
- `recent_catalysts`: 字符串数组，列最近 30 天内 1~3 个事件驱动；`risk_alerts` 已写过的不要重复
- `news_sentiment`: `positive | neutral | negative` 一句话客观判断
- `chip_concentration`: 0~100 整数筹码集中度百分比（如无数据填 `null`）

**重点**：这些字段仅用于辅助打分 `research_framework.dimensions`，不在 decision dashboard 渲染，对 `sentiment_score / trend_prediction / operation_advice / decision_type` **完全不产生影响**。填错不会扣分；漏写不会扣分；请按事实判断自由发挥。

## 评分锚定（C 修复，2026-07-17）
**基础分 50 分起步**，按下列 6 项硬权重累加；缺失维度越多，可调范围越窄（避免全员偏空 / 偏多漂移）。

| 检查项 | 权重 | 多头加分 | 空头减分 |
|---|---|---:|---:|
| 多头排列（MA5>MA10>MA20）| 30 | +30 | -30 |
| 乖离率安全（bias_ma5 < 5%）| 20 | +20 | -20（>5% 减 20）|
| 量能配合（缩量回调 / 放量突破）| 20 | +20 | -20（放量杀跌）|
| 无重大利空（风险事件）| 10 | 0 | -10 |
| 筹码健康（数据可获时）| 10 | +10 | -10（数据缺失=0）|
| PE 估值合理（PE<50 / PE<-50）| 10 | +10 | -10（极端值）|

**最终 score 必须等于 `50 + Σ(权重项) + 大盘环境调整`**，不得自由打分。

**按数据完整性锁定区间**：

| 缺失维度 | score 强制区间 | trend_prediction 强制 | decision_type 强制 |
|---|---|---|---|
| 0 缺失 | 0-100 自由 | 自由 | 自由 |
| 缺 1 项 | 10-90 | 自由 | 自由 |
| 缺 2 项（含 chip 或 news）| 15-85 | 自由 | 自由 |
| 缺 3 项 | 20-80 | "震荡" | `hold` |
| 缺 4+ 项 | 35-65 | "震荡" + 标注"数据缺失无法判断方向" | `hold` |
| 大盘高风险（high_risk）| 任何区间再额外压 5 分 | - | - |

**显示你的计算过程**（在 `analysis_summary` 字段）："score = 50 + 30(多头) + 20(乖离) + 20(量能) + 0(无利空) + 10(筹码) + 10(PE) + 0(大盘) = 140 → 钳到 100"。
"""

        return prompt

    def _format_volume(self, volume: Optional[float]) -> str:
        """格式化成交量显示"""
        if volume is None:
            return "N/A"
        if volume >= 1e8:
            return f"{volume / 1e8:.2f} 亿股"
        elif volume >= 1e4:
            return f"{volume / 1e4:.2f} 万股"
        else:
            return f"{volume:.0f} 股"

    def _format_amount(self, amount: Optional[float]) -> str:
        """格式化成交额显示"""
        if amount is None:
            return "N/A"
        if amount >= 1e8:
            return f"{amount / 1e8:.2f} 亿元"
        elif amount >= 1e4:
            return f"{amount / 1e4:.2f} 万元"
        else:
            return f"{amount:.0f} 元"

    def _format_percent(self, value: Optional[float]) -> str:
        """格式化百分比显示"""
        if value is None:
            return "N/A"
        try:
            return f"{float(value):.2f}%"
        except (TypeError, ValueError):
            return "N/A"

    def _format_price(self, value: Optional[float]) -> str:
        """格式化价格显示"""
        if value is None:
            return "N/A"
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return "N/A"

    def _build_market_snapshot(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """构建当日行情快照（展示用）"""
        today = context.get("today", {}) or {}
        realtime = context.get("realtime", {}) or {}
        yesterday = context.get("yesterday", {}) or {}

        prev_close = yesterday.get("close")
        close = today.get("close")
        high = today.get("high")
        low = today.get("low")

        amplitude = None
        change_amount = None
        if prev_close not in (None, 0) and high is not None and low is not None:
            try:
                amplitude = (
                    (float(cast(float, high)) - float(cast(float, low)))
                    / float(cast(float, prev_close))
                    * 100
                )
            except (TypeError, ValueError, ZeroDivisionError):
                amplitude = None
        if prev_close is not None and close is not None:
            try:
                change_amount = float(close) - float(prev_close)
            except (TypeError, ValueError):
                change_amount = None

        snapshot = {
            "date": context.get("date", "未知"),
            "close": self._format_price(close),
            "open": self._format_price(today.get("open")),
            "high": self._format_price(high),
            "low": self._format_price(low),
            "prev_close": self._format_price(prev_close),
            "pct_chg": self._format_percent(today.get("pct_chg")),
            "change_amount": self._format_price(change_amount),
            "amplitude": self._format_percent(amplitude),
            "volume": self._format_volume(today.get("volume")),
            "amount": self._format_amount(today.get("amount")),
        }

        if realtime:
            snapshot.update(
                {
                    "price": self._format_price(realtime.get("price")),
                    "volume_ratio": realtime.get("volume_ratio", "N/A"),
                    "turnover_rate": self._format_percent(
                        realtime.get("turnover_rate")
                    ),
                    "source": getattr(
                        realtime.get("source"), "value", realtime.get("source", "N/A")
                    ),
                }
            )

        return snapshot

    def _check_content_integrity(
        self,
        result: AnalysisResult,
        *,
        require_phase_decision: bool = False,
    ) -> Tuple[bool, List[str]]:
        """Delegate to module-level check_content_integrity."""
        return check_content_integrity(
            result, require_phase_decision=require_phase_decision
        )

    def _build_integrity_complement_prompt(
        self, missing_fields: List[str], report_language: str = "zh"
    ) -> str:
        """Build complement instruction for missing mandatory fields."""
        report_language = normalize_report_language(report_language)
        if report_language == "en":
            lines = [
                "### Completion requirements: fill the missing mandatory fields below and output the full JSON again:"
            ]
            for f in missing_fields:
                if f == "sentiment_score":
                    lines.append("- sentiment_score: integer score from 0 to 100")
                elif f == "operation_advice":
                    lines.append("- operation_advice: localized action advice")
                elif f == "analysis_summary":
                    lines.append("- analysis_summary: concise analysis summary")
                elif f == "dashboard.core_conclusion.one_sentence":
                    lines.append(
                        "- dashboard.core_conclusion.one_sentence: one-line decision"
                    )
                elif f == "dashboard.intelligence.risk_alerts":
                    lines.append(
                        "- dashboard.intelligence.risk_alerts: risk alert list (can be empty)"
                    )
                elif f == "dashboard.battle_plan.sniper_points.stop_loss":
                    lines.append(
                        "- dashboard.battle_plan.sniper_points.stop_loss: stop-loss level"
                    )
                elif f == "dashboard.phase_decision.phase_context":
                    lines.append(
                        "- dashboard.phase_decision.phase_context: public market phase summary subset"
                    )
                elif f == "dashboard.phase_decision.action_window":
                    lines.append(
                        "- dashboard.phase_decision.action_window: phase-aware action window"
                    )
                elif f == "dashboard.phase_decision.immediate_action":
                    lines.append(
                        "- dashboard.phase_decision.immediate_action: act now / wait / watch / no intraday action"
                    )
                elif f == "dashboard.phase_decision.watch_conditions":
                    lines.append(
                        "- dashboard.phase_decision.watch_conditions: list of watch conditions"
                    )
                elif f == "dashboard.phase_decision.next_check_time":
                    lines.append(
                        "- dashboard.phase_decision.next_check_time: next check point or market-local time"
                    )
                elif f == "dashboard.phase_decision.confidence_reason":
                    lines.append(
                        "- dashboard.phase_decision.confidence_reason: confidence rationale and data limits"
                    )
                elif f == "dashboard.phase_decision.data_limitations":
                    lines.append(
                        "- dashboard.phase_decision.data_limitations: list of phase/data quality limitations"
                    )
            return "\n".join(lines)

        lines = ["### 补全要求：请在上方分析基础上补充以下必填内容，并输出完整 JSON："]
        for f in missing_fields:
            if f == "sentiment_score":
                lines.append("- sentiment_score: 0-100 综合评分")
            elif f == "operation_advice":
                lines.append("- operation_advice: 买入/加仓/持有/减仓/卖出/观望")
            elif f == "analysis_summary":
                lines.append("- analysis_summary: 综合分析摘要")
            elif f == "dashboard.core_conclusion.one_sentence":
                lines.append("- dashboard.core_conclusion.one_sentence: 一句话决策")
            elif f == "dashboard.intelligence.risk_alerts":
                lines.append(
                    "- dashboard.intelligence.risk_alerts: 风险警报列表（可为空数组）"
                )
            elif f == "dashboard.battle_plan.sniper_points.stop_loss":
                lines.append("- dashboard.battle_plan.sniper_points.stop_loss: 止损价")
            elif f == "dashboard.phase_decision.phase_context":
                lines.append(
                    "- dashboard.phase_decision.phase_context: 公开低敏市场阶段摘要子集"
                )
            elif f == "dashboard.phase_decision.action_window":
                lines.append("- dashboard.phase_decision.action_window: 阶段化行动窗口")
            elif f == "dashboard.phase_decision.immediate_action":
                lines.append(
                    "- dashboard.phase_decision.immediate_action: 立即行动/等待确认/观察/无盘中动作"
                )
            elif f == "dashboard.phase_decision.watch_conditions":
                lines.append(
                    "- dashboard.phase_decision.watch_conditions: 观察条件数组"
                )
            elif f == "dashboard.phase_decision.next_check_time":
                lines.append(
                    "- dashboard.phase_decision.next_check_time: 下一次检查点或市场本地时间"
                )
            elif f == "dashboard.phase_decision.confidence_reason":
                lines.append(
                    "- dashboard.phase_decision.confidence_reason: 置信度理由与数据限制"
                )
            elif f == "dashboard.phase_decision.data_limitations":
                lines.append(
                    "- dashboard.phase_decision.data_limitations: 阶段/数据质量限制数组"
                )
        return "\n".join(lines)

    def _build_integrity_retry_prompt(
        self,
        base_prompt: str,
        previous_response: str,
        missing_fields: List[str],
        report_language: str = "zh",
    ) -> str:
        """Build retry prompt using the previous response as the complement baseline."""
        complement = self._build_integrity_complement_prompt(
            missing_fields, report_language=report_language
        )
        previous_output = previous_response.strip()
        if normalize_report_language(report_language) == "en":
            prefix = "### The previous output is below. Complete the missing fields based on that output and return the full JSON again. Do not omit existing fields:"
        else:
            prefix = "### 上一次输出如下，请在该输出基础上补齐缺失字段，并重新输出完整 JSON。不要省略已有字段："
        return "\n\n".join(
            [
                base_prompt,
                prefix,
                previous_output,
                complement,
            ]
        )

    def _apply_placeholder_fill(
        self, result: AnalysisResult, missing_fields: List[str]
    ) -> None:
        """Delegate to module-level apply_placeholder_fill."""
        apply_placeholder_fill(result, missing_fields)

    def _parse_response(
        self, response_text: str, code: str, name: str
    ) -> AnalysisResult:
        """
        解析 Gemini 响应（决策仪表盘版）

        尝试从响应中提取 JSON 格式的分析结果，包含 dashboard 字段
        如果解析失败，尝试智能提取或返回默认结果
        """
        _final_result: Optional[AnalysisResult] = None
        try:
            report_language = normalize_report_language(
                getattr(self._get_runtime_config(), "report_language", "zh")
            )
            # 清理响应文本：移除 markdown 代码块标记
            cleaned_text = response_text
            if "```json" in cleaned_text:
                cleaned_text = cleaned_text.replace("```json", "").replace("```", "")
            elif "```" in cleaned_text:
                cleaned_text = cleaned_text.replace("```", "")

            # 优先使用括号匹配的候选 JSON，避免 LLM 在 prose 中夹带 `{` `}`
            # 或在 JSON 之后再追加一段不完整对象导致的范围错配。
            json_str = self._extract_first_json_object(cleaned_text)

            if json_str:
                # 尝试修复常见的 JSON 问题
                json_str = self._fix_json_string(json_str)

                data = json.loads(json_str)

                # 防御 LLM 偶发返回非 dict（list / 标量 / None）。
                # 当 LLM 在 integrity retry 上下文里把"补字段"误解为"追加新对象"时，
                # 偶尔会输出 [ {...}, {...} ]，导致下面 data.get(...) 抛
                # 'list' object has no attribute 'get'。此处统一降级为 dict，
                # 优先取包含核心字段的元素（多元素时启发式选择），否则取第一个。
                if not isinstance(data, dict):
                    if isinstance(data, list) and data:
                        candidate = next(
                            (
                                x
                                for x in data
                                if isinstance(x, dict)
                                and (
                                    "sentiment_score" in x
                                    or "operation_advice" in x
                                    or "dashboard" in x
                                    or "code" in x
                                )
                            ),
                            None,
                        )
                        if candidate is None:
                            candidate = next(
                                (x for x in data if isinstance(x, dict)),
                                None,
                            )
                        if candidate is not None:
                            logger.warning(
                                "[LLM解析] LLM 返回 JSON list，自动降级为 dict 元素"
                                " (list_len=%d, picked_keys=%s, code=%s)",
                                len(data),
                                sorted(list(candidate.keys()))[:8],
                                candidate.get("code", "?"),
                            )
                            data = candidate
                        else:
                            raise ValueError(
                                f"LLM returned a JSON list with no dict element (len={len(data)})"
                            )
                    else:
                        raise ValueError(
                            f"LLM returned non-object JSON: {type(data).__name__}"
                        )

                # Schema validation (lenient: on failure, continue with raw dict)
                try:
                    AnalysisReportSchema.model_validate(data)
                except Exception as e:
                    logger.warning(
                        "LLM report schema validation failed, continuing with raw dict: %s",
                        str(e)[:100],
                    )

                # P0 fix (2026-07-17): auto-fill missing dashboard substructures
                # (action_checklist, phase_decision, sniper_points, etc.) so
                # silent-degrade outputs (e.g. 601208 with 0 items, placeholder
                # phase_context) get a usable, complete report. The fill is
                # deterministic and the log is attached under _postprocess_log.
                from src.services.report_postprocess import postprocess_report
                from src.services.report_postprocess import (
                    _extract_factor as _ep_factor,
                )

                _missing_dims: List[str] = []
                _chip_ctx = (
                    (data.get("context_snapshot") or {}).get("enhanced_context") or {}
                ).get("chip_context") or {}
                if not _chip_ctx.get("data"):
                    _missing_dims.append("chip")
                if (
                    not data.get("news_content")
                    or len(str(data.get("news_content") or "")) <= 200
                ):
                    _missing_dims.append("news")
                _market_phase = (
                    _ep_factor(data, ("context_snapshot", "market_phase"))
                    or "postmarket"
                )
                data, _postprocess_log = postprocess_report(
                    data,
                    missing_data_dimensions=_missing_dims,
                    market_phase=str(_market_phase),
                )
                if _postprocess_log["fill_count"] > 0:
                    data["_postprocess_log"] = _postprocess_log
                    logger.info(
                        "[LLM解析] auto-filled %d dashboard field(s): %s",
                        _postprocess_log["fill_count"],
                        ", ".join(
                            f["field"] for f in _postprocess_log["filled_fields"]
                        ),
                    )

                # 提取 dashboard 数据
                dashboard = data.get("dashboard", None)

                # 优先使用 AI 返回的股票名称（如果原名称无效或包含代码）
                ai_stock_name = data.get("stock_name")
                if ai_stock_name and (
                    name.startswith("股票") or name == code or "Unknown" in name
                ):
                    name = ai_stock_name

                # 解析所有字段，使用默认值防止缺失
                # 解析 decision_type，如果没有则根据 operation_advice 推断
                decision_type = data.get("decision_type", "")
                if not decision_type:
                    op = data.get(
                        "operation_advice",
                        "Hold" if report_language == "en" else "持有",
                    )
                    decision_type = infer_decision_type_from_advice(op, default="hold")

                explicit_action = data.get("action")
                if explicit_action is None and isinstance(dashboard, dict):
                    explicit_action = dashboard.get("action")

                result = AnalysisResult(
                    code=code,
                    name=name,
                    # 核心指标
                    # P1 fix (2026-07-17): re-compute sentiment_score from
                    # observed factors (ma_alignment / bias_ma5 / volume /
                    # chip / PE / market risk). The LLM's score is recorded
                    # under _reanchor_log.original_score for audit.
                    sentiment_score=int(data.get("sentiment_score", 50)),
                    trend_prediction=data.get(
                        "trend_prediction",
                        "Sideways" if report_language == "en" else "震荡",
                    ),
                    operation_advice=data.get(
                        "operation_advice",
                        "Hold" if report_language == "en" else "持有",
                    ),
                    decision_type=decision_type,
                    confidence_level=localize_confidence_level(
                        data.get(
                            "confidence_level",
                            "Medium" if report_language == "en" else "中",
                        ),
                        report_language,
                    ),
                    report_language=report_language,
                    # 决策仪表盘
                    dashboard=dashboard,
                    # 走势分析
                    trend_analysis=data.get("trend_analysis", ""),
                    short_term_outlook=data.get("short_term_outlook", ""),
                    medium_term_outlook=data.get("medium_term_outlook", ""),
                    # 技术面
                    technical_analysis=data.get("technical_analysis", ""),
                    ma_analysis=data.get("ma_analysis", ""),
                    volume_analysis=data.get("volume_analysis", ""),
                    pattern_analysis=data.get("pattern_analysis", ""),
                    # 基本面
                    fundamental_analysis=data.get("fundamental_analysis", ""),
                    sector_position=data.get("sector_position", ""),
                    company_highlights=data.get("company_highlights", ""),
                    # 情绪面/消息面
                    news_summary=data.get("news_summary", ""),
                    market_sentiment=data.get("market_sentiment", ""),
                    hot_topics=data.get("hot_topics", ""),
                    # 综合
                    analysis_summary=data.get(
                        "analysis_summary",
                        "Analysis completed" if report_language == "en" else "分析完成",
                    ),
                    key_points=data.get("key_points", ""),
                    risk_warning=data.get("risk_warning", ""),
                    buy_reason=data.get("buy_reason", ""),
                    # 元数据
                    search_performed=data.get("search_performed", False),
                    data_sources=data.get(
                        "data_sources",
                        "Technical data" if report_language == "en" else "技术面数据",
                    ),
                    success=True,
                )

                # P1 fix (2026-07-17): re-anchor sentiment_score to a
                # deterministic value computed from the factors the LLM
                # actually reported. This eliminates ±15..55 score drift
                # across runs of the same input. The original LLM score
                # is preserved on the result object via _reanchor_log.
                try:
                    from src.services.score_reanchor import (
                        extract_reanchor_inputs,
                        reanchor_score,
                    )

                    _reanchor_inputs = extract_reanchor_inputs(data)
                    _new_score, _factors, _reanchor_log = reanchor_score(
                        data, _reanchor_inputs
                    )
                    if (
                        _reanchor_log.get("original_score") is not None
                        and _reanchor_log["recomputed_score"]
                        != _reanchor_log["original_score"]
                    ):
                        logger.info(
                            "[ScoreReanchor] %s: %d → %d (delta=%+d, %d adjustments)",
                            code,
                            _reanchor_log["original_score"],
                            _reanchor_log["recomputed_score"],
                            _reanchor_log["delta"],
                            len(_reanchor_log["adjustments"]),
                        )
                    result.sentiment_score = _new_score
                    if hasattr(result, "dashboard") and isinstance(
                        result.dashboard, dict
                    ):
                        result.dashboard["_reanchor_log"] = _reanchor_log
                        result.dashboard["_reanchor_factors"] = _factors
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[ScoreReanchor] %s failed, keeping LLM score: %s",
                        code,
                        exc,
                    )

                # P2-fix (2026-07-18): 透传 LLM 根级的 six_dimension_inputs
                # 到 result（六维评分的"主观键值"来源）。详见 prompt 章节
                # 「长线六维·主观键值」。
                _six = data.get("six_dimension_inputs")
                if isinstance(_six, dict):
                    _six_safe = cast(Dict[str, Any], _six)
                    result.six_dimension_inputs = _six_safe

                _final_result = populate_decision_action_fields(
                    result, explicit_action=explicit_action
                )

        except Exception as exc:  # noqa: BLE001
            # Capture JSONDecodeError as well as any other parse failure —
            # keeps type-checker happy because the function still returns
            # on every code path through this catch-all.
            logger.warning(
                "JSON 解析失败 (%s)，回退到 _parse_text_response",
                exc,
            )
            return self._parse_text_response(response_text, code, name)
        except BaseException:  # pragma: no cover - impossible path
            # pyright's flow analysis is unhappy with re-raise propagation of
            # BaseException subclasses, so the catchall is necessary even
            # though in practice this is unreachable. We still return to
            # satisfy the declared return type.
            logger.warning("_parse_response: BaseException raised, falling back")
            return self._parse_text_response(response_text, code, name)

        assert _final_result is not None
        return _final_result

    def _fix_json_string(self, json_str: str) -> str:
        """修复常见的 JSON 格式问题"""
        import re

        # 移除注释
        json_str = re.sub(r"//.*?\n", "\n", json_str)
        json_str = re.sub(r"/\*.*?\*/", "", json_str, flags=re.DOTALL)

        # 修复尾随逗号
        json_str = re.sub(r",\s*}", "}", json_str)
        json_str = re.sub(r",\s*]", "]", json_str)

        # 确保布尔值是小写
        json_str = json_str.replace("True", "true").replace("False", "false")

        # fix by json-repair
        json_str = repair_json(json_str)

        return json_str

    @staticmethod
    def _extract_json_candidate(text: str) -> Optional[str]:
        """Locate a single JSON object inside *text* and return the substring.

        The previous ``find("{")`` / ``rfind("}")`` heuristic failed whenever
        the LLM (notably the MiniMax-M3 thinking model) emitted prose
        containing stray braces (e.g. ``Start with { and end with }``) or
        followed the JSON with a second incomplete object after ``}``. In
        those cases the captured substring mixed prose and JSON, breaking
        ``json.loads`` and silently dropping the ``six_dimension_inputs``
        block that downstream code reads via ``result.six_dimension_inputs``.

        Strategy (ordered best-effort):

        1. If a ``\\`\\`\\`json`` fenced block exists, return its inner text.
        2. Otherwise, iterate over candidate ``{`` positions (filtered to
           JSON-structural separators and newlines) and bracket-match each
           forward to its closing ``}``. Return the first candidate whose
           parsed object contains a "dashboard" key (the most reliable
           marker that we have located the outermost decision dashboard
           rather than a nested sub-object like ``intelligence.latest_news``).
        3. Fall back to the first candidate whose parse yields any object,
           regardless of shape.
        4. Return ``None`` if no balanced JSON object can be located.
        """

        if not text:
            return None

        # 1. Look for a fenced JSON block. Capture the entire fenced body
        #    (between the opening and closing ``` markers), then bracket-match
        #    the outermost {...} inside it. Using a non-greedy regex would
        #    stop at the first nested `}`, so we do the matching manually.
        fence_pattern = re.compile(r"```(?:json|JSON)?\s*(\{)", re.DOTALL)
        fence_match = fence_pattern.search(text)
        if fence_match:
            fence_open = fence_match.start(1)
            fence_end_idx = text.find("```", fence_open + 1)
            if fence_end_idx > fence_open:
                match_end = _scan_json_object_end(text, fence_open)
                if match_end is not None and match_end < fence_end_idx + 4:
                    candidate = text[fence_open : match_end + 1]
                    try:
                        json.loads(candidate)
                    except Exception:  # noqa: BLE001
                        pass
                    else:
                        return candidate

        candidates = _candidate_json_open_positions(text)
        # Prefer the parse candidate that:
        #  1. parses to a dict,
        #  2. contains a "dashboard" / "six_dimension_inputs" key (or
        #     "code" + "operation_advice"), and
        #  3. is the largest by character length (so we prefer the outermost
        #     decision dashboard over nested sub-objects like
        #     ``intelligence.latest_news``).
        best_match: Optional[str] = None
        best_match_len = 0
        fallback: Optional[str] = None
        fallback_len = 0
        for open_idx in candidates:
            match_end = _scan_json_object_end(text, open_idx)
            if match_end is None:
                continue
            candidate = text[open_idx : match_end + 1]
            try:
                parsed = json.loads(candidate)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(parsed, dict):
                continue
            cand_len = len(candidate)
            if cand_len > fallback_len:
                fallback = candidate
                fallback_len = cand_len
            is_root = (
                "dashboard" in parsed
                or "six_dimension_inputs" in parsed
                or ("code" in parsed and "operation_advice" in parsed)
            )
            if is_root and cand_len > best_match_len:
                best_match = candidate
                best_match_len = cand_len
        if best_match is not None:
            # If the best match lacks ``six_dimension_inputs`` but the
            # response contains another JSON object that is a bare
            # six-dimension dict (600176 repro), merge it in.
            try:
                best_parsed = json.loads(best_match)
            except Exception:  # noqa: BLE001
                best_parsed = None
            if (
                isinstance(best_parsed, dict)
                and "six_dimension_inputs" not in best_parsed
            ):
                wrapped = _find_and_wrap_bare_six_dim(text, best_parsed)
                if wrapped is not None:
                    return json.dumps(wrapped, ensure_ascii=False)
            return best_match
        if fallback is not None:
            # If the largest parseable object is missing six_dimension_inputs
            # (e.g. the LLM truncated the response after the dashboard block
            # but before the six-dimension block), try to recover the
            # six-dimension values from the LLM's planning notes in the prose.
            try:
                fallback_parsed = json.loads(fallback)
            except Exception:  # noqa: BLE001
                fallback_parsed = None
            if (
                isinstance(fallback_parsed, dict)
                and "six_dimension_inputs" not in fallback_parsed
            ):
                six_recovery = _recover_six_dimension_inputs_from_prose(text)
                if six_recovery is not None:
                    fallback_parsed["six_dimension_inputs"] = six_recovery
                    fallback_parsed["_recovered_from_prose"] = True
                    return json.dumps(fallback_parsed, ensure_ascii=False)
                # Some LLMs emit the six-dimension keys as a bare top-level
                # object *without* the ``six_dimension_inputs`` wrapper
                # (600176 repro). Wrap them so downstream code can find
                # the dict under the expected key.
                wrapped = _wrap_bare_six_dim_dict(fallback_parsed)
                if wrapped is not None:
                    return json.dumps(wrapped, ensure_ascii=False)
            return fallback

        # No parseable JSON object at all. As a final fallback, recover
        # six_dimension_inputs from the LLM planning notes so the
        # six-dimension scoring can still populate non-default indicators
        # instead of defaulting to "数据缺失，使用中性分".
        six_recovery = _recover_six_dimension_inputs_from_prose(text)
        if six_recovery is not None:
            envelope = json.dumps(
                {
                    "_recovered_from_prose": True,
                    "six_dimension_inputs": six_recovery,
                },
                ensure_ascii=False,
            )
            return envelope
        return None

    @staticmethod
    def _extract_first_json_object(text: str) -> str:
        """Public façade that always returns *some* substring to parse.

        Falls back to the legacy ``find('{')`` / ``rfind('}')`` range when
        :meth:`_extract_json_candidate` fails so callers always receive a
        non-empty string. Returning an empty value would force callers to
        duplicate the heuristic and risks divergent behaviour; the legacy
        fallback is only a safety net for malformed responses.
        """

        candidate = GeminiAnalyzer._extract_json_candidate(text)
        if candidate is not None:
            return candidate
        if not text:
            return ""
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < 0 or end <= start:
            return ""
        return text[start : end + 1]

    def _validate_json_response(self, text: str) -> None:
        """Validate that *text* contains a parseable JSON object.

        Used as the ``response_validator`` argument to :meth:`_call_litellm` so
        that a JSON-less or unparseable reply from the primary model is treated
        as a model failure and triggers fallback to the next configured model.

        Raises:
            ValueError: if no JSON object is found in *text*.
            json.JSONDecodeError: if the extracted JSON cannot be parsed (after
                :meth:`_fix_json_string` attempts repair).
        """
        cleaned = text
        if "```json" in cleaned:
            cleaned = cleaned.replace("```json", "").replace("```", "")
        elif "```" in cleaned:
            cleaned = cleaned.replace("```", "")

        # B3 fix: detect "multi-segment JSON" pattern that the MiniMax-M3
        # thinking model occasionally produces. The LLM emits one JSON,
        # then "thinks again", then emits a second JSON. ``find("{")`` +
        # ``rfind("}")`` would then return the whole "outer wrapper" as a
        # single dict-like text, which ``json.loads`` may parse as a list of
        # objects — corrupting ``_parse_response``. Reject explicitly.
        json_open = cleaned.count("{")
        json_close = cleaned.count("}")
        if json_open > 1 and json_close > 1:
            # Heuristic: if there are clearly multiple JSON-shaped objects
            # in the response, refuse. The "obvious" tell is a literal
            # ``}\n\n{`` (close then open) or a fenced `````json`` block.
            if re.search(r"\}\s*[\r\n]+\s*\{", cleaned) or cleaned.count("```") >= 2:
                raise ValueError(
                    "LLM response contains multiple JSON objects (multi-segment output). "
                    "Treat as model failure to fall back."
                )

        # B3 fix: detect "refusal" / "I cannot" patterns from safety-tuned
        # providers that bypass the JSON entirely. We treat these as
        # validation failures so the fallback chain kicks in.
        refusal_markers = (
            "I can't help",
            "I cannot help",
            "I can't assist",
            "I cannot assist",
            "I won't",
            "I'm not able to",
            "I am not able to",
            "as an AI",
            "as a language model",
            "很抱歉，我无法",
            "抱歉，我不能",
            "无法完成",
        )
        if any(marker.lower() in cleaned[:600].lower() for marker in refusal_markers):
            raise ValueError(
                "LLM response appears to be a refusal rather than a JSON dashboard"
            )

        # 方案 C（修订）：在原始 cleaned 文本上做一次"裸值类型预检"，
        # 防止 LLM 偶发返回 `[ {...}, {...} ]` / 纯标量（`42` / `"x"` / `null`）。
        # 这些形态在下方 `find("{") + rfind("}")` 截取后会被错误地
        # 还原成 dict（截取了内层 {…}），导致下游 _parse_response 仍按 dict
        # 处理而忽略 list 语义。此处直接拒收，让 fallback chain 接管。
        stripped = cleaned.strip()
        if stripped.startswith("["):
            raise ValueError("LLM response is a JSON array, not a JSON object")
        # 标量/裸字符串/null/布尔预检：第一个非空白字符不是 `{` 也不是 `[`，
        # 且响应里也没有 `{`（防止误伤首部带说明文字的合法对象）。
        if stripped and stripped[0] != "{" and "{" not in stripped:
            first = stripped[0]
            if (
                first in {'"', "'", "-", "+"}
                or first.isdigit()
                or stripped.startswith("null")
                or stripped.startswith("true")
                or stripped.startswith("false")
            ):
                raise ValueError(
                    f"LLM response starts with scalar/quote, not JSON object"
                )

        json_start = cleaned.find("{")
        json_end = cleaned.rfind("}") + 1

        if json_start < 0 or json_end <= json_start:
            raise ValueError("No JSON object found in LLM response")

        json_str = cleaned[json_start:json_end]
        json_str = self._fix_json_string(json_str)
        parsed = json.loads(json_str)
        if not isinstance(parsed, dict):
            raise ValueError(
                f"LLM response parsed to {type(parsed).__name__}, not dict"
            )

    def _parse_text_response(
        self, response_text: str, code: str, name: str
    ) -> AnalysisResult:
        """从纯文本响应中尽可能提取分析信息"""
        report_language = normalize_report_language(
            getattr(self._get_runtime_config(), "report_language", "zh")
        )
        # 尝试识别关键词来判断情绪
        sentiment_score = 50
        trend = "Sideways" if report_language == "en" else "震荡"
        advice = "Hold" if report_language == "en" else "持有"

        text_lower = response_text.lower()

        # 简单的情绪识别
        positive_keywords = [
            "看多",
            "买入",
            "上涨",
            "突破",
            "强势",
            "利好",
            "加仓",
            "bullish",
            "buy",
        ]
        negative_keywords = [
            "看空",
            "卖出",
            "下跌",
            "跌破",
            "弱势",
            "利空",
            "减仓",
            "bearish",
            "sell",
        ]

        positive_count = sum(1 for kw in positive_keywords if kw in text_lower)
        negative_count = sum(1 for kw in negative_keywords if kw in text_lower)

        if positive_count > negative_count + 1:
            sentiment_score = 65
            trend = "Bullish" if report_language == "en" else "看多"
            advice = "Buy" if report_language == "en" else "买入"
            decision_type = "buy"
        elif negative_count > positive_count + 1:
            sentiment_score = 35
            trend = "Bearish" if report_language == "en" else "看空"
            advice = "Sell" if report_language == "en" else "卖出"
            decision_type = "sell"
        else:
            decision_type = "hold"

        # 截取前500字符作为摘要
        summary = (
            response_text[:500]
            if response_text
            else ("No analysis result" if report_language == "en" else "无分析结果")
        )

        result = AnalysisResult(
            code=code,
            name=name,
            sentiment_score=sentiment_score,
            trend_prediction=trend,
            operation_advice=advice,
            decision_type=decision_type,
            confidence_level="Low" if report_language == "en" else "低",
            analysis_summary=summary,
            key_points="JSON parsing failed; treat this as best-effort output."
            if report_language == "en"
            else "JSON解析失败，仅供参考",
            risk_warning="The result may be inaccurate. Cross-check with other information."
            if report_language == "en"
            else "分析结果可能不准确，建议结合其他信息判断",
            raw_response=response_text,
            success=False,
            error_message="LLM response is not valid JSON; analysis result will not be persisted",
            report_language=report_language,
        )
        return populate_decision_action_fields(result)

    def batch_analyze(
        self, contexts: List[Dict[str, Any]], delay_between: float = 2.0
    ) -> List[AnalysisResult]:
        """
        批量分析多只股票

        注意：为避免 API 速率限制，每次分析之间会有延迟

        Args:
            contexts: 上下文数据列表
            delay_between: 每次分析之间的延迟（秒）

        Returns:
            AnalysisResult 列表
        """
        results = []

        for i, context in enumerate(contexts):
            if i > 0:
                logger.debug(f"等待 {delay_between} 秒后继续...")
                time.sleep(delay_between)

            result = self.analyze(context)
            results.append(result)

        return results


# 便捷函数
def get_analyzer() -> GeminiAnalyzer:
    """获取 LLM 分析器实例"""
    return GeminiAnalyzer()


if __name__ == "__main__":
    # 测试代码
    logging.basicConfig(level=logging.DEBUG)

    # 模拟上下文数据
    test_context = {
        "code": "600519",
        "date": "2026-01-09",
        "today": {
            "open": 1800.0,
            "high": 1850.0,
            "low": 1780.0,
            "close": 1820.0,
            "volume": 10000000,
            "amount": 18200000000,
            "pct_chg": 1.5,
            "ma5": 1810.0,
            "ma10": 1800.0,
            "ma20": 1790.0,
            "volume_ratio": 1.2,
        },
        "ma_status": "多头排列 📈",
        "volume_change_ratio": 1.3,
        "price_change_ratio": 1.5,
    }

    analyzer = GeminiAnalyzer()

    if analyzer.is_available():
        print("=== AI 分析测试 ===")
        result = analyzer.analyze(test_context)
        print(f"分析结果: {result.to_dict()}")
    else:
        print("Gemini API 未配置，跳过测试")
