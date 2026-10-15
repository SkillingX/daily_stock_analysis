"""MCP cancellation is checked after initialization and at HTTP dispatch."""
import asyncio
from contextlib import asynccontextmanager
from time import monotonic

import httpx
import pytest

from src.core.research_run import ResearchRun, ResearchStopped
from src.services.mcp_client import apply_mcp_request_deadline, call_mcp_tool


@pytest.mark.parametrize("modern", [False, True])
def test_cancellation_during_mcp_initialize_does_not_call_tool_and_closes_session(monkeypatch, modern):
    import mcp
    from mcp.client import streamable_http
    run = ResearchRun(5)
    operations = []

    @asynccontextmanager
    async def legacy_transport(endpoint, *, headers, timeout, terminate_on_close):
        operations.append("transport_open")
        try:
            yield (None, None, None)
        finally:
            operations.append("transport_close")

    @asynccontextmanager
    async def modern_transport(endpoint, *, http_client, terminate_on_close):
        operations.append("transport_open")
        assert http_client.event_hooks["request"]
        try:
            yield (None, None, None)
        finally:
            operations.append("transport_close")

    class Session:
        def __init__(self, *args):
            pass

        async def __aenter__(self):
            operations.append("session_open")
            return self

        async def __aexit__(self, *args):
            operations.append("session_close")

        async def initialize(self):
            operations.append("initialize")
            run.stop("cancelled")

        async def call_tool(self, *args):
            pytest.fail("Cancellation during initialize must not dispatch the tool")

    monkeypatch.setattr(streamable_http, "streamable_http_client", modern_transport if modern else legacy_transport, raising=False)
    monkeypatch.setattr(mcp, "ClientSession", Session)
    with run.bind(), pytest.raises(ResearchStopped):
        asyncio.run(call_mcp_tool("https://fixture.invalid/mcp", "test-only", "fixture", {}, timeout_seconds=5))
    assert operations == ["transport_open", "session_open", "initialize", "session_close", "transport_close"]


def test_mcp_http_hook_rechecks_run_before_each_dispatch():
    run = ResearchRun(5)
    request = httpx.Request("POST", "https://fixture.invalid/mcp")
    with run.bind():
        asyncio.run(apply_mcp_request_deadline(request, deadline=monotonic() + 5))
        run.stop("cancelled")
        with pytest.raises(ResearchStopped):
            asyncio.run(apply_mcp_request_deadline(request, deadline=monotonic() + 5))
