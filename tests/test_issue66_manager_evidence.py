# -*- coding: utf-8 -*-
"""Issue #66 A：终稿输入有界完整 + 已完成研究不被经理重写覆盖。

A1：超长业务/新闻证据、维度缺失/降级组合下，真实 _call_wave 出站消息始终是
    完整合法 JSON；每个待成文维度保留状态/时点/核心字段/叙述；参考资料摘录
    单独限量且省略显式标注；无后部维度被挤掉。
A2：fake 经理返回旧技术/资金段落、旧标题、旧小计时，资格链强制复用已完成
    研究（正文=研究员叙述、标题=规则标题、小计=六维框架规则摘要），并经真实
    编排→终稿→保存→API/SSE→Markdown/PDF 垂直链验证不覆盖当前研究。

边界：参考资料摘录本身是合法留存证据（渲染层带免责说明继续展示）；本票修复
的是「旧摘录挤掉当前事实 + 经理重写覆盖已完成研究」，不承诺消灭模型任意
事实错误。全程离线（fake LLM / 无网络 / 零真实供应商）。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from tests.test_choice_integration import choice_transport  # noqa: F401
from tests.test_issue64_no_target_mode import _DeterministicResearchAdapter

from src.deep_research_dims.manager_writeup import (
    DIM_SECTION_KEYS,
    MANAGER_DIM_SECTION_KEYS,
    MANAGER_SECTION_KEYS,
    _bounded_dim_payload,
    _call_wave,
    generate_manager_writeup,
)
from src.deep_research_dims.render import build_view, render_markdown
from src.schemas.deep_research_dims import (
    ACTIVE_DIM_IDS,
    DIM_MODELS,
    RetainedEvidence,
    RetainedFact,
)
from src.schemas.research_framework import (
    DimensionScore,
    IndicatorScore,
    ResearchFramework,
)


def _base_dims() -> dict:
    return {d: DIM_MODELS[d]() for d in ACTIVE_DIM_IDS}


class _MessageCaptureLLM:
    """按 system 内容区分波次返回 JSON，并捕获全部出站消息。"""

    def __init__(self, payloads: Dict[str, Dict[str, Any]]):
        self.payloads = payloads
        self.user_messages: List[str] = []

    def call_text(self, messages, **kwargs):
        system = messages[0]["content"]
        self.user_messages.append(messages[1]["content"])
        if '"title_angle"' in system:
            key = "A"
        elif '"thesis_points"' in system:
            key = "B"
        else:
            key = "C"
        return type(
            "R", (), {"content": json.dumps(self.payloads[key], ensure_ascii=False)}
        )()


def _minimal_wave_payloads() -> Dict[str, Dict[str, Any]]:
    return {
        "A": {
            "title_angle": "资金与技术的同源观察",
            "executive_summary": "定性判断与成立条件与跟踪清单。" * 10,
            "section_titles": {k: f"判断句标题{k}" for k in MANAGER_SECTION_KEYS},
            "dim_verdicts": {
                k: f"{k}定性"
                for k in ("基本面", "消息面", "资金面", "情绪面", "技术面", "宏观")
            },
        },
        "B": {
            "section_narratives": {
                "investment": "四段式论述" * 30,
                "market": "四段式论述" * 30,
                "worldview": "四段式论述" * 30,
                "valuation": "四段式论述" * 30,
            },
            "thesis_points": [
                {"header": "判断句论点", "body": "第一句含数字。" + "展开论述。" * 12}
            ],
            "risk_matrix": [{"risk": "风险", "impact": "影响路径", "trigger": "事实验证条件"}],
            "money_paths": [
                {"name": "财报验证驱动", "win_rate": "约50%", "playbook": "研究验证事件与依据。" * 4}
            ],
            "scenario_triggers": {"optimistic": "条件一", "neutral": "条件二", "pessimistic": "条件三"},
        },
        "C": {
            "section_narratives": {
                k: "维度论述" * 30 for k in MANAGER_DIM_SECTION_KEYS
            },
        },
    }


def _oversized_dims() -> dict:
    """生产形态的超长资料：前部业务/情报携带大量参考资料摘录与结构化明细，
    资金/技术携带当前窗口事实（历史缺陷中被整体截掉的后部维度）。"""
    from src.schemas.report_schema import DataPerspective, PricePosition

    dims = _base_dims()
    old_news = [
        RetainedEvidence(
            tool="search_comprehensive_intel",
            title=f"旧新闻资料{i}",
            url=f"https://example.com/old/{i}",
            source="example.com",
            snippet=(
                "2026-08-07 新闻：参考成本价 37.31 元、支撑位 36.69 元。" + "背景叙述。" * 80
            ),
            published_date="2026-08-07",
        )
        for i in range(9)
    ]
    dims["data"] = dims["data"].model_copy(update={
        "narrative": "现价 26.71 元，MA20 29.55 元，量比温和，日线截至 2026-10-29。",
        "perspective": DataPerspective(price_position=PricePosition(current_price=26.71)),
    })
    dims["business"] = dims["business"].model_copy(update={
        "narrative": "公司深耕光通信涂覆材料，中端市场份额稳固，产品线向半导体材料延伸。",
        "main_products": {"value": "光纤涂覆材料与电子化学品"},
        "product_segments": {
            "source": "定期报告",
            "coverage": "2026 中报",
            "items": [
                {
                    "name": f"产品线{i}",
                    "revenue": 1000.0 + i,
                    "cost": 600.0 + i,
                    "gross_profit": 400.0 + i,
                    "gross_margin": 0.4,
                    "yoy_pct": 12.0,
                    "share": f"{i * 3}%",
                    "trend": "平稳",
                    "segment_note": f"产品线{i}叙述性说明",
                    # 身份键刻意放在晚位（key9-20）：核心保留不得依赖键顺序
                    "unit": "万元",
                    "currency": "CNY",
                    "source_family": "filings",
                    "qualification": "single_family；未独立核验",
                    "is_stale": False,
                    "statement_scope": "consolidated",
                    "adjustment": "none",
                    "disclosed_at": "2026-08-26",
                    "normalization_error": "",
                    "rule_eligible": i % 3 != 0,
                    "period_basis": "reporting_period",
                }
                for i in range(12)
            ],
            "errors": [],
        },
        "retained_evidence": list(old_news),
    })
    dims["intel"] = dims["intel"].model_copy(update={
        "narrative": "消息面根因为订单回暖，机制是下游补库；风险集中在原料价格。",
        "event_calendar": [
            {
                "event": f"验证事件{i}",
                "date": f"2026-11-{i + 1:02d}",
                "outcomes": ["超预期", "符合预期"],
                "plans": ["跟踪披露数据"],
            }
            for i in range(6)
        ],
        "retained_evidence": list(old_news[:8]),
    })
    dims["capital"] = dims["capital"].model_copy(update={
        "narrative": "主力资金单日净流出1546.53万元，5日净流入3766.50万元，10日净流出3163.53万元。",
        "flow_summary": "主力资金近端分歧，单日流出、5日回流。",
        "as_of_date": "2026-10-29",
        "expected_date": "2026-10-29",
        "flow_score": 55,
        "retained_facts": [
            RetainedFact(
                tool="get_capital_flow",
                label="单日主力净流入",
                value="-15465286.0",
                source="choice",
                period="2026-10-29",
                unit="CNY",
                qualification="single_source",
            ),
            RetainedFact(
                tool="get_capital_flow",
                label="5交易日主力净流入",
                value="37665000.0",
                source="choice",
                period="2026-10-29",
                unit="CNY",
                qualification="single_source",
            ),
            RetainedFact(
                tool="get_capital_flow",
                label="10交易日主力净流入",
                value="-31635300.0",
                source="choice",
                period="2026-10-29",
                unit="CNY",
                qualification="single_source",
            ),
        ],
    })
    dims["technical"] = dims["technical"].model_copy(update={
        "narrative": "现价 26.71 元，MA20 29.55 元，RSI6 38.33，日线截至 2026-10-29。",
        "indicator_summary": "RSI6 中性偏弱，短均线空头排列。",
        "support": 25.8,
        "resistance": 29.55,
        "score": 45,
        "retained_facts": [
            RetainedFact(
                tool="get_realtime_quote",
                label="价格",
                value="26.71",
                source="choice",
                period="2026-10-29",
                unit="CNY",
            ),
            RetainedFact(
                tool="analyze_trend",
                label="MA20",
                value="29.55",
                source="compute",
                period="2026-10-29",
                unit="CNY",
            ),
            # 零值事实 + False 资格：不得被真值过滤丢弃（issue #66 反例）
            RetainedFact(
                tool="get_capital_flow",
                label="北向资金净流入",
                value="0",
                source="choice",
                period="2026-10-29",
                unit="CNY",
                qualification="single_source；资格不合格",
            ),
        ],
    })
    return dims


def _assert_wave_message_contract(message: str, *, narrated_keys: List[str]) -> Dict[str, Any]:
    """出站消息必须是完整合法 JSON；每个待成文维度保留核心事实。

    无后部维度被挤掉的保证来自构造（无全局截断、每维度独立有界），不设单维
    字符预算断言。
    """
    facts = json.loads(message)  # 截断型缺陷在此直接失败
    dims = facts.get("dims") or {}
    for key in narrated_keys:
        assert key in dims, f"待成文维度 {key} 被挤掉"
        payload = dims[key]
        assert payload.get("status"), f"{key} 状态缺失"
        assert payload.get("as_of"), f"{key} 时点缺失"
        assert "narrative" in payload, f"{key} 叙述缺失"
    return facts


class TestWaveMessageBounded:
    def test_wave_messages_are_complete_json_with_late_dims(self) -> None:
        """超长业务/情报资料不再挤掉后部维度；资金/技术事实进入波 A/B 摘要。

        绑定与叙述现价锚一致的共享行情（issue #68）：出站 market 摘要叙述
        在真实绑定报价下照常携带。
        """
        from src.core.research_run import ResearchRun
        from src.deep_research_dims.context import SharedContext

        dims = _oversized_dims()
        llm = _MessageCaptureLLM(_minimal_wave_payloads())
        run = ResearchRun(600.0)
        run.context = SharedContext("300398", "飞凯材料", "2026-10-30T15:00:00", quote={"price": 26.71})
        with run.bind():
            out = generate_manager_writeup(llm, "飞凯材料", "300398", dims, [])
        assert len(llm.user_messages) == 3

        wave_a = json.loads(llm.user_messages[0])
        digest = wave_a["completed_research"]
        assert "26.71" in json.dumps(digest["technical"], ensure_ascii=False)
        assert digest["capital"]["as_of_date"] == "2026-10-29"
        assert "narrative" in digest["market"], "盘面核心事实注入波 A"

        wave_b = json.loads(llm.user_messages[1])
        assert "26.71" in json.dumps(wave_b["completed_research"]["technical"], ensure_ascii=False)
        assert "1546.53" in wave_b["completed_research"]["capital"]["narrative"]
        _assert_wave_message_contract(
            llm.user_messages[1],
            narrated_keys=["signal", "conclusion", "plan", "scenarios", "bayesian", "data", "intel"],
        )

        wave_c = json.loads(llm.user_messages[2])
        facts = _assert_wave_message_contract(
            llm.user_messages[2],
            narrated_keys=list(MANAGER_DIM_SECTION_KEYS),
        )
        # 受保护维度不再要求经理重写
        assert "capital" not in facts["dims"] and "technical" not in facts["dims"]
        # 超长参考资料单独限量并显式省略
        business = facts["dims"]["business"]
        assert len(business.get("retained_evidence") or []) <= 3
        assert "retained_evidence" in business.get("_omissions", {})
        assert any("省略" in note for note in business["_omissions"].values())
        intel = facts["dims"]["intel"]
        assert len(intel.get("retained_evidence") or []) <= 3
        assert out["section_narratives"]["technical"] == dims["technical"].narrative

    def test_bounded_payload_always_valid_json_for_missing_and_degraded_combos(self) -> None:
        dims = _oversized_dims()
        del dims["us_china"]  # 维度整体缺失
        dims["business"] = dims["business"].model_copy(update={"status": "skipped"})
        dims["sentiment"] = dims["sentiment"].model_copy(
            update={"status": "degraded", "degraded_reason": "搜索源不可用"}
        )
        llm = _MessageCaptureLLM(_minimal_wave_payloads())
        generate_manager_writeup(llm, "飞凯材料", "300398", dims, [])
        wave_b_keys = ["signal", "conclusion", "plan", "scenarios", "bayesian", "data", "intel"]
        wave_c_keys = [k for k in MANAGER_DIM_SECTION_KEYS if k in dims]
        _assert_wave_message_contract(llm.user_messages[1], narrated_keys=wave_b_keys)
        facts = _assert_wave_message_contract(llm.user_messages[2], narrated_keys=wave_c_keys)
        assert facts["dims"]["business"]["status"] == "skipped"
        assert facts["dims"]["sentiment"]["degraded_reason"] == "搜索源不可用"

    def test_bounded_payload_keeps_narrative_for_every_dim(self) -> None:
        """每个维度载荷独立有界（字符串限量/列表限量/事实限量），叙述都保留。"""
        for key in ("business", "intel", "technical", "capital"):
            dim = _oversized_dims()[key]
            payload = _bounded_dim_payload(key, dim)
            assert payload["narrative"], "叙述在任何维度都保留"

    @pytest.mark.parametrize("reorder", ["identity", "reversed"])
    def test_late_retained_fact_records_survive_reordering(self, reorder: str) -> None:
        """晚位事实记录（quote/history 之后、第 12 条之后的支撑/阻力等）整体
        保留，不按条数或顺序剪枝；A/B 摘要与波 C 载荷都携带完整记录
        （真实技术维度 18+ 条的反例形态；旧 first-N 实现会丢第 13 条后记录）。"""
        dims = _oversized_dims()
        # 构造 20 条合法事实：支撑/阻力（含来源/时点/资格）位于第 13-14 条
        records = [
            RetainedFact(
                tool="get_daily_history",
                label=f"最近交易日记录{i}",
                value=str(20.0 + i * 0.1),
                source="choice",
                period="2026-10-29",
                unit="CNY",
            )
            for i in range(18)
        ]
        records[12:12] = [
            RetainedFact(
                tool="support_resistance", label="支撑", value="25.80",
                source="compute", period="2026-10-29", unit="CNY",
                qualification="engine computed；未独立核验",
            ),
            RetainedFact(
                tool="support_resistance", label="阻力", value="29.55",
                source="compute", period="2026-10-29", unit="CNY",
                qualification="engine computed；未独立核验",
            ),
        ]
        if reorder == "reversed":
            records = list(reversed(records))  # 支撑/阻力被重排到前部/中部之外
        dims["technical"] = dims["technical"].model_copy(update={
            "retained_facts": records,
        })
        assert len(records) == 20

        llm = _MessageCaptureLLM(_minimal_wave_payloads())
        generate_manager_writeup(llm, "飞凯材料", "300398", dims, [])
        wave_a = json.loads(llm.user_messages[0])
        wave_b = json.loads(llm.user_messages[1])
        wave_c = json.loads(llm.user_messages[2])

        digest_facts = wave_a["completed_research"]["technical"]["retained_facts"]
        expected = [json.loads(fact.model_dump_json()) for fact in records]
        # A/B 摘要与输入完全相等（不按条数/顺序丢记录；technical 受保护，
        # 波 C 不再携带其载荷——事实经由 A/B 摘要到达）
        assert digest_facts == expected, "波 A 摘要丢失晚位事实记录"
        assert wave_b["completed_research"]["technical"]["retained_facts"] == expected, (
            "波 B 摘要丢失晚位事实记录"
        )
        assert wave_c["dims"].get("technical") is None
        support = next(fact for fact in digest_facts if fact["label"] == "支撑")
        assert support["qualification"] == "engine computed；未独立核验"

    def test_nested_core_identity_survives_regardless_of_key_order(self) -> None:
        """晚位身份键（unit/currency/source_family/qualification 等）与零值/False
        事实不被前缀剪枝或真值过滤丢弃（issue #66 反例）。"""
        dims = _oversized_dims()
        llm = _MessageCaptureLLM(_minimal_wave_payloads())
        generate_manager_writeup(llm, "飞凯材料", "300398", dims, [])
        wave_c = json.loads(llm.user_messages[2])
        items = wave_c["dims"]["business"]["product_segments"]["items"]
        first = items[0]
        for late_key in (
            "unit", "currency", "source_family", "qualification", "is_stale",
            "statement_scope", "adjustment", "disclosed_at", "rule_eligible",
            "period_basis",
        ):
            assert late_key in first, f"晚位核心键 {late_key} 被键序剪枝丢弃"
        assert first["unit"] == "万元" and first["currency"] == "CNY"
        assert first["source_family"] == "filings"
        assert first["is_stale"] is False, "False 值必须原样保留"
        # 零值事实连同资格文本一起保留（真值过滤会把它丢掉）
        wave_a = json.loads(llm.user_messages[0])
        tech_facts = wave_a["completed_research"]["technical"]["retained_facts"]
        zero_fact = next(fact for fact in tech_facts if fact["label"] == "北向资金净流入")
        assert zero_fact["value"] == "0"
        assert zero_fact["qualification"] == "single_source；资格不合格"
        # 波 C 维度载荷同样携带完整身份键
        c_tech_facts = wave_c.get("dims", {}).get("technical", {}).get("retained_facts")
        assert c_tech_facts is None or all(
            {"tool", "label", "value", "source", "period", "unit", "qualification"} <= set(fact)
            for fact in c_tech_facts
        )


# ---------------------------------------------------------------------------
# A2：fake 经理旧内容不得覆盖已完成研究（资格链 + 渲染）
# ---------------------------------------------------------------------------


_STALE_TECH_PARAGRAPH = (
    "技术面尚不能给出趋势确认：唯一可用行情来自 2026-08-07 新闻"
    "（参考成本价 37.31 元、支撑位 36.69 元），当前现价与最新K线缺失。"
)
_STALE_CAPITAL_PARAGRAPH = (
    "资金面数据缺失，无法判断主力资金方向；资料仅指向经营现金流改善，"
    "市场主力资金三窗口均未取得。"
)


def _stale_wave_payloads() -> Dict[str, Dict[str, Any]]:
    """fake 波 A 摘要按已完成事实撰写（注入验证见 A1 用例）；标题/小计/波 C
    段落故意返回旧内容——这三类表面由本票确定性保护。"""
    payloads = _minimal_wave_payloads()
    payloads["A"] = {
        **payloads["A"],
        "section_titles": {
            **payloads["A"]["section_titles"],
            "technical": "缺少最新行情",
            "capital": "资金数据缺失",
        },
        "dim_verdicts": {
            **payloads["A"]["dim_verdicts"],
            "技术面": "缺数据",
            "资金面": "缺数据",
        },
    }
    payloads["C"] = {
        "section_narratives": {
            **payloads["C"]["section_narratives"],
            "technical": _STALE_TECH_PARAGRAPH,
            "capital": _STALE_CAPITAL_PARAGRAPH,
        },
    }
    return payloads


class TestStaleManagerCannotOverrideCompletedResearch:
    def test_qualification_forces_protected_sections_from_research(self) -> None:
        dims = _oversized_dims()
        dims["six_dim"] = dims["six_dim"].model_copy(update={
            "framework": ResearchFramework(
                dimension_total=62.0,
                dimensions=[
                    DimensionScore(
                        dimension="技术面",
                        weight=0.2,
                        score=45.0,
                        indicators=[
                            IndicatorScore(
                                name="趋势结构",
                                score=45.0,
                                weight=1.0,
                                summary="现价26.71元位于MA20下方，RSI6中性偏弱",
                            )
                        ],
                    ),
                    DimensionScore(
                        dimension="资金面",
                        weight=0.15,
                        score=55.0,
                        indicators=[
                            IndicatorScore(
                                name="主力资金",
                                score=55.0,
                                weight=1.0,
                                summary="单日净流出、5日回流，近端分歧",
                            )
                        ],
                    ),
                ],
            ),
        })
        llm = _MessageCaptureLLM(_stale_wave_payloads())
        manager = generate_manager_writeup(llm, "飞凯材料", "300398", dims, [])
        # 正文：直接复用研究员叙述
        assert manager["section_narratives"]["technical"] == dims["technical"].narrative
        assert manager["section_narratives"]["capital"] == dims["capital"].narrative
        # 标题/小计：规则与框架来源
        assert manager["section_titles"]["technical"] == DIM_SECTION_KEYS["technical"]
        assert manager["section_titles"]["capital"] == DIM_SECTION_KEYS["capital"]
        assert manager["dim_verdicts"]["技术面"] == "现价26.71元位于MA20下方，RSI6中性偏弱"
        assert manager["dim_verdicts"]["资金面"] == "单日净流出、5日回流，近端分歧"
        # 经理所辖维度（业务）叙述照常采用经理成文，不被受保护逻辑误伤
        assert manager["section_narratives"]["business"].startswith("维度论述")

    def test_protected_sections_render_research_not_stale_prose(self) -> None:
        dims = _oversized_dims()
        llm = _MessageCaptureLLM(_stale_wave_payloads())
        manager = generate_manager_writeup(llm, "飞凯材料", "300398", dims, [])
        view = build_view("飞凯材料", "300398", "2026-10-30", [], dims, [], manager=manager)
        markdown = render_markdown(view)
        assert _STALE_TECH_PARAGRAPH not in markdown
        assert _STALE_CAPITAL_PARAGRAPH not in markdown
        assert "缺少最新行情" not in markdown and "资金数据缺失" not in markdown
        assert "缺数据" not in markdown
        # 渲染保留当前技术叙述与资金三窗口叙述；旧新闻摘录作为留存证据仍在
        # 业务节附录中展示（免责说明保留，属合法留存，非重写覆盖）。
        assert dims["technical"].narrative in markdown
        assert "1546.53" in markdown
        assert "已取得的参考资料" in markdown
        # 受保护节标题为单标题（规则标题与节名相同时不重复并列），
        # dim 标记切分与子报告链接不受影响。
        assert markdown.count("### 资金面分析") == 1
        assert markdown.count("### 技术面分析") == 1
        assert "资金面分析｜资金面分析" not in markdown
        assert "技术面分析｜技术面分析" not in markdown
        # 其他维度充分分析保留（经理标题照常渲染）
        assert "判断句标题business" in markdown

    def test_protected_without_research_stays_honest(self) -> None:
        """dim 非 ok 时强制不生效：真实缺失/degraded 不被改写成有效结论。"""
        dims = _base_dims()
        dims["capital"] = dims["capital"].model_copy(
            update={"status": "degraded", "degraded_reason": "资金源不可用", "narrative": ""}
        )
        llm = _MessageCaptureLLM(_stale_wave_payloads())
        manager = generate_manager_writeup(llm, "飞凯材料", "300398", dims, [])
        assert "capital" not in manager["section_narratives"]
        assert "capital" not in manager["section_titles"]
        view = build_view("飞凯材料", "300398", "2026-10-30", [], dims, [], manager=manager)
        markdown = render_markdown(view)
        assert _STALE_CAPITAL_PARAGRAPH not in markdown
        assert "资金源不可用" in markdown


# ---------------------------------------------------------------------------
# A2 垂直链：真实编排→终稿→保存→API/SSE→Markdown/PDF（复用 issue64 环境）
# ---------------------------------------------------------------------------


_TECHNICAL_RESEARCH = {
    "chanlun_summary": "日线一段上涨后的中枢震荡，当前位于中枢上沿附近。",
    "support": 19.0,
    "resistance": 22.5,
    "indicator_summary": "RSI6 中性，短均线粘合，量能温和。",
    "wave_note": "不适用",
    "score": 60,
    "basis": "现价 20.00 元附近震荡，结构给出支撑 19.0 元、阻力 22.5 元。",
    "narrative": (
        "现价 20.00 元，短均线走平，RSI6 中性；日线处于中枢震荡，"
        "支撑 19.0 元、阻力 22.5 元。缺口为周线级别趋势确认。"
    ),
}

_STALE_MANAGER_WAVE_A = {
    "title_angle": "旧闻支撑下的观望",
    "executive_summary": (
        "本研究在未取得可追溯目标价的口径下完成：基本面组件完整，"
        "消息面根因为订单回暖。结论为观察与条件跟踪，后续验证条件见各维度与作战计划。"
    ),
    "section_titles": {
        "investment": "行动保持观察的依据",
        "market": "订单回暖驱动的盘面结构",
        "worldview": "补库周期的三种走向",
        "valuation": "估值锚从哪里来",
        "business": "光通信涂覆的中端突围",
        "intel": "订单回暖与风险并存",
        "capital": "资金数据缺失",
        "technical": "缺少最新行情",
        "sentiment": "机构观点未取得",
        "us_china": "出口管制风险有限",
    },
    "dim_verdicts": {
        "基本面": "财务组件完整，健康度中等",
        "消息面": "订单回暖且风险可列举",
        "资金面": "缺数据",
        "情绪面": "机构观点未取得，如实披露",
        "技术面": "缺数据",
        "宏观": "未纳入本轮研究",
    },
}

_STALE_MANAGER_WAVE_B = {
    "section_narratives": {
        "investment": "研究在无目标价口径下完成，行动保持观察与条件跟踪。六维与贝叶斯结果仅作观察条件引用，不推升行动等级。后续以订单落地与资金连续性作为主要验证事实。",
        "market": "盘面结构由订单回暖驱动，机制为下游补库带动出货。量级与持续性以已披露口径为准。",
        "worldview": "第一种走向是订单持续落地带动出货上行；第二种是补库结束后回归平稳；第三种是原料成本上行压缩利润。三种走向均以已披露数据验证。",
        "valuation": "本报告未取得可追溯目标价，采用非估值分析：不给目标价、预期收益或估值驱动结论；估值相关区间全部留空。",
    },
    "thesis_points": [
        {"header": "无目标价的观察研究", "body": "本研究在未取得可追溯目标价的口径下完成，估值区间留空，行动保持观察与条件跟踪，不以高分推升行动等级，验证条件见作战计划。"},
        {"header": "资金近端持续流入", "body": "资金面显示主力资金近端持续流入，与消息面订单回暖的根因一致；连续性以已披露口径为准，单源数据未独立核验。"},
        {"header": "验证条件已经明确", "body": "后续以订单落地、成本走势与资金连续性为验证事实；任何走向切换都以已披露数据为准，不依赖假设推演。"},
    ],
    "risk_matrix": [
        {"risk": "上游原料价格波动", "impact": "成本上行压缩毛利", "trigger": "原料披露价格高于已披露区间"},
        {"risk": "客户集中度较高", "impact": "单一大客户波动放大业绩弹性", "trigger": "披露的客户结构出现变化"},
    ],
    "money_paths": [
        {"name": "财报验证驱动", "win_rate": "不提供", "playbook": "以披露的订单落地与成本数据验证判断，操作条件只引用规则作战计划。"}
    ],
    "scenario_triggers": {
        "optimistic": "订单持续落地且原料成本稳定",
        "neutral": "补库平稳、出货平稳",
        "pessimistic": "原料成本上行压缩利润",
    },
}

_STALE_MANAGER_WAVE_C = {
    "section_narratives": {
        "business": "公司深耕光通信涂覆材料的配方与工艺，中端市场地位稳固，产品线向半导体材料延伸，分产品收入结构未披露。",
        "intel": "消息面根因为订单回暖，机制是下游补库；风险集中在原料价格与客户集中度。年报披露为下一验证节点。",
        "technical": _STALE_TECH_PARAGRAPH,
        "capital": _STALE_CAPITAL_PARAGRAPH,
    },
}


class _StaleManagerAdapter(_DeterministicResearchAdapter):
    """终稿三波返回过期/矛盾内容；研究员消费沿用既有确定性完成数据。

    call_with_tools/call_text 原样继承；未预期的 LLM 消费沿用父类断言，
    防止静默旁路。
    """

    def __init__(self) -> None:
        self.manager_user_messages: List[str] = []

    def _explore_content(self, user: str) -> str:
        if "技术面深度分析" in user:
            return json.dumps(_TECHNICAL_RESEARCH, ensure_ascii=False)
        return super()._explore_content(user)

    def _text_content(self, system: str) -> str:
        if "主笔基金经理" in system:
            if '"title_angle"' in system:
                return json.dumps(_STALE_MANAGER_WAVE_A, ensure_ascii=False)
            if '"thesis_points"' in system:
                return json.dumps(_STALE_MANAGER_WAVE_B, ensure_ascii=False)
            return json.dumps(_STALE_MANAGER_WAVE_C, ensure_ascii=False)
        return super()._text_content(system)

    def call_text(self, messages, **kwargs):
        system = str(next((m.get("content") or "") for m in messages if m.get("role") == "system"))
        if "主笔基金经理" in system:
            self.manager_user_messages.append(
                str(next((m.get("content") or "") for m in messages if m.get("role") == "user"))
            )
        return super().call_text(messages, **kwargs)


def test_stale_manager_response_vertical_report_api_sse_md_pdf(
    tmp_path, monkeypatch, choice_transport,
) -> None:
    """fake 终稿返回旧技术/资金段落与旧标题/旧小计时，真实垂直链仍保持
    当前技术研究与资金三窗口；其他维度充分分析保留。"""
    import io

    from fastapi.testclient import TestClient
    from pypdf import PdfReader
    from src.services import deep_research_service as service_module
    from tests.test_issue64_no_target_mode import (
        _NO_TARGET_RESEARCH_DIMS,
        _assert_single_done_terminal,
        _no_target_vertical_env,
        _sse_events,
    )

    service, database, app, _transport = _no_target_vertical_env(
        tmp_path, monkeypatch, choice_transport,
    )
    adapter = _StaleManagerAdapter()
    monkeypatch.setattr(service_module, "_get_dual_track_adapter", lambda: adapter)
    try:
        with TestClient(app) as client:
            stream = client.post(
                "/generate/stream",
                json={
                    "stock_code": "300398", "stock_name": "飞凯材料",
                    "dims": sorted(set(_NO_TARGET_RESEARCH_DIMS) | {"technical"}),
                    "force_refresh": True,
                },
            )
            assert stream.status_code == 200, stream.text
            events = _sse_events(stream)
            done = _assert_single_done_terminal(events)
            rid = done["report_id"]
            assert done["status"] == "success", (
                done["status"], done.get("error"),
                {
                    key: (value.get("status"), value.get("degraded_reason"))
                    for key, value in (done.get("dimensions") or {}).items()
                    if value.get("status") not in (None, "ok")
                },
            )

            # 波 A 出站消息注入已完成研究（真实编排路径上的注入验证）
            assert adapter.manager_user_messages, "经理波 A 消息未被捕获"
            wave_a_facts = json.loads(adapter.manager_user_messages[0])
            digest = wave_a_facts["completed_research"]
            assert "20.00" in json.dumps(digest["technical"], ensure_ascii=False)
            assert digest["capital"]["status"] == "ok"

            markdown = done["markdown"]
            # 当前技术研究的叙述保持，旧段落/旧标题/旧小计不覆盖
            assert _STALE_TECH_PARAGRAPH not in markdown
            assert _STALE_CAPITAL_PARAGRAPH not in markdown
            assert "37.31" not in markdown and "36.69" not in markdown
            assert "缺少最新行情" not in markdown and "资金数据缺失" not in markdown
            assert "缺数据" not in markdown
            assert _TECHNICAL_RESEARCH["narrative"] in markdown, "当前技术叙述保留"
            assert "现价 20.00 元" in markdown
            # 经理所辖维度叙述照常渲染（其他维度充分分析保留）
            assert "光通信涂覆材料的配方与工艺" in markdown

            data = client.get(f"/reports/{rid}").json()["data"]
            assert data["markdown"] == markdown
            assert data["dimensions"]["technical"]["status"] == "ok"
            assert data["dimensions"]["technical"]["narrative"] == _TECHNICAL_RESEARCH["narrative"]

            # 真实惰性 PDF：旧内容不得进入导出
            pdf = client.get(f"/reports/{rid}/pdf")
            assert pdf.status_code == 200, pdf.text
            reader = PdfReader(io.BytesIO(pdf.content))
            pdf_text = "".join(page.extract_text() or "" for page in reader.pages)
            assert _STALE_TECH_PARAGRAPH not in pdf_text
            assert "37.31" not in pdf_text
            assert "现价 20.00 元" in pdf_text
    finally:
        database._engine.dispose()


def test_call_wave_sends_single_valid_json_user_message() -> None:
    """真实 _call_wave 出站入口：facts 整体序列化，不再有 [:5000] 硬截。"""
    captured: List[List[Dict[str, str]]] = []

    class _Adapter:
        def call_text(self, messages, **kwargs):
            captured.append(messages)
            return type("R", (), {"provider": "fixture", "finish_reason": "stop", "content": "{}"})()

    big_facts = {"stock": "飞凯材料（300398）", "blob": "长" * 20000}
    _call_wave(_Adapter(), "system", big_facts, wave="wave_probe")
    user = captured[0][1]["content"]
    assert len(user) > 5000, "有界组织后的消息不再被 5000 字符硬截"
    assert json.loads(user)["blob"].startswith("长")
