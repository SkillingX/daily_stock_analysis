"""Real supplier tables survive business research failure and report rendering."""
import pytest
from markdown2 import markdown
from data_provider.ifind_fundamental_adapter import IfindFetcher
from tests.test_ifind_choice_integration import ifind_transport
from tests.test_choice_integration import choice_transport


def test_products_survive_model_failure_and_render_in_existing_reports(ifind_transport, monkeypatch):
    from src.agent.deep_research import explore_agents
    from src.deep_research.researchers.business import parse
    from src.services.fundamentals_service import _render_markdown
    from src.agent.tools import data_tools
    from data_provider.base import DataFetcherManager
    config, response, calls = ifind_transport
    monkeypatch.setattr(data_tools, '_fetcher_manager_singleton', DataFetcherManager())
    # Replace the model boundary only; real supplier parser and report builders remain.
    monkeypatch.setattr(explore_agents, '_run_explore', lambda *a, **k: {'ok':False,'error':'model unavailable'})
    output = explore_agents.run_business_agent('300260','新莱应材',None, research={'company':{'MAINBUSINESS':'公司既有资料'}})
    dim, _ = parse(output['data'], 0)
    assert len(dim.product_segments.items) == 4
    first = dim.product_segments.items[0]
    assert first.name == '无菌包材'
    assert first.revenue.value == 1411150168.69
    assert first.revenue.unit == 'currency_base'
    assert first.report_date == '2025-12-31'
    assert dim.status == 'degraded'
    md = _render_markdown('新莱应材','300260','2026-10-14',{'business':dim.model_dump(),'financial':{},'sector':{}},'test')
    assert '14.11 亿元' in md and '2025-12-31' in md and 'iFinD' in md
    assert '公司既有资料' in md
    assert '部分覆盖' in md
    assert len([call for call in calls if '主营构成' in call[1]]) == 1


def test_product_join_rejects_cross_period_and_duplicate_names(ifind_transport):
    config, response, calls = ifind_transport
    for key, params in response['data']['indicators_params'].items():
        if '项目名称' in key:
            params['报告期'] = '20241231'
    data = IfindFetcher('https://fixture.invalid','test-only').product_segments('300260')
    assert not data.items
    assert data.errors


def test_product_empty_cells_do_not_shift_following_values(ifind_transport):
    config, response, calls = ifind_transport
    response['data']['answer'] = response['data']['answer'].replace('1411150168.6900|1028218656.3100','|1028218656.3100')
    data = IfindFetcher('https://fixture.invalid','test-only').product_segments('300260')
    assert data.items[0].revenue is None
    assert data.items[0].cost.value == 1028218656.31
    assert data.items[1].name == '高纯及超高纯应用材料'


def test_duplicate_product_names_are_not_double_counted(ifind_transport):
    config, response, calls = ifind_transport
    response['data']['answer'] = response['data']['answer'].replace('高纯及超高纯应用材料', '无菌包材')
    data = IfindFetcher('https://fixture.invalid','test-only').product_segments('300260')
    assert len(data.items) == 2
    assert all(item.name != '无菌包材' for item in data.items)
    assert 'product_identity_missing_or_duplicate' in data.errors


@pytest.fixture
def fundamentals_runtime(ifind_transport, monkeypatch, tmp_path):
    from data_provider.base import DataFetcherManager
    from data_provider.choice_adapter import ChoiceAdapter
    from src.agent.tools import data_tools
    from src.agent.deep_research import explore_agents
    from src.deep_research_dims import context
    from src.services import fundamentals_service as service
    from src.services import deep_research_service
    from src.storage import DatabaseManager
    import src.storage
    native = ChoiceAdapter._native_request

    def sdk(method, args, options, deadline):
        reply = native(method, args, options, deadline)
        if method == 'css' and 'MAINBUSINESS' in reply['indicators']:
            reply['data'][args[0]][reply['indicators'].index('MAINBUSINESS')] = '公司既有资料'
        return reply

    monkeypatch.setattr(ChoiceAdapter, '_native_request', staticmethod(sdk))
    manager = DataFetcherManager()
    monkeypatch.setattr(data_tools, '_fetcher_manager_singleton', manager)
    # Unrelated quote/history/chip transports and the model boundary only.
    monkeypatch.setattr(context, '_safe_quote', lambda *a: {})
    monkeypatch.setattr(context, '_safe_history', lambda *a: [])
    monkeypatch.setattr(context, '_safe_chip', lambda *a: {})
    monkeypatch.setattr(explore_agents, '_run_explore', lambda *a, **kw: {'ok': False, 'error': 'model unavailable'})
    monkeypatch.setattr(deep_research_service, '_get_dual_track_adapter', lambda: None)
    monkeypatch.setattr(service, '_REPORT_DIR', tmp_path / 'reports')
    database = DatabaseManager('sqlite:///' + str(tmp_path / 'reports.db'))
    monkeypatch.setattr(src.storage, 'get_db', lambda: database)
    return service, manager, database


def test_fundamental_force_refresh_updates_supplier_products(ifind_transport, fundamentals_runtime):
    config, response, calls = ifind_transport
    service, manager, database = fundamentals_runtime
    first = service.generate_fundamentals_report('300260', '新莱应材', force_refresh=True)
    old = first['dims']['business']['product_segments']['items'][0]['revenue']['value']
    response['data']['answer'] = response['data']['answer'].replace('1411150168.6900', '1511150168.6900')
    fresh = service.generate_fundamentals_report('300260', '新莱应材', force_refresh=True)
    product = fresh['dims']['business']['product_segments']['items'][0]
    assert product['revenue']['value'] == 1511150168.69 != old
    assert product['report_date'] == '2025-12-31'
    assert '15.11 亿元' in fresh['markdown']
    saved = database.get_fundamentals_report(fresh['report_id'])
    assert service.report_markdown(saved) == fresh['markdown']
    html = markdown(service.report_markdown(saved), extras=['tables'])
    products = html.split('主营分产品明细（iFinD）', 1)[1].split('</table>', 1)[0]
    body = products.split('<tbody>', 1)[1].split('</tbody>', 1)[0]
    assert body.count('<tr>') == 4


def test_product_failure_does_not_prevent_same_day_recovery(ifind_transport, fundamentals_runtime, monkeypatch):
    from data_provider.cross_source_validator import SourceReadError
    config, response, calls = ifind_transport
    service, manager, database = fundamentals_runtime
    query = IfindFetcher._async_query

    async def fail_products(self, tool, question, **kwargs):
        if '主营构成' in question:
            raise SourceReadError('ifind', 'timeout')
        return await query(self, tool, question, **kwargs)

    monkeypatch.setattr(IfindFetcher, '_async_query', fail_products)
    failed = service.generate_fundamentals_report('300260', '新莱应材', force_refresh=True)
    assert failed['dims']['business']['product_segments']['errors'] == ['ifind:timeout']
    assert '公司既有资料' in failed['markdown']
    monkeypatch.setattr(IfindFetcher, '_async_query', query)
    recovered = service.generate_fundamentals_report('300260', '新莱应材')
    assert recovered['report_id'] != failed['report_id']
    assert len(recovered['dims']['business']['product_segments']['items']) == 4


def test_invalid_product_rank_retains_other_products_and_choice(ifind_transport, fundamentals_runtime):
    config, response, calls = ifind_transport
    service, manager, database = fundamentals_runtime
    response['data']['answer'] = response['data']['answer'].replace('第1名', '第0名')
    report = service.generate_fundamentals_report('300260', '新莱应材', force_refresh=True)
    products = report['dims']['business']['product_segments']
    assert products['errors'] == ['invalid_rank']
    assert len(products['items']) == 3
    assert '公司既有资料' in report['markdown']


def test_supplier_numeric_overflow_retains_other_facts(ifind_transport, fundamentals_runtime):
    config, response, calls = ifind_transport
    service, manager, database = fundamentals_runtime
    response['data']['answer'] = response['data']['answer'].replace('1411150168.6900', '1e999999999亿')
    report = service.generate_fundamentals_report('300260', '新莱应材', force_refresh=True)
    product = report['dims']['business']['product_segments']['items'][0]
    assert product['revenue'] is None
    assert product['cost']['value'] == 1028218656.31
    assert '公司既有资料' in report['markdown']


def test_product_evidence_survives_guardrail_contract_roundtrip(ifind_transport, fundamentals_runtime):
    from src.schemas.deep_research_dims import parse_dim
    config, response, calls = ifind_transport
    config.financial_primary_source = 'ifind'
    service, manager, database = fundamentals_runtime
    report = service.generate_fundamentals_report('300260', '新莱应材', force_refresh=True)
    business = parse_dim('business', report['dims']['business'])
    assert len(business.product_segments.items) == 4
    assert business.product_segments.items[0].revenue.source == 'ifind'
