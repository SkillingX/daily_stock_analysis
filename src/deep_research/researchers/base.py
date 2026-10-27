# -*- coding: utf-8 -*-
"""研究员基座：注册记录、LLM 输出类型矫正、通用契约解析。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from src.report_language import adopt_structured_source_grades
from src.schemas.deep_research_dims import DIM_MODELS, DimEnvelope


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
        for sub in ("summary", "note", "text", "reason", "interpretation", "description", "analysis"):
            if isinstance(value.get(sub), str):
                return value[sub]
        import json as _json

        return _json.dumps(value, ensure_ascii=False)
    return str(value)


def generic_parse(dim_id: str, parsed: Dict[str, Any], steps: int) -> tuple[DimEnvelope, int]:
    """通用契约解析：字段过滤 + 类型矫正（研究员特殊解析在各模块自治）。"""
    model = DIM_MODELS[dim_id]
    fields = model.model_fields
    payload: Dict[str, Any] = {}
    parsed = adopt_structured_source_grades(parsed or {})
    if not isinstance(parsed, dict):
        parsed = {}
    for k, v in parsed.items():
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
    # Parsing succeeds independently of whether the research is complete.
    payload["status"] = "degraded" if payload.get("status") == "degraded" or payload.get("degraded_reason") else "ok"
    return model(**payload), steps


def _coerce_list(dim_id: str, key: str, value: list) -> list:
    """ownership 三个列表由该研究员的适配边界完成条目校验后原样传入；其余 list 原样返回。"""
    return value
