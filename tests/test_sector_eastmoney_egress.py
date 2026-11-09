# -*- coding: utf-8 -*-
"""板块专项东财公共行情出站代理（SECTOR_EASTMONEY_PROXY_URL）— 确定性回归。

离线确定性验证。**测试隔离不改变全局状态**：

- 本模块 **import 无副作用**：不清理 env、不替换任何 socket/DNS/proxy 缝；
  import 时只对缝做只读快照（`_IMPORT_SNAPSHOT`）供 invariance 断言；
- 隔离由 autouse fixture（`_egress_isolation`）在**每个测试作用域内**安装并
  在 teardown **完全恢复**（含断言）：代理 env 清空（原值还原）、系统代理
  自动发现 neutralize（跨平台的 env/requests 缝；CPython 仅在 darwin 定义的
  sysconf 属性按存在性快照/临时替换/恢复，缺席保持缺席、在位恢复原对象
  身份）、TCP 仅放行本轮注册的精确回环端口（受控 no-forward fixture；哨兵
  负向对照证明未注册回环端口——如本机系统代理——不可用）、DNS 仅数字地址
  与本轮注册的 fixture 主机映射（解析由守卫直接合成，零真实外部查询）；
- 进程级 pre-import 全网守卫由受守卫 runner（外部验收入口，独占进程）承担；
  本模块不要求其他测试文件向本模块的私有状态注册，普通 pytest 收集本模块
  不影响同进程其他模块的既有回环测试（含新旧测试双顺序组合运行，见
  交付证据）。

覆盖的行为反例：

- 未配置复现既有直连/默认请求选项（send 缝观察 + wire 直连尝试观测）；
  配置后显式覆盖环境 HTTP(S)_PROXY/NO_PROXY
- 主机匹配完整矩阵：主域、数字分片、大小写规范化、欺骗后缀/子串、无关东财域
- ``_http_get_json`` 与 ``_bounded_ak`` 两条 worker 路径真实走代理 fixture
- 真实 AkShare ``stock_board_industry_hist_em`` 经伪代理：同一合法形状 klines
  响应体，HTTP 200 被采为 DataFrame（对照），HTTP 407/502 在 SDK 解析前被
  共享传输边界拒绝（不采纳任何事实）；观测 wire 状态码与请求数
- HTTPS CONNECT 到达 fixture；自签证书 MITM fixture 证明证书校验保留（校验
  在位时握手在证书链处失败、隧道内零 HTTP 请求；负向对照强制 ``verify=False``
  时同一 fixture 能被采纳伪造体——证明断言对校验降级敏感；CONNECT 观测本身
  只证明路由）
- 3xx 不跟随、响应体不采纳（SectorScopedRedirectError）；407/502（含合法形状
  体）/畸形响应/连接失败/预算耗尽显式失败；无隐藏直连回退（守卫保证）
- 带凭证代理错误经日志/gap/异常面/格式化 traceback 零泄漏：脱敏对象是实际
  渲染状态（args + OSError strerror/filename/filename2 + urllib3 reason +
  __cause__/__context__ 链）与 URL 变体，先脱敏后截断
- 并发 sector/非 sector 请求、执行器复用、嵌套上下文无路由/上下文泄漏
- 无效配置显式失败且只提示变量名（原始输入控制字符先于归一化拒绝、主机语法
  校验）；registry/系统设置敏感掩码与只读展示；Config repr 不含代理值；
  get/schema 与两路导出（export_env/export_desktop_env，dotenv span 级重组）
  均不含部署 secret——**无法证明已安全剔除时显式拒绝导出（fail-closed，
  含畸形语句/解析失败），绝不返回未过滤原文**；无关内容逐字节保留
- 模块 import 无副作用与 teardown 完全恢复有专门不变式检查
"""

from __future__ import annotations

import http.server
import io
import ipaddress
import json
import os
import socket
import ssl
import tempfile
import threading
import urllib.request as _urllib_request
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# 测试隔离层：**import 无副作用**；隔离只在 autouse fixture 作用域内安装并
# 完全恢复（teardown 断言状态还原）。进程级 pre-import 全网守卫由受守卫
# runner（外部验收入口，独占进程）承担——普通 pytest 收集本模块不得改变
# 全局状态（socket/DNS/proxy/env），不得影响其他模块的既有回环测试。
# ---------------------------------------------------------------------------

# import 时的缝快照（只读捕获，用于 invariance 断言；不做任何替换）。
# 平台可选属性（CPython 仅在 darwin 分支定义 getproxies_macosx_sysconf /
# proxy_bypass_macosx_sysconf）按存在性记录：缺席不读取、不制造。
_OPTIONAL_PLATFORM_PROXY_ATTRS = (
    "getproxies_macosx_sysconf",
    "proxy_bypass_macosx_sysconf",
)
_IMPORT_SNAPSHOT: Dict[str, Any] = {
    "connect": socket.socket.connect,
    "connect_ex": socket.socket.connect_ex,
    "getaddrinfo": socket.getaddrinfo,
    "getproxies": _urllib_request.getproxies,
}
for _optional_name in _OPTIONAL_PLATFORM_PROXY_ATTRS:
    if hasattr(_urllib_request, _optional_name):
        _IMPORT_SNAPSHOT[_optional_name] = getattr(_urllib_request, _optional_name)

_PROXY_ENV_VARS = ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
                   "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy")
_PROXY_VAR = "SECTOR_EASTMONEY_PROXY_URL"
_AUDIT_PATH = os.environ.get("SECTOR_EGRESS_ISOLATION_AUDIT")


class _IsolationScope:
    """单次测试的隔离作用域：fresh 注册表 + 本轮允许/拒绝记录。"""

    def __init__(self) -> None:
        self.owned_ports: Set[int] = set()
        self.dns_mappings: Dict[str, int] = {}
        self.denied: List[str] = []
        self.allowed: List[str] = []


_ACTIVE: Optional[_IsolationScope] = None
# 累计审计（跨用例；runner 注入 SECTOR_EGRESS_ISOLATION_AUDIT 时落盘）
_CUMULATIVE_DENIED: List[str] = []
_CUMULATIVE_ALLOWED: List[str] = []


def _active_scope() -> _IsolationScope:
    if _ACTIVE is None:
        raise RuntimeError("隔离 fixture 未激活：不能注册/操作 fixture 端口")
    return _ACTIVE


def _allow_fixture_port(port: int) -> None:
    _active_scope().owned_ports.add(int(port))


def _disallow_fixture_port(port: int) -> None:
    _active_scope().owned_ports.discard(int(port))


def _is_loopback_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _write_isolation_audit() -> None:
    if not _AUDIT_PATH:
        return
    import atexit

    def _dump() -> None:
        Path(_AUDIT_PATH).write_text(json.dumps({
            "allowed_targets": _CUMULATIVE_ALLOWED,
            "denied_targets": _CUMULATIVE_DENIED,
        }, indent=2) + "\n", encoding="utf-8")

    atexit.register(_dump)


_write_isolation_audit()

# --- 第三方/项目模块导入（import 本身无网络副作用）---

import requests  # noqa: E402
import requests.utils as _requests_utils  # noqa: E402

from src.config import (  # noqa: E402
    Config,
    redact_sector_eastmoney_proxy_text,
    validate_sector_eastmoney_proxy_url,
)
from src.patches.eastmoney_patch import (  # noqa: E402
    SectorScopedRedirectError,
    eastmoney_patch,
    is_scoped_sector_quote_url,
    sector_eastmoney_proxy,
)
import src.patches.eastmoney_patch as _eastmoney_patch_module  # noqa: E402
from src.services import sector_analysis_service as svc  # noqa: E402

# 授权获取确定性合成值：autouse 隔离 fixture 作用域内把模块级 ``_get_nid``
# 短路为固定值（纯函数、零网络）——wire 测试的计数/预算/denial 断言不依赖
# 真实授权外联、授权缓存冷热/TTL 或前序用例是否启用过 auth 路由
# （CI 42462666183 根因：前序模块将 ``_auth_patch_enabled`` 永久置位后，
# 每次东财目标 dispatch 都会对 anonflow2 发真实授权外联）。
_SYNTHETIC_NID = "synthetic-nid-for-egress-tests"


class _DeterministicAuthRandom:
    """auth/throttle 确定性替代（作用域内替换模块级 ``random`` 引用）。

    生产 auth 分支 dispatch 前有 ``random.uniform(1, 4)`` 秒防风控暂停——
    auth 激活路径的组成部分。测试作用域内将 uniform 归零、choice 取首元素，
    使 wire 计数/预算断言不受随机暂停影响；teardown 恢复原模块引用。
    """

    @staticmethod
    def uniform(low: float, high: float) -> float:
        return 0.0

    @staticmethod
    def choice(seq: Any) -> Any:
        return seq[0]


def _env_only_proxy_bypass(host: str, **kwargs: Any) -> int:
    return _urllib_request.proxy_bypass_environment(host)


_VALID_BK_KLINES_BODY: Dict[str, Any] = {
    "data": {"klines": ["2026-11-08,10,11,12,9,100,1000,30,10,1,2"]}
}

# 回环 TLS fixture 证书（构建期 openssl 生成后内嵌，运行零外部依赖）：
# 自签（不受信 CA）+ CN 与目标主机一致——校验在位时唯一失败点是证书链信任，
# 而非主机名不匹配。仅用于本机回环 MITM fixture，不构成任何真实凭据。
_FIXTURE_CERT_PEM = """-----BEGIN CERTIFICATE-----
MIIDWTCCAkGgAwIBAgIUcfN/ELoyEeSDKmMLFXmc3GzvQBIwDQYJKoZIhvcNAQEL
BQAwPDEcMBoGA1UEAwwTcHVzaDIuZWFzdG1vbmV5LmNvbTEcMBoGA1UECgwTTG9v
cGJhY2tUZXN0Rml4dHVyZTAeFw0yNjExMDgwNTQ3MjBaFw0zNjExMDUwNTQ3MjBa
MDwxHDAaBgNVBAMME3B1c2gyLmVhc3Rtb25leS5jb20xHDAaBgNVBAoME0xvb3Bi
YWNrVGVzdEZpeHR1cmUwggEiMA0GCSqGSIb3DQEBAQUAA4IBDwAwggEKAoIBAQCa
vfN0K4S1CGTiTFhoTg4Isegs2F1HtRiiTw2CtScUESLQDAAEJM2bS4S49LjNXSZI
zjKel7LIGwMTHVK5jQEPLPKfMKaxA1hdSkcKGO43IqYS78avhvTijijlfI1Ckm/i
RD7z/pVndZDwbaj95T1F8xrf1mYA33o9lImqAcW3pwrVe3EQB2SbXBHjdo3u6egQ
LhOTQ3CQXxpAh5z0XFtK/LYZEsSaUwYBBA/oMr2rYureoPhZuLbFrj+VMjTP9n6N
mtIs+qBuAcMOqv//lZMbI1z33qKgnn+D9Z03WKl3L38XWpJifwU2+tvlUekvLC7z
RW2yxdCVb99FJIosYaDJAgMBAAGjUzBRMB0GA1UdDgQWBBRMpbOQUb0/4T1SVxbr
+sOjlnqhhzAfBgNVHSMEGDAWgBRMpbOQUb0/4T1SVxbr+sOjlnqhhzAPBgNVHRMB
Af8EBTADAQH/MA0GCSqGSIb3DQEBCwUAA4IBAQBuBLs0CB07PzEAKr0Y886Oco09
fy8gIh1n1NjI+8IRgLK2YuGy4MKhmIF9yv6s4OLr75H8rqCRIWhtnwNrg6sGPH6P
9L6SXeGaS/FwXqFM9suRMiYxmAiWY6AJMmIgI33xKJSgXHbhM5jMcpbYCz7uqEyK
3rwytNzL8xqeDMsp8RYLBHxM2K6DbDgajZS9VfURFklmezg2Rx92a0G2RcljDVsY
oEs9/j2PT3yA6TPhubcQMEpVwe8TQY6Tt/eHwEf6VN05AYY+exN//NYf3Lyv8Fsh
BNl6qIC2oOpaZbk0H/TaQFA1SfdLCsr4SHn6RLX7HUc+D4u2yWV9LkGFhURC
-----END CERTIFICATE-----
"""

_FIXTURE_KEY_PEM = """-----BEGIN PRIVATE KEY-----
MIIEvAIBADANBgkqhkiG9w0BAQEFAASCBKYwggSiAgEAAoIBAQCavfN0K4S1CGTi
TFhoTg4Isegs2F1HtRiiTw2CtScUESLQDAAEJM2bS4S49LjNXSZIzjKel7LIGwMT
HVK5jQEPLPKfMKaxA1hdSkcKGO43IqYS78avhvTijijlfI1Ckm/iRD7z/pVndZDw
baj95T1F8xrf1mYA33o9lImqAcW3pwrVe3EQB2SbXBHjdo3u6egQLhOTQ3CQXxpA
h5z0XFtK/LYZEsSaUwYBBA/oMr2rYureoPhZuLbFrj+VMjTP9n6NmtIs+qBuAcMO
qv//lZMbI1z33qKgnn+D9Z03WKl3L38XWpJifwU2+tvlUekvLC7zRW2yxdCVb99F
JIosYaDJAgMBAAECggEADlJnC6knduCYWGrmgn45mvZpsxTcyqaT6W8q+fOtnT7a
G6qwqbGE/MroNXzOSeZFhSwzJS3h1IF42bZiEghfQQ9Q7rOVtKvG7EN+qww+/V1j
ROdqWpPsFbfrSGosYi1O1lX+BEkw0iA10lA5Tt5+ZaX9e8V1MBQpqooVP7s67dIO
5qaScMSmgdzRwRaje8kyYhuOjN4ZfbEHE8W5gBUWirLNLW0808WmEqXS4oeg7DzD
KENrQQL7XNff1ZvtdFDWhEg0O5gVa+T3JZ06uxs9MoXajDo42LsppQxrvsr/rM8k
i6IFCDV2aG+ztWS7sj1n3XM/GzNzIJvLAuGTGUf5DQKBgQDKRoz4PbfnlVNAH81m
JW8jeZut9XYhhgXMG1EKcaXrWwd0Gmlf6sRlFJtbOTAL3VAwnT/KCJLgUQjrYTiH
C868szUqTekZUMDil3j8S8pk1bZEt/CyMhi3tVkBbMQ9xG4QoWg6aQVcJXhfCwR5
yxPmTsYQ49ipwXgXUmnlqEbsvwKBgQDD12taHDik23bV0eGZ6z4nnaaV0xCqnpef
kRjOcxJyB0hkcDP/QNV/N62zwRg9+P6fFqh8MdmdO75pgpWtz+6iFPmamXWfbNXU
aayIzUz5x01W9mshI1PO6qzWhGsKT9XX99Ci3T9dGqZB1Re3/5ZCsqGjL9x+vPEH
v1O79bdsdwKBgH+T5d/AVZjL9vkP1eXiIGnYjpc2pS5ZsJhedkvHCGYcQOQ6sFGd
6gby61WrCSHCkYxH8e7EX5b5pU2IQTXz/Y3I3fRztVyRMUb5UIaYauwUWuSWu0o5
DJv51kxuxItMtDrrAqksigPnYjdX5JfIvSFad9Q5UPMookzbXw1JiIpTAoGAUTV6
5prdQj9fP7sQeer6kyLkeSxUtOuepCRezP0aoP40HrIIXchcv6wu82MW0lv1+bJD
m3xwaKrlgosL1k9q/PsFpf4MsFdJNBO939yCFvF2OKFmOw+TKTJXCX1Pha8lVbEF
awOjys/KMXJTq5Doli/MpwapwERJDpVf+4kgznECgYA2+yVeJ3cQkJeb6DQfbI/7
AslcVgx53xdNIb0ScxOtkrorrJLVAHhPQ48r+Z9CHuQglay3KlFxxjPeW+NG1AEk
WJAj6pBpeluZFQRunGqnW9H/sD+dVasNBDHzHAsSq15h7d3ujUVAh7I+361qAmcI
Ch2/GZVt4gfvjN50apO4fw==
-----END PRIVATE KEY-----
"""


class _FakeForwardProxy(http.server.BaseHTTPRequestHandler):
    """最小正向代理：只与回环客户端交互，绝不向任何第三方发起连接。

    行为按请求路径（类属性 ``behavior``）选择；全部收到的请求记录在类属性
    ``received``/``connects``/``statuses``/``mitm_received``。默认响应
    ``{"ok": true}``。
    """

    behavior: str = "ok"
    received: List[Dict[str, Any]] = []
    connects: List[str] = []
    statuses: List[int] = []
    mitm_received: List[str] = []
    tls_context: Optional["ssl.SSLContext"] = None

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 基类签名
        return  # 静默 http.server 默认 stderr 噪声

    def _record(self) -> None:
        _FakeForwardProxy.received.append({
            "command": self.command,
            "path": self.path,
            "host": self.headers.get("Host", ""),
        })

    def _send_json(self, status: int, payload: Dict[str, Any],
                   extra_headers: Optional[Dict[str, str]] = None,
                   raw_body: Optional[bytes] = None,
                   content_type: str = "application/json") -> None:
        body = raw_body if raw_body is not None else json.dumps(payload).encode("utf-8")
        _FakeForwardProxy.statuses.append(status)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._record()
        behavior = _FakeForwardProxy.behavior
        if behavior == "redirect":
            self._send_json(
                302, {"valid": "looking-body"},
                extra_headers={"Location": "https://evil.example.com/steal"},
            )
            self.close_connection = True
        elif behavior == "proxy407":
            # 407 + 合法形状体：拒绝必须发生在状态边界，而非体解析失败
            self._send_json(407, _VALID_BK_KLINES_BODY)
            self.close_connection = True
        elif behavior == "em502":
            # 502 + 合法形状体（真实 AkShare 解析前不查状态码，此形状若不设
            # 状态边界会被采为业务数据）
            self._send_json(502, _VALID_BK_KLINES_BODY)
            self.close_connection = True
        elif behavior == "bkvalid":
            self._send_json(200, _VALID_BK_KLINES_BODY)
            self.close_connection = True
        elif behavior == "badjson":
            body = b"<html>502 Service Unavailable</html>"
            self._send_json(200, {}, raw_body=body, content_type="text/html")
            self.close_connection = True
        elif behavior == "hang":
            # 预算反例：worker 在途；调用方按剩余预算放弃等待（结果丢弃）
            threading.Event().wait(1.5)
            self._send_json(200, {"late": True})
            self.close_connection = True
        else:  # ok
            self._send_json(200, {"ok": True})

    def do_CONNECT(self) -> None:
        """HTTPS 目标的 CONNECT：记录目标主机后按行为分支处理隧道。

        - 默认：回 200 Established 后立即断开隧道（只证明 CONNECT 路由到达，
          不构成任何 TLS/证书结论）。
        - ``tls_mitm``：在隧道上以**自签证书**冒充目标完成服务端 TLS 握手
          （MITM fixture）。证书链不受信 → 校验在位的客户端在握手处失败、
          零 HTTP 请求进入隧道（``mitm_received`` 为空）；若校验被关闭，
          握手成功、伪造响应体可被业务采纳——正/负向对照共同证明测试对
          verify 降级敏感。fixture 绝不向第三方发起连接。
        """
        _FakeForwardProxy.connects.append(self.path)  # "push2.eastmoney.com:443"
        self.connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        if _FakeForwardProxy.behavior == "tls_mitm":
            context = _FakeForwardProxy.tls_context
            assert context is not None, "tls_mitm 需要 fixture 先构建 TLS 上下文"
            self.connection.settimeout(5)
            try:
                tls_conn = context.wrap_socket(self.connection, server_side=True)
            except (ssl.SSLError, OSError):
                # 客户端校验拒绝（预期正路径）：握手在服务端同步失败
                self.close_connection = True
                return
            try:
                request = b""
                while b"\r\n\r\n" not in request and len(request) < 65536:
                    chunk = tls_conn.recv(4096)
                    if not chunk:
                        break
                    request += chunk
                if request:
                    _FakeForwardProxy.mitm_received.append(
                        request.split(b"\r\n", 1)[0].decode("latin-1"))
                    body = b'{"attacker": true}'
                    tls_conn.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                        b"Content-Length: " + str(len(body)).encode("ascii")
                        + b"\r\n\r\n" + body)
            except (ssl.SSLError, OSError):
                pass
            finally:
                try:
                    tls_conn.close()
                except OSError:
                    pass
        self.close_connection = True


class _DirectRecorder(http.server.BaseHTTPRequestHandler):
    """直连对照器：非 sector 请求不经代理时在此被观测到。"""

    received: List[str] = []

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 基类签名
        return

    def do_GET(self) -> None:
        _DirectRecorder.received.append(self.path)
        body = b'{"direct": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _ResettingListener:
    """接受连接后立即关闭的回环监听器：直连尝试得到确定性 ConnectionError。

    用途：未配置/非板块路径的 wire 级「确走直连」观测——目标主机经 getaddrinfo
    打桩映射到本监听器端口（端口注册为 fixture 持有），直连尝试被 reset
    （计入 ``attempts``）；若 scoped 路由被错误注入，请求会改走伪代理 fixture
    （可观测），本对照即失败。全程回环且仅 fixture 持有端口，零真实外联
    （含 DNS：目标主机名不触发真实解析）。
    """

    def __init__(self) -> None:
        self._server = socket.socket()
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(16)
        self._stop = threading.Event()
        self.port: int = self._server.getsockname()[1]
        self.attempts = 0
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        self._server.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
                self.attempts += 1
                conn.close()
            except OSError:
                continue

    def start(self) -> None:
        _allow_fixture_port(self.port)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(2)
        self._server.close()
        _disallow_fixture_port(self.port)


@contextmanager
def _dns_loopback(hostname: str, port: int) -> Iterator[None]:
    """把 ``hostname`` 的解析合成到回环 ``port``（写入本轮隔离作用域）。

    使「直连尝试」可在无真实外联/无真实 DNS（含本机 VPN/代理 fake-IP DNS）
    的前提下被确定性观测——解析结果由守卫直接合成，绝不调用真实解析器。
    """
    _active_scope().dns_mappings[hostname] = int(port)
    try:
        yield
    finally:
        _active_scope().dns_mappings.pop(hostname, None)


class _LoopbackFixture:
    """伪代理 + 直连对照器（端口注册进本轮隔离作用域）。"""

    def __init__(self) -> None:
        self.proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeForwardProxy)
        self.direct = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _DirectRecorder)
        self.proxy_addr = f"http://127.0.0.1:{self.proxy.server_address[1]}"
        self.direct_url = f"http://127.0.0.1:{self.direct.server_address[1]}/direct"
        self._tls_dir: Optional[tempfile.TemporaryDirectory] = None

    def start(self) -> None:
        _allow_fixture_port(self.proxy.server_address[1])
        _allow_fixture_port(self.direct.server_address[1])
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()
        threading.Thread(target=self.direct.serve_forever, daemon=True).start()
        # TLS MITM 上下文：内嵌 PEM 写入私有临时目录，仅 fixture 生命周期内存在
        self._tls_dir = tempfile.TemporaryDirectory()
        cert_path = Path(self._tls_dir.name) / "fixture-cert.pem"
        key_path = Path(self._tls_dir.name) / "fixture-key.pem"
        cert_path.write_text(_FIXTURE_CERT_PEM, encoding="ascii")
        key_path.write_text(_FIXTURE_KEY_PEM, encoding="ascii")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert_path), str(key_path))
        _FakeForwardProxy.tls_context = context

    def stop(self) -> None:
        _FakeForwardProxy.tls_context = None
        if self._tls_dir is not None:
            self._tls_dir.cleanup()
            self._tls_dir = None
        self.proxy.shutdown()
        self.direct.shutdown()
        self.proxy.server_close()
        self.direct.server_close()
        _disallow_fixture_port(self.proxy.server_address[1])
        _disallow_fixture_port(self.direct.server_address[1])

    def reset(self, *, behavior: str = "ok") -> None:
        _FakeForwardProxy.behavior = behavior
        _FakeForwardProxy.received = []
        _FakeForwardProxy.connects = []
        _FakeForwardProxy.statuses = []
        _FakeForwardProxy.mitm_received = []
        _DirectRecorder.received = []


# ---------------------------------------------------------------------------
# 配置环境助手：进程 env 变更 + Config 单例重建（finally 全量恢复）
# ---------------------------------------------------------------------------


@contextmanager
def _proxy_config(proxy_value: Optional[str], *,
                  extra_env: Optional[Dict[str, str]] = None) -> Iterator[None]:
    backup_var = os.environ.get(_PROXY_VAR)
    backup_extra = {name: os.environ.get(name) for name in (extra_env or {})}
    try:
        if proxy_value is None:
            os.environ.pop(_PROXY_VAR, None)
        else:
            os.environ[_PROXY_VAR] = proxy_value
        for name, value in (extra_env or {}).items():
            os.environ[name] = value
        Config.reset_instance()
        yield
    finally:
        if backup_var is None:
            os.environ.pop(_PROXY_VAR, None)
        else:
            os.environ[_PROXY_VAR] = backup_var
        for name, saved in (backup_extra or {}).items():
            if saved is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = saved
        Config.reset_instance()


@pytest.fixture(autouse=True)
def _egress_isolation() -> Iterator[_IsolationScope]:
    """作用域隔离（autouse，函数级）：import 无副作用——隔离只在每个测试
    期间安装，teardown 完全恢复并断言还原。

    安装内容：清空继承代理 env（记录原值）→ **隔离真实授权获取**（作用域内
    把模块级 ``_get_nid`` 短路为固定合成值、``_auth_patch_enabled`` 固定为
    True——本地与 CI/生产行为一致且覆盖 auth 激活路径，wire 断言不依赖真实
    授权外联/授权缓存冷热 TTL/前序用例；auth 路由本身不关闭）→ neutralize
    系统代理自动发现（stdlib + requests 按值绑定缝；平台可选的 macOS sysconf
    缝按**存在性**快照/临时替换/恢复——缺席保持缺席，在位则恢复原对象身份）
    → 以本轮 fresh 注册表替换 ``socket.connect``/``connect_ex``/
    ``getaddrinfo``（TCP 仅放行本轮注册的精确回环端口；DNS 仅数字地址与本轮
    注册的主机映射，解析由守卫直接合成）。
    恢复内容：全部缝按快照还原、env 按原值还原、活跃作用域清空，并断言
    全局状态与 import 快照一致——不毒化同进程其他模块的既有测试。
    """
    global _ACTIVE

    saved_env = {name: os.environ.get(name) for name in _PROXY_ENV_VARS}
    saved = {
        "connect": socket.socket.connect,
        "connect_ex": socket.socket.connect_ex,
        "getaddrinfo": socket.getaddrinfo,
        "getproxies": _urllib_request.getproxies,
        "requests_getproxies": _requests_utils.getproxies,
        "requests_proxy_bypass": _requests_utils.proxy_bypass,
        "auth_patch_enabled": _eastmoney_patch_module._auth_patch_enabled,
    }
    saved_auth_nid = _eastmoney_patch_module._get_nid
    saved_auth_random = _eastmoney_patch_module.random
    # 平台可选属性：进入时的存在性与原对象身份（缺席不读取）
    entry_platform_attrs = {
        name: getattr(_urllib_request, name)
        for name in _OPTIONAL_PLATFORM_PROXY_ATTRS
        if hasattr(_urllib_request, name)
    }
    for name in _PROXY_ENV_VARS:
        os.environ.pop(name, None)
    # create=True：属性缺席时临时安装中和实现，stop() 恢复为缺席；
    # 属性在位时保存原对象并在 stop() 恢复同一身份。进入 try 之后才安装，
    # 保证 setup 异常也走 finally 恢复。
    platform_stack = ExitStack()
    _urllib_request.getproxies = (
        lambda: _urllib_request.getproxies_environment())
    _requests_utils.getproxies = _urllib_request.getproxies
    _requests_utils.proxy_bypass = _env_only_proxy_bypass

    scope = _IsolationScope()

    def guarded_connect(sock: socket.socket, address: Any) -> Any:
        if (isinstance(address, tuple) and len(address) >= 2
                and isinstance(address[0], str)
                and _is_loopback_host(address[0])
                and isinstance(address[1], int)
                and address[1] in scope.owned_ports):
            scope.allowed.append(repr(address))
            _CUMULATIVE_ALLOWED.append(repr(address))
            return saved["connect"](sock, address)
        record = repr(address)
        scope.denied.append(record)
        _CUMULATIVE_DENIED.append(record)
        raise AssertionError(
            f"测试隔离守卫：仅允许本轮注册的 fixture 回环端口，拒绝 {address!r}")

    def guarded_connect_ex(sock: socket.socket, address: Any) -> Any:
        try:
            return guarded_connect(sock, address)
        except AssertionError:
            # connect_ex 以错误码表达失败（urllib3 不走此缝，守卫为完备性保留）
            return -1

    def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        # 主机映射由守卫直接合成（本轮显式注册），不触达真实解析器；
        # 数字地址放行；其余主机名拒绝——离线运行零真实外部 DNS 查询
        if isinstance(host, str) and host in scope.dns_mappings:
            port = scope.dns_mappings[host]
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
        try:
            ipaddress.ip_address(host)
            return saved["getaddrinfo"](host, *args, **kwargs)
        except (ValueError, TypeError):
            pass
        record = f"dns:{host!r}"
        scope.denied.append(record)
        _CUMULATIVE_DENIED.append(record)
        raise AssertionError(f"测试隔离守卫：非 fixture DNS 查询被拒绝 {host!r}")

    socket.socket.connect = guarded_connect  # type: ignore[assignment]
    socket.socket.connect_ex = guarded_connect_ex  # type: ignore[assignment]
    socket.getaddrinfo = guarded_getaddrinfo  # type: ignore[assignment]
    _ACTIVE = scope
    try:
        # 真实授权获取隔离：_get_nid 短路为固定合成值、auth/throttle 随机暂停
        # 确定性归零（auth 路由保持开启，requests/TLS/proxy 状态拒绝等真实
        # 风险层零 mock）
        platform_stack.enter_context(patch.object(
            _eastmoney_patch_module, "_get_nid",
            lambda user_agent: _SYNTHETIC_NID))
        platform_stack.enter_context(patch.object(
            _eastmoney_patch_module, "random", _DeterministicAuthRandom))
        _eastmoney_patch_module._auth_patch_enabled = True
        platform_stack.enter_context(patch.object(
            _urllib_request, "getproxies_macosx_sysconf", lambda: {}, create=True))
        platform_stack.enter_context(patch.object(
            _urllib_request, "proxy_bypass_macosx_sysconf",
            lambda host: 0, create=True))
        yield scope
    finally:
        # 完全恢复 + invariance 断言（teardown 失败即测试失败，绝不静默残留）
        platform_stack.close()
        assert _eastmoney_patch_module._get_nid is saved_auth_nid
        assert _eastmoney_patch_module.random is saved_auth_random
        _eastmoney_patch_module._auth_patch_enabled = saved["auth_patch_enabled"]
        assert _eastmoney_patch_module._auth_patch_enabled == saved["auth_patch_enabled"]
        for name in _OPTIONAL_PLATFORM_PROXY_ATTRS:
            if name in entry_platform_attrs:
                assert getattr(_urllib_request, name) is entry_platform_attrs[name], name
            else:
                assert not hasattr(_urllib_request, name), name
        socket.socket.connect = saved["connect"]  # type: ignore[assignment]
        socket.socket.connect_ex = saved["connect_ex"]  # type: ignore[assignment]
        socket.getaddrinfo = saved["getaddrinfo"]  # type: ignore[assignment]
        _urllib_request.getproxies = saved["getproxies"]
        _requests_utils.getproxies = saved["requests_getproxies"]
        _requests_utils.proxy_bypass = saved["requests_proxy_bypass"]
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        _ACTIVE = None
        assert socket.socket.connect is _IMPORT_SNAPSHOT["connect"]
        assert socket.socket.connect_ex is _IMPORT_SNAPSHOT["connect_ex"]
        assert socket.getaddrinfo is _IMPORT_SNAPSHOT["getaddrinfo"]
        assert _urllib_request.getproxies is _IMPORT_SNAPSHOT["getproxies"]
        assert _ACTIVE is None


@pytest.fixture()
def loopback(_egress_isolation: _IsolationScope) -> Iterator[_LoopbackFixture]:
    eastmoney_patch(budget_only=True)  # 幂等：确保 wire 边界在位
    fixture = _LoopbackFixture()
    fixture.start()
    fixture.reset()
    denials_before = len(_egress_isolation.denied)
    try:
        yield fixture
    finally:
        fixture.stop()
        # 本测试期间零被拒目标（允许目标全部为本轮注册端口——守卫按构造保证）
        assert len(_egress_isolation.denied) == denials_before, \
            _egress_isolation.denied[denials_before:]
        # 执行器复用无上下文残留（worker 线程后续任务不得继承 scoped 代理）——
        # 覆盖所有失败路径（重定向/HTTP 状态拒绝/TLS/连接失败/预算）的退出
        clean = svc._FETCH_EXECUTOR.submit(
            lambda: sector_eastmoney_proxy.get()).result(5)
        assert clean is None, clean


def _budget(seconds: float) -> "svc.ReportBudget":
    return svc.ReportBudget(seconds, finish_margin=0.1)


# ---------------------------------------------------------------------------
# 0) 测试隔离自身：系统代理发现 neutralize + 未持有回环端口不可用（哨兵）
# ---------------------------------------------------------------------------


def test_isolation_neutralizes_system_proxy_discovery() -> None:
    """测试进程内系统代理自动发现不可见（macOS sysconf 缝 + requests 缝）。

    根因背景：仅放行「任意回环」的守卫挡不住本机系统代理——它在回环上监听、
    可中转外网；因此默认路径的确定性除「零被拒计数」外，还须证明 discovery
    在测试进程内不再暴露系统代理。显式 env 注入优先级由专门用例行使。
    """
    import requests.utils as requests_utils

    sysconf = getattr(_urllib_request, "getproxies_macosx_sysconf", None)
    # 平台可选缝：fixture 在测试期间安装中和实现（缺席平台临时 create，
    # teardown 恢复缺席）；在位时必须是空发现，不得暴露系统代理。
    assert callable(sysconf) and sysconf() == {}
    assert _urllib_request.getproxies() == {}
    assert requests_utils.getproxies() == {}
    # 显式环境注入仍可见（优先级用例依赖此路径，不被隔离层吞掉）
    os.environ["HTTP_PROXY"] = "http://192.0.2.1:9"
    try:
        Config.reset_instance()
        assert _urllib_request.getproxies() == {"http": "http://192.0.2.1:9"}
    finally:
        os.environ.pop("HTTP_PROXY", None)
        Config.reset_instance()


def test_isolation_denies_unowned_loopback_listener() -> None:
    """哨兵负向对照：未注册的回环监听器（如本机系统代理端口）不可被使用。

    本机系统代理是回环端口但不是本测试的 fixture——「0 被拒计数」不能自证
    隔离，须证明对未持有回环端口的任何连接尝试都被拒绝且对端零接收。
    """
    sentinel = socket.socket()
    sentinel.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sentinel.bind(("127.0.0.1", 0))
    sentinel.listen(1)
    port = sentinel.getsockname()[1]
    accepted: List[Any] = []
    stop = threading.Event()

    def accept_loop() -> None:
        sentinel.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = sentinel.accept()
                accepted.append(conn)
                conn.close()
            except OSError:
                continue

    thread = threading.Thread(target=accept_loop, daemon=True)
    thread.start()
    scope = _active_scope()
    denials_before = len(scope.denied)
    try:
        with pytest.raises(AssertionError, match="测试隔离守卫"):
            requests.get(f"http://127.0.0.1:{port}/", timeout=2)
    finally:
        stop.set()
        thread.join(1)
        sentinel.close()
    assert len(scope.denied) == denials_before + 1, scope.denied
    assert repr(("127.0.0.1", port)) in scope.denied[-1], scope.denied[-1]
    assert accepted == [], "未持有端口竟能建立连接——隔离失效"


def test_auth_fetch_synthetic_under_cold_or_expired_cache(
        _egress_isolation: _IsolationScope) -> None:
    """冷缓存/过期缓存下授权获取都是确定性合成值，零真实外联。

    CI 42462666183 根因回归：授权缓存（成功 TTL 20s、失败负缓存每次仍重试）
    的冷热与 TTL 不影响 wire 测试行为——fixture 已把 ``_get_nid`` 短路为固定
    合成值，缓存字段完全不被触及。
    """
    cache = _eastmoney_patch_module._cache
    saved_data, saved_expire = cache.data, cache.expire_at
    denials_before = len(_egress_isolation.denied)
    try:
        # 冷缓存（CI 28 分钟全套后的真实状态）
        cache.data, cache.expire_at = None, 0
        assert _eastmoney_patch_module._get_nid("ua") == _SYNTHETIC_NID
        # 过期缓存（有过残值但已过期）
        cache.data, cache.expire_at = "stale-nid", 0
        assert _eastmoney_patch_module._get_nid("ua") == _SYNTHETIC_NID
        # 零真实外联：无新增 denial（含 DNS）
        assert len(_egress_isolation.denied) == denials_before, \
            _egress_isolation.denied[denials_before:]
    finally:
        cache.data, cache.expire_at = saved_data, saved_expire


def test_wire_dispatch_stable_when_prior_module_enabled_auth(
        loopback: _LoopbackFixture) -> None:
    """前序模块启用 auth 路由后 wire dispatch 仍稳定（CI 失败机制直接反例）。

    既有测试调用 ``eastmoney_patch()``（budget_only=False）会把模块级
    ``_auth_patch_enabled`` 永久置 True——CI 上到本文件 wire 测试时每次东财
    目标 dispatch 都触发真实授权外联（anonflow2），被 DNS 守卫拒绝后进入
    denial 并漂移计数。fixture 作用域内的合成授权短路使 dispatch 与前序
    状态完全无关。
    """
    with _proxy_config(loopback.proxy_addr):
        # 显式走"前序模块"真实置位路径（fixture 已固定 True，此调用再验证
        # 真实入口不改变确定性）
        eastmoney_patch(budget_only=False)
        try:
            assert svc._http_get_json(
                "http://7.push2his.eastmoney.com/api/qt/stock/kline/get",
                {"secid": "90.BK1277"}, budget=_budget(5.0)) == {"ok": True}
        finally:
            eastmoney_patch(budget_only=True)
    assert _FakeForwardProxy.received, "wire dispatch 应到达 fixture 代理"
    scope_denied = len(_active_scope().denied)
    assert scope_denied == 0, _active_scope().denied


def test_module_import_is_inert_and_restores_global_state() -> None:
    """import 无副作用 + teardown 完全恢复的不变式检查。

    本测试体运行时隔离 fixture 已激活（seams 为作用域替换版本）；teardown
    恢复并断言与 import 快照一致（快照在模块 import 时只读捕获）。本测试
    断言的是「本轮作用域内替换确实发生且与模块提供的守卫语义一致」，
    teardown 断言负责「恢复后与 import 时完全一致」——两条合起来证明：
    收集/导入本模块不改变全局状态，fixture 退出后不留任何残留。
    """
    assert socket.socket.connect is not _IMPORT_SNAPSHOT["connect"]  # 作用域内已替换
    assert socket.getaddrinfo is not _IMPORT_SNAPSHOT["getaddrinfo"]
    sysconf = getattr(_urllib_request, "getproxies_macosx_sysconf", None)
    assert callable(sysconf) and sysconf() == {}
    # 授权获取在作用域内为确定性合成短路（teardown 恢复原对象由 fixture 断言）
    assert _eastmoney_patch_module._get_nid("ua") == _SYNTHETIC_NID
    assert _eastmoney_patch_module._auth_patch_enabled is True
    # auth/throttle 随机暂停在作用域内确定性归零
    assert _eastmoney_patch_module.random.uniform(1, 4) == 0.0
    # 显式 env 注入 sentinel：fixture teardown 应将其原样恢复
    os.environ["HTTP_PROXY"] = "http://collection-sentinel.invalid:9"
    # 活跃作用域为本轮 fresh 注册表（非进程级共享残留）
    assert isinstance(_active_scope(), _IsolationScope)
    assert _active_scope().owned_ports == set()
    assert _active_scope().dns_mappings == {}

    # 对导入不变式的静态断言：模块只读捕获快照，未在 import 期做任何注册表
    # 属性写入（socket 模块无本模块私有残留属性）
    for attr in vars(socket):
        if attr.startswith("_sector_egress"):
            raise AssertionError(f"模块 import 残留 socket 属性: {attr}")


# ---------------------------------------------------------------------------
# 1) 配置校验：空/未设置兼容、合法形态、无效值只提变量名
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    (None, None),
    ("", None),
    ("   ", None),
    ("http://10.0.0.1:8080", "http://10.0.0.1:8080"),
    ("https://proxy.internal", "https://proxy.internal"),
    ("http://svcuser:svcpass@10.0.0.1:3128", "http://svcuser:svcpass@10.0.0.1:3128"),
    ("HTTPS://Proxy.Example.com:8443", "HTTPS://Proxy.Example.com:8443"),
    ("http://[::1]:3128", "http://[::1]:3128"),
    ("http://localhost:8118", "http://localhost:8118"),
    ("http://my_proxy.corp:3128", "http://my_proxy.corp:3128"),
    ("http://7.push2his.eastmoney.com:8080", "http://7.push2his.eastmoney.com:8080"),
])
def test_proxy_config_valid_forms(raw: Optional[str], expected: Optional[str]) -> None:
    assert validate_sector_eastmoney_proxy_url(raw) == expected


@pytest.mark.parametrize("raw", [
    "ftp://10.0.0.1:8080",
    "socks5://10.0.0.1:1080",
    "http://",
    "10.0.0.1:8080",
    "http://host/prefix?x=1",
    "http://host/path",
    "http://host/#frag",
    "http://host:70000",
    "http://host:0",
    "http://ho st",
    "http://host\x00",
    "http://ho\nst",
    # 原始输入控制字符必须在归一化（strip）之前拒绝：换行前缀的「其余合法」
    # URL、前后缀空白中的换行/制表符一律不放行
    "\nhttp://10.0.0.1:8080",
    "  \n http://10.0.0.1:8080",
    "\thttp://10.0.0.1:8080",
    "http://10.0.0.1:8080\n",
    # urlparse 不校验主机字符集：% 等非法字符、非 ASCII、坏连字符/空段由
    # DNS/IP 语法校验拒绝
    "http://bad%host:8080",
    "http://ho st:8080",
    "http://-bad.label:8080",
    "http://bad-.label:8080",
    "http://label..gap:8080",
    "https://中文名:8080",
])
def test_proxy_config_invalid_rejected_with_key_name_only(raw: str) -> None:
    with pytest.raises(ValueError) as excinfo:
        validate_sector_eastmoney_proxy_url(raw)
    message = str(excinfo.value)
    assert _PROXY_VAR in message, message
    # 安全错误只含变量名，绝不回显配置值、不链原始解析错误
    assert raw not in message, message
    assert "urlparse" not in message and "urllib" not in message


# ---------------------------------------------------------------------------
# 2) 主机匹配完整矩阵
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url,expected", [
    ("https://push2.eastmoney.com/api/qt/stock/get", True),
    ("https://push2his.eastmoney.com/api/qt/stock/trends2/get", True),
    ("http://7.push2his.eastmoney.com/api/qt/stock/kline/get", True),
    ("https://17.push2.eastmoney.com/api/qt/clist/get", True),
    ("https://99.push2his.eastmoney.com/x", True),
    ("https://1.push2.eastmoney.com/x", True),
    ("https://PUSH2.EASTMONEY.COM/x", True),
    ("https://17.Push2His.EastMoney.com/x", True),
    # 欺骗形态：子串/后缀/多余数字段/端口伪装一律不匹配
    ("https://evilpush2.eastmoney.com/x", False),
    ("https://push2.eastmoney.com.evil.com/x", False),
    ("https://push21.eastmoney.com/x", False),
    ("https://0.push2.eastmoney.com/x", True),
    # 无关东财域与其他主机：保持既有路径
    ("https://datacenter.eastmoney.com/securities/api/data/v1/get", False),
    ("https://anonflow2.eastmoney.com/backend/api/webreport", False),
    ("https://quote.eastmoney.com/center/x", False),
    ("https://qt.gtimg.cn/q=sh000300", False),
    ("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get", False),
    ("https://www.qyyjt.cn/x", False),
    ("not-a-url", False),
    ("", False),
])
def test_scoped_host_matching_matrix(url: str, expected: bool) -> None:
    assert is_scoped_sector_quote_url(url) is expected


# ---------------------------------------------------------------------------
# 3) registry / 系统设置敏感面 + Config repr + 导出剔除
# ---------------------------------------------------------------------------


def test_registry_marks_proxy_sensitive_password_readonly() -> None:
    from src.core.config_registry import get_field_definition

    field = get_field_definition("SECTOR_EASTMONEY_PROXY_URL")
    assert field["is_sensitive"] is True
    assert field["ui_control"] == "password"
    assert field["is_editable"] is False
    assert field["category"] == "data_source"


def test_config_repr_excludes_proxy_value() -> None:
    """部署 secret 不进生成 dataclass repr，字段值仍可编程读取。"""
    synthetic = "http://svcuser:svcpass@10.9.8.7:3128"
    config = Config(sector_eastmoney_proxy_url=synthetic)
    rendered = repr(config)
    assert synthetic not in rendered
    assert "svcuser" not in rendered and "svcpass" not in rendered
    assert "10.9.8.7" not in rendered
    assert config.sector_eastmoney_proxy_url == synthetic  # 功能读取不受影响
    assert "None" in repr(Config(sector_eastmoney_proxy_url=None))


def _make_env_service(env_path: Path) -> Any:
    from src.core.config_manager import ConfigManager
    from src.services.system_config_service import SystemConfigService

    return SystemConfigService(manager=ConfigManager(env_path=env_path))


def test_system_config_service_masks_proxy_value() -> None:
    from src.core.config_manager import ConfigManager
    from src.services.system_config_service import SystemConfigService

    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / ".env"
        env_path.write_text(
            "STOCK_LIST=600519,000001\n"
            "SECTOR_EASTMONEY_PROXY_URL=http://svcuser:svcpass@10.9.8.7:3128\n",
            encoding="utf-8",
        )
        backup_env = os.environ.get("ENV_FILE")
        os.environ["ENV_FILE"] = str(env_path)
        Config.reset_instance()
        try:
            service = SystemConfigService(manager=ConfigManager(env_path=env_path))
            payload = service.get_config(include_schema=True)
            items = {item["key"]: item for item in payload["items"]}
            item = items["SECTOR_EASTMONEY_PROXY_URL"]
            assert item["value"] == payload["mask_token"]
            assert item["is_masked"] is True
            assert item["schema"]["is_sensitive"] is True
            flat = json.dumps(payload, ensure_ascii=False)
            assert "svcuser" not in flat and "svcpass" not in flat
            assert "10.9.8.7" not in flat
        finally:
            if backup_env is None:
                os.environ.pop("ENV_FILE", None)
            else:
                os.environ["ENV_FILE"] = backup_env
            Config.reset_instance()


def _dotenv_parse(content: str) -> Dict[str, Any]:
    from dotenv import dotenv_values

    return dict(dotenv_values(stream=io.StringIO(content), interpolate=False))


def _is_deployment_key(key: Any) -> bool:
    # dotenv 保留键名两侧引号（'"KEY"'）——比较前剥离引号 Token
    return key is not None and str(key).strip().strip("\"'").upper() == _PROXY_VAR


def test_config_exports_exclude_deployment_only_key() -> None:
    """export_env 与 export_desktop_env 均不得携带部署 secret。

    以 ConfigManager 同源 dotenv 解析逐形态驱动：``export `` 前缀、单/双引号
    键、多行引号值（整体剔除）、顶层历史注释赋值行、重复赋值；无关设置经
    **导出→回析等价**校验（非仅子串缺失），无关键文件内容逐字节不变。
    """
    import io

    synthetic = "http://svcuser:svcpass@10.9.8.7:3128"
    key = _PROXY_VAR
    cases = {
        "plain": "STOCK_LIST=600519\n" + key + "=" + synthetic + "\n",
        "export_prefix": "STOCK_LIST=600519\nexport " + key + "=" + synthetic + "\n",
        "quoted_key": "STOCK_LIST=600519\n'" + key + "'='" + synthetic + "'\n",
        "dquoted_key": 'STOCK_LIST=600519\n"' + key + '"="' + synthetic + '"\n',
        "multiline_secret": (
            "STOCK_LIST=600519\n" + key
            + "='http://\nsvcuser:svcpass@10.9.8.7:3129'\n"),
        "duplicates": (
            "STOCK_LIST=600519\n" + key + "=http://a:b@h:1\n"
            + key + "=" + synthetic + "\n"),
        "top_commented_history": (
            "STOCK_LIST=600519\n# " + key + "=http://old:pw@10.0.0.1:1\n"
            "# 说明注释保留\n"),
        "unrelated_multiline_with_keyish_comment": (
            'CUSTOM_NOTE="first line\n# ' + key + '=plain prompt text\nlast line"\n'
            + key + "=" + synthetic + "\nSTOCK_LIST=600519\n"),
        "unrelated_quoted_multiline": (
            "UNRELATED='start\n# OTHER_KEY=x\nend'\nSTOCK_LIST=600519\n"),
        "unrelated_u2028": "CUSTOM_NOTE='first" + chr(0x2028)
                          + "second'\nSTOCK_LIST=600519\n",
        "no_final_newline": "STOCK_LIST=600519",
        "keyless_comments": "# 纯注释\n# " + key + " 只在注释文本里提及\n",
    }
    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / ".env"
        for name, content in cases.items():
            env_path.write_text(content, encoding="utf-8")
            service = _make_env_service(env_path)
            before = _dotenv_parse(content)
            for export_name in ("export_env", "export_desktop_env"):
                exported = getattr(service, export_name)()["content"]
                label = f"{name}/{export_name}"
                assert "svcuser" not in exported, label
                assert "svcpass" not in exported, label
                assert synthetic not in exported, label
                assert "old:pw" not in exported, label
                assert not any(_is_deployment_key(k) for k in _dotenv_parse(exported)), \
                    label
                unrelated_before = {k: v for k, v in before.items()
                                    if not _is_deployment_key(k)}
                unrelated_after = {k: v for k, v in _dotenv_parse(exported).items()
                                   if not _is_deployment_key(k)}
                assert unrelated_before == unrelated_after, label
                if name == "top_commented_history":
                    # 顶层历史注释赋值行被剔除；同文件其余注释/内容逐字节保留
                    assert ("# " + key + "=http://old:pw@10.0.0.1:1") not in exported, \
                        label
                    assert "# 说明注释保留" in exported, label
                    assert exported == ("STOCK_LIST=600519\n# 说明注释保留\n"), label
                elif not any(_is_deployment_key(k) for k in before) \
                        and name != "keyless_comments":
                    # 无关键文件（dotenv 可见层面无任何部署键素材）：导出逐字节
                    # 不变（不追加换行、不规范化 U+2028）
                    assert content == exported, label
            # key 存在形态：剔除后剩余内容回析与「原文剔除该键」语义一致
            if any(_is_deployment_key(k) for k in before):
                exported = service.export_env()["content"]
                assert _dotenv_parse(exported) == {
                    k: v for k, v in before.items() if not _is_deployment_key(k)
                }, name


def test_config_exports_fail_closed_on_malformed_secret_statements() -> None:
    """畸形 deployment 语句（binding.error）不得以未过滤原文成功导出。

    普通编辑错误（未闭合引号/尾随垃圾）使 dotenv 无法给出键语义——此时
    无法证明部署 secret 已被安全剔除，两路导出必须显式失败
    （``ConfigExportError``，消息只含安全语义、不含原始语句/值/原因链），
    绝不返回原文；无关畸形语句（不含 deployment 键名）原样保留，同文件
    内的合法 deployment 赋值仍被剔除，BOM 原样保留。
    """
    from src.services.system_config_service import ConfigExportError

    synthetic = "http://svcuser:svcpass@10.9.8.7:3128"
    key = _PROXY_VAR
    malformed_cases = {
        "unterminated_quote": key + "='" + synthetic + "\nSTOCK_LIST=600519\n",
        "trailing_garbage": key + "='" + synthetic + "' garbage\nSTOCK_LIST=600519\n",
    }
    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / ".env"
        for name, content in malformed_cases.items():
            env_path.write_text(content, encoding="utf-8")
            service = _make_env_service(env_path)
            for export_name in ("export_env", "export_desktop_env"):
                with pytest.raises(ConfigExportError) as excinfo:
                    getattr(service, export_name)()
                message = str(excinfo.value)
                assert "svcpass" not in message and synthetic not in message, name
                assert ".env" in message, name

        # 无关畸形语句（不含 deployment 键名）原样保留；同文件的合法
        # deployment 赋值仍被剔除；BOM 原样保留
        mixed = ("UNRELATED=\"unterminated\n"
                 + key + "=" + synthetic + "\n"
                 + "STOCK_LIST=600519\n")
        env_path.write_text(mixed, encoding="utf-8")
        service = _make_env_service(env_path)
        exported = service.export_env()["content"]
        assert 'UNRELATED="unterminated' in exported  # 无关畸形 span 原样保留
        assert synthetic not in exported and (key + "=") not in exported
        assert exported == 'UNRELATED="unterminated\nSTOCK_LIST=600519\n'

        bom_mixed = "\ufeff# Windows UTF8 document\n" + key + "=" + synthetic + "\n"
        env_path.write_text(bom_mixed, encoding="utf-8")
        service = _make_env_service(env_path)
        exported = service.export_env()["content"]
        assert exported.startswith("\ufeff")  # BOM 原样保留
        assert synthetic not in exported
        assert exported == "\ufeff# Windows UTF8 document\n"


def test_config_import_update_semantics_preserved_for_other_keys() -> None:
    """导出剔除不改既有导入/更新语义：导出→改值→导入回路正常，且部署
    secret 在导入合并后仍保留在活跃 .env（剔除只在导出载荷，不动部署侧）。
    """
    from src.core.config_manager import ConfigManager
    from src.services.system_config_service import SystemConfigService

    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / ".env"
        env_path.write_text(
            "STOCK_LIST=600519,000001\n"
            "SECTOR_EASTMONEY_PROXY_URL=http://svcuser:svcpass@10.9.8.7:3128\n",
            encoding="utf-8",
        )
        service = SystemConfigService(manager=ConfigManager(env_path=env_path))
        exported = service.export_env()["content"]
        assert "STOCK_LIST=600519,000001" in exported
        assert "svcuser" not in exported  # 部署 secret 不进导出载荷
        merged = exported.replace("STOCK_LIST=600519,000001",
                                  "STOCK_LIST=000001,600519")
        result = service.import_env(
            config_version=service.export_env()["config_version"],
            content=merged,
            reload_now=True,
        )
        assert result["success"] is True
        assert result["applied_count"] >= 1
        assert "STOCK_LIST" in result["updated_keys"]
        live = env_path.read_text(encoding="utf-8")
        assert "STOCK_LIST=000001,600519" in live  # 导入生效
        assert "SECTOR_EASTMONEY_PROXY_URL=http://svcuser:svcpass@10.9.8.7:3128" in live


# ---------------------------------------------------------------------------
# 4) wire 级：未配置复现既有直连默认（send 缝观察 + wire 直连尝试）
# ---------------------------------------------------------------------------


def test_unset_config_reproduces_direct_request_options(loopback: _LoopbackFixture) -> None:
    """未配置代理：sector worker 对 push2his 出站不带显式 proxies/allow_redirects。"""
    observed: List[Dict[str, Any]] = []

    def recording_send(self: requests.Session, request: requests.PreparedRequest,
                       **kwargs: Any) -> requests.Response:
        observed.append(dict(kwargs))
        raise requests.ConnectionError("synthetic wire stop")

    with _proxy_config(None):
        with patch.object(requests.Session, "send", recording_send):
            result = svc._http_get_json(
                "https://push2his.eastmoney.com/api/qt/stock/kline/get",
                {"secid": "90.BK1277"}, budget=_budget(5.0))
    assert result is None  # 合成终端失败按既有缺口语义（默认 1 次重试 → 2 条 wire）
    assert len(observed) == 2, observed
    for kwargs in observed:
        assert not (kwargs.get("proxies") or {}).get("http"), kwargs.get("proxies")
        assert not (kwargs.get("proxies") or {}).get("https"), kwargs.get("proxies")
        assert kwargs.get("allow_redirects") is not False
        # 校验保留：任何路径都不注入 verify=False
        assert kwargs.get("verify") is not False, kwargs.get("verify")


def test_unset_config_wire_goes_direct_without_proxy(loopback: _LoopbackFixture) -> None:
    """wire 级：未配置时确走直连（kwargs 观测须有 wire 行为对应）。

    push2his 解析打桩到「接受即 reset」回环监听器（端口注册为 fixture 持有）：
    直连尝试得到确定性 ConnectionError（默认 1 次重试 → 2 次直连尝试）；若
    scoped 路由被错误注入，请求会改走伪代理并成功——本测试即失败。系统代理
    自动发现在测试进程内已 neutralize（见隔离用例），显式清空环境代理为双保险。
    """
    listener = _ResettingListener()
    listener.start()
    try:
        with _dns_loopback("push2his.eastmoney.com", listener.port):
            with _proxy_config(None, extra_env={
                "HTTP_PROXY": "", "http_proxy": "",
                "HTTPS_PROXY": "", "https_proxy": "",
            }):
                result = svc._http_get_json(
                    "https://push2his.eastmoney.com/api/qt/stock/kline/get",
                    {"secid": "90.BK1277"}, budget=_budget(6.0))
    finally:
        listener.stop()
    assert result is None  # 直连 reset 按既有缺口语义
    assert listener.attempts == 2, listener.attempts  # wire 直连发生且被观测
    assert _FakeForwardProxy.received == []  # 零代理注入
    assert _DirectRecorder.received == []


# ---------------------------------------------------------------------------
# 5) wire 级：配置后显式覆盖环境代理/NO_PROXY；两条 worker 路径
# ---------------------------------------------------------------------------


def test_configured_proxy_overrides_env_for_push2_http(loopback: _LoopbackFixture) -> None:
    """环境 HTTP_PROXY/NO_PROXY 干扰下，push2his HTTP 目标仍走 scoped 代理。"""
    with _proxy_config(
        loopback.proxy_addr,
        extra_env={
            "HTTP_PROXY": "http://192.0.2.1:9",
            "http_proxy": "http://192.0.2.1:9",
            "HTTPS_PROXY": "http://192.0.2.1:9",
            "https_proxy": "http://192.0.2.1:9",
            "NO_PROXY": ".eastmoney.com,push2his.eastmoney.com",
            "no_proxy": ".eastmoney.com,push2his.eastmoney.com",
        },
    ):
        result = svc._http_get_json(
            "http://7.push2his.eastmoney.com/api/qt/stock/kline/get",
            {"secid": "90.BK1277"}, budget=_budget(5.0))
    assert result == {"ok": True}
    received = _FakeForwardProxy.received
    assert len(received) == 1, received
    assert received[0]["host"].startswith("7.push2his.eastmoney.com"), received
    # 环境干扰代理（192.0.2.1，非回环）不可达——流量没走它；守卫拒绝并计数
    assert _DirectRecorder.received == []


def test_bounded_ak_worker_path_routes_via_proxy(loopback: _LoopbackFixture) -> None:
    """``_bounded_ak`` worker 内 bind：AkShare 等价出站真实经 scoped 代理。"""
    import akshare as ak

    def fake_ak() -> Any:
        resp = requests.get(
            "http://17.push2.eastmoney.com/api/qt/clist/get",
            params={"pn": "1", "pz": "100"}, timeout=6,
            headers={"User-Agent": "test-ua"})
        resp.raise_for_status()
        return resp.json()

    with _proxy_config(loopback.proxy_addr):
        with patch.object(ak, "stock_board_industry_name_em", fake_ak):
            data = svc._bounded_ak(ak.stock_board_industry_name_em,
                                   budget=_budget(5.0))
    assert data == {"ok": True}
    received = _FakeForwardProxy.received
    assert len(received) == 1, received
    assert received[0]["host"].startswith("17.push2.eastmoney.com"), received


# ---------------------------------------------------------------------------
# 6) 真实 AkShare HTTP 状态边界：合法形状体 200 采纳对照 / 407、502 拒绝
# ---------------------------------------------------------------------------


def test_real_akshare_adopts_http200_valid_body_as_control(
        loopback: _LoopbackFixture) -> None:
    """对照（不 mock）：真实 ``stock_board_industry_hist_em`` 经代理对同一
    合法形状 klines 体在 HTTP 200 下正常采纳——证明 407/502 拒绝测试的
    fixture 体本可被 SDK 采纳，拒绝只能来自状态边界而非体解析失败。
    """
    import akshare as ak

    with _proxy_config(loopback.proxy_addr):
        loopback.reset(behavior="bkvalid")
        frame = svc._bounded_ak(
            ak.stock_board_industry_hist_em, symbol="BK1277",
            start_date="20261108", end_date="20261108",
            budget=_budget(6.0), retries=0)
    assert len(frame) == 1
    assert str(frame.iloc[0]["收盘"]) == "11"
    assert _FakeForwardProxy.statuses == [200], _FakeForwardProxy.statuses
    assert len(_FakeForwardProxy.received) == 1, _FakeForwardProxy.received


@pytest.mark.parametrize("behavior,expected_status", [
    ("proxy407", 407),
    ("em502", 502),
])
def test_real_akshare_rejects_http_error_before_parsing(
        loopback: _LoopbackFixture, behavior: str, expected_status: int) -> None:
    """真实 AkShare 路径（无 mock）：HTTP 4xx/5xx + 合法形状体在 SDK 解析前
    被共享传输边界拒绝。

    断言：显式 RequestException 类别（requests.HTTPError）、消息含状态码且
    无任何 URL、wire 状态码与请求数被观测、零事实采纳、worker 上下文恢复。
    """
    import akshare as ak

    with _proxy_config(loopback.proxy_addr):
        loopback.reset(behavior=behavior)
        with pytest.raises(requests.RequestException) as excinfo:
            svc._bounded_ak(
                ak.stock_board_industry_hist_em, symbol="BK1277",
                start_date="20261108", end_date="20261108",
                budget=_budget(6.0), retries=0)
        message = str(excinfo.value)
        assert isinstance(excinfo.value, requests.HTTPError), type(excinfo.value)
        assert str(expected_status) in message
        assert "eastmoney" not in message and "127.0.0.1" not in message
        assert _FakeForwardProxy.statuses == [expected_status], _FakeForwardProxy.statuses
        assert len(_FakeForwardProxy.received) == 1, _FakeForwardProxy.received
        clean = svc._FETCH_EXECUTOR.submit(
            lambda: sector_eastmoney_proxy.get()).result(5)
        assert clean is None, clean


def test_real_akshare_407_502_messages_and_wire_for_helper_path(
        loopback: _LoopbackFixture, caplog: pytest.LogCaptureFixture) -> None:
    """helper 路径（``_http_get_json``）：407/502/畸形体的真实 wire 分支与
    缺口文案逐一对得上。

    - 407/502：状态边界拒绝 → 缺口含状态码语义；wire 状态码被观测
    - 200 + HTML 体：JSON 解码分支（``response not JSON``）→ wire 状态 200
    """
    import logging

    with _proxy_config(loopback.proxy_addr):
        with caplog.at_level(logging.WARNING,
                             logger="src.services.sector_analysis_service"):
            loopback.reset(behavior="proxy407")
            assert svc._http_get_json("http://push2.eastmoney.com/api/qt/stock/get",
                                      {}, budget=_budget(5.0)) is None
            assert _FakeForwardProxy.statuses == [407, 407], \
                _FakeForwardProxy.statuses  # 默认 1 次重试

            loopback.reset(behavior="em502")
            assert svc._http_get_json("http://push2.eastmoney.com/api/qt/stock/get",
                                      {}, budget=_budget(5.0)) is None
            assert _FakeForwardProxy.statuses == [502, 502], _FakeForwardProxy.statuses

            loopback.reset(behavior="badjson")
            assert svc._http_get_json("http://push2.eastmoney.com/api/qt/stock/get",
                                      {}, budget=_budget(5.0)) is None
            assert _FakeForwardProxy.statuses == [200, 200], _FakeForwardProxy.statuses

    log_text = "\n".join(str(rec.getMessage()) for rec in caplog.records)
    assert "407" in log_text and "502" in log_text
    assert "response not JSON" in log_text  # badjson 走的是解码分支而非状态分支


# ---------------------------------------------------------------------------
# 7) HTTPS：CONNECT 路由 + 自签证书 MITM 证明证书校验保留
# ---------------------------------------------------------------------------


def test_https_connect_via_proxy_tunnel_no_direct_fallback(
        loopback: _LoopbackFixture) -> None:
    """HTTPS 目标经代理 CONNECT 到达 fixture；代理失败无隐藏直连回退。

    CONNECT 观测只证明**路由**，证书校验结论由 MITM fixture 测试承担；本
    测试的结论是：隧道关闭 → 显式供应商失败 → fixture 外零连接（守卫保证）。
    """
    with _proxy_config(loopback.proxy_addr):
        result = svc._http_get_json(
            "https://push2.eastmoney.com/api/qt/stock/get",
            {"secid": "1.600519"}, budget=_budget(6.0), retries=0)
    assert result is None  # 显式供应商失败（隧道关闭），无数据采纳
    assert len(_FakeForwardProxy.connects) == 1, _FakeForwardProxy.connects
    assert _FakeForwardProxy.connects[0].startswith("push2.eastmoney.com:443")
    assert _FakeForwardProxy.received == []


def test_https_certificate_verification_retained_mitm_fails_closed(
        loopback: _LoopbackFixture) -> None:
    """自签证书 MITM fixture：证书校验保留在真实 requests 缝上被证明。

    正路径：自签（不受信）证书 + CN 与目标一致 → 校验在位时 TLS 握手在
    证书链处失败（``certificate verify failed``），隧道内零 HTTP 请求、
    零数据采纳。负向对照：强制 ``verify=False`` 时**同一 fixture** 握手
    成功、伪造体 ``{"attacker": true}`` 被业务采纳——证明上一行断言对
    verify 降级敏感（若校验被关闭，本测试即失败），而非隧道关闭的假象。
    """
    import akshare as ak

    def fake_ak() -> Any:
        return requests.get("https://push2.eastmoney.com/api/qt/stock/get",
                            timeout=6).json()

    with _proxy_config(loopback.proxy_addr):
        loopback.reset(behavior="tls_mitm")

        assert svc._http_get_json("https://push2.eastmoney.com/api/qt/stock/get",
                                  {"secid": "1.600519"}, budget=_budget(6.0),
                                  retries=0) is None
        assert _FakeForwardProxy.connects, _FakeForwardProxy.connects
        assert _FakeForwardProxy.mitm_received == [], _FakeForwardProxy.mitm_received

        with pytest.raises(requests.exceptions.SSLError) as excinfo:
            with patch.object(ak, "stock_board_industry_name_em", fake_ak):
                svc._bounded_ak(ak.stock_board_industry_name_em, retries=0,
                                budget=_budget(6.0))
        assert "certificate verify failed" in str(excinfo.value).lower(), \
            str(excinfo.value)
        assert _FakeForwardProxy.mitm_received == [], _FakeForwardProxy.mitm_received
        clean = svc._FETCH_EXECUTOR.submit(
            lambda: sector_eastmoney_proxy.get()).result(5)
        assert clean is None, clean

        loopback.reset(behavior="tls_mitm")
        original_send = requests.Session.send

        def verify_false_send(session_self: requests.Session,
                              prepared: requests.PreparedRequest,
                              **kwargs: Any) -> requests.Response:
            kwargs["verify"] = False
            return original_send(session_self, prepared, **kwargs)

        requests.Session.send = verify_false_send  # type: ignore[assignment]
        try:
            adopted = svc._http_get_json("https://push2.eastmoney.com/api/qt/stock/get",
                                         {"secid": "1.600519"}, budget=_budget(6.0),
                                         retries=0)
        finally:
            requests.Session.send = original_send  # type: ignore[assignment]
        assert adopted == {"attacker": True}, adopted
        assert len(_FakeForwardProxy.mitm_received) == 1, _FakeForwardProxy.mitm_received


# ---------------------------------------------------------------------------
# 8) 3xx / 连接失败 / 预算
# ---------------------------------------------------------------------------


def test_redirect_3xx_not_followed_and_body_not_adopted(loopback: _LoopbackFixture) -> None:
    """scoped 请求 3xx：不跟随、不采纳 valid-looking body，显式供应商失败。"""
    with _proxy_config(loopback.proxy_addr):
        loopback.reset(behavior="redirect")
        result = svc._http_get_json(
            "http://push2.eastmoney.com/api/qt/stock/get",
            {"secid": "1.600519"}, budget=_budget(5.0), retries=0)
        assert result is None
        assert _FakeForwardProxy.statuses == [302], _FakeForwardProxy.statuses

        def fake_ak() -> Any:
            return requests.get("http://push2.eastmoney.com/api/qt/stock/get",
                                timeout=6).json()

        import akshare as ak
        with pytest.raises(SectorScopedRedirectError) as excinfo:
            with patch.object(ak, "stock_board_industry_name_em", fake_ak):
                svc._bounded_ak(ak.stock_board_industry_name_em, retries=0,
                                budget=_budget(5.0))
        message = str(excinfo.value)
        assert "302" in message
        assert "evil.example.com" not in message  # Location 不进异常文案
        assert len(_FakeForwardProxy.received) == 2, _FakeForwardProxy.received
        assert _FakeForwardProxy.statuses == [302, 302], _FakeForwardProxy.statuses


def test_proxy_connection_refused_no_direct_fallback(loopback: _LoopbackFixture) -> None:
    """代理端口关闭：显式失败，绝无隐藏直连回退（守卫保证）。

    用「绑定时取得、随即关闭」的真实关闭端口产生 OS 级 connection refused
    （端口临时注册为 fixture 允许目标，使 OS 自然拒绝——比固定端口 1 更
    确定性，也不依赖对其他未持有端口的守卫豁免）。
    """
    holder = socket.socket()
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", 0))
    closed_port = holder.getsockname()[1]
    holder.close()
    _allow_fixture_port(closed_port)  # 允许连接尝试（OS 层自然拒绝）
    try:
        with _proxy_config(f"http://127.0.0.1:{closed_port}"):
            result = svc._http_get_json(
                "https://push2.eastmoney.com/api/qt/stock/get",
                {}, budget=_budget(5.0))
    finally:
        _disallow_fixture_port(closed_port)
    assert result is None
    assert _FakeForwardProxy.received == []
    assert _DirectRecorder.received == []


def test_budget_exhaustion_discards_in_flight_no_late_wire(
        loopback: _LoopbackFixture) -> None:
    """慢出站：按剩余取数预算放弃等待，不采纳在途结果，预算后零新出站。"""
    import time as _time

    def slow_supplier() -> Any:
        _time.sleep(1.5)
        return {"late": True}

    with _proxy_config(loopback.proxy_addr):
        with pytest.raises(svc.BudgetExhausted):
            svc._bounded_ak(slow_supplier, budget=_budget(0.8))
        assert _FakeForwardProxy.received == []
        with pytest.raises(svc.BudgetExhausted):
            svc._http_get_json("http://7.push2his.eastmoney.com/api/qt/stock/kline/get",
                               {}, budget=_budget(0.05), retries=0)
        assert _FakeForwardProxy.received == []


# ---------------------------------------------------------------------------
# 9) 凭证零泄漏：日志/gap/异常面/格式化 traceback 先脱敏后截断
# ---------------------------------------------------------------------------


def test_credential_bearing_proxy_error_redacted_before_truncation(
        loopback: _LoopbackFixture, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    cred_proxy = loopback.proxy_addr.replace(
        "http://", "http://svcuser:svcpass@")
    proxy_host_port = loopback.proxy_addr.split("//", 1)[1]
    import akshare as ak

    def fake_ak() -> Any:
        return requests.get("https://push2.eastmoney.com/api/qt/stock/get",
                            timeout=6).json()

    with _proxy_config(cred_proxy):
        loopback.reset(behavior="proxy407")
        with caplog.at_level(logging.WARNING, logger="src.services.sector_analysis_service"):
            assert svc._http_get_json("https://push2.eastmoney.com/api/qt/stock/get",
                                      {}, budget=_budget(5.0)) is None
        log_text = "\n".join(str(rec.getMessage()) for rec in caplog.records)
        assert "svcuser" not in log_text
        assert "svcpass" not in log_text
        assert proxy_host_port not in log_text
        assert cred_proxy not in log_text

        with pytest.raises(requests.RequestException) as excinfo:
            with patch.object(ak, "stock_board_industry_name_em", fake_ak):
                svc._bounded_ak(ak.stock_board_industry_name_em, budget=_budget(5.0))
        message = str(excinfo.value)
        assert "svcuser" not in message
        assert "svcpass" not in message
        assert proxy_host_port not in message
        assert cred_proxy not in message

    raw = f"Cannot connect to proxy {cred_proxy} (connection refused)"
    redacted = redact_sector_eastmoney_proxy_text(raw, cred_proxy)
    assert "svcuser" not in redacted and "svcpass" not in redacted
    assert proxy_host_port not in redacted
    assert redacted[:40].count("[REDACTED]") >= 1


def test_wrapped_and_chained_proxy_errors_redacted_via_worker_surface() -> None:
    """实际渲染状态零泄漏（非仅 args）：errno/strerror、errno/str/filename
    OSError 经真实 ``_bounded_ak`` worker 路径传播后，外层 ``str``、既有
    ``str(exc)[:N]`` gap、``traceback.format_exception`` 全部无 URL/user/
    password；``MaxRetryError.reason`` %r 渲染路径、``__cause__``/
    ``__context__`` 链接段、无凭证 host:port 连接池 repr 变体同样覆盖；
    异常类别逐层保留，未配置/无关心异常零改动。
    """
    import traceback
    from urllib3.exceptions import MaxRetryError
    from urllib3.exceptions import ProxyError as Urllib3ProxyError

    cred_proxy = "http://svcuser:svcpass@10.9.8.7:3128"
    host_port = "10.9.8.7:3128"
    original_getter = svc._current_sector_em_proxy_url
    svc._current_sector_em_proxy_url = lambda: cred_proxy
    try:
        for name, inner in (
            ("errno_strerror", OSError(111, cred_proxy)),
            ("errno_str_filename", OSError(111, "cannot connect", cred_proxy)),
        ):
            def supplier(inner=inner) -> Any:
                raise requests.exceptions.ProxyError(inner)

            with pytest.raises(requests.exceptions.ProxyError) as excinfo:
                svc._bounded_ak(supplier, budget=_budget(5.0), retries=0)
            redacted = excinfo.value
            gap = f"东财个股行业查询失败: {str(redacted)[:50]}"
            formatted = "".join(traceback.format_exception(redacted))
            assert cred_proxy not in str(redacted), name
            assert "svcuser" not in gap and "svcpass" not in gap, name
            assert cred_proxy not in gap, name
            assert cred_proxy not in formatted, name
            assert cred_proxy not in repr(redacted.args), name
            assert type(redacted) is requests.exceptions.ProxyError, name
            assert cred_proxy not in str(redacted.args[0]), name
    finally:
        svc._current_sector_em_proxy_url = original_getter
    clean = svc._FETCH_EXECUTOR.submit(
        lambda: sector_eastmoney_proxy.get()).result(5)
    assert clean is None, clean

    # requests 包装 urllib3 MaxRetryError 的形态（str 经 reason 属性 %r 渲染）
    wrapped = requests.exceptions.ProxyError(
        MaxRetryError(None, "/api/qt/stock/get",
                      Urllib3ProxyError("Unable to connect to proxy",
                                        OSError(cred_proxy))))
    redacted = svc._redact_scoped_proxy_error(wrapped, cred_proxy)
    assert type(redacted) is requests.exceptions.ProxyError
    assert cred_proxy not in str(redacted)
    assert cred_proxy not in repr(redacted)
    assert "svcuser" not in str(redacted) and "svcpass" not in str(redacted)
    assert host_port not in str(redacted)
    assert "".join(traceback.format_exception(redacted)).count(cred_proxy) == 0
    for arg in redacted.args:
        assert cred_proxy not in repr(arg), arg

    max_retry = MaxRetryError(None, "/api/qt/stock/get",
                              Urllib3ProxyError("Cannot connect to proxy",
                                                OSError(cred_proxy)))
    redacted_retry = svc._redact_scoped_proxy_error(max_retry, cred_proxy)
    assert isinstance(redacted_retry, MaxRetryError)
    assert cred_proxy not in str(redacted_retry)
    assert cred_proxy not in repr(redacted_retry.reason)

    cause = OSError(cred_proxy)
    linked = requests.ConnectionError("proxy connect failed")
    linked.__cause__ = cause
    redacted_link = svc._redact_scoped_proxy_error(linked, cred_proxy)
    assert cred_proxy not in repr(redacted_link.args)
    assert cred_proxy not in repr(redacted_link.__cause__.args)
    assert cred_proxy not in str(redacted_link.__cause__)
    chained = requests.ConnectionError("outer")
    chained.__context__ = OSError(cred_proxy)
    redacted_chain = svc._redact_scoped_proxy_error(chained, cred_proxy)
    assert cred_proxy not in repr(redacted_chain.__context__.args)
    assert cred_proxy not in str(redacted_chain.__context__)

    plain = requests.exceptions.ProxyError(
        f"Cannot connect to proxy {host_port}: Max retries exceeded")
    redacted_plain = svc._redact_scoped_proxy_error(plain, cred_proxy)
    assert host_port not in str(redacted_plain)
    assert type(redacted_plain) is requests.exceptions.ProxyError

    untouched = requests.exceptions.ProxyError(f"keep {cred_proxy} as-is")
    assert svc._redact_scoped_proxy_error(untouched, None) is untouched
    assert cred_proxy in str(untouched)

    benign = requests.ConnectionError("ordinary supplier failure")
    assert svc._redact_scoped_proxy_error(benign, cred_proxy) is benign
    assert str(benign) == "ordinary supplier failure"


# ---------------------------------------------------------------------------
# 10) 并发/嵌套/执行器复用无泄漏；非 sector 消费者同 host 不路由（wire 级）
# ---------------------------------------------------------------------------


def test_concurrent_sector_and_nonsector_requests_isolated(
        loopback: _LoopbackFixture) -> None:
    """并发：sector 出站走 scoped 代理；非 sector 直连对照器（无代理拦截）。"""
    results: Dict[str, Any] = {}

    def sector_task() -> None:
        results["sector"] = svc._http_get_json(
            "http://7.push2his.eastmoney.com/api/qt/stock/kline/get",
            {"secid": "90.BK1277"}, budget=_budget(6.0))

    def nonsector_task() -> None:
        with requests.Session() as session:
            resp = session.get(loopback.direct_url, timeout=6)
            results["nonsector"] = resp.json()

    with _proxy_config(loopback.proxy_addr):
        thread_a = threading.Thread(target=sector_task)
        thread_b = threading.Thread(target=nonsector_task)
        thread_a.start()
        thread_b.start()
        thread_a.join(10)
        thread_b.join(10)

    assert results["sector"] == {"ok": True}, results
    assert results["nonsector"] == {"direct": True}, results
    assert _DirectRecorder.received == ["/direct"], _DirectRecorder.received
    assert len(_FakeForwardProxy.received) == 1, _FakeForwardProxy.received
    assert _FakeForwardProxy.received[0]["host"].startswith("7.push2his.eastmoney.com")


def test_nonsector_consumer_same_host_not_routed_wire(loopback: _LoopbackFixture) -> None:
    """wire 级：配置代理但未 bind 的消费者访问 push2 目标仍直连（按上下文判定）。

    push2 解析打桩到「接受即 reset」回环监听器（端口注册为 fixture 持有）：
    未 bind 消费者的直连尝试被 reset（ConnectionError，wire 直连被观测）；
    若路由被错误注入，请求会改走伪代理并成功——本测试即失败。系统代理
    自动发现在测试进程内已 neutralize（见隔离用例）。
    """
    listener = _ResettingListener()
    listener.start()
    try:
        with _dns_loopback("push2.eastmoney.com", listener.port):
            with _proxy_config(loopback.proxy_addr, extra_env={
                "HTTP_PROXY": "", "http_proxy": "",
                "HTTPS_PROXY": "", "https_proxy": "",
            }):
                assert sector_eastmoney_proxy.get() is None
                with pytest.raises(requests.RequestException):
                    requests.get("https://push2.eastmoney.com/api/qt/stock/get",
                                 timeout=6)
    finally:
        listener.stop()
    assert listener.attempts == 1, listener.attempts  # wire 直连发生且被观测
    assert _FakeForwardProxy.received == []  # scoped 路由未被注入到无关消费者


def test_nested_context_and_executor_reuse_no_leakage(loopback: _LoopbackFixture) -> None:
    """嵌套 bind 恢复外层值；执行器复用与异常/超时退出后上下文归零。"""
    with _proxy_config(loopback.proxy_addr):
        outer_token = sector_eastmoney_proxy.set("http://outer.example:1")
        try:
            assert svc._http_get_json(
                "http://7.push2his.eastmoney.com/api/qt/stock/kline/get",
                {}, budget=_budget(5.0)) == {"ok": True}
            assert sector_eastmoney_proxy.get() == "http://outer.example:1"
        finally:
            sector_eastmoney_proxy.reset(outer_token)
        assert sector_eastmoney_proxy.get() is None

        def failing() -> Any:
            raise requests.ConnectionError("synthetic supplier failure")

        import akshare as ak
        with pytest.raises(requests.ConnectionError):
            with patch.object(ak, "stock_board_industry_name_em", failing):
                svc._bounded_ak(ak.stock_board_industry_name_em, budget=_budget(5.0))
        worker_state = svc._FETCH_EXECUTOR.submit(
            lambda: (sector_eastmoney_proxy.get(),)).result(5)
        assert worker_state == (None,), worker_state


# ---------------------------------------------------------------------------
# 11) 配置生效的进程级投影（Config 单例）
# ---------------------------------------------------------------------------


def test_config_singleton_exposes_validated_proxy(loopback: _LoopbackFixture) -> None:
    with _proxy_config(loopback.proxy_addr):
        config = Config.get_instance()
        assert config.sector_eastmoney_proxy_url == loopback.proxy_addr
        assert svc._current_sector_em_proxy_url() == loopback.proxy_addr
    assert Config.get_instance().sector_eastmoney_proxy_url is None
    assert svc._current_sector_em_proxy_url() is None
