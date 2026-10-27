"""Per-attempt model ledger: attribution, outbound facts, terminal states.

One attempt = one real admission attempt through :mod:`src.llm.concurrency`.
Parameter-recovery retries and fallback models each count as their own
attempt, so an extra request can never hide inside one logical call. The
ledger lives on the current ``ResearchRun``; calls outside a research run are
not recorded (the report ledger is the scope of this contract).
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import monotonic
from typing import TYPE_CHECKING, Any, Iterator
from uuid import uuid4

if TYPE_CHECKING:
    from src.core.research_run import ResearchRun

# Terminal states. Pre-outbound states keep outbound=False; in-flight states
# keep the outbound fact and any usage the provider did return.
STATUS_OK = "ok"
# Set when the call entered the transport but no response has arrived yet;
# snapshots taken while a request is in flight must not read as success.
STATUS_IN_FLIGHT = "in_flight"
STATUS_QUEUE_TIMEOUT = "queue_timeout"
STATUS_STOPPED_PRE_OUTBOUND = "stopped_pre_outbound"
STATUS_TIMEOUT = "timeout"
STATUS_REJECTED = "rejected"
STATUS_PROVIDER_ERROR = "provider_error"
STATUS_CANCELLED_IN_FLIGHT = "cancelled_in_flight"
STATUS_PARSE_ERROR = "parse_error"
STATUS_TRUNCATED = "truncated"

_PRE_OUTBOUND_STATUSES = frozenset({STATUS_QUEUE_TIMEOUT, STATUS_STOPPED_PRE_OUTBOUND})

# Business entry points that own model attempts inside a research run.
ENTRY_AGENT = "agent"
ENTRY_NARRATE = "narrate"
ENTRY_SCORING = "scoring"
ENTRY_FINAL_CONCLUSION = "final_conclusion"
ENTRY_MANAGER_WRITEUP = "manager_writeup"

# The in-progress attempt is exposed so post-transport layers (runner parse,
# truncation checks) can refine the terminal state of the same attempt.
current_attempt: ContextVar[ModelAttempt | None] = ContextVar("current_model_attempt", default=None)
# Most recently completed attempt in this context. The transport resets
# current_attempt on return, but parse/refine happens one frame above it;
# each new transport call overwrites this, so stale reads cannot outlive the
# immediately following response handling.
last_attempt: ContextVar[ModelAttempt | None] = ContextVar("last_model_attempt", default=None)
# (entry, purpose) set by the business call site; the transport reads it.
attempt_entry: ContextVar[tuple[str, str]] = ContextVar("model_attempt_entry", default=("", ""))


@contextmanager
def attempt_scope(entry: str, purpose: str = "") -> Iterator[None]:
    token = attempt_entry.set((entry, purpose))
    try:
        yield
    finally:
        attempt_entry.reset(token)


@dataclass
class ModelAttempt:
    """One admission attempt with attribution and provider-provided usage only."""

    attempt_id: str = field(default_factory=lambda: uuid4().hex)
    run_id: str = ""
    entry: str = ""
    purpose: str = ""
    dim: str = ""
    target_model: str = ""
    # The model that actually went on the wire; None (unknown) until the
    # router picked a deployment and the request left the process.
    wire_model: str | None = None
    # The request actually reached the transport layer: the SDK's post-call
    # hook fired (any outcome: response, HTTP error, connection failure) or a
    # response object exists. Local validation failures that never handed the
    # request to the transport stay False even though the call was entered.
    outbound: bool = False
    transport_observed: bool = False
    queue_seconds: float | None = None
    exec_seconds: float | None = None
    status: str = STATUS_OK
    # Only the usage keys the provider actually supplied (sanitized); absent
    # keys stay absent and must never be filled with zero.
    usage_provided: dict[str, Any] = field(default_factory=dict)
    # "provided" | "absent" | "unset": pre-normalization presence from the
    # per-call observer; "unset" means no transport response was observed.
    usage_presence: str = "unset"
    # Audit-row id in llm_usage when this attempt's usage was persisted;
    # None (no fabricated id) when nothing was written.
    usage_record_id: int | None = None
    # Runtime handle to the streamed-response wrapper (litellm
    # CustomStreamWrapper) observed at the post-call transport boundary, used
    # by the stream consumer to reach the raw SDK stream. Never serialized
    # and cleared as soon as the consumer takes it over; not a ledger fact.
    stream_transport: Any = field(default=None, repr=False, compare=False)
    finish_reason: str | None = None
    started_at: float = field(default_factory=monotonic)
    _recorded: bool = field(default=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def commit(self, run: ResearchRun | None) -> None:
        """Idempotently attach this attempt to the run ledger, if any."""
        with self._lock:
            if self._recorded:
                return
            self._recorded = True
        if run is not None:
            run.record_attempt(self)

    def fail(self, *, status: str, queue_started: float, run: ResearchRun | None) -> None:
        """Commit a terminal state for an attempt that produced no response.

        The admission release closure may have committed first (status still
        ``ok``); the transport error is the authoritative terminal state, so
        the status is always updated even after an earlier commit.
        """
        with self._lock:
            if not self._recorded and not self.outbound:
                self.queue_seconds = monotonic() - queue_started
        self.status = status
        self.commit(run)

    def refine_status(self, status: str, finish_reason: str | None = None) -> None:
        """Refine the terminal state after the transport already returned ok."""
        with self._lock:
            if self.status == STATUS_OK:
                self.status = status
            if finish_reason is not None:
                self.finish_reason = finish_reason


def classify_call_error(error: BaseException, *, stopped: bool) -> str:
    """Map a transport error onto a terminal state using its real type."""
    if stopped:
        return STATUS_CANCELLED_IN_FLIGHT
    name = type(error).__name__
    if "Timeout" in name:
        return STATUS_TIMEOUT
    if "RateLimit" in name or "Authentication" in name or "PermissionDenied" in name:
        return STATUS_REJECTED
    return STATUS_PROVIDER_ERROR


def normalize_usage_core(attempt: ModelAttempt) -> tuple[Any, Any, Any]:
    """Normalized core fields for one attempt; invalid provider usage → all None."""
    from src.llm.usage import normalize_litellm_usage

    usage = normalize_litellm_usage(attempt.usage_provided, model=attempt.wire_model or attempt.target_model)
    if usage.get("cache_observation") == "invalid_provider_usage":
        return (None, None, None)
    return (
        usage.get("normalized_prompt_tokens"),
        usage.get("normalized_completion_tokens"),
        usage.get("normalized_total_tokens"),
    )


def summarize_attempts(
    attempts: list[ModelAttempt],
    *,
    origin_run_id: str | None = None,
    origin_report_id: str | None = None,
) -> dict[str, Any]:
    """Aggregate the per-run ledger into the report-level usage payload.

    Known/unknown are decided per field: an absent provider key is unknown,
    never zero. Totals sum only known values, so a partially known report
    keeps its confirmable total and flags ``completeness`` instead of
    pretending all usage was observed.
    """
    outbound = [a for a in attempts if a.outbound]
    by_status: dict[str, int] = {}
    for attempt in attempts:
        by_status[attempt.status] = by_status.get(attempt.status, 0) + 1

    # "Confirmed" means the provider actually reported usage (an explicit
    # all-zero payload counts); a numeric zero total therefore never decides
    # completeness on its own.
    # "Confirmed" needs an actually known core field, not just a presence
    # marker with an empty payload.
    # "Confirmed" needs a valid known core field: an empty payload or one
    # holding only invalid values (negative counts) confirms nothing.
    confirmed = [a for a in outbound if a.usage_presence == "provided" and any(
        v is not None for v in normalize_usage_core(a))]
    known_input = known_output = known_total = 0
    unknown_input = unknown_output = unknown_total = 0
    for attempt in outbound:
        prompt, completion, total = normalize_usage_core(attempt)
        known_input += prompt if prompt is not None else 0
        known_output += completion if completion is not None else 0
        known_total += total if total is not None else 0
        unknown_input += prompt is None
        unknown_output += completion is None
        unknown_total += total is None

    if not outbound:
        completeness = "no_outbound"
    elif unknown_input == 0 and unknown_output == 0 and unknown_total == 0:
        completeness = "complete"
    elif confirmed:
        completeness = "partial"
    else:
        completeness = "unknown"

    wait_seconds = sum(a.queue_seconds or 0.0 for a in attempts)
    exec_seconds = sum(a.exec_seconds or 0.0 for a in attempts)
    by_entry: dict[str, dict[str, Any]] = {}
    for attempt in attempts:
        bucket = by_entry.setdefault(
            attempt.entry or "unattributed",
            {"attempts": 0, "outbound": 0, "wait_seconds": 0.0, "exec_seconds": 0.0},
        )
        bucket["attempts"] += 1
        bucket["outbound"] += attempt.outbound
        bucket["wait_seconds"] += attempt.queue_seconds or 0.0
        bucket["exec_seconds"] += attempt.exec_seconds or 0.0

    summary: dict[str, Any] = {
        # attempts stays an integer count; per-attempt detail is exposed
        # separately via attempt_records so the report shape stays consistent.
        "attempts": len(attempts),
        "outbound": len(outbound),
        "transport_observed": sum(1 for a in attempts if a.transport_observed),
        "pre_outbound_terminated": sum(a.status in _PRE_OUTBOUND_STATUSES for a in attempts),
        "by_status": by_status,
        "by_entry": by_entry,
        "known": {"input_tokens": known_input, "output_tokens": known_output, "total_tokens": known_total},
        "unknown_fields": {"input_tokens": unknown_input, "output_tokens": unknown_output, "total_tokens": unknown_total},
        "completeness": completeness,
        "model_wait_seconds": round(wait_seconds, 3),
        "model_exec_seconds": round(exec_seconds, 3),
    }
    if origin_run_id is not None:
        # Cache hits and history reads keep the generating run's attribution.
        summary["origin_run_id"] = origin_run_id
    if origin_report_id is not None:
        summary["origin_report_id"] = origin_report_id
    return summary


def refine_attempt_parse_error() -> None:
    """Refine the just-returned attempt whose structured output did not parse.

    Callers (explore JSON extraction, scoring, manager waves) run after the
    transport returned successfully; the ok-guard keeps truncation and
    cancellation terminal states untouched and usage stays as adopted.
    """
    attempt = last_attempt.get()
    if attempt is not None and attempt.status == STATUS_OK:
        attempt.refine_status(STATUS_PARSE_ERROR)


def attempts_detail(attempts: list[ModelAttempt]) -> list[dict[str, Any]]:
    """Per-attempt sidecar payload: attribution, terminal state, usage link.

    Goes into the report sidecar (``{report_id}_dims.json``) so every attempt
    stays traceable after save/history reads; ``usage_record_id`` links back
    to the existing per-call audit rows instead of duplicating them.
    """
    return [
        {
            "attempt_id": a.attempt_id, "run_id": a.run_id, "entry": a.entry,
            "purpose": a.purpose, "dim": a.dim,
            "target_model": a.target_model, "wire_model": a.wire_model,
            "outbound": a.outbound, "transport_observed": a.transport_observed,
            "queue_seconds": None if a.queue_seconds is None else round(a.queue_seconds, 3),
            "exec_seconds": None if a.exec_seconds is None else round(a.exec_seconds, 3),
            "status": a.status, "finish_reason": a.finish_reason,
            "usage_presence": a.usage_presence, "usage_provided": dict(a.usage_provided),
            "usage_record_id": a.usage_record_id,
        }
        for a in attempts
    ]


def known_total_tokens(summary: dict[str, Any]) -> int:
    """The confirmable total only; unknown usage never becomes a fake zero claim."""
    return int((summary.get("known") or {}).get("total_tokens") or 0)
