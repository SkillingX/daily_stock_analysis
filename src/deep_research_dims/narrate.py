# -*- coding: utf-8 -*-
"""叙述型维度的 LLM 单轮调用：数字已由后台代码算好，LLM 只写叙述。

约束（需求 §3-2）：prompt 明令禁止输出/修改任何数字；返回值仅一段叙述文本，
失败静默降级为模板默认句（不阻断维度）。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)

_AGENTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
    "deep_research",
    "agents",
)


def load_dim_prompt(dim_id: str) -> str:
    """读取维度 prompt（缺失时返回空串，调用方走模板默认句）。"""
    path = os.path.join(_AGENTS_DIR, f"{dim_id}.md")
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def load_constitution() -> str:
    return load_dim_prompt("_constitution")


def narrate(
    llm_adapter: Any,
    dim_id: str,
    facts: Dict[str, Any],
    default_sentence: str,
    timeout: float = 90.0,
) -> str:
    """单轮叙述调用。facts 为已算好的结构化结果（JSON 序列化进 prompt）。

    LLM 被明确要求只写叙述、复述数字时不得改动；任何异常/空响应返回 default_sentence。
    """
    prompt = load_dim_prompt(dim_id)
    if not prompt or llm_adapter is None:
        return default_sentence
    import json

    system = (
        load_constitution()
        + "\n\n你是投研报告的分区叙述作者。只输出本分区的中文叙述段落（200 字内），"
        "禁止输出标题/列表/JSON；输入中的数字与结论必须原样复述，不得修改、不得新增数字。"
    )
    user = f"## 分区职责\n{prompt}\n\n## 已算好的事实（JSON）\n```json\n{json.dumps(facts, ensure_ascii=False, default=str)[:3000]}\n```"
    try:
        response = llm_adapter.call_text(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.3,
            timeout=timeout,
        )
        text = (response.content or "").strip()
        if len(text) < 20:
            return default_sentence
        return text
    except Exception as exc:  # noqa: BLE001 - 叙述失败只降级
        logger.warning("[DualTrack] 叙述生成失败 %s: %s", dim_id, exc)
        return default_sentence
