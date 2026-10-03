# -*- coding: utf-8 -*-
"""消息面研究员——独立研究员 Skill（SKILL.md + 本模块代码）。

代码自治：本模块持有该员的工具子集/契约解析/Skill 定义；探索循环为全研究员共享基建。
"""

from __future__ import annotations

from src.agent.deep_research.explore_agents import run_intel_agent
from src.deep_research.researchers.base import generic_parse

DIM_ID = "intel"
DISPLAY_NAME = "消息面研究员"
SKILL_MD = "researchers/news/SKILL.md"
TOOLS = frozenset(frozenset({'search_comprehensive_intel', 'get_market_indices', 'search_stock_news'}))
TTL_HOURS = 4.0
SCORE_KEY = None

runner_fn = run_intel_agent


def parse(parsed, steps):
    return generic_parse(DIM_ID, parsed, steps)
