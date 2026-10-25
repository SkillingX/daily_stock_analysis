# -*- coding: utf-8 -*-
"""深度投研新八章（消息面+产业链主导）质量校验器测试。

按 ``docs/deep-research-chain-news-logic-plan.md``：

- 新八章结构（投资结论 + 一~八）
- 技术面节奏层为 optional（缺工具不阻断）
- 国产替代 / 中美链必须显式给出适用性判断（强相关/低相关/不适用）
- 缺适用性扣 5 分 / 个
- PE 估值口径一致性检测保留
- search 工具集（社区讨论工具已随 issue #46 退役）
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from src.agent.deep_research_validator import (
    _APPLICABILITY_HIGH_KEYWORDS,
    _APPLICABILITY_LOW_KEYWORDS,
    _LAYER_REQUIREMENTS,
    _REQUIRED_SECTIONS,
    _has_applicability_judgment,
    _section_present,
    DeepResearchValidator,
    ValidationResult,
)


# ---------------------------------------------------------------------------
# 新八章满分 fixture（含强相关行业 + 适用性判断 + 全部工具）
# ---------------------------------------------------------------------------

GOOD_MD = """# 韦尔股份(603501)深度投研报告
> **评级：买入** | **目标价：200 元** | **当前价：180 元** | **上行空间：+11%**
> **核心逻辑：CIS 国产替代 + 中美链受益 + 财务兑现 + 技术面资金流共振**
> **关键催化剂：高端 CIS 量产** | **核心风险：海外大客户份额下滑**

---

## 投资结论

### 国产替代 / 中美平行链适用性
- 适用性：强相关
- 半导体 CIS 国产替代是核心逻辑

### 三种世界观
| 情景 | 概率 | 目标价 | 核心假设 |
|------|------|--------|----------|
| 牛市 | 30% | 240 | 替代加速 |
| 基准 | 50% | 200 | 平稳量产 |
| 熊市 | 20% | 130 | 替代延后 |

（三情景概率之和必须严格等于 100%）

### 投资评级
- 短期（3 个月）：增持
- 中期（6 个月）：买入
- 长期（12 个月）：买入

---

## 一、消息面与产业政策

**【结论】** 政策友好度 7/10，行业政策利好明显。

### 1.1 官方公告与财报线索（primary）
公司发布 2025 年报，营收同比 +18%，公告披露高端 CIS 进入量产。

### 1.2 公开新闻与产业事件（news）
行业新闻多次提及 CIS 国产替代加速。

### 1.3 政策敏感度与监管风险
工信部 / 证监会出台相关支持政策，监管风险低。

### 1.4 国内外社区讨论与市场分歧（community_cn / community_global）
雪球 / 东方财富股吧关注度高；社区讨论出现分歧（看多/看空共存）。

### 1.5 消息面可信度分层
primary 支撑事实；community_cn 仅做线索。

---

## 二、产业链位置与瓶颈环节

**【结论】** 处于产业链中游核心节点，是关键瓶颈环节之一。

### 2.1 产业链图谱（上 / 中 / 下游）
上游晶圆厂 → 中游 CIS 设计 → 下游手机/汽车摄像头模组。

### 2.2 瓶颈与利润分配
中游设计是利润集中点，公司处于关键环节。

### 2.3 板块归属双源校验（东财 + 同花顺，confirmed / partial / conflict / unverified / not_applicable）
东方财富 + 同花顺双源 confirmed（high confidence），板块归属明确。

---

## 三、国产替代与平行替换

**【结论】** 强相关行业，已进入量产阶段。

### 3.1 适用性判断
强相关，半导体 CIS 国产替代核心标的。

### 3.2 替代阶段（传闻 / 送样 / 验证 / 导入 / 量产）
**当前替代阶段：量产**（公告披露 2024Q4 起批量出货）。

### 3.3 替代对象 / 替代环节 / 证据等级
替代对象：索尼；替代环节：中高端 CIS；证据等级：primary（公司公告）。

---

## 四、材料 / 工艺 / 环节独特地位

**【结论】** 工艺独特，认证壁垒高，是真壁垒。

### 4.1 材料 / 工艺独特性
高动态范围工艺独特，材料纯度达 6N。

### 4.2 壁垒类型（专利 / 配方 / 良率 / 纯度 / 认证）
拥有 50+ 项核心专利，良率行业领先。

### 4.3 客户验证周期与切换成本
客户验证周期 18 个月，切换成本高。

### 4.4 扩产难度与同行可复制性
扩产周期 24 个月，国内同行 36 个月内难以复制。

---

## 五、中美产业链关系

**【结论】** 强相关，受益于中美平行替换。

### 5.1 适用性判断
强相关，公司在中国链与美国链均有布局。

### 5.2 中国链位置
国内手机品牌主力供应商。

### 5.3 美国 / 全球链位置
原海外大客户份额下滑，国产替代加速。

### 5.4 出口管制 / 制裁 / 限制
存在出口管制风险，但国内需求足以消化。

---

## 六、财务与估值验证

**【结论】** 当前 PE(TTM)=40，财务已开始兑现产业链逻辑。

### 6.1 财务与产业链兑现
营收同比 +18%，毛利率 32%，ROE 22%，产业链逻辑部分反映。

### 6.2 估值是否透支预期
PE 40x 未明显透支；牛市情景 PE 区间 45-50x 高于当前 PE(TTM) 40x，逻辑自洽。

### 6.3 三情景估值（PE 口径一致性自检）
牛市 30% 50x / 基准 50% 40x / 熊市 20% 25x。

---

## 七、技术面节奏（次要，≤ 15% 篇幅）

**【结论】** 资金流与产业逻辑共振，技术面仅回答节奏。

### 7.1 价格位置与支撑阻力
MA5 178 / MA10 175 / MA20 168，支撑 165 阻力 195。

### 7.2 资金流共振
主力近 5 日净流入 +3.2 亿元。

### 7.3 交易节奏建议
回踩 175 附近可建仓。

---

## 八、风险、证伪条件与下一步验证

### 8.1 产业链证伪条件
若海外 CIS 大厂大幅降价，本逻辑证伪。

### 8.2 国产替代证伪条件
若 2026Q2 量产不及预期，证伪。

### 8.3 中美链证伪条件
若出口管制反转，公司海外业务受损逻辑证伪。

### 8.4 下一步验证清单
跟踪 2026Q1 业绩公告 + 客户验证进度。
"""

FULL_TOOLS: List[Dict[str, Any]] = [
    {"tool": "get_market_indices"},
    {"tool": "search_comprehensive_intel"},
    {"tool": "search_stock_news"},
    {"tool": "get_sector_rankings"},
    {"tool": "get_stock_info"},
    {"tool": "analyze_trend"},
    {"tool": "get_capital_flow"},
]


# ---------------------------------------------------------------------------
# _LAYER_REQUIREMENTS / _REQUIRED_SECTIONS 结构性测试
# ---------------------------------------------------------------------------


class TestNewFrameworkSchema:
    """验证 validator 内部 schema 与 docs 方案一致。"""

    def test_eight_layers_in_order(self):
        layers = list(_LAYER_REQUIREMENTS.keys())
        assert layers == [
            "消息面与产业政策",
            "产业链位置",
            "国产替代",
            "材料工艺独特地位",
            "中美产业链关系",
            "财务与估值",
            "技术面节奏",
        ]

    def test_technical_layer_is_optional(self):
        tech = _LAYER_REQUIREMENTS["技术面节奏"]
        assert tech.get("optional") is True
        # 其它层都不能是 optional
        for k, v in _LAYER_REQUIREMENTS.items():
            if k == "技术面节奏":
                continue
            assert not v.get("optional"), f"{k} 不应为 optional"

    def test_required_sections_cover_eight_chapters(self):
        # 投资结论 + 一~八 共 9 个 section 关键字（与 system_prompt §六 一致）
        assert len(_REQUIRED_SECTIONS) == 9
        assert "投资结论" in _REQUIRED_SECTIONS
        assert "消息面与产业政策" in _REQUIRED_SECTIONS
        assert "产业链位置与瓶颈环节" in _REQUIRED_SECTIONS
        assert "国产替代与平行替换" in _REQUIRED_SECTIONS
        assert "技术面节奏" in _REQUIRED_SECTIONS

    def test_no_legacy_five_layer_keys(self):
        # 旧"五层穿透"层名不应再出现
        for legacy in ("宏观", "博弈"):
            assert legacy not in _LAYER_REQUIREMENTS


# ---------------------------------------------------------------------------
# 满分支 / 降权 / 适用性 测试
# ---------------------------------------------------------------------------


class TestDeepResearchValidatorNew:
    def setup_method(self) -> None:
        self.validator = DeepResearchValidator()

    def test_complete_strong_related_report_full_score(self):
        """满分支报告（强相关行业 + 适用性 + 全部工具）→ score=100 / passed=True"""
        result = self.validator.validate(GOOD_MD, FULL_TOOLS)
        assert result.passed is True
        assert result.score == 100
        assert result.missing_layers == []
        assert result.missing_sections == []
        assert result.conclusion_count >= 7
        assert 95.0 <= result.probability_sum <= 105.0

    def test_low_related_baijiu_with_optional_tech_missing_passes(self):
        """白酒/银行等低相关行业：缺技术面工具 + 缺技术面关键词 → passed=True（不阻断）"""
        baijiu = GOOD_MD.replace(
            "## 三、国产替代与平行替换\n\n**【结论】** 强相关行业，已进入量产阶段。\n\n"
            "### 3.1 适用性判断\n强相关，半导体 CIS 国产替代核心标的。\n\n"
            "### 3.2 替代阶段（传闻 / 送样 / 验证 / 导入 / 量产）\n"
            "**当前替代阶段：量产**（公告披露 2024Q4 起批量出货）。\n\n"
            "### 3.3 替代对象 / 替代环节 / 证据等级\n"
            "替代对象：索尼；替代环节：中高端 CIS；证据等级：primary（公司公告）。\n\n"
            "## 四、材料 / 工艺 / 环节独特地位",
            "## 三、国产替代与平行替换\n\n**【结论】** 国产替代：低相关。本公司核心逻辑不来自进口替代或中美链重构，报告不强行套用。\n\n"
            "## 四、材料 / 工艺 / 环节独特地位",
        ).replace(
            "## 五、中美产业链关系\n\n**【结论】** 强相关，受益于中美平行替换。\n\n"
            "### 5.1 适用性判断\n强相关，公司在中国链与美国链均有布局。\n\n"
            "### 5.2 中国链位置",
            "## 五、中美产业链关系\n\n**【结论】** 中美平行链：低相关。本公司核心逻辑不来自进口替代或中美链重构，报告不强行套用。\n\n",
        )
        # 移除技术面章节
        baijiu = (
            baijiu.split("## 七、技术面节奏（次要")[0]
            + "## 八、风险、证伪条件与下一步验证\n\n风险 1。"
        )
        # 缺技术面工具
        tools_no_tech = [
            t
            for t in FULL_TOOLS
            if t["tool"]
            not in {"analyze_trend", "get_capital_flow"}
        ]
        result = self.validator.validate(baijiu, tools_no_tech)
        assert result.passed is True
        # 技术面 optional 缺不阻断
        assert "技术面节奏" in [d for d in result.details if "技术面" in d][
            0
        ] or any("技术面" in d for d in result.details)
        # 茅台 report 不应被误判 missing_layer
        assert "技术面节奏" not in result.missing_layers

    def test_strong_related_without_applicability_keyword_penalized(self):
        """强相关行业但章节未出现「强相关/低相关/不适用/适用性判断」 → score-10"""
        no_app = """# 韦尔股份(603501)深度投研报告

## 投资结论
**【结论】** 核心逻辑 CIS 替代。

### 三种世界观
| 情景 | 概率 | 目标价 |
|------|------|--------|
| 牛市 | 30% | 240 |
| 基准 | 50% | 200 |
| 熊市 | 20% | 130 |

## 一、消息面与产业政策
**【结论】** 监管偏紧。

## 二、产业链位置与瓶颈环节
**【结论】** 半导体中游。

## 三、国产替代与平行替换
**【结论】** 处于量产阶段。
替代对象为索尼。

## 四、材料 / 工艺 / 环节独特地位
**【结论】** 工艺壁垒高。

## 五、中美产业链关系
**【结论】** 出口管制涉及。

## 六、财务与估值验证
**【结论】** 当前 PE(TTM)=40。

## 七、技术面节奏（次要）
**【结论】** 平稳。

## 八、风险、证伪条件与下一步验证
风险 1。
"""
        result = self.validator.validate(no_app, FULL_TOOLS)
        # 满分支 100，缺 2 处适用性扣 10 → 90
        assert result.score == 90
        assert any("未显式给出适用性判断" in d for d in result.details)
        assert any("国产替代" in d and "适用性" in d for d in result.details)
        assert any("中美产业链关系" in d and "适用性" in d for d in result.details)

    def test_only_one_applicability_missing_penalized_5(self):
        """只缺 1 个适用性判断 → score-5"""
        one_app = """# 韦尔股份(603501)深度投研报告

## 投资结论
### 三种世界观
| 情景 | 概率 | 目标价 |
|------|------|--------|
| 牛市 | 30% | 240 |
| 基准 | 50% | 200 |
| 熊市 | 20% | 130 |

## 一、消息面与产业政策
**【结论】** 监管偏紧。

## 二、产业链位置与瓶颈环节
**【结论】** 半导体中游。

## 三、国产替代与平行替换
**【结论】** 强相关行业，替代对象为索尼。

## 四、材料 / 工艺 / 环节独特地位
**【结论】** 工艺壁垒高。

## 五、中美产业链关系
**【结论】** 出口管制涉及。

## 六、财务与估值验证
**【结论】** 当前 PE(TTM)=40。

## 七、技术面节奏（次要）
**【结论】** 平稳。

## 八、风险、证伪条件与下一步验证
风险 1。
"""
        result = self.validator.validate(one_app, FULL_TOOLS)
        # 满分支 100，只缺 1 处适用性扣 5 → 95
        assert result.score == 95
        assert sum("未显式给出适用性判断" in d for d in result.details) == 1

    def test_missing_required_layer_fails(self):
        """缺失必需层（消息面）→ 阻断 passed"""
        # 删掉消息面章节
        no_news = (
            GOOD_MD.replace(
                "## 一、消息面与产业政策",
                "## 一、新闻与产业政策",  # 改名后不含消息面/公告/新闻/社区/政策等关键词
            )
            .replace("新闻和", "X和")
            .replace("社区讨论", "X讨论")
            .replace("监管", "X管")
        )
        # 但 GOOD_MD 全文里"新闻和"很多，手动改更安全
        no_news = """# X
## 投资结论
**【结论】** 中性
## 二、产业链位置
**【结论】** 中游
## 三、国产替代
**【结论】** 强相关
## 四、材料
**【结论】** 工艺壁垒
## 五、中美
**【结论】** 出口管制
## 六、财务
**【结论】** 当前 PE(TTM)=40
## 七、技术
**【结论】** 平稳
## 八、风险
风险 1
"""
        result = self.validator.validate(no_news, FULL_TOOLS)
        # 必含 "消息面与产业政策" 关键词才能过
        assert "消息面与产业政策" in result.missing_layers or result.passed is False

    def test_empty_input_safe(self):
        result = self.validator.validate("")
        assert result.score == 0
        assert result.passed is False
        assert set(result.missing_layers) == set(_LAYER_REQUIREMENTS.keys())

    def test_probability_sum_still_validated(self):
        """三情景概率和 95%-105% 才视为合法（保留旧行为）"""
        bad_prob = GOOD_MD.replace(
            "| 牛市 | 30% | 240 | 替代加速 |",
            "| 牛市 | 70% | 240 | 替代加速 |",
        ).replace(
            "| 熊市 | 20% | 130 | 替代延后 |",
            "| 熊市 | 5% | 130 | 替代延后 |",
        )
        result = self.validator.validate(bad_prob, FULL_TOOLS)
        # 125% 应在 details 中提示
        assert any("三情景概率和" in d for d in result.details)


class TestApplicabilityHelpers:
    def test_section_present_matches_layer_key(self):
        md = "## 国产替代与平行替换\nfoo"
        assert _section_present(md, "国产替代") is True
        assert _section_present(md, "中美产业链关系") is False

    def test_has_applicability_judgment_low_keyword(self):
        for kw in _APPLICABILITY_LOW_KEYWORDS:
            md = f"## 国产替代与平行替换\n{kw}\n"
            assert _has_applicability_judgment(md, "国产替代") is True, kw

    def test_has_applicability_judgment_high_keyword(self):
        for kw in _APPLICABILITY_HIGH_KEYWORDS:
            md = f"## 国产替代与平行替换\n{kw}\n"
            assert _has_applicability_judgment(md, "国产替代") is True, kw

    def test_has_applicability_judgment_subsection(self):
        md = "## 三、国产替代与平行替换\n### 3.1 适用性判断\nfoo"
        assert _has_applicability_judgment(md, "国产替代") is True

    def test_has_applicability_judgment_missing(self):
        md = "## 国产替代与平行替换\n**【结论】** 量产阶段，替代对象为索尼。"
        assert _has_applicability_judgment(md, "国产替代") is False

    def test_has_applicability_judgment_scoped_to_layer(self):
        """修复：适用性判断必须**在该层所在章节**内才计，避免跨章节误判。"""
        # 第三节无适用性判断；第五节有"强相关"——但属中美章节，不应替国产替代背书
        md = (
            "## 三、国产替代与平行替换\n"
            "**【结论】** 量产阶段，替代对象为索尼。\n\n"
            "## 五、中美产业链关系\n"
            "**【结论】** 强相关，出口管制涉及。\n"
        )
        assert _has_applicability_judgment(md, "国产替代") is False
        assert _has_applicability_judgment(md, "中美产业链关系") is True


# ---------------------------------------------------------------------------
# 工厂层 / 工具集 wiring 测试
# ---------------------------------------------------------------------------


class TestDeepResearchToolSet:
    """issue #45: 公开双源校验工具已退役。"""

    def test_retired_probe_tool_not_registered(self):
        from src.agent.factory import _DEEP_RESEARCH_TOOL_NAMES

        assert "verify_supply_chain_evidence" not in _DEEP_RESEARCH_TOOL_NAMES

    def test_tool_registry_builds_with_p1(self):
        """build_deep_research_executor 工具数 ≥ 11。"""
        from src.agent.factory import _DEEP_RESEARCH_TOOL_NAMES

        # issue #45 退役 verify_supply_chain_evidence 后为 9 个
        assert len(_DEEP_RESEARCH_TOOL_NAMES) >= 9

    def test_factory_comment_matches_new_framework(self):
        from src.agent.factory import _DEEP_RESEARCH_TOOL_NAMES

        # 11 个工具对应新八章（不含 backtest/技术冗余工具）
        expected = {
            "get_realtime_quote",
            "get_daily_history",
            "get_stock_info",
            "get_capital_flow",
            "analyze_trend",
            "search_stock_news",
            "search_comprehensive_intel",
            "get_market_indices",
            "get_sector_rankings",
        }
        assert _DEEP_RESEARCH_TOOL_NAMES == expected



class TestExecutorUserMessage:
    """验证 _build_user_message 反映新框架（不是旧五层穿透措辞）。"""

    def test_user_message_uses_new_framework_terms(self) -> None:
        from src.agent.deep_research_executor import DeepResearchExecutor

        msg = DeepResearchExecutor._build_user_message("600519", "贵州茅台", "deep")
        # 新八章关键字
        for kw in (
            "消息面与产业政策",
            "产业链位置",
            "国产替代",
            "中美",
            "财务与估值",
            "技术面节奏",
            "适用性",
            "替代阶段",
        ):
            assert kw in msg, f"新框架关键词缺失: {kw}"
        # 旧"五层穿透"不应再作为强制措辞
        assert "五层穿透" not in msg
        assert "不可跳层" not in msg

    def test_system_prompt_has_new_eight_sections(self) -> None:
        from src.agent.deep_research_executor import build_deep_research_system_prompt

        prompt = build_deep_research_system_prompt()
        for section in (
            "一、消息面与产业政策",
            "二、产业链位置与瓶颈环节",
            "三、国产替代与平行替换",
            "四、材料 / 工艺 / 环节独特地位",
            "五、中美产业链关系",
            "六、财务与估值验证",
            "七、技术面节奏（次要",
            "八、风险、证伪条件与下一步验证",
        ):
            assert section in prompt, f"system_prompt.md 缺失新章节: {section}"

    def test_system_prompt_marks_technical_demotion(self) -> None:
        from src.agent.deep_research_executor import build_deep_research_system_prompt

        prompt = build_deep_research_system_prompt()
        # 技术面降权 / 15% 篇幅上限
        assert "15%" in prompt
        assert "不得推翻产业链主判断" in prompt


# ---------------------------------------------------------------------------
# 旧 PE 口径一致性测试仍生效（回归保护）
# ---------------------------------------------------------------------------


class TestPEPremiumContradictionRegression:
    """新框架不应破坏旧 PE 估值口径一致性检测。"""

    def test_premium_below_current_pe_flagged(self) -> None:
        from src.agent.deep_research_validator import (
            _detect_pe_premium_contradictions,
            _extract_current_pe,
        )

        md = "当前 PE(TTM): 30.0\n溢价抬升至 20-25x PE"
        cur = _extract_current_pe(md)
        assert cur == 30.0
        cs = _detect_pe_premium_contradictions(md, cur)
        assert len(cs) == 1
        assert cs[0].current_pe == 30.0
        assert cs[0].scenario_pe_high == 25.0
