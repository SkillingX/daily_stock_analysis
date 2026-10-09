# -*- coding: utf-8 -*-
"""
Tests for the fundamental_snapshot ``as_of_date`` plumbing.

The column is migrated by
``scripts/migrate_fundamental_snapshot_as_of_20260708.py`` and the value
flows from
``DataFetcherManager._derive_as_of_date(result_ctx)`` →
``pipeline.save_fundamental_snapshot(..., as_of_date=...)`` →
``FundamentalSnapshot.as_of_date``. The staleness detector reads the
column (when wrapped in a result-context dict) before scanning the
payload text, so a fresh 2025 snapshot with a year-2020 forecast
embedded in another block is still treated as fresh.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.base import DataFetcherManager


class TestAsOfDerivation(unittest.TestCase):
    def test_explicit_yyyymmdd_normalised(self) -> None:
        ctx = {"as_of": "20251231"}
        self.assertEqual(DataFetcherManager._derive_as_of_date(ctx), "2025-12-31")

    def test_explicit_iso_date_preserved(self) -> None:
        ctx = {"as_of": "2025-09-30"}
        self.assertEqual(DataFetcherManager._derive_as_of_date(ctx), "2025-09-30")

    def test_forecast_year_does_not_prove_actual_report_date(self) -> None:
        ctx = {
            "earnings": {
                "data": {
                    "forecast_summary": "预计2024年1-3月归属于母公司净利润盈利:1,000万元",
                }
            }
        }
        self.assertIsNone(DataFetcherManager._derive_as_of_date(ctx))

    def test_note_year_does_not_prove_actual_report_date(self) -> None:
        ctx = {
            "growth": {
                "data": {
                    "revenue_yoy": 8.5,
                    "roe": 33.0,
                    "_meta_note": "as of 2024-12-31",
                }
            }
        }
        self.assertIsNone(DataFetcherManager._derive_as_of_date(ctx))

    def test_unrelated_years_do_not_fabricate_december_period(self) -> None:
        ctx = {
            "earnings": {"data": {"forecast_summary": "预计2020年1-3月..."}},
            "growth": {"data": {"revenue_yoy": 5.0, "_period": "2024Q3"}},
        }
        self.assertIsNone(DataFetcherManager._derive_as_of_date(ctx))

    def test_returns_none_when_no_year(self) -> None:
        self.assertIsNone(DataFetcherManager._derive_as_of_date({}))
        self.assertIsNone(
            DataFetcherManager._derive_as_of_date(
                {"earnings": {"data": {"forecast_summary": ""}}}
            )
        )
        self.assertIsNone(
            DataFetcherManager._derive_as_of_date(
                {"growth": {"data": {"revenue_yoy": 8.5}}}
            )
        )


class TestStalenessPrefersAsOf(unittest.TestCase):
    """When the payload dict carries an explicit ``as_of`` field the
    staleness detector should trust it instead of scanning text.
    """

    def test_explicit_fresh_as_of_overrides_stale_text(self) -> None:
        payload = {
            "as_of": "2025-12-31",
            "earnings": {
                "data": {
                    # Text references 2020 but the data layer stamped 2025
                    "forecast_summary": "预计2020年1-3月...（historical note）",
                }
            },
        }
        self.assertFalse(
            DataFetcherManager._detect_payload_staleness(payload, current_year=2026)
        )

    def test_explicit_stale_as_of_downgrades(self) -> None:
        payload = {
            "as_of": "2023-12-31",
            "earnings": {"data": {"revenue": 1.0}},
        }
        self.assertTrue(
            DataFetcherManager._detect_payload_staleness(payload, current_year=2026)
        )

    def test_explicit_exactly_at_threshold_not_stale(self) -> None:
        payload = {"as_of": "2025-12-31", "growth": {"data": {"roe": 30.0}}}
        self.assertFalse(
            DataFetcherManager._detect_payload_staleness(payload, current_year=2026)
        )

    def test_no_as_of_falls_back_to_text_scan(self) -> None:
        # When the result context is a plain block payload (no top-level
        # ``as_of``), the legacy text-year scan still applies.
        block_payload = {
            "data": {
                "forecast_summary": "预计2020年1-3月归属于母公司净利润盈利:1,000万元",
            }
        }
        self.assertTrue(
            DataFetcherManager._detect_payload_staleness(
                block_payload, current_year=2026
            )
        )


class TestParseAsOfYear(unittest.TestCase):
    def test_iso_format(self) -> None:
        self.assertEqual(DataFetcherManager._parse_as_of_year("2025-12-31"), 2025)

    def test_compact_format(self) -> None:
        self.assertEqual(DataFetcherManager._parse_as_of_year("20251231"), 2025)

    def test_invalid(self) -> None:
        self.assertIsNone(DataFetcherManager._parse_as_of_year(""))
        self.assertIsNone(DataFetcherManager._parse_as_of_year(None))
        self.assertIsNone(DataFetcherManager._parse_as_of_year("abcd"))
        self.assertIsNone(DataFetcherManager._parse_as_of_year(2025))


if __name__ == "__main__":
    unittest.main()
