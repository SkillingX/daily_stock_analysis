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
        lambda **kwargs: values["frame"] if values.get("frame") is not None else pd.DataFrame({
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


@pytest.mark.parametrize("old_key", ["stage0_fund_600519", "stage0_fund_v2_600519", "stage0_fund_v3_600519", "stage0_fund_v9_cv0_600519"])
def test_legacy_mapping_snapshot_is_not_reused(financial_sources: dict[str, Any], old_key: str) -> None:
    dim_cache.save_snapshot(old_key, {"pe_ttm": None, "revenue_growth": None, "market_cap": None})
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
    record["mapping_version"] = 2
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
        currency="CNY", field_meta={"pe_ratio": {"caliber": "TTM"}, "pb_ratio": {"caliber": "MRQ"}},
    )
    provider = Mock(side_effect=[quote, fallback])
    financial_sources["fetcher"].get_realtime_quote = provider
    result = data_tools.get_stock_info_tool.handler(stock_code="600519")
    assert result["pe_ratio"] == 0.0
    assert result["pb_ratio"] == (2.0 if missing_pb else 0.0)
    assert result["total_mv"] == 0.0
    assert result["circ_mv"] == 0.0
    assert provider.call_count == (2 if missing_pb else 1)


def test_transposed_financial_amounts_and_latest_period_reach_consumers(
    financial_sources: dict[str, Any],
) -> None:
    frame = pd.DataFrame({
        "指标": ["营业总收入", "归母净利润", "经营活动产生的现金流量净额", "净资产收益率",
               "毛利率", "营业总收入增长率", "归属母公司净利润增长率"],
        "20251231": [900.0, 250.0, 350.0, 12.0, 20.0, 5.0, 6.0],
        "20260630": [1000.0, 300.0, 400.0, 18.0, 35.0, 12.0, 9.0],
    })
    financial_sources["frame"] = frame
    manager = data_tools._get_fetcher_manager()
    raw = manager.get_fundamental_context("600519")
    report = raw["earnings"]["data"]["financial_report"]
    assert report["report_date"] == "2026-06-30"
    assert report["revenue"] == 1000.0
    assert report["net_profit_parent"] == 300.0
    assert report["operating_cash_flow"] == 400.0
    assert report["roe"] == 18.0
    ctx = context.build_shared_context("600519", "样本")
    dim = build_fundamental_dim(ctx)
    assert dim.profitability["roe"]["value"] == 18.0
    assert dim.growth_quality["revenue_yoy"]["value"] == 12.0


@pytest.mark.parametrize("columns_reversed", [False, True])
def test_latest_period_gaps_and_zero_never_borrow_an_older_value(
    financial_sources: dict[str, Any], columns_reversed: bool,
) -> None:
    frame = pd.DataFrame({
        "指标": ["净资产收益率", "毛利率", "营业总收入增长率", "归属母公司净利润增长率"],
        "20251231": [18.0, 35.0, 12.0, 9.0],
        "20260630": [float("nan"), 0.0, -5.0, 0.0],
    })
    if columns_reversed:
        frame = frame[["20260630", "指标", "20251231"]]
    financial_sources["frame"] = frame
    bundle = data_tools._get_fetcher_manager()._fundamental_adapter.get_fundamental_bundle("600519")
    assert {key: value for key, value in bundle["growth"].items() if key != "field_meta"} == {
        "roe": None, "gross_margin": 0.0, "revenue_yoy": -5.0,
        "net_profit_yoy": 0.0, "report_date": "2026-06-30",
    }


@pytest.mark.parametrize("date_label", ["最新一期", "注：2026-06-30"])
def test_unconfirmed_date_is_preserved_as_unknown(
    financial_sources: dict[str, Any], date_label: str,
) -> None:
    financial_sources["frame"] = pd.DataFrame({"指标": ["净资产收益率"], date_label: [18.0]})
    bundle = data_tools._get_fetcher_manager()._fundamental_adapter.get_fundamental_bundle("600519")
    assert bundle["growth"]["roe"] == 18.0
    assert bundle["growth"]["report_date"] is None
    assert bundle["earnings"]["financial_report"]["report_date"] is None


def test_ambiguous_financial_table_does_not_claim_data_success(financial_sources: dict[str, Any]) -> None:
    financial_sources["frame"] = pd.DataFrame({"指标": ["净资产收益率"], "说明一": [18.0], "说明二": [19.0]})
    bundle = data_tools._get_fetcher_manager()._fundamental_adapter.get_fundamental_bundle("600519")
    assert bundle["status"] == "not_supported"
    assert "financial_table:unconfirmed_or_ambiguous_period" in bundle["errors"]


def test_duplicate_category_metric_survives_real_consumer_chain(financial_sources: dict[str, Any]) -> None:
    financial_sources["frame"] = pd.DataFrame({
        "选项": ["常用指标", "盈利能力", "成长", "成长", "盈利能力"],
        "指标": ["净资产收益率", "净资产收益率", "营业总收入增长率", "归属母公司净利润增长率", "毛利率"],
        "20260630": [18.0, 18.0, 12.0, 9.0, 35.0],
    })
    ctx = context.build_shared_context("600519", "样本")
    assert ctx.fundamental["roe"] == 18.0
    assert build_fundamental_dim(ctx).profitability["roe"]["value"] == 18.0


def test_conflicting_duplicate_metric_is_explicitly_unavailable(financial_sources: dict[str, Any]) -> None:
    financial_sources["frame"] = pd.DataFrame({"指标": ["净资产收益率", "净资产收益率"], "20260630": [18.0, 20.0]})
    bundle = data_tools._get_fetcher_manager()._fundamental_adapter.get_fundamental_bundle("600519")
    assert bundle["growth"]["roe"] is None
    assert "financial_table:conflicting_duplicate_metric:净资产收益率" in bundle["errors"]


@pytest.mark.parametrize("wrong_amount", ["总资产净利润率(%)", "扣非净利润", "归母净利润(扣非)", "净利润"])
def test_amount_identity_does_not_borrow_a_different_profit_metric(
    financial_sources: dict[str, Any], wrong_amount: str,
) -> None:
    financial_sources["frame"] = pd.DataFrame({"报告期": ["2026-06-30"], wrong_amount: [2.5], "净资产收益率": [18.0]})
    bundle = data_tools._get_fetcher_manager()._fundamental_adapter.get_fundamental_bundle("600519")
    assert bundle["earnings"]["financial_report"]["net_profit_parent"] is None


def test_financial_period_metadata_does_not_claim_growth_data_success(financial_sources: dict[str, Any]) -> None:
    financial_sources["frame"] = pd.DataFrame({"指标": ["净资产收益率"], "20260630": [None]})
    raw = data_tools._get_fetcher_manager().get_fundamental_context("600519")
    assert raw["growth"]["data"]["report_date"] == "2026-06-30"
    assert raw["growth"]["status"] == "not_supported"


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("second", [18.0, 20.0])
def test_normalized_duplicate_labels_do_not_change_with_row_order(
    financial_sources: dict[str, Any], reverse: bool, second: float,
) -> None:
    frame = pd.DataFrame({"指标": ["净资产收益率", "净资产收益率(%)"], "20260630": [18.0, second]})
    financial_sources["frame"] = frame.iloc[::-1] if reverse else frame
    raw = data_tools._get_fetcher_manager().get_fundamental_context("600519")
    assert raw["growth"]["data"]["roe"] == (18.0 if second == 18.0 else None)
    if second == 20.0:
        assert "financial_table:conflicting_duplicate_metric:净资产收益率" in raw["growth"]["errors"]


def test_row_fallback_uses_same_latest_financial_period(
    financial_sources: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    import akshare

    rows = pd.DataFrame({
        "股票代码": ["600519", "600519"], "日期": ["2025-12-31", "2026-06-30"],
        "营业总收入": [900.0, 1000.0], "归母净利润": [250.0, 300.0],
        "经营活动产生的现金流量净额": [350.0, 400.0], "净资产收益率": [12.0, 18.0],
        "销售毛利率": [20.0, 35.0], "主营业务收入增长率(%)": [5.0, 12.0],
        "净利润同比": [6.0, 9.0],
    }, index=[0, 0])
    monkeypatch.setattr(akshare, "stock_financial_abstract", lambda **kwargs: pd.DataFrame())
    monkeypatch.setattr(akshare, "stock_financial_analysis_indicator", lambda **kwargs: rows)
    manager = data_tools._get_fetcher_manager()
    raw = manager.get_fundamental_context("600519")
    assert raw["growth"]["data"] == {
        "roe": 18.0, "gross_margin": 35.0, "revenue_yoy": 12.0,
        "net_profit_yoy": 9.0, "report_date": "2026-06-30",
    }
    assert raw["earnings"]["data"]["financial_report"] == {
        "revenue": 1000.0, "net_profit_parent": 300.0, "operating_cash_flow": 400.0,
        "roe": 18.0, "report_date": "2026-06-30",
    }
    assert "growth:stock_financial_analysis_indicator" in manager._fundamental_adapter.get_fundamental_bundle("600519")["source_chain"]
    ctx = context.build_shared_context("600519", "样本")
    dim = build_fundamental_dim(ctx)
    assert dim.profitability["roe"]["value"] == 18.0
    assert dim.growth_quality["revenue_yoy"]["value"] == 12.0
