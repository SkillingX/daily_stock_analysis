"""One bounded HTTP MCP call, shared by financial data and Coding Plan search.

Issue #51: the shared boundary records one start and one terminal end, keyed by
call_id, before transport or adapter mapping can drop them. ``dispatched`` is
true only after ``tools/call`` is actually entered. HTTP 429 stays transport
throttling; supplier envelopes in structured or text content keep code/subCode
and redacted msg/subMsg. Logging failures cannot change the business result.
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from contextvars import ContextVar
from functools import partial
from inspect import signature
import json
import logging
from time import monotonic
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import httpx

if TYPE_CHECKING:
    from mcp.types import CallToolResult

logger = logging.getLogger(__name__)

# Business adapters may pre-set this so their reply parsing shares the
# transport-layer call_id; the boundary generates its own otherwise.
current_call_id: ContextVar[str | None] = ContextVar("mcp_call_id", default=None)

_EVIDENCE_MSG_LIMIT = 120
_CHAIN_LIMIT = 8


class _ObservedHttpError(Exception):
    """HTTP status the MCP SDK turned into a generic error, without a body."""

    def __init__(self, status: int) -> None:
        self.response = SimpleNamespace(status_code=status)
        super().__init__(f"http_{status}")


def _credential_secrets(authorization: str) -> tuple[str, ...]:
    """Known caller credentials for this call, including a bare token echo."""
    raw = authorization.strip()
    found: list[str] = []
    if len(raw) >= 8:
        found.append(raw)
    if raw.lower().startswith("bearer "):
        token = raw.split(None, 1)[1].strip()
        if len(token) >= 8:
            found.append(token)
    return tuple(dict.fromkeys(found))


def _mask_secrets(text: str, secrets: tuple[str, ...]) -> str:
    masked = text
    for secret in secrets:
        if secret and secret in masked:
            masked = masked.replace(secret, "[REDACTED]")
    return masked


def _safe_text(value: Any, *, max_length: int = _EVIDENCE_MSG_LIMIT, secrets: tuple[str, ...] = ()) -> str | None:
    """Bound and desensitize free text before it reaches a log line.

    Reuses the decision-signal sanitizer and also masks a credential this call
    already sent, even when the supplier echoes it as bare text.
    """
    from src.utils.sanitize import sanitize_decision_signal_text
    text = sanitize_decision_signal_text(value)
    if not text:
        return None
    return _mask_secrets(text, secrets)[:max_length]


def _evidence(call_id: str, endpoint: str, tool: str, *, secrets: tuple[str, ...] = (), **fields: Any) -> None:
    """One bounded evidence line. A failing handler must not escape."""
    try:
        record: dict[str, Any] = {"event": "mcp_call", "call_id": call_id, "tool": tool}
        try:
            record["endpoint"] = httpx.URL(endpoint).host
        except Exception:  # noqa: BLE001 — endpoint shape must not break evidence
            record["endpoint"] = ""
        for key, value in fields.items():
            if value is None:
                continue
            if isinstance(value, str):
                value = _mask_secrets(value, secrets)
            record[key] = value
        line = json.dumps(record, ensure_ascii=False, default=str)
        logger.info("[MCP] %s", _mask_secrets(line, secrets)[:2000])
    except Exception:  # noqa: BLE001 — logging must not change the business result
        return


def _exception_chain(exc: BaseException) -> str:
    """Cause, context and ExceptionGroup links — not the class MRO."""
    names: list[str] = []
    seen: set[int] = set()

    def walk(item: BaseException | None) -> None:
        if item is None or id(item) in seen or len(names) >= _CHAIN_LIMIT:
            return
        seen.add(id(item))
        names.append(type(item).__name__)
        grouped = getattr(item, "exceptions", None)
        if grouped:
            for child in grouped:
                if isinstance(child, BaseException):
                    walk(child)
        walk(item.__cause__)
        context = item.__context__
        if context is not None and context is not item.__cause__ and not item.__suppress_context__:
            walk(context)

    walk(exc)
    return ">".join(names)[:240]


def _bounded_deadline(deadline: float | None, timeout_seconds: float, run: Any) -> float:
    """Shorten to the caller's stage, request and report bounds. Never extend them."""
    from src.patches.eastmoney_patch import request_deadline
    bounds = [monotonic() + max(timeout_seconds, 0.0)]
    if deadline is not None:
        bounds.append(deadline)
    if (outer := request_deadline.get()) is not None:
        bounds.append(outer)
    if run is not None:
        bounds.append(run.deadline)
    return min(bounds)


def _failure_http_status(http_log: list[dict[str, Any]]) -> int | None:
    """Last real HTTP error. GET 405 during stream negotiation is not one."""
    for entry in reversed(http_log):
        status = entry.get("status")
        if isinstance(status, int) and status >= 400 and status != 405:
            return status
    return None


def json_object_from_text(text: str) -> dict[str, Any] | None:
    """A JSON object, including one embedded after an MCP isError prefix."""
    candidates = [text]
    start = text.find("{")
    if start > 0:
        candidates.append(text[start:])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None



def _bounded_evidence_value(value: Any, *, textual: bool, secrets: tuple[str, ...]) -> Any:
    """Scalar codes stay; a malformed object is its type, never the payload."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return _safe_text(value, secrets=secrets) if textual else value[:40]
    return type(value).__name__


def _business_fields(result: Any, secrets: tuple[str, ...]) -> dict[str, Any]:
    """code/subCode/msg/subMsg from structured content or a text envelope."""
    payloads: list[dict[str, Any]] = []
    structured = getattr(result, "structured_content", None)
    if structured is None:
        structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        payloads.append(structured)
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        parsed = json_object_from_text(text) if text else None
        if parsed is not None:
            payloads.append(parsed)
    found: dict[str, Any] = {}
    aliases = (
        ("code", "business_code"),
        ("subCode", "business_sub_code"),
        ("sub_code", "business_sub_code"),
        ("msg", "business_msg"),
        ("subMsg", "business_sub_msg"),
        ("sub_msg", "business_sub_msg"),
    )
    for payload in payloads:
        for source, dest in aliases:
            if dest in found or source not in payload:
                continue
            value = _bounded_evidence_value(payload.get(source), textual=dest.endswith("msg"), secrets=secrets)
            if value is None:
                continue
            found[dest] = value
    return found


async def apply_mcp_request_deadline(request: httpx.Request, *, deadline: float) -> None:
    from src.core.research_run import current_research
    if run := current_research.get():
        run.check()
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError("MCP deadline exhausted")
    configured = request.extensions.get("timeout") or {}
    request.extensions["timeout"] = {
        part: min(value, remaining) if isinstance(value, (int, float)) else remaining
        for part in ("connect", "read", "write", "pool")
        for value in [configured.get(part)]
    }


async def _record_http_response(
    response: httpx.Response,
    http_log: list[dict[str, Any]],
    secrets: tuple[str, ...] = (),
) -> None:
    # One entry per wire round-trip. GET 405 during stream negotiation next to
    # a successful POST is normal protocol behaviour, never a business failure.
    request_id = _safe_text(
        response.headers.get("request-id") or response.headers.get("x-request-id"),
        max_length=80,
        secrets=secrets,
    )
    entry: dict[str, Any] = {"status": response.status_code}
    if request_id:
        entry["request_id"] = request_id
    http_log.append(entry)


async def call_mcp_tool(
    endpoint: str,
    authorization: str,
    tool: str,
    arguments: dict[str, object],
    *,
    timeout_seconds: float,
    deadline: float | None = None,
) -> CallToolResult:
    """No retries or background session; callers own response validation and errors."""
    from mcp import ClientSession
    from mcp.client import streamable_http
    from src.core.research_run import (
        carries_fatal_group_member,
        current_research,
        is_cancellation_exception,
        research_dimension,
    )

    run = current_research.get()
    call_id = current_call_id.get() or uuid4().hex[:12]
    secrets = _credential_secrets(authorization)
    limit = _bounded_deadline(deadline, timeout_seconds, run)
    remaining = limit - monotonic()
    started = monotonic()
    http_log: list[dict[str, Any]] = []
    dispatched = False
    _evidence(
        call_id, endpoint, tool, secrets=secrets, phase="start", dispatched=False,
        run_id=getattr(run, "run_id", None), dim=research_dimension.get() or None,
        budget_seconds=round(max(remaining, 0.0), 3),
    )
    stack = AsyncExitStack()
    try:
        if run is not None:
            run.check()
        if remaining <= 0:
            raise TimeoutError("MCP deadline exhausted")
        transport = getattr(streamable_http, "streamable_http_client", None) or getattr(streamable_http, "streamablehttp_client")
        if "http_client" in signature(transport).parameters:
            from mcp.shared._httpx_utils import create_mcp_http_client
            client = create_mcp_http_client(headers={"Authorization": authorization})
            client.timeout = remaining
            client.event_hooks["request"].append(partial(apply_mcp_request_deadline, deadline=limit))
            client.event_hooks["response"].append(partial(_record_http_response, http_log=http_log, secrets=secrets))
            await stack.enter_async_context(client)
            streams = await stack.enter_async_context(transport(endpoint, http_client=client, terminate_on_close=False))
        else:
            streams = await stack.enter_async_context(transport(
                endpoint, headers={"Authorization": authorization}, timeout=remaining, terminate_on_close=False,
            ))
        session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
        await session.initialize()
        if run is not None:
            run.check()
        dispatched = True
        result = await session.call_tool(tool, arguments)
    except (asyncio.CancelledError, Exception, BaseExceptionGroup) as exc:
        _evidence(
            call_id, endpoint, tool, secrets=secrets, phase="end", status="error", dispatched=dispatched,
            duration_ms=int((monotonic() - started) * 1000), http=http_log or None,
            error_kind=type(exc).__name__, exception_chain=_exception_chain(exc),
        )
        # A cancellation-shaped exception only means a user cancel or an
        # exhausted report budget when the run's own state says so (issue #58);
        # a real transport timeout keeps its timeout/error semantics instead of
        # being mislabelled, and run.check() raises the preserved
        # ResearchStopped through the existing stop-aware call sites. A fatal
        # member (KeyboardInterrupt/SystemExit) in the same group is never
        # absorbed by the cancellation translation.
        if run is not None and is_cancellation_exception(exc) and not carries_fatal_group_member(exc):
            run.check()
        status = _failure_http_status(http_log)
        if (
            status is not None
            and not isinstance(exc, asyncio.CancelledError)
            and getattr(getattr(exc, "response", None), "status_code", None) is None
        ):
            raise _ObservedHttpError(status) from exc
        raise
    finally:
        # Tearing the session down can raise stream-close noise. It must not
        # mask the business result above, or a genuine request timeout.
        try:
            await stack.aclose()
        except (asyncio.CancelledError, Exception, BaseExceptionGroup) as close_exc:
            _evidence(
                call_id, endpoint, tool, secrets=secrets, phase="session_close_noise",
                duration_ms=int((monotonic() - started) * 1000),
                error_kind=type(close_exc).__name__,
            )
    is_error = bool(getattr(result, "is_error", getattr(result, "isError", False)))
    _evidence(
        call_id, endpoint, tool, secrets=secrets, phase="end", status="ok", dispatched=True,
        duration_ms=int((monotonic() - started) * 1000), http=http_log or None,
        mcp_is_error=is_error, **_business_fields(result, secrets),
    )
    return result
