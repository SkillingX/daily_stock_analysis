"""One process-wide admission limit for actual model requests and their streams."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextvars import ContextVar
from time import monotonic
from typing import Any

logger = logging.getLogger(__name__)
_init_lock = threading.Lock()
_slots: threading.BoundedSemaphore | None = None
_in_flight = 0
_admitted: ContextVar[bool] = ContextVar("model_request_admitted", default=False)


class ModelQueueTimeout(TimeoutError):
    """The caller's time budget expired while waiting for model capacity."""


def _get_slots() -> threading.BoundedSemaphore:
    global _slots
    with _init_lock:
        if _slots is None:
            from src.config import get_config

            _slots = threading.BoundedSemaphore(get_config().deep_research_llm_concurrency)
        return _slots


def _remaining(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise ModelQueueTimeout("等待模型额度超时，尚未发送模型请求")
    return remaining


def _deadline(kwargs: dict[str, Any]) -> float | None:
    timeout = kwargs.get("timeout")
    return monotonic() + timeout if isinstance(timeout, (int, float)) else None


class _LimitedStream(Iterator[Any]):
    """Own a permit until the underlying stream ends or is explicitly closed."""

    def __init__(self, stream: Any, release: Callable[[], None]) -> None:
        self._stream = iter(stream)
        self._release = release
        self._close_lock = threading.Lock()
        self._closed = False

    def __iter__(self) -> _LimitedStream:
        return self

    def __next__(self) -> Any:
        try:
            return next(self._stream)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            close = getattr(self._stream, "close", None)
            if close is None:
                # LiteLLM sync streams expose the provider stream, but only an
                # async close of their own; close the actual sync transport.
                close = getattr(getattr(self._stream, "completion_stream", None), "close", None)
            if close is not None:
                close()
            self._closed = True
            self._release()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def _start_request(slots: threading.BoundedSemaphore, kwargs: dict[str, Any], queued_at: float) -> Callable[[], None]:
    global _in_flight
    started = monotonic()
    with _init_lock:
        _in_flight += 1
        active = _in_flight
    logger.info("[ModelRequest] start active=%d queue_seconds=%.3f model=%s", active, started - queued_at, kwargs.get("model"))

    def release() -> None:
        global _in_flight
        with _init_lock:
            _in_flight -= 1
            active = _in_flight
        slots.release()
        logger.info("[ModelRequest] end active=%d model_seconds=%.3f model=%s", active, monotonic() - started, kwargs.get("model"))

    return release


def limited_completion(call: Callable[..., Any], **kwargs: Any) -> Any:
    """Dispatch one sync request; waiting consumes the supplied request timeout."""
    if _admitted.get():
        return call(**kwargs)
    started = monotonic()
    deadline = _deadline(kwargs)
    slots = _get_slots()
    if not slots.acquire(timeout=_remaining(deadline)):
        raise ModelQueueTimeout("等待模型额度超时，尚未发送模型请求")
    stream_owned = False
    release = slots.release
    token = _admitted.set(True)
    try:
        remaining = _remaining(deadline)
        if remaining is not None:
            kwargs["timeout"] = remaining
        release = _start_request(slots, kwargs, started)
        response = call(**kwargs)
        if kwargs.get("stream"):
            response = _LimitedStream(response, release)
            stream_owned = True
        return response
    finally:
        _admitted.reset(token)
        if not stream_owned:
            release()


async def limited_acompletion(call: Callable[..., Awaitable[Any]], **kwargs: Any) -> Any:
    """Use the same slots without blocking the event loop or orphaning acquire work."""
    started = monotonic()
    deadline = _deadline(kwargs)
    slots = _get_slots()
    while not slots.acquire(blocking=False):
        remaining = _remaining(deadline)
        await asyncio.sleep(min(0.025, remaining) if remaining is not None else 0.025)
    release = slots.release
    try:
        remaining = _remaining(deadline)
        if remaining is not None:
            kwargs["timeout"] = remaining
        release = _start_request(slots, kwargs, started)
        return await call(**kwargs)
    finally:
        release()
