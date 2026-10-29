"""Compute capital amounts only from complete, dated, single-source windows.
Choice A-share CSD NETINFLOW is in yuan (see choice-quantapi-integration.md).
Legacy iFinD remains available only outside the Choice deployment profile.

Issue #64 最近完整交易日：三窗口共同截至最新已收盘交易日 T；仅当 T 缺行/空值
且截至 T-1（含 T-1）的连续 10 个应有交易日均有合格数值时，一并改用 T-1。
实际采用截至日随 ``as_of_date`` 贯穿 coverage 与字段资格记录。
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import logging
from time import monotonic
from typing import Any, Literal, Sequence

from icontract import ensure, require

logger = logging.getLogger(__name__)

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
        from data_provider.cross_source_validator import AnchorQuality, AnchorReading, SourceReadError, adopted_field_record
        adapter = ChoiceAdapter()
        end = adapter.completed_day()
        start = end - timedelta(days=max(60, days))
        deadline = deadline if deadline is not None else monotonic() + get_config().fundamental_fetch_timeout_seconds
        result: dict[str, Any] = {"status": "partial", "source": "choice", "currency": "CNY", "source_unit": "CNY",
                                  "main_net_inflow": None, "inflow_5d": None, "inflow_10d": None,
                                  "errors": [], "daily_series": [], "coverage": {}}
        try:
            expected = sorted(adapter.trading_dates(start, end, deadline=deadline), reverse=True)
            series = adapter.capital_series(code, start, end, deadline=deadline)
            result["daily_series"] = [{"date": d, "value": v} for d, v in series]
            result["expected_date"] = expected[0] if expected else None
            result["latest_date"] = max((d for d, v in series if v is not None), default=None)
            by_date = dict(series)
            as_of, offset = _select_window_end(expected, series, by_date)
            result["as_of_date"] = as_of
            for n, field in ((1, "main_net_inflow"), (5, "inflow_5d"), (10, "inflow_10d")):
                window = expected[offset:offset + n]
                missing = [d for d in window if by_date.get(d) is None]
                amount = compute_cumulative(series, n, trading_dates=expected[offset:], source_unit="CNY")
                complete = amount is not None
                result[field] = float(amount) if amount is not None else None
                result["coverage"][str(n)] = {"expected_dates": window, "missing_dates": missing, "complete": complete}
                if not complete:
                    result["errors"].append(f"choice:window_{n}_incomplete")
            if offset:
                # T 未就绪而采用 T-1：保留 T 未就绪证据并在说明中披露（issue #64）。
                result["coverage"]["pending_day"] = {"expected_date": expected[0], "observed": False}
                result["note"] = (
                    f"资金数据截至{as_of}（最近完整交易日）；{expected[0]}资金窗口尚未完整，"
                    f"单日及近5/10交易日均截至{as_of}。"
                )
            if result["main_net_inflow"] is not None:
                reading = AnchorReading("choice", result["main_net_inflow"], observed_at=as_of,
                                        unit="currency_base", currency="CNY", caliber="daily_main_net_inflow", raw_label="CSD NETINFLOW")
                result["field_meta"] = {"main_inflow": adopted_field_record("main_inflow", reading, AnchorQuality(status="single_source", reason_codes=("single_family",)))}
            else:
                result["field_meta"] = {"main_inflow": {"value": None, "rule_eligible": False, "quality": {"status": "missing", "reason_codes": ["window_incomplete"]}}}
            if not expected:
                result["errors"].append("choice:calendar_unavailable")
            result["status"] = "ok" if not result["errors"] else "partial"
        except SourceReadError as exc:
            result["status"] = "failed"
            result["errors"].append(str(exc))
        return result

    from data_provider.ifind_fundamental_adapter import IfindFetcher
    from data_provider.cross_source_validator import AnchorReading, adopted_field_record
    fetcher = IfindFetcher.get_instance()
    if not fetcher.available or (deadline is not None and deadline <= monotonic()):
        return {}
    from data_provider.cross_source_validator import SourceReadError
    try:
        series = fetcher.fetch_main_inflow_series(code, days, **({"deadline": deadline} if deadline is not None else {}))
    except SourceReadError as exc:
        logger.warning("iFinD capital flow unavailable: %s", exc)
        return {}
    if not series:
        return {}
    reading = AnchorReading("ifind", series[0][1], observed_at=series[0][0])
    return {"main_net_inflow": series[0][1], "inflow_5d": None, "inflow_10d": None,
            "daily_series": [{"date": d, "value": v} for d, v in series], "source": "ifind",
            "errors": ["ifind:window_calendar_and_unit_unconfirmed"],
            "field_meta": {"main_inflow": adopted_field_record("main_inflow", reading, selection_reason="fallback")}}


def _select_window_end(
    expected: Sequence[str], series: Sequence[tuple[str, float | None]], by_date: dict[str, Any]
) -> tuple[str | None, int]:
    """在同一次交易日历+CSD 响应内选择共同截至日（issue #64，零追加查询）。

    返回 (as_of_date, offset)：优先 T=expected[0]；仅当 T 缺行/空值且截至 T-1
    （含 T-1）的连续 10 个应有交易日共同完整（复用既有 ``compute_cumulative``
    合计层判断：全日期有有限值、无同日冲突）时改用 T-1（offset=1）。
    T 已有值但窗口中间缺数不通过回退掩盖；日历不足由合计层拒绝。非法数值
    到不了这里——Choice 响应解析层已按无效响应拒绝。
    """
    if not expected:
        return None, 0
    if by_date.get(expected[0]) is not None:
        return expected[0], 0
    if compute_cumulative(series, 10, trading_dates=expected[1:], source_unit="CNY") is not None:
        return expected[1], 1
    return expected[0], 0
