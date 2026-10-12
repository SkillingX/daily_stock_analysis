# -*- coding: utf-8 -*-
"""
Tests for SearchService.search_comprehensive_intel behavior.

Covers:
- Dimension result presence and max_searches limiting
- Configured provider preference and per-dimension fallback
- Per-dimension failure isolation
- Elapsed time baseline for future parallelization work
"""

import os
import sys
import time
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Mock newspaper before search_service import (optional dependency)
if "newspaper" not in sys.modules:
    mock_np = MagicMock()
    mock_np.Article = MagicMock()
    mock_np.Config = MagicMock()
    sys.modules["newspaper"] = mock_np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.search_service import SearchResponse, SearchResult, SearchService


class _DummyProvider:
    def __init__(self, name="DummyProvider"):
        self.name = name
        self.is_available = True
        self.calls = []

    def search(self, query, max_results=5, days=7, **kwargs):
        self.calls.append((query, max_results, days, kwargs))
        return SearchResponse(
            query=query,
            results=[
                SearchResult(
                    title=f"[{self.name}] {query}",
                    snippet="snippet",
                    url=f"https://example.com/{self.name}",
                    source="example.com",
                    published_date=datetime.now().date().isoformat(),
                )
            ],
            provider=self.name,
            success=True,
        )


class _FailingProvider:
    def __init__(self, name="FailingProvider"):
        self.name = name
        self.is_available = True

    def search(self, query, max_results=5, days=7, **kwargs):
        return SearchResponse(
            query=query,
            results=[],
            provider=self.name,
            success=False,
            error_message="forced failure",
        )


class TestSearchComprehensiveIntel(unittest.TestCase):
    def _build_service(self, providers=None):
        service = SearchService(
            searxng_public_instances_enabled=False,
            news_max_age_days=3,
            news_strategy_profile="short",
        )
        service._providers = providers or []
        return service

    def test_returns_expected_dimensions_for_cn_stock(self):
        provider = _DummyProvider()
        service = self._build_service([provider])

        results = service.search_comprehensive_intel(
            stock_code="600519",
            stock_name="贵州茅台",
            max_searches=6,
        )

        self.assertIn("latest_news", results)
        self.assertIn("market_analysis", results)
        self.assertIn("risk_check", results)
        self.assertIn("announcements", results)
        self.assertIn("earnings", results)
        self.assertIn("industry", results)
        self.assertTrue(all(r.success for r in results.values()))

    def test_max_searches_limits_dimension_count(self):
        provider = _DummyProvider()
        service = self._build_service([provider])

        results = service.search_comprehensive_intel(
            stock_code="600519",
            stock_name="贵州茅台",
            max_searches=3,
        )

        self.assertEqual(len(results), 3)
        self.assertEqual(len(provider.calls), 3)

    def test_healthy_first_provider_serves_all_dimensions(self):
        p1 = _DummyProvider("P1")
        p2 = _DummyProvider("P2")
        service = self._build_service([p1, p2])

        service.search_comprehensive_intel(
            stock_code="600519",
            stock_name="贵州茅台",
            max_searches=4,
        )

        # A healthy configured provider avoids unnecessary fallback calls.
        self.assertEqual(len(p1.calls), 4)
        self.assertEqual(len(p2.calls), 0)

    def test_single_dimension_failure_does_not_break_others(self):
        working = _DummyProvider("Working")
        failing = _FailingProvider("Failing")
        service = self._build_service([failing, working])

        results = service.search_comprehensive_intel(
            stock_code="600519",
            stock_name="贵州茅台",
            max_searches=3,
        )

        # Failure in the first provider is repaired for each dimension
        dim_names = list(results.keys())
        self.assertTrue(results[dim_names[0]].success)
        self.assertTrue(results[dim_names[1]].success)
        self.assertTrue(results[dim_names[2]].success)

    def test_strict_freshness_filtering_is_applied(self):
        provider = _DummyProvider()
        service = self._build_service([provider])

        with patch.object(service, "_filter_news_response") as mock_filter:
            mock_filter.side_effect = lambda response, **kwargs: SearchResponse(
                query=response.query,
                results=[],
                provider=response.provider,
                success=True,
            )
            service.search_comprehensive_intel(
                stock_code="600519",
                stock_name="贵州茅台",
                max_searches=1,
            )

        # Only latest_news is requested and it has strict_freshness=True
        self.assertEqual(mock_filter.call_count, 1)

    def test_foreign_stock_uses_different_dimension_set(self):
        provider = _DummyProvider()
        service = self._build_service([provider])

        results = service.search_comprehensive_intel(
            stock_code="AAPL",
            stock_name="Apple",
            max_searches=6,
        )

        # Foreign set does not include "announcements"
        self.assertIn("latest_news", results)
        self.assertIn("risk_check", results)
        self.assertNotIn("announcements", results)


class TestSearchComprehensiveIntelBenchmark(unittest.TestCase):
    @pytest.mark.benchmark
    def test_serial_dimensions_elapsed_baseline(self):
        """Record baseline latency of serial dimension execution."""
        provider = _DummyProvider()
        service = SearchService(
            searxng_public_instances_enabled=False,
            news_max_age_days=3,
            news_strategy_profile="short",
        )
        service._providers = [provider]

        start = time.monotonic()
        service.search_comprehensive_intel(
            stock_code="600519",
            stock_name="贵州茅台",
            max_searches=5,
        )
        elapsed = time.monotonic() - start

        # 5 dimensions * 0.5s sleep = 2.5s minimum; allow overhead
        self.assertGreaterEqual(elapsed, 2.0)


if __name__ == "__main__":
    unittest.main()


def test_keyless_html_provider_enters_scraper(monkeypatch):
    from src.services import html_scrape_news
    calls = []
    def fetch(code, days):
        calls.append(code)
        return []
    monkeypatch.setattr(html_scrape_news, '_fetch_eastmoney_announcements', fetch)
    provider = html_scrape_news.HTMLScrapeNewsProvider()
    assert provider.is_available
    response = provider.search('300260 公告')
    assert calls == ['300260']
    assert response.error_message != 'html_scrape 未配置 API Key'


def test_public_fallback_not_used_while_configured_provider_works():
    public = _DummyProvider('html_scrape')
    configured = _DummyProvider('Configured')
    service = TestSearchComprehensiveIntel()._build_service([public, configured])
    with patch('src.search_service.time.sleep'):
        results = service.search_comprehensive_intel('300260', '新莱应材', max_searches=4)
    assert len(results) == 4 and len(configured.calls) == 4 and not public.calls


def test_expired_tool_deadline_stops_further_searches():
    from src.patches.eastmoney_patch import request_deadline
    provider = _DummyProvider()
    service = TestSearchComprehensiveIntel()._build_service([provider])
    token = request_deadline.set(time.monotonic() - 1)
    try:
        assert service.search_comprehensive_intel('300260', '新莱应材') == {}
        assert not provider.calls
    finally:
        request_deadline.reset(token)
