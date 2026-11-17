# -*- coding: utf-8 -*-
"""第五十一轮：守卫 teardown 真失败的独立子 pytest 验证（不虚构）。

独立文件承载 pytester（模块级 pytest_plugins 合法；manager 文件的 autouse
网络守卫不作用于本文件——子进程 pytest 由本层承载）。零外部出站：泄漏子
测试的外部 connect 在子守卫 connect 前拦截（OSError），不触达真实网络。
"""
from __future__ import annotations

import pytest

pytest_plugins = ["pytester"]


LEAKY_TEST = """
import socket
import pytest


def test_leaky_external_connect(network_guard):
    try:
        sock = socket.socket()
        sock.connect(("198.18.0.1", 443))  # 生产吞掉 OSError（业务断言通过）
        sock.close()
    except OSError:
        pass
    assert True  # 业务断言通过——teardown 仍须使本测试失败


"""


LOOPBACK_TEST = """
import socket
import threading


def test_loopback_service_works(network_guard):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    accepted: list = []

    def accept_once():
        conn, _ = srv.accept()
        accepted.append(conn)

    t = threading.Thread(target=accept_once, daemon=True)
    t.start()
    client = socket.create_connection(("127.0.0.1", port), timeout=2)
    t.join(timeout=2)
    client.close()
    srv.close()
    if accepted:
        accepted[0].close()
    assert accepted, "loopback real connect failed"
    assert network_guard == []
"""

# 第五十二轮：conftest 直接**注入共享实现源码**（tests/_network_guard.py）——
# 嵌套 pytest 失败即证明实际使用的守卫 fixture teardown 生效，非手写副本。
import pathlib as _pathlib
import tests._network_guard as _shared_guard

SHARED_GUARD_SOURCE = _pathlib.Path(_shared_guard.__file__).read_text(encoding="utf-8")
GUARD_CONFTEST = SHARED_GUARD_SOURCE


def test_guard_teardown_fails_for_swallowed_leak(pytester):
    """泄漏测试（生产吞异常形态）在隔离运行中必然失败——teardown 非空
    断言真实生效，非清空后声称。"""
    pytester.makeconftest(GUARD_CONFTEST)
    pytester.makepyfile(LEAKY_TEST)
    result = pytester.runpytest("-q")
    # teardown 异常计为 errors（业务断言 passed=1 但测试未绿）——生产吞异常
    # 也无法通过的直接证据。
    result.assert_outcomes(passed=1, errors=1)  # 泄漏测试 teardown error
    result.stdout.fnmatch_lines(["*unexpected external network access*"])


def test_guard_loopback_passes_in_isolation(pytester):
    """受控 loopback 合法连接在守卫下通过（不阻止受控 loopback 测试）。"""
    pytester.makeconftest(GUARD_CONFTEST)
    pytester.makepyfile(LOOPBACK_TEST)
    result = pytester.runpytest("-q", "--tb=no")
    result.assert_outcomes(passed=1)


def test_pseudo_prefix_host_not_loopback(pytester):
    """伪前缀 "127.example.com" 不判定 loopback：触发 DNS 前被守卫记录+阻断
    （OSError），泄漏计入 teardown——非 loopback 误放行。"""
    pytester.makeconftest(GUARD_CONFTEST)
    pytester.makepyfile("""
import socket
import pytest


def test_pseudo_prefix_connect(network_guard):
    sock = socket.socket()
    with pytest.raises(OSError, match="external network disabled"):
        sock.connect(("127.example.com", 443))
    sock.close()
    assert any("127.example.com" in entry for entry in network_guard)
""")
    result = pytester.runpytest("-q")
    # 伪前缀连接被阻断（OSError 按测试契约处理）→ 测试通过；
    # 泄漏记录计入 teardown error（非 loopback 不误放行）
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*127.example.com*"])
