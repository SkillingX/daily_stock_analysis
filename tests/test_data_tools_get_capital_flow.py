# -*- coding: utf-8 -*-
"""
Contract tests for get_capital_flow tool output semantics.
"""

import os
import sys
import unittest
from dataclasses import asdict
from data_provider.cross_source_validator import AnchorReading, CrossSourceValidator
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.agent.tools.data_tools import (
    _backfill_capital_flow,
    _handle_get_capital_flow,
)


class _DummyManagerOk:
    """Returns a well-formed capital flow context."""

    def get_capital_flow_context(self, _stock_code: str):
        return {
            "status": "ok",
            "data": {
                "stock_flow": {
                    "main_net_inflow": 1500000.0,
                    "inflow_5d": 8000000.0,
                    "inflow_10d": 15000000.0,
                },
                "sector_rankings": {
                    "top": [{"name": "白酒", "inflow": 5e8}, {"name": "半导体", "inflow": 3e8}],
                    "bottom": [{"name": "煤炭", "inflow": -2e8}],
                },
            },
            "errors": [],
        }


class _DummyManagerNotSupported:
    """Returns not_supported status (e.g. ETF or HK stock)."""

    def get_capital_flow_context(self, _stock_code: str):
        return {"status": "not_supported"}


class _DummyManagerRaises:
    """Simulates a fetch failure."""

    def get_capital_flow_context(self, _stock_code: str):
        raise RuntimeError("network timeout")


class _DummyManagerFailed:
    """Returns failed status with all-Nones (akshare push2his 不可达场景)."""

    def get_capital_flow_context(self, _stock_code: str):
        return {
            "status": "failed",
            "data": {
                "stock_flow": {
                    "main_net_inflow": None,
                    "inflow_5d": None,
                    "inflow_10d": None,
                },
                "sector_rankings": {"top": [], "bottom": []},
            },
            "errors": ["push2his.eastmoney.com unreachable"],
        }


class TestGetCapitalFlowContract(unittest.TestCase):

    def test_ok_response_shape(self) -> None:
        """Happy path: key fields are present and values match the source data."""
        with patch(
            "src.agent.tools.data_tools._get_fetcher_manager",
            return_value=_DummyManagerOk(),
        ):
            result = _handle_get_capital_flow("600519")

        self.assertEqual(result["stock_code"], "600519")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["main_net_inflow"], 1500000.0)
        self.assertEqual(result["inflow_5d"], 8000000.0)
        self.assertEqual(result["inflow_10d"], 15000000.0)
        self.assertIn("sector_rankings", result)
        self.assertIn("top_inflow_sectors", result["sector_rankings"])
        self.assertIn("top_outflow_sectors", result["sector_rankings"])
        # At most 3 items are returned per ranking list
        self.assertLessEqual(len(result["sector_rankings"]["top_inflow_sectors"]), 3)
        self.assertEqual(result["errors"], [])

    def test_not_supported_for_non_cn_or_etf(self) -> None:
        """ETF / non-CN stocks return status=not_supported with an explanatory note."""
        with patch(
            "src.agent.tools.data_tools._get_fetcher_manager",
            return_value=_DummyManagerNotSupported(),
        ):
            result = _handle_get_capital_flow("510300")

        self.assertEqual(result["stock_code"], "510300")
        self.assertEqual(result["status"], "not_supported")
        self.assertIn("note", result)

    def test_exception_path_formatting(self) -> None:
        """Fetch errors are caught and returned with status=error."""
        with patch(
            "src.agent.tools.data_tools._get_fetcher_manager",
            return_value=_DummyManagerRaises(),
        ):
            result = _handle_get_capital_flow("600519")

        self.assertEqual(result["stock_code"], "600519")
        self.assertEqual(result["status"], "error")
        self.assertIn("capital flow fetch failed", result["error"])
        self.assertIn("network timeout", result["error"])


class TestBackfillCapitalFlow(unittest.TestCase):
    """_backfill_capital_flow：纯函数，多源回填 + 不覆盖 + status 提升 + 来源标注。"""

    def _failed_result(self):
        return {
            "stock_code": "688486",
            "status": "failed",
            "main_net_inflow": None,
            "inflow_5d": None,
            "inflow_10d": None,
            "note": "资金流数据源暂不可用",
            "errors": ["push2his unreachable"],
        }

    def _cv(self, main_inflow_value=2.5e8):
        return {"enabled": True, "anchors": {"main_inflow": CrossSourceValidator([]).verify("688486", "main_inflow", primary_reading=AnchorReading("mx", main_inflow_value, unit="currency_base", currency="CNY")).to_compact()}}

    def _cum(self):
        return {
            "main_net_inflow": 3.0e8,
            "field_meta": {"main_inflow": asdict(AnchorReading("ifind", 3.0e8, unit="currency_base", currency="CNY"))},
            "inflow_5d": 13.0,
            "inflow_10d": 19.0,
            "daily_series": [{"date": "20260625", "value": 3.0e8}],
            "source": "ifind",
        }

    def test_no_source_unchanged_new_dict(self):
        base = self._failed_result()
        out = _backfill_capital_flow(base, None, None)
        self.assertEqual(out["status"], "failed")  # 未提升
        self.assertIsNone(out["main_net_inflow"])
        self.assertIn("note", out)  # failed note 保留
        self.assertNotIn("capital_flow_fallback", out)
        self.assertIsNot(out, base)  # 返回新 dict（不可变）

    def test_cumulative_only_backfills_all(self):
        out = _backfill_capital_flow(self._failed_result(), None, self._cum())
        self.assertEqual(out["status"], "partial")  # failed → partial
        self.assertNotIn("note", out)  # failed note 移除（已不准确）
        self.assertEqual(out["main_net_inflow"], 3.0e8)  # iFinD 单日
        self.assertEqual(out["inflow_5d"], 13.0)
        self.assertEqual(out["inflow_10d"], 19.0)
        fb = out["capital_flow_fallback"]
        self.assertEqual(fb["source"], "ifind")
        self.assertEqual(len(fb["daily_series"]), 1)

    def test_cv_preferred_over_cumulative_for_main(self):
        # main_net_inflow 优先双源 CV（mx+ifind）；5d/10d 仍来自 iFinD 累计
        out = _backfill_capital_flow(self._failed_result(), self._cv(9.9e8), self._cum())
        self.assertEqual(out["main_net_inflow"], 9.9e8)  # CV 优先
        self.assertEqual(out["capital_flow_fallback"]["source"], "mx")
        self.assertEqual(out["inflow_5d"], 13.0)
        self.assertEqual(out["inflow_10d"], 19.0)

    def test_existing_values_not_overwritten(self):
        base = {
            "status": "ok",
            "main_net_inflow": 1.0,
            "inflow_5d": 2.0,
            "inflow_10d": 3.0,
        }
        out = _backfill_capital_flow(base, self._cv(), self._cum())
        self.assertEqual(out["status"], "ok")  # 已 ok，不提升
        self.assertEqual(out["main_net_inflow"], 1.0)  # 不覆盖
        self.assertEqual(out["inflow_5d"], 2.0)
        self.assertNotIn("capital_flow_fallback", out)  # 无回填，无标记

    def test_partial_source_cv_only(self):
        # 仅 CV（无累计）：回填 main_net_inflow，5d/10d 仍 None
        out = _backfill_capital_flow(self._failed_result(), self._cv(), {})
        self.assertEqual(out["main_net_inflow"], 2.5e8)
        self.assertIsNone(out["inflow_5d"])
        self.assertEqual(out["capital_flow_fallback"]["source"], "mx")
        self.assertEqual(out["capital_flow_fallback"]["daily_series"], [])

    def test_cv_anchor_none_falls_back_to_cumulative(self):
        # CV 锚点值为 None（双源失败）→ main_net_inflow 回退 iFinD 单日
        cv = {"enabled": True, "anchors": {"main_inflow": {"v": None}}}
        out = _backfill_capital_flow(self._failed_result(), cv, self._cum())
        self.assertEqual(out["main_net_inflow"], 3.0e8)
        self.assertEqual(out["capital_flow_fallback"]["source"], "ifind")


class TestGetCapitalFlowBackfillE2E(unittest.TestCase):
    """_handle_get_capital_flow 端到端：failed → CV+iFinD 回填（开关开/关）。"""

    @staticmethod
    def _fake_cv(main_value=2.5e8):
        return {
            "enabled": True,
            "anchors": {
                "main_inflow": CrossSourceValidator([]).verify("688486", "main_inflow", primary_reading=AnchorReading("mx", main_value, unit="currency_base", currency="CNY")).to_compact(),
                "margin_balance": {"v": 1.2e9, "conf": "high"},
            },
            "summary": "1/2 锚点双源验证通过",
        }

    def test_failed_backfilled_when_cv_on(self):
        captured = {}

        def fake_cum(code):
            captured["cum_code"] = code
            return {
                "main_net_inflow": 3.0e8,
                "inflow_5d": 13.0,
                "inflow_10d": 19.0,
                "daily_series": [{"date": "20260625", "value": 3.0e8}],
                "source": "ifind",
            }

        with patch(
            "src.agent.tools.data_tools._get_fetcher_manager",
            return_value=_DummyManagerFailed(),
        ), patch(
            "src.agent.tools.data_tools.build_cross_validation_block",
            return_value=self._fake_cv(),
        ), patch(
            "src.agent.tools.data_tools.get_main_inflow_cumulative",
            side_effect=fake_cum,
        ):
            result = _handle_get_capital_flow("688486")

        self.assertEqual(captured["cum_code"], "688486")
        # failed → partial（已回填），failed note 移除
        self.assertEqual(result["status"], "partial")
        self.assertNotIn("note", result)
        # CV 双源优先于 iFinD 单日
        self.assertEqual(result["main_net_inflow"], 2.5e8)
        self.assertEqual(result["inflow_5d"], 13.0)
        self.assertEqual(result["inflow_10d"], 19.0)
        # CV 块 + 来源标注
        self.assertTrue(result["cross_validation"]["enabled"])
        fb = result["capital_flow_fallback"]
        self.assertEqual(fb["source"], "mx")
        self.assertEqual(len(fb["daily_series"]), 1)

    def test_no_backfill_when_cv_off_zero_regression(self):
        # 开关关 → _cv None：不回填、无 cross_validation、无 fallback（零回归）
        with patch(
            "src.agent.tools.data_tools._get_fetcher_manager",
            return_value=_DummyManagerFailed(),
        ), patch(
            "src.agent.tools.data_tools.build_cross_validation_block",
            return_value=None,
        ), patch(
            "src.agent.tools.data_tools.get_main_inflow_cumulative"
        ) as m_cum:
            result = _handle_get_capital_flow("688486")

        m_cum.assert_not_called()  # 开关关，iFinD 不触发
        self.assertEqual(result["status"], "failed")  # 原样
        self.assertIsNone(result["main_net_inflow"])
        self.assertNotIn("cross_validation", result)
        self.assertNotIn("capital_flow_fallback", result)
        self.assertIn("note", result)  # failed note 保留

    def test_ok_not_overwritten_when_cv_on(self):
        # akshare 成功（status ok，字段齐全）→ CV 开也不覆盖、不加 fallback
        with patch(
            "src.agent.tools.data_tools._get_fetcher_manager",
            return_value=_DummyManagerOk(),
        ), patch(
            "src.agent.tools.data_tools.build_cross_validation_block",
            return_value=self._fake_cv(),
        ), patch(
            "src.agent.tools.data_tools.get_main_inflow_cumulative",
            return_value={"main_net_inflow": 9.9, "inflow_5d": 9.9, "inflow_10d": 9.9},
        ):
            result = _handle_get_capital_flow("600519")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["main_net_inflow"], 1500000.0)  # akshare 原值
        self.assertEqual(result["inflow_5d"], 8000000.0)
        self.assertNotIn("capital_flow_fallback", result)  # 无回填


if __name__ == "__main__":
    unittest.main()
