"""Contract entry checks native annual identity and full capital-window amounts."""
from decimal import Decimal

import pytest

from data_provider.choice_adapter import AnnualReport
from data_provider.capital_flow_provider import compute_cumulative
from data_provider.cross_source_validator import AnchorReading, select_current_financials


def test_annual_identity_discards_inconsistent_native_gross_amount():
    row = AnnualReport(report_date="2025-12-31", disclosure_date="2026-04-28", revenue=Decimal(100), operating_cost=Decimal(60), gross_profit=Decimal(99)).as_record()
    assert row["gross_profit"] is None
    assert row["errors"] == ["gross_profit:identity_mismatch"]


@pytest.mark.parametrize("value", [float("inf"), "invalid"])
def test_cached_money_is_validated_before_scoring(value):
    record = {"year": 2025, "report_date": "2025-12-31", "disclosure_date": "2026-04-28", "revenue": value, "source": "choice", "currency": "CNY", "statement_scope": "consolidated", "adjustment": "unadjusted"}
    with pytest.raises(ValueError):
        AnnualReport.from_record(record)


def test_disclosure_requires_a_real_available_date():
    with pytest.raises(ValueError):
        AnnualReport(report_date="2025-12-31", disclosure_date="2026-99-99")


def test_sum_contract_preserves_exact_decimal_and_full_dates():
    dates = ["2026-10-13", "2026-10-12"]
    assert compute_cumulative([(dates[0], 0.1), (dates[1], 0.2)], 2, trading_dates=dates, source_unit="CNY") == Decimal("0.3")
    assert compute_cumulative([(dates[0], 0.1)], 2, trading_dates=dates, source_unit="CNY") is None


@pytest.mark.parametrize("missing", ["source", "currency", "statement_scope", "adjustment"])
def test_cached_unknown_provenance_cannot_be_relabelled(missing):
    record = AnnualReport(report_date="2025-12-31", disclosure_date="2026-04-28", revenue=Decimal(100)).as_record()
    record.pop(missing)
    with pytest.raises(ValueError):
        AnnualReport.from_record(record)


@pytest.mark.parametrize('value', [0.0, -123.0, 123.0])
def test_current_snapshot_keeps_latest_period_and_zero_or_negative_values(value):
    from datetime import date
    year = date.today().year - 1
    period = f'{year}-12-31'
    primary = AnchorReading('ifind', value, period=period, period_basis='annual', caliber='operating_revenue', unit='currency_base', currency='CNY', statement_scope='consolidated', adjustment='restated')
    older = AnchorReading('choice', 999.0, period=f'{year - 1}-12-31', period_basis='annual', caliber='parent_net_profit', unit='currency_base', currency='CNY', statement_scope='consolidated', adjustment='restated')
    actual, selected = select_current_financials({'revenue': primary}, {'net_profit': older})
    assert actual == period
    assert selected == {'revenue': primary}


def test_current_snapshot_without_confirmed_period_preserves_gap():
    reading = AnchorReading('ifind', 1.0, unit='currency_base', currency='CNY', statement_scope='consolidated', adjustment='restated')
    assert select_current_financials({'revenue': reading}, {}) == (None, {})


def test_explicit_future_disclosure_cannot_be_adopted_as_current():
    from dataclasses import replace
    from datetime import date
    year = date.today().year - 1
    reading = AnchorReading('ifind', 123.0, period=f'{year}-12-31', period_basis='annual', caliber='operating_revenue', unit='currency_base', currency='CNY', statement_scope='consolidated', adjustment='restated', disclosed_at=f'{year + 2}-01-01')
    assert select_current_financials({'revenue': reading}, {}) == (None, {})
    # An unknown disclosure day alone does not erase useful current-period data.
    unknown = replace(reading, disclosed_at=None)
    assert select_current_financials({'revenue': unknown}, {}) == (reading.period, {'revenue': unknown})
