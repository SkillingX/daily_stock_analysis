"""Verify every acquired reading participates without replacing the adopted value.
Run: python -m pytest tests/test_all_source_validation.py
Requires project dependencies; injected Source responses only, no external service.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from itertools import permutations
from threading import Event
from time import monotonic
import json

import pytest

from data_provider.cross_source_validator import AnchorReading, CrossSourceValidator
from src.agent.tools.cross_validation_helpers import build_cross_validation_block


def pe(source: str, value: float, **metadata: object) -> AnchorReading:
    return AnchorReading(source, value, caliber="TTM", unit="multiple", observed_at=datetime.now(timezone.utc).isoformat(), **metadata)


def verify(readings: list[AnchorReading], field: str = "pe_ratio"):
    sources = [SimpleNamespace(name=reading.source, read=lambda *args, r=reading: r) for reading in readings[1:]]
    validator = CrossSourceValidator(sources)
    block = build_cross_validation_block("600519", [field], primary_readings={field: readings[0]}, validator=validator)
    assert block is not None
    return block["anchors"][field]


@pytest.mark.parametrize("values", [[30, 30.1, 300], [50, 30, 30.1], [999, 30, 30.1], [33, 30, 36], [30, 30.1, 30.2, 300]])
def test_every_comparable_pair_can_disprove_agreement(values: list[float]) -> None:
    names = ["mx", "ifind", "mx_mcp", "fuyao"]
    result = verify([pe(name, value) for name, value in zip(names, values)])
    assert result["v"] == values[0]
    assert result["quality"]["status"] == "conflict" and result["conf"] == "low" and not result["agreed"]
    assert len(result["readings"]) == len(values)


def test_exact_financial_tolerance_boundary_does_not_become_float_conflict() -> None:
    result = verify([pe("mx", 36.0), pe("ifind", 32.4)])
    assert result["quality"]["status"] == "verified" and result["diff"] == 10.0


@pytest.mark.parametrize("names,values,status", [
    (["mx", "mx_mcp"], [30, 30.1], "single_source"),
    (["mx", "mx_mcp"], [30, 300], "conflict"),
    (["mx", "unknown"], [30, 30.1], "not_comparable"),
    (["mx", "unknown"], [30, 300], "conflict"),
    (["mx", "ifind", "unknown"], [30, 30.1, 300], "conflict"),
    (["mx", "ifind", "unknown"], [30, 30.1, 30.2], "verified"),
    (["unknown", "mx", "ifind"], [30, 30.1, 30.2], "not_comparable"),
    (["unknown", "mx", "ifind"], [300, 30, 30.1], "conflict"),
])
def test_family_limits_independent_support_but_never_hides_numeric_conflict(names: list[str], values: list[float], status: str) -> None:
    result = verify([pe(name, value) for name, value in zip(names, values)])
    assert result["v"] == values[0] and result["quality"]["status"] == status
    assert result["agreed"] == (status == "verified")
    assert [item["source"] for item in result["readings"]] == names


def test_different_financial_period_is_disclosed_without_undoing_independent_actual_period_agreement() -> None:
    readings = [AnchorReading(name, value, caliber="operating_revenue", period=period, unit="currency_base", currency="CNY") for name, value, period in [("mx", 100, "2025年报"), ("ifind", 100, "2025年报"), ("fuyao", 50, "2026中报")]]
    result = verify(readings, "revenue")
    assert result["quality"]["status"] == "verified" and result["agreed"]
    assert result["readings"][2]["relation"] == "not_comparable"
    assert "period_mismatch" in result["readings"][2]["reason_codes"]


@pytest.mark.parametrize("values,status", [([1e8, 1e7], "verified"), ([1e8, 1e4], "not_comparable"), ([1e8, -1e7, 1e8], "conflict"), ([0, 1e8, -1e8], "conflict"), ([0, 0], "not_comparable"), ([0], "not_comparable")])
def test_direction_and_magnitude_evidence_uses_all_pairs_without_majority(values: list[float], status: str) -> None:
    names = ["mx", "ifind", "fuyao"]
    readings = [AnchorReading(name, value, unit="currency_base", currency="CNY", observed_at=datetime.now(timezone.utc).isoformat()) for name, value in zip(names, values)]
    result = verify(readings, "main_inflow")
    assert result["quality"]["status"] == status and result["agreed"] == (status == "verified")


def test_data_time_unknown_and_stale_cannot_be_verified_by_fetch_time() -> None:
    now = datetime.now(timezone.utc)
    for observed in (None, (now - timedelta(days=1)).isoformat()):
        readings = [pe("mx", 30), AnchorReading("ifind", 30, caliber="TTM", unit="multiple", observed_at=observed, fetched_at=now.isoformat())]
        result = verify(readings)
        assert result["quality"]["status"] == "not_comparable" and not result["agreed"]


def test_far_magnitude_source_has_explicit_reading_limit() -> None:
    observed = datetime.now(timezone.utc).isoformat()
    readings = [AnchorReading(name, value, unit="currency_base", currency="CNY", observed_at=observed) for name, value in [("mx", 1e8), ("ifind", 1e7), ("fuyao", 1e4)]]
    result = verify(readings, "main_inflow")
    assert result["quality"]["status"] == "not_comparable"
    assert result["readings"][2]["relation"] == "not_comparable"
    assert "magnitude_mismatch" in result["readings"][2]["reason_codes"]


def test_disabled_validation_performs_no_source_calls_and_keeps_main_value() -> None:
    def forbidden(*args):
        pytest.fail("disabled validation must not collect a source")
    result = CrossSourceValidator([SimpleNamespace(name="ifind", read=forbidden)]).verify("600519", "pe_ratio", primary_reading=pe("mx", 0), enabled=False)
    assert result.to_compact()["quality"]["status"] == "unverified"
    assert result.value == 0 and not result.agreed


def test_secondary_order_never_changes_conflict_or_adopted_value() -> None:
    for secondary in permutations([pe("ifind", 30.1), pe("mx_mcp", 30.2), pe("fuyao", 300)]):
        result = verify([pe("mx", 30), *secondary])
        assert result["v"] == 30 and result["quality"]["status"] == "conflict"


@pytest.mark.parametrize("name,expected", [("mx", "single_source"), ("unknown", "not_comparable")])
def test_deadline_returns_completed_evidence_without_waiting_for_slow_source(name: str, expected: str) -> None:
    release = Event()
    source = SimpleNamespace(name="ifind", read=lambda *args: (release.wait(1), pe("ifind", 30))[1])
    validator = CrossSourceValidator([source])
    start = monotonic()
    try:
        result = validator.verify("600519", "pe_ratio", primary_reading=pe(name, 30), deadline=start + 0.03)
        assert monotonic() - start < 0.2
        assert result.quality.status == expected and "timeout" in result.quality.reason_codes
        assert result.to_compact()["source_errors"] == [{"source": "ifind", "reason_code": "timeout"}]
    finally:
        release.set()


def test_zero_remaining_budget_does_not_submit_sources() -> None:
    def forbidden(*args):
        pytest.fail("expired budget must not call a source")
    result = CrossSourceValidator([SimpleNamespace(name="ifind", read=forbidden)]).verify("600519", "pe_ratio", primary_reading=pe("mx", 30), deadline=monotonic() - 1)
    assert result.quality.status == "single_source" and "timeout" in result.quality.reason_codes


def test_tool_block_keeps_one_deadline_across_anchors() -> None:
    def forbidden(*args):
        pytest.fail("expired shared budget must not call a source for any anchor")
    validator = CrossSourceValidator([SimpleNamespace(name="ifind", read=forbidden)])
    block = build_cross_validation_block("600519", ["pe_ratio", "pb_ratio"], primary_readings={"pe_ratio": pe("mx", 30)}, validator=validator, deadline=monotonic() - 1)
    assert block is not None
    assert block["anchors"]["pe_ratio"]["quality"]["status"] == "single_source"
    assert block["anchors"]["pb_ratio"]["quality"]["status"] == "missing"


def test_failure_and_missing_sources_are_disclosed_with_a_valid_main_reading() -> None:
    def fail(*args):
        raise RuntimeError("fixture failure")
    sources = [SimpleNamespace(name="ifind", read=fail), SimpleNamespace(name="mx_mcp", read=lambda *args: None)]
    result = CrossSourceValidator(sources).verify("600519", "pe_ratio", primary_reading=pe("mx", 30))
    assert result.quality.status == "single_source"
    assert result.to_compact()["source_errors"] == [{"source": "ifind", "reason_code": "source_error"}, {"source": "mx_mcp", "reason_code": "missing"}]
    missing = CrossSourceValidator(sources).verify("600519", "pe_ratio")
    assert missing.quality.status == "missing" and missing.value is None and not missing.agreed


def test_single_known_family_with_unknown_financial_period_keeps_local_input_limit() -> None:
    main = AnchorReading("mx", 18, caliber="weighted_roe", unit="percentage_point")
    result = CrossSourceValidator([]).verify("600519", "roe", primary_reading=main)
    assert result.quality.status == "single_source" and not result.agreed
    assert "period_unknown" in result.quality.reason_codes


def test_real_four_connected_source_parsers_retain_fourth_source_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    import requests
    from data_provider.mx_data_adapter import MXClient, MXSource
    from data_provider.ifind_fundamental_adapter import IfindSource, _parse_ifind_response
    from data_provider.mx_mcp_adapter import MxMcpSource, _parse_mx_mcp_response
    from data_provider.fuyao_adapter import FuyaoSource, _parse_fuyao_response

    observed = datetime.now(timezone.utc).isoformat()
    response = {"status": 0, "data": {"data": {"searchDataResultDTO": {"dataTableDTOList": [{"table": {"headName": [observed], "pe": ["30"]}, "nameMap": {"pe": "市盈率TTM"}}]}}}}
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: SimpleNamespace(status_code=200, json=lambda: response))
    ifind_raw = json.dumps({"data": {"answer": f"|市盈率TTM|observed_at|\n|---|---|\n|30.1|{observed}|"}})
    choice_raw = json.dumps({"data": [{"columns": ["指标", observed], "items": [["市盈率TTM", 30.2]]}]})
    fuyao_raw = json.dumps({"code": 0, "data": {"items": [{"thscode": "600519.SH", "fields": {"pe_ttm": 300, "observed_at": observed}}]}})
    sources = [MXSource(MXClient(api_key="offline-fixture")),
               IfindSource(SimpleNamespace(available=True, fetch=lambda code, field, period: _parse_ifind_response(ifind_raw, ["市盈率TTM"], field, period))),
               MxMcpSource(SimpleNamespace(available=True, fetch=lambda code, field, period: _parse_mx_mcp_response(choice_raw, ["市盈率TTM"], field, period))),
               FuyaoSource(SimpleNamespace(available=True, fetch=lambda code, field, period: _parse_fuyao_response(fuyao_raw, field, period)))]
    result = CrossSourceValidator(sources).verify("600519", "pe_ratio").to_compact()
    assert result["quality"]["status"] == "conflict" and result["v"] == 30 and not result["agreed"]
    assert [reading["source"] for reading in result["readings"]] == ["mx", "ifind", "mx_mcp", "fuyao"]


def test_warm_financial_dimension_cache_does_not_survive_validation_mode_change(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from src.config import get_config
    from src.deep_research_dims import dim_cache

    monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(get_config(), "deep_research_cross_validate", True)
    from src.deep_research_dims.fundamental_dim import build_fundamental_dim
    from tests.test_f1_financial_quality import context
    dim_cache.save_cached_dim("600519", "fundamental", build_fundamental_dim(context({"pe_ratio": 30, "pb_ratio": 2, "roe": 18, "gross_margin": 40, "revenue_yoy": 10, "net_profit_yoy": 10})).model_dump(mode="json"))
    dim_cache.save_cached_dim("600519", "business", {"name": "business fixture"})
    assert dim_cache.load_cached_dim("600519", "fundamental") is None  # 启用核验时重算当前证据
    monkeypatch.setattr(get_config(), "deep_research_cross_validate", False)
    assert dim_cache.load_cached_dim("600519", "fundamental") is None
    assert dim_cache.load_cached_dim("600519", "business") is not None
