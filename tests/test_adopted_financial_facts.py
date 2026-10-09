"""Check provenance and validation follow the actual tool facts and quote merge.
Run: python -m pytest tests/test_adopted_financial_facts.py
Requires project dependencies; injected provider responses, no live requests.
"""
from dataclasses import asdict
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from data_provider.base import DataFetcherManager
from data_provider.cross_source_validator import AnchorReading, CrossSourceValidator
from data_provider.realtime_types import RealtimeSource, UnifiedRealtimeQuote
from src.agent.tools import data_tools, cross_validation_helpers


def test_real_stock_tool_verifies_adopted_999_instead_of_another_30_high(monkeypatch: pytest.MonkeyPatch) -> None:
    observed = datetime.now(timezone.utc).isoformat()
    main = AnchorReading("mx", 999, caliber="TTM", unit="multiple", observed_at=observed)
    raw = {"market": "cn", "status": "partial", "valuation": {"data": {"pe_ratio": 999, "pb_ratio": 2, "total_mv": 100, "circ_mv": 50}, "field_meta": {"pe_ratio": asdict(main)}}}
    manager = SimpleNamespace(get_fundamental_context=lambda code: raw, get_belong_boards=lambda code: [], get_stock_name=lambda code: "样本")
    readings = [AnchorReading(name, value, caliber="TTM", unit="multiple", observed_at=observed) for name, value in [("ifind", 30), ("mx_mcp", 30.1)]]
    validator = CrossSourceValidator([SimpleNamespace(name=r.source, read=lambda code, field, period, r=r: r if field == "pe_ratio" else None) for r in readings])
    monkeypatch.setattr(data_tools, "_fetcher_manager_singleton", manager)
    monkeypatch.setattr(cross_validation_helpers, "_get_validator", lambda: validator)
    response = data_tools._handle_get_stock_info("600519")
    assert response["pe_ratio"] == 999
    evidence = response["cross_validation"]["anchors"]["pe_ratio"]
    assert evidence["v"] == 999 and evidence["quality"]["status"] == "conflict" and not evidence["agreed"]
    record = response["fundamental_context"]["valuation"]["field_meta"]["pe_ratio"]
    assert record["value"] == 999 and record["source"] == "mx" and not record["rule_eligible"]


def test_quote_merge_keeps_each_supplemented_field_source_time_and_caliber() -> None:
    main = UnifiedRealtimeQuote("600519", source=RealtimeSource.AKSHARE_SINA, price=20, pe_ratio=0)
    secondary = UnifiedRealtimeQuote("600519", source=RealtimeSource.EFINANCE, price=21, pe_ratio=30, pb_ratio=2)
    secondary.field_meta = {"pb_ratio": asdict(AnchorReading("ifind", 2, caliber="MRQ", unit="multiple", observed_at=datetime.now(timezone.utc).isoformat()))}
    main.field_meta = {"pe_ratio": asdict(AnchorReading("akshare_sina", 0, caliber=None, unit="multiple"))}
    DataFetcherManager._merge_quote_fields(main, secondary)
    assert main.pe_ratio == 0 and main.pb_ratio == 2
    assert main.field_meta["pb_ratio"]["source"] == "ifind"
    assert main.field_meta["pb_ratio"]["caliber"] == "MRQ"
    assert main.field_meta["pe_ratio"]["source"] == "akshare_sina"


def test_conflicting_growth_is_evidence_and_cannot_be_backfilled() -> None:
    anchor = {"v": 99, "conf": "low", "quality": {"status": "conflict"}, "readings": [asdict(AnchorReading("mx", 99, caliber="weighted_roe", unit="percentage_point", period="2025年报"))]}
    result = data_tools._backfill_growth_from_validation({"status": "failed", "data": {}}, {"anchors": {"roe": anchor}})
    assert result["data"].get("roe") is None


def test_valid_single_source_growth_backfill_keeps_actual_period_and_unit() -> None:
    reading = AnchorReading("mx", 18, caliber="weighted_roe", unit="percentage_point", period="2025年报")
    anchor = CrossSourceValidator([]).verify("600519", "roe", primary_reading=reading).to_compact()
    result = data_tools._backfill_growth_from_validation({"status": "failed", "data": {}}, {"anchors": {"roe": anchor}})
    assert result["data"]["roe"] == 18
    assert result["field_meta"]["roe"]["period"] == "2025-12-31"
    assert result["field_meta"]["roe"]["unit"] == "percentage_point"


def test_unknown_family_numeric_outlier_blocks_tool_growth_backfill() -> None:
    readings = [AnchorReading(name, value, caliber="weighted_roe", unit="percentage_point", period="2025年报") for name, value in [("mx", 30), ("ifind", 30.1), ("unknown", 300)]]
    validator = CrossSourceValidator([SimpleNamespace(name=r.source, read=lambda *args, r=r: r) for r in readings])
    anchor = validator.verify("600519", "roe").to_compact()
    result = data_tools._backfill_growth_from_validation({"data": {}}, {"anchors": {"roe": anchor}})
    assert anchor["quality"]["status"] == "conflict" and result["data"].get("roe") is None


def test_missing_anchor_selects_first_intrinsically_valid_candidate_without_losing_evidence() -> None:
    readings = [AnchorReading("mx", 99), AnchorReading("ifind", 18, caliber="weighted_roe", unit="percentage_point", period="2025年报")]
    validator = CrossSourceValidator([SimpleNamespace(name=r.source, read=lambda *args, r=r: r) for r in readings])
    result = validator.verify("600519", "roe").to_compact()
    assert result["v"] == 18 and len(result["readings"]) == 2
    assert result["readings"][1]["value"] == 99


def test_compact_does_not_change_the_adopted_financial_value() -> None:
    reading = AnchorReading("mx", 18.123456789, caliber="weighted_roe", unit="percentage_point", period="2025年报")
    result = CrossSourceValidator([]).verify("600519", "roe", primary_reading=reading).to_compact()
    record = cross_validation_helpers.field_record_from_validation("roe", result)
    assert result["v"] == reading.value and record is not None and record["value"] == reading.value


@pytest.mark.parametrize("field,caliber", [("pe_ratio", "dynamic_pe"), ("pb_ratio", None), ("roe", "ending_equity_roe")])
def test_unknown_or_different_methods_are_evidence_not_rule_inputs(field: str, caliber: str | None) -> None:
    from data_provider.cross_source_validator import adopted_field_record
    reading = AnchorReading("fuyao", 18, caliber=caliber, unit="percentage_point" if field == "roe" else "multiple")
    record = adopted_field_record(field, reading)
    assert record["value"] == 18 and not record["rule_eligible"]


def test_legacy_capital_series_cannot_bypass_unit_currency_contract() -> None:
    result = data_tools._backfill_capital_flow({"main_net_inflow": None}, None, {"main_net_inflow": 100, "source": "ifind"})
    assert result["main_net_inflow"] is None


def test_manager_financial_block_preserves_distinct_field_actual_periods() -> None:
    first = AnchorReading("mx", 10, caliber="operating_revenue_yoy", unit="percentage_point", period="2026中报")
    second = AnchorReading("ifind", 18, caliber="weighted_roe", unit="percentage_point", period="2025年报")
    block = DataFetcherManager._build_fundamental_block("partial", {"revenue_yoy": 10, "roe": 18, "field_meta": {"revenue_yoy": asdict(first), "roe": asdict(second)}})
    assert block["data"] == {"revenue_yoy": 10, "roe": 18}
    assert block["field_meta"]["roe"]["period"] == "2025-12-31"
    assert block["field_meta"]["revenue_yoy"]["period"] == "2026-06-30"


def test_dynamic_candidate_does_not_block_valid_later_ttm_candidate() -> None:
    readings = [AnchorReading("mx", 99, caliber="dynamic_pe", unit="multiple"), AnchorReading("ifind", 30, caliber="TTM", unit="multiple")]
    validator = CrossSourceValidator([SimpleNamespace(name=r.source, read=lambda *args, r=r: r) for r in readings])
    result = validator.verify("600519", "pe_ratio").to_compact()
    assert result["v"] == 30 and len(result["readings"]) == 2


def test_quote_supplement_and_tool_fallback_reject_unknown_valuation_methods() -> None:
    main = UnifiedRealtimeQuote("600519", price=20)
    secondary = UnifiedRealtimeQuote("600519", pe_ratio=99, pb_ratio=2)
    DataFetcherManager._merge_quote_fields(main, secondary)
    assert main.pe_ratio is None and main.pb_ratio is None
    result = data_tools._fallback_valuation_from_quote(SimpleNamespace(get_realtime_quote=lambda code: secondary), "600519", {})
    assert result.get("pe_ratio") is None and result.get("pb_ratio") is None


@pytest.mark.parametrize("raw,expected", [(2.0, 200_000_000.0), (None, None)])
def test_native_capital_adapter_and_manager_share_one_value_and_do_not_count_metadata(monkeypatch: pytest.MonkeyPatch, raw: float | None, expected: float | None) -> None:
    import pandas as pd
    from data_provider.fundamental_adapter import AkshareFundamentalAdapter
    from src.config import Config
    adapter = AkshareFundamentalAdapter()
    responses = iter([(pd.DataFrame({"主力净流入(亿元)": [raw]}), "stock_individual_fund_flow", []), (None, None, [])])
    monkeypatch.setattr(adapter, "_call_df_candidates", lambda *args: next(responses, (None, None, [])))
    mgr = DataFetcherManager(fetchers=[])
    mgr._fundamental_adapter = adapter
    monkeypatch.setattr("src.config.get_config", lambda: Config(fundamental_cache_ttl_seconds=0))
    result = mgr.get_capital_flow_context("600519")
    assert result["data"]["stock_flow"]["main_net_inflow"] == expected
    if expected is None:
        assert result["status"] != "ok"
    else:
        metadata = result["data"]["stock_flow"]["field_meta"]["main_inflow"]
        assert metadata["value"] == expected and metadata["source"] == "akshare:stock_individual_fund_flow"


def test_projection_does_not_turn_existing_conflict_into_eligible_supplement() -> None:
    from data_provider.cross_source_validator import AnchorQuality, adopted_field_record
    secondary = UnifiedRealtimeQuote("600519", pe_ratio=30, field_meta={"pe_ratio": adopted_field_record("pe_ratio", AnchorReading("mx", 30, caliber="TTM", unit="multiple"), AnchorQuality(status="conflict"))})
    primary = UnifiedRealtimeQuote("600519", price=20)
    DataFetcherManager._merge_quote_fields(primary, secondary)
    assert primary.pe_ratio is None


@pytest.mark.parametrize("period,basis", [("2025年报", "annual"), ("2026中报", "YTD")])
def test_native_selected_financial_period_keeps_response_basis(monkeypatch: pytest.MonkeyPatch, period: str, basis: str) -> None:
    import pandas as pd
    from data_provider.fundamental_adapter import AkshareFundamentalAdapter
    adapter = AkshareFundamentalAdapter()
    responses = iter([(pd.DataFrame({"指标": ["营业收入同比增长率(%)"], period: [10]}), "stock_financial_abstract", []), (None, None, []), (None, None, []), (None, None, [])])
    monkeypatch.setattr(adapter, "_call_df_candidates", lambda *args: next(responses, (None, None, [])))
    import akshare
    monkeypatch.setattr(akshare, "stock_institute_hold", lambda: pd.DataFrame())
    result = adapter.get_fundamental_bundle("600519")
    assert result["growth"]["field_meta"]["revenue_yoy"]["period_basis"] == basis


def test_money_supplement_respects_proven_subject_currency() -> None:
    main = UnifiedRealtimeQuote("600519", price=20, currency="CNY")
    secondary = UnifiedRealtimeQuote("600519", total_mv=100, currency="USD")
    DataFetcherManager._merge_quote_fields(main, secondary)
    assert main.total_mv is None
    metadata = {"current_price": asdict(AnchorReading("mx", 20, unit="currency_base", currency="CNY"))}
    result = data_tools._fallback_valuation_from_quote(SimpleNamespace(get_realtime_quote=lambda code: secondary), "600519", {}, metadata)
    assert result.get("total_mv") is None


def test_primary_field_currency_also_binds_subject_without_global_currency() -> None:
    main = UnifiedRealtimeQuote("600519", price=20, field_meta={"current_price": asdict(AnchorReading("mx", 20, unit="currency_base", currency="CNY"))})
    secondary = UnifiedRealtimeQuote("600519", total_mv=100, currency="USD")
    DataFetcherManager._merge_quote_fields(main, secondary)
    assert main.total_mv is None


def test_current_validator_stale_reason_survives_consumption_projection() -> None:
    reading = AnchorReading("mx", 10, unit="currency_base", currency="CNY", observed_at="2020-01-01T10:00:00+08:00")
    result = CrossSourceValidator([]).verify("600519", "current_price", primary_reading=reading).to_compact()
    record = cross_validation_helpers.field_record_from_validation("current_price", result)
    assert record is not None and record["is_stale"] and not record["rule_eligible"]


def test_realtime_missing_price_rejects_opposite_currency_before_selecting_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    observed = datetime.now(timezone.utc).isoformat()
    readings = [AnchorReading("mx", 10, unit="currency_base", currency="USD", observed_at=observed), AnchorReading("ifind", 20, unit="currency_base", currency="CNY", observed_at=observed)]
    validator = CrossSourceValidator([SimpleNamespace(name=r.source, read=lambda *args, r=r: r) for r in readings])
    quote = UnifiedRealtimeQuote("600519", currency="CNY")
    monkeypatch.setattr(data_tools, "_fetcher_manager_singleton", SimpleNamespace(get_realtime_quote=lambda code: quote))
    monkeypatch.setattr(cross_validation_helpers, "_get_validator", lambda: validator)
    response = data_tools._handle_get_realtime_quote("600519")
    assert response["price"] == 20 and response["cross_validation"]["anchors"]["current_price"]["v"] == 20


def test_stale_supplement_is_rejected_even_before_cv_attaches_a_flag() -> None:
    main = UnifiedRealtimeQuote("600519", price=20, currency="CNY")
    secondary = UnifiedRealtimeQuote("600519", total_mv=100, currency="CNY", provider_timestamp="2020-01-01T10:00:00+08:00")
    DataFetcherManager._merge_quote_fields(main, secondary)
    assert main.total_mv is None


def test_stale_first_candidate_does_not_block_later_fresh_fact() -> None:
    stale = AnchorReading("mx", 10, unit="currency_base", currency="CNY", observed_at="2020-01-01T10:00:00+08:00")
    fresh = AnchorReading("ifind", 20, unit="currency_base", currency="CNY", observed_at=datetime.now(timezone.utc).isoformat())
    validator = CrossSourceValidator([SimpleNamespace(name=r.source, read=lambda *args, r=r: r) for r in [stale, fresh]])
    result = validator.verify("600519", "current_price").to_compact()
    assert result["v"] == 20 and len(result["readings"]) == 2
    record = cross_validation_helpers.field_record_from_validation("current_price", result)
    assert record is not None and record["rule_eligible"]
