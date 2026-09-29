# -*- coding: utf-8 -*-
"""规则决策护栏：10 条规则（需求 §5.3），纯函数、可单测、无 LLM。

规则触发即产生 GuardrailEvent，处置分两类：
- ``rewrite``：打回对应维度重算（归一化/换表述）；
- ``override``：直接改写结论（降级/收敛）并注明理由。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List

from icontract import require

from src.schemas.deep_research_dims import GuardrailEvent


def _event(rule_id: str, dim: str, action: str, reason: str) -> GuardrailEvent:
    return GuardrailEvent(rule_id=rule_id, dim=dim, action=action, reason=reason)


def rule1_capital_outflow(signal: Dict[str, Any], intel: Dict[str, Any]) -> List[GuardrailEvent]:
    """买入/增持信号但资金流显著净流出 → 信号降一级。"""
    rating = signal.get("rating")
    if rating not in ("买入", "增持"):
        return []
    risk_text = " ".join((intel.get("intelligence") or {}).get("risk_alerts") or [])
    if "净流出" not in risk_text:
        return []
    new_rating = "增持" if rating == "买入" else "中性"
    return [
        _event(
            "GR1_capital_outflow",
            "signal",
            "override",
            f"评级 {rating} → {new_rating}（资金流净流出）",
        )
    ]


def rule2_negative_edge(conclusion: Dict[str, Any]) -> List[GuardrailEvent]:
    """长线建仓/加仓但 edge ≤ 0 → 降级观察。"""
    c = conclusion.get("conclusion") or {}
    if c.get("action") in ("建仓", "加仓") and float(c.get("edge") or 0.0) <= 0:
        return [
            _event(
                "GR2_negative_edge",
                "conclusion",
                "override",
                "行动降级为「观察」（Edge ≤ 0，认知差不存在）",
            )
        ]
    return []


def rule3_weak_six_dim(
    conclusion: Dict[str, Any], six_dim: Dict[str, Any]
) -> List[GuardrailEvent]:
    """建仓/加仓但六维总分 < 55 → 降级观察。"""
    c = conclusion.get("conclusion") or {}
    if c.get("action") not in ("建仓", "加仓"):
        return []
    total = float((six_dim.get("framework") or {}).get("dimension_total") or 50.0)
    if total < 55:
        return [
            _event(
                "GR3_weak_six_dim",
                "conclusion",
                "override",
                f"行动降级为「观察」（六维总分 {total:.0f} < 55，评分锚不支持）",
            )
        ]
    return []


@require(lambda scenarios_payload: isinstance(scenarios_payload, dict))
def rule4_probability_sum(scenarios_payload: Dict[str, Any]) -> List[GuardrailEvent]:
    """情景概率和 ≠ 100% → 机器归一化（scenarios_dim 已内置），异常时记录。"""
    prob_sum = float(scenarios_payload.get("probability_sum") or 0.0)
    if not (0.95 <= prob_sum <= 1.05):
        return [
            _event(
                "GR4_probability_sum",
                "scenarios",
                "rewrite",
                f"概率和 {prob_sum * 100:.0f}% 异常，已触发归一化（见 L5）",
            )
        ]
    return []


def rule5_pe_band(scenarios_payload: Dict[str, Any]) -> List[GuardrailEvent]:
    """溢价情景 PE 上限低于当前 PE(TTM) → 估值口径矛盾提示（叙述型披露）。"""
    basis = scenarios_payload.get("valuation_basis")
    pe = scenarios_payload.get("current_pe_ttm")
    if basis != "PE_TTM" or not isinstance(pe, (int, float)) or pe <= 0:
        return []
    desc = " ".join(
        (s.get("description") or "")
        for s in ((scenarios_payload.get("scenarios") or {}).get("scenarios") or [])
    )
    if ("溢价" in desc or "抬升" in desc or "扩张" in desc) and "回归" not in desc:
        return [
            _event(
                "GR5_pe_band",
                "scenarios",
                "rewrite",
                "存在溢价/抬升表述，请复核情景 PE 区间是否 ≥ 当前 PE(TTM)",
            )
        ]
    return []


def rule6_anchor_deviation(
    conclusion: Dict[str, Any], scenarios_payload: Dict[str, Any]
) -> List[GuardrailEvent]:
    """三情景目标价（EV）与 1 年价值锚偏离 > 30% → 内部矛盾标记。"""
    c = conclusion.get("conclusion") or {}
    anchor = c.get("value_range_1y")
    ev = scenarios_payload.get("expected_value")
    if not anchor or not isinstance(ev, (int, float)):
        return []
    nums = [float(x) for x in str(anchor).replace("元", "").split("-") if x.strip().replace(".", "", 1).isdigit()]
    if not nums:
        return []
    mid = sum(nums) / len(nums)
    if mid > 0 and abs(ev - mid) / mid > 0.30:
        return [
            _event(
                "GR6_anchor_deviation",
                "scenarios",
                "override",
                f"EV {ev} 与 1 年价值锚 {anchor} 偏离 >30%，置信度降档",
            )
        ]
    return []


_ACTION_ORDER = {"建仓": 0, "加仓": 1, "持有": 2, "减仓": 3, "止损": 4, "观察": 2}
_RATING_TO_ACTION = {"买入": "加仓", "增持": "持有", "中性": "观察", "减持": "减仓"}
# 行动 → 评级上限（冲突时收敛到该行动允许的最激进评级）
_ACTION_TO_RATING_CAP = {
    "建仓": "买入",
    "加仓": "增持",
    "持有": "增持",
    "观察": "中性",
    "减仓": "中性",
    "止损": "中性",
}
_RATING_ORDER = {"买入": 0, "增持": 1, "中性": 2, "减持": 3}


def rule7_signal_conclusion_consistency(
    signal: Dict[str, Any], conclusion: Dict[str, Any]
) -> List[GuardrailEvent]:
    """S1 评级与 L3 行动方向冲突 → 收敛到该行动对应的评级上限（非只降一级）。"""
    rating = signal.get("rating")
    action = (conclusion.get("conclusion") or {}).get("action")
    if not rating or not action:
        return []
    implied = _RATING_TO_ACTION.get(str(rating))
    if implied is None:
        return []
    # 冲突定义：评级隐含动作比长线行动更激进（如 买入/增持 遇上 减仓/观察）
    if _ACTION_ORDER.get(implied, 2) >= _ACTION_ORDER.get(str(action), 2):
        return []
    cap = _ACTION_TO_RATING_CAP.get(str(action), "中性")
    current_rank = _RATING_ORDER.get(str(rating), 2)
    cap_rank = _RATING_ORDER.get(cap, 2)
    final = cap if cap_rank > current_rank else str(rating)
    if final == rating:
        return []
    return [
        _event(
            "GR7_signal_conclusion",
            "signal",
            "override",
            f"信号「{rating}」与长线行动「{action}」冲突，收敛至「{final}」（该行动评级上限 {cap}）",
        )
    ]


def rule8_unverified_intel(intel: Dict[str, Any]) -> List[GuardrailEvent]:
    """unverified 情报支撑确定性结论 → 打回重筛（披露型）。"""
    unverified = int(intel.get("unverified_count") or 0)
    intelligence = intel.get("intelligence") or {}
    has_definitive = any(
        k in str(intelligence.get("sentiment_summary") or "")
        for k in ("确认", "实锤", "已导入", "已量产")
    )
    if unverified > 0 and has_definitive:
        return [
            _event(
                "GR8_unverified_intel",
                "intel",
                "rewrite",
                f"{unverified} 条 unverified 情报与确定性表述并存，需重筛来源等级",
            )
        ]
    return []


def rule9_evidence_freshness(bayesian: Dict[str, Any]) -> List[GuardrailEvent]:
    """证据 < 3 条或全部早于 90 天 → 置信度上限「低」，行动不得高于观察。"""
    b = bayesian.get("bayesian") or {}
    accepted = b.get("evidence_log") or []
    fresh = 0
    today = date.today()
    for e in accepted:
        try:
            day = datetime.strptime(str(e.get("date"))[:10], "%Y-%m-%d").date()
            if (today - day).days <= 90:
                fresh += 1
        except (ValueError, TypeError):
            continue
    if len(accepted) >= 3 and fresh >= 1:
        return []
    return [
        _event(
            "GR9_evidence_freshness",
            "bayesian",
            "override",
            f"合格证据 {fresh}/{len(accepted)} 条（新鲜），置信度上限「低」，行动封顶「观察」",
        )
    ]


def rule10_subjective_divergence(six_dim: Dict[str, Any]) -> List[GuardrailEvent]:
    """LLM 主观键值与规则分背离 > 20 分 → 并列披露，不静默平均。"""
    events: List[GuardrailEvent] = []
    framework = six_dim.get("framework") or {}
    for d in framework.get("dimensions") or []:
        for ind in d.get("indicators") or []:
            if ind.get("basis") != "llm":
                continue
            # 背离检测需要规则对照值；指标 summary 中携带 rule_score 时生效
            summary = str(ind.get("summary") or "")
            if "rule_score=" not in summary:
                continue
            try:
                rule_score = float(summary.split("rule_score=")[1].split()[0])
            except (ValueError, IndexError):
                continue
            if abs(float(ind.get("score") or 50.0) - rule_score) > 20:
                events.append(
                    _event(
                        "GR10_subjective_divergence",
                        "six_dim",
                        "override",
                        f"{d.get('dimension')}·{ind.get('name')}：LLM {ind.get('score')} vs 规则 {rule_score}，背离>20，并列展示",
                    )
                )
    return events


def apply_guardrails(payloads: Dict[str, Dict[str, Any]]) -> tuple[List[GuardrailEvent], Dict[str, Any]]:
    """按波次执行全部规则（波次 3 合成后统一执行一遍）。

    Returns:
        (events, adjusted)：adjusted 为被 override 的维度 payload 修订副本。
    """
    events: List[GuardrailEvent] = []
    events += rule1_capital_outflow(payloads.get("signal", {}), payloads.get("intel", {}))
    events += rule2_negative_edge(payloads.get("conclusion", {}))
    events += rule3_weak_six_dim(
        payloads.get("conclusion", {}), payloads.get("six_dim", {})
    )
    events += rule4_probability_sum(payloads.get("scenarios", {}))
    events += rule5_pe_band(payloads.get("scenarios", {}))
    events += rule6_anchor_deviation(
        payloads.get("conclusion", {}), payloads.get("scenarios", {})
    )
    events += rule7_signal_conclusion_consistency(
        payloads.get("signal", {}), payloads.get("conclusion", {})
    )
    events += rule8_unverified_intel(payloads.get("intel", {}))
    events += rule9_evidence_freshness(payloads.get("bayesian", {}))
    events += rule10_subjective_divergence(payloads.get("six_dim", {}))

    adjusted = {k: dict(v) for k, v in payloads.items()}
    _apply_overrides(events, adjusted)
    return events, adjusted


def _apply_overrides(events: List[GuardrailEvent], adjusted: Dict[str, Dict[str, Any]]) -> None:
    """把 override 处置落到 payload（shallow copy 顶层写安全；嵌套结论需再拷贝）。"""
    for ev in events:
        if ev.action != "override":
            continue
        target = adjusted.get(ev.dim)
        if target is None:
            continue
        if ev.rule_id == "GR1_capital_outflow" and "→" in ev.reason:
            target["rating"] = ev.reason.split("→")[1].split("（")[0].strip()
        elif ev.rule_id in ("GR2_negative_edge", "GR3_weak_six_dim"):
            inner = dict(adjusted.get("conclusion") or {})
            c = dict(inner.get("conclusion") or {})
            c["action"] = "观察"
            c["position"] = "观察"
            inner["conclusion"] = c
            adjusted["conclusion"] = inner
        elif ev.rule_id == "GR7_signal_conclusion" and "收敛至「" in ev.reason:
            target["rating"] = ev.reason.split("收敛至「")[1].split("」")[0]
        elif ev.rule_id == "GR9_evidence_freshness":
            inner = dict(adjusted.get("conclusion") or {})
            c = dict(inner.get("conclusion") or {})
            if c.get("action") in ("建仓", "加仓"):
                c["action"] = "观察"
                c["position"] = "观察"
            inner["conclusion"] = c
            adjusted["conclusion"] = inner


def append_override_note(payload: Dict[str, Any], field: str, old_value: str, new_value: str) -> Dict[str, Any]:
    """维度被护栏 override 后，给叙述字段追加修订说明（解决叙述陈旧问题）。

    返回更新后的 payload 副本；无变化时原样返回。
    """
    if old_value == new_value:
        return payload
    note = f"（注：经决策护栏调整后，{field} 由「{old_value}」修订为「{new_value}」。）"
    inner_text = str(payload.get("narrative") or payload.get("rationale") or "")
    updated = dict(payload)
    if "rationale" in updated:
        updated["rationale"] = (inner_text + note) if inner_text else note
    else:
        updated["narrative"] = (inner_text + note) if inner_text else note
    return updated
