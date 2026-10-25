"""The deadline and retained results of one in-process research run."""

from __future__ import annotations

import threading
from collections.abc import Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, TimeoutError as FutureTimeout, wait
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Callable, Literal
from uuid import uuid4

current_research: ContextVar[ResearchRun | None] = ContextVar("current_research", default=None)
research_dimension: ContextVar[str] = ContextVar("research_dimension", default="")


class ResearchStopped(TimeoutError):
    """No new work may start; completed evidence can still be rendered."""


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
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _pending: set[Future[Any]] = field(default_factory=set, repr=False)
    _on_clean: Callable[[], None] | None = field(default=None, repr=False)

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
        deadline = min(self.deadline, monotonic() + timeout) if timeout is not None else self.deadline
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

    def publish_daily_history(self, as_of: str, records: list[dict[str, Any]], source: str) -> None:
        """Share one completed session inside this run. Does not touch a returned context."""
        with self._lock:
            if self.finalized or self.stop_reason or self.remaining() <= 0 or not records:
                return
            current = self.daily_history
            if current and current.get("as_of") != as_of:
                return
            if current and len(current.get("records") or []) >= len(records):
                return
            self.daily_history = {"as_of": as_of, "records": [dict(row) for row in records], "source": source}

    def published_daily_history(self) -> dict[str, Any] | None:
        with self._lock:
            if self.finalized or self.stop_reason or self.remaining() <= 0:
                return None
            return self.daily_history

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
        "get_capital_flow": {"main_net_inflow": "当日主力净流入", "inflow_5d": "5交易日主力净流入", "inflow_10d": "10交易日主力净流入"},
        "get_realtime_quote": {"price": "价格", "change_pct": "涨跌幅", "volume": "成交量", "amount": "成交额"},
        "get_stock_info": {"name": "公司名称", "pe_ratio": "PE", "pb_ratio": "PB", "total_mv": "总市值", "circ_mv": "流通市值", "revenue": "营业收入", "net_profit": "归母净利润", "roe": "加权ROE", "gross_margin": "毛利率", "revenue_yoy": "营收同比", "net_profit_yoy": "归母净利同比"},
        "get_daily_history": {"open": "最近交易日开盘", "high": "最近交易日最高", "low": "最近交易日最低", "close": "最近交易日收盘", "volume": "最近交易日成交量"},
        "analyze_trend": {"ma5": "5日均线", "ma10": "10日均线", "ma20": "20日均线", "current_price": "价格", "macd_dif": "MACD DIF", "rsi_6": "6日RSI"},
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
        quality = meta.get("quality") or {}
        try:
            record = RetainedFact(
                tool=tool, label=label, value=str(value)[:300],
                source=str(meta.get("source") or result.get("source") or "工具未标注来源")[:160],
                period=str(meta.get("period") or meta.get("observed_at") or values.get("date") or result.get("latest_date") or result.get("date") or "")[:80],
                unit=" / ".join(str(part) for part in (meta.get("unit") or result.get("source_unit"), meta.get("currency") or result.get("currency")) if part)[:80],
                qualification="；".join([str(quality.get("status") or "未独立核验"), *[str(code) for code in quality.get("reason_codes", [])], *[str(code) for code in meta.get("input_reasons", [])], *[str(meta[key]) for key in ("period_basis", "statement_scope", "adjustment") if meta.get(key)], *(["历史估算"] if result.get("estimated") else [])])[:300],
            )
            records.append(record.model_dump())
        except ValidationError:
            continue
    return records
