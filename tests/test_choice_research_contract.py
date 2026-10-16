"""Research inputs and target references stay traceable through the report boundary."""
from datetime import date, timedelta

import pytest

from tests.test_choice_integration import choice_transport
from src.deep_research_dims.context import SharedContext, _safe_fundamental
from src.deep_research_dims.fundamental_dim import build_fundamental_dim
from src.deep_research_dims.sector_dim import build_sector_dim
from src.deep_research_dims.scenarios_dim import build_scenarios_dim
from src.deep_research_dims.signal_dim import build_signal_dim
from src.deep_research_dims.render import build_view, render_markdown, split_dim_sections
from src.deep_research_dims.manager_writeup import qualified_manager_writeup
from src.deep_research.researchers.ownership import parse
from src.schemas.deep_research_dims import DataDim
from tests.test_deep_research_dims import _all_default_dims


@pytest.mark.parametrize('failed_query,reason', [
    ('all', 'authentication_failed'),
    ('header', 'authentication_failed'),
    ('financials', 'timeout'),
    ('valuation', 'permission_denied'),
    ('targets', 'quota_exhausted'),
    ('empty', None),
])
def test_financial_query_errors_reach_only_affected_fields(choice_transport, monkeypatch, failed_query, reason):
    from data_provider.choice_adapter import ChoiceAdapter
    from data_provider.cross_source_validator import SourceReadError
    native = ChoiceAdapter._native_request

    def request(method, args, options, deadline):
        indicators = args[1] if method == 'css' else ''
        query = 'header' if 'LATESTREPORTNEW' in indicators else 'financials' if 'ROEWA' in indicators else 'valuation' if 'PETTM' in indicators else 'targets' if 'RATINGTARGETPRICE' in indicators else None
        if reason and (failed_query == 'all' or failed_query == query):
            raise SourceReadError('choice', reason)
        reply = native(method, args, options, deadline)
        if failed_query == 'empty' and query == 'financials':
            reply['data'][args[0]] = [None] * len(reply['indicators'])
        return reply

    monkeypatch.setattr(ChoiceAdapter, '_native_request', staticmethod(request))
    ctx = SharedContext('300398', '飞凯材料', date.today().isoformat())
    ctx.fundamental = _safe_fundamental(ctx.stock_code, ctx)
    dim = build_fundamental_dim(ctx)
    for field, evidence in dim.data_quality.fields.items():
        affected = failed_query == 'all' or (failed_query == 'valuation' and field in {'pe_ratio', 'pb_ratio'}) or (failed_query in {'header', 'financials'} and field not in {'pe_ratio', 'pb_ratio'})
        assert evidence.source_errors == ((f'choice:{reason}',) if affected else ())
        if affected:
            assert evidence.reading is None and not evidence.rule_eligible
        elif failed_query != 'empty' or field in {'pe_ratio', 'pb_ratio'}:
            assert evidence.reading is not None
        else:
            assert evidence.reading is None
            assert 'source_error' not in evidence.quality.reason_codes
    if failed_query == 'all':
        assert dim.data_quality.obtained_count == 0 and dim.health_score is None
        dims = _all_default_dims()
        dims['fundamental'] = dim
        markdown = render_markdown(build_view(ctx.stock_name, ctx.stock_code, ctx.as_of, [], dims, []))
        assert 'Choice：登录验证未通过' in markdown
        assert 'source_error' not in markdown and 'authentication_failed' not in markdown
        rows = [line for line in split_dim_sections(markdown)['fundamental'].splitlines() if line.startswith(('| PE |', '| PB |', '| ROE |', '| 毛利率 |', '| 营收同比 |', '| 归母净利同比 |'))]
        assert len(rows) == 6 and all(row.count('|') == 8 and row.endswith(' 否 |') for row in rows)


def test_choice_inputs_reach_financial_sector_and_target_report(choice_transport):
    ctx = SharedContext('300260', 'fixture', date.today().isoformat())
    ctx.quote = {'price': 50.0}
    ctx.fundamental = _safe_fundamental(ctx.stock_code, ctx)
    fund = build_fundamental_dim(ctx)
    assert fund.financial_safety['leverage'] == 65
    assert fund.financial_safety['period'] == '2026-06-30'
    sector = build_sector_dim(ctx)
    assert sector.sector_hint == '机械设备'
    scenario = build_scenarios_dim(ctx, 50.0, {'scenarios': [{'type': 'optimistic', 'probability': 1, 'value_anchor': 900}]})
    assert scenario.status == 'ok' and scenario.target_price == 60
    assert scenario.expected_value is None
    assert scenario.scenarios.scenarios[0].value_anchor is None
    assert scenario.analyst_target.indicator == 'RATINGTARGETPRICE'
    signal = build_signal_dim(ctx, {'conclusion': {'action': '观察'}}, scenario.model_dump())
    assert signal.target_price == 60 and signal.expected_value is None
    assert '三情景期望' not in signal.one_sentence
    dims = _all_default_dims()
    dims.update(fundamental=fund, sector=sector, scenarios=scenario, signal=signal)
    flow_text = 'Choice 主力净流入，截至2026-10-13：当日 4,531.71 万元；5交易日 12,547.58 万元；10交易日 -10,024.83 万元。单源数据。'
    dims['capital'] = dims['capital'].model_copy(update={'flow_summary': flow_text})
    view = build_view('fixture', '300260', ctx.as_of, [], dims, [], manager={'section_narratives': {'worldview': '机构盈利需要观察'}})
    markdown = render_markdown(view)
    assert '机构目标价综合值' in markdown and 'Choice / RATINGTARGETPRICE' in markdown
    assert '203.09 亿元' in markdown
    assert '期望值桥' not in markdown
    assert '资产负债率（%）' in markdown
    assert flow_text in markdown
    assert '| 60.0 元 | 50.00 元 | 70.00 元 |' in markdown


@pytest.mark.parametrize('changes', [
    {'targets': {'RATINGTARGETPRICE': None}},
    {'targets': {'RATINGTARGETPRICE': 10, 'RATINGTARGETPRICEMIN': 20}},
    {'targets': {'RATINGTARGETPRICE': 'NaN'}},
    {'as_of': (date.today() - timedelta(days=10)).isoformat()},
    {'as_of': (date.today() + timedelta(days=1)).isoformat()},
])
def test_invalid_missing_or_stale_target_cannot_become_model_valuation(changes):
    ctx = SharedContext('300260', 'fixture', date.today().isoformat())
    ctx.fundamental = {'research': {'source': 'choice', 'as_of': date.today().isoformat(), 'target_window_days': 180, 'targets': {'RATINGTARGETPRICE': 60}, **changes}}
    scenario = build_scenarios_dim(ctx, 50, {'scenarios': [{'probability': 1, 'value_anchor': 99}]})
    assert scenario.status == 'degraded'
    assert scenario.target_price is scenario.expected_value is None
    signal = build_signal_dim(ctx, {}, scenario.model_dump())
    assert signal.status == 'degraded' and signal.target_price is None


@pytest.mark.parametrize('failure,label', [
    ('authentication_failed', '登录验证'),
    ('permission_denied', '权限'),
    ('quota_exhausted', '额度'),
    ('timeout', '超时'),
    ('sdk_unavailable', '连接'),
    ('invalid_response', '查询未完成'),
])
@pytest.mark.parametrize('price', [None, 60.0])
def test_target_query_failure_is_not_reported_as_an_empty_window(failure, label, price):
    ctx = SharedContext('300398', '飞凯材料', date.today().isoformat())
    ctx.fundamental = {'research': {'source': 'choice', 'as_of': ctx.as_of,
                                  'target_window_days': 180, 'targets': {'RATINGTARGETPRICE': price},
                                  'errors': [f'choice:targets:{failure}']}}
    scenario = build_scenarios_dim(ctx, 27.73)
    assert scenario.status == 'degraded'
    assert scenario.target_price is scenario.expected_value is None
    assert label in scenario.degraded_reason
    assert '近180日窗口未提供机构目标价' not in scenario.degraded_reason
    assert failure not in scenario.narrative


def test_other_choice_failure_does_not_hide_successful_empty_target_window():
    ctx = SharedContext('300398', '飞凯材料', date.today().isoformat())
    ctx.fundamental = {'research': {'source': 'choice', 'as_of': ctx.as_of,
                                  'target_window_days': 180, 'targets': {},
                                  'errors': ['choice:industry:authentication_failed']}}
    scenario = build_scenarios_dim(ctx, 27.73)
    assert '近180日窗口未提供机构目标价' in scenario.degraded_reason
    assert 'authentication_failed' not in scenario.narrative


def test_missing_sector_and_ownership_are_not_called_complete():
    sector = build_sector_dim(SharedContext('300260', '新莱应材', date.today().isoformat()))
    assert sector.sector_hint == '' and sector.status == 'degraded'
    dim, _ = parse({'controller': '已知实控人', 'score': 90}, 1)
    assert dim.status == 'degraded' and dim.score is None
    assert '十大股东' in dim.data_gaps


def test_manager_cannot_replace_machine_price_order_with_false_comparison():
    dims = _all_default_dims()
    dims['data'] = DataDim(narrative='现价（49.8） < MA5（50.384） < MA10（55.721） < MA20（56.522）')
    manager = {'section_narratives': {'market': 'MA5（50.384）＞现价（49.8）＞MA10（55.721）＞MA20（56.522）'}}
    qualified = qualified_manager_writeup(manager, dims)
    assert qualified['section_narratives']['market'] == dims['data'].narrative


@pytest.mark.parametrize('section', ['sentiment', 'capital', 'technical', 'intel', 'business', 'ownership', 'supply_chain', 'us_china'])
def test_incomplete_valuation_preserves_research_but_rejects_manager_actions(section):
    dims = _all_default_dims()
    dims['scenarios'] = dims['scenarios'].model_copy(update={'status': 'degraded', 'degraded_reason': 'Choice未提供目标价'})
    dims[section] = dims[section].model_copy(update={'narrative': '独立研究原文与来源仍然保留'})
    action = '评级升为增持并开始建仓；持有者减仓离场'
    manager = {'section_narratives': {section: action}, 'section_titles': {section: action}}
    markdown = render_markdown(build_view('fixture', '300260', date.today().isoformat(), [], dims, [], manager=manager))
    assert action not in markdown
    assert '独立研究原文与来源仍然保留' in markdown


@pytest.mark.parametrize('price', [None, 0, 80.0, float('inf'), float('nan'), True])
def test_target_reference_never_promotes_missing_or_negative_upside(price):
    ctx = SharedContext('300260', 'fixture', date.today().isoformat(), quote={'price': price})
    payload = {'status': 'ok', 'target_price': 60.0, 'valuation_basis': 'analyst_consensus'}
    signal = build_signal_dim(ctx, {'conclusion': {'action': '持有'}}, payload)
    assert signal.rating == '中性'
    assert 'None%' not in signal.one_sentence
    if price != 80.0:
        assert signal.status == 'degraded'


def test_model_failure_preserves_choice_company_and_holder_evidence(choice_transport, monkeypatch):
    from src.agent.deep_research import explore_agents
    from src.agent.tools import data_tools
    monkeypatch.setattr(explore_agents, '_run_explore', lambda *a, **kw: {'ok': False, 'error': 'timeout'})
    monkeypatch.setattr(data_tools, '_handle_get_stock_info', lambda *a, **kw: {'fundamental_context': {'research': {'as_of': date.today().isoformat(), 'company': {'AHOLDER': '实控人甲', 'MAINBUSINESS': '制造设备'}, 'holders': {'HOLDNAMER': '股东甲;股东乙', 'period': '2026-06-30'}}}})
    ownership = explore_agents.run_ownership_agent('300260', 'fixture', None)
    assert ownership['ok'] and ownership['data']['status'] == 'degraded'
    assert ownership['data']['top_holders'][1]['name'] == '股东乙'
    assert '实控人甲' in ownership['data']['controller']
    business = explore_agents.run_business_agent('300260', 'fixture', None)
    assert business['data']['business_model']['value'] == '制造设备'
    assert business['data']['status'] == 'degraded'


@pytest.mark.parametrize('target_result', ['ok', 'empty', 'authentication_failed', 'timeout', 'financial_authentication_failed'])
def test_target_reference_survives_real_service_sse_and_persisted_readback(choice_transport, monkeypatch, tmp_path, target_result):
    import json
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.v1.endpoints import deep_research as endpoint
    from src.agent.deep_research import orchestrator
    from src.services import deep_research_service as service
    from src.storage import DatabaseManager
    from data_provider.choice_adapter import ChoiceAdapter
    native = ChoiceAdapter._native_request

    def request(method, args, options, deadline):
        if target_result == 'financial_authentication_failed' and method == 'css' and any(indicator in args[1] for indicator in ('LATESTREPORTNEW', 'ROEWA', 'PETTM')):
            return {'error': 'authentication_failed', 'native_code': 10001009}
        if method == 'css' and 'RATINGTARGETPRICE' in args[1]:
            if target_result in {'authentication_failed', 'timeout'}:
                return {'error': target_result, 'native_code': 10001009 if target_result == 'authentication_failed' else None}
            reply = native(method, args, options, deadline)
            if target_result == 'empty':
                reply['data'][args[0]] = [None, None, None]
            return reply
        return native(method, args, options, deadline)

    monkeypatch.setattr(ChoiceAdapter, '_native_request', staticmethod(request))
    choice_transport.deep_research_engine = 'dual_track'
    monkeypatch.setattr(choice_transport, 'is_agent_available', lambda: True)
    choice_transport.database_path = str(tmp_path / 'research.db')
    DatabaseManager.reset_instance()
    monkeypatch.setattr(service, '_DEEP_RESEARCH_DIR', tmp_path / 'reports')
    monkeypatch.setattr(service, '_get_dual_track_adapter', lambda: None)
    ctx = SharedContext('300260', 'fixture', date.today().isoformat(), quote={'price': 50.0})
    ctx.fundamental = _safe_fundamental('300260', ctx)
    # Freeze the upstream market snapshot, retaining real orchestration/serialization/storage.
    monkeypatch.setattr(orchestrator, 'build_shared_context', lambda *a, **kw: ctx)
    app = FastAPI()
    app.include_router(endpoint.router, prefix='/api/v1/deep-research')
    try:
        with TestClient(app) as client:
            response = client.post('/api/v1/deep-research/generate/stream', json={'stock_code': '300260', 'stock_name': 'fixture', 'force_refresh': True, 'dims': ['fundamental', 'scenarios']})
            events = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith('data:')]
            assert not [event for event in events if event['type'] == 'error'], events
            done = next(event for event in events if event['type'] == 'done')
            scenario = done['dimensions']['scenarios']
            reference = scenario['analyst_target']
            if target_result in {'ok', 'financial_authentication_failed'}:
                assert scenario['target_price'] == 60.0
                assert reference['as_of'] == date.today().isoformat() and reference['price'] == '60'
            else:
                assert scenario['status'] == 'degraded'
                assert scenario['target_price'] is reference is None
                assert ('近180日窗口未提供机构目标价' in scenario['narrative']) == (target_result == 'empty')
            report_id = done['report_id']
            reread = client.get(f'/api/v1/deep-research/reports/{report_id}').json()
            downloaded = client.get(f'/api/v1/deep-research/reports/{report_id}/markdown').text
            assert reread['data']['markdown'] == downloaded == done['markdown']
            if target_result == 'financial_authentication_failed':
                quality = reread['data']['dimensions']['fundamental']['data_quality']
                assert quality == done['dimensions']['fundamental']['data_quality']
                assert quality['obtained_count'] == 0
                assert all(field['source_errors'] == ['choice:authentication_failed'] for field in quality['fields'].values())
                assert 'Choice：登录验证未通过' in downloaded
                assert 'source_error' not in downloaded
                assert '|\n盈利质量：' not in downloaded
            dims_response = client.get(f'/api/v1/deep-research/reports/{report_id}/dims')
            assert dims_response.status_code == 200
            assert dims_response.json()['data']['dimensions']['scenarios']['analyst_target'] == reference
    finally:
        DatabaseManager.reset_instance()
