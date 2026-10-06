# -*- coding: utf-8 -*-
"""研究员基座：注册记录、LLM 输出类型矫正、通用契约解析。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from src.schemas.deep_research_dims import DIM_MODELS, DimEnvelope, HolderInfo


@dataclass(frozen=True)
class Researcher:
    """一位研究员 = 一个独立 Skill（SKILL.md + 本模块代码）。

    runner: (stock_code, stock_name, llm_adapter, progress_cb, max_steps) -> dict
    parser: (parsed_dict, steps) -> (DimEnvelope, steps)
    """

    dim_id: str
    display_name: str
    skill_md: str  # researchers/<dir>/SKILL.md
    tools: frozenset
    runner: Callable[..., Dict[str, Any]]
    parser: Callable[[Dict[str, Any], int], tuple]
    ttl_hours: float
    score_key: Optional[str] = "score"  # 供 six_dim 直供评分；None=详报型
    notes: str = ""


def coerce_value(key: str, value: Any) -> Any:
    """LLM 输出类型矫正：dict → summary/note 子字段或紧凑 JSON；score 类取数值。"""
    if value is None or isinstance(value, (str, int, float, bool, list)):
        return value
    if isinstance(value, dict):
        if key.endswith("score") or key == "score":
            for sub in ("score", "value", "data"):
                if isinstance(value.get(sub), (int, float)):
                    return value[sub]
            return None
        for sub in ("summary", "note", "text", "reason"):
            if isinstance(value.get(sub), str):
                return value[sub]
        import json as _json

        return _json.dumps(value, ensure_ascii=False)[:300]
    return str(value)


def generic_parse(dim_id: str, parsed: Dict[str, Any], steps: int) -> tuple[DimEnvelope, int]:
    """通用契约解析：字段过滤 + 类型矫正（研究员特殊解析在各模块自治）。"""
    model = DIM_MODELS[dim_id]
    fields = model.model_fields
    payload: Dict[str, Any] = {}
    for k, v in (parsed or {}).items():
        if k not in fields:
            continue
        ann = fields[k].annotation
        # List[HolderInfo] / List[dict] → 展平 name 字段拼成字符串列表
        if isinstance(v, list) and ann not in (str, int, float, bool):
            payload[k] = _coerce_list(dim_id, k, v)
        elif (isinstance(ann, type) and issubclass(ann, dict)) or getattr(ann, "__origin__", None) is dict:
            payload[k] = v if isinstance(v, dict) else ({} if v is None else {"value": v})
        else:
            payload[k] = coerce_value(k, v)
    payload["status"] = "ok"
    return model(**payload), steps


def _coerce_list(dim_id: str, key: str, value: list) -> list:
    """List[dict] → List[str]，对 ownership 三字段取 dict["name"] 拼字符串；其余原样返回。"""
    ownership_list_keys = {"top_holders", "executives", "recent_changes"}
    if dim_id == "ownership" and key in ownership_list_keys:
        result = []
        for item in value:
            if isinstance(item, dict):
                name = item.get("name") or item.get("holder_name") or str(item)
            elif isinstance(item, str):
                name = item
            else:
                name = str(item)
            result.append(HolderInfo(name=name))
        return result
    # 普通 list（如 List[str]）原样返回
    return value
