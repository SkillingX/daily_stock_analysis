"""Compute capital amounts only from complete, dated, single-source windows.
Choice CSD NETINFLOW units remain unconfirmed: expose coverage, not amounts.
Legacy iFinD remains available only outside the Choice deployment profile.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from time import monotonic
from typing import Any, Literal, Sequence

from icontract import ensure, require

_WINDOW_5D = 5
_WINDOW_10D = 10
_DEFAULT_FETCH_DAYS = 12


@require(lambda n: n > 0)
@ensure(lambda result, trading_dates, n, source_unit: result is None or (source_unit is not None and trading_dates is not None and len(set(trading_dates)) >= n))
def compute_cumulative(
    series: Sequence[tuple[str, float | None]] | None, n: int, *,
    trading_dates: Sequence[str] | None = None,
    source_unit: Literal["CNY", "CNY_10K"] | None = None,
) -> Decimal | None:
    """Return yuan only when every expected trading date has one finite value."""
    if not trading_dates or source_unit is None:
        return None
    expected = sorted({date.fromisoformat(d).isoformat() for d in trading_dates}, reverse=True)[:n]
    if len(expected) < n:
        return None
    values: dict[str, Decimal | None] = {}
    for day, raw in series or ():
        day = date.fromisoformat(day).isoformat()
        value = Decimal(str(raw)) if raw is not None else None
        if value is not None and not value.is_finite():
            value = None
        if day in values and values[day] != value:
            return None
        values[day] = value
    if any(values.get(day) is None for day in expected):
        return None
    total = sum((value for day in expected if (value := values[day]) is not None), Decimal(0))
    return total * (10000 if source_unit == "CNY_10K" else 1)


def get_main_inflow_cumulative(
    code: str, days: int = _DEFAULT_FETCH_DAYS, *, deadline: float | None = None,
) -> dict[str, Any]:
    from src.config import get_config
    if get_config().data_source_profile == "choice":
        from data_provider.choice_adapter import ChoiceAdapter
        from data_provider.cross_source_validator import SourceReadError
        adapter = ChoiceAdapter()
        end = date.today()
        start = end - timedelta(days=max(60, days))
        deadline = deadline if deadline is not None else monotonic() + get_config().fundamental_fetch_timeout_seconds
        result: dict[str, Any] = {"status": "partial", "source": "choice", "currency": "CNY", "source_unit": None,
                                  "main_net_inflow": None, "inflow_5d": None, "inflow_10d": None,
                                  "errors": ["choice:netinflow_unit_unconfirmed"], "daily_series": [], "coverage": {}}
        try:
            expected = sorted(adapter.trading_dates(start, end, deadline=deadline), reverse=True)
            series = adapter.capital_series(code, start, end, deadline=deadline)
            result["daily_series"] = [{"date": d, "value": v} for d, v in series]
            result["expected_date"] = expected[0] if expected else None
            result["latest_date"] = max((d for d, v in series if v is not None), default=None)
            by_date = dict(series)
            for n in (1, 5, 10):
                window = expected[:n]
                missing = [d for d in window if by_date.get(d) is None]
                complete = len(window) == n and not missing
                result["coverage"][str(n)] = {"expected_dates": window, "missing_dates": missing, "complete": complete}
                if not complete:
                    result["errors"].append(f"choice:window_{n}_incomplete")
            latest = result["latest_date"]
            result["field_meta"] = {"main_inflow": {"source": "choice", "source_family": "eastmoney", "observed_at": latest,
                                    "currency": "CNY", "unit": None, "value": None, "rule_eligible": False,
                                    "quality": {"status": "missing", "reason_codes": ["unit_unknown"]}}}
            if not expected:
                result["errors"].append("choice:calendar_unavailable")
        except SourceReadError as exc:
            result["status"] = "failed"
            result["errors"].append(str(exc))
        return result

    from data_provider.ifind_fundamental_adapter import IfindFetcher
    from data_provider.cross_source_validator import AnchorReading, adopted_field_record
    fetcher = IfindFetcher.get_instance()
    if not fetcher.available or (deadline is not None and deadline <= monotonic()):
        return {}
    series = fetcher.fetch_main_inflow_series(code, days, **({"deadline": deadline} if deadline is not None else {}))
    if not series:
        return {}
    reading = AnchorReading("ifind", series[0][1], observed_at=series[0][0])
    return {"main_net_inflow": series[0][1], "inflow_5d": None, "inflow_10d": None,
            "daily_series": [{"date": d, "value": v} for d, v in series], "source": "ifind",
            "errors": ["ifind:window_calendar_and_unit_unconfirmed"],
            "field_meta": {"main_inflow": adopted_field_record("main_inflow", reading, selection_reason="fallback")}}
