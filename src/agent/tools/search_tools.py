# -*- coding: utf-8 -*-
"""
Search tools — wraps SearchService methods as agent-callable tools.

Tools:
- search_stock_news: search latest stock news
- search_comprehensive_intel: multi-dimensional intelligence search
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from src.agent.tools.registry import ToolParameter, ToolDefinition

logger = logging.getLogger(__name__)


def _get_db():
    """Lazy import for DatabaseManager."""
    from src.storage import get_db

    return get_db()


def _get_search_service():
    """Return shared SearchService singleton."""
    from src.search_service import get_search_service

    return get_search_service()


def _canonical_search_code(stock_code: str) -> str:
    from data_provider.base import canonical_stock_code, normalize_stock_code

    return canonical_stock_code(normalize_stock_code(str(stock_code or "").strip()))


def _persist_news_response(
    *,
    stock_code: str,
    stock_name: str,
    dimension: str,
    response,
    source_type: Optional[str] = None,
    reliability_hint: Optional[str] = None,
) -> None:
    """Best-effort news persistence for Agent search tools.

    P3 信息源分层（按 docs/deep-research-chain-news-logic-plan.md §信息源策略）：
    调用方可在 ``source_type`` 传入 ``official / news / industry / community_cn /
    community_global``，``reliability_hint`` 传入 ``high / medium / low / unverified``。
    老调用方不传 → 写 NULL（与历史 primary/news 隐式语义一致）。
    """
    if (
        not response
        or not getattr(response, "success", False)
        or not getattr(response, "results", None)
    ):
        return

    code = _canonical_search_code(stock_code)
    try:
        saved_count = _get_db().save_news_intel(
            code=code,
            name=stock_name,
            dimension=dimension,
            query=response.query,
            response=response,
            query_context=None,
            source_type=source_type,
            reliability_hint=reliability_hint,
        )
        logger.info(
            "Agent news intel persisted for %s (dimension=%s, source_type=%s, "
            "reliability=%s, new_records=%s)",
            code,
            dimension,
            source_type,
            reliability_hint,
            saved_count,
        )
    except Exception as exc:
        logger.warning(
            "Agent news intel persistence failed for %s (dimension=%s, source_type=%s): %s",
            code,
            dimension,
            source_type,
            exc,
        )


def _handle_search_stock_news(stock_code: str, stock_name: str) -> dict[str, Any]:
    """Search latest news for a stock."""
    service = _get_search_service()

    if not service.is_available:
        return {"error": "No search engine available (no API keys configured)"}

    response = service.search_stock_news(stock_code, stock_name, max_results=5)

    if not response.success:
        return {
            "query": response.query,
            "success": False,
            "error": response.error_message,
        }

    _persist_news_response(
        stock_code=stock_code,
        stock_name=stock_name,
        dimension="latest_news",
        response=response,
    )

    return {
        "query": response.query,
        "provider": response.provider,
        "success": True,
        "results_count": len(response.results),
        "results": [
            {
                "title": r.title,
                "snippet": r.snippet,
                "url": r.url,
                "source": r.source,
                "published_date": r.published_date,
            }
            for r in response.results
        ],
    }


search_stock_news_tool = ToolDefinition(
    name="search_stock_news",
    description="Search for the latest news articles about a specific stock. "
    "Requires both stock_code and stock_name for accurate search. "
    "Returns news titles, snippets, sources, and URLs.",
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="Stock code, e.g., '600519'",
        ),
        ToolParameter(
            name="stock_name",
            type="string",
            description="Stock name in Chinese, e.g., '贵州茅台'",
        ),
    ],
    handler=_handle_search_stock_news,
    category="search",
)


# ============================================================
# search_comprehensive_intel
# ============================================================


def _handle_search_comprehensive_intel(
    stock_code: str, stock_name: str, *, profile: str = "comprehensive"
) -> dict[str, Any]:
    """Multi-dimensional intelligence search."""
    service = _get_search_service()

    if not service.is_available:
        return {"error": "No search engine available (no API keys configured)"}

    intel_results = service.search_comprehensive_intel(
        stock_code=stock_code,
        stock_name=stock_name,
        max_searches=3 if profile == "business" else 6,
        profile=profile,
    )

    if not intel_results:
        return {"error": "Comprehensive intel search returned no results"}

    # Format into readable report
    report = service.format_intel_report(intel_results, stock_name) if profile != "business" else None

    # Also return structured data
    dimensions = {}
    for dim_name, response in intel_results.items():
        if response and response.success:
            _persist_news_response(
                stock_code=stock_code,
                stock_name=stock_name,
                dimension=dim_name,
                response=response,
            )
            dimensions[dim_name] = {
                "query": response.query,
                "results_count": len(response.results),
                "results": [
                    {
                        "title": r.title,
                        "snippet": r.snippet,
                        "source": r.source,
                        "url": r.url,
                        "published_date": r.published_date,
                    }
                    for r in response.results[
                        :3
                    ]  # limit to 3 per dimension to save tokens
                ],
            }

        elif profile == "business":
            dimensions[dim_name] = {"results": [], "error": "资料获取失败"}

    if profile == "business":
        seen: list[dict[str, Any]] = []
        for dimension in dimensions.values():
            records = dimension["results"]
            unique = []
            for record in records:
                if record not in seen:
                    unique.append(record)
                    seen.append(record)
            dimension["results"] = unique
            dimension["results_count"] = len(unique)
            dimension["duplicate_results_omitted"] = len(records) - len(unique)
        return {"dimensions": dimensions}
    return {"report": report, "dimensions": dimensions}


search_comprehensive_intel_tool = ToolDefinition(
    name="search_comprehensive_intel",
    description="Multi-dimensional intelligence search: latest news, market analysis, "
    "risk checking, earnings outlook, and industry trends for a stock. "
    "Returns a formatted report and structured results.",
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="Stock code, e.g., '600519'",
        ),
        ToolParameter(
            name="stock_name",
            type="string",
            description="Stock name in Chinese, e.g., '贵州茅台'",
        ),
    ],
    handler=_handle_search_comprehensive_intel,
    category="search",
)


# ---------------------------------------------------------------------------
# P2（按 docs/deep-research-chain-news-logic-plan.md）：
# search_market_discussion —— 国内外社区讨论与市场分歧检索
# ---------------------------------------------------------------------------
#
# 输出结构包含 ``source_type``（community_cn / community_global） / ``source_name`` /
# ``title`` / ``snippet`` / ``url`` / ``published_date`` / ``claim`` / ``reliability_hint``。
#
# 行为约束（与文档方案一致）：
# - **fail-open**：agent_reach 不可用（未安装 / 渠道异常）→ 返回空 results 与 status=unavailable，
#   不抛异常，不阻断主报告；
# - **source_type 必填**：社区源绝不标为 primary（primary 留给公告/财报/交易所/巨潮/监管/公司官网）；
# - **国内外双源**：当前默认先拉雪球热帖（community_cn 主流），其它源（X / Reddit / 海外论坛）
#   渠道不可用时静默跳过，单源失败不拖垮整体；
# - **reliability_hint**：固定为 "low"（社区线索不得单独支撑"确认/实锤/已导入/已量产"）。
# - **与 search_stock_news / search_comprehensive_intel 解耦**：本工具只做社区分歧线索，
#   不参与事实确认，不入 news_intel 持久化（避免污染 primary/news 维度）。
# ---------------------------------------------------------------------------


def _normalize_discussion_items(
    items: List[Any],
    source_type: str,
    source_name: str,
    stock_code: str,
) -> List[Dict[str, Any]]:
    """把 AgentReachService 返回的 ContentItem 列表统一映射到讨论检索输出结构。"""
    code = _canonical_search_code(stock_code) if stock_code else ""
    normalized = []
    for item in items or []:
        title = getattr(item, "title", "") or ""
        content = getattr(item, "content", "") or ""
        snippet = getattr(item, "snippet", "") or content[:200]
        url = getattr(item, "url", "") or ""
        author = getattr(item, "author", "") or ""
        published = getattr(item, "published_at", None) or ""
        # claim：从标题/内容截取（≤ 200 字符）作为"社区讨论的核心断言"
        claim_source = title or content
        claim = claim_source[:200] + ("..." if len(claim_source) > 200 else "")
        normalized.append(
            {
                "source_type": source_type,  # community_cn | community_global
                "source_name": source_name,  # 雪球热帖 | Reddit | X ...
                "stock_code": code,
                "title": title,
                "snippet": snippet,
                "url": url,
                "author": author,
                "published_date": published,
                "claim": claim,
                # 社区线索固定为 low，绝不写成 high
                # （按 docs §信息源策略：社区源不得单独支撑确认）
                "reliability_hint": "low",
            }
        )
    return normalized


def _handle_search_market_discussion(
    stock_code: str = "",
    stock_name: str = "",
    source: str = "xueqiu_hot",
    limit: int = 10,
) -> Dict[str, Any]:
    """社区讨论与市场分歧检索。

    Args:
        stock_code: 股票代码（仅用于结果标记，不参与过滤；社区源普遍是热榜性质）。
        stock_name: 股票名称（仅用于结果标记）。
        source: 社区源标识，默认 ``xueqiu_hot``（雪球热帖）。
        limit: 最多返回条目数（默认 10）。

    Returns:
        dict: ``status`` + ``source_type`` + ``source_name`` + ``results``（标准化列表）。
        渠道不可用 / 异常时 ``status="unavailable"``，results=[]，不抛异常。
    """
    # 按 source 决定预期 source_type（海外渠道即使是 unavailable 也要如实返回 community_global）
    expected_source_type = (
        "community_global" if source == "x_global" else "community_cn"
    )
    expected_source_name = source

    try:
        from src.services.agent_reach_service import AgentReachService
    except Exception as exc:  # noqa: BLE001
        logger.warning("search_market_discussion 不可用: agent_reach 导入失败: %s", exc)
        return {
            "status": "unavailable",
            "source_type": expected_source_type,
            "source_name": expected_source_name,
            "results": [],
            "error": f"agent_reach 不可用: {exc}",
        }

    service = AgentReachService()
    if not service.is_available():
        reason = service.unavailable_reason() or "agent_reach 不可用"
        logger.info("search_market_discussion 跳过: %s", reason)
        return {
            "status": "unavailable",
            "source_type": expected_source_type,
            "source_name": expected_source_name,
            "results": [],
            "error": reason,
        }

    results: List[Dict[str, Any]] = []
    used_source_name = expected_source_name
    used_source_type = expected_source_type

    if source == "xueqiu_hot":
        try:
            items = service.get_xueqiu_hot_posts(limit=limit) or []
            results = _normalize_discussion_items(
                items,
                source_type="community_cn",
                source_name="雪球热帖",
                stock_code=stock_code,
            )
            used_source_name = "雪球热帖"
            used_source_type = "community_cn"
        except Exception as exc:  # noqa: BLE001
            logger.warning("search_market_discussion 雪球热帖失败: %s", exc)
            return {
                "status": "unavailable",
                "source_type": "community_cn",
                "source_name": "雪球热帖",
                "results": [],
                "error": f"雪球热帖失败: {exc}",
            }
    elif source == "x_global":
        # 海外社区占位：当前渠道未接入时静默 unavailable，不抛异常
        return {
            "status": "unavailable",
            "source_type": "community_global",
            "source_name": "x_global",
            "results": [],
            "error": "海外社区渠道暂未接入",
        }
    else:
        return {
            "status": "unavailable",
            "source_type": expected_source_type,
            "source_name": source,
            "results": [],
            "error": f"未知 source={source}，仅支持 xueqiu_hot / x_global",
        }

    # 4) P3 持久化（best-effort）：社区源按 source_type=community_cn/community_global
    # + reliability_hint=low 写入 news_intel
    if results and stock_code:
        try:
            _persist_market_discussion(
                stock_code=stock_code,
                stock_name=stock_name or "",
                source=source,
                results=results,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("search_market_discussion 持久化包装失败: %s", exc)

    return {
        "status": "ok" if results else "empty",
        "source_type": used_source_type,
        "source_name": used_source_name,
        "count": len(results),
        "results": results,
    }


def _persist_market_discussion(
    *,
    stock_code: str,
    stock_name: str,
    source: str,
    results: List[Dict[str, Any]],
) -> None:
    """把社区检索结果按 P3 信息源分层（community_cn / community_global + low）入库。

    复用 ``_persist_news_response`` 共享的 ``save_news_intel`` 通道（news_intel 新增
    source_type / reliability_hint 两列，按 docs/deep-research-chain-news-logic-plan.md
    §信息源策略）。失败 best-effort（不抛异常不阻断主报告）。
    """
    if not results:
        return
    try:
        from src.search_service import SearchResponse, SearchResult
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "search_market_discussion 持久化跳过: SearchResponse 导入失败: %s", exc
        )
        return

    search_results: List[SearchResult] = []
    for item in results:
        search_results.append(
            SearchResult(
                title=item.get("title", "") or "",
                snippet=item.get("snippet", "") or item.get("claim", "") or "",
                url=item.get("url", "") or "",
                source=item.get("source_name", "") or "社区",
                published_date=item.get("published_date") or None,
            )
        )
    response = SearchResponse(
        query=f"market_discussion:{source}",
        results=search_results,
        success=True,
        provider=source,
    )
    if source == "xueqiu_hot":
        source_type = "community_cn"
        dimension = "community_cn"
    else:
        source_type = "community_global"
        dimension = "community_global"
    try:
        _persist_news_response(
            stock_code=stock_code or "",
            stock_name=stock_name or "",
            dimension=dimension,
            response=response,
            source_type=source_type,
            reliability_hint="low",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("search_market_discussion 持久化失败: %s", exc)


search_market_discussion_tool = ToolDefinition(
    name="search_market_discussion",
    description=(
        "国内/海外社区讨论与市场分歧检索。覆盖雪球 / 东方财富股吧 / 同花顺社区 / 微博 / 知乎 / 贴吧"
        "（community_cn）与 Reddit / X / 海外论坛（community_global）公开讨论。"
        "返回结构含 source_type（community_cn / community_global）/ source_name / title / "
        "snippet / url / published_date / claim / reliability_hint。\n"
        "**重要约束**：社区线索只用于识别市场分歧、关注度、传闻线索和市场认知差，"
        "**reliability_hint 固定为 low**；不得单独支撑「确认/实锤/已导入/已量产」。"
        "海外渠道（X / Reddit）当前为 fail-open，未接入时返回 status=unavailable 不阻断主报告。"
    ),
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="股票代码（仅用于结果标记，不参与过滤；社区源普遍是热榜性质）",
            required=False,
            default="",
        ),
        ToolParameter(
            name="stock_name",
            type="string",
            description="股票名称（仅用于结果标记）",
            required=False,
            default="",
        ),
        ToolParameter(
            name="source",
            type="string",
            description="社区源标识：xueqiu_hot（雪球热帖，默认）/ x_global（海外，未接入）",
            required=False,
            default="xueqiu_hot",
            enum=["xueqiu_hot", "x_global"],
        ),
        ToolParameter(
            name="limit",
            type="integer",
            description="最多返回条目数（默认 10）",
            required=False,
            default=10,
        ),
    ],
    handler=_handle_search_market_discussion,
    category="search",
)


ALL_SEARCH_TOOLS = [
    search_stock_news_tool,
    search_comprehensive_intel_tool,
    search_market_discussion_tool,  # P2: 国内外社区讨论与市场分歧检索
]
