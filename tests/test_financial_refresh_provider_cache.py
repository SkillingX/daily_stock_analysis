"""Refresh native valuation responses through existing caches and report persistence.
Run: python -m pytest tests/test_financial_refresh_provider_cache.py
Requires project dependencies; provider leaf responses and report files are isolated.
"""
from importlib import import_module
from typing import Any
from copy import deepcopy

import pandas as pd
import pytest

from tests.test_fundamentals_data_quality import financial_sources
from tests.test_fundamentals_refresh import refresh_inputs
from data_provider.base import DataFetcherManager
from data_provider.mx_data_adapter import financial_refresh
from data_provider.realtime_types import CircuitBreaker
from src.agent.tools import data_tools
from src.services import fundamentals_service as service
from src.storage import get_db


@pytest.fixture(params=("efinance", "akshare"))
def provider_cache(refresh_inputs: dict[str, Any], request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    name = request.param
    module = import_module(f"data_provider.{name}_fetcher")
    state: dict[str, Any] = {"value": 30.0, "calls": 0, "module": module}
    def response(*args: Any, **kwargs: Any) -> pd.DataFrame:
        state["calls"] += 1
        return pd.DataFrame([
            {"股票代码": code, "代码": code, "股票名称": "样本", "名称": "样本",
             "最新价": state["value"], "市盈率": state["value"], "市盈率-动态": state["value"],
             "总市值": 1000000.0, "流通市值": 900000.0}
            for code in ("600519", "000001", "512880", "159001")
        ])
    leaf = import_module(name)
    if name == "efinance":
        monkeypatch.setattr(leaf.stock, "get_realtime_quotes", response)
        fetcher = module.EfinanceFetcher()
    else:
        monkeypatch.setattr(leaf, "stock_zh_a_spot_em", response)
        monkeypatch.setattr(leaf, "fund_etf_spot_em", response)
        fetcher = module.AkshareFetcher()
    monkeypatch.setattr(fetcher, "_set_random_user_agent", lambda: None)
    monkeypatch.setattr(fetcher, "_enforce_rate_limit", lambda: None)
    monkeypatch.setattr(fetcher, "get_sector_rankings", lambda *args: ([], []))
    breaker = CircuitBreaker()
    monkeypatch.setattr(module, "get_realtime_circuit_breaker", lambda: breaker)
    for cache in ("_realtime_cache", "_etf_realtime_cache"):
        monkeypatch.setattr(module, cache, {"data": None, "timestamp": 0, "ttl": 3600})
    cfg = refresh_inputs["config"]
    cfg.realtime_source_priority = "efinance" if name == "efinance" else "akshare_em"
    manager = DataFetcherManager(fetchers=[fetcher])
    manager._mx_source = refresh_inputs["manager"]._mx_source
    monkeypatch.setattr(data_tools, "_fetcher_manager_singleton", manager)
    monkeypatch.setattr(manager, "get_chip_distribution", lambda code: None)
    monkeypatch.setattr(service, "_research_dim", lambda *args: {"status": "degraded"})
    state["fetcher"] = fetcher
    return state


@pytest.mark.parametrize("code,other,cache_name", [("600519", "000001", "_realtime_cache"), ("512880", "159001", "_etf_realtime_cache")])
def test_target_refresh_preserves_other_and_ordinary_native_cache(provider_cache: dict[str, Any], code: str, other: str, cache_name: str) -> None:
    state = provider_cache
    fetcher = state["fetcher"]
    assert fetcher.get_realtime_quote(code).price == 30.0
    cached = getattr(state["module"], cache_name)
    frame, timestamp = deepcopy(cached["data"]), cached["timestamp"]
    state["value"] = 18.0
    token = financial_refresh.set((code, "explicit-test-refresh"))
    try:
        assert fetcher.get_realtime_quote(other).price == 30.0
        assert state["calls"] == 1
        assert fetcher.get_realtime_quote(code).price == 18.0
    finally:
        financial_refresh.reset(token)
    assert fetcher.get_realtime_quote(code).price == 30.0
    assert fetcher.get_realtime_quote(other).price == 30.0
    assert state["calls"] == 2 and cached["timestamp"] == timestamp
    pd.testing.assert_frame_equal(cached["data"], frame)


def test_specialist_refresh_saves_recovered_native_pe(provider_cache: dict[str, Any]) -> None:
    state = provider_cache
    first = service.generate_fundamentals_report("600519", "样本")
    assert first["status"] == "success"
    assert first["dims"]["financial"]["valuation_detail"]["pe_ttm"] == 30.0
    before = state["calls"]
    cached = deepcopy(state["module"]._realtime_cache["data"])
    state["value"] = 18.0
    reused = service.generate_fundamentals_report("600519", "样本")
    assert reused["status"] == "already_exists" and reused["report_id"] == first["report_id"]
    assert state["calls"] == before
    second = service.generate_fundamentals_report("600519", "样本", force_refresh=True)
    assert second["status"] == "success" and second["report_id"] != first["report_id"]
    assert second["dims"]["financial"]["valuation_detail"]["pe_ttm"] == 18.0
    assert get_db().get_fundamentals_report(first["report_id"]) is not None
    assert state["calls"] > before
    pd.testing.assert_frame_equal(state["module"]._realtime_cache["data"], cached)
