"""The deadline and retained results of one in-process research run."""

from __future__ import annotations

import threading
from collections.abc import Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, TimeoutError as FutureTimeout, wait
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from time import monotonic
from typing import TYPE_CHECKING, Any, Callable, Literal, TypeVar, cast
from uuid import uuid4

if TYPE_CHECKING:
    from src.llm.attempt import ModelAttempt

_T = TypeVar("_T")


def daily_demand_span_start(required: date, days: int) -> date:
    """日线扩窗请求的跨度起点（``load_history_df`` 窗口起点同一公式，单一来源）。"""
    return required - timedelta(days=int(days * 1.8) + 10)


def _records_cover_demand(records: list[dict[str, Any]], as_of: str, days: int) -> bool:
    """权威帧记录是否实际满足某需求：行数足额，或帧起点已到请求跨度起点。

    与 ``history_loader._frame_covers`` 同一覆盖语义（rows OR 请求跨度起点），
    供 run 锁内的未满足需求记录/消解复用；不取数、不判源（权威帧即同一性）。
    """
    if not records:
        return False
    if len(records) >= max(1, days):
        return True
    try:
        required = datetime.strptime(as_of, "%Y-%m-%d").date()
        start = datetime.strptime(str(records[0].get("date"))[:10], "%Y-%m-%d").date()
    except ValueError:
        return False
    return start <= daily_demand_span_start(required, days)


def supplier_need_key(supplier: str, contract: str, need: str, qualifier: str, refresh: str = "") -> str:
    """In-report identity for one supplier need. Not a cache key and not persisted."""
    return f"{supplier}|{contract}|{need}|{qualifier}|{refresh}"


def vendor_source_label(research: dict[str, Any] | None) -> str:
    """research 块实际来源的展示标签（issue #73）：不硬编码 Choice。

    iFinD 主源深研的公司/研究事实按块内真实 ``source`` 标注（iFinD）；历史
    Choice 载荷保持 Choice 标签不变；未知来源回退为供应商中性词，不冒认。
    """
    source = str((research or {}).get("source") or "")
    return {"choice": "Choice", "ifind": "iFinD"}.get(source, source or "供应商")


def research_field_date(research: dict[str, Any] | None, company: dict[str, Any] | None, indicator: str) -> str:
    """公司事实的截止日期：优先该列供应商自身日期（实际列元数据），无则回退
    块 as_of（历史 Choice 载荷形状）；两者都缺为「未标注」——iFinD 缺字段日期
    不得从请求日期或无关块推断。"""
    field_dates = (company or {}).get("field_dates") or {}
    return str(field_dates.get(indicator) or (research or {}).get("as_of") or "未标注")


def _isolated_copy(result: _T) -> _T:
    import copy

    if isinstance(result, dict):
        return cast(_T, copy.deepcopy(result))
    return result

current_research: ContextVar[ResearchRun | None] = ContextVar("current_research", default=None)
research_dimension: ContextVar[str] = ContextVar("research_dimension", default="")


class ResearchStopped(TimeoutError):
    """No new work may start; completed evidence can still be rendered."""


def is_cancellation_exception(exc: BaseException) -> bool:
    """True for a bare CancelledError or an exception group carrying one.

    SDK internals (asyncio.wait_for, anyio task groups) surface real timeouts
    and user cancels alike as cancellation-shaped exceptions. This predicate
    only recognises the shape; callers pair it with the run's own state
    (``stop_reason`` / ``finalized`` / ``remaining()``) to tell a user cancel
    from a transport timeout (issue #58).
    """
    import asyncio

    if isinstance(exc, asyncio.CancelledError):
        return True
    return isinstance(exc, BaseExceptionGroup) and exc.subgroup(asyncio.CancelledError) is not None


def carries_fatal_group_member(exc: BaseException) -> bool:
    """True for an exception group whose members include a system-exit signal.

    A cancellation member never gives the cancellation handling permission to
    absorb an unrelated fatal member (issue #58): callers re-raise such groups
    untouched so ``KeyboardInterrupt``/``SystemExit`` keep propagating, and
    only then apply their cancellation translation to pure-cancel shapes.
    """
    if not isinstance(exc, BaseExceptionGroup):
        return False
    return exc.subgroup((KeyboardInterrupt, SystemExit)) is not None


def _same_source_extension(current: dict[str, Any], candidate: dict[str, Any]) -> bool:
    """True only for a longer same-source window whose overlap agrees."""
    if current.get("source") != candidate.get("source"):
        return False
    old_records = list(current.get("records") or [])
    new_records = list(candidate.get("records") or [])
    if len(new_records) <= len(old_records):
        return False
    old_by_date = {str(row.get("date"))[:10]: row for row in old_records}
    for row in new_records:
        previous = old_by_date.get(str(row.get("date"))[:10])
        if previous is None:
            continue
        for key in ("open", "high", "low", "close", "volume", "amount"):
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


@dataclass
class ResearchRun:
    timeout_seconds: float
    run_id: str = field(default_factory=lambda: uuid4().hex)
    deadline: float = field(init=False)
    stop_reason: Literal["report_budget", "cancelled"] | None = None
    finalized: bool = False
    dimensions: dict[str, Any] = field(default_factory=dict)
    context: Any = None
    progress: Callable[[dict[str, Any]], None] | None = None
    evidence: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    facts: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    vendor_research: dict[str, dict[str, Any]] = field(default_factory=dict)
    daily_history: dict[str, Any] | None = None
    # issue #86（PR87 R13）：本报告内未满足的日线扩窗需求（"as_of|days" → 拒绝
    # 原因）。私有状态：记录/消解/快照全部在本 run 既有 _lock 内经
    # record_unmet_daily_window / _resolve_covered_unmet_locked /
    # unmet_daily_history_snapshot 进行，模块外不得直接读写；不持久化、不跨
    # 报告，仅供最终成文如实披露（停止后部分报告仍可读取）。
    _daily_history_unmet: dict[str, str] = field(default_factory=dict)
    # Model attempt ledger (issue #55): one entry per admission attempt.
    # Appends survive finish/cancel so a late in-flight usage stays recorded.
    attempts: list[ModelAttempt] = field(default_factory=list, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _pending: set[Future[Any]] = field(default_factory=set, repr=False)
    _on_clean: Callable[[], None] | None = field(default=None, repr=False)
    # Same-report single-flight results (issue #51): key -> completed-or-running
    # future. Failures are retained too; nothing here outlives this run.
    _fetches: dict[str, Future[Any]] = field(default_factory=dict, repr=False)
    # issue #71：Choice 原生 10001009（登录次数达上限）后，本报告内不再重复尝试
    # Choice 登录；字段随 run 生命周期，下一次报告照常尝试，不构成跨报告熔断。
    choice_login_limited: bool = False

    def __post_init__(self) -> None:
        self.deadline = monotonic() + self.timeout_seconds

    def remaining(self) -> float:
        return max(0.0, self.deadline - monotonic())

    def stop(self, reason: Literal["report_budget", "cancelled"]) -> None:
        with self._lock:
            if self.stop_reason is None and not self.finalized:
                self.stop_reason = reason

    def check(self) -> None:
        if self.remaining() <= 0:
            self.stop("report_budget")
        if self.stop_reason or self.finalized:
            raise ResearchStopped(self.reason_text)

    @property
    def reason_text(self) -> str:
        return "用户已取消本次研究" if self.stop_reason == "cancelled" else "报告总时间预算已用完"

    @property
    def clean(self) -> bool:
        with self._lock:
            return not self._pending

    def track(self, future: Future[Any]) -> None:
        with self._lock:
            self._pending.add(future)

        def done(completed: Future[Any]) -> None:
            with self._lock:
                self._pending.discard(completed)
            self._notify_clean()

        future.add_done_callback(done)

    def finish(self) -> None:
        with self._lock:
            self.finalized = True
        self._notify_clean()

    def when_clean(self, callback: Callable[[], None]) -> None:
        with self._lock:
            self._on_clean = callback
        self._notify_clean()

    def _notify_clean(self) -> None:
        callback = None
        with self._lock:
            if self.finalized and not self._pending:
                callback, self._on_clean = self._on_clean, None
        if callback is not None:
            callback()

    def wait_for(self, future: Future[Any], timeout: float | None = None) -> Any:
        from src.patches.eastmoney_patch import request_deadline
        caps = [self.deadline]
        if timeout is not None:
            caps.append(monotonic() + timeout)
        if (outer := request_deadline.get()) is not None:
            caps.append(outer)
        deadline = min(caps)
        while True:
            if future.done():
                return future.result()
            self.check()
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise FutureTimeout()
            try:
                return future.result(timeout=min(0.05, remaining))
            except FutureTimeout:
                if future.done():
                    return future.result()

    def completed(self, futures: Iterable[Future[Any]], timeout: float | None) -> Iterator[Future[Any]]:
        pending = set(futures)
        deadline = min(self.deadline, monotonic() + timeout) if timeout is not None else self.deadline
        while pending:
            self.check()
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise FutureTimeout()
            done, pending = wait(pending, timeout=min(0.05, remaining), return_when=FIRST_COMPLETED)
            yield from done

    def emit(self, event: dict[str, Any]) -> None:
        if self.progress is not None and not self.finalized:
            try:
                self.progress({**event, "run_id": self.run_id, "dim": research_dimension.get()})
            except Exception:
                # A detached progress listener cannot stop accepted research.
                pass

    def record_attempt(self, attempt: ModelAttempt) -> None:
        """Attach one model attempt to this run; finalization does not drop it."""
        with self._lock:
            self.attempts.append(attempt)

    def adopt_daily_history(self, as_of: str, records: list[dict[str, Any]], source: str) -> dict[str, Any] | None:
        """Atomically qualify and adopt one same-report window. Returns a copy.

        Stopped/expired runs adopt nothing. A consumed window is not replaced
        by another source, a conflicting overlap or a shorter late result;
        a longer same-source window with consistent overlap may extend it.
        Callers must use the returned frame, not the candidate they fetched.
        同一把锁内：采纳后立即把当前权威帧实际覆盖的未满足需求消解掉
        （PR87 R13）——并发重复消解幂等，迟到采纳不会留下已解决残留。
        """
        import copy

        with self._lock:
            if self.finalized or self.stop_reason or self.remaining() <= 0 or not records:
                return None
            from time import monotonic

            from src.patches.eastmoney_patch import request_deadline

            deadline = request_deadline.get()
            if deadline is not None and deadline <= monotonic():
                raise TimeoutError("daily window deadline exhausted before adoption")
            current = self.daily_history
            candidate = {
                "as_of": as_of,
                "records": [dict(row) for row in records],
                "source": source,
                "consumed": True,
            }
            if current is None:
                self.daily_history = candidate
            elif current.get("as_of") != as_of:
                pass
            elif _same_source_extension(current, candidate):
                self.daily_history = candidate
            if self.daily_history is None:
                return None
            self.daily_history["consumed"] = True
            self._resolve_covered_unmet_locked()
            return copy.deepcopy(self.daily_history)

    def record_unmet_daily_window(self, as_of: str, days: int, reason: str) -> None:
        """换源/重叠冲突拒绝后的未满足需求记录（run 锁内对当前权威帧复核）。

        迟到拒绝不得复活已解决状态：当前权威帧已按同一覆盖语义（行数或请求
        跨度起点、同一 as_of）实际满足该需求时不记录。纯状态更新，不取数；
        停止/预算后的部分报告仍可经快照读取既有状态。
        """
        key = f"{as_of}|{days}"
        with self._lock:
            frame = self.daily_history
            if frame is not None:
                frame_as_of = str(frame.get("as_of") or "")
                if frame_as_of == as_of and _records_cover_demand(
                    list(frame.get("records") or []), as_of, days
                ):
                    return
            self._daily_history_unmet[key] = reason

    def unmet_daily_history_snapshot(self) -> dict[str, str]:
        """未满足扩窗需求的隔离快照（同一把锁；停止后部分报告仍可读取）。"""
        with self._lock:
            return dict(self._daily_history_unmet)

    def _resolve_covered_unmet_locked(self) -> None:
        """锁内（调用方已持有 ``self._lock``）：当前权威帧实际覆盖的需求清除。

        幂等：锁内单方迭代快照键，并发重复消解不会相互踩踏（无 KeyError）。
        """
        frame = self.daily_history
        if frame is None or not self._daily_history_unmet:
            return
        as_of = str(frame.get("as_of") or "")
        records = list(frame.get("records") or [])
        for key in list(self._daily_history_unmet):
            parts = key.split("|")
            if len(parts) != 2 or parts[0] != as_of:
                continue
            try:
                days = int(parts[1])
            except ValueError:
                continue
            if _records_cover_demand(records, as_of, days):
                del self._daily_history_unmet[key]

    def publish_daily_history(self, as_of: str, records: list[dict[str, Any]], source: str) -> None:
        """Share one completed session inside this run. Does not touch a returned context."""
        self.adopt_daily_history(as_of, records, source)

    def published_daily_history(self) -> dict[str, Any] | None:
        """Read a copy of this run's shared window; reading marks it consumed."""
        import copy

        with self._lock:
            if self.finalized or self.stop_reason or self.remaining() <= 0:
                return None
            from time import monotonic

            from src.patches.eastmoney_patch import request_deadline

            deadline = request_deadline.get()
            if deadline is not None and deadline <= monotonic():
                raise TimeoutError("daily window deadline exhausted before adoption")
            if self.daily_history is None:
                return None
            self.daily_history["consumed"] = True
            return copy.deepcopy(self.daily_history)

    def single_fetch(self, key: str, fetch: Callable[[], _T], *, timeout: float | None = None) -> _T:
        """One outbound per report for the same need; failures are shared too.

        The first caller executes ``fetch`` in its own thread; concurrent
        callers wait on the same future. A waiter passes its own remaining
        stage budget as ``timeout`` so a short-budget consumer is not held
        until a longer owner finishes — it raises ``FutureTimeout`` instead,
        while the owner keeps running for the other consumers. Dict results
        are deep-copied per consumer so downstream edits cannot leak.
        """
        with self._lock:
            self.check()
            existing = self._fetches.get(key)
            future: Future[Any]
            if existing is not None:
                future = existing
                owner = False
            else:
                future = Future()
                self._fetches[key] = future
                owner = True
        if not owner:
            # The future was stored for this key's fetch; wait_for only knows Any.
            return _isolated_copy(cast(_T, self.wait_for(future, timeout=timeout)))
        try:
            result = fetch()
        except BaseException as exc:
            future.set_exception(exc)
            raise
        else:
            future.set_result(result)
            return _isolated_copy(result)

    def retain_vendor(self, dim: str, research: dict[str, Any]) -> None:
        with self._lock:
            if not self.finalized and self.stop_reason is None and self.remaining() > 0:
                self.vendor_research[dim] = dict(research)

    def retain(self, tool: str, result: Any) -> None:
        """Retain bounded search evidence, never arbitrary raw tool responses."""
        if not isinstance(result, dict) or result.get("error") or result.get("success") is False:
            return
        self.retain_facts(tool, result)
        from src.schemas.deep_research_dims import RetainedEvidence
        from pydantic import ValidationError

        candidates = result.get("results") or result.get("articles") or []
        dimensions = result.get("dimensions")
        if isinstance(dimensions, dict):
            candidates = [item for value in dimensions.values() if isinstance(value, dict)
                          for item in value.get("results", []) if isinstance(item, dict)]
        if not isinstance(candidates, list):
            return
        records = []
        for item in candidates[:10]:
            if not isinstance(item, dict):
                continue
            try:
                evidence = RetainedEvidence(
                    tool=tool[:100], title=str(item.get("title") or "")[:300],
                    url=str(item.get("url") or item.get("link") or ""),
                    source=str(item.get("source") or "搜索资料")[:160],
                    snippet=str(item.get("snippet") or item.get("content") or "")[:1000],
                    published_date=str(item.get("published_date") or "")[:40],
                )
                records.append(evidence.model_dump())
            except ValidationError:
                continue
        dim = research_dimension.get() or "legacy"
        with self._lock:
            if dim and not self.finalized and self.stop_reason is None and self.remaining() > 0:
                retained = self.evidence.setdefault(dim, [])
                retained.extend(record for record in records if record not in retained)
                self.evidence[dim] = retained[:20]

    def retain_facts(self, tool: str, result: Any) -> None:
        records = retained_tool_facts(tool, result)
        with self._lock:
            if records and not self.finalized and self.stop_reason is None and self.remaining() > 0:
                dim = research_dimension.get() or "legacy"
                self.facts.setdefault(dim, []).extend(records)
                self.facts[dim] = self.facts[dim][-30:]

    @contextmanager
    def bind(self) -> Iterator[None]:
        from src.patches.eastmoney_patch import request_deadline
        token = current_research.set(self)
        outer = request_deadline.get()
        budget = request_deadline.set(min(self.deadline, outer) if outer is not None else self.deadline)
        try:
            yield
        finally:
            request_deadline.reset(budget)
            current_research.reset(token)


def retained_tool_facts(tool: str, result: Any) -> list[dict[str, str]]:
    """Keep only known scalar source fields; never dump arbitrary tool JSON."""
    if not isinstance(result, dict) or result.get("error") or result.get("success") is False:
        return []
    from src.schemas.deep_research_dims import RetainedFact
    from pydantic import ValidationError
    import math

    fields = {
        # issue #64：单日窗口不再称「当日」——实际截至日随 field_meta.observed_at/
        # as_of_date 投影进 period。
        "get_capital_flow": {"main_net_inflow": "单日主力净流入", "inflow_5d": "5交易日主力净流入", "inflow_10d": "10交易日主力净流入"},
        "get_realtime_quote": {"price": "价格", "change_pct": "涨跌幅", "volume": "成交量", "amount": "成交额"},
        "get_stock_info": {"name": "公司名称", "pe_ratio": "PE", "pb_ratio": "PB", "total_mv": "总市值", "circ_mv": "流通市值", "revenue": "营业收入", "net_profit": "归母净利润", "roe": "加权ROE", "gross_margin": "毛利率", "revenue_yoy": "营收同比", "net_profit_yoy": "归母净利同比"},
        "get_daily_history": {"open": "最近交易日开盘", "high": "最近交易日最高", "low": "最近交易日最低", "close": "最近交易日收盘", "volume": "最近交易日成交量"},
        "analyze_trend": {"ma5": "5日均线", "ma10": "10日均线", "ma20": "20日均线", "current_price": "价格", "macd_dif": "MACD DIF", "rsi_6": "6日RSI"},
        "get_fundamental_context": {"institution_holding_change": "机构持仓变化", "top10_holder_change": "十大股东持股变化"},
        "support_resistance": {"support": "支撑", "resistance": "阻力"},
        "chanlun_engine": {"current_trend": "缠论趋势", "position": "缠论位置", "divergence": "缠论背驰"},
    }.get(tool, {})
    values = dict(result)
    metadata = dict(result.get("field_meta") or {})
    if tool == "get_stock_info":
        context = result.get("fundamental_context") or {}
        for key in ("valuation", "growth", "earnings"):
            block = context.get(key) or {}
            data = block.get("data") or {}
            if key == "earnings":
                data = data.get("financial_report") or {}
            values.update(data)
            metadata.update(block.get("field_meta") or {})
        if "net_profit_parent" in values:
            values["net_profit"] = values["net_profit_parent"]
    if tool == "get_daily_history":
        rows = result.get("data") or []
        values = rows[-1] if isinstance(rows, list) and rows and isinstance(rows[-1], dict) else {}
    records = []
    for key, label in fields.items():
        value = values.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float, str)) or value == "":
            continue
        if isinstance(value, (int, float)) and not math.isfinite(value):
            continue
        meta = metadata.get({"main_net_inflow": "main_inflow", "price": "current_price"}.get(key, key)) or {}
        if key == "top10_holder_change" and meta.get("caliber") == "top10_shareholder_concentration":
            label = "十大股东集中度变化（计算）"
        quality = meta.get("quality") or {}
        # issue #64：资金窗口无字段级 meta 时优先用实际共同截至日 as_of_date，
        # 不得把序列最新有效日（latest_date）当作窗口截至日。
        period = str(meta.get("period") or meta.get("observed_at") or result.get("as_of_date") or values.get("date") or result.get("latest_date") or result.get("date") or "")
        if meta.get("previous_period"):
            period = f"{meta['previous_period']} → {period}"
        try:
            record = RetainedFact(
                tool=tool, label=label, value=str(value)[:300],
                source=str(meta.get("source") or result.get("source") or "工具未标注来源")[:160],
                period=period[:80],
                unit=" / ".join(str(part) for part in (meta.get("unit") or result.get("source_unit"), meta.get("currency") or result.get("currency")) if part)[:80],
                qualification="；".join([str(quality.get("status") or "未独立核验"), *[str(code) for code in quality.get("reason_codes", [])], *[str(code) for code in meta.get("input_reasons", [])], *[str(meta[key]) for key in ("caliber", "period_basis", "statement_scope", "adjustment") if meta.get(key)], *([str(meta["normalization_error"])] if meta.get("normalization_error") else []), *(["资格不合格"] if meta.get("rule_eligible") is False else []), *(["历史估算"] if result.get("estimated") else [])])[:300],
            )
            records.append(record.model_dump())
        except ValidationError:
            continue
    return records
