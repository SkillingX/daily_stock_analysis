# -*- coding: utf-8 -*-
"""阶段 0：共享上下文装配（一次取齐，全程复用，as_of 时点诚实声明）。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

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
        records: List[Dict[str, Any]] = df.tail(days).to_dict(orient="records")  # type: ignore[assignment]
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
    """基本面快照：日内不变，走 24h 快照缓存（实测抓取 25s，为阶段 0 最慢一路）。

    行情/日线/筹码刻意不缓存（价格数据必须新鲜）；基本面缓存失败不缓存，
    保留下次重试机会。
    """
    from src.deep_research_dims.dim_cache import load_snapshot, save_snapshot

    cache_key = f"stage0_fund_{code}"
    cached = load_snapshot(cache_key, ttl_hours=24.0)
    if cached is not None:
        return cached
    try:
        from src.agent.tools.data_tools import (
            _compact_fundamental_context,
            _get_fetcher_manager,
        )

        manager = _get_fetcher_manager()
        raw = manager.get_fundamental_context(code)
        compact = _compact_fundamental_context(raw)
        valuation = (compact.get("valuation") or {}).get("data") or {}
        financial = (compact.get("growth") or {}).get("data") or {}
        institution = (compact.get("institution") or {}).get("data") or {}
        boards = (compact.get("boards") or {}).get("data") or {}
        industry_hint = ""
        if isinstance(boards, dict):
            for key in ("industry", "industry_name", "sw_industry", "sector"):
                value = boards.get(key)
                if isinstance(value, str) and value.strip():
                    industry_hint = value.strip()
                    break
        result = {
            "pe_ttm": valuation.get("pe_ratio") or valuation.get("pe_ttm"),
            "pb": valuation.get("pb_ratio"),
            "market_cap": valuation.get("market_cap")
            or valuation.get("total_market_cap"),
            "roe": financial.get("roe"),
            "revenue_growth": financial.get("revenue_yoy")
            or financial.get("revenue_growth"),
            "net_profit_yoy": financial.get("net_profit_yoy"),
            "gross_margin": financial.get("gross_margin"),
            "institution_holding_change": institution.get("institution_holding_change"),
            "industry_hint": industry_hint,
            "source": "fundamental_context",
        }
        # fuyao 兜底（按官方文档契约：GET /api/a-share/... + X-api-key）：
        # 主链（tushare/efinance 等）拿不到的关键字段用同花顺 fuyao 补，
        # 估值快照 + 利润表/资产负债表推算 ROE/毛利率/增速。
        def _fuyao_get(path: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            import os as _os
            import requests as _rq

            key = (_os.getenv("FUYAO_API_KEY") or "").strip()
            if not key:
                return None
            try:
                resp = _rq.get(
                    f"https://fuyao.aicubes.cn{path}",
                    params=params,
                    headers={"X-api-key": key},
                    timeout=20,
                )
                body = resp.json()
            except Exception:  # noqa: BLE001
                return None
            if resp.status_code == 429 or (isinstance(body, dict) and body.get("code") not in (0, None)):
                return None
            data = body.get("data") or {}
            items = data.get("item") or []
            return items[0] if items else None

        try:
            thscode = f"{code}.SH" if code.startswith(("60", "68", "9")) else f"{code}.SZ"
            snap = _fuyao_get("/api/a-share/valuations/snapshot", {"thscodes": thscode})
            if snap:
                if result.get("pe_ttm") is None:
                    result["pe_ttm"] = snap.get("pe_ttm")
                if result.get("pb") is None:
                    result["pb"] = snap.get("pb_mrq")
                result["source"] = "fundamental_context+fuyao"
            income = _fuyao_get(
                "/api/a-share/financials/income-statements",
                {"thscode": thscode, "period": "annual", "limit": 2},
            )
            balance = _fuyao_get(
                "/api/a-share/financials/balance-sheets",
                {"thscode": thscode, "period": "annual", "limit": 1},
            )
            if income:
                rev = income.get("operating_income")
                cost = income.get("operating_costs")
                np_ = income.get("parent_holder_net_profit")
                if result.get("gross_margin") is None and rev and cost is not None:
                    result["gross_margin"] = round((rev - cost) / rev * 100, 2)
                if result.get("roe") is None and np_ and balance:
                    equity = balance.get("holder_equity_total")
                    if equity:
                        result["roe"] = round(np_ / equity * 100, 2)
        except Exception as exc:  # noqa: BLE001 - 兜底失败不阻断
            logger.debug("[DualTrack] fuyao 兜底失败 %s: %s", code, exc)
        # 全 None 的占位结果不缓存（缓存污染 bug：失败抓取会被固化 24h）
        if result.get("pe_ttm") is not None or result.get("roe") is not None:
            save_snapshot(cache_key, result)
        else:
            ctx.limitation("基本面数据全缺（快照不缓存，下次重试）")
        return result
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
        result: Dict[str, Any] = {}
        if callable(data):
            result = data()  # type: ignore[assignment]
        elif isinstance(dist, dict):
            result = dict(dist)
        return result
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
    """装配共享快照。设计原则：尽力装配、失败只记录不抛（阶段 0 之后才允许整票失败）。

    性能：五个数据源的失败降级链都较慢（实测合计 ~35s），彼此无依赖，用线程池
    并行取数，整体耗时 ≈ 最慢一路而非五路之和。
    """
    from concurrent.futures import ThreadPoolExecutor

    ctx = SharedContext(
        stock_code=stock_code,
        stock_name=stock_name,
        as_of=datetime.now().isoformat(timespec="seconds"),
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        fut_quote = pool.submit(_safe_quote, stock_code, ctx)
        fut_history = pool.submit(_safe_history, stock_code, 260, ctx)
        fut_fund = pool.submit(_safe_fundamental, stock_code, ctx)
        fut_chip = pool.submit(_safe_chip, stock_code, ctx)
        ctx.quote = fut_quote.result()
        ctx.history = fut_history.result()
        ctx.fundamental = fut_fund.result()
        ctx.chip = fut_chip.result()
    ctx.history_reports = _safe_history_reports(stock_code, ctx)
    return ctx


def fetch_fuyao_financial_series(code: str, limit: int = 4) -> Dict[str, Any]:
    """fuyao 多年年报序列（估值快照 + 利润表/负债表/现金流），24h 快照缓存控频。

    Returns:
        {"years": [{year, revenue, net_profit, gross_margin, roe, debt_ratio,
                    op_cash_flow, eps}...], "valuation": {...}, "as_of": str}
        失败返回空结构，不抛异常。
    """
    import os as _os
    import requests as _rq

    from src.deep_research_dims.dim_cache import load_snapshot, save_snapshot

    key = f"fin_series_{code}"
    cached = load_snapshot(key, ttl_hours=24.0)
    if cached is not None:
        return cached

    out: Dict[str, Any] = {"years": [], "valuation": {}, "as_of": ""}
    api_key = (_os.getenv("FUYAO_API_KEY") or "").strip()
    if not api_key:
        return out
    thscode = f"{code}.SH" if code.startswith(("60", "68", "9")) else f"{code}.SZ"

    def _get(path: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        try:
            resp = _rq.get(
                f"https://fuyao.aicubes.cn{path}",
                params=params,
                headers={"X-api-key": api_key},
                timeout=20,
            )
            if resp.status_code == 429:
                return []
            body = resp.json()
            if body.get("code") not in (0, None):
                return []
            return (body.get("data") or {}).get("item") or []
        except Exception:  # noqa: BLE001
            return []

    incomes = _get("/api/a-share/financials/income-statements",
                   {"thscode": thscode, "period": "annual", "limit": limit})
    balances = _get("/api/a-share/financials/balance-sheets",
                    {"thscode": thscode, "period": "annual", "limit": limit})
    cashflows = _get("/api/a-share/financials/cash-flow-statements",
                     {"thscode": thscode, "period": "annual", "limit": limit})
    snaps = _get("/api/a-share/valuations/snapshot", {"thscodes": thscode})

    bal_by_year = {b.get("fiscal_year"): b for b in balances}
    cf_by_year = {c.get("fiscal_year"): c for c in cashflows}
    for item in incomes:
        year = item.get("fiscal_year")
        rev = item.get("operating_income")
        cost = item.get("operating_costs")
        np_ = item.get("parent_holder_net_profit")
        bal = bal_by_year.get(year) or {}
        cf = cf_by_year.get(year) or {}
        equity = bal.get("holder_equity_total")
        assets = bal.get("assets_total")
        debt = bal.get("total_debt")
        out["years"].append(
            {
                "year": year,
                "revenue": rev,
                "net_profit": np_,
                "gross_margin": round((rev - cost) / rev * 100, 2) if rev and cost is not None else None,
                "roe": round(np_ / equity * 100, 2) if np_ is not None and equity else None,
                "debt_ratio": round(debt / assets * 100, 2) if debt is not None and assets else None,
                "op_cash_flow": cf.get("act_cash_flow_net"),
                "eps": item.get("basic_eps"),
            }
        )
    if snaps:
        out["valuation"] = snaps[0]
    out["as_of"] = datetime.now().isoformat(timespec="seconds")
    if out["years"]:
        save_snapshot(key, out)
    return out
