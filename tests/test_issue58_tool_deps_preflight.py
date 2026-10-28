# -*- coding: utf-8 -*-
"""Issue #58 部署工具依赖预检回归：真实导入链判定，失败必须显式报告。

当前解释器环境是已验证组合（litellm 1.106.4 + mcp 2.3.0）时预检必须通过；
导入失败路径只注入导入机制本身（不 mock 被测 SDK 符号）。旧依赖组合
（mcp 1.x + litellm 1.106.4 的 ``CacheableResult`` 缺口）的端到端拦截由
隔离 venv 升级验证与 deploy 顺序脚本覆盖，不在单测内重建 site-packages。
"""

from __future__ import annotations

import importlib

import pytest

from scripts.check_tool_deps import CHECKS, preflight


def test_current_verified_combination_passes_preflight():
    """本仓库已验证组合下全部导入链可用（真实 import，无桩）。"""
    report = preflight()
    assert report["status"] == "ok", report
    assert all(check["status"] == "ok" for check in report["checks"])  # type: ignore[union-attr]
    versions = report["versions"]
    assert versions["litellm"] and versions["mcp"]  # type: ignore[index]


def test_checks_cover_the_real_tools_import_chain():
    """预检覆盖 litellm 工具路径的懒加载 MCP 导入与当前 MCP transport。"""
    names = {name for name, _, _ in CHECKS}
    assert "litellm_tools_mcp_handler" in names
    assert "litellm_proxy_mcp_gateway" in names
    assert "mcp_streamable_http_transport" in names
    # 每条 (module, attribute) 都真实可导入 —— 防止预检列表与生产导入漂移。
    for _, module, attribute in CHECKS:
        assert getattr(importlib.import_module(module), attribute) is not None


def test_failed_import_is_reported_not_swallowed(monkeypatch: pytest.MonkeyPatch):
    """导入失败（如旧 mcp 缺 CacheableResult）→ 预检明确 failed，不静默放行。"""
    def broken_import(module: str):
        if module.startswith("litellm"):
            raise ImportError("cannot import name 'CacheableResult' from 'mcp.types'")
        return importlib.import_module(module)

    monkeypatch.setattr("scripts.check_tool_deps.import_module", broken_import)
    report = preflight()
    assert report["status"] == "failed"
    failures = [check for check in report["checks"] if check["status"] == "failed"]  # type: ignore[union-attr]
    assert failures and "CacheableResult" in str(failures[0]["error"])


def test_fatal_group_member_is_never_absorbed_by_preflight(monkeypatch: pytest.MonkeyPatch):
    """取消成员不能让预检吸收同组的 SystemExit：致命组保持传播（与运行时同契约）。"""
    import asyncio

    def fatal_import(module: str):
        raise BaseExceptionGroup("import stopped", [asyncio.CancelledError(), SystemExit(9)])

    monkeypatch.setattr("scripts.check_tool_deps.import_module", fatal_import)
    with pytest.raises(BaseExceptionGroup) as excinfo:
        preflight()
    assert excinfo.value.subgroup(SystemExit) is not None
