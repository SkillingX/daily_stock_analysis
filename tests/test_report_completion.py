"""Completion reflects core evidence; rule-based phase needs no model request."""

from datetime import date
from typing import Any

import pytest

from src.deep_research.researchers.ownership import parse


def ownership_payload(**updates: Any) -> dict[str, Any]:
    return {
        "controller": "公告披露的实际控制人甲（primary）",
        "top_holders": [{"name": "股东甲", "source": "定期报告"}],
        "executives": [{"name": "高管甲", "detail": "董事长", "source": "任职公告"}],
        "score": 80,
        **updates,
    }


def test_supplementary_gaps_do_not_fail_verified_core_research() -> None:
    gaps = ["完整履历未取得", "近一年减持公告未取得", "知识库无资料", "ifind:products:invalid_response"]
    dim, _ = parse(ownership_payload(core_data_gaps=[], data_gaps=gaps), 1)
    assert dim.status == "ok" and dim.score == 80
    assert dim.data_gaps == gaps
    assert dim.core_data_gaps == []


@pytest.mark.parametrize("core_gaps", [["实控人仅为推测，未取得认定公告"], {"unknown": "缺项"}, ""])
def test_unverified_or_malformed_core_assessment_stays_incomplete(core_gaps: Any) -> None:
    dim, _ = parse(ownership_payload(core_data_gaps=core_gaps), 1)
    assert dim.status == "degraded" and dim.score is None
    assert dim.core_data_gaps and set(dim.core_data_gaps) <= set(dim.data_gaps)


@pytest.mark.parametrize("field", ["controller", "top_holders", "executives"])
def test_empty_core_cannot_be_overridden_by_model_assessment(field: str) -> None:
    dim, _ = parse(ownership_payload(core_data_gaps=[], **{field: "" if field == "controller" else []}), 1)
    assert dim.status == "degraded" and dim.score is None
    assert dim.core_data_gaps


def test_legacy_unknown_gaps_and_recorded_model_failure_stay_incomplete() -> None:
    legacy, _ = parse(ownership_payload(data_gaps=["未分类的资料缺口"]), 1)
    failed, _ = parse(ownership_payload(core_data_gaps=[], degraded_reason="研究模型未完成"), 1)
    assert legacy.status == failed.status == "degraded"
    assert legacy.score is failed.score is None
    assert failed.degraded_reason == "研究模型未完成"


def test_partial_core_list_adaptation_cannot_claim_complete() -> None:
    dim, _ = parse(ownership_payload(core_data_gaps=[], executives=[{"name": "已核实高管"}, {}]), 1)
    assert dim.status == "degraded" and dim.score is None
    assert any("核心高管" in gap for gap in dim.core_data_gaps)


def test_partial_supplier_details_remain_core_gap() -> None:
    dim, _ = parse(ownership_payload(core_data_gaps=[], shareholder_details={"coverage": "partial", "errors": ["period_mismatch"]}), 1)
    assert dim.status == "degraded" and dim.score is None
    assert "period_mismatch" in dim.core_data_gaps


def test_malformed_general_gap_cannot_be_hidden_by_empty_core_assessment() -> None:
    dim, _ = parse(ownership_payload(core_data_gaps=[], data_gaps=[{}]), 1)
    assert dim.status == "degraded" and dim.score is None
    assert "无法识别" in dim.data_gaps[0]


@pytest.mark.parametrize("controller", ["  ", "未知", "资料缺失"])
def test_controller_placeholder_is_not_core_evidence(controller: str) -> None:
    dim, _ = parse(ownership_payload(core_data_gaps=[], controller=controller), 1)
    assert dim.status == "degraded" and "实际控制人信息" in dim.core_data_gaps


def test_phase_calendar_failure_keeps_explicit_rule_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.deep_research_dims import phase_dim
    from src.deep_research_dims.context import SharedContext

    def unavailable(*args: Any) -> None:
        raise RuntimeError("calendar unavailable")

    monkeypatch.setattr(phase_dim, "is_market_open", unavailable)
    dim = phase_dim.build_phase_dim(SharedContext("600519", "样本", "2026-10-29"))
    assert dim.narrative and dim.phase.action_window in dim.narrative
    assert "兜底" in dim.phase.data_limitations[0]


def test_sector_gap_keeps_the_capital_ranking_contract() -> None:
    from src.deep_research_dims.context import SharedContext
    from src.deep_research_dims.sector_dim import build_sector_dim
    dim = build_sector_dim(SharedContext("600519", "样本", "2026-10-29", fundamental={"industry_hint": "食品饮料"}))
    assert "板块资金排行未取得" in dim.data_gaps


@pytest.mark.parametrize("skip_narrations", [False, True])
def test_full_orchestrator_phase_has_rule_text_and_zero_narration_calls(
    monkeypatch: pytest.MonkeyPatch, skip_narrations: bool,
) -> None:
    from src.agent.deep_research import orchestrator
    from src.core.trading_calendar import MarketPhase
    from src.deep_research_dims import phase_dim
    from src.deep_research_dims.context import SharedContext

    monkeypatch.setattr(orchestrator, "build_shared_context", lambda *a: SharedContext("600519", "样本", "2026-10-29"))
    monkeypatch.setattr(phase_dim, "is_market_open", lambda *a: True)
    monkeypatch.setattr(phase_dim, "infer_market_phase", lambda *a: MarketPhase.INTRADAY)
    monkeypatch.setattr(phase_dim, "get_effective_trading_date", lambda *a: date(2026, 10, 29))
    calls: list[str] = []

    def narrate(adapter: Any, dim: str, *args: Any) -> str:
        calls.append(dim)
        return "不应再向模型索取阶段说明"

    monkeypatch.setattr(orchestrator, "narrate", narrate)
    result = orchestrator.run_dual_track(
        "600519", "样本", None, dims_filter={"phase"}, force_refresh=True,
        skip_narrations=skip_narrations, write_cache=False,
    )
    dim = result.dims["phase"]
    assert calls == []
    assert dim.status == "ok" and dim.narrative
    assert dim.phase.action_window in dim.narrative
    assert dim.phase.next_check_time in dim.narrative
    # The report already renders these structured phase facts directly.
    assert dim.phase.action_window in result.markdown
    assert dim.phase.next_check_time in result.markdown
