"""Keep transformed financial metrics out of level and year-on-year rules.
Run: python -m pytest tests/test_financial_metric_identity.py
Requires project dependencies; supplier responses and snapshot files are isolated.
"""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest

from data_provider.cross_source_validator import AnchorReading, CrossSourceValidator, adopted_field_record
from data_provider.fundamental_adapter import AkshareFundamentalAdapter
from data_provider.ifind_fundamental_adapter import _parse_ifind_response
from data_provider.mx_data_adapter import MXClient, MXSource
from data_provider.mx_mcp_adapter import _parse_mx_mcp_response
from src.agent.tools.cross_validation_helpers import field_record_from_validation
from src.deep_research_dims.context import SharedContext
from src.deep_research_dims.fundamental_dim import build_fundamental_dim
from src.deep_research_dims.six_dim import build_six_dim


def parse_metric(source: str, field: str, values: dict[str, float]) -> AnchorReading | None:
    keywords = {"gross_margin": ["销售毛利率", "毛利率"], "roe": ["净资产收益率", "ROE"],
                "revenue_yoy": ["营业收入"], "net_profit_yoy": ["归母净利润"]}[field]
    if source == "mx":
        return MXSource().read_bundle({"_mx_period": "2025年报", **values}, field)
    if source == "ifind":
        headers = "|".join(values)
        cells = "|".join(str(value) for value in values.values())
        markdown = f"|报告期|{headers}|\n|---|{'---|' * len(values)}\n|2025年报|{cells}|"
        return _parse_ifind_response(json.dumps({"data": {"answer": markdown}}), keywords, field, None)
    if source == "choice":
        payload = {"data": [{"columns": ["指标", "2025年报"], "items": list(values.items())}]}
        return _parse_mx_mcp_response(json.dumps(payload), keywords, field, None)
    adapter = AkshareFundamentalAdapter()
    frame = pd.DataFrame([{"报告期": "2025年报", **values}])
    with patch.object(adapter, "_call_df_candidates", side_effect=lambda candidates: (frame, "fixture", []) if candidates[0][0] == "stock_financial_abstract" else (None, None, [])):
        growth = adapter.get_fundamental_bundle("600519")["growth"]
    from data_provider.cross_source_validator import reading_from_field_record
    return reading_from_field_record(growth.get(field), growth.get("field_meta", {}).get(field))


@pytest.mark.parametrize("source", ["mx", "ifind", "choice", "akshare"])
@pytest.mark.parametrize("field,label", [
    ("gross_margin", "销售毛利率同比(%)"),
    ("roe", "净资产收益率ROE(加权)同比(%)"),
    ("revenue_yoy", "营业收入3年复合增长率(%)"),
    ("net_profit_yoy", "归母净利润3年复合增长率(%)"),
    ("revenue_yoy", "营业收入增长率(%)"),
    ("roe", "净资产收益率ROE(非加权)(%)"),
])
def test_transformed_or_undefined_metric_is_not_a_rule_input(source: str, field: str, label: str) -> None:
    reading = parse_metric(source, field, {label: 2.0})
    assert reading is None or not adopted_field_record(field, reading)["rule_eligible"]


@pytest.mark.parametrize("source", ["mx", "ifind", "choice", "akshare"])
@pytest.mark.parametrize("field,bad_label,valid_label,expected", [
    ("gross_margin", "销售毛利率同比(%)", "销售毛利率(%)", 40.0),
    ("roe", "净资产收益率ROE(加权)同比(%)", "净资产收益率ROE(加权)(%)", 18.0),
    ("revenue_yoy", "营业收入3年复合增长率(%)", "营业收入同比增长率(%)", -20.0),
    ("net_profit_yoy", "归母净利润3年复合增长率(%)", "归母净利润同比增长率(%)", 0.0),
])
def test_real_metric_survives_an_adjacent_transformed_column(source: str, field: str, bad_label: str, valid_label: str, expected: float) -> None:
    reading = parse_metric(source, field, {bad_label: 2.0, valid_label: expected})
    assert reading is not None and reading.value == expected
    assert adopted_field_record(field, reading)["rule_eligible"]


def test_agreeing_margin_changes_do_not_verify_or_score_as_margin() -> None:
    sources = [SimpleNamespace(name=name, read=lambda code, field, period=None, name=name: parse_metric(name, field, {"销售毛利率同比(%)": 2.0})) for name in ("mx", "ifind")]
    validator = CrossSourceValidator(sources)
    try:
        result = validator.verify("600519", "gross_margin")
        assert not result.agreed and result.confidence != "high"
        compact = result.to_compact()
        record = field_record_from_validation("gross_margin", compact)
        ctx = SharedContext("600519", "样本", "2026-10-09", fundamental={"gross_margin": compact["v"], "field_meta": {"gross_margin": record}})
        f1 = build_fundamental_dim(ctx)
        assert f1.profitability["gross_margin"]["score"] is None and f1.health_score is None
        six = build_six_dim(ctx, f1_payload=f1.model_dump()).model_dump()
        health = next(item for dim in six["framework"]["dimensions"] if dim["dimension"] == "基本面" for item in dim["indicators"] if item["name"] == "业务与财务健康")
        assert health["score"] == 50 and health["data_gap"]
    finally:
        validator._pool.shutdown(wait=False, cancel_futures=True)


def test_manager_to_f1_uses_actual_yoy_instead_of_cagr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from tests.test_financial_missing_fields import manager
    from src.agent.tools import data_tools
    from src.deep_research_dims import dim_cache
    from src.deep_research_dims.context import _safe_fundamental

    mgr = manager(monkeypatch, {})
    response = {"status": 0, "data": {"data": {"searchDataResultDTO": {"dataTableDTOList": [{
        "table": {"headName": ["2025年报"], "cagr": [10], "yoy": [-20]},
        "nameMap": {"cagr": "营业收入3年复合增长率(%)", "yoy": "营业收入同比增长率(%)"},
    }]}}}}
    client = MXClient(api_key="offline-fixture")
    monkeypatch.setattr(client, "query", lambda *args, **kwargs: response)
    mgr._mx_source = MXSource(client)
    monkeypatch.setattr(data_tools, "_get_fetcher_manager", lambda: mgr)
    monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path))
    ctx = SharedContext("600519", "样本", "2026-10-09")
    ctx.fundamental = _safe_fundamental("600519", ctx)
    dim = build_fundamental_dim(ctx)
    assert dim.growth_quality["revenue_yoy"]["value"] == -20.0
    assert dim.health_score == 35.0
