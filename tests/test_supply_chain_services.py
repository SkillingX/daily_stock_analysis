# -*- coding: utf-8 -*-
"""供应链分析（Serenity 方法）数据层与工具层单元测试。

不依赖 LLM 与网络，只验证 scorecard 加载、打分逻辑与工具 handler 容错。
数据来源：data/supply_chain_skill/（随仓库一起）。
"""

from __future__ import annotations

from src.services.supply_chain import scorecard
from src.agent.tools.supply_chain_tools import (
    ALL_SUPPLY_CHAIN_TOOLS,
    FACTOR_KEYS,
    PENALTY_KEYS,
    _handle_score_supply_chain_bottleneck,
)


def _sample_factors(value: float = 4.0) -> dict:
    return {key: value for key in FACTOR_KEYS}


def _sample_penalties(value: float = 1.0) -> dict:
    return {key: value for key in PENALTY_KEYS}


def _full_input(**overrides):
    data = {
        "ticker": "EXAMPLE",
        "company": "示例公司",
        "market": "A-share",
        "factors": _sample_factors(4.0),
        "penalties": _sample_penalties(1.0),
        "evidence": [
            {"claim": "某环节少数厂商垄断", "source": "公司年报", "strength": "primary"}
        ],
        "what_could_weaken_view": ["竞品扩产", "需求转弱"],
    }
    data.update(overrides)
    return data


# ============================================================
# scorecard 纯函数
# ============================================================


class TestScorecard:
    def test_score_returns_result_and_verdict(self):
        result, verdict = scorecard.score(_full_input())
        assert isinstance(result, dict)
        assert isinstance(verdict, str)
        assert "final_score" in result

    def test_score_in_zero_to_hundred(self):
        result, _ = scorecard.score(_full_input())
        assert 0 <= result["final_score"] <= 100

    def test_to_markdown_renders(self):
        result, _ = scorecard.score(_full_input())
        md = scorecard.to_markdown(result)
        assert isinstance(md, str)
        assert "Final score" in md or "Verdict" in md

    def test_to_markdown_zh_uses_chinese_labels_no_field_names(self):
        result, _ = scorecard.score(_full_input())
        md = scorecard.to_markdown_zh(result)
        assert "瓶颈打分卡" in md
        assert "因子" in md and "惩罚项" in md
        # 不得泄露内部 snake_case 字段名（输出契约：不用字段名）
        for key in ("demand_inflection", "chokepoint_severity", "dilution_financing"):
            assert key not in md

    def test_to_markdown_zh_translates_verdict(self):
        high, _ = scorecard.score(
            _full_input(factors=_sample_factors(5), penalties=_sample_penalties(0))
        )
        md = scorecard.to_markdown_zh(high)
        assert "研究优先级" in md  # 顶级/高 研究优先级
        assert "Top research priority" not in md

    def test_label_maps_match_upstream_keys(self):
        # 防漂移：中文标签表必须覆盖上游全部 factor/penalty key
        from src.services.supply_chain.scorecard import (
            _FACTOR_LABEL_ZH,
            _PENALTY_LABEL_ZH,
        )

        module = scorecard._load_module()
        assert set(_FACTOR_LABEL_ZH) == set(module.WEIGHTS)
        assert set(_PENALTY_LABEL_ZH) == set(module.TEMPLATE["penalties"])

    def test_higher_chokepoint_scores_higher(self):
        # 高卡点（因子全 5、惩罚全 0）应明显高于低卡点（因子全 0）
        high, _ = scorecard.score(
            _full_input(factors=_sample_factors(5), penalties=_sample_penalties(0))
        )
        low, _ = scorecard.score(
            _full_input(factors=_sample_factors(0), penalties=_sample_penalties(0))
        )
        assert high["final_score"] > low["final_score"]

    def test_penalties_reduce_score(self):
        no_penalty, _ = scorecard.score(_full_input(penalties=_sample_penalties(0)))
        heavy_penalty, _ = scorecard.score(_full_input(penalties=_sample_penalties(5)))
        assert no_penalty["final_score"] > heavy_penalty["final_score"]


# ============================================================
# 工具 handler
# ============================================================


class TestScoreTool:
    def test_normal_scoring(self):
        result = _handle_score_supply_chain_bottleneck(
            ticker="EXAMPLE",
            company="示例公司",
            market="A-share",
            factors=_sample_factors(4),
            penalties=_sample_penalties(1),
            evidence=[{"claim": "x", "source": "y", "strength": "primary"}],
            what_could_weaken_view=["a"],
        )
        assert "verdict" in result
        assert "final_score" in result
        assert "score_report_markdown" in result
        assert "usage_note" in result
        assert "error" not in result

    def test_tool_returns_chinese_verdict_and_md(self):
        # 工具返回的 verdict/markdown 已中文化，不泄露英文 verdict 与字段名
        result = _handle_score_supply_chain_bottleneck(
            ticker="EXAMPLE",
            company="示例公司",
            factors=_sample_factors(5),
            penalties=_sample_penalties(0),
        )
        assert "research priority" not in result["verdict"].lower()
        assert result["verdict"]
        md = result["score_report_markdown"]
        assert "瓶颈打分卡" in md
        for key in ("demand_inflection", "architecture_coupling", "dilution_financing"):
            assert key not in md

    def test_missing_factors_default_zero(self):
        # 完全不传 factors / penalties，应补默认 0 而非报错
        result = _handle_score_supply_chain_bottleneck(ticker="X", company="Y")
        assert "error" not in result
        assert "verdict" in result

    def test_invalid_ratings_coerced(self):
        # 非法值（999 / 字符串）应被规整到 0-5，不报错
        result = _handle_score_supply_chain_bottleneck(
            ticker="X",
            company="Y",
            factors={
                "demand_inflection": 999,
                "chokepoint_severity": "bad",
                "evidence_quality": -3,
            },
        )
        assert "error" not in result
        assert "verdict" in result

    def test_partial_factors_filled(self):
        # 只传部分因子，其余补 0
        result = _handle_score_supply_chain_bottleneck(
            ticker="X",
            company="Y",
            factors={"demand_inflection": 5},  # 只传 1 个
        )
        assert "error" not in result


# ============================================================
# 工具集元数据
# ============================================================


class TestToolMetadata:
    def test_supply_chain_tools(self):
        # v3 增 6 个深度小节工具 → 共 11 个工具
        # issue #45 退役 verify_supply_chain_evidence 后为 10 个
        assert len(ALL_SUPPLY_CHAIN_TOOLS) == 10
        names = {t.name for t in ALL_SUPPLY_CHAIN_TOOLS}
        assert names == {
            "score_supply_chain_bottleneck",
            "search_semianalysis",
            "search_clue_hype",
            "search_supply_chain_kb",
            # [v3] 深度小节工具
            "analyze_product_matrix",
            "analyze_market_position",
            "extract_key_partners",
            "analyze_industry_outlook",
            "analyze_financial_quality",
            "analyze_capacity_outlook",
        }
        # 验证 score 工具存在且类别为 analysis（使用 name-based 查找）
        score_tool = next(t for t in ALL_SUPPLY_CHAIN_TOOLS if t.name == "score_supply_chain_bottleneck")
        assert score_tool.category == "analysis"

    def test_eight_factors_eight_penalties(self):
        assert len(FACTOR_KEYS) == 8
        assert len(PENALTY_KEYS) == 8
        # 因子与惩罚 key 不重叠
        assert not (set(FACTOR_KEYS) & set(PENALTY_KEYS))

    def test_tool_has_required_params(self):
        tool = next(t for t in ALL_SUPPLY_CHAIN_TOOLS if t.name == "score_supply_chain_bottleneck")
        param_names = {p.name for p in tool.parameters}
        assert {"ticker", "company", "factors"} <= param_names
