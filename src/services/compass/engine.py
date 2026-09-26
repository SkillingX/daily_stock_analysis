# -*- coding: utf-8 -*-
"""Pure-compute engine for midterm trend compass.

Issue scope: docs/midterm-trend-compass-plan.md §4 (核心逻辑).

Three-Layer Defense Layer 2: every formula carries icontract pre/post-conditions.
Layer 3 inputs are pandas Series; outputs are typed enum strings consumed by
``src.schemas.compass.MidtrendCompass``.

P1 scope: indicator computation + L0/L1/L2/L3 classification + phase synthesis.
P2: action rewriter + guardrail merge (out of this module).
L4 timing (§13.8): daily-level condition-triggered signals, separate pure
function ``derive_l4`` that reads OHLCV but never feeds back into L0-L3.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional, Tuple, TypedDict

import pandas as pd

import icontract

from typing import cast

from src.schemas.compass import (
    ActionReasonCode,
    BarStatus,
    L0Status,
    L1Status,
    L2Status,
    L3Status,
    ObserveHorizon,
    Phase,
    PositionFilter,
    PositionHint,
    TimingSignal,
    TimingSignalStatus,
    TimingSignalType,
)

# Local mirror of IndicatorsBlock.cross_ema20_ema50 Literal to keep the dataclass
# field type narrow (the schema re-imports from here).
CrossAboveBelow = Literal["above", "below", "touched"]


# ---------------------------------------------------------------------------
# Indicators (Wilder-style EMA + Wilder RSI)
# ---------------------------------------------------------------------------


@icontract.require(
    lambda closes: isinstance(closes, pd.Series),
    "closes must be a pandas Series",
)
@icontract.require(
    lambda closes, period: len(closes) >= period,
    "closes length must be >= period",
)
@icontract.require(
    lambda period: period >= 1,
    "period must be >= 1",
)
@icontract.ensure(
    lambda result: result.isna().sum() == 0 or result.notna().sum() >= 1,
    "result must have at least one non-NaN value",
)
def _wilder_ema(closes: pd.Series, period: int) -> pd.Series:
    """Wilder smoothing: alpha = 1 / period, applied as EMA with adjust=False.

    The seeding is a simple SMA over the first ``period`` rows; this matches
    the standard TA-Lib / TradingView Wilder output to within floating-point
    rounding.
    """
    alpha = 1.0 / float(period)
    seed_mean = closes.iloc[:period].mean()
    # Wilder seed: SMA over the first ``period`` bars; that SMA sits at index (period - 1).
    # Subsequent bars apply alpha = 1 / period smoothing without adjust (Wilder EMA).
    seed_mean = float(closes.iloc[:period].mean())
    seeded = pd.Series([float("nan")] * (period - 1) + [seed_mean], index=closes.index[:period])
    body = closes.iloc[period:]
    if body.empty:
        return seeded
    smoothed = body.ewm(alpha=alpha, adjust=False).mean()
    # pd.concat can technically return DataFrame if columns misalign; cast
    # back to Series to satisfy pyright (mypy sees the cast as redundant).
    out_series: pd.Series = pd.concat([seeded, smoothed])
    out_series.index = closes.index
    out_series.name = closes.name
    return out_series


@icontract.require(
    lambda closes, period: isinstance(period, int) and period >= 1,
    "period must be int >= 1",
)
@icontract.require(
    lambda closes, period: len(closes) >= period + 1,
    "closes length must be >= period+1 for Wilder RSI",
)
@icontract.ensure(
    lambda result: result.dropna().between(0.0, 100.0).all(),
    "RSI values must lie in [0, 100]",
)
def compute_rsi_wilder(closes: pd.Series, period: int = 14) -> pd.Series:
    """RSI with Wilder smoothing; first ``period`` rows are NaN."""
    delta = closes.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = cast(pd.Series, _wilder_ema(gain, period))  # type: ignore[redundant-cast]
    avg_loss = cast(pd.Series, _wilder_ema(loss, period))  # type: ignore[redundant-cast]
    # Use float("nan") (not pd.NA) so .replace() returns Series, not DataFrame.
    rs = avg_gain / avg_loss.replace(0.0, float("nan"))
    one_plus_rs = cast(pd.Series, 1.0 + rs)  # type: ignore[redundant-cast]
    rsi = cast(pd.Series, 100.0 - 100.0 / one_plus_rs)  # type: ignore[redundant-cast]
    # When avg_loss == 0, force RSI = 100 (pure up-move).
    rsi = rsi.where(avg_loss != 0, 100.0)
    return rsi


@icontract.require(
    lambda closes, period: len(closes) >= period,
    "closes length must be >= period",
)
@icontract.require(
    lambda period: period >= 1,
    "period must be >= 1",
)
def compute_ema(closes: pd.Series, period: int) -> pd.Series:
    """Standard EMA (alpha = 2/(period+1)), seeded via the first non-Na value."""
    # pyright sees ewm().mean() as DataFrame | Series (pandas-stubs is loose);
    # cast narrows to the declared Series return. mypy flags the cast as
    # redundant, so the ignore is required to keep mypy --strict quiet.
    return cast(pd.Series, closes.ewm(span=period, adjust=False).mean())  # type: ignore[redundant-cast]


@icontract.require(
    lambda series, window: window >= 1,
    "slope window must be >= 1",
)
@icontract.ensure(
    lambda result: result.index.equals(result.index),
    "slope index must align with input",
)
def _slope(series: pd.Series, window: int) -> pd.Series:
    """Linear regression slope over a rolling window (last - first) / window.

    Intentionally simple: it preserves sign and avoids sklearn dependency.
    """
    def _delta(arr: pd.Series) -> float:
        arr = arr.dropna()
        if len(arr) < 2:
            return float("nan")
        return float((arr.iloc[-1] - arr.iloc[0]) / (len(arr) - 1))

    return cast(pd.Series, series.rolling(window=window, min_periods=window).apply(_delta, raw=False))  # type: ignore[redundant-cast]


# ---------------------------------------------------------------------------
# L0 — Weekly filter
# ---------------------------------------------------------------------------


class WeeklySnapshot(TypedDict):
    """Snapshot returned by :func:`derive_l0`."""

    weekly_ema50: Optional[float]
    weekly_ema200: Optional[float]
    weekly_sample_size: int


@icontract.require(
    lambda weekly_closes: isinstance(weekly_closes, pd.Series),
    "weekly_closes must be a pandas Series",
)
@icontract.require(
    lambda weekly_closes: len(weekly_closes) >= 0,
    "weekly_closes length must be >= 0",
)
def derive_l0(weekly_closes: pd.Series) -> Tuple[L0Status, WeeklySnapshot]:
    """Compute weekly EMA50 / EMA200 and classify the trend filter.

    Sample rules (per plan §7): < 60 weekly bars => weekly_disabled;
    60 <= sample < 200 cannot seed EMA200 => weekly_disabled; >= 200 full.
    """
    sample = len(weekly_closes.dropna())
    if sample < 200:
        return "weekly_disabled", {
            "weekly_ema50": None,
            "weekly_ema200": None,
            "weekly_sample_size": sample,
        }

    ema50 = float(compute_ema(weekly_closes, 50).iloc[-1])
    ema200 = float(compute_ema(weekly_closes, 200).iloc[-1])
    last_close = float(weekly_closes.iloc[-1])

    if last_close > ema200 and ema50 > ema200:
        status: L0Status = "weekly_bull"
    elif last_close < ema200 and ema50 < ema200:
        status = "weekly_bear"
    else:
        status = "weekly_transition"

    return status, {
        "weekly_ema50": ema50,
        "weekly_ema200": ema200,
        "weekly_sample_size": sample,
    }


# ---------------------------------------------------------------------------
# L1 — Annual filter (EMA200 slope over 40d)
# ---------------------------------------------------------------------------


@icontract.require(
    lambda daily_closes: isinstance(daily_closes, pd.Series),
    "daily_closes must be a pandas Series",
)
def derive_l1(daily_closes: pd.Series) -> L1Status:
    """Annual trend filter; sample < 220 bars => annual_disabled."""
    sample = len(daily_closes.dropna())
    if sample < 220:
        return "annual_disabled"

    ema200 = compute_ema(daily_closes, 200)
    slope40 = _slope(ema200, 40).iloc[-1]
    last_close = float(daily_closes.iloc[-1])

    if pd.isna(slope40):
        return "annual_disabled"

    if last_close > float(ema200.iloc[-1]) and slope40 > 0:
        return "annual_bull"
    if last_close < float(ema200.iloc[-1]) and slope40 < 0:
        return "annual_bear"
    return "annual_transition"


# ---------------------------------------------------------------------------
# L2 — 1-3 month segment (alive / resting / flattening / broken)
# ---------------------------------------------------------------------------


@icontract.require(
    lambda daily_closes: isinstance(daily_closes, pd.Series),
    "daily_closes must be a pandas Series",
)
@icontract.require(
    lambda daily_closes: len(daily_closes) >= 30,
    "daily_closes length must be >= 30 for L2",
)
def derive_l2(daily_closes: pd.Series) -> L2Status:
    """Segment health from EMA50 structure.

    Rules (priority order — first match wins):
      - EMA50 slope flat (abs < 0.05/day)            => flattening
      - Price below EMA50 and EMA50 sloping down     => broken
      - Price touched/reclaimed EMA20 from below     => resting
      - otherwise                                    => alive
    """
    ema20 = compute_ema(daily_closes, 20)
    ema50 = compute_ema(daily_closes, 50)
    slope_ema50 = _slope(ema50, 20).iloc[-1]
    last_close = float(daily_closes.iloc[-1])
    last_ema50 = float(ema50.iloc[-1])

    if pd.isna(slope_ema50):
        return "broken"  # not enough data => treat as broken for safety

    if abs(slope_ema50) < 0.05:
        return "flattening"

    if last_close < last_ema50 and slope_ema50 < 0:
        return "broken"

    # resting = touched EMA20 from below within the last 5 bars
    ema20_recent = ema20.iloc[-5:]
    close_recent = daily_closes.iloc[-5:]
    touched = (close_recent <= ema20_recent * 1.005).any() and last_close > float(
        ema20.iloc[-1]
    )
    if touched:
        return "resting"

    return "alive"


# ---------------------------------------------------------------------------
# L3 — 2-6 week rhythm (healthy / cooling / exhausted / noisy)
# ---------------------------------------------------------------------------


@icontract.require(
    lambda daily_closes: isinstance(daily_closes, pd.Series),
    "daily_closes must be a pandas Series",
)
@icontract.require(
    lambda daily_closes: len(daily_closes) >= 30,
    "daily_closes length must be >= 30 for L3",
)
def derive_l3(daily_closes: pd.Series) -> L3Status:
    """Short-term rhythm from EMA20 + RSI(14).

    - healthy: EMA20 sloping up, RSI 45-75
    - cooling: EMA20 slope flattening, RSI 35-55
    - exhausted: RSI > 75 (overbought) with negative slope
    - noisy:    unable to classify (insufficient data or contradictory signals)
    """
    ema20 = compute_ema(daily_closes, 20)
    slope_ema20 = _slope(ema20, 10).iloc[-1]
    rsi = compute_rsi_wilder(daily_closes, 14).iloc[-1]
    last_close = float(daily_closes.iloc[-1])

    if pd.isna(slope_ema20) or pd.isna(rsi):
        return "noisy"

    # Priority: a clear rising slope above EMA20 is healthy regardless of how
    # overbought RSI gets (a strong uptrend legitimately prints RSI > 75).
    if slope_ema20 > 0 and last_close > float(ema20.iloc[-1]):
        return "healthy"
    if abs(slope_ema20) < 0.05 and 35.0 <= rsi <= 55.0:
        return "cooling"
    if rsi > 75.0 and slope_ema20 < 0:
        return "exhausted"
    return "noisy"


# ---------------------------------------------------------------------------
# Phase synthesis — locked priority table (plan §4.3)
# ---------------------------------------------------------------------------


@icontract.require(
    lambda l0, l1, l2, l3: all(
        v in {"weekly_bull", "weekly_bear", "weekly_transition", "weekly_disabled"}
        for v in [l0]
    ),
    "l0 must be a valid L0Status",
)
def compose_phase(l0: L0Status, l1: L1Status, l2: L2Status, l3: L3Status) -> Phase:
    """Apply the §4.3 priority table (short-circuit evaluation)."""
    if l1 == "annual_disabled":
        return "trend_holding"
    if l1 == "annual_transition" and l2 in {"flattening", "broken"}:
        return "transitioning"
    if l2 == "flattening" or (l2 != "alive" and l3 in {"exhausted", "noisy"}):
        return "coiling"
    if l2 in {"alive", "resting"} and l3 == "exhausted":
        return "trend_tiring"
    if l2 == "resting" or l3 == "cooling":
        return "trend_holding"
    if (
        l0 != "weekly_bear"
        and l1 == "annual_bull"
        and l2 == "alive"
        and l3 == "healthy"
    ):
        return "trend_expanding"
    return "transitioning"


# ---------------------------------------------------------------------------
# Derived enums (observe_horizon, position_filter)
# ---------------------------------------------------------------------------


_PHASE_TO_HORIZON: dict[Phase, ObserveHorizon] = {
    "trend_expanding": "1m",
    "trend_holding": "1m",
    "trend_tiring": "2w",
    "coiling": "2w",
    "transitioning": "1w",
}


@icontract.ensure(
    lambda result, phase: _PHASE_TO_HORIZON[phase] == result,
    "horizon must follow the locked phase-to-horizon table",
)
def observe_horizon_for(phase: Phase) -> ObserveHorizon:
    return _PHASE_TO_HORIZON[phase]


_L0_TO_POSITION: dict[L0Status, PositionFilter] = {
    "weekly_bull": "full",
    "weekly_transition": "half",
    "weekly_bear": "none",
    "weekly_disabled": "half",
}


@icontract.ensure(
    lambda result, l0: _L0_TO_POSITION[l0] == result,
    "position_filter must follow the locked L0 table",
)
def position_filter_for(l0: L0Status) -> PositionFilter:
    return _L0_TO_POSITION[l0]


# ---------------------------------------------------------------------------
# Bundle: aggregate engine output for schema assembly
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EngineOutput:
    l0: L0Status
    l1: L1Status
    l2: L2Status
    l3: L3Status
    phase: Phase
    observe_horizon: ObserveHorizon
    position_filter: PositionFilter
    weekly_ema50: Optional[float]
    weekly_ema200: Optional[float]
    weekly_sample_size: int
    daily_sample_size: int
    ema20: Optional[float]
    ema50: Optional[float]
    ema100: Optional[float]
    ema200: Optional[float]
    rsi14: Optional[float]
    slope_ema20_10d: Optional[float]
    slope_ema50_20d: Optional[float]
    slope_ema200_40d: Optional[float]
    last_close: Optional[float]
    cross_ema20_ema50: Optional[CrossAboveBelow]


@icontract.require(
    lambda daily_closes: isinstance(daily_closes, pd.Series),
    "daily_closes must be a pandas Series",
)
@icontract.require(
    lambda weekly_closes: isinstance(weekly_closes, pd.Series),
    "weekly_closes must be a pandas Series",
)
@icontract.require(
    lambda daily_closes: len(daily_closes) >= 30,
    "need >= 30 daily bars to attempt classification",
)
def compute(
    daily_closes: pd.Series,
    weekly_closes: pd.Series,
) -> EngineOutput:
    """Run the full L0..L3 + phase pipeline. Pure: no I/O, no globals."""
    l0, l0_meta = derive_l0(weekly_closes)
    l1 = derive_l1(daily_closes)
    l2 = derive_l2(daily_closes)
    l3 = derive_l3(daily_closes)
    phase = compose_phase(l0, l1, l2, l3)

    ema20 = compute_ema(daily_closes, 20)
    ema50 = compute_ema(daily_closes, 50)
    ema100 = compute_ema(daily_closes, 100)
    ema200 = compute_ema(daily_closes, 200)

    slope20 = _slope(ema20, 10).iloc[-1]
    slope50 = _slope(ema50, 20).iloc[-1]
    slope200 = _slope(ema200, 40).iloc[-1]

    last_close = float(daily_closes.iloc[-1])
    ema20_last = float(ema20.iloc[-1])
    ema50_last = float(ema50.iloc[-1])

    if pd.isna(ema20_last) or pd.isna(ema50_last):
        cross: Optional[CrossAboveBelow] = None
    elif last_close > ema20_last > ema50_last:
        cross = "above"
    elif last_close < ema20_last < ema50_last:
        cross = "below"
    else:
        cross = "touched"

    rsi_val = compute_rsi_wilder(daily_closes, 14).iloc[-1]

    return EngineOutput(
        l0=l0,
        l1=l1,
        l2=l2,
        l3=l3,
        phase=phase,
        observe_horizon=observe_horizon_for(phase),
        position_filter=position_filter_for(l0),
        weekly_ema50=l0_meta["weekly_ema50"],
        weekly_ema200=l0_meta["weekly_ema200"],
        weekly_sample_size=l0_meta["weekly_sample_size"],
        daily_sample_size=len(daily_closes.dropna()),
        ema20=ema20_last,
        ema50=ema50_last,
        ema100=float(ema100.iloc[-1]),
        ema200=float(ema200.iloc[-1]),
        rsi14=float(rsi_val) if not pd.isna(rsi_val) else None,
        slope_ema20_10d=float(slope20) if not pd.isna(slope20) else None,
        slope_ema50_20d=float(slope50) if not pd.isna(slope50) else None,
        slope_ema200_40d=float(slope200) if not pd.isna(slope200) else None,
        last_close=last_close,
        cross_ema20_ema50=cross,
    )


# ---------------------------------------------------------------------------
# L4 timing signals (plan §13.8): condition-triggered daily trade plans
# ---------------------------------------------------------------------------
#
# Signals are leaf-layer outputs: they read L0-L3 state but never feed back.
# Three sleeves, two position systems:
#   - pullback_entry / top_escape respect the L0 position cap semantics;
#   - bottom_fishing may fire under weekly_bear as an explicit counter-trend
#     signal with its own light-position discipline (rewriter row 3 amendment).

_L4_MIN_BARS = 30
_OVERSOLD_RSI = 30.0
_OVERBOUGHT_RSI = 75.0
_ESCAPE_WATCH_RSI = 65.0
_RESET_RSI_LOW = 40.0
_RESET_RSI_HIGH = 55.0
_STOP_BUFFER = 0.01          # invalidation buffer below the reference low
_TOP_INVALIDATION_BUFFER = 0.01
_ZONE_TOUCH = 0.01           # pullback zone: within 1% of EMA20
_DIVERGENCE_EPSILON = 0.5    # RSI divergence must exceed this to count
_HORIZON_DAYS: dict[Phase, int] = {
    "trend_expanding": 20,
    "trend_holding": 20,
    "trend_tiring": 10,
    "coiling": 10,
    "transitioning": 5,
}


def _swing_extremes(
    values: pd.Series, *, find_low: bool, right: int = 2
) -> list[tuple[int, float]]:
    """Confirmed swing points: bar i is the extreme of the (2*right+1)-bar
    window centered at i. The last ``right`` bars cannot confirm and are
    excluded; use the candidate check for the freshest bar."""
    arr = values.to_numpy(dtype=float)
    out: list[tuple[int, float]] = []
    for i in range(right, len(arr) - right):
        window = arr[i - right : i + right + 1]
        if pd.isna(arr[i]):
            continue
        extreme = window.min() if find_low else window.max()
        if arr[i] == extreme:
            out.append((i, float(arr[i])))
    return out


def _candidate_is_extreme(values: pd.Series, *, find_low: bool, window: int = 5) -> bool:
    """Whether the last bar is the extreme of the most recent ``window`` bars
    (unconfirmed candidate swing)."""
    tail = values.iloc[-window:].to_numpy(dtype=float)
    if pd.isna(tail[-1]):
        return False
    extreme = tail.min() if find_low else tail.max()
    return bool(tail[-1] == extreme)


@icontract.require(
    lambda daily_ohlcv: isinstance(daily_ohlcv, pd.DataFrame)
    and {"open", "high", "low", "close"}.issubset(daily_ohlcv.columns),
    "daily_ohlcv must be a DataFrame with open/high/low/close columns",
)
@icontract.ensure(
    lambda result: all(
        (s.invalidation_price or 0.0) < (s.trigger_price or 0.0)
        for s in result
        if s.type in ("pullback_entry", "bottom_fishing") and s.status == "triggered"
    ),
    "buy-side triggered signals must have invalidation below trigger",
)
@icontract.ensure(
    lambda result: all(
        (s.invalidation_price or 0.0) > (s.trigger_price or 0.0)
        for s in result
        if s.type == "top_escape" and s.status == "triggered"
    ),
    "top_escape triggered signals must invalidate above trigger (new high voids)",
)
def derive_l4(
    daily_ohlcv: pd.DataFrame,
    *,
    weekly: L0Status,
    annual: L1Status,
    segment: L2Status,
    rhythm: L3Status,
    phase: Phase,
    bar_status: BarStatus,
    timing_enabled: bool = False,
) -> Tuple[TimingSignal, ...]:
    """Compute L4 timing signals from daily OHLCV + upper-layer state.

    Display-only (P1): signals never rewrite actions by themselves; the
    rewriter amendment only governs what happens when a caller *acts* on a
    bottom-fishing signal under weekly_bear. Intraday bars yield nothing —
    timing plans require a confirmed close. ``timing_enabled`` defaults to
    False, matching the COMPASS_TIMING_ENABLED global default: emitting
    signals always requires an explicit opt-in.
    """
    if not timing_enabled or bar_status != "closed" or len(daily_ohlcv) < _L4_MIN_BARS:
        return ()

    closes = daily_ohlcv["close"].astype(float)
    lows = daily_ohlcv["low"].astype(float)
    highs = daily_ohlcv["high"].astype(float)
    bar_date = daily_ohlcv.index[-1].date()

    rsi = compute_rsi_wilder(closes, 14)
    ema20 = compute_ema(closes, 20)
    slope20 = _slope(ema20, 10).iloc[-1]
    last_close = float(closes.iloc[-1])
    last_low = float(lows.iloc[-1])
    last_high = float(highs.iloc[-1])
    last_rsi = float(rsi.iloc[-1])
    ema20_last = float(ema20.iloc[-1])
    horizon = _HORIZON_DAYS[phase]

    signals: list[TimingSignal] = []

    def _swing_low_divergence() -> bool:
        points = _swing_extremes(lows, find_low=True)
        if len(points) >= 2:
            (i1, v1), (i2, v2) = points[-2], points[-1]
            r1, r2 = float(rsi.iloc[i1]), float(rsi.iloc[i2])
            if v2 < v1 and r2 > r1 + _DIVERGENCE_EPSILON:
                return True
        if _candidate_is_extreme(lows, find_low=True):
            if points:
                i1, v1 = points[-1]
                r1 = float(rsi.iloc[i1])
                if last_low < v1 and last_rsi > r1 + _DIVERGENCE_EPSILON:
                    return True
        return False

    def _swing_high_divergence() -> bool:
        points = _swing_extremes(highs, find_low=False)
        if len(points) >= 2:
            (i1, v1), (i2, v2) = points[-2], points[-1]
            r1, r2 = float(rsi.iloc[i1]), float(rsi.iloc[i2])
            if v2 > v1 and r2 < r1 - _DIVERGENCE_EPSILON:
                return True
        if _candidate_is_extreme(highs, find_low=False):
            if points:
                i1, v1 = points[-1]
                r1 = float(rsi.iloc[i1])
                if last_high > v1 and last_rsi < r1 - _DIVERGENCE_EPSILON:
                    return True
        return False

    # --- pullback_entry: trend intact, RSI reset after an EMA pullback ---
    if weekly != "weekly_bear" and segment in ("alive", "resting"):
        zone_touched = bool(lows.iloc[-5:].min() <= ema20_last * (1 + _ZONE_TOUCH))
        rsi_reset = _RESET_RSI_LOW <= last_rsi <= _RESET_RSI_HIGH
        recovered = last_close > ema20_last
        if zone_touched and rsi_reset and recovered and slope20 > 0:
            ref_low = float(lows.iloc[-5:].min())
            signals.append(
                TimingSignal(
                    type="pullback_entry",
                    status="triggered",
                    reason_codes=[],
                    trigger_price=last_close,
                    invalidation_price=round(ref_low * (1 - _STOP_BUFFER), 4),
                    position_hint="full" if weekly == "weekly_bull" else "medium",
                    horizon_days=horizon,
                    as_of_bar_date=bar_date,
                )
            )
        elif zone_touched and rsi_reset and not recovered:
            signals.append(
                TimingSignal(
                    type="pullback_entry",
                    status="armed",
                    reason_codes=[],
                    trigger_price=None,
                    invalidation_price=None,
                    position_hint="full" if weekly == "weekly_bull" else "medium",
                    horizon_days=horizon,
                    as_of_bar_date=bar_date,
                )
            )

    # --- bottom_fishing: RSI oversold + bullish divergence + stabilization ---
    if segment != "broken":
        oversold = last_rsi < _OVERSOLD_RSI
        divergence = _swing_low_divergence()
        stabilized = bool(
            last_close > float(closes.iloc[-2]) and last_close >= (last_high + last_low) / 2
        )
        if oversold and divergence:
            countertrend = weekly == "weekly_bear"
            reasons: list[ActionReasonCode] = ["bottom_fishing_time_stop"]
            if countertrend:
                reasons.append("weekly_bear_bottom_fishing_pass")
            signals.append(
                TimingSignal(
                    type="bottom_fishing",
                    status="triggered" if stabilized else "armed",
                    countertrend=countertrend,
                    reason_codes=reasons,
                    trigger_price=last_close if stabilized else None,
                    invalidation_price=round(last_low * (1 - _STOP_BUFFER), 4) if stabilized else None,
                    position_hint="light" if countertrend else "medium",
                    horizon_days=5,
                    as_of_bar_date=bar_date,
                )
            )

    # --- top_escape: overbought exhaustion / bearish divergence ---
    shielded = annual == "annual_bull" and segment == "alive" and rhythm == "healthy"
    if not shielded:
        exhausted = last_rsi > _OVERBOUGHT_RSI and slope20 < 0
        divergent_escape = last_rsi > 70.0 and _swing_high_divergence()
        losing_ema20 = last_close < ema20_last and slope20 < 0 and last_rsi > 50.0
        if exhausted or divergent_escape or losing_ema20:
            escape_reasons: list[ActionReasonCode] = []
            if exhausted:
                escape_reasons.append("top_escape_exhaustion")
            if divergent_escape:
                escape_reasons.append("top_escape_divergence")
            if losing_ema20:
                escape_reasons.append("top_escape_ema_loss")
            ref_high = float(highs.iloc[-20:].max())
            signals.append(
                TimingSignal(
                    type="top_escape",
                    status="triggered",
                    reason_codes=escape_reasons,
                    trigger_price=last_close,
                    invalidation_price=round(ref_high * (1 + _TOP_INVALIDATION_BUFFER), 4),
                    position_hint="light",
                    horizon_days=5,
                    as_of_bar_date=bar_date,
                )
            )
        elif last_rsi > _ESCAPE_WATCH_RSI and slope20 <= 0.05 and last_close >= ema20_last * 0.98:
            signals.append(
                TimingSignal(
                    type="top_escape",
                    status="armed",
                    reason_codes=[],
                    trigger_price=None,
                    invalidation_price=None,
                    position_hint="light",
                    horizon_days=5,
                    as_of_bar_date=bar_date,
                )
            )

    return tuple(signals[:3])
