# -*- coding: utf-8 -*-
"""基本面研究员（业务画像）——独立研究员 Skill（SKILL.md + 本模块代码）。

代码自治：本模块持有该员的工具子集/契约解析/Skill 定义；探索循环为全研究员共享基建。
"""

from __future__ import annotations

from typing import Any

from src.agent.deep_research.explore_agents import run_business_agent
from src.deep_research.researchers.base import generic_parse

DIM_ID = "business"
DISPLAY_NAME = "基本面研究员（业务画像）"
SKILL_MD = "researchers/business/SKILL.md"
TOOLS = frozenset(frozenset({'search_comprehensive_intel', 'get_market_indices', 'search_stock_news'}))
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


def parse(parsed, steps):
    return generic_parse(DIM_ID, _strip_noise_keys(parsed or {}), steps)
