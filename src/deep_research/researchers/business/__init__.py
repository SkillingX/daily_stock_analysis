# -*- coding: utf-8 -*-
"""基本面研究员（业务画像）——独立研究员 Skill（SKILL.md + 本模块代码）。

代码自治：本模块持有该员的工具子集/契约解析/Skill 定义；探索循环为全研究员共享基建。
"""

from __future__ import annotations

from typing import Any
import json
from data_provider.ifind_fundamental_adapter import ProductSegments

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


def _substantive(value: Any) -> bool:
    if isinstance(value, dict):
        text = str(value.get("value") or value.get("summary") or "").strip()
    else:
        text = str(value or "").strip()
    return bool(text) and text not in {"未知", "暂无", "待补充", "N/A", "无", "缺失", "未披露", "—", "-"}


def _core_complete(dim: BusinessDim) -> bool:
    products = _substantive(dim.main_products) or bool(dim.product_segments and dim.product_segments.items)
    return _substantive(dim.business_model) and products


def parse(parsed: dict[str, Any], steps: int) -> tuple[BusinessDim, int]:
    payload = _strip_noise_keys(parsed or {})
    products = payload.pop("product_segments", None)
    dim, used = generic_parse(DIM_ID, payload, steps)
    assert isinstance(dim, BusinessDim)
    if products is not None:
        dim = dim.model_copy(update={"product_segments": ProductSegments.model_validate_json(json.dumps(products))})
    if dim.status == "degraded" and dim.degraded_reason:
        return dim, used
    if not _core_complete(dim):
        dim = dim.model_copy(update={"status": "degraded", "degraded_reason": dim.degraded_reason or "业务资料不足，已有内容仅供部分参考"})
    return dim, used
