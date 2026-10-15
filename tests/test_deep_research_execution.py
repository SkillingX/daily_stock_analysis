"""Execution and report-boundary regressions; no live provider requests."""

from pathlib import Path

import pytest

from src.agent.llm_adapter import LLMResponse
from src.deep_research_dims import narrate as narrative_module


@pytest.mark.parametrize("default", ["", "已有规则结论"])
def test_provider_error_cannot_become_research_prose(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, default: str) -> None:
    (tmp_path / "phase.md").write_text("解释已有事实")
    monkeypatch.setattr(narrative_module, "_AGENTS_DIR", str(tmp_path))

    class FailedProvider:
        def call_text(self, *args: object, **kwargs: object) -> LLMResponse:
            return LLMResponse(provider="error", content="All LLM models failed. Last error: model request timed out " * 3)

    assert narrative_module.narrate(FailedProvider(), "phase", {"status": "known"}, default) == default

from typing import Any
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.test_model_concurrency import provider
from tests.test_choice_integration import choice_transport


@pytest.mark.parametrize("mode", [1, 2])
def test_partial_research_preserves_evidence_and_matches_api_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    provider: tuple[str, dict[str, int]], choice_transport: Any, mode: int,
) -> None:
    import time
    from src.config import Config, get_config
    from src.storage import DatabaseManager
    from src.search_service import SearchService, SearchResult, SearchResponse
    from src.services import deep_research_service as service_module
    from api.v1.endpoints.deep_research import router
    base, observed = provider
    observed["research_mode"] = mode
    cfg = choice_transport
    cfg.deep_research_timeout_seconds = 30 if mode == 1 else 2.0
    cfg.deep_research_engine = "dual_track"
    cfg.litellm_model = "openai/fixture"
    cfg.agent_litellm_model = "openai/fixture"
    cfg.llm_model_list = [{"model_name": "openai/fixture", "litellm_params": {"model": "openai/fixture", "api_base": base, "api_key": "fixture-key"}}]
    monkeypatch.setattr(Config, "_instance", cfg)
    monkeypatch.setattr(service_module, "_dual_track_adapter", None)
    monkeypatch.setattr(service_module, "_DEEP_RESEARCH_DIR", tmp_path)
    monkeypatch.setattr(DatabaseManager, "_instance", None)
    database = DatabaseManager(f"sqlite:///{tmp_path / 'reports.sqlite'}")
    monkeypatch.setattr(service_module, "get_db", lambda: database)
    from src.agent.deep_research import orchestrator
    from src.deep_research_dims.context import SharedContext, _safe_fundamental
    context = SharedContext("600519", "样本", "2026-10-15", quote={"price": 50.0})
    context.fundamental = _safe_fundamental("600519", context)
    monkeypatch.setattr(orchestrator, "build_shared_context", lambda *args, **kwargs: context)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setattr(SearchService, "is_available", property(lambda self: True))
    monkeypatch.setattr(SearchService, "search_comprehensive_intel", lambda self, **kwargs: {
        "business": SearchResponse(query="fixture", provider="fixture", results=[SearchResult(title="公司公告样本", snippet="已取得的业务资料样本", url="https://example.com/disclosure", source="公司公告", published_date="2026-10-01")])
    })
    before = time.monotonic()
    try:
        service = service_module.deep_research_service
        result = service.generate_report("600519", "样本", dims=["business", "fundamental"], force_refresh=True)
        assert time.monotonic() - before < (10 if mode == 1 else 4)
        assert result["status"] == "partial", result
        assert result["report_id"]
        assert "All LLM models failed" not in result["markdown"]
        business = result["dimensions"]["business"]
        if mode == 1:
            assert business["retained_evidence"][0]["url"] == "https://example.com/disclosure"
            assert "公司公告样本" in result["markdown"]
            assert "研究时限耗尽" not in business["degraded_reason"]
        else:
            assert "超时" in business["degraded_reason"] or "预算" in business["degraded_reason"]
        assert not result.get("cache_hit")
        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as client:
            detail = client.get(f"/reports/{result['report_id']}")
            assert detail.status_code == 200
            assert detail.json()["data"]["markdown"] == result["markdown"]
            download = client.get(f"/reports/{result['report_id']}/markdown")
            assert download.status_code == 200
            assert download.text == result["markdown"]
        assert observed["count"] == (2 if mode == 1 else 1)
    finally:
        database._engine.dispose()


def test_report_budget_bounds_real_stage_zero_source_http(monkeypatch: pytest.MonkeyPatch) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from time import monotonic, sleep
    import requests
    from src.core.research_run import ResearchRun
    from src.deep_research_dims import context

    class Source(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            sleep(0.6)
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{}')
            except (BrokenPipeError, ConnectionResetError):
                pass
        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Source)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(context, '_safe_quote', lambda *args: {})
    monkeypatch.setattr(context, '_safe_history', lambda *args: [])
    monkeypatch.setattr(context, '_safe_chip', lambda *args: {})
    monkeypatch.setattr(context, '_safe_history_reports', lambda *args: [])
    def financial(*args: object) -> dict[str, object]:
        try:
            return requests.get(f'http://127.0.0.1:{server.server_port}/', timeout=2).json()
        except requests.RequestException:
            return {}
    monkeypatch.setattr(context, '_safe_fundamental', financial)
    try:
        started = monotonic()
        with ResearchRun(0.15).bind():
            context.build_shared_context('600519', '样本')
        assert monotonic() - started < 0.45
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


@pytest.mark.parametrize('dim_id,payload,expected', [
    ('business', {'company': {'MAINBUSINESS': '有效主营资料'}}, '有效主营资料'),
    ('ownership', {'company': {'AHOLDER': '已取得的实控人'}, 'holders': {'HOLDNAMER': '股东甲;股东乙', 'period': '2026-06-30'}}, '股东甲'),
])
def test_vendor_only_interruption_persists_a_partial_report(dim_id: str, payload: dict[str, Any], expected: str) -> None:
    from src.core.research_run import ResearchRun
    from src.agent.deep_research.orchestrator import _interrupted_result
    run = ResearchRun(10)
    run.retain_vendor(dim_id, payload)
    run.stop('cancelled')
    result = _interrupted_result(run, '600519', '样本', {dim_id}, None)
    assert result.success and result.status == 'partial'
    assert expected in result.markdown
    assert result.dims[dim_id].status == 'degraded'


@pytest.mark.parametrize('dim_id', ['business', 'ownership'])
def test_research_parser_preserves_recorded_failure_reason(dim_id: str) -> None:
    from importlib import import_module
    parser = import_module(f'src.deep_research.researchers.{dim_id}').parse
    dim, _ = parser({'status': 'degraded', 'degraded_reason': '模型请求超时，保留已取得资料', 'data_gaps': ['高管资料待补充']}, 1)
    assert '模型请求超时' in dim.degraded_reason
    assert dim.status == 'degraded'


def test_valid_nonsearch_tool_facts_survive_failed_model_and_interruption() -> None:
    from src.core.research_run import ResearchRun, research_dimension
    from src.agent.deep_research.orchestrator import _interrupted_result
    from src.agent.tools.registry import ToolDefinition, ToolRegistry
    run = ResearchRun(10)
    registry = ToolRegistry()
    record = {'main_net_inflow': 150000000.0, 'inflow_5d': None, 'source': 'choice', 'source_unit': '元', 'currency': 'CNY', 'latest_date': '2026-10-14', 'field_meta': {'main_inflow': {'source': 'choice', 'unit': 'currency_base', 'currency': 'CNY', 'quality': {'status': 'not_comparable', 'reason_codes': ['period_unknown']}}}}
    registry.register(ToolDefinition('get_capital_flow', 'fixture', [], lambda: record))
    token = research_dimension.set('capital')
    try:
        with run.bind():
            registry.execute('get_capital_flow')
    finally:
        research_dimension.reset(token)
    run.stop('cancelled')
    result = _interrupted_result(run, '600519', '样本', {'capital'}, None)
    assert result.success and result.status == 'partial'
    assert len(result.dims['capital'].retained_facts) == 1
    assert result.dims['capital'].flow_score is None
    assert '150000000.0' in result.markdown and 'choice' in result.markdown
    assert '尚不能直接对比' in result.markdown and '财期未知' in result.markdown
    assert 'CNY' not in result.markdown
    assert '5交易日主力净流入' not in result.markdown


def test_retained_source_facts_follow_real_price_and_financial_metadata_keys() -> None:
    from src.core.research_run import ResearchRun
    run = ResearchRun(10)
    price_meta = {'source': 'choice', 'unit': 'currency_base', 'currency': 'CNY', 'observed_at': '2026-10-15T15:00:00', 'quality': {'status': 'conflict', 'reason_codes': ['numeric_conflict']}, 'input_reasons': []}
    run.retain('get_realtime_quote', {'price': 12.5, 'source': 'sina', 'field_meta': {'current_price': price_meta}})
    fact = run.facts['legacy'][0]
    assert fact['source'] == 'choice' and fact['period'] == price_meta['observed_at']
    assert fact['unit'] == 'currency_base / CNY' and 'conflict' in fact['qualification']
    run.retain('get_stock_info', {'code': '600519', 'name': '样本', 'pe_ratio': 0.0, 'fundamental_context': {'valuation': {'data': {'pe_ratio': 0.0}, 'field_meta': {'pe_ratio': {'source': 'choice', 'unit': 'multiple'}}}, 'growth': {'data': {'roe': -1.23}, 'field_meta': {'roe': {'source': 'ifind', 'period': '2026-06-30', 'period_basis': 'YTD', 'unit': 'percentage_point', 'quality': {'status': 'not_comparable', 'reason_codes': ['adjustment_mismatch']}}}}}})
    facts = {fact['label']: fact for fact in run.facts['legacy']}
    assert facts['PE']['value'] == '0.0' and facts['PE']['source'] == 'choice'
    assert facts['加权ROE']['value'] == '-1.23' and facts['加权ROE']['source'] == 'ifind'
    assert facts['加权ROE']['period'] == '2026-06-30' and 'adjustment_mismatch' in facts['加权ROE']['qualification']


@pytest.mark.parametrize('quote,expected', [({'price': None, 'name': '样本', 'field_meta': {}}, False), ({'price': 12.5, 'source': 'choice'}, True)])
def test_stage_zero_interruption_requires_actual_measurement_and_keeps_it(quote: dict[str, Any], expected: bool) -> None:
    from src.core.research_run import ResearchRun
    from src.deep_research_dims.context import SharedContext
    from src.agent.deep_research.orchestrator import _interrupted_result
    run = ResearchRun(10)
    run.context = SharedContext('600519', '样本', '2026-10-15', quote=quote)
    run.stop('cancelled')
    result = _interrupted_result(run, '600519', '样本', {'data'}, None)
    assert result.success == expected
    assert bool(result.markdown) == expected
    if expected:
        assert '12.5' in result.markdown and 'choice' in result.markdown


def test_current_financial_amounts_are_useful_even_without_health_score_fields() -> None:
    from src.core.research_run import ResearchRun
    from src.deep_research_dims.context import SharedContext
    from src.agent.deep_research.orchestrator import _interrupted_result
    from data_provider.cross_source_validator import AnchorReading, adopted_field_record
    record = adopted_field_record('revenue', AnchorReading('ifind', 1230000000, period='2026-06-30', unit='currency_base', currency='CNY', caliber='operating_revenue'))
    run = ResearchRun(10)
    run.context = SharedContext('600519', '样本', '2026-10-15', fundamental={'revenue': 1230000000, 'field_meta': {'revenue': record}, 'cross_validation': {'anchors': {}}})
    run.stop('cancelled')
    result = _interrupted_result(run, '600519', '样本', {'fundamental'}, None)
    assert result.success and result.status == 'partial'
    assert result.dims['fundamental'].data_quality.obtained_count == 0
    assert '12.30 亿元' in result.markdown


@pytest.mark.parametrize('dim_id', ['capital', 'business', 'ownership'])
def test_cancel_between_direct_sources_stops_the_next_prefetch(monkeypatch: pytest.MonkeyPatch, dim_id: str) -> None:
    from src.agent.deep_research import explore_agents
    from src.agent.tools import data_tools
    from src.config import Config
    from src.core.research_run import ResearchRun, ResearchStopped
    from types import SimpleNamespace
    cfg = Config(data_source_profile='choice', enable_ifind_mcp=True, ifind_mcp_endpoint='http://127.0.0.1/fixture', ifind_mcp_token='fixture')
    monkeypatch.setattr(explore_agents, 'get_config', lambda: cfg)
    monkeypatch.setattr(explore_agents, 'load_dim_prompt', lambda _: 'fixture')
    run = ResearchRun(10)
    calls = []
    def first_source(*args: object, **kwargs: object) -> dict[str, object]:
        calls.append('first')
        run.stop('cancelled')
        return {'fundamental_context': {'research': {}}}
    def second_source(*args: object, **kwargs: object) -> dict[str, object]:
        calls.append('second')
        return {}
    monkeypatch.setattr(data_tools, '_handle_get_capital_flow', first_source)
    monkeypatch.setattr(data_tools, '_handle_get_stock_info', first_source if dim_id != 'capital' else second_source)
    monkeypatch.setattr(data_tools, '_get_fetcher_manager', lambda: SimpleNamespace(get_ifind_research_context=second_source))
    with run.bind(), pytest.raises(ResearchStopped):
        getattr(explore_agents, f'run_{dim_id}_agent')('600519', '样本', object())
    assert calls == ['first']
