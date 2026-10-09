"""Exercise the financial scale contract and existing numeric tolerance at the Source seam.
Run: python -m pytest tests/test_fundamental_units_contract.py
Requires project dependencies; no external service or database.
"""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from data_provider.cross_source_validator import AnchorReading, CrossSourceValidator, adopted_field_record, normalize_anchor_value
from data_provider.fuyao_adapter import _parse_fuyao_response
from data_provider.ifind_fundamental_adapter import _parse_ifind_response
from data_provider.mx_data_adapter import MXSource
from data_provider.mx_mcp_adapter import _parse_mx_mcp_response


@pytest.mark.parametrize("raw,expected", [("0", 0.0), ("-0.123456", -1234.56), ("1.23e5", 1230000000.0)])
def test_decimal_scale_keeps_finite_json_numbers_and_existing_tolerance(raw: str, expected: float) -> None:
    observed = datetime.now(timezone.utc).isoformat()
    reading = _parse_mx_mcp_response(json.dumps({"response": {"总市值（万元，人民币）": raw, "observed_at": observed}}), ["总市值"], "total_mv", None)
    assert reading is not None and reading.value == expected
    peer = AnchorReading("ifind", expected, unit="currency_base", currency="CNY", observed_at=observed)
    result = CrossSourceValidator([SimpleNamespace(name="ifind", read=lambda *args: peer)]).verify("600519", "total_mv", primary_reading=reading)
    assert result.confidence == "high" and result.agreed
    json.dumps(result.to_compact(), allow_nan=False)


def test_opposite_extreme_finite_readings_have_finite_relative_difference() -> None:
    observed = datetime.now(timezone.utc).isoformat()
    reading = _parse_mx_mcp_response(json.dumps({"response": {"市盈率TTM": 1e308, "observed_at": observed}}), ["市盈率TTM"], "pe_ratio", None)
    assert reading is not None
    peer = AnchorReading("ifind", -1e308, caliber="TTM", unit="multiple", observed_at=observed)
    result = CrossSourceValidator([SimpleNamespace(name="ifind", read=lambda *args: peer)]).verify("600519", "pe_ratio", primary_reading=reading)
    assert result.quality.status == "conflict" and result.discrepancy_pct == 200.0
    json.dumps(result.to_compact(), allow_nan=False)


@pytest.mark.parametrize("label,raw,currency,expected", [
    ("总市值（亿美元）", "2", "USD", 200_000_000.0),
    ("总市值", "2亿美元", "USD", 200_000_000.0),
    ("总市值（亿港元）", "2", "HKD", 200_000_000.0),
    ("总市值", "2亿港元", "HKD", 200_000_000.0),
    ("总市值（万日元）", "2", "JPY", 20_000.0),
    ("总市值（亿欧元）", "-2", "EUR", -200_000_000.0),
    ("总市值（万英镑）", "2", "GBP", 20_000.0),
    ("总市值（百万瑞士法郎）", "2", "CHF", 2_000_000.0),
    ("总市值（亿美元）", "0", "USD", 0.0),
    ("总市值（万美元）", "1.23e5", "USD", 1_230_000_000.0),
])
def test_foreign_currency_names_preserve_the_declared_scale(label: str, raw: str, currency: str, expected: float) -> None:
    value, unit, parsed_currency, error = normalize_anchor_value("total_mv", raw, label)
    assert (value, unit, parsed_currency, error) == (expected, "currency_base", currency, None)


@pytest.mark.parametrize("source", ["mx", "choice", "ifind", "fuyao"])
def test_foreign_amounts_reach_source_eligibility_in_basic_currency_units(source: str) -> None:
    observed = datetime.now(timezone.utc).isoformat()
    label = "总市值（亿美元）"
    if source == "mx":
        reading = MXSource().read_bundle({label: "2", "observed_at": observed}, "total_mv")
    elif source == "choice":
        payload = {"data": [{"columns": ["指标", observed], "items": [[label, "2"]]}]}
        reading = _parse_mx_mcp_response(json.dumps(payload), ["总市值"], "total_mv", None)
    elif source == "ifind":
        answer = f"|{label}|observed_at|\n|---|---|\n|2|{observed}|"
        reading = _parse_ifind_response(json.dumps({"data": {"answer": answer}}), ["总市值"], "total_mv", None)
    else:
        payload = {"code": 0, "data": {"items": [{"thscode": "AAPL.US", "fields": {"total_mv": 2, "unit": "亿美元", "currency": "USD", "observed_at": observed}}]}}
        reading = _parse_fuyao_response(json.dumps(payload), "total_mv", None)
    assert reading is not None and reading.value == 200_000_000.0
    assert adopted_field_record("total_mv", reading)["rule_eligible"]


def test_equivalent_foreign_amounts_agree_through_real_source_parsers() -> None:
    observed = datetime.now(timezone.utc).isoformat()
    main = _parse_mx_mcp_response(json.dumps({"data": [{"columns": ["指标", observed], "items": [["总市值（亿美元）", "2"]]}]}), ["总市值"], "total_mv", None)
    answer = f"|总市值（单位：元，美元）|observed_at|\n|---|---|\n|200000000|{observed}|"
    peer = _parse_ifind_response(json.dumps({"data": {"answer": answer}}), ["总市值"], "total_mv", None)
    assert main is not None and peer is not None
    validator = CrossSourceValidator([SimpleNamespace(name="ifind", read=lambda *_: peer)])
    try:
        result = validator.verify("AAPL", "total_mv", primary_reading=main)
        assert result.value == 200_000_000.0
        assert result.quality.status == "verified" and result.agreed and not result.conflicts
    finally:
        validator._pool.shutdown(wait=False, cancel_futures=True)


@pytest.mark.parametrize("raw,label,unit", [
    ("2亿未知元", "总市值", None),
    ("2", "总市值（亿未知元）", None),
    ("2", "总市值", "亿未知元"),
    ("2美元", "总市值（亿美元）", None),
    ("2", "总市值（十万人民币元）", None),
    ("2", "总市值（十万日元）", None),
    ("2", "总市值一百万人民币元", None),
])
def test_unrecognized_or_contradictory_scale_is_not_valid_money(raw: str, label: str, unit: str | None) -> None:
    _, canonical, _, error = normalize_anchor_value("total_mv", raw, label, unit=unit, currency="USD")
    assert canonical is None and error is not None
