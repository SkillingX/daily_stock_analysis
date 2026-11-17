# -*- coding: utf-8 -*-
"""issue #91 第十轮：标准 HTTP 日线 curl 可中断传输边界（本机回环 server）。

真实 curl 子进程 + 受控行为（慢 header/慢 body/停止/超时/超限/凭据），零外
部 TCP/DNS。核心契约：socket read 不可跨越绝对 deadline——requests 的
iter_content 每块返回后才检查、慢响应实测超期 ~0.4s 且取消需等读取超时
（子 agent 实证），curl ``--max-time`` + 父进程 selectors 20ms 轮询 +
``finally`` kill 实现真实硬截止与即时取消。凭据只进 0600 临时 header
文件：server 端断言 header 正确、argv 不携带（fake Popen 捕获）。
"""
from __future__ import annotations

import json
import socket
import threading
import time as _time_module
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import monotonic

import pytest

from data_provider import ifind_http_daily_adapter as H
from data_provider.base import DataFetchError
from src.core.research_run import ResearchRun, ResearchStopped, current_research
from src.patches.eastmoney_patch import request_deadline

TOKEN_BODY = json.dumps({
    "errorcode": 0, "errmsg": "success",
    "data": {"access_token": "loopback-token", "expired_time": int(_time_module.time()) + 86400},
}).encode()


class _Server:
    """受控行为回环 server：路径 → 行为（记录收到的 header/body）。"""

    def __init__(self) -> None:
        self.records: dict[str, object] = {"headers": [], "bodies": []}
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _make_handler(self):
        records = self.records

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                records["headers"] = [*records["headers"], dict(self.headers)]
                records["bodies"] = [*records["bodies"], body]
                if self.path.startswith("/ok"):
                    payload = (
                        TOKEN_BODY if self.path.startswith("/ok/token")
                        else json.dumps({"errorcode": 0, "errmsg": "success", "tables": []}).encode()
                    )
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if self.path.startswith("/slow-header"):
                    # header 之后 0.55s 才发字节：requests 实测跨 deadline 场景。
                    _time_module.sleep(0.55)
                    payload = b'{"errorcode":0}'
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if self.path.startswith("/slow-body"):
                    self.send_response(200)
                    self.send_header("Content-Length", "40")
                    self.end_headers()
                    self.wfile.write(b'{"errorco')  # 先发半截
                    self.wfile.flush()
                    _time_module.sleep(0.55)  # 再滴流余下 body
                    self.wfile.write(b'de":0}' + b" " * 30)
                    return
                if self.path.startswith("/unauthorized"):
                    self.send_response(401)
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"{}")
                    return
                if self.path.startswith("/forbidden"):
                    self.send_response(403)
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"{}")
                    return
                if self.path.startswith("/server-error"):
                    self.send_response(500)
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"{}")
                    return
                if self.path.startswith("/huge"):
                    self.send_response(200)
                    self.send_header("Content-Length", str(H._MAX_EXPORT_BYTES + 4096))
                    self.end_headers()
                    for _ in range(0, H._MAX_EXPORT_BYTES + 4096, 65536):
                        self.wfile.write(b"x" * 65536)
                    return
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

        return Handler

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture()
def loopback():
    server = _Server()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture()
def short_deadline(monkeypatch):
    """0.15s 绝对 deadline（子 agent 实测同款），验证硬截止。"""
    token_value = request_deadline.set(monotonic() + 0.15)
    try:
        yield
    finally:
        request_deadline.reset(token_value)


def test_post_success_and_headers(loopback):
    """200 JSON：header 凭据正确到达 server，body 返回精确解析。"""
    status, body = H._http_api_curl_post(
        loopback.url("/ok/data"),
        {"Content-Type": "application/json", "access_token": "secret-token"},
        b'{"codes":"600519.SH"}',
        label="history",
    )
    assert status == 200
    assert json.loads(body)["errorcode"] == 0
    sent = loopback.records["headers"][0]
    assert sent.get("access_token") == "secret-token"
    assert sent.get("Content-Type") == "application/json"
    assert loopback.records["bodies"][0] == b'{"codes":"600519.SH"}'


def test_slow_header_aborts_at_deadline(loopback, short_deadline):
    """慢 header（header 后 0.55s 才发字节）：deadline 0.15s 处硬截止。

    requests 实测同场景 0.56s 才抛（超期 ~0.41s）；curl --max-time 硬切。
    """
    started = monotonic()
    with pytest.raises((TimeoutError, DataFetchError)):
        H._http_api_curl_post(loopback.url("/slow-header"), {}, b"{}", label="history")
    elapsed = monotonic() - started
    assert elapsed < 0.45, f"读取跨过绝对 deadline（{elapsed:.2f}s）"


def test_slow_body_aborts_at_deadline(loopback, short_deadline):
    """慢 body（先发半截再滴流）：deadline 处硬截止，不等滴流完成。"""
    started = monotonic()
    with pytest.raises((TimeoutError, DataFetchError)):
        H._http_api_curl_post(loopback.url("/slow-body"), {}, b"{}", label="history")
    elapsed = monotonic() - started
    assert elapsed < 0.45, f"读取跨过绝对 deadline（{elapsed:.2f}s）"


def test_cancel_propagates_immediately(loopback):
    """用户取消（ResearchStopped）即时生效：不等读取/超时。"""
    run = ResearchRun(timeout_seconds=30)
    token = current_research.set(run)
    try:
        import unittest.mock as _mock

        with _mock.patch.object(ResearchRun, "check", side_effect=ResearchStopped("cancelled")):
            with pytest.raises(ResearchStopped):
                H._http_api_curl_post(
                    loopback.url("/slow-header"), {}, b"{}", label="history",
                )
    finally:
        current_research.reset(token)


def test_error_status_branches(loopback):
    """非 200 分支：401/403 由调用方判鉴权、500 原样状态码。"""
    status, _body = H._http_api_curl_post(
        loopback.url("/unauthorized"), {}, b"{}", label="history",
    )
    assert status == 401
    status, _body = H._http_api_curl_post(
        loopback.url("/forbidden"), {}, b"{}", label="history",
    )
    assert status == 403
    status, _body = H._http_api_curl_post(
        loopback.url("/server-error"), {}, b"{}", label="history",
    )
    assert status == 500


def test_huge_body_rejected_at_bound(loopback):
    """响应体超限：父进程边界立即拒绝（不等 curl 下完全部正文）。

    消息前缀沿用共享既有拆分函数（``ifind export content-length ...``）——
    MCP 导出同一边界函数的既有语义，未重命名共享函数（Ponytail 复核）。
    """
    started = monotonic()
    with pytest.raises(DataFetchError, match="content-length exceeds bound"):
        H._http_api_curl_post(loopback.url("/huge"), {}, b"{}", label="history")
    assert monotonic() - started < 30.0  # 不因下完 1MB+ 才拒绝而显著拖延


def test_credentials_absent_from_argv(monkeypatch, loopback):
    """凭据不进 argv：Popen 参数里只有临时 header 文件路径，无 token 值。"""
    captured: dict = {}
    real_popen = __import__("subprocess").Popen

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        return real_popen(argv, **kwargs)

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    H._http_api_curl_post(
        loopback.url("/ok/token"),
        {"Content-Type": "application/json", "refresh_token": "loopback-refresh-secret"},
        None, label="token refresh",
    )
    argv_text = " ".join(captured["argv"])
    assert "loopback-refresh-secret" not in argv_text
    assert any(arg.startswith("@") and "ifind-http-hdr-" in arg for arg in captured["argv"])
    sent = loopback.records["headers"][0]
    assert sent.get("refresh_token") == "loopback-refresh-secret"


def test_token_refresh_via_curl(loopback):
    """token 端点端到端（curl）：200 → 解析 data。"""
    status, body = H._http_api_curl_post(
        loopback.url("/ok/token"),
        {"Content-Type": "application/json", "refresh_token": "refresh"},
        None, label="token refresh",
    )
    assert status == 200
    parsed = json.loads(body)
    assert parsed["data"]["access_token"] == "loopback-token"
