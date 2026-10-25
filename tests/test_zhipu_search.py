"""Coding Plan replacement replays the MCP wire result through shared search."""
import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import httpx
from mcp.types import CallToolResult

from src.config import Config
from src.search_service import SearchService, ZhipuSearchProvider


@pytest.fixture
def zhipu_wire(monkeypatch):
    from src.services import mcp_client
    payload = json.loads((Path(__file__).parent / 'fixtures/zhipu_coding_search.json').read_text())
    calls = []

    async def call(endpoint, authorization, tool, arguments, *, timeout_seconds, deadline=None):
        calls.append((endpoint, authorization, tool, arguments, timeout_seconds, deadline))
        return CallToolResult.model_validate(payload)

    monkeypatch.setattr(mcp_client, 'call_mcp_tool', call)
    return payload, calls


def test_real_coding_plan_reply_maps_original_links_and_keeps_unknown_dates(zhipu_wire):
    service = SearchService(coding_plan_search_provider='zhipu', zhipu_api_key='test-only', searxng_public_instances_enabled=False)
    response = service.search_stock_events('300260', '新莱应材')
    assert response.success
    assert response.provider == 'Zhipu'
    assert response.results[0].url == 'https://data.eastmoney.com/notice/300260.html'
    assert response.results[0].source == 'data.eastmoney.com'
    assert response.results[0].published_date is None
    assert '半年' in response.results[0].snippet
    assert 'Zhipu' in response.to_context()
    assert zhipu_wire[1][0][2] == 'web_search_prime'


def test_tavily_residual_key_cannot_construct_or_call_a_provider(monkeypatch):
    import sys
    forbidden = MagicMock(side_effect=AssertionError('retired Tavily must not be called'))
    monkeypatch.setitem(sys.modules, 'tavily', forbidden)
    service = SearchService(tavily_keys=['leftover'], searxng_public_instances_enabled=False)
    assert 'Tavily' not in [provider.name for provider in service._providers]
    assert not Config(tavily_api_keys=['leftover'], searxng_public_instances_enabled=False).has_search_capability_enabled()
    forbidden.assert_not_called()


def test_replacement_keeps_other_providers_and_minimax_default(zhipu_wire):
    config = dict(anspire_keys=['a'], bocha_keys=['b'], brave_keys=['c'], serpapi_keys=['d'], minimax_keys=['e'], searxng_base_urls=['https://fixture.invalid'])
    original = SearchService(**config)
    replaced = SearchService(**config, coding_plan_search_provider='zhipu', zhipu_api_key='test-only')
    original_names = [provider.name for provider in original._providers]
    replaced_names = [provider.name for provider in replaced._providers]
    assert replaced_names == ['Zhipu' if name == 'MiniMax' else name for name in original_names]
    assert Config(coding_plan_search_provider='zhipu', zhipu_api_key='test-only', searxng_public_instances_enabled=False).has_search_capability_enabled()


def test_selected_zhipu_without_key_is_configuration_error():
    with pytest.raises(ValueError, match='LLM_ZHIPU_API_KEY'):
        Config(coding_plan_search_provider='zhipu')


@pytest.mark.parametrize('payload', [{}, {'content':[{'type':'text','text':'not json'}]}, {'content':[{'type':'text','text':'[]'}]}, {'content':[{'type':'text','text':'[{"title":"x","link":"javascript:alert(1)","content":"x"}]'}]}, {'isError':True,'content':[{'type':'text','text':'credential must not escape'}]}])
def test_invalid_empty_and_mcp_errors_are_safe_failures(zhipu_wire, payload):
    zhipu_wire[0].clear()
    zhipu_wire[0].update(payload)
    result = ZhipuSearchProvider(['test-only']).search('300260')
    assert not result.success
    assert not result.results
    assert 'credential must not escape' not in (result.error_message or '')


def test_expired_deadline_skips_wire_request(zhipu_wire):
    from src.patches.eastmoney_patch import request_deadline
    token = request_deadline.set(time.monotonic() - 1)
    try:
        response = ZhipuSearchProvider(['test-only']).search('300260')
    finally:
        request_deadline.reset(token)
    assert not response.success
    assert not zhipu_wire[1]


@pytest.mark.parametrize('status,reason', [(401, 'authentication_failed'), (403, 'authentication_failed'), (429, 'quota_exhausted')])
def test_wire_failures_are_classified_and_do_not_leak_key(monkeypatch, status, reason):
    from src.services import mcp_client

    async def failed(*args, **kwargs):
        request = httpx.Request('POST', ZhipuSearchProvider.API_ENDPOINT)
        raise httpx.HTTPStatusError('test-only must not escape', request=request, response=httpx.Response(status, request=request))

    monkeypatch.setattr(mcp_client, 'call_mcp_tool', failed)
    result = ZhipuSearchProvider(['test-only']).search('300260')
    assert not result.success
    assert result.error_message == f'zhipu:{reason}'


def test_news_rejects_undated_or_expired_results_but_research_keeps_undated(zhipu_wire, monkeypatch):
    from src.services.html_scrape_news import HTMLScrapeNewsProvider
    from src.search_service import SearchResponse
    # Isolate network transport of the unrelated final fallback.
    monkeypatch.setattr(HTMLScrapeNewsProvider, '_do_search', lambda self, query, *a, **k: SearchResponse(query, [], 'html_scrape', success=False))
    payload, calls = zhipu_wire
    undated = {'title':'新莱应材300260主营业务', 'link':'https://example.com/research', 'content':'主营业务和生产情况'}
    old = {**undated, 'title':'新莱应材300260旧新闻', 'published_date':'2000-01-01'}
    payload['content'] = [{'type':'text', 'text':json.dumps([undated, old])}]
    service = SearchService(coding_plan_search_provider='zhipu', zhipu_api_key='test-only', searxng_public_instances_enabled=False)
    assert not service.search_stock_news('300260', '新莱应材').results
    research = service.search_stock_events('300260', '新莱应材')
    assert research.success and any(item.published_date is None for item in research.results)


def test_singleton_reload_changes_search_provider_without_old_cache(monkeypatch):
    from src import search_service
    config = Config(minimax_api_keys=['test-only'], searxng_public_instances_enabled=False)
    monkeypatch.setattr('src.config.get_config', lambda: config)
    search_service.reset_search_service()
    original = search_service.get_search_service()
    original._cache['test'] = (time.time(), None)
    config.coding_plan_search_provider = 'zhipu'
    config.zhipu_api_key = 'test-only'
    search_service.reset_search_service()
    try:
        updated = search_service.get_search_service()
        assert updated is not original and not updated._cache
        assert 'Zhipu' in [p.name for p in updated._providers]
        assert 'MiniMax' not in [p.name for p in updated._providers]
    finally:
        search_service.reset_search_service()


def test_http_mcp_handshake_and_ifind_use_same_bounded_transport(monkeypatch):
    from data_provider.ifind_fundamental_adapter import IfindFetcher
    from mcp.shared import _httpx_utils
    import asyncio

    calls = []
    def wire(request):
        data = json.loads(request.content)
        calls.append((request.headers['Authorization'], data, request.extensions['timeout']))
        if 'id' not in data:
            return httpx.Response(202)
        if data['method'] == 'initialize':
            result = {'protocolVersion':data.get('params', {}).get('protocolVersion'), 'capabilities':{}, 'serverInfo':{'name':'fixture', 'version':'1'}}
        elif data['method'] == 'tools/list':
            result = {'tools':[]}
        else:
            result = {'content':[{'type':'text', 'text':'{"code":1,"data":{"answer":"ok","indicators_params":{}}}'}]}
        return httpx.Response(200, json={'jsonrpc':'2.0','id':data['id'],'result':result})

    monkeypatch.setattr(_httpx_utils, 'create_mcp_http_client', lambda headers: httpx.AsyncClient(headers=headers, transport=httpx.MockTransport(wire)))
    deadline = time.monotonic() + 5
    result = asyncio.run(IfindFetcher('https://fixture.invalid/mcp', 'Bearer test-only')._async_query('get_stock_financials', '300260', deadline=deadline))
    assert result['code'] == 1
    tool_call = next(data for _, data, _ in calls if data['method'] == 'tools/call')
    assert tool_call['params'] == {'name':'get_stock_financials','arguments':{'query':'300260'}}
    assert all(auth == 'Bearer test-only' for auth, _, _ in calls)
    assert all(0 < timeout <= 5 for _, _, parts in calls for timeout in parts.values())


@pytest.mark.parametrize('entrypoint', ['singleton', 'pipeline', 'market_review', 'hotspot'])
def test_all_runtime_factories_forward_the_same_search_selection(monkeypatch, entrypoint, zhipu_wire):
    from src import search_service
    from src.core import pipeline, market_review_runtime
    from src.services import alphasift_service
    config = Config(coding_plan_search_provider='zhipu', zhipu_api_key='test-only', searxng_public_instances_enabled=False)
    monkeypatch.setattr('src.config.get_config', lambda: config)
    built = []

    def create(**kwargs):
        service = SearchService(**kwargs)
        built.append(service)
        return service

    monkeypatch.setattr(search_service, 'SearchService', create)
    monkeypatch.setattr('src.notification.NotificationService', MagicMock())
    if entrypoint == 'singleton':
        search_service.reset_search_service()
        search_service.get_search_service()
        search_service.reset_search_service()
    elif entrypoint == 'pipeline':
        monkeypatch.setattr(pipeline, 'SearchService', create)
        for name in ('get_db', 'DataFetcherManager', 'StockTrendAnalyzer', 'GeminiAnalyzer', 'NotificationService'):
            monkeypatch.setattr(pipeline, name, MagicMock())
        pipeline.StockAnalysisPipeline(config=config)
    elif entrypoint == 'market_review':
        market_review_runtime.build_market_review_runtime(config)
    else:
        alphasift_service._build_hotspot_event_routes_from_search('新莱应材', config)
    assert len(built) == 1
    # html_scrape 搜索回退已随 issue #47 退役，套餐入口失败不再回退。
    assert [p.name for p in built[0]._providers] == ['Zhipu']


def test_settings_and_status_do_not_offer_retired_search_channels():
    from src.core.config_registry import get_registered_field_keys
    from bot.commands.status import StatusCommand
    # issue #47：退休渠道不出现在设置页与状态展示。
    assert 'TAVILY_API_KEYS' not in get_registered_field_keys()
    config = Config(tavily_api_keys=['leftover'], bocha_api_keys=['leftover'], coding_plan_search_provider='zhipu', zhipu_api_key='test-only')
    status = StatusCommand()._collect_status(config)
    assert status['search_zhipu'] and not status['search_minimax']
    formatted = StatusCommand()._format_status(status, 'telegram')
    for retired in ('Tavily', 'Bocha', 'Brave', 'SerpAPI', 'SearXNG'):
        assert retired not in formatted


def test_readonly_provider_credentials_are_masked_before_reaching_settings(tmp_path, monkeypatch):
    from src.core.config_manager import ConfigManager
    from src.services.system_config_service import SystemConfigService
    from src.core.config_registry import get_field_definition
    env = tmp_path / '.env'
    env.write_text('IFIND_MCP_TOKEN=private-fixture-token\nTAVILY_API_KEYS=retired-fixture-key\n')
    monkeypatch.setenv('ENV_FILE', str(env))
    result = SystemConfigService(manager=ConfigManager(env_path=env)).get_config()
    # 退休渠道不进入设置页载荷；保留渠道的密钥仍打码。
    assert all(item['key'] != 'TAVILY_API_KEYS' for item in result['items'])
    item = next(item for item in result['items'] if item['key'] == 'IFIND_MCP_TOKEN')
    assert item['value'] == '******' and item['is_masked']
    for name in ('FINANCIAL_PRIMARY_SOURCE', 'CODING_PLAN_SEARCH_PROVIDER'):
        assert all(option['label'] != option['value'] for option in get_field_definition(name)['options'])
