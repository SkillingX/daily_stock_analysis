"""Regress report deadlines, nested model retries, and persisted partial results.
Run: python -m pytest tests/test_fundamentals_generation_budget.py
Uses synthetic providers and a loopback HTTP server; no external credentials.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from time import monotonic, sleep
from typing import Any
from unittest.mock import patch
import os
import json

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


@pytest.mark.parametrize('configured, expected', [(None, 300.0), ('150', 150.0), ('300', 300.0), ('600', 300.0)])
def test_report_budget_configuration_preserves_explicit_shorter_values(configured: str | None, expected: float, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    env_file = tmp_path / '.env'
    env_file.write_text('')
    environment = {'ENV_FILE': str(env_file)}
    if configured is not None:
        environment['FUNDAMENTALS_REPORT_TIMEOUT_SECONDS'] = configured
    monkeypatch.setattr(Config, '_instance', None)
    with patch.dict(os.environ, environment, clear=True):
        assert Config.get_instance().fundamentals_report_timeout_seconds == expected
    assert Config().fundamentals_report_timeout_seconds == 300.0


@pytest.mark.parametrize('configured, expected', [
    (None, 'low'), ('low', 'low'), ('medium', 'medium'), ('high', 'high'),
    ('xhigh', 'xhigh'), ('max', 'max'), ('invalid', 'low'),
])
def test_minimax_reasoning_effort_configuration_defaults_to_low(configured: str | None, expected: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    env_file = tmp_path / '.env'
    env_file.write_text('')
    environment = {'ENV_FILE': str(env_file)}
    if configured is not None:
        environment['MINIMAX_REASONING_EFFORT'] = configured
    monkeypatch.setattr(Config, '_instance', None)
    with patch.dict(os.environ, environment, clear=True):
        assert Config.get_instance().minimax_reasoning_effort == expected
    assert Config().minimax_reasoning_effort == 'low'


@pytest.fixture
def exploration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(explore_agents, '_registry_source', ToolRegistry)
    monkeypatch.setattr(explore_agents, 'load_constitution', lambda: '')
    monkeypatch.setattr(explore_agents, 'load_dim_prompt', lambda _: 'fixture business rules')


def test_failed_model_is_not_retried_as_invalid_json(exploration: None, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[float] = []
    def loop(**kwargs: Any) -> RunLoopResult:
        calls.append(kwargs['max_wall_clock_seconds'])
        return RunLoopResult(success=False, error='timed out')
    monkeypatch.setattr(explore_agents, 'run_agent_loop', loop)
    out = explore_agents._run_explore('business', set(), 'fixture', object(), deadline=monotonic() + 1)
    assert not out['ok'] and len(calls) == 1


def test_json_repair_uses_original_deadline(exploration: None, monkeypatch: pytest.MonkeyPatch) -> None:
    budgets: list[float] = []
    def loop(**kwargs: Any) -> RunLoopResult:
        budgets.append(kwargs['max_wall_clock_seconds'])
        sleep(0.02)
        return RunLoopResult(success=True, content='not-json' if len(budgets) == 1 else '{"narrative":"ok"}')
    monkeypatch.setattr(explore_agents, 'run_agent_loop', loop)
    out = explore_agents._run_explore('business', set(), 'fixture', object(), deadline=monotonic() + 1)
    assert out['ok'] and len(budgets) == 2 and budgets[1] < budgets[0] - 0.01
    started = monotonic()
    out = explore_agents._run_explore('business', set(), 'fixture', object(), deadline=started - 0.03)
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
    disconnected = Event()
    read_durations: list[float] = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            calls.append(self.path)
            self.rfile.read(int(self.headers['Content-Length']))
            started = monotonic()
            self.connection.settimeout(2)
            # EOF proves that the real SDK closed its timed-out HTTP request.
            # SDK cold-start and error mapping are outside the network boundary.
            try:
                if self.rfile.read(1) == b'':
                    read_durations.append(monotonic() - started)
                    disconnected.set()
            except TimeoutError:
                pass
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
        call_started = monotonic()
        out = adapter.call_with_tools([{'role': 'user', 'content': 'fixture'}], [], timeout=0.3)
        timing = {'total_seconds': monotonic() - call_started, 'http_seconds': read_durations}
        assert out.provider == 'error'
        assert calls == ['/v1/chat/completions']
        assert disconnected.wait(0.5), f'SDK left the timed-out connection open: {timing}'
        assert len(read_durations) == 1 and read_durations[0] < 1.5, timing
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


@pytest.mark.parametrize('channel, dim_id, wire_model, fallback, effort', [
    (True, 'business', 'openai/MiniMax-M3.1-Flash', False, 'low'),
    (False, 'business', 'openai/MiniMax-M3.1-Flash', False, 'medium'),
    (True, 'business', 'openai/MiniMax-M3', False, 'low'),
    (True, 'business', 'openai/glm-5.3', False, 'low'),
    (True, 'intel', 'openai/MiniMax-M3.1-Flash', False, 'low'),
    (True, 'business', 'openai/MiniMax-M3.1-Flash', True, 'low'),
])
def test_minimax_reasoning_effort_reaches_supported_models_at_http_boundary(channel: bool, dim_id: str, wire_model: str, fallback: bool, effort: str, exploration: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.agent.tools.registry import ToolDefinition
    bodies: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            bodies.append(body)
            status = 200
            if fallback and len(bodies) == 2:
                data = {'error': {'message': 'Fixture model unavailable', 'type': 'invalid_request_error', 'code': 'model_not_found'}}
                status = 400
            elif len(bodies) == 1:
                message = {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 's1', 'type': 'function', 'function': {'name': 'search_comprehensive_intel', 'arguments': '{}'}}]}
                finish = 'tool_calls'
            else:
                message = {'role': 'assistant', 'content': '{"business_model":{"value":"主营材料"},"narrative":"已有证据"}'}
                finish = 'stop'
            if status == 200:
                data = {'id': 'fixture', 'object': 'chat.completion', 'created': 0, 'model': body['model'], 'choices': [{'index': 0, 'message': message, 'finish_reason': finish}]}
            encoded = json.dumps(data).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_args: Any) -> None:
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}/v1'
    model = 'business-alias' if channel else wire_model
    cfg = Config(litellm_model=model, openai_api_keys=['fixture-only'], openai_base_url=base)
    cfg.minimax_reasoning_effort = effort
    if channel:
        cfg.llm_model_list = [{'model_name': model, 'litellm_params': {'model': wire_model, 'api_base': base, 'api_key': 'fixture-only'}}]
    if fallback:
        cfg.litellm_fallback_models = ['openai/glm-5.3']
        cfg.llm_model_list.append({'model_name': 'openai/glm-5.3', 'litellm_params': {'model': 'openai/glm-5.3', 'api_base': base, 'api_key': 'fixture-only'}})
    adapter = LLMToolAdapter(cfg)
    if not channel:
        adapter._router = None
    registry = ToolRegistry()
    registry.register(ToolDefinition('search_comprehensive_intel', 'fixture', [], lambda **kwargs: {'evidence': '主营材料'}))
    monkeypatch.setattr(explore_agents, '_registry_source', lambda: registry)
    try:
        out = explore_agents._run_explore(dim_id, {'search_comprehensive_intel'}, 'fixture', adapter, deadline=monotonic() + 30)
        assert out['ok'], out
        expected_efforts = [
            effort if 'minimax-m3.1-flash' in body['model'].lower() else None
            for body in bodies
        ]
        assert [body.get('reasoning_effort') for body in bodies] == expected_efforts
        assert [body.get('thinking') for body in bodies] == [
            {'type': 'adaptive'} if expected else None for expected in expected_efforts
        ]
        assert [body['model'] for body in bodies] == [wire_model.removeprefix('openai/')] * 2 + (['glm-5.3'] if fallback else [])
    finally:
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


@pytest.mark.parametrize('budget, business_status', [(150.0, 'degraded'), (300.0, 'ok')])
def test_report_can_finish_after_old_budget_or_preserve_partial_results(budget: float, business_status: str, financial_sources: dict[str, Any], exploration: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    from api.v1.endpoints.fundamentals import router
    from src.agent import runner
    from src.agent.llm_adapter import ToolCall
    from src.agent.tools.registry import ToolDefinition
    from src.config import get_config
    from src.deep_research_dims import context
    from src.services import fundamentals_service as service
    from src.storage import get_db

    ctx = context.SharedContext('600519', '样本', '2026-10-21', fundamental={'roe': 0.0})
    monkeypatch.setattr(context, 'build_shared_context', lambda *args, **kwargs: ctx)
    get_config().fundamentals_report_timeout_seconds = budget
    clock = [100.0]
    monkeypatch.setattr(service, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(explore_agents, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(runner.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(service, '_REPORT_DIR', tmp_path / 'reports')
    registry = ToolRegistry()
    registry.register(ToolDefinition('search_comprehensive_intel', 'fixture', [], lambda **kwargs: {'evidence': '主营材料'}))
    monkeypatch.setattr(explore_agents, '_registry_source', lambda: registry)
    calls: list[float] = []

    class Model:
        def call_with_tools(self, messages: Any, tools: Any, timeout: float | None = None, **kwargs: Any) -> LLMResponse:
            assert timeout is not None
            calls.append(timeout)
            if tools:
                clock[0] += 2
                return LLMResponse(provider='fixture', tool_calls=[ToolCall('s1', 'search_comprehensive_intel', {})])
            clock[0] += min(175.0, timeout + 0.01)
            if timeout < 175:
                return LLMResponse(provider='error', content='synthetic timeout')
            return LLMResponse(provider='fixture', content='{"business_model":{"value":"主营材料"},"narrative":"完成的业务研究"}')

    monkeypatch.setattr('src.services.deep_research_service._get_dual_track_adapter', Model)
    app = FastAPI()
    app.include_router(router, prefix='/api/v1/fundamentals')
    with TestClient(app) as client:
        response = client.post('/api/v1/fundamentals/generate', params={'stock_code': '600519', 'stock_name': '样本', 'force_refresh': 'true'})
        assert response.status_code == 200
        out = response.json()
        try:
            assert out['report_id'] and out['dims']['business']['status'] == business_status
            assert len(calls) == 2 and calls[1] == pytest.approx(budget - 2)
            assert out['dims']['financial']['profitability']['roe']['value'] == 0.0
            saved = client.get('/api/v1/fundamentals/reports/' + out['report_id']).json()
            assert saved['data']['markdown'] == out['markdown']
            if budget == 300:
                assert clock[0] - 100 == 177
                assert '完成的业务研究' in out['markdown']
            else:
                assert out['status'] == 'partial'
                assert '研究预算不足' in out['dims']['business']['degraded_reason']
                assert '研究预算不足' in saved['data']['markdown']
        finally:
            get_db().delete_fundamentals_report(out['report_id'])


@pytest.mark.parametrize('error, expected', [
    ('Agent has insufficient budget', '研究预算不足'),
    ('等待模型额度超时', '等待模型额度超时，尚未发送模型请求'),
    ('request timed out', '模型请求超时'),
])
def test_partial_report_preserves_failure_reason_in_history(error: str, expected: str, financial_sources: dict[str, Any], exploration: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    from api.v1.endpoints.fundamentals import router
    from src.services import fundamentals_service as service
    from src.storage import get_db

    monkeypatch.setattr(service, '_REPORT_DIR', tmp_path / 'reports')
    monkeypatch.setattr(explore_agents, 'run_agent_loop', lambda **kwargs: RunLoopResult(success=False, error=error))
    app = FastAPI()
    app.include_router(router, prefix='/api/v1/fundamentals')
    with TestClient(app) as client:
        response = client.post('/api/v1/fundamentals/generate', params={'stock_code': '600519', 'stock_name': '样本', 'force_refresh': 'true'})
        assert response.status_code == 200
        out = response.json()
        try:
            assert out['status'] == 'partial'
            assert expected in out['dims']['business']['degraded_reason']
            assert expected in out['markdown']
            saved = client.get('/api/v1/fundamentals/reports/' + out['report_id']).json()['data']
            assert json.loads(saved['dims_json'])['business']['degraded_reason'] == out['dims']['business']['degraded_reason']
            assert saved['markdown'] == out['markdown']
            assert client.get('/api/v1/fundamentals/reports/' + out['report_id'] + '/markdown').text == out['markdown']
            assert out['report_id'] in {row['id'] for row in client.get('/api/v1/fundamentals/reports').json()['data']}
        finally:
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
        assert out['dims']['sector']['sector_hint'] == ''
        assert out['dims']['sector']['status'] == 'degraded'
        assert out['dims']['sector']['base_rate_basis'] == 'industry_base_rate:银行'
        assert out['status'] == 'partial' and out['report_id']
    finally:
        if out['report_id']:
            get_db().delete_fundamentals_report(out['report_id'])


def test_research_deadline_is_shared_across_tools_and_reserves_final_response(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.agent import runner
    from src.agent.llm_adapter import ToolCall
    from src.agent.tools.registry import ToolDefinition
    from src.patches.eastmoney_patch import request_deadline
    clock = [100.0]
    monkeypatch.setattr(runner.time, 'monotonic', lambda: clock[0])
    executions: list[float] = []
    calls: list[tuple[int, float]] = []
    registry = ToolRegistry()
    def slow_search() -> dict[str, bool]:
        deadline = request_deadline.get()
        assert deadline is not None
        executions.append(deadline)
        clock[0] = min(clock[0] + 15, deadline)
        return {'timeout': clock[0] >= deadline}
    registry.register(ToolDefinition('search','fixture',[],slow_search))
    class Model:
        def call_with_tools(self, messages: Any, tools: Any, timeout: float | None = None) -> LLMResponse:
            assert timeout is not None
            calls.append((len(tools),timeout))
            clock[0] += 2
            if tools:
                return LLMResponse(provider='fixture',tool_calls=[ToolCall(str(len(calls)),'search',{})])
            return LLMResponse(provider='fixture',content='{"narrative":"资料不足"}')
    out = runner.run_agent_loop(messages=[{'role':'user','content':'fixture'}],tool_registry=registry,llm_adapter=Model(),max_wall_clock_seconds=120,research_deadline=130)
    assert out.success
    assert executions == [130,130]
    assert calls == [(1,30),(1,13),(0,90)]
    assert clock[0] == 132


def test_closing_round_never_executes_returned_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.agent import runner
    from src.agent.llm_adapter import ToolCall
    from src.agent.tools.registry import ToolDefinition
    registry = ToolRegistry()
    def forbidden() -> dict[str, str]:
        pytest.fail('Tools executed during final generation')
    registry.register(ToolDefinition('search','fixture',[],forbidden))
    class Model:
        def call_with_tools(self, messages: Any, tools: Any, **kwargs: Any) -> LLMResponse:
            assert tools == []
            return LLMResponse(provider='fixture',tool_calls=[ToolCall('1','search',{})])
    out = runner.run_agent_loop(messages=[{'role':'user','content':'fixture'}],tool_registry=registry,llm_adapter=Model(),max_wall_clock_seconds=30,research_deadline=monotonic()-1)
    assert not out.success and 'tool' in out.error.lower()


def test_truncated_json_is_not_a_successful_agent_report() -> None:
    from src.agent.runner import run_agent_loop
    class Model:
        def call_with_tools(self, *args: Any, **kwargs: Any) -> LLMResponse:
            return LLMResponse(provider='fixture',content='{"narrative":"incomplete"}',finish_reason='length')
    out = run_agent_loop(messages=[{'role':'user','content':'fixture'}],tool_registry=ToolRegistry(),llm_adapter=Model())
    assert not out.success and 'length' in out.error


def test_slow_tool_http_stops_with_research_budget() -> None:
    import requests
    from src.agent.runner import run_agent_loop
    from src.agent.llm_adapter import ToolCall
    from src.agent.tools.registry import ToolDefinition
    release = Event()
    stopped = Event()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            release.wait(2)
        def log_message(self, *_args: Any) -> None:
            pass
    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server.daemon_threads = True
    worker = Thread(target=server.serve_forever,daemon=True)
    worker.start()
    def search() -> dict[str, bool]:
        try:
            with requests.Session() as client:
                client.trust_env = False
                client.get(f'http://127.0.0.1:{server.server_port}')
            return {'ok':True}
        finally:
            stopped.set()
    registry = ToolRegistry()
    registry.register(ToolDefinition('search','fixture',[],search))
    class Model:
        def call_with_tools(self, messages: Any, tools: Any, **kwargs: Any) -> LLMResponse:
            if tools:
                return LLMResponse(provider='fixture',tool_calls=[ToolCall('1','search',{})])
            return LLMResponse(provider='fixture',content='{"data_gaps":["搜索超时"]}')
    try:
        start = monotonic()
        out = run_agent_loop(messages=[{'role':'user','content':'fixture'}],tool_registry=registry,llm_adapter=Model(),max_steps=2,max_wall_clock_seconds=30,research_deadline=start+0.1)
        assert out.success and '搜索超时' in out.content
        assert stopped.wait(0.5), 'Underlying request still running after deadline'
        assert monotonic()-start < 1
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=1)


def test_refresh_reruns_business_instead_of_reusing_dimension_cache(financial_sources: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    from src.services import fundamentals_service as service
    from src.storage import get_db
    calls: list[int] = []
    class Model:
        def call_with_tools(self, *args: Any, **kwargs: Any) -> LLMResponse:
            calls.append(1)
            return LLMResponse(provider='fixture',content='{"business_model":{"value":"已获取主营资料"},"narrative":"fixture"}')
    monkeypatch.setattr('src.services.deep_research_service._get_dual_track_adapter',Model)
    monkeypatch.setattr(service,'_REPORT_DIR',tmp_path/'reports')
    ids: list[str] = []
    try:
        for _ in range(2):
            result = service.generate_fundamentals_report('600519','样本',force_refresh=True)
            assert result['report_id'] and result['dims']['business']['status']=='ok'
            ids.append(result['report_id'])
        assert len(calls)==2 and ids[0]!=ids[1]
        assert all(get_db().get_fundamentals_report(rid) is not None for rid in ids)
    finally:
        for rid in ids:get_db().delete_fundamentals_report(rid)


def test_json_repair_keeps_business_tools_closed(exploration: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.agent.llm_adapter import ToolCall
    from src.agent.tools.registry import ToolDefinition
    registry = ToolRegistry()
    searches: list[int] = []
    def search(**kwargs: Any) -> dict[str, str]:
        searches.append(1)
        return {'evidence': 'fixture'}
    registry.register(ToolDefinition('search_comprehensive_intel', 'fixture', [], search))
    monkeypatch.setattr(explore_agents, '_registry_source', lambda: registry)
    enabled: list[bool] = []
    class Model:
        def call_with_tools(self, messages: Any, tools: Any, **kwargs: Any) -> LLMResponse:
            enabled.append(bool(tools))
            if tools:
                return LLMResponse(provider='fixture', tool_calls=[ToolCall('s1', 'search_comprehensive_intel', {})])
            return LLMResponse(provider='fixture', content='not-json' if len(enabled) == 2 else '{"narrative":"fixture"}')
    out = explore_agents._run_explore('business', {'search_comprehensive_intel'}, 'fixture', Model(), deadline=monotonic()+30)
    assert out['ok']
    assert enabled == [True, False, False]
    assert searches == [1]


@pytest.mark.parametrize('payload', [
    {'business_model': {}, 'main_products': {}, 'competitive_position': {}, 'vs_market_leader': {}, 'score': None, 'narrative': '无法取得业务资料'},
    {'business_model': {'说明': '已取得部分主营资料'}, 'data_gaps': ['收入结构缺失'], 'narrative': '仅部分资料可用'},
])
def test_missing_business_evidence_is_partial_and_not_cached(payload: dict[str, Any], financial_sources: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    import json
    from api.v1.endpoints.fundamentals import router
    from src.deep_research_dims.dim_cache import load_cached_dim, save_cached_dim
    from src.services import fundamentals_service as service
    from src.storage import get_db
    class Model:
        def call_with_tools(self, *args: Any, **kwargs: Any) -> LLMResponse:
            return LLMResponse(provider='fixture', content=json.dumps(payload))
    monkeypatch.setattr('src.services.deep_research_service._get_dual_track_adapter', Model)
    monkeypatch.setattr(service, '_REPORT_DIR', tmp_path/'reports')
    # Old caches must not turn missing evidence into a successful dimension.
    save_cached_dim('600519', 'business', dict(payload, status='ok'))
    assert load_cached_dim('600519', 'business') is None
    app = FastAPI()
    app.include_router(router, prefix='/api/v1/fundamentals')
    with TestClient(app) as client:
        out = client.post('/api/v1/fundamentals/generate', params={'stock_code': '600519', 'stock_name': '样本', 'force_refresh': True}).json()
        try:
            assert out['status'] == 'partial'
            assert out['dims']['business']['status'] == 'degraded'
            assert payload['narrative'] in out['markdown']
            assert load_cached_dim('600519', 'business') is None
            saved = client.get('/api/v1/fundamentals/reports/'+out['report_id']).json()
            assert saved['data']['markdown'] == out['markdown']
        finally:
            if out.get('report_id'): get_db().delete_fundamentals_report(out['report_id'])


@pytest.mark.parametrize('data_state', ['absent', 'empty', 'override'])
def test_business_prompt_survives_an_empty_data_volume(monkeypatch: pytest.MonkeyPatch, tmp_path: Any, data_state: str) -> None:
    from src.deep_research_dims import narrate
    monkeypatch.setattr(narrate, '_AGENTS_DIR', str(tmp_path))
    if data_state != 'absent':
        (tmp_path / 'business.md').write_text('custom business rules' if data_state == 'override' else '')
    captured: list[str] = []
    class Model:
        def call_with_tools(self, messages: Any, tools: Any, **kwargs: Any) -> LLMResponse:
            captured.append(messages[0]['content'])
            return LLMResponse(provider='fixture', content='{"narrative":"fixture"}')
    monkeypatch.setattr(explore_agents, '_registry_source', ToolRegistry)
    out = explore_agents.run_business_agent('300260', '样本', Model())
    assert out['ok'] and captured
    assert ('custom business rules' if data_state == 'override' else '禁止编造数字') in captured[0]


def test_missing_business_prompt_fails_before_model_call(exploration: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(explore_agents, 'load_dim_prompt', lambda _: '')
    out = explore_agents.run_business_agent('300260', '样本', object())
    assert not out['ok'] and '提示词缺失' in out['error']


@pytest.mark.parametrize('tls', [False, True])
@pytest.mark.parametrize('response_part', ['headers', 'body', 'chunk_header', 'gzip'])
def test_dripping_tool_response_ends_worker_and_closes_connection(response_part: str, tls: bool, tmp_path: Any) -> None:
    import requests
    from src.agent.runner import _execute_tools
    from src.agent.llm_adapter import ToolCall
    from src.agent.tools.registry import ToolDefinition
    release, disconnected, stopped = Event(), Event(), Event()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            try:
                if response_part == 'headers':
                    self.wfile.write(b'HTTP/1.1 200 OK\r\nX-Slow: ')
                else:
                    self.send_response(200)
                    if response_part == 'chunk_header':
                        self.send_header('Transfer-Encoding', 'chunked')
                    else:
                        self.send_header('Content-Length', '100000')
                    if response_part == 'gzip':
                        self.send_header('Content-Encoding', 'gzip')
                    self.end_headers()
                    if response_part == 'gzip':
                        # Valid gzip header with a slowly delivered optional filename.
                        self.wfile.write(b'\x1f\x8b\x08\x08\x00\x00\x00\x00\x00\x03')
                while not release.wait(0.025):
                    self.wfile.write(b'0' if response_part == 'chunk_header' else b'x')
                    self.wfile.flush()
            except OSError:
                disconnected.set()
        def log_message(self, *_args: Any) -> None:
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    if tls:
        import ssl
        import subprocess
        cert, key = tmp_path / 'cert.pem', tmp_path / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-keyout', str(key), '-out', str(cert), '-days', '1',
                        '-subj', '/CN=localhost'], check=True, capture_output=True)
        tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls_context.load_cert_chain(cert, key)
        server.socket = tls_context.wrap_socket(server.socket, server_side=True)
    server.daemon_threads = True
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    def search() -> str:
        try:
            with requests.Session() as client:
                client.trust_env = False
                return client.get(f"{'https' if tls else 'http'}://127.0.0.1:{server.server_port}", verify=False).text
        finally:
            stopped.set()
    registry = ToolRegistry()
    registry.register(ToolDefinition('search', 'fixture', [], search))
    try:
        _execute_tools([ToolCall('s1', 'search', {})], registry, 1, None, [], tool_wait_timeout_seconds=0.15)
        assert stopped.wait(0.5), 'Request worker remains alive after its deadline'
        assert disconnected.wait(0.5), 'Timed out connection was not closed'
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=1)
        assert stopped.wait(1)


def test_http_budget_preserves_session_redirect_cookies_and_unbudgeted_reads() -> None:
    import gzip
    import requests
    from src.patches.eastmoney_patch import eastmoney_patch, request_deadline
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def do_GET(self) -> None:
            if self.path == '/redirect':
                self.send_response(302)
                self.send_header('Set-Cookie', 'fixture=kept')
                self.send_header('Location', '/json')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            body = gzip.compress(b'{"ok":true}')
            if self.path == '/slow':
                sleep(0.1)
            self.send_response(200 if self.headers.get('Cookie') == 'fixture=kept' else 403)
            self.send_header('Content-Encoding', 'gzip')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *_args: Any) -> None:
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    eastmoney_patch(budget_only=True)
    try:
        with requests.Session() as client:
            client.trust_env = False
            base = f'http://127.0.0.1:{server.server_port}'
            token = request_deadline.set(monotonic() + 0.5)
            try:
                response = client.get(base + '/redirect', timeout=(1, 1))
                assert response.status_code == 200 and response.json() == {'ok': True}
                with client.get(base + '/json', stream=True) as streamed:
                    assert streamed.json() == {'ok': True}
            finally:
                request_deadline.reset(token)
            # Same pool, after the old deadline; no stale budget may affect it.
            sleep(0.5)
            assert client.get(base + '/slow', timeout=1).json() == {'ok': True}
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=1)
