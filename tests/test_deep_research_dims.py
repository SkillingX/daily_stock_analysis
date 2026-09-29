# -*- coding: utf-8 -*-
"""双轨引擎单元测试：契约 / LR 标定 / 概率归一化 / 护栏规则 / 渲染结构。

全部离线（无网络、无 LLM），覆盖需求文档 §5.3 护栏表与 §5.4 标定表的核心路径。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from icontract import ViolationError

from src.deep_research_dims.bayesian_dim import calibrate_lr
from src.deep_research_dims.context import SharedContext
from src.deep_research_dims.data_dim import build_data_dim
from src.deep_research_dims.guardrail import (
    apply_guardrails,
    rule1_capital_outflow,
    rule2_negative_edge,
    rule3_weak_six_dim,
    rule7_signal_conclusion_consistency,
    rule9_evidence_freshness,
)
from src.deep_research_dims.plan_dim import compute_atr14
from src.deep_research_dims.render import build_view, render_markdown, validate_structure
from src.deep_research_dims.scenarios_dim import (
    build_scenarios_dim,
    expected_value,
    normalize_probabilities,
)
from src.schemas.bayesian_framework import EvidenceItem
from src.schemas.deep_research_dims import (
    BayesianDim,
    ConclusionDim,
    DataDim,
    DIM_IDS,
    HistoryDim,
    IntelDim,
    parse_dim,
    PhaseDim,
    PlanDim,
    ScenariosDim,
    SignalDim,
    SixDimDim,
    SupplyChainDim,
)
from src.schemas.value_scenarios import Scenario


def _fake_history(n: int = 30, base: float = 10.0) -> list[dict]:
    rows = []
    for i in range(n):
        close = base + i * 0.1
        rows.append(
            {
                "date": str(date(2026, 8, 1) + timedelta(days=i)),
                "open": close - 0.05,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
                "volume": 10000 + i * 100,
            }
        )
    return rows


def _ctx(**overrides) -> SharedContext:
    ctx = SharedContext(stock_code="600519", stock_name="贵州茅台", as_of="2026-09-28T10:00:00")
    ctx.history = _fake_history()
    ctx.quote = {"price": 13.0}
    ctx.fundamental = {"pe_ttm": 25.0, "pb": 5.0}
    for k, v in overrides.items():
        setattr(ctx, k, v)
    return ctx


# ---------------------------------------------------------------------------
# LR 标定表（§5.4-2）
# ---------------------------------------------------------------------------


class TestLRCalibration:
    def test_in_range_passthrough(self):
        lr, clamped = calibrate_lr("strong_positive", 3.0)
        assert lr == 3.0 and not clamped

    def test_out_of_range_clamps_to_midpoint(self):
        lr, clamped = calibrate_lr("weak_positive", 9.9)
        assert clamped and 1.2 <= lr < 2.0

    def test_none_uses_midpoint(self):
        lr, clamped = calibrate_lr("neutral", None)
        assert clamped and 0.9 <= lr <= 1.1

    def test_invalid_strength_rejected(self):
        with pytest.raises(ViolationError):
            calibrate_lr("super_positive", 2.0)


# ---------------------------------------------------------------------------
# 概率归一化与 EV（护栏规则 4 底层）
# ---------------------------------------------------------------------------


class TestScenarioMath:
    def test_normalize_sum_exactly_one(self):
        scenarios = [
            Scenario(type="optimistic", probability=0.333),
            Scenario(type="neutral", probability=0.333),
            Scenario(type="pessimistic", probability=0.334),
        ]
        normalized = normalize_probabilities(scenarios)
        assert sum(s.probability for s in normalized) == pytest.approx(1.0, abs=1e-9)

    def test_expected_value_weighted(self):
        scenarios = [
            Scenario(type="optimistic", probability=0.5, value_anchor=20.0),
            Scenario(type="pessimistic", probability=0.5, value_anchor=10.0),
        ]
        assert expected_value(scenarios) == 15.0

    def test_expected_value_ignores_missing_anchor(self):
        scenarios = [Scenario(type="neutral", probability=1.0, value_anchor=None)]
        assert expected_value(scenarios) is None


# ---------------------------------------------------------------------------
# 维度后台代码
# ---------------------------------------------------------------------------


class TestDataDim:
    def test_build_with_history(self):
        dim = build_data_dim(_ctx())
        assert dim.status == "ok"
        p = dim.perspective
        assert p is not None
        assert p.price_position.current_price == 13.0
        assert p.trend_status.ma_alignment in ("多头排列", "纠缠", "空头排列")
        assert dim.ma250_deviation_pct is None  # 30 条数据不足 250 日均线

    def test_degraded_without_history(self):
        dim = build_data_dim(_ctx(history=[]))
        assert dim.status == "degraded"


class TestAtr:
    def test_atr_positive(self):
        atr = compute_atr14(_fake_history(30))
        assert atr > 0

    def test_atr_requires_15(self):
        with pytest.raises(ViolationError):
            compute_atr14(_fake_history(5))


class TestValuationBasis:
    def test_loss_maker_switches_to_pb(self):
        ctx = _ctx()
        ctx.fundamental = {"pe_ttm": -3.0}
        dim = build_scenarios_dim(ctx, current_price=13.0)
        assert dim.valuation_basis == "PB"

    def test_profit_maker_uses_pe(self):
        dim = build_scenarios_dim(_ctx(), current_price=13.0)
        assert dim.valuation_basis == "PE_TTM"
        assert dim.probability_sum == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 护栏规则（§5.3）
# ---------------------------------------------------------------------------


def _bayesian_payload(edge: float = 0.1, evidence_dates: list[str] | None = None) -> dict:
    evidence = [
        EvidenceItem(
            evidence=f"e{i}",
            strength="weak_positive",
            lr=1.5,
            posterior_p=0.5,
            date=d,
        )
        for i, d in enumerate(evidence_dates or [str(date.today())] * 3)
    ]
    from src.schemas.bayesian_framework import BayesianFramework

    return BayesianDim(
        bayesian=BayesianFramework(
            prior_p=0.6,
            market_implied_p=0.5,
            edge=edge,
            posterior_p=0.65,
            position_suggestion="3-5%",
            evidence_log=evidence,
        )
    ).model_dump()


class TestGuardrailRules:
    def test_rule2_downgrades_negative_edge(self):
        conclusion = ConclusionDim(
            conclusion={
                "prior_p": 0.6,
                "market_implied_p": 0.5,
                "edge": -0.1,
                "position": "建仓",
                "action": "建仓",
            }
        ).model_dump()
        events = rule2_negative_edge(conclusion)
        assert len(events) == 1 and events[0].rule_id == "GR2_negative_edge"

    def test_rule3_downgrades_weak_six_dim(self):
        conclusion = ConclusionDim(
            conclusion={
                "prior_p": 0.6,
                "market_implied_p": 0.5,
                "edge": 0.2,
                "position": "加仓",
                "action": "加仓",
            }
        ).model_dump()
        six = SixDimDim(
            framework={
                "dimension_total": 40.0,
                "dimensions": [],
                "scoring_version": "v1",
            }
        ).model_dump()
        events = rule3_weak_six_dim(conclusion, six)
        assert len(events) == 1

    def test_rule9_caps_stale_evidence(self):
        stale = _bayesian_payload(evidence_dates=["2026-01-01"] * 3)
        events = rule9_evidence_freshness(stale)
        assert any(e.rule_id == "GR9_evidence_freshness" for e in events)

    def test_rule9_passes_with_fresh_evidence(self):
        events = rule9_evidence_freshness(_bayesian_payload())
        assert events == []

    def test_rule7_flags_conflict(self):
        signal = SignalDim(rating="买入").model_dump()
        conclusion = ConclusionDim(
            conclusion={
                "prior_p": 0.6,
                "market_implied_p": 0.5,
                "edge": 0.2,
                "position": "减仓",
                "action": "减仓",
            }
        ).model_dump()
        events = rule7_signal_conclusion_consistency(signal, conclusion)
        assert len(events) == 1

    def test_apply_guardrails_override_conclusion(self):
        payloads = {
            "conclusion": ConclusionDim(
                conclusion={
                    "prior_p": 0.6,
                    "market_implied_p": 0.7,
                    "edge": -0.1,
                    "position": "建仓",
                    "action": "建仓",
                }
            ).model_dump(),
            "signal": SignalDim(rating="中性").model_dump(),
            "six_dim": SixDimDim(
                framework={"dimension_total": 80.0, "dimensions": [], "scoring_version": "v1"}
            ).model_dump(),
            "intel": IntelDim().model_dump(),
            "bayesian": _bayesian_payload(edge=-0.1),
            "scenarios": ScenariosDim(probability_sum=1.0).model_dump(),
        }
        events, adjusted = apply_guardrails(payloads)
        assert any(e.rule_id == "GR2_negative_edge" for e in events)
        assert adjusted["conclusion"]["conclusion"]["action"] == "观察"

    def test_rule1_single_authority_no_double_downgrade(self):
        """A1 回归：信号构建不预降级，GR1 只处罚一次（买入 → 增持，而非 → 中性）。"""
        from src.deep_research_dims.signal_dim import build_signal_dim

        ctx = _ctx()
        conclusion = ConclusionDim(
            conclusion={
                "prior_p": 0.7,
                "market_implied_p": 0.5,
                "edge": 0.2,
                "position": "建仓",
                "action": "建仓",
            }
        ).model_dump()
        scenarios = ScenariosDim(
            probability_sum=1.0, expected_value=20.0
        ).model_dump()
        signal = build_signal_dim(ctx, conclusion, scenarios)
        assert signal.rating == "买入"  # 构建期不降级（上行 20/13-1>20%）

        intel = IntelDim(
            intelligence={
                "risk_alerts": ["主力资金连续 5 日净流出（news）"],
            }
        ).model_dump()
        events = rule1_capital_outflow(signal.model_dump(), intel)
        assert len(events) == 1
        payloads = {
            "signal": signal.model_dump(),
            "intel": intel,
            "conclusion": conclusion,
            "scenarios": scenarios,
            "six_dim": SixDimDim(
                framework={"dimension_total": 80.0, "dimensions": [], "scoring_version": "v1"}
            ).model_dump(),
            "bayesian": _bayesian_payload(),
        }
        _events, adjusted = apply_guardrails(payloads)
        assert adjusted["signal"]["rating"] == "增持"  # 一次降级到位

    def test_rule7_caps_to_action_rating_cap(self):
        """A2 回归：买入 vs 行动「观察」→ 评级收敛到「中性」（上限映射），非只降一级。"""
        signal = SignalDim(rating="买入").model_dump()
        conclusion = ConclusionDim(
            conclusion={
                "prior_p": 0.6,
                "market_implied_p": 0.5,
                "edge": 0.2,
                "position": "观察",
                "action": "观察",
            }
        ).model_dump()
        events = rule7_signal_conclusion_consistency(signal, conclusion)
        assert len(events) == 1
        payloads = {
            "signal": signal,
            "conclusion": conclusion,
            "intel": IntelDim().model_dump(),
            "six_dim": SixDimDim(
                framework={"dimension_total": 80.0, "dimensions": [], "scoring_version": "v1"}
            ).model_dump(),
            "bayesian": _bayesian_payload(),
            "scenarios": ScenariosDim(probability_sum=1.0).model_dump(),
        }
        _events, adjusted = apply_guardrails(payloads)
        assert adjusted["signal"]["rating"] == "中性"

    def test_append_override_note(self):
        from src.deep_research_dims.guardrail import append_override_note

        payload = {"rationale": "原论证建仓。", "narrative": ""}
        updated = append_override_note(payload, "行动", "建仓", "观察")
        assert "由「建仓」修订为「观察」" in updated["rationale"]
        unchanged = append_override_note(payload, "行动", "建仓", "建仓")
        assert unchanged["rationale"] == "原论证建仓。"


# ---------------------------------------------------------------------------
# 契约解析 + 渲染结构
# ---------------------------------------------------------------------------


class TestPhaseFallback:
    def test_phase_never_degrades_on_calendar_failure(self, monkeypatch):
        """日历数据源抛异常时回退本地兜底，维度不降级（E2E 抓到的真实问题）。"""
        from src.deep_research_dims import phase_dim

        def _boom(*_a, **_k):
            raise RuntimeError("trade_cal permission denied")

        # phase_dim 用的是 from-import 名，直接 patch 模块内引用
        monkeypatch.setattr(phase_dim, "is_market_open", _boom)
        dim = phase_dim.build_phase_dim(_ctx())
        assert dim.status == "ok"
        assert dim.phase is not None
        assert any("兜底" in x for x in dim.phase.data_limitations)


def _all_default_dims() -> dict:
    dims = {
        "signal": SignalDim(),
        "data": DataDim(),
        "intel": IntelDim(),
        "plan": PlanDim(),
        "phase": PhaseDim(),
        "history": HistoryDim(),
        "six_dim": SixDimDim(),
        "bayesian": BayesianDim(),
        "conclusion": ConclusionDim(),
        "supply_chain": SupplyChainDim(),
        "scenarios": ScenariosDim(probability_sum=1.0),
    }
    assert set(dims) == set(DIM_IDS)
    return dims


class TestContractsAndRender:
    def test_parse_dim_roundtrip(self):
        for dim_id, model in _all_default_dims().items():
            parsed = parse_dim(dim_id, model.model_dump())
            assert parsed.dim == dim_id

    def test_parse_dim_unknown_rejected(self):
        with pytest.raises(ValueError):
            parse_dim("nope", {})

    def test_render_structure_complete(self):
        dims = _all_default_dims()
        view = build_view(
            "贵州茅台", "600519", "2026-09-28T10:00:00", ["测试局限"], dims, []
        )
        markdown = render_markdown(view)
        assert validate_structure(markdown) == []
        assert "不构成投资建议" in markdown
        assert "测试局限" in markdown

    def test_render_includes_guardrail_table(self):
        from src.schemas.deep_research_dims import GuardrailEvent

        dims = _all_default_dims()
        events = [
            GuardrailEvent(
                rule_id="GR2_negative_edge",
                dim="conclusion",
                action="override",
                reason="Edge ≤ 0",
            )
        ]
        view = build_view("贵州茅台", "600519", "2026-09-28T10:00:00", [], dims, events)
        markdown = render_markdown(view)
        assert "GR2_negative_edge" in markdown
        assert "Edge ≤ 0" in markdown
