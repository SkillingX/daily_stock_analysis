# -*- coding: utf-8 -*-
"""XSearchProvider 单元测试（Phase 2 核心，离线，mock adanos）。

覆盖：
- adanos /x/stocks/v1/stock/{ticker} 响应映射：top_tweets → SearchResult + 舆情摘要。
- fail-open：found:false / 无 ticker / HTTP 错误 / 异常 → 空结果，不抛。
- is_available 随 key 开关；search(ticker=...) 传参；合成 URL 稳定（dedup 友好）。

详见 docs/x-media-source-plan.md。
"""

import os
import time

import pytest

from src.search_service import (
    SearchService,
    XSearchProvider,
    SearchResult,
    get_search_service,
    reset_search_service,
    resolve_x_ticker,
)


class _FakeResp:
    """模拟 requests.Response（仅需 status_code / json / text）。"""

    def __init__(self, status_code: int = 200, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _sample_payload(ticker: str = "AAPL", found: bool = True, tweets=None) -> dict:
    return {
        "ticker": ticker,
        "company_name": "Apple Inc.",
        "found": found,
        "buzz_score": 72.5,
        "mentions": 1200,
        "unique_tweets": 800,
        "sentiment_score": 0.35,
        "bullish_pct": 60,
        "bearish_pct": 25,
        "trend": "rising",
        "period_days": 3,
        "top_authors": [{"author": "a", "mentions": 10}],
        "top_tweets": tweets if tweets is not None else [
            {"text_snippet": "AAPL earnings beat", "author": "trader1",
             "created_at": "2026-06-29T10:00:00Z", "likes": 100, "retweets": 20,
             "views": 5000, "sentiment_label": "positive", "sentiment_score": 0.8},
            {"text_snippet": "AAPL overvalued here", "author": "bear2",
             "created_at": "2026-06-29T11:00:00Z", "likes": 50, "retweets": 5,
             "views": 2000, "sentiment_label": "negative", "sentiment_score": -0.5},
        ],
    }


class TestXSearchProviderMapping:
    """adanos 响应 → SearchResult 映射。"""

    def test_maps_top_tweets_to_search_results(self, monkeypatch):
        monkeypatch.setattr(
            "src.search_service._get_with_retry",
            lambda url, **kw: _FakeResp(200, _sample_payload()),
        )
        p = XSearchProvider(["key"], "https://api.adanos.org")
        resp = p.search("AAPL", max_results=5, ticker="AAPL")

        assert resp.success
        assert resp.provider == "X"
        # 摘要 1 条 + 2 条推文
        assert len(resp.results) == 3
        tweet_results = resp.results[1:]
        assert all(isinstance(r, SearchResult) for r in tweet_results)
        t1 = tweet_results[0]
        assert "AAPL earnings beat" in t1.title
        assert "trader1" in (t1.source or "")
        assert t1.url.startswith("https://x.com/")
        assert t1.published_date == "2026-06-29T10:00:00Z"
        # 互动量与情绪标签进入 snippet
        assert "❤100" in t1.snippet
        assert "positive" in t1.snippet

    def test_summary_item_is_first(self, monkeypatch):
        monkeypatch.setattr(
            "src.search_service._get_with_retry",
            lambda url, **kw: _FakeResp(200, _sample_payload()),
        )
        p = XSearchProvider(["key"], "https://api.adanos.org")
        resp = p.search("AAPL", ticker="AAPL")
        summary = resp.results[0]
        assert "AAPL" in summary.title
        assert "buzz" in summary.snippet and "72" in summary.snippet
        assert summary.source == "x.com"

    def test_respects_max_results(self, monkeypatch):
        payload = _sample_payload(tweets=[
            {"text_snippet": f"tweet {i}", "author": f"u{i}",
             "created_at": f"2026-06-2{i % 9}T10:0{i}:00Z"}
            for i in range(10)
        ])
        monkeypatch.setattr("src.search_service._get_with_retry", lambda url, **kw: _FakeResp(200, payload))
        p = XSearchProvider(["key"], "https://api.adanos.org")
        resp = p.search("AAPL", max_results=3, ticker="AAPL")
        # 摘要 1 + 最多 3 条推文
        assert len(resp.results) == 4

    def test_empty_top_tweets_still_returns_summary(self, monkeypatch):
        payload = _sample_payload(tweets=[])
        monkeypatch.setattr("src.search_service._get_with_retry", lambda url, **kw: _FakeResp(200, payload))
        p = XSearchProvider(["key"], "https://api.adanos.org")
        resp = p.search("AAPL", ticker="AAPL")
        assert resp.success
        assert len(resp.results) == 1  # 仅摘要


class TestXSearchProviderFailOpen:
    """fail-open：无覆盖/无 ticker/错误 → 空结果，不抛异常。"""

    def test_found_false_returns_empty_success(self, monkeypatch):
        monkeypatch.setattr(
            "src.search_service._get_with_retry",
            lambda url, **kw: _FakeResp(200, _sample_payload(found=False, tweets=[])),
        )
        p = XSearchProvider(["key"], "https://api.adanos.org")
        resp = p.search("600519", ticker="600519")  # A 股无覆盖
        assert resp.success
        assert resp.results == []

    def test_no_ticker_returns_empty(self):
        p = XSearchProvider(["key"], "https://api.adanos.org")
        resp = p.search("query", ticker=None)
        assert resp.results == []

    def test_http_error_fail_open(self, monkeypatch):
        monkeypatch.setattr(
            "src.search_service._get_with_retry",
            lambda url, **kw: _FakeResp(401, None, text='{"detail":"Invalid API key."}'),
        )
        p = XSearchProvider(["key"], "https://api.adanos.org")
        resp = p.search("AAPL", ticker="AAPL")
        assert resp.results == []
        assert not resp.success
        assert resp.error_message

    def test_request_exception_fail_open(self, monkeypatch):
        def boom(url, **kw):
            raise RuntimeError("network down")

        monkeypatch.setattr("src.search_service._get_with_retry", boom)
        p = XSearchProvider(["key"], "https://api.adanos.org")
        resp = p.search("AAPL", ticker="AAPL")
        assert resp.results == []
        assert not resp.success


class TestXSearchProviderAvailability:
    def test_available_with_keys(self):
        assert XSearchProvider(["k"], "u").is_available is True

    def test_unavailable_without_keys(self):
        assert XSearchProvider([], "u").is_available is False


class TestXSearchProviderUrlStability:
    def test_synthetic_url_stable_for_same_tweet(self, monkeypatch):
        payload = _sample_payload(tweets=[
            {"text_snippet": "same tweet", "author": "x1", "created_at": "2026-06-29T10:00:00Z"},
        ])
        monkeypatch.setattr("src.search_service._get_with_retry", lambda url, **kw: _FakeResp(200, payload))
        p = XSearchProvider(["key"], "https://api.adanos.org")
        r1 = p.search("AAPL", ticker="AAPL").results[1]
        r2 = p.search("AAPL", ticker="AAPL").results[1]
        assert r1.url == r2.url  # dedup 友好

    def test_synthetic_url_when_no_created_at(self, monkeypatch):
        payload = _sample_payload(tweets=[
            {"text_snippet": "no time tweet", "author": "x2"},  # 无 created_at
        ])
        monkeypatch.setattr("src.search_service._get_with_retry", lambda url, **kw: _FakeResp(200, payload))
        p = XSearchProvider(["key"], "https://api.adanos.org")
        tweet = p.search("AAPL", ticker="AAPL").results[1]
        assert tweet.url.startswith("https://x.com/x2/status/")


class TestSearchServiceXRegistration:
    """Phase 1 装配：SearchService 注册 XSearchProvider，复用 social 配置。"""

    def test_registers_x_provider_when_keys_given(self):
        svc = SearchService(x_keys=["k"], x_api_url="https://api.adanos.org")
        x = [p for p in svc._providers if isinstance(p, XSearchProvider)]
        assert len(x) == 1
        assert x[0].is_available

    def test_no_x_provider_when_keys_absent(self):
        svc = SearchService()  # 无任何 key
        assert not any(isinstance(p, XSearchProvider) for p in svc._providers)

    def test_x_provider_uses_custom_api_url(self):
        svc = SearchService(x_keys=["k"], x_api_url="https://custom.example.com")
        x = next(p for p in svc._providers if isinstance(p, XSearchProvider))
        assert x._api_url == "https://custom.example.com"

    def test_get_search_service_wires_from_social_config(self, monkeypatch):
        from types import SimpleNamespace

        fake_config = SimpleNamespace(
            bocha_api_keys=[], tavily_api_keys=[], anspire_api_keys=[],
            brave_api_keys=[], serpapi_keys=[], minimax_api_keys=[],
            coding_plan_search_provider="minimax", zhipu_api_key=None,
            searxng_base_urls=[], searxng_public_instances_enabled=False,
            news_max_age_days=3, news_strategy_profile="short",
            social_sentiment_api_key="sk-test",
            social_sentiment_api_url="https://api.adanos.org",
        )
        monkeypatch.setattr("src.config.get_config", lambda: fake_config)
        reset_search_service()
        try:
            svc = get_search_service()
            x = [p for p in svc._providers if isinstance(p, XSearchProvider)]
            assert len(x) == 1  # social key 存在 → 复用为 X provider
        finally:
            reset_search_service()

    def test_get_search_service_no_x_when_social_key_absent(self, monkeypatch):
        from types import SimpleNamespace

        fake_config = SimpleNamespace(
            bocha_api_keys=[], tavily_api_keys=[], anspire_api_keys=[],
            brave_api_keys=[], serpapi_keys=[], minimax_api_keys=[],
            coding_plan_search_provider="minimax", zhipu_api_key=None,
            searxng_base_urls=[], searxng_public_instances_enabled=False,
            news_max_age_days=3, news_strategy_profile="short",
            social_sentiment_api_key=None,
            social_sentiment_api_url="https://api.adanos.org",
        )
        monkeypatch.setattr("src.config.get_config", lambda: fake_config)
        reset_search_service()
        try:
            svc = get_search_service()
            assert not any(isinstance(p, XSearchProvider) for p in svc._providers)
        finally:
            reset_search_service()


class TestResolveXTicker:
    """股票代码 → adanos X ticker 解析。"""

    @pytest.mark.parametrize("code,expected", [
        ("AAPL", "AAPL"),
        ("aapl", "AAPL"),          # 大写归一
        ("$TSLA", "TSLA"),         # 去 $ 前缀
        ("BRK.A", "BRK.A"),        # 带后缀
        ("NVDA", "NVDA"),
    ])
    def test_us_codes_resolve(self, code, expected):
        assert resolve_x_ticker(code) == expected

    @pytest.mark.parametrize("code", [
        "600519",   # A 股
        "00700",    # 港股
        "0700",     # 港股短码
        "",         # 空
        None,       # None
        "AAPL123",  # 字母+数字（非法 ticker）
    ])
    def test_non_us_returns_none(self, code):
        assert resolve_x_ticker(code) is None


class TestSearchStockNewsXBranch:
    """search_stock_news 对 X provider 传 ticker（US 触发、A 股跳过）。"""

    @staticmethod
    def _svc():
        return SearchService(x_keys=["k"], x_api_url="https://api.adanos.org")

    def test_us_stock_fires_x_with_ticker(self, monkeypatch):
        calls = []

        def fake_get(url, **kw):
            calls.append(url)
            return _FakeResp(200, _sample_payload("AAPL"))

        monkeypatch.setattr("src.search_service._get_with_retry", fake_get)
        resp = self._svc().search_stock_news("AAPL", "Apple")
        assert any("/x/stocks/v1/stock/AAPL" in u for u in calls)  # X 被 adanos 调用
        assert resp.success

    def test_a_share_skips_x(self, monkeypatch):
        calls = []

        def fake_get(url, **kw):
            calls.append(url)
            return _FakeResp(200, _sample_payload("600519", found=False))

        monkeypatch.setattr("src.search_service._get_with_retry", fake_get)
        # 600519 是 A 股 → resolve_x_ticker=None → X 被 continue 跳过，adanos 不应被调用
        self._svc().search_stock_news("600519", "贵州茅台")
        assert not any("/x/stocks/v1/stock" in u for u in calls)


class TestSearchComprehensiveIntelXBranch:
    """search_comprehensive_intel 对 X provider 传 ticker（US 触发）。"""

    def test_us_stock_fires_x_in_intel(self, monkeypatch):
        calls = []

        def fake_get(url, **kw):
            calls.append(url)
            return _FakeResp(200, _sample_payload("AAPL"))

        monkeypatch.setattr("src.search_service._get_with_retry", fake_get)
        svc = SearchService(x_keys=["k"], x_api_url="https://api.adanos.org")
        svc.search_comprehensive_intel("AAPL", "Apple", max_searches=2)
        assert any("/x/stocks/v1/stock/AAPL" in u for u in calls)


class TestXSearchProviderCache:
    """per-ticker TTL 缓存：命中免重复调用 adanos。"""

    @staticmethod
    def _provider(**kw):
        return XSearchProvider(["key"], "https://api.adanos.org", **kw)

    def test_cache_hit_avoids_refetch(self, monkeypatch):
        calls = []

        def fake_get(url, **kw):
            calls.append(url)
            return _FakeResp(200, _sample_payload("AAPL"))

        monkeypatch.setattr("src.search_service._get_with_retry", fake_get)
        p = self._provider()
        p.search("AAPL", ticker="AAPL")
        p.search("AAPL", ticker="AAPL")  # 命中缓存
        assert len(calls) == 1

    def test_cache_ttl_expiry_refetches(self, monkeypatch):
        calls = []

        def fake_get(url, **kw):
            calls.append(url)
            return _FakeResp(200, _sample_payload("AAPL"))

        monkeypatch.setattr("src.search_service._get_with_retry", fake_get)
        p = self._provider(cache_ttl=0.0)  # 立即过期
        p.search("AAPL", ticker="AAPL")
        time.sleep(0.01)
        p.search("AAPL", ticker="AAPL")
        assert len(calls) == 2

    def test_failure_not_cached(self, monkeypatch):
        calls = []

        def fake_get(url, **kw):
            calls.append(url)
            return _FakeResp(401, None, text="bad key")  # 失败

        monkeypatch.setattr("src.search_service._get_with_retry", fake_get)
        p = self._provider(breaker_threshold=99)  # 暂不熔断，专注缓存
        p.search("AAPL", ticker="AAPL")
        p.search("AAPL", ticker="AAPL")  # 失败不缓存 → 再次调用
        assert len(calls) == 2

    def test_found_false_cached(self, monkeypatch):
        """found:false 返回 success=True → 应缓存（避免重复确认无覆盖）。"""
        calls = []

        def fake_get(url, **kw):
            calls.append(url)
            return _FakeResp(200, _sample_payload("600519", found=False, tweets=[]))

        monkeypatch.setattr("src.search_service._get_with_retry", fake_get)
        p = self._provider()
        p.search("600519", ticker="600519")   # found:false → success → 缓存
        p.search("600519", ticker="600519")   # 命中缓存
        assert len(calls) == 1


class TestXSearchProviderCircuitBreaker:
    """熔断：连续失败→冷却期跳过；冷却结束→恢复；成功→重置。"""

    @staticmethod
    def _provider(**kw):
        return XSearchProvider(["key"], "https://api.adanos.org", **kw)

    def test_opens_after_threshold_failures(self, monkeypatch):
        calls = []

        def fake_get(url, **kw):
            calls.append(url)
            return _FakeResp(500, None, text="server error")

        monkeypatch.setattr("src.search_service._get_with_retry", fake_get)
        p = self._provider(breaker_threshold=2, breaker_cooldown=300)
        p.search("AAPL", ticker="AAPL")  # 失败 1
        p.search("AAPL", ticker="AAPL")  # 失败 2 → 触发熔断（失败不缓存，仍 fetch）
        assert len(calls) == 2
        resp = p.search("AAPL", ticker="AAPL")  # 熔断打开 → 跳过，不再 fetch
        assert resp.success and resp.results == []  # fail-open 空结果
        assert len(calls) == 2  # 第 3 次没打 adanos

    def test_success_resets_breaker(self, monkeypatch):
        state = {"fail": True}

        def fake_get(url, **kw):
            if state["fail"]:
                return _FakeResp(500, None, text="err")
            return _FakeResp(200, _sample_payload("AAPL"))

        monkeypatch.setattr("src.search_service._get_with_retry", fake_get)
        p = self._provider(breaker_threshold=3, breaker_cooldown=300, cache_ttl=0.0)
        p.search("AAPL", ticker="AAPL")  # 失败 1
        p.search("AAPL", ticker="AAPL")  # 失败 2（未达阈值）
        state["fail"] = False
        resp = p.search("AAPL", ticker="AAPL")  # 成功 → 重置
        assert resp.success
        # 重置后再失败，计数应从 0 重新开始（未熔断）
        state["fail"] = True
        r = p.search("AAPL", ticker="AAPL")
        assert r.success is False  # 实际 fetch 了（未熔断），返回失败
        assert not p._breaker_is_open()

    def test_recovers_after_cooldown(self, monkeypatch):
        calls = []

        def fake_get(url, **kw):
            calls.append(url)
            return _FakeResp(500, None, text="err")

        monkeypatch.setattr("src.search_service._get_with_retry", fake_get)
        p = self._provider(breaker_threshold=1, breaker_cooldown=0.05)
        p.search("AAPL", ticker="AAPL")  # 失败 1 → 立即熔断
        assert p._breaker_is_open()
        time.sleep(0.08)  # 过冷却
        assert not p._breaker_is_open()  # 半开：允许重试

    def test_no_ticker_bypasses_breaker(self, monkeypatch):
        """ticker=None 不经缓存/熔断路径。"""
        calls = []

        def fake_get(url, **kw):
            calls.append(url)
            return _FakeResp(200, _sample_payload())

        monkeypatch.setattr("src.search_service._get_with_retry", fake_get)
        p = self._provider(breaker_threshold=1, breaker_cooldown=300)
        # 手动把熔断拧开
        p._breaker_failures = 5
        p._breaker_trip_until = time.monotonic() + 300
        resp = p.search("query", ticker=None)  # 无 ticker → 直接 execute，不被熔断拦截
        assert resp.results == []  # _do_search ticker=None → 空
        assert len(calls) == 0  # 且没打 adanos（_do_search 早返空）


# ---------------------------------------------------------------------------
# Phase 5 · opt-in 真 API 集成测试
# ---------------------------------------------------------------------------

_HAS_X_KEY = bool(os.getenv("SOCIAL_SENTIMENT_API_KEY"))
_X_API_URL = os.getenv("SOCIAL_SENTIMENT_API_URL", "https://api.adanos.org")


@pytest.mark.skipif(not _HAS_X_KEY, reason="未设置 SOCIAL_SENTIMENT_API_KEY，跳过真 API 集成测试")
@pytest.mark.network
class TestXSearchProviderRealAPI:
    """真 adanos API 集成测试（需 SOCIAL_SENTIMENT_API_KEY；离线/CI 默认跳过）。

    运行：export SOCIAL_SENTIMENT_API_KEY="sk_..." && pytest tests/test_x_search.py -m network
    """

    def test_real_us_ticker_returns_tweets(self):
        """美股热门 ticker 应返回舆情摘要 + 推文（或 found:false 时空结果，均不抛）。"""
        p = XSearchProvider([os.environ["SOCIAL_SENTIMENT_API_KEY"]], _X_API_URL, cache_ttl=0.0)
        resp = p.search("AAPL", ticker="AAPL")
        assert resp.success
        # 热门股应有数据；若 adanos 偶发无覆盖也接受空（只要 success 不抛）
        assert isinstance(resp.results, list)

    def test_real_a_share_no_coverage(self):
        """A 股代码走 ticker=None 路径 → 空结果（不调 adanos、不抛）。"""
        p = XSearchProvider([os.environ["SOCIAL_SENTIMENT_API_KEY"]], _X_API_URL)
        # 模拟 search_stock_news 的真实调用：A 股经 resolve_x_ticker → None
        from src.search_service import resolve_x_ticker
        assert resolve_x_ticker("600519") is None
        resp = p.search("600519", ticker=resolve_x_ticker("600519"))
        assert resp.success and resp.results == []
