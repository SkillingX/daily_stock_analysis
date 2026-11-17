# -*- coding: utf-8 -*-
"""manager 聚合测试共用的网络守卫实现（第五十二轮提取）。

单一来源供 test_ifind_http_financial_manager 与 pytester 隔离验证
（test_ifind_http_guard_isolation 经源码注入子 conftest）复用——嵌套
pytest 失败即可证明**实际使用的守卫** teardown 生效，非手写副本。
仅隔离外部依赖（外部 connect 记录+抛 OSError 阻断、loopback 委托真实
connect、Popen 记录程序名），不替身被测财务快照链路。
"""
from __future__ import annotations

import ipaddress
import socket
import subprocess
from typing import Any, Callable, Iterator, List, Tuple

import pytest


def _is_loopback(address: Any) -> bool:
    """精确 loopback 判定：仅 ``localhost`` 与 ``ip_address(host).is_loopback``
    ——伪前缀（如 "127.example.com"）不匹配、不触发 DNS/连接（第五十二轮）。"""
    host = address[0] if isinstance(address, tuple) else str(address)
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def guarded_connect_factory(
    leaks: List[str],
) -> Callable[[socket.socket, Any], Any]:
    original_connect = socket.socket.connect

    def guarded_connect(self: socket.socket, address: Any):
        if _is_loopback(address):
            return original_connect(self, address)  # loopback 委托真实 connect
        leaks.append(f"connect:{address}")
        # 记录后抛 OSError 阻断：不伪装连接成功；即使生产吞掉异常，
        # teardown 非空断言仍使测试失败。
        raise OSError("external network disabled in test")

    return guarded_connect


def guarded_create_connection_factory(
    leaks: List[str],
) -> Callable[..., socket.socket]:
    original_create_connection = socket.create_connection

    def guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> socket.socket:
        if _is_loopback(address):
            return original_create_connection(address, *args, **kwargs)
        leaks.append(f"create_connection:{address}")
        raise OSError("external network disabled in test")

    return guarded_create_connection


def guarded_popen_factory(leaks: List[str]) -> Callable[..., Any]:
    def guarded_popen(args: Any, *p_args: Any, **p_kwargs: Any) -> Any:
        # 只记录程序名（argv[0]），不泄露参数（可能含凭据/路径）。
        prog = args[0] if isinstance(args, (list, tuple)) and args else str(args)
        leaks.append(f"subprocess:{prog}"[:200])
        raise OSError("subprocess disabled in manager tests")

    return guarded_popen


@pytest.fixture(autouse=True)
def network_guard(monkeypatch: pytest.MonkeyPatch) -> Iterator[List[str]]:
    """外部连接**记录后抛 OSError 阻断**（不伪装连接成功——即使生产吞掉
    异常，teardown 非空断言仍使测试失败，第五十一轮）；loopback 委托真实
    connect（受控本地服务可用且被 server accept 证明）；Popen 只记录程序
    名（不泄露参数）。"""
    import socket

    leaks: List[str] = []
    original_connect = socket.socket.connect
    original_create_connection = socket.create_connection
    original_popen = subprocess.Popen
    socket.socket.connect = guarded_connect_factory(leaks)  # type: ignore[method-assign]
    socket.create_connection = guarded_create_connection_factory(leaks)  # type: ignore[assignment]
    subprocess.Popen = guarded_popen_factory(leaks)  # type: ignore[assignment]
    yield leaks

    # 显式还原受 patch 入口（第五十二轮）：teardown assert 失败不得阻断还
    # 原链——防 subprocess.Popen 等残留泄漏到后续测试（顺序污染实测）。
    socket.socket.connect = original_connect
    socket.create_connection = original_create_connection
    subprocess.Popen = original_popen
    assert not leaks, f"unexpected external network access: {leaks}"
