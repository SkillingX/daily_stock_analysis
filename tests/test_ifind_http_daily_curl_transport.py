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
                if self.path.startswith("/too-many-requests"):
                    self.send_response(429)
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"{}")
                    return
                if self.path.startswith("/request-id"):
                    # 诊断脱敏样本：request ID 含非法字符与超长尾巴（白名单
                    # 过滤 + 限长后应只剩合法前缀）。
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header(
                        "x-request-id",
                        "abc-123.Def_4\ninjected: header|x" + "y" * 200)
                    payload = json.dumps({"errorcode": 0, "errmsg": "success", "tables": []}).encode()
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if self.path.startswith("/reset"):
                    # 服务端直接断连、不回复任何 HTTP 字节（历史 curl 52
                    # 「空回复」现场的最小复现）。
                    try:
                        self.connection.close()
                    except Exception:
                        pass
                    self.close_connection = True
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
                if self.path.startswith("/sentinel-in-body"):
                    # 第二次根审查反例：合法 JSON 字段值内含完整诊断哨兵
                    # 文本——业务正文必须原样保留（诊断不得改写 stdout）。
                    # pos=first/last 覆盖哨兵在不同字段位置。
                    if self.path.partition("?pos=")[2] == "first":
                        payload = json.dumps({
                            "note": "__IFIND_CURL_META__-prefix-marker",
                            "errorcode": 0, "tables": []}).encode()
                    else:
                        payload = json.dumps({
                            "errorcode": 0, "tables": [],
                            "company": "__IFIND_CURL_META__valid-business-text"}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if self.path.startswith("/exact-max"):
                    # 正文恰好 _MAX_EXPORT_BYTES（边界内）或 +1（越界）——
                    # 独立诊断通道不得挤占正文配额（复审阻断 4 反例）。
                    oversize = self.path.startswith("/exact-max-plus")
                    size = H._MAX_EXPORT_BYTES + (1 if oversize else 0)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(size))
                    self.end_headers()
                    chunk = b"x" * 65536
                    written = 0
                    while written < size:
                        take = min(len(chunk), size - written)
                        self.wfile.write(chunk[:take])
                        written += take
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
    """200 JSON：header 凭据正确到达 server，body 返回精确解析且类型为
    ``bytes``（既定 ``Tuple[int, bytes]`` 契约，不得退化为 bytearray）。"""
    status, body = H._http_api_curl_post(
        loopback.url("/ok/data"),
        {"Content-Type": "application/json", "access_token": "secret-token"},
        b'{"codes":"600519.SH"}',
        label="history",
    )
    assert status == 200
    assert isinstance(body, bytes), f"返回类型须为 bytes（实测 {type(body).__name__}）"
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


# ===== 第十七轮：curl 失败最小传输诊断（write-out 元数据与正文分离） =====

def test_success_body_separated_from_writeout_meta(loopback):
    """write-out 元数据与响应正文分离：成功返回的 body 仍是可解析 JSON
    （哨兵段不混入正文），元数据可解析出白名单字段。"""
    status, body = H._http_api_curl_post(
        loopback.url("/ok/data"),
        {"Content-Type": "application/json", "access_token": "secret-token"},
        b'{"codes":"600519.SH"}',
        label="history",
    )
    assert status == 200
    assert json.loads(body)["errorcode"] == 0  # 正文不被 write-out 尾巴污染


def test_curl_52_empty_reply_transport_diagnostics(loopback, caplog):
    """服务端直接断连（curl 52 空回复现场）：既有失败语义保持，且失败诊断
    日志携带退出码/标签/预算/耗时等白名单元数据；缺项如实标 unavailable
    （本场景无 HTTP 状态/节点元数据缺失时不虚构）。"""
    import logging

    with caplog.at_level(logging.WARNING, logger="data_provider.ifind_http_daily_adapter"):
        with pytest.raises(DataFetchError, match="curl exit 52"):
            H._http_api_curl_post(loopback.url("/reset"), {}, b"{}", label="history")
    texts = [record.getMessage() for record in caplog.records]
    assert any("transport diagnostics" in text and "curl_exit=52" in text for text in texts), texts
    diag = next(text for text in texts if "transport diagnostics" in text)
    assert diag.startswith("ifind http daily history transport diagnostics:")
    assert "failure=empty_reply" in diag
    # 回环断连现场的可得元数据：节点 IP 与 DNS/TCP 时间应被采集（诊断价值）。
    assert "remote_ip=127.0.0.1" in diag
    assert "budget_at_start" in diag and "elapsed" in diag


def test_non200_status_meta_logged_as_warning(loopback, caplog):
    """非 200 返回（401/429）：传输层成功返回，但默认日志下诊断以 warning
    保留现场（节点/时延/HTTP 状态白名单证据）——不推断额度/限流原因、不记
    正文/header；200 仍 DEBUG。"""
    import logging

    with caplog.at_level(logging.WARNING, logger="data_provider.ifind_http_daily_adapter"):
        status, _body = H._http_api_curl_post(
            loopback.url("/too-many-requests"), {}, b"{}", label="history")
    assert status == 429
    warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    diag = next(t for t in warns if "transport diagnostics" in t)
    assert "http_status=429" in diag and "remote_ip=127.0.0.1" in diag, diag
    assert "rate_limited" not in diag and "quota" not in diag.lower(), "不推断原因"
    # 401 同一白名单证据形态。
    with caplog.at_level(logging.WARNING, logger="data_provider.ifind_http_daily_adapter"):
        status, _body = H._http_api_curl_post(
            loopback.url("/unauthorized"), {}, b"{}", label="history")
    assert status == 401
    warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("transport diagnostics" in t and "http_status=401" in t for t in warns)


def test_success_200_stays_debug_only(loopback, caplog):
    """200 成功诊断仍 DEBUG（默认日志零增量）。"""
    import logging

    with caplog.at_level(logging.WARNING, logger="data_provider.ifind_http_daily_adapter"):
        status, _body = H._http_api_curl_post(
            loopback.url("/ok/data"), {}, b"{}", label="history")
    assert status == 200
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_request_id_filtered_and_bounded(loopback, caplog):
    """request ID 白名单过滤 + 限长：换行/冒号等非法字符剔除（日志注入被
    消除）、超长截断到 128；过滤后的剩余字符均在白名单集合内（仅展示用
    途，无语义解释）。"""
    import logging
    import re

    with caplog.at_level(logging.DEBUG, logger="data_provider.ifind_http_daily_adapter"):
        status, _body = H._http_api_curl_post(
            loopback.url("/request-id"), {}, b"{}", label="history")
    assert status == 200
    debug_texts = [record.getMessage() for record in caplog.records
                   if record.levelno == logging.DEBUG]
    diag = next(text for text in debug_texts if "request_id=" in text)
    value = diag.split("request_id=", 1)[1].split()[0]
    assert value, "过滤后仍有合法 request ID"
    assert re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value), value
    assert "\n" not in value and ":" not in value and "|" not in value, \
        "日志注入字符（换行/冒号/竖线）必须剔除"
    assert len(value) <= 128, "超长尾巴截断"


def test_diagnostics_never_leak_credentials(loopback, caplog):
    """失败诊断不得泄漏凭据/URL 参数/正文/原始 stderr：带 access_token 的
    请求直接断连（curl 会向 stderr 写错误文本），日志与异常消息不含 token
    值、URL、请求体、stderr 原文——诊断只承载白名单帧字段。"""
    import logging

    with caplog.at_level(logging.DEBUG, logger="data_provider.ifind_http_daily_adapter"):
        with pytest.raises(DataFetchError):
            H._http_api_curl_post(
                loopback.url("/reset"),
                {"Content-Type": "application/json", "access_token": "loopback-secret-token"},
                b'{"codes":"600519.SH","secret":"x"}',
                label="history",
            )
    all_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "loopback-secret-token" not in all_text
    assert "127.0.0.1:" not in all_text.replace("remote_ip=127.0.0.1", ""), "URL 不进诊断日志"
    assert '"codes"' not in all_text and "600519.SH" not in all_text
    assert "Empty reply" not in all_text and "curl:" not in all_text, \
        "原始 stderr 错误文本不入日志（退出码承载失败语义）"


def test_hard_deadline_local_abort_reason_logged(loopback, short_deadline, caplog):
    """硬截止：本地中止（元数据可能缺失）只报告本地已知时间/原因，且不为
    等统计数据延长请求（elapsed 断言同既有契约）。"""
    import logging

    started = monotonic()
    with caplog.at_level(logging.WARNING, logger="data_provider.ifind_http_daily_adapter"):
        with pytest.raises((TimeoutError, DataFetchError)):
            H._http_api_curl_post(loopback.url("/slow-header"), {}, b"{}", label="history")
    elapsed = monotonic() - started
    assert elapsed < 0.45, f"读取跨过绝对 deadline（{elapsed:.2f}s）"
    texts = [record.getMessage() for record in caplog.records]
    assert any("aborted locally" in text for text in texts), "本地中止有明确原因日志"


def test_parse_curl_meta_whitelist_bounds_and_garbage():
    """write-out 元数据解析：白名单字段、限长、数值校验；异常形态整段/逐
    字段降级为不可得，不抛异常（诊断采集失败不得覆盖原异常）。"""
    good = (
        "200|221.194.164.121|0.012|0.045|0.201|0.812|0.988|0"
    ).encode()
    meta = H._parse_curl_meta(good)
    assert meta["http_status"] == "200"
    assert meta["remote_ip"] == "221.194.164.121"
    assert meta["curl_exit"] == "0"
    assert meta["total_seconds"] == "0.988"
    # 旧 curl 输出字面 %{exitcode}：该字段不可得，其余保留。
    legacy = "200|221.194.164.121|0.012|0.045|0.201|0.812|0.988|%{exitcode}".encode()
    meta = H._parse_curl_meta(legacy)
    assert meta["curl_exit"] is None and meta["http_status"] == "200"
    # 字段数不符/空/None：整段不可得。
    assert H._parse_curl_meta(b"200|1|2|3") == {}
    assert H._parse_curl_meta(b"") == {}
    assert H._parse_curl_meta(None) == {}
    # 非法数值（时间字段必须是有限非负数字）：字段不可得。
    bad_time = "200|ip|abc|0.045|0.201|0.812|0.988|0".encode()
    assert H._parse_curl_meta(bad_time)["dns_seconds"] is None
    # 非法 IP（超长数字串）：白名单拒绝，字段不可得（不被超长值撑爆日志）。
    huge_ip = ("200|" + "9" * 500 + "|0.012|0.045|0.201|0.812|0.988|0").encode()
    assert H._parse_curl_meta(huge_ip)["remote_ip"] is None


def test_split_curl_meta_sentinel_semantics():
    """stderr 通道帧提取：哨兵（rfind）之后为元数据段；前置 curl 错误文本
    不影响提取；无哨兵（硬截止被杀/旧 curl）返回 None。"""
    frame = b"200|1.2.3.4|0|0|0|0|1|0"
    err_text = b"curl: (52) Empty reply from server\n"
    meta = H._split_curl_meta(err_text + b"\n\n__IFIND_CURL_META__" + frame)
    assert meta == frame, "哨兵之后是元数据段（错误文本前置不干扰）"
    assert H._split_curl_meta(b"curl: (28) timed out") is None
    assert H._split_curl_meta(b"") is None
    assert H._split_curl_meta(None) is None
    # 哨兵撞车（错误文本含字面哨兵）：rfind 取最后一次（真帧在末尾）。
    tricky = err_text + b"__IFIND_CURL_META__junk" + b"\n\n__IFIND_CURL_META__" + frame
    assert H._split_curl_meta(tricky) == frame


# ===== 复审阻断 4：诊断字节独立于响应正文（stderr write-out 通道） =====

def test_exact_max_body_succeeds_with_stderr_meta(loopback, caplog):
    """正文恰好 _MAX_EXPORT_BYTES（合法边界）：传输成功（不被诊断尾部挤
    出配额），且 stderr write-out 帧经真实 curl 输出并被解析（诊断日志含
    http_status=200 与节点 IP）。"""
    import logging

    with caplog.at_level(logging.DEBUG, logger="data_provider.ifind_http_daily_adapter"):
        status, body = H._http_api_curl_post(
            loopback.url("/exact-max"), {}, b"{}", label="history",
        )
    assert status == 200
    assert isinstance(body, bytes), f"返回类型须为 bytes（实测 {type(body).__name__}）"
    assert len(body) == H._MAX_EXPORT_BYTES, "恰好上限的正文完整返回"
    debug_texts = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    diag = next(t for t in debug_texts if "transport diagnostics" in t)
    assert "http_status=200" in diag and "remote_ip=127.0.0.1" in diag, \
        f"stderr 帧经真实 curl 提取：{diag}"


def test_exact_max_plus_one_body_rejected_at_bound(loopback):
    """正文 _MAX_EXPORT_BYTES + 1：原边界语义拒绝（诊断通道不放宽配额）。"""
    started = monotonic()
    with pytest.raises(DataFetchError, match="content-length exceeds bound"):
        H._http_api_curl_post(loopback.url("/exact-max-plus"), {}, b"{}", label="history")
    assert monotonic() - started < 30.0


# ===== 第二次根审查：合法正文含哨兵文本时诊断不得改写 stdout =====

def test_sentinel_inside_body_preserved_last_field(loopback, caplog):
    """合法 JSON 最后字段值内含完整哨兵文本（协调者复现形态，83 字节）：
    正文原样返回（字节不变）、json 可解析、哨兵文本完整保留；同一请求的
    stderr 诊断仍正常可得——诊断解析永不截断业务正文。"""
    import logging

    expected = json.dumps({
        "errorcode": 0, "tables": [],
        "company": "__IFIND_CURL_META__valid-business-text"}).encode()
    assert len(expected) == 83, f"复现形态 83 字节（实测 {len(expected)}）"
    with caplog.at_level(logging.DEBUG, logger="data_provider.ifind_http_daily_adapter"):
        status, body = H._http_api_curl_post(
            loopback.url("/sentinel-in-body?pos=last"), {}, b"{}", label="history")
    assert status == 200
    assert body == expected, f"正文必须原样保留（实测 {len(body)} 字节 ≠ {len(expected)}）"
    parsed = json.loads(body)
    assert parsed["company"] == "__IFIND_CURL_META__valid-business-text"
    debug_texts = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("transport diagnostics" in t and "http_status=200" in t for t in debug_texts), \
        "stderr 诊断同请求仍正常可得"


def test_sentinel_inside_body_preserved_first_field(loopback):
    """哨兵文本在正文字段的其他位置（首字段）同样不截断——诊断逻辑对
    stdout 的处理与哨兵位置无关（只读 stderr 通道）。"""
    expected = json.dumps({
        "note": "__IFIND_CURL_META__-prefix-marker",
        "errorcode": 0, "tables": []}).encode()
    status, body = H._http_api_curl_post(
        loopback.url("/sentinel-in-body?pos=first"), {}, b"{}", label="history")
    assert status == 200
    assert body == expected, f"正文原样保留（实测 {len(body)} 字节 ≠ {len(expected)}）"
    assert json.loads(body)["note"].startswith("__IFIND_CURL_META__")
