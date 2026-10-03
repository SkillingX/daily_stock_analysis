# -*- coding: utf-8 -*-
"""研究员注册表：dim_id → Researcher。新增研究员 = 一个模块 + 一条记录。"""

from __future__ import annotations

from typing import Dict

from src.deep_research.researchers.base import Researcher
from src.deep_research.researchers import (
    business, capital, news, ownership, sentiment, supply_chain, technical, us_china,
)

# runner 以名字引用：编排器从全局按名解析（保证测试可 monkeypatch）
# 键一律为维度 id（DIM_IDS）；模块名与维度 id 不同者（news→intel）在此映射
_RUNNER_NAMES = {
    "intel": "run_intel_agent",
    "supply_chain": "run_supply_chain_agent",
    "technical": "run_technical_agent",
    "capital": "run_capital_agent",
    "sentiment": "run_sentiment_agent",
    "ownership": "run_ownership_agent",
    "us_china": "run_us_china_agent",
    "business": "run_business_agent",
}

_MODULES = {
    "news": news, "supply_chain": supply_chain, "technical": technical,
    "capital": capital, "sentiment": sentiment, "ownership": ownership,
    "us_china": us_china, "business": business,
}

REGISTRY: Dict[str, Researcher] = {
    mod.DIM_ID: Researcher(
        dim_id=mod.DIM_ID,
        display_name=mod.DISPLAY_NAME,
        skill_md=mod.SKILL_MD,
        tools=mod.TOOLS,
        runner=None,  # 运行时按 _RUNNER_NAMES 从编排器全局解析
        parser=mod.parse,
        ttl_hours=mod.TTL_HOURS,
        score_key=mod.SCORE_KEY,
    )
    for dim, mod in _MODULES.items()
}

researcher_dim_ids = tuple(REGISTRY.keys())
RUNNER_NAMES = _RUNNER_NAMES
