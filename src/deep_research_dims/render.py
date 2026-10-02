# -*- coding: utf-8 -*-
"""双轨报告成文：Jinja 模板直灌（零 LLM），数字全部来自结构化维度数据。

含成文后结构校验（validator 两层化中的"成文后结构校验"层）。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.schemas.deep_research_dims import (
    BayesianDim,
    ConclusionDim,
    DataDim,
    GuardrailEvent,
    HistoryDim,
    IntelDim,
    PhaseDim,
    PlanDim,
    ScenariosDim,
    SignalDim,
    SixDimDim,
    SupplyChainDim,
)

_TEMPLATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "templates",
)
_TEMPLATE_NAME = "deep_research_dual_track.j2"

REQUIRED_HEADINGS = [
    "## 一、结论",
    "## 二、盘面解读",
    "## 三、走势预测",
    "## 四、六维评分总表",
    "## 五、详情分析",
    "## 六、交易参考",
    "## 附录",
]


def _degraded_note(dim: Any) -> str:
    status = getattr(dim, "status", "ok")
    if status == "degraded":
        return f"⚠️ 本维度生成不充分：{getattr(dim, 'degraded_reason', '未知原因')}，以下内容为降级占位。"
    if status == "skipped":
        return "— 本维度未选择生成（省钱模式），以下内容为默认占位，不代表分析结论。"
    return ""


def build_view(
    stock_name: str,
    stock_code: str,
    as_of: str,
    ctx_limitations: List[str],
    dims: Dict[str, Any],
    guardrail_events: List[GuardrailEvent],
    report_id: str = "",
    final_conclusion: str = "",
) -> Dict[str, Any]:
    """把契约模型装配成模板视图（纯数据搬运，不做计算）。"""
    signal: SignalDim = dims["signal"]
    data: DataDim = dims["data"]
    intel: IntelDim = dims["intel"]
    plan: PlanDim = dims["plan"]
    phase: PhaseDim = dims["phase"]
    history: HistoryDim = dims["history"]
    six_dim: SixDimDim = dims["six_dim"]
    bayesian: BayesianDim = dims["bayesian"]
    conclusion: ConclusionDim = dims["conclusion"]
    supply_chain: SupplyChainDim = dims["supply_chain"]
    scenarios: ScenariosDim = dims["scenarios"]
    fundamental: Any = dims["fundamental"]
    sector: Any = dims["sector"]

    return {
        "stock_name": stock_name,
        "stock_code": stock_code,
        "as_of": as_of,
        "report_id": report_id,
        "final_conclusion": final_conclusion,
        "dim_anchor_items": [
            {"id": dim_id, "anchor": anchor}
            for dim_id, anchor in DIM_SECTION_ANCHORS.items()
        ],
        "signal": signal,
        "data": data,
        "intel": intel,
        "plan": plan,
        "phase": phase,
        "history": history,
        "six_dim": six_dim,
        "bayesian": bayesian,
        "conclusion": conclusion,
        "supply_chain": supply_chain,
        "scenarios": scenarios,
        "fundamental": fundamental,
        "sector": sector,
        "guardrail_events": guardrail_events,
        "limitations": ctx_limitations,
        "degraded_note": _degraded_note,
    }


def render_markdown(view: Dict[str, Any]) -> str:
    env = Environment(
        loader=FileSystemLoader(_TEMPLATE_DIR),
        autoescape=select_autoescape(enabled_extensions=()),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    template = env.get_template(_TEMPLATE_NAME)
    return template.render(**view)


def validate_structure(markdown: str) -> List[str]:
    """成文后结构校验：返回缺失章节列表（空=通过）。"""
    return [h for h in REQUIRED_HEADINGS if h not in markdown]


# ---------------------------------------------------------------------------
# 按维度切分子报告（方向 A：每维度独立 .md，内容即合并报告对应章节，天然满足
# 「子报告 ≥ 合并报告章节」验收；skipped 维度章节含"未选择生成"注记）
# ---------------------------------------------------------------------------

# 维度 id → 报告章节锚点（#### 级，与五段模板严格对齐；切分子报告用）
DIM_SECTION_ANCHORS: Dict[str, str] = {
    "fundamental": "#### 财务与基本面（F1）",
    "sector": "#### 板块分析（F2）",
    "supply_chain": "#### 产业链解读",
    "intel": "#### 消息面详析",
    "six_dim": "#### 六维指标明细",
    "scenarios": "#### 情景与时间层级",
    "bayesian": "#### 贝叶斯证据链",
    "conclusion": "#### 投资结论",
    "signal": "#### 信号",
    "data": "#### 数据透视",
    "plan": "#### 作战计划",
    "phase": "#### 阶段决策",
    "history": "#### 历史对比",
}


def split_dim_sections(markdown: str) -> Dict[str, str]:
    """把整份报告切成 {dim_id: 章节 markdown}。

    章节范围 = 锚点行（### 开头）到下一个 ###/## 标题前。锚点缺失的维度不出现在
    结果里（调用方按缺失处理）。切分基于模板章节锚点，与成文结构同源，不复制
    模板代码。
    """
    lines = markdown.split("\n")
    # 锚点行号（按出现顺序）
    anchors: List[tuple[str, int]] = []
    for dim_id, anchor in DIM_SECTION_ANCHORS.items():
        for i, line in enumerate(lines):
            if line.strip() == anchor:
                anchors.append((dim_id, i))
                break
    anchors.sort(key=lambda x: x[1])

    sections: Dict[str, str] = {}
    for idx, (dim_id, start) in enumerate(anchors):
        end = len(lines)
        for j in range(start + 1, len(lines)):
            stripped = lines[j].strip()
            if (
                stripped.startswith("#### ")
                or stripped.startswith("### ")
                or stripped.startswith("## ")
            ):
                end = j
                break
        sections[dim_id] = "\n".join(lines[start:end]).strip() + "\n"
    return sections


def build_dim_report(
    dim_id: str,
    section_markdown: str,
    stock_name: str,
    stock_code: str,
    as_of: str,
) -> str:
    """单维度子报告：小头（标题/数据截至/免责）+ 章节正文。"""
    label = DIM_SECTION_ANCHORS[dim_id].lstrip("# ").strip()
    header = (
        f"# {stock_name}（{stock_code}）· {label}（子报告）\n\n"
        f"> 数据截至：{as_of}｜摘自双轨深度投研报告，单维度详版\n\n"
    )
    footer = "\n---\n\n*本报告由 AI 生成，不构成投资建议。*\n"
    return header + section_markdown.rstrip() + "\n" + footer
