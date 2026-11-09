"""DB-first K-line history loader for Agent tools.

Provides:
- ContextVar-based frozen target_date propagation across threads
- ``load_history_df``: read from DB first, DataFetcherManager fallback
- issue #86: current-stock task window scope (ContextVar, same lifecycle as
  frozen target_date) so enabled eligible reads share one complete same-source
  window per task instead of stitching fresh tails onto old DB rows. A
  changed-source or overlap-conflicting expansion fails that demand explicitly
  (the returned *source* carries the reason); the adopted frame is retained.

Fixes #1066 – eliminates 45+ redundant HTTP requests per stock in Agent mode.
"""
from __future__ import annotations

import contextvars
import logging
from concurrent.futures import Future, TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from threading import Lock, RLock
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple, cast
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from src.deep_research_dims.context import SharedContext

import pandas as pd

logger = logging.getLogger(__name__)
_CACHE_MIN_RECORDS = 30

# ---------------------------------------------------------------------------
# Frozen target date (ContextVar) – set once per stock in pipeline, read by
# all agent tool threads via copy_context().run().
# ---------------------------------------------------------------------------
_frozen_target_date: contextvars.ContextVar[Optional[date]] = contextvars.ContextVar(
    "_frozen_target_date", default=None,
)


def set_frozen_target_date(d: date) -> contextvars.Token[Any]:
    return _frozen_target_date.set(d)


def get_frozen_target_date() -> Optional[date]:
    return _frozen_target_date.get()


def reset_frozen_target_date(token: contextvars.Token[Any]) -> None:
    _frozen_target_date.reset(token)


def refuse_stopped_or_expired() -> None:
    """启用范围内复用/发布/落库前的停止与外层 deadline 复查。"""
    from time import monotonic

    from src.core.research_run import current_research
    from src.patches.eastmoney_patch import request_deadline

    run = current_research.get()
    if run is not None:
        run.check()
    deadline = request_deadline.get()
    if deadline is not None and deadline <= monotonic():
        raise TimeoutError("daily window deadline exhausted before adoption")


def propagate_if_control_exhausted(exc: BaseException) -> None:
    """只把真正的停止或已耗尽的外层控制预算继续抛出。

    没有 run、也没有外层 deadline 时，分析器/供应商自己的 TimeoutError 保持
    原有隔离，不当成任务取消。
    """
    from src.core.research_run import ResearchStopped, current_research

    if isinstance(exc, ResearchStopped):
        raise exc

    run = current_research.get()
    if run is not None:
        run.check()
    from time import monotonic

    from src.patches.eastmoney_patch import request_deadline

    deadline = request_deadline.get()
    if deadline is not None and deadline <= monotonic():
        if isinstance(exc, TimeoutError):
            raise exc
        raise TimeoutError("daily window deadline exhausted") from exc


# ---------------------------------------------------------------------------
# issue #86：当前股票任务窗口作用域（ContextVar，随 frozen_target_date 生命周期）。
# 启用范围内（choice + ENABLE_IFIND_MCP + 合格沪深普通股）的一次股票任务共享
# 一个完整同源窗口：消费读副本、扩窗整窗重取、失败/迟到结果不覆盖已采用帧。
# ---------------------------------------------------------------------------
@dataclass
class _WindowFrame:
    source: str
    as_of: str  # 最新已完成交易日（ISO）
    records: List[Dict[str, Any]]  # 升序 std 列记录
    consumed: bool = False

    @property
    def start(self) -> str:
        return str(self.records[0].get("date"))[:10] if self.records else ""


class _StockWindowScope:
    """一个股票任务的共享窗口持有者；锁保护下的发布/读取/失败/单飞。"""

    def __init__(self, stock: str, as_of: str, refresh: str) -> None:
        self.stock = stock
        self.as_of = as_of
        self.refresh = refresh
        self._lock = RLock()
        self._frame: Optional[_WindowFrame] = None
        self._failures: Dict[str, str] = {}
        self._inflight: Dict[str, Future[Tuple[pd.DataFrame, str]]] = {}

    def read(self, min_rows: int, span_start: date) -> Optional[Tuple[pd.DataFrame, str]]:
        """消费读：覆盖需求则返回记录副本（并标记已消费），否则 None。"""
        with self._lock:
            refuse_stopped_or_expired()
            frame = self._frame
            if frame is None or not _frame_covers(frame, min_rows, span_start):
                return None
            frame.consumed = True
            records = [dict(row) for row in frame.records]
            source = frame.source
        return _frame_to_df(records), source

    def peek(self) -> Optional[_WindowFrame]:
        with self._lock:
            return self._frame

    def adopt_and_serve(self, records: List[Dict[str, Any]], source: str) -> Tuple[pd.DataFrame, str]:
        """在同一把锁里采纳、标记已消费，并返回实际采纳帧的副本。"""
        stamped = [{**dict(row), "data_source": source} for row in records]
        candidate = _WindowFrame(source=source, as_of=self.as_of, records=stamped, consumed=True)
        with self._lock:
            refuse_stopped_or_expired()
            current = self._frame
            if current is None:
                self._frame = candidate
            elif current.source == source and len(candidate.records) > len(current.records) and _overlap_consistent(current.records, candidate.records):
                self._frame = candidate
            self._frame.consumed = True
            adopted = self._frame
            served = [dict(row) for row in adopted.records]
            adopted_source = adopted.source
        return _frame_to_df(served), adopted_source

    def note_failure(self, need: str, reason: str) -> None:
        with self._lock:
            self._failures.setdefault(need, reason)

    def mark_consumed(self, frame: _WindowFrame) -> None:
        """把交付给消费者的帧标记为已消费（换源扩窗自此降级保留）。"""
        with self._lock:
            frame.consumed = True

    def failure_for(self, need: str) -> Optional[str]:
        with self._lock:
            return self._failures.get(need)

    def fetch_once(
        self, need: str, fetch: Callable[[], Tuple[pd.DataFrame, str]]
    ) -> Tuple[pd.DataFrame, str]:
        """同需求并发只执行一次完整获取（MCP+CSV+解析）；失败同样共享。

        等待者在等待期间持续检查运行停止与外层 deadline——已取消的 run 即使
        没有外层 deadline 也不会无限等待；所有等待方拿到结果前都经同一停止
        检查，迟到的成功不会在停止后被消费。
        """
        from time import monotonic as _monotonic

        from src.core.research_run import ResearchStopped, current_research
        from src.patches.eastmoney_patch import request_deadline

        def _waiter_alive() -> None:
            run = current_research.get()
            if run is not None:
                run.check()
            deadline = request_deadline.get()
            if deadline is not None and deadline <= _monotonic():
                raise TimeoutError("shared daily window fetch deadline exhausted")

        with self._lock:
            refuse_stopped_or_expired()
            existing = self._inflight.get(need)
            owner = existing is None
            future: Future[Tuple[pd.DataFrame, str]] = existing or Future()
            if owner:
                self._inflight[need] = future
        if not owner:
            deadline = request_deadline.get()
            while True:
                _waiter_alive()
                if future.done():
                    _waiter_alive()
                    return _copy_result(future.result())
                remaining = (deadline - _monotonic()) if deadline is not None else 0.2
                if remaining <= 0:
                    raise TimeoutError("shared daily window fetch deadline exhausted")
                try:
                    return _copy_result(future.result(timeout=min(0.05, remaining)))
                except FutureTimeout:
                    continue
        try:
            result = fetch()
        except BaseException as exc:
            future.set_exception(exc)
            raise
        # owner 完成后同样过停止检查：停止/超时后的迟到成功不得写入可复用的
        # 成功 future（等待方与后续同需求消费共享该 future）。
        try:
            _waiter_alive()
        except (ResearchStopped, TimeoutError) as stop_exc:
            future.set_exception(stop_exc)
            raise
        future.set_result(result)
        return _copy_result(result)


def _copy_result(result: Tuple[pd.DataFrame, str]) -> Tuple[pd.DataFrame, str]:
    df, source = result
    return df.copy(), source


def _frame_to_df(records: List[Dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame([dict(row) for row in records])


def _frame_covers(frame: _WindowFrame, min_rows: int, span_start: date) -> bool:
    if not frame.records:
        return False
    if len(frame.records) >= max(1, min_rows):
        return True
    # 行数不足但已覆盖请求跨度起点：窗口是可得完整历史，不再重复整窗重取。
    start = frame.start
    if not start:
        return False
    try:
        return datetime.strptime(start, "%Y-%m-%d").date() <= span_start
    except ValueError:
        return False


_VALUE_FIELDS = ("open", "high", "low", "close", "volume", "amount")


def _overlap_consistent(old_records: List[Dict[str, Any]], new_records: List[Dict[str, Any]]) -> bool:
    old_by_date = {str(row.get("date"))[:10]: row for row in old_records}
    for row in new_records:
        day = str(row.get("date"))[:10]
        previous = old_by_date.get(day)
        if previous is None:
            continue
        for key in _VALUE_FIELDS:
            old_value, new_value = previous.get(key), row.get(key)
            if old_value is None and new_value is None:
                continue
            try:
                if abs(float(old_value) - float(new_value)) > 1e-6:
                    return False
            except (TypeError, ValueError):
                if old_value != new_value:
                    return False
    return True


def _expansion_rejection(
    served_source: str,
    served_records: List[Dict[str, Any]],
    candidate_source: str,
    candidate_records: List[Dict[str, Any]],
) -> Optional[str]:
    """未被采纳的扩窗候选的显式拒绝原因；同源一致候选返回 None（照常服务旧帧）。

    采纳入口（作用域/run 槽）只负责保留权威帧；这里在权威帧上分类：换源或
    重叠值冲突的候选说明本次扩窗无法被诚实地满足——返回拒绝原因，由调用方
    让该次需求显式失败并共享失败，已采用帧留给已满足的更短需求复用。同源
    且重叠一致的候选是供应商真实可得短历史（如 IPO/首取原源），不算拒绝。
    """
    if served_source == candidate_source and _overlap_consistent(served_records, candidate_records):
        return None
    if served_source != candidate_source:
        return (
            f"daily window expansion rejected: retained {served_source} window kept, "
            f"candidate source {candidate_source} not adopted"
        )
    return (
        f"daily window expansion rejected: retained {served_source} window kept, "
        "candidate conflicts with adopted window on overlapping rows"
    )


_current_stock_window: contextvars.ContextVar[Optional[_StockWindowScope]] = contextvars.ContextVar(
    "_current_stock_window", default=None,
)


def open_current_stock_scope(
    stock_code: str, *, target_date: Optional[date] = None, refresh: str = ""
) -> Tuple[contextvars.Token[Any], _StockWindowScope]:
    """随 frozen_target_date 生命周期打开当前股票任务窗口作用域。"""
    scope = _StockWindowScope(
        _history_code_candidates(stock_code)[1],
        cache_session_date(stock_code, target_date).isoformat(),
        refresh,
    )
    return _current_stock_window.set(scope), scope


def close_current_stock_scope(token: contextvars.Token[Any]) -> None:
    _current_stock_window.reset(token)


def _matching_scope(stock_code: str, required: date) -> Optional[_StockWindowScope]:
    from data_provider.ifind_daily_adapter import ifind_daily_read_scope

    scope = _current_stock_window.get()
    if scope is None or not ifind_daily_read_scope(stock_code):
        return None
    if scope.stock != _history_code_candidates(stock_code)[1]:
        return None
    if scope.as_of != required.isoformat():
        return None
    return scope


def current_window_frame(
    stock_code: str, *, min_rows: int = 1, target_date: Optional[date] = None
) -> Optional[Tuple[pd.DataFrame, str]]:
    """读取（并消费）当前股票任务的共享窗口；未启用/不匹配/未覆盖返回 None。"""
    if target_date is not None:
        required = cache_session_date(stock_code, target_date)
    elif (frozen := get_frozen_target_date()) is not None:
        required = cache_session_date(stock_code, frozen)
    else:
        required = cache_session_date(stock_code)
    scope = _matching_scope(stock_code, required)
    from src.core.research_run import current_research

    run = current_research.get()
    if run is not None:
        refuse_stopped_or_expired()
        slot = run.published_daily_history()
        # 槽在停止时按既有契约返回 None（部分报告渲染）。阻塞读返回后必须再查
        # 控制状态，不能把锁等待期间的取消当成缓存缺失。
        refuse_stopped_or_expired()
        if not slot or slot.get("as_of") != required.isoformat():
            return None
        frame = _WindowFrame(
            source=str(slot.get("source") or ""), as_of=str(slot.get("as_of")),
            records=[dict(row) for row in slot.get("records") or []], consumed=True,
        )
        if not _frame_covers(frame, min_rows, required - timedelta(days=1)):
            return None
        return _frame_to_df(frame.records), frame.source
    if scope is None:
        return None
    refuse_stopped_or_expired()
    return scope.read(min_rows, required - timedelta(days=1))


def analysis_context_from_window(
    stock_code: str,
    records: List[Dict[str, Any]],
    *,
    analyze_ma: Optional[Callable[[Any], str]] = None,
    source: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """从本次共享窗口派生今日/昨日上下文（与 storage.get_analysis_context 同形）。

    不再单独从 DB 取最新两天，避免「新取窗口 + 旧 DB 行」混算；均线形态
    判断复用调用方注入的同一实现，不复制业务语义。携带真实日线来源（实际
    提供方）、价格口径（iFinD 现金分红前复权窗口才标 cash-qfq）与窗口实际
    截止日——截止日是本次已采纳窗口的最后一日，不冒充供应商回传 baseDate。
    """
    usable = [row for row in records if str(row.get("date") or "")[:10] > ""]
    if len(usable) < 2:
        return None
    provider = str(source or usable[-1].get("data_source") or "")
    today_row, yesterday_row = usable[-1], usable[-2]
    today = dict(today_row)
    today["code"] = stock_code
    yesterday = dict(yesterday_row)
    yesterday["code"] = stock_code
    if provider:
        today["data_source"] = provider
        yesterday["data_source"] = provider
    context: Dict[str, Any] = {
        "code": stock_code,
        "date": str(today_row.get("date"))[:10],
        "today": today,
        "yesterday": yesterday,
    }
    if provider:
        context["daily_history_source"] = provider
        from data_provider.ifind_daily_adapter import is_ifind_daily_source

        if is_ifind_daily_source(provider):
            context["daily_price_mode"] = "前复权(现金分红)"
    context["daily_history_as_of"] = str(today_row.get("date"))[:10]
    try:
        today_volume = float(today_row.get("volume")) if today_row.get("volume") is not None else None
        yesterday_volume = float(yesterday_row.get("volume")) if yesterday_row.get("volume") is not None else None
        today_close = float(today_row.get("close")) if today_row.get("close") is not None else None
        yesterday_close = float(yesterday_row.get("close")) if yesterday_row.get("close") is not None else None
    except (TypeError, ValueError):
        return context
    if yesterday_volume and yesterday_volume > 0 and today_volume is not None:
        context["volume_change_ratio"] = float(today_volume / yesterday_volume)
    if yesterday_close and yesterday_close > 0 and today_close is not None:
        context["price_change_ratio"] = float((today_close - yesterday_close) / yesterday_close * 100)
    if analyze_ma is not None:
        context["ma_status"] = analyze_ma(
            SimpleNamespace(close=today_row.get("close"), ma5=today_row.get("ma5"),
                            ma10=today_row.get("ma10"), ma20=today_row.get("ma20"))
        )
    return context


def _window_contains_ifind(bars: List[Any]) -> bool:
    """读取范围内是否含本票遗留 iFinD 日线行（含 iFinD 与旧源混合）。"""
    from data_provider.ifind_daily_adapter import is_ifind_daily_source

    return any(is_ifind_daily_source(getattr(bar, "data_source", None)) for bar in bars)


def db_bars_carry_ifind(bars: List[Any]) -> bool:
    """公开资格边界：DB 行窗口是否含本票 iFinD 日线行（issue #86 revision-2）。

    所有直接消费持久 DB 日线行的入口（断点续传、趋势、普通/Agent 上下文、
    预取、工具）必须先经此判定；含 iFinD 行（含混合）的窗口拒绝，改走原链
    整窗重取，纯旧源混存缓存保持既有规则。
    """
    return _window_contains_ifind(bars)


def qualified_analysis_context(
    stock_code: str,
    db: Any,
    *,
    analyze_ma: Optional[Callable[[Any], str]] = None,
) -> Optional[Dict[str, Any]]:
    """今日/昨日上下文的统一资格入口（issue #86 revision-2）。

    先读共享窗口（启用范围内的当前任务帧）；否则读 DB 最新两天并做 iFinD
    遗留行污染判定——被污染的窗口不放行，改经 ``load_history_df`` 整窗走原
    链重取后从新鲜窗口派生；纯旧源窗口保持既有 DB 语义。启用且合格的证券
    在共享窗口缺失（获取失败/未取得）时**不回退旧 DB**，如实返回 None。
    """
    from data_provider.ifind_daily_adapter import ifind_daily_read_scope
    from src.core.research_run import ResearchStopped

    try:
        window = current_window_frame(stock_code, min_rows=2)
    except ResearchStopped:
        raise
    except TimeoutError as exc:
        propagate_if_control_exhausted(exc)
        logger.debug("qualified_analysis_context(%s): shared window unavailable: %s", stock_code, exc)
        window = None
    except Exception as exc:  # noqa: BLE001 — 窗口读取失败按缺失处理
        logger.debug("qualified_analysis_context(%s): shared window unavailable: %s", stock_code, exc)
        window = None
    if window is not None:
        window_df, window_source = window
        records = [
            {**row, "date": str(row.get("date"))[:10]}
            for row in window_df.to_dict(orient="records")
        ]
        derived: Optional[Dict[str, Any]] = analysis_context_from_window(
            stock_code, records, analyze_ma=analyze_ma, source=window_source
        )
        return derived
    if ifind_daily_read_scope(stock_code):
        # 启用且合格：共享窗口缺失即上下文缺失，不以旧 DB 行复活。
        return None
    context: Optional[Dict[str, Any]] = db.get_analysis_context(stock_code)
    if context is None:
        return None
    try:
        latest = list(db.get_latest_data(stock_code, days=2) or [])
    except Exception:  # noqa: BLE001 — 读不到来源时保持既有语义
        return context
    if latest and db_bars_carry_ifind(latest):
        logger.info(
            "qualified_analysis_context(%s): DB 最新窗口含遗留 iFinD 行，整窗走原链重取",
            stock_code,
        )
        df, source = load_history_df(stock_code, days=30)
        if df is None or df.empty:
            return None
        records = [
            {**row, "date": str(row.get("date"))[:10]}
            for row in df.to_dict(orient="records")
        ]
        refetched: Optional[Dict[str, Any]] = analysis_context_from_window(
            stock_code, records, analyze_ma=analyze_ma, source=source
        )
        return refetched
    return context


# ---------------------------------------------------------------------------
# Internal DataFetcherManager singleton (fallback only)
# ---------------------------------------------------------------------------
_fetcher_singleton = None
_fetcher_lock = Lock()


def _get_fetcher_manager():
    global _fetcher_singleton
    if _fetcher_singleton is None:
        with _fetcher_lock:
            if _fetcher_singleton is None:
                from data_provider import DataFetcherManager
                _fetcher_singleton = DataFetcherManager()
    return _fetcher_singleton


# ---------------------------------------------------------------------------
# DB-first history loader
# ---------------------------------------------------------------------------
def _history_code_candidates(stock_code: str) -> Tuple[List[str], str]:
    from data_provider.base import canonical_stock_code, normalize_stock_code

    raw_code = str(stock_code or "").strip()
    normalized_code = canonical_stock_code(normalize_stock_code(raw_code))
    candidates: List[str] = []
    for candidate in (canonical_stock_code(raw_code), normalized_code):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates, normalized_code


def _coerce_bar_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").date()
        except ValueError:
            return date.min
    if hasattr(value, "date"):
        try:
            coerced = value.date()
            return coerced if isinstance(coerced, date) else date.min
        except Exception:
            return date.min
    return date.min


def _bar_date(bar: Any) -> date:
    row_date = _coerce_bar_date(getattr(bar, "date", None))
    if row_date != date.min:
        return row_date
    if hasattr(bar, "to_dict"):
        try:
            return _coerce_bar_date((bar.to_dict() or {}).get("date"))
        except Exception:
            return date.min
    return date.min


def _select_best_bars(db, stock_code: str, start: date, end: date) -> Tuple[Optional[str], list[Any]]:
    candidates, normalized_code = _history_code_candidates(stock_code)
    best_code = None
    best_bars = []
    best_key = None

    for candidate in candidates:
        bars = list(db.get_data_range(candidate, start, end) or [])
        if not bars:
            continue
        latest_date = max(_bar_date(bar) for bar in bars)
        key = (latest_date, len(bars), candidate == normalized_code)
        if best_key is None or key > best_key:
            best_key = key
            best_code = candidate
            best_bars = bars

    return best_code, best_bars


def cache_session_date(
    stock_code: str,
    target_date: Optional[date] = None,
    now: Optional[datetime] = None,
) -> date:
    """Latest completed daily bar for this analysis point, not the calendar date."""
    from src.core.trading_calendar import (
        MARKET_TIMEZONE,
        get_effective_trading_date,
        get_market_for_stock,
    )

    market = get_market_for_stock(stock_code) or "cn"
    if now is not None:
        return get_effective_trading_date(market, current_time=now)
    if target_date is not None:
        tz = ZoneInfo(MARKET_TIMEZONE.get(market, "Asia/Shanghai"))
        end_of_day = datetime.combine(target_date, time(23, 59), tzinfo=tz)
        return get_effective_trading_date(market, current_time=end_of_day)
    return get_effective_trading_date(market)


def _history_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    records = cast(List[Dict[str, Any]], df.to_dict(orient="records"))
    for row in records:
        if "date" in row:
            row["date"] = str(row["date"])[:10]
    return records


def _trim_to_session(df: pd.DataFrame, required: date) -> pd.DataFrame:
    if df is None or df.empty or "date" not in getattr(df, "columns", []):
        return df
    dates = df["date"].map(_coerce_bar_date)
    if (dates <= required).all():
        return df
    return df.loc[dates <= required].copy()


def _publish_history(df: pd.DataFrame, source: str, required: date) -> None:
    """Publish an already session-trimmed frame; keeps only the freshness guard."""
    from src.core.research_run import current_research

    run = current_research.get()
    if run is None or df is None or df.empty:
        return
    records = _history_records(df)
    latest = max((str(row.get("date") or "")[:10] for row in records), default="")
    if latest != required.isoformat():
        return
    run.publish_daily_history(required.isoformat(), records, source)


def adopt_published_history(ctx: "SharedContext") -> bool:
    """Copy this run's completed session into an empty context. Never fetches."""
    from src.core.research_run import current_research

    run = current_research.get()
    if run is None or getattr(ctx, "history", None):
        return False
    slot = run.published_daily_history()
    if not slot:
        return False
    try:
        analyzed_at = datetime.fromisoformat(str(ctx.as_of))
    except ValueError:
        return False
    required = cache_session_date(ctx.stock_code, now=analyzed_at)
    if slot.get("as_of") != required.isoformat():
        return False
    ctx.history = [dict(row) for row in slot["records"]]
    ctx.history_source = str(slot.get("source") or "")
    return True


def load_history_df(
    stock_code: str,
    days: int = 60,
    target_date: Optional[date] = None,
    min_records: Optional[int] = None,
    force_refresh: bool = False,
) -> Tuple[Optional[pd.DataFrame], str]:
    """Load K-line history, DB first with DataFetcherManager fallback.

    Returns ``(df, source)`` where *source* is ``"db_cache"`` on DB hit or the
    actual provider name on network fallback.  Returns ``(None, "none")`` when
    both paths fail in the legacy (non-enabled) path.  In the enabled window
    path every failure returns ``(None, <explicit reason>)`` instead — a
    changed-source or overlap-conflicting expansion of an already adopted
    window fails that demand explicitly (e.g. ``"daily window expansion
    rejected: ..."``) while the adopted frame is retained for later satisfied
    shorter needs; a genuine same-source short window (IPO/original source) is
    served as-is and is never rejected merely for having fewer rows than
    requested.

    ``min_records`` raises the DB-cache hit threshold for callers that need a
    longer window (e.g. weekly resampling needs ~1000 bars): without it a DB
    holding only the latest ~260 bars satisfies the legacy 30-bar floor and a
    1200-day request would silently return the short cache (issue #42).

    issue #86：启用范围内的合格沪深普通股读取不采纳持久 DB 命中——同一股票
    任务内先复用当前共享窗口（或深研 run 槽位），不足则经真实目标截止日完整
    重取并按同源一致规则整窗替换；换源/重叠冲突的扩窗对该次需求显式失败并
    携带原因，已采用帧不换源；失败在同一作用域内复用，不跨任务缓存失败。
    任何模式下，读取范围含本票遗留 iFinD 行（含 iFinD/旧源混合）的 DB 窗口
    都被拒绝并走原链；纯旧源混存缓存保持既有命中规则。
    """
    from data_provider.ifind_daily_adapter import ifind_daily_read_scope
    from src.core.research_run import daily_demand_span_start
    from src.storage import get_db

    if target_date is not None:
        required = cache_session_date(stock_code, target_date)
    elif (frozen := get_frozen_target_date()) is not None:
        required = cache_session_date(stock_code, frozen)
    else:
        required = cache_session_date(stock_code)
    start = daily_demand_span_start(required, days)
    if min_records is not None:
        required_records = min(int(min_records), days)
    else:
        required_records = max(min(days, _CACHE_MIN_RECORDS), 1)

    enabled_read = ifind_daily_read_scope(stock_code)
    scope = _matching_scope(stock_code, required)
    active_run = None
    if enabled_read:
        from src.core.research_run import current_research

        active_run = current_research.get()

    def _fetch_via_manager() -> Tuple[pd.DataFrame, str]:
        # 真实目标截止日传入 manager：历史冻结目标走原源时不再「先取今日再裁剪」。
        result: Tuple[pd.DataFrame, str] = _get_fetcher_manager().get_daily_data(
            stock_code, days=days, end_date=required.isoformat()
        )
        return result

    # 1) 活跃 ResearchRun 的历史槽是唯一权威帧，即使同时存在股票作用域。
    if enabled_read and active_run is not None and not force_refresh:
        refuse_stopped_or_expired()
        slot = active_run.published_daily_history()
        if slot and slot.get("as_of") == required.isoformat():
            slot_records = [dict(row) for row in slot.get("records") or []]
            slot_frame = _WindowFrame(
                source=str(slot.get("source") or ""), as_of=str(slot.get("as_of")),
                records=slot_records, consumed=True,
            )
            if _frame_covers(slot_frame, days, start):
                refuse_stopped_or_expired()
                return _frame_to_df(slot_records), slot_frame.source

    # 2) 无 run 时，当前股票任务作用域才持有帧。
    if enabled_read and active_run is None and scope is not None and not force_refresh:
        reused = scope.read(days, start)
        if reused is not None:
            refuse_stopped_or_expired()
            df, source = reused
            _publish_history(df, source, required)
            refuse_stopped_or_expired()
            logger.debug(
                "load_history_df(%s): %d bars from shared task window through %s",
                stock_code, len(df), required.isoformat(),
            )
            return df, source

    # 3) 持久 DB：启用范围不作为命中依据；其余模式沿用既有规则，仅拒绝含
    #    本票遗留 iFinD 行的窗口（含混合），纯旧源混存缓存保持原行为。
    if not enabled_read:
        try:
            db = get_db()
            _code, bars = _select_best_bars(db, stock_code, start, required)
            usable = [bar for bar in bars if date.min < _bar_date(bar) <= required]
            latest_date = max((_bar_date(bar) for bar in usable), default=date.min)
            if usable and latest_date == required and len(usable) >= required_records:
                if _window_contains_ifind(usable):
                    logger.info(
                        "load_history_df(%s): DB 窗口含本票遗留 iFinD 行，拒绝缓存命中并走原链",
                        stock_code,
                    )
                else:
                    df = pd.DataFrame([bar.to_dict() for bar in usable])
                    logger.debug(
                        "load_history_df(%s): %d bars from DB through %s (requested %d)",
                        stock_code, len(df), required.isoformat(), days,
                    )
                    _publish_history(df, "db_cache", required)
                    return df, "db_cache"
        except Exception as e:
            logger.debug("load_history_df(%s): DB read failed: %s", stock_code, e)

    # 4) 网络完整获取：启用范围内按「当前任务作用域 > 深研 run 槽位 > 临时
    #    作用域」复用与发布；同源一致才整窗替换，已消费窗口不换源。
    if enabled_read:
        return _enabled_network_window(
            stock_code, days=days, required=required,
            scope=scope, force_refresh=force_refresh, fetch=_fetch_via_manager,
        )
    try:
        df, source = _fetch_via_manager()
        if df is not None and not df.empty:
            trimmed = _trim_to_session(df, required)
            if trimmed is not None and not trimmed.empty:
                _publish_history(trimmed, source, required)
                return trimmed, source
            logger.warning(
                "load_history_df(%s): %s 返回 %d 行但均晚于已完成交易日 %s，本次不采用",
                stock_code, source, len(df), required.isoformat(),
            )
    except Exception as e:
        logger.warning("load_history_df(%s): DataFetcherManager failed: %s", stock_code, e)
    return None, "none"


def _enabled_network_window(
    stock_code: str,
    *,
    days: int,
    required: date,
    scope: Optional[_StockWindowScope],
    force_refresh: bool,
    fetch: Callable[[], Tuple[pd.DataFrame, str]],
) -> Tuple[Optional[pd.DataFrame], str]:
    """启用范围内的网络完整获取：同需求单飞、失败共享、同源整窗替换。"""
    from time import monotonic

    from src.core.research_run import ResearchStopped, current_research
    from src.patches.eastmoney_patch import request_deadline

    run = current_research.get()

    def _stop_or_expired() -> None:
        """采纳/发布前的停止与外层 deadline 复查（含无 run 的普通路径）。"""
        active_run = current_research.get()
        if active_run is not None:
            active_run.check()
        deadline = request_deadline.get()
        if deadline is not None and deadline <= monotonic():
            raise TimeoutError("daily window deadline exhausted before adoption")

    def _trim_or_fail(df: pd.DataFrame, source: str) -> Optional[List[Dict[str, Any]]]:
        trimmed = _trim_to_session(df, required)
        if trimmed is None or trimmed.empty:
            logger.warning(
                "load_history_df(%s): %s 返回 %d 行但均晚于已完成交易日 %s，本次不采用",
                stock_code, source, len(df), required.isoformat(),
            )
            return None
        return _history_records(trimmed)

    if run is None and scope is not None:
        need = f"{stock_code}|{required.isoformat()}|{days}|{scope.refresh}"
        if force_refresh:
            need += "|forced"
        prior_failure = None if force_refresh else scope.failure_for(need)
        if prior_failure is not None:
            # 失败复用也要先过控制复查：取消/外层 deadline 不因命中 memo 被吞。
            refuse_stopped_or_expired()
            logger.info(
                "load_history_df(%s): 同任务同需求此前已失败（%s），不再重复获取",
                stock_code, prior_failure,
            )
            return None, prior_failure
        try:
            df, source = scope.fetch_once(need, fetch)
        except ResearchStopped:
            raise
        except TimeoutError as exc:
            propagate_if_control_exhausted(exc)
            reason = f"TimeoutError: {exc}"
            scope.note_failure(need, reason)
            logger.warning("load_history_df(%s): DataFetcherManager failed: %s", stock_code, exc)
            return None, reason
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
            scope.note_failure(need, reason)
            logger.warning("load_history_df(%s): DataFetcherManager failed: %s", stock_code, e)
            return None, reason
        if df is None or df.empty:
            scope.note_failure(need, "empty result")
            logger.warning("load_history_df(%s): 网络返回空窗口", stock_code)
            return None, "empty result"
        _stop_or_expired()
        records = _trim_or_fail(df, source)
        if records is None:
            reason = f"no rows on or before {required.isoformat()}"
            scope.note_failure(need, reason)
            return None, reason
        _stop_or_expired()
        served_df, served_source = scope.adopt_and_serve(records, source)
        rejection = _expansion_rejection(
            served_source, _history_records(served_df), source, records,
        )
        if rejection is not None:
            # 换源/重叠冲突扩窗：本次需求显式失败（原因随 source 传出），已采用
            # 帧保留，同需求失败共享，不重复获取。
            scope.note_failure(need, rejection)
            logger.warning("load_history_df(%s): %s", stock_code, rejection)
            return None, rejection
        return served_df, served_source

    if run is not None:
        # 深研 run：同一报告内同需求（窗口/契约/刷新）只完整获取一次，失败同样共享。
        from src.config import get_config
        from src.core.research_run import supplier_need_key
        from data_provider.mx_data_adapter import financial_refresh

        marker = financial_refresh.get()
        refresh = (marker[1] if marker else "") + ("|forced" if force_refresh else "")
        key = supplier_need_key(
            "daily-window", get_config().data_source_contract, stock_code,
            f"{days}|{required.isoformat()}", refresh,
        )
        try:
            df, source = run.single_fetch(key, fetch)
        except ResearchStopped:
            raise
        except TimeoutError as exc:
            propagate_if_control_exhausted(exc)
            logger.warning("load_history_df(%s): DataFetcherManager failed: %s", stock_code, exc)
            return None, f"TimeoutError: {exc}"
        except Exception as e:
            logger.warning("load_history_df(%s): DataFetcherManager failed: %s", stock_code, e)
            return None, f"{type(e).__name__}: {e}"
        if df is None or df.empty:
            logger.warning("load_history_df(%s): 网络返回空窗口", stock_code)
            return None, "empty result"
        _stop_or_expired()
        records = _trim_or_fail(df, source)
        if records is None:
            return None, f"no rows on or before {required.isoformat()}"
        _stop_or_expired()
        adopted_slot = run.adopt_daily_history(required.isoformat(), records, source)
        if adopted_slot is None:
            raise ResearchStopped("research stopped before daily window adoption")
        served_records = [dict(row) for row in adopted_slot.get("records") or []]
        served_source = str(adopted_slot.get("source") or source)
        rejection = _expansion_rejection(served_source, served_records, source, records)
        if rejection is not None:
            # 换源/重叠冲突扩窗：该次需求显式失败；run 内同需求取数已被 single_fetch
            # 去重，失败不换健康记账，权威槽原样保留给已满足的更短需求。未满足
            # 需求经 owner 方法记录：run 锁内按当前权威帧复核，迟到拒绝不复活
            # 已被并发一致采纳满足的状态；采纳侧在同一把锁内消解已覆盖需求。
            run.record_unmet_daily_window(required.isoformat(), days, rejection)
            logger.warning("load_history_df(%s): %s", stock_code, rejection)
            return None, rejection
        return _frame_to_df(served_records), served_source

    # 无 run 无作用域的直接入口：本次调用临时作用域（含并发去重），finally 清理。
    temp_token, temp_scope = open_current_stock_scope(stock_code, target_date=required)
    try:
        need = f"{stock_code}|{required.isoformat()}|{days}|{temp_scope.refresh}"
        if force_refresh:
            need += "|forced"
        try:
            df, source = temp_scope.fetch_once(need, fetch)
        except ResearchStopped:
            raise
        except TimeoutError as exc:
            propagate_if_control_exhausted(exc)
            logger.warning("load_history_df(%s): DataFetcherManager failed: %s", stock_code, exc)
            return None, f"TimeoutError: {exc}"
        except Exception as e:
            logger.warning("load_history_df(%s): DataFetcherManager failed: %s", stock_code, e)
            return None, f"{type(e).__name__}: {e}"
        if df is None or df.empty:
            logger.warning("load_history_df(%s): 网络返回空窗口", stock_code)
            return None, "empty result"
        _stop_or_expired()
        records = _trim_or_fail(df, source)
        if records is None:
            return None, f"no rows on or before {required.isoformat()}"
        _stop_or_expired()
        served_df, served_source = temp_scope.adopt_and_serve(records, source)
        rejection = _expansion_rejection(
            served_source, _history_records(served_df), source, records,
        )
        if rejection is not None:
            logger.warning("load_history_df(%s): %s", stock_code, rejection)
            return None, rejection
        return served_df, served_source
    finally:
        close_current_stock_scope(temp_token)
