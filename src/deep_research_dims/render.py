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
    "## 短线六件套",
    "### 一、信号",
    "### 二、数据透视",
    "### 三、情报",
    "### 四、作战计划",
    "### 五、阶段决策",
    "### 六、历史对比",
    "## 长线五段式",
    "### 七、投资结论",
    "### 八、产业链解读",
    "### 九、长期价值与情景",
    "### 十、贝叶斯证据链",
    "### 十一、六维评分明细",
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

    return {
        "stock_name": stock_name,
        "stock_code": stock_code,
        "as_of": as_of,
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
