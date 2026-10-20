# -*- coding: utf-8 -*-
"""S1 信号：S2+S3+L3+L5 合成评级与目标价。叙述型 LLM。

合成规则（机器）：评级由 长线行动×EV上行空间×资金流 三因子映射；
护栏规则 1/7 在此之后裁决（见 guardrail.py）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from decimal import Decimal
from math import isfinite
from icontract import ensure

from src.deep_research_dims.context import SharedContext
from src.deep_research_dims.scenarios_dim import with_valuation_cascade
from src.schemas.deep_research_dims import Confidence, Rating, SignalDim


@ensure(lambda current, target, result: result is None or current is not None and target is not None and current > 0)
def _upside(current: Optional[float], target: Optional[float]) -> Optional[float]:
    if current and target and current > 0:
        return float(round((Decimal(str(target)) - Decimal(str(current))) / Decimal(str(current)) * 100, 2))
    return None


def _map_rating(action: str, upside: Optional[float]) -> tuple[Rating, Confidence]:
    """机器评级映射（资金流出降级的唯一权威是护栏规则 GR1，此处不做）。"""
    if upside is None or upside <= 0:
        return "中性", "低"
    if action in ("建仓", "加仓"):
        rating: Rating = "买入" if (upside or 0) >= 20 else "增持"
        conf: Confidence = "高" if (upside or 0) >= 20 else "中"
    elif action == "持有":
        rating, conf = "增持", "中"
    elif action == "减仓":
        rating, conf = "中性", "中"
    else:
        rating, conf = "中性", "低"
    return rating, conf


def build_signal_dim(
    ctx: SharedContext,
    conclusion_payload: Dict[str, Any],
    scenarios_payload: Dict[str, Any],
) -> SignalDim:
    """S1：综合信号（波次 3，依赖链末端产出）。资金流出降级由护栏 GR1 裁决。"""
    conclusion = (conclusion_payload or {}).get("conclusion") or {}
    action = str(conclusion.get("action") or "观察")
    ev = (scenarios_payload or {}).get("expected_value")
    target = scenarios_payload.get("target_price") or ev
    current = ctx.quote.get("price")


    if scenarios_payload.get("status") != "ok" or target is None:
        return SignalDim(status="degraded", degraded_reason=with_valuation_cascade("估值依据不足"), rating="中性", confidence="低",
                         one_sentence="估值不可用，暂观察；技术点位见独立行动计划。", narrative="缺少估值依据，不生成目标价或期望收益。")

    if not isinstance(current, (int, float)) or isinstance(current, bool) or not isfinite(current) or current <= 0:
        return SignalDim(status="degraded", degraded_reason="当前价格缺失或无效，无法计算参考价差", rating="中性", confidence="低",
                         target_price=float(target), one_sentence="已有机构目标价参考，但当前价格不可用，暂不形成综合信号。", narrative="估值来源见情景维度；缺少现价不计算价差或买卖评级。")

    upside = _upside(
        float(current) if isinstance(current, (int, float)) else None,
        float(target) if isinstance(target, (int, float)) else None,
    )

    rating, conf = _map_rating(action, upside)
    prob_sum = (scenarios_payload or {}).get("probability_sum") or 0
    one_sentence = (
        f"长线行动「{action}」，" + (f"机构目标价综合值 {target} 元（非概率期望值），" if scenarios_payload.get("valuation_basis") == "analyst_consensus" else f"三情景期望价值 {ev}（概率和 {prob_sum * 100:.0f}%），") +
        f"当前价 {current}，参考价差 {upside}%。"
    ).strip()
    return SignalDim(
        rating=rating,
        target_price=float(target) if isinstance(target, (int, float)) else None,
        expected_value=float(ev) if isinstance(ev, (int, float)) else None,
        signal_type=f"长线锚定·{action}",
        confidence=conf,
        one_sentence=one_sentence,
        narrative="信号由长线结论与有来源的估值参考合成（规则），机构目标价不保证实现。",
    )
