# -*- coding: utf-8 -*-
"""基本面研究员（业务画像）——独立研究员 Skill（SKILL.md + 本模块代码）。

代码自治：本模块持有该员的工具子集/契约解析/Skill 定义；探索循环为全研究员共享基建。
"""

from __future__ import annotations

from typing import Any

from src.agent.deep_research.explore_agents import run_business_agent
from src.deep_research.researchers.base import generic_parse
from src.schemas.deep_research_dims import BusinessDim

DIM_ID = "business"
DISPLAY_NAME = "基本面研究员（业务画像）"
SKILL_MD = "researchers/business/SKILL.md"
TOOLS = frozenset({'search_comprehensive_intel'})
TTL_HOURS = 24.0
SCORE_KEY = 'score'

runner_fn = run_business_agent


def _strip_noise_keys(value: Any) -> Any:
    """递归剔除纯背景噪音键（大盘指数原始数据块，渲染无投资价值且喧宾夺主）。"""
    if isinstance(value, dict):
        return {
            k: _strip_noise_keys(v)
            for k, v in value.items()
            if k != "index_environment_for_context_only"
        }
    if isinstance(value, list):
        return [_strip_noise_keys(v) for v in value]
    return value


def parse(parsed: dict[str, Any], steps: int) -> tuple[BusinessDim, int]:
    dim, used = generic_parse(DIM_ID, _strip_noise_keys(parsed or {}), steps)
    assert isinstance(dim, BusinessDim)
    if dim.data_gaps or not any((dim.business_model, dim.main_products, dim.competitive_position, dim.vs_market_leader)):
        dim = dim.model_copy(update={"status": "degraded", "degraded_reason": "业务资料不足，已有内容仅供部分参考"})
    return dim, used
