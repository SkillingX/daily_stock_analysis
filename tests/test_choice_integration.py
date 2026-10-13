"""Choice transport fixtures exercise the real manager and financial quality path."""

from datetime import date
from types import SimpleNamespace

import pytest
import requests

from data_provider.base import DataFetcherManager
from src.config import Config
from src.deep_research_dims.context import SharedContext, _safe_fundamental
from src.deep_research_dims.fundamental_dim import financial_field_quality


class ChoiceSDK:
    def start(self, options, **kwargs):
        assert "ForceLogin=0" in options
        return SimpleNamespace(ErrorCode=0, ErrorMsg="success")

    def stop(self):
        return SimpleNamespace(ErrorCode=0, ErrorMsg="success")

    def css(self, code, indicators, options):
        values = {"NAME": "新莱应材", "LATESTREPORTNEW": "2026/6/30", "ACTUALISSUINGDATE": "2026/8/29",
                  "ROEWA": 0.0, "GPMARGIN": 23.419588, "YOYOR": 9.687095, "YOYPNI": -7.517907,
                  "PETTM": 121.655277, "PB": 9.298764, "MV": 20308776249.6}
        fields = indicators.split(",")
        return SimpleNamespace(ErrorCode=0, ErrorMsg="success", Codes=[code], Indicators=fields,
                               Dates=[date.today().isoformat()], Data={code: [values.get(k) for k in fields]})

    def tradedates(self, start, end, options):
        return SimpleNamespace(ErrorCode=0, ErrorMsg="success", Codes=[], Indicators=[], Dates=[], Data=[date.today().isoformat()])


@pytest.fixture
def choice_transport(monkeypatch, tmp_path):
    from src.deep_research_dims import dim_cache
    monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path / "cache"))
    config = Config(enable_fundamental_pipeline=True, deep_research_cross_validate=False)
    config.data_source_profile = "choice"
    monkeypatch.setattr(Config, "_instance", config)
    from data_provider.choice_adapter import ChoiceAdapter
    sdk = ChoiceSDK()
    def native_request(method, args, options, deadline):
        reply = getattr(sdk, method)(*args, options)
        return {"codes": reply.Codes, "indicators": reply.Indicators, "dates": reply.Dates, "data": reply.Data}
    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(native_request))
    # Empty public transport replies exercise auxiliary assembly without HTTP.
    from data_provider.fundamental_adapter import AkshareFundamentalAdapter
    monkeypatch.setattr(AkshareFundamentalAdapter, "_call_df_candidates", lambda *args: (None, "fixture_public", []))
    import akshare
    monkeypatch.setattr(akshare, "stock_institute_hold", lambda: None)
    import efinance
    import pandas as pd
    monkeypatch.setattr(efinance.stock, "get_realtime_quotes", lambda *args, **kwargs: pd.DataFrame([{"股票名称": "半导体", "涨跌幅": 1.0, "成交额": 1000.0}]))
    def forbidden_http(*args, **kwargs):
        pytest.fail("Complete Choice snapshots must not issue legacy/public fallback HTTP requests")
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden_http)
    return config


def test_choice_snapshot_preserves_values_and_semantics(choice_transport, monkeypatch):
    manager = DataFetcherManager()
    result = manager.get_fundamental_context("300260", budget_seconds=20)
    growth = result["growth"]
    assert growth["data"]["roe"] == 0.0
    assert growth["data"]["net_profit_yoy"] == -7.517907
    assert result["valuation"]["data"]["total_mv"] == 20308776249.6
    ctx = SharedContext("300260", "新莱应材", date.today().isoformat())
    from src.agent.tools import data_tools
    monkeypatch.setattr(data_tools, "_fetcher_manager_singleton", manager)
    fund = _safe_fundamental("300260", ctx)
    quality = financial_field_quality(fund, "roe")
    assert quality.rule_eligible
    assert quality.reading.source == "choice"
    assert quality.reading.source_family == "eastmoney"
    assert quality.reading.period == "2026-06-30"
    assert quality.quality.status != "verified"


def test_old_keys_cannot_enable_sources_after_choice_failure(choice_transport, monkeypatch):
    from data_provider.choice_adapter import ChoiceAdapter
    from data_provider.cross_source_validator import SourceReadError
    from src.agent.tools.cross_validation_helpers import build_cross_validation_block
    from src.deep_research_dims.context import fetch_fuyao_financial_series
    from data_provider.capital_flow_provider import get_main_inflow_cumulative
    for key in ("MX_APIKEY", "IFIND_MCP_TOKEN", "FUYAO_API_KEY", "MX_MCP_API_KEY"):
        monkeypatch.setenv(key, "test-credential")
    monkeypatch.setenv("IFIND_MCP_ENDPOINT", "https://invalid.example")
    choice_transport.enable_fuyao = True
    choice_transport.fuyao_api_key = "test-credential"
    choice_transport.enable_mx_mcp = True
    choice_transport.mx_mcp_api_key = "test-credential"
    choice_transport.deep_research_cross_validate = True
    def denied(*args, **kwargs):
        raise SourceReadError("choice", "authentication_failed")
    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(denied))
    result = DataFetcherManager().get_fundamental_context("300260")
    assert result["status"] == "partial"
    assert "choice:authentication_failed" in result["errors"]
    assert build_cross_validation_block("300260", ["roe"]) is None
    assert fetch_fuyao_financial_series("300260")["years"] == []
    assert get_main_inflow_cumulative("300260")["status"] == "not_supported"


def test_refresh_replaces_snapshot_value_and_metadata_together(choice_transport, monkeypatch):
    from data_provider.choice_adapter import ChoiceAdapter
    from data_provider.mx_data_adapter import financial_refresh
    manager = DataFetcherManager()
    first = manager.get_fundamental_context("300260")
    request = ChoiceAdapter._native_request
    def changed(method, args, options, deadline):
        raw = request(method, args, options, deadline)
        if "ROEWA" in raw["indicators"]:
            raw["data"][args[0]][raw["indicators"].index("ROEWA")] = 1.23
        return raw
    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(changed))
    assert manager.get_fundamental_context("300260")["growth"]["data"]["roe"] == 0
    token = financial_refresh.set(("300260", "refresh-test"))
    try:
        fresh = manager.get_fundamental_context("300260")
    finally:
        financial_refresh.reset(token)
    assert first["growth"]["field_meta"]["roe"]["value"] == 0
    assert fresh["growth"]["data"]["roe"] == 1.23
    assert fresh["growth"]["field_meta"]["roe"]["value"] == 1.23


def test_public_details_are_retained_without_overwriting_choice_financials(choice_transport, monkeypatch):
    import akshare
    import pandas as pd
    from data_provider.fundamental_adapter import AkshareFundamentalAdapter
    monkeypatch.setattr(akshare, "stock_institute_hold", lambda: pd.DataFrame([{"代码": "300260", "持股变化": 12.5}]))
    def transport(self, candidates):
        if candidates[0][0] == "stock_yjyg_em":
            return pd.DataFrame([{"代码": "300260", "业绩变动": "公开业绩预告"}]), "stock_yjyg_em", []
        return None, "fixture_public", []
    monkeypatch.setattr(AkshareFundamentalAdapter, "_call_df_candidates", transport)
    context = DataFetcherManager().get_fundamental_context("300260")
    assert context["growth"]["data"]["roe"] == 0.0
    assert context["institution"]["data"]["institution_holding_change"] == 12.5
    assert context["earnings"]["data"]["forecast_summary"] == "公开业绩预告"
    assert context["boards"]["data"]


def test_supply_rule_projection_does_not_mutate_original_snapshot(choice_transport, monkeypatch):
    from src.agent.tools import data_tools, supply_chain_tools
    original = {"fundamental_context": {"growth": {"data": {"roe": 8.0}, "field_meta": {"roe": {"value": 8.0, "rule_eligible": False}}}, "valuation": {"data": {}, "field_meta": {}}}}
    monkeypatch.setattr(data_tools, "_handle_get_stock_info", lambda *a: original)
    monkeypatch.setattr(data_tools, "_handle_get_realtime_quote", lambda *a: {})
    projection = supply_chain_tools._fetch_real_stock_info_uncached("300260")
    assert projection["fundamental_context"]["growth"]["data"]["roe"] is None
    assert original["fundamental_context"]["growth"]["data"]["roe"] == 8.0
