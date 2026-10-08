"""Regressions for supplier evidence reaching the real cross-source validator.
Run: python -m pytest tests/test_cross_source_data_quality.py
Requires project dependencies; all provider transports are fixture responses.
"""

from types import SimpleNamespace
import json
from datetime import datetime, timezone

import pytest
import requests

from data_provider.mx_data_adapter import MXClient, MXSource
from data_provider.cross_source_validator import AnchorReading, CrossSourceValidator
from data_provider.cross_source_validator import PeriodBasis
from data_provider.ifind_fundamental_adapter import IfindSource, _parse_ifind_response
from data_provider.mx_mcp_adapter import MxMcpSource, _parse_mx_mcp_response
from data_provider.fuyao_adapter import FuyaoSource, _parse_fuyao_response
from src.agent.tools.cross_validation_helpers import build_cross_validation_block


def mx_financial_response(periods: list[str], values: list[str]) -> dict[str, object]:
    return {
        "status": 0,
        "data": {"data": {"searchDataResultDTO": {"dataTableDTOList": [{
            "entityName": "样本(600519.SH)",
            "table": {"headName": periods, "row1": values},
            "nameMap": {"row1": "营业收入（单位：元）"},
        }]}}},
    }


def test_mx_requested_annual_value_uses_actual_response_column(monkeypatch: pytest.MonkeyPatch) -> None:
    response = mx_financial_response(["2026中报", "2025年报"], ["50", "100"])
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: SimpleNamespace(status_code=200, json=lambda: response))
    source = MXSource(MXClient(api_key="offline-fixture"))
    reading = source.read("600519", "revenue", period="2025年报")
    assert reading is not None
    assert reading.value == 100.0
    assert reading.period == "2025-12-31"
    assert reading.period_basis == "annual"
    assert reading.requested_period == "2025年报"


def test_fuyao_uses_returned_report_and_keeps_requested_period() -> None:
    raw = json.dumps({"code": 0, "data": {"items": [{
        "thscode": "600519.SH", "report": "2026-2", "fields": {"revenue": 50.0},
    }]}})
    fetcher = SimpleNamespace(available=True, fetch=lambda code, field, period: _parse_fuyao_response(raw, field, period))
    reading = FuyaoSource(fetcher=fetcher).read("600519", "revenue", "2025年报")
    assert reading is not None
    assert reading.period == "2026-06-30"
    assert reading.period_basis == "YTD"
    assert reading.requested_period == "2025年报"


def test_choice_response_columns_preserve_actual_requested_period() -> None:
    raw = json.dumps({"data": [{
        "columns": ["指标", "2026中报", "2025年报"],
        "items": [["营业收入（单位：元）", 50, 100]],
    }]})
    fetcher = SimpleNamespace(available=True, fetch=lambda code, field, period: _parse_mx_mcp_response(raw, ["营业收入"], field, period))
    reading = MxMcpSource(fetcher=fetcher).read("600519", "revenue", "2025年报")
    assert reading is not None
    assert reading.value == 100.0
    assert reading.period == "2025-12-31"
    assert reading.period_basis == "annual"
    assert reading.requested_period == "2025年报"


@pytest.mark.parametrize("field", ["revenue", "net_profit", "roe", "gross_margin", "revenue_yoy", "net_profit_yoy"])
def test_unknown_actual_financial_period_is_not_strictly_verified(field: str) -> None:
    from data_provider.cross_source_validator import Unit
    unit: Unit = "currency_base" if field in {"revenue", "net_profit"} else "percentage_point"
    primary = AnchorReading("mx", 100.0, caliber="published", period="2025年报", unit=unit, currency="CNY")
    secondary = AnchorReading("ifind", 100.0, caliber="published", period=None, unit=unit, currency="CNY")
    source = SimpleNamespace(name="ifind", read=lambda *args: secondary)
    result = CrossSourceValidator([source]).verify("600519", field, primary_reading=primary)
    assert result.value == 100.0
    assert result.confidence == "medium"
    assert not result.agreed
    assert "period_unknown" in result.quality.reason_codes


def test_ifind_response_period_survives_source_wrapper() -> None:
    raw = json.dumps({"data": {"answer": "|报告期|营业收入（单位：元）|\n|---|---|\n|2026中报|50|\n|2025年报|100|"}})
    fetcher = SimpleNamespace(available=True, fetch=lambda code, field, period: _parse_ifind_response(raw, ["营业收入"], field, period))
    reading = IfindSource(fetcher=fetcher).read("600519", "revenue", "2025年报")
    assert reading is not None
    assert reading.value == 100.0
    assert reading.period == "2025-12-31"
    assert reading.period_basis == "annual"
    assert reading.requested_period == "2025年报"


@pytest.mark.parametrize("periods,values", [(["2026中报", "2025年报"], ["50", "100"]), (["2025年报", "2026中报"], ["100", "50"])])
def test_mx_period_selection_is_column_order_independent(
    monkeypatch: pytest.MonkeyPatch, periods: list[str], values: list[str],
) -> None:
    response = mx_financial_response(periods, values)
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: SimpleNamespace(status_code=200, json=lambda: response))
    reading = MXSource(MXClient(api_key="offline-fixture")).read("600519", "revenue", "2025年报")
    assert reading is not None and reading.value == 100.0 and reading.period == "2025-12-31"


def test_mx_missing_requested_period_retains_actual_fallback_and_compact(monkeypatch: pytest.MonkeyPatch) -> None:
    response = mx_financial_response(["2026中报"], ["50"])
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: SimpleNamespace(status_code=200, json=lambda: response))
    reading = MXSource(MXClient(api_key="offline-fixture")).read("600519", "revenue", "2025年报")
    assert reading is not None and reading.value == 50.0
    assert reading.period == "2026-06-30" and reading.period_basis == "YTD"
    assert reading.requested_period == "2025年报"
    peer = AnchorReading("ifind", 50.0, caliber=reading.caliber, period="2026中报")
    validator = CrossSourceValidator([SimpleNamespace(name="ifind", read=lambda *args: peer)])
    block = build_cross_validation_block("600519", ["revenue"], period="2025年报", primary_readings={"revenue": reading}, validator=validator)
    assert block is not None
    assert block["anchors"]["revenue"]["period"] == "2026-06-30"
    assert block["anchors"]["revenue"]["period_basis"] == "YTD"
    assert block["anchors"]["revenue"]["requested_period"] == "2025年报"


@pytest.mark.parametrize("secondary_period,basis", [("2025-12-31", "unknown"), ("2025年报", "single_quarter")])
def test_unknown_or_incompatible_financial_period_basis_prevents_verification(secondary_period: str, basis: PeriodBasis) -> None:
    primary = AnchorReading("mx", 100.0, caliber="operating_revenue", period="2025年报", unit="currency_base", currency="CNY")
    secondary = AnchorReading("ifind", 100.0, caliber="operating_revenue", period=secondary_period, period_basis=basis, unit="currency_base", currency="CNY")
    result = CrossSourceValidator([SimpleNamespace(name="ifind", read=lambda *args: secondary)]).verify("600519", "revenue", primary_reading=primary)
    assert result.confidence == "medium" and not result.agreed
    assert any(code.startswith("period_basis_") for code in result.quality.reason_codes)


def test_confirmed_annual_date_and_chinese_period_are_equivalent() -> None:
    primary = AnchorReading("mx", 100.0, caliber="operating_revenue", period="2025年报", unit="currency_base", currency="CNY")
    secondary = AnchorReading("ifind", 100.0, caliber="operating_revenue", period="20251231 annual", unit="currency_base", currency="CNY")
    result = CrossSourceValidator([SimpleNamespace(name="ifind", read=lambda *args: secondary)]).verify("600519", "revenue", primary_reading=primary)
    assert result.confidence == "high" and result.agreed
    assert result.period == "2025-12-31" and result.period_basis == "annual"


def test_request_echo_and_unknown_valuation_labels_do_not_prove_metadata() -> None:
    raw = json.dumps({"request": {"query": "2025年报", "report_period": "2025年报"}, "response": {"营业收入": 100.0}})
    reading = _parse_mx_mcp_response(raw, ["营业收入"], "revenue", "2025年报")
    assert reading is not None and reading.period is None and reading.requested_period == "2025年报"
    pe = _parse_mx_mcp_response(json.dumps({"response": {"市盈率": 30.0}}), ["市盈率"], "pe_ratio", None)
    assert pe is not None and pe.caliber is None
    pb = _parse_fuyao_response(json.dumps({"code": 0, "data": {"items": [{"thscode": "600519.SH", "fields": {"pb_mrq": 2.0}}]}}), "pb_ratio", None)
    assert pb is not None and pb.caliber == "MRQ"


def test_manager_as_of_uses_actual_period_not_a_guessed_year() -> None:
    from data_provider.base import DataFetcherManager

    assert DataFetcherManager._derive_as_of_date({"growth": {"data": {"report_date": "2026-06-30", "roe": 2025.0}}}) == "2026-06-30"
    assert DataFetcherManager._derive_as_of_date({"earnings": {"data": {"forecast_summary": "2025年预增"}}}) is None


@pytest.mark.parametrize("period", ["2025/6/30", "2025-6-30"])
def test_unpadded_dates_remain_usable_in_real_financial_table(period: str) -> None:
    import pandas as pd
    from data_provider.fundamental_adapter import _select_financial_row

    row, actual = _select_financial_row(pd.DataFrame({"指标": ["营业收入"], period: [100.0], "2024/6/30": [80.0]}), "600519")
    assert row is not None and actual == "2025-06-30"


@pytest.mark.parametrize("source", ["mx", "ifind", "choice", "fuyao"])
def test_explicit_single_quarter_basis_survives_all_source_routes(source: str, monkeypatch: pytest.MonkeyPatch) -> None:
    label = "营业收入（单季，元，人民币）"
    if source == "mx":
        response = mx_financial_response(["2026中报"], ["50"])
        response["data"]["data"]["searchDataResultDTO"]["dataTableDTOList"][0]["nameMap"]["row1"] = label
        monkeypatch.setattr(requests, "post", lambda *args, **kwargs: SimpleNamespace(status_code=200, json=lambda: response))
        reading = MXSource(MXClient(api_key="offline-fixture")).read("600519", "revenue")
    elif source == "ifind":
        raw = json.dumps({"data": {"answer": f"|报告期|{label}|\n|---|---|\n|2026中报|50|"}})
        reading = _parse_ifind_response(raw, ["营业收入"], "revenue", None)
    elif source == "choice":
        raw = json.dumps({"data": [{"columns": ["指标", "2026中报"], "items": [[label, 50]]}]})
        reading = _parse_mx_mcp_response(raw, ["营业收入"], "revenue", None)
    else:
        raw = json.dumps({"code": 0, "data": {"items": [{"thscode": "600519.SH", "report": "2026-2", "fields": {"revenue": 50.0, "period_basis": "single_quarter", "unit": "元", "currency": "CNY"}}]}})
        reading = _parse_fuyao_response(raw, "revenue", None)
    assert reading is not None and reading.period == "2026-06-30"
    assert reading.period_basis == "single_quarter"
    peer = AnchorReading("peer", 50.0, caliber=reading.caliber, period="2026中报", unit=reading.unit, currency=reading.currency)
    result = CrossSourceValidator([SimpleNamespace(name="peer", read=lambda *args: peer)]).verify("600519", "revenue", primary_reading=reading)
    assert result.confidence == "medium" and not result.agreed
    assert "period_basis_mismatch" in result.quality.reason_codes


@pytest.mark.parametrize("source", ["ifind", "choice"])
def test_requested_field_gap_falls_back_to_valid_actual_period(source: str) -> None:
    if source == "ifind":
        raw = json.dumps({"data": {"answer": "|报告期|营业收入|\n|---|---|\n|2025年报|--|\n|2026中报|50|"}})
        reading = _parse_ifind_response(raw, ["营业收入"], "revenue", "2025年报")
    else:
        raw = json.dumps({"data": [{"columns": ["指标", "2025年报", "2026中报"], "items": [["营业收入", None, 50]]}]})
        reading = _parse_mx_mcp_response(raw, ["营业收入"], "revenue", "2025年报")
    assert reading is not None and reading.value == 50.0
    assert reading.period == "2026-06-30" and reading.requested_period == "2025年报"


def test_response_observation_date_is_retained_without_using_it_as_financial_period() -> None:
    raw = json.dumps({"data": {"answer": "|日期|市盈率TTM|\n|---|---|\n|20260625|30|"}})
    reading = _parse_ifind_response(raw, ["市盈率TTM"], "pe_ratio", None)
    assert reading is not None and reading.observed_at == "20260625"
    assert reading.period is None


def test_mx_falls_back_to_latest_nonempty_field_in_returned_table(monkeypatch: pytest.MonkeyPatch) -> None:
    response = mx_financial_response(["2025年报", "2026中报", "2026一季报"], [None, None, "40"])
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: SimpleNamespace(status_code=200, json=lambda: response))
    reading = MXSource(MXClient(api_key="offline-fixture")).read("600519", "revenue", "2025年报")
    assert reading is not None and reading.value == 40.0
    assert reading.period == "2026-03-31" and reading.requested_period == "2025年报"


@pytest.mark.parametrize("observed", ["2026-06-25", "2026-06-25T15:00:00+08:00", "2026-06-25(日)"])
def test_choice_quote_date_column_is_observation_time(observed: str) -> None:
    raw = json.dumps({"data": [{"columns": ["指标", observed], "items": [["收盘价", 100]]}]})
    reading = _parse_mx_mcp_response(raw, ["收盘价"], "current_price", None)
    assert reading is not None and reading.observed_at == observed
    assert reading.period is None


@pytest.mark.parametrize("observed", ["2026-06-25", "2026-06-25T15:00:00+08:00", "2026-06-25(日)"])
def test_mx_quote_date_column_keeps_full_observation_time(observed: str, monkeypatch: pytest.MonkeyPatch) -> None:
    response = mx_financial_response([observed], ["100"])
    response["data"]["data"]["searchDataResultDTO"]["dataTableDTOList"][0]["nameMap"]["row1"] = "最新价"
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: SimpleNamespace(status_code=200, json=lambda: response))
    reading = MXSource(MXClient(api_key="offline-fixture")).read("600519", "current_price")
    assert reading is not None and reading.observed_at == observed and reading.period is None


@pytest.mark.parametrize("source", ["mx", "ifind", "choice", "fuyao"])
@pytest.mark.parametrize("label,raw", [("总市值（万元，人民币）", "100000"), ("总市值（元，人民币）", "1000000000"), ("总市值（人民币）", "100000万元")])
def test_equivalent_amount_units_through_real_sources(source: str, label: str, raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    observed = datetime.now(timezone.utc).isoformat()
    if source == "mx":
        response = mx_financial_response(["2026中报"], [raw])
        response["data"]["data"]["searchDataResultDTO"]["dataTableDTOList"][0]["nameMap"]["row1"] = label
        dto = response["data"]["data"]["searchDataResultDTO"]["dataTableDTOList"][0]
        dto["nameMap"]["obs"] = "observed_at"
        dto["table"]["obs"] = [observed]
        monkeypatch.setattr(requests, "post", lambda *args, **kwargs: SimpleNamespace(status_code=200, json=lambda: response))
        reading = MXSource(MXClient(api_key="offline-fixture")).read("600519", "total_mv")
    elif source == "ifind":
        text = json.dumps({"data": {"answer": f"|{label}|observed_at|\n|---|---|\n|{raw}|{observed}|"}})
        reading = _parse_ifind_response(text, ["总市值"], "total_mv", None)
    elif source == "choice":
        reading = _parse_mx_mcp_response(json.dumps({"response": {label: raw, "observed_at": observed}}), ["总市值"], "total_mv", None)
    else:
        unit = "万元" if "万元" in label else "元" if "（元" in label else None
        fields = {"total_mv": raw, "currency": "CNY", "observed_at": observed}
        if unit:
            fields["unit"] = unit
        reading = _parse_fuyao_response(json.dumps({"code": 0, "data": {"items": [{"thscode": "600519.SH", "fields": fields}]}}), "total_mv", None)
    assert reading is not None and reading.value == 1_000_000_000.0
    assert reading.unit == "currency_base" and reading.currency == "CNY"
    peer = AnchorReading("ifind" if source in {"mx", "choice"} else "mx", 1_000_000_000.0, unit="currency_base", currency="CNY", observed_at=observed)
    result = CrossSourceValidator([SimpleNamespace(name=peer.source, read=lambda *args: peer)]).verify("600519", "total_mv", primary_reading=reading)
    assert result.confidence == "high" and result.agreed
    compact = result.to_compact()
    assert compact["unit"] == "currency_base" and compact["currency"] == "CNY"
    assert compact["raw_value"] == raw


@pytest.mark.parametrize("label,raw,expected_unit", [("ROE（%）", "18%", "percentage_point"), ("ROE（ratio）", "0.18", "percentage_point"), ("ROE", "0.18", None)])
def test_percentage_scale_requires_response_evidence(label: str, raw: str, expected_unit: str | None) -> None:
    reading = _parse_mx_mcp_response(json.dumps({"response": {label: raw}}), ["ROE"], "roe", None)
    assert reading is not None and reading.unit == expected_unit
    assert reading.value == (18.0 if expected_unit else 0.18)


@pytest.mark.parametrize("label,raw,currency", [("总市值（万元，人民币）", "100亿元", "CNY"), ("总市值（元）", "100", None), ("总市值（元，美元）", "100", "USD"), ("总市值（单位：桶，人民币）", "100", "CNY"), ("总市值（人民币）", "100桶", "CNY")])
def test_ambiguous_or_incompatible_units_and_currency_do_not_verify(label: str, raw: str, currency: str | None) -> None:
    reading = _parse_mx_mcp_response(json.dumps({"response": {label: raw}}), ["总市值"], "total_mv", None)
    assert reading is not None and reading.currency == currency
    peer = AnchorReading("peer", reading.value, unit="currency_base", currency="CNY")
    result = CrossSourceValidator([SimpleNamespace(name="peer", read=lambda *args: peer)]).verify("600519", "total_mv", primary_reading=reading)
    assert result.confidence == "medium" and not result.agreed
    assert result.to_compact()["raw_value"] == raw


@pytest.mark.parametrize("currency", ["EUR", "unknown", "", " ", "N/A", "UNK", "NAN", "XYZ"])
def test_explicit_currency_is_not_overwritten_or_verified_as_unknown(currency: str) -> None:
    label = "总市值（元，人民币）" if currency == "EUR" else "总市值（元）"
    reading = _parse_mx_mcp_response(json.dumps({"response": {label: 100, "currency": currency}}), ["总市值"], "total_mv", None)
    assert reading is not None
    peer = AnchorReading("ifind", 100, unit=reading.unit, currency=reading.currency)
    result = CrossSourceValidator([SimpleNamespace(name="ifind", read=lambda *args: peer)]).verify("600519", "total_mv", primary_reading=reading)
    assert result.confidence == "medium" and not result.agreed
    if currency == "EUR":
        assert reading.currency == "EUR" and reading.normalization_error == "currency_conflict"
    assert result.to_compact()["raw_currency"] == currency


def test_percentage_does_not_require_an_amount_currency() -> None:
    raw = json.dumps({"response": {"ROE（%）": 18, "currency": "unknown"}})
    reading = _parse_mx_mcp_response(raw, ["ROE"], "roe", None)
    assert reading is not None and reading.value == 18 and reading.unit == "percentage_point"
    assert reading.normalization_error is None


def test_fuyao_explicit_unit_currency_participates_in_contradiction_check() -> None:
    raw = json.dumps({"code": 0, "data": {"items": [{"thscode": "600519.SH", "fields": {"total_mv": 100, "unit": "人民币元", "currency": "EUR"}}]}})
    fetcher = SimpleNamespace(available=True, fetch=lambda code, field, period: _parse_fuyao_response(raw, field, period))
    reading = FuyaoSource(fetcher=fetcher).read("600519", "total_mv")
    assert reading is not None and reading.normalization_error == "currency_conflict"
    peer = AnchorReading("ifind", 100, unit="currency_base", currency="EUR")
    result = CrossSourceValidator([SimpleNamespace(name="ifind", read=lambda *args: peer)]).verify("600519", "total_mv", primary_reading=reading)
    assert result.confidence == "medium" and not result.agreed
    assert result.to_compact()["raw_unit"] == "人民币元"
