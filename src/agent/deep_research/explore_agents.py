# -*- coding: utf-8 -*-
"""探索型维度 Agent：S3 情报 / L4 产业链（唯二需要 ReAct 工具循环的维度）。

工具子集精选（factory.py 模式），max_steps=8；输出强制 JSON，契约校验失败重试 1 次。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from src.agent.runner import run_agent_loop
from src.agent.tools.registry import ToolRegistry
from src.deep_research_dims.narrate import load_constitution, load_dim_prompt

logger = logging.getLogger(__name__)

_INTEL_TOOLS = {
    "search_stock_news",
    "search_comprehensive_intel",
    "get_market_indices",
}
_SUPPLY_CHAIN_TOOLS = {
    "search_comprehensive_intel",
    "verify_supply_chain_evidence",
    "get_sector_rankings",
    "get_stock_info",
}


def _build_registry(source_registry: Any, names: set[str]) -> Any:
    """从问股注册表复制工具子集到独立实例（不污染问股单例）。"""
    registry = ToolRegistry()
    for tool in source_registry.list_tools():
        if tool.name in names:
            registry.register(tool)
    missing = names - set(registry.list_names())
    if missing:
        logger.warning("[DualTrack] 探索维度缺失工具: %s", sorted(missing))
    return registry


def _registry_source() -> Any:
    from src.agent.factory import get_tool_registry

    return get_tool_registry()


def _run_explore(
    dim_id: str,
    tool_names: set[str],
    user_task: str,
    llm_adapter: Any,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    max_steps: int = 8,
    retry: int = 1,
) -> Dict[str, Any]:
    """跑一轮探索循环，返回 LLM 最终 JSON dict；失败重试 retry 次。"""
    system = (
        load_constitution()
        + "\n\n"
        + load_dim_prompt(dim_id)
        + "\n\n最终回答必须是且仅是一个 JSON 对象（不要 Markdown 代码围栏外的任何文字）。"
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_task},
    ]
    registry = _build_registry(_registry_source(), tool_names)

    last_error = ""
    for attempt in range(retry + 1):
        loop = run_agent_loop(
            messages=messages,
            tool_registry=registry,
            llm_adapter=llm_adapter,
            max_steps=max_steps,
            progress_callback=progress_callback,
            max_wall_clock_seconds=480.0,
            stock_scope=None,
        )
        parsed = _extract_json(loop.content)
        if parsed is not None:
            return {"ok": True, "data": parsed, "steps": loop.total_steps}
        last_error = loop.error or "LLM 未输出可解析 JSON"
        messages.append(
            {
                "role": "user",
                "content": "你的最终回答不是合法 JSON。请重新输出：仅一个 JSON 对象，无其他文字。",
            }
        )
    return {"ok": False, "error": last_error}


def _extract_json(content: str) -> Optional[Dict[str, Any]]:
    if not content:
        return None
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        import json

        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        pass
    try:
        import json_repair  # type: ignore[import-not-found]

        obj = json_repair.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:  # noqa: BLE001
        return None


def run_intel_agent(
    stock_code: str,
    stock_name: str,
    llm_adapter: Any,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    max_steps: int = 8,
) -> Dict[str, Any]:
    """S3 情报探索：新闻/公告/风险/催化剂 + 贝叶斯证据候选。"""
    task = (
        f"请对 A 股 {stock_name}（{stock_code}）执行情报收集并直接输出 JSON：\n"
        "1) 先用 search_stock_news 查个股新闻公告，search_comprehensive_intel 查宏观/行业情报，"
        "get_market_indices 取大盘环境；\n"
        "2) 输出 JSON 字段：sentiment_summary(字符串)、earnings_outlook(字符串)、"
        "risk_alerts(字符串数组)、positive_catalysts(字符串数组)、latest_news(字符串)、"
        "unverified_count(整数：其中来源不可靠/社区传闻的条数)、"
        "evidence_items(数组，元素：{evidence,strength,lr,date}；strength 取 "
        "strong_positive/weak_positive/neutral/weak_negative/strong_negative 之一，"
        "lr 为该证据似然比(正数)，date 为 YYYY-MM-DD；只收录近 90 天、可支撑长线判断的证据，"
        "最多 6 条)。\n"
        "来源等级标注（primary/news/industry/community_*/inferred/unverified）写在字符串内容里。"
    )
    return _run_explore(
        "intel", _INTEL_TOOLS, task, llm_adapter, progress_callback, max_steps
    )


def run_supply_chain_agent(
    stock_code: str,
    stock_name: str,
    llm_adapter: Any,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    max_steps: int = 8,
) -> Dict[str, Any]:
    """L4 产业链探索：图谱/定位/瓶颈 + 双源校验。"""
    task = (
        f"请对 A 股 {stock_name}（{stock_code}）执行产业链调研并直接输出 JSON：\n"
        "1) 用 search_comprehensive_intel 调研产业链位置与瓶颈，get_sector_rankings 看板块归属，"
        "verify_supply_chain_evidence 做东财+同花顺双源校验，get_stock_info 取基本面佐证；\n"
        "2) 输出 JSON 字段：company_position(字符串，必填：公司在产业链中的环节定位)、"
        "chain_map(数组，元素：{level(上/中/下游之一), companies(字符串数组), concentration(可选字符串)})、"
        "chokepoints(数组，元素：{type(取 patent/capacity/geo/tech/cert 之一), description(字符串), "
        "confidence(取 high/medium/low)})、upstream(字符串数组)、downstream(字符串数组)、"
        "bargaining_power(字符串)、us_china_chain(对象：{role, substitution_progress, sanction_risk, "
        "dual_chain_impact}，低相关行业可省略该对象)、"
        "verification_status(取 confirmed/partial/conflict/unverified/not_applicable 之一)。\n"
        "所有具体客户/份额/产能数字必须带来源等级标签；取不到就写 inferred 并说明依据。"
    )
    return _run_explore(
        "supply_chain",
        _SUPPLY_CHAIN_TOOLS,
        task,
        llm_adapter,
        progress_callback,
        max_steps,
    )
