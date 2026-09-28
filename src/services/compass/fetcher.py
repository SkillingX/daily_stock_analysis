# -*- coding: utf-8 -*-
"""Data access layer for the midterm trend compass.

Issue scope: docs/midterm-trend-compass-plan.md §4.1, §7.

P1 strategy: weekly closes are derived from daily via ``W-FRI`` resample rather
than introducing a new data source. This keeps ``data_provider/`` untouched
(per ``LOOP_CONSTRAINTS.md`` denylist) and still satisfies the "≥ 60 weekly
bars" rule from the plan: 60 weekly bars ≈ 1.2 years of daily data.

The default window is 1200 trading days (~5 years) so the derived weekly
series reaches the ≥ 200 weekly bars required by ``engine.derive_l0`` to seed
the weekly EMA200; 600 days only yields ~170 weekly bars and permanently
disables the L0 filter.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional, Tuple

import pandas as pd
import icontract

from data_provider import DataFetcherManager


@icontract.require(
    lambda code: isinstance(code, str) and len(code) >= 4,
    "code must be a non-empty stock code string",
)
@icontract.require(
    lambda days: days >= 30,
    "days must be >= 30 (minimum for indicator stability)",
)
def fetch_daily_closes(
    manager: DataFetcherManager,
    code: str,
    *,
    days: int = 1200,
    end_date: Optional[str] = None,
) -> Tuple[pd.Series, str]:
    """Fetch qfq daily closes via the existing DataFetcherManager.

    Returns a ``pd.Series`` indexed by ``DatetimeIndex`` (sorted ascending, no
    duplicates) and the source name that served the request.

    Raises ``DataFetchError`` (re-raised) when every fetcher fails.
    """
    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")
    start_dt = datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=days * 2)
    start_date = start_dt.strftime("%Y-%m-%d")

    df, source = manager.get_daily_data(
        stock_code=code, start_date=start_date, end_date=end_date, days=days,
    )

    if df is None or df.empty or "close" not in df.columns:
        raise ValueError(f"daily data for {code} missing or empty (source={source})")

    closes = (
        df.set_index(pd.to_datetime(df["date"]))["close"]
        .astype(float)
        .sort_index()
        .loc[lambda s: ~s.index.duplicated(keep="last")]
        .rename("close")
    )
    return closes, source


def derive_weekly_closes(daily_closes: pd.Series) -> pd.Series:
    """Resample daily closes to weekly (Friday close). Pure function.

    Contract (soft): if ``daily_closes`` is non-empty, its index must be a
    ``DatetimeIndex`` — empty inputs short-circuit because pandas cannot infer
    a frequency from no samples.
    """
    if daily_closes.empty:
        return daily_closes.copy()
    if not isinstance(daily_closes.index, pd.DatetimeIndex):
        raise ValueError("daily_closes must be indexed by DatetimeIndex")
    weekly = (
        daily_closes.resample("W-FRI", label="right", closed="right").last().dropna()
    )
    weekly.name = "weekly_close"
    return weekly


@icontract.require(
    lambda manager: manager is not None,
    "manager must be a DataFetcherManager instance",
)
def fetch_daily_ohlcv(
    manager: DataFetcherManager,
    code: str,
    *,
    days: int = 1200,
    end_date: Optional[str] = None,
) -> Tuple[pd.DataFrame, str]:
    """Fetch qfq daily OHLCV (L4 timing input). Same window policy as
    ``fetch_daily_closes``; columns: open/high/low/close/volume indexed by
    ascending DatetimeIndex, deduplicated."""
    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")
    start_dt = datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=days * 2)
    start_date = start_dt.strftime("%Y-%m-%d")

    df, source = manager.get_daily_data(
        stock_code=code, start_date=start_date, end_date=end_date, days=days,
    )
    required = {"open", "high", "low", "close"}
    if df is None or df.empty or not required.issubset(df.columns):
        raise ValueError(f"daily OHLCV data for {code} missing required columns (source={source})")

    df_indexed = df.set_index(pd.to_datetime(df["date"])).sort_index()
    ohlcv = df_indexed.loc[~df_indexed.index.duplicated(keep="last")]
    for col in ("open", "high", "low", "close", "volume"):
        if col in ohlcv.columns:
            ohlcv[col] = ohlcv[col].astype(float)
    return ohlcv, source


@icontract.require(
    lambda manager: manager is not None,
    "manager must be a DataFetcherManager instance",
)
def fetch_for_compass(
    manager: DataFetcherManager,
    code: str,
    *,
    days: int = 1200,
    end_date: Optional[str] = None,
) -> Tuple[pd.Series, pd.Series, str]:
    """Convenience: fetch daily + derive weekly in one call."""
    daily, source = fetch_daily_closes(
        manager, code, days=days, end_date=end_date,
    )
    weekly = derive_weekly_closes(daily)
    return daily, weekly, source
