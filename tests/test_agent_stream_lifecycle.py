"""Agent adapter streaming: aggregation fidelity, lifecycle, usage provenance.

Issue #57: the shared LLMToolAdapter entry streams upstream responses and
aggregates them back into the original full-response parser contract, with a
real wall-clock lifecycle for the consumed stream and provider-actual usage
only. Loopback HTTP providers only — never paid APIs.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest

from src.llm.attempt import (
    ENTRY_AGENT,
    STATUS_CANCELLED_IN_FLIGHT,
    STATUS_OK,
    STATUS_STOPPED_PRE_OUTBOUND,
    STATUS_TIMEOUT,
    STATUS_TRUNCATED,
    attempt_scope,
)

# Fixture-owned safety bound: an endless provider stream stops feeding on its
# own long before any test-level timeout, so a missing product watchdog fails
# a bounded test instead of hanging pytest on a live connection.
_ENDLESS_SELF_STOP_SECONDS = 45.0


@pytest.fixture
def stream_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Any, dict[str, Any]]]:
    """Loopback provider with a scriptable SSE stream and wire observability."""
    state: dict[str, Any] = {
        "count": 0,
        "active": 0,
        "peak": 0,
        "streams": 0,
        "wire_bodies": [],
        "script": [],
        # endless: keep emitting small chunks until the client disconnects.
        "endless": False,
        "endless_delay": 0.05,
        "endless_deadline": 0.0,
        "script_queue": [],
    }
    lock = threading.Lock()

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                state["count"] += 1
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
                state["wire_bodies"].append(dict(request))
                # A script queue serves one script per request (multi-turn
                # agent conversations); otherwise the single script repeats.
                queue = state.get("script_queue") or []
                if queue:
                    script = list(queue[min(state["count"], len(queue)) - 1])
                else:
                    script = list(state["script"])
                endless = bool(state["endless"])
                endless_deadline = float(state["endless_deadline"])
            try:
                if not request.get("stream"):
                    payload = {
                        "id": "fixture", "object": "chat.completion", "created": 0,
                        "model": "fixture",
                        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": '{"status":"ok"}'}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    }
                    data = json.dumps(payload).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                with lock:
                    state["streams"] += 1
                self._serve_stream(script, endless, endless_deadline)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                with lock:
                    state["active"] -= 1

        def _client_gone(self) -> bool:
            # A handler sleeping between chunks must still observe the forced
            # client disconnect, otherwise server-side cleanup evidence is
            # unprovable for stall scripts.
            try:
                self.connection.settimeout(0.0)
                return self.connection.recv(1, 0x40) == b""  # MSG_PEEK
            except OSError:
                return False

        def _sleep_observing(self, seconds: float) -> bool:
            """Sleep in slices, returning True when the client disconnected."""
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if self._client_gone():
                    return True
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            return self._client_gone()

        def _serve_stream(self, script: list[dict[str, Any]], endless: bool, endless_deadline: float) -> None:
            # Headers go out immediately; body timing follows the script so
            # tests can separate "headers arrived" from "first SSE chunk".
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for event in script:
                    if event.get("delay"):
                        if self._sleep_observing(event["delay"]):
                            return
                    payload: dict[str, Any] = {
                        "id": "fixture", "object": "chat.completion.chunk", "created": 0,
                        "model": event.get("model", "fixture"),
                    }
                    if event.get("empty_choices"):
                        payload["choices"] = []
                    else:
                        payload["choices"] = [{
                            "index": 0,
                            "delta": event.get("delta") or {},
                            "finish_reason": event.get("finish"),
                        }]
                    if "usage" in event:
                        payload["usage"] = event["usage"]
                    if "choice_usage" in event:
                        # Some providers (current Kimi) deliver the usage tail
                        # on the choice instead of the chunk; render it where
                        # such a provider actually puts it.
                        payload["choices"][0]["usage"] = event["choice_usage"]
                    self.wfile.write(("data: " + json.dumps(payload) + "\n\n").encode())
                    self.wfile.flush()
                if endless:
                    # Fixture-owned self stop: never feed past the deadline so
                    # a broken product cannot keep a server thread alive.
                    while time.monotonic() < endless_deadline:
                        if self._sleep_observing(float(state["endless_delay"])):
                            return
                        payload = {
                            "id": "fixture", "object": "chat.completion.chunk", "created": 0,
                            "model": "fixture",
                            "choices": [{"index": 0, "delta": {"content": "x"}, "finish_reason": None}],
                        }
                        self.wfile.write(("data: " + json.dumps(payload) + "\n\n").encode())
                        self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        def log_message(self, *_args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}/v1"
    try:
        from src.config import Config
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
        monkeypatch.setattr(DatabaseManager, "_instance", None)
        database = DatabaseManager(f"sqlite:///{tmp_path / 'stream.sqlite'}")
        monkeypatch.setattr(DatabaseManager, "_instance", database)
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        from src.llm import concurrency

        monkeypatch.setattr(concurrency, "_slots", None)
        monkeypatch.setattr(concurrency, "_in_flight", 0)
        from src.agent.llm_adapter import LLMToolAdapter

        adapter = LLMToolAdapter(config)
        yield adapter, state
    finally:
        state["endless"] = False
        state["endless_deadline"] = 0.0
        server.shutdown()
        server.server_close()
        from src.llm import concurrency

        concurrency._slots = None


def _messages() -> list[dict[str, Any]]:
    return [{"role": "user", "content": "hello"}]


def _wait_for(predicate: Any, timeout: float = 10.0, message: str = "condition never met") -> None:
    end = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < end, message
        time.sleep(0.005)


def _wait_server_idle(state: dict[str, Any], timeout: float = 10.0) -> None:
    """Server-side disconnect evidence: no handler is still feeding a stream."""
    _wait_for(lambda: state["active"] == 0, timeout, "server handler never observed the client disconnect")


def _wait_outbound(state: dict[str, Any], count: int, timeout: float = 10.0) -> None:
    _wait_for(lambda: state["count"] >= count, timeout, "request never reached the wire")


def _ok_script() -> list[dict[str, Any]]:
    return [
        {"delta": {"role": "assistant", "content": '{"status":"ok"}'}, "finish": "stop"},
        {"empty_choices": True, "usage": {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9}},
    ]


def test_agent_completion_streams_and_aggregates_original_contract(
    stream_env: tuple[Any, dict[str, Any]],
) -> None:
    """The shared agent entry streams on the wire and aggregates into the
    original full-response contract: text, reasoning, finish, tool-ready
    shape and provider-actual usage from the tail chunk."""
    adapter, state = stream_env
    state["script"] = [
        {"delta": {"role": "assistant", "reasoning_content": "先想"}},
        {"delta": {"content": '{"sta'}},
        {"delta": {"content": 'tus":"ok"}'}, "finish": "stop"},
        {"empty_choices": True, "usage": {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9}},
    ]
    from src.core.research_run import ResearchRun

    run = ResearchRun(30)
    with run.bind():
        with attempt_scope(ENTRY_AGENT, "step"):
            response = adapter.call_completion(_messages(), timeout=15)
    assert response.provider != "error", response.content
    assert response.content == '{"status":"ok"}'
    assert response.reasoning_content == "先想"
    assert response.finish_reason == "stop"
    # The wire request actually streamed and asked for the usage tail.
    [wire] = state["wire_bodies"]
    assert wire.get("stream") is True
    assert (wire.get("stream_options") or {}).get("include_usage") is True
    # Provider-actual tail usage, never a synthesized estimate.
    assert response.usage.get("normalized_prompt_tokens") == 4
    assert response.usage.get("normalized_completion_tokens") == 5
    assert response.usage.get("normalized_total_tokens") == 9
    assert response.usage.get("provider_reported_prompt_tokens") == 4
    [attempt] = run.attempts
    assert attempt.status == STATUS_OK
    assert attempt.usage_presence == "provided"
    assert attempt.outbound is True


def test_absent_provider_usage_stays_unknown_everywhere(
    stream_env: tuple[Any, dict[str, Any]],
) -> None:
    """A complete stream whose provider never sent usage stays unknown: the
    LiteLLM-synthesized stream estimates are never promoted, neither in the
    parsed response usage nor in the attempt ledger (root RED R5)."""
    adapter, state = stream_env
    state["script"] = [
        {"delta": {"role": "assistant", "content": '{"ok":true}'}, "finish": "stop"},
    ]
    from src.core.research_run import ResearchRun
    from src.llm.attempt import summarize_attempts

    run = ResearchRun(15)
    with run.bind(), attempt_scope(ENTRY_AGENT, "no-usage"):
        response = adapter.call_completion(_messages(), timeout=10)
    assert response.provider != "error", response.content
    assert response.content == '{"ok":true}'
    assert response.usage.get("normalized_prompt_tokens") is None, response.usage
    assert response.usage.get("normalized_completion_tokens") is None, response.usage
    assert response.usage.get("normalized_total_tokens") is None, response.usage
    summary = summarize_attempts(run.attempts)
    assert summary["completeness"] == "unknown", summary
    assert summary["outbound"] == state["count"] == 1
    assert summary["known"]["total_tokens"] == 0
    [attempt] = run.attempts
    assert attempt.usage_presence == "absent"


@pytest.mark.parametrize("arguments", ['{"stock', "[]"])
def test_invalid_stream_tool_arguments_never_escape_executable(
    stream_env: tuple[Any, dict[str, Any]], arguments: str,
) -> None:
    """Truncated or non-object tool arguments are a parse failure of the
    whole response: no executable ToolCall, no fallback replay (root R7)."""
    adapter, state = stream_env
    state["script"] = [
        {"delta": {"tool_calls": [{"index": 0, "id": "tool-1", "type": "function",
                                   "function": {"name": "get_quote", "arguments": arguments}}]},
         "finish": "tool_calls"},
    ]
    tool = {"type": "function", "function": {"name": "get_quote", "parameters": {"type": "object"}}}
    from src.core.research_run import ResearchRun

    run = ResearchRun(15)
    with run.bind(), attempt_scope(ENTRY_AGENT, "invalid-tool"):
        response = adapter.call_with_tools([{"role": "user", "content": "quote"}], [tool], timeout=10)
    assert response.provider == "error", response
    assert not response.tool_calls, response.tool_calls
    assert state["count"] == 1, state["wire_bodies"]


def test_interleaved_tool_fragments_aggregate_with_provider_fields(
    stream_env: tuple[Any, dict[str, Any]],
) -> None:
    """Multi-tool interleaved argument fragments, thought signatures and a
    usage-only tail survive into the original response contract."""
    adapter, state = stream_env
    state["script"] = [
        {"delta": {"role": "assistant", "reasoning_content": "先查行情"}},
        {"delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                   "function": {"name": "get_quote", "arguments": '{"stock'}}]}},
        {"delta": {"tool_calls": [{"index": 1, "id": "call_2", "type": "function",
                                   "function": {"name": "get_daily", "arguments": '{"days":5}'}}]}},
        {"delta": {"tool_calls": [{"index": 0,
                                   "function": {"name": None, "arguments": '_code":"600519"}'}}]}},
        {"delta": {}, "finish": "tool_calls"},
        {"empty_choices": True, "usage": {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9}},
    ]
    from src.core.research_run import ResearchRun

    run = ResearchRun(15)
    with run.bind(), attempt_scope(ENTRY_AGENT, "tools"):
        response = adapter.call_completion(_messages(), timeout=15)
    assert response.provider != "error", response.content
    assert response.finish_reason == "tool_calls"
    [first, second] = response.tool_calls
    assert (first.name, first.arguments) == ("get_quote", {"stock_code": "600519"})
    assert (second.name, second.arguments) == ("get_daily", {"days": 5})
    assert response.reasoning_content == "先查行情"
    assert response.usage.get("normalized_total_tokens") == 9
    [attempt] = run.attempts
    assert attempt.status == STATUS_OK
    assert attempt.usage_presence == "provided"


def test_choice_level_usage_tail_is_provider_actual(
    stream_env: tuple[Any, dict[str, Any]],
) -> None:
    """Current Kimi also delivers usage on the choice instead of the chunk;
    a choice-only tail counts as provider-actual. The usage never rides the
    chunk top level here, so losing the choice observation loses the tail."""
    adapter, state = stream_env
    state["script"] = [
        {"delta": {"role": "assistant", "content": '{"ok":1}'}, "finish": "stop",
         "choice_usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}},
    ]
    from src.core.research_run import ResearchRun

    run = ResearchRun(15)
    with run.bind(), attempt_scope(ENTRY_AGENT, "choice-usage"):
        response = adapter.call_completion(_messages(), timeout=10)
    assert response.provider != "error", response.content
    assert response.usage.get("normalized_prompt_tokens") == 7
    assert response.usage.get("normalized_total_tokens") == 10
    [attempt] = run.attempts
    assert attempt.usage_presence == "provided"
    assert attempt.usage_provided == {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}


def test_top_level_usage_keeps_priority_over_choice_tail(
    stream_env: tuple[Any, dict[str, Any]],
) -> None:
    """When one stream delivers usage on both the chunk and its choice, the
    top level wins and the diverging choice values never leak through."""
    adapter, state = stream_env
    state["script"] = [
        {"delta": {"role": "assistant", "content": '{"ok":1}'}, "finish": "stop",
         "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
         "choice_usage": {"prompt_tokens": 90, "completion_tokens": 9, "total_tokens": 99}},
    ]
    from src.core.research_run import ResearchRun

    run = ResearchRun(15)
    with run.bind(), attempt_scope(ENTRY_AGENT, "top-priority-usage"):
        response = adapter.call_completion(_messages(), timeout=10)
    assert response.provider != "error", response.content
    assert response.usage.get("normalized_prompt_tokens") == 7
    assert response.usage.get("normalized_total_tokens") == 10
    [attempt] = run.attempts
    assert attempt.usage_presence == "provided"
    assert attempt.usage_provided == {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}


def test_stream_wall_clock_cap_interrupts_endless_chunks(
    stream_env: tuple[Any, dict[str, Any]],
) -> None:
    """Continuous small chunks cannot extend the response wall clock.

    The absolute deadline starts at the real outbound (header latency
    included) and covers first chunk, reasoning, body and end. The watchdog
    closes the transport so the blocked read returns in bounded time, the
    server observes the disconnect, the attempt keeps a truncation terminal
    state, and the permit is reusable in a fresh run afterwards.
    """
    adapter, state = stream_env
    from src.config import Config
    from src.core.research_run import ResearchRun

    Config._instance.llm_stream_response_timeout = 1.0
    state["endless"] = True
    state["endless_deadline"] = time.monotonic() + _ENDLESS_SELF_STOP_SECONDS
    run = ResearchRun(30)
    with run.bind():
        with attempt_scope(ENTRY_AGENT, "step"):
            started = time.monotonic()
            response = adapter.call_completion(_messages(), timeout=20)
            elapsed = time.monotonic() - started
    assert response.provider == "error", response.content
    assert elapsed < 5, elapsed  # bounded, not the caller's 20s budget
    [attempt] = run.attempts
    assert attempt.status == STATUS_TIMEOUT  # per-call wall clock, not truncation
    assert attempt.outbound is True
    assert attempt.usage_presence == "absent"  # consumed stream sent no usage
    # Issue 57 attribution: the wall-clock expiry surfaces as a bare
    # TimeoutError whose str() is empty; the boundary text must still be
    # recognisably timeout for explore_agents' text classifier.
    assert "timeout" in response.content.lower(), response.content
    # Server-side evidence: the endless handler stops feeding once the
    # client socket closed.
    _wait_server_idle(state)
    assert state["active"] == 0
    # The permit is reusable: a fresh run with a normal script completes.
    state["endless"] = False
    state["script"] = _ok_script()
    run2 = ResearchRun(15)
    with run2.bind(), attempt_scope(ENTRY_AGENT, "next"):
        followup = adapter.call_completion(_messages(), timeout=10)
    assert followup.provider != "error", followup.content
    assert state["peak"] <= 1  # real concurrency never exceeded the limit
    [follow_attempt] = run2.attempts
    assert follow_attempt.status == STATUS_OK


def test_blank_timeout_error_stays_recognisable_at_boundary(
    stream_env: tuple[Any, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue 57 boundary formatting: a bare TimeoutError (empty str()) keeps
    the exception type in the error content so text classifiers still see a
    timeout; a non-empty message passes through unchanged."""
    adapter, _state = stream_env

    def blank_timeout(*_args: Any, **_kwargs: Any) -> Any:
        raise TimeoutError()

    def labelled_timeout(*_args: Any, **_kwargs: Any) -> Any:
        raise TimeoutError(
            "LLM completion timed out before trying fallback model openai/fixture"
        )

    from src.core.research_run import ResearchRun

    run = ResearchRun(30)
    with run.bind(), attempt_scope(ENTRY_AGENT, "blank"):
        monkeypatch.setattr(adapter, "_call_litellm_model", blank_timeout)
        response = adapter.call_completion(_messages(), timeout=5)
    assert response.provider == "error", response.content
    assert "Last error: TimeoutError" in response.content, response.content
    assert "timeout" in response.content.lower(), response.content

    with run.bind(), attempt_scope(ENTRY_AGENT, "labelled"):
        monkeypatch.setattr(adapter, "_call_litellm_model", labelled_timeout)
        response = adapter.call_completion(_messages(), timeout=5)
    assert (
        "Last error: LLM completion timed out before trying fallback model openai/fixture"
        in response.content
    ), response.content


def test_cancel_interrupts_blocked_read_and_releases_permit(
    stream_env: tuple[Any, dict[str, Any]],
) -> None:
    """Cancellation interrupts a blocked mid-stream read in bounded time.

    The stream stalls after the first chunk; run.stop() must close the
    transport so the read returns well before the stall ends. The cancelled
    run keeps cancelled_in_flight (real outbound) and its old-run follow-up
    stays zero-outbound; only a fresh run proves the freed permit works.
    """
    adapter, state = stream_env
    from src.core.research_run import ResearchRun

    state["script"] = [
        {"delta": {"role": "assistant", "content": "部分"}},
        {"delta": {"content": "永远不会到达"}, "delay": 20},
    ]
    run = ResearchRun(30)
    with run.bind():
        with attempt_scope(ENTRY_AGENT, "step"):

            def cancel_soon() -> None:
                _wait_outbound(state, 1)
                time.sleep(0.3)  # let the read block on the stalled second chunk
                run.stop("cancelled")

            threading.Thread(target=cancel_soon, daemon=True).start()
            started = time.monotonic()
            response = adapter.call_completion(_messages(), timeout=25)
            elapsed = time.monotonic() - started
        assert response.provider == "error", response.content
        assert elapsed < 10, elapsed  # interrupted, not the 20s stall
        [attempt] = run.attempts
        assert attempt.status == STATUS_CANCELLED_IN_FLIGHT
        assert attempt.outbound is True
        # Server-side evidence: the stalled handler observed the disconnect.
        _wait_server_idle(state, timeout=15)
        assert state["active"] == 0
        # The cancelled run accepts no new outbound at all.
        with attempt_scope(ENTRY_AGENT, "old-run"):
            stale = adapter.call_completion(_messages(), timeout=5)
        assert stale.provider == "error"
        assert state["count"] == 1, "a cancelled run must never send another request"
    assert run.attempts[-1].status == STATUS_STOPPED_PRE_OUTBOUND
    # Capacity reuse is proven in a fresh run with a normal script.
    state["script"] = _ok_script()
    run2 = ResearchRun(15)
    with run2.bind(), attempt_scope(ENTRY_AGENT, "next"):
        followup = adapter.call_completion(_messages(), timeout=10)
    assert followup.provider != "error", followup.content
    assert state["peak"] <= 1
    [follow_attempt] = run2.attempts
    assert follow_attempt.status == STATUS_OK


def test_report_budget_exhaustion_keeps_run_cause_and_releases_permit(
    stream_env: tuple[Any, dict[str, Any]],
) -> None:
    """A report budget running out mid-stream ends the response as the
    per-call wall clock (the scope is min(caller, report remaining, cap)),
    the exhausted run reports its real cause on the next check, and the
    freed permit carries a fresh run to completion."""
    adapter, state = stream_env
    from src.core.research_run import ResearchStopped, ResearchRun

    state["endless"] = True
    state["endless_deadline"] = time.monotonic() + _ENDLESS_SELF_STOP_SECONDS
    run = ResearchRun(1.0)  # the report budget itself is the wall clock here
    with run.bind():
        with attempt_scope(ENTRY_AGENT, "step"):
            response = adapter.call_completion(_messages(), timeout=20)
    assert response.provider == "error", response.content
    [attempt] = run.attempts
    assert attempt.status == STATUS_TIMEOUT
    assert attempt.outbound is True
    # The exhausted run reports its real cause at the next checkpoint.
    with pytest.raises(ResearchStopped, match="报告总时间预算已用完"):
        run.check()
    assert run.stop_reason == "report_budget"
    _wait_server_idle(state)
    assert state["active"] == 0
    # Fresh run: the freed permit carries a normal request to completion.
    state["endless"] = False
    state["script"] = _ok_script()
    run2 = ResearchRun(15)
    with run2.bind(), attempt_scope(ENTRY_AGENT, "next"):
        followup = adapter.call_completion(_messages(), timeout=10)
    assert followup.provider != "error", followup.content
    assert state["peak"] <= 1
    [follow_attempt] = run2.attempts
    assert follow_attempt.status == STATUS_OK


def test_streamed_tool_call_roundtrip_carries_provider_fields(
    stream_env: tuple[Any, dict[str, Any]],
) -> None:
    """A streamed tool call with provider roundtrip fields is executed and
    the follow-up request carries the full assistant message back: the tool
    result, the thought signature and the original content."""
    adapter, state = stream_env
    opinion = '{"signal":"hold","confidence":0.5,"reasoning":"工具数据充分"}'
    state["script_queue"] = [
        [
            {"delta": {"tool_calls": [{
                "index": 0, "id": "call_rt", "type": "function",
                "function": {"name": "get_stock_info", "arguments": '{"stock_code":"600519"}'},
                "provider_specific_fields": {"thought_signature": "sig-roundtrip"},
            }]}, "finish": "tool_calls"},
            {"empty_choices": True, "usage": {"prompt_tokens": 6, "completion_tokens": 2, "total_tokens": 8}},
        ],
        [
            {"delta": {"role": "assistant", "content": opinion}, "finish": "stop"},
            {"empty_choices": True, "usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13}},
        ],
    ]
    from src.core.research_run import ResearchRun

    run = ResearchRun(30)
    with run.bind():
        with attempt_scope(ENTRY_AGENT, "roundtrip"):
            first = adapter.call_with_tools(_messages(), [{
                "type": "function",
                "function": {"name": "get_stock_info", "parameters": {"type": "object"}},
            }], timeout=15)
        assert first.provider != "error", first.content
        [tool_call] = first.tool_calls
        assert tool_call.thought_signature == "sig-roundtrip"
        # The provider-required roundtrip field survives the aggregation.
        assert tool_call.provider_specific_fields.get("thought_signature") == "sig-roundtrip"
        # Execute the tool and send the second round with the full assistant
        # message: the executed result and the roundtrip field go back out.
        tool_result = {"name": "贵州茅台", "price": 50.0}
        follow_messages = _messages() + [
            {"role": "assistant", "content": first.content,
             "tool_calls": [{"id": tool_call.id, "name": tool_call.name,
                             "arguments": tool_call.arguments,
                             "thought_signature": tool_call.thought_signature,
                             "provider_specific_fields": tool_call.provider_specific_fields}]},
            {"role": "tool", "tool_call_id": tool_call.id, "content": json.dumps(tool_result)},
        ]
        with attempt_scope(ENTRY_AGENT, "roundtrip-2"):
            second = adapter.call_with_tools(follow_messages, [], timeout=15)
    assert second.provider != "error", second.content
    assert json.loads(second.content or "{}")["signal"] == "hold"
    [wire] = state["wire_bodies"][1:2]
    [assistant_message] = [m for m in wire["messages"] if m.get("role") == "assistant"]
    [sent_tool_call] = assistant_message["tool_calls"]
    assert sent_tool_call["function"]["name"] == "get_stock_info"
    psf = sent_tool_call.get("provider_specific_fields") or {}
    assert psf.get("thought_signature") == "sig-roundtrip"
    [tool_message] = [m for m in wire["messages"] if m.get("role") == "tool"]
    assert json.loads(tool_message["content"])["name"] == "贵州茅台"


def test_async_scoring_entries_keep_blocking_agent_off_the_loop(
    stream_env: tuple[Any, dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The async research scoring entries hand the blocking Agent run to a
    worker thread: the adapter's local asyncio.run works inside a running
    event loop with a real Agent, real runner and loopback SDK (only the
    external tool data is replaced)."""
    adapter, state = stream_env
    opinion = '{"signal":"hold","confidence":0.5,"reasoning":"工具数据充分"}'
    state["script_queue"] = [
        [
            {"delta": {"tool_calls": [{
                "index": 0, "id": "call_sc", "type": "function",
                "function": {"name": "get_stock_info", "arguments": '{"stock_code":"600519"}'},
            }]}, "finish": "tool_calls"},
            {"empty_choices": True, "usage": {"prompt_tokens": 6, "completion_tokens": 2, "total_tokens": 8}},
        ],
        [
            {"delta": {"role": "assistant", "content": opinion}, "finish": "stop"},
            {"empty_choices": True, "usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13}},
        ],
    ]
    from src.agent.agents import SupplyChainAgent
    from src.agent.factory import get_tool_registry
    from src.core.research_run import ResearchRun
    from src.services.research_scoring_service import ResearchScoringService

    service = ResearchScoringService.__new__(ResearchScoringService)
    service._agents_initialized = True
    registry = get_tool_registry()
    # External tool data only: the Agent, runner, admission and adapter stay real.
    monkeypatch.setattr(registry, "execute", lambda name, **kwargs: {
        "name": "贵州茅台", "industry": "白酒", "source": "fixture",
    })
    service._supply_chain_agent = SupplyChainAgent(
        tool_registry=registry, llm_adapter=adapter,
    )
    service._value_agent = None

    async def main() -> Any:
        # A running event loop is exactly the production context of these
        # scoring entries; before to_thread the adapter's local asyncio.run
        # raised here.
        return await service.analyze_supply_chain_agent("600519", "贵州茅台")

    run = ResearchRun(30)
    with run.bind(), attempt_scope(ENTRY_AGENT, "scoring"):
        result = asyncio.run(main())
    assert result["success"] is True, result
    assert result["signal"] == "hold"
    assert state["count"] == 2
