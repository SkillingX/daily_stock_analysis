"""Independent fixed-SHA source identity probes; no network or real database.
Run with owned isolation runner; synthetic supplier fields preserve real parsers.
Requires project pytest dependencies.
"""
import json
from datetime import datetime, timezone
import pandas as pd
import pytest
from typing import Any

from data_provider.mx_data_adapter import MXSource
from data_provider.cross_source_validator import adopted_field_record, AnchorReading, AnchorQuality
from data_provider.fundamental_adapter import _pick_financial_value
from data_provider.ifind_fundamental_adapter import _parse_ifind_response, _IFIND_ANCHOR_QUERIES
from data_provider.mx_mcp_adapter import _parse_mx_mcp_response, _MX_MCP_ANCHOR_QUERIES
from tests.test_fundamentals_data_quality import financial_sources


def test_runtime_manager_reset_rebinds_validator_source(
    financial_sources: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    import data_provider
    from data_provider.base import DataFetcherManager
    from data_provider.mx_data_adapter import MXClient
    from src.agent.tools import data_tools, cross_validation_helpers
    from src.config import Config

    monkeypatch.setattr("src.config.get_config", lambda: Config(deep_research_cross_validate=True))
    monkeypatch.setattr(cross_validation_helpers, "_validator_instance", None)
    first_manager = data_tools._get_fetcher_manager()
    first_manager._mx_source = MXSource(MXClient(api_key="fixture-first"))
    assert cross_validation_helpers._get_validator()._sources[0] is first_manager._mx_source
    new_manager = DataFetcherManager(fetchers=[financial_sources["fetcher"]])
    new_manager._mx_source = MXSource(MXClient(api_key="fixture-new"))
    monkeypatch.setattr(data_provider, "DataFetcherManager", lambda: new_manager)
    data_tools.reset_fetcher_manager()
    assert data_tools._get_fetcher_manager() is new_manager
    assert cross_validation_helpers._get_validator()._sources[0] is new_manager._mx_source


def test_fresh_quote_finalization_does_not_clear_current_conflict(financial_sources):
    from src.deep_research_dims.context import build_shared_context
    from src.deep_research_dims.fundamental_dim import build_fundamental_dim
    quote = financial_sources["quote"]
    quote.pe_ratio = 999
    quote.provider_timestamp = datetime.now(timezone.utc).isoformat()
    record = adopted_field_record("pe_ratio", AnchorReading("efinance", 999, caliber="TTM", unit="multiple", observed_at=quote.provider_timestamp), AnchorQuality(status="conflict", reason_codes=("numeric_conflict",)))
    quote.field_meta = {"pe_ratio": record}
    f1 = build_fundamental_dim(build_shared_context("600519", "样本"))
    assert f1.data_quality.fields["pe_ratio"].quality.status == "conflict"
    assert not f1.data_quality.fields["pe_ratio"].rule_eligible
    assert f1.valuation_detail["score"] is None


def test_stock_info_projection_does_not_clear_current_conflict(financial_sources):
    from src.agent.tools.data_tools import _handle_get_stock_info
    quote = financial_sources["quote"]
    quote.pe_ratio = 999
    quote.field_meta = {"pe_ratio": adopted_field_record("pe_ratio", AnchorReading("efinance", 999, caliber="TTM", unit="multiple"), AnchorQuality(status="conflict", reason_codes=("numeric_conflict",)))}
    response = _handle_get_stock_info("600519")
    record = response["fundamental_context"]["valuation"]["field_meta"]["pe_ratio"]
    assert record["quality"]["status"] == "conflict"
    assert not record["rule_eligible"]


def test_acquired_mx_bundle_is_not_fetched_again_by_validation(financial_sources, monkeypatch):
    from types import SimpleNamespace
    from src.agent.tools import data_tools, cross_validation_helpers
    from src.config import Config
    from src.deep_research_dims.context import build_shared_context
    from data_provider.mx_data_adapter import MXClient
    import requests

    financial_sources["frame"] = pd.DataFrame()
    cfg = Config(realtime_source_priority="efinance", fundamental_cache_ttl_seconds=0, deep_research_cross_validate=True)
    monkeypatch.setattr("src.config.get_config", lambda: cfg)
    # Exercise the helper's real source factory, rather than injecting a shared client.
    manager_source = MXSource(MXClient(api_key="fixture"))
    monkeypatch.setattr(data_tools._get_fetcher_manager(), "_mx_source", manager_source)
    monkeypatch.setattr(cross_validation_helpers, "_validator_instance", None)
    calls = []
    def post(url, **kwargs):
        query = kwargs["json"]["toolQuery"]
        calls.append(query)
        names = {"r0": "净资产收益率ROE(加权)(%)", "r1": "毛利率(%)", "r2": "营业收入同比增长率(%)", "r3": "归母净利润同比增长率(%)"}
        table = {"headName": ["2025年报"], "r0": [18], "r1": [40], "r2": [10], "r3": [10]}
        payload = {"status": 0, "data": {"data": {"searchDataResultDTO": {"dataTableDTOList": [{"table": table, "nameMap": names}]}}}}
        return SimpleNamespace(status_code=200, json=lambda: payload)
    monkeypatch.setattr(requests, "post", post)
    ctx = build_shared_context("600519", "样本")
    assert ctx.fundamental["roe"] == 18
    financial_queries = [query for query in calls if "最新价" not in query]
    assert len(financial_queries) == 1, financial_queries


def test_mx_ttm_identity_survives_adjacent_static_column():
    reading = MXSource().read_bundle({"市盈率(静态)": 10, "市盈率TTM": 30}, "pe_ratio")
    assert reading is not None
    assert (reading.value, reading.caliber) == (30, "TTM")
    assert adopted_field_record("pe_ratio", reading)["rule_eligible"]


def test_mx_weighted_identity_survives_adjacent_unweighted_column():
    reading = MXSource().read_bundle({"_mx_period": "2025年报", "净资产收益率(%)": 8, "净资产收益率ROE(加权)(%)": 18}, "roe")
    assert reading is not None
    assert (reading.value, reading.caliber) == (18, "weighted_roe")
    assert adopted_field_record("roe", reading)["rule_eligible"]


def test_native_weighted_identity_survives_adjacent_unweighted_column():
    metadata = {}
    value = _pick_financial_value(pd.Series({"净资产收益率(%)": 8, "净资产收益率(加权)(%)": 18}), ["净资产收益率", "ROE"], [], field="roe", source="akshare", period="2025年报", field_meta=metadata)
    assert value == 18
    assert metadata["roe"]["rule_eligible"]


def test_ifind_weighted_identity_survives_adjacent_unweighted_column():
    response = {"data": {"answer": "|报告期|净资产收益率ROE（单位：%）|净资产收益率ROE(加权,公布值)（单位：%）|\n|---|---|---|\n|2025年报|8|18|"}}
    reading = _parse_ifind_response(json.dumps(response), _IFIND_ANCHOR_QUERIES["roe"][2], "roe", "2025年报")
    assert reading is not None
    assert (reading.value, reading.caliber) == (18, "weighted_roe")


def test_choice_weighted_identity_survives_adjacent_unweighted_column():
    response = {"data": [{"columns": ["指标", "2025年报"], "items": [["净资产收益率ROE(%)", 8], ["净资产收益率ROE(加权)(%)", 18]]}]}
    reading = _parse_mx_mcp_response(json.dumps(response), _MX_MCP_ANCHOR_QUERIES["roe"][2], "roe", "2025年报")
    assert reading is not None
    assert (reading.value, reading.caliber) == (18, "weighted_roe")
