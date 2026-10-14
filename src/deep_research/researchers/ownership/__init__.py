# -*- coding: utf-8 -*-
"""股权高管研究员——独立研究员 Skill（SKILL.md + 本模块代码）。

代码自治：本模块持有该员的工具子集/契约解析/Skill 定义；探索循环为全研究员共享基建。
"""

from __future__ import annotations
from typing import Any
import json
from data_provider.ifind_fundamental_adapter import IfindShareholders

from src.agent.deep_research.explore_agents import run_ownership_agent
from src.deep_research.researchers.base import generic_parse
from src.schemas.deep_research_dims import OwnershipDim

DIM_ID = "ownership"
DISPLAY_NAME = "股权高管研究员"
SKILL_MD = "researchers/ownership/SKILL.md"
TOOLS = frozenset(frozenset({'search_comprehensive_intel', 'search_stock_news', 'search_knowledge_base'}))
TTL_HOURS = 24.0
SCORE_KEY = 'score'

runner_fn = run_ownership_agent


def parse(parsed: dict[str, Any], steps: int) -> tuple[OwnershipDim, int]:
    payload = dict(parsed)
    details = payload.pop("shareholder_details", None)
    dim, used = generic_parse(DIM_ID, payload, steps)
    assert isinstance(dim, OwnershipDim)
    if details is not None:
        dim = dim.model_copy(update={"shareholder_details": IfindShareholders.model_validate_json(json.dumps(details))})
    missing = [label for label, value in (("实际控制人信息", dim.controller), ("十大股东", dim.top_holders or (dim.shareholder_details.items if dim.shareholder_details else [])), ("核心高管", dim.executives)) if not value]
    if missing or dim.data_gaps:
        dim = dim.model_copy(update={"status": "degraded", "score": None, "degraded_reason": "股权资料不足，已有内容仅供部分参考", "data_gaps": list(dict.fromkeys([*dim.data_gaps, *missing]))})
    return dim, used
