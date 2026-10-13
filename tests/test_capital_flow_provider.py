"""Full trading windows use actual calendar dates, units and non-null values."""
from decimal import Decimal

import pytest

from data_provider.capital_flow_provider import compute_cumulative

DATES = ["2026-10-13", "2026-10-12", "2026-10-09", "2026-09-30", "2026-09-29"]


@pytest.mark.parametrize("unit,multiplier", [("CNY", 1), ("CNY_10K", 10000)])
def test_complete_window_sorts_deduplicates_and_preserves_zero_negative(unit, multiplier):
    series = [(DATES[3], 3), (DATES[0], 0), (DATES[4], 4), (DATES[1], -1), (DATES[2], 2), (DATES[2], 2)]
    assert compute_cumulative(series, 5, trading_dates=DATES, source_unit=unit) == Decimal(8 * multiplier)


@pytest.mark.parametrize("series", [[], [(DATES[0], 1)], [(d, None if i == 2 else i) for i, d in enumerate(DATES)], [(d, i) for i, d in enumerate(DATES[1:])]])
def test_short_missing_or_stale_window_is_not_a_five_day_total(series):
    assert compute_cumulative(series, 5, trading_dates=DATES, source_unit="CNY") is None


def test_calendar_and_unit_are_required():
    series = [(d, i) for i, d in enumerate(DATES)]
    assert compute_cumulative(series, 5, source_unit="CNY") is None
    assert compute_cumulative(series, 5, trading_dates=DATES) is None
    assert compute_cumulative(series, 10, trading_dates=DATES, source_unit="CNY") is None
    assert compute_cumulative(series + [(DATES[0], 99)], 5, trading_dates=DATES, source_unit="CNY") is None


def test_invalid_calendar_is_rejected():
    with pytest.raises(ValueError):
        compute_cumulative([("d1", 1)], 1, trading_dates=["d1"], source_unit="CNY")
