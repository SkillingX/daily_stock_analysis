# -*- coding: utf-8 -*-
"""
Pipeline 运行期上下文的类型契约。

``StockAnalysisPipeline._run`` 内部通过 ``initial_context`` / ``enriched_context`` /
``target_context`` 这样的 ``dict[str, Any]`` 在阶段之间传递数据。这些字典的 key
集合和 value 类型历史上靠注释维护，导致 mypy 无法做类型检查，下游到处需要
``Any | None`` 的兜底。

本文件提供一组 ``TypedDict`` 描述这些字典的形状。**新代码**应优先使用这些类型
作为注解；**旧代码**按 Batch 治理计划逐步替换，**不强制**一次性迁移。

使用约定：
- 所有 key 必填的字段用普通声明。
- 某些阶段才注入的可选字段用 ``NotRequired[...]``。
- 依赖外部服务实例的字段（``SearchService`` 等）也声明为 ``NotRequired``，
  因为它们通常在 ``__init__`` 之后才被注入。
"""

from __future__ import annotations

from datetime import date
from typing import Any, NotRequired, TypedDict


class _ServiceFields(TypedDict, total=False):
    """可选注入的运行时服务实例。新模块可加，老模块按需补充。"""

    search_service: Any
    notification_service: Any


class PipelineContextDict(_ServiceFields, total=False):
    """``StockAnalysisPipeline._run`` 内部使用的运行期上下文。

    必填字段（从初始 dict literal 注入）：
    - ``stock_code`` / ``stock_name`` / ``report_type`` / ``report_language``
    - ``fundamental_context``

    可选字段（按阶段注入）：
    - ``portfolio_context``、``market_phase_context``、``daily_market_context`` 等
    """

    stock_code: str
    stock_name: str
    report_type: str
    report_language: str
    fundamental_context: dict[str, Any]
    portfolio_context: NotRequired[dict[str, Any]]
    market_phase_context: NotRequired[dict[str, Any]]
    skills: NotRequired[Any]
    realtime_quote: NotRequired[dict[str, Any]]
    trend_result: NotRequired[dict[str, Any]]
    news_context: NotRequired[dict[str, Any]]
    analysis_context_pack_summary: NotRequired[str]
    belong_boards: NotRequired[list[str]]
    daily_market_context: NotRequired[dict[str, Any]]
    daily_market_context_summary: NotRequired[str]
    agent_skills_state: NotRequired[dict[str, Any]]


class DailyMarketContextDict(TypedDict, total=False):
    """``DailyMarketContext.to_safe_dict()`` 返回结构的类型契约。"""

    trading_date: NotRequired[date]
    market_phase: NotRequired[str]
    sentiment: NotRequired[str]
    breadth: NotRequired[dict[str, Any]]
    sectors: NotRequired[list[dict[str, Any]]]
    key_events: NotRequired[list[dict[str, Any]]]


__all__ = [
    "PipelineContextDict",
    "DailyMarketContextDict",
]
