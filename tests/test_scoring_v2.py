# -*- coding: utf-8 -*-
"""评分框架 v2 单元测试：权重校验 / 打分纪律 / 规则打分器 / 单一评分源。"""

from __future__ import annotations

from src.scoring.indicators_v2 import (
    aggregate_v2_dimensions,
    framework_total_v2,
    gap_indicator,
    score_chip_cost,
    score_institution_change,
    score_support_indicator,
    score_valuation,
)
from src.scoring.weights_v2 import (
    DIMENSION_WEIGHTS_V2,
    INDICATORS_V2,
    SCORING_VERSION_V2,
    validate_v2_weights,
)


class TestWeightsV2:
    def test_validate_passes_on_shipped_table(self):
        validate_v2_weights()  # 不抛即通过

    def test_dimension_weights_sum_to_one(self):
        assert abs(sum(DIMENSION_WEIGHTS_V2.values()) - 1.0) < 1e-6

    def test_indicator_weights_sum_to_one_per_dimension(self):
        for dim, indicators in INDICATORS_V2.items():
            total = sum(w for _, _, w, _ in indicators)
            assert abs(total - 1.0) < 1e-6, dim


class TestScoringDiscipline:
    def test_missing_indicator_becomes_neutral_gap(self):
        results = aggregate_v2_dimensions({"技术面": {}})
        tech = next(d for d in results if d["dimension"] == "技术面")
        assert tech["data_gaps"] == 3  # 缠论/罗盘/支撑指标全缺
        assert tech["score"] == 50.0  # 全缺 → 中性
        for ind in tech["indicators"]:
            assert ind["data_gap"] is True
            assert ind["confidence"] == "low"
            assert "数据缺口" in ind["summary"]

    def test_available_indicators_renormalized(self):
        chanlun = {
            "id": "chanlun_struct",
            "score": 80.0,
            "confidence": "medium",
            "basis": "rule",
            "data_gap": False,
            "summary": "上涨趋势突破中枢",
        }
        results = aggregate_v2_dimensions(
            {
                "技术面": {
                    "chanlun_struct": chanlun,
                    "compass_weekly": None,
                    "support_indicator": None,
                }
            }
        )
        tech = next(d for d in results if d["dimension"] == "技术面")
        # 只有缠论可用（维度内权重 0.40），归一后权重=1.0 → 维度分=80
        assert tech["score"] == 80.0
        assert tech["data_gaps"] == 2

    def test_gap_indicator_shape(self):
        gap = gap_indicator("x", "测试原因")
        assert gap["score"] == 50.0 and gap["data_gap"] is True


class TestRuleScorers:
    def test_valuation_pe_bands(self):
        assert score_valuation(12, None)["score"] == 78.0
        assert score_valuation(30, None)["score"] == 52.0
        assert score_valuation(70, None)["score"] == 26.0

    def test_valuation_loss_maker_switches_pb(self):
        out = score_valuation(-3.0, 1.2)
        assert out is not None and "PB" in out["summary"]

    def test_valuation_no_data_returns_none(self):
        assert score_valuation(None, None) is None

    def test_institution_change(self):
        assert score_institution_change(8.0)["score"] == 74.0
        assert score_institution_change(-8.0)["score"] == 34.0
        assert score_institution_change(None) is None

    def test_chip_cost(self):
        out = score_chip_cost(13.0, 11.0, 65.0)
        assert out is not None and out["score"] == 65.0
        assert score_chip_cost(None, None, None) is None

    def test_support_indicator_requires_two_parts(self):
        # 只有 RSI 一个要素 → 缺数据
        assert score_support_indicator(None, None, None, 55.0, None) is None
        out = score_support_indicator(13.0, 11.0, 16.0, 55.0, 0.5)
        assert out is not None


class TestSingleSource:
    def test_total_is_weighted_sum(self):
        dims = [
            {"dimension": d, "score": 60.0} for d in DIMENSION_WEIGHTS_V2
        ]
        total = framework_total_v2(dims)
        assert total == 60.0  # 全维 60 分 → 总分 60（单一评分源语义）

    def test_scoring_version_marked(self):
        assert SCORING_VERSION_V2 == "v2.0"
