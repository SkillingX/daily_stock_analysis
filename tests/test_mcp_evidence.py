# -*- coding: utf-8 -*-
"""Issue #51 evidence through production call_mcp_tool and IfindFetcher.query.

A local MCPServer speaks streamable HTTP on an OS-selected port. Tests call
the production functions; they do not replay the protocol with a bare client.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import logging
import socket
import threading
from time import monotonic

import pytest

from data_provider.cross_source_validator import SourceReadError, source_failure_text
from data_provider.ifind_fundamental_adapter import IfindFetcher, _ifind_data
from src.services import mcp_client


def _records() -> list[str]:
    found: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            found.append(record.getMessage())

    handler = Collect()
    mcp_client.logger.setLevel(logging.INFO)
    mcp_client.logger.addHandler(handler)
    return found, handler


def _payloads(lines: list[str]) -> list[dict]:
    payloads = []
    for line in lines:
        if "[MCP] " not in line:
            continue
        payloads.append(json.loads(line.split("[MCP] ", 1)[1]))
    return payloads


def _ends(lines: list[str], call_id: str | None = None) -> list[dict]:
    ends = [item for item in _payloads(lines) if item.get("phase") == "end"]
    if call_id is None:
        return ends
    return [item for item in ends if item.get("call_id") == call_id]


@contextmanager
def local_mcp(tool, *, http_status: int | None = None):
    """Real TCP MCP server. GET returns 405; POST is the MCP session."""
    from mcp.server.mcpserver import MCPServer
    import uvicorn

    methods: list[str] = []
    invocations: list[str] = []
    server_app = MCPServer("issue-51-fixture", log_level="ERROR")

    @server_app.tool(name="get_stock_info", structured_output=True)
    async def info(query: str) -> dict[str, object]:
        import asyncio
        invocations.append("get_stock_info")
        result = tool(query)
        if asyncio.iscoroutine(result):
            result = await result
        if not isinstance(result, dict):
            raise TypeError("structured tool must return a dict")
        return result

    application = server_app.streamable_http_app(json_response=True)

    async def app(scope, receive, send):
        if scope["type"] != "http":
            await application(scope, receive, send)
            return
        methods.append(scope["method"])
        if scope["method"] == "GET":
            await send({"type": "http.response.start", "status": 405, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        if scope["method"] == "POST" and http_status is not None:
            body = b""
            while True:
                message = await receive()
                if message["type"] != "http.request":
                    continue
                body += message.get("body") or b""
                if not message.get("more_body"):
                    break
            if b"tools/call" in body:
                await send({"type": "http.response.start", "status": http_status, "headers": [
                    (b"content-type", b"text/plain"), (b"request-id", b"rid-429"),
                ]})
                await send({"type": "http.response.body", "body": b""})
                return

            sent = False

            async def replay():
                nonlocal sent
                if sent:
                    return {"type": "http.disconnect"}
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}

            await application(scope, replay, send)
            return
        await application(scope, receive, send)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(app, host="127.0.0.1", log_level="error", access_log=False, timeout_graceful_shutdown=1)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    started = monotonic()
    while not server.started and monotonic() - started < 3:
        threading.Event().wait(0.02)
    if not server.started:
        raise RuntimeError("local MCP server did not start")
    try:
        yield f"http://127.0.0.1:{port}/mcp", methods, invocations
    finally:
        server.should_exit = True
        thread.join(3)
        sock.close()


def _envelope(msg: str, *, code: int = 1, sub_code: int = 0, answer: str = "ok") -> dict[str, object]:
    return {"code": code, "subCode": sub_code, "msg": msg, "subMsg": "fixture detail", "data": {"answer": answer}}


class TestProductionMcpTransport:
    def test_get_405_post_success_redacts_known_credential_and_keeps_subcode(self) -> None:
        secret = "fixture-native-secret-value"
        lines, handler = _records()
        try:
            with local_mcp(lambda query: _envelope(secret, sub_code=7, answer="|证券代码|所属概念|\n|---|---|\n|300260.SZ|芯片概念|\n")) as (endpoint, methods, tools):
                data = IfindFetcher(endpoint, secret, 5).query("get_stock_info", "300260.SZ 所属概念", deadline=monotonic() + 5)
        finally:
            mcp_client.logger.removeHandler(handler)
        assert data.answer.startswith("|证券代码|")
        assert methods.count("GET") >= 1 and methods.count("POST") >= 1
        assert tools == ["get_stock_info"]
        ends = _ends(lines)
        assert len(ends) == 1 and ends[0]["dispatched"] is True
        assert 405 in [item["status"] for item in ends[0]["http"]]
        assert 200 in [item["status"] for item in ends[0]["http"]]
        assert ends[0]["business_sub_code"] == 7
        assert secret not in "".join(lines)
        assert "[REDACTED]" in "".join(lines)

    def test_explicit_permission_denial_is_not_authentication(self) -> None:
        with pytest.raises(SourceReadError) as denied:
            _ifind_data({"code": 0, "msg": "权限不足", "data": {"answer": ""}})
        with pytest.raises(SourceReadError) as unauthenticated:
            _ifind_data({"code": 0, "msg": "未认证", "data": {"answer": ""}})
        with pytest.raises(SourceReadError) as throttled:
            _ifind_data({"code": 0, "msg": "请求限流", "data": {"answer": ""}})
        with pytest.raises(SourceReadError) as quota:
            _ifind_data({"code": 0, "msg": "额度耗尽", "data": {"answer": ""}})
        assert denied.value.reason_code == "permission_denied"
        assert unauthenticated.value.reason_code == "authentication_failed"
        assert throttled.value.reason_code == "rate_limited"
        assert quota.value.reason_code == "quota_exhausted"
        phrases = [source_failure_text(item.value.reason_code) for item in (denied, unauthenticated, throttled, quota)]
        assert len(set(phrases)) == 4

    def test_business_answer_text_is_not_a_supplier_failure(self) -> None:
        # 业务表值（受限流通股）包含“限流”子串，成功回包的表格内容不是故障文本；
        # 表格之外的散文拒绝文案（如用量已耗尽）仍要按供应商故障分类。
        data = _ifind_data({"code": 1, "msg": "success", "data": {"answer": "|证券代码|股东持股股份性质|\n|---|---|\n|300260.SZ|受限流通股,流通A股|\n"}})
        assert "受限流通股" in data.answer
        with pytest.raises(SourceReadError) as refused:
            _ifind_data({"code": 1, "msg": "success", "data": {"answer": "当前账户MCP请求用量已耗尽，可以升级权益。"}})
        assert refused.value.reason_code == "quota_exhausted"
        with pytest.raises(SourceReadError) as throttled:
            _ifind_data({"code": 0, "msg": "接口限流，请稍后再试", "data": {"answer": ""}})
        assert throttled.value.reason_code == "rate_limited"

    def test_submsg_is_classified_and_numeric_subcode_is_not(self) -> None:
        cases = {
            "权限不足": "permission_denied",
            "额度耗尽": "quota_exhausted",
            "请求限流": "rate_limited",
            "Token已过期": "authentication_failed",
        }
        for sub_msg, reason in cases.items():
            with pytest.raises(SourceReadError) as caught:
                _ifind_data({"code": 0, "msg": "error", "subMsg": sub_msg, "data": {"answer": ""}})
            assert caught.value.reason_code == reason
        with pytest.raises(SourceReadError) as snake:
            _ifind_data({"code": 0, "msg": "error", "sub_msg": "权限不足", "data": {"answer": ""}})
        assert snake.value.reason_code == "permission_denied"
        with pytest.raises(SourceReadError) as numeric:
            _ifind_data({"code": 0, "msg": "error", "subCode": 403, "data": {"answer": ""}})
        assert numeric.value.reason_code == "business_error"

    def test_http_429_is_not_quota_and_explicit_quota_stays_quota(self) -> None:
        lines, handler = _records()
        try:
            with local_mcp(lambda query: _envelope("ok"), http_status=429) as (endpoint, methods, tools):
                with pytest.raises(SourceReadError) as limited:
                    IfindFetcher(endpoint, "fixture-token-value", 5).query("get_stock_info", "q", deadline=monotonic() + 5)
            with local_mcp(lambda query: _envelope("额度耗尽", code=0, sub_code=11)) as (endpoint, _methods, quota_tools):
                with pytest.raises(SourceReadError) as exhausted:
                    IfindFetcher(endpoint, "fixture-token-value", 5).query("get_stock_info", "q", deadline=monotonic() + 5)
        finally:
            mcp_client.logger.removeHandler(handler)
        assert limited.value.reason_code == "rate_limited"
        assert exhausted.value.reason_code == "quota_exhausted"
        assert tools == []  # HTTP 429 rejects the tools/call before the tool body
        assert quota_tools == ["get_stock_info"]
        assert any(item.get("dispatched") is True and any(entry.get("status") == 429 for entry in item.get("http") or []) for item in _ends(lines))
        http_statuses = [item["status"] for end in _ends(lines) for item in (end.get("http") or [])]
        assert 429 in http_statuses
        assert "quota_exhausted" not in limited.value.reason_code

    def test_is_error_text_envelope_is_classified_not_collapsed(self) -> None:
        from mcp.server.mcpserver.exceptions import ToolError

        envelope = json.dumps({"code": 0, "subCode": 403, "msg": "权限不足", "subMsg": "no entitlement", "data": {"answer": ""}})
        lines, handler = _records()

        def boom(query: str) -> dict[str, object]:
            raise ToolError(envelope)

        try:
            with local_mcp(boom) as (endpoint, _methods, tools):
                with pytest.raises(SourceReadError) as caught:
                    IfindFetcher(endpoint, "fixture-token-value", 5).query("get_stock_info", "q", deadline=monotonic() + 5)
        finally:
            mcp_client.logger.removeHandler(handler)
        assert caught.value.reason_code == "permission_denied"
        assert tools == ["get_stock_info"]
        end = _ends(lines)[-1]
        assert end["mcp_is_error"] is True
        assert end["business_code"] == 0
        assert end["business_sub_code"] == 403
        assert "权限不足" in end["business_msg"]

    def test_empty_bad_field_and_wrong_security_stay_distinct(self) -> None:
        tables = {
            "empty": "|证券代码|所属概念|\n|---|---|\n|300260.SZ| |\n",
            "broken": "|证券代码|证券简称|\n|---|---|\n|300260.SZ|新莱应材|\n",
            "mismatch": "|证券代码|所属概念|\n|---|---|\n|600519.SH|白酒概念|\n",
        }
        seen: dict[str, list[str]] = {}
        for name, answer in tables.items():
            with local_mcp(lambda query, answer=answer: _envelope("success", answer=answer)) as (endpoint, _methods, tools):
                membership = IfindFetcher(endpoint, "fixture-token-value", 5).concept_membership("300260", deadline=monotonic() + 5)
            seen[name] = membership.errors
            assert tools == ["get_stock_info"]
        assert seen == {"empty": ["no_concepts"], "broken": ["field_error"], "mismatch": ["security_mismatch"]}

    def test_expired_deadline_and_cancellation_record_evidence_without_dispatch(self) -> None:
        from src.core.research_run import ResearchRun, ResearchStopped

        lines, handler = _records()
        try:
            with local_mcp(lambda query: _envelope("should-not-run")) as (endpoint, _methods, tools):
                fetcher = IfindFetcher(endpoint, "fixture-token-value", 5)
                with pytest.raises(SourceReadError) as expired:
                    fetcher.query("get_stock_info", "q", deadline=monotonic() - 1)
                run = ResearchRun(10)
                run.stop("cancelled")
                with run.bind(), pytest.raises(ResearchStopped):
                    fetcher.query("get_stock_info", "q", deadline=monotonic() + 5)
        finally:
            mcp_client.logger.removeHandler(handler)
        assert expired.value.reason_code == "timeout"
        assert tools == []
        ends = _ends(lines)
        assert len(ends) == 2
        assert all(item["dispatched"] is False for item in ends)
        assert all(item["phase"] == "end" for item in ends)
        starts = [item for item in _payloads(lines) if item.get("phase") == "start"]
        assert {item["call_id"] for item in starts} == {item["call_id"] for item in ends}

    def test_actual_request_timeout_is_not_replaced_by_close_noise(self) -> None:
        import asyncio

        async def slow(query: str) -> dict[str, object]:
            await asyncio.sleep(2)
            return _envelope("late")

        lines, handler = _records()
        try:
            with local_mcp(slow) as (endpoint, _methods, tools):
                started = monotonic()
                with pytest.raises(SourceReadError) as caught:
                    IfindFetcher(endpoint, "fixture-token-value", 5).query("get_stock_info", "q", deadline=monotonic() + 0.3)
                elapsed = monotonic() - started
        finally:
            mcp_client.logger.removeHandler(handler)
        assert caught.value.reason_code == "timeout"
        assert elapsed < 1.0
        assert tools == ["get_stock_info"]
        ends = [item for item in _ends(lines) if item.get("status") == "error"]
        assert ends and ends[-1]["dispatched"] is True
        assert "session_close_noise" not in ends[-1].get("error_kind", "")
        assert ">" in ends[-1]["exception_chain"]
        assert "Exception>BaseException" not in ends[-1]["exception_chain"]

    def test_logging_failure_does_not_change_the_business_result(self) -> None:
        class Boom(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                raise RuntimeError("log handler failed")

        handler = Boom()
        mcp_client.logger.addHandler(handler)
        mcp_client.logger.setLevel(logging.INFO)
        try:
            with local_mcp(lambda query: _envelope("success", answer="kept")) as (endpoint, _methods, tools):
                data = IfindFetcher(endpoint, "fixture-token-value", 5).query("get_stock_info", "q", deadline=monotonic() + 5)
        finally:
            mcp_client.logger.removeHandler(handler)
        assert data.answer == "kept"
        assert tools == ["get_stock_info"]

    def test_nested_transport_exception_chain_is_cause_not_mro(self, monkeypatch) -> None:
        from mcp import ClientSession

        async def raising(self, *args, **kwargs):
            try:
                raise ValueError("inner")
            except ValueError as exc:
                raise RuntimeError("outer") from exc

        monkeypatch.setattr(ClientSession, "call_tool", raising)
        lines, handler = _records()
        try:
            with local_mcp(lambda query: _envelope("ok")) as (endpoint, methods, tools):
                with pytest.raises(SourceReadError):
                    IfindFetcher(endpoint, "fixture-token-value", 5).query("get_stock_info", "q", deadline=monotonic() + 5)
        finally:
            mcp_client.logger.removeHandler(handler)
        chain = _ends(lines)[-1]["exception_chain"]
        assert chain == "RuntimeError>ValueError"
        assert "Exception>BaseException" not in chain
        assert tools == []
        assert "POST" in methods  # initialize still used the real transport

    def test_exception_group_is_part_of_the_recorded_chain(self, monkeypatch) -> None:
        from mcp import ClientSession

        async def grouped(self, *args, **kwargs):
            raise ExceptionGroup("transport", [ValueError("inner")])

        monkeypatch.setattr(ClientSession, "call_tool", grouped)
        lines, handler = _records()
        try:
            with local_mcp(lambda query: _envelope("ok")) as (endpoint, methods, _tools):
                with pytest.raises(SourceReadError):
                    IfindFetcher(endpoint, "fixture-token-value", 5).query("get_stock_info", "q", deadline=monotonic() + 5)
        finally:
            mcp_client.logger.removeHandler(handler)
        chain = _ends(lines)[-1]["exception_chain"]
        assert chain.startswith("ExceptionGroup>ValueError")
        assert "Exception>BaseException" not in chain
        assert "POST" in methods

    def test_malformed_code_object_is_not_dumped_into_evidence(self) -> None:
        body = "fixture-body-" * 1000
        lines, handler = _records()
        try:
            with local_mcp(lambda query: {"code": {"unexpected_payload": body}, "msg": "bad", "data": {"answer": ""}}) as (endpoint, _methods, tools):
                with pytest.raises(SourceReadError) as caught:
                    IfindFetcher(endpoint, "fixture-token-value", 5).query("get_stock_info", "q", deadline=monotonic() + 5)
        finally:
            mcp_client.logger.removeHandler(handler)
        assert caught.value.reason_code == "invalid_response"
        assert tools == ["get_stock_info"]
        blob = "".join(lines)
        assert body not in blob and "unexpected_payload" not in blob
        assert all(len(line) <= 2000 for line in lines)
        assert _ends(lines)[-1]["business_code"] == "dict"



def test_expired_zhipu_search_records_undispatched_timeout() -> None:
    """An already-expired Zhipu deadline still records one undispatched attempt."""
    from src.patches.eastmoney_patch import request_deadline
    from src.search_service import ZhipuSearchProvider

    lines, handler = _records()
    try:
        with local_mcp(lambda query: {"code": 1, "msg": "success", "data": {"answer": "unused"}}) as (endpoint, methods, tools):
            provider = ZhipuSearchProvider(["fixture-token-value"])
            provider.API_ENDPOINT = endpoint
            token = request_deadline.set(monotonic() - 1)
            try:
                result = provider._do_search("300260", "fixture-token-value", 3)
            finally:
                request_deadline.reset(token)
    finally:
        mcp_client.logger.removeHandler(handler)
    assert result.success is False
    assert result.error_message == "zhipu:timeout"
    assert methods == [] and tools == []
    ends = _ends(lines)
    starts = [item for item in _payloads(lines) if item.get("phase") == "start"]
    assert len(starts) == 1 and starts[0]["dispatched"] is False
    assert len(ends) == 1 and ends[0]["dispatched"] is False
    assert ends[0]["call_id"] == starts[0]["call_id"]
    assert "fixture-token-value" not in "".join(lines)
