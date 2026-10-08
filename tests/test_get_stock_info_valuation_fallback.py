# -*- coding: utf-8 -*-
"""get_stock_info 估值 fail-open 兜底单元测试（_fallback_valuation_from_quote）。

覆盖 ``_handle_get_stock_info`` 在 fundamental valuation 缺失时用 get_realtime_quote
兜底补 pe/pb/市值的全部分支，mock fetcher manager，不依赖网络。

100% 覆盖 ``_fallback_valuation_from_quote`` 的 4 条路径：
- valuation 已有数据 → 不兜底（_handle_get_stock_info 不调用 get_realtime_quote）
- valuation 缺失 + quote 可用 → 补全缺失字段
- valuation 缺失 + quote 抛异常 → 保持原值，不抛
- valuation 缺失 + quote 为 None → 保持原值
- fundamental 已有 pb 但缺 pe → 只补 pe，保留 pb（不覆盖）
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.agent.tools.data_tools import (
    _fallback_valuation_from_quote,
    _handle_get_stock_info,
)


def _quote(**kw):
    """构造轻量 realtime quote（字段对齐 UnifiedRealtimeQuote 用法）。"""
    base = {"source": "ifind", "currency": "CNY", "field_meta": {"pe_ratio": {"caliber": "TTM"}, "pb_ratio": {"caliber": "MRQ"}}, "pe_ratio": 67.89, "pb_ratio": 8.23, "total_mv": 2.5948e11, "circ_mv": 2.5948e11}
    base.update(kw)
    return SimpleNamespace(**base)


def _mock_manager(*, fund_valuation, quote, boards=None, name="中国巨石", quote_exc=None):
    """构造 mock fetcher manager，控制 fundamental valuation 与 realtime quote 返回。"""
    mgr = SimpleNamespace()
    # fundamental_context → compact 后 valuation.data = fund_valuation
    mgr.get_fundamental_context = lambda code: {
        "market": "cn",
        "valuation": {"status": "failed" if not fund_valuation else "available", "data": fund_valuation},
    }
    mgr.build_failed_fundamental_context = lambda code, reason: {"market": "cn", "valuation": {"data": {}}}
    if quote_exc is not None:
        mgr.get_realtime_quote = lambda code: (_ for _ in ()).throw(quote_exc)
    else:
        mgr.get_realtime_quote = lambda code: quote
    mgr.get_belong_boards = lambda code: boards or []
    mgr.get_stock_name = lambda code: name
    return mgr


class TestFallbackValuationFromQuote:
    """_fallback_valuation_from_quote 四条路径 + 不覆盖语义。"""

    def test_fills_missing_fields_from_quote(self):
        mgr = _mock_manager(fund_valuation={}, quote=_quote())
        result = _fallback_valuation_from_quote(mgr, "600176", {})
        assert result["pe_ratio"] == 67.89
        assert result["pb_ratio"] == 8.23
        assert result["total_mv"] == pytest.approx(2.5948e11)
        assert result["circ_mv"] == pytest.approx(2.5948e11)

    def test_keeps_existing_non_empty_fields(self):
        """fundamental 已有 pb（非空）→ 只补缺失的 pe/市值，不覆盖 pb。"""
        mgr = _mock_manager(fund_valuation={}, quote=_quote())
        result = _fallback_valuation_from_quote(mgr, "600176", {"pb_ratio": 9.99})
        assert result["pb_ratio"] == 9.99  # 保留 fundamental 已有值
        assert result["pe_ratio"] == 67.89  # 补缺失
        assert result["total_mv"] == pytest.approx(2.5948e11)

    def test_exception_returns_original_valuation(self):
        """get_realtime_quote 抛异常 → 返回原 valuation，不抛。"""
        mgr = _mock_manager(fund_valuation={}, quote=None, quote_exc=RuntimeError("net down"))
        original = {"pe_ratio": None}
        result = _fallback_valuation_from_quote(mgr, "600176", original)
        assert result is original

    def test_none_quote_returns_original_valuation(self):
        mgr = _mock_manager(fund_valuation={}, quote=None)
        original = {}
        result = _fallback_valuation_from_quote(mgr, "600176", original)
        assert result is original

    def test_partial_quote_only_fills_available(self):
        """quote 只含 pe（其余 None）→ 只补 pe，其余保持原状。"""
        mgr = _mock_manager(fund_valuation={}, quote=_quote(pb_ratio=None, total_mv=None, circ_mv=None))
        result = _fallback_valuation_from_quote(mgr, "600176", {})
        assert result["pe_ratio"] == 67.89
        assert result.get("pb_ratio") is None


class TestHandleGetStockInfoFallback:
    """_handle_get_stock_info 端到端兜底行为。"""

    def test_skips_fallback_when_valuation_present(self):
        """fundamental valuation 有 pe → 不调 get_realtime_quote，用 fundamental 值。"""
        mgr = _mock_manager(
            fund_valuation={"pe_ratio": 30.0, "pb_ratio": 5.0, "total_mv": 1e11, "circ_mv": 1e11},
            quote=_quote(),  # 若误调会覆盖，用于断言未调用
        )
        with patch("src.agent.tools.data_tools._get_fetcher_manager", return_value=mgr):
            mgr.get_realtime_quote = lambda code: pytest.fail("不应调用 get_realtime_quote")
            r = _handle_get_stock_info("600176")
        assert r["pe_ratio"] == 30.0  # 用 fundamental 的，非兜底
        assert r["pb_ratio"] == 5.0

    def test_triggers_fallback_when_pe_missing(self):
        """fundamental valuation 缺 pe → 兜底 get_realtime_quote 补全。"""
        mgr = _mock_manager(fund_valuation={"pe_ratio": None}, quote=_quote())
        with patch("src.agent.tools.data_tools._get_fetcher_manager", return_value=mgr):
            r = _handle_get_stock_info("600176")
        assert r["pe_ratio"] == 67.89
        assert r["pb_ratio"] == 8.23
        assert r["total_mv"] == pytest.approx(2.5948e11)

    def test_fallback_failure_keeps_none(self):
        """兜底也失败 → pe/pb/市值保持 None，不抛异常，其余字段正常。"""
        mgr = _mock_manager(fund_valuation={}, quote=None, quote_exc=RuntimeError("down"))
        with patch("src.agent.tools.data_tools._get_fetcher_manager", return_value=mgr):
            r = _handle_get_stock_info("600176")
        assert r["pe_ratio"] is None
        assert r["name"] == "中国巨石"  # 其他字段不受影响
        assert r["code"] == "600176"
