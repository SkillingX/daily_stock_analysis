"""Per-attempt model ledger against a loopback provider: real admission, transport, parse.

Covers issue #55 attribution and terminal-state acceptance: outbound vs
pre-outbound waiting, cancelled in-flight usage, provider rejection, parameter
recovery as separate attempts, unknown vs confirmed-zero usage, and no
cross-run accounting.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest

from src.llm.attempt import (
    ENTRY_AGENT,
    ENTRY_NARRATE,
    STATUS_CANCELLED_IN_FLIGHT,
    STATUS_OK,
    STATUS_PROVIDER_ERROR,
    STATUS_QUEUE_TIMEOUT,
    STATUS_REJECTED,
    STATUS_STOPPED_PRE_OUTBOUND,
    STATUS_TIMEOUT,
    STATUS_TRUNCATED,
    attempt_scope,
    known_total_tokens,
    summarize_attempts,
)


@pytest.fixture
def usage_provider() -> Iterator[tuple[str, dict[str, Any]]]:
    state: dict[str, Any] = {"mode": "default", "count": 0, "active": 0, "peak": 0, "hold": 0.0, "reject_calls": 0, "reject_models": set()}
    lock = threading.Lock()

    class Provider(BaseHTTPRequestHandler):
        def _respond(self, message: dict[str, Any], usage: dict[str, int] | None, status: int = 200, model: str = "fixture", stream: bool = False) -> None:
            if stream and status == 200:
                # The same business payload rendered as a legal stream.
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()

                def sse(payload: dict[str, Any]) -> None:
                    self.wfile.write(("data: " + json.dumps(payload) + "\n\n").encode())
                    self.wfile.flush()

                if message.get("tool_calls"):
                    tool_call = message["tool_calls"][0]["function"]
                    sse({"id": "fixture", "object": "chat.completion.chunk", "created": 0, "model": model,
                         "choices": [{"index": 0, "delta": {
                             "role": "assistant",
                             "tool_calls": [{"index": 0, "id": message["tool_calls"][0]["id"], "type": "function",
                                             "function": {"name": tool_call["name"], "arguments": tool_call["arguments"]}}],
                         }, "finish_reason": None}]})
                else:
                    sse({"id": "fixture", "object": "chat.completion.chunk", "created": 0, "model": model,
                         "choices": [{"index": 0, "delta": {"role": "assistant", "content": message.get("content")}, "finish_reason": None}]})
                sse({"id": "fixture", "object": "chat.completion.chunk", "created": 0, "model": model,
                     "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
                if usage is not None:
                    sse({"id": "fixture", "object": "chat.completion.chunk", "created": 0, "model": model,
                         "choices": [], "usage": usage})
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                return
            payload: dict[str, Any] = {
                "id": "fixture", "object": "chat.completion", "created": 0, "model": model,
                "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
            }
            if usage is not None:
                payload["usage"] = usage
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                state["count"] += 1
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
                mode = state["mode"]
                hold = state["hold"]
                reject_calls = state["reject_calls"]
                reject_models = set(state["reject_models"])
            try:
                if hold:
                    time.sleep(hold)
                if mode in ("reject", "reject_with_usage") or str(request.get("model")) in reject_models:
                    reject_usage = {"prompt_tokens": 7, "completion_tokens": 0, "total_tokens": 7} if mode == "reject_with_usage" else None
                    self._respond({"role": "assistant", "content": ""}, reject_usage, status=429)
                    return
                if mode == "reject_once" and state["count"] <= reject_calls:
                    self._respond({"role": "assistant", "content": ""}, None, status=429)
                    return
                if mode == "temperature_error" and state["count"] == 1:
                    body = b'{"error":{"message":"Invalid parameter: only supported temperature value is `0.5`.","type":"invalid_request_error"}}'
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                usage: dict[str, Any] | None = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
                if mode == "no_usage":
                    usage = None
                elif mode == "zero_usage":
                    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
                elif mode == "partial_usage":
                    usage = {"prompt_tokens": 10}
                elif mode == "detail_usage":
                    usage = {
                        "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                        "prompt_tokens_details": {"cached_tokens": 4},
                        "completion_tokens_details": {"reasoning_tokens": 3},
                    }
                if mode == "stream_ok":
                    self._stream_response(abort=False, tail_usage=False)
                    return
                if mode == "stream_tail_usage":
                    self._stream_response(abort=False, tail_usage=True)
                    return
                if mode == "stream_partial_usage":
                    self._stream_response(abort=False, tail_usage="partial")
                    return
                if mode == "stream_no_usage":
                    self._stream_response(abort=False, tail_usage=False)
                    return
                if mode == "stream_fail":
                    self._stream_response(hold_after_first=True)
                    return
                if mode == "alias_model":
                    # The provider echoes a different model name in the
                    # response body: a response alias, never the wire request.
                    self._respond({"role": "assistant", "content": '{"status":"ok"}'}, usage, model="fixture-alias-echo", stream=bool(request.get("stream")))
                    return
                self._respond({"role": "assistant", "content": '{"status":"ok"}'}, usage, stream=bool(request.get("stream")))
            finally:
                with lock:
                    state["active"] -= 1

        def _stream_response(self, *, abort: bool = False, hold_after_first: bool = False, tail_usage: bool = False) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()

            def chunk(delta: dict[str, Any], finish: Any = None, usage: dict[str, int] | None = None) -> bytes:
                payload = {"id": "fixture", "object": "chat.completion.chunk", "created": 0,
                           "model": "fixture", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                if usage is not None:
                    payload["usage"] = usage
                return ("data: " + json.dumps(payload) + "\n\n").encode()
            self.wfile.write(chunk({"role": "assistant", "content": "ok"}))
            self.wfile.flush()
            if hold_after_first:
                # Mid-stream stall longer than the caller's read timeout: a
                # genuine read failure the transport must surface.
                time.sleep(20)
                return
            self.wfile.write(chunk({"content": "!"}, finish="stop"))
            self.wfile.flush()
            if tail_usage:
                tail = {"prompt_tokens": 10} if tail_usage == "partial" else {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9}
                self.wfile.write(chunk({}, usage=tail))
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

        def log_message(self, *_args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", state
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def ledger_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, usage_provider: tuple[str, dict[str, Any]]) -> Iterator[tuple[Any, dict[str, Any]]]:
    base, state = usage_provider
    from src import llm
    from src.config import Config
    from src.llm import concurrency
    from src.storage import DatabaseManager

    config = Config()
    config.llm_model_list = [
        {"model_name": "openai/fixture", "litellm_params": {"model": "openai/fixture", "api_base": base, "api_key": "fixture-key"}}
    ]
    config.litellm_model = "openai/fixture"
    config.agent_litellm_model = "openai/fixture"
    config.llm_temperature = 0.3
    config.deep_research_llm_concurrency = 1
    config.litellm_fallback_models = []
    monkeypatch.setattr(Config, "_instance", config)
    # DatabaseManager.__new__ short-circuits when _instance is set, so the
    # per-test database must start from a cleared instance (same pattern as
    # the lifecycle fixtures).
    monkeypatch.setattr(DatabaseManager, "_instance", None)
    database = DatabaseManager(f"sqlite:///{tmp_path / 'usage.sqlite'}")
    monkeypatch.setattr(DatabaseManager, "_instance", database)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    # Fresh admission slots per test; the production default limit stays 1.
    monkeypatch.setattr(concurrency, "_slots", None)
    monkeypatch.setattr(concurrency, "_in_flight", 0)
    from src.agent.llm_adapter import LLMToolAdapter

    adapter = LLMToolAdapter(config)
    try:
        yield adapter, state
    finally:
        llm.concurrency._slots = None  # type: ignore[attr-defined]


def _messages() -> list[dict[str, Any]]:
    return [{"role": "user", "content": "hello"}]


def _audit_rows() -> list[dict[str, Any]]:
    from src.storage import get_db

    with get_db().get_session() as session:  # type: ignore[attr-defined]
        from src.storage import LLMUsage

        rows = session.query(LLMUsage).all()
        return [{"call_type": r.call_type, "model": r.model, "total_tokens": r.total_tokens} for r in rows]


def test_success_attempt_attribution_and_single_audit_row(ledger_env: tuple[Any, dict[str, Any]]) -> None:
    """One outbound: ledger entry with run/dim/timing/usage and exactly one audit row."""
    adapter, state = ledger_env
    from src.core.research_run import ResearchRun, research_dimension
    from src.llm import concurrency

    concurrency._slots = None
    run = ResearchRun(30, run_id="a" * 32)
    with run.bind():
        with attempt_scope(ENTRY_AGENT, "step1"):
            token = research_dimension.set("intel")
            try:
                response = adapter.call_completion(_messages(), timeout=10)
            finally:
                research_dimension.reset(token)
    assert response.provider != "error", response.content
    assert state["count"] == 1
    [attempt] = run.attempts
    assert attempt.run_id == run.run_id and attempt.entry == ENTRY_AGENT
    assert attempt.dim == "intel" and attempt.outbound is True
    # wire_model is the model observed on the outbound payload ("fixture"),
    # not the configured target ("openai/fixture") nor a response echo.
    assert attempt.status == STATUS_OK and attempt.wire_model == "fixture"
    assert attempt.queue_seconds is not None and attempt.exec_seconds is not None
    summary = summarize_attempts(run.attempts, origin_run_id=run.run_id)
    assert summary["outbound"] == 1 and summary["completeness"] == "complete"
    assert summary["known"] == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    assert known_total_tokens(summary) == 15
    # The runner used to be the only persister; now the adapter is, so one
    # outbound still yields exactly one audit row (same model identity value).
    rows = _audit_rows()
    assert rows == [{"call_type": "agent", "model": "openai/fixture", "total_tokens": 15}]


def test_absent_usage_stays_unknown_vs_confirmed_zero(ledger_env: tuple[Any, dict[str, Any]]) -> None:
    """Absent usage and an explicit all-zero payload are two distinct facts.

    The pre-normalization capture (log_post_api_call) keeps the provider's
    original payload before LiteLLM synthesizes an all-zero Usage, so an
    explicit zero is confirmed zero consumption while an absent usage stays
    unknown — never a fake zero claim in either direction.
    """
    adapter, state = ledger_env
    from src.core.research_run import ResearchRun

    state["mode"] = "no_usage"
    run = ResearchRun(30)
    with run.bind():
        with attempt_scope(ENTRY_NARRATE, "data"):
            response = adapter.call_completion(_messages(), timeout=10)
    assert response.provider != "error"
    [absent] = run.attempts
    assert absent.usage_provided == {}
    summary = summarize_attempts(run.attempts)
    assert summary["completeness"] == "unknown"
    assert summary["known"]["total_tokens"] == 0
    assert known_total_tokens(summary) == 0  # unknown is never a confirmed zero
    assert summary["unknown_fields"]["total_tokens"] == 1

    state["mode"] = "zero_usage"
    run_zero = ResearchRun(30)
    with run_zero.bind():
        with attempt_scope(ENTRY_NARRATE, "phase"):
            response = adapter.call_completion(_messages(), timeout=10)
    assert response.provider != "error"
    [confirmed] = run_zero.attempts
    assert confirmed.usage_provided.get("total_tokens") == 0
    summary_zero = summarize_attempts(run_zero.attempts)
    assert summary_zero["completeness"] == "complete"
    assert summary_zero["known"] == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    assert summary_zero["unknown_fields"] == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


def test_confirmed_zero_plus_absent_usage_is_partial(ledger_env: tuple[Any, dict[str, Any]]) -> None:
    """Mixed known/unknown across attempts must read as partial, not unknown."""
    adapter, state = ledger_env
    from src.core.research_run import ResearchRun

    run = ResearchRun(30)
    state["mode"] = "zero_usage"
    with run.bind():
        with attempt_scope(ENTRY_NARRATE, "phase"):
            adapter.call_completion(_messages(), timeout=10)
    state["mode"] = "no_usage"
    with run.bind():
        with attempt_scope(ENTRY_NARRATE, "data"):
            adapter.call_completion(_messages(), timeout=10)
    summary = summarize_attempts(run.attempts)
    assert summary["outbound"] == 2
    assert summary["completeness"] == "partial"
    assert summary["known"]["total_tokens"] == 0  # confirmed zeros do not vanish
    assert summary["unknown_fields"]["total_tokens"] == 1


def test_reasoning_and_cache_details_are_not_double_counted(ledger_env: tuple[Any, dict[str, Any]]) -> None:
    adapter, state = ledger_env
    state["mode"] = "detail_usage"
    from src.core.research_run import ResearchRun

    run = ResearchRun(30)
    with run.bind():
        with attempt_scope(ENTRY_AGENT, "step1"):
            adapter.call_completion(_messages(), timeout=10)
    summary = summarize_attempts(run.attempts)
    # Reasoning/cache are details inside input/output; totals must not re-add them.
    assert summary["known"] == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    provided = run.attempts[0].usage_provided
    assert provided["prompt_tokens_details"]["cached_tokens"] == 4
    assert provided["completion_tokens_details"]["reasoning_tokens"] == 3


def test_sync_stream_detail_usage_keeps_nested_actuals(ledger_env: tuple[Any, dict[str, Any]]) -> None:
    """The shared sync stream keeps the nested usage details providers send.

    GLM/Kimi-compatible providers deliver prompt_tokens_details and
    completion_tokens_details as nested objects on the raw SDK usage object;
    the sync observation must recursively keep exactly the fields the
    provider actually sent, matching the async agent path above. Nested
    details stay inside input/output — totals are never re-added.
    """
    adapter, state = ledger_env
    state["mode"] = "detail_usage"
    from src.config import Config
    from src.core.research_run import ResearchRun
    from src.llm import concurrency
    import litellm

    config = Config._instance
    assert config is not None
    base_url = config.llm_model_list[0]["litellm_params"]["api_base"]
    run = ResearchRun(30)
    with run.bind():
        with attempt_scope(ENTRY_AGENT, "sync-details"):
            stream = concurrency.limited_completion(
                litellm.completion, model="openai/fixture", messages=_messages(),
                timeout=15, stream=True, api_key="fixture-key", api_base=base_url,
                stream_options={"include_usage": True},
            )
            consumed = list(stream)
    assert consumed
    [attempt] = run.attempts
    assert attempt.status == STATUS_OK
    assert attempt.usage_presence == "provided"
    provided = attempt.usage_provided
    assert provided["prompt_tokens_details"]["cached_tokens"] == 4
    assert provided["completion_tokens_details"]["reasoning_tokens"] == 3
    assert provided["total_tokens"] == 15
    summary = summarize_attempts(run.attempts)
    assert summary["known"] == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}


def test_queue_timeout_records_pre_outbound_without_request(ledger_env: tuple[Any, dict[str, Any]]) -> None:
    adapter, state = ledger_env
    state["hold"] = 2.0
    import concurrent.futures

    from src.core.research_run import ResearchRun

    run = ResearchRun(30)
    with run.bind():
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            holder = pool.submit(
                lambda: adapter.call_completion(_messages(), timeout=15)
            )
            deadline = time.monotonic() + 5
            while state["count"] == 0:
                assert time.monotonic() < deadline
                time.sleep(0.01)
            try:
                with attempt_scope(ENTRY_AGENT, "waiter"):
                    waiter_response = adapter.call_completion(_messages(), timeout=0.4)
            except Exception:  # noqa: BLE001 - adapter envelopes transport errors
                waiter_response = None
            holder.result(timeout=30)
    assert waiter_response is None or waiter_response.provider == "error"
    waiter = [a for a in run.attempts if a.purpose == "waiter"]
    assert len(waiter) == 1
    assert waiter[0].status == STATUS_QUEUE_TIMEOUT and waiter[0].outbound is False
    # Queued without outbound: the wire model stays unknown, never filled
    # from the configured target model.
    assert waiter[0].wire_model is None
    assert waiter[0].queue_seconds is not None
    assert state["count"] == 1  # the waiter never reached the provider


def test_cancelled_before_outbound_has_zero_outbound(ledger_env: tuple[Any, dict[str, Any]]) -> None:
    adapter, state = ledger_env
    state["hold"] = 1.5
    import concurrent.futures

    from src.core.research_run import ResearchRun

    run = ResearchRun(30)
    with run.bind():
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(
                lambda: adapter.call_completion(_messages(), timeout=15)
            )
            deadline = time.monotonic() + 5
            while state["count"] == 0:
                assert time.monotonic() < deadline
                time.sleep(0.01)
            run.stop("cancelled")
            response = adapter.call_completion(_messages(), timeout=10)
    # Caller sees the adapter's error envelope; the ledger keeps the facts.
    assert response.provider == "error"
    stopped = [a for a in run.attempts if not a.outbound]
    assert stopped and all(a.status == STATUS_STOPPED_PRE_OUTBOUND for a in stopped)
    assert all(a.wire_model is None for a in stopped)  # never went out: unknown
    assert state["count"] == 1
    future.result(timeout=30)


def test_cancelled_in_flight_keeps_outbound_and_usage(ledger_env: tuple[Any, dict[str, Any]]) -> None:
    adapter, state = ledger_env
    state["hold"] = 1.2
    from contextvars import copy_context

    from src.core.research_run import ResearchRun

    run = ResearchRun(30)
    with run.bind():

        def in_flight_call() -> Any:
            with attempt_scope(ENTRY_AGENT, "step1"):
                return adapter.call_completion(_messages(), timeout=15)

        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(copy_context().run, in_flight_call)
            deadline = time.monotonic() + 5
            while state["count"] == 0:
                assert time.monotonic() < deadline
                time.sleep(0.01)
            run.stop("cancelled")
            future.result(timeout=30)
    in_flight = [a for a in run.attempts if a.outbound]
    assert len(in_flight) == 1
    assert in_flight[0].status == STATUS_CANCELLED_IN_FLIGHT
    # The streamed response was cancelled while the server was still holding
    # the headers: the call went out and stayed attributed, but the stream
    # never opened, so nothing was observed at all (the old non-stream path
    # returned a full payload the provider had computed).
    assert in_flight[0].usage_presence == "unset"
    assert in_flight[0].usage_provided == {}
    summary = summarize_attempts(run.attempts)
    assert summary["known"]["total_tokens"] == 0
    assert summary["completeness"] == "unknown"


def test_provider_rejection_and_outbound_timeout_states(ledger_env: tuple[Any, dict[str, Any]]) -> None:
    adapter, state = ledger_env
    from src.core.research_run import ResearchRun

    state["mode"] = "reject"
    run = ResearchRun(30)
    with run.bind():
        adapter.call_completion(_messages(), timeout=10)
    rejected = [a for a in run.attempts if a.outbound]
    assert rejected and all(a.status == STATUS_REJECTED for a in rejected)
    # The 429 never produced a response to adopt: the wire model survives
    # because the pre-call observation already recorded it.
    assert all(a.wire_model == "fixture" for a in rejected)

    state["mode"] = "default"
    state["hold"] = 5.0
    run2 = ResearchRun(30)
    with run2.bind():
        response = adapter.call_completion(_messages(), timeout=1.0)
    assert response.provider == "error"
    timed_out = [a for a in run2.attempts if a.outbound]
    assert timed_out and all(a.status == STATUS_TIMEOUT for a in timed_out)
    assert all(a.wire_model == "fixture" for a in timed_out)  # same on timeout
    state["hold"] = 0.0


def test_parameter_recovery_counts_two_outbounds(ledger_env: tuple[Any, dict[str, Any]]) -> None:
    adapter, state = ledger_env
    state["mode"] = "temperature_error"
    from src.core.research_run import ResearchRun

    run = ResearchRun(30)
    with run.bind():
        with attempt_scope(ENTRY_AGENT, "step1"):
            response = adapter.call_completion(_messages(), timeout=10)
    assert response.provider != "error", response.content
    assert state["count"] == 2  # the recovery retry is its own real request
    assert len(run.attempts) == 2
    statuses = [a.status for a in run.attempts]
    assert STATUS_OK in statuses
    summary = summarize_attempts(run.attempts)
    assert summary["outbound"] == 2


def test_fallback_models_are_separate_attempts(ledger_env: tuple[Any, dict[str, Any]]) -> None:
    adapter, state = ledger_env
    # LiteLLM strips the "openai/" prefix on the wire; reject the primary by
    # its wire name so only the first attempt fails.
    state["reject_models"] = {"fixture"}
    from src.config import Config

    config = Config._instance
    assert config is not None
    base = config.llm_model_list[0]["litellm_params"]["api_base"]
    config.llm_model_list = [
        {"model_name": "openai/fixture", "litellm_params": {"model": "openai/fixture", "api_base": base, "api_key": "fixture-key"}},
        {"model_name": "openai/backup", "litellm_params": {"model": "openai/backup", "api_base": base, "api_key": "fixture-key"}},
    ]
    config.litellm_fallback_models = ["openai/backup"]
    adapter._init_litellm()
    from src.core.research_run import ResearchRun

    run = ResearchRun(30)
    with run.bind():
        response = adapter.call_completion(_messages(), timeout=10)
    assert response.provider != "error", response.content
    assert state["count"] == 2
    assert len([a for a in run.attempts if a.outbound]) == 2
    assert {a.status for a in run.attempts} == {STATUS_REJECTED, STATUS_OK}
    # Each attempt keeps its own wire model. The backup attempt's response
    # body echoes "fixture" (an alias); the observed outbound "backup" wins.
    assert [a.wire_model for a in run.attempts if a.outbound] == ["fixture", "backup"]


def test_response_alias_never_replaces_observed_request_model(ledger_env: tuple[Any, dict[str, Any]]) -> None:
    """A successful response echoing a different model name is a response alias.

    wire_model is the model observed on the outbound payload; the response's
    own model field must not overwrite it.
    """
    adapter, state = ledger_env
    state["mode"] = "alias_model"
    from src.core.research_run import ResearchRun

    run = ResearchRun(30)
    with run.bind():
        with attempt_scope(ENTRY_AGENT, "step1"):
            response = adapter.call_completion(_messages(), timeout=10)
    assert response.provider != "error", response.content
    [attempt] = run.attempts
    assert attempt.status == STATUS_OK and attempt.outbound is True
    assert attempt.wire_model == "fixture"  # observed leaving on the wire
    assert attempt.wire_model != "fixture-alias-echo"  # response alias dropped


def test_two_runs_never_cross_account(ledger_env: tuple[Any, dict[str, Any]]) -> None:
    adapter, _ = ledger_env
    from src.core.research_run import ResearchRun

    run_a = ResearchRun(30, run_id="a" * 32)
    run_b = ResearchRun(30, run_id="b" * 32)
    with run_a.bind():
        with attempt_scope(ENTRY_AGENT, "step1"):
            adapter.call_completion(_messages(), timeout=10)
    with run_b.bind():
        with attempt_scope(ENTRY_NARRATE, "data"):
            adapter.call_completion(_messages(), timeout=10)
    assert [a.run_id for a in run_a.attempts] == [run_a.run_id]
    assert [a.run_id for a in run_b.attempts] == [run_b.run_id]
    assert len(run_a.attempts) == 1 and len(run_b.attempts) == 1


def test_local_validation_failure_is_not_transport_observed(ledger_env: tuple[Any, dict[str, Any]]) -> None:
    """A LiteLLM local rejection (no HTTP sent) is not real outbound.

    Admission is not outbound: a local preflight failure never reached the
    transport layer, so the actual-outbound count must stay zero.
    """
    adapter, state = ledger_env
    import litellm
    import pytest as _pytest

    from src.core.research_run import ResearchRun
    from src.llm import concurrency

    # Deterministic local rejection: no credentials anywhere, unknown bare
    # model name — LiteLLM must reject before handing anything to a transport.
    monkeypatch = _pytest.MonkeyPatch()
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(litellm, "api_key", None, raising=False)
    monkeypatch.setattr(litellm, "openai_key", None, raising=False)
    try:
        run = ResearchRun(30)
        with run.bind():
            with attempt_scope(ENTRY_AGENT, "step1"):
                try:
                    concurrency.limited_completion(
                        litellm.completion,
                        model="xyz-no-slash-local-validation-fixture",
                        messages=[{"role": "user", "content": "local validation only"}],
                        timeout=2,
                    )
                except Exception:  # noqa: BLE001 - local rejection expected
                    pass
        [attempt] = run.attempts
        assert attempt.outbound is False
        assert attempt.transport_observed is False
        assert attempt.status == STATUS_PROVIDER_ERROR
        assert attempt.wire_model is None  # no transport handoff: unknown
        assert attempt.usage_presence == "unset"
        assert attempt.usage_record_id is None
        assert state["count"] == 0  # provider observed zero real requests
        summary = summarize_attempts(run.attempts)
        assert summary["outbound"] == 0 and summary["transport_observed"] == 0
    finally:
        monkeypatch.undo()


def test_partial_usage_fields_are_partial_not_complete(ledger_env: tuple[Any, dict[str, Any]]) -> None:
    """Usage with only some fields is partial; missing fields stay unknown."""
    adapter, state = ledger_env
    state["mode"] = "partial_usage"
    from src.core.research_run import ResearchRun

    run = ResearchRun(30)
    with run.bind():
        with attempt_scope(ENTRY_AGENT, "step1"):
            response = adapter.call_completion(_messages(), timeout=10)
    assert response.provider != "error"
    [attempt] = run.attempts
    assert attempt.usage_presence == "provided"
    assert set(attempt.usage_provided) == {"prompt_tokens"}
    summary = summarize_attempts(run.attempts)
    assert summary["completeness"] == "partial"
    assert summary["known"] == {"input_tokens": 10, "output_tokens": 0, "total_tokens": 0}
    assert summary["unknown_fields"] == {"input_tokens": 0, "output_tokens": 1, "total_tokens": 1}


def test_stream_success_releases_permit_and_records_usage_unknown(ledger_env: tuple[Any, dict[str, Any]]) -> None:
    """A complete stream keeps its permit released and usage honestly unknown."""
    adapter, state = ledger_env
    state["mode"] = "stream_ok"
    from src.config import Config
    from src.core.research_run import ResearchRun
    from src.llm import concurrency
    import litellm

    config = Config._instance
    assert config is not None
    run = ResearchRun(30)
    with run.bind():
        with attempt_scope(ENTRY_AGENT, "step1"):
            base_url = Config._instance.llm_model_list[0]["litellm_params"]["api_base"]
            stream = concurrency.limited_completion(
                litellm.completion, model="openai/fixture", messages=_messages(),
                timeout=15, stream=True, api_key="fixture-key", api_base=base_url,
            )
            consumed = [chunk for chunk in stream]
        assert consumed
        state["mode"] = "default"
        # The permit is gone once the stream ended: a new request goes out.
        with attempt_scope(ENTRY_AGENT, "step2"):
            response = adapter.call_completion(_messages(), timeout=15)
    assert response.provider != "error", response.content
    [streamed, second] = run.attempts
    assert streamed.outbound is True and streamed.transport_observed is True
    assert streamed.wire_model == "fixture"  # observed on the outbound payload
    # Natural exhaustion finalizes the stream attempt itself as ok — not
    # inferred from the second call succeeding.
    assert streamed.status == STATUS_OK
    # include_usage was requested and the stream completed: a missing tail
    # is an observed absence, never an unobserved transport.
    assert streamed.usage_presence == "absent"
    assert streamed.exec_seconds is not None
    assert second.status == STATUS_OK


def test_stream_abort_keeps_terminal_state(ledger_env: tuple[Any, dict[str, Any]]) -> None:
    """A mid-stream stall past the read budget ends in a failure terminal, not ok.

    (A peer that simply closes the connection early is EOF in SSE semantics;
    LiteLLM treats that as natural exhaustion — ok with fewer chunks — and
    the ledger records it accordingly, see docs/model-attribution-usage.md.)
    """
    adapter, state = ledger_env
    state["mode"] = "stream_fail"
    from src.config import Config
    from src.core.research_run import ResearchRun
    from src.llm import concurrency
    import litellm

    run = ResearchRun(30)
    with run.bind():
        with attempt_scope(ENTRY_AGENT, "step1"):
            base_url = Config._instance.llm_model_list[0]["litellm_params"]["api_base"]
            stream = concurrency.limited_completion(
                litellm.completion, model="openai/fixture", messages=_messages(),
                timeout=3, stream=True, api_key="fixture-key", api_base=base_url,
            )
            try:
                for _chunk in stream:
                    pass
            except Exception:  # noqa: BLE001 - read timeout raises mid-stream
                pass
    state["mode"] = "default"
    [attempt] = run.attempts
    assert attempt.outbound is True
    # LiteLLM wraps mid-stream read failures as MidStreamFallbackError
    # (ServiceUnavailableError): a real failure terminal, never ok.
    assert attempt.status == STATUS_PROVIDER_ERROR
    assert attempt.wire_model == "fixture"  # known model survives the failure


def test_rejected_response_usage_recovered_from_error_body(ledger_env: tuple[Any, dict[str, Any]]) -> None:
    """A 429 whose body reports usage: recovered via the exception .body.

    LiteLLM raises instead of returning on 4xx, but APIStatusError.body
    keeps the parsed provider payload, so the usage (7 tokens) is recovered,
    persisted exactly once, and linked to the rejected attempt.
    """
    adapter, state = ledger_env
    state["mode"] = "reject_with_usage"
    from src.core.research_run import ResearchRun

    run = ResearchRun(30)
    with run.bind():
        with attempt_scope(ENTRY_AGENT, "step1"):
            response = adapter.call_completion(_messages(), timeout=10)
    assert response.provider == "error"
    [attempt] = run.attempts
    assert attempt.status == STATUS_REJECTED
    assert attempt.outbound is True and attempt.transport_observed is True
    assert attempt.usage_presence == "provided"
    assert attempt.usage_provided.get("total_tokens") == 7
    assert attempt.usage_record_id is not None
    rows = _audit_rows()
    assert rows == [{"call_type": "agent", "model": "openai/fixture", "total_tokens": 7}]
    summary = summarize_attempts(run.attempts)
    assert summary["known"]["total_tokens"] == 7  # failed call, real usage kept


def test_confirmed_zero_usage_writes_audit_row(ledger_env: tuple[Any, dict[str, Any]]) -> None:
    """An explicit all-zero payload is confirmed usage: one audit row, no filter."""
    adapter, state = ledger_env
    state["mode"] = "zero_usage"
    from src.core.research_run import ResearchRun

    run = ResearchRun(30)
    with run.bind():
        with attempt_scope(ENTRY_NARRATE, "phase"):
            response = adapter.call_completion(_messages(), timeout=10)
    assert response.provider != "error"
    [attempt] = run.attempts
    assert attempt.usage_presence == "provided"
    assert attempt.usage_record_id is not None
    rows = _audit_rows()
    assert rows == [{"call_type": "narrate", "model": "openai/fixture", "total_tokens": 0}]


def test_manager_wave_purposes_are_distinct_entries() -> None:
    """Executed manager waves carry stable wave purposes at the call site."""
    from src.deep_research_dims import manager_writeup as mw
    from unittest.mock import MagicMock

    captured: list[tuple[str, str]] = []
    from unittest.mock import MagicMock as _MM  # noqa: F401

    class _Recorder:
        def call_text(self, *_args: Any, **_kwargs: Any) -> Any:
            entry, purpose = __import__("src.llm.attempt", fromlist=["attempt_entry"]).attempt_entry.get()
            captured.append((entry, purpose))
            resp = MagicMock()
            resp.provider = "openai"
            resp.finish_reason = "stop"
            resp.content = '{"title": "样本标题"}'
            return resp

    adapter = _Recorder()
    # Evidence gate satisfied so the waves actually execute (skipped waves
    # must not record anything).
    dim = MagicMock()
    dim.status = "ok"
    dims = {key: dim for key in ("fundamental", "capital", "scenarios", "conclusion", "signal")}
    result = mw.generate_manager_writeup(adapter, "样本", "600519", dims, [])
    purposes = [p for e, p in captured if e == "manager_writeup"]
    assert purposes, "manager waves must record their entry"
    assert len(set(purposes)) == len(purposes), "wave purposes must be distinguishable"
    assert all(p.startswith("wave_") for p in purposes)
    assert result == {} or isinstance(result, dict)


def test_empty_provided_usage_is_unknown_not_partial() -> None:
    """A presence marker without any known core field is not "partial"."""
    from src.llm.attempt import ModelAttempt

    attempt = ModelAttempt(run_id="r", entry=ENTRY_AGENT, target_model="openai/fixture",
                           outbound=True, usage_presence="provided")
    attempt.usage_provided = {}
    summary = summarize_attempts([attempt])
    assert summary["completeness"] == "unknown"


def test_negative_usage_keeps_invalid_diagnostics() -> None:
    """Negative provider counts stay visible for the invalid-usage diagnosis."""
    from src.llm.usage import normalize_litellm_usage, sanitized_provider_usage

    sanitized = sanitized_provider_usage({"prompt_tokens": -1, "completion_tokens": 5, "total_tokens": 15})
    assert sanitized["prompt_tokens"] == -1  # not silently dropped
    usage = normalize_litellm_usage(sanitized, model="openai/gpt-test")
    assert usage["cache_observation"] == "invalid_provider_usage"


def test_explicit_early_close_is_truncated_not_ok(ledger_env: tuple[Any, dict[str, Any]]) -> None:
    """Closing a stream before natural exhaustion is not a success."""
    adapter, state = ledger_env
    state["mode"] = "stream_ok"
    from src.config import Config
    from src.core.research_run import ResearchRun
    from src.llm import concurrency
    from src.llm.attempt import STATUS_TRUNCATED as TRUNCATED
    import litellm

    config = Config._instance
    assert config is not None
    base_url = config.llm_model_list[0]["litellm_params"]["api_base"]
    run = ResearchRun(30)
    with run.bind():
        with attempt_scope(ENTRY_AGENT, "step1"):
            stream = concurrency.limited_completion(
                litellm.completion, model="openai/fixture", messages=_messages(),
                timeout=15, stream=True, api_key="fixture-key", api_base=base_url,
            )
            first = next(iter(stream))
            assert first is not None
            stream.close()  # caller aborts before [DONE]
    state["mode"] = "default"
    [attempt] = run.attempts
    assert attempt.status == TRUNCATED  # unconsumed output, never ok
    assert attempt.outbound is True
    assert attempt.wire_model == "fixture"  # close() keeps the observed model


def test_stream_tail_usage_persists_after_scope_exit(ledger_env: tuple[Any, dict[str, Any]]) -> None:
    """A stream consumed after its creating scope exited still attributes once.

    The entry/purpose snapshot lives on the attempt, so tail usage adoption
    and audit persistence cannot read an already-exited ContextVar.
    """
    adapter, state = ledger_env
    state["mode"] = "stream_tail_usage"
    from src.config import Config
    from src.core.research_run import ResearchRun
    from src.llm import concurrency
    import litellm

    config = Config._instance
    assert config is not None
    base_url = config.llm_model_list[0]["litellm_params"]["api_base"]
    run = ResearchRun(30)
    stream = None
    with run.bind():
        with attempt_scope(ENTRY_NARRATE, "data"):
            stream = concurrency.limited_completion(
                litellm.completion, model="openai/fixture", messages=_messages(),
                timeout=15, stream=True, api_key="fixture-key", api_base=base_url,
                stream_options={"include_usage": True},
            )
    # Scope exited; only now does the caller consume the stream.
    consumed = [chunk for chunk in stream]
    state["mode"] = "default"
    assert consumed
    [attempt] = run.attempts
    assert attempt.entry == ENTRY_NARRATE  # attribution from the creation scope
    assert attempt.status == STATUS_OK
    assert attempt.usage_presence == "provided"
    assert attempt.usage_provided.get("total_tokens") == 9
    assert attempt.usage_record_id is not None  # persisted once with the right entry
    rows = _audit_rows()
    assert rows == [{"call_type": "narrate", "model": "openai/fixture", "total_tokens": 9}]


def test_stream_tail_presence_from_raw_sse(ledger_env: tuple[Any, dict[str, Any]]) -> None:
    """Stream tail presence comes from the raw SSE chunk, not the SDK object.

    With include_usage the SDK synthesizes a full Usage when the provider sent
    only prompt_tokens or nothing at all; completeness must follow the raw
    provider payload: partial stays partial, absent stays unknown.
    """
    adapter, state = ledger_env
    from src.config import Config
    from src.core.research_run import ResearchRun
    from src.llm import concurrency
    import litellm

    config = Config._instance
    assert config is not None
    base_url = config.llm_model_list[0]["litellm_params"]["api_base"]

    def consume(mode: str) -> Any:
        state["mode"] = mode
        run = ResearchRun(30)
        stream = None
        with run.bind():
            with attempt_scope(ENTRY_AGENT, "step1"):
                stream = concurrency.limited_completion(
                    litellm.completion, model="openai/fixture", messages=_messages(),
                    timeout=15, stream=True, api_key="fixture-key", api_base=base_url,
                    stream_options={"include_usage": True},
                )
        assert list(stream)
        state["mode"] = "default"
        return run.attempts[0]

    partial = consume("stream_partial_usage")
    assert partial.status == STATUS_OK
    assert partial.usage_presence == "provided"
    assert set(partial.usage_provided) == {"prompt_tokens"}
    assert summarize_attempts([partial])["completeness"] == "partial"

    absent = consume("stream_no_usage")
    assert absent.status == STATUS_OK
    assert absent.usage_presence == "absent"
    assert summarize_attempts([absent])["completeness"] == "unknown"
    assert absent.usage_record_id is None


def test_stream_close_exception_still_releases_permit(ledger_env: tuple[Any, dict[str, Any]]) -> None:
    """A raising underlying close must not leak the admission permit."""
    adapter, state = ledger_env
    state["mode"] = "stream_ok"
    from src.config import Config
    from src.core.research_run import ResearchRun
    from src.llm import concurrency
    import litellm

    config = Config._instance
    assert config is not None
    base_url = config.llm_model_list[0]["litellm_params"]["api_base"]
    run = ResearchRun(30)
    with run.bind():
        with attempt_scope(ENTRY_AGENT, "step1"):
            stream = concurrency.limited_completion(
                litellm.completion, model="openai/fixture", messages=_messages(),
                timeout=15, stream=True, api_key="fixture-key", api_base=base_url,
            )
            first = next(iter(stream))
            assert first is not None
            # The stream is still open (not exhausted); the underlying close
            # now fails while the permit is genuinely held.
            def boom() -> None:
                raise RuntimeError("close transport failed")

            stream._stream.close = boom
            with pytest.raises(RuntimeError):
                stream.close()
        state["mode"] = "default"
        # The permit is gone: a following request goes out despite the broken close.
        with attempt_scope(ENTRY_AGENT, "step2"):
            response = adapter.call_completion(_messages(), timeout=15)
    # The broken close must not hold the slot: the following request reached
    # the provider (count grew), proving the permit was released.
    assert state["count"] >= 2
    assert response.provider != "error", response.content


def test_cancel_stream_after_owning_run_context_exits(ledger_env: tuple[Any, dict[str, Any]]) -> None:
    """Cancellation after the creating scope exited still wins the terminal.

    The stream captures its owning run at creation; consuming it after both
    run.bind() and attempt_scope exited must still record the cancellation
    and keep the usage attribution.
    """
    adapter, state = ledger_env
    state["mode"] = "stream_ok"
    from src.config import Config
    from src.core.research_run import ResearchRun
    from src.llm import concurrency
    from src.llm.attempt import STATUS_CANCELLED_IN_FLIGHT as CANCELLED
    import litellm

    config = Config._instance
    assert config is not None
    base_url = config.llm_model_list[0]["litellm_params"]["api_base"]
    run = ResearchRun(30)
    stream = None
    with run.bind():
        with attempt_scope(ENTRY_AGENT, "step1"):
            stream = concurrency.limited_completion(
                litellm.completion, model="openai/fixture", messages=_messages(),
                timeout=15, stream=True, api_key="fixture-key", api_base=base_url,
            )
            first = next(iter(stream))
            assert first is not None
    # Both scopes exited; cancel, then finish consuming.
    run.stop("cancelled")
    consumed = list(stream)
    state["mode"] = "default"
    assert consumed
    [attempt] = run.attempts
    assert attempt.status == CANCELLED
    assert attempt.entry == ENTRY_AGENT
    assert attempt.outbound is True
