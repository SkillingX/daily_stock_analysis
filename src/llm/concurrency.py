"""One process-wide admission limit for actual model requests and their streams."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextvars import ContextVar
from time import monotonic
from typing import Any

from src.core.research_run import current_research, research_dimension

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
    run = current_research.get()
    if run is not None:
        run.check()
    if deadline is None:
        return None
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise ModelQueueTimeout("等待模型额度超时，尚未发送模型请求")
    return remaining


def _deadline(kwargs: dict[str, Any]) -> float | None:
    timeout = kwargs.get("timeout")
    deadline = monotonic() + timeout if isinstance(timeout, (int, float)) else None
    run = current_research.get()
    if run is not None:
        run.check()
        deadline = min(deadline, run.deadline) if deadline is not None else run.deadline
    return deadline


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
    run = current_research.get()
    identity = run.run_id if run is not None else ""
    dim = research_dimension.get()
    if run is not None:
        run.emit({"type": "model_start", "queue_seconds": started - queued_at})
    messages = kwargs.get("messages") or []
    input_chars = sum(len(str(message.get("content") or "")) for message in messages if isinstance(message, dict))
    logger.info("[ModelRequest] start run=%s dim=%s active=%d queue_seconds=%.3f input_chars=%d model=%s", identity, dim, active, started - queued_at, input_chars, kwargs.get("model"))

    def release() -> None:
        global _in_flight
        with _init_lock:
            _in_flight -= 1
            active = _in_flight
        slots.release()
        if run is not None:
            run.emit({"type": "model_done", "model_seconds": monotonic() - started, "dim": dim})
        logger.info("[ModelRequest] end run=%s dim=%s active=%d model_seconds=%.3f model=%s", identity, dim, active, monotonic() - started, kwargs.get("model"))

    return release


def limited_completion(call: Callable[..., Any], **kwargs: Any) -> Any:
    """Dispatch one sync request; waiting consumes the supplied request timeout."""
    if _admitted.get():
        return call(**kwargs)
    started = monotonic()
    deadline = _deadline(kwargs)
    slots = _get_slots()
    if run := current_research.get():
        run.emit({"type": "model_queued"})
    while True:
        remaining = _remaining(deadline)
        if slots.acquire(timeout=min(0.05, remaining) if remaining is not None else 0.05):
            break
    stream_owned = False
    release = slots.release
    token = _admitted.set(True)
    try:
        remaining = _remaining(deadline)
        if remaining is not None:
            kwargs["timeout"] = remaining
        if current_research.get() is not None:
            from src.config import get_config
            kwargs["timeout"] = min(float(kwargs["timeout"]), get_config().llm_stream_response_timeout)
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
        if current_research.get() is not None:
            from src.config import get_config
            kwargs["timeout"] = min(float(kwargs["timeout"]), get_config().llm_stream_response_timeout)
        release = _start_request(slots, kwargs, started)
        return await call(**kwargs)
    finally:
        release()
