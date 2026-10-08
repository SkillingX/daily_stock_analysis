# -*- coding: utf-8 -*-
"""
Contract tests for get_stock_info tool output semantics.

Covers:
- board semantics (belong_boards / boards alias / sector_rankings)
- growth-block cross-validation backfill (akshare 失败兜底)
- _latest_annual_period / _backfill_growth_from_validation pure helpers
"""

import os
import sys
import unittest
from datetime import date
from typing import Any
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.agent.tools.data_tools import (  # noqa: E402
    _backfill_growth_from_validation,
    _handle_get_stock_info,
    _latest_annual_period,
)


def _cv_anchor(field: str, value: float) -> dict[str, Any]:
    from data_provider.cross_source_validator import AnchorReading, CrossSourceValidator

    calibers = {"roe": "weighted_roe", "gross_margin": "gross_margin", "revenue_yoy": "operating_revenue_yoy"}
    return CrossSourceValidator([]).verify(
        "600519", field, primary_reading=AnchorReading(
            "mx", value, caliber=calibers[field], unit="percentage_point",
            period="2025-12-31", period_basis="annual",
        ),
    ).to_compact()


class _DummyManager:
    def __init__(self):
        self._context = {
            "market": "cn",
            "status": "partial",
            "coverage": {
                "valuation": "ok",
                "growth": "not_supported",
                "earnings": "not_supported",
                "institution": "not_supported",
                "capital_flow": "not_supported",
                "dragon_tiger": "not_supported",
                "boards": "ok",
            },
            "valuation": {
                "status": "ok",
                "data": {
                    "pe_ratio": 12.3,
                    "pb_ratio": 2.1,
                    "total_mv": 1.0e11,
                    "circ_mv": 7.0e10,
                },
            },
            "growth": {"status": "not_supported", "data": {}},
            "earnings": {"status": "not_supported", "data": {}},
            "institution": {"status": "not_supported", "data": {}},
            "capital_flow": {"status": "not_supported", "data": {}},
            "dragon_tiger": {"status": "not_supported", "data": {}},
            "boards": {
                "status": "ok",
                "data": {
                    "top": [{"name": "白酒", "change_pct": 2.3}],
                    "bottom": [{"name": "煤炭", "change_pct": -1.7}],
                },
            },
        }
        self._belong_boards = [{"name": "白酒"}, {"name": "消费"}]

    def get_fundamental_context(self, _stock_code: str):
        return self._context

    def build_failed_fundamental_context(self, _stock_code: str, _reason: str):
        return {}

    def get_belong_boards(self, _stock_code: str):
        return self._belong_boards

    def get_stock_name(self, _stock_code: str):
        return "贵州茅台"


class TestGetStockInfoContract(unittest.TestCase):
    def test_get_stock_info_preserves_board_semantics(self) -> None:
        manager = _DummyManager()
        # 隔离交叉验证（开关可能因本地 .env 开启而走真实网络）：此处只验证 board 语义
        with patch("src.agent.tools.data_tools._get_fetcher_manager", return_value=manager), \
             patch("src.agent.tools.data_tools.build_cross_validation_block", return_value=None):
            result = _handle_get_stock_info("600519")

        self.assertEqual(result["name"], "贵州茅台")
        self.assertEqual(result["code"], "600519")
        self.assertEqual(result["pe_ratio"], 12.3)
        self.assertEqual(result["pb_ratio"], 2.1)

        # Contract: boards is compatibility alias of belong_boards.
        self.assertEqual(result["belong_boards"], manager._belong_boards)
        self.assertEqual(result["boards"], result["belong_boards"])

        # Contract: sector_rankings comes from fundamental_context.boards.data.
        self.assertEqual(result["sector_rankings"], manager._context["boards"]["data"])
        self.assertEqual(
            result["fundamental_context"]["boards"]["data"],
            result["sector_rankings"],
        )
        # 开关关 → 无 cross_validation 字段，growth 不回填
        self.assertNotIn("cross_validation", result)


class TestLatestAnnualPeriod(unittest.TestCase):
    def test_after_april_uses_prev_year(self):
        # 2026-06：2025年报已披露（截止 2026-04-30）
        self.assertEqual(_latest_annual_period(date(2026, 6, 25)), "2025年报")

    def test_before_may_uses_two_years_ago(self):
        # 2026-02：2025年报尚未披露（截止 4-30）→ 最新是 2024年报
        self.assertEqual(_latest_annual_period(date(2026, 2, 15)), "2024年报")

    def test_injectable_today(self):
        self.assertEqual(_latest_annual_period(date(2025, 12, 1)), "2024年报")
        self.assertEqual(_latest_annual_period(date(2025, 4, 29)), "2023年报")

    def test_default_today_returns_annual(self):
        period = _latest_annual_period()
        self.assertTrue(period.endswith("年报"))


class TestBackfillGrowthFromValidation(unittest.TestCase):
    def test_fills_none_from_cv_and_promotes_status(self):
        growth = {"status": "not_supported", "data": {
            "revenue_yoy": None, "gross_margin": None, "roe": None, "net_profit_yoy": None}}
        cv = {"anchors": {
            "gross_margin": _cv_anchor("gross_margin", 52.3),
            "revenue_yoy": _cv_anchor("revenue_yoy", 18.5), "roe": _cv_anchor("roe", 10.2)}}
        out = _backfill_growth_from_validation(growth, cv)
        self.assertAlmostEqual(out["data"]["gross_margin"], 52.3)
        self.assertAlmostEqual(out["data"]["revenue_yoy"], 18.5)
        self.assertAlmostEqual(out["data"]["roe"], 10.2)
        self.assertIsNone(out["data"]["net_profit_yoy"])  # 无对应 CV 锚点
        self.assertEqual(out["status"], "partial")  # 原 data 全 None → 提升到 partial

    def test_does_not_overwrite_existing_values(self):
        growth = {"status": "ok", "data": {"gross_margin": 50.0, "revenue_yoy": None}}
        cv = {"anchors": {"gross_margin": _cv_anchor("gross_margin", 99.0), "revenue_yoy": _cv_anchor("revenue_yoy", 18.5)}}
        out = _backfill_growth_from_validation(growth, cv)
        self.assertEqual(out["data"]["gross_margin"], 50.0)  # 已有值不覆盖
        self.assertAlmostEqual(out["data"]["revenue_yoy"], 18.5)
        self.assertEqual(out["status"], "ok")  # 原 data 非空 → 不改 status

    def test_no_cv_block_returns_copy(self):
        growth = {"status": "not_supported", "data": {}}
        out = _backfill_growth_from_validation(growth, None)
        self.assertEqual(out["data"], {})
        self.assertEqual(out["status"], "not_supported")

    def test_skips_when_cv_value_none(self):
        # CV 锚点存在但 v=None（缺失）→ 不回填、不改 status
        growth = {"status": "not_supported", "data": {"gross_margin": None}}
        cv = {"anchors": {"gross_margin": {"v": None}}}
        out = _backfill_growth_from_validation(growth, cv)
        self.assertIsNone(out["data"]["gross_margin"])
        self.assertEqual(out["status"], "not_supported")

    def test_none_growth_block(self):
        cv = {"anchors": {"gross_margin": _cv_anchor("gross_margin", 52.3)}}
        out = _backfill_growth_from_validation(None, cv)
        self.assertAlmostEqual(out["data"]["gross_margin"], 52.3)
        self.assertEqual(out["status"], "partial")

    def test_returns_new_dict(self):
        growth = {"status": "ok", "data": {"gross_margin": 50.0}}
        out = _backfill_growth_from_validation(growth, {"anchors": {}})
        self.assertIsNot(out, growth)  # 不可变：返回新对象


class TestGrowthBackfillE2E(unittest.TestCase):
    """_handle_get_stock_info 端到端：CV 开启时 growth 回填 + period/fields 透传。"""

    @staticmethod
    def _fake_cv():
        return {
            "enabled": True,
            "anchors": {
                "gross_margin": _cv_anchor("gross_margin", 52.3),
                "revenue_yoy": _cv_anchor("revenue_yoy", 18.5),
                "roe": _cv_anchor("roe", 10.2),
            },
            "summary": "0/9 锚点双源验证通过",
        }

    def test_growth_backfilled_when_cv_on(self):
        manager = _DummyManager()  # growth = not_supported/空
        captured = {}

        def fake_build(code, fields, period=None, **_kw):
            captured["code"] = code
            captured["fields"] = list(fields)
            captured["period"] = period
            return self._fake_cv()

        with patch("src.agent.tools.data_tools._get_fetcher_manager", return_value=manager), \
             patch("src.agent.tools.data_tools.build_cross_validation_block", side_effect=fake_build):
            result = _handle_get_stock_info("688486")

        # CV 调用：含新增长字段 + 非空 period
        self.assertIn("gross_margin", captured["fields"])
        self.assertIn("revenue_yoy", captured["fields"])
        self.assertTrue(captured["period"] and captured["period"].endswith("年报"))
        # growth 块被回填
        growth = result["fundamental_context"]["growth"]
        self.assertAlmostEqual(growth["data"]["gross_margin"], 52.3)
        self.assertAlmostEqual(growth["data"]["revenue_yoy"], 18.5)
        self.assertEqual(growth["status"], "partial")  # not_supported → partial
        # CV 块注入 response
        self.assertTrue(result["cross_validation"]["enabled"])

    def test_no_backfill_when_cv_off(self):
        manager = _DummyManager()
        with patch("src.agent.tools.data_tools._get_fetcher_manager", return_value=manager), \
             patch("src.agent.tools.data_tools.build_cross_validation_block", return_value=None):
            result = _handle_get_stock_info("688486")
        # 开关关 → 无 CV → growth 原样（空），零回归
        self.assertEqual(result["fundamental_context"]["growth"]["data"], {})
        self.assertNotIn("cross_validation", result)


if __name__ == "__main__":
    unittest.main()
