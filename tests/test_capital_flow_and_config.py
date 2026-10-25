# -*- coding: utf-8 -*-
"""fundamental 超时放宽 + capital_flow 失败提示单元测试。

覆盖本轮两项修复：
1. config 放宽 fundamental 超时默认值（fetch 3→10s, stage 8→25s）。
2. _handle_get_capital_flow 在 status=failed 时加 note（诚实降级，避免 LLM「数据缺失」）。

mock fetcher manager，不依赖网络。
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest


class TestFundamentalTimeoutDefaults:
    """config fundamental 超时默认值放宽。"""

    def test_fetch_timeout_default_widened(self):
        from src.config import FUNDAMENTAL_FETCH_TIMEOUT_SECONDS_DEFAULT

        assert FUNDAMENTAL_FETCH_TIMEOUT_SECONDS_DEFAULT == 10.0

    def test_stage_timeout_default_widened(self):
        from src.config import FUNDAMENTAL_STAGE_TIMEOUT_SECONDS_DEFAULT

        assert FUNDAMENTAL_STAGE_TIMEOUT_SECONDS_DEFAULT == 25.0

    def test_config_object_uses_widened_defaults(self):
        """未设环境变量时，get_config() 返回放宽后的默认值。"""
        from src.config import get_config

        cfg = get_config()
        assert cfg.fundamental_fetch_timeout_seconds == 10.0
        assert cfg.fundamental_stage_timeout_seconds == 25.0


class TestCapitalFlowFailureNote:
    """_handle_get_capital_flow 失败提示。"""

    @staticmethod
    def _manager(status: str, stock_flow: dict = None, errors=None):
        mgr = SimpleNamespace()
        mgr.get_capital_flow_context = lambda code: {
            "status": status,
            "data": {"stock_flow": stock_flow or {}, "sector_rankings": {}},
            "errors": errors or [],
        }
        return mgr

    def test_failed_status_adds_note(self):
        """status=failed → 返回 note，明确资金流源暂不可用。"""
        from src.agent.tools.data_tools import _handle_get_capital_flow

        with patch(
            "src.agent.tools.data_tools._get_fetcher_manager",
            return_value=self._manager("failed"),
        ):
            r = _handle_get_capital_flow("600176")
        assert r["status"] == "failed"
        assert "note" in r
        assert "暂不可用" in r["note"]
        assert r["main_net_inflow"] is None

    def test_ok_status_no_failure_note(self):
        """status=ok → 有数据，不加失败 note。"""
        from src.agent.tools.data_tools import _handle_get_capital_flow

        from src.config import get_config
        with patch(
            "src.agent.tools.data_tools._get_fetcher_manager",
            return_value=self._manager(
                "ok", stock_flow={"main_net_inflow": 1.2e8, "inflow_5d": 3e8, "inflow_10d": 5e8}
            ),
        ), patch.object(get_config(), "data_source_profile", "legacy"):
            r = _handle_get_capital_flow("600176")
        assert r["status"] == "ok"
        assert "note" not in r  # 成功不加分流提示
        assert r["main_net_inflow"] == pytest.approx(1.2e8)

    def test_not_supported_returns_a_share_note(self):
        """status=not_supported → 提前返回 A 股限制提示（已有逻辑，不受影响）。"""
        from src.agent.tools.data_tools import _handle_get_capital_flow

        with patch(
            "src.agent.tools.data_tools._get_fetcher_manager",
            return_value=self._manager("not_supported"),
        ):
            r = _handle_get_capital_flow("600176")
        assert r["status"] == "not_supported"
        assert "A-share" in r["note"] or "A股" in r["note"]

    def test_context_exception_returns_error(self):
        """get_capital_flow_context 抛异常 → 返回 status=error，不抛。"""
        from src.agent.tools.data_tools import _handle_get_capital_flow

        mgr = SimpleNamespace()

        def _raise(code):
            raise RuntimeError("net down")

        mgr.get_capital_flow_context = _raise
        with patch(
            "src.agent.tools.data_tools._get_fetcher_manager",
            return_value=mgr,
        ):
            r = _handle_get_capital_flow("600176")
        assert r["status"] == "error"
        assert "capital flow fetch failed" in r["error"]
