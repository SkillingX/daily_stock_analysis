# -*- coding: utf-8 -*-
"""Issue #66 PR 复审 P2 修复回归（PR67 head 12134e19）。

P2-1：调度停止后，候选内部 AkshareFetcher 东财→新浪 fallback 仍派发第二次
    requests HTTP（共享 HTTP 补丁只读研究取消/截止，不读任务所属停止 Event）。
    回归保留真实 DataFetcherManager / AkshareFetcher / 内部 fallback / requests
    补丁链路：首个 AK 端点请求 /first、set 所属实例 Event 并返回空表；修复后
    仅 /first 出站；无停止对照仍到达 /second 并返回有效统计（上下文隔离）。
P2-2：继承的 request_deadline 已过期（或停止/预算在源选择期间变化）时，两个
    公开入口的先行 TickFlow 源仍被派发。先行派发前必须有停止/预算门禁，源
    选择后、SDK 调用前复查；预算充足对照正常返回有效结果。
补充边界：auth/throttle 之后、真正派发前的停止复查；研究取消分类
    （ResearchStopped）不被停止检查遮蔽；停止仅作用于所属上下文（无全局标志）。
全程仅回环地址，零供应商出站。
"""

from __future__ import annotations

import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional

import pandas as pd
import pytest
import requests


# ---------------------------------------------------------------------------
# 本地 HTTP 夹具（仅回环；服务端记录实际收到的请求路径）
# ---------------------------------------------------------------------------


class _RecordingServer(ThreadingHTTPServer):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.requests_log: List[dict] = []
        self._log_lock = threading.Lock()

    def record(self, path: str) -> None:
        with self._log_lock:
            self.requests_log.append({"path": path, "t": time.monotonic()})


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - http.server 契约
        server: _RecordingServer = self.server  # type: ignore[assignment]
        server.record(self.path)
        body = b"{}"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass


@pytest.fixture()
def local_http_server() -> Iterator[_RecordingServer]:
    server = _RecordingServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def _attempted_paths(server: _RecordingServer) -> List[str]:
    return [entry["path"] for entry in server.requests_log]


def _stats_dataframe() -> pd.DataFrame:
    """一行有效 A 股行情（600001 测试股，现价 10.0 / 昨收 9.0）。"""
    return pd.DataFrame(
        [
            {
                "代码": "600001",
                "名称": "测试股",
                "最新价": 10.0,
                "昨收": 9.0,
                "成交额": 1000000.0,
            }
        ]
    )


def _install_fake_akshare(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stop_event: Optional[threading.Event],
    base: str,
    first_result: Any,
    second_result: Any,
) -> None:
    """注入假 akshare：东财端点 /first（可选 set 停止 Event），新浪端点 /second。"""

    def first() -> Any:
        requests.get(base + "/first")  # 无 timeout（现实形态；补丁注入剩余预算）
        if stop_event is not None:
            stop_event.set()
        return first_result

    def second() -> Any:
        requests.get(base + "/second")  # 无 timeout（现实形态）
        return second_result

    monkeypatch.setitem(
        sys.modules, "akshare", SimpleNamespace(stock_zh_a_spot_em=first, stock_zh_a_spot=second)
    )


class _RecordingTickFlowSdk:
    """记录先行 SDK 调用的 TickFlow 替身（零网络）。"""

    def __init__(self) -> None:
        self.calls: List[str] = []

    def get_main_indices(self, region: str = "cn") -> List[Dict[str, Any]]:
        self.calls.append("indices")
        return [{"code": "sh000001", "current": 1.0}]

    def get_market_stats(self) -> Dict[str, Any]:
        self.calls.append("stats")
        return {"up_count": 1}


def _manager_with_tickflow(sdk: Any, fetchers: Optional[List[Any]] = None) -> Any:
    from data_provider.base import DataFetcherManager

    manager = DataFetcherManager()
    manager._fetchers = list(fetchers or [])
    manager._get_tickflow_fetcher = lambda: sdk
    return manager


# ---------------------------------------------------------------------------
# P2-1：调度停止后候选内部 fallback 不得派发新 HTTP（真实管理器/获取器/补丁链路）
# ---------------------------------------------------------------------------


def test_stop_after_first_endpoint_suppresses_internal_fallback(
    local_http_server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """root 反例：首个 AK 端点期间 Event 已 set，内部新浪 fallback 不得再出站。"""
    from data_provider.akshare_fetcher import AkshareFetcher
    from data_provider.base import DataFetcherManager
    from src.scheduler import current_stop_event

    stop_event = threading.Event()
    base = f"http://127.0.0.1:{local_http_server.server_address[1]}"
    _install_fake_akshare(
        monkeypatch,
        stop_event=stop_event,
        base=base,
        first_result=pd.DataFrame(),  # 东财空表 → 进入内部新浪 fallback
        second_result=_stats_dataframe(),
    )
    fetcher = AkshareFetcher(sleep_min=0.0, sleep_max=0.0)
    manager = DataFetcherManager()
    manager._fetchers = [fetcher]
    manager._get_tickflow_fetcher = lambda: None

    token = current_stop_event.set(stop_event)
    try:
        result = manager.get_market_stats(purpose="prfix-internal-fallback")
    finally:
        current_stop_event.reset(token)

    attempted = _attempted_paths(local_http_server)
    assert attempted == ["/first"], f"停止后内部 fallback 仍派发了新 HTTP: {attempted}"
    assert result == {}, "停止后不产出部分结果"


def test_no_stop_internal_fallback_still_reaches_second(
    local_http_server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """对照（无停止）：内部 fallback 正常工作，/second 出站且统计有效（上下文隔离）。"""
    from data_provider.akshare_fetcher import AkshareFetcher
    from data_provider.base import DataFetcherManager

    base = f"http://127.0.0.1:{local_http_server.server_address[1]}"
    _install_fake_akshare(
        monkeypatch,
        stop_event=None,
        base=base,
        first_result=pd.DataFrame(),
        second_result=_stats_dataframe(),
    )
    fetcher = AkshareFetcher(sleep_min=0.0, sleep_max=0.0)
    manager = DataFetcherManager()
    manager._fetchers = [fetcher]
    manager._get_tickflow_fetcher = lambda: None

    result = manager.get_market_stats(purpose="prfix-internal-control")

    attempted = _attempted_paths(local_http_server)
    assert attempted == ["/first", "/second"], f"无停止时内部 fallback 应完整尝试: {attempted}"
    assert isinstance(result, dict) and result.get("up_count") == 1, f"fallback 统计应有效: {result}"


def test_stop_is_context_scoped_not_global(local_http_server) -> None:
    """停止 Event 已 set 但未装入当前上下文时，出站不受影响（禁止全局停止标志）。"""
    from src.patches.eastmoney_patch import eastmoney_patch

    eastmoney_patch(budget_only=True)
    base = f"http://127.0.0.1:{local_http_server.server_address[1]}"
    orphan_event = threading.Event()
    orphan_event.set()  # 已 set，但不属于当前上下文

    response = requests.get(base + "/context-scope-probe")

    assert response.status_code == 200
    assert _attempted_paths(local_http_server) == ["/context-scope-probe"]


# ---------------------------------------------------------------------------
# P2-2：先行 TickFlow 源派发前的停止/预算门禁（含源选择期间变化）
# ---------------------------------------------------------------------------


def test_expired_inherited_deadline_gates_leading_tickflow() -> None:
    """root 反例：继承的 request_deadline 已过期，先行 SDK 不得被派发（零调用）。"""
    from src.patches.eastmoney_patch import request_deadline

    sdk = _RecordingTickFlowSdk()
    manager = _manager_with_tickflow(sdk)

    token = request_deadline.set(time.monotonic() - 1.0)
    try:
        indices = manager.get_main_indices(region="cn")
        stats = manager.get_market_stats(purpose="prfix-expired-budget")
    finally:
        request_deadline.reset(token)

    assert sdk.calls == [], f"过期继承预算仍派发了先行 SDK: {sdk.calls}"
    assert indices == [] and stats == {}


def test_valid_budget_tickflow_control() -> None:
    """对照（预算充足）：先行 SDK 正常派发并返回有效结果。"""
    from src.patches.eastmoney_patch import request_deadline

    sdk = _RecordingTickFlowSdk()
    manager = _manager_with_tickflow(sdk)

    token = request_deadline.set(time.monotonic() + 20.0)
    try:
        indices = manager.get_main_indices(region="cn")
        stats = manager.get_market_stats(purpose="prfix-budget-control")
    finally:
        request_deadline.reset(token)

    assert sdk.calls == ["indices", "stats"], f"预算充足时应派发先行 SDK: {sdk.calls}"
    assert indices == [{"code": "sh000001", "current": 1.0}]
    assert stats == {"up_count": 1}


@pytest.mark.parametrize("method", ["indices", "stats"])
def test_stop_during_tickflow_selection_gates_dispatch(method: str) -> None:
    """root 反例：源选择期间收到停止——SDK 调用前必须复查（仅入口检查不足）。"""
    from src.scheduler import current_stop_event

    stop_event = threading.Event()
    sdk = _RecordingTickFlowSdk()
    manager = _manager_with_tickflow(sdk)

    def select_sets_stop() -> Any:
        stop_event.set()  # 源选择（懒初始化）期间收到停止
        return sdk

    manager._get_tickflow_fetcher = select_sets_stop

    token = current_stop_event.set(stop_event)
    try:
        if method == "indices":
            result = manager.get_main_indices(region="cn")
        else:
            result = manager.get_market_stats(purpose="prfix-stop-selection")
    finally:
        current_stop_event.reset(token)

    assert sdk.calls == [], f"源选择后未复查停止意图，SDK 仍被派发: {sdk.calls}"
    assert result in ([], {}), f"停止后不产出部分结果: {result}"


@pytest.mark.parametrize("method", ["indices", "stats"])
def test_budget_expiring_during_tickflow_selection_gates_dispatch(method: str) -> None:
    """root 反例：源选择期间继承预算耗尽——SDK 调用前必须复查。"""
    from src.patches.eastmoney_patch import request_deadline

    sdk = _RecordingTickFlowSdk()
    manager = _manager_with_tickflow(sdk)

    def slow_selection() -> Any:
        time.sleep(0.05)  # 选择耗时超过继承的剩余预算（0.02s）
        return sdk

    manager._get_tickflow_fetcher = slow_selection

    token = request_deadline.set(time.monotonic() + 0.02)
    try:
        if method == "indices":
            result = manager.get_main_indices(region="cn")
        else:
            result = manager.get_market_stats(purpose="prfix-budget-selection")
    finally:
        request_deadline.reset(token)

    assert sdk.calls == [], f"源选择后未复查剩余预算，SDK 仍被派发: {sdk.calls}"
    assert result in ([], {}), f"预算耗尽后不产出部分结果: {result}"


# ---------------------------------------------------------------------------
# 补充边界：auth/throttle 之后、真正派发前的停止复查与分类保护
# ---------------------------------------------------------------------------


def test_stop_suppresses_dispatch_after_eastmoney_auth_boundary(
    local_http_server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """停止在 auth/throttle 阶段到达时，真正派发前必须拦截（东财目标 URL 形态）。"""
    from src.patches import eastmoney_patch as em
    from src.scheduler import current_stop_event

    em.eastmoney_patch()  # 启用 auth 注入路径
    stop_event = threading.Event()

    def fake_get_nid(user_agent: str) -> Optional[str]:
        stop_event.set()  # auth 阶段（真实形态为网络+随机暂停）期间收到停止
        return "nid-prfix-test"

    monkeypatch.setattr(em, "_get_nid", fake_get_nid)
    monkeypatch.setattr(em.random, "uniform", lambda low, high: 0.0)  # 暂停归零，保持确定性

    # URL 含 push2.eastmoney.com 触发 is_target（auth 分支），但实际派发到回环
    url = f"http://127.0.0.1:{local_http_server.server_address[1]}/push2.eastmoney.com/probe"

    token = current_stop_event.set(stop_event)
    try:
        with pytest.raises(requests.RequestException):
            requests.get(url)
    finally:
        current_stop_event.reset(token)

    assert _attempted_paths(local_http_server) == [], "停止后仍经 auth 边界派发了出站请求"


def test_no_stop_eastmoney_auth_target_still_dispatches(
    local_http_server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """对照（无停止）：auth 目标 URL 正常出站（auth 分支本身不被停止门禁误伤）。"""
    from src.patches import eastmoney_patch as em

    em.eastmoney_patch()

    def fake_get_nid(user_agent: str) -> Optional[str]:
        return "nid-prfix-test"

    monkeypatch.setattr(em, "_get_nid", fake_get_nid)
    monkeypatch.setattr(em.random, "uniform", lambda low, high: 0.0)

    url = f"http://127.0.0.1:{local_http_server.server_address[1]}/push2.eastmoney.com/probe"
    response = requests.get(url)

    assert response.status_code == 200
    assert _attempted_paths(local_http_server) == ["/push2.eastmoney.com/probe"]


def test_research_cancel_classification_not_shadowed_by_stop(local_http_server) -> None:
    """研究取消与调度停止同时存在时，保持既有研究取消分类（ResearchStopped 优先）。"""
    from src.core.research_run import ResearchRun, ResearchStopped, current_research
    from src.patches.eastmoney_patch import eastmoney_patch
    from src.scheduler import current_stop_event

    eastmoney_patch(budget_only=True)
    run = ResearchRun(timeout_seconds=60.0)
    run.stop("cancelled")  # 研究取消分类
    stop_event = threading.Event()
    stop_event.set()  # 调度停止同时存在

    base = f"http://127.0.0.1:{local_http_server.server_address[1]}"
    stop_token = current_stop_event.set(stop_event)
    run_token = current_research.set(run)
    try:
        with pytest.raises(ResearchStopped):
            requests.get(base + "/research-cancel-probe")
    finally:
        current_research.reset(run_token)
        current_stop_event.reset(stop_token)

    assert _attempted_paths(local_http_server) == [], "研究取消拦截时不得真实派发"


def test_stop_during_nested_auth_not_swallowed_as_auth_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """auth 内层派发（_get_nid 网络请求）期间的停止保持原分类，不写共享失败缓存。

    _get_nid 捕获 requests.RequestException 写 5 分钟授权失败缓存；任务停止是
    调度语义，不得被误当供应商授权失败（lock/auth 嵌套边界）。
    """
    from src.patches import eastmoney_patch as em
    from src.scheduler import current_stop_event

    em.eastmoney_patch()  # 启用 auth 路径
    fresh_cache = em.AuthCache()
    fresh_cache.data = None
    fresh_cache.expire_at = 12345.0  # 哨兵：失败路径会改写为 now+300
    monkeypatch.setattr(em, "_cache", fresh_cache)

    def fake_nested_post(method: str, url: str, **kwargs: Any) -> Any:
        raise em.ScheduledStopRequested("stop during nested auth dispatch")

    monkeypatch.setattr(em.requests, "request", fake_nested_post)

    stop_event = threading.Event()
    stop_event.set()
    token = current_stop_event.set(stop_event)
    try:
        with pytest.raises(em.ScheduledStopRequested):
            em._get_nid("prfix-ua")
    finally:
        current_stop_event.reset(token)

    assert fresh_cache.expire_at == 12345.0, "停止不得被当作授权失败写入共享失败缓存"
    assert fresh_cache.data is None
