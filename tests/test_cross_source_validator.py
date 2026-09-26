# -*- coding: utf-8 -*-
"""cross_source_validator 单测 —— 纯逻辑 100% 覆盖。

覆盖：纯函数（容差/量级/差异）、数值判定（口径/报告期/容差）、方向判定
（资金流方向+量级）、边界（单源/无源/未知锚点）、validator 编排（并行/异常隔离）、
to_compact 序列化、锚点规格表完整性。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.cross_source_validator import (  # noqa: E402
    ANCHOR_SPECS,
    MODE_DIRECTION,
    MODE_NUMERIC,
    AnchorReading,
    AnchorVerification,
    CrossSourceValidator,
    _discrepancy_pct,
    _judge_direction,
    _judge_missing,
    _judge_numeric,
    _judge_single,
    _judge_unknown,
    _magnitude_tier,
    _round,
    _within_tolerance,
)
from data_provider.cross_source_validator import AnchorSpec  # noqa: E402


def _reading(source: str, value: float, caliber=None, period=None) -> AnchorReading:
    return AnchorReading(source=source, value=value, caliber=caliber, period=period)


class _FakeSource:
    """可编排的假数据源：按 field 返回预设读数 / None / 抛异常。"""

    def __init__(self, name, table, raise_on=None):
        self.name = name
        self._table = table  # dict[field -> AnchorReading|None]
        self._raise_on = raise_on or set()

    def read(self, code, field, period=None):
        if field in self._raise_on:
            raise RuntimeError(f"{self.name} boom on {field}")
        return self._table.get(field)


class TestPureFunctions(unittest.TestCase):
    def test_round_truncates_precision(self):
        self.assertEqual(_round(3.14159265), 3.1416)
        self.assertEqual(_round(30.0), 30.0)

    def test_discrepancy_pct(self):
        self.assertAlmostEqual(_discrepancy_pct(100, 99), 1.0)
        self.assertAlmostEqual(_discrepancy_pct(30.5, 30.4), 0.3278, places=3)
        self.assertEqual(_discrepancy_pct(0, 0), 0.0)
        self.assertAlmostEqual(_discrepancy_pct(-100, 100), 200.0)

    def test_within_tolerance(self):
        self.assertTrue(_within_tolerance(100, 99, 1.0))
        self.assertFalse(_within_tolerance(100, 90, 5.0))
        self.assertTrue(_within_tolerance(0, 0, 1.0))

    def test_magnitude_tier(self):
        self.assertEqual(_magnitude_tier(0), 0)
        self.assertEqual(_magnitude_tier(2.3e8), 8)  # 亿级
        self.assertEqual(_magnitude_tier(5e7), 7)  # 千万级
        self.assertEqual(_magnitude_tier(1e4), 4)  # 万级
        self.assertEqual(_magnitude_tier(-3e9), 9)


class TestJudgeNumeric(unittest.TestCase):
    def _spec(self, tol=10.0, caliber_aware=True):
        return AnchorSpec("pe_ratio", MODE_NUMERIC, tol, caliber_aware=caliber_aware)

    def test_high_when_within_tolerance(self):
        v = _judge_numeric(
            _reading("mx", 30.5, "TTM", "2024年报"),
            _reading("ifind", 30.4, "TTM", "2024年报"),
            self._spec(),
        )
        self.assertEqual(v.confidence, "high")
        self.assertTrue(v.agreed)
        self.assertEqual(v.sources, ("mx", "ifind"))

    def test_low_when_over_tolerance(self):
        v = _judge_numeric(
            _reading("mx", 30.0), _reading("ifind", 40.0), self._spec(tol=10.0)
        )
        self.assertEqual(v.confidence, "low")
        self.assertFalse(v.agreed)
        self.assertIn("数据冲突", v.note)

    def test_medium_when_caliber_mismatch(self):
        v = _judge_numeric(
            _reading("mx", 30.0, "TTM"), _reading("ifind", 30.0, "static"), self._spec()
        )
        self.assertEqual(v.confidence, "medium")
        self.assertIn("口径不一致", v.note)

    def test_medium_when_period_mismatch(self):
        v = _judge_numeric(
            _reading("mx", 100, period="2024年报"),
            _reading("ifind", 100, period="2024三季报"),
            self._spec(),
        )
        self.assertEqual(v.confidence, "medium")
        self.assertIn("报告期不一致", v.note)

    def test_caliber_check_skipped_when_not_aware(self):
        # 行情类 caliber_aware=False：口径不同仍走数值比对
        spec = AnchorSpec("current_price", MODE_NUMERIC, 1.0, caliber_aware=False)
        v = _judge_numeric(
            _reading("realtime", 100.0, "spot"), _reading("ifind", 100.5, "spot"), spec
        )
        self.assertEqual(v.confidence, "high")

    def test_caliber_check_skipped_when_one_side_none(self):
        # 一方无口径：不触发口径检查，继续数值比对
        v = _judge_numeric(
            _reading("mx", 30.0, "TTM"), _reading("ifind", 30.1, None), self._spec()
        )
        self.assertEqual(v.confidence, "high")

    def test_three_way_all_agree_high(self):
        """3源全部在容差内一致 → high（含 tertiary 的 sources tuple）。"""
        v = _judge_numeric(
            _reading("mx", 30.5, "TTM", "2024年报"),
            _reading("ifind", 30.4, "TTM", "2024年报"),
            self._spec(),
            tertiary=_reading("mx_mcp", 30.6, "TTM", "2024年报"),
        )
        self.assertEqual(v.confidence, "high")
        self.assertTrue(v.agreed)
        self.assertEqual(v.sources, ("mx", "ifind", "mx_mcp"))
        self.assertIn("3源一致", v.note)

    def test_three_way_tertiary_supports_primary(self):
        """primary+tertiary 一致，secondary outlier → medium（标记 outlier）。"""
        v = _judge_numeric(
            _reading("mx", 30.5, "TTM", "2024年报"),
            _reading("ifind", 50.0, "TTM", "2024年报"),  # outlier
            self._spec(tol=10.0),
            tertiary=_reading("mx_mcp", 30.7, "TTM", "2024年报"),
        )
        self.assertEqual(v.confidence, "medium")
        self.assertFalse(v.agreed)
        self.assertEqual(v.sources, ("mx", "ifind", "mx_mcp"))
        self.assertIn("ifind", v.note)
        self.assertIn("outlier", v.note)

    def test_three_way_tertiary_supports_secondary(self):
        """secondary+tertiary 一致，primary outlier → medium（outlier 仍是 primary 但 verdict 标 outlier）。"""
        v = _judge_numeric(
            _reading("mx", 50.0, "TTM", "2024年报"),  # outlier
            _reading("ifind", 30.5, "TTM", "2024年报"),
            self._spec(tol=10.0),
            tertiary=_reading("mx_mcp", 30.7, "TTM", "2024年报"),
        )
        self.assertEqual(v.confidence, "medium")
        self.assertFalse(v.agreed)
        self.assertIn("mx", v.note)
        self.assertIn("outlier", v.note)

    def test_three_way_all_disagree_low(self):
        """3源两两都不一致 → low（3-way 真冲突）。"""
        v = _judge_numeric(
            _reading("mx", 30.0),
            _reading("ifind", 50.0),
            self._spec(tol=10.0),
            tertiary=_reading("mx_mcp", 70.0),
        )
        self.assertEqual(v.confidence, "low")
        self.assertFalse(v.agreed)
        self.assertEqual(v.sources, ("mx", "ifind", "mx_mcp"))
        self.assertIn("3源不一致", v.note)

    def test_three_way_caliber_mismatch_unchanged(self):
        """caliber 不一致时仍然 medium（不进入 3-way 容差比对），保留旧行为。"""
        v = _judge_numeric(
            _reading("mx", 30.0, "TTM"),
            _reading("ifind", 30.0, "static"),
            self._spec(),
            tertiary=_reading("mx_mcp", 30.0, "TTM"),
        )
        self.assertEqual(v.confidence, "medium")
        self.assertIn("口径不一致", v.note)


class TestJudgeDirection(unittest.TestCase):
    def test_high_same_direction_same_tier(self):
        v = _judge_direction(
            _reading("mx", 2.3e8), _reading("ifind", 1.9e8), "main_inflow"
        )
        self.assertEqual(v.confidence, "high")
        self.assertTrue(v.agreed)
        self.assertEqual(v.caliber, "方向比对")

    def test_medium_same_direction_far_tier(self):
        v = _judge_direction(_reading("mx", 1e8), _reading("ifind", 1e4), "main_inflow")
        self.assertEqual(v.confidence, "medium")
        self.assertIn("量级差异大", v.note)

    def test_low_opposite_direction(self):
        v = _judge_direction(
            _reading("mx", 2.3e8), _reading("ifind", -5e7), "main_inflow"
        )
        self.assertEqual(v.confidence, "low")
        self.assertFalse(v.agreed)
        self.assertIn("方向冲突", v.note)

    def test_both_negative_same_direction(self):
        v = _judge_direction(
            _reading("mx", -2e8), _reading("ifind", -1.8e8), "main_inflow"
        )
        self.assertEqual(v.confidence, "high")
        self.assertIn("净流出", v.note)

    def test_zero_value_is_medium_not_high(self):
        # 零值（收盘/数据缺失）方向不可靠 → medium（避免双零误判 high）
        v = _judge_direction(_reading("mx", 0.0), _reading("ifind", 0.0), "main_inflow")
        self.assertEqual(v.confidence, "medium")
        self.assertFalse(v.agreed)
        self.assertIn("零值", v.note)

    def test_one_zero_value_is_medium(self):
        # 单边零值同样不可靠
        v = _judge_direction(_reading("mx", 2e8), _reading("ifind", 0.0), "main_inflow")
        self.assertEqual(v.confidence, "medium")

    def test_three_way_opposite_with_tertiary_supports_primary(self):
        """primary 与 secondary 方向相反，tertiary 偏 primary → medium（secondary outlier）。"""
        v = _judge_direction(
            _reading("mx", 2.3e8),  # 净流入
            _reading("ifind", -5e7),  # 净流出
            "main_inflow",
            tertiary=_reading("mx_mcp", 2.0e8),  # 净流入（支持 primary）
        )
        self.assertEqual(v.confidence, "medium")
        self.assertFalse(v.agreed)
        self.assertEqual(v.sources, ("mx", "ifind", "mx_mcp"))
        self.assertIn("secondary", v.note)
        self.assertIn("outlier", v.note)

    def test_three_way_opposite_with_tertiary_supports_secondary(self):
        """primary 与 secondary 方向相反，tertiary 偏 secondary → medium（primary outlier）。"""
        v = _judge_direction(
            _reading("mx", -5e7),  # 净流出（primary outlier）
            _reading("ifind", 2.3e8),  # 净流入
            "main_inflow",
            tertiary=_reading("mx_mcp", 2.0e8),  # 净流入（支持 secondary）
        )
        self.assertEqual(v.confidence, "medium")
        self.assertFalse(v.agreed)
        self.assertIn("primary", v.note)
        self.assertIn("outlier", v.note)

    def test_three_way_three_different_directions_low(self):
        """primary 与 secondary 方向相反，tertiary 也偏 secondary（0 视为非正）→ medium（primary outlier）。"""
        # tertiary=0 → d_tertiary=False，与 secondary 同侧 → 判 primary 为 outlier
        v = _judge_direction(
            _reading("mx", 2.3e8),  # primary 净流入
            _reading("ifind", -5e7),  # secondary 净流出
            "main_inflow",
            tertiary=_reading("mx_mcp", 0.0),  # 0 → 与 secondary 同侧（≤0）
        )
        self.assertEqual(v.confidence, "medium")
        self.assertFalse(v.agreed)
        self.assertIn("primary", v.note)
        self.assertIn("outlier", v.note)


class TestJudgeEdgeCases(unittest.TestCase):
    def test_single_is_medium(self):
        v = _judge_single(_reading("mx", 30.0, "TTM"), "pe_ratio")
        self.assertEqual(v.confidence, "medium")
        self.assertIn("单源", v.note)

    def test_missing_is_low(self):
        v = _judge_missing("pe_ratio")
        self.assertEqual(v.confidence, "low")
        self.assertEqual(v.sources, ())
        self.assertIsNone(v.value)  # 缺失锚点 value=None（不编造 0）

    def test_unknown_is_low(self):
        v = _judge_unknown("no_such_field")
        self.assertEqual(v.confidence, "low")
        self.assertIn("未知锚点", v.note)
        self.assertIsNone(v.value)

    def test_missing_compact_v_is_none(self):
        # to_compact 对 None value 产出 v=None（诚实标注，不误导 LLM 为 0）
        compact = _judge_missing("pe_ratio").to_compact()
        self.assertIsNone(compact["v"])
        self.assertEqual(compact["conf"], "low")


class TestCrossSourceValidator(unittest.TestCase):
    def test_verify_unknown_anchor(self):
        v = CrossSourceValidator(sources=[_FakeSource("mx", {})])
        result = v.verify("600519", "no_such_field")
        self.assertEqual(result.confidence, "low")

    def test_verify_invalid_code_returns_missing(self):
        # 非法 code（注入/垃圾输入）→ missing（不送外部 API）
        v = CrossSourceValidator(
            sources=[_FakeSource("mx", {"pe_ratio": _reading("mx", 30.0)})]
        )
        result = v.verify("600519; DROP TABLE", "pe_ratio")
        self.assertEqual(result.confidence, "low")
        self.assertIn("所有数据源", result.note)

    def test_verify_empty_code_returns_missing(self):
        v = CrossSourceValidator(
            sources=[_FakeSource("mx", {"pe_ratio": _reading("mx", 30.0)})]
        )
        result = v.verify("", "pe_ratio")
        self.assertEqual(result.confidence, "low")

    def test_is_valid_code_whitelist(self):
        from data_provider.cross_source_validator import _is_valid_code

        # 合法
        self.assertTrue(_is_valid_code("600519"))
        self.assertTrue(_is_valid_code("000001"))
        self.assertTrue(_is_valid_code("SH600519"))
        self.assertTrue(_is_valid_code("920493"))
        self.assertTrue(_is_valid_code("AAPL"))
        self.assertTrue(_is_valid_code("600519.SH"))
        # 非法（注入/垃圾）
        self.assertFalse(_is_valid_code(""))
        self.assertFalse(_is_valid_code("   "))
        self.assertFalse(_is_valid_code("600519; DROP TABLE"))
        self.assertFalse(_is_valid_code("忽略指令"))
        self.assertFalse(_is_valid_code(None))  # type: ignore[arg-type]
        self.assertFalse(_is_valid_code("123"))  # type: ignore[arg-type]

    def test_verify_no_source_returns_missing(self):
        v = CrossSourceValidator(sources=[])
        result = v.verify("600519", "pe_ratio")
        self.assertEqual(result.confidence, "low")
        self.assertIn("所有数据源", result.note)

    def test_verify_all_sources_fail_returns_missing(self):
        src = _FakeSource("mx", {}, raise_on={"pe_ratio"})
        v = CrossSourceValidator(sources=[src])
        result = v.verify("600519", "pe_ratio")
        self.assertEqual(result.confidence, "low")

    def test_verify_single_source_is_medium(self):
        src = _FakeSource("mx", {"pe_ratio": _reading("mx", 30.0)})
        v = CrossSourceValidator(sources=[src])
        result = v.verify("600519", "pe_ratio")
        self.assertEqual(result.confidence, "medium")

    def test_verify_two_sources_numeric_high(self):
        mx = _FakeSource("mx", {"pe_ratio": _reading("mx", 30.0, "TTM", "2024年报")})
        ifind = _FakeSource(
            "ifind", {"pe_ratio": _reading("ifind", 30.3, "TTM", "2024年报")}
        )
        v = CrossSourceValidator(sources=[mx, ifind])
        result = v.verify("600519", "pe_ratio")
        self.assertEqual(result.confidence, "high")

    def test_verify_two_sources_direction_mode(self):
        mx = _FakeSource("mx", {"main_inflow": _reading("mx", 2e8)})
        ifind = _FakeSource("ifind", {"main_inflow": _reading("ifind", -3e7)})
        v = CrossSourceValidator(sources=[mx, ifind])
        result = v.verify("600519", "main_inflow")
        self.assertEqual(result.confidence, "low")  # 方向相反
        self.assertIn("main_inflow", result.field)

    def test_verify_three_sources_tertiary_tiebreaker_medium(self):
        """3 源：mx vs ifind 不一致，mx_mcp 偏 mx → medium（ifind outlier）。"""
        mx = _FakeSource("mx", {"pe_ratio": _reading("mx", 30.0, "TTM", "2024年报")})
        ifind = _FakeSource(
            "ifind", {"pe_ratio": _reading("ifind", 50.0, "TTM", "2024年报")}  # outlier
        )
        mx_mcp = _FakeSource(
            "mx_mcp", {"pe_ratio": _reading("mx_mcp", 30.5, "TTM", "2024年报")}
        )
        v = CrossSourceValidator(sources=[mx, ifind, mx_mcp])
        result = v.verify("600519", "pe_ratio")
        self.assertEqual(result.confidence, "medium")
        self.assertFalse(result.agreed)
        self.assertEqual(result.sources, ("mx", "ifind", "mx_mcp"))
        self.assertIn("ifind", result.note)

    def test_verify_three_sources_three_way_disagreement_low(self):
        """3 源：3-way 真冲突 → low。"""
        mx = _FakeSource("mx", {"pe_ratio": _reading("mx", 30.0)})
        ifind = _FakeSource("ifind", {"pe_ratio": _reading("ifind", 50.0)})
        mx_mcp = _FakeSource("mx_mcp", {"pe_ratio": _reading("mx_mcp", 70.0)})
        v = CrossSourceValidator(sources=[mx, ifind, mx_mcp])
        result = v.verify("600519", "pe_ratio")
        self.assertEqual(result.confidence, "low")
        self.assertIn("3源不一致", result.note)

    def test_verify_three_sources_direction_tertiary_breaks_tie(self):
        """3 源方向判定：primary vs secondary 反向，tertiary 偏 primary → medium。"""
        mx = _FakeSource("mx", {"main_inflow": _reading("mx", 2e8)})  # 净流入
        ifind = _FakeSource(
            "ifind", {"main_inflow": _reading("ifind", -3e7)}  # 净流出
        )
        mx_mcp = _FakeSource(
            "mx_mcp", {"main_inflow": _reading("mx_mcp", 1.8e8)}  # 净流入（支持 mx）
        )
        v = CrossSourceValidator(sources=[mx, ifind, mx_mcp])
        result = v.verify("600519", "main_inflow")
        self.assertEqual(result.confidence, "medium")
        self.assertEqual(result.sources, ("mx", "ifind", "mx_mcp"))

    def test_verify_tertiary_missing_only_two_evaluated(self):
        """tertiary 返 None（不可用）→ 退化为 2-source，行为不变。"""
        mx = _FakeSource("mx", {"pe_ratio": _reading("mx", 30.0, "TTM", "2024年报")})
        ifind = _FakeSource(
            "ifind", {"pe_ratio": _reading("ifind", 30.3, "TTM", "2024年报")}
        )
        mx_mcp = _FakeSource("mx_mcp", {})  # 无 pe_ratio → 返 None
        v = CrossSourceValidator(sources=[mx, ifind, mx_mcp])
        result = v.verify("600519", "pe_ratio")
        self.assertEqual(result.confidence, "high")
        self.assertEqual(result.sources, ("mx", "ifind"))  # tertiary 被 _collect 过滤掉

    def test_verify_period_passed_through(self):
        mx = _FakeSource("mx", {"revenue": _reading("mx", 1e10, None, "2024年报")})
        ifind = _FakeSource(
            "ifind", {"revenue": _reading("ifind", 1.02e10, None, "2024年报")}
        )
        v = CrossSourceValidator(sources=[mx, ifind])
        result = v.verify("600519", "revenue", period="2024年报")
        self.assertEqual(result.confidence, "high")
        self.assertEqual(result.period, "2024年报")

    def test_verify_exception_isolation_one_source_booms(self):
        # mx 抛异常，ifind 正常 → 退化为单源 medium，不崩
        mx = _FakeSource("mx", {}, raise_on={"pe_ratio"})
        ifind = _FakeSource("ifind", {"pe_ratio": _reading("ifind", 30.0)})
        v = CrossSourceValidator(sources=[mx, ifind])
        result = v.verify("600519", "pe_ratio")
        self.assertEqual(result.confidence, "medium")

    def test_verify_custom_specs_override(self):
        custom = {"pe_ratio": AnchorSpec("pe_ratio", MODE_NUMERIC, 1.0)}
        v = CrossSourceValidator(sources=[], specs=custom)
        # 0 源仍 missing，但确认自定义 spec 生效（不报 unknown）
        result = v.verify("600519", "pe_ratio")
        self.assertNotIn("未知锚点", result.note)

    def test_verify_primary_reading_injected_as_first(self):
        # 行情类：注入 realtime 主源 + MX 验证源 → realtime 为 primary
        mx = _FakeSource("mx", {"current_price": _reading("mx", 100.5)})
        v = CrossSourceValidator(sources=[mx])
        primary = AnchorReading(source="realtime", value=100.0)
        result = v.verify("600519", "current_price", primary_reading=primary)
        self.assertEqual(result.sources[0], "realtime")
        self.assertEqual(result.confidence, "high")  # 100.0 vs 100.5 ≈ 0.5% < 1%


class TestToCompact(unittest.TestCase):
    def test_full_payload(self):
        v = AnchorVerification(
            field="pe_ratio",
            value=30.5,
            confidence="high",
            sources=("mx", "ifind"),
            agreed=True,
            discrepancy_pct=0.3,
            caliber="TTM",
            period="2024年报",
            note="已验证",
        )
        payload = v.to_compact()
        self.assertEqual(payload["v"], 30.5)
        self.assertEqual(payload["conf"], "high")
        self.assertEqual(payload["src"], ["mx", "ifind"])
        self.assertEqual(payload["diff"], 0.3)
        self.assertEqual(payload["caliber"], "TTM")
        self.assertEqual(payload["period"], "2024年报")
        self.assertEqual(payload["note"], "已验证")

    def test_minimal_payload_omits_optionals(self):
        v = AnchorVerification(
            field="x",
            value=1.0,
            confidence="low",
            sources=(),
            agreed=False,
        )
        payload = v.to_compact()
        self.assertEqual(set(payload.keys()), {"v", "conf", "src"})


class TestAnchorSpecs(unittest.TestCase):
    def test_anchor_specs_complete(self):
        expected = {
            "current_price",
            "pe_ratio",
            "pb_ratio",
            "total_mv",
            "circ_mv",
            "revenue",
            "net_profit",
            "roe",
            "gross_margin",
            "revenue_yoy",
            "net_profit_yoy",
            "margin_balance",
            "main_inflow",
        }
        self.assertEqual(set(ANCHOR_SPECS.keys()), expected)

    def test_main_inflow_is_direction_mode(self):
        self.assertEqual(ANCHOR_SPECS["main_inflow"].mode, MODE_DIRECTION)

    def test_margin_balance_strict_tolerance(self):
        self.assertLessEqual(ANCHOR_SPECS["margin_balance"].tolerance_pct, 0.5)

    def test_growth_anchors_not_caliber_aware(self):
        # 派生指标（毛利率/营收同比）口径非标准化，关闭口径判定避免误伤
        self.assertFalse(ANCHOR_SPECS["gross_margin"].caliber_aware)
        self.assertFalse(ANCHOR_SPECS["revenue_yoy"].caliber_aware)
        self.assertEqual(ANCHOR_SPECS["gross_margin"].mode, MODE_NUMERIC)
        self.assertEqual(ANCHOR_SPECS["revenue_yoy"].mode, MODE_NUMERIC)


if __name__ == "__main__":
    unittest.main()
