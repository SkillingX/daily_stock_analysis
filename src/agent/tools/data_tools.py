# -*- coding: utf-8 -*-
"""
Data tools — wraps DataFetcherManager methods as agent-callable tools.

Tools:
- get_realtime_quote: real-time stock quote
- get_daily_history: historical OHLCV data
- get_chip_distribution: chip distribution analysis
- get_analysis_context: historical analysis context from DB
"""

import logging
from datetime import date
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

from src.agent.tools.registry import ToolParameter, ToolDefinition
from data_provider.cross_source_validator import adopted_field_record, reading_from_field_record
from data_provider.capital_flow_provider import get_main_inflow_cumulative
from src.agent.tools.cross_validation_helpers import build_cross_validation_block, field_record_from_validation

logger = logging.getLogger(__name__)

_fetcher_manager_singleton = None
_fetcher_manager_lock = Lock()
_DAILY_HISTORY_DEFAULT_DAYS = 60
_DAILY_HISTORY_MAX_DAYS = 365


def _get_fetcher_manager() -> Any:
    """Return a module-level singleton DataFetcherManager.

    Re-creating the manager on every tool call causes Tushare re-init overhead
    (~2 s each) and prevents circuit-breaker cooldown from taking effect across
    consecutive tool calls within the same agent run.
    """
    from data_provider import DataFetcherManager

    global _fetcher_manager_singleton
    if _fetcher_manager_singleton is None:
        with _fetcher_manager_lock:
            if _fetcher_manager_singleton is None:
                _fetcher_manager_singleton = DataFetcherManager()
    return _fetcher_manager_singleton


def reset_fetcher_manager() -> None:
    """Clear the cached DataFetcherManager so runtime config reloads take effect."""
    global _fetcher_manager_singleton
    with _fetcher_manager_lock:
        _fetcher_manager_singleton = None


def _get_db() -> Any:
    """Lazy import for DatabaseManager."""
    from src.storage import get_db

    return get_db()


def _normalize_history_days(days: Any) -> Tuple[int, Dict[str, Any]]:
    """Normalize LLM-provided history window and return response metadata."""
    requested_days = days
    warning = None
    try:
        if isinstance(days, bool):
            raise ValueError("bool is not a valid days value")
        effective_days = int(days)
    except (TypeError, ValueError):
        effective_days = _DAILY_HISTORY_DEFAULT_DAYS
        warning = (
            f"Invalid days value {requested_days!r}; "
            f"using default {_DAILY_HISTORY_DEFAULT_DAYS}."
        )

    if effective_days < 1:
        effective_days = 1
        warning = f"days must be >= 1; using {effective_days}."
    elif effective_days > _DAILY_HISTORY_MAX_DAYS:
        effective_days = _DAILY_HISTORY_MAX_DAYS
        warning = f"days exceeds max {_DAILY_HISTORY_MAX_DAYS}; truncated."

    metadata: Dict[str, Any] = {}
    if warning is not None:
        metadata.update(
            {
                "warning": warning,
                "requested_days": requested_days,
                "effective_days": effective_days,
            }
        )
    return effective_days, metadata


def _history_code_candidates(stock_code: str) -> Tuple[List[str], str]:
    """Return cache lookup candidates plus canonical write code."""
    from data_provider.base import canonical_stock_code, normalize_stock_code

    raw_code = str(stock_code or "").strip()
    normalized_code = canonical_stock_code(normalize_stock_code(raw_code))
    candidates: List[str] = []
    for candidate in (canonical_stock_code(raw_code), normalized_code):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates, normalized_code


def _append_history_metadata(
    response: dict[str, Any], metadata: Dict[str, Any]
) -> dict[str, Any]:
    if metadata:
        response.update(metadata)
    return response


def _compact_fundamental_context(fundamental_context: dict[str, Any]) -> dict[str, Any]:
    """Reduce token footprint for tool responses while keeping key semantics."""
    if not isinstance(fundamental_context, dict):
        return {}
    blocks = (
        "valuation",
        "growth",
        "earnings",
        "institution",
        "capital_flow",
        "dragon_tiger",
        "boards",
    )
    compact = {
        "market": fundamental_context.get("market"),
        "status": fundamental_context.get("status"),
        "coverage": fundamental_context.get("coverage", {}),
    }
    for block in blocks:
        payload = fundamental_context.get(block, {})
        if isinstance(payload, dict):
            compact[block] = {
                "status": payload.get("status"),
                "data": payload.get("data", {}),
                "field_meta": dict(payload.get("field_meta") or {}),
            }
        else:
            compact[block] = {"status": "failed", "data": {}}
    return compact


def _latest_annual_period(today: Optional[date] = None) -> str:
    """推导最新已披露 A 股年报报告期（"YYYY年报"）。

    年报 N 须于 N+1 年 4/30 前披露：5 月起上年度年报可获取，否则取再上一年。
    用于驱动 iFinD 财务类查询（需报告期）；MX 取不到指期会自动回退最新（见 mx 适配器）。
    ``today`` 可注入便于单测。
    """
    today = today or date.today()
    year = today.year - 1 if today.month >= 5 else today.year - 2
    return f"{year}年报"


# growth 块字段 → 交叉验证锚点名：akshare 失败时从 CV 回填这些 None 字段。
_GROWTH_CV_FIELDS = ("revenue_yoy", "gross_margin", "roe", "net_profit_yoy")


def _backfill_growth_from_validation(
    growth_block: Optional[Dict[str, Any]], cv_block: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """用交叉验证结果回填 growth 块中的 None 字段（akshare 失败兜底）。

    仅回填 ``data`` 中为 None 的 ``revenue_yoy/gross_margin/roe``，从
    ``cv_block["anchors"][field]["v"]`` 取值（v 非 None 才填）；已有非空值不覆盖。
    回填补到值且原 data 为空 → ``status`` 置 "partial"（诚实：CV 兜底，非完整 bundle）。
    无 cv_block / 无锚点 / 无值可填 → 原样返回新副本。返回**新 dict**（不可变）。
    """
    block = dict(growth_block or {})
    data = dict(block.get("data") or {})
    records = dict(block.get("field_meta") or {})
    original_had_value = any(v is not None for v in data.values())
    anchors = cv_block.get("anchors") if isinstance(cv_block, dict) else None
    backfilled_any = False
    if isinstance(anchors, dict):
        for field in _GROWTH_CV_FIELDS:
            if data.get(field) is not None:
                continue
            anchor = anchors.get(field)
            record = field_record_from_validation(field, anchor) if isinstance(anchor, dict) else None
            if record is not None and record["rule_eligible"]:
                data[field] = record["value"]
                records[field] = record
                backfilled_any = True
    if not backfilled_any:
        return block
    if (
        not original_had_value
    ):  # 原数据全 None/空 → 提升到 partial（CV 兜底，非完整 bundle）
        block["status"] = "partial"
    block["data"] = data
    block["field_meta"] = records
    return block


def _backfill_capital_flow(
    result: Optional[Dict[str, Any]],
    cv_block: Optional[Dict[str, Any]],
    ifind_cumulative: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """用交叉验证 + iFinD 多日累计回填资金流缺失字段（akshare push2his 不可达兜底）。

    - ``main_net_inflow`` 缺失 ← 优先 ``cv_block["anchors"]["main_inflow"]["v"]``
      （MX+iFinD 双源方向验证值），否则 ``ifind_cumulative["main_net_inflow"]``（iFinD 单日）。
    - ``inflow_5d`` / ``inflow_10d`` 缺失 ← ``ifind_cumulative``（iFinD 多日序列累计；
      MX 无多日、akshare 不可达 → 累计仅 iFinD 单源）。
    - 已有非空值不覆盖；回填补到值且原 status 非 ``ok`` → status 提升到 ``partial``，
      并移除原 ``failed`` note（「暂缺资金流分析」已不准确，避免误导 LLM）。
    - 附 ``capital_flow_fallback={"source","daily_series"}`` 透明标注来源。
    无值可填 / 无源 → 原样返回新副本。返回**新 dict**（不可变）。
    """
    out = dict(result or {})
    records = dict(out.get("field_meta") or {})
    cumulative = ifind_cumulative if isinstance(ifind_cumulative, dict) else {}
    anchors = cv_block.get("anchors") if isinstance(cv_block, dict) else None
    backfilled = False
    fallback_source = None

    # main_net_inflow：优先双源 CV（方向验证），否则 iFinD 单日序列首值
    if out.get("main_net_inflow") is None:
        value = None
        if isinstance(anchors, dict):
            anchor = anchors.get("main_inflow")
            record = field_record_from_validation("main_inflow", anchor) if isinstance(anchor, dict) else None
            value = record["value"] if record is not None and record["rule_eligible"] else None
            if value is not None and record is not None:
                fallback_source = record["source"]
                records["main_inflow"] = record
        conflict = isinstance(anchors, dict) and (anchors.get("main_inflow") or {}).get("quality", {}).get("status") == "conflict"
        if value is None and not conflict and cumulative.get("main_net_inflow") is not None:
            reading = reading_from_field_record(cumulative["main_net_inflow"], (cumulative.get("field_meta") or {}).get("main_inflow"))
            record = adopted_field_record("main_inflow", reading, selection_reason="fallback") if reading is not None else None
            if record is not None and record["rule_eligible"]:
                value = record["value"]
                records["main_inflow"] = record
                fallback_source = record["source"]
        if value is not None:
            out["main_net_inflow"] = value
            backfilled = True

    # inflow_5d / inflow_10d：仅 iFinD 多日序列可提供（累计为单源，诚实标注）
    for field in ("inflow_5d", "inflow_10d"):
        if out.get(field) is None and cumulative.get(field) is not None:
            out[field] = cumulative[field]
            backfilled = True
            fallback_source = fallback_source or cumulative.get("source", "ifind")

    if not backfilled:
        return out
    if (result or {}).get("status") != "ok":
        out["status"] = "partial"
        out.pop("note", None)  # 原 failed note 已不准确
    out["capital_flow_fallback"] = {
        "source": fallback_source or "ifind",
        "daily_series": cumulative.get("daily_series") or [],
    }
    out["field_meta"] = records
    return out


def _compact_portfolio_snapshot(
    snapshot: dict[str, Any], include_positions: bool = False, top_n: int = 5
) -> dict[str, Any]:
    """Shrink portfolio snapshot payload for default tool responses."""
    if not isinstance(snapshot, dict):
        return {}
    compact_accounts = []
    for account in snapshot.get("accounts", []) or []:
        if not isinstance(account, dict):
            continue
        positions = list(account.get("positions") or [])
        positions = sorted(
            positions,
            key=lambda item: float((item or {}).get("market_value_base") or 0.0),
            reverse=True,
        )
        account_payload = {
            "account_id": account.get("account_id"),
            "account_name": account.get("account_name"),
            "market": account.get("market"),
            "base_currency": account.get("base_currency"),
            "total_equity": account.get("total_equity"),
            "total_market_value": account.get("total_market_value"),
            "total_cash": account.get("total_cash"),
            "realized_pnl": account.get("realized_pnl"),
            "unrealized_pnl": account.get("unrealized_pnl"),
            "fx_stale": account.get("fx_stale"),
        }
        if include_positions:
            account_payload["positions"] = positions
        else:
            account_payload["position_count"] = len(positions)
            account_payload["top_positions"] = positions[:top_n]
        compact_accounts.append(account_payload)

    return {
        "as_of": snapshot.get("as_of"),
        "cost_method": snapshot.get("cost_method"),
        "currency": snapshot.get("currency"),
        "account_count": snapshot.get("account_count"),
        "total_cash": snapshot.get("total_cash"),
        "total_market_value": snapshot.get("total_market_value"),
        "total_equity": snapshot.get("total_equity"),
        "realized_pnl": snapshot.get("realized_pnl"),
        "unrealized_pnl": snapshot.get("unrealized_pnl"),
        "fx_stale": snapshot.get("fx_stale"),
        "accounts": compact_accounts,
    }


def _compact_portfolio_risk(risk: dict[str, Any], top_n: int = 10) -> dict[str, Any]:
    """Shrink portfolio risk payload for tool responses."""
    if not isinstance(risk, dict):
        return {}
    concentration = risk.get("concentration", {}) or {}
    top_positions = list(concentration.get("top_positions") or [])
    top_positions = sorted(
        top_positions,
        key=lambda item: float((item or {}).get("weight_pct") or 0.0),
        reverse=True,
    )[:top_n]
    stop_loss = risk.get("stop_loss", {}) or {}
    stop_items = list(stop_loss.get("items") or [])
    stop_items = sorted(
        stop_items,
        key=lambda item: float((item or {}).get("loss_pct") or 0.0),
        reverse=True,
    )[:top_n]
    drawdown = risk.get("drawdown", {}) or {}
    return {
        "as_of": risk.get("as_of"),
        "currency": risk.get("currency"),
        "cost_method": risk.get("cost_method"),
        "thresholds": risk.get("thresholds", {}),
        "concentration": {
            "alert": concentration.get("alert", False),
            "top_weight_pct": concentration.get("top_weight_pct"),
            "top_positions": top_positions,
        },
        "drawdown": {
            "alert": drawdown.get("alert", False),
            "max_drawdown_pct": drawdown.get("max_drawdown_pct"),
            "current_drawdown_pct": drawdown.get("current_drawdown_pct"),
            "fx_stale": drawdown.get("fx_stale", False),
        },
        "stop_loss": {
            "near_alert": stop_loss.get("near_alert", False),
            "triggered_count": stop_loss.get("triggered_count", 0),
            "near_count": stop_loss.get("near_count", 0),
            "items": stop_items,
        },
    }


# ============================================================
# get_realtime_quote
# ============================================================


def _handle_get_realtime_quote(stock_code: str) -> dict[str, Any]:
    """Get real-time stock quote."""
    manager = _get_fetcher_manager()
    quote = manager.get_realtime_quote(stock_code)
    if quote is None:
        return {
            "error": f"No realtime quote available for {stock_code}",
            "retriable": False,
            "note": "All data sources unavailable (network or circuit-breaker). Skip this tool and proceed with historical data only.",
        }

    response = {
        "code": quote.code,
        "name": quote.name,
        "price": quote.price,
        "change_pct": quote.change_pct,
        "change_amount": quote.change_amount,
        "volume": quote.volume,
        "amount": quote.amount,
        "volume_ratio": quote.volume_ratio,
        "turnover_rate": quote.turnover_rate,
        "amplitude": quote.amplitude,
        "open": quote.open_price,
        "high": quote.high,
        "low": quote.low,
        "pre_close": quote.pre_close,
        "pe_ratio": quote.pe_ratio,
        "pb_ratio": quote.pb_ratio,
        "total_mv": quote.total_mv,
        "circ_mv": quote.circ_mv,
        "change_60d": quote.change_60d,
        "source": quote.source.value
        if hasattr(quote.source, "value")
        else str(quote.source),
    }
    from data_provider.realtime_types import quote_field_records
    records = quote_field_records(quote)
    response["field_meta"] = records
    # 当前采用价作为核验主读数，逐字段保留真实来源。
    # quote.price 可能为 None（盘前/停牌/数据缺口），此时不注入 primary_reading，
    # validator 仍可用 MX/iFinD 单源验证 current_price（fail-open，不静默丢锚点）
    primary = reading_from_field_record(quote.price, records.get("current_price"))
    _cv = build_cross_validation_block(
        stock_code,
        ["current_price"],
        expected_currencies={"current_price": quote.currency} if getattr(quote, "currency", None) else None,
        primary_readings=(
            {"current_price": primary}
            if primary is not None
            else None
        ),
    )
    if _cv:
        record = field_record_from_validation("current_price", _cv["anchors"].get("current_price") or {})
        if record is not None:
            records["current_price"] = record
            if response["price"] is None and record["rule_eligible"]:
                response["price"] = record["value"]
        response["cross_validation"] = _cv
    return response


get_realtime_quote_tool = ToolDefinition(
    name="get_realtime_quote",
    description="Get real-time stock quote including price, change%, volume ratio, "
    "turnover rate, PE, PB, market cap. Returns live market data.",
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="Stock code, e.g., '600519' (A-share), 'AAPL' (US), 'hk00700' (HK)",
        ),
    ],
    handler=_handle_get_realtime_quote,
    category="data",
)


# ============================================================
# get_daily_history
# ============================================================


def _handle_get_daily_history(stock_code: str, days: int = 60) -> dict[str, Any]:
    """Get daily OHLCV history data."""
    effective_days, metadata = _normalize_history_days(days)

    from src.services.history_loader import load_history_df

    df, source = load_history_df(stock_code, days=effective_days)

    if df is None or df.empty:
        return _append_history_metadata(
            {"error": f"No historical data available for {stock_code}"},
            metadata,
        )

    if source != "db_cache":
        _, normalized_code = _history_code_candidates(stock_code)
        try:
            saved_count = _get_db().save_daily_data(df, normalized_code, source)
            logger.info(
                "Agent daily history persisted for %s (source=%s, new_records=%s)",
                normalized_code,
                source,
                saved_count,
            )
        except Exception as exc:
            logger.warning(
                "Agent daily history persistence failed for %s: %s",
                normalized_code,
                exc,
            )

    # Convert DataFrame to list of dicts (last N records)
    records = df.tail(min(effective_days, len(df))).to_dict(orient="records")
    # Ensure date is string
    for r in records:
        if "date" in r:
            r["date"] = str(r["date"])

    response_code = stock_code
    if source == "db_cache" and records:
        response_code = records[-1].get("code") or response_code

    return _append_history_metadata(
        {
            "code": response_code,
            "source": source,
            "cache_hit": source == "db_cache",
            "requested_days": effective_days,
            "effective_days": effective_days,
            "actual_records": len(records),
            "partial_cache": source == "db_cache" and len(records) < effective_days,
            "total_records": len(records),
            "data": records,
        },
        metadata,
    )


get_daily_history_tool = ToolDefinition(
    name="get_daily_history",
    description="Get daily OHLCV (open, high, low, close, volume) historical data "
    "with MA5/MA10/MA20 indicators. Returns the last N trading days.",
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="Stock code, e.g., '600519' (A-share), 'AAPL' (US)",
        ),
        ToolParameter(
            name="days",
            type="integer",
            description="Number of trading days to fetch (default: 60)",
            required=False,
            default=60,
        ),
    ],
    handler=_handle_get_daily_history,
    category="data",
)


# ============================================================
# get_chip_distribution
# ============================================================


def _estimate_chip_from_history(
    stock_code: str, df: Any, current_price: Optional[float]
) -> Optional[Dict[str, Any]]:
    """em 筹码源不可用时，用历史K线估算筹码核心指标（降级）。

    akshare ``stock_cyq_em`` 是唯一筹码源，em 网络/代理不可达时全部失败（无 fallback）。
    此函数用历史K线（tencent 等可用源）+ 当前价按成交量加权估算：
    - ``avg_cost``: VWAP（成交量加权均价）
    - ``profit_ratio``: 成交量加权获利盘比例（close ≤ 当前价的成交量占比）
    - ``cost_70/90``: 价格 15/5% 与 85/95% 分位
    - ``concentration``: 成本区间 / 均价

    返回带 ``estimated=True`` 标记的 dict（供 LLM 标注精度有限）；数据不足返回 None。
    """
    if df is None or getattr(df, "empty", True) or len(df) < 10:
        return None
    if not current_price or current_price <= 0:
        try:
            current_price = float(df.iloc[-1]["close"])
        except (KeyError, ValueError, TypeError):
            return None
    try:
        close = df["close"].astype(float)
        volume = df["volume"].astype(float)
    except (KeyError, ValueError, TypeError):
        return None
    total_vol = float(volume.sum())
    if total_vol <= 0:
        return None
    avg_cost = float((close * volume).sum() / total_vol)
    profit_ratio = float(volume[close <= current_price].sum() / total_vol)
    cost_70_low = float(close.quantile(0.15))
    cost_70_high = float(close.quantile(0.85))
    cost_90_low = float(close.quantile(0.05))
    cost_90_high = float(close.quantile(0.95))
    return {
        "code": stock_code,
        "date": str(df.iloc[-1].get("date", "")),
        "source": "estimated_from_history",
        "estimated": True,
        "profit_ratio": profit_ratio,
        "avg_cost": avg_cost,
        "cost_90_low": cost_90_low,
        "cost_90_high": cost_90_high,
        "concentration_90": (cost_90_high - cost_90_low) / avg_cost,
        "cost_70_low": cost_70_low,
        "cost_70_high": cost_70_high,
        "concentration_70": (cost_70_high - cost_70_low) / avg_cost,
    }


def _handle_get_chip_distribution(stock_code: str) -> dict[str, Any]:
    """Get chip distribution data."""
    manager = _get_fetcher_manager()
    chip = manager.get_chip_distribution(stock_code)

    if chip is None:
        # fail-open：em 筹码源不可用时，用历史K线估算降级（标注 estimated）
        try:
            from src.services.history_loader import load_history_df

            df, _ = load_history_df(stock_code, days=60)
            quote = manager.get_realtime_quote(stock_code)
            current_price = getattr(quote, "price", None) if quote else None
            estimated = _estimate_chip_from_history(stock_code, df, current_price)
        except Exception as exc:
            logger.debug("get_chip_distribution(%s) 估算降级失败: %s", stock_code, exc)
            estimated = None
        if estimated:
            logger.info(
                "get_chip_distribution(%s): em 源不可用，历史K线估算降级", stock_code
            )
            return estimated
        return {"error": f"No chip distribution data available for {stock_code}"}

    return {
        "code": chip.code,
        "date": chip.date,
        "source": chip.source,
        "profit_ratio": chip.profit_ratio,
        "avg_cost": chip.avg_cost,
        "cost_90_low": chip.cost_90_low,
        "cost_90_high": chip.cost_90_high,
        "concentration_90": chip.concentration_90,
        "cost_70_low": chip.cost_70_low,
        "cost_70_high": chip.cost_70_high,
        "concentration_70": chip.concentration_70,
    }


get_chip_distribution_tool = ToolDefinition(
    name="get_chip_distribution",
    description="Get chip distribution analysis for a stock. Returns profit ratio, "
    "average cost, chip concentration at 90% and 70% levels. "
    "Useful for judging support/resistance and holding structure.",
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="A-share stock code, e.g., '600519'",
        ),
    ],
    handler=_handle_get_chip_distribution,
    category="data",
)


# ============================================================
# get_analysis_context
# ============================================================


def _handle_get_analysis_context(stock_code: str) -> dict[str, Any]:
    """Get stored analysis context from database."""
    db = _get_db()
    context = db.get_analysis_context(stock_code)

    if context is None:
        return {"error": f"No analysis context in DB for {stock_code}"}

    # Return safely serializable version (remove raw_data to save tokens)
    safe_context: Dict[str, Any] = {}
    for k, v in context.items():
        if k == "raw_data":
            safe_context["has_raw_data"] = True
            safe_context["raw_data_count"] = len(v) if isinstance(v, list) else 0
        else:
            safe_context[k] = v

    return safe_context


get_analysis_context_tool = ToolDefinition(
    name="get_analysis_context",
    description="Get historical analysis context from the database for a stock. "
    "Returns today's and yesterday's OHLCV data, MA alignment status, "
    "volume and price changes. Provides the technical data foundation.",
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="Stock code, e.g., '600519'",
        ),
    ],
    handler=_handle_get_analysis_context,
    category="data",
)


# ============================================================
# get_stock_info
# ============================================================


def _fallback_valuation_from_quote(
    manager: Any, stock_code: str, valuation: Dict[str, Any], field_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """fundamental valuation 超时缺失时，用 ``get_realtime_quote`` 兜底 pe/pb/市值。

    fundamental pipeline 的 valuation 受严格 fetch 超时（默认 3s）限制，而
    ``get_realtime_quote`` 带 em→sina→tencent fallback 实测可达 4-5s，em 源失败时
    易超时 → pe/pb/市值全 None（投研报告「数据缺失」）。估值是报告核心，此处用无
    超时限制的 ``get_realtime_quote`` 兜底补全缺失字段；失败则保持原值，不阻塞。

    只补缺失字段，fundamental 已拿到的非空值优先保留（不覆盖更优来源）。
    """
    try:
        quote = manager.get_realtime_quote(stock_code)
    except Exception as exc:
        logger.debug("get_stock_info valuation fallback failed %s: %s", stock_code, exc)
        return valuation
    if not quote:
        return valuation
    merged = dict(valuation)
    from data_provider.realtime_types import quote_field_records
    records = quote_field_records(quote)
    known_currencies = {record.get("currency") for key, record in (field_meta or {}).items() if key in {"current_price", "total_mv", "circ_mv"} and record.get("currency")}
    for field in ("pe_ratio", "pb_ratio", "total_mv", "circ_mv"):
        if field in {"total_mv", "circ_mv"} and known_currencies and known_currencies != {(records.get(field) or {}).get("currency")}:
            continue
        if merged.get(field) is None and (records.get(field) or {}).get("rule_eligible"):
            merged[field] = getattr(quote, field, None)
            if field_meta is not None and field in records:
                field_meta[field] = {**records[field], "selection_reason": "valuation_quote_fallback"}
    return merged


def _handle_get_stock_info(stock_code: str) -> dict[str, Any]:
    """Get stock fundamental information through unified fundamental context."""
    manager = _get_fetcher_manager()
    try:
        fundamental_context = manager.get_fundamental_context(stock_code)
    except Exception as e:
        logger.warning(
            f"get_stock_info via fundamental pipeline failed for {stock_code}: {e}"
        )
        fundamental_context = manager.build_failed_fundamental_context(
            stock_code, str(e)
        )

    compact_context = _compact_fundamental_context(fundamental_context)
    valuation = compact_context.get("valuation", {}).get("data", {})
    valuation_meta = dict(compact_context.get("valuation", {}).get("field_meta") or {})
    # fail-open：fundamental valuation 受严格超时易缺失，用 get_realtime_quote 兜底估值
    if any(valuation.get(field) is None for field in ("pe_ratio", "pb_ratio", "total_mv", "circ_mv")):
        valuation = _fallback_valuation_from_quote(manager, stock_code, valuation, valuation_meta)
    compact_context["valuation"] = {**compact_context.get("valuation", {}), "data": valuation, "field_meta": valuation_meta}
    primary_readings = {}
    for block_name in ("valuation", "growth", "earnings"):
        block = compact_context.get(block_name) or {}
        data = block.get("data") or {}
        if block_name == "earnings":
            data = data.get("financial_report") or {}
        records = dict(block.get("field_meta") or {})
        for field in ("pe_ratio", "pb_ratio", "total_mv", "circ_mv", "revenue", "net_profit", *_GROWTH_CV_FIELDS):
            scalar_key = "net_profit_parent" if field == "net_profit" and block_name == "earnings" else field
            reading = reading_from_field_record(data.get(scalar_key), records.get(field))
            if reading is not None and field not in primary_readings:
                primary_readings[field] = reading
                records[field] = adopted_field_record(field, reading, previous_record=records.get(field))
        block["field_meta"] = records

    # opt-in 交叉验证：估值/财务/增长锚点。period 驱动 iFinD 财务类查询返回数据；
    # 快照类锚点（行情/估值）不受 period 影响。开关关 → None（零回归）。
    _cv = build_cross_validation_block(
        stock_code,
        [
            "pe_ratio",
            "pb_ratio",
            "total_mv",
            "circ_mv",
            "revenue",
            "net_profit",
            "roe",
            "gross_margin",
            "revenue_yoy",
            "net_profit_yoy",
        ],
        period=_latest_annual_period(),
        primary_readings=primary_readings,
    )
    # akshare growth 失败时用 CV 回填毛利率/营收增速/ROE，避免报告「数据缺失」
    if _cv:
        for block_name in ("valuation", "growth", "earnings"):
            records = (compact_context.get(block_name) or {}).get("field_meta") or {}
            for field in list(records):
                anchor = _cv["anchors"].get(field)
                current = reading_from_field_record(records[field].get("value"), records[field])
                if isinstance(anchor, dict) and current == primary_readings.get(field):
                    record = field_record_from_validation(field, anchor)
                    if record is not None:
                        records[field] = record
        compact_context = {
            **compact_context,
            "growth": _backfill_growth_from_validation(
                compact_context.get("growth"), _cv
            ),
        }

    sector_rankings = compact_context.get("boards", {}).get("data", {})
    belong_boards = manager.get_belong_boards(stock_code)

    stock_name = stock_code.upper()
    try:
        stock_name = manager.get_stock_name(stock_code) or stock_name
    except Exception:
        pass

    response = {
        "code": stock_code.upper(),
        "name": stock_name,
        "pe_ratio": valuation.get("pe_ratio"),
        "pb_ratio": valuation.get("pb_ratio"),
        "total_mv": valuation.get("total_mv"),
        "circ_mv": valuation.get("circ_mv"),
        "fundamental_context": compact_context,
        "belong_boards": belong_boards,
        # Compatibility alias for existing callers; prefer belong_boards.
        # Planned for future deprecation in a major version.
        "boards": belong_boards,
        "sector_rankings": sector_rankings,
    }
    if _cv:
        response["cross_validation"] = _cv
    return response


get_stock_info_tool = ToolDefinition(
    name="get_stock_info",
    description="Get stock fundamental information: valuation, growth, earnings, institution flow, "
    "stock sector membership (belong_boards; boards is compatibility alias) and "
    "sector rankings. Returns a compact fundamental_context to reduce token usage.",
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="A-share stock code, e.g., '600519'",
        ),
    ],
    handler=_handle_get_stock_info,
    category="data",
)


# ============================================================
# get_portfolio_snapshot
# ============================================================


def _handle_get_portfolio_snapshot(
    account_id: Optional[int] = None,
    cost_method: str = "fifo",
    include_positions: bool = False,
    include_risk: bool = True,
    as_of: Optional[str] = None,
) -> dict[str, Any]:
    """Get compact portfolio snapshot for account-aware suggestions."""
    method = (cost_method or "fifo").strip().lower()
    if method not in {"fifo", "avg"}:
        return {"error": "cost_method must be fifo or avg"}

    as_of_date = None
    if as_of:
        try:
            as_of_date = date.fromisoformat(str(as_of).strip())
        except ValueError:
            return {"error": "as_of must be YYYY-MM-DD"}

    try:
        from src.services.portfolio_service import PortfolioService
        from src.services.portfolio_risk_service import PortfolioRiskService
    except Exception as exc:
        logger.warning("get_portfolio_snapshot unavailable: %s", exc)
        return {
            "status": "not_supported",
            "error": f"portfolio module unavailable: {exc}",
        }

    try:
        portfolio_service = PortfolioService()
        snapshot = portfolio_service.get_portfolio_snapshot(
            account_id=account_id,
            as_of=as_of_date,
            cost_method=method,
        )
        result = {
            "status": "ok",
            "snapshot": _compact_portfolio_snapshot(
                snapshot, include_positions=bool(include_positions)
            ),
        }
        if include_risk:
            try:
                risk_service = PortfolioRiskService(portfolio_service=portfolio_service)
                risk = risk_service.get_risk_report(
                    account_id=account_id,
                    as_of=as_of_date,
                    cost_method=method,
                )
                result["risk"] = {"status": "ok", **_compact_portfolio_risk(risk)}
            except Exception as risk_exc:
                logger.warning("get_portfolio_snapshot risk block failed: %s", risk_exc)
                result["risk"] = {"status": "failed", "error": str(risk_exc)}
        return result
    except Exception as exc:
        logger.warning("get_portfolio_snapshot failed: %s", exc)
        return {
            "status": "failed",
            "error": f"failed to fetch portfolio snapshot: {exc}",
        }


get_portfolio_snapshot_tool = ToolDefinition(
    name="get_portfolio_snapshot",
    description="Get portfolio snapshot summary and optional risk blocks. "
    "Default returns compact summary for lower token usage; "
    "set include_positions=true to include full position details.",
    parameters=[
        ToolParameter(
            name="account_id",
            type="integer",
            description="Optional account id; omit to use all active accounts.",
            required=False,
            default=None,
        ),
        ToolParameter(
            name="cost_method",
            type="string",
            description="Cost method: fifo or avg (default: fifo).",
            required=False,
            default="fifo",
            enum=["fifo", "avg"],
        ),
        ToolParameter(
            name="include_positions",
            type="boolean",
            description="Whether to include full positions in snapshot output (default: false).",
            required=False,
            default=False,
        ),
        ToolParameter(
            name="include_risk",
            type="boolean",
            description="Whether to include risk summary block (default: true).",
            required=False,
            default=True,
        ),
        ToolParameter(
            name="as_of",
            type="string",
            description="Optional snapshot date in YYYY-MM-DD format (default: today).",
            required=False,
            default=None,
        ),
    ],
    handler=_handle_get_portfolio_snapshot,
    category="data",
)


# ============================================================
# Export all data tools
# ============================================================

ALL_DATA_TOOLS = [
    get_realtime_quote_tool,
    get_daily_history_tool,
    get_chip_distribution_tool,
    get_analysis_context_tool,
    get_stock_info_tool,
    get_portfolio_snapshot_tool,
]


# ============================================================
# get_capital_flow
# ============================================================


def _handle_get_capital_flow(stock_code: str) -> dict[str, Any]:
    """Get main-force capital flow data for a stock."""
    manager = _get_fetcher_manager()
    try:
        ctx = manager.get_capital_flow_context(stock_code)
    except Exception as exc:
        logger.warning("get_capital_flow failed for %s: %s", stock_code, exc)
        return {
            "stock_code": stock_code,
            "status": "error",
            "error": f"capital flow fetch failed: {exc}",
        }

    status = ctx.get("status", "not_supported")
    if status == "not_supported":
        return {
            "stock_code": stock_code,
            "status": "not_supported",
            "note": "Capital flow data is only available for A-share stocks (not ETFs/indices).",
        }

    data = ctx.get("data", {})
    stock_flow = data.get("stock_flow") or {}
    sector_rankings = data.get("sector_rankings") or {}
    errors = ctx.get("errors") or []

    # failed（资金流源 stock_individual_fund_flow 走 push2his.eastmoney.com，代理/限流
    # 环境下可能不可达且无 em 备份源）→ 加明确 note，避免 LLM 误判「数据缺失」而编造
    note = None
    if status == "failed":
        note = (
            "资金流数据源暂不可用（push2his 接口不可达/超时），本报告暂缺资金流分析，"
            "请结合成交量/换手率等技术指标判断主力动向。"
        )

    result = {
        "stock_code": stock_code,
        "status": status,
        "main_net_inflow": stock_flow.get("main_net_inflow"),
        "inflow_5d": stock_flow.get("inflow_5d"),
        "inflow_10d": stock_flow.get("inflow_10d"),
        "sector_rankings": {
            "top_inflow_sectors": sector_rankings.get("top", [])[:3],
            "top_outflow_sectors": sector_rankings.get("bottom", [])[:3],
        },
        "errors": errors,
    }
    if note:
        result["note"] = note
    # opt-in 交叉验证：主力净流入/融资余额 MX↔iFinD 双源验证（方向+量级）；开关关 → _cv None，零回归。
    records = dict(ctx.get("field_meta") or stock_flow.get("field_meta") or {})
    main = reading_from_field_record(result["main_net_inflow"], records.get("main_inflow"))
    if main is not None:
        records["main_inflow"] = adopted_field_record("main_inflow", main, previous_record=records.get("main_inflow"))
    result["field_meta"] = records
    currency = ctx.get("currency") or stock_flow.get("currency") or (records.get("main_inflow") or {}).get("currency")
    _cv = build_cross_validation_block(stock_code, ["main_inflow", "margin_balance"], expected_currencies={field: currency for field in ("main_inflow", "margin_balance")} if currency else None, primary_readings={"main_inflow": main} if main is not None else None)
    if _cv:
        for field, anchor in _cv["anchors"].items():
            record = field_record_from_validation(field, anchor)
            if record is not None:
                records[field] = record
        # iFinD 多日累计（稳定源）：akshare push2his 不可达 → 主力净流入/5d/10d 回填
        result = _backfill_capital_flow(
            result, _cv, get_main_inflow_cumulative(stock_code)
        )
        result["cross_validation"] = _cv
    return result


get_capital_flow_tool = ToolDefinition(
    name="get_capital_flow",
    description=(
        "Get main-force (主力) capital flow data for an A-share stock. "
        "Returns today's net inflow, 5-day and 10-day cumulative inflows, "
        "and top sector-level capital flow rankings. "
        "Only supported for A-share individual stocks (not ETFs, indices, HK, or US stocks)."
    ),
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="A-share stock code, e.g., '600519'",
        ),
    ],
    handler=_handle_get_capital_flow,
    category="data",
)


ALL_DATA_TOOLS.append(get_capital_flow_tool)
