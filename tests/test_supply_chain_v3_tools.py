# -*- coding: utf-8 -*-
"""[v3 PR-B] 6 个深度小节工具的单测。

覆盖：
1. 工具元数据：name / category / parameters / handler signature
2. handler fail-open：KB 不可用 / LLM 不可用 / JSON 解析失败
3. handler 正常路径：mock KB + mock LLM，返回结构化 dict（经 Pydantic 校验）
4. handler 契约校验：LLM 输出字段错 → 整条记录被跳过，不拖垮整批
5. 工具注册：11 个工具全部在 ALL_SUPPLY_CHAIN_TOOLS（v2 5个 + v3 6个）
"""

from __future__ import annotations

from typing import Any, List
from unittest.mock import MagicMock, patch

import pytest

from src.agent.tools.registry import ToolDefinition
from src.agent.tools.supply_chain_tools import (
    ALL_SUPPLY_CHAIN_TOOLS,
    _format_v3_kb_hits,
    _handle_analyze_capacity_outlook,
    _handle_analyze_financial_quality,
    _handle_analyze_industry_outlook,
    _handle_analyze_market_position,
    _handle_analyze_product_matrix,
    _handle_extract_key_partners,
    _parse_v3_json,
    analyze_capacity_outlook_tool,
    analyze_financial_quality_tool,
    analyze_industry_outlook_tool,
    analyze_market_position_tool,
    analyze_product_matrix_tool,
    extract_key_partners_tool,
)


V3_TOOL_NAMES = {
    "analyze_product_matrix",
    "analyze_market_position",
    "extract_key_partners",
    "analyze_industry_outlook",
    "analyze_financial_quality",
    "analyze_capacity_outlook",
}


def _fake_llm_response(content: str) -> Any:
    """构造一个伪 LLMResponse（duck-typed，handler 只读 .content）。"""
    resp = MagicMock()
    resp.content = content
    return resp


def _fake_kb(hits: List[Any] | None = None, agg: float = 0.5) -> Any:
    """构造一个伪 SupplyChainKBResult。"""
    result = MagicMock()
    result.hits = hits or []
    result.aggregate_score = agg
    return result


@pytest.fixture(autouse=True)
def _no_real_data_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """[v3 真实数据] 全局 mock 掉 _fetch_real_* 防止网络 hang 拖累测试。"""
    import src.agent.tools.supply_chain_tools as tools_mod

    monkeypatch.setattr(tools_mod, "_fetch_real_stock_info", lambda ticker: {})
    monkeypatch.setattr(tools_mod, "_fetch_real_realtime_quote", lambda ticker: {})


# ============================================================
# 工具注册 & 元数据
# ============================================================


class TestV3ToolRegistration:
    def test_all_tools_registered(self) -> None:
        """v2 5 个 + v3 6 个 = 11 个工具。"""
        # issue #45 退役 verify_supply_chain_evidence 后为 10 个
        assert len(ALL_SUPPLY_CHAIN_TOOLS) == 10
        names = {t.name for t in ALL_SUPPLY_CHAIN_TOOLS}
        assert V3_TOOL_NAMES <= names

    def test_v3_tool_categories(self) -> None:
        """5 个 v3 工具都属于 analysis 类别。"""
        for tool in ALL_SUPPLY_CHAIN_TOOLS:
            if tool.name in V3_TOOL_NAMES:
                assert tool.category == "analysis"
                assert isinstance(tool, ToolDefinition)

    def test_v3_tool_required_params(self) -> None:
        """ticker / company 是必填，market / industry_hint / top_k 可选。"""
        for tool in ALL_SUPPLY_CHAIN_TOOLS:
            if tool.name not in V3_TOOL_NAMES:
                continue
            param_names = {p.name for p in tool.parameters}
            assert "ticker" in param_names
            assert "company" in param_names
            required = {p.name for p in tool.parameters if p.required}
            assert "ticker" in required
            assert "company" in required
            assert "top_k" not in required
            assert "industry_hint" not in required


# ============================================================
# JSON 解析
# ============================================================


class TestParseV3Json:
    def test_pure_json(self) -> None:
        out = _parse_v3_json('{"a": 1, "b": "x"}')
        assert out == {"a": 1, "b": "x"}

    def test_markdown_fence(self) -> None:
        out = _parse_v3_json('```json\n{"a": 2}\n```')
        assert out == {"a": 2}

    def test_markdown_fence_no_lang(self) -> None:
        out = _parse_v3_json('```\n{"a": 3}\n```')
        assert out == {"a": 3}

    def test_extra_text_around_json(self) -> None:
        out = _parse_v3_json('一些废话 {"a": 4} 一些更多废话')
        assert out == {"a": 4}

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError):
            _parse_v3_json("no json here")

    def test_json_repair_trailing_comma(self) -> None:
        """json_repair 自动修复尾随逗号。"""
        out = _parse_v3_json('{"a": 1, "b": 2,}')
        assert out == {"a": 1, "b": 2}

    def test_json_repair_single_quotes(self) -> None:
        """json_repair 自动修复单引号。"""
        out = _parse_v3_json("{'a': 1, 'b': 'x'}")
        assert out == {"a": 1, "b": "x"}

    def test_nested_objects_picks_largest(self) -> None:
        """多个嵌套 JSON 块时取最长的（最完整的）。"""
        text = '{"small": 1} {"outlooks": [{"x": 1}, {"y": 2}], "more": "data"}'
        out = _parse_v3_json(text)
        assert "outlooks" in out
        assert "small" not in out

    def test_nested_products_picked_over_inner_item(self) -> None:
        """[v3 修复] 含数组的最外层 dict 必须优先于内部第一个产品。"""
        text = '{"products": [{"name": "A"}, {"name": "B"}]}'
        out = _parse_v3_json(text)
        assert "products" in out
        assert len(out["products"]) == 2

    def test_truncated_then_repaired(self) -> None:
        """截断 JSON（缺右括号）由 json_repair 修复。

        json_repair 对截断的容忍度有限——可能补全最外层 dict
        但不一定能恢复数组中的元素。本测试接受 json_repair 的实际能力范围。
        """
        out = _parse_v3_json('{"products": [{"name": "A"}')
        # 至少要解析为 dict（json_repair 会尝试补全）
        assert isinstance(out, dict)


# ============================================================
# KB hits 格式化
# ============================================================


class TestFormatV3KBHits:
    def test_no_retriever(self) -> None:
        out = _format_v3_kb_hits(None, "600519", "贵州茅台", "", 5)
        assert "不可用" in out

    def test_zero_hits(self) -> None:
        kb = MagicMock()
        kb.retrieve.return_value = _fake_kb(hits=[], agg=0.1)
        out = _format_v3_kb_hits(kb, "600519", "贵州茅台", "白酒", 5)
        assert "0 命中" in out

    def test_with_hits(self) -> None:
        hit = MagicMock()
        hit.document_id = "doc1"
        hit.chunk_id = "c1"
        hit.score = 0.8
        hit.content = "茅台是高端白酒龙头"
        kb = MagicMock()
        kb.retrieve.return_value = _fake_kb(hits=[hit], agg=0.8)
        out = _format_v3_kb_hits(kb, "600519", "贵州茅台", "白酒", 5)
        assert "doc1" in out
        assert "c1" in out
        assert "茅台" in out

    def test_kb_exception_caught(self) -> None:
        kb = MagicMock()
        kb.retrieve.side_effect = RuntimeError("KB down")
        out = _format_v3_kb_hits(kb, "600519", "贵州茅台", "", 5)
        assert "异常" in out


# ============================================================
# §6 analyze_product_matrix handler
# ============================================================


class TestAnalyzeProductMatrix:
    def test_no_llm_returns_error(self) -> None:
        with patch(
            "src.agent.tools.supply_chain_tools._get_v3_chat_llm",
            return_value=None,
        ):
            out = _handle_analyze_product_matrix(ticker="600519", company="贵州茅台")
        assert "error" in out
        assert out["products"] == []
        assert out["ticker"] == "600519"

    def test_valid_llm_output(self) -> None:
        llm = MagicMock()
        llm.call_text.return_value = _fake_llm_response(
            '{"products": [{"name": "飞天 53°", "category": "core", '
            '"revenue_share_pct": 65.0, "gross_margin_pct": 92.0, '
            '"target_market": ["商务宴请"], "price_band": "1499+", '
            '"differentiators": ["品牌护城河"], '
            '"evidence_strength": "primary"}]}'
        )
        # [v3 真实数据] mock 掉 fetch_real_* 防止网络 hang
        with (
            patch(
                "src.agent.tools.supply_chain_tools._get_v3_chat_llm",
                return_value=llm,
            ),
            patch(
                "src.agent.tools.supply_chain_tools._fetch_real_stock_info",
                return_value={},
            ),
            patch(
                "src.agent.tools.supply_chain_tools._fetch_real_realtime_quote",
                return_value={},
            ),
        ):
            out = _handle_analyze_product_matrix(
                ticker="600519", company="贵州茅台", industry_hint="白酒"
            )
        assert "error" not in out, f"got {out}"
        assert len(out["products"]) == 1
        p = out["products"][0]
        assert p["name"] == "飞天 53°"
        assert p["category"] == "core"
        assert p["revenue_share_pct"] == 65.0
        assert p["evidence_strength"] == "primary"

    def test_invalid_product_skipped(self) -> None:
        """LLM 输出中含无效字段时，整条被跳过，其它正常。"""
        llm = MagicMock()
        llm.call_text.return_value = _fake_llm_response(
            '{"products": ['
            '{"name": "valid", "category": "core", "revenue_share_pct": 50.0},'
            '{"name": "x", "category": "INVALID_ENUM", "revenue_share_pct": 50.0}'
            "]}"
        )
        with (
            patch(
                "src.agent.tools.supply_chain_tools._get_v3_chat_llm",
                return_value=llm,
            ),
            patch(
                "src.agent.tools.supply_chain_tools._fetch_real_stock_info",
                return_value={},
            ),
            patch(
                "src.agent.tools.supply_chain_tools._fetch_real_realtime_quote",
                return_value={},
            ),
        ):
            out = _handle_analyze_product_matrix(ticker="600519", company="贵州茅台")
        assert len(out["products"]) == 1
        assert out["products"][0]["name"] == "valid"

    def test_malformed_json_returns_error(self) -> None:
        llm = MagicMock()
        llm.call_text.return_value = _fake_llm_response("not json at all")
        with patch(
            "src.agent.tools.supply_chain_tools._get_v3_chat_llm",
            return_value=llm,
        ):
            out = _handle_analyze_product_matrix(ticker="600519", company="贵州茅台")
        assert "error" in out

    def test_llm_exception_returns_error(self) -> None:
        llm = MagicMock()
        llm.call_text.side_effect = RuntimeError("LLM down")
        with patch(
            "src.agent.tools.supply_chain_tools._get_v3_chat_llm",
            return_value=llm,
        ):
            out = _handle_analyze_product_matrix(ticker="600519", company="贵州茅台")
        assert "error" in out


# ============================================================
# §7 analyze_market_position handler
# ============================================================


class TestAnalyzeMarketPosition:
    def test_no_llm(self) -> None:
        with patch(
            "src.agent.tools.supply_chain_tools._get_v3_chat_llm",
            return_value=None,
        ):
            out = _handle_analyze_market_position(ticker="600519", company="贵州茅台")
        assert "error" in out
        assert out["positions"] == []

    def test_valid(self) -> None:
        llm = MagicMock()
        llm.call_text.return_value = _fake_llm_response(
            '{"positions": [{"subsegment": "高端白酒", '
            '"market_share_pct": 33.0, "market_rank": 1, '
            '"cr3_pct": 75.0, "share_trend": "rising", '
            '"top_competitors": ["五粮液", "泸州老窖"], '
            '"substitution_risk": "low", '
            '"evidence_strength": "primary"}]}'
        )
        with patch(
            "src.agent.tools.supply_chain_tools._get_v3_chat_llm",
            return_value=llm,
        ):
            out = _handle_analyze_market_position(ticker="600519", company="贵州茅台")
        assert len(out["positions"]) == 1
        pos = out["positions"][0]
        assert pos["market_share_pct"] == 33.0
        assert pos["market_rank"] == 1

    def test_contract_violation_skipped(self) -> None:
        """rank 给出但 share 缺失 → 契约违反 → 整条被跳过。"""
        llm = MagicMock()
        llm.call_text.return_value = _fake_llm_response(
            '{"positions": [{"subsegment": "X", "market_rank": 1}]}'
        )
        with patch(
            "src.agent.tools.supply_chain_tools._get_v3_chat_llm",
            return_value=llm,
        ):
            out = _handle_analyze_market_position(ticker="600519", company="贵州茅台")
        assert out["positions"] == []


# ============================================================
# §8 extract_key_partners handler
# ============================================================


class TestExtractKeyPartners:
    def test_no_llm(self) -> None:
        with patch(
            "src.agent.tools.supply_chain_tools._get_v3_chat_llm",
            return_value=None,
        ):
            out = _handle_extract_key_partners(ticker="600519", company="贵州茅台")
        assert "error" in out
        assert out["customers"] == []
        assert out["suppliers"] == []

    def test_anonymous_customer_preserved(self) -> None:
        llm = MagicMock()
        llm.call_text.return_value = _fake_llm_response(
            '{"customers": [{"name": "客户A", "share_pct": 12.0, '
            '"is_related_party": true, "is_anonymous": true, '
            '"public_source": "annual_report", '
            '"evidence_strength": "primary"}], '
            '"suppliers": [{"name": "有机高粱基地", "share_pct": 35.0, '
            '"public_source": "prospectus"}]}'
        )
        with patch(
            "src.agent.tools.supply_chain_tools._get_v3_chat_llm",
            return_value=llm,
        ):
            out = _handle_extract_key_partners(ticker="600519", company="贵州茅台")
        assert len(out["customers"]) == 1
        c = out["customers"][0]
        assert c["is_anonymous"] is True
        assert c["side"] == "customer"
        assert len(out["suppliers"]) == 1
        s = out["suppliers"][0]
        assert s["side"] == "supplier"

    def test_invalid_partner_skipped(self) -> None:
        """非法 side / 缺 name 时被 Pydantic 拒绝 → 跳过。"""
        llm = MagicMock()
        llm.call_text.return_value = _fake_llm_response(
            '{"customers": ['
            '{"name": "valid", "public_source": "news"},'
            '{"name": "x", "public_source": "INVALID_ENUM"}'
            '], "suppliers": []}'
        )
        with patch(
            "src.agent.tools.supply_chain_tools._get_v3_chat_llm",
            return_value=llm,
        ):
            out = _handle_extract_key_partners(ticker="600519", company="贵州茅台")
        assert len(out["customers"]) == 1


# ============================================================
# §9 analyze_industry_outlook handler
# ============================================================


class TestAnalyzeIndustryOutlook:
    def test_no_llm(self) -> None:
        with patch(
            "src.agent.tools.supply_chain_tools._get_v3_chat_llm",
            return_value=None,
        ):
            out = _handle_analyze_industry_outlook(ticker="600519", company="贵州茅台")
        assert "error" in out
        assert out["outlooks"] == []

    def test_valid_growing(self) -> None:
        llm = MagicMock()
        llm.call_text.return_value = _fake_llm_response(
            '{"outlooks": [{"subsegment": "高端白酒", '
            '"subsegment_status": "growing", '
            '"tam_2024_usd_bn": 200.0, "tam_2027e_usd_bn": 300.0, '
            '"cagr_2024_2027_pct": 14.5, "china_share_pct": 95.0, '
            '"demand_drivers": ["商务复苏"], "policy_catalysts": ["消费税"], '
            '"substitution_threats": ["进口烈酒"], '
            '"time_window": "mid_term_6_12m", '
            '"evidence_strength": "primary"}]}'
        )
        with patch(
            "src.agent.tools.supply_chain_tools._get_v3_chat_llm",
            return_value=llm,
        ):
            out = _handle_analyze_industry_outlook(
                ticker="600519", company="贵州茅台", industry_hint="白酒"
            )
        assert len(out["outlooks"]) == 1
        o = out["outlooks"][0]
        assert o["tam_2027e_usd_bn"] == 300.0
        assert o["subsegment_status"] == "growing"

    def test_declining_tam_exempt(self) -> None:
        """declining 行业允许 TAM 大幅下降。"""
        llm = MagicMock()
        llm.call_text.return_value = _fake_llm_response(
            '{"outlooks": [{"subsegment": "传统化工", '
            '"subsegment_status": "declining", '
            '"tam_2024_usd_bn": 100.0, "tam_2027e_usd_bn": 20.0, '
            '"demand_drivers": ["淘汰"], "time_window": "long_term_12_36m"}]}'
        )
        with patch(
            "src.agent.tools.supply_chain_tools._get_v3_chat_llm",
            return_value=llm,
        ):
            out = _handle_analyze_industry_outlook(ticker="X", company="Y")
        assert len(out["outlooks"]) == 1
        assert out["outlooks"][0]["tam_2027e_usd_bn"] == 20.0


# ============================================================
# §10 analyze_financial_quality handler
# ============================================================


class TestAnalyzeFinancialQuality:
    def test_no_llm(self) -> None:
        """[v3 兜底] LLM 不可用时，仍返回 1 条占位报告（不空数组），red_flags 含真实数据兜底。"""
        with patch(
            "src.agent.tools.supply_chain_tools._get_v3_chat_llm",
            return_value=None,
        ):
            out = _handle_analyze_financial_quality(ticker="600519", company="贵州茅台")
        # 现在不再返回 error，而是直接构造兜底报告
        assert "error" not in out
        assert len(out["reports"]) >= 1
        assert out["reports"][0]["period"] == "2024Q3"

    def test_valid_report(self) -> None:
        llm = MagicMock()
        llm.call_text.return_value = _fake_llm_response(
            '{"reports": [{"period": "2024Q3", '
            '"revenue_yoy_pct": 15.0, "gross_margin_pct": 91.5, '
            '"gross_margin_change_yoy_pct": 0.5, '
            '"operating_cash_flow_yoy_pct": 18.0, '
            '"ar_to_revenue_pct": 2.3, "inventory_days": 365, '
            '"contract_liability_yoy_pct": 25.0, '
            '"capex_intensity_pct": 8.0, '
            '"capacity_utilization_pct": 95.0, '
            '"revenue_segments": {"茅台酒": 85.0, "系列酒": 13.0, "其他": 2.0}, '
            '"red_flags": [], '
            '"evidence_strength": "primary"}]}'
        )
        with patch(
            "src.agent.tools.supply_chain_tools._get_v3_chat_llm",
            return_value=llm,
        ):
            out = _handle_analyze_financial_quality(ticker="600519", company="贵州茅台")
        assert len(out["reports"]) == 1
        r = out["reports"][0]
        assert r["period"] == "2024Q3"
        assert r["revenue_segments"]["茅台酒"] == 85.0
        assert sum(r["revenue_segments"].values()) == 100.0

    def test_invalid_period_skipped(self) -> None:
        """period 不符合 YYYY / YYYYQ[1-4] → Pydantic 拒绝 → LLM 输出无效 → 走兜底报告。"""
        llm = MagicMock()
        llm.call_text.return_value = _fake_llm_response(
            '{"reports": [{"period": "2024-Q3"}]}'
        )
        with patch(
            "src.agent.tools.supply_chain_tools._get_v3_chat_llm",
            return_value=llm,
        ):
            out = _handle_analyze_financial_quality(ticker="600519", company="贵州茅台")
        # [v3 兜底] LLM 无效 → handler 构造兜底报告（不空）
        assert len(out["reports"]) == 1
        assert out["reports"][0]["period"] == "2024Q3"

    def test_period_with_chinese_appended_skipped(self) -> None:
        """[v3 修复] period='2024Q3 或 2024' 不符合原模式（handler 现在仍按 strict 校验，
        但下游渲染层/工具不应返回此格式）。本测试保留非法字符串应被拒绝。"""
        llm = MagicMock()
        llm.call_text.return_value = _fake_llm_response(
            '{"reports": [{"period": "2024Q3 或 2024", "revenue_yoy_pct": 5.0}]}'
        )
        with patch(
            "src.agent.tools.supply_chain_tools._get_v3_chat_llm",
            return_value=llm,
        ):
            out = _handle_analyze_financial_quality(ticker="600519", company="贵州茅台")
        # [v3 兜底] 同样走兜底
        assert len(out["reports"]) == 1
        assert out["reports"][0]["period"] == "2024Q3"

    def test_segment_sum_too_off_skipped(self) -> None:
        """占比合计偏差 > 5% → 契约违反 → 跳过 → 走兜底。"""
        llm = MagicMock()
        llm.call_text.return_value = _fake_llm_response(
            '{"reports": [{"period": "2024Q3", '
            '"revenue_segments": {"A": 50.0, "B": 30.0}}]}'
        )
        with patch(
            "src.agent.tools.supply_chain_tools._get_v3_chat_llm",
            return_value=llm,
        ):
            out = _handle_analyze_financial_quality(ticker="600519", company="贵州茅台")
        # [v3 兜底] 走兜底报告
        assert len(out["reports"]) == 1
        assert out["reports"][0]["period"] == "2024Q3"


# ============================================================
# §10.b analyze_capacity_outlook handler
# ============================================================


class TestAnalyzeCapacityOutlook:
    def test_no_llm(self) -> None:
        """LLM 不可用时返回 insufficient_data 兜底。"""
        with patch(
            "src.agent.tools.supply_chain_tools._get_v3_chat_llm",
            return_value=None,
        ):
            out = _handle_analyze_capacity_outlook(
                ticker="600519",
                company="贵州茅台",
                industry_hint="白酒",
            )
        assert out["capacity_outlook"]["trend"] == "insufficient_data"
        assert out["capacity_outlook"]["confidence"] == "low"

    def test_valid_forecast(self) -> None:
        """正常 LLM 输出 → 返回结构化 forecast。"""
        llm = MagicMock()
        llm.call_text.return_value = _fake_llm_response(
            '{"ticker": "600519", '
            '"company": "贵州茅台", '
            '"industry_unit_hint": "万千升/年", '
            '"historical_summary": "近3期均值为 85.3%", '
            '"historical_data_quality": "partial", '
            '"forecasts": [{'
            '"time_window": "near_term_3_6m", '
            '"period_label": "2026-10", '
            '"predicted_utilization_pct": 88.5, '
            '"predicted_output_volume": 5.2, '
            '"predicted_output_unit": "万千升", '
            '"inference_basis": "下游订单饱满+中秋旺季", '
            '"demand_signals": ["下游订单饱满", "季节性旺季"], '
            '"capacity_change_factors": [], '
            '"confidence": "high", '
            '"evidence_strength": "analysis"}], '
            '"trend": "rising", '
            '"trend_rationale": "需求旺盛+产能逐步释放", '
            '"capacity_bottleneck_risk": "medium", '
            '"demand_supply_balance": "tight", '
            '"expansion_plans": [{'
            '"project_name": "茅台酱香产能扩建", '
            '"expected_completion": "2026Q3", '
            '"expected_capacity_addition": "1.5万千升/年", '
            '"progress_status": "ramping", '
            '"source": "年报披露", '
            '"evidence_strength": "primary"}], '
            '"data_source_notes": "年报披露+行业数据", '
            '"confidence": "high"}'
        )
        with patch(
            "src.agent.tools.supply_chain_tools._get_v3_chat_llm",
            return_value=llm,
        ):
            out = _handle_analyze_capacity_outlook(
                ticker="600519",
                company="贵州茅台",
                industry_hint="白酒",
                historical_capacity="2025Q2: 92.5%, 2025Q1: 88.3%, 2024Q4: 85.0%",
                demand_drivers="高端白酒需求旺盛, 茅台系列酒放量",
                expansion_projects="茅台酱香产能扩建，预计2026Q3投产",
                capacity_unit_hint="万千升/年",
                benchmark_utilization=75.0,
                seasonal_pattern="Q4>Q2>Q3>Q1",
            )
        assert out["capacity_outlook"]["ticker"] == "600519"
        assert len(out["capacity_outlook"]["forecasts"]) == 1
        assert out["capacity_outlook"]["trend"] == "rising"

    def test_insufficient_data_fallback(self) -> None:
        """LLM 输出无效 → 返回 insufficient_data 兜底。"""
        llm = MagicMock()
        llm.call_text.return_value = _fake_llm_response("invalid json")
        with patch(
            "src.agent.tools.supply_chain_tools._get_v3_chat_llm",
            return_value=llm,
        ):
            out = _handle_analyze_capacity_outlook(
                ticker="600519",
                company="贵州茅台",
            )
        assert out["capacity_outlook"]["trend"] == "insufficient_data"


# ============================================================
# 工具定义 + 参数
# ============================================================


class TestV3ToolDefinitions:
    def test_analyze_product_matrix_tool(self) -> None:
        assert analyze_product_matrix_tool.name == "analyze_product_matrix"
        assert analyze_product_matrix_tool.handler == _handle_analyze_product_matrix
        param_names = {p.name for p in analyze_product_matrix_tool.parameters}
        assert {"ticker", "company", "market", "industry_hint", "top_k"} == param_names

    def test_analyze_market_position_tool(self) -> None:
        assert analyze_market_position_tool.name == "analyze_market_position"
        assert analyze_market_position_tool.handler == _handle_analyze_market_position

    def test_extract_key_partners_tool(self) -> None:
        assert extract_key_partners_tool.name == "extract_key_partners"
        assert extract_key_partners_tool.handler == _handle_extract_key_partners

    def test_analyze_industry_outlook_tool(self) -> None:
        assert analyze_industry_outlook_tool.name == "analyze_industry_outlook"
        assert analyze_industry_outlook_tool.handler == _handle_analyze_industry_outlook

    def test_analyze_financial_quality_tool(self) -> None:
        assert analyze_financial_quality_tool.name == "analyze_financial_quality"
        assert (
            analyze_financial_quality_tool.handler == _handle_analyze_financial_quality
        )

    def test_analyze_capacity_outlook_tool(self) -> None:
        assert analyze_capacity_outlook_tool.name == "analyze_capacity_outlook"
        assert (
            analyze_capacity_outlook_tool.handler == _handle_analyze_capacity_outlook
        )


# ============================================================
# 工厂自动注册（不破坏问股工具集）
# ============================================================


class TestV3FactoryWiring:
    """SupplyChainExecutor 应通过 factory 自动获得 6 个新工具。"""

    def test_supply_chain_tools_list_includes_v3(self) -> None:
        """ALL_SUPPLY_CHAIN_TOOLS 是 factory 直接消费的列表——验证 6 个 v3 工具全在。"""
        names = {t.name for t in ALL_SUPPLY_CHAIN_TOOLS}
        assert V3_TOOL_NAMES <= names
        # 同时 v2 旧工具不退化
        assert {
            "score_supply_chain_bottleneck",
            "search_semianalysis",
            "search_clue_hype",
            "search_supply_chain_kb",
        } <= names

    def test_factory_code_references_all_supply_chain_tools(self) -> None:
        """factory.build_supply_chain_executor 源码必须引用 ALL_SUPPLY_CHAIN_TOOLS。"""
        import inspect

        from src.agent import factory

        src = inspect.getsource(factory.build_supply_chain_executor)
        assert "ALL_SUPPLY_CHAIN_TOOLS" in src
        # 验证 5 个 v3 工具的 handler 全部在 supply_chain_executor 注册链可用
        for tool_name in V3_TOOL_NAMES:
            # handler 名可以从模块中导出，验证注册链无遗漏
            handler_name = "_handle_" + tool_name
            import src.agent.tools.supply_chain_tools as tools_mod

            assert hasattr(tools_mod, handler_name), f"handler {handler_name} 缺失"
