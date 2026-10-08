"""Exercise missing-field selection and shared deadlines through real manager/source paths.
Run: python -m pytest tests/test_financial_missing_fields.py
Requires project dependencies; provider transports and owned caches are isolated.
"""
from types import SimpleNamespace
from typing import Any
from time import monotonic
import time
import asyncio
import json
from unittest.mock import Mock

import pytest

from data_provider.base import DataFetcherManager
from data_provider.tushare_ifind_fundamental_adapter import TushareIfindFundamentalAdapter
from data_provider.cross_source_validator import AnchorReading, adopted_field_record
from src.config import Config


def record(field: str, value: float, period: str | None = None) -> dict:
    caliber = {"roe": "weighted_roe", "gross_margin": "gross_margin", "revenue_yoy": "operating_revenue_yoy", "net_profit_yoy": "parent_net_profit_yoy"}[field]
    return adopted_field_record(field, AnchorReading("ifind", value, caliber=caliber, unit="percentage_point", period=period))


def manager(monkeypatch: pytest.MonkeyPatch, growth: dict, earnings: dict | None = None) -> DataFetcherManager:
    cfg = Config(fundamental_cache_ttl_seconds=0, deep_research_cross_validate=False)
    monkeypatch.setattr("src.config.get_config", lambda: cfg)
    mgr = DataFetcherManager(fetchers=[])
    monkeypatch.setattr(mgr, "get_realtime_quote", lambda code: None)
    monkeypatch.setattr(mgr._fundamental_adapter, "get_fundamental_bundle", lambda code: {"growth": growth, "earnings": earnings or {"dividend": {"cash": 1}}, "institution": {"top10_holder_change": 0}, "status": "partial"})
    monkeypatch.setattr(mgr, "get_capital_flow_context", lambda *args, **kwargs: {})
    monkeypatch.setattr(mgr, "get_belong_boards", lambda *args, **kwargs: [])
    monkeypatch.setattr(mgr, "get_sector_rankings", lambda *args, **kwargs: {})
    monkeypatch.setattr(mgr, "get_dragon_tiger_context", lambda *args, **kwargs: {})
    monkeypatch.setattr(mgr._fundamental_adapter, "get_dragon_tiger_flag", lambda *args, **kwargs: {})
    monkeypatch.setattr(mgr, "get_board_context", lambda *args, **kwargs: {})
    mgr._mx_source = SimpleNamespace(available=False)
    return mgr


def test_partial_growth_still_uses_existing_fallback_when_cv_off(monkeypatch: pytest.MonkeyPatch) -> None:
    growth = {"revenue_yoy": 10.0, "field_meta": {"revenue_yoy": record("revenue_yoy", 10)}}
    mgr = manager(monkeypatch, growth)
    fallback = Mock(return_value={"growth": {"roe": 18, "gross_margin": 40, "field_meta": {"roe": record("roe", 18, "2025年报"), "gross_margin": record("gross_margin", 40)}}})
    mgr._tushare_ifind_adapter = SimpleNamespace(available=True, get_fundamental_bundle=fallback)
    result = mgr.get_fundamental_context("600519")
    assert result["growth"]["data"]["roe"] == 18
    assert result["growth"]["data"]["revenue_yoy"] == 10
    assert result["growth"]["field_meta"]["roe"]["period"] == "2025-12-31"
    assert result["growth"]["field_meta"]["revenue_yoy"]["period"] is None
    fallback.assert_called_once()


def test_acquired_primary_earnings_roe_precedes_fallback_and_needs_no_roe_request(monkeypatch: pytest.MonkeyPatch) -> None:
    growth = {key: value for key, value in [("revenue_yoy", 10.0), ("net_profit_yoy", 20.0), ("gross_margin", 40.0)]}
    earnings = {"financial_report": {"roe": 18.0, "field_meta": {"roe": record("roe", 18, "2025年报")}}, "dividend": {"cash": 1}}
    mgr = manager(monkeypatch, growth, earnings)
    mgr._fundamental_adapter.get_fundamental_bundle = lambda code: {"growth": growth, "earnings": earnings, "institution": {"top10_holder_change": 0}, "status": "partial"}
    fallback = Mock(return_value={"growth": {"roe": 14, "field_meta": {"roe": record("roe", 14, "2025年报")}}})
    mgr._tushare_ifind_adapter = SimpleNamespace(available=True, get_fundamental_bundle=fallback)
    result = mgr.get_fundamental_context("600519")
    assert result["growth"]["data"]["roe"] == 18
    fallback.assert_not_called()


def test_expired_mx_deadline_does_not_make_transport_request(monkeypatch: pytest.MonkeyPatch) -> None:
    from data_provider.mx_data_adapter import MXClient, MXSource
    transport = Mock(side_effect=AssertionError("Expired deadline must not call transport"))
    monkeypatch.setattr("requests.post", transport)
    source = MXSource(MXClient(api_key="fixture", timeout=8))
    assert source.read("600519", "roe", deadline=monotonic() - 1) is None
    transport.assert_not_called()


def test_real_mx_transport_timeout_and_retry_share_remaining_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    import time
    import requests
    from data_provider.mx_data_adapter import MXClient, MXSource
    timeouts: list[float] = []
    def transport(*args, **kwargs):
        timeouts.append(kwargs["timeout"])
        time.sleep(kwargs["timeout"] + 0.005)
        raise requests.ReadTimeout("fixture")
    monkeypatch.setattr(requests, "post", transport)
    source = MXSource(MXClient(api_key="fixture", timeout=8))
    start = monotonic()
    assert source.read("600519", "roe", deadline=start + 0.02) is None
    assert len(timeouts) == 1 and 0 < timeouts[0] <= 0.02
    assert monotonic() - start < 0.2


def test_zero_manager_budget_does_not_start_fallback_or_mx(monkeypatch: pytest.MonkeyPatch) -> None:
    mgr = manager(monkeypatch, {})
    fallback = Mock(side_effect=AssertionError("zero budget"))
    mx = Mock(side_effect=AssertionError("zero budget"))
    mgr._tushare_ifind_adapter = SimpleNamespace(available=True, get_fundamental_bundle=fallback)
    mgr._mx_source = SimpleNamespace(available=True, _client=SimpleNamespace(query_financials=mx))
    result = mgr.get_fundamental_context("600519", budget_seconds=0)
    assert result["growth"]["data"] == {}
    fallback.assert_not_called()
    mx.assert_not_called()


def test_acquired_earnings_keeps_zero_and_rejects_invalid_stale_conflicting_candidates() -> None:
    growth = {"revenue_yoy": 10.0}
    candidates = {"roe": 0.0, "gross_margin": 40.0, "net_profit_yoy": 99.0,
                  "field_meta": {"roe": record("roe", 0), "gross_margin": record("gross_margin", 40, "2020年报"), "net_profit_yoy": {**record("net_profit_yoy", 99), "quality": {"status": "conflict"}}}}
    DataFetcherManager._fill_growth_fields(growth, candidates, "primary_earnings")
    assert growth["roe"] == 0 and growth["revenue_yoy"] == 10
    assert growth.get("gross_margin") is None and growth.get("net_profit_yoy") is None


def test_concurrent_first_http_budget_installation_delegates_once(monkeypatch: pytest.MonkeyPatch) -> None:
    import requests
    from threading import Event, Thread, current_thread
    from src.patches import eastmoney_patch as patch_module

    first_read, other_done = Event(), Event()
    calls: list[float] = []
    results: list[tuple[Any, Any, int]] = []
    def transport(session: requests.Session, method: str, url: str, **kwargs: Any) -> requests.Response:
        calls.append(kwargs["timeout"])
        response = requests.Response()
        response.status_code, response._content = 200, b"{}"
        return response
    monkeypatch.setattr(requests.Session, "request", transport)
    monkeypatch.setattr(patch_module._patch_sign, "patched", False)
    monkeypatch.setattr(patch_module, "_auth_patch_enabled", False)
    monkeypatch.setattr(patch_module, "original_request", transport)
    real_is_patched = patch_module._patch_sign.is_patched
    def scheduled_read() -> bool:
        result = real_is_patched()
        if current_thread().name == "paused-installer" and not result:
            first_read.set()
            other_done.wait(0.1)
        return result
    monkeypatch.setattr(patch_module._patch_sign, "is_patched", scheduled_read)
    mgr = manager(monkeypatch, {})
    def install(name: str) -> None:
        try:
            results.append(mgr._run_with_timeout(lambda: requests.get("https://fixture.invalid/financial", timeout=17), 0.03, name))
        finally:
            if name == "other":
                other_done.set()
    paused = Thread(target=install, args=("paused",), name="paused-installer")
    other = Thread(target=install, args=("other",))
    paused.start()
    assert first_read.wait(1)
    other.start()
    paused.join(2)
    other.join(2)
    assert not paused.is_alive() and not other.is_alive()
    assert len(results) == 2 and all(error is None for _, error, _ in results)
    assert len(calls) == 2 and all(0 < value <= 0.03 for value in calls)
    installed = requests.Session.request
    patch_module.eastmoney_patch()
    assert patch_module._auth_patch_enabled and requests.Session.request is installed


def test_legacy_sdk_request_gets_task_local_timeout_without_changing_other_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    import requests
    from src.patches import eastmoney_patch as patch_module
    from src.patches.eastmoney_patch import request_deadline
    seen: list[tuple[float | None, float | None]] = []
    def transport(session, method, url, **kwargs):
        seen.append((request_deadline.get(), kwargs.get("timeout")))
        response = requests.Response()
        response.status_code = 200
        response._content = b"{}"
        return response
    monkeypatch.setattr(requests.Session, "request", transport)
    monkeypatch.setattr(patch_module._patch_sign, "patched", False)
    monkeypatch.setattr(patch_module, "_auth_patch_enabled", False)
    monkeypatch.setattr(patch_module, "original_request", patch_module.original_request)
    patch_module.eastmoney_patch(budget_only=True)
    mgr = manager(monkeypatch, {})
    result, error, _ = mgr._run_with_timeout(lambda: requests.get("https://sdk.example/financial", timeout=100), 0.03, "fixture")
    assert result is not None and error is None
    assert seen[0][0] is not None and seen[0][1] is not None and 0 < seen[0][1] <= 0.03
    requests.get("https://sdk.example/unrelated", timeout=17)
    assert seen[1] == (None, 17) and request_deadline.get() is None


@pytest.mark.parametrize("modern", [False, True])
@pytest.mark.parametrize("module_name,class_name", [("ifind_fundamental_adapter", "IfindFetcher"), ("mx_mcp_adapter", "MxMcpFetcher")])
def test_actual_mcp_session_uses_remaining_timeout_and_cancels_slow_tool(monkeypatch: pytest.MonkeyPatch, module_name: str, class_name: str, modern: bool) -> None:
    import asyncio
    import importlib
    from contextlib import asynccontextmanager
    import mcp
    from mcp.client import streamable_http
    timeouts: list[float] = []
    closed: list[bool] = []
    @asynccontextmanager
    async def stream(url, headers=None, timeout=30):
        timeouts.append(timeout)
        try:
            yield (None, None, None)
        finally:
            closed.append(True)
    class Session:
        def __init__(self, *args):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def initialize(self):
            pass
        async def call_tool(self, *args):
            await asyncio.sleep(1)
    monkeypatch.setattr(streamable_http, "streamablehttp_client", stream, raising=False)
    if modern:
        @asynccontextmanager
        async def modern_stream(url, *, http_client=None):
            timeouts.append(http_client.timeout.read)
            try:
                yield (None, None)
            finally:
                closed.append(True)
        monkeypatch.setattr(streamable_http, "streamable_http_client", modern_stream, raising=False)
    else:
        monkeypatch.setattr(streamable_http, "streamable_http_client", stream, raising=False)
    monkeypatch.setattr(mcp, "ClientSession", Session)
    module = importlib.import_module(f"data_provider.{module_name}")
    kwargs = {"token": "fixture"} if class_name == "IfindFetcher" else {"api_key": "fixture"}
    fetcher = getattr(module, class_name)(endpoint="https://mcp.example", timeout_seconds=8, **kwargs)
    start = monotonic()
    assert fetcher.fetch("600519", "roe", "2025年报", deadline=start + 0.02) is None
    assert timeouts and 0 < timeouts[0] <= 0.02 and closed
    assert monotonic() - start < 0.3


def test_slow_validation_returns_completed_evidence_and_bounds_repeated_workers() -> None:
    import time
    from threading import Event, Lock
    from data_provider.cross_source_validator import CrossSourceValidator
    active = 0
    maximum = 0
    lock = Lock()
    release = Event()
    class Slow:
        name = "mx"
        def read(self, *args):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                release.wait(0.5)
                return None
            finally:
                with lock:
                    active -= 1
    fast = SimpleNamespace(name="ifind", read=lambda *args: AnchorReading("ifind", 18, caliber="weighted_roe", unit="percentage_point", period="2025年报"))
    validator = CrossSourceValidator([fast, Slow()], max_workers=4)
    start = monotonic()
    first = validator.verify("600519", "roe", deadline=start + 0.02)
    assert first.value == 18 and ("mx", "timeout") in first.source_errors
    for _ in range(8):
        validator.verify("600519", "roe", deadline=monotonic() + 0.005)
    assert maximum <= 4 and monotonic() - start < 0.3
    release.set()
    time.sleep(0.01)


def install_real_mcp_mock_http(monkeypatch, *, slow_tool=False, slow_delete=False):
    httpx2 = pytest.importorskip("httpx2")
    import mcp.shared._httpx_utils as helper
    calls = []
    async def transport(request):
        calls.append((request.method, time.monotonic(), request.extensions.get("timeout")))
        if request.method == "DELETE":
            if slow_delete:
                await asyncio.sleep(request.extensions["timeout"]["read"])
                raise httpx2.ReadTimeout("fixture timeout")
            return httpx2.Response(204)
        if request.method == "GET":
            return httpx2.Response(405)
        body = json.loads(request.content)
        if "id" not in body:
            return httpx2.Response(202)
        if body["method"] == "initialize":
            result = {"protocolVersion":"2025-11-25", "capabilities":{"tools":{}}, "serverInfo":{"name":"fixture","version":"1"}}
        elif body["method"] == "tools/list":
            result = {"tools":[{"name":"get_stock_financials", "inputSchema":{"type":"object"}}]}
        else:
            if slow_tool:
                calls.append(("tools/call", time.monotonic(), request.extensions.get("timeout")))
                try:
                    await asyncio.sleep(0.3)
                except asyncio.CancelledError:
                    calls.append(("tools/cancelled", time.monotonic(), request.extensions.get("timeout")))
                    raise
            raw = json.dumps({"data":{"answer":"|报告期|净资产收益率ROE(加权,公布值)（单位：%）|\n|---|---|\n|2025年报|18%|"}})
            result = {"content":[{"type":"text","text":raw}]}
        return httpx2.Response(200, headers={"content-type":"application/json", "mcp-session-id":"fixture-session"}, json={"jsonrpc":"2.0","id":body["id"],"result":result})
    def create(headers=None, **kwargs):
        return httpx2.AsyncClient(headers=headers, transport=httpx2.MockTransport(transport))
    monkeypatch.setattr(helper, "create_mcp_http_client", create)
    return calls


def test_ifind_requested_period_hint_keeps_actual_other_period(monkeypatch):
    from data_provider.ifind_fundamental_adapter import IfindFetcher, IfindSource
    install_real_mcp_mock_http(monkeypatch)
    adapter = TushareIfindFundamentalAdapter(ifind_source=IfindSource(IfindFetcher(endpoint="https://fixture.invalid", token="fixture", timeout_seconds=1)))
    try:
        result = adapter.get_fundamental_bundle("600519", "2026中报", fields=("roe",), include_institution=False, deadline=time.monotonic()+1)
        assert result["growth"].get("roe") == 18
        assert result["growth"]["field_meta"]["roe"]["period"] == "2025-12-31"
    finally:
        adapter._pool.shutdown(wait=True)


@pytest.mark.parametrize("module_name,class_name", [("ifind_fundamental_adapter","IfindFetcher"),("mx_mcp_adapter","MxMcpFetcher")])
def test_real_sdk_does_not_start_cleanup_io_past_deadline(monkeypatch, module_name, class_name):
    import importlib
    calls = install_real_mcp_mock_http(monkeypatch, slow_tool=True, slow_delete=True)
    cls = getattr(importlib.import_module("data_provider."+module_name), class_name)
    fetcher = cls(endpoint="https://fixture.invalid", timeout_seconds=1, **({"token":"fixture"} if class_name == "IfindFetcher" else {"api_key":"fixture"}))
    start = time.monotonic()
    result = fetcher.fetch("600519", "roe", "2025年报", deadline=start+0.03)
    elapsed = time.monotonic()-start
    assert result is None
    assert 0.025 <= elapsed < 0.15
    assert [call for call in calls if call[0] == "tools/call"]
    assert [call for call in calls if call[0] == "tools/cancelled"]
    assert len([call for call in calls if call[0] == "POST"]) >= 3
    assert not [call for call in calls if call[0] == "DELETE" and call[1] >= start+0.03]



def test_stale_mx_candidate_does_not_occupy_missing_field(monkeypatch: pytest.MonkeyPatch) -> None:
    import requests
    from data_provider.mx_data_adapter import MXClient, MXSource
    mgr = manager(monkeypatch, {"revenue_yoy": 10, "net_profit_yoy": 20, "gross_margin": 40})
    mgr._tushare_ifind_adapter = SimpleNamespace(available=False)
    mgr._mx_source = MXSource(MXClient(api_key="fixture", ttl=0))
    body = {"data": {"data": {"searchDataResultDTO": {"dataTableDTOList": [{"nameMap": {"roe": "加权净资产收益率(%)"}, "table": {"headName": ["2020年报"], "roe": ["18%"]}}]}}}}
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: SimpleNamespace(status_code=200, json=lambda: body))
    result = mgr.get_fundamental_context("600519", budget_seconds=1)
    assert result["growth"]["data"].get("roe") is None


def test_repeated_slow_fallback_does_not_accumulate_queue_items(monkeypatch: pytest.MonkeyPatch) -> None:
    from threading import Event
    release = Event()
    source = SimpleNamespace(available=True, read=lambda *args, **kwargs: (release.wait(0.5), None)[1])
    adapter = TushareIfindFundamentalAdapter(ifind_source=source)
    mgr = manager(monkeypatch, {"revenue_yoy": 10})
    mgr._tushare_ifind_adapter = adapter
    try:
        for _ in range(25):
            mgr.get_fundamental_context("600519", budget_seconds=0.004)
        assert adapter._pool._work_queue.qsize() <= 4
    finally:
        release.set()
        adapter._pool.shutdown(wait=True)
