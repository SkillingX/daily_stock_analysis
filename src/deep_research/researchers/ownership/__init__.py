# -*- coding: utf-8 -*-
"""股权高管研究员——独立研究员 Skill（SKILL.md + 本模块代码）。

代码自治：本模块持有该员的工具子集/契约解析/Skill 定义；探索循环为全研究员共享基建。
"""

from __future__ import annotations
from typing import Any
import json
from pydantic import ValidationError

from data_provider.ifind_fundamental_adapter import IfindShareholders

from src.deep_research.researchers.base import generic_parse
from src.schemas.deep_research_dims import HolderInfo, OwnershipDim

# issue #64：股权高管研究员已退役（不再注册进 registry、不启动 runner/取数）。
# 本模块仅保留 read-only 解析（历史 19 项产物的兼容读取防线），无出站。
DIM_ID = "ownership"
DISPLAY_NAME = "股权高管研究员（本版已停用）"
SKILL_MD = "researchers/ownership/SKILL.md"
TOOLS = frozenset()
TTL_HOURS = 24.0
SCORE_KEY = 'score'


def _adapt_holder_list(value: Any, label: str) -> tuple[list[HolderInfo], list[str]]:
    """股权三个列表字段的已知无歧义形状适配，再交严格条目契约校验。

    模型偶尔把列表输出成 JSON 数组字符串或 {"window":…, "items":[…]} 包装对象，
    会让整维严格校验失败、丢掉已取得的核心资料。这里只解包明确形状（JSON 字符串
    严格解码、包装对象只认 items 记录列表），逐条过 HolderInfo；空对象被条目
    模型填占位名的记录不算有效条目。无法识别或校验失败的条目记为 {label} 的
    具体缺项。
    """
    if value is None:
        return [], []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return [], [f"{label}未解析：模型输出为非法 JSON 字符串"]
    if isinstance(value, dict):
        items = value.get("items")
        if not isinstance(items, list):
            return [], [f"{label}未解析：模型输出为无法识别的对象结构"]
        value = items
    if not isinstance(value, list):
        return [], [f"{label}未解析：模型输出类型无法识别"]
    valid: list[HolderInfo] = []
    dropped = 0
    for item in value:
        try:
            holder = HolderInfo.model_validate(item)
        except ValidationError:
            dropped += 1
            continue
        if holder.name == HolderInfo.PLACEHOLDER_NAME:
            dropped += 1
            continue
        valid.append(holder)
    gaps = [f"{label}有 {dropped} 条记录未通过校验，已剔除"] if dropped else []
    return valid, gaps


def _adapt_data_gaps(value: object) -> tuple[list[str], bool]:
    """只恢复明确的缺口文字；未知形状显式披露，不把对象字符串化。"""
    if not isinstance(value, list):
        return ["资料缺口未解析：模型输出应为数组，需重新核实"], True
    gaps: list[str] = []
    invalid = 0
    for entry in value:
        if isinstance(entry, dict) and isinstance(entry.get("item"), str) and entry["item"].strip():
            entry = "；".join(
                text.strip() for key in ("item", "reason", "detail")
                if isinstance(text := entry.get(key), str) and text.strip()
            )
        if isinstance(entry, str) and entry.strip():
            gaps.append(entry.strip())
        else:
            invalid += 1
    if invalid:
        gaps.append(f"资料缺口有 {invalid} 条记录无法识别，需重新核实")
    return list(dict.fromkeys(gaps)), bool(invalid)


def parse(parsed: dict[str, Any], steps: int) -> tuple[OwnershipDim, int]:
    payload = dict(parsed)
    details = payload.pop("shareholder_details", None)
    payload["data_gaps"], unparsed_gaps = _adapt_data_gaps(payload.get("data_gaps", []))
    if payload.get("core_data_gaps") is not None:
        payload["core_data_gaps"], _ = _adapt_data_gaps(payload["core_data_gaps"])
    adapted = {
        key: _adapt_holder_list(payload.get(key), label)
        for key, label in (("top_holders", "前十大股东"), ("executives", "核心高管"), ("recent_changes", "近一年股权变动"))
    }
    payload.update({key: records for key, (records, _gaps) in adapted.items()})
    dim, used = generic_parse(DIM_ID, payload, steps)
    assert isinstance(dim, OwnershipDim)
    if details is not None:
        dim = dim.model_copy(update={"shareholder_details": IfindShareholders.model_validate_json(json.dumps(details))})
    supplier_gaps: list[str] = []
    if dim.shareholder_details and (dim.shareholder_details.errors or dim.shareholder_details.coverage != "complete"):
        supplier_gaps = ["iFinD股东资料覆盖不完整", *dim.shareholder_details.errors]
    # 未分类的旧缺口保持保守处理；新研究明确区分核心缺口与补充资料。
    adapt_gaps = [gap for records, gaps in adapted.values() for gap in gaps]
    controller_known = dim.controller.strip() and dim.controller.strip() not in {"未知", "暂无", "待补充", "N/A", "缺失", "未披露", "—", "-", "资料缺失"}
    missing = [label for label, value in (("实际控制人信息", controller_known), ("十大股东", dim.top_holders or (dim.shareholder_details.items if dim.shareholder_details else [])), ("核心高管", dim.executives)) if not value]
    core_adapt_gaps = [gap for key in ("top_holders", "executives") for gap in adapted[key][1]]
    core_gaps = list(dict.fromkeys([*(dim.core_data_gaps or []), *supplier_gaps, *missing, *core_adapt_gaps]))
    gaps = list(dict.fromkeys([*dim.data_gaps, *core_gaps, *adapt_gaps]))
    unclassified = dim.core_data_gaps is None and bool(dim.data_gaps)
    updates: dict[str, Any] = {"data_gaps": gaps}
    if dim.core_data_gaps is not None:
        updates["core_data_gaps"] = core_gaps
    if core_gaps or unclassified or unparsed_gaps or dim.status == "degraded":
        updates.update(status="degraded", score=None, degraded_reason=dim.degraded_reason or "股权核心资料不足或未核实，已有内容仅供部分参考")
    dim = dim.model_copy(update=updates)
    return dim, used
