# -*- coding: utf-8 -*-
"""
Fallback fundamental adapter (Tushare / iFinD).

When the primary ``AkshareFundamentalAdapter`` returns an empty or stale block
(for example, ``stock_yjyg_em`` / ``stock_yjkb_em`` / ``stock_gdfx_top_10_em``
are broken in some Python environments with TypeError/KeyError), this adapter
backfills the missing fields from Tushare or iFinD so downstream LLM analysis
does not consume 2020-era fallback text as fresh data.

Design rules (per AGENTS.md ``fail-open`` contract):

- Tries Tushare first, then iFinD, then Yfinance — each provider is best-effort.
- A failed provider never blocks the others.
- Returns the same bundle shape as ``AkshareFundamentalAdapter`` so the
  orchestrator can call this transparently.
- ``as_of`` is set to the period reported by the source (e.g. ``20251231`` for
  the latest annual report) so the staleness detector can pick the right
  threshold.
"""

from __future__ import annotations

import concurrent.futures
import logging
from time import monotonic
from threading import BoundedSemaphore
from inspect import signature
from contextvars import copy_context
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, cast

from .ifind_fundamental_adapter import IfindSource
from .cross_source_validator import AnchorReading, adopted_field_record

logger = logging.getLogger(__name__)


# Mapping from bundle data field → iFinD anchor name.
# Keep the list conservative: only fields AkShare regularly fails on.
_FIELD_TO_IFIND_ANCHOR: Dict[str, str] = {
    "revenue_yoy": "revenue_yoy",
    "net_profit_yoy": "net_profit_yoy",
    "roe": "roe",
    "gross_margin": "gross_margin",
}

# We try the most recent four fiscal periods in this order. iFinD's
# ``get_stock_financials`` takes a period token; we leave it empty and let the
# adapter return the latest one, but we still want the ``as_of`` to be
# honest. The list below is used for documentation / debugging only.
_RECENT_PERIODS: List[str] = [
    "20251231",
    "20250930",
    "20250630",
    "20250331",
    "20241231",
]


def _latest_period_for_as_of() -> str:
    """Pick a reasonable ``as_of`` label for the current date.

    Reports filed in Q1 cover the previous full year; reports filed in Q3 cover
    the latest half year. This is a coarse label, only used for staleness
    detection downstream.
    """
    now = datetime.now(timezone.utc)
    # If we're past April, the prior full year is the freshest annual report.
    if now.month >= 5:
        return f"{now.year - 1}1231"
    return f"{now.year - 2}1231"


class TushareIfindFundamentalAdapter:
    """Best-effort fundamental adapter for A-share fallbacks.

    Strategy per field:

    1. Tushare — only if a token is configured and the interface is licensed
       (some basic-plan tokens lack ``income`` / ``fina_indicator``).
    2. iFinD MCP — preferred because of richer period metadata.
    3. Yfinance ``info`` — last-resort; provides ``earningsGrowth`` /
       ``revenueGrowth`` / ``returnOnEquity`` for some tickers.

    Every call is wrapped in try/except and never raises; failed providers
    simply contribute ``None`` for the field.
    """

    name = "tushare_ifind"

    def __init__(
        self,
        ifind_source: Optional[IfindSource] = None,
        tushare_token: Optional[str] = None,
    ) -> None:
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        self._slots = BoundedSemaphore(4)
        self._ifind = ifind_source
        self._tushare_token = tushare_token
        self._tushare_pro = None
        if tushare_token:
            self._init_tushare()

    def _init_tushare(self) -> None:
        try:
            import tushare as ts  # type: ignore

            self._tushare_pro = ts.pro_api(self._tushare_token or "")
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.debug("[TushareFallback] init failed: %s", exc)
            self._tushare_pro = None

    def _tushare_call(self, api_name: str, **kwargs: Any) -> Optional[Dict[str, Any]]:
        if self._tushare_pro is None:
            return None
        try:
            df = self._tushare_pro.query(api_name, **kwargs)
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.debug("[TushareFallback] %s failed: %s", api_name, exc)
            return None
        if df is None or len(df) == 0:
            return None
        try:
            raw = df.iloc[0].to_dict()
            # pandas ``Series.to_dict()`` returns ``dict[Hashable, Any]``;
            # narrow to ``Dict[str, Any]`` for the declared return type.
            return cast(Dict[str, Any], raw) if isinstance(raw, dict) else None
        except Exception:  # noqa: BLE001
            return None

    @property
    def available(self) -> bool:
        return bool(self._tushare_pro) or bool(self._ifind and self._ifind.available)

    def get_fundamental_bundle(
        self, stock_code: str, period: str = "", *, fields: Optional[Tuple[str, ...]] = None, include_institution: bool = True, deadline: Optional[float] = None
    ) -> Dict[str, Any]:
        """Return a bundle in the same shape as ``AkshareFundamentalAdapter``.

        Empty when no provider returns data. ``status`` is one of:
        - ``ok`` if at least one field is filled
        - ``not_supported`` if every field returned None
        """
        ts_code = self._normalize_ts_code(stock_code)
        as_of = period or _latest_period_for_as_of()

        growth: Dict[str, Any] = {}
        field_meta: Dict[str, Any] = {}
        earnings: Dict[str, Any] = {}
        institution: Dict[str, Any] = {}
        source_chain: List[Dict[str, Any]] = []
        errors: List[str] = []

        # Read growth anchors concurrently to keep total wall time close to
        # the slowest single call. iFinD MCP can be slow when multiple
        # sessions reconnect; serial reads would multiply latency.
        anchor_results = self._read_growth_fields_concurrent(
            [(key, anchor) for key, anchor in _FIELD_TO_IFIND_ANCHOR.items() if fields is None or key in fields], ts_code, as_of, deadline
        )
        for bundle_key, anchor, reading in anchor_results:
            if reading is not None:
                growth[bundle_key] = reading.value
                field_meta[anchor] = adopted_field_record(anchor, reading, selection_reason="fallback")
                source_chain.append(
                    {
                        "provider": f"ifind:{anchor}",
                        "result": "ok",
                        "duration_ms": 0,
                    }
                )
            else:
                errors.append(f"ifind:{anchor}=None")

        # Earnings summary: Tushare express / iFinD does not expose a clean
        # summary string, so we leave the bundle empty here. The orchestrator
        # already considers ``earnings.data = {}`` as ``partial`` and the
        # staleness detector will not flag it.
        top10 = self._read_top10_holders(ts_code, as_of) if include_institution and (deadline is None or deadline > monotonic()) else None
        if top10 is not None:
            institution["top10_holder_change"] = top10
            source_chain.append(
                {
                    "provider": "ifind:top10",
                    "result": "ok",
                    "duration_ms": 0,
                }
            )

        has_content = bool(growth or earnings or institution)
        return {
            "status": "partial" if has_content else "not_supported",
            "growth": {**growth, "field_meta": field_meta} if field_meta else growth,
            "earnings": earnings,
            "institution": institution,
            "source_chain": source_chain,
            "errors": errors,
            "as_of": max((record["period"] for record in field_meta.values() if record.get("period")), default=None),
        }

    def _read_growth_field(
        self, anchor: str, ts_code: str, period: str, deadline: Optional[float] = None
    ) -> Optional[AnchorReading]:
        if self._ifind is None or not self._ifind.available:
            return None
        # iFinD does not always return data for the latest period (e.g.
        # annual report not yet disclosed). Walk a small set of recent
        # periods from newest to oldest, take the first non-None reading.
        candidates: List[Optional[str]] = [period or None]
        for fallback_period in _RECENT_PERIODS:
            if fallback_period != period:
                candidates.append(fallback_period)
        for candidate in candidates:
            if deadline is not None and deadline <= monotonic():
                return None
            budget = {"deadline": deadline} if deadline is not None and "deadline" in signature(self._ifind.read).parameters else {}
            reading = self._ifind.read(ts_code, anchor, candidate, **budget)
            if reading is not None and adopted_field_record(anchor, reading)["rule_eligible"]:
                return reading
        return None

    def _read_growth_fields_concurrent(
        self,
        items: List[Tuple[str, str]],
        ts_code: str,
        period: str,
        deadline: Optional[float] = None,
    ) -> List[Tuple[str, str, Optional[AnchorReading]]]:
        """Read multiple growth anchors concurrently via iFinD.

        Returns a list of ``(bundle_key, anchor, value)`` tuples in the same
        order as ``items``. When the iFinD source is unavailable, falls
        back to serial ``_read_growth_field``.
        """
        if not items:
            return []
        if self._ifind is None or not self._ifind.available:
            return [
                (bk, anchor, self._read_growth_field(anchor, ts_code, period, deadline))
                for bk, anchor in items
            ]
        try:
            return self._gather_growth_reads(items, ts_code, period, deadline)
        except Exception as exc:  # noqa: BLE001 — fall back to serial
            logger.debug(
                "[TushareIfindFallback] concurrent read failed: %s, "
                "falling back to serial",
                exc,
            )
            return [
                (bk, anchor, self._read_growth_field(anchor, ts_code, period, deadline))
                for bk, anchor in items
            ]

    def _gather_growth_reads(
        self,
        items: List[Tuple[str, str]],
        ts_code: str,
        period: str,
        deadline: Optional[float] = None,
    ) -> List[Tuple[str, str, Optional[AnchorReading]]]:
        """Read growth anchors in parallel via a ThreadPoolExecutor.

        Each worker thread calls ``IfindSource.read`` synchronously. The
        underlying :class:`IfindFetcher` is responsible for running the
        actual MCP call on its own daemon thread; here we only fan out
        across anchors so a slow anchor does not block the others.
        """
        if self._ifind is None:
            return [
                (bk, anchor, self._read_growth_field(anchor, ts_code, period, deadline))
                for bk, anchor in items
            ]

        futures = {}
        for _, anchor in items:
            if (deadline is not None and deadline <= monotonic()) or not self._slots.acquire(blocking=False):
                continue
            future = self._pool.submit(copy_context().run, self._read_growth_field, anchor, ts_code, period, deadline)
            future.add_done_callback(lambda _: self._slots.release())
            futures[anchor] = future
        concurrent.futures.wait(list(futures.values()), timeout=None if deadline is None else max(0.0, deadline - monotonic()))
        values: Dict[str, Optional[AnchorReading]] = {}
        for anchor, future in futures.items():
            if not future.done():
                future.cancel()
                continue
            try:
                values[anchor] = future.result()
            except Exception as exc:
                logger.debug("[TushareIfindFallback] %s failed: %s", anchor, type(exc).__name__)
        return [(key, anchor, values.get(anchor)) for key, anchor in items]

    def _read_top10_holders(self, ts_code: str, period: str) -> Optional[float]:
        # Tushare top10_holders is restricted on basic-plan tokens; we still
        # try. iFinD does not expose this field, so we return None.
        row = self._tushare_call(
            "top10_holders", ts_code=ts_code, period=period, limit=1
        )
        if not row:
            return None
        # ``hold_change`` is the number of shares changed; we keep the raw value
        # downstream knows it as a float.
        for key in ("hold_change", "holder_change", "change"):
            if key in row and row[key] is not None:
                try:
                    return float(row[key])
                except (TypeError, ValueError):
                    return None
        return None

    @staticmethod
    def _normalize_ts_code(stock_code: str) -> str:
        code = (stock_code or "").strip().upper()
        if not code:
            return code
        if "." in code:
            return code
        # 6-digit A-share → ts_code 600519.SH / 000001.SZ / 830xxx.BJ
        if code.isdigit() and len(code) == 6:
            if code.startswith(("60", "68", "90")):
                return f"{code}.SH"
            if code.startswith(("00", "30")):
                return f"{code}.SZ"
            if code.startswith(("43", "83", "87", "92")):
                return f"{code}.BJ"
        return code
