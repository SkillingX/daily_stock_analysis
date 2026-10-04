# -*- coding: utf-8 -*-
"""研报体 v2 回归测试：经理终稿（manager_writeup）+ v2 模板渲染 + 渲染层修复。

全部离线（fake LLM / 无网络）。
"""

from __future__ import annotations

import json

from src.deep_research_dims.manager_writeup import (
    DIM_SECTION_KEYS,
    SECTION_FALLBACK_TITLES,
    _clean_title,
    _extract_json,
    generate_manager_writeup,
    section_title,
)
from src.deep_research_dims.render import (
    DIM_LABELS,
    VIEW_DIM_ORDER,
    _degraded_note,
    _dedup_history_rows,
    _format_cn,
    build_view,
    render_markdown,
    split_dim_sections,
    validate_structure,
)
from src.schemas.deep_research_dims import DIM_IDS, DIM_MODELS, HistoryDim, HistoryRow


def _dims() -> dict:
    return {d: DIM_MODELS[d]() for d in DIM_IDS}


# ---------------------------------------------------------------------------
# _format_cn：JSON 裸奔治理
# ---------------------------------------------------------------------------


class TestFormatCn:
    def test_json_string_to_bullets(self):
        out = _format_cn('{"structure": "缺失", "count": 3}')
        assert "- **结构**：缺失" in out
        assert "- **count**：3" in out

    def test_token_map_key_translation(self):
        out = _format_cn({"unverified": 4, "not_applicable": 1})
        assert "- **未验证**：4" in out
        assert "- **不适用**：1" in out

    def test_nested_dict_and_none(self):
        out = _format_cn({"roe": {"value": None, "label": "偏低"}})
        assert "**roe**：" in out
        assert "**数值**：缺失" in out

    def test_list_of_scalars_and_dicts(self):
        out = _format_cn(["甲", {"name": "茅台酒", "tag": "核心"}])
        assert "- 甲" in out
        assert "- 名称：茅台酒；定位：核心" in out

    def test_plain_string_passthrough(self):
        assert _format_cn("普通文本") == "普通文本"


# ---------------------------------------------------------------------------
# 降级原因收敛（Pydantic 报错不进投资人可见文本）
# ---------------------------------------------------------------------------


class TestDegradedNoteSanitize:
    def test_validation_error_collapsed(self):
        reason = "7 validation errors for OwnershipDim\ntop_holders.0\n  Input should be a valid string"
        note = _degraded_note(type("D", (), {"status": "degraded", "degraded_reason": reason})())
        assert "数据校验未通过" in note
        assert "Input should be" not in note
        assert "validation error" not in note

    def test_reason_truncated_to_first_line(self):
        reason = "接口超时：连接 search backend 失败\nTraceback...more"
        note = _degraded_note(type("D", (), {"status": "degraded", "degraded_reason": reason})())
        assert "Traceback" not in note
        assert "接口超时" in note

    def test_ok_status_empty_note(self):
        assert _degraded_note(type("D", (), {"status": "ok", "degraded_reason": None})()) == ""


# ---------------------------------------------------------------------------
# manager_writeup：JSON 提取 / 清洗 / 三波生成与降级
# ---------------------------------------------------------------------------


class TestExtractJson:
    def test_think_block_stripped(self):
        out = _extract_json("<think>推理</think>\n{\"a\": 1}")
        assert out == {"a": 1}

    def test_garbage_around_json(self):
        out = _extract_json("前置说明\n{\"a\": 1}\n后续备注")
        assert out == {"a": 1}

    def test_invalid_returns_empty(self):
        assert _extract_json("没有JSON") == {}


class TestCleanTitle:
    def test_markdown_symbols_removed(self):
        assert _clean_title("**不是 X，而是 Y**") == "不是 X，而是 Y"

    def test_length_capped(self):
        assert len(_clean_title("长" * 100)) == 45


class _WaveLLM:
    """按 system 内容区分波次返回 JSON 的 fake LLM。"""

    def __init__(self, fail_waves: set | None = None, raise_all: bool = False):
        self.fail_waves = fail_waves or set()
        self.raise_all = raise_all
        self.calls = 0

    def call_text(self, messages, **kwargs):
        if self.raise_all:
            raise RuntimeError("llm down")
        self.calls += 1
        system = messages[0]["content"]
        if '"title_angle"' in system:
            wave = "A"
            payload = {
                "title_angle": "高端白酒现金流平台**",
                "executive_summary": "定性与公式与条件与跟踪清单。" * 10,
                "section_titles": {k: f"判断句标题{k}" for k in list(SECTION_FALLBACK_TITLES) + list(DIM_SECTION_KEYS)},
                "dim_verdicts": {k: f"{k}定性" for k in ("基本面", "消息面", "资金面", "情绪面", "技术面", "宏观")},
            }
        elif '"worldview"' in system:
            wave = "B"
            payload = {
                "section_narratives": {k: f"四段式论述{'内容' * 30}" for k in SECTION_FALLBACK_TITLES},
                "thesis_points": [
                    {"header": f"判断句论点{i}", "body": f"第一句含数字{i}。" + "展开论述。" * 12}
                    for i in range(3)
                ],
                "risk_matrix": [
                    {"risk": f"风险{i}", "impact": f"影响路径{i}", "trigger": f"毛利率破{28 + i}%"}
                    for i in range(3)
                ],
                "money_paths": [
                    {"name": f"路径{i}", "win_rate": f"约{50 - i * 10}%", "playbook": f"触发条件{i}，目标位看到{i}。" + "执行要点。" * 6}
                    for i in range(3)
                ],
                "decision_matrix": [
                    {
                        "persona": f"状态{i}",
                        "action": f"指令{i}",
                        "price_range": f"{20 + i}-{21 + i}",
                        "rationale": f"理由{i}含数字",
                    }
                    for i in range(3)
                ],
                "scenario_triggers": {
                    "optimistic": "收入增速超30%",
                    "neutral": "收入增速15-30%",
                    "pessimistic": "毛利率破28%",
                },
            }
        else:
            wave = "C"
            payload = {"section_narratives": {k: f"维度论述{'内容' * 30}" for k in DIM_SECTION_KEYS}}
        if wave in self.fail_waves:
            return type("R", (), {"content": "not json at all"})()
        return type("R", (), {"content": json.dumps(payload, ensure_ascii=False)})()


class TestGenerateManagerWriteup:
    def test_three_waves_success(self):
        llm = _WaveLLM()
        out = generate_manager_writeup(llm, "贵州茅台", "600519", _dims(), [])
        assert llm.calls == 3
        assert out["title_angle"] == "高端白酒现金流平台"  # ** 被清洗
        assert len(out["executive_summary"]) >= 30
        assert set(out["section_titles"]) == set(SECTION_FALLBACK_TITLES) | set(DIM_SECTION_KEYS)
        assert set(out["section_narratives"]) == set(SECTION_FALLBACK_TITLES) | set(DIM_SECTION_KEYS)
        # 波 B 新增两块：首屏论点 + 风险矩阵
        assert len(out["thesis_points"]) == 3
        assert all(t["header"] and len(t["body"]) >= 30 for t in out["thesis_points"])
        assert len(out["risk_matrix"]) == 3
        assert all(r["risk"] and r["impact"] for r in out["risk_matrix"])
        # 波 B 扩展：赚钱路径/决策矩阵/情景触发
        assert len(out["money_paths"]) == 3
        assert all(p["name"] and p["playbook"] for p in out["money_paths"])
        assert len(out["decision_matrix"]) == 3
        assert all(r["persona"] and r["action"] for r in out["decision_matrix"])
        assert set(out["scenario_triggers"]) == {"optimistic", "neutral", "pessimistic"}
        # 波 A 扩展：六维小计定性
        assert out["dim_verdicts"]["基本面"] == "基本面定性"

    def test_wave_b_missing_fields_degrade_to_empty(self):
        """波 B JSON 合法但缺 thesis_points/risk_matrix 键 → 两块降级为空，叙事不受影响。"""

        class _PartialLLM:
            calls = 0

            def call_text(self, messages, **kwargs):
                self.calls += 1
                system = messages[0]["content"]
                if '"title_angle"' in system:
                    payload = {"title_angle": "t", "section_titles": {}}
                elif '"worldview"' in system:
                    payload = {"section_narratives": {"investment": "论述" * 30}}
                else:
                    payload = {"section_narratives": {}}
                return type("R", (), {"content": json.dumps(payload, ensure_ascii=False)})()

        llm = _PartialLLM()
        out = generate_manager_writeup(llm, "贵州茅台", "600519", _dims(), [])
        assert out["section_narratives"].get("investment")
        assert out["thesis_points"] == []
        assert out["risk_matrix"] == []

    def test_wave_failure_degrades_only_that_wave(self):
        llm = _WaveLLM(fail_waves={"C"})
        out = generate_manager_writeup(llm, "贵州茅台", "600519", _dims(), [])
        assert out["title_angle"]
        assert set(out["section_narratives"]) == set(SECTION_FALLBACK_TITLES)

    def test_llm_down_returns_empty(self):
        out = generate_manager_writeup(_WaveLLM(raise_all=True), "贵州茅台", "600519", _dims(), [])
        assert out == {
            "title_angle": "",
            "executive_summary": "",
            "section_titles": {},
            "section_narratives": {},
            "thesis_points": [],
            "risk_matrix": [],
            "money_paths": [],
            "decision_matrix": [],
            "scenario_triggers": {},
            "dim_verdicts": {},
        }

    def test_none_adapter_returns_empty(self):
        assert generate_manager_writeup(None, "贵州茅台", "600519", _dims(), [])["section_titles"] == {}


class TestSectionTitleFallback:
    def test_no_manager_uses_fallback(self):
        assert section_title(None, "business") == DIM_SECTION_KEYS["business"]
        assert section_title({}, "investment") == SECTION_FALLBACK_TITLES["investment"]

    def test_manager_title_wins(self):
        mgr = {"section_titles": {"business": "不是壳，而是平台"}}
        assert section_title(mgr, "business") == "不是壳，而是平台"
        assert section_title(mgr, "capital") == DIM_SECTION_KEYS["capital"]


# ---------------------------------------------------------------------------
# v2 模板渲染：全量 + 降级路径
# ---------------------------------------------------------------------------


_MANAGER = {
    "title_angle": "高端白酒现金流平台，缺乏催化的观察窗口",
    "executive_summary": "不是简单的板块β标的，而是品牌现金流平台。成立条件：批价企稳、动销改善、外资回流。真正要跟踪的不是目标价，而是批价与动销数据。",
    "section_titles": {
        "investment": "不是板块β，而是独立现金流",
        "business": "品牌护城河完好，但数据盲区扩大",
    },
    "section_narratives": {
        "investment": "判断：观察。机制：缺乏催化。数据：总分 53 分。脆弱点：批价继续下行。",
        "business": "判断：护城河完好。机制：产地与工艺不可复制。数据：毛利率领先同业。脆弱点：批价倒挂。",
    },
}


class TestRenderV2:
    def _view(self, manager=None):
        dims = _dims()
        return build_view(
            "贵州茅台", "600519", "2026-10-03T08:29:00", ["筹码分布数据缺失"], dims, [],
            report_id="600519_20261003test", manager=manager,
        )

    def test_structure_complete_with_manager(self):
        md = render_markdown(self._view(_MANAGER))
        assert validate_structure(md) == []
        assert md.startswith("# 贵州茅台（600519）深度投研：高端白酒现金流平台")
        assert "## 内容概括" in md
        # P0-1 后节标题只在 ## 行渲染一次，节首不再重复粗体行
        assert "## 一、结论：不是板块β，而是独立现金流" in md
        assert md.count("**不是板块β，而是独立现金流**") == 0
        assert "### 业务画像与竞争地位｜品牌护城河完好，但数据盲区扩大" in md
        assert "维度论述" not in md  # 无此 key 不渲染
        assert md.count("**报告结论**") == 0  # v1 重复块不复发

    def test_structure_complete_without_manager(self):
        md = render_markdown(self._view(None))
        assert validate_structure(md) == []
        assert "### 业务画像与竞争地位" in md
        assert "## 内容概括" not in md

    def test_degraded_dim_sanitized_in_report(self):
        dims = _dims()
        reason = "7 validation errors for SentimentDim\nunverified_count\n  Input should be a valid integer"
        dims["sentiment"] = dims["sentiment"].model_copy(
            update={"status": "degraded", "degraded_reason": reason}
        )
        view = build_view("贵州茅台", "600519", "2026-10-03T08:29:00", [], dims, [])
        md = render_markdown(view)
        assert "数据校验未通过" in md
        assert "Input should be" not in md

    def test_business_json_field_formatted(self):
        dims = _dims()
        dims["business"] = dims["business"].model_copy(
            update={
                "main_products": {
                    "structure": "数据缺失",
                    "products_known": [{"name": "茅台酒", "tag": "核心大单品"}],
                }
            }
        )
        view = build_view("贵州茅台", "600519", "2026-10-03T08:29:00", [], dims, [])
        md = render_markdown(view)
        assert "- **结构**：数据缺失" in md
        assert "- 名称：茅台酒；定位：核心大单品" in md
        assert '{"' not in md  # 无裸 JSON 语法（键名保留原文可接受）

    def test_scenario_names_translated(self):
        dims = _dims()
        from src.schemas.value_scenarios import Scenario, ValueScenarios

        dims["scenarios"] = dims["scenarios"].model_copy(
            update={
                "scenarios": ValueScenarios(
                    scenarios=[
                        Scenario(type="optimistic", probability=0.5, value_anchor=10.0),
                        Scenario(type="pessimistic", probability=0.5, value_anchor=8.0),
                    ]
                ),
                "probability_sum": 1.0,
            }
        )
        view = build_view("贵州茅台", "600519", "2026-10-03T08:29:00", [], dims, [])
        md = render_markdown(view)
        assert "| 乐观 |" in md
        assert "| 悲观 |" in md
        assert "optimistic" not in md

    def test_split_sections_by_marker(self):
        md = render_markdown(self._view(_MANAGER))
        sections = split_dim_sections(md)
        assert set(sections) == set(VIEW_DIM_ORDER)
        for dim_id in VIEW_DIM_ORDER:
            assert sections[dim_id].startswith(f"### {DIM_LABELS[dim_id]}"), dim_id

    def test_history_dedup(self):
        rows = [
            HistoryRow(report_id="r1", created_at="2026-10-02T10:00", rating_hint="中性", one_sentence="X"),
            HistoryRow(report_id="r2", created_at="2026-10-02T11:00", rating_hint="中性", one_sentence="X"),
            HistoryRow(report_id="r3", created_at="2026-10-01T10:00", rating_hint="中性", one_sentence="Y"),
        ]
        history = HistoryDim(rows=rows)
        kept = _dedup_history_rows(history)
        assert len(kept) == 2
        view = build_view(
            "贵州茅台", "600519", "2026-10-03T08:29:00", [], _dims(), [],
            manager=_MANAGER,
        )
        assert isinstance(view["report_type"], str)


# ---------------------------------------------------------------------------
# 回归：机器契约行不被术语中文化 / 结构化字段截断不产生无效 JSON
# ---------------------------------------------------------------------------


class TestMachineLinesUntouched:
    def test_anchor_and_marker_lines_keep_dim_id(self):
        dims = _dims()
        view = build_view(
            "贵州茅台", "600519", "2026-10-03T08:29:00", [], dims, [],
            report_id="r1", manager=_MANAGER,
        )
        md = render_markdown(view)
        assert "<!-- dim:business -->" in md  # 子报告切分标记保留（split_dim_sections 依赖）
        assert '<a name=' not in md  # 裸锚点已删：前端 react-markdown 丢弃原始 HTML，锚点既无效又是文本噪音
        assert "sec-dim-业务" not in md


class TestCoerceCapKeepsValidJson:
    def test_long_structured_dict_stays_valid_json(self):
        from src.agent.deep_research.orchestrator import _coerce_researcher_value

        big = {
            "structure": "说明" * 100,
            "products_known": [{"name": "茅台酒", "tag": "核心" * 80}],
        }
        out = _coerce_researcher_value("main_products", big)
        parsed = json.loads(out)  # 截断上限内必须是合法 JSON（cn 过滤器可解析）
        assert parsed["products_known"][0]["name"] == "茅台酒"

    def test_summary_subkey_extracted(self):
        from src.agent.deep_research.orchestrator import _coerce_researcher_value

        assert _coerce_researcher_value("note", {"summary": "要点"}) == "要点"


# ---------------------------------------------------------------------------
# 投资人可读性：财务指标块 / 贝叶斯摘要 / 键名中文化 / 标题去冒号
# ---------------------------------------------------------------------------


class TestInvestorReadableFormatting:
    def test_metric_block(self):
        from src.deep_research_dims.render import _format_metric_block

        md = _format_metric_block(
            {
                "roe": {"value": 7.9, "score": 45.0, "label": "偏弱"},
                "gross_margin": {"value": 30.93, "score": 65.0, "label": "中高"},
                "revenue_yoy": {"value": None, "score": None, "label": "数据缺失"},
                "scissors": -3.5,
            }
        )
        assert "- ROE（净资产收益率）：7.9%（评价：偏弱）" in md
        assert "- 毛利率：30.93%（评价：中高）" in md
        assert "营收同比增速：数据缺失" in md
        assert "剪刀差（营收-净利增速差）：-3.5%" in md
        assert "score" not in md  # 内部评分不进投资人文本

    def test_bayes_summary_percent(self):
        from src.deep_research_dims.render import _bayes_summary

        bayes = type(
            "B",
            (),
            {"bayesian": type("M", (), {"prior_p": 0.2695, "posterior_p": 0.2695, "edge": -0.230533, "position_suggestion": "0-1%"})()},
        )()
        line = _bayes_summary(bayes)
        assert "先验胜率 27.0% → 后验胜率 27.0%" in line
        assert "Edge -0.23" in line

    def test_cn_key_translation(self):
        out = _format_cn({"source_grade": "推断", "index_environment_for_context_only": {"sh000001": {"change_pct": 0.3}}})
        assert "- **来源等级**：推断" in out
        assert "当日大盘环境（仅背景参考）" in out
        assert "涨跌幅" in out

    def test_title_colon_becomes_comma(self):
        assert _clean_title("中性观察：期望上行仅1.25%") == "中性观察，期望上行仅1.25%"

    def test_business_parse_strips_noise_key(self):
        from src.deep_research.researchers.business import parse

        dim, _ = parse(
            {
                "main_products": {"products": ["晶闸管"], "index_environment_for_context_only": {"a": 1}},
                "competitive_position": {"position": "中型IDM", "index_environment_for_context_only": {"b": 2}},
            },
            1,
        )
        assert "index_environment_for_context_only" not in dim.main_products
        assert "index_environment_for_context_only" not in dim.competitive_position
        assert dim.main_products["products"] == ["晶闸管"]

    def test_report_has_readability_aids(self):
        from src.schemas.report_schema import SniperPoints

        dims = _dims()
        dims["plan"] = dims["plan"].model_copy(
            update={"sniper_points": SniperPoints(ideal_buy=23.0, stop_loss=31.33, take_profit=36.19)}
        )
        view = build_view(
            "贵州茅台", "600519", "2026-10-03T08:29:00", [], dims, [],
            report_id="r1", manager=_MANAGER,
        )
        md = render_markdown(view)
        assert "📌 怎么读这张表" in md
        assert "术语速查" in md


# ---------------------------------------------------------------------------
# 卖方首屏展示层：隐含空间 / 期望值敏感性 / 财务快照 / 论点 / 风险矩阵
# ---------------------------------------------------------------------------


class TestSellSideView:
    def _dims_full(self) -> dict:
        """注入完整情景 + 现价 + 财务指标的典型视图数据。"""
        from src.schemas.report_schema import DataPerspective, PricePosition
        from src.schemas.value_scenarios import Scenario, ValueScenarios

        dims = _dims()
        dims["data"] = dims["data"].model_copy(
            update={"perspective": DataPerspective(price_position=PricePosition(current_price=31.92))}
        )
        dims["scenarios"] = dims["scenarios"].model_copy(
            update={
                "scenarios": ValueScenarios(
                    scenarios=[
                        Scenario(type="optimistic", probability=0.25, value_anchor=41.5),
                        Scenario(type="neutral", probability=0.5, value_anchor=31.92),
                        Scenario(type="pessimistic", probability=0.25, value_anchor=23.94),
                    ]
                ),
                "probability_sum": 1.0,
                "expected_value": 32.32,
                "current_pe_ttm": 51.0,
                "time_paths": {
                    "1天/1周": "缩量企稳则延续 22-22.5 震荡；放量突破 22.8 上看 23.5",
                    "1月": "月线放量突破 24 则上看 26；跌破 20 下看 18.5",
                },
            }
        )
        dims["fundamental"] = dims["fundamental"].model_copy(
            update={
                "profitability": {
                    "roe": {"value": 7.9, "label": "偏弱"},
                    "gross_margin": {"value": 30.93, "label": "中高"},
                },
                "valuation_detail": {"pe_ttm": 51.0, "pb": 4.2, "market_cap": "235亿"},
            }
        )
        from src.schemas.research_framework import (
            DimensionScore,
            IndicatorScore,
            ResearchFramework,
        )

        dims["six_dim"] = dims["six_dim"].model_copy(
            update={
                "framework": ResearchFramework(
                    dimension_total=45.95,
                    dimensions=[
                        DimensionScore(
                            dimension="基本面",
                            weight=0.3,
                            score=46.58,
                            indicators=[
                                IndicatorScore(
                                    name="业务与财务健康", score=46.67, weight=0.5,
                                    summary="财务健康分 46.67/100（可得 3 项指标均值）",
                                )
                            ],
                        )
                    ],
                )
            }
        )
        return dims

    def test_upside_pct(self):
        from src.deep_research_dims.render import _upside_pct

        assert _upside_pct(32.32, 31.92) == 1.3
        assert _upside_pct(28.0, 31.92) == -12.3
        assert _upside_pct(None, 31.92) is None
        assert _upside_pct(32.32, "N/A") is None
        assert _upside_pct(32.32, 0) is None
        assert _upside_pct("abc", 31.92) is None

    def test_ev_sensitivity_matrix(self):
        from src.deep_research_dims.render import _ev_sensitivity

        md = _ev_sensitivity(_dims()["scenarios"].__class__())  # 空情景 → 空串
        assert md == ""
        # 空 ScenariosDim（属性在但 scenarios.scenarios 为 None）
        from src.deep_research_dims.render import _ev_sensitivity as f

        dims = self._dims_full()
        out = f(dims["scenarios"])
        assert "中性概率 40%" in out and "中性概率 60%" in out
        assert "| 中性锚不变 | 32.40 | 32.32 | 32.24 |" in out
        assert "| 中性锚 -10% | 31.12 | 30.72 | 30.32 |" in out
        assert "| 中性锚 +10% | 33.68 | 33.92 | 34.16 |" in out

    def test_fin_snapshot_with_gaps(self):
        from src.deep_research_dims.render import _fin_snapshot

        dims = self._dims_full()
        md = _fin_snapshot(dims["fundamental"])
        assert "| ROE | 7.9% | 偏弱 |" in md
        assert "| 毛利率 | 30.93% | 中高 |" in md
        assert "| PE(TTM) | 51.0 | — |" in md  # 倍数指标不加 %
        assert "| 市值 | 235亿 | — |" in md
        assert "| 营收同比增速 | 数据缺失 | — |" in md  # growth_quality 未注入
        assert "补齐数据后看" in md  # 缺口声明

    def test_first_page_renders_thesis_risks_upside(self):
        manager = {
            **_MANAGER,
            "thesis_points": [
                {"header": "MOSFET已是增长引擎", "body": "2026H1收入11.71亿、占比56%，毛利率31.4%。" + "这是定价错位。"}
            ] * 3,
            "risk_matrix": [
                {"risk": "成熟制程价格战再起", "impact": "2025年毛利率已降5.4pct", "trigger": "毛利率破28%"}
            ] * 2,
        }
        view = build_view(
            "捷捷微电", "300623", "2026-10-03T18:00:00", [], self._dims_full(), [],
            report_id="r2", manager=manager,
        )
        md = render_markdown(view)
        assert validate_structure(md) == []
        # 首屏论点
        assert "■ **MOSFET已是增长引擎。**" in md
        assert "## 三条核心判断" in md
        # 评级行隐含空间 + PE(TTM)
        assert "隐含空间：**1.3%**" in md
        assert "PE(TTM)：51.0 倍" in md
        # 财务快照
        assert "## 财务快照（最新披露期）" in md
        # 风险矩阵节与表
        assert "## 四、风险矩阵" in md
        assert "| 成熟制程价格战再起 | 2025年毛利率已降5.4pct | 毛利率破28% |" in md
        # 估值敏感性出现在走势节
        assert "期望值敏感性" in md
        assert "| 中性锚不变 | 32.40 | 32.32 | 32.24 |" in md
        # 子报告切分不受影响
        sections = split_dim_sections(md)
        assert set(sections) == set(VIEW_DIM_ORDER)

    def test_first_page_degrades_without_new_fields(self):
        """manager 无 thesis_points/risk_matrix → 论点区不渲染，风险节回退占位，结构仍完整。"""
        view = build_view(
            "捷捷微电", "300623", "2026-10-03T18:00:00", [], self._dims_full(), [],
            report_id="r3", manager=_MANAGER,
        )
        md = render_markdown(view)
        assert validate_structure(md) == []
        assert "## 三条核心判断" not in md
        assert "## 四、风险矩阵" in md
        assert "（情报维度未产出风险条目，本节为占位）" in md
        assert "隐含空间：**1.3%**" in md  # 规则计算不依赖 LLM

    def test_clean_header_punctuation_boundary(self):
        """P0-2：论点标题在句读边界截断，不硬切成残句。"""
        from src.deep_research_dims.manager_writeup import _clean_header

        # 长句在 limit 内最后一个句读处切开，无尾部标点
        assert _clean_header("偏离MA250达-21%，中长期处于显著弱势区间") == "偏离MA250达-21%"
        # ≤limit 的标题保留原句、只收尾部标点（尊重 LLM 完整判断，不过度截断）
        assert _clean_header("赔率不足，不值得追。") == "赔率不足，不值得追"
        assert _clean_header("市场隐含0.45高于后验0.28，定价偏贵") == "市场隐含0.45高于后验0.28"

    def test_clean_event_date(self):
        """P0-4：未闭合括号/超长日期收口。"""
        from src.deep_research_dims.render import _clean_event_date

        assert _clean_event_date("未知(按A股规则需在2026年4月30日") == "未知"
        assert _clean_event_date("2026-10-23") == "2026-10-23"
        assert _clean_event_date("") == "日期待定"
        assert _clean_event_date(None) == "日期待定"
        assert _clean_event_date("2026年三季报（预计10月下旬披露）") == "2026年三季报（预计10月下旬披露）"
        assert _clean_event_date("超长日期描述" * 10).endswith("…")

    def test_score_band_view(self):
        """评分区间条 + 结算日期（生成后 1 个月）。"""
        from src.deep_research_dims.render import _score_band_view

        v = _score_band_view(45.95, "2026-10-04T08:42:06")
        assert v["score"] == "45.95" and v["band"] == "中性震荡"
        assert v["settlement"] == "2026-11-04"
        assert _score_band_view(40, "2026-10-04")["band"] == "观望偏空"
        assert _score_band_view(80, "2026-10-04")["band"] == "强烈看多"
        assert _score_band_view(None, "2026-10-04")["band"] == "数据不足"
        # 跨年
        assert _score_band_view(50, "2026-12-15")["settlement"] == "2027-01-15"

    def test_first_page_renders_v4_blocks(self):
        """P1：赚钱路径/决策矩阵/评分区间条/情景触发条件/小计定性全渲染。"""
        manager = {
            **_MANAGER,
            "thesis_points": [{"header": "MOSFET已是增长引擎", "body": "2026H1收入11.71亿、占比56%。" + "这是定价错位。"}] * 3,
            "money_paths": [
                {"name": "财报验证驱动", "win_rate": "约50%", "playbook": "三季报毛利率≥33%右侧介入，持有至年报。" + "看订单变现重估。"}
            ] * 2,
            "decision_matrix": [
                {"persona": "空仓者", "action": "现在不买，挂左侧试仓单", "price_range": "27.5", "rationale": "接近52周低的安全垫位"}
            ] * 2,
            "scenario_triggers": {"optimistic": "单季收入≥6亿", "neutral": "收入4-6亿", "pessimistic": "毛利率破28%"},
            "dim_verdicts": {"基本面": "最大拖累项，改善需证据"},
        }
        view = build_view(
            "至纯科技", "603690", "2026-10-04T08:42:06", [], self._dims_full(), [],
            report_id="r4", manager=manager,
        )
        md = render_markdown(view)
        assert validate_structure(md) == []
        assert "## 评分区间" in md
        assert "**总评分" in md and "中性震荡" in md
        assert "2026-11-04 结算验证" in md
        assert "## 三条赚钱路径（按确定性排序）" in md
        assert "**财报验证驱动**（胜率 约50%）" in md
        assert "**按持仓状态的操作决策**" in md
        assert "| 空仓者 | 现在不买，挂左侧试仓单 | 27.5 |" in md
        # 台账编号
        assert "T1-0" in md and "T1-L" in md and "T1-R" in md and "T1-SL" in md and "T1-S1" in md
        # 情景触发条件列
        assert "| 单季收入≥6亿 |" in md and "| 收入4-6亿 |" in md and "| 毛利率破28% |" in md
        # 时间轴表格
        assert "| 时间轴 | 触发条件与目标位 |" in md
        # 维度小计定性
        assert "**小计：最大拖累项，改善需证据**" in md

    def test_event_plans_date_cleaned_in_report(self):
        """P0-4 集成：脏日期经 event_plans 视图清洗后进报告。"""
        from src.schemas.deep_research_dims import EventPlan

        dims = self._dims_full()
        dims["intel"] = dims["intel"].model_copy(
            update={
                "event_calendar": [
                    EventPlan(event="2026年三季报", date="未知(按A股规则需在2026年4月30日", outcomes=["超预期"], plans=["执行 T1-R"])
                ]
            }
        )
        view = build_view("至纯科技", "603690", "2026-10-04T08:42:06", [], dims, [], report_id="r5", manager=_MANAGER)
        md = render_markdown(view)
        assert "（未知）" in md
        assert "未知(按A股规则需在2026年4月30日" not in md
