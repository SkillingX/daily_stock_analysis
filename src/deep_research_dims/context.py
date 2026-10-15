# -*- coding: utf-8 -*-
"""阶段 0：共享上下文装配（一次取齐，全程复用，as_of 时点诚实声明）。"""

from __future__ import annotations

from contextvars import copy_context
import logging
from time import monotonic
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
        from data_provider.realtime_types import quote_field_records
        return {
            "field_meta": quote_field_records(quote),
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
    from src.deep_research_dims.dim_cache import FUNDAMENTAL_MAPPING_VERSION, load_snapshot, save_snapshot
    from src.config import get_config

    config = get_config()
    from src.patches.eastmoney_patch import request_deadline
    deadline = monotonic() + config.fundamental_stage_timeout_seconds
    outer_deadline = request_deadline.get()
    if outer_deadline is not None:
        deadline = min(deadline, outer_deadline)
    cache_key = f"stage0_fund_{config.data_source_contract}_v{FUNDAMENTAL_MAPPING_VERSION}_cv{int(bool(get_config().deep_research_cross_validate))}_{code}"
    cached = load_snapshot(cache_key, ttl_hours=24.0)
    from data_provider.mx_data_adapter import financial_refresh
    refresh = financial_refresh.get()
    financial_keys = ("pe_ttm", "pb", "roe", "gross_margin", "revenue_growth", "net_profit_yoy", "market_cap")
    if cached is not None and all(cached.get(key) is not None for key in financial_keys) and not config.deep_research_cross_validate and not (refresh and refresh[0] == code):
        return cached
    try:
        from src.agent.tools.data_tools import (
            _compact_fundamental_context,
            _get_fetcher_manager,
        )

        manager = _get_fetcher_manager()
        remaining = max(0.0, deadline - monotonic())
        # Verification needs its own share of the same stage budget, not leftover zero.
        verification_budget = 0.0 if config.data_source_profile == "choice" else min(config.fundamental_fetch_timeout_seconds, remaining / 2) if config.deep_research_cross_validate else min(config.fundamental_fetch_timeout_seconds, remaining / 3) if config.enable_fuyao and config.fuyao_api_key else 0.0
        fetch_deadline = deadline - verification_budget
        raw = manager.get_fundamental_context(code, budget_seconds=max(0.0, fetch_deadline - monotonic()))
        compact = _compact_fundamental_context(raw)
        for error in dict.fromkeys(compact.get("errors") or []):
            ctx.limitation(f"基本面取数: {error}")
        valuation = (compact.get("valuation") or {}).get("data") or {}
        financial = (compact.get("growth") or {}).get("data") or {}
        institution = (compact.get("institution") or {}).get("data") or {}
        boards = (compact.get("boards") or {}).get("data") or {}
        industry_hint = ""
        research = compact.get("research") or {}
        choice_industry = (research.get("industry") or {}).get("HISSW2021")
        if isinstance(choice_industry, str):
            industry_hint = choice_industry.strip()
        if isinstance(boards, dict):
            for key in ("industry", "industry_name", "sw_industry", "sector"):
                value = boards.get(key)
                if not industry_hint and isinstance(value, str) and value.strip():
                    industry_hint = value.strip()
                    break
        result: Dict[str, Any] = {
            "pe_ttm": next(
                (valuation[k] for k in ("pe_ratio", "pe_ttm") if valuation.get(k) is not None), None
            ),
            "pb": valuation.get("pb_ratio"),
            "market_cap": next(
                (valuation[k] for k in ("total_mv", "market_cap", "total_market_cap") if valuation.get(k) is not None), None
            ),
            "revenue": financial.get("revenue"),
            "net_profit": financial.get("net_profit"),
            "roe": financial.get("roe"),
            "revenue_growth": next(
                (financial[k] for k in ("revenue_yoy", "revenue_growth") if financial.get(k) is not None), None
            ),
            "net_profit_yoy": financial.get("net_profit_yoy"),
            "gross_margin": financial.get("gross_margin"),
            "institution_holding_change": institution.get("institution_holding_change"),
            "industry_hint": industry_hint,
            "research": research,
            "balance_sheet": financial.get("balance_sheet") or {},
            "sector_rankings": (((compact.get("capital_flow") or {}).get("data") or {}).get("sector_rankings") or {}),
            "stock_name": raw.get("stock_name"),
            "source": raw.get("source", "choice") if config.data_source_profile == "choice" else "fundamental_context",
            "errors": list(compact.get("errors") or []),
            "field_meta": {**((compact.get("valuation") or {}).get("field_meta") or {}), **((compact.get("growth") or {}).get("field_meta") or {})},
        }
        from src.agent.tools.cross_validation_helpers import build_cross_validation_block, field_record_from_validation
        from data_provider.cross_source_validator import AnchorQuality, adopted_field_record, reading_from_field_record
        aliases = {"revenue": "revenue", "net_profit": "net_profit", "pe_ratio": "pe_ttm", "pb_ratio": "pb", "roe": "roe", "gross_margin": "gross_margin", "revenue_yoy": "revenue_growth", "net_profit_yoy": "net_profit_yoy"}
        records = result["field_meta"]
        primary = {}
        for field, key in aliases.items():
            reading = reading_from_field_record(result.get(key), records.get(field))
            if reading is not None:
                primary[field] = reading
                previous = records.get(field) or {}
                conflict = previous.get("value") == reading.value and (previous.get("quality") or {}).get("status") == "conflict"
                if config.data_source_profile != "choice":
                    records[field] = adopted_field_record(field, reading, AnchorQuality(status="conflict" if conflict else "unverified"), previous_record=previous)
        # Reuse the same supplier adapter for missing fields and verification.
        if config.data_source_profile != "choice" and config.enable_fuyao and config.fuyao_api_key:
            from data_provider.fuyao_adapter import FuyaoFetcher, FuyaoSource
            source = FuyaoSource(FuyaoFetcher(endpoint=config.fuyao_endpoint, api_key=config.fuyao_api_key))
            period = next((reading.period for reading in primary.values() if reading.period), None) or ((compact.get("growth") or {}).get("data") or {}).get("report_date")
            fallback_deadline = fetch_deadline if config.deep_research_cross_validate else deadline
            for field, key in aliases.items():
                if result[key] is not None or monotonic() >= fallback_deadline:
                    continue
                reading, error, _ = manager._run_with_timeout(
                    lambda field=field: source.read(code, field, period, deadline=fallback_deadline),
                    min(config.fundamental_fetch_timeout_seconds, max(0.0, fallback_deadline - monotonic())), "fuyao_fallback",
                )
                if error:
                    ctx.limitation(f"Fuyao补值未完成: {error}")
                    result["errors"].append(error)
                    if "quota" in error or "authentication" in error or "http_" in error:
                        break
                    continue
                if reading is not None:
                    record = adopted_field_record(field, reading, selection_reason="fuyao_fallback")
                    if record["rule_eligible"]:
                        result[key], primary[field], records[field] = record["value"], reading, record
        cross_validation = compact.get("cross_validation") if config.data_source_profile == "choice" else build_cross_validation_block(code, aliases, primary_readings=primary, deadline=deadline)
        if cross_validation:
            result["cross_validation"] = cross_validation
        # Choice's manager already qualified supplements against the snapshot period.
        # Retain rejected candidates as comparison evidence, not newly adopted values.
        if cross_validation and config.data_source_profile != "choice":
            for field, anchor in cross_validation["anchors"].items():
                record = field_record_from_validation(field, anchor)
                if record is None and not result.get(aliases[field]):
                    records[field] = {"quality": anchor.get("quality", {"status": "missing"})}
                if record is not None:
                    records[field] = record
                    key = aliases[field]
                    if result.get(key) is None:
                        result[key] = record["value"]
        if not result.get("errors") and all(result.get(key) is not None for key in financial_keys):
            save_snapshot(cache_key, result)
        else:
            ctx.limitation("基本面数据不完整（快照不缓存，下次重试）")
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


def build_shared_context(stock_code: str, stock_name: str, *, deadline: float | None = None) -> SharedContext:
    """装配共享快照。设计原则：尽力装配、失败只记录不抛（阶段 0 之后才允许整票失败）。

    行情、日线和筹码在线程池运行，与本身已限时的财务装配并行。
    各路共用阶段截止时间；超时保留可用数据及失败证据，不占用后续研究预算。
    """
    from concurrent.futures import ThreadPoolExecutor

    ctx = SharedContext(
        stock_code=stock_code,
        stock_name=stock_name,
        as_of=datetime.now().isoformat(timespec="seconds"),
    )
    from concurrent.futures import wait
    from src.config import get_config

    stage_deadline = monotonic() + get_config().fundamental_stage_timeout_seconds
    if deadline is not None:
        stage_deadline = min(stage_deadline, deadline)
    pool = ThreadPoolExecutor(max_workers=3)
    # Each worker owns its limitations so late IO cannot mutate a returned context.
    workers = {key: SharedContext(stock_code, stock_name, ctx.as_of) for key in ("quote", "history", "chip")}
    from src.patches.eastmoney_patch import eastmoney_patch, request_deadline
    eastmoney_patch(budget_only=True)
    token = request_deadline.set(stage_deadline)
    try:
        futures = {
            "quote": pool.submit(copy_context().run, _safe_quote, stock_code, workers["quote"]),
            "history": pool.submit(copy_context().run, _safe_history, stock_code, 260, workers["history"]),
            "chip": pool.submit(copy_context().run, _safe_chip, stock_code, workers["chip"]),
        }
        # Financial IO already has bounded workers/validation. Assemble its timeout
        # evidence here instead of racing its summary against an identical outer timer.
        ctx.fundamental = _safe_fundamental(stock_code, ctx)
        wait(futures.values(), timeout=max(0.0, stage_deadline - monotonic()))
        for key, future in futures.items():
            if future.done():
                setattr(ctx, key, future.result())
                for limitation in workers[key].limitations:
                    ctx.limitation(limitation)
            else:
                future.cancel()
                ctx.limitation(f"{key} 装配超时，本次未采用")
    finally:
        request_deadline.reset(token)
        pool.shutdown(wait=False, cancel_futures=True)
    ctx.history_reports = _safe_history_reports(stock_code, ctx)
    return ctx


def fetch_fuyao_financial_series(code: str, limit: int = 4) -> Dict[str, Any]:
    """fuyao 多年年报序列（估值快照 + 利润表/负债表/现金流），24h 快照缓存控频。

    Returns:
        {"years": [{year, revenue, net_profit, gross_margin, roe, debt_ratio,
                    op_cash_flow, eps}...], "valuation": {...}, "as_of": str}
        失败返回空结构，不抛异常。
    """
    from src.config import get_config
    from data_provider.base import normalize_stock_code
    code = normalize_stock_code(code)
    if get_config().data_source_profile == "choice":
        from data_provider.choice_adapter import ChoiceAdapter, AnnualReport
        from data_provider.mx_data_adapter import financial_refresh
        from src.deep_research_dims.dim_cache import load_snapshot, save_snapshot
        from src.patches.eastmoney_patch import request_deadline
        adapter = ChoiceAdapter()
        key = f"{adapter.contract_version}_annual_{code}_{limit}"
        refresh = financial_refresh.get()
        cached = None if refresh and refresh[0] == code else load_snapshot(key, ttl_hours=24)
        if cached is not None:
            try:
                cached["years"] = [AnnualReport.from_record(row).as_record() for row in cached["years"]]
                return cached
            except (ValueError, KeyError, TypeError):
                logger.warning("Choice 年报缓存合同无效，重新查询")
        deadline = monotonic()+get_config().fundamental_stage_timeout_seconds
        outer = request_deadline.get()
        if outer is not None:
            deadline = min(deadline, outer)
        result = adapter.annual_reports(code, limit, deadline=deadline)
        if result["status"] == "ok":
            save_snapshot(key, result)
        return result
    import os as _os
    import requests as _rq

    from src.deep_research_dims.dim_cache import load_snapshot, save_snapshot
    from data_provider.mx_data_adapter import financial_refresh

    key = f"fin_series_legacy_v2_{code}"
    refresh = financial_refresh.get()
    cached = None if refresh and refresh[0] == code else load_snapshot(key, ttl_hours=24.0)
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
                "roe": None,
                "ending_equity_return": round(np_ / equity * 100, 2) if np_ is not None and equity else None,
                "roe_method": "unknown",
                "source": "fuyao",
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
