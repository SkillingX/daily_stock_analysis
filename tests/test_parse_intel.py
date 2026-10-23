# -*- coding: utf-8 -*-
"""情报解析与事件表格契约测试（issue #40）。

合理长度的根因/事件/结果/预案正文不得被解析层静默硬截；
未来事件以表格呈现，结果与预案数量不等时不逐项配对。
"""

from __future__ import annotations

from src.agent.deep_research.orchestrator import _parse_intel
from src.deep_research_dims.render import _event_plans_view
from src.schemas.deep_research_dims import IntelDim


def _calendar_entry(
    event: str = "年度业绩预告",
    date: str = "2026-04-30",
    outcomes: list[str] | None = None,
    plans: list[str] | None = None,
) -> dict:
    return {
        "event": event,
        "date": date,
        "outcomes": ["超预期：全年净利上修"] if outcomes is None else outcomes,
        "plans": ["业绩预告超预期后回调加仓，跌破预告日前低点止损"] if plans is None else plans,
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
        plans = [f"第{i}条预案：针对结果的应对动作与退出条件" for i in range(1, 7)]
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
                plans=[f"预案{i}-{j}" for j in range(1, 3)],
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


class TestEventPlansTableView:
    def test_cell_pipe_and_newline_escaped(self):
        dim = _intel(
            [_calendar_entry(event="股权激励|授予", outcomes=["结果A\n带换行"], plans=["预案|含竖线"])]
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
            [_calendar_entry(outcomes=["结果甲", "结果乙"], plans=["唯一预案"])]
        )
        row = _event_plans_view(dim)[0]
        # 两个结果、一个预案：各自独立编号，不按 1:1 配对
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
    def _render(self, calendar: list[dict]) -> str:
        from jinja2 import Environment, FileSystemLoader, select_autoescape

        env = Environment(
            loader=FileSystemLoader("templates"),
            autoescape=select_autoescape(enabled_extensions=()),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        template = env.get_template("event_plans.j2")
        return template.render(event_plans=_event_plans_view(_intel(calendar)))

    def test_render_markdown_contains_event_table(self):
        markdown = self._render(
            [
                _calendar_entry(
                    event="限售股解禁",
                    date="2026-11-15",
                    outcomes=["解禁后抛压集中：短期回调", "承接充分：平稳消化"],
                    plans=["解禁前减仓至半仓", "放量跌破前低止损"],
                )
            ]
        )
        assert "| 日期 | 事件 | 可能结果 | 应对预案 |" in markdown
        assert "解禁前减仓至半仓" in markdown
        # 表格行数不被竖线/换行破坏
        row = next(line for line in markdown.splitlines() if "限售股解禁" in line)
        assert row.startswith("|") and row.count("|") >= 5

    def test_rendered_table_pipe_inside_cell_does_not_break_rows(self):
        markdown = self._render([_calendar_entry(event="事件|甲", outcomes=["结果|乙"])])
        rows = [line for line in markdown.splitlines() if line.startswith("|")]
        assert all(line.rstrip().endswith("|") for line in rows)

    def test_disclaimer_for_unpaired_lists(self):
        markdown = self._render([_calendar_entry(outcomes=["结果甲", "结果乙"], plans=["唯一预案"])])
        assert "不逐项配对" in markdown
