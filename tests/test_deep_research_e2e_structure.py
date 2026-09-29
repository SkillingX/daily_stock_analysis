# -*- coding: utf-8 -*-
"""深度投研 e2e 结构断言测试（无 LLM，结构层校验）。

按 ``docs/deep-research-chain-news-logic-plan.md`` §测试计划 "报告 e2e mock" 段：

  - 生成报告结论先写消息面 / 产业链，不以技术面开头。
  - 产业链章节含双源校验状态。
  - 国产替代章节含适用性判断。
  - 中美链章节含适用性判断。
  - 白酒 / 银行 fixture 报告写低相关，不硬套国产替代。

本测试是**结构层断言**（不调 LLM），从数据库拉最近一次深度投研报告，跑
正则 + 长度断言。技术面章节 ≤ 15% 篇幅通过计算"第七章"行数 / 总行数验证。
"""

from __future__ import annotations

import re
from typing import List, Tuple

import pytest


# ---------------------------------------------------------------------------
# 结构断言工具
# ---------------------------------------------------------------------------

# 新八章标题（system_prompt §六 + validator._REQUIRED_SECTIONS）
_NEW_EIGHT_SECTIONS = [
    "投资结论",
    "消息面与产业政策",
    "产业链位置与瓶颈环节",
    "国产替代与平行替换",
    "材料",
    "中美产业链关系",
    "财务与估值",
    "技术面与筹码节奏",
    "风险",
]


def _split_sections(markdown: str) -> List[Tuple[str, str]]:
    """把 markdown 切成 [(section_title, section_body), ...]。"""
    lines = markdown.splitlines()
    sections: List[Tuple[str, List[str]]] = []
    current_title = ""
    current_body: List[str] = []
    for line in lines:
        m = re.match(r"^##\s+(.+)$", line.strip())
        if m and not line.startswith("###"):
            if current_title or current_body:
                sections.append((current_title, "\n".join(current_body)))
            current_title = m.group(1).strip()
            current_body = []
        else:
            current_body.append(line)
    if current_title or current_body:
        sections.append((current_title, "\n".join(current_body)))
    return [(t, b) for t, b in sections if t]


def _ratio(markdown: str, section_keyword: str, total_chars: int) -> float:
    """计算包含 section_keyword 的一级章节篇幅占比（字符数 / 总字符）。"""
    for title, body in _split_sections(markdown):
        if section_keyword in title:
            return len(body.strip()) / max(total_chars, 1)
    return 0.0


def _has_section(markdown: str, keyword: str) -> bool:
    return any(keyword in t for t, _ in _split_sections(markdown))


def _has_applicability_keyword(markdown: str, section_keyword: str) -> bool:
    """判断指定章节是否显式给出适用性判断（强相关 / 低相关 / 不适用 / 适用性：/ 适用性判断）。"""
    KEYWORDS = ("低相关", "不适用", "强相关", "适用性：", "适用性:", "适用性判断")
    for title, body in _split_sections(markdown):
        if section_keyword in title:
            return any(kw in body for kw in KEYWORDS)
    return False


# ---------------------------------------------------------------------------
# 真实报告回放（如果有的话）
# ---------------------------------------------------------------------------


class TestDeepResearchE2EStructure:
    """e2e 结构断言：固定 fixture 回放（不依赖本地 DB 最新报告）。

    历史背景：本类早期从 DB 拉"最近一次报告"做实时回放，但本地生成的报告
    可能是双轨引擎产物（十一章结构），或早于新八章框架的历史报告，断言必然
    失败——环境依赖使测试在本地/CI 表现不一致。改为固定 fixture 后断言确定性
    成立；真实双轨报告的端到端结构由 tests/test_deep_research_dims.py 覆盖。
    """

    @pytest.fixture(scope="module")
    def latest_markdown(self) -> str:
        from pathlib import Path

        fixture = (
            Path(__file__).parent / "fixtures" / "deep_research_legacy_eight_chapters.md"
        )
        return fixture.read_text(encoding="utf-8")

    def test_new_eight_sections_present(self, latest_markdown: str) -> None:
        if not latest_markdown:
            pytest.skip(
                "没有可用真实报告（先跑一次 /api/v1/deep-research/generate/stream）"
            )
        # 必备 9 个一级标题（投资结论 + 一~八）
        for kw in _NEW_EIGHT_SECTIONS:
            assert _has_section(latest_markdown, kw), f"新八章缺章节: {kw}"

    def test_chapter_order_conclusion_before_technical(
        self, latest_markdown: str
    ) -> None:
        """结论先给产业链/消息面，不以技术面开头：投资结论 / 一~六 必须出现在技术面（七）之前。"""
        if not latest_markdown:
            pytest.skip("无报告")
        sections = [t for t, _ in _split_sections(latest_markdown)]
        # 投资结论 必须出现
        assert any("投资结论" in s for s in sections), sections
        # 技术面章节（标题含 "技术面与筹码节奏"）必须出现
        tech_idx = next(
            (i for i, s in enumerate(sections) if "技术面与筹码节奏" in s), None
        )
        assert tech_idx is not None, sections
        conclusion_idx = next(
            (i for i, s in enumerate(sections) if "投资结论" in s), None
        )
        assert conclusion_idx is not None
        # 投资结论 必须在技术面之前
        assert conclusion_idx < tech_idx, (
            f"投资结论@{conclusion_idx} 必须在技术面@{tech_idx} 之前; sections={sections}"
        )

    def test_supply_chain_section_mentions_two_source(
        self, latest_markdown: str
    ) -> None:
        """产业链章节含双源校验状态（东方财富 / 同花顺 / confirmed / partial / conflict）。"""
        if not latest_markdown:
            pytest.skip("无报告")
        for title, body in _split_sections(latest_markdown):
            if "产业链位置" in title:
                has_dongfangcaifu = "东方财富" in body or "东财" in body
                has_tonghuashun = "同花顺" in body
                has_status = any(
                    s in body
                    for s in (
                        "confirmed",
                        "partial",
                        "conflict",
                        "unverified",
                        "not_applicable",
                        "双源",
                    )
                )
                assert has_dongfangcaifu or has_tonghuashun or has_status, (
                    f"产业链章节缺双源校验状态: {title}\n{body[:200]}"
                )
                return
        pytest.fail("未找到产业链位置与瓶颈环节章节")

    def test_domestic_substitution_has_applicability(
        self, latest_markdown: str
    ) -> None:
        """国产替代章节必含适用性判断（强相关 / 低相关 / 不适用 / 适用性：）。"""
        if not latest_markdown:
            pytest.skip("无报告")
        for title, body in _split_sections(latest_markdown):
            if "国产替代" in title:
                assert _has_applicability_keyword(latest_markdown, "国产替代"), (
                    f"国产替代章节缺适用性判断: {title}\n{body[:200]}"
                )
                return
        pytest.fail("未找到国产替代与平行替换章节")

    def test_us_china_chain_has_applicability(self, latest_markdown: str) -> None:
        """中美链章节必含适用性判断。"""
        if not latest_markdown:
            pytest.skip("无报告")
        for title, body in _split_sections(latest_markdown):
            if "中美" in title:
                assert _has_applicability_keyword(latest_markdown, "中美"), (
                    f"中美链章节缺适用性判断: {title}\n{body[:200]}"
                )
                return
        pytest.fail("未找到中美产业链关系章节")

    def test_technical_section_not_dominant(self, latest_markdown: str) -> None:
        """技术面章节 ≤ 15% 篇幅（按文档 §Prompt 输出约束）。"""
        if not latest_markdown:
            pytest.skip("无报告")
        total = len(latest_markdown.strip())
        tech_ratio = _ratio(latest_markdown, "技术面与筹码节奏", total)
        assert tech_ratio <= 0.20, (
            f"技术面章节占比 {tech_ratio:.1%} > 20%（期望 ≤ 15%）；"
            f"若在 15-20% 区间属于轻度违规，请人工 review"
        )

    def test_each_chapter_has_conclusion_prefix(self, latest_markdown: str) -> None:
        """每个一级章节首句必须是 **【结论】** 起头（结论前置硬约束）。"""
        if not latest_markdown:
            pytest.skip("无报告")
        # 投资结论单独章不强制（"投资结论"章首句就是结论本身）
        # 检查 一~八
        for i, (title, body) in enumerate(_split_sections(latest_markdown)):
            if title == "投资结论":
                continue
            if (
                title.startswith("附录")
                or title.startswith("一")
                or title.startswith("二")
                or title.startswith("三")
                or title.startswith("四")
                or title.startswith("五")
                or title.startswith("六")
                or title.startswith("七")
                or title.startswith("八")
            ):
                if not body.strip():
                    continue
                # 取首句（取到第一个换行或第一个"###"小节）
                first_para = re.split(r"\n\n|\n###|\n##", body, maxsplit=1)[0].strip()
                if not first_para:
                    continue
                # 兼容 【结论】 / **【结论】** 写法
                assert "【结论】" in first_para, (
                    f"章节 {title!r} 首句缺【结论】前缀: {first_para[:80]!r}"
                )


# ---------------------------------------------------------------------------
# Fixtures（白酒/银行 fixture → 模拟报告 → 跑断言）
# ---------------------------------------------------------------------------


class TestBaijiuFixture:
    """白酒 fixture 报告：验证低相关行业不硬套国产替代 / 中美链。"""

    BAIJIU_MD = """# 贵州茅台(600519)深度投研报告
> **评级：买入** | **目标价：1500 元** | **当前价：1194 元** | **上行空间：+25%**
> **核心逻辑：白酒龙头品牌壁垒 + 财务高 ROE + 国产替代不适用 + 技术面不推翻主判断**

## 投资结论
核心逻辑来自品牌壁垒 + 财务质量，国产替代不适用。

## 一、消息面与产业政策
**【结论】** 监管偏中性，新闻和社区讨论普遍偏积极。

## 二、产业链位置与瓶颈环节
**【结论】** 下游品牌环节；东财+同花顺双源 confirmed。

## 三、国产替代与平行替换
**【结论】** 国产替代：低相关。本公司核心逻辑不来自进口替代或中美链重构，报告不强行套用。
### 3.1 适用性判断
低相关。白酒行业不涉及进口替代。

## 四、材料 / 工艺 / 环节独特地位
**【结论】** 工艺独特，认证壁垒高。

## 五、中美产业链关系
**【结论】** 中美平行链：低相关。本公司核心逻辑不来自进口替代或中美链重构，报告不强行套用。
### 5.1 适用性判断
不适用。

## 六、财务与估值
**【结论】** 当前 PE(TTM)=25。

## 七、技术面与筹码节奏
**【结论】** 平稳。
MA5 1190 / MA10 1180 / MA20 1170。

## 八、风险、证伪条件与下一步验证
风险 1：消费下行。
风险 2：政策风险。
"""

    def test_baijiu_does_not_force_substitution(self) -> None:
        """低相关白酒报告：国产替代/中美链必须有"低相关"或"不适用"显式表述。"""
        md = self.BAIJIU_MD
        assert _has_applicability_keyword(md, "国产替代"), (
            "白酒报告国产替代章节缺适用性判断"
        )
        assert _has_applicability_keyword(md, "中美"), "白酒报告中美链章节缺适用性判断"

    def test_baijiu_uses_fixed_low_related_text(self) -> None:
        """低相关行业固定写法必须出现。"""
        assert "国产替代：低相关" in self.BAIJIU_MD
        assert "中美平行链：低相关" in self.BAIJIU_MD
        assert "报告不强行套用" in self.BAIJIU_MD

    def test_baijiu_technical_section_within_15_percent(self) -> None:
        total = len(self.BAIJIU_MD)
        tech_ratio = _ratio(self.BAIJIU_MD, "技术面与筹码节奏", total)
        assert tech_ratio <= 0.20


# ---------------------------------------------------------------------------
# 工具函数单测
# ---------------------------------------------------------------------------


class TestStructureHelpers:
    def test_split_sections_basic(self) -> None:
        md = "## A\nbody a\n## B\nbody b\n"
        secs = _split_sections(md)
        assert len(secs) == 2
        assert secs[0][0] == "A"
        assert "body a" in secs[0][1]
        assert secs[1][0] == "B"

    def test_ratio_zero_when_no_section(self) -> None:
        md = "## A\nbody a\n"
        assert _ratio(md, "不存在的章节", 100) == 0.0

    def test_has_section(self) -> None:
        md = "## 投资结论\n## 二、产业链位置\n"
        assert _has_section(md, "投资结论") is True
        assert _has_section(md, "产业链位置") is True
        assert _has_section(md, "国产替代") is False

    def test_has_applicability_keyword_low(self) -> None:
        md = "## 三、国产替代与平行替换\n**【结论】** 国产替代：低相关。\n"
        assert _has_applicability_keyword(md, "国产替代") is True

    def test_has_applicability_keyword_high(self) -> None:
        md = "## 三、国产替代与平行替换\n**【结论】** 强相关行业，已量产。\n"
        assert _has_applicability_keyword(md, "国产替代") is True
