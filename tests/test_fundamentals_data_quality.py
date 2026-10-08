"""Regression coverage for source data reaching the shared snapshot and F1 report.
Run: python -m pytest tests/test_fundamentals_data_quality.py
Requires project dependencies; provider responses and disk caches are isolated.
"""

from pathlib import Path
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pandas as pd
import pytest
from jinja2 import Environment, FileSystemLoader

from data_provider.base import DataFetcherManager
from data_provider.realtime_types import RealtimeSource, UnifiedRealtimeQuote
from src.agent.tools import data_tools
from src.config import Config
from src.deep_research_dims import context, dim_cache
from src.deep_research_dims.fundamental_dim import build_fundamental_dim


@pytest.fixture
def financial_sources(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    """Supply provider responses while keeping manager, snapshot, F1 and template real."""
    import akshare
    import requests

    def forbidden_request(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Regression tests must not contact a live provider")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden_request)

    values: dict[str, Any] = {"ratio": 0.0, "market_cap": 100_000_000_000.0}
    quote = UnifiedRealtimeQuote(
        "600519", name="样本", source=RealtimeSource.EFINANCE, price=20.0,
        pe_ratio=0.0, pb_ratio=0.0, total_mv=values["market_cap"],
    )
    fetcher = SimpleNamespace(name="EfinanceFetcher", priority=0, get_realtime_quote=lambda code: quote)
    values["fetcher"] = fetcher
    values["quote"] = quote
    manager = DataFetcherManager(fetchers=[fetcher])
    cfg = Config(realtime_source_priority="efinance", fundamental_cache_ttl_seconds=0)
    monkeypatch.setattr("src.config.get_config", lambda: cfg)
    monkeypatch.setattr(data_tools, "_fetcher_manager_singleton", manager)
    monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("FUYAO_API_KEY", raising=False)
    monkeypatch.setattr(
        akshare, "stock_financial_abstract",
        lambda **kwargs: pd.DataFrame({
            "指标": ["净资产收益率", "毛利率", "营业总收入增长率", "归属母公司净利润增长率"],
            "20251231": [values["ratio"]] * 4,
        }),
    )
    for endpoint in ("stock_yjyg_em", "stock_yjkb_em", "stock_fhps_detail_em",
                     "stock_institute_hold", "stock_zh_a_gdhs_detail_em", "stock_individual_fund_flow",
                     "stock_fund_flow_individual", "stock_lhb_stock_statistic_em"):
        monkeypatch.setattr(akshare, endpoint, lambda **kwargs: pd.DataFrame(), raising=False)
    # Unrelated history/chip/persistence reads are outside this financial seam.
    monkeypatch.setattr("src.services.history_loader.load_history_df", lambda *args, **kwargs: (pd.DataFrame(), "fixture"))
    monkeypatch.setattr(manager, "get_chip_distribution", lambda code: None)
    monkeypatch.setattr(context, "_safe_history_reports", lambda code, ctx: [])
    return values


@pytest.mark.parametrize("ratio", [0.0, -5.0, 18.0, None])
def test_financial_values_reach_snapshot_f1_and_report(
    financial_sources: dict[str, Any], ratio: float | None,
) -> None:
    financial_sources["ratio"] = ratio
    ctx = context.build_shared_context("600519", "样本")
    assert ctx.fundamental["revenue_growth"] == ratio
    assert ctx.fundamental["net_profit_yoy"] == ratio
    assert ctx.fundamental["roe"] == ratio
    assert ctx.fundamental["gross_margin"] == ratio
    assert ctx.fundamental["pe_ttm"] == 0.0
    dim = build_fundamental_dim(ctx).model_dump()
    assert dim["profitability"]["roe"]["value"] == ratio
    assert dim["growth_quality"]["revenue_yoy"]["value"] == ratio
    env = Environment(loader=FileSystemLoader(Path(__file__).parents[1] / "templates"))
    md = env.get_template("fundamentals_report.j2").render(
        stock_name="样本", stock_code="600519", financial=dim,
        business={}, sector={}, degraded_note=lambda dim: "",
    )
    expected = "缺失" if ratio is None else str(ratio)
    for label in ("ROE", "毛利率", "营收同比", "净利同比"):
        assert f"{label} {expected}" in md


def test_canonical_market_cap_reaches_f1(financial_sources: dict[str, Any]) -> None:
    ctx = context.build_shared_context("600519", "样本")
    assert ctx.fundamental["market_cap"] == 100_000_000_000.0
    assert build_fundamental_dim(ctx).valuation_detail["market_cap"] == 100_000_000_000.0


def test_legacy_mapping_snapshot_is_not_reused(financial_sources: dict[str, Any]) -> None:
    dim_cache.save_snapshot("stage0_fund_600519", {"pe_ttm": None, "revenue_growth": None, "market_cap": None})
    ctx = context.build_shared_context("600519", "样本")
    assert ctx.fundamental["pe_ttm"] == 0.0
    assert ctx.fundamental["revenue_growth"] == 0.0
    assert ctx.fundamental["market_cap"] == 100_000_000_000.0


@pytest.mark.parametrize("dim_id", ["fundamental", "six_dim", "scenarios"])
def test_legacy_financial_cache_misses_without_invalidating_other_dims(
    financial_sources: dict[str, Any], dim_id: str,
) -> None:
    dim_cache.save_cached_dim("600519", dim_id, {"market_cap": 100.0})
    dim_cache.save_cached_dim("600519", "business", {"narrative": "保留"})
    path = Path(dim_cache._CACHE_DIR) / f"600519_{dim_id}.json"
    record = json.loads(path.read_text())
    del record["mapping_version"]
    path.write_text(json.dumps(record))
    assert dim_cache.load_cached_dim("600519", dim_id) is None
    assert dim_cache.load_cached_dim("600519", "business") == {"narrative": "保留"}
    dim_cache.save_cached_dim("600519", dim_id, {"market_cap": 100.0})
    assert dim_cache.load_cached_dim("600519", dim_id) == {"market_cap": 100.0}


@pytest.mark.parametrize("missing_pb", [False, True])
def test_stock_info_valuation_fallback_only_fills_none(
    financial_sources: dict[str, Any], missing_pb: bool,
) -> None:
    quote = financial_sources["quote"]
    quote.total_mv = 0.0
    quote.circ_mv = 0.0
    quote.pb_ratio = None if missing_pb else 0.0
    fallback = UnifiedRealtimeQuote(
        "600519", name="样本", source=RealtimeSource.EFINANCE,
        price=20.0, pe_ratio=25.0, pb_ratio=2.0, total_mv=100.0, circ_mv=80.0,
    )
    provider = Mock(side_effect=[quote, fallback])
    financial_sources["fetcher"].get_realtime_quote = provider
    result = data_tools.get_stock_info_tool.handler(stock_code="600519")
    assert result["pe_ratio"] == 0.0
    assert result["pb_ratio"] == (2.0 if missing_pb else 0.0)
    assert result["total_mv"] == 0.0
    assert result["circ_mv"] == 0.0
    assert provider.call_count == (2 if missing_pb else 1)
