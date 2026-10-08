"""Exercise the financial scale contract and existing numeric tolerance at the Source seam.
Run: python -m pytest tests/test_fundamental_units_contract.py
Requires project dependencies; no external service or database.
"""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from data_provider.cross_source_validator import AnchorReading, CrossSourceValidator
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
