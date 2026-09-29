# -*- coding: utf-8 -*-
"""S4 作战计划：ATR 规则点位 + 风控仓位，LLM 仅叙述。"""

from __future__ import annotations

from typing import Any, Dict, List

from icontract import ensure, require

from src.deep_research_dims.context import SharedContext
from src.schemas.deep_research_dims import PlanDim
from src.schemas.report_schema import PositionStrategy, SniperPoints


@require(lambda history: len(history) >= 15, "计算 ATR14 至少需要 15 条日线")
@ensure(lambda result: result > 0, "ATR 必须为正")
def compute_atr14(history: List[Dict[str, Any]]) -> float:
    """14 日 ATR（Wilder 简化版：TR 的 14 日均值）。"""
    trs: List[float] = []
    for prev, cur in zip(history[-15:-1], history[-14:]):
        high, low = float(cur["high"]), float(cur["low"])
        prev_close = float(prev["close"])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return round(sum(trs) / len(trs), 4)


def _pick_stop(data: Dict[str, Any], atr: float, current: float) -> float:
    """止损取「MA20 与 1.5×ATR 孰近」的下沿，避免固定百分比一刀切。"""
    ma20 = (data.get("perspective") or {}).get("price_position", {}).get("ma20")
    atr_stop = current - 1.5 * atr
    if isinstance(ma20, (int, float)) and ma20 < current:
        return round(max(float(ma20), atr_stop), 2)
    return round(atr_stop, 2)


def build_plan_dim(
    ctx: SharedContext,
    data_dim_payload: Dict[str, Any],
    position_suggestion: str,
) -> PlanDim:
    """S4：ATR 点位 + 仓位映射。LLM 仅写 narrative。"""
    if len(ctx.history) < 15:
        return PlanDim(status="degraded", degraded_reason="日线不足 15 条，无法计算 ATR")
    price_pos = (data_dim_payload or {}).get("perspective", {}).get("price_position", {})
    current = price_pos.get("current_price") or ctx.quote.get("price")
    if not isinstance(current, (int, float)) or current <= 0:
        return PlanDim(status="degraded", degraded_reason="现价缺失，无法计算点位")

    atr = compute_atr14(ctx.history)
    support = price_pos.get("support_level")
    ideal = round(min(current, float(support) + 0.5 * atr), 2) if isinstance(support, (int, float)) else round(current - atr, 2)
    secondary = round(ideal - 0.5 * atr, 2)
    stop = _pick_stop(data_dim_payload, atr, float(current))
    take_profit = round(float(current) + 3 * atr, 2)

    sniper = SniperPoints(
        ideal_buy=ideal,
        secondary_buy=secondary,
        stop_loss=stop,
        take_profit=take_profit,
    )
    strategy = PositionStrategy(
        suggested_position=position_suggestion or "观察",
        entry_plan=f"理想买 {ideal} 附近首批，{secondary} 补第二批；触发需量比≥1.5",
        risk_control=f"跌破 {stop} 无条件止损（ATR14={atr}）；单笔亏损控制在账户 1% 内",
    )
    checklist = [
        f"价格触及 {ideal} 且当日量比 ≥ 1.5 才执行首批",
        f"跌破 {stop} 立即止损，不补仓",
        f"到达 {take_profit} 分批止盈至少一半",
        "出现报告外重大公告，暂停执行并重检",
    ]
    return PlanDim(
        sniper_points=sniper,
        position_strategy=strategy,
        action_checklist=checklist,
        atr14=atr,
        basis=f"ATR14={atr}，支撑={support}，现价={current}",
        narrative=strategy.risk_control or "",
    )
