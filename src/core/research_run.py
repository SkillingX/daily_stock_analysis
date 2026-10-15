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
    vendor_research: dict[str, dict[str, Any]] = field(default_factory=dict)
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

    def retain_vendor(self, dim: str, research: dict[str, Any]) -> None:
        with self._lock:
            if not self.finalized and self.stop_reason is None and self.remaining() > 0:
                self.vendor_research[dim] = dict(research)

    def retain(self, tool: str, result: Any) -> None:
        """Retain bounded search evidence, never arbitrary raw tool responses."""
        if not isinstance(result, dict) or result.get("error") or result.get("success") is False:
            return
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
                self.evidence.setdefault(dim, []).extend(records)
                self.evidence[dim] = self.evidence[dim][:20]

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
