"""Regress report deadlines, nested model retries, and persisted partial results.
Run: python -m pytest tests/test_fundamentals_generation_budget.py
Uses synthetic providers and a loopback HTTP server; no external credentials.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Semaphore, Thread
from time import monotonic, sleep
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.agent.deep_research import explore_agents
from src.agent.llm_adapter import LLMResponse, LLMToolAdapter
from src.agent.runner import RunLoopResult
from src.agent.tools.registry import ToolRegistry
from src.config import Config
from src.llm.errors import call_litellm_with_param_recovery
from tests.test_fundamentals_data_quality import financial_sources  # noqa: F401


@pytest.fixture
def exploration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(explore_agents, '_registry_source', ToolRegistry)
    monkeypatch.setattr(explore_agents, 'load_constitution', lambda: '')
    monkeypatch.setattr(explore_agents, 'load_dim_prompt', lambda _: '')
    monkeypatch.setattr(explore_agents, '_llm_semaphore', lambda: Semaphore(1))


def test_failed_model_is_not_retried_as_invalid_json(exploration: None, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[float] = []
    def loop(**kwargs: Any) -> RunLoopResult:
        calls.append(kwargs['max_wall_clock_seconds'])
        return RunLoopResult(success=False, error='timed out')
    monkeypatch.setattr(explore_agents, 'run_agent_loop', loop)
    out = explore_agents._run_explore('business', set(), 'fixture', object(), deadline=monotonic() + 1)
    assert not out['ok'] and len(calls) == 1


def test_json_repair_and_semaphore_use_original_deadline(exploration: None, monkeypatch: pytest.MonkeyPatch) -> None:
    budgets: list[float] = []
    def loop(**kwargs: Any) -> RunLoopResult:
        budgets.append(kwargs['max_wall_clock_seconds'])
        sleep(0.02)
        return RunLoopResult(success=True, content='not-json' if len(budgets) == 1 else '{"narrative":"ok"}')
    monkeypatch.setattr(explore_agents, 'run_agent_loop', loop)
    out = explore_agents._run_explore('business', set(), 'fixture', object(), deadline=monotonic() + 1)
    assert out['ok'] and len(budgets) == 2 and budgets[1] < budgets[0] - 0.01
    monkeypatch.setattr(explore_agents, '_llm_semaphore', lambda: Semaphore(0))
    started = monotonic()
    out = explore_agents._run_explore('business', set(), 'fixture', object(), deadline=started + 0.03)
    assert not out['ok'] and len(budgets) == 2 and monotonic() - started < 0.3


def test_param_recovery_subtracts_elapsed_time(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter([100.0, 100.4])
    monkeypatch.setattr('src.llm.errors.monotonic', lambda: next(clock))
    timeouts: list[float] = []
    def call(kwargs: dict[str, Any]) -> str:
        timeouts.append(kwargs['timeout'])
        if len(timeouts) == 1:
            raise RuntimeError('Unsupported parameter: temperature is not supported')
        return 'ok'
    assert call_litellm_with_param_recovery(call, model='openai/fixture', call_kwargs={'temperature': 0.5, 'timeout': 1.0}, cache_recovery=False) == 'ok'
    assert timeouts == pytest.approx([1.0, 0.6])


@pytest.mark.parametrize('channel', [True, False])
def test_real_router_and_sdk_do_not_retry_slow_http(channel: bool) -> None:
    calls: list[str] = []
    release = Event()
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            calls.append(self.path)
            self.rfile.read(int(self.headers['Content-Length']))
            release.wait(2)
            self.send_response(200)
            self.end_headers()
        def log_message(self, *_args: Any) -> None:
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    model = 'openai/gpt-4o-mini'
    base = f'http://127.0.0.1:{server.server_port}/v1'
    cfg = Config(litellm_model=model, openai_api_keys=['fixture-only'], openai_base_url=base)
    if channel:
        cfg.llm_model_list = [{'model_name': model, 'litellm_params': {'model': model, 'api_base': base, 'api_key': 'fixture-only'}}]
    adapter = LLMToolAdapter(cfg)
    if not channel:
        adapter._router = None  # exercise the direct provider branch too
    try:
        started = monotonic()
        out = adapter.call_with_tools([{'role': 'user', 'content': 'fixture'}], [], timeout=0.3)
        assert out.provider == 'error'
        assert calls == ['/v1/chat/completions']
        assert monotonic() - started < 1.5
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_http_timeout_saves_partial_report_and_can_be_reopened(financial_sources: dict[str, Any], exploration: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    from api.v1.endpoints.fundamentals import router
    from src.config import get_config
    from src.services import fundamentals_service as svc
    from src.storage import get_db
    calls: list[float] = []
    class SlowModel:
        def call_with_tools(self, messages: Any, tools: Any, timeout: float | None = None) -> LLMResponse:
            assert timeout is not None
            calls.append(timeout)
            sleep(timeout)
            return LLMResponse(provider='error', content='synthetic timeout')
    get_config().fundamentals_report_timeout_seconds = 0.5
    monkeypatch.setattr('src.services.deep_research_service._get_dual_track_adapter', SlowModel)
    monkeypatch.setattr(svc, '_REPORT_DIR', tmp_path / 'reports')
    app = FastAPI()
    app.include_router(router, prefix='/api/v1/fundamentals')
    with TestClient(app) as client:
        started = monotonic()
        response = client.post('/api/v1/fundamentals/generate', params={'stock_code':'600519', 'stock_name':'样本', 'force_refresh':'true'})
        assert response.status_code == 200
        out = response.json()
        assert out['status'] == 'partial' and out['report_id']
        assert len(calls) == 1 and monotonic() - started < 2
        assert out['dims']['business']['status'] == 'degraded'
        assert '业务画像未完成' in out['markdown']
        saved = client.get('/api/v1/fundamentals/reports/' + out['report_id'])
        assert saved.status_code == 200 and saved.json()['data']['markdown'] == out['markdown']
        get_db().delete_fundamentals_report(out['report_id'])


def test_stage_deadline_preserves_research_budget_and_rejects_late_quote(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.deep_research_dims import context
    cfg = Config(fundamental_stage_timeout_seconds=0.03)
    monkeypatch.setattr('src.config.get_config', lambda: cfg)
    release = Event()
    finished = Event()
    def slow_quote(code: str, ctx: context.SharedContext) -> dict[str, Any]:
        release.wait(1)
        ctx.limitation('late worker mutation')
        finished.set()
        return {'name': 'late quote'}
    monkeypatch.setattr(context, '_safe_quote', slow_quote)
    monkeypatch.setattr(context, '_safe_fundamental', lambda *args: {'roe': 4.56})
    monkeypatch.setattr(context, '_safe_history', lambda *args: [])
    monkeypatch.setattr(context, '_safe_chip', lambda *args: {})
    monkeypatch.setattr(context, '_safe_history_reports', lambda *args: [])
    started = monotonic()
    try:
        ctx = context.build_shared_context('300260', '新莱应材', deadline=started + 0.4)
        assert monotonic() - started < 0.2
        assert ctx.quote == {} and ctx.fundamental == {'roe': 4.56}
        assert 'quote 装配超时，本次未采用' in ctx.limitations
    finally:
        release.set()
        assert finished.wait(1)
    assert ctx.quote == {} and 'late worker mutation' not in ctx.limitations


def test_code_only_resolved_name_reaches_sector_rules(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    from src.deep_research_dims import context
    from src.services import fundamentals_service as svc
    from src.storage import get_db
    ctx = context.SharedContext('600036', '600036', '2026-10-12', quote={'name': '招商银行'})
    monkeypatch.setattr(context, 'build_shared_context', lambda *args, **kwargs: ctx)
    monkeypatch.setattr(svc, '_research_dim', lambda *args, **kwargs: {'status': 'degraded', 'degraded_reason': 'fixture'})
    monkeypatch.setattr(svc, '_same_day_dedup_report', lambda code: None)
    monkeypatch.setattr(svc, '_REPORT_DIR', tmp_path / 'reports')
    out = svc.generate_fundamentals_report('600036')
    try:
        assert out['stock_name'] == ctx.stock_name == '招商银行'
        assert out['dims']['sector']['sector_hint'] == '招商银行'
        assert out['dims']['sector']['base_rate_basis'] == 'industry_base_rate:银行'
        assert out['status'] == 'partial' and out['report_id']
    finally:
        if out['report_id']:
            get_db().delete_fundamentals_report(out['report_id'])
