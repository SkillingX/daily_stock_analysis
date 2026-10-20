"""DB-first K-line history loader for Agent tools.

Provides:
- ContextVar-based frozen target_date propagation across threads
- ``load_history_df``: read from DB first, DataFetcherManager fallback

Fixes #1066 – eliminates 45+ redundant HTTP requests per stock in Agent mode.
"""
from __future__ import annotations

import contextvars
import logging
from datetime import date, datetime, time, timedelta
from threading import Lock
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, cast
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from src.deep_research_dims.context import SharedContext

import pandas as pd

logger = logging.getLogger(__name__)
_CACHE_MIN_RECORDS = 30

# ---------------------------------------------------------------------------
# Frozen target date (ContextVar) – set once per stock in pipeline, read by
# all agent tool threads via copy_context().run().
# ---------------------------------------------------------------------------
_frozen_target_date: contextvars.ContextVar[Optional[date]] = contextvars.ContextVar(
    "_frozen_target_date", default=None,
)


def set_frozen_target_date(d: date) -> contextvars.Token[Any]:
    return _frozen_target_date.set(d)


def get_frozen_target_date() -> Optional[date]:
    return _frozen_target_date.get()


def reset_frozen_target_date(token: contextvars.Token[Any]) -> None:
    _frozen_target_date.reset(token)


# ---------------------------------------------------------------------------
# Internal DataFetcherManager singleton (fallback only)
# ---------------------------------------------------------------------------
_fetcher_singleton = None
_fetcher_lock = Lock()


def _get_fetcher_manager():
    global _fetcher_singleton
    if _fetcher_singleton is None:
        with _fetcher_lock:
            if _fetcher_singleton is None:
                from data_provider import DataFetcherManager
                _fetcher_singleton = DataFetcherManager()
    return _fetcher_singleton


# ---------------------------------------------------------------------------
# DB-first history loader
# ---------------------------------------------------------------------------
def _history_code_candidates(stock_code: str) -> Tuple[List[str], str]:
    from data_provider.base import canonical_stock_code, normalize_stock_code

    raw_code = str(stock_code or "").strip()
    normalized_code = canonical_stock_code(normalize_stock_code(raw_code))
    candidates: List[str] = []
    for candidate in (canonical_stock_code(raw_code), normalized_code):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates, normalized_code


def _coerce_bar_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").date()
        except ValueError:
            return date.min
    if hasattr(value, "date"):
        try:
            coerced = value.date()
            return coerced if isinstance(coerced, date) else date.min
        except Exception:
            return date.min
    return date.min


def _bar_date(bar: Any) -> date:
    row_date = _coerce_bar_date(getattr(bar, "date", None))
    if row_date != date.min:
        return row_date
    if hasattr(bar, "to_dict"):
        try:
            return _coerce_bar_date((bar.to_dict() or {}).get("date"))
        except Exception:
            return date.min
    return date.min


def _select_best_bars(db, stock_code: str, start: date, end: date) -> Tuple[Optional[str], list[Any]]:
    candidates, normalized_code = _history_code_candidates(stock_code)
    best_code = None
    best_bars = []
    best_key = None

    for candidate in candidates:
        bars = list(db.get_data_range(candidate, start, end) or [])
        if not bars:
            continue
        latest_date = max(_bar_date(bar) for bar in bars)
        key = (latest_date, len(bars), candidate == normalized_code)
        if best_key is None or key > best_key:
            best_key = key
            best_code = candidate
            best_bars = bars

    return best_code, best_bars


def cache_session_date(
    stock_code: str,
    target_date: Optional[date] = None,
    now: Optional[datetime] = None,
) -> date:
    """Latest completed daily bar for this analysis point, not the calendar date."""
    from src.core.trading_calendar import (
        MARKET_TIMEZONE,
        get_effective_trading_date,
        get_market_for_stock,
    )

    market = get_market_for_stock(stock_code) or "cn"
    if now is not None:
        return get_effective_trading_date(market, current_time=now)
    if target_date is not None:
        tz = ZoneInfo(MARKET_TIMEZONE.get(market, "Asia/Shanghai"))
        end_of_day = datetime.combine(target_date, time(23, 59), tzinfo=tz)
        return get_effective_trading_date(market, current_time=end_of_day)
    return get_effective_trading_date(market)


def _history_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    records = cast(List[Dict[str, Any]], df.to_dict(orient="records"))
    for row in records:
        if "date" in row:
            row["date"] = str(row["date"])[:10]
    return records


def _trim_to_session(df: pd.DataFrame, required: date) -> pd.DataFrame:
    if df is None or df.empty or "date" not in getattr(df, "columns", []):
        return df
    dates = df["date"].map(_coerce_bar_date)
    if (dates <= required).all():
        return df
    return df.loc[dates <= required].copy()


def _publish_history(df: pd.DataFrame, source: str, required: date) -> None:
    """Publish an already session-trimmed frame; keeps only the freshness guard."""
    from src.core.research_run import current_research

    run = current_research.get()
    if run is None or df is None or df.empty:
        return
    records = _history_records(df)
    latest = max((str(row.get("date") or "")[:10] for row in records), default="")
    if latest != required.isoformat():
        return
    run.publish_daily_history(required.isoformat(), records, source)


def adopt_published_history(ctx: "SharedContext") -> bool:
    """Copy this run's completed session into an empty context. Never fetches."""
    from src.core.research_run import current_research

    run = current_research.get()
    if run is None or getattr(ctx, "history", None):
        return False
    slot = run.published_daily_history()
    if not slot:
        return False
    try:
        analyzed_at = datetime.fromisoformat(str(ctx.as_of))
    except ValueError:
        return False
    required = cache_session_date(ctx.stock_code, now=analyzed_at)
    if slot.get("as_of") != required.isoformat():
        return False
    ctx.history = [dict(row) for row in slot["records"]]
    return True


def load_history_df(
    stock_code: str,
    days: int = 60,
    target_date: Optional[date] = None,
) -> Tuple[Optional[pd.DataFrame], str]:
    """Load K-line history, DB first with DataFetcherManager fallback.

    Returns ``(df, source)`` where *source* is ``"db_cache"`` on DB hit or the
    actual provider name on network fallback.  Returns ``(None, "none")`` when
    both paths fail.
    """
    from src.storage import get_db

    if target_date is not None:
        required = cache_session_date(stock_code, target_date)
    elif (frozen := get_frozen_target_date()) is not None:
        required = cache_session_date(stock_code, frozen)
    else:
        required = cache_session_date(stock_code)
    start = required - timedelta(days=int(days * 1.8) + 10)
    required_records = max(min(days, _CACHE_MIN_RECORDS), 1)

    try:
        db = get_db()
        _code, bars = _select_best_bars(db, stock_code, start, required)
        usable = [bar for bar in bars if date.min < _bar_date(bar) <= required]
        latest_date = max((_bar_date(bar) for bar in usable), default=date.min)
        if usable and latest_date == required and len(usable) >= required_records:
            df = pd.DataFrame([bar.to_dict() for bar in usable])
            logger.debug(
                "load_history_df(%s): %d bars from DB through %s (requested %d)",
                stock_code, len(df), required.isoformat(), days,
            )
            _publish_history(df, "db_cache", required)
            return df, "db_cache"
    except Exception as e:
        logger.debug("load_history_df(%s): DB read failed: %s", stock_code, e)

    try:
        manager = _get_fetcher_manager()
        df, source = manager.get_daily_data(stock_code, days=days)
        if df is not None and not df.empty:
            trimmed = _trim_to_session(df, required)
            if trimmed is not None and not trimmed.empty:
                _publish_history(trimmed, source, required)
                return trimmed, source
            logger.warning(
                "load_history_df(%s): %s 返回 %d 行但均晚于已完成交易日 %s，本次不采用",
                stock_code, source, len(df), required.isoformat(),
            )
    except Exception as e:
        logger.warning("load_history_df(%s): DataFetcherManager failed: %s", stock_code, e)

    return None, "none"
