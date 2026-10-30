# -*- coding: utf-8 -*-
"""Issue #66 B：市场指数/统计取数共享截止 + 调度停止与在途工作收尾。

B1：真实本地 HTTP 服务模拟无响应/慢流/多请求 fallback（含 market_stats 同一
    契约）；共享截止真实终止 HTTP 读取、按既有局部隔离保留其他有效结果、
    并发上下文预算互不污染、后续任务可成功；服务端记录实际发出的请求路径。
    全程仅回环地址，零供应商出站。
B2：真实 main() 装配的自选股回调（经 run_with_schedule 注册，保留真实
    Scheduler/pipeline/executor/lock/repo）在受控子进程内：SIGTERM 后待启股票
    不再出站、在途有界工作收尾、终态真实（partial）；无停止但 run_full_analysis
    返回 None 时终态 failed（不伪称 success）；正常完成对照 success 且任务锁在
    进程仍存活时可再次获取（真实释放）。子进程 env 继承 gate-support 离线
    封锁（PYTHONPATH sitecustomize：外部 socket 与原生付费 SDK）。
B3：不可合作阻塞 fixture 只证明超时诊断保留任务/线程身份且不伪报退出。
停止意图实例隔离：直接 Scheduler 公开入口 / run_immediately / 两实例
counterexample（不靠测试预先 set ContextVar 补上下文）。
"""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional

import pytest

# ---------------------------------------------------------------------------
# 本地 HTTP 夹具（仅回环；stall=无响应，slowdrip=慢流，delayed=有界延迟完成；
# 服务端记录实际收到的请求路径与连接起止）
# ---------------------------------------------------------------------------


class _RecordingServer(ThreadingHTTPServer):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.requests_log: List[dict] = []
        self._log_lock = threading.Lock()

    def record(self, path: str, event: str) -> None:
        with self._log_lock:
            self.requests_log.append({"path": path, "event": event, "t": time.monotonic()})


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - http.server 契约
        server: _RecordingServer = self.server  # type: ignore[assignment]
        server.record(self.path, "open")
        try:
            if self.path.startswith("/stall"):
                time.sleep(60)  # 客户端截止后断开，写入失败自然结束
                self.send_response(200)
                self.end_headers()
            elif self.path.startswith("/slowdrip"):
                self.send_response(200)
                self.send_header("Content-Length", "100000")
                self.end_headers()
                for _ in range(100000):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.05)  # 慢速流：requests 的空闲超时永不触发
            elif self.path.startswith("/delayed"):
                time.sleep(1.0)  # 有界在途工作（B2 收尾断言）
                body = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/stats"):
                body = b'{"up": 1000, "down": 800}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            server.record(self.path, "close")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass


@pytest.fixture()
def local_http_server():
    server = _RecordingServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


class _LocalFetcher:
    """直连本地服务的候选 fetcher：模拟已安装 AKShare 的无 timeout requests 调用。"""

    def __init__(self, name: str, path: str):
        self.name = name
        self._path = path
        self._base = ""

    def bind(self, base: str) -> "_LocalFetcher":
        self._base = base
        return self

    def get_main_indices(self, region: str = "cn") -> Optional[List[dict]]:
        if region != "cn":
            return None
        import requests

        response = requests.get(self._base + self._path)  # 无 timeout（现实形态）
        response.raise_for_status()
        return [{"code": "sh000001", "name": "上证指数", "current": 1.0}]

    def get_market_stats(self) -> Optional[dict]:
        import requests

        response = requests.get(self._base + self._path)  # 无 timeout（现实形态）
        response.raise_for_status()
        return {"up": 1000, "down": 800}


@pytest.fixture()
def short_budget(monkeypatch: pytest.MonkeyPatch) -> float:
    """缩短指数/统计共享预算，避免长测试等待（默认 30s 为生产值，不在测试内改动）。"""
    from data_provider import base as data_base

    monkeypatch.setattr(data_base, "INDEX_FETCH_BUDGET_SECONDS", 3.0)
    return 3.0


def _manager_with_fetchers(*fetchers: _LocalFetcher, base: str) -> Any:
    from data_provider.base import DataFetcherManager

    manager = DataFetcherManager()
    manager._fetchers = [fetcher.bind(base) for fetcher in fetchers]
    return manager


# ---------------------------------------------------------------------------
# B1：共享截止真实终止读取（指数 + market_stats 同一契约）
# ---------------------------------------------------------------------------


def test_main_indices_deadline_terminates_stalled_read(
    local_http_server, short_budget
) -> None:
    """无响应服务：预算内真实终止（不是无限等待）；结果隔离为空且不抛出。"""
    from src.patches.eastmoney_patch import request_deadline

    assert request_deadline.get() is None, "进入前无外溢预算"
    manager = _manager_with_fetchers(
        _LocalFetcher("stall", "/stall"), base=f"http://127.0.0.1:{local_http_server.server_address[1]}"
    )
    started = time.monotonic()
    result = manager.get_main_indices(region="cn")
    elapsed = time.monotonic() - started
    assert result == []
    assert elapsed < short_budget + 2.0, f"截止未生效（耗时 {elapsed:.1f}s）"
    assert request_deadline.get() is None, "退出后上下文预算不外泄"
    paths = {entry["path"] for entry in local_http_server.requests_log}
    assert "/stall" in paths


def test_main_indices_deadline_terminates_slow_stream(
    local_http_server, short_budget
) -> None:
    """慢速流：字节持续到达使空闲超时失效，共享截止仍终止读取。"""
    manager = _manager_with_fetchers(
        _LocalFetcher("drip", "/slowdrip"), base=f"http://127.0.0.1:{local_http_server.server_address[1]}"
    )
    started = time.monotonic()
    result = manager.get_main_indices(region="cn")
    elapsed = time.monotonic() - started
    assert result == []
    assert elapsed < short_budget + 2.0, f"慢流未被共享截止终止（耗时 {elapsed:.1f}s）"


def test_market_stats_deadline_terminates_stalled_read(
    local_http_server, short_budget
) -> None:
    """market_stats 与指数同一停止/I/O 契约：无响应时预算内终止并隔离为空。"""
    manager = _manager_with_fetchers(
        _LocalFetcher("stall", "/stall"), base=f"http://127.0.0.1:{local_http_server.server_address[1]}"
    )
    started = time.monotonic()
    result = manager.get_market_stats(purpose="test")
    elapsed = time.monotonic() - started
    assert result == {}
    assert elapsed < short_budget + 2.0, f"stats 截止未生效（耗时 {elapsed:.1f}s）"


def test_index_budget_covers_fallback_chain_and_isolates(
    local_http_server, short_budget
) -> None:
    """多请求 fallback：首个候选耗尽预算后剩余候选被跳过（服务端无后续候选请
    求）；随后新一次调用（新预算）可成功——本次工作资源已释放。"""
    base = f"http://127.0.0.1:{local_http_server.server_address[1]}"
    manager = _manager_with_fetchers(
        _LocalFetcher("first", "/stall"), _LocalFetcher("second", "/delayed"), base=base
    )
    assert manager.get_main_indices(region="cn") == [], "预算耗尽后不产出部分结果"
    # 局部隔离：预算耗尽后没有向后续候选发出新请求
    attempted = [entry["path"] for entry in local_http_server.requests_log]
    assert "/delayed" not in attempted, f"预算耗尽后仍请求了后续候选: {attempted}"

    recovered = _manager_with_fetchers(_LocalFetcher("only", "/delayed"), base=base)
    assert recovered.get_main_indices(region="cn") == [
        {"code": "sh000001", "name": "上证指数", "current": 1.0}
    ]


class _StopDuringCallFetcher(_LocalFetcher):
    """首个候选 HTTP 已出站后、候选收尾前收到停止并返回空结果（模拟 root 反例
    形态）：真实在途请求沿有界读取收尾，结果为空以进入 fallback 选择——停止后
    的一切新派发（下一候选及候选内部请求）由共享边界/候选检查拦截，本回归以此
    击穿旧实现（仅预算检查）。"""

    def __init__(self, name: str, path: str, stop_event: threading.Event):
        super().__init__(name, path)
        self._stop_event = stop_event

    def get_main_indices(self, region: str = "cn") -> Optional[List[dict]]:
        super().get_main_indices(region)  # 真实 HTTP 出站（在途工作）后返回空
        self._stop_event.set()  # 候选收尾前收到停止 → 进入 fallback 选择
        return None

    def get_market_stats(self) -> Optional[dict]:
        super().get_market_stats()
        self._stop_event.set()
        return None


@pytest.mark.parametrize("kind", ["indices", "stats"])
def test_candidates_stop_checking_shared_event_mid_call(
    local_http_server, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """root 反例回归：先行候选（空结果）期间 Event 已 set，下一候选不得出站。"""
    from contextvars import copy_context

    from src.scheduler import current_stop_event

    stop_event = threading.Event()
    base = f"http://127.0.0.1:{local_http_server.server_address[1]}"
    # 先行候选与下一候选使用不同可响应路径，出站记录可区分
    first_path = "/delayed" if kind == "indices" else "/stats"
    second_path = "/stats" if kind == "indices" else "/delayed"
    manager = _manager_with_fetchers(
        _StopDuringCallFetcher("first", first_path, stop_event),
        _LocalFetcher("second", second_path),
        base=base,
    )

    outcome: dict = {}

    def run_with_stop_context() -> None:
        token = current_stop_event.set(stop_event)
        try:
            if kind == "indices":
                outcome["value"] = manager.get_main_indices(region="cn")
            else:
                outcome["value"] = manager.get_market_stats(purpose="test")
        finally:
            current_stop_event.reset(token)

    thread = threading.Thread(target=copy_context().run, args=(run_with_stop_context,))
    thread.start()
    thread.join(timeout=30)
    assert outcome["value"] in ([], {}), "停止后不产出部分结果"
    attempted = [entry["path"] for entry in local_http_server.requests_log]
    assert second_path not in attempted, f"停止后仍请求了下一候选: {attempted}"
    assert first_path in attempted, "先行候选确实出站"


def test_index_first_valid_result_still_returns_early(
    local_http_server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """对照（无停止）：先行候选有效结果直接返回，后续候选不出站（既有契约）。"""
    base = f"http://127.0.0.1:{local_http_server.server_address[1]}"
    manager = _manager_with_fetchers(
        _LocalFetcher("first", "/delayed"), _LocalFetcher("second", "/stats"), base=base
    )
    assert manager.get_main_indices(region="cn") == [
        {"code": "sh000001", "name": "上证指数", "current": 1.0}
    ]
    attempted = [entry["path"] for entry in local_http_server.requests_log]
    assert "/stats" not in attempted


def test_prefetch_stock_names_stop_event_stops_remaining_lookups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """逐股名称预取消费同一停止 Event：首个名称查询期间收到停止后，剩余名称
    不再出站；已取得/已缓存结果保留（use_bulk=False 现有语义不变）。"""
    from contextvars import copy_context

    from data_provider.base import DataFetcherManager
    from src.scheduler import current_stop_event

    stop_event = threading.Event()
    manager = DataFetcherManager()
    queries: list[str] = []

    def _first_sets_stop(code: str, allow_realtime: bool = True) -> Optional[str]:
        queries.append(code)
        if len(queries) == 1:
            stop_event.set()  # 首个名称查询期间收到停止
        return f"名称{code}"

    monkeypatch.setattr(manager, "get_stock_name", _first_sets_stop)

    def run_with_stop_context() -> None:
        token = current_stop_event.set(stop_event)
        try:
            manager.prefetch_stock_names(["600001", "600002", "600003"], use_bulk=False)
        finally:
            current_stop_event.reset(token)

    thread = threading.Thread(target=copy_context().run, args=(run_with_stop_context,))
    thread.start()
    thread.join(timeout=10)
    assert queries == ["600001"], f"停止后仍查询了剩余名称: {queries}"


def test_market_overview_stop_check_skips_stats_after_stop(
    monkeypatch: pytest.MonkeyPatch, local_http_server
) -> None:
    """同一调用者的连续取数受停止契约约束：停止后 market_stats 不再出站。"""
    import src.core.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "pipeline_stopped", lambda: True)
    fetcher = _LocalFetcher("stats", "/stats").bind(
        f"http://127.0.0.1:{local_http_server.server_address[1]}"
    )
    fake_self = SimpleNamespace(fetcher_manager=SimpleNamespace(
        get_main_indices=lambda region: [],
        get_market_stats=lambda purpose: fetcher.get_market_stats(),
    ))
    context: dict = {}
    pipeline_module.StockAnalysisPipeline._attach_market_overview(fake_self, context, "cn")
    assert "market_stats" not in context, "停止后不得进入 stats 调用"
    assert local_http_server.requests_log == [], "停止后无任何出站"


def test_index_budget_is_isolated_between_concurrent_contexts(
    local_http_server, short_budget
) -> None:
    """并发上下文预算互不污染：短预算线程被截止，长预算线程完成同一服务。"""
    from src.patches.eastmoney_patch import eastmoney_patch, request_deadline

    eastmoney_patch(budget_only=True)
    base = f"http://127.0.0.1:{local_http_server.server_address[1]}"
    results: dict[str, Any] = {}

    def short_context() -> None:
        token = request_deadline.set(time.monotonic() + 1.0)
        try:
            results["short"] = _LocalFetcher("short", "/slowdrip").bind(base).get_main_indices(region="cn")
        except Exception as exc:  # noqa: BLE001 - 截止表现为 requests 异常
            results["short"] = f"stopped:{type(exc).__name__}"
        finally:
            request_deadline.reset(token)

    def long_context() -> None:
        token = request_deadline.set(time.monotonic() + 20.0)
        try:
            results["long"] = _LocalFetcher("long", "/delayed").bind(base).get_main_indices(region="cn")
        finally:
            request_deadline.reset(token)

    threads = [threading.Thread(target=short_context), threading.Thread(target=long_context)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert str(results["short"]).startswith("stopped:"), "短预算必须被真实截止"
    assert results["long"] == [{"code": "sh000001", "name": "上证指数", "current": 1.0}], (
        "长预算上下文不被短预算污染"
    )


# ---------------------------------------------------------------------------
# B2：真实 main() 自选股回调受控子进程（保留真实 Scheduler/pipeline/executor/
# lock/repo；仅替身外部配置/供应商/模型/通知/后台服务）
# ---------------------------------------------------------------------------

_CHILD_SCRIPT = r"""
# —— 测试级离线防护（在任何 main/provider import 之前安装；CI 无本机产物亦可运行）：
# 仅回环 socket + 禁止原生付费 Choice SDK。与既有 offline guard 形状一致。 ——
import ipaddress
import socket
import sys

_real_connect = socket.socket.connect


def _local_only(address):
    if not isinstance(address, tuple):
        return True
    try:
        return ipaddress.ip_address(address[0]).is_loopback
    except (ValueError, IndexError):
        return str(address[0]) == "localhost"


def _guarded_connect(self, address):
    if not _local_only(address):
        raise OSError("child guard: external sockets disabled (loopback-only test)")
    return _real_connect(self, address)


socket.socket.connect = _guarded_connect


class _NoNativeChoice:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "EmQuantAPI":
            raise ModuleNotFoundError("child guard: native Choice SDK disabled")
        return None


sys.meta_path.insert(0, _NoNativeChoice())

# 断言防护已生效（不发真实外网探针）
assert socket.socket.connect is _guarded_connect, "child socket guard not installed"
try:
    import EmQuantAPI  # noqa: F401
    raise AssertionError("child guard: native Choice SDK import should be blocked")
except ModuleNotFoundError:
    pass

import os
import time

sys.argv = ["main.py", "--schedule", "--no-notify"]

state_dir = os.environ["STATE_DIR"]
slow_url = os.environ["SLOW_URL"]
failure_mode = os.environ.get("FAILURE_MODE", "")

import requests

# —— 外部边界替身：个股取数/分析改为对本地 delayed 的真实有界 HTTP ——
from src.analyzer import AnalysisResult
from src.core.pipeline import StockAnalysisPipeline
from data_provider.base import DataFetcherManager
from src.services import supply_chain_report_service


def bounded_fetch(self, code, current_time=None):
    with open(os.path.join(state_dir, "outbound.log"), "a", encoding="utf-8") as fh:
        fh.write(f"started:{code}\n")
        fh.flush()
    requests.get(f"{slow_url}/delayed", timeout=(2, 5))  # 在途有界工作
    with open(os.path.join(state_dir, "outbound.log"), "a", encoding="utf-8") as fh:
        fh.write(f"done:{code}\n")
    return True, None


def bounded_analyze(self, code, report_type, **kwargs):
    time.sleep(0.05)
    result = AnalysisResult(
        code=code, name=code, sentiment_score=50,
        trend_prediction="震荡", operation_advice="观望",
    )
    result.success = True
    result.query_id = f"q-{code}"
    return result


StockAnalysisPipeline.fetch_and_save_stock_data = bounded_fetch  # type: ignore[assignment]
StockAnalysisPipeline.analyze_stock = bounded_analyze  # type: ignore[assignment]

if failure_mode == "none_result":
    # pipeline.run 的批量预取（>=5 只才触发）真实抛出 → run_full_analysis
    # 广捕获返回 None（既有 broad catch 路径，非停止）
    def _raise(self, codes):
        raise RuntimeError("prefetch fixture failure")
    DataFetcherManager.prefetch_realtime_quotes = _raise  # type: ignore[assignment]
else:
    DataFetcherManager.prefetch_realtime_quotes = lambda self, codes: 0  # type: ignore[assignment]
# 批量名称预取与后台 Serenity 报告为基础设施外部调用：替身静默
DataFetcherManager.prefetch_stock_names = lambda self, codes, use_bulk=False: None  # type: ignore[assignment]
supply_chain_report_service.supply_chain_report_service.generate_report_async = lambda **kwargs: {
    "status": "skipped"
}

import main

main.main()
"""


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _child_env(tmp_path: Path, server: _RecordingServer, codes: List[str], mode: str) -> dict:
    state_dir = tmp_path / f"state-{mode}"
    state_dir.mkdir(exist_ok=True)
    env = {
        k: os.environ[k]
        for k in ("PATH", "HOME", "LANG", "TMPDIR", "TERM", "PYTHONPATH", "ISSUE64_REAL_CHOICE_MODULE")
        if k in os.environ
    }
    env.update(
        # 子进程脚本自带测试级离线防护（仅回环 socket + 禁原生 Choice SDK），
        # 不依赖本机 ignored 产物；继承父进程已验证的 PYTHONPATH 外层防护。
        ENV_FILE=str(tmp_path / "blank.env"),
        DATABASE_PATH=str(tmp_path / f"db-{mode}.sqlite"),
        LOG_DIR=str(tmp_path / f"logs-{mode}"),
        NO_PROXY="127.0.0.1,localhost",
        no_proxy="127.0.0.1,localhost",
        LITELLM_LOCAL_MODEL_COST_MAP="True",
        CHOICE_FORCE_LOGIN="false",
        LITELLM_TELEMETRY="False",
        # 调度配置：注册真实自选股回调并立即执行
        STOCK_LIST=",".join(codes),
        WATCHLIST_ANALYSIS_TIME="10:00",
        SCHEDULE_RUN_IMMEDIATELY="true",
        TRADING_DAY_CHECK_ENABLED="false",
        MARKET_REVIEW_ENABLED="false",
        SINGLE_STOCK_NOTIFY="false",
        MAX_WORKERS="2",
        STATE_DIR=str(state_dir),
        SLOW_URL=f"http://127.0.0.1:{server.server_address[1]}",
        FAILURE_MODE="none_result" if mode == "none" else "",
    )
    (tmp_path / "blank.env").write_text("")
    return env


def _read_outbound(state_dir: Path) -> str:
    outbound = state_dir / "outbound.log"
    return outbound.read_text(encoding="utf-8") if outbound.exists() else ""


def _wait_for(predicate, timeout: float, message: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"等待超时: {message}")


def _terminal_status(database_path: str, timeout: float) -> Optional[str]:
    found: List[str] = []

    def _poll() -> bool:
        if not Path(database_path).exists():
            return False
        try:
            with sqlite3.connect(database_path) as conn:
                rows = conn.execute(
                    "SELECT status FROM scheduled_task_log ORDER BY id"
                ).fetchall()
            found[:] = [row[0] for row in rows]
        except sqlite3.OperationalError:
            return False
        return bool(found) and found[-1] in {"success", "partial", "failed"}

    _wait_for(_poll, timeout, "任务终态未落库")
    return found[-1]


def _try_flock(lock_path: Path) -> bool:
    import fcntl

    if not lock_path.exists():
        return False
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return True
    except (BlockingIOError, OSError):
        return False
    finally:
        handle.close()


def _lock_path(database_path: str) -> Path:
    from src.core.scheduled_task_lock import task_lock_path

    return task_lock_path(
        type("C", (), {"database_path": database_path})(), "watchlist_analysis"
    )


def _spawn_child(env: dict, tmp_path: Path, mode: str) -> subprocess.Popen:
    """子进程 stdout 落盘（防止 PIPE 缓冲塞满阻塞；失败时可读日志）。"""
    child_log = tmp_path / f"child-{mode}.log"
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD_SCRIPT],
        cwd=_repo_root(),
        env=env,
        stdout=child_log.open("w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )


def test_scheduler_sigterm_skips_queued_and_drains_bounded_work(
    tmp_path: Path, local_http_server
) -> None:
    """SIGTERM：排队股票不再出站、在途有界工作收尾、终态 partial、进程自然退出。"""
    codes = [f"60000{i}" for i in range(6)]
    env = _child_env(tmp_path, local_http_server, codes, "stop")
    state_dir = Path(env["STATE_DIR"])
    database = env["DATABASE_PATH"]

    process = _spawn_child(env, tmp_path, "stop")
    try:
        _wait_for(
            lambda: _read_outbound(state_dir).count("started:") >= 1,
            30,
            "在途工作未开始",
        )
        # 首批在途时发送 SIGTERM（delayed=1s，任务仍在途）
        time.sleep(0.3)
        process.send_signal(signal.SIGTERM)
        status = _terminal_status(database, 30)
        assert status == "partial", f"停止后终态应为 partial: {status}"
        process.communicate(timeout=60)
        assert process.returncode == 0, (
            f"子进程未自然退出（rc={process.returncode}）\n"
            f"{(tmp_path / 'child-stop.log').read_text(encoding='utf-8')[-3000:]}"
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()

    log_text = _read_outbound(state_dir)
    started_codes = {
        line.split(":", 1)[1] for line in log_text.splitlines() if line.startswith("started:")
    }
    done_codes = {
        line.split(":", 1)[1] for line in log_text.splitlines() if line.startswith("done:")
    }
    # 在途工作真实收尾；待启股票不再出站（远小于全部 6 只）
    assert done_codes, f"在途工作未收尾: {log_text}"
    assert len(started_codes) <= 4, f"停止后仍启动了排队股票: {sorted(started_codes)}"
    assert len(started_codes) < len(codes), "排队股票应至少有部分未开始"


def test_scheduler_normal_completion_control_and_live_lock_release(
    tmp_path: Path, local_http_server
) -> None:
    """对照：全部完成、终态 success；任务收尾后、进程仍存活时锁真实可再取；
    随后 SIGTERM 干净退出。"""
    codes = [f"60000{i}" for i in range(4)]
    env = _child_env(tmp_path, local_http_server, codes, "control")
    state_dir = Path(env["STATE_DIR"])
    database = env["DATABASE_PATH"]

    process = _spawn_child(env, tmp_path, "control")
    try:
        status = _terminal_status(database, 60)
        assert status == "success", f"对照终态应为 success: {status}"
        # 任务已收尾、子进程仍存活：锁必须真实可再取（不是靠 pid 死亡判 stale）
        assert process.poll() is None, "对照子进程应仍在调度循环中运行"
        _wait_for(lambda: _try_flock(_lock_path(database)), 5, "收尾后锁仍不可获取")
        assert _read_outbound(state_dir).count("started:") == len(codes), "对照应全部执行"
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=30)
        assert process.returncode == 0, (
            f"子进程未自然退出\n{(tmp_path / 'child-control.log').read_text(encoding='utf-8')[-2000:]}"
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def test_none_result_without_stop_records_failed(
    tmp_path: Path, local_http_server
) -> None:
    """无停止但 run_full_analysis 返回 None（真实 broad catch 路径）：终态 failed，
    不伪称 success。"""
    codes = [f"60000{i}" for i in range(6)]  # >=5 触发批量预取替身的真实抛出
    env = _child_env(tmp_path, local_http_server, codes, "none")
    database = env["DATABASE_PATH"]

    process = _spawn_child(env, tmp_path, "none")
    try:
        status = _terminal_status(database, 60)
        assert status == "failed", f"None 且无停止的终态应为 failed: {status}"
        # 调度循环为常驻进程：终态断言后 SIGTERM 干净收尾
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=30)
        assert process.returncode == 0, (
            f"子进程未自然退出\n{(tmp_path / 'child-none.log').read_text(encoding='utf-8')[-2000:]}"
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


# ---------------------------------------------------------------------------
# B3 + 实例隔离：不可合作 fixture 与停止意图所有权（真实 wrapper 路径）
# ---------------------------------------------------------------------------


def test_stop_intent_belongs_to_scheduler_instance_via_real_wrappers() -> None:
    """两实例/并发无关工作 counterexample：直接 Scheduler 公开入口的任务线程
    显式持有本实例 Event；A 停止不影响独立实例 B 与新建实例。"""

    from src.scheduler import Scheduler, stop_requested

    instance_a = Scheduler(schedule_time="23:59")
    instance_b = Scheduler(schedule_time="23:59")
    worker_observed: dict[str, Optional[bool]] = {"before": None, "after": None, "busy_b": None}
    release = threading.Event()

    instance_a.add_daily_task(
        name="probe_a",
        task=lambda: (
            worker_observed.__setitem__("before", stop_requested()),
            instance_a.shutdown_handler._signal_handler(signal.SIGTERM, None),
            worker_observed.__setitem__("after", stop_requested()),
        ),
        schedule_time="23:59",
        run_immediately=True,
    )
    instance_b.add_daily_task(
        name="probe_b",
        task=lambda: worker_observed.__setitem__("busy_b", stop_requested()),
        schedule_time="23:59",
        run_immediately=True,
    )
    _wait_for(lambda: worker_observed["after"] is not None, 5, "A 的任务线程未执行")
    _wait_for(lambda: worker_observed["busy_b"] is not None, 5, "B 的任务线程未执行")
    release.set()

    assert worker_observed["before"] is False, "停止前 worker 不应看到停止意图"
    assert worker_observed["after"] is True, "A 停止后其 worker 应看到停止意图"
    assert worker_observed["busy_b"] is False, "A 停止不得影响独立实例 B 的 worker"
    assert instance_a.shutdown_handler.should_shutdown, "A 已停止"
    assert not instance_b.shutdown_handler.should_shutdown, "B 未受影响"

    # 新建实例 C：不继承 A 的停止意图
    instance_c = Scheduler(schedule_time="23:59")
    assert not instance_c.shutdown_handler.should_shutdown, "新实例不得继承旧停止意图"


def test_uncooperative_task_is_reported_not_claimed_finished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不可合作阻塞 fixture：观察预算届满后记录任务/线程身份与栈，不伪称
    全部退出（本测试不把该 fixture 当作所有 SDK 已解决的证据）。"""
    import logging

    from src.scheduler import Scheduler

    monkeypatch.setattr("src.scheduler.SHUTDOWN_OBSERVE_SECONDS", 1.0)
    scheduler = Scheduler(schedule_time="23:59")
    release = threading.Event()
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logging.getLogger("src.scheduler").addHandler(_Capture())
    scheduler.add_daily_task(
        name="blocked",
        task=release.wait,  # 不可合作：不会在停止时返回
        schedule_time="23:59",
        run_immediately=True,
    )

    def _stop_and_run() -> None:
        scheduler.shutdown_handler._signal_handler(signal.SIGTERM, None)
        scheduler.run()

    runner = threading.Thread(target=_stop_and_run, daemon=True)
    runner.start()
    runner.join(timeout=30)
    assert not runner.is_alive(), "调度循环未在观察预算后返回"
    release.set()  # 测试自清理（teardown，不计为产品自然退出证据）

    messages = [record.getMessage() for record in records]
    assert any("仍有任务未收尾" in message for message in messages), f"缺少诚实诊断: {messages}"
    assert any("scheduler-task-blocked" in message for message in messages), (
        f"诊断未保留任务/线程身份: {messages}"
    )
    assert not any("全部任务已完成" in message for message in messages)


def test_stop_before_dispatch_prevents_all_immediate_callbacks() -> None:
    """停止后派发入口 guard：stop() 之后 add_daily_task / set_daily_task /
    add_background_task(run_immediately=True) 都不再调用回调（独立实例不受影响）。"""
    from src.scheduler import Scheduler

    scheduler = Scheduler(schedule_time="23:59")
    calls: list[str] = []
    scheduler.stop()

    scheduler.set_daily_task(lambda: calls.append("single"), run_immediately=True)
    scheduler.add_daily_task(
        name="named", task=lambda: calls.append("named"),
        schedule_time="23:59", run_immediately=True,
    )
    scheduler.add_background_task(
        lambda: calls.append("background"), interval_seconds=60,
        run_immediately=True, name="background",
    )
    time.sleep(0.3)
    assert calls == [], f"停止后仍派发了回调: {calls}"

    # 独立实例不受该停止影响：任务照常派发
    other = Scheduler(schedule_time="23:59")
    other_calls: list[str] = []
    release = threading.Event()
    other.add_daily_task(
        name="independent", task=lambda: (other_calls.append("ran"), release.wait(timeout=2)),
        schedule_time="23:59", run_immediately=True,
    )
    _wait_for(lambda: bool(other_calls), 5, "独立实例任务未派发")
    release.set()


def test_stop_method_sets_event_and_single_task_guard_keeps_identity() -> None:
    """stop() 设置本实例 Event；单任务超龄守卫保留活线程身份，结束后释放。"""
    from src.scheduler import Scheduler

    scheduler = Scheduler(schedule_time="23:59")
    runs: list[str] = []
    release = threading.Event()

    scheduler.add_daily_task(
        name="slow",
        task=lambda: (runs.append("start"), release.wait(timeout=5)),
        schedule_time="23:59",
        run_immediately=True,
    )
    _wait_for(lambda: bool(runs), 5, "任务未开始")
    # 在途：重复触发被跳过（活身份不因超龄清理）
    scheduler._safe_run_named_task("slow")
    assert len(runs) == 1, "在途任务不得被重复派发"
    assert "slow" in scheduler._running_named_tasks
    release.set()
    thread = scheduler._named_task_threads["slow"]
    thread.join(timeout=10)
    assert "slow" not in scheduler._running_named_tasks, "任务结束后守卫应释放"

    # stop() 同步设置 Event（不只 _running=False）
    assert not scheduler.shutdown_handler.should_shutdown
    scheduler.stop()
    assert scheduler.shutdown_handler.should_shutdown


# ---------------------------------------------------------------------------
# 任务锁：活 owner 身份保留（超龄不重复派发）；已死 owner 立即过期
# ---------------------------------------------------------------------------


class _LockConfig:
    def __init__(self, database_path: str) -> None:
        self.database_path = database_path


def test_live_owner_lock_is_never_stale_by_age(tmp_path: Path) -> None:
    """存活 owner 超龄：锁不得被清理/重复获取（过期运行标记不得重复派发）。"""
    import json as _json
    from datetime import datetime, timedelta

    from src.core.scheduled_task_lock import (
        _is_stale_lock,
        acquire_task_lock,
        release_task_lock,
        task_lock_path,
    )

    config = _LockConfig(str(tmp_path / "db.sqlite"))
    lock_path = task_lock_path(config, "aged_task")
    token = acquire_task_lock(config, "aged_task")
    assert token is not None
    # 把 started_at 拨回超龄时刻，但 owner PID 仍存活（本进程）
    metadata = _json.loads(lock_path.read_text(encoding="utf-8"))
    metadata["started_at"] = (datetime.now() - timedelta(hours=3)).isoformat()
    lock_path.write_text(_json.dumps(metadata), encoding="utf-8")

    assert not _is_stale_lock(lock_path, 7200), "存活 owner 不得按年龄判过期"
    assert acquire_task_lock(config, "aged_task") is None, "超龄活 owner 不得被重复获取"
    release_task_lock(token)
    assert acquire_task_lock(config, "aged_task") is not None, "真实释放后可再次获取"
