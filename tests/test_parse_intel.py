# -*- coding: utf-8 -*-
"""情报解析与事件表格契约测试（issue #40；issue #68 语义收敛）。

合理长度的根因/事件/结果/核验动作正文不得被解析层静默硬截；
未来事件以表格呈现，结果与核验动作数量不等时不逐项配对。
issue #68：事件预案（plans）槽位只允许研究核验/跟踪动作，独立交易指令
（含 T1 买卖编号、仓位/止损类）在采用边界拒绝并披露，不进入缓存与派生证据。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.agent.deep_research.orchestrator import _parse_intel
from src.deep_research_dims.render import _event_plans_view
from src.schemas.deep_research_dims import IntelDim


def _calendar_entry(
    event: str = "年度业绩预告",
    date: str = "2026-04-30",
    outcomes: Any = None,
    plans: Any = None,
) -> dict:
    return {
        "event": event,
        "date": date,
        "outcomes": ["超预期：全年净利上修"] if outcomes is None else outcomes,
        # issue #68：默认预案为研究核验/跟踪动作（合法形态）
        "plans": ["业绩预告披露后对照公告原文核验净利与毛利率阈值"] if plans is None else plans,
    }


def _intel(calendar: list[dict]) -> IntelDim:
    dim, _ = _parse_intel({"event_calendar": calendar}, 1)
    return dim


class TestParseIntelKeepsFullText:
    def test_root_cause_320_chars_not_truncated(self):
        text = "这是用于确认文本截断的完整句子。" * 20  # 320 字符
        dim, _ = _parse_intel(
            {"root_cause": {k: text for k in ("event", "mechanism", "magnitude", "persistence")}}, 1
        )
        assert dim.root_cause is not None
        assert dim.root_cause.event == text
        assert dim.root_cause.mechanism == text
        assert dim.root_cause.magnitude == text
        assert dim.root_cause.persistence == text

    def test_fifth_and_later_outcomes_plans_kept(self):
        outcomes = [f"第{i}种可能结果及其具体传导路径说明" for i in range(1, 7)]
        plans = [f"第{i}条核验动作：对照披露原文验证该结果的成立条件" for i in range(1, 7)]
        dim, _ = _parse_intel({"event_calendar": [_calendar_entry(outcomes=outcomes, plans=plans)]}, 1)
        assert len(dim.event_calendar[0].outcomes) == 6
        assert len(dim.event_calendar[0].plans) == 6

    def test_seventh_event_kept(self):
        calendar = [
            _calendar_entry(event=f"事件{i}", date="2026-05-01") for i in range(1, 8)
        ]
        dim, _ = _parse_intel({"event_calendar": calendar}, 1)
        assert len(dim.event_calendar) == 7

    def test_overflow_disclosed_in_truncation_notes(self):
        """审查反例：11 个事件、每项 9 个结果 → 丢弃数量随报告保存（truncation_notes）。"""
        calendar = [
            _calendar_entry(
                event=f"事件{i}",
                date="2026-05-01",
                outcomes=[f"结果{i}-{j}" for j in range(1, 10)],
                plans=[f"核验动作{i}-{j}" for j in range(1, 3)],
            )
            for i in range(1, 12)
        ]
        dim, _ = _parse_intel({"event_calendar": calendar}, 1)
        assert len(dim.event_calendar) == 10
        assert all(len(ev.outcomes) == 8 for ev in dim.event_calendar)
        notes = "；".join(dim.truncation_notes)
        assert "丢弃 1 项" in notes
        assert "可能结果超出 8 条上限，丢弃 1 条" in notes

    def test_extreme_length_truncation_disclosed(self):
        huge = "超" * 5000
        dim, _ = _parse_intel({"root_cause": {"event": huge}}, 1)
        assert dim.root_cause is not None
        assert len(dim.root_cause.event) < len(huge)
        assert "截断" in dim.root_cause.event

    def test_evidence_long_text_kept(self):
        evidence = "公告原文摘录：公司预计 2026 年前三季度实现归母净利润 12.3 亿元，" * 10
        dim, _ = _parse_intel(
            {"evidence_items": [{"evidence": evidence, "strength": "strong_positive", "lr": 2.0, "date": "2026-10-01"}]}, 1
        )
        assert dim.evidence_items and dim.evidence_items[0].evidence == evidence


class TestPlanDirectiveRejection:
    """issue #68：预案槽位的独立交易指令在采用边界拒绝并披露。"""

    def test_independent_trade_plans_rejected_and_disclosed(self):
        """冻结反例形态：T1 编号买入/仓位/止损指令不得进入采用载荷。"""
        dim = _intel(
            [
                _calendar_entry(
                    event="年度业绩预告",
                    outcomes=["净利超预期", "净利符合预期"],
                    plans=[
                        "T1-R 右侧确认买入：确认净利达标后于放量收复 36 元时建仓，仓位 ≤3%",
                        "T1-SL 止损：跌破 32 元无条件清仓离场（一票否决）",
                    ],
                )
            ]
        )
        assert dim.event_calendar[0].plans == []
        notes = "；".join(dim.truncation_notes)
        assert "未采用" in notes
        assert "研究核验/跟踪动作" in notes
        assert "规则作战计划" in notes
        # 被拒绝的指令数字不复制进任何采用字段
        assert "36 元" not in notes and "32 元" not in notes

    def test_plain_trade_word_plan_rejected(self):
        dim = _intel([_calendar_entry(plans=["回调加仓，跌破前低止损"])])
        assert dim.event_calendar[0].plans == []
        assert "未采用" in "；".join(dim.truncation_notes)

    def test_research_followup_plans_kept(self):
        """合规研究核验/跟踪动作与财务阈值条件保留。"""
        plans = [
            "披露后对照公告原文核验净利 ≥5.5 亿元且毛利率 ≥40% 的阈值",
            "跟踪原料价格披露并复核涨价假设，操作条件以本报告规则作战计划为准",
        ]
        dim = _intel([_calendar_entry(plans=list(plans))])
        assert dim.event_calendar[0].plans == plans
        assert dim.truncation_notes == []

    def test_outcomes_and_evidence_free_text_not_touched(self):
        """事件/结果/证据属自由叙述：合法研报引用（增持/目标价）不得被无差别删除。"""
        dim, _ = _parse_intel(
            {
                "event_calendar": [
                    _calendar_entry(
                        outcomes=["券商维持增持评级且目标价 29.4 元获得市场认可", "目标价下调至现价下方"],
                        plans=["对照研报原文核验目标价依据"],
                    )
                ],
                "evidence_items": [
                    {"evidence": "华泰证券维持增持评级，目标价 29.4 元（news 级二手）", "strength": "weak_positive", "lr": 1.2, "date": "2026-10-30"}
                ],
            },
            1,
        )
        assert "增持评级" in dim.event_calendar[0].outcomes[0]
        assert dim.event_calendar[0].plans == ["对照研报原文核验目标价依据"]
        assert "增持" in dim.evidence_items[0].evidence
        assert dim.truncation_notes == []

    def test_mixed_plans_keep_compliant_and_reject_directive(self):
        dim = _intel(
            [
                _calendar_entry(
                    outcomes=["投产落地", "投产延期"],
                    plans=["公告确认投产后核验客户认证进展", "跌势结束后的 ≤2% 试探建仓", "跟踪产能利用率披露"],
                )
            ]
        )
        assert dim.event_calendar[0].plans == [
            "公告确认投产后核验客户认证进展",
            "跟踪产能利用率披露",
        ]
        notes = "；".join(dim.truncation_notes)
        assert "第 2 条预案含独立交易指令" in notes

    def test_rejection_count_matches_position_not_index(self):
        """披露中的条目序号按原始位置计，拒绝多条时各自可见。"""
        dim = _intel(
            [
                _calendar_entry(
                    outcomes=["A", "B", "C"],
                    plans=["核验一", "止损离场", "核验二", "36 元挂单买入"],
                )
            ]
        )
        notes = "；".join(dim.truncation_notes)
        assert "第 2 条预案" in notes and "第 4 条预案" in notes
        assert len(dim.event_calendar[0].plans) == 2

    def test_quoted_rating_reference_is_research_followup(self):
        """root 反例：引号内评级引用（维持“买入”评级）是合规核验动作。"""
        quoted = "核验券商维持“买入”评级、目标价29.4元的研报原文"
        dim = _intel([_calendar_entry(plans=[quoted])])
        assert dim.event_calendar[0].plans == [quoted]
        assert dim.truncation_notes == []

    def test_research_prefix_does_not_excuse_independent_trade(self):
        """root 反例：「核验」/「若…则」前缀不豁免独立交易指令。"""
        dim = _intel(
            [
                _calendar_entry(
                    plans=["核验财报后在36元买入，仓位3%", "若业绩超预期则增持至3%"]
                )
            ]
        )
        assert dim.event_calendar[0].plans == []
        notes = "；".join(dim.truncation_notes)
        assert notes.count("未采用") == 2

    def test_quoted_number_directive_not_excused(self):
        """root 反例：引号包裹数字指令（执行“36元买入、仓位3%”）不豁免。"""
        dim = _intel([_calendar_entry(plans=["若超预期，执行“36元买入、仓位3%”"])])
        assert dim.event_calendar[0].plans == []
        assert "未采用" in "；".join(dim.truncation_notes)

    def test_quoted_non_rating_command_not_excused_even_without_numbers(self):
        """协调者简洁性决定：引号内非评级交易命令（无数字）同样拒绝。"""
        dim = _intel([_calendar_entry(plans=["若兑现不及预期，执行“止损离场”"])])
        assert dim.event_calendar[0].plans == []
        assert "未采用" in "；".join(dim.truncation_notes)

    def test_bare_rating_quote_without_context_not_excused(self):
        """root 反例：引号内容是评级标签但无评级语境（则“买入”）仍是指令。"""
        dim = _intel([_calendar_entry(plans=["若业绩超预期则“买入”"])])
        assert dim.event_calendar[0].plans == []
        assert "未采用" in "；".join(dim.truncation_notes)

    def test_rating_quote_exception_requires_adjacent_context(self):
        """例外仅在紧邻评级语境生效：维持“买入”评级保留，脱离语境拒绝。"""
        kept = "核验券商维持“买入”评级、目标价29.4元的研报原文"
        dim = _intel(
            [
                _calendar_entry(
                    outcomes=["评级维持", "评级下调"],
                    plans=[kept, "若业绩超预期则“买入”"],
                )
            ]
        )
        assert dim.event_calendar[0].plans == [kept]
        notes = "；".join(dim.truncation_notes)
        assert "第 2 条预案含独立交易指令" in notes

    def test_rating_citation_independent_of_quote_punctuation(self):
        """spec 反例：无引号/ASCII 引号/弯引号评级引用语义一致，均保留。"""
        variants = [
            "核验券商维持买入评级、目标价29.4元的研报原文",
            '核验券商维持"买入"评级、目标价29.4元的研报原文',
            "核验券商维持“买入”评级、目标价29.4元的研报原文",
        ]
        for plan in variants:
            dim = _intel([_calendar_entry(plans=[plan])])
            assert dim.event_calendar[0].plans == [plan], plan
            assert dim.truncation_notes == [], plan

    def test_ascii_quotes_do_not_excuse_commands_or_bare_labels(self):
        """spec 反例：ASCII 引号不豁免指令（执行"36元买入、仓位3%"）与
        无评级语境的裸标签（若业绩超预期则"买入"）。"""
        kept = '核验券商维持"买入"评级、目标价29.4元的研报原文'
        dim = _intel(
            [
                _calendar_entry(
                    plans=[kept, '执行"36元买入、仓位3%"', '若业绩超预期则"买入"']
                )
            ]
        )
        assert dim.event_calendar[0].plans == [kept]
        notes = "；".join(dim.truncation_notes)
        assert notes.count("未采用") == 2

    def test_unquoted_rating_evidence_survives_to_bayesian(self):
        """standards 反例：无引号「券商维持买入评级，目标价29.4元」证据
        在贝叶斯消费前不被删除。"""
        evidence = "券商维持买入评级，目标价29.4元（news级二手）"
        dim, _ = _parse_intel(
            {
                "evidence_items": [
                    {"evidence": evidence, "strength": "weak_positive", "lr": 1.2, "date": "2026-10-30"}
                ]
            },
            1,
        )
        assert [item.evidence for item in dim.evidence_items] == [evidence]
        assert dim.truncation_notes == []

    def test_rating_citation_does_not_excuse_directive_elsewhere(self):
        """豁免只覆盖紧邻评级语境的标签本身；同一条目其他位置的指令仍拒绝。"""
        dim = _intel(
            [_calendar_entry(plans=["核验维持买入评级研报，然后在36元买入、仓位3%"])]
        )
        assert dim.event_calendar[0].plans == []
        assert "未采用" in "；".join(dim.truncation_notes)

    def test_quoted_factual_spans_still_preserved(self):
        """引号内事实性短语（非评级标签、非指令）不受影响。"""
        plans = ["预告披露后复核“业绩兑现—估值扩张”链条是否延续", "跟踪“部分产品价格上涨”的持续性披露"]
        dim = _intel([_calendar_entry(plans=list(plans))])
        assert dim.event_calendar[0].plans == plans

    def test_shareholder_increase_tracking_preserved(self):
        """root 反例：「核对股东增持公告」是股东行为跟踪，不是交易指令。"""
        plans = ["核对股东增持公告", "核验券商维持增持评级、目标价29.4元的研报原文"]
        dim = _intel([_calendar_entry(plans=list(plans))])
        assert dim.event_calendar[0].plans == plans

    def test_directive_in_outcomes_rejected_not_adopted(self):
        """root 反例：outcomes 夹带「36元买入、仓位3%」不进入采用载荷。"""
        trade = "业绩超预期后36元买入，仓位3%"
        dim = _intel(
            [
                _calendar_entry(
                    outcomes=[trade, "净利润5.5亿元"],
                    plans=["核验公告原文"],
                )
            ]
        )
        assert trade not in dim.event_calendar[0].outcomes
        assert dim.event_calendar[0].outcomes == ["净利润5.5亿元"]
        assert any("可能结果含独立交易指令" in note for note in dim.truncation_notes)

    def test_directive_in_evidence_rejected_before_bayesian(self):
        """root 反例：evidence_items 夹带指令在贝叶斯消费前拒绝并披露。"""
        trade = "业绩超预期后36元买入，仓位3%"
        dim, _ = _parse_intel(
            {
                "event_calendar": [_calendar_entry(plans=["核验公告原文"])],
                "evidence_items": [
                    {"evidence": trade, "strength": "weak_positive", "lr": 1.3, "date": "2026-10-30"},
                    {"evidence": "券商维持增持评级，目标价29.4元（news 级二手）", "strength": "weak_positive", "lr": 1.2, "date": "2026-10-30"},
                ],
            },
            1,
        )
        assert [e.evidence for e in dim.evidence_items] == [
            "券商维持增持评级，目标价29.4元（news 级二手）"
        ]
        assert any("不进入贝叶斯证据链" in note for note in dim.truncation_notes)

    def test_directive_in_event_title_rejects_whole_event(self):
        """root 反例：事件标题夹带指令时整个事件不采用，不清洗标题伪装合规。"""
        bad_event = "业绩超预期后36元买入，仓位3%"
        dim = _intel(
            [
                {"event": bad_event, "date": "未知", "outcomes": ["净利润5.5亿元"], "plans": ["核验公告原文"]},
                _calendar_entry(event="年度业绩预告"),
            ]
        )
        assert [ev.event for ev in dim.event_calendar] == ["年度业绩预告"]
        assert any("标题含独立交易指令" in note for note in dim.truncation_notes)

    def test_legitimate_event_titles_preserved(self):
        """披露/解禁/会议等合法事件标题不受指令检测影响。"""
        titles = [
            "2026 年度业绩预告 / 年报预约披露（净利润同比变动超 50% 触发预告披露）",
            "控股股东股份变动与监管合规事件（历史处罚记录存在，新增检索为空属数据缺失）",
        ]
        dim = _intel([_calendar_entry(event=title) for title in titles])
        assert [ev.event[:12] for ev in dim.event_calendar] == [title[:12] for title in titles]
        assert dim.truncation_notes == []

    def test_gr11_requires_followup_only_when_completely_missing(self):
        """GR11 语义对齐：核验动作少于结果数是正常形态，缺失才提示。"""
        dim = _intel(
            [_calendar_entry(outcomes=["A", "B", "C"], plans=["核验公告原文"])]
        )
        payload = dim.model_dump()
        from src.deep_research_dims.guardrail import rule11_event_plan_completeness

        assert rule11_event_plan_completeness(payload) == []
        empty = _intel(
            [_calendar_entry(outcomes=["A", "B"], plans=[])]
        )
        events = rule11_event_plan_completeness(empty.model_dump())
        assert len(events) == 1
        assert "无研究核验/跟踪动作" in events[0].reason


class TestSlotShapeNormalization:
    """issue #68 真实报告反例（2026-10-30 飞凯材料授权实跑证据）：
    outcomes/plans 槽位以单个完整字符串返回，旧实现按字符拆成条目并伪造
    「超出 8 条上限，丢弃 33 条」披露。归一化契约：单个字符串是一条完整
    条目；字符串数组保持原序与既有上限语义；缺失/空一致；错误容器/标量
    与非字符串成员可见拒绝——不清洗成研究内容、不静默消失、不拖垮整个研究。
    """

    # 2026-10-30 飞凯材料真实响应中的完整 plans 字符串（按事件顺序）
    _OBSERVED_STRING_PLANS = [
        "对照巨潮资讯公告原文核验预告中的营收/净利润区间与变动原因，并与三季报趋势交叉验证",
        "跟踪披露后逐项核验分业务收入、毛利率、一次性损益明细（以定期报告原文为准）",
        "跟踪公司公告及投资者关系活动记录（primary）确认量产时点与产能数据；核验此前试生产报道的真实性",
        "跟踪行业协会/权威产业报告及监管公告，量化对光通信涂料/半导体材料订单的影响",
    ]

    def test_observed_single_string_plans_kept_whole(self):
        """真实响应重放：四个事件的 plans 均为单个完整字符串，各自完整保留为
        一条，不按字符拆散，也不伪造超限丢弃披露。"""
        calendar = [
            _calendar_entry(event=f"事件{i}", outcomes=[f"结果{i}"], plans=plan)
            for i, plan in enumerate(self._OBSERVED_STRING_PLANS, 1)
        ]
        dim, _ = _parse_intel({"event_calendar": calendar}, 1)
        assert [ev.plans for ev in dim.event_calendar] == [
            [plan] for plan in self._OBSERVED_STRING_PLANS
        ]
        assert dim.truncation_notes == []

    def test_observed_outcomes_list_and_string_plans_coexist(self):
        """真实报告形态：outcomes 为正常字符串数组、plans 为单字符串，二者并存。"""
        dim = _intel(
            [
                _calendar_entry(
                    event="2026 年年度业绩预告或快报",
                    date="未知（通常次年 1 月底前）",
                    outcomes=[
                        "净利润同比大幅增长（半导体/光通信材料需求放量）",
                        "净利润同比小幅增长或持平",
                        "净利润同比下滑",
                    ],
                    plans=self._OBSERVED_STRING_PLANS[0],
                )
            ]
        )
        assert len(dim.event_calendar[0].outcomes) == 3
        assert dim.event_calendar[0].plans == [self._OBSERVED_STRING_PLANS[0]]
        assert dim.truncation_notes == []

    def test_single_string_outcome_kept_whole(self):
        observed = "净利润同比大幅增长（半导体/光通信材料需求放量）"
        dim = _intel([_calendar_entry(outcomes=observed)])
        assert dim.event_calendar[0].outcomes == [observed]
        assert dim.truncation_notes == []

    def test_missing_none_and_empty_slots_consistent(self):
        # 键完全缺失与显式 null、空字符串一致为空
        dim, _ = _parse_intel(
            {"event_calendar": [{"event": "年度业绩预告", "date": "2026-04-30"}]}, 1
        )
        assert dim.event_calendar[0].outcomes == []
        assert dim.event_calendar[0].plans == []
        dim, _ = _parse_intel(
            {"event_calendar": [
                {"event": "年度业绩预告", "date": "2026-04-30", "outcomes": None, "plans": None}
            ]}, 1
        )
        assert dim.event_calendar[0].outcomes == []
        assert dim.event_calendar[0].plans == []
        dim = _intel([_calendar_entry(outcomes="", plans="")])
        assert dim.event_calendar[0].outcomes == []
        assert dim.event_calendar[0].plans == []
        assert dim.truncation_notes == []

    def test_single_string_trade_directive_rejected_whole(self):
        """字符串形态的独立交易指令整条拒绝并披露；不得按字符拆散后逐字绕过。"""
        directive = "T1-SL 止损：跌破 32 元无条件清仓离场（一票否决）"
        dim = _intel([_calendar_entry(plans=directive)])
        assert dim.event_calendar[0].plans == []
        notes = "；".join(dim.truncation_notes)
        assert "第 1 条预案含独立交易指令" in notes
        assert "未采用" in notes

    def test_string_outcome_trade_directive_rejected_whole(self):
        directive = "业绩超预期后36元买入，仓位3%"
        dim = _intel([_calendar_entry(outcomes=directive)])
        assert dim.event_calendar[0].outcomes == []
        assert "第 1 条可能结果含独立交易指令" in "；".join(dim.truncation_notes)

    @pytest.mark.parametrize("bad", [{"0": "核验公告原文"}, 123, 3.14, True])
    def test_wrong_container_or_scalar_rejected_visibly(self, bad):
        dim = _intel([_calendar_entry(plans=bad, outcomes=bad)])
        assert dim.event_calendar[0].plans == []
        assert dim.event_calendar[0].outcomes == []
        notes = "；".join(dim.truncation_notes)
        assert f"字段不是字符串或字符串数组（{type(bad).__name__}），未采用" in notes
        # 错误形状不得被 str() 清洗成研究内容
        assert str(bad) not in notes

    def test_non_string_list_members_rejected_positions_preserved(self):
        dim = _intel(
            [
                _calendar_entry(
                    outcomes=["事实结果一", 123, None, {"x": 1}, "事实结果二"],
                    plans=["核验公告原文", ["嵌套"], "核验跟踪数据披露"],
                )
            ]
        )
        assert dim.event_calendar[0].outcomes == ["事实结果一", "事实结果二"]
        assert dim.event_calendar[0].plans == ["核验公告原文", "核验跟踪数据披露"]
        notes = "；".join(dim.truncation_notes)
        assert "第 2 条可能结果非字符串（int），未采用" in notes
        assert "第 3 条可能结果非字符串（NoneType），未采用" in notes
        assert "第 4 条可能结果非字符串（dict），未采用" in notes
        assert "第 2 条应对预案非字符串（list），未采用" in notes
        adopted = dim.event_calendar[0].outcomes + dim.event_calendar[0].plans
        assert all("123" not in item and "None" not in item for item in adopted)

    def test_string_is_one_item_not_overflow_characters(self):
        """单字符串是 1 条：不伪造超限；真实 9 条列表仍正确丢弃并披露。"""
        long_plan = "对照披露原文逐项核验营收、净利润与毛利率区间及变动原因；" * 3
        dim = _intel([_calendar_entry(plans=long_plan)])
        assert len(dim.event_calendar[0].plans) == 1
        assert not any("上限" in note for note in dim.truncation_notes)
        nine = [f"核验动作{j}" for j in range(1, 10)]
        dim = _intel([_calendar_entry(plans=nine)])
        assert len(dim.event_calendar[0].plans) == 8
        assert "应对预案超出 8 条上限，丢弃 1 条" in "；".join(dim.truncation_notes)

    def test_mixed_string_list_keeps_order_and_rejects_directive(self):
        """真实数组形态与单字符串形态混合时：顺序保留，指令整条拒绝。"""
        plans = [
            "核验披露原文",
            "若业绩超预期则“买入”",
            self._OBSERVED_STRING_PLANS[1],
        ]
        dim = _intel([_calendar_entry(plans=list(plans))])
        assert dim.event_calendar[0].plans == [plans[0], plans[2]]
        notes = "；".join(dim.truncation_notes)
        assert "第 2 条预案含独立交易指令" in notes


class TestEventPlansTableView:
    def test_cell_pipe_and_newline_escaped(self):
        dim = _intel(
            [_calendar_entry(event="股权激励|授予", outcomes=["结果A\n带换行"], plans=["核验|含竖线"])]
        )
        view = _event_plans_view(dim)
        assert len(view) == 1
        row = view[0]
        assert "|" not in row["event"].replace("\\|", "")
        assert "\\|" in row["event"]
        assert "\n" not in row["outcomes_md"]
        assert "\\|" in row["plans_md"]

    def test_unequal_outcomes_plans_not_paired(self):
        dim = _intel(
            [_calendar_entry(outcomes=["结果甲", "结果乙"], plans=["唯一核验动作"])]
        )
        row = _event_plans_view(dim)[0]
        # 两个结果、一个核验动作：各自独立编号，不按 1:1 配对
        assert row["outcomes_md"].count("<br>") == 1
        assert "结果甲" in row["outcomes_md"] and "结果乙" in row["outcomes_md"]
        assert row["plans_md"].startswith("1.")

    def test_unknown_date_rendered_as_pending(self):
        dim = _intel([_calendar_entry(date="")])
        row = _event_plans_view(dim)[0]
        assert row["date"] == "日期待定"

    def test_empty_plans_show_placeholder(self):
        dim = _intel([_calendar_entry(plans=[])])
        row = _event_plans_view(dim)[0]
        assert row["plans_md"] == "—"

    def test_event_order_preserved(self):
        dim = _intel([_calendar_entry(event=f"事件{i}") for i in range(1, 4)])
        view = _event_plans_view(dim)
        assert [r["event"] for r in view] == ["事件1", "事件2", "事件3"]


class TestEventTableInMarkdown:
    def _render(self, calendar: list[dict], *, follow_note: str = "操作条件以报告规则作战计划为准。") -> str:
        from jinja2 import Environment, FileSystemLoader, select_autoescape

        env = Environment(
            loader=FileSystemLoader("templates"),
            autoescape=select_autoescape(enabled_extensions=()),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        template = env.get_template("event_plans.j2")
        return template.render(
            event_plans=_event_plans_view(_intel(calendar)),
            event_plan_follow_note=follow_note,
        )

    def test_render_markdown_contains_event_table(self):
        markdown = self._render(
            [
                _calendar_entry(
                    event="限售股解禁",
                    date="2026-11-15",
                    outcomes=["解禁后抛压集中：短期回调", "承接充分：平稳消化"],
                    plans=["解禁披露后核验实际减持规模与承接量", "对照解禁前 20 日均量跟踪抛压"],
                )
            ]
        )
        assert "| 日期 | 事件 | 可能结果 | 研究核验/跟踪 |" in markdown
        assert "解禁披露后核验实际减持规模与承接量" in markdown
        # 表格行数不被竖线/换行破坏
        row = next(line for line in markdown.splitlines() if "限售股解禁" in line)
        assert row.startswith("|") and row.count("|") >= 5

    def test_rendered_table_pipe_inside_cell_does_not_break_rows(self):
        markdown = self._render([_calendar_entry(event="事件|甲", outcomes=["结果|乙"])])
        rows = [line for line in markdown.splitlines() if line.startswith("|")]
        assert all(line.rstrip().endswith("|") for line in rows)

    def test_disclaimer_for_unpaired_lists(self):
        markdown = self._render([_calendar_entry(outcomes=["结果甲", "结果乙"], plans=["唯一核验动作"])])
        assert "不逐项配对" in markdown
