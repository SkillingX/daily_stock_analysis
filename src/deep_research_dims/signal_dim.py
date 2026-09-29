# -*- coding: utf-8 -*-
"""S1 信号：S2+S3+L3+L5 合成评级与目标价。叙述型 LLM。

合成规则（机器）：评级由 长线行动×EV上行空间×资金流 三因子映射；
护栏规则 1/7 在此之后裁决（见 guardrail.py）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from src.deep_research_dims.context import SharedContext
from src.schemas.deep_research_dims import Confidence, Rating, SignalDim


def _upside(current: Optional[float], target: Optional[float]) -> Optional[float]:
    if current and target and current > 0:
        return round((target - current) / current * 100, 2)
    return None


def _map_rating(action: str, upside: Optional[float]) -> tuple[Rating, Confidence]:
    """机器评级映射（资金流出降级的唯一权威是护栏规则 GR1，此处不做）。"""
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
    current = ctx.quote.get("price")
    upside = _upside(
        float(current) if isinstance(current, (int, float)) else None,
        float(ev) if isinstance(ev, (int, float)) else None,
    )

    rating, conf = _map_rating(action, upside)
    prob_sum = (scenarios_payload or {}).get("probability_sum") or 0
    one_sentence = (
        f"长线行动「{action}」，三情景期望价值 {ev}（概率和 {prob_sum * 100:.0f}%），"
        f"当前价 {current}，期望上行空间 {upside}%。"
    ).strip()
    return SignalDim(
        rating=rating,
        target_price=float(ev) if isinstance(ev, (int, float)) else None,
        expected_value=float(ev) if isinstance(ev, (int, float)) else None,
        signal_type=f"长线锚定·{action}",
        confidence=conf,
        one_sentence=one_sentence,
        narrative="信号由长线结论与情景 EV 合成（规则）。",
    )
