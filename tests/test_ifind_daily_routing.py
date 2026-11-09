# -*- coding: utf-8 -*-
"""issue #86：DataFetcherManager 的 iFinD 日线路由与兜底边界（离线）。

覆盖：合格身份优先尝试、失败按既有候选相对次序兜底、未资格证券/历史目标/
日历不可用在出站前跳过且不计供应商故障、未来结束日约束、停止信号传播不
触发兜底、短期熔断只影响 iFinD 自身键。
"""
from __future__ import annotations

import pytest


from data_provider.base import BaseFetcher, DataFetchError, DataFetcherManager
from data_provider.realtime_types import CircuitBreaker
from src.core.research_run import ResearchRun, ResearchStopped
from tests.ifind_daily_testutil import (
    LATEST_SESSION,
    RecordingIfindStub,
    enabled_config,
    std_frame,
    strict_calendar,
)


@pytest.fixture(autouse=True)
def isolated_daily_source_health(monkeypatch):
    health = DataFetcherManager._daily_source_health
    monkeypatch.setattr(
        DataFetcherManager,
        "_daily_source_health",
        CircuitBreaker(
            failure_threshold=health.failure_threshold,
            cooldown_seconds=health.cooldown_seconds,
            half_open_max_calls=health.half_open_max_calls,
        ),
    )


class _LegacyFetcher(BaseFetcher):
    def __init__(self, name: str, df=None, error: Exception | None = None):
        self.name = name
        self.priority = 10
        self.df = df
        self.error = error
        self.calls: list[dict] = []

    def _fetch_raw_data(self, stock_code, start_date, end_date):
        raise NotImplementedError

    def _normalize_data(self, df, stock_code):
        return df

    def get_daily_data(self, stock_code, start_date=None, end_date=None, days=30):
        self.calls.append({
            "stock_code": stock_code, "start_date": start_date,
            "end_date": end_date, "days": days,
        })
        if self.error is not None:
            raise self.error
        if self.df is None:
            return std_frame()
        return self.df


@pytest.fixture()
def manager(monkeypatch):
    strict_calendar(monkeypatch)
    enabled_config(monkeypatch)
    first = _LegacyFetcher("LegacyOneFetcher")
    second = _LegacyFetcher("LegacyTwoFetcher")
    mgr = DataFetcherManager(fetchers=[first, second])
    mgr._ifind_daily_fetcher = None
    return mgr, first, second


def test_qualified_stock_uses_ifind_first_without_legacy_call(manager):
    mgr, first, second = manager
    stub = RecordingIfindStub(df=std_frame())
    mgr._ifind_daily_fetcher = stub
    df, source = mgr.get_daily_data("600519.SH")
    assert source == "IfindDailyFetcher"
    assert len(df) == 5
    assert len(stub.calls) == 1
    assert stub.calls[0]["stock_code"] == "600519.SH"
    assert stub.calls[0]["end_date"] == LATEST_SESSION.isoformat()
    assert first.calls == [] and second.calls == []


def test_bare_cn_code_infers_exchange_identity(manager):
    mgr, _first, _second = manager
    stub = RecordingIfindStub(df=std_frame())
    mgr._ifind_daily_fetcher = stub
    _df, source = mgr.get_daily_data("000001")
    assert source == "IfindDailyFetcher"
    assert stub.calls[0]["stock_code"] == "000001.SZ"


def test_ifind_failure_falls_back_with_unchanged_candidate_order(manager):
    mgr, first, second = manager
    stub = RecordingIfindStub(error=DataFetchError("ifind 现金复权元数据不合格，整窗不采纳"))
    mgr._ifind_daily_fetcher = stub
    df, source = mgr.get_daily_data("600519")
    assert source == "LegacyOneFetcher"
    assert len(first.calls) == 1 and second.calls == []
    assert stub.calls[0]["stock_code"] == "600519.SH"


@pytest.mark.parametrize("code", ["000001.SH", "399001.SZ", "399006.SZ", "510300", "510300.SH", "920748", "HK00700", "900901"])
def test_unsupported_codes_never_dispatch_ifind(manager, code):
    mgr, first, _second = manager
    stub = RecordingIfindStub(df=std_frame())
    mgr._ifind_daily_fetcher = stub
    _df, source = mgr.get_daily_data(code)
    assert stub.calls == []
    assert source == "LegacyOneFetcher"
    # 未资格证券不出站也不计 iFinD 供应商故障
    assert DataFetcherManager._daily_source_health.is_available("daily_data:cn:IfindDailyFetcher")


def test_historical_target_skips_ifind_and_passes_real_cutoff(manager):
    mgr, first, _second = manager
    stub = RecordingIfindStub(df=std_frame())
    mgr._ifind_daily_fetcher = stub
    _df, source = mgr.get_daily_data("600519", end_date="2026-11-05")
    assert stub.calls == []
    assert source == "LegacyOneFetcher"
    # 真实目标结束日传入原源，不再「先取今日再裁剪」
    assert first.calls[0]["end_date"] == "2026-11-05"


def test_future_end_clamped_to_latest_completed_session(manager):
    mgr, _first, _second = manager
    stub = RecordingIfindStub(df=std_frame())
    mgr._ifind_daily_fetcher = stub
    _df, _source = mgr.get_daily_data("600519", end_date="2026-11-10")
    assert stub.calls[0]["end_date"] == LATEST_SESSION.isoformat()


def test_disabled_config_keeps_original_routing(monkeypatch):
    strict_calendar(monkeypatch)
    enabled_config(monkeypatch, enabled=False)
    first = _LegacyFetcher("LegacyOneFetcher")
    mgr = DataFetcherManager(fetchers=[first])
    stub = RecordingIfindStub(df=std_frame())
    mgr._ifind_daily_fetcher = stub
    _df, source = mgr.get_daily_data("600519")
    assert stub.calls == []
    assert source == "LegacyOneFetcher"


def test_calendar_unavailable_skips_ifind(monkeypatch):
    from src.core import trading_calendar as tc

    enabled_config(monkeypatch)
    monkeypatch.setattr(tc, "recent_completed_trading_sessions", lambda *a, **k: None)
    first = _LegacyFetcher("LegacyOneFetcher")
    mgr = DataFetcherManager(fetchers=[first])
    stub = RecordingIfindStub(df=std_frame())
    mgr._ifind_daily_fetcher = stub
    _df, source = mgr.get_daily_data("600519")
    assert stub.calls == []
    assert source == "LegacyOneFetcher"


def test_research_stop_propagates_without_fallback(manager):
    mgr, first, _second = manager
    stub = RecordingIfindStub(error=ResearchStopped("用户已取消本次研究"))
    mgr._ifind_daily_fetcher = stub
    with pytest.raises(ResearchStopped):
        mgr.get_daily_data("600519")
    assert first.calls == []
    # 停止后的失败不计入熔断
    assert DataFetcherManager._daily_source_health.is_available("daily_data:cn:IfindDailyFetcher")


def test_circuit_breaker_only_gates_ifind_key(manager):
    mgr, first, _second = manager
    stub = RecordingIfindStub(error=DataFetchError("ifind 完整窗口缺少交易日"))
    mgr._ifind_daily_fetcher = stub
    for _ in range(2):
        _df, source = mgr.get_daily_data("600519")
        assert source == "LegacyOneFetcher"
    assert len(stub.calls) == 2
    # 第三次：iFinD 日线键熔断，直接走既有候选
    _df, source = mgr.get_daily_data("600519")
    assert source == "LegacyOneFetcher"
    assert len(stub.calls) == 2
    assert len(first.calls) == 3
    assert first.calls[0]["stock_code"] == "600519"


def test_expired_outer_deadline_during_query_is_not_health_failure(monkeypatch):
    import time
    from types import SimpleNamespace

    from data_provider.ifind_daily_adapter import IfindDailyFetcher
    from src.patches.eastmoney_patch import request_deadline
    from src.services import history_loader as history

    strict_calendar(monkeypatch)
    enabled_config(monkeypatch)
    monkeypatch.setattr(
        DataFetcherManager,
        "_daily_source_health",
        CircuitBreaker(failure_threshold=2, cooldown_seconds=60, half_open_max_calls=1),
    )
    legacy = _LegacyFetcher("LegacyOneFetcher")
    mgr = DataFetcherManager(fetchers=[legacy])
    calls = []

    def query(*_args, **_kwargs):
        calls.append("query")
        time.sleep(0.2)
        return SimpleNamespace(answer="", indicators_params={})

    mgr._ifind_daily_fetcher = IfindDailyFetcher(SimpleNamespace(available=True, query=query))
    monkeypatch.setattr(history, "_get_fetcher_manager", lambda: mgr)
    token, scope = history.open_current_stock_scope("600519", target_date=LATEST_SESSION)
    deadline = request_deadline.set(time.monotonic() + 0.05)
    try:
        with pytest.raises(TimeoutError):
            history.load_history_df("600519", days=1, target_date=LATEST_SESSION)
        assert calls == ["query"]
        assert legacy.calls == []
        assert scope.peek() is None
        assert DataFetcherManager._daily_source_health.is_available("daily_data:cn:IfindDailyFetcher")
    finally:
        request_deadline.reset(deadline)
        history.close_current_stock_scope(token)


def test_local_attempt_timeout_with_healthy_outer_budget_falls_back(manager):
    mgr, first, _second = manager
    stub = RecordingIfindStub(error=TimeoutError("daily attempt deadline exhausted"))
    mgr._ifind_daily_fetcher = stub
    _df, source = mgr.get_daily_data("600519", end_date=LATEST_SESSION.isoformat())
    assert source == "LegacyOneFetcher"
    assert len(first.calls) == 1


def test_deadline_expired_before_dispatch_sends_nothing(manager):
    import time

    from src.patches.eastmoney_patch import request_deadline

    mgr, first, _second = manager
    stub = RecordingIfindStub(df=std_frame())
    mgr._ifind_daily_fetcher = stub
    deadline = request_deadline.set(time.monotonic() - 1)
    try:
        with pytest.raises(TimeoutError):
            mgr.get_daily_data("600519", end_date=LATEST_SESSION.isoformat())
        assert stub.calls == []
        assert first.calls == []
    finally:
        request_deadline.reset(deadline)
