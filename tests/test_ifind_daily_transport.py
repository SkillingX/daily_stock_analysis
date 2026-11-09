# -*- coding: utf-8 -*-
"""issue #86：iFinD 导出 CSV 传输边界测试（本机受控回环 server，零外部 TCP/DNS）。

保留真实 curl 下载/流式读取/状态码/大小/停止语义；URL 门槛（精确域校验）
在 test_ifind_daily_adapter.py 单独覆盖，本文件直接以已验证 URL 形态调用
``stream_export_csv``。
"""
from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import monotonic

import pytest

from data_provider import ifind_daily_adapter as A
from data_provider.base import DataFetchError
from src.core.research_run import ResearchRun, ResearchStopped
from src.patches import eastmoney_patch


@pytest.fixture()
def loopback_server():
    """本机回环 HTTP server：记录请求头并按路径返回受控行为。"""
    records: dict[str, object] = {"headers": None, "paths": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            records["paths"] = [*records["paths"], self.path]  # type: ignore[misc]
            records["headers"] = dict(self.headers)
            if self.path.startswith("/ok"):
                body = b"col\n" + b"1\n" * 10
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.startswith("/redirect"):
                self.send_response(302)
                self.send_header("Location", "/ok")
                self.end_headers()
                return
            if self.path.startswith("/missing"):
                self.send_response(404)
                self.send_header("Content-Length", "9")
                self.end_headers()
                self.wfile.write(b"not found")
                return
            if self.path.startswith("/dup-length"):
                body = b"a\n1\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                if "good-bad" in self.path:
                    self.send_header("Content-Length", "4")
                    self.send_header("Content-Length", "100")
                else:
                    self.send_header("Content-Length", "100")
                    self.send_header("Content-Length", "4")
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.startswith("/dup-encoding"):
                body = b"a\n1\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                self.send_header("Content-Length", "4")
                if "good-bad" in self.path:
                    self.send_header("Content-Encoding", "identity")
                    self.send_header("Content-Encoding", "gzip")
                else:
                    self.send_header("Content-Encoding", "gzip")
                    self.send_header("Content-Encoding", "identity")
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.startswith("/equal-length"):
                body = b"a\n1\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                self.send_header("Content-Length", "4")
                self.send_header("Content-Length", "4")
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.startswith("/bigger-declared"):
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                self.send_header("Content-Length", str(A._MAX_EXPORT_BYTES + 1))
                self.end_headers()
                return
            if self.path.startswith("/bigger-body"):
                # 不声明超限 Content-Length：专测实际流读取过程中的字节上限
                self.send_response(200)
                self.send_header("Connection", "close")
                self.end_headers()
                for _ in range(20):
                    self.wfile.write(b"x" * (A._MAX_EXPORT_BYTES // 16))
                    self.wfile.flush()
                return
            if self.path.startswith("/slow"):
                self.send_response(200)
                self.send_header("Content-Length", "300000")
                self.end_headers()
                for _ in range(6):
                    self.wfile.write(b"y" * 50000)
                    self.wfile.flush()
                    threading.Event().wait(0.08)
                return
            if self.path.startswith("/badutf8"):
                body = b"\xff\xfe\x00bad"
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(500)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", records
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_stream_success_without_credentials(loopback_server):
    base, records = loopback_server
    text = A.stream_export_csv(f"{base}/ok")
    assert text.startswith("col\n")
    headers = records["headers"] or {}  # type: ignore[union-attr]
    assert "Authorization" not in headers
    assert "Cookie" not in headers


def test_stream_rejects_redirect_without_following(loopback_server):
    base, records = loopback_server
    with pytest.raises(DataFetchError, match="302"):
        A.stream_export_csv(f"{base}/redirect")
    # 禁止跟随重定向：redirect 之后不应再请求 /ok
    assert all(path != "/ok" for path in records["paths"])  # type: ignore[union-attr]


def test_stream_rejects_error_status(loopback_server):
    base, _records = loopback_server
    with pytest.raises(DataFetchError, match="404"):
        A.stream_export_csv(f"{base}/missing")


def test_stream_rejects_oversized_content_length(loopback_server):
    base, _records = loopback_server
    with pytest.raises(DataFetchError, match="content-length"):
        A.stream_export_csv(f"{base}/bigger-declared")


def test_stream_aborts_when_body_exceeds_bound(loopback_server):
    base, _records = loopback_server
    with pytest.raises(DataFetchError, match="byte bound"):
        A.stream_export_csv(f"{base}/bigger-body")


def test_stream_deadline_aborts_slow_stream(loopback_server):
    base, _records = loopback_server
    # 中止信号有两条合法路径：逐块 deadline 检查抛 TimeoutError；或进程内
    # 已安装的 eastmoney_patch socket 读取预算（全量套件中被早先测试永久安装）
    # 在 socket 层抛超时并被传输边界归一为 DataFetchError。两者都是有界中止。
    token = eastmoney_patch.request_deadline.set(monotonic() + 0.25)
    started = monotonic()
    try:
        with pytest.raises((TimeoutError, DataFetchError)):
            A.stream_export_csv(f"{base}/slow")
    finally:
        eastmoney_patch.request_deadline.reset(token)
    assert monotonic() - started < 5.0  # 有界中止，不能拖完整个慢流


def test_stream_deadline_aborts_slow_stream_with_global_read_budget(loopback_server, monkeypatch):
    """全量套件条件：eastmoney_patch 的 socket 读取预算已永久安装。"""
    from src.patches import eastmoney_patch as patch_module

    was_installed = patch_module._read_budget_installed
    original_readinto = socket.SocketIO.readinto
    try:
        patch_module.eastmoney_patch(budget_only=True)
        base, _records = loopback_server
        token = eastmoney_patch.request_deadline.set(monotonic() + 0.25)
        started = monotonic()
        try:
            with pytest.raises((TimeoutError, DataFetchError)):
                A.stream_export_csv(f"{base}/slow")
        finally:
            eastmoney_patch.request_deadline.reset(token)
        assert monotonic() - started < 5.0
    finally:
        # 恢复本用例安装前的进程状态（未安装则卸载包装）
        if not was_installed:
            socket.SocketIO.readinto = original_readinto
            patch_module._read_budget_installed = False


def test_stream_propagates_research_stop_before_request(loopback_server):
    base, _records = loopback_server
    run = ResearchRun(timeout_seconds=30)
    run.stop("cancelled")
    token = eastmoney_patch.request_deadline.set(run.deadline)
    research_token = None
    try:
        from src.core.research_run import current_research

        research_token = current_research.set(run)
        with pytest.raises(ResearchStopped):
            A.stream_export_csv(f"{base}/ok")
    finally:
        eastmoney_patch.request_deadline.reset(token)
        if research_token is not None:
            from src.core.research_run import current_research

            current_research.reset(research_token)


def test_stream_propagates_stop_mid_stream(loopback_server):
    base, _records = loopback_server
    run = ResearchRun(timeout_seconds=30)
    token = eastmoney_patch.request_deadline.set(run.deadline)
    research_token = None
    try:
        from src.core.research_run import current_research

        research_token = current_research.set(run)
        timer = threading.Timer(0.12, run.stop, args=("cancelled",))
        timer.start()
        try:
            with pytest.raises(ResearchStopped):
                A.stream_export_csv(f"{base}/slow")
        finally:
            timer.cancel()
    finally:
        eastmoney_patch.request_deadline.reset(token)
        if research_token is not None:
            from src.core.research_run import current_research

            current_research.reset(research_token)


def test_stream_rejects_invalid_utf8(loopback_server):
    base, _records = loopback_server
    with pytest.raises(DataFetchError, match="utf-8"):
        A.stream_export_csv(f"{base}/badutf8")


def test_stream_wraps_transport_failure(loopback_server):
    base, _records = loopback_server
    # 连接被拒绝的端口：curl 失败被归一为传输错误
    with pytest.raises(DataFetchError, match="transport"):
        A.stream_export_csv("http://127.0.0.1:1/ok")


def test_duplicate_critical_headers_rejected_before_collapse(loopback_server):
    base, _records = loopback_server
    with pytest.raises(DataFetchError, match="content-length"):
        A.stream_export_csv(f"{base}/dup-length")
    with pytest.raises(DataFetchError, match="content-length"):
        A.stream_export_csv(f"{base}/dup-length-good-bad")
    with pytest.raises(DataFetchError, match="content-encoding"):
        A.stream_export_csv(f"{base}/dup-encoding")
    with pytest.raises(DataFetchError, match="content-encoding"):
        A.stream_export_csv(f"{base}/dup-encoding-good-bad")
    assert A.stream_export_csv(f"{base}/equal-length") == "a\n1\n"


def test_raw_loopback_rejects_malformed_length_and_repeated_framing():
    """原始字节回环：非法 Content-Length token 与矛盾 Transfer-Encoding 在折叠前拒绝。"""
    import socket
    import threading

    cases = {
        b"HTTP/1.1 200 OK\r\nContent-Type: text/csv\r\nContent-Length: +4\r\nContent-Length: 4\r\n\r\na\n1\n": "content-length",
        b"HTTP/1.1 200 OK\r\nContent-Type: text/csv\r\nContent-Length: 4\r\nContent-Length: +4\r\n\r\na\n1\n": "content-length",
        b"HTTP/1.1 200 OK\r\nContent-Type: text/csv\r\nContent-Length: 0_4\r\nContent-Length: 4\r\n\r\na\n1\n": "content-length",
        b"HTTP/1.1 200 OK\r\nContent-Type: text/csv\r\nContent-Length: 4\r\nContent-Length: 0_4\r\n\r\na\n1\n": "content-length",
        b"HTTP/1.1 200 OK\r\nContent-Type: text/csv\r\nContent-Length: +4\r\n\r\na\n1\n": "content-length",
        b"HTTP/1.1 200 OK\r\nContent-Type: text/csv\r\nTransfer-Encoding: gzip\r\nTransfer-Encoding: chunked\r\n\r\n4\r\na\n1\n\r\n0\r\n\r\n": "transfer-encoding",
        b"HTTP/1.1 200 OK\r\nContent-Type: text/csv\r\nTransfer-Encoding: chunked\r\nTransfer-Encoding: gzip\r\n\r\n4\r\na\n1\n\r\n0\r\n\r\n": "transfer-encoding",
        b"HTTP/1.1 200 OK\r\nContent-Type: text/csv\r\nContent-Length: 4\r\nTransfer-Encoding: chunked\r\n\r\n4\r\na\n1\n\r\n0\r\n\r\n": "framing",
    }
    positives = {
        b"HTTP/1.1 200 OK\r\nContent-Type: text/csv\r\nContent-Length: 4\r\n\r\na\n1\n": "a\n1\n",
        b"HTTP/1.1 200 OK\r\nContent-Type: text/csv\r\nContent-Length: 4\r\nContent-Length: 4\r\n\r\na\n1\n": "a\n1\n",
        b"HTTP/1.1 200 OK\r\nContent-Type: text/csv\r\nTransfer-Encoding: chunked\r\n\r\n4\r\na\n1\n\r\n0\r\n\r\n": "a\n1\n",
    }

    def serve(payload: bytes) -> str:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        def accept_one():
            try:
                conn, _addr = listener.accept()
                with conn:
                    conn.recv(8192)
                    conn.sendall(payload)
            finally:
                listener.close()

        thread = threading.Thread(target=accept_one)
        thread.start()
        try:
            return A.stream_export_csv(f"http://127.0.0.1:{port}/")
        finally:
            thread.join(2)

    for payload, match in cases.items():
        with pytest.raises(DataFetchError, match=match):
            serve(payload)
    for payload, expected in positives.items():
        assert serve(payload) == expected


# ---------------------------------------------------------------------------
# 修正组3：一次完整尝试的有限绝对 deadline（不依赖全局 socket 补丁）
# ---------------------------------------------------------------------------
def test_attempt_deadline_created_without_outer(monkeypatch):
    from data_provider.ifind_daily_adapter import _EXPORT_STAGE_SECONDS, attempt_deadline
    from src.patches import eastmoney_patch

    token = eastmoney_patch.request_deadline.set(None)
    try:
        _assert_attempt_deadline_without_outer(attempt_deadline, _EXPORT_STAGE_SECONDS)
    finally:
        eastmoney_patch.request_deadline.reset(token)


def _assert_attempt_deadline_without_outer(attempt_deadline, export_stage):
    from time import monotonic

    start = monotonic()
    deadline = attempt_deadline(start)
    assert deadline is not None
    budget = 30.0 + export_stage
    reserve = min(8.0, budget * 0.3)
    assert start + (budget - reserve) - 1 <= deadline <= start + (budget - reserve) + 1


def test_attempt_deadline_capped_by_smaller_outer(monkeypatch):
    from data_provider.ifind_daily_adapter import attempt_deadline
    from src.patches import eastmoney_patch
    from time import monotonic

    outer = monotonic() + 5.0
    token = eastmoney_patch.request_deadline.set(outer)
    try:
        assert attempt_deadline() == outer
    finally:
        eastmoney_patch.request_deadline.reset(token)


def test_fetcher_sets_finite_attempt_deadline_without_outer(monkeypatch):
    """直接入口无外层 deadline：MCP 与导出阶段都看到同一个有限绝对 deadline。"""
    from data_provider.ifind_daily_adapter import IfindDailyFetcher
    from data_provider.ifind_fundamental_adapter import IfindData, IfindFetcher
    from src.patches import eastmoney_patch
    from tests.ifind_daily_testutil import daily_fixture, strict_calendar
    from time import monotonic

    strict_calendar(monkeypatch)
    seen: dict = {"query": None, "download": None}

    def fake_query(self, tool, question, *, deadline=None):
        seen["query"] = eastmoney_patch.request_deadline.get()
        return IfindData(answer=daily_fixture("halt")["answer"],
                         indicators_params=daily_fixture("halt")["indicators_params"])

    monkeypatch.setattr(IfindFetcher, "query", fake_query)
    start = monotonic()
    fetcher = IfindDailyFetcher(IfindFetcher("https://fixture.invalid/mcp", "test-only"))
    df = fetcher.get_daily_data("600150.SH", "2025-08-19", "2025-08-20")
    assert len(df) == 2
    assert seen["query"] is not None and seen["query"] <= start + 51.0  # 30+20 预算内
    # 尝试结束后恢复原状（无外层时不残留自建 deadline）
    assert eastmoney_patch.request_deadline.get() is None


def test_drip_stream_aborts_within_budget(loopback_server, monkeypatch):
    """8B/40ms 慢滴流：20ms 预算内中止（逐字节检查，无需全局 socket 补丁）。"""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from time import monotonic
    from src.patches import eastmoney_patch
    from src.patches import eastmoney_patch as patch_module

    # 确保未安装全局 socket 读取预算包装（复现 root 探测条件）
    was_installed = patch_module._read_budget_installed
    original_readinto = socket.SocketIO.readinto
    if was_installed:
        socket.SocketIO.readinto = patch_module.original_request and original_readinto
        patch_module._read_budget_installed = False
    try:
        drip_records: dict = {"hits": 0}

        class DripHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                drip_records["hits"] += 1
                self.send_response(200)
                self.send_header("Content-Length", "800")
                self.end_headers()
                for _ in range(100):
                    self.wfile.write(b"12345678")
                    self.wfile.flush()
                    threading.Event().wait(0.04)

        server = ThreadingHTTPServer(("127.0.0.1", 0), DripHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/drip"
            started = monotonic()
            token = eastmoney_patch.request_deadline.set(monotonic() + 0.02)
            try:
                with pytest.raises((TimeoutError, DataFetchError)):
                    A.stream_export_csv(url)
            finally:
                eastmoney_patch.request_deadline.reset(token)
            elapsed = monotonic() - started
            assert elapsed < 0.15, f"慢滴流中止耗时 {elapsed*1000:.0f}ms，超出有界预算"
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)
    finally:
        if was_installed:
            socket.SocketIO.readinto = original_readinto
            patch_module._read_budget_installed = was_installed


def test_header_stall_aborts_within_budget(loopback_server, monkeypatch):
    """响应头停顿：连接/读取超时钳制到剩余预算，头阶段即中止。"""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from time import monotonic
    from src.patches import eastmoney_patch
    from src.patches import eastmoney_patch as patch_module

    was_installed = patch_module._read_budget_installed
    original_readinto = socket.SocketIO.readinto
    if was_installed:
        patch_module._read_budget_installed = False
    try:
        class StallHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                threading.Event().wait(0.5)  # 头部停顿远超预算
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        server = ThreadingHTTPServer(("127.0.0.1", 0), StallHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/stall"
            started = monotonic()
            token = eastmoney_patch.request_deadline.set(monotonic() + 0.02)
            try:
                with pytest.raises((TimeoutError, DataFetchError)):
                    A.stream_export_csv(url)
            finally:
                eastmoney_patch.request_deadline.reset(token)
            elapsed = monotonic() - started
            assert elapsed < 0.3, f"头部停顿中止耗时 {elapsed*1000:.0f}ms"
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)
    finally:
        if was_installed:
            socket.SocketIO.readinto = original_readinto
            patch_module._read_budget_installed = was_installed


def test_parse_phase_respects_deadline(monkeypatch):
    """解析阶段逐行复查：预算过期在解析中抛出，不等到自然结束。"""
    from src.patches import eastmoney_patch
    from time import monotonic
    from tests.ifind_daily_testutil import daily_csv_text
    from data_provider.ifind_daily_adapter import _parse_csv_rows

    token = eastmoney_patch.request_deadline.set(monotonic() - 1)  # 已过期
    try:
        with pytest.raises(TimeoutError):
            _parse_csv_rows(daily_csv_text("sz_long"), "000001.SZ")
    finally:
        eastmoney_patch.request_deadline.reset(token)


# ---------------------------------------------------------------------------
# 修正组4：无 ambient 导出凭据（合成 netrc/代理反例）
# ---------------------------------------------------------------------------
def test_export_transport_ignores_synthetic_netrc_and_proxies(loopback_server, monkeypatch, tmp_path):
    """合成 netrc/无效代理都不进入导出请求：实际请求头零凭据、直连回环。"""
    import os

    base, records = loopback_server
    netrc_file = tmp_path / "synthetic.netrc"
    netrc_file.write_text("machine o.thsi.cn\nlogin probe-user\npassword probe-pass\n")
    netrc_file.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc_file))
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    # 生产路径（不注入 fetch）：隔离一次性传输
    text = A.stream_export_csv(f"{base}/ok")
    assert text.startswith("col\n")
    headers = records["headers"] or {}  # type: ignore[union-attr]
    assert "Authorization" not in headers, "合成 netrc 凭据进入了导出请求"
    assert "Cookie" not in headers
    # 无效代理未被采用（否则直连回环不可能成功）
    assert any(path == "/ok" for path in records["paths"])  # type: ignore[union-attr]


def test_header_drip_respects_absolute_deadline():
    """响应头逐字节滴入：20ms 绝对 deadline 不能等头拼完（root 反例 275ms）。

    不要求进程级读预算补丁未安装——全量套件里早先测试会永久装上它。
    独立性由 test_bounded_transport_independent_of_global_read_patch 的干净子进程证明。
    """
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from src.patches import eastmoney_patch as patch

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            try:
                for byte in b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok":
                    self.connection.sendall(bytes([byte]))
                    time.sleep(0.005)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        started = monotonic()
        token = patch.request_deadline.set(started + 0.02)
        try:
            with pytest.raises(TimeoutError):
                A.stream_export_csv(f"http://127.0.0.1:{server.server_port}/header-drip")
        finally:
            patch.request_deadline.reset(token)
        assert monotonic() - started < 0.12
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_bounded_transport_independent_of_global_read_patch():
    """干净子进程：全局读预算未安装时，头滴流与静默体取消仍有界。"""
    import os
    import subprocess
    import sys

    script = r"""
import socket, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from data_provider import ifind_daily_adapter as A
from src.core.research_run import ResearchRun, ResearchStopped, current_research
from src.patches import eastmoney_patch as patch
assert not patch._read_budget_installed
assert socket.SocketIO.readinto.__module__ == "socket"

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass
    def do_GET(self):
        try:
            if self.path == "/header-drip":
                for byte in b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok":
                    self.connection.sendall(bytes([byte])); time.sleep(0.005)
            else:
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers(); self.wfile.flush(); time.sleep(0.4); self.wfile.write(b"ok")
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
port = server.server_port
started = time.monotonic()
token = patch.request_deadline.set(started + 0.02)
try:
    try:
        A.stream_export_csv(f"http://127.0.0.1:{port}/header-drip")
        raise SystemExit("header drip did not abort")
    except TimeoutError:
        pass
finally:
    patch.request_deadline.reset(token)
assert time.monotonic() - started < 0.12, time.monotonic() - started
run = ResearchRun(timeout_seconds=30)
research_token = current_research.set(run)
started = time.monotonic()
token = patch.request_deadline.set(started + 1.0)
timer = threading.Timer(0.03, run.stop, args=("cancelled",))
timer.start()
try:
    try:
        A.stream_export_csv(f"http://127.0.0.1:{port}/silent-body")
        raise SystemExit("silent body did not observe cancel")
    except ResearchStopped:
        pass
finally:
    timer.cancel(); patch.request_deadline.reset(token); current_research.reset(research_token)
assert time.monotonic() - started < 0.2, time.monotonic() - started
server.shutdown(); server.server_close()
print("independent-ok")
"""
    root = str(Path(__file__).resolve().parents[1])
    env = os.environ.copy()
    env["ENV_FILE"] = "/dev/null"
    env["PYTHONPATH"] = root
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr[-800:]
    assert "independent-ok" in completed.stdout


def test_silent_body_cancellation_observed_while_blocked():
    """头已返回、正文静默：取消必须在下一轮询内冒出，不能等正文恢复。"""
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from src.core.research_run import ResearchRun, current_research
    from src.patches import eastmoney_patch as patch

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            try:
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.flush()
                time.sleep(0.4)
                self.wfile.write(b"ok")
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    run = ResearchRun(timeout_seconds=30)
    research_token = current_research.set(run)
    try:
        started = monotonic()
        token = patch.request_deadline.set(started + 1.0)
        timer = threading.Timer(0.03, run.stop, args=("cancelled",))
        timer.start()
        try:
            with pytest.raises(ResearchStopped):
                A.stream_export_csv(f"http://127.0.0.1:{server.server_port}/silent-body")
        finally:
            timer.cancel()
            patch.request_deadline.reset(token)
        assert monotonic() - started < 0.2
    finally:
        current_research.reset(research_token)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
