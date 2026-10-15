"""Search cancellation reaches real MCP/HTTP seams and retains prior evidence."""
import json
from datetime import date
from types import SimpleNamespace

import httpx
import pytest
import requests
from mcp.shared import _httpx_utils

from src.core.research_run import ResearchRun, ResearchStopped
from src.search_service import BaseSearchProvider, SearchResponse, SearchResult, SearchService


@pytest.mark.parametrize("entrypoint", ["news", "comprehensive"])
def test_mcp_initialize_cancellation_never_dispatches_html_fallback(monkeypatch, entrypoint):
    run = ResearchRun(10)
    methods, sent = [], []

    def wire(request):
        data = json.loads(request.content)
        methods.append(data["method"])
        if "id" not in data:
            return httpx.Response(202)
        assert data["method"] == "initialize"
        run.stop("cancelled")
        result = {"protocolVersion": data["params"]["protocolVersion"], "capabilities": {}, "serverInfo": {"name": "fixture", "version": "1"}}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": data["id"], "result": result})

    def send(self, request, **kwargs):
        sent.append(request.url)
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"data":{"list":[]}}'
        return response

    monkeypatch.setattr(_httpx_utils, "create_mcp_http_client", lambda headers: httpx.AsyncClient(headers=headers, transport=httpx.MockTransport(wire)))
    monkeypatch.setattr(requests.Session, "send", send)
    service = SearchService(coding_plan_search_provider="zhipu", zhipu_api_key="test-only", searxng_public_instances_enabled=False)
    with run.bind(), pytest.raises(ResearchStopped):
        if entrypoint == "news":
            service.search_stock_news("300260", "新莱应材")
        else:
            service.search_comprehensive_intel("300260", "新莱应材", max_searches=2)
    assert methods == ["initialize"]
    assert not sent
    assert not service._cache_inflight
    assert not service._cache
    assert service._providers[0]._key_errors["test-only"] == 0


def test_comprehensive_retains_successful_dimension_before_later_cancellation(monkeypatch):
    run = ResearchRun(5)
    calls = []

    class Provider(BaseSearchProvider):
        def _do_search(self, query, *args, **kwargs):
            calls.append(query)
            if len(calls) == 2:
                run.stop("cancelled")
                run.check()
            return SearchResponse(query, [SearchResult("新莱应材300260公告", "新莱应材披露报告", "https://example.com/notice", "example.com", date.today().isoformat())], self.name)

    service = SearchService(searxng_public_instances_enabled=False)
    provider = Provider(["test-only"], "fixture")
    service._providers = [provider]
    monkeypatch.setattr("src.search_service.time.sleep", lambda _: None)
    with run.bind(), pytest.raises(ResearchStopped):
        service.search_comprehensive_intel("300260", "新莱应材", max_searches=3)
    assert len(calls) == 2
    assert run.evidence["legacy"][0]["url"] == "https://example.com/notice"
    assert provider._key_errors["test-only"] == 0


def test_requests_dispatch_checks_cancellation_after_auth_preparation(monkeypatch):
    from src.patches import eastmoney_patch as patch
    run = ResearchRun(5)
    sent = []
    patch.eastmoney_patch(budget_only=True)
    monkeypatch.setattr(patch, "_auth_patch_enabled", True)
    monkeypatch.setattr(patch, "_get_nid", lambda *a: None)
    monkeypatch.setattr(patch, "ua", SimpleNamespace(random="fixture"))
    monkeypatch.setattr(patch.time, "sleep", lambda _: run.stop("cancelled"))
    monkeypatch.setattr(requests.Session, "send", lambda *a, **k: sent.append(True))
    with run.bind(), pytest.raises(ResearchStopped):
        requests.get("https://push2.eastmoney.com/fixture", timeout=1)
    assert not sent


def test_retain_same_search_records_does_not_duplicate_capacity():
    run = ResearchRun(5)
    data = {"results": [{"title": "公告", "url": "https://example.com/notice", "snippet": "正文"}]}
    run.retain("search_comprehensive_intel", data)
    run.retain("search_comprehensive_intel", data)
    assert len(run.evidence["legacy"]) == 1
