# -*- coding: utf-8 -*-
"""S4 作战计划金融不变式契约测试（issue #39）。

同一多头条件买入方案的点位必须处于一致计算语境：
止损 < 补仓 < 首批 ≤ 现价、止盈 > 首批；无法满足时不输出可执行点位。
"""

from src.deep_research_dims.context import SharedContext
from src.deep_research_dims.plan_dim import build_plan_dim, sniper_relation_violation
from src.schemas.report_schema import SniperPoints


def _ctx(price: float, history_len: int = 15) -> SharedContext:
    """±1% 振幅 → TR=2%·price；price=100 时 ATR=2.0，点位关系可手算。"""
    ctx = SharedContext("fixture", "fixture", "2026-10-23")
    ctx.quote = {"price": price}
    ctx.history = [
        {"high": price * 1.01, "low": price * 0.99, "close": price}
        for _ in range(history_len)
    ]
    return ctx


def _payload(current: float, support=None, ma20=None) -> dict:
    position = {"current_price": current}
    if support is not None:
        position["support_level"] = support
    if ma20 is not None:
        position["ma20"] = ma20
    return {"perspective": {"price_position": position}}


def _flat_history(price: float, low: float, high: float, days: int = 15) -> list[dict]:
    """收盘恒定 → ATR = high-low 的均值（反例复现用：ATR=8.9983 语境）。"""
    return [{"high": high, "low": low, "close": price} for _ in range(days)]


class TestSniperRelations:
    def test_pm_counterexample_no_stop_above_buy(self):
        """现价 162.16 / 支撑 128.66 / ATR≈9：不得再出现买入 133 同时止损 148。"""
        ctx = SharedContext("fixture", "fixture", "2026-10-23")
        ctx.quote = {"price": 162.16}
        ctx.history = _flat_history(100.0, 95.50085, 104.49915)
        plan = build_plan_dim(ctx, _payload(162.16, support=128.66), "观察")
        if plan.sniper_points is None:
            return  # 不形成可执行方案也满足验收
        assert sniper_relation_violation(plan.sniper_points) is None
        assert float(plan.sniper_points.stop_loss) < float(plan.sniper_points.secondary_buy)

    def test_normal_plan_relations_hold(self):
        ctx = _ctx(100.0)
        plan = build_plan_dim(ctx, _payload(100.0, support=95.0, ma20=94.0), "观察")
        sniper = plan.sniper_points
        assert sniper is not None
        assert sniper_relation_violation(sniper) is None
        # 支撑 + 0.5×ATR 回调买入；MA20 下沿更近时作为止损锚
        assert float(sniper.ideal_buy) == 96.0
        assert float(sniper.secondary_buy) == 95.0
        assert float(sniper.stop_loss) == 94.0

    def test_ma_close_to_entry_anchor_keeps_watch_only(self):
        """MA20 介于补仓价与首批价之间 → 无法形成一致方案，保留观察不出点位。"""
        ctx = _ctx(100.0)
        plan = build_plan_dim(ctx, _payload(100.0, support=95.0, ma20=95.6), "观察")
        assert plan.sniper_points is None
        assert "未形成" in (plan.position_strategy.entry_plan or "")
        assert plan.position_strategy.suggested_position == "观察"

    def test_support_far_below_price_uses_entry_anchor_stop(self):
        """支撑远低现价：止损相对入场锚而非现价，不再出现止损高于买价。"""
        ctx = _ctx(160.0)
        plan = build_plan_dim(ctx, _payload(160.0, support=90.0), "观察")
        sniper = plan.sniper_points
        assert sniper is not None
        assert sniper_relation_violation(sniper) is None
        assert float(sniper.stop_loss) < float(sniper.secondary_buy) < float(sniper.ideal_buy)

    def test_no_support_uses_current_minus_atr(self):
        ctx = _ctx(100.0)
        plan = build_plan_dim(ctx, _payload(100.0), "观察")
        sniper = plan.sniper_points
        assert sniper is not None
        assert sniper_relation_violation(sniper) is None

    def test_ma_above_current_ignored_as_stop_anchor(self):
        """MA20 高于现价时不能作为多头止损锚。"""
        ctx = _ctx(100.0)
        plan = build_plan_dim(ctx, _payload(100.0, support=95.0, ma20=110.0), "观察")
        sniper = plan.sniper_points
        if sniper is not None:
            assert float(sniper.stop_loss) < float(sniper.secondary_buy)


class TestPlanDegradation:
    def test_short_history_degrades(self):
        ctx = SharedContext("fixture", "fixture", "2026-10-23")
        ctx.quote = {"price": 100.0}
        ctx.history = []
        plan = build_plan_dim(ctx, _payload(100.0, support=95.0), "观察")
        assert plan.status == "degraded"

    def test_missing_price_degrades(self):
        ctx = SharedContext("fixture", "fixture", "2026-10-23")
        ctx.quote = {}
        ctx.history = _ctx(100.0).history
        plan = build_plan_dim(ctx, {"perspective": {"price_position": {}}}, "观察")
        assert plan.status == "degraded"

    def test_invalid_price_degrades(self):
        ctx = _ctx(100.0)
        plan = build_plan_dim(ctx, _payload(-1.0, support=95.0), "观察")
        assert plan.status == "degraded"

    def test_negative_support_keeps_watch_only(self):
        """审查反例：现价 100、ATR 2、支撑 -1 → 不得输出首批 0/补仓 -1/止损 -3 的可执行计划。"""
        ctx = _ctx(100.0)
        plan = build_plan_dim(ctx, _payload(100.0, support=-1.0), "观察")
        assert plan.sniper_points is None
        entry_plan = plan.position_strategy.entry_plan or ""
        assert "支撑位数据无效" in entry_plan
        assert plan.position_strategy.suggested_position == "观察"

    def test_sniper_rejects_non_positive_points(self):
        """不变式入口：非正/NaN 点位直接判违规，不靠顺序校验兜底。"""
        bad = SniperPoints(ideal_buy=0.0, secondary_buy=-1.0, stop_loss=-3.0, take_profit=6.0)
        assert sniper_relation_violation(bad) is not None
        nan_sniper = SniperPoints(ideal_buy=float("nan"), secondary_buy=1.0, stop_loss=0.5, take_profit=2.0)
        assert sniper_relation_violation(nan_sniper) is not None


class TestPlanWording:
    def test_valid_plan_marks_conditional_entry(self):
        ctx = _ctx(100.0)
        plan = build_plan_dim(ctx, _payload(100.0, support=95.0, ma20=94.0), "观察")
        assert "条件买入" in (plan.position_strategy.entry_plan or "")
        # 未取得真实持仓成本，不得声称为用户实际持仓计算止损
        assert "持仓" in (plan.position_strategy.risk_control or "")
        assert any("触发" in item or "未持仓" in item for item in plan.action_checklist)

    def test_watch_plan_keeps_trigger_context(self):
        ctx = _ctx(100.0)
        plan = build_plan_dim(ctx, _payload(100.0, support=95.0, ma20=95.6), "观察")
        entry_plan = plan.position_strategy.entry_plan or ""
        assert "支撑" in entry_plan or "95" in entry_plan
        assert plan.atr14 and plan.atr14 > 0
