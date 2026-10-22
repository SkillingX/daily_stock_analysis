"""Trusted supplier facts survive failed research and saved-report round trips."""
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
from time import monotonic

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from data_provider.base import DataFetcherManager
from data_provider.choice_adapter import ChoiceAdapter
from data_provider.ifind_fundamental_adapter import IfindFetcher
from src.core.research_run import ResearchRun, current_research
from src.deep_research_dims.context import SharedContext
from tests.test_choice_integration import choice_transport
from tests.test_ifind_choice_integration import ifind_transport


@pytest.fixture
def vendor_runtime(ifind_transport, monkeypatch, tmp_path):
    from api.v1.endpoints import deep_research, fundamentals
    from src.agent.deep_research import explore_agents, orchestrator
    from src.agent.tools import data_tools
    from src.deep_research_dims import context, dim_cache
    from src.services import deep_research_service, fundamentals_service
    from src.storage import DatabaseManager
    import src.storage

    config, response, calls = ifind_transport
    config.financial_primary_source = "ifind"
    monkeypatch.setattr(explore_agents, "get_config", lambda: config)
    native = ChoiceAdapter._native_request

    def sdk(method, args, options, deadline):
        reply = native(method, args, options, deadline)
        if method == "css":
            values = {"MAINBUSINESS": "供应商公司业务资料", "MAINPRODUCT": "供应商主营产品资料",
                      "AHOLDER": "可信实控人", "HOLDNAMER": "股东甲;股东乙"}
            reply["data"][args[0]] = [values.get(key, value) for key, value in zip(reply["indicators"], reply["data"][args[0]])]
        return reply

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(sdk))
    research = ChoiceAdapter().snapshot("300260", deadline=monotonic() + 5)["research"]
    original_query = IfindFetcher._async_query
    holders = json.loads((Path(__file__).parent / "fixtures/ifind_shareholders.json").read_text())

    async def query(self, tool, question, **kwargs):
        if tool == "get_stock_shareholders":
            calls.append((tool, question))
            return deepcopy(holders)
        return await original_query(self, tool, question, **kwargs)

    monkeypatch.setattr(IfindFetcher, "_async_query", query)
    manager = DataFetcherManager()
    monkeypatch.setattr(data_tools, "_fetcher_manager_singleton", manager)
    monkeypatch.setattr(deep_research_service, "_get_dual_track_adapter", lambda: None)
    monkeypatch.setattr(deep_research_service, "get_deep_research_engine", lambda: "dual_track")
    monkeypatch.setattr(deep_research_service, "_DEEP_RESEARCH_DIR", tmp_path / "deep")
    monkeypatch.setattr(deep_research_service, "_report_cache", {})
    monkeypatch.setattr(fundamentals_service, "_REPORT_DIR", tmp_path / "fundamentals")
    database = DatabaseManager("sqlite:///" + str(tmp_path / "reports.db"))
    monkeypatch.setattr(src.storage, "get_db", lambda: database)
    monkeypatch.setattr(deep_research_service, "get_db", lambda: database)
    monkeypatch.setattr(fundamentals, "get_db", lambda: database)
    cache_writes = []
    monkeypatch.setattr(dim_cache, "save_cached_dim", lambda code, dim_id, data: cache_writes.append(dim_id))

    def shared(*args, **kwargs):
        snapshot = SharedContext("300260", "新莱应材", date.today().isoformat())
        snapshot.fundamental = {"research": deepcopy(research)}
        return snapshot

    monkeypatch.setattr(context, "build_shared_context", shared)
    monkeypatch.setattr(orchestrator, "build_shared_context", shared)
    app = FastAPI()
    app.include_router(deep_research.router, prefix="/api/v1/deep-research")
    app.include_router(fundamentals.router, prefix="/api/v1/fundamentals")
    return {"config": config, "research": research, "manager": manager, "calls": calls,
            "shared": shared, "database": database, "app": app, "cache_writes": cache_writes,
            "explore": explore_agents, "orchestrator": orchestrator,
            "deep": deep_research_service, "fundamentals": fundamentals_service}


def _assert_vendor(dim_id, payload, research, *, details=True):
    from src.schemas.deep_research_dims import parse_dim
    dim = parse_dim(dim_id, payload)
    assert dim.status == "degraded" and dim.score is None
    assert dim.degraded_reason
    if dim_id == "ownership":
        assert "可信实控人" in dim.controller and "Choice" in dim.controller
        assert research["as_of"] in dim.controller
        assert {holder.name for holder in dim.top_holders} == {"股东甲", "股东乙"}
        assert all("Choice" in holder.source and research["holders"]["period"] in holder.source for holder in dim.top_holders)
        if details:
            assert dim.shareholder_details is not None
            assert len(dim.shareholder_details.items) == 10
            holder = dim.shareholder_details.items[0]
            assert holder.name == "李水波" and holder.report_date == "2025-12-31"
            assert holder.shares.source == "ifind" and holder.shares.unit == "shares"
            assert holder.shares.value == 102200000 and holder.ratio.value == 25.06
    else:
        assert dim.business_model["value"] == "供应商公司业务资料"
        assert "Choice" in dim.business_model["source"] and research["as_of"] in dim.business_model["source"]
        assert dim.main_products["value"] == "供应商主营产品资料"
        if details:
            assert dim.product_segments is not None
            assert len(dim.product_segments.items) == 4
            product = dim.product_segments.items[0]
            assert product.name == "无菌包材" and product.report_date == "2025-12-31"
            assert product.revenue.source == "ifind" and product.revenue.unit == "currency_base"
            assert product.revenue.value == 1411150168.69
    return dim


@pytest.mark.parametrize("dim_id,mode", [("ownership", "bad_list"), ("ownership", "object_gaps"), ("business", "bad_score"), ("business", "runner_error"), ("business", "data_gaps")])
def test_failed_deep_research_keeps_vendor_details_through_saved_api(vendor_runtime, monkeypatch, dim_id, mode):
    runtime = vendor_runtime
    invalid = {"executives": {"source_note": "模型错误列表"}, "recent_changes": {"window": "本期未取得"}} if dim_id == "ownership" else {"score": 999, "narrative": "模型未通过契约"}
    if mode == "data_gaps":
        invalid = {"data_gaps": ["证据不足"], "business_model": {}, "main_products": {}}
    if mode == "object_gaps":
        invalid = {"executives": [{"name": "高管甲", "source": "公告"}], "data_gaps": [{"item": "近一年减持公告未取得"}], "narrative": "模型已取得的股权研究"}
    if mode not in {"runner_error", "data_gaps", "object_gaps"}:
        from src.deep_research.researchers import REGISTRY
        if dim_id == "ownership":
            # 契约收敛（#35）：包装列表形状在股权适配边界解包/记缺项，不再抛
            # ValidationError；本用例继续验证坏形状下供应商资料保留与不写成功缓存。
            dim, _steps = REGISTRY[dim_id].parser(invalid, 1)
            assert dim.status == "degraded"
            assert dim.executives == [] and dim.recent_changes == []
            assert any("核心高管" in gap for gap in dim.data_gaps)
        else:
            with pytest.raises(ValidationError):
                REGISTRY[dim_id].parser(invalid, 1)

    def model(*args, **kwargs):
        if mode == "runner_error":
            raise RuntimeError("研究员运行异常")
        return {"ok": True, "steps": 1, "data": deepcopy(invalid)}

    monkeypatch.setattr(runtime["explore"], "_run_explore", model)
    report = runtime["deep"].DeepResearchService().generate_report("300260", "新莱应材", dims=[dim_id], force_refresh=True)
    assert report["status"] == "partial" and report["report_id"]
    _assert_vendor(dim_id, report["dimensions"][dim_id], runtime["research"])
    assert not runtime["cache_writes"]
    assert not runtime["deep"]._report_cache
    rid = report["report_id"]
    assert runtime["database"].get_deep_research_report(rid) is not None
    with TestClient(runtime["app"]) as client:
        detail = client.get(f"/api/v1/deep-research/reports/{rid}")
        download = client.get(f"/api/v1/deep-research/reports/{rid}/markdown")
    assert detail.status_code == download.status_code == 200
    saved = detail.json()["data"]
    _assert_vendor(dim_id, saved["dimensions"][dim_id], runtime["research"])
    assert report["markdown"] == saved["markdown"] == download.text
    if mode == "object_gaps":
        ownership = saved["dimensions"]["ownership"]
        assert ownership["executives"][0]["name"] == "高管甲"
        assert ownership["data_gaps"] == ["近一年减持公告未取得"]
        assert ownership["narrative"] == "模型已取得的股权研究"
        assert "近一年减持公告未取得" in download.text
        assert "validation error" not in download.text and "{'item':" not in download.text
    assert ("李水波" if dim_id == "ownership" else "无菌包材") in download.text
    assert "iFinD" in download.text and "2025-12-31" in download.text


@pytest.mark.parametrize("mode", ["bad_score", "model_error", "missing_prompt", "data_gaps"])
def test_fundamentals_keeps_business_facts_on_failure_without_success_cache(vendor_runtime, monkeypatch, mode):
    runtime = vendor_runtime
    if mode == "missing_prompt":
        # Already acquired typed evidence remains usable even without a prompt.
        runtime["research"]["product_segments"] = runtime["manager"].get_ifind_research_context("300260", "products")
        monkeypatch.setattr(runtime["explore"], "load_dim_prompt", lambda _: "")
    else:
        assert "product_segments" not in runtime["research"]
        reply = {"ok": True, "steps": 1, "data": {"score": 999}} if mode == "bad_score" else {"ok": False, "steps": 1, "error": "模型调用失败"}
        if mode == "data_gaps":
            reply = {"ok": True, "steps": 1, "data": {"data_gaps": ["证据不足"], "business_model": {}, "main_products": {}}}
        monkeypatch.setattr(runtime["explore"], "_run_explore", lambda *a, **kw: deepcopy(reply))
    with TestClient(runtime["app"]) as client:
        generated = client.post("/api/v1/fundamentals/generate?stock_code=300260&stock_name=新莱应材&force_refresh=true")
        assert generated.status_code == 200, generated.text
        report = generated.json()
        _assert_vendor("business", report["dims"]["business"], runtime["research"])
        assert report["status"] == "partial" and report["report_id"]
        rid = report["report_id"]
        detail = client.get(f"/api/v1/fundamentals/reports/{rid}")
        download = client.get(f"/api/v1/fundamentals/reports/{rid}/markdown?download=1")
        retry = client.post("/api/v1/fundamentals/generate?stock_code=300260&stock_name=新莱应材")
    assert detail.status_code == download.status_code == retry.status_code == 200
    saved = detail.json()["data"]
    _assert_vendor("business", json.loads(saved["dims_json"])["business"], runtime["research"])
    assert report["markdown"] == saved["markdown"] == download.text
    assert "无菌包材" in download.text and "iFinD" in download.text
    assert "validation error" not in download.text and "errors.pydantic.dev" not in download.text
    assert retry.json()["report_id"] != rid and retry.json()["status"] == "partial"
    assert not runtime["cache_writes"]


@pytest.mark.parametrize("reason", ["cancelled", "report_budget"])
def test_stage_zero_stop_keeps_choice_facts_before_researchers_start(vendor_runtime, monkeypatch, reason):
    runtime = vendor_runtime
    run = ResearchRun(30)
    shared = runtime["shared"]
    calls_before = len(runtime["calls"])

    def stopped_stage_zero(*args, **kwargs):
        context = shared()
        assert current_research.get() is run
        assert not run.vendor_research
        run.stop(reason)
        return context

    def forbidden(*args, **kwargs):
        pytest.fail("Stage-zero cancellation must not launch model researchers")

    monkeypatch.setattr(runtime["orchestrator"], "build_shared_context", stopped_stage_zero)
    monkeypatch.setattr(runtime["explore"], "_run_explore", forbidden)
    report = runtime["deep"].DeepResearchService().generate_report("300260", "新莱应材", dims=["business", "ownership"], force_refresh=True, run=run)
    assert report["status"] == "partial" and report["end_reason"] == reason
    assert not run.vendor_research and len(runtime["calls"]) == calls_before
    for dim_id in ("business", "ownership"):
        _assert_vendor(dim_id, report["dimensions"][dim_id], runtime["research"], details=False)
    assert "供应商公司业务资料" in report["markdown"] and "可信实控人" in report["markdown"]
    assert run.reason_text in report["markdown"]
    assert not runtime["cache_writes"]
