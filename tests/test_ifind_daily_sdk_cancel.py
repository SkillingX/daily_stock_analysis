# -*- coding: utf-8 -*-
"""issue #86：真实 MCP SDK 对已绑定未监听端口的取消，必须走日线源失败兜底。

不访问外部供应商。只拥有 127.0.0.1 端口且不 listen，合成 token。
"""
from __future__ import annotations

import socket
from time import monotonic

import pytest

from data_provider.base import BaseFetcher, DataFetcherManager
from data_provider.ifind_daily_adapter import IfindDailyFetcher
from data_provider.ifind_fundamental_adapter import IfindFetcher
from data_provider.realtime_types import CircuitBreaker
from src.core.research_run import (
    ResearchRun,
    ResearchStopped,
    current_research,
    is_cancellation_exception,
)
from src.patches.eastmoney_patch import request_deadline
from src.services import history_loader as H
from tests.ifind_daily_testutil import LATEST_SESSION, enabled_config, std_frame, strict_calendar


class _LegacyFetcher(BaseFetcher):
    name = "LegacyOneFetcher"
    priority = 10

    def __init__(self) -> None:
        self.calls: list[str] = []

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str):
        raise NotImplementedError

    def _normalize_data(self, df, stock_code: str):
        return df

    def get_daily_data(self, stock_code, start_date=None, end_date=None, days=30):
        self.calls.append(stock_code)
        return std_frame(rows=5)


def _refused_endpoint() -> tuple[socket.socket, str]:
    owned = socket.socket()
    owned.bind(("127.0.0.1", 0))
    return owned, f"https://127.0.0.1:{owned.getsockname()[1]}/mcp"


def _install(monkeypatch, *, deadline_offset: float, on_sdk_cancel=None):
    strict_calendar(monkeypatch)
    enabled_config(monkeypatch)
    monkeypatch.setattr(
        DataFetcherManager,
        "_daily_source_health",
        CircuitBreaker(failure_threshold=2, cooldown_seconds=60, half_open_max_calls=1),
    )
    owned, endpoint = _refused_endpoint()
    mcp = IfindFetcher(endpoint=endpoint, token="synthetic-test-token", timeout_seconds=20)
    calls = {"mcp": 0, "cancel_observed": 0}
    real_query = mcp.query

    def counted(tool, question, **kwargs):
        calls["mcp"] += 1
        try:
            return real_query(tool, question, **kwargs)
        except BaseException as exc:
            # 只在观察到真实 SDK 取消形状时回调（原始异常原样重抛）。
            if on_sdk_cancel is not None and is_cancellation_exception(exc):
                calls["cancel_observed"] += 1
                on_sdk_cancel()
            raise

    mcp.query = counted  # type: ignore[method-assign]
    daily = IfindDailyFetcher(mcp)
    legacy = _LegacyFetcher()
    manager = DataFetcherManager(fetchers=[legacy])
    manager._ifind_daily_fetcher = daily
    reasons: list[str] = []
    original = manager._record_daily_source_failure

    def record(fetcher, market, reason):
        reasons.append(str(reason))
        return original(fetcher, market, reason)

    manager._record_daily_source_failure = record  # type: ignore[method-assign]
    monkeypatch.setattr(H, "_get_fetcher_manager", lambda: manager)
    monkeypatch.setattr(
        "data_provider.ifind_daily_adapter.stream_export_csv",
        lambda url: (_ for _ in ()).throw(AssertionError(f"csv started {url}")),
    )
    token = request_deadline.set(monotonic() + deadline_offset)
    return owned, calls, legacy, reasons, token


def test_sdk_cancel_on_refused_port_falls_back_without_run(monkeypatch):
    owned, calls, legacy, reasons, deadline = _install(monkeypatch, deadline_offset=90)
    scope_token, scope = H.open_current_stock_scope("600519", target_date=LATEST_SESSION)
    try:
        frame, source = H.load_history_df("600519", days=5, target_date=LATEST_SESSION)
        assert source == "LegacyOneFetcher"
        assert frame is not None and len(frame) == 5
        assert calls["mcp"] == 1
        assert legacy.calls == ["600519"]
        assert any("unexpected MCP transport cancellation" in reason for reason in reasons)
        assert scope.peek() is not None
        assert scope.peek().source == "LegacyOneFetcher"
    finally:
        request_deadline.reset(deadline)
        H.close_current_stock_scope(scope_token)
        owned.close()


def test_sdk_cancel_with_live_run_still_falls_back(monkeypatch):
    owned, calls, legacy, reasons, deadline = _install(monkeypatch, deadline_offset=90)
    run = ResearchRun(timeout_seconds=120)
    research_token = current_research.set(run)
    scope_token, _scope = H.open_current_stock_scope("600519", target_date=LATEST_SESSION)
    try:
        frame, source = H.load_history_df("600519", days=5, target_date=LATEST_SESSION)
        assert source == "LegacyOneFetcher"
        assert frame is not None and len(frame) == 5
        assert calls["mcp"] == 1
        assert legacy.calls == ["600519"]
        assert any("unexpected MCP transport cancellation" in reason for reason in reasons)
        assert run.stop_reason is None
    finally:
        current_research.reset(research_token)
        request_deadline.reset(deadline)
        H.close_current_stock_scope(scope_token)
        owned.close()


def test_sdk_cancel_after_run_stop_does_not_fall_back(monkeypatch):
    """真实 SDK 取消后、适配器分类前 run 已停止：控制信号传出，不兜底。

    确定性顺序（PR87 R12，替代原 0.4s 定时器）：在外向查询边界观察到真实
    SDK CancelledError 的瞬间停止 run 并原样重抛同一异常——_query_daily_mcp
    的 run.check() 必然先于 DataFetchError 分类抛出 ResearchStopped。不依赖
    平台时延；重复热路径同样成立。adac CI 上端口 0.02s 即拒，旧定时器从未
    触发停止导致健康兜底（DID NOT RAISE）。
    """
    total_mcp = 0
    for _attempt in range(3):
        run = ResearchRun(timeout_seconds=120)
        owned, calls, legacy, reasons, deadline = _install(
            monkeypatch, deadline_offset=90,
            on_sdk_cancel=lambda run=run: run.stop("cancelled"),
        )
        research_token = current_research.set(run)
        scope_token, scope = H.open_current_stock_scope("600519", target_date=LATEST_SESSION)
        try:
            with pytest.raises(ResearchStopped):
                H.load_history_df("600519", days=5, target_date=LATEST_SESSION)
            assert calls["cancel_observed"] == 1  # 真实 SDK 取消确被观察
            assert run.stop_reason == "cancelled"  # 停止先于适配器分类发生
            assert calls["mcp"] == 1
            total_mcp += calls["mcp"]
            assert legacy.calls == []
            assert reasons == []
            assert scope.peek() is None
            assert DataFetcherManager._daily_source_health.is_available("daily_data:cn:IfindDailyFetcher")
        finally:
            current_research.reset(research_token)
            request_deadline.reset(deadline)
            H.close_current_stock_scope(scope_token)
            owned.close()
    assert total_mcp == 3  # 每轮都真实调用 SDK，无跳过/重试


def test_expired_outer_budget_does_not_start_mcp_or_fallback(monkeypatch):
    owned, calls, legacy, reasons, deadline = _install(monkeypatch, deadline_offset=-1)
    scope_token, scope = H.open_current_stock_scope("600519", target_date=LATEST_SESSION)
    try:
        with pytest.raises(TimeoutError):
            H.load_history_df("600519", days=5, target_date=LATEST_SESSION)
        assert calls["mcp"] == 0
        assert legacy.calls == []
        assert reasons == []
        assert scope.peek() is None
    finally:
        request_deadline.reset(deadline)
        H.close_current_stock_scope(scope_token)
        owned.close()
