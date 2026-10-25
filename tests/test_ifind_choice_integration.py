"""Replay supplier responses through the real manager and report evidence path."""
import json
from copy import deepcopy
from pathlib import Path

import pytest

from tests.test_choice_integration import choice_transport
from data_provider.base import DataFetcherManager
from data_provider.choice_adapter import ChoiceAdapter
from data_provider.ifind_fundamental_adapter import IfindFetcher


@pytest.fixture
def ifind_transport(choice_transport, monkeypatch):
    choice_transport.enable_ifind_mcp = True
    choice_transport.ifind_mcp_endpoint = 'https://fixture.invalid/mcp'
    choice_transport.ifind_mcp_token = 'test-only'
    response = json.loads((Path(__file__).parent / 'fixtures/ifind_financials.json').read_text())
    calls = []
    import efinance
    import pandas as pd
    monkeypatch.setattr(efinance.stock, 'get_belong_board', lambda *a, **k: pd.DataFrame())

    async def query(self, tool, question, *, deadline=None):
        calls.append((tool, question))
        return deepcopy(response)

    monkeypatch.setattr(IfindFetcher, '_async_query', query, raising=False)
    native = ChoiceAdapter._native_request

    def sdk(method, args, options, deadline):
        raw = native(method, args, options, deadline)
        if method == 'css':
            values = {'LATESTREPORTNEW': '2025/12/31', 'ACTUALISSUINGDATE': '2026/04/25',
                      'OPERATEREVENEW': 2997710000, 'PARENTNETPROFITNEW': 175060000,
                      'ROEWA': 8.65, 'YOYOR': 5.2194, 'YOYPNI': -22.656}
            raw['data'][args[0]] = [values.get(k, v) for k, v in zip(raw['indicators'], raw['data'][args[0]])]
        return raw

    monkeypatch.setattr(ChoiceAdapter, '_native_request', staticmethod(sdk))
    return choice_transport, response, calls


def test_real_multi_table_response_reaches_snapshot_and_verification(ifind_transport):
    config, response, calls = ifind_transport
    result = DataFetcherManager().get_fundamental_context('300260', budget_seconds=30)
    growth = result['growth']['data']
    assert growth['revenue'] == 2997710000
    assert growth['net_profit'] == 175060000
    assert result['growth']['field_meta']['revenue']['source'] == 'choice'
    reading = result['cross_validation']['anchors']['revenue']['readings'][1]
    assert reading['source'] == 'ifind'
    assert reading['value'] == 2997710000
    assert reading['period'] == '2025-12-31'
    assert reading['unit'] == 'currency_base'
    assert result['cross_validation']['anchors']['revenue']['quality']['status'] != 'verified'
    assert calls


def test_ifind_primary_reaches_report_with_choice_verification(ifind_transport, monkeypatch):
    from src.agent.tools import data_tools
    from src.deep_research_dims.context import SharedContext, _safe_fundamental
    from src.deep_research_dims.fundamental_dim import build_fundamental_dim

    config, response, calls = ifind_transport
    config.financial_primary_source = 'ifind'
    for params in response['data']['indicators_params'].values():
        params.update({'币种': 'CNY', '报表类型': '合并报表(调整)', '报告期': '20251231'})
    manager = DataFetcherManager()
    monkeypatch.setattr(data_tools, '_fetcher_manager_singleton', manager)
    ctx = SharedContext('300260', '新莱应材', '2026-10-15')
    ctx.fundamental = _safe_fundamental('300260', ctx)
    snapshot = build_fundamental_dim(ctx).financial_snapshot
    assert snapshot['revenue'].reading.source == 'ifind'
    assert snapshot['revenue'].reading.value == 2997710000
    assert snapshot['revenue'].quality.status == 'verified'
    assert snapshot['revenue'].rule_eligible
    assert '每个报告期' in calls[0][1]


def test_qualified_ifind_fields_are_not_requeried_from_choice(ifind_transport, monkeypatch):
    from data_provider.cross_source_validator import AnchorReading, SourceReadError
    from data_provider.ifind_fundamental_adapter import IfindFetcher

    config, response, calls = ifind_transport
    config.financial_primary_source = 'ifind'
    readings = {
        field: AnchorReading(
            "ifind", 10.0, period="2025-12-31", period_basis="annual",
            statement_scope="consolidated", adjustment="restated",
            caliber="operating_revenue" if field == "revenue" else "weighted_roe" if field == "roe" else "gross_margin" if field == "gross_margin" else "operating_revenue_yoy" if field == "revenue_yoy" else "parent_net_profit_yoy" if field == "net_profit_yoy" else "parent_net_profit",
            unit="currency_base" if field in {"revenue", "net_profit"} else "percentage_point",
            currency="CNY" if field in {"revenue", "net_profit"} else None,
        )
        for field in ("revenue", "net_profit", "roe", "gross_margin", "revenue_yoy", "net_profit_yoy")
    }
    monkeypatch.setattr(IfindFetcher, "financial_snapshot", lambda self, code, period, deadline: readings)

    def _unavailable(*args, **kwargs):
        raise SourceReadError("ifind", "invalid_response")

    monkeypatch.setattr(IfindFetcher, "product_segments", _unavailable)
    monkeypatch.setattr(IfindFetcher, "shareholders", _unavailable)
    seen = []
    native = ChoiceAdapter._native_request

    def sdk(method, args, options, deadline):
        if method == "css":
            seen.append(args[1] if len(args) > 1 else "")
        return native(method, args, options, deadline)

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(sdk))
    result = DataFetcherManager().get_fundamental_context("300260", budget_seconds=30)
    assert not any("OPERATEREVENEW" in str(item) or "ROEWA" in str(item) for item in seen)
    assert any("PETTM" in str(item) or "MV" in str(item) for item in seen)
    assert result["growth"]["field_meta"]["revenue"]["source"] == "ifind"
    assert result["growth"]["field_meta"]["revenue"]["quality"]["status"] != "verified"


@pytest.mark.parametrize('ifind_period,choice_period,expected_source', [
    ('20260630', '20251231', 'ifind'),
    ('20251231', '20260630', 'choice'),
    ('20271231', '20251231', 'choice'),
])
def test_primary_snapshot_uses_latest_real_period_not_supplier_order(ifind_transport, monkeypatch, ifind_period, choice_period, expected_source):
    """合格的当前 iFinD 字段不再为了找更新的 Choice 财期而重查。

    未来或不合格的 iFinD 行仍允许 Choice 补当前财期，且不倒退到更旧财期。
    """
    config, response, calls = ifind_transport
    config.financial_primary_source = 'ifind'
    response['data']['answer'] = response['data']['answer'].replace('20251231', ifind_period)
    for params in response['data']['indicators_params'].values():
        params.update({'币种': 'CNY', '报表类型': '合并报表(调整)', '报告期': ifind_period})
    native = ChoiceAdapter._native_request

    def sdk(method, args, options, deadline):
        raw = native(method, args, options, deadline)
        if method == 'css' and 'LATESTREPORTNEW' in raw['indicators']:
            raw['data'][args[0]][raw['indicators'].index('LATESTREPORTNEW')] = f'{choice_period[:4]}/{choice_period[4:6]}/{choice_period[6:]}'
        return raw

    monkeypatch.setattr(ChoiceAdapter, '_native_request', staticmethod(sdk))
    result = DataFetcherManager().get_fundamental_context('300260', budget_seconds=30)
    record = result['growth']['field_meta']['revenue']
    expected = ifind_period if expected_source == 'ifind' else choice_period
    assert record['source'] == expected_source
    assert record['period'] == f'{expected[:4]}-{expected[4:6]}-{expected[6:]}'
    assert result['cross_validation']['anchors']['revenue']['quality']['status'] != 'verified'


def test_new_period_unknown_ratios_are_not_filled_with_older_choice_values(ifind_transport):
    config, response, calls = ifind_transport
    config.financial_primary_source = 'ifind'
    response.clear()
    response.update(json.loads((Path(__file__).parent / 'fixtures/ifind_current_financials.json').read_text()))
    result = DataFetcherManager().get_fundamental_context('300260', budget_seconds=30)
    growth = result['growth']
    assert growth['data']['report_date'] == '2026-06-30'
    assert growth['field_meta']['revenue']['source'] == 'ifind'
    assert growth['data']['roe'] is None
    assert growth['data']['gross_margin'] is None
    assert 'balance_sheet' not in growth['data']
    assert result['cross_validation']['anchors']['roe']['readings']


def test_future_row_cannot_hide_valid_latest_period_in_same_batch(ifind_transport):
    config, response, calls = ifind_transport
    config.financial_primary_source = 'ifind'
    response['data']['answer'] = '| 证券代码 | 报告期 | 营业收入（单位：元） |\n| --- | --- | --- |\n| 300260.SZ | 20271231 | 9999999 |\n| 300260.SZ | 20260630 | 123456 |'
    response['data']['indicators_params'] = {'营业收入': {'单位':'元', '币种':'CNY', '报表类型':'合并报表(调整)'}}
    result = DataFetcherManager().get_fundamental_context('300260', budget_seconds=30)
    assert result['growth']['data']['report_date'] == '2026-06-30'
    assert result['growth']['data']['revenue'] == 123456
    assert result['growth']['field_meta']['revenue']['source'] == 'ifind'


def test_future_disclosure_cannot_verify_current_choice_value(ifind_transport, monkeypatch):
    from src.agent.tools import data_tools
    from src.deep_research_dims.context import SharedContext, _safe_fundamental
    from src.deep_research_dims.fundamental_dim import build_fundamental_dim
    config, response, calls = ifind_transport
    config.financial_primary_source = 'ifind'
    for params in response['data']['indicators_params'].values():
        params.update({'币种':'CNY', '报表类型':'合并报表(调整)', '报告期':'20251231'})
    response['data']['answer'] += '\n\n| 证券代码 | 定期报告实际披露日期 |\n| --- | --- |\n| 300260.SZ | 20270101 |'
    response['data']['indicators_params']['定期报告实际披露日期'] = {'报告期':'20251231'}
    monkeypatch.setattr(data_tools, '_fetcher_manager_singleton', DataFetcherManager())
    ctx = SharedContext('300260', '新莱应材', '2026-10-15')
    ctx.fundamental = _safe_fundamental('300260', ctx)
    field = build_fundamental_dim(ctx).financial_snapshot['revenue']
    assert field.reading.source == 'choice'
    assert field.reading.value == 2997710000
    assert field.quality.status != 'verified'
    assert 'disclosure_in_future' in field.quality.reason_codes


def test_ifind_primary_conflict_blocks_rules_without_replacing_main_value(ifind_transport, monkeypatch):
    from src.agent.tools import data_tools
    from src.deep_research_dims.context import SharedContext, _safe_fundamental
    from src.deep_research_dims.fundamental_dim import build_fundamental_dim
    config, response, calls = ifind_transport
    config.financial_primary_source = 'ifind'
    for params in response['data']['indicators_params'].values():
        params.update({'币种': 'CNY', '报表类型': '合并报表(调整)', '报告期': '20251231'})
    response['data']['answer'] = response['data']['answer'].replace('29.9771亿', '49.9771亿')
    monkeypatch.setattr(data_tools, '_fetcher_manager_singleton', DataFetcherManager())
    ctx = SharedContext('300260', '新莱应材', '2026-10-15')
    ctx.fundamental = _safe_fundamental('300260', ctx)
    snapshot = build_fundamental_dim(ctx).financial_snapshot
    assert snapshot['revenue'].reading.source == 'ifind'
    assert snapshot['revenue'].reading.value == 4997710000
    assert snapshot['revenue'].quality.status == 'conflict'
    assert not snapshot['revenue'].rule_eligible


def test_changing_primary_strategy_does_not_reuse_old_field_sources(ifind_transport):
    config, response, calls = ifind_transport
    for params in response['data']['indicators_params'].values():
        params.update({'币种': 'CNY', '报表类型': '合并报表(调整)', '报告期': '20251231'})
    manager = DataFetcherManager()
    old = manager.get_fundamental_context('300260', budget_seconds=30)
    config.financial_primary_source = 'ifind'
    new = manager.get_fundamental_context('300260', budget_seconds=30)
    assert old['data_contract'] != new['data_contract']
    assert old['growth']['field_meta']['revenue']['source'] == 'choice'
    assert new['growth']['field_meta']['revenue']['source'] == 'ifind'


def test_explicit_metadata_verifies_and_conflict_reaches_report(ifind_transport):
    from src.deep_research_dims.context import SharedContext, _safe_fundamental
    from src.deep_research_dims.fundamental_dim import build_fundamental_dim
    from src.agent.tools import data_tools
    config, response, calls = ifind_transport
    for params in response['data']['indicators_params'].values():
        params.update({'币种': 'CNY', '报表类型': '合并报表', '调整口径': '重述后', '报告期': '20251231'})
    manager = DataFetcherManager()
    result = manager.get_fundamental_context('300260', budget_seconds=30)
    assert result['cross_validation']['anchors']['revenue']['quality']['status'] == 'verified'
    assert result['cross_validation']['anchors']['roe']['quality']['status'] == 'verified'
    monkey_value = response['data']['answer'].replace('29.9771亿', '49.9771亿')
    response['data']['answer'] = monkey_value
    from data_provider.mx_data_adapter import financial_refresh
    token = financial_refresh.set(('300260', 'new-report'))
    original = data_tools._fetcher_manager_singleton
    data_tools._fetcher_manager_singleton = manager
    try:
        ctx = SharedContext('300260', '新莱应材', '2026-10-14')
        ctx.fundamental = _safe_fundamental('300260', ctx)
    finally:
        financial_refresh.reset(token)
        data_tools._fetcher_manager_singleton = original
    assert ctx.fundamental['revenue'] == 2997710000
    assert ctx.fundamental['field_meta']['revenue']['quality']['status'] == 'conflict'
    snapshot = build_fundamental_dim(ctx).financial_snapshot
    assert not snapshot['revenue'].rule_eligible
    assert snapshot['revenue'].quality.status == 'conflict'
    assert 'pb_ratio' not in snapshot


def test_report_keeps_secondary_evidence_and_safe_failure(ifind_transport, monkeypatch):
    from src.deep_research_dims.context import SharedContext, _safe_fundamental
    from src.deep_research_dims.fundamental_dim import build_fundamental_dim
    from src.agent.tools import data_tools
    from data_provider.cross_source_validator import SourceReadError
    from jinja2 import Environment, FileSystemLoader
    config, response, calls = ifind_transport
    manager = DataFetcherManager()
    monkeypatch.setattr(data_tools, '_fetcher_manager_singleton', manager)
    ctx = SharedContext('300260', '新莱应材', '2026-10-14')
    ctx.fundamental = _safe_fundamental('300260', ctx)
    snapshot = build_fundamental_dim(ctx).financial_snapshot
    rendered = Environment(loader=FileSystemLoader('templates')).get_template('financial_snapshot.j2').render(snapshot=snapshot)
    assert 'iFinD' in rendered or 'ifind' in rendered
    assert '29.9771亿' in rendered
    assert '2025-12-31' in rendered
    assert snapshot['revenue'].quality.status != 'verified'

    async def failed(self, *args, **kwargs):
        raise SourceReadError('ifind', 'authentication_failed')
    monkeypatch.setattr(IfindFetcher, '_async_query', failed)
    from data_provider.mx_data_adapter import financial_refresh
    token = financial_refresh.set(('300260', 'failure'))
    try:
        ctx.fundamental = _safe_fundamental('300260', ctx)
    finally:
        financial_refresh.reset(token)
    snapshot = build_fundamental_dim(ctx).financial_snapshot
    rendered = Environment(loader=FileSystemLoader('templates')).get_template('financial_snapshot.j2').render(snapshot=snapshot)
    assert snapshot['revenue'].reading.value == 2997710000
    assert snapshot['revenue'].source_errors == ('ifind:authentication_failed',)
    assert 'iFinD：登录验证未通过' in rendered
    assert 'authentication_failed' not in rendered
    assert snapshot['revenue'].quality.status != 'verified'


@pytest.mark.parametrize('reason', ['timeout', 'authentication_failed', 'quota_exhausted', 'invalid_response'])
def test_failed_verification_is_bounded_and_not_success_cached(ifind_transport, monkeypatch, reason):
    from data_provider.cross_source_validator import SourceReadError
    config, response, calls = ifind_transport
    async def failed(self, *args, **kwargs):
        raise SourceReadError('ifind', reason)
    monkeypatch.setattr(IfindFetcher, '_async_query', failed)
    manager = DataFetcherManager()
    result = manager.get_fundamental_context('300260', budget_seconds=30)
    assert result['growth']['data']['revenue'] == 2997710000
    assert f'ifind:{reason}' in result['errors']
    async def recovered(self, *args, **kwargs):
        calls.append((args[0], args[1]))
        return deepcopy(response)
    monkeypatch.setattr(IfindFetcher, '_async_query', recovered)
    recovered_result = manager.get_fundamental_context('300260', budget_seconds=30)
    assert calls
    assert f'ifind:{reason}' not in recovered_result['errors']


def test_unknown_or_older_metadata_cannot_fill_current_snapshot(ifind_transport, monkeypatch):
    config, response, calls = ifind_transport
    native = ChoiceAdapter._native_request
    def missing(method, args, options, deadline):
        raw = native(method, args, options, deadline)
        if method == 'css':
            vals = raw['data'][args[0]]
            for field in ('OPERATEREVENEW', 'PARENTNETPROFITNEW'):
                if field in raw['indicators']:
                    vals[raw['indicators'].index(field)] = None
        return raw
    monkeypatch.setattr(ChoiceAdapter, '_native_request', staticmethod(missing))
    # Real response has no currency, so unknown identity cannot fill money holes.
    assert DataFetcherManager().get_fundamental_context('300260', budget_seconds=30)['growth']['data'].get('revenue') is None
    for params in response['data']['indicators_params'].values():
        params.update({'币种':'CNY','报表类型':'合并报表(调整)','报告期':'20241231'})
    response['data']['answer'] = response['data']['answer'].replace('20251231', '20241231')
    assert DataFetcherManager().get_fundamental_context('300260', budget_seconds=30)['growth']['data'].get('revenue') is None


def test_missing_security_identity_is_not_reconstructed_from_query(ifind_transport):
    config, response, calls = ifind_transport
    response['data']['answer'] = response['data']['answer'].replace('300260.SZ', '')
    result = DataFetcherManager().get_fundamental_context('300260', budget_seconds=30)
    assert all(len(a['readings']) == 1 for a in result['cross_validation']['anchors'].values())


@pytest.mark.parametrize('answer', ['', '请求已完成，但没有返回指标表'])
def test_empty_financial_data_is_disclosed_and_retried(ifind_transport, answer):
    config, response, calls = ifind_transport
    response['data']['answer'] = answer
    response['data']['indicators_params'] = {}
    manager = DataFetcherManager()
    first = manager.get_fundamental_context('300260', budget_seconds=30)
    assert 'ifind:invalid_response' in first['errors']
    assert first['growth']['data']['revenue'] == 2997710000
    response.clear()
    response.update(json.loads((Path(__file__).parent / 'fixtures/ifind_financials.json').read_text()))
    recovered = manager.get_fundamental_context('300260', budget_seconds=30)
    assert 'ifind:invalid_response' not in recovered['errors']
    assert len(calls) == 2


def test_conflicting_duplicate_headers_cannot_verify_financials(ifind_transport):
    config, response, calls = ifind_transport
    response['data']['answer'] = '|证券代码|日期|营业收入（单位：元）|营业收入（单位：元）|\n|---|---|---|---|\n|300260.SZ|20251231|999亿|29.9771亿|'
    result = DataFetcherManager().get_fundamental_context('300260', budget_seconds=30)
    assert 'ifind:invalid_response' in result['errors']
    assert result['growth']['data']['revenue'] == 2997710000
    assert result['cross_validation']['anchors']['revenue']['quality']['status'] != 'verified'


def test_conflicting_same_period_tables_cannot_verify_financials(ifind_transport):
    config, response, calls = ifind_transport
    response['data']['answer'] += '\n\n|证券代码|日期|营业收入（单位：元）|\n|---|---|---|\n|300260.SZ|20251231|999亿|'
    result = DataFetcherManager().get_fundamental_context('300260', budget_seconds=30)
    assert 'ifind:invalid_response' in result['errors']
    assert result['growth']['data']['revenue'] == 2997710000
    assert result['cross_validation']['anchors']['revenue']['quality']['status'] != 'verified'


@pytest.mark.parametrize('seconds', [0, -1, float('nan'), float('inf')])
def test_enabled_configuration_rejects_invalid_deadlines(seconds):
    from src.config import Config
    with pytest.raises(ValueError, match='IFIND_MCP_TIMEOUT_SECONDS'):
        Config(data_source_profile='choice', enable_ifind_mcp=True,
               ifind_mcp_endpoint='https://fixture.invalid', ifind_mcp_token='test-only',
               ifind_mcp_timeout_seconds=seconds)


@pytest.mark.parametrize('settings', [
    {'financial_primary_source': 'unknown'},
    {'financial_primary_source': 'ifind'},
    {'financial_primary_source': 'ifind', 'data_source_profile': 'choice'},
])
def test_ifind_primary_requires_explicit_profile_and_enabled_credentials(settings):
    from src.config import Config
    with pytest.raises(ValueError, match='FINANCIAL_PRIMARY_SOURCE'):
        Config(**settings)


def test_real_current_amounts_verify_with_explicit_cny_deployment_policy(ifind_transport, monkeypatch):
    config, response, calls = ifind_transport
    actual = json.loads((Path(__file__).parent / 'fixtures/ifind_current_financials.json').read_text())
    response.clear()
    response.update(actual)
    native = ChoiceAdapter._native_request
    def current(method, args, options, deadline):
        raw = native(method, args, options, deadline)
        if method == 'css':
            values = {'LATESTREPORTNEW':'2026/6/30','ACTUALISSUINGDATE':'2026/8/29',
                      'OPERATEREVENEW':1545836236.66,'PARENTNETPROFITNEW':99959723.94,
                      'ROEWA':4.56,'GPMARGIN':23.419588,'YOYOR':9.687095,'YOYPNI':-7.517907}
            raw['data'][args[0]] = [values.get(k,v) for k,v in zip(raw['indicators'],raw['data'][args[0]])]
        return raw
    monkeypatch.setattr(ChoiceAdapter, '_native_request', staticmethod(current))
    result = DataFetcherManager().get_fundamental_context('300260', budget_seconds=30)
    anchors = result['cross_validation']['anchors']
    for field in ('revenue','net_profit'):
        assert anchors[field]['quality']['status'] == 'verified'
        assert anchors[field]['readings'][1]['currency'] == 'CNY'
        assert anchors[field]['readings'][1]['raw_currency'] is None
    for field in ('roe','gross_margin','revenue_yoy','net_profit_yoy'):
        assert anchors[field]['quality']['status'] == 'not_comparable'


@pytest.mark.parametrize('primary_source', ['choice', 'ifind'])
def test_financial_report_api_accepts_real_snapshot_and_roundtrip(ifind_transport, monkeypatch, tmp_path, primary_source):
    from src.agent.tools import data_tools
    from src.storage import DatabaseManager
    from src.services import financial_analysis_service as service
    from api.v1.endpoints import financial_analysis as endpoint
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    ifind_transport[0].financial_primary_source = primary_source
    for params in ifind_transport[1]['data']['indicators_params'].values():
        params.update({'币种': 'CNY', '报表类型': '合并报表(调整)', '报告期': '20251231'})
    monkeypatch.setattr(data_tools, '_fetcher_manager_singleton', DataFetcherManager())
    database = DatabaseManager('sqlite:///'+str(tmp_path/'reports.db'))
    monkeypatch.setattr(service, '_REPORT_DIR', tmp_path/'reports')
    monkeypatch.setattr(endpoint, 'get_db', lambda: database)
    import src.storage
    monkeypatch.setattr(src.storage, 'get_db', lambda: database)
    app = FastAPI()
    app.include_router(endpoint.router, prefix='/api/v1/financial-analysis')
    with TestClient(app) as client:
        generated = client.post('/api/v1/financial-analysis/generate?stock_code=300260&force_refresh=true')
        assert generated.status_code == 200, generated.text
        body = generated.json()
        assert body['analysis']['financial_snapshot']['revenue']['reading']['value'] == 2997710000
        assert body['analysis']['financial_snapshot']['revenue']['reading']['source'] == primary_source
        rid = body['report_id']
        reread = client.get(f'/api/v1/financial-analysis/reports/{rid}').json()
        downloaded = client.get(f'/api/v1/financial-analysis/reports/{rid}/markdown?download=1')
        assert body['markdown'] == reread['data']['markdown'] == downloaded.text
        import re
        assert re.search(r'2997710000|29\.9771\s*亿|29\.98\s*亿', body['markdown'])


def test_agent_stock_info_reuses_current_snapshot_quality(ifind_transport, monkeypatch):
    from src.agent.tools import data_tools
    config, response, calls = ifind_transport
    for params in response['data']['indicators_params'].values():
        params.update({'币种':'CNY','报表类型':'合并报表(调整)','报告期':'20251231'})
    manager = DataFetcherManager()
    monkeypatch.setattr(data_tools, '_fetcher_manager_singleton', manager)
    info = data_tools._handle_get_stock_info('300260')
    assert info['cross_validation']['anchors']['revenue']['quality']['status'] == 'verified'
    assert info['fundamental_context']['growth']['field_meta']['revenue']['quality']['status'] == 'verified'
    assert info['fundamental_context']['growth']['data']['revenue'] == 2997710000


def test_deep_report_retries_failed_verification_instead_of_day_cache(ifind_transport, monkeypatch, tmp_path):
    from data_provider.base import DataFetcherManager
    from data_provider.ifind_fundamental_adapter import IfindFetcher
    from data_provider.cross_source_validator import SourceReadError
    from src.agent.tools import data_tools
    from src.agent.deep_research import orchestrator
    from src.deep_research_dims import context
    from src.services import deep_research_service as service
    from src.storage import DatabaseManager
    import src.storage
    config, response, calls = ifind_transport
    config.deep_research_engine = 'dual_track'
    monkeypatch.setattr(data_tools, '_fetcher_manager_singleton', DataFetcherManager())
    monkeypatch.setattr(context, '_safe_quote', lambda *a: {})
    monkeypatch.setattr(context, '_safe_history', lambda *a: [])
    monkeypatch.setattr(context, '_safe_history_reports', lambda *a: [])
    monkeypatch.setattr(service, '_get_dual_track_adapter', lambda: None)
    monkeypatch.setattr(orchestrator, '_write_reflection_journals', lambda *a: None)
    monkeypatch.setattr(service, '_DEEP_RESEARCH_DIR', tmp_path / 'reports')
    monkeypatch.setattr(service, '_report_cache', {})
    database = DatabaseManager('sqlite:///' + str(tmp_path / 'reports.db'))
    monkeypatch.setattr(service, 'get_db', lambda: database)
    monkeypatch.setattr(src.storage, 'get_db', lambda: database)
    query = IfindFetcher._async_query
    async def failed(self, *a, **kw):
        raise SourceReadError('ifind', 'timeout')
    monkeypatch.setattr(IfindFetcher, '_async_query', failed)
    first = service.DeepResearchService().generate_report('300260', '新莱应材', dims=['fundamental'], force_refresh=True)
    assert first['dimensions']['fundamental']['financial_snapshot']['revenue']['source_errors']
    assert first['status'] == 'success'
    assert not service._report_cache
    # A success-shaped entry retained from older code must also be rejected on read.
    service._report_cache[service._date_key('300260', ['fundamental'])] = (first['report_id'], 0, first)
    monkeypatch.setattr(IfindFetcher, '_async_query', query)
    recovered = service.DeepResearchService().generate_report('300260', '新莱应材', dims=['fundamental'])
    assert not recovered.get('cache_hit')
    assert not recovered['dimensions']['fundamental']['financial_snapshot']['revenue']['source_errors']


def test_choice_expired_device_is_an_authentication_failure():
    from data_provider.choice_adapter import _native_error
    assert _native_error(10001020, 'userInfo has expired,please reactivate') == 'authentication_failed'


@pytest.mark.parametrize('candidate_period', ['older', 'unknown'])
def test_rejected_candidates_remain_evidence_without_report_or_agent_adoption(ifind_transport, monkeypatch, candidate_period):
    from src.agent.tools import data_tools
    from src.deep_research_dims.context import SharedContext, _safe_fundamental
    config, response, calls = ifind_transport
    for params in response['data']['indicators_params'].values():
        params.update({'币种': 'CNY', '报表类型': '合并报表(调整)'})
        if candidate_period == 'unknown':
            params['报告期'] = '最新一期'
    if candidate_period == 'unknown':
        response['data']['answer'] = response['data']['answer'].replace('20251231', '最新一期')
    native = ChoiceAdapter._native_request

    def missing(method, args, options, deadline):
        raw = native(method, args, options, deadline)
        if method == 'css':
            values = {'LATESTREPORTNEW': '2026/6/30' if candidate_period == 'older' else None,
                      'OPERATEREVENEW': None, 'PARENTNETPROFITNEW': None, 'ROEWA': None}
            raw['data'][args[0]] = [values.get(k, v) for k, v in zip(raw['indicators'], raw['data'][args[0]])]
        return raw

    monkeypatch.setattr(ChoiceAdapter, '_native_request', staticmethod(missing))
    manager = DataFetcherManager()
    monkeypatch.setattr(data_tools, '_fetcher_manager_singleton', manager)
    ctx = SharedContext('300260', '新莱应材', '2026-10-14')
    ctx.fundamental = _safe_fundamental('300260', ctx)
    assert all(ctx.fundamental[field] is None for field in ('revenue', 'net_profit', 'roe'))
    assert ctx.fundamental['cross_validation']['anchors']['revenue']['readings']
    info = data_tools._handle_get_stock_info('300260')
    assert all(info['fundamental_context']['growth']['data'].get(field) is None for field in ('revenue', 'net_profit', 'roe'))
