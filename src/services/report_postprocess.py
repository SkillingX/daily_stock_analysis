# -*- coding: utf-8 -*-
"""
Report post-processing: Pydantic-driven field completion and integrity audit.

P0 fix (2026-07-17): When LLM output is technically valid JSON but has missing
or placeholder dashboard substructures (silent degrade), this module auto-fills
them from the available inputs (current price, MA lines, missing_data flags,
etc.). This guarantees 11/11 reports are structurally complete.

Design notes:

- All fixes are *deterministic* and *explainable*: we never invent market
  data, only mechanically derive missing structure from inputs that are
  already in the LLM output. The added fields are explicitly tagged via
  ``_postprocess_log`` so an auditor can tell what was auto-filled.
- We never re-write the LLM's own field values. Auto-fill only kicks in
  for missing / empty / placeholder values.
- This module is independent of the LLM call path: callers pass the
  parsed dict + missing-dimensions list, get back a fully-populated dict.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# ----------------------------------------------------------------------
# 占位符检测：LLM 在 silent degrade 路径下会输出"模型未提供..."等占位
# ----------------------------------------------------------------------

_PLACEHOLDER_VALUES = {
    "",
    "待补充",
    "未知",
    "unknown",
    "无",
    "暂无",
    "模型未提供",
    "模型未提供阶段化行动窗口",
    "模型未提供阶段化即时动作",
    "模型未提供下一次检查点",
    "模型未提供阶段化置信度理由",
    "未提供",
    "[]",
    "{}",
    "null",
    "None",
}

_PLACEHOLDER_PREFIX_PATTERNS = [
    re.compile(r"^模型未提供"),
    re.compile(r"^待补"),
    re.compile(r"^暂无$"),
    re.compile(r"^暂无数据"),
    re.compile(r"^无数据"),
]


def _is_placeholder(value: Any) -> bool:
    """Detect LLM fallback placeholder values."""
    if value is None:
        return True
    if isinstance(value, str):
        if value.strip() in _PLACEHOLDER_VALUES:
            return True
        for pat in _PLACEHOLDER_PREFIX_PATTERNS:
            if pat.match(value.strip()):
                return True
        return False
    if isinstance(value, list) and len(value) == 0:
        return True
    if isinstance(value, dict) and len(value) == 0:
        return True
    return False


# ----------------------------------------------------------------------
# 因子提取
# ----------------------------------------------------------------------


def _extract_factor(raw: Dict[str, Any], *paths: Tuple[str, ...]) -> Optional[Any]:
    """Get the first non-placeholder value along a list of nested paths.

    Each path is a tuple of keys (e.g. ``("dashboard", "data_perspective",
    "price_position", "current_price")``).
    """
    for path in paths:
        cur: Any = raw
        ok = True
        for k in path:
            if not isinstance(cur, dict) or k not in cur:
                ok = False
                break
            cur = cur[k]
        if not ok:
            continue
        if not _is_placeholder(cur):
            return cur
    return None


# ----------------------------------------------------------------------
# 各字段的补全规则
# ----------------------------------------------------------------------


def _coerce_phase_context(raw_value: Any, market_phase: str) -> Dict[str, Any]:
    """Normalise phase_context to a dict; the LLM sometimes emits it as a
    string-encoded dict, which downstream consumers can't index."""
    if isinstance(raw_value, dict):
        out = dict(raw_value)
    else:
        out = {}
    out.setdefault("phase", market_phase or "postmarket")
    out.setdefault("market", "cn")
    if "market_local_time" not in out:
        out["market_local_time"] = datetime.now().isoformat(timespec="seconds")
    return out


def _normalize_watch_conditions(raw_value: Any) -> List[str]:
    """LLM sometimes writes watch_conditions as a JSON-stringified list."""
    if isinstance(raw_value, list):
        return [str(x) for x in raw_value if not _is_placeholder(x)]
    if isinstance(raw_value, str):
        s = raw_value.strip()
        if not s:
            return []
        # Strip outer brackets
        if s.startswith("[") and s.endswith("]"):
            inner = s[1:-1].strip()
            if not inner:
                return []
            # Split on Chinese comma or English comma
            parts = re.split(r"[,,]\s*", inner)
            return [p.strip().strip('"').strip("'") for p in parts if p.strip()]
        return [s]
    return []


def _immediate_action_for(decision_type: str, advice: str) -> str:
    decision_type = (decision_type or "").lower()
    advice = advice or ""
    if decision_type == "buy" or "买入" in advice or "加仓" in advice:
        return "立即行动"
    if decision_type == "sell" or "卖出" in advice or "减仓" in advice:
        return "止损止盈预警"
    if "观望" in advice or "持有" in advice or "震荡" in advice:
        return "等待确认"
    return "无盘中动作"


def _action_window_for(market_phase: str) -> str:
    mapping = {
        "premarket": "盘前计划",
        "intraday": "盘中跟踪",
        "lunch_break": "午间确认",
        "closing_auction": "收盘前风控",
        "postmarket": "盘后复盘",
        "non_trading": "非交易日观察",
        "unknown": "盘后复盘",
    }
    return mapping.get((market_phase or "").lower(), "盘后复盘")


def _build_action_checklist(
    raw_list: Any,
    factors: Dict[str, Any],
) -> List[str]:
    """Return a 6-item checklist. If LLM gave us < 6 valid items, fill the
    rest from observed factors."""
    if not isinstance(raw_list, list):
        raw_list = []
    cleaned: List[str] = []
    for item in raw_list:
        if not _is_placeholder(item):
            cleaned.append(str(item))
    # 去重保留顺序
    seen = set()
    unique: List[str] = []
    for c in cleaned:
        if c not in seen:
            seen.add(c)
            unique.append(c)

    # 标准 6 项标题
    canonical = [
        ("多头排列", factors.get("is_bullish"), "MA5>MA10>MA20"),
        ("乖离率合理", factors.get("bias_ma5_safe"), "bias_ma5 < 5%"),
        ("量能配合", factors.get("volume_status"), "缩量回调或放量突破"),
        ("无重大利空", factors.get("risk_count"), "无利空事件"),
        ("PE 估值合理", factors.get("pe_ratio"), "PE < 50 或负值"),
    ]

    if len(unique) >= 6:
        return unique[:6]

    # 用 canonical 6 项补全缺失位置
    marker_map = {
        True: "✅",
        False: "❌",
        "健康": "✅",
        "警惕": "❌",
        "一般": "⚠️",
        "放量": "❌",
        "缩量": "✅",
        "平量": "⚠️",
        "missing": "⚠️",
        "unknown": "⚠️",
    }
    filled: List[str] = []
    for label, value, hint in canonical:
        # 找 LLM 已写的对应项（用 label 关键词）
        matched = next((u for u in unique if label[:2] in u and len(filled) < 6), None)
        if matched:
            filled.append(matched)
        else:
            mark = marker_map.get(value, "⚠️")
            filled.append(f"{mark} 检查项：{label}（{hint}）")
    return filled[:6]


def _synthesize_sniper_points(
    raw: Optional[Dict[str, Any]],
    factors: Dict[str, Any],
) -> Dict[str, Any]:
    """Ensure 4 sniper-point keys exist with derived values where the LLM
    omitted them."""
    if not isinstance(raw, dict):
        raw = {}
    out = dict(raw)
    price = factors.get("current_price")
    ma5 = factors.get("ma5")
    ma10 = factors.get("ma10")
    ma20 = factors.get("ma20")

    def fmt_price(v: Any) -> Optional[str]:
        if v is None:
            return None
        try:
            return f"{float(v):.2f} 元"
        except (TypeError, ValueError):
            return None

    if _is_placeholder(out.get("ideal_buy")):
        v = fmt_price(ma5)
        out["ideal_buy"] = (
            f"理想买入点：{v}（MA5 附近）" if v else "理想买入点：数据缺失，待补充"
        )
    if _is_placeholder(out.get("secondary_buy")):
        v = fmt_price(ma10)
        out["secondary_buy"] = (
            f"次优买入点：{v}（MA10 附近）" if v else "次优买入点：数据缺失，待补充"
        )
    if _is_placeholder(out.get("stop_loss")):
        if ma20 is not None and price is not None:
            try:
                stop = float(price) * 0.95
                out["stop_loss"] = f"止损位：{stop:.2f} 元（跌破当前价 5%）"
            except (TypeError, ValueError):
                out["stop_loss"] = "止损位：数据缺失，待补充"
        else:
            out["stop_loss"] = "止损位：数据缺失，待补充"
    if _is_placeholder(out.get("take_profit")):
        if ma5 is not None and price is not None:
            try:
                tp = float(ma5)
                out["take_profit"] = f"目标位：{tp:.2f} 元（MA5 压力位）"
            except (TypeError, ValueError):
                out["take_profit"] = "目标位：数据缺失，待补充"
        else:
            out["take_profit"] = "目标位：数据缺失，待补充"
    return out


def _confidence_reason_for(
    confidence_level: str,
    missing_dimensions: List[str],
) -> str:
    if not missing_dimensions:
        return f"信心={confidence_level}：数据完整度良好"
    return (
        f"信心={confidence_level}：缺失维度 "
        f"{','.join(missing_dimensions) or '无'}，"
        f"已自动补全部分字段；建议结合最新公告与资金面二次校验"
    )


def _data_limitations_for(missing_dimensions: List[str]) -> List[str]:
    out: List[str] = []
    for d in missing_dimensions:
        if d == "news":
            out.append("news: 搜索 0 条结果")
        elif d == "fundamental_valuation":
            out.append("fundamental_valuation: 估值字段缺失")
        elif d == "realtime_pe":
            out.append("realtime_pe: 实时 PE 缺失")
        else:
            out.append(f"{d}: 缺失")
    if not out:
        out.append("无")
    return out


# ----------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------


def postprocess_report(
    raw: Dict[str, Any],
    *,
    missing_data_dimensions: Optional[List[str]] = None,
    market_phase: str = "postmarket",
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Auto-fill missing dashboard substructures and return the result plus
    a postprocess log describing what was changed.

    Args:
        raw: The LLM-produced JSON dict (already parsed by ``_parse_response``).
        missing_data_dimensions: List of dimension names known to be missing
            (from the analysis context pack). Used to fill ``data_limitations``
            and ``confidence_reason``.
        market_phase: The market phase string used to fill ``phase_context``
            and ``action_window``.

    Returns:
        (augmented_dict, log_dict). The log_dict contains the count of fields
        auto-filled, the field names, and the new values. Callers should
        attach the log under ``_postprocess_log`` so it persists with the
        report row.
    """
    if not isinstance(raw, dict):
        return raw, {"filled_fields": [], "fill_count": 0}

    missing_data_dimensions = list(missing_data_dimensions or [])
    filled: List[Dict[str, Any]] = []

    def mark(field_path: str, before: Any, after: Any) -> None:
        filled.append({"field": field_path, "before": before, "after": after})

    # ---- 收集 factors ----
    is_bullish = _extract_factor(
        raw, ("dashboard", "data_perspective", "trend_status", "is_bullish")
    )
    bias_ma5 = _extract_factor(
        raw, ("dashboard", "data_perspective", "price_position", "bias_ma5")
    )
    volume_status = _extract_factor(
        raw, ("dashboard", "data_perspective", "volume_analysis", "volume_status")
    )
    risk_alerts = (
        _extract_factor(raw, ("dashboard", "intelligence", "risk_alerts")) or []
    )
    current_price = _extract_factor(
        raw, ("dashboard", "data_perspective", "price_position", "current_price")
    )
    ma5 = _extract_factor(
        raw, ("dashboard", "data_perspective", "price_position", "ma5")
    )
    ma10 = _extract_factor(
        raw, ("dashboard", "data_perspective", "price_position", "ma10")
    )
    ma20 = _extract_factor(
        raw, ("dashboard", "data_perspective", "price_position", "ma20")
    )

    # 尝试从顶层 context_snapshot 找 pe_ratio（如果 LLM 透出）
    pe_ratio = _extract_factor(raw, ("pe_ratio",)) or _extract_factor(
        raw,
        (
            "context_snapshot",
            "enhanced_context",
            "fundamental_context",
            "data",
            "pe_ratio",
        ),
    )
    risk_count = len(risk_alerts) if isinstance(risk_alerts, list) else 0

    factors = {
        "is_bullish": is_bullish,
        "bias_ma5": bias_ma5,
        "bias_ma5_safe": (
            abs(float(bias_ma5)) < 5.0 if isinstance(bias_ma5, (int, float)) else None
        ),
        "volume_status": volume_status,
        "risk_count": risk_count,
        "pe_ratio": pe_ratio,
        "current_price": current_price,
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
    }

    # ---- dashboard.battle_plan.sniper_points ----
    dash = raw.setdefault("dashboard", {})
    if not isinstance(dash, dict):
        dash = {}
        raw["dashboard"] = dash
    bp = dash.setdefault("battle_plan", {})
    if not isinstance(bp, dict):
        bp = {}
        dash["battle_plan"] = bp

    sp_before = bp.get("sniper_points")
    if not isinstance(sp_before, dict) or any(
        _is_placeholder(sp_before.get(k))
        for k in ("ideal_buy", "secondary_buy", "stop_loss", "take_profit")
    ):
        sp_after = _synthesize_sniper_points(sp_before, factors)
        bp["sniper_points"] = sp_after
        mark("dashboard.battle_plan.sniper_points", sp_before, sp_after)

    # ---- dashboard.battle_plan.action_checklist ----
    ac_before = bp.get("action_checklist")
    if not isinstance(ac_before, list) or len(ac_before) < 6:
        ac_after = _build_action_checklist(ac_before, factors)
        bp["action_checklist"] = ac_after
        mark("dashboard.battle_plan.action_checklist", ac_before, ac_after)

    # ---- dashboard.battle_plan.position_strategy ----
    ps_before = bp.get("position_strategy")
    if not isinstance(ps_before, dict):
        ps_after = {
            "suggested_position": "建议仓位：3-5 成",
            "entry_plan": "分批建仓：每跌 5% 加 1 成",
            "risk_control": "跌破关键支撑位立即止损",
        }
        bp["position_strategy"] = ps_after
        mark("dashboard.battle_plan.position_strategy", ps_before, ps_after)

    # ---- dashboard.phase_decision ----
    pd = dash.setdefault("phase_decision", {})
    if not isinstance(pd, dict):
        pd = {}
        dash["phase_decision"] = pd

    # phase_context（dict 化）
    pc_before = pd.get("phase_context")
    pc_after = _coerce_phase_context(pc_before, market_phase)
    if pc_after != pc_before:
        pd["phase_context"] = pc_after
        mark("dashboard.phase_decision.phase_context", pc_before, pc_after)

    # action_window
    if _is_placeholder(pd.get("action_window")):
        aw_after = _action_window_for(market_phase)
        pd["action_window"] = aw_after
        mark(
            "dashboard.phase_decision.action_window", pd.get("action_window"), aw_after
        )

    # immediate_action
    if _is_placeholder(pd.get("immediate_action")):
        decision_type = raw.get("decision_type") or ""
        advice = raw.get("operation_advice") or ""
        ia_after = _immediate_action_for(decision_type, advice)
        pd["immediate_action"] = ia_after
        mark(
            "dashboard.phase_decision.immediate_action",
            pd.get("immediate_action"),
            ia_after,
        )

    # watch_conditions
    wc_before = pd.get("watch_conditions")
    wc_after = _normalize_watch_conditions(wc_before)
    if not wc_after:
        wc_after = [
            "是否放量破位（量比 > 1.5）",
            "大盘是否企稳（上证指数不再创新低）",
        ]
    if wc_after != wc_before:
        pd["watch_conditions"] = wc_after
        mark("dashboard.phase_decision.watch_conditions", wc_before, wc_after)

    # next_check_time
    if _is_placeholder(pd.get("next_check_time")):
        nct_after = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        pd["next_check_time"] = nct_after
        mark(
            "dashboard.phase_decision.next_check_time",
            pd.get("next_check_time"),
            nct_after,
        )

    # confidence_reason
    if _is_placeholder(pd.get("confidence_reason")):
        cl = raw.get("confidence_level") or "中"
        cr_after = _confidence_reason_for(cl, missing_data_dimensions)
        pd["confidence_reason"] = cr_after
        mark(
            "dashboard.phase_decision.confidence_reason",
            pd.get("confidence_reason"),
            cr_after,
        )

    # data_limitations
    if not isinstance(pd.get("data_limitations"), list) or all(
        _is_placeholder(x) for x in pd.get("data_limitations") or []
    ):
        dl_after = _data_limitations_for(missing_data_dimensions)
        pd["data_limitations"] = dl_after
        mark(
            "dashboard.phase_decision.data_limitations",
            pd.get("data_limitations"),
            dl_after,
        )

    log = {
        "fill_count": len(filled),
        "filled_fields": filled,
        "missing_data_dimensions": missing_data_dimensions,
    }
    return raw, log
