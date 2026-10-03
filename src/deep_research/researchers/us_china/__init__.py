# -*- coding: utf-8 -*-
"""中美竞争研究员——独立研究员 Skill（SKILL.md + 本模块代码）。

代码自治：本模块持有该员的工具子集/契约解析/Skill 定义；探索循环为全研究员共享基建。
"""

from __future__ import annotations

from src.agent.deep_research.explore_agents import run_us_china_agent
from src.deep_research.researchers.base import generic_parse

DIM_ID = "us_china"
DISPLAY_NAME = "中美竞争研究员"
SKILL_MD = "researchers/us_china/SKILL.md"
TOOLS = frozenset(frozenset({'search_comprehensive_intel', 'verify_supply_chain_evidence'}))
TTL_HOURS = 120.0
SCORE_KEY = None

runner_fn = run_us_china_agent


def parse(parsed, steps):
    return generic_parse(DIM_ID, parsed, steps)
