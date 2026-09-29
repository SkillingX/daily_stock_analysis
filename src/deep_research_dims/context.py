# -*- coding: utf-8 -*-
"""阶段 0：共享上下文装配（一次取齐，全程复用，as_of 时点诚实声明）。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


@dataclass
class SharedContext:
    """整份报告共享的数据快照。任一字段失败不致命，记录进 limitations。"""

    stock_code: str
    stock_name: str
    as_of: str  # ISO，阶段 0 装配完成时点
    quote: Dict[str, Any] = field(default_factory=dict)
    history: List[Dict[str, Any]] = field(default_factory=list)  # OHLCV dicts, 升序
    fundamental: Dict[str, Any] = field(default_factory=dict)
    chip: Dict[str, Any] = field(default_factory=dict)
    history_reports: List[Dict[str, Any]] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)

    def limitation(self, msg: str) -> None:
        if msg not in self.limitations:
            self.limitations.append(msg)


def _safe_history(code: str, days: int, ctx: SharedContext) -> List[Dict[str, Any]]:
    try:
        from src.services.history_loader import load_history_df

        df, source = load_history_df(code, days=days)
        if df is None or df.empty:
            ctx.limitation("日线历史数据缺失")
            return []
        records = df.tail(days).to_dict(orient="records")
        for r in records:
            if "date" in r:
                r["date"] = str(r["date"])
        return records
    except Exception as exc:  # noqa: BLE001 - 阶段0装配失败只降级
        logger.warning("[DualTrack] 历史数据装配失败 %s: %s", code, exc)
        ctx.limitation(f"日线历史数据装配失败: {exc}")
        return []


def _safe_quote(code: str, ctx: SharedContext) -> Dict[str, Any]:
    try:
        from src.agent.tools.data_tools import _get_fetcher_manager

        quote = _get_fetcher_manager().get_realtime_quote(code)
        if quote is None:
            ctx.limitation("实时行情缺失")
            return {}
        return {
            "price": getattr(quote, "price", None),
            "change_pct": getattr(quote, "change_percent", None)
            or getattr(quote, "change_pct", None),
            "volume": getattr(quote, "volume", None),
            "turnover": getattr(quote, "turnover", None),
            "name": getattr(quote, "name", None) or ctx.stock_name,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DualTrack] 行情装配失败 %s: %s", code, exc)
        ctx.limitation(f"实时行情装配失败: {exc}")
        return {}


def _safe_fundamental(code: str, ctx: SharedContext) -> Dict[str, Any]:
    try:
        from src.agent.tools.data_tools import (
            _compact_fundamental_context,
            _get_fetcher_manager,
        )

        manager = _get_fetcher_manager()
        raw = manager.get_fundamental_context(code)
        compact = _compact_fundamental_context(raw)
        valuation = (compact.get("valuation") or {}).get("data") or {}
        financial = (compact.get("financial") or {}).get("data") or {}
        return {
            "pe_ttm": valuation.get("pe_ratio") or valuation.get("pe_ttm"),
            "pb": valuation.get("pb_ratio"),
            "market_cap": valuation.get("market_cap")
            or valuation.get("total_market_cap"),
            "roe": financial.get("roe"),
            "revenue_growth": financial.get("revenue_growth")
            or financial.get("revenue_yoy"),
            "gross_margin": financial.get("gross_margin"),
            "source": "fundamental_context",
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DualTrack] 基本面装配失败 %s: %s", code, exc)
        ctx.limitation(f"基本面数据装配失败: {exc}")
        return {}


def _safe_chip(code: str, ctx: SharedContext) -> Dict[str, Any]:
    try:
        from src.agent.tools.data_tools import _get_fetcher_manager

        dist = _get_fetcher_manager().get_chip_distribution(code)
        if dist is None:
            ctx.limitation("筹码分布数据缺失")
            return {}
        data = getattr(dist, "to_dict", None)
        return data() if callable(data) else dict(dist) if isinstance(dist, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DualTrack] 筹码装配失败 %s: %s", code, exc)
        ctx.limitation(f"筹码分布装配失败: {exc}")
        return {}


def _safe_history_reports(code: str, ctx: SharedContext) -> List[Dict[str, Any]]:
    try:
        from src.storage import get_db

        rows, _total = get_db().get_deep_research_reports(
            stock_code=code, limit=10, offset=0
        )
        return [r.to_dict() for r in rows]
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DualTrack] 历史报告读取失败 %s: %s", code, exc)
        ctx.limitation(f"历史报告读取失败: {exc}")
        return []


def build_shared_context(stock_code: str, stock_name: str) -> SharedContext:
    """装配共享快照。设计原则：尽力装配、失败只记录不抛（阶段 0 之后才允许整票失败）。"""
    ctx = SharedContext(
        stock_code=stock_code,
        stock_name=stock_name,
        as_of=datetime.now().isoformat(timespec="seconds"),
    )
    ctx.quote = _safe_quote(stock_code, ctx)
    ctx.history = _safe_history(stock_code, 260, ctx)
    ctx.fundamental = _safe_fundamental(stock_code, ctx)
    ctx.chip = _safe_chip(stock_code, ctx)
    ctx.history_reports = _safe_history_reports(stock_code, ctx)
    return ctx
