# -*- coding: utf-8 -*-
"""资金面研究员——独立研究员 Skill（SKILL.md + 本模块代码）。

代码自治：本模块持有该员的工具子集/契约解析/Skill 定义；探索循环为全研究员共享基建。
"""

from __future__ import annotations

from src.agent.deep_research.explore_agents import run_capital_agent
from src.deep_research.researchers.base import generic_parse

DIM_ID = "capital"
DISPLAY_NAME = "资金面研究员"
SKILL_MD = "researchers/capital/SKILL.md"
TOOLS = frozenset(frozenset({'get_chip_distribution', 'get_capital_flow', 'get_stock_info'}))
TTL_HOURS = 24.0
SCORE_KEY = 'flow_score'

runner_fn = run_capital_agent


def parse(parsed, steps):
    return generic_parse(DIM_ID, parsed, steps)
