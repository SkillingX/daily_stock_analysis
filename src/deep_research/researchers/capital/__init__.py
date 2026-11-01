# -*- coding: utf-8 -*-
"""资金面研究员——独立研究员 Skill（SKILL.md + 本模块代码）。

代码自治：本模块持有该员的工具子集/契约解析/Skill 定义；探索循环为全研究员共享基建。
"""

from __future__ import annotations

from typing import Any

from src.agent.deep_research.explore_agents import run_capital_agent
from src.deep_research.researchers.base import generic_parse
from src.schemas.deep_research_dims import DimEnvelope, parse_dim

DIM_ID = "capital"
DISPLAY_NAME = "资金面研究员"
SKILL_MD = "researchers/capital/SKILL.md"
TOOLS = frozenset({'get_capital_flow', 'get_stock_info'})
TTL_HOURS = 24.0
SCORE_KEY = 'flow_score'

runner_fn = run_capital_agent


def parse(parsed: dict[str, Any], steps: int) -> tuple[DimEnvelope, int]:
    from pydantic import ValidationError

    try:
        dim, used = generic_parse(DIM_ID, parsed, steps)
    except ValidationError:
        # issue #71/R5：契约拒绝窄边界——CapitalDim 契约仍是唯一范围权威
        #（拒绝不放宽、不加重复范围判断、不夹分不假成功），但采纳边界已校验的
        # 白名单证据随降级维度实际保存，失败可定位回放；固定脱敏原因不含原始
        # ValidationError 输入值；不宣称 adopted（契约拒绝即采纳门未通过）。
        payload: dict[str, Any] = {
            "dim": DIM_ID,
            "status": "degraded",
            "degraded_reason": "资金研究员模型输出未通过资金维度契约校验；失败形态证据随维度留存。",
        }
        evidence = parsed.get("adoption_evidence")
        if isinstance(evidence, dict):
            if evidence.get("outcome") == "adopted":
                evidence = {**evidence, "outcome": "contract_rejected"}
            payload["adoption_evidence"] = evidence
        dim, used = parse_dim(DIM_ID, payload), steps
    if parsed.get("status") == "degraded":
        dim = dim.model_copy(update={"status": "degraded", "degraded_reason": parsed.get("degraded_reason")})
    return dim, used
