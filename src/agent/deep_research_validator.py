# -*- coding: utf-8 -*-
"""深度投研报告 · 质量校验器（三层防线的 L2 检测层）。

对 Agent 产出的 Markdown 报告做结构化质量校验：
1. 新八章结构完整性：投资结论 + 消息面与产业政策 + 产业链位置 + 国产替代 +
   材料工艺独特地位 + 中美产业链关系 + 财务与估值 + 技术面节奏 + 风险证伪。
2. 结论前置：每个一级章节首句是否为结论句。
3. 三情景概率和：是否落在 [95%, 105%] 容差区间。

框架调整（按 ``docs/deep-research-chain-news-logic-plan.md``）：
- 旧"五层穿透"（宏观 / 产业 / 财务 / 估值 / 博弈）改为新八章（消息面 / 产业链 /
  国产替代 / 材料工艺 / 中美链 / 财务估值 / 技术面 / 风险证伪）。
- 旧"宏观"被并入"消息面与产业政策"；"财务"与"估值"合并为"财务与估值验证"。
- 国产替代与中美链各自必含**适用性判断**（强相关 / 低相关 / 不适用），否则降分。
- **技术面降权**：技术面节奏层缺工具不阻断报告（标记"缺少择时信息"），
  关键词缺失只降分；其它层缺失仍按"必要工具未调用"或"内容关键词缺失"扣分。

校验结果驱动 executor 的 L3 兜底（失败→追加提示重生成；再失败→标注降级）。
校验本身是只读的纯函数，不抛异常，不阻塞主流程。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, cast

logger = logging.getLogger(__name__)


# 每层要求：关键词（内容覆盖，命中其一即可）+ 工具组（每组任一即可，所有组都要满足）。
# 与 system_prompt.md 第二章「强制工具调用检查点」保持一致。
#
# ``optional=True`` 的层（当前仅"技术面节奏"）即使缺工具或缺关键词也不阻断
# passed 判定；只降分并写入 details 提示"缺少择时信息"。其它层缺失会阻断。
_LAYER_REQUIREMENTS: Dict[str, Dict[str, object]] = {
    "消息面与产业政策": {
        "keywords": [
            "消息",
            "公告",
            "新闻",
            "政策",
            "监管",
            "分歧",
            "宏观",
            "市场",
            "大盘",
            "指数",
            "流动性",
        ],
        "tool_groups": [
            {"search_stock_news", "search_comprehensive_intel"},
            {"get_market_indices"},
        ],
    },
    "产业链位置": {
        "keywords": [
            "产业链",
            "上游",
            "中游",
            "下游",
            "瓶颈",
            "卡点",
            "关键材料",
            "关键设备",
            "关键工艺",
            "板块归属",
            "板块",
        ],
        "tool_groups": [
            {"get_sector_rankings"},
            {"search_comprehensive_intel"},
        ],
    },
    "国产替代": {
        "keywords": [
            "国产替代",
            "进口替代",
            "平行替换",
            "送样",
            "验证",
            "导入",
            "量产",
            "替代阶段",
            "不适用",
            "低相关",
        ],
        "tool_groups": [],  # 国产替代层靠内容关键词（含适用性判断）
    },
    "材料工艺独特地位": {
        "keywords": [
            "材料",
            "工艺",
            "良率",
            "纯度",
            "认证",
            "扩产",
            "客户验证",
            "专利",
            "配方",
            "壁垒",
        ],
        "tool_groups": [],  # 内容必含
    },
    "中美产业链关系": {
        "keywords": [
            "中美",
            "美国链",
            "中国链",
            "出口管制",
            "制裁",
            "平行链",
            "EDA",
            "不适用",
            "低相关",
        ],
        "tool_groups": [],  # 内容必含适用性判断
    },
    "财务与估值": {
        "keywords": [
            "营收",
            "利润",
            "ROE",
            "毛利率",
            "现金流",
            "杜邦",
            "资产负债",
            "净利率",
            "估值",
            "PE",
            "PB",
            "DCF",
            "SOTP",
            "目标价",
            "安全边际",
            "情景",
            "PEG",
            "EV/EBITDA",
        ],
        "tool_groups": [{"get_stock_info"}],
    },
    "技术面节奏": {
        "keywords": [
            "均线",
            "K线",
            "量能",
            "资金流",
            "主力",
            "催化",
            "股东户数",
            "融资余额",
            "支撑",
            "阻力",
            "技术面",
        ],
        "tool_groups": [{"analyze_trend", "get_capital_flow"}],
        "optional": True,  # P0 框架调整：技术面降权，缺不阻断
    },
}

# 一级章节标题（用于结论前置校验）
# 新八章：投资结论 + 一~八（与 system_prompt.md §六 严格对齐）
_REQUIRED_SECTIONS = [
    "投资结论",
    "消息面与产业政策",
    "产业链位置与瓶颈环节",
    "国产替代与平行替换",
    "材料",
    "中美产业链关系",
    "财务与估值",
    "技术面节奏",
    "风险",
]

# 国产替代/中美链的"低相关"或"不适用"固定写法（用于适配性判定的关键字面覆盖）
_APPLICABILITY_LOW_KEYWORDS = ("低相关", "不适用")
_APPLICABILITY_HIGH_KEYWORDS = ("强相关", "适用性判断")


@dataclass(frozen=True)
class ValidationResult:
    """质量校验结果（不可变）。"""

    passed: bool
    score: int  # 0-100
    missing_layers: List[str] = field(default_factory=list)
    missing_tool_groups: List[str] = field(default_factory=list)
    missing_sections: List[str] = field(default_factory=list)
    conclusion_count: int = 0
    probability_sum: float = 0.0
    details: List[str] = field(default_factory=list)
    pe_contradictions: List[PeContradiction] = field(default_factory=list)


def _extract_called_tools(tool_calls_log: List[Dict[str, object]]) -> Set[str]:
    """从工具调用日志提取已调用的工具名集合。"""
    called: Set[str] = set()
    for entry in tool_calls_log or []:
        name = entry.get("tool") if isinstance(entry, dict) else None
        if isinstance(name, str) and name:
            called.add(name)
    return called


def _check_layer(
    layer: str,
    requirement: Dict[str, object],
    markdown: str,
    called_tools: Set[str],
) -> Dict[str, object]:
    """检查单层覆盖情况。返回 {content_ok, tools_ok, missing_groups}。"""
    keywords: List[str] = list(cast(Iterable[str], requirement.get("keywords") or []))
    content_ok = any(kw in markdown for kw in keywords)

    tool_groups: List[Any] = list(
        cast(Iterable[Any], requirement.get("tool_groups") or [])
    )
    missing_groups: List[str] = []
    tools_ok = True
    if tool_groups:
        for group in tool_groups:
            if not (group & called_tools):
                tools_ok = False
                missing_groups.append(layer)

    return {
        "content_ok": content_ok,
        "tools_ok": tools_ok,
        "missing_groups": missing_groups,
    }


def _count_conclusions(markdown: str) -> int:
    """统计结论前置标记数量（【结论】）。"""
    return len(re.findall(r"【结论】", markdown))


def _count_validation_markers(markdown: str) -> tuple[Any, ...]:
    """统计双源验证标注：返回 (verified✓, conflict⚠)。

    温和信息性统计，不参与 passed/score 判定（避免 LLM 未严格遵循格式时误判）。
    """
    verified = len(re.findall(r"✓", markdown))
    conflict = len(re.findall(r"⚠", markdown))
    return verified, conflict


def _check_probability_sum(markdown: str) -> float:
    """提取三情景概率并求和。

    匹配情景表/正文中形如「牛市 | 25%」「概率：50%」的数字。
    容错：取前 3 个最可能的概率值求和（牛市/基准/熊市）。
    """
    # 优先匹配「情景 ... XX%」表格行
    table_probs = re.findall(
        r"(?:牛市|基准|熊市|base|bull|bear)[^\d]{0,20}?(\d{1,3})\s*%",
        markdown,
        re.IGNORECASE,
    )
    if len(table_probs) >= 3:
        vals = [int(p) for p in table_probs[:3]]
        return float(sum(vals))
    # 兜底：匹配所有百分号数字，取前 3 个
    all_probs = re.findall(r"(\d{1,3})\s*%", markdown)
    if len(all_probs) >= 3:
        vals = [int(p) for p in all_probs[:3]]
        return float(sum(vals))
    return 0.0


# ---------------------------------------------------------------------------
# PE 估值口径一致性检测
#
# 防止「溢价抬升至 X-Y x PE」但 X-Y 明显低于当前 PE(TTM) 的自相矛盾：报告头部
# 声明的【当前 PE(TTM)】是估值基准锚点，溢价/抬升情景的 PE 区间上限必须 ≥ 当前 PE。
# 本检测是只读、非阻断的 L2 质量信号：仅写入 details + pe_contradictions，不改变
# passed/score（避免误判触发不必要的重生成，影响无关报告）。
# ---------------------------------------------------------------------------

# 当前 PE(TTM) 提取模式（仅识别 TTM 限定口径，避免误抓正文中的情景 PE）。
# 按优先级匹配，命中其一即返回。
_CURRENT_PE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"PE\s*[\(（]\s*TTM\s*[\)）][：:\s]*([0-9]+(?:\.[0-9]+)?)"),
    re.compile(r"市盈率\s*[\(（]\s*TTM\s*[\)）][：:\s]*([0-9]+(?:\.[0-9]+)?)"),
    re.compile(r"PE\s+TTM[：:\s]*([0-9]+(?:\.[0-9]+)?)"),
)

# 估值区间提取模式：区间位于 PE 之前，如 '120-130x PE' / '120~130倍PE' / '120至130 PE'。
_PE_BAND_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*[-–—~～至到]\s*(\d+(?:\.\d+)?)\s*[x×倍]*\s*PE"
)

# 溢价/抬升类表述：此类情景的 PE 区间上限应 ≥ 当前 PE(TTM)，否则为估值口径矛盾。
_PREMIUM_VERBS: tuple[str, ...] = (
    "溢价",
    "抬升",
    "拔估值",
    "估值扩张",
    "估值提升",
    "冲高",
    "享受",
)

# 回归/消化类表述：此类情景的 PE 低于当前 PE(TTM) 是合理的，不算矛盾。
_REVERSION_VERBS: tuple[str, ...] = (
    "回归",
    "消化",
    "回落",
    "压缩",
    "杀估值",
    "均值回归",
    "下杀",
)

# 在 PE 区间前回看的字符窗口长度（用于判定该区间所属的估值表述类型）。
_PE_CONTEXT_WINDOW = 50


@dataclass(frozen=True)
class PeContradiction:
    """PE 估值口径矛盾（溢价/抬升情景 PE 上限低于当前 PE(TTM)）。"""

    current_pe: float
    scenario_pe_high: float
    sentence: str
    detail: str


def _extract_current_pe(markdown: str) -> Optional[float]:
    """从报告头部提取当前 PE(TTM)。仅识别 TTM 限定口径，未声明则返回 None。"""
    for pattern in _CURRENT_PE_PATTERNS:
        match = pattern.search(markdown)
        if match:
            return float(match.group(1))
    return None


def _find_pe_bands(markdown: str) -> List[tuple[int, float, float]]:
    """提取估值区间 'low-high x PE'。返回 [(起始位置, 区间下限, 区间上限), ...]。"""
    return [
        (match.start(), float(match.group(1)), float(match.group(2)))
        for match in _PE_BAND_RE.finditer(markdown)
    ]


def _detect_pe_premium_contradictions(
    markdown: str, current_pe: Optional[float]
) -> List[PeContradiction]:
    """检测『溢价/抬升』情景 PE 区间上限低于当前 PE(TTM) 的估值口径矛盾。"""
    if current_pe is None:
        return []

    contradictions: List[PeContradiction] = []
    for pos, _low, high in _find_pe_bands(markdown):
        window = markdown[max(0, pos - _PE_CONTEXT_WINDOW) : pos]
        # 显式回归/消化表述 → PE 低于当前是合理的，跳过
        if any(verb in window for verb in _REVERSION_VERBS):
            continue
        # 溢价/抬升表述且区间上限 < 当前 PE → 估值口径矛盾
        if any(verb in window for verb in _PREMIUM_VERBS) and high < current_pe:
            contradictions.append(
                PeContradiction(
                    current_pe=current_pe,
                    scenario_pe_high=high,
                    sentence=window + markdown[pos : pos + 20],
                    detail=(
                        f"『溢价/抬升』情景 PE 上限 {high:g}x 低于当前 PE(TTM) "
                        f"{current_pe:g}x：估值口径矛盾（溢价情景 PE 应 ≥ 当前 PE，"
                        "或改用『回归/消化』表述并说明 EPS 增速如何吸收估值下降）"
                    ),
                )
            )
    return contradictions


class DeepResearchValidator:
    """深度投研报告质量校验器。"""

    def validate(
        self,
        markdown: str,
        tool_calls_log: Optional[List[Dict[str, object]]] = None,
    ) -> ValidationResult:
        """校验报告。纯函数，不抛异常。

        计分模型：
        - 每个非 optional 层满分 20（10 内容 + 10 工具），optional 层（技术面）满分 5。
        - 章节缺失每章 -3，最多 -15。
        - 结论不足每少 1 个 -2，最多 -10。
        - 国产替代 / 中美链缺失适用性判断，各额外 -5（最强收敛信号）。
        - optional 层（技术面）缺失工具 + 关键词同时缺，**不阻断** passed；只降分。
        """
        if not markdown or not markdown.strip():
            return ValidationResult(
                passed=False,
                score=0,
                missing_layers=list(_LAYER_REQUIREMENTS.keys()),
                details=["报告内容为空"],
            )

        called_tools = _extract_called_tools(tool_calls_log or [])
        details: List[str] = []
        missing_layers: List[str] = []
        missing_tool_groups: List[str] = []
        layer_score = 0.0

        for layer, requirement in _LAYER_REQUIREMENTS.items():
            check = _check_layer(layer, requirement, markdown, called_tools)
            optional = bool(requirement.get("optional", False))
            # optional 层（技术面）满分 5，其它层满分 20（10 内容 + 10 工具）。
            tool_groups: List[Any] = list(
                cast(Iterable[Any], requirement.get("tool_groups") or [])
            )
            layer_incomplete = False
            if tool_groups:
                if optional:
                    # optional 层：内容/工具都缺也只 -3 分，不阻断
                    if check["content_ok"]:
                        layer_point = 2.5
                    else:
                        layer_point = 0.0
                    if check["tools_ok"]:
                        layer_point += 2.5
                    else:
                        # 缺工具 + 缺内容 → 标记"缺少择时信息"但不阻断
                        if not check["content_ok"]:
                            details.append(
                                f"{layer}层：缺少择时信息（工具与关键词均未覆盖，建议补 analyze_trend / get_capital_flow 至少其一）"
                            )
                else:
                    layer_point = 0.0
                    if check["content_ok"]:
                        layer_point += 10.0
                    else:
                        layer_incomplete = True  # 内容关键词缺失也算该层不完整
                    if check["tools_ok"]:
                        layer_point += 10.0
                    else:
                        layer_incomplete = True
                        missing_tool_groups.extend(
                            cast(List[str], check["missing_groups"])
                        )
                    if layer_incomplete:
                        missing_layers.append(layer)
            else:
                # 无强制工具的层：满分 20（optional 5），靠内容关键词
                if optional:
                    layer_point = 5.0 if check["content_ok"] else 0.0
                else:
                    layer_point = 20.0 if check["content_ok"] else 0.0
                    if not check["content_ok"]:
                        missing_layers.append(layer)
            layer_score += layer_point

            # 必要信息提示（非 optional 层缺失时进入 details）
            if (not check["content_ok"] or not check["tools_ok"]) and not (
                optional and not check["content_ok"] and not check["tools_ok"]
            ):
                reasons: List[str] = []
                if not check["content_ok"]:
                    reasons.append("内容关键词缺失")
                if not check["tools_ok"]:
                    reasons.append("必要工具未调用")
                details.append(f"{layer}层：{'; '.join(reasons)}")

        # 章节完整性
        missing_sections = [s for s in _REQUIRED_SECTIONS if s not in markdown]

        # 国产替代 / 中美链适用性判断检测：缺则各 -5（额外信号，不进 missing_layers，
        # 但写 details 提示；白名单/黑名单行业可在调用方扩展）。
        applicability_penalty = 0
        for layer_key in ("国产替代", "中美产业链关系"):
            # 通过内容关键词层面已经覆盖；进一步要求"适用性判断"或"低相关/不适用"显式表述
            section_ok = _section_present(markdown, layer_key)
            if not section_ok:
                continue
            if not _has_applicability_judgment(markdown, layer_key):
                applicability_penalty += 5
                details.append(
                    f"{layer_key}层：未显式给出适用性判断（强相关/低相关/不适用），按 docs 方案应扣 5 分"
                )

        # 结论前置
        conclusion_count = _count_conclusions(markdown)
        # 概率和
        probability_sum = _check_probability_sum(markdown)

        # 综合评分（先按层覆盖计算底层分，再做章节/适用性/结论扣分，最后夹到 [0, 100]）
        # 注意：底层 layer_score 已含 optional 层的"满分 5"，所以理论上限 > 100；
        # 但语义上"满分"即"无任何扣分点"，先取 min(100, layer_score) 再扣分更直观，
        # 且让适用性/章节/结论扣分对满分报告**可见**（不会被超额底分吃掉）。
        score = min(100, int(round(layer_score)))
        # 章节缺失扣分
        score -= min(len(missing_sections) * 3, 15)
        # 适用性缺失扣分（强收敛信号：缺一次 -5，两个全缺 -10）
        score -= applicability_penalty
        # 结论不足扣分
        if conclusion_count < 7:
            score -= min((7 - conclusion_count) * 2, 10)
        score = max(0, min(100, score))

        # 阻断条件：required 层（含技术面之外的所有层）不能有 missing_layers，
        # 工具组不能缺，章节缺失 ≤ 1，结论 ≥ 5。
        # 技术面降权：作为 optional 层，缺失只降分不阻断。
        passed = (
            not missing_layers
            and not missing_tool_groups
            and len(missing_sections) <= 1
            and conclusion_count >= 5
        )

        if missing_sections:
            details.append(f"缺失章节：{', '.join(missing_sections)}")
        if conclusion_count < 7:
            details.append(f"结论前置标记仅 {conclusion_count} 个（期望 ≥7）")
        if probability_sum > 0 and not (95.0 <= probability_sum <= 105.0):
            details.append(f"三情景概率和为 {probability_sum:.0f}%（期望 100%）")

        # 双源验证标注统计（信息性，不扣分；LLM 按 system_prompt 标 ✓/⚠）
        verified, conflict = _count_validation_markers(markdown)
        if verified or conflict:
            details.append(
                f"双源验证标注：✓×{verified}（验证通过）/ ⚠×{conflict}（冲突已披露）"
            )

        # PE 估值口径一致性检测（非阻断 L2 信号：只记入 details，不改变 passed/score）
        current_pe = _extract_current_pe(markdown)
        pe_contradictions = _detect_pe_premium_contradictions(markdown, current_pe)
        if pe_contradictions:
            details.append(
                f"估值口径矛盾：{len(pe_contradictions)} 处『溢价/抬升』情景 PE 上限低于当前 PE(TTM) {current_pe:g}x"
            )

        return ValidationResult(
            passed=passed,
            score=score,
            missing_layers=missing_layers,
            missing_tool_groups=missing_tool_groups,
            missing_sections=missing_sections,
            conclusion_count=conclusion_count,
            probability_sum=probability_sum,
            details=details,
            pe_contradictions=pe_contradictions,
        )


def _section_present(markdown: str, layer_key: str) -> bool:
    """粗粒度判断 markdown 是否包含指定层对应的章节标题（按 _REQUIRED_SECTIONS 关键字）。

    例如 ``layer_key='国产替代'`` 会匹配「国产替代与平行替换」；``layer_key='中美产业链关系'``
    会匹配「中美产业链关系」；技术面层匹配「技术面节奏」。
    """
    if not markdown:
        return False
    for section in _REQUIRED_SECTIONS:
        if layer_key in section and section in markdown:
            return True
    return layer_key in markdown


def _extract_layer_section(markdown: str, layer_key: str) -> str:
    """提取指定层对应的一级章节内容（layer_key → 章节标题 → 切片到下个一级章节前）。

    用于把"适用性判断"的检测**限定到该层所在章节**，避免误把其它章节的"强相关"等同章引用过来。
    """
    if not markdown:
        return ""
    section_title = ""
    for section in _REQUIRED_SECTIONS:
        if layer_key in section:
            section_title = section
            break
    if not section_title:
        return ""
    # 兼容两种写法：markdown 里可能是 "## 三、国产替代与平行替换"（带数字前缀），
    # 也可能是直接 "国产替代与平行替换"。先精确匹配，失败再按 "…section_title" 模糊匹配。
    if section_title in markdown:
        start = markdown.index(section_title)
        rest = markdown[start + len(section_title) :]
    else:
        # 模糊：以 section_title 出现在 "## " 之后的位置
        match = re.search(
            r"^#{1,6}\s+[^\n]*?" + re.escape(section_title),
            markdown,
            flags=re.MULTILINE,
        )
        if match is None:
            return ""
        start = match.start()
        rest = markdown[match.end() :]
    # 找下一个一级章节（## 开头），强制要求前一个换行符避免误匹配本章节
    # 兼容 ## 一~九章 等中文标题，所以不做 (?!#) 严格限制
    next_match = re.search(r"\n## ", rest)
    if next_match is None:
        return rest
    return rest[: next_match.start()]


def _has_applicability_judgment(markdown: str, layer_key: str) -> bool:
    """判断指定层是否显式给出适用性判断。

    判定方法：在 markdown 中**该层对应的一级章节内**是否出现以下任一关键词：
      - ``低相关`` / ``不适用``：低相关行业的固定写法
      - ``强相关`` / ``适用性判断``：强相关行业的显式适用性表述
      - ``适用性：`` 之类冒号风格标注
      - 子章节标题包含「适用性判断」
    """
    if not markdown:
        return False
    # 仅检查该层所在章节，避免误判（关键修复）
    section = _extract_layer_section(markdown, layer_key)
    if not section:
        return False
    haystack = section
    for kw in (
        *_APPLICABILITY_LOW_KEYWORDS,
        *_APPLICABILITY_HIGH_KEYWORDS,
        "适用性：",
        "适用性:",
    ):
        if kw in haystack:
            return True
    if re.search(r"适用性判断", haystack):
        return True
    return False
    haystack = markdown
    for kw in (
        *_APPLICABILITY_LOW_KEYWORDS,
        *_APPLICABILITY_HIGH_KEYWORDS,
        "适用性：",
        "适用性:",
    ):
        if kw in haystack:
            return True
    # 子章节「3.1 适用性判断」/「5.1 适用性判断」等
    if re.search(r"适用性判断", haystack):
        return True
    return False
