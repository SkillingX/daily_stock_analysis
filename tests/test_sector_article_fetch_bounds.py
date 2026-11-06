# -*- coding: utf-8 -*-
"""文章探查有界 I/O 契约——真实本地 socket 端到端证明（issue #83 决策 3/边界终态）。

区别于 fake 响应桩（test_sector_research_admission）本文件用**真实 TCP
socket**（``http.server`` 本地回环 + DNS 假名映射名义主机）证明：

- per-response 字节上限在**真实读取路径**生效：探查在有限时间返回
  ``article_oversize``，服务端确认客户端真实关闭连接（EOF/BrokenPipe），
  不是「全量读完后喂给 parser 再叫有界」（独立 oversize 控制保留）
- 绝对取数截止在真实慢速流上**先于字节上限**触发（``probe_timeout``/
  ``transport``；短 fetch 预算 + 慢流使 deadline 前 sent < cap——deadline
  终止被真实锻炼，不是被 bytecap 抢先）
- 重定向后的**最终响应成功性**在读取/解析前校验：403/404/500/304 携带
  valid-looking 出版 meta 的错误页零日期升级（``article_http_error``），
  错误 body 不进 parser，服务端确认客户端关闭（issue #83 边界终态）
- 真实响应流上的元数据日期提取（除 DNS 假名外全真实 I/O）
- 真实 302 跳转（公共目标）手动循环跟随成功

服务器只绑定 127.0.0.1 回环（仅本 fixture 绑定的端口）；被测 URL 是合法
名义公网主机名（DNS 层 monkeypatch 指向回环），来源结构校验按真实契约
通过。macOS 系统代理可能劫持回环流量（root 实录重定向到无关 7890）：
fixture 显式旁路全部代理环境（NO_PROXY='*' + 清 http(s)_proxy/
all_proxy），且禁止任何非本测试端口的外部连接。
"""

from __future__ import annotations

import os
import socket
import threading
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator, Tuple

import pytest

from src.services.sector_analysis_service import (
    ReportBudget,
    _probe_article_date_evidence,
)
from src.services import sector_research
from src.services.sector_research import CATALYST_ARTICLE_MAX_BYTES

_TEST_HOST = "bounded-test.example.com"

_META_HTML = """<!DOCTYPE html>
<html><head>
<meta property="article:published_time" content="{ts}">
<title>白酒行业新政策</title>
</head><body><p>正文</p></body></html>"""


class _ServerState:
    """服务端观测：发送字节与客户端断开事件（EOF 证明）。"""

    def __init__(self) -> None:
        self.sent = 0
        self.client_closed = threading.Event()
        self.requests: list[str] = []
        self.slow_chunk_seconds = 0.0
        self.status = 200  # 终态响应状态（错误页负控用；同 valid-looking meta）
        self.lock = threading.Lock()

    def record(self, path: str) -> None:
        with self.lock:
            self.requests.append(path)


def _make_server(state: _ServerState) -> ThreadingHTTPServer:
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # 静默访问日志
            pass

        def do_GET(self) -> None:
            state.record(self.path)
            if self.path == "/meta" or self.path.startswith("/dated/"):
                # /dated/2020/01/01/story.html 形态：路径自带 URL 声明日期
                # （冲突判定用），HTML meta 都是「今天」
                body = _META_HTML.format(
                    ts=datetime.now().date().isoformat() + "T09:00:00"
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/hop":
                self.send_response(302)
                self.send_header("Location", "/meta")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path == "/hop-conflict":
                # 重定向到**自带旧日期声明**的终态（302→/dated/2020/…）：
                # 终态 URL 声明(2020-01-01) ≠ meta 日期(今天) → 终态口径下
                # 是 provenance 冲突（终局：不得借原始无日期 URL 绕过）
                self.send_response(302)
                self.send_header("Location", "/dated/2020/01/01/story.html")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path == "/hop-consistent":
                # 重定向到**自带今日声明**的终态：终态 URL 声明 == meta 日期
                # → 两源互证，重定向条目照常合格（一致日期正控）
                self.send_response(302)
                self.send_header("Location", f"/dated/{datetime.now().date().isoformat()}/story.html")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path == "/hop-status":
                # 重定向到错误终态：成功性校验必须在**最终响应**上生效
                self.send_response(302)
                self.send_header("Location", "/error-meta")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path == "/error-meta":
                # 与 200 正控**同样的 valid-looking 出版 meta**，仅状态不同：
                # 证明错误 body 永不被 parser 接受（零日期升级）
                body = _META_HTML.format(
                    ts=datetime.now().date().isoformat() + "T09:00:00"
                ).encode("utf-8")
                self.send_response(state.status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass  # 客户端在校验后立即关闭（预期）
                return
            # /big：声明巨大 Content-Length 并持续发送，直到客户端断开
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(10 ** 12))
            self.end_headers()
            chunk = b"x" * 65536
            try:
                while True:
                    if state.slow_chunk_seconds:
                        threading.Event().wait(state.slow_chunk_seconds)
                    self.wfile.write(chunk)
                    with state.lock:
                        state.sent += len(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                state.client_closed.set()

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    return server


@pytest.fixture()
def bounds_server(monkeypatch: pytest.MonkeyPatch
                  ) -> Iterator[Tuple[ThreadingHTTPServer, _ServerState]]:
    """本地回环服务器 + DNS 假名 + 显式代理旁路（macOS 系统代理实录劫持）。"""
    # 显式旁路系统代理：只允许本 fixture 绑定的 127.0.0.1 端口，无外部网络。
    # （root-standards-article.* exit1：macOS 代理把回环重定向到无关 7890）
    for name in ("http_proxy", "https_proxy", "all_proxy",
                 "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    state = _ServerState()
    server = _make_server(state)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> list:
        if host == _TEST_HOST:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                     ("127.0.0.1", port))]
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    yield server, state
    server.shutdown()
    server.server_close()


class TestBoundedFetchRealSocket:
    """真实 socket 端到端：字节上限读取期生效 + 客户端真实关闭。"""

    def test_oversize_stops_reading_and_closes_connection_real_socket(
            self, bounds_server):
        server, state = bounds_server
        evidence, failure, final_url = _probe_article_date_evidence(
            f"http://{_TEST_HOST}/big", ReportBudget(30.0),
        )
        assert failure == "article_oversize"
        assert evidence.metadata_date is None
        # 客户端真实停止读取并关闭：服务端在有限时间内观察到断开（EOF/
        # BrokenPipe），而不是无限继续发送
        assert state.client_closed.wait(timeout=5.0), (
            "client did not close the connection after reaching byte cap")
        # 真实数据流发生（不是 0 字节假流）
        assert state.sent >= 64 * 1024

    def test_real_metadata_extraction_over_real_socket(self, bounds_server):
        server, state = bounds_server
        evidence, failure, final_url = _probe_article_date_evidence(
            f"http://{_TEST_HOST}/meta", ReportBudget(30.0),
        )
        assert failure is None, failure
        assert evidence.metadata_date == datetime.now().date()
        assert evidence.raw_basis is not None
        assert "article:published_time" in evidence.raw_basis
        assert state.requests == ["/meta"]

    def test_real_public_redirect_followed_over_real_socket(
            self, bounds_server):
        server, state = bounds_server
        evidence, failure, final_url = _probe_article_date_evidence(
            f"http://{_TEST_HOST}/hop", ReportBudget(30.0),
        )
        assert failure is None, failure
        assert evidence.metadata_date == datetime.now().date()
        assert state.requests == ["/hop", "/meta"]

    @pytest.mark.parametrize("status", [403, 404, 500, 304])
    def test_error_status_never_upgrades_date_real_socket(
            self, bounds_server, status: int):
        # issue #83 边界终态：重定向后的**最终响应**非 2xx → 零日期升级；
        # 错误页与 200 正控携带**同样 valid-looking 出版 meta**，证明错误
        # body 不被读取/解析；安全类别只含状态码（零 body/header 泄漏）。
        # （错误页是小响应：服务端写完即关，client_closed EOF 观测只适用
        # 于 /big 持续流场景，这里以零升级 + 类别断言为准）
        server, state = bounds_server
        state.status = status
        evidence, failure, final_url = _probe_article_date_evidence(
            f"http://{_TEST_HOST}/error-meta", ReportBudget(30.0),
        )
        assert failure == f"article_http_error:{status}", failure
        assert evidence.metadata_date is None
        assert evidence.raw_basis is None
        assert evidence.url_declared_date is None

    def test_redirect_to_error_status_zero_upgrade(self, bounds_server):
        # 302 → 403 终态：成功性校验作用在**最终响应**上（逐跳循环之后）
        server, state = bounds_server
        state.status = 403
        evidence, failure, final_url = _probe_article_date_evidence(
            f"http://{_TEST_HOST}/hop-status", ReportBudget(30.0),
        )
        assert failure == "article_http_error:403", failure
        assert evidence.metadata_date is None
        assert state.requests == ["/hop-status", "/error-meta"]

    def test_error_status_failure_still_returns_known_terminal(self,
                                                               bounds_server):
        # source-contract convergence：到达终态的安全失败（非 2xx）同样
        # 返回实际终态 URL——背景保留实际文章 provenance（不丢终态地址）；
        # 对照：连接前/跳转中失败终态未知（见 oversize/transport 区分）
        server, state = bounds_server
        state.status = 404
        evidence, failure, final_url = _probe_article_date_evidence(
            f"http://{_TEST_HOST}/hop-status", ReportBudget(30.0),
        )
        assert failure == "article_http_error:404", failure
        assert evidence.metadata_date is None
        assert final_url == f"http://{_TEST_HOST}/error-meta"

    def test_oversize_failure_returns_known_terminal(self, bounds_server):
        # 2xx 终态读取超限：终态已到达，随失败返回实际地址（背景 provenance）
        server, state = bounds_server
        evidence, failure, final_url = _probe_article_date_evidence(
            f"http://{_TEST_HOST}/big", ReportBudget(30.0),
        )
        assert failure == "article_oversize", failure
        assert final_url == f"http://{_TEST_HOST}/big"

    def test_redirect_terminal_url_governs_conflict_real_socket(
            self, bounds_server):
        # spec FAIL 反例（provenance repair #2）：original→302→自带旧日期
        # 声明的终态→200 meta「今天」。终态 URL 声明(2020-01-01) 参与
        # **同一冲突判定**——不得借原始无日期 URL 绕过（旧行为会登记 cat-1
        # complete）；探查如实返回终态 URL 供升级登记（dedup/溯源域名）。
        server, state = bounds_server
        evidence, failure, final_url = _probe_article_date_evidence(
            f"http://{_TEST_HOST}/hop-conflict", ReportBudget(30.0),
        )
        assert failure is None, failure
        assert final_url == f"http://{_TEST_HOST}/dated/2020/01/01/story.html"
        assert evidence.metadata_date == datetime.now().date()
        assert evidence.url_declared_date == date(2020, 1, 1)
        assert state.requests == ["/hop-conflict",
                                  "/dated/2020/01/01/story.html"]
        qualified, reason = sector_research.qualifies_article_declared_date(
            evidence,
            datetime.now().date() - timedelta(days=2), datetime.now().date(),
        )
        assert qualified is None
        assert reason == "url_meta_date_conflict"

    def test_redirect_consistent_dates_stay_qualified_real_socket(
            self, bounds_server):
        # 一致日期重定向正控：终态 URL 声明 == meta 日期 → 两源互证，
        # 重定向条目照常合格（date_basis=article_metadata 路径不变）
        server, state = bounds_server
        evidence, failure, final_url = _probe_article_date_evidence(
            f"http://{_TEST_HOST}/hop-consistent", ReportBudget(30.0),
        )
        assert failure is None, failure
        assert final_url == (
            f"http://{_TEST_HOST}/dated/{datetime.now().date().isoformat()}"
            "/story.html")
        assert evidence.metadata_date == datetime.now().date()
        assert evidence.url_declared_date == datetime.now().date()
        qualified, reason = sector_research.qualifies_article_declared_date(
            evidence,
            datetime.now().date() - timedelta(days=2), datetime.now().date(),
        )
        assert qualified == datetime.now().date()
        assert reason == "article_metadata"

    def test_no_redirect_final_url_equals_original_real_socket(
            self, bounds_server):
        # 无重定向：final_url == 原 URL（升级登记的 dedup 键/域名不变）
        server, state = bounds_server
        evidence, failure, final_url = _probe_article_date_evidence(
            f"http://{_TEST_HOST}/meta", ReportBudget(30.0),
        )
        assert failure is None, failure
        assert final_url == f"http://{_TEST_HOST}/meta"
        assert evidence.metadata_date == datetime.now().date()

    def test_absolute_deadline_clamps_real_slow_stream(self, bounds_server):
        # 绝对取数截止在真实慢速流上**先于字节上限**触发（issue #83 边界
        # 终态修正）：短 fetch 预算 + 0.15s/块 → deadline 前约 12 块 ≈
        # 0.75MB（含单块在途超读 64KB 仍 < 1MB cap），deadline 是被真实
        # 锻炼的终止原因，不是被 article_oversize 抢先
        server, state = bounds_server
        state.slow_chunk_seconds = 0.15
        budget = ReportBudget(2.0, finish_margin=0.5)
        started = time.monotonic()
        evidence, failure, final_url = _probe_article_date_evidence(
            f"http://{_TEST_HOST}/big", budget,
        )
        elapsed = time.monotonic() - started
        # deadline/transport 终止（不是 bytecap：sent < cap 证明字节上限
        # 未先触发）；探查在预算内有界返回
        assert failure in ("probe_timeout", "transport"), failure
        assert failure != "article_oversize"
        assert evidence.metadata_date is None
        assert elapsed < 8.0, elapsed  # 有界返回（预算 2s + 容差）
        assert state.sent < CATALYST_ARTICLE_MAX_BYTES, (
            f"deadline control must terminate below byte cap, sent "
            f"{state.sent} >= {CATALYST_ARTICLE_MAX_BYTES}")
        # 客户端在截止后关闭连接（服务端观察到断开；慢速流下 BrokenPipe
        # 可能由下一块写出触发，允许小概率经由 OSError 分支的等价关闭）
        assert state.client_closed.wait(timeout=10.0)
