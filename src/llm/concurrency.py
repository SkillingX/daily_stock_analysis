"""One process-wide admission limit for actual model requests and their streams."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextvars import ContextVar
from time import monotonic
from typing import Any

from src.core.research_run import ResearchStopped, current_research, research_dimension
from src.llm.attempt import (
    STATUS_CANCELLED_IN_FLIGHT,
    STATUS_IN_FLIGHT,
    STATUS_OK,
    STATUS_PROVIDER_ERROR,
    STATUS_QUEUE_TIMEOUT,
    STATUS_STOPPED_PRE_OUTBOUND,
    STATUS_TRUNCATED,
    ModelAttempt,
    attempt_entry,
    classify_call_error,
    current_attempt,
)

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
    """Own a permit until the underlying stream ends or is explicitly closed.

    The attempt stays in_flight while the stream is open: a returned-but-
    unconsumed stream is not a success. Natural exhaustion (StopIteration)
    finalizes ok; a read failure refines the failure terminal state; a usage
    chunk arriving in the stream tail is adopted.
    """

    def __init__(self, stream: Any, release: Callable[[], None], attempt: ModelAttempt | None = None) -> None:
        self._stream = iter(stream)
        self._release = release
        self._attempt = attempt
        self._close_lock = threading.Lock()
        self._closed = False
        # Raw SSE chunks captured before the SDK parses them: LiteLLM
        # synthesizes a full Usage object when stream_options include_usage is
        # set, so provider field presence can only be decided from the raw
        # chunk text the SDK iterates.
        self._raw_usage: dict[str, Any] | None = None
        self._raw_finish: str | None = None
        # The litellm stream wrapper observed at the transport boundary: its
        # ``completion_stream`` is the raw SDK stream regardless of whether a
        # Router fallback wraps the outer object. Taken over from the attempt
        # (which no longer needs it) so nothing else holds a transport ref.
        self._raw_wrapper = getattr(attempt, "stream_transport", None) if attempt is not None else None
        if attempt is not None:
            attempt.stream_transport = None
        if self._raw_wrapper is None and hasattr(getattr(stream, "completion_stream", None), "_iterator"):
            self._raw_wrapper = stream
        # The run that owned this stream at creation. Consumption may happen
        # after the caller's bind() scope exited, so cancellation and usage
        # attribution must use this captured reference, not a fresh lookup.
        from src.core.research_run import current_research

        self._run = current_research.get()
        self._wrap_raw_iterator()

    def __iter__(self) -> _LimitedStream:
        return self

    def __next__(self) -> Any:
        try:
            chunk = next(self._stream)
        except StopIteration:
            self._close_normally()
            raise
        except GeneratorExit:
            self.close()
            raise
        except BaseException as exc:
            # Classify before close(): close() itself finalizes an untouched
            # in_flight attempt as truncated, which would mask the read error.
            if self._attempt is not None and self._attempt.status == STATUS_IN_FLIGHT:
                self._attempt.status = STATUS_CANCELLED_IN_FLIGHT if self._run is not None and self._run.stop_reason else classify_call_error(exc, stopped=False)
            self.close()
            raise
        self._adopt_chunk(chunk)
        return chunk

    def _adopt_chunk(self, chunk: Any) -> None:
        if self._attempt is None or self._attempt.usage_presence != "unset":
            return
        if not getattr(chunk, "usage", None):
            return
        # Presence from the raw SDK iterator observation, never the
        # synthesized outer chunk: only keys the provider actually sent.
        if not self._raw_usage:
            self._attempt.usage_presence = "absent"
            return
        from src.llm.usage import sanitized_provider_usage

        self._attempt.usage_presence = "provided"
        self._attempt.usage_provided = sanitized_provider_usage(self._raw_usage)
        _persist_attempt_audit(self._attempt)

    def _wrap_raw_iterator(self) -> None:
        """Observe provider usage at the raw SDK chunk iterator.

        LiteLLM reads raw ``ChatCompletionChunk`` objects from the openai
        ``Stream`` and then synthesizes a full Usage; wrapping that iterator
        captures ``model_fields_set`` — the keys the provider actually sent —
        and forwards every chunk unchanged. No SSE re-parsing, no raw text.
        """
        inner = getattr(self._raw_wrapper, "completion_stream", None)
        if inner is None or not hasattr(inner, "_iterator"):
            return
        iterator = inner._iterator
        owner = self

        class _ObservingIter:
            def __init__(self, source: Any) -> None:
                self._source = iter(source)

            def __iter__(self) -> _ObservingIter:
                return self

            @staticmethod
            def _provider_keys(usage: Any) -> dict[str, Any] | None:
                """The usage fields the provider actually sent, if any.

                Providers deliver usage either as a pydantic payload (top
                level) or a plain dict (current Kimi puts it on the choice);
                only explicit keys count, never defaults. ``exclude_unset``
                recurses into nested Pydantic details (cache and reasoning
                breakdowns): hand-picking top-level fields would keep those
                details as opaque objects the Mapping-only sanitizer drops.
                """
                if usage is None:
                    return None
                if isinstance(usage, dict):
                    return dict(usage)
                fields = getattr(usage, "model_fields_set", None)
                if fields:
                    dumped = usage.model_dump(exclude_unset=True)
                    return {key: dumped[key] for key in fields if key in dumped}
                return None

            def __next__(self) -> Any:
                chunk = next(self._source)
                top_level = self._provider_keys(getattr(chunk, "usage", None))
                if top_level is not None:
                    owner._raw_usage = top_level
                else:
                    # Some providers deliver the usage tail on the choice
                    # instead of the chunk; the top level keeps priority.
                    choices = getattr(chunk, "choices", None) or []
                    choice_usage = self._provider_keys(getattr(choices[0], "usage", None)) if choices else None
                    if choice_usage is not None:
                        owner._raw_usage = choice_usage
                # Stream finish lives on the chunk, not the response object.
                choices = getattr(chunk, "choices", None)
                finish = getattr(choices[0], "finish_reason", None) if choices else None
                if finish:
                    owner._raw_finish = finish
                return chunk

        inner._iterator = _ObservingIter(iterator)

    def _close_normally(self) -> None:
        if self._attempt is not None and self._attempt.status == STATUS_IN_FLIGHT:
            # Priority: cancellation, then an explicit provider truncation
            # (finish_reason length/content_filter on a chunk), else ok.
            if self._run is not None and self._run.stop_reason:
                self._attempt.status = STATUS_CANCELLED_IN_FLIGHT
            elif self._raw_finish in {"length", "content_filter"}:
                self._attempt.status = STATUS_TRUNCATED
                self._attempt.finish_reason = self._raw_finish
            else:
                self._attempt.status = STATUS_OK
                if self._raw_finish:
                    self._attempt.finish_reason = self._raw_finish
            if self._attempt.usage_presence == "unset" and self._attempt.status == STATUS_OK:
                # The stream was fully consumed with include_usage requested:
                # a missing provider usage is an observed absence, not an
                # unobserved transport.
                self._attempt.usage_presence = "absent"
        self.close()

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            # An explicit close without a natural finish means the output was
            # not fully consumed: not ok. Cancellation of the owning run wins
            # (captured reference, valid after the caller's scope exited);
            # truncation terminal states set earlier keep their priority.
            if self._attempt is not None and self._attempt.status == STATUS_IN_FLIGHT:
                self._attempt.status = STATUS_CANCELLED_IN_FLIGHT if self._run is not None and self._run.stop_reason else STATUS_TRUNCATED
            try:
                close = getattr(self._stream, "close", None)
                if close is None:
                    # LiteLLM sync streams expose the provider stream, but only
                    # an async close of their own; close the actual sync transport.
                    close = getattr(getattr(self._raw_wrapper, "completion_stream", None), "close", None)
                if close is not None:
                    close()
            finally:
                # The permit must be released even when the underlying close
                # raises; otherwise one broken stream blocks the process slot.
                self._closed = True
                self._release()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def _start_request(slots: threading.BoundedSemaphore, kwargs: dict[str, Any], queued_at: float, attempt: ModelAttempt) -> Callable[[], None]:
    global _in_flight
    started = monotonic()
    with _init_lock:
        _in_flight += 1
        active = _in_flight
    run = current_research.get()
    identity = run.run_id if run is not None else ""
    dim = research_dimension.get()
    attempt.queue_seconds = started - queued_at
    # Commit when the call enters the transport: a cancelled run's
    # partial_ready snapshot already shows this attempt as in_flight. The
    # state is in_flight — admission started a send, which is neither success
    # nor proof of outbound; outbound is only set by transport evidence (the
    # SDK post-call hook or a returned response object).
    attempt.status = STATUS_IN_FLIGHT
    attempt.commit(run)
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
        attempt.exec_seconds = monotonic() - started
        attempt.commit(run)
        if run is not None:
            run.emit({"type": "model_done", "model_seconds": monotonic() - started, "dim": dim})
        logger.info("[ModelRequest] end run=%s dim=%s active=%d model_seconds=%.3f model=%s", identity, dim, active, monotonic() - started, kwargs.get("model"))

    return release


def _new_attempt(kwargs: dict[str, Any]) -> ModelAttempt:
    run = current_research.get()
    entry, purpose = attempt_entry.get()
    return ModelAttempt(
        run_id=run.run_id if run is not None else "",
        entry=entry,
        purpose=purpose,
        dim=research_dimension.get(),
        target_model=str(kwargs.get("model") or ""),
    )


def _attach_observer(kwargs: dict[str, Any], attempt: ModelAttempt) -> None:
    """Inject the per-call observer without dropping a caller's logger_fn."""
    observer = _make_usage_observer(attempt)
    original_fn = kwargs.get("logger_fn")
    if original_fn is None:
        kwargs["logger_fn"] = observer
        return

    def chained(model_call_details: dict[str, Any]) -> None:
        observer(model_call_details)
        original_fn(model_call_details)

    kwargs["logger_fn"] = chained


def _make_usage_observer(attempt: ModelAttempt) -> Callable[[dict[str, Any]], None]:
    """Per-call logger_fn binding the attempt itself.

    LiteLLM invokes ``logger_fn(model_call_details)`` at the transport
    boundary: the ``pre_api_call`` event carries the exact payload handed to
    the transport (its ``model`` is the router-picked deployment actually
    going on the wire), and the post-call event carries the pre-normalization
    provider payload before the absent/explicit-zero distinction is destroyed.
    Binding the mutable attempt (rather than a ContextVar setter) keeps the
    result visible across the copy_context the router runs the chain under.
    Any caller-provided logger_fn is chained.
    """
    from src.llm.usage import sanitized_provider_usage

    def observer(model_call_details: dict[str, Any]) -> None:
        # The post-call hook firing at all means LiteLLM handed the request to
        # the transport layer (response, HTTP error, or connection failure all
        # land here); local preflight failures never reach it.
        attempt.outbound = True
        attempt.transport_observed = True
        if model_call_details.get("log_event_type") == "pre_api_call":
            # The actual outbound model is known before any response exists,
            # so timeouts, 429s and connection failures keep it too. Only
            # this scalar is read from the payload — no body text, headers
            # or other fields are stored.
            args = model_call_details.get("additional_args")
            body = args.get("complete_input_dict") if isinstance(args, dict) else None
            model = body.get("model") if isinstance(body, dict) else None
            if isinstance(model, str) and model:
                attempt.wire_model = model
            return
        original = model_call_details.get("original_response")
        if isinstance(original, str):
            try:
                original = json.loads(original)
            except (TypeError, ValueError):
                return
        if not isinstance(original, dict):
            # A streamed response arrives as the litellm stream wrapper; the
            # consumer needs its raw SDK stream (usage observation, forced
            # close), which is unreachable through the Router's own outer
            # wrapper. The wrapper reference is handed over and cleared by
            # the consumer; nothing is serialized.
            raw_stream = getattr(original, "completion_stream", None)
            if raw_stream is not None and hasattr(raw_stream, "_iterator"):
                attempt.stream_transport = original
            return
        usage = original.get("usage")
        if isinstance(usage, dict):
            attempt.usage_presence = "provided"
            attempt.usage_provided = sanitized_provider_usage(usage)
        else:
            attempt.usage_presence = "absent"

    return observer


def _extract_error_response_usage(attempt: ModelAttempt, error: BaseException) -> None:
    """Keep usage the provider returned on an error HTTP response.

    Observer coverage ends where litellm raises instead of returning; an
    APIStatusError still carries the raw httpx response, whose body may
    report real usage (e.g. a 429 with usage). Only the usage sub-object is
    read; nothing else from the body is stored.
    """
    if attempt.usage_presence != "unset":
        return
    from src.llm.usage import sanitized_provider_usage

    # Documented exception attributes first: APIStatusError.body already holds
    # the parsed error response (usage included on 4xx); the raw httpx
    # response is a fallback when the body attribute was not populated.
    body: Any = getattr(error, "body", None)
    if not isinstance(body, dict):
        response = getattr(error, "response", None)
        try:
            body = response.json() if response is not None else None
        except Exception:  # noqa: BLE001 - consumed/non-JSON bodies are absent
            body = None
    if isinstance(body, dict):
        usage = body.get("usage")
        if isinstance(usage, dict):
            attempt.usage_presence = "provided"
            attempt.usage_provided = sanitized_provider_usage(usage)
        elif "usage" in body:
            attempt.usage_presence = "absent"
    # An error response (any HTTP status) is transport evidence regardless of
    # whether its body parsed.
    attempt.outbound = True
    attempt.transport_observed = True


def _fail_attempt(attempt: ModelAttempt, error: BaseException, queued_at: float) -> None:
    """Record a terminal state for an attempt that never produced a response."""
    run = current_research.get()
    if attempt.outbound:
        _extract_error_response_usage(attempt, error)
    if isinstance(error, ModelQueueTimeout):
        status = STATUS_QUEUE_TIMEOUT
    elif isinstance(error, ResearchStopped):
        status = STATUS_STOPPED_PRE_OUTBOUND
    elif attempt.outbound:
        status = STATUS_CANCELLED_IN_FLIGHT if run is not None and run.stop_reason else classify_call_error(error, stopped=False)
    elif attempt.status == STATUS_IN_FLIGHT:
        # Admitted but no transport evidence ever arrived (local preflight
        # rejection): the call failed without leaving the process.
        status = STATUS_PROVIDER_ERROR
    else:
        status = STATUS_STOPPED_PRE_OUTBOUND
    attempt.fail(status=status, queue_started=queued_at, run=run)
    # Expose the failed attempt too: post-transport layers (failure usage
    # persistence) need the attempt that just ended. Parse refinement is
    # guarded on status == ok, so this cannot mislabel a transport failure.
    if attempt.outbound:
        from src.llm.attempt import last_attempt

        last_attempt.set(attempt)


def _adopt_response(attempt: ModelAttempt, response: Any) -> None:
    """Keep identity and usage from what the transport actually returned.

    The per-call observer (pre-normalization) is authoritative when it fired:
    an explicit all-zero usage stays confirmed, an absent usage stays unknown.
    Without observer evidence the returned response object itself proves the
    transport happened; usage then falls back to the non-zero-signal rule
    because LiteLLM synthesizes all-zero Usage for absent provider payloads.
    """
    from src.llm.attempt import last_attempt
    from src.llm.usage import extract_usage_payload, has_provider_usage_payload

    # A returned response object is itself transport evidence.
    attempt.outbound = True
    attempt.transport_observed = True
    if attempt.usage_presence == "unset":
        payload = extract_usage_payload(response)
        if hasattr(payload, "model_dump"):
            payload = payload.model_dump()
        if isinstance(payload, dict) and has_provider_usage_payload(payload):
            attempt.usage_provided = payload
            attempt.usage_presence = "provided"
        else:
            attempt.usage_provided = {}
    if attempt.wire_model is None:
        # No valid non-empty outbound model was observed (hook never fired,
        # or the pre-call payload carried no usable model): the returned
        # object's model is the best-effort fallback and may be a response
        # alias. An observed request model is never overwritten by it.
        wire_model = str(getattr(response, "model", "") or "")
        attempt.wire_model = wire_model or None
    choice = getattr(response, "choices", None)
    if choice:
        finish = getattr(choice[0], "finish_reason", None)
        attempt.finish_reason = finish
        if finish in {"length", "content_filter"}:
            attempt.status = STATUS_TRUNCATED
    last_attempt.set(attempt)


def _persist_attempt_audit(attempt: ModelAttempt) -> None:
    """Persist this attempt's per-call audit row from inside its lifetime.

    Exactly one row per attempt with real usage (including failures that
    carried provider usage and each parameter-recovery/fallback retry); the
    normalized view comes from the existing normalizer so provider-absent
    fields stay NULL. Rows are attributed to the business entry; attempts
    without usage or entry write nothing and never fabricate a row id.
    """
    # The attempt's entry snapshot, not the ContextVar: streams may be
    # consumed after the business scope that created them has exited.
    entry = attempt.entry
    if not entry or attempt.usage_record_id is not None:
        return
    if attempt.usage_presence != "provided" or not attempt.usage_provided:
        return
    from src.llm.usage import normalize_litellm_usage
    from src.storage import persist_llm_usage

    # Audit rows keep the requested model identity (consistent with the
    # historical rows); the wire-model distinction lives in the attempt ledger.
    model_for_usage = attempt.target_model or attempt.wire_model
    if not model_for_usage or model_for_usage == "error":
        return
    usage = normalize_litellm_usage(attempt.usage_provided, model=model_for_usage)
    row_id = persist_llm_usage(usage, model_for_usage, call_type=entry)
    if row_id is not None:
        attempt.usage_record_id = row_id


def adopt_raw_stream_usage(
    attempt: ModelAttempt | None,
    raw_usage: dict[str, Any] | None,
    *,
    observed: bool = False,
) -> None:
    """Adopt provider-actual usage observed at a raw stream iterator.

    Async stream consumption cannot reuse the sync wrapper's chunk hook, so
    the consumer calls this once its raw observation is complete — always
    before the response-level adoption, so the per-attempt ledger sees only
    the keys the provider actually sent. ``observed`` marks that a raw
    iterator was really attached and consumed: without it the call never
    reached the stream (e.g. an HTTP error before the first chunk) and the
    absence must stay unset so error-body usage can still be adopted.
    """
    if not observed or attempt is None or attempt.usage_presence != "unset":
        return
    if not raw_usage:
        attempt.usage_presence = "absent"
        return
    from src.llm.usage import sanitized_provider_usage

    attempt.usage_presence = "provided"
    attempt.usage_provided = sanitized_provider_usage(raw_usage)
    _persist_attempt_audit(attempt)


def limited_completion(call: Callable[..., Any], **kwargs: Any) -> Any:
    """Dispatch one sync request; waiting consumes the supplied request timeout."""
    if _admitted.get():
        return call(**kwargs)
    started = monotonic()
    attempt = _new_attempt(kwargs)
    token = current_attempt.set(attempt)
    _attach_observer(kwargs, attempt)
    try:
        response = _limited_completion(call, kwargs, started, attempt)
        if not kwargs.get("stream"):
            # A non-stream response is fully consumed: in_flight ends here.
            if attempt.status == STATUS_IN_FLIGHT:
                attempt.status = STATUS_OK
            run = current_research.get()
            if run is not None and run.stop_reason:
                # The request did leave the process and returned; keep the
                # outbound fact and usage instead of a late success as ok.
                attempt.status = STATUS_CANCELLED_IN_FLIGHT
            _adopt_response(attempt, response)
            _persist_attempt_audit(attempt)
        else:
            # Streams stay in_flight until consumed; identity-only adoption.
            _adopt_response(attempt, response)
        return response
    except BaseException as exc:
        _fail_attempt(attempt, exc, started)
        _persist_attempt_audit(attempt)
        raise
    finally:
        current_attempt.reset(token)


def _limited_completion(call: Callable[..., Any], kwargs: dict[str, Any], started: float, attempt: ModelAttempt) -> Any:
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
        release = _start_request(slots, kwargs, started, attempt)
        response = call(**kwargs)
        if kwargs.get("stream"):
            response = _LimitedStream(response, release, attempt)
            stream_owned = True
        return response
    finally:
        _admitted.reset(token)
        if not stream_owned:
            release()


async def limited_acompletion(call: Callable[..., Awaitable[Any]], **kwargs: Any) -> Any:
    """Use the same slots without blocking the event loop or orphaning acquire work."""
    if _admitted.get():
        return await call(**kwargs)
    started = monotonic()
    attempt = _new_attempt(kwargs)
    token = current_attempt.set(attempt)
    _attach_observer(kwargs, attempt)
    try:
        deadline = _deadline(kwargs)
        slots = _get_slots()
        while not slots.acquire(blocking=False):
            try:
                remaining = _remaining(deadline)
            except BaseException as exc:
                _fail_attempt(attempt, exc, started)
                raise
            await asyncio.sleep(min(0.025, remaining) if remaining is not None else 0.025)
        release = slots.release
        try:
            remaining = _remaining(deadline)
            if remaining is not None:
                kwargs["timeout"] = remaining
            if current_research.get() is not None:
                from src.config import get_config
                kwargs["timeout"] = min(float(kwargs["timeout"]), get_config().llm_stream_response_timeout)
            release = _start_request(slots, kwargs, started, attempt)
            response = await call(**kwargs)
            if attempt.status == STATUS_IN_FLIGHT:
                attempt.status = STATUS_OK
            run = current_research.get()
            if run is not None and run.stop_reason:
                attempt.status = STATUS_CANCELLED_IN_FLIGHT
            _adopt_response(attempt, response)
            _persist_attempt_audit(attempt)
            return response
        finally:
            release()
    except BaseException as exc:
        _fail_attempt(attempt, exc, started)
        _persist_attempt_audit(attempt)
        raise
    finally:
        current_attempt.reset(token)
