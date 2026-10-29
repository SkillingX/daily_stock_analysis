"""Two explicit top-ten snapshots measure concentration, never institutional buying."""
from decimal import Decimal
import json
from types import SimpleNamespace
from typing import Any

import pytest

from data_provider.ifind_fundamental_adapter import IfindShareholders


def holders(period: str, ratio: float = 6.0) -> dict[str, Any]:
    return {"coverage": "complete", "items": [
        {"name": f"股东{rank}", "rank": rank, "report_date": period,
         "ratio": {"source": "ifind", "value": ratio, "period": period, "unit": "percentage_point"},
         "shares": {"source": "ifind", "value": 100.0, "period": period, "unit": "shares"}}
        for rank in range(1, 11)
    ]}


def test_concentration_uses_each_period_top_ten_and_decimal_difference() -> None:
    from data_provider.ifind_fundamental_adapter import shareholder_concentration_change
    current = holders("2026-09-30", 6.126)
    current["items"][9]["name"] = "新进入前十，不能称原股东清仓"
    values = shareholder_concentration_change(IfindShareholders.model_validate_json(json.dumps(current)), IfindShareholders.model_validate_json(json.dumps(holders("2026-06-30", 6.211))))
    assert values == (Decimal("-0.850"), Decimal("61.260"), Decimal("62.110"))


def test_zero_change_is_valid() -> None:
    from data_provider.ifind_fundamental_adapter import shareholder_concentration_change
    values = shareholder_concentration_change(
        IfindShareholders.model_validate_json(json.dumps(holders("2026-09-30"))),
        IfindShareholders.model_validate_json(json.dumps(holders("2026-06-30"))),
    )
    assert values == (Decimal(0), Decimal(60), Decimal(60))


@pytest.mark.parametrize("bad", ["missing_rank", "duplicate_rank", "wrong_unit", "partial", "mixed_period", "ratio_period", "future", "reversed", "sum_exceeds_100"])
def test_unqualified_snapshots_never_produce_change(bad: str) -> None:
    from data_provider.ifind_fundamental_adapter import shareholder_concentration_change
    current = holders("2026-09-30")
    previous = holders("2026-06-30")
    if bad == "missing_rank": current["items"].pop()
    if bad == "duplicate_rank": current["items"][-1]["rank"] = 1
    if bad == "wrong_unit": current["items"][0]["ratio"]["unit"] = "multiple"
    if bad == "partial": current["coverage"] = "partial"
    if bad == "mixed_period": current["items"][0]["report_date"] = "2026-06-30"
    if bad == "ratio_period": current["items"][0]["ratio"]["period"] = "2026-06-30"
    if bad == "future": current = holders("2099-09-30")
    if bad == "reversed": previous = holders("2026-12-31")
    if bad == "sum_exceeds_100": current = holders("2026-09-30", 11.0)
    with pytest.raises(ValueError):
        shareholder_concentration_change(IfindShareholders.model_validate_json(json.dumps(current)), IfindShareholders.model_validate_json(json.dumps(previous)))


@pytest.mark.parametrize("prior_complete", [True, False])
def test_capital_reuses_current_holders_and_only_fetches_prior(
    monkeypatch: pytest.MonkeyPatch, prior_complete: bool,
) -> None:
    from src.agent.deep_research import explore_agents
    from src.agent.tools import data_tools
    from src.core.research_run import ResearchRun, research_dimension
    from src.deep_research_dims.context import SharedContext
    cfg = SimpleNamespace(data_source_profile="choice", enable_ifind_mcp=True, fundamental_fetch_timeout_seconds=20)
    monkeypatch.setattr(explore_agents, "get_config", lambda: cfg)
    calls = []
    previous = holders("2026-06-30", 6.211) if prior_complete else {"coverage": "partial", "errors": ["ifind:timeout"], "items": []}

    def fetch(code: str, section: str, **kwargs: Any) -> dict[str, Any]:
        calls.append((code, section, kwargs))
        return previous

    monkeypatch.setattr(data_tools, "_get_fetcher_manager", lambda: SimpleNamespace(get_ifind_research_context=fetch))
    run = ResearchRun(60)
    run.context = SharedContext("300260", "样本", "2026-10-29", fundamental={
        "research": {"shareholder_details": holders("2026-09-30", 6.126)},
        "institution": {"status": "not_supported", "data": {}, "field_meta": {}},
    })
    token = research_dimension.set("capital")
    try:
        with run.bind():
            material = explore_agents._institution_materials("300260")
    finally:
        research_dimension.reset(token)
    assert len(calls) == 1 and calls[0][:2] == ("300260", "holders")
    assert calls[0][2]["period"] == "2026-06-30"
    assert calls[0][2]["deadline"] <= run.deadline
    if prior_complete:
        assert material["data"]["top10_holder_change"] == -0.85
        assert material["data"].get("institution_holding_change") is None
        record = material["field_meta"]["top10_holder_change"]
        assert record["rule_eligible"] and record["unit"] == "percentage_point"
        assert record["caliber"] == "top10_shareholder_concentration"
        assert record["previous_period"] == "2026-06-30" and record["period"] == "2026-09-30"
        assert run.facts["capital"][0]["period"] == "2026-06-30 → 2026-09-30"
        assert run.facts["capital"][0]["label"] == "十大股东集中度变化（计算）"
        assert "top10_shareholder_concentration" in run.facts["capital"][0]["qualification"]
    else:
        assert material["status"] != "ok" and not material["field_meta"]
        assert "ifind:timeout" in material["errors"][-1]


def test_real_adapter_reuses_cache_and_force_refresh_within_existing_entry(
    ifind_transport: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pathlib import Path
    from data_provider.base import DataFetcherManager
    from data_provider.mx_data_adapter import financial_refresh
    from src.agent.deep_research import explore_agents
    from src.agent.tools import data_tools
    from src.core.research_run import ResearchRun
    from src.deep_research_dims.context import SharedContext

    _config, response, calls = ifind_transport
    response.clear()
    raw = (Path(__file__).parent / "fixtures/ifind_shareholders.json").read_text().replace("20251231", "20250630")
    response.update(json.loads(raw))
    monkeypatch.setattr(data_tools, "_fetcher_manager_singleton", DataFetcherManager())
    for marker, expected in ((None, 1), (None, 1), (("300260", "refresh-test"), 2)):
        run = ResearchRun(30)
        run.context = SharedContext("300260", "样本", "2026-10-29", fundamental={
            "research": {"shareholder_details": holders("2025-09-30", 6.126)},
            "institution": {"status": "not_supported", "data": {}, "field_meta": {}},
        })
        token = financial_refresh.set(marker)
        try:
            with run.bind():
                first = explore_agents._institution_materials("300260")
                again = explore_agents._institution_materials("300260")
        finally:
            financial_refresh.reset(token)
        assert len(calls) == expected
        assert "2025-06-30" in calls[-1][1]
        assert first == again
        assert first["data"]["top10_holder_change"] == 3.58
        assert first["field_meta"]["top10_holder_change"]["rule_eligible"]


@pytest.mark.parametrize("when", ["before_fetch", "after_fetch"])
def test_cancel_does_not_publish_late_holding_material(
    monkeypatch: pytest.MonkeyPatch, when: str,
) -> None:
    from src.agent.deep_research import explore_agents
    from src.agent.tools import data_tools
    from src.core.research_run import ResearchRun, ResearchStopped
    from src.deep_research_dims.context import SharedContext
    monkeypatch.setattr(explore_agents, "get_config", lambda: SimpleNamespace(
        data_source_profile="choice", enable_ifind_mcp=True, fundamental_fetch_timeout_seconds=20,
    ))
    run = ResearchRun(30)
    run.context = SharedContext("300260", "样本", "2026-10-29", fundamental={
        "research": {"shareholder_details": holders("2026-09-30")},
        "institution": {"status": "not_supported", "data": {}, "field_meta": {}},
    })
    calls = []

    def fetch(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        run.stop("cancelled")
        return holders("2026-06-30")

    monkeypatch.setattr(data_tools, "_get_fetcher_manager", lambda: SimpleNamespace(get_ifind_research_context=fetch))
    if when == "before_fetch":
        run.stop("cancelled")
    with run.bind(), pytest.raises(ResearchStopped):
        explore_agents._institution_materials("300260")
    assert len(calls) == (0 if when == "before_fetch" else 1)
    assert not run.facts
    assert run.context.fundamental["institution"]["status"] == "not_supported"


# The real manager/adapter fixture supplies only a transport replay, retaining
# period/identity/unit parsing, per-report single_fetch, caching and refresh.
from tests.test_ifind_choice_integration import ifind_transport  # noqa: E402,F401
from tests.test_choice_integration import choice_transport  # noqa: E402,F401
