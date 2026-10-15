"""One bounded HTTP MCP call, shared by financial data and Coding Plan search."""
from __future__ import annotations

from contextlib import AsyncExitStack
from functools import partial
from inspect import signature
from time import monotonic
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from mcp.types import CallToolResult


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


async def call_mcp_tool(endpoint: str, authorization: str, tool: str, arguments: dict[str, object], *, timeout_seconds: float, deadline: float | None = None) -> CallToolResult:
    """No retries or background session; callers own response validation and errors."""
    from mcp import ClientSession
    from mcp.client import streamable_http
    from src.core.research_run import current_research
    run = current_research.get()
    if run is not None:
        run.check()

    transport = getattr(streamable_http, "streamable_http_client", None) or getattr(streamable_http, "streamablehttp_client")
    limit = min(deadline, monotonic() + timeout_seconds) if deadline is not None else monotonic() + timeout_seconds
    remaining = limit - monotonic()
    if remaining <= 0:
        raise TimeoutError("MCP deadline exhausted")
    async with AsyncExitStack() as stack:
        if "http_client" in signature(transport).parameters:
            from mcp.shared._httpx_utils import create_mcp_http_client
            client = create_mcp_http_client(headers={"Authorization": authorization})
            client.timeout = remaining
            client.event_hooks["request"].append(partial(apply_mcp_request_deadline, deadline=limit))
            await stack.enter_async_context(client)
            streams = await stack.enter_async_context(transport(endpoint, http_client=client, terminate_on_close=False))
        else:
            streams = await stack.enter_async_context(transport(endpoint, headers={"Authorization": authorization}, timeout=remaining, terminate_on_close=False))
        session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
        await session.initialize()
        if run is not None:
            run.check()
        return await session.call_tool(tool, arguments)
