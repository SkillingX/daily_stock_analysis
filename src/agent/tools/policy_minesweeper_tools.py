# -*- coding: utf-8 -*-
"""政策与公告双维度排雷专属工具集。

当前仅 1 个工具：``score_policy_minesweeper``（包装
``src.services.policy_minesweeper_scorecard`` 的确定性评分卡）。其余数据/情报
工具（行情/新闻/基本面/板块）**复用问股的全局 ToolRegistry**，通过
``build_policy_minesweeper_executor`` 在 factory 里合并注册（见 factory.py）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from src.agent.tools.registry import ToolDefinition, ToolParameter
from src.services import policy_minesweeper_scorecard as _scorecard

logger = logging.getLogger(__name__)

# 六维方向评分（各 -100 强利空 ~ +100 强利好）；key 与 scorecard 保持一致
DIMENSION_HINTS: Dict[str, str] = {
    "event_importance": "事件重要性（利好+ / 利空-）",
    "policy_exposure": "政策相关度/暴露度",
    "earnings_impact": "盈利影响",
    "valuation_impact": "估值影响",
    "price_sensitivity": "股价敏感度（市值/流动性/Beta）",
    "time_urgency": "时间紧迫度",
}


def _handle_score_policy_minesweeper(
    stock_code: str,
    stock_name: str,
    horizon: str = "medium",
    dimensions: Optional[Dict[str, Any]] = None,
    alpha_score: Optional[float] = None,
    beta_score: Optional[float] = None,
    dominant_factor: str = "",
    confidence: Optional[float] = None,
    scenarios: Optional[Dict[str, Any]] = None,
    evidence: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """对一只 A 股做"政策与公告双维度排雷"打分（综合分 -100~+100）。"""
    payload = {
        "stock_code": stock_code or "",
        "stock_name": stock_name or "",
        "dimensions": dimensions or {},
        "alpha_score": alpha_score,
        "beta_score": beta_score,
        "dominant_factor": dominant_factor or "",
        "confidence": confidence,
        "scenarios": scenarios or {},
        "evidence": evidence or [],
    }
    try:
        result = _scorecard.score(payload, horizon)
    except Exception as exc:  # noqa: BLE001 - 评分卡纯函数，任何异常都回退为错误而非崩
        logger.error("policy minesweeper scorecard failed for %s: %s", stock_code, exc, exc_info=True)
        return {"error": f"打分失败: {exc}", "input_echo": payload}

    return {
        "stock_code": result["stock_code"],
        "stock_name": result["stock_name"],
        "verdict": f"{result['emoji']} {result['label']}",
        "final": result["final"],
        "action": result["action"],
        "expected_car": result["expected_car"],
        "score_report_markdown": _scorecard.to_markdown(result),
        "usage_note": (
            "以上为政策与公告双维度排雷打分结果，衡量政策/公告对股价的利好利空冲击，"
            "非精确预测；预期冲击为历史经验区间。引用请保留证据来源与日期。"
            "不构成投资建议，买卖由你自己决定。"
        ),
    }


score_policy_minesweeper_tool = ToolDefinition(
    name="score_policy_minesweeper",
    description=(
        "对一只 A 股做『政策与公告双维度排雷』打分（综合分 -100 强利空 ~ +100 强利好）。"
        "输入：六维方向评分（事件重要性/政策相关度/盈利影响/估值影响/股价敏感度/"
        "时间紧迫度，各 -100~+100）、α 公司层面分、β 政策层面分、时间窗口"
        "（short/medium/long）。返回综合分、5 档等级、仓位指令、预期冲击区间与 "
        "Markdown 报告。用于『排雷 300750』『政策公告对 XX 是利好还是利空』类问题。"
    ),
    parameters=[
        ToolParameter(name="stock_code", type="string",
                      description="A 股代码（如 300750）", required=True),
        ToolParameter(name="stock_name", type="string",
                      description="公司名称", required=True),
        ToolParameter(name="horizon", type="string",
                      description="时间窗口：short(1-5日)/medium(1-4周)/long(1-6月)",
                      required=False, enum=["short", "medium", "long"], default="medium"),
        ToolParameter(
            name="dimensions", type="object",
            description="六维方向评分（各 -100~+100），key 固定："
            + "；".join(f"{k}({h})" for k, h in DIMENSION_HINTS.items()),
            required=True,
        ),
        ToolParameter(name="alpha_score", type="number",
                      description="公司层面（α 公告扫描）综合分 -100~+100，可选",
                      required=False, default=None),
        ToolParameter(name="beta_score", type="number",
                      description="政策层面（β 政策分析）综合分 -100~+100，可选",
                      required=False, default=None),
        ToolParameter(name="dominant_factor", type="string",
                      description="主导因子说明（如『宏观压制 > 公司利好』），可选",
                      required=False, default=""),
        ToolParameter(name="confidence", type="number",
                      description="置信度 0~1，可选", required=False, default=None),
        ToolParameter(
            name="scenarios", type="object",
            description="情景分析：{optimistic/base_case/pessimistic: {assumption, score}}，可选",
            required=False, default=None,
        ),
        ToolParameter(
            name="evidence", type="array",
            description="证据列表，每项 {claim, source, date, strength(primary/media/analysis/social/rumor), url}；"
                        "公司公告类证据的 url 必须为公告原文地址（取自检索工具结果，非编造），"
                        "取不到则 source 标注「待核验」",
            required=False, default=None,
        ),
    ],
    handler=_handle_score_policy_minesweeper,
    category="analysis",
)


# ============================================================
# 公司公告检索（复用共享 SearchService，返回带原文地址的结果）
# ============================================================

# 一手公告源：交易所/巨潮/港交所/SEC 等（is_official=True）；媒体（腾讯/新浪/东财）返回 False
_OFFICIAL_ANNOUNCEMENT_DOMAINS = (
    "cninfo.com.cn",       # 巨潮资讯网（沪深公告法定披露平台）
    "sse.com", "sse.com.cn",  # 上交所
    "szse.cn",             # 深交所
    "hkexnews.hk",         # 港交所
    "sec.gov", "nasdaq.com", "nyse.com",  # 美股监管/交易所
)


def _get_announcement_search_service():
    """Lazy 共享 SearchService 访问器（测试可 monkeypatch 替换为 fake，避免真实网络）。"""
    from src.search_service import get_search_service

    return get_search_service()


def _build_announcement_query(stock_name: Any, stock_code: Any) -> str:
    """构造公告导向检索 query（覆盖重大事项，巨潮/官网/证券媒体一并命中）。"""
    name = str(stock_name or "").strip()
    code = str(stock_code or "").strip()
    prefix = f"{name} {code}".strip() or code or name
    return f"{prefix} 公司公告 巨潮资讯"


def _pick_search_provider(service: Any) -> Any:
    """返回共享 SearchService 上首个可用 provider（或 None）。

    走 provider 原生 ``.search()``（绕过 ``search_stock_news`` 面向新闻的过滤/再排序），
    保留一手公告源（巨潮/交易所/官网）。SearchService 内部本就用 ``_providers`` 列表
    迭代，此处沿用同一稳定结构。
    """
    if service is None:
        return None
    for provider in getattr(service, "_providers", None) or []:
        if getattr(provider, "is_available", False):
            return provider
    return None


def _is_official_announcement_source(url: Any, source: Any) -> bool:
    """判断结果是否来自一手公告源（交易所/巨潮/官网）。媒体源（腾讯/新浪等）返回 False。"""
    text = f"{url or ''} {source or ''}".lower()
    return any(domain in text for domain in _OFFICIAL_ANNOUNCEMENT_DOMAINS)


def _handle_search_company_announcements(
    stock_code: str,
    stock_name: str,
    max_results: int = 8,
) -> Dict[str, Any]:
    """检索公司公告（腾讯证券/新浪证券/巨潮/交易所/公司官网），返回带原文地址的结果。

    复用共享 ``SearchService`` 的 provider（配置 ``SERPAPI_API_KEYS`` 等后可用），走其
    原生 ``.search()`` 保留一手公告源；每条结果含 ``url``（公告原文地址，可直接填入
    证据）与 ``is_official`` 标注。服务不可用或检索失败时返回 ``error``，α 据此标注
    「待核验」而非编造。
    """
    provider = _pick_search_provider(_get_announcement_search_service())
    if provider is None:
        return {
            "error": "搜索引擎不可用（未配置 SERPAPI_API_KEYS 等），无法检索公司公告，请将相关判断标注「待核验」",
            "stock_code": stock_code,
        }

    query = _build_announcement_query(stock_name, stock_code)
    try:
        response = provider.search(query, max_results=max_results, days=30)
    except Exception as exc:  # noqa: BLE001 - 检索异常不得拖垮 agent
        logger.error("[PolicyMinesweeper] 公司公告检索异常 (%s): %s", stock_code, exc)
        return {"error": f"公司公告检索异常: {exc}", "stock_code": stock_code, "query": query}

    if not getattr(response, "success", False):
        return {
            "error": getattr(response, "error_message", None) or "公司公告搜索失败",
            "stock_code": stock_code,
            "query": query,
        }

    items = [
        {
            "title": getattr(r, "title", ""),
            "snippet": (getattr(r, "snippet", "") or "")[:500],
            "url": getattr(r, "url", ""),
            "source": getattr(r, "source", ""),
            "date": getattr(r, "published_date", ""),
            "is_official": _is_official_announcement_source(
                getattr(r, "url", ""), getattr(r, "source", "")
            ),
        }
        for r in (getattr(response, "results", None) or [])[:max_results]
    ]
    return {
        "stock_code": stock_code,
        "stock_name": stock_name,
        "query": query,
        "provider": getattr(response, "provider", ""),
        "count": len(items),
        "announcements": items,
    }


search_company_announcements_tool = ToolDefinition(
    name="search_company_announcements",
    description=(
        "检索一只 A 股的公司公告（来自腾讯证券/新浪证券/巨潮资讯/交易所/公司官网等），"
        "返回标题/摘要/**公告原文地址 url**/来源/日期，并标注是否一手披露源（is_official）。"
        "用于『排雷 300750』『查 XX 最近公告/增发/回购/业绩预告』。配置 SERPAPI_API_KEYS 后可用。"
    ),
    parameters=[
        ToolParameter(name="stock_code", type="string",
                      description="A 股代码（如 300750）", required=True),
        ToolParameter(name="stock_name", type="string",
                      description="公司名称", required=True),
    ],
    handler=_handle_search_company_announcements,
    category="search",
)


ALL_POLICY_MINESWEEPER_TOOLS = [
    score_policy_minesweeper_tool,
    search_company_announcements_tool,
]
