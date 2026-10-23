# -*- coding: utf-8 -*-
"""S4 作战计划：ATR 规则点位 + 风控仓位，LLM 仅叙述。"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

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


def _pick_stop(data: Dict[str, Any], atr: float, entry_anchor: float) -> float:
    """止损取「MA20 与 1.5×ATR 孰近」的下沿，基准为同一方案的首批入场锚。

    止损服务于「按该方案入场后的风险控制」，锚定入场价而非现价——
    两个语境混用会产生止损高于买入价的矛盾点位（issue #39 反例）。
    """
    ma20 = (data.get("perspective") or {}).get("price_position", {}).get("ma20")
    atr_stop = entry_anchor - 1.5 * atr
    if isinstance(ma20, (int, float)) and ma20 < entry_anchor:
        return round(max(float(ma20), atr_stop), 2)
    return round(atr_stop, 2)


def _valid_positive(value: Any) -> bool:
    """价格类输入合法性：数值、有限且为正（bool 不是价格）。"""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) > 0
    )


def sniper_relation_violation(sniper: SniperPoints) -> Optional[str]:
    """多头条件买入方案的必要价格关系；返回违规描述或 None（有效）。

    不变式：各点位有限且为正，且止损 < 补仓 < 首批 ≤ 现价锚，止盈 > 首批。
    无法满足时不输出可执行买入指令，保留观察（issue #39）。
    """
    ideal = float(sniper.ideal_buy) if _valid_positive(sniper.ideal_buy) else None
    secondary = float(sniper.secondary_buy) if _valid_positive(sniper.secondary_buy) else None
    stop = float(sniper.stop_loss) if _valid_positive(sniper.stop_loss) else None
    take_profit = float(sniper.take_profit) if _valid_positive(sniper.take_profit) else None
    if ideal is None or secondary is None or stop is None or take_profit is None:
        return "点位存在缺失、非正或非有限数值"
    if ideal < secondary:
        return f"首批入场价 {ideal} 低于补仓价 {secondary}"
    if secondary <= stop:
        return f"止损 {stop} 不低于补仓价 {secondary}，入场后止损线高于成本"
    if take_profit <= ideal:
        return f"止盈 {take_profit} 不高于首批入场价 {ideal}"
    return None


@ensure(
    lambda result: result.sniper_points is None
    or sniper_relation_violation(result.sniper_points) is None,
    "可执行狙击点位必须满足同一多头方案的价格关系",
)
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
    if not _valid_positive(current):
        return PlanDim(status="degraded", degraded_reason="现价缺失或非法，无法计算点位")
    current = float(current)

    atr = compute_atr14(ctx.history)
    if not math.isfinite(atr) or atr <= 0:
        return PlanDim(status="degraded", degraded_reason=f"ATR 计算结果非法（{atr}），无法计算点位")
    support = price_pos.get("support_level")
    # 支撑缺失（None）走现价-ATR 回退；支撑为脏值（≤0/非有限）是数据质量异常，
    # 按无效输入保留观察并披露原因，不得放宽校验继续出可执行计划。
    if support is not None and not _valid_positive(support):
        return PlanDim(
            sniper_points=None,
            position_strategy=PositionStrategy(
                suggested_position="观察",
                entry_plan=(
                    f"支撑位数据无效（{support}），本次不形成可执行买入方案；"
                    f"保留观察，待支撑数据修复后重估"
                ),
                risk_control="支撑数据无效时无有效方案锚，不生成买卖指令",
            ),
            action_checklist=[
                "保留观察，不执行买入（未持仓视角；本计划未假设用户持仓）",
                "复核数据源支撑位口径，修复后重新生成方案",
                "出现报告外重大公告，暂停执行并重检",
            ],
            atr14=atr,
            basis=f"ATR14={atr}，支撑={support}（非法值），现价={current}",
            narrative=f"支撑位数据无效（{support}），无法形成一致的买入方案，保留观察。",
        )
    ideal = round(min(current, float(support) + 0.5 * atr), 2) if _valid_positive(support) else round(current - atr, 2)
    secondary = round(ideal - 0.5 * atr, 2)
    stop = _pick_stop(data_dim_payload, atr, float(ideal))
    take_profit = round(float(current) + 3 * atr, 2)

    sniper = SniperPoints(
        ideal_buy=ideal,
        secondary_buy=secondary,
        stop_loss=stop,
        take_profit=take_profit,
    )
    violation = sniper_relation_violation(sniper)
    if violation:
        # 不用任意固定倍数强行凑数：明确不形成可执行方案，保留观察及触发条件。
        watch_anchor = support if isinstance(support, (int, float)) else current
        return PlanDim(
            sniper_points=None,
            position_strategy=PositionStrategy(
                suggested_position="观察",
                entry_plan=(
                    f"当前价格结构未形成可执行买入方案（{violation}）；"
                    f"保留观察，触发条件：回落站稳支撑 {round(float(watch_anchor), 2)} 且量比放大"
                ),
                risk_control="无有效止损锚，不生成买卖指令；待形成一致价格关系后重估",
            ),
            action_checklist=[
                "保留观察，不执行买入（未持仓视角；本计划未假设用户持仓）",
                f"关注支撑 {round(float(watch_anchor), 2)} 附近量价表现，形成有效方案后再评估",
                "出现报告外重大公告，暂停执行并重检",
            ],
            atr14=atr,
            basis=f"ATR14={atr}，支撑={support}，现价={current}；{violation}",
            narrative="当前价格结构无法形成入场、补仓与止损一致的买入方案，保留观察。",
        )
    strategy = PositionStrategy(
        suggested_position=position_suggestion or "观察",
        entry_plan=(
            f"条件买入（未持仓视角）：回落至 {ideal} 且当日量比 ≥ 1.5 触发首批，"
            f"{secondary} 补第二批；触发前以观察为主，非按现价追高"
        ),
        risk_control=(
            f"首批成交后，跌破 {stop} 无条件止损（相对首批入场锚计算，ATR14={atr}）；"
            "单笔亏损控制在账户 1% 内；本计划未取得用户持仓成本，已有持仓请按自身成本另行评估"
        ),
    )
    checklist = [
        f"未持仓触发条件：价格回落至 {ideal} 且当日量比 ≥ 1.5 才执行首批买入",
        f"首批成交后跌破 {stop} 立即止损，不补仓（止损锚定首批成本）",
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
