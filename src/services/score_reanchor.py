# -*- coding: utf-8 -*-
"""
Score Reanchor (P1 fix 2026-07-17).

LLMs score inconsistently even with the same prompt: across 5 runs of the
same 11-stock batch we saw ±15 to ±55 drift. Temperature scaling doesn't
help thinking models (the thinking chain is sampled separately). Telling
the LLM "you MUST follow these weights" in the prompt doesn't work
either — observed in batches 3-5 where the LLM ignored the table.

The fix: do not let the LLM score. After parsing, re-compute the score
deterministically from the factors the LLM *did* report (ma_alignment,
bias_ma5, volume_status, risk_alerts count, pe_ratio,
market_high_risk). The original LLM score is preserved in
``_reanchor_log.original_score`` for audit, and the recomputed value
replaces ``sentiment_score``.

Output of ``reanchor_score`` is a 0-100 integer with a guaranteed
explanation string in ``_reanchor_log.factors``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# 因子提取（与 P0 共享同一套逻辑，但接口更直接）
# ----------------------------------------------------------------------


def _coerce_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "yes", "多头", "bullish", "看多", "↑"):
            return True
        if v in ("false", "no", "空头", "bearish", "看空", "↓", "缠绕"):
            return False
    return None


def _coerce_float(value: Any) -> Optional[float]:
    if value is None or value == "" or value == "N/A":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_bullish_volume(volume_status: Any) -> Optional[bool]:
    """Map volume_status text to a bullish/bearish/null signal.

    Note: "缩量" in a downtrend is generally bullish (less selling pressure).
    In an uptrend, "缩量" is bearish (no conviction). Without a trend
    context, treat 缩量 as neutral (None).
    """
    if not isinstance(volume_status, str):
        return None
    v = volume_status.strip()
    if v in ("放量", "放量杀跌", "放量上涨"):
        # Treat as bearish (volume confirms move; we can't tell direction)
        return False
    if v in ("平量",):
        return None
    if v in ("缩量",):
        return None
    return None


def _classify_volume(
    volume_status: Any, is_bullish_trend: Optional[bool]
) -> Optional[bool]:
    """Refined volume classification: bullish = volume supports trend."""
    raw = _is_bullish_volume(volume_status)
    if raw is None:
        # 缩量/平量: in downtrend, no conviction = mildly bearish
        if isinstance(volume_status, str) and volume_status in ("缩量", "平量"):
            if is_bullish_trend is False:
                return False  # 空头 + 缩量 = 弱势但稳
            elif is_bullish_trend is True:
                return False  # 多头 + 缩量 = 上涨乏力
        return None
    return raw


# ----------------------------------------------------------------------
# 评分
# ----------------------------------------------------------------------

SCORE_BASE = 50
SCORE_RANGE = (0, 100)


def _clamp(score: int) -> int:
    lo, hi = SCORE_RANGE
    return max(lo, min(hi, score))


def extract_reanchor_inputs(
    raw: Any,
) -> Dict[str, Any]:
    """Pull the factors the LLM *did* report from the parsed JSON."""
    if not isinstance(raw, dict):
        raw = {}

    def _nested_dict(parent: Any, key: str) -> Dict[str, Any]:
        v = parent.get(key)
        return v if isinstance(v, dict) else {}

    dash: Dict[str, Any] = _nested_dict(raw, "dashboard")
    dp: Dict[str, Any] = _nested_dict(dash, "data_perspective")
    intel: Dict[str, Any] = _nested_dict(dash, "intelligence")

    is_bullish = _coerce_bool(_nested_dict(dp.get("trend_status") or {}, "is_bullish"))
    bias_ma5 = _coerce_float(_nested_dict(dp, "price_position").get("bias_ma5"))
    volume_status = _nested_dict(dp, "volume_analysis").get("volume_status")
    risk_alerts = intel.get("risk_alerts")
    risk_count = len(risk_alerts) if isinstance(risk_alerts, list) else 0

    ctx_snapshot: Dict[str, Any] = _nested_dict(raw, "context_snapshot")
    pe_ratio: Optional[float] = _coerce_float(raw.get("pe_ratio"))
    if pe_ratio is None:
        pe_ratio = _coerce_float(ctx_snapshot.get("pe_ratio"))

    market_high_risk = False
    risk_tags = ctx_snapshot.get("risk_tags") or []
    if isinstance(risk_tags, list):
        market_high_risk = any(
            isinstance(t, str) and t.lower() in ("high_risk", "market_cooling")
            for t in risk_tags
        )

    return {
        "is_bullish": is_bullish,
        "bias_ma5": bias_ma5,
        "bias_ma5_safe": (bias_ma5 is not None and abs(bias_ma5) < 5.0),
        "volume_status": volume_status,
        "volume_supports_trend": _classify_volume(volume_status, is_bullish),
        "risk_alert_count": risk_count,
        "pe_ratio": pe_ratio,
        "market_high_risk": market_high_risk,
    }


def reanchor_score(
    raw: Dict[str, Any],
    inputs: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Dict[str, Any], Dict[str, Any]]:
    """Re-compute sentiment_score from observed factors.

    Returns:
        (new_score, factors_dict, log_dict).
        - new_score: clamped integer in [0, 100]
        - factors_dict: the inputs used
        - log_dict: {"original_score", "recomputed_score", "delta", "adjustments": [...]}
    """
    factors = inputs if inputs is not None else extract_reanchor_inputs(raw)
    adjustments: List[Dict[str, Any]] = []

    score = SCORE_BASE

    # 均线排列
    if factors.get("is_bullish") is True:
        score += 30
        adjustments.append({"factor": "ma_alignment", "value": True, "delta": 30})
    elif factors.get("is_bullish") is False:
        score -= 30
        adjustments.append({"factor": "ma_alignment", "value": False, "delta": -30})

    # 乖离率
    if factors.get("bias_ma5_safe") is True:
        score += 20
        adjustments.append(
            {"factor": "bias_ma5", "value": factors.get("bias_ma5"), "delta": 20}
        )
    elif factors.get("bias_ma5") is not None:
        score -= 20
        adjustments.append(
            {"factor": "bias_ma5", "value": factors.get("bias_ma5"), "delta": -20}
        )

    # 量能
    vol_supports = factors.get("volume_supports_trend")
    if vol_supports is False:
        score -= 20
        adjustments.append(
            {"factor": "volume", "value": factors.get("volume_status"), "delta": -20}
        )
    elif vol_supports is True:
        score += 20
        adjustments.append(
            {"factor": "volume", "value": factors.get("volume_status"), "delta": 20}
        )
    # None: 不动

    # 利空事件
    if factors.get("risk_alert_count", 0) > 0:
        score -= 10
        adjustments.append(
            {
                "factor": "risk_alerts",
                "value": factors["risk_alert_count"],
                "delta": -10,
            }
        )

    # PE
    pe = factors.get("pe_ratio")
    if pe is not None and pe > 0:
        if pe < 50:
            score += 10
            adjustments.append({"factor": "pe", "value": pe, "delta": 10})
        elif pe >= 50:
            score -= 10
            adjustments.append({"factor": "pe", "value": pe, "delta": -10})

    # 大盘高风险
    if factors.get("market_high_risk"):
        score -= 5
        adjustments.append({"factor": "market_risk", "value": "high", "delta": -5})

    new_score = _clamp(score)
    original_score: Optional[int] = None
    if isinstance(raw, dict):
        try:
            raw_score = raw.get("sentiment_score")
            if raw_score is not None and not isinstance(raw_score, bool):
                original_score = int(raw_score)
        except (TypeError, ValueError):
            original_score = None

    log = {
        "original_score": original_score,
        "recomputed_score": new_score,
        "delta": (new_score - original_score) if original_score is not None else None,
        "adjustments": adjustments,
    }
    return new_score, factors, log
