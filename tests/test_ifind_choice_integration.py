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


def test_ifind_primary_reaches_report_without_single_source_verification(ifind_transport, monkeypatch):
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
    # 合格 iFinD 字段不再重查 Choice，单一 iFinD 来源不得写成 verified
    assert snapshot['revenue'].quality.status != 'verified'
    assert snapshot['revenue'].rule_eligible
    assert '每个报告期' in calls[0][1]


def test_qualified_ifind_fields_are_not_requeried_from_choice(ifind_transport, monkeypatch):
    from data_provider.cross_source_validator import AnchorReading, SourceReadError
    from data_provider.ifind_fundamental_adapter import IfindFetcher

    config, response, calls = ifind_transport
    config.financial_primary_source = 'ifind'
    missing = "gross_margin"
    qualified = ("revenue", "net_profit", "roe", "revenue_yoy", "net_profit_yoy")
    readings = {
        field: AnchorReading(
            "ifind", 10.0, period="2025-12-31", period_basis="annual",
            statement_scope="consolidated", adjustment="restated",
            caliber="operating_revenue" if field == "revenue" else "weighted_roe" if field == "roe" else "operating_revenue_yoy" if field == "revenue_yoy" else "parent_net_profit_yoy" if field == "net_profit_yoy" else "parent_net_profit",
            unit="currency_base" if field in {"revenue", "net_profit"} else "percentage_point",
            currency="CNY" if field in {"revenue", "net_profit"} else None,
        )
        for field in qualified
    }
    monkeypatch.setattr(IfindFetcher, "financial_snapshot", lambda self, code, period, deadline: readings)

    def _unavailable(*args, **kwargs):
        raise SourceReadError("ifind", "invalid_response")

    monkeypatch.setattr(IfindFetcher, "product_segments", _unavailable)
    monkeypatch.setattr(IfindFetcher, "shareholders", _unavailable)
    seen = []
    native = ChoiceAdapter._native_request

    def sdk(method, args, options, deadline):
        raw = native(method, args, options, deadline)
        if method == "css":
            seen.append(args[1] if len(args) > 1 else "")
            indicators = raw.get("indicators") or []
            if "LATESTREPORTNEW" in indicators:
                raw["data"][args[0]][indicators.index("LATESTREPORTNEW")] = "2026/06/30"
        return raw

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(sdk))
    result = DataFetcherManager().get_fundamental_context("300260", budget_seconds=30)
    requested = " ".join(seen)
    for indicator in ("OPERATEREVENEW", "PARENTNETPROFITNEW", "ROEWA", "YOYOR", "YOYPNI"):
        assert indicator not in requested
    assert "GPMARGIN" in requested
    assert "PETTM" in requested and "MV" in requested
    assert "HISSW2021" in requested and "RATINGTARGETPRICE" in requested
    assert result["growth"]["data"]["report_date"] == "2025-12-31"
    assert result["growth"]["field_meta"]["revenue"]["source"] == "ifind"
    assert result["growth"]["field_meta"]["revenue"]["period"] == "2025-12-31"
    assert result["growth"]["data"][missing] is None
    assert result["growth"]["field_meta"]["revenue"]["quality"]["status"] != "verified"


def test_qualified_older_period_ifind_field_stays_adopted_and_unqueried(ifind_transport, monkeypatch):
    from data_provider.cross_source_validator import AnchorReading, SourceReadError
    from data_provider.ifind_fundamental_adapter import IfindFetcher

    config, response, calls = ifind_transport
    config.financial_primary_source = 'ifind'

    def _reading(field: str, period: str) -> AnchorReading:
        return AnchorReading(
            "ifind", 10.0, period=period, period_basis="annual",
            statement_scope="consolidated", adjustment="restated",
            caliber="operating_revenue" if field == "revenue" else "weighted_roe" if field == "roe" else "operating_revenue_yoy" if field == "revenue_yoy" else "parent_net_profit_yoy" if field == "net_profit_yoy" else "parent_net_profit",
            unit="currency_base" if field in {"revenue", "net_profit"} else "percentage_point",
            currency="CNY" if field in {"revenue", "net_profit"} else None,
        )

    # revenue 合格但财期早于其他合格字段：仍不重查 Choice，也不因快照财期更新丢成缺项
    readings = {"revenue": _reading("revenue", "2025-12-31")}
    readings.update({field: _reading(field, "2026-06-30") for field in ("net_profit", "roe", "revenue_yoy", "net_profit_yoy")})
    monkeypatch.setattr(IfindFetcher, "financial_snapshot", lambda self, code, period, deadline: readings)

    def _unavailable(*args, **kwargs):
        raise SourceReadError("ifind", "invalid_response")

    monkeypatch.setattr(IfindFetcher, "product_segments", _unavailable)
    monkeypatch.setattr(IfindFetcher, "shareholders", _unavailable)
    seen = []
    native = ChoiceAdapter._native_request

    def sdk(method, args, options, deadline):
        raw = native(method, args, options, deadline)
        if method == "css":
            seen.append(args[1] if len(args) > 1 else "")
            indicators = raw.get("indicators") or []
            if "LATESTREPORTNEW" in indicators:
                raw["data"][args[0]][indicators.index("LATESTREPORTNEW")] = "2026/06/30"
        return raw

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(sdk))
    result = DataFetcherManager().get_fundamental_context("300260", budget_seconds=30)
    requested = " ".join(seen)
    assert "OPERATEREVENEW" not in requested
    assert "GPMARGIN" in requested  # 缺口字段仍重查并按同期补位
    assert result["growth"]["data"]["revenue"] == 10.0
    assert result["growth"]["field_meta"]["revenue"]["period"] == "2025-12-31"
    assert result["growth"]["field_meta"]["revenue"]["source"] == "ifind"
    assert result["growth"]["data"]["report_date"] == "2026-06-30"
    assert result["growth"]["field_meta"]["gross_margin"]["source"] == "choice"
    assert result["growth"]["field_meta"]["revenue"]["quality"]["status"] != "verified"


@pytest.mark.parametrize('ifind_period,choice_period,expected_source', [
    ('20260630', '20251231', 'ifind'),
    ('20251231', '20260630', 'ifind'),
    ('20271231', '20251231', 'choice'),
])
def test_primary_snapshot_uses_latest_real_period_not_supplier_order(ifind_transport, monkeypatch, ifind_period, choice_period, expected_source):
    """合格的当前 iFinD 字段不再为了找更新的 Choice 财期而重查。

    未来或不合格的 iFinD 行仍允许 Choice 补当前财期，且不倒退到更旧财期；
    合格字段不会被另一字段更新的 Choice 财期挤掉。
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
    # 合格 iFinD 值不再重查 Choice：无第二来源即无冲突，按单源披露且不标 verified
    assert snapshot['revenue'].quality.status == 'single_source'
    assert snapshot['revenue'].rule_eligible


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


def test_stock_info_deep_scope_zero_ifind_shareholders_full_positive(ifind_transport, monkeypatch):
    """B1（R9，iFinD 启用态口径）：get_stock_info 深研范围零 get_stock_shareholders
    出站、full 范围保留正调用——替换 enable_ifind_mcp=False 下的空洞零断言。

    iFinD 股东查询挂在 ifind-primary 聚合链（_ifind_research_omissions），
    full 正调用以 financial_primary_source='ifind' 为前提；深研零断言在
    include_holders=False 处强制，与配置无关地为真。"""
    from src.agent.tools import data_tools
    from src.core.research_run import ResearchRun, research_dimension

    config, response, calls = ifind_transport
    config.financial_primary_source = 'ifind'
    monkeypatch.setattr(data_tools, "_fetcher_manager_singleton", DataFetcherManager())
    with ResearchRun(120).bind():
        # 深研范围：零 iFinD 股东出站（iFinD 已启用且为主源，零是真实退役边界）
        research_dimension.set("capital")
        deep_info = data_tools._handle_get_stock_info("300260")
        assert deep_info.get("code") == "300260"
        assert [c for c in calls if c[0] == "get_stock_shareholders"] == [], "深研 scope 不得发起 iFinD 股东查询"
        # full 范围（dimension 空）：股东查询正调用被保留（普通消费者能力不变）
        research_dimension.set("")
        full_info = data_tools._handle_get_stock_info("300260")
        assert full_info.get("code") == "300260"
        assert any(c[0] == "get_stock_shareholders" for c in calls), "full 范围保留 iFinD 股东正调用"


def test_b1_lifecycle_stage0_stock_info_warm_force_parallel_scopes(ifind_transport, monkeypatch):
    """B1 冻结 AC（真实缝，msg_0f5f341a5878）：深研 stage0 冷聚合写 scope 缓存 →
    模型阶段 get_stock_info 热态命中（Choice CSD 出站零新增，证明真复用）；
    financial_refresh 强刷绕过缓存真实重新聚合（CSD 计数上升）时退役操作仍零；
    深研/普通两 scope 在同一 active ResearchRun 下经 copy_context 真实并行
    single_fetch（full 侧股东正调用保留、深研侧零）。research_dimension
    token/finally 恢复，不跨测试泄漏。"""
    from concurrent.futures import ThreadPoolExecutor
    from contextvars import copy_context

    from data_provider.choice_adapter import ChoiceAdapter
    from data_provider.mx_data_adapter import financial_refresh
    from src.agent.tools import data_tools
    from src.core.research_run import ResearchRun, research_dimension
    from src.deep_research_dims.context import SharedContext, _safe_fundamental

    config, response, calls = ifind_transport
    config.financial_primary_source = 'ifind'
    monkeypatch.setattr(data_tools, "_fetcher_manager_singleton", DataFetcherManager())
    # Choice 原生 CSD 计数：证明 warm 真命中缓存、force 真重聚合（不只看 holders==0）。
    wrapped_native = ChoiceAdapter._native_request
    csd_calls = {"count": 0}

    def counting_native(method, args, options, deadline):
        if method == "csd":
            csd_calls["count"] += 1
        return wrapped_native(method, args, options, deadline)

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(counting_native))

    def holders_count() -> int:
        return sum(1 for tool, _question in calls if tool == "get_stock_shareholders")

    with ResearchRun(120).bind():
        dimension_token = research_dimension.set("capital")
        try:
            # —— stage0 冷聚合（真实写深研 scope 缓存）——
            ctx = SharedContext("300260", "样本", "2026-10-29")
            ctx.fundamental = _safe_fundamental("300260", ctx)
            holders_after_stage0 = holders_count()
            assert holders_after_stage0 == 0, "深研 stage0 零股东出站"
            csd_after_stage0 = csd_calls["count"]
            assert csd_after_stage0 > 0, "stage0 冷聚合发生真实 Choice CSD 出站"
            # —— 模型阶段 stock_info 热态：命中 stage0 深研 scope 缓存 ——
            info = data_tools._handle_get_stock_info("300260")
            assert info.get("code") == "300260"
            assert holders_count() == holders_after_stage0, "stock_info 热态股东零新增"
            assert csd_calls["count"] == csd_after_stage0, "热态命中深研 scope 缓存，Choice 零新增出站"
            # —— financial_refresh 强刷：绕过缓存真实重新聚合，退役操作仍零 ——
            refresh_token = financial_refresh.set(("300260", "force-b1"))
            try:
                forced = data_tools._handle_get_stock_info("300260")
                assert forced.get("code") == "300260"
                assert holders_count() == holders_after_stage0, "强刷重聚合深研 scope 仍零股东出站"
                assert csd_calls["count"] > csd_after_stage0, "强刷确实绕过缓存重新聚合"
            finally:
                financial_refresh.reset(refresh_token)
        finally:
            research_dimension.reset(dimension_token)

        # —— 双消费者真实并行（copy_context 携带 active ResearchRun 进线程，
        # single_fetch 缝真实生效；deep/full run 键不同 → 各自聚合）——
        def run_scope(deep: bool) -> str:
            token = research_dimension.set("capital" if deep else "")
            try:
                assert current_research() is not None, "线程内经 copy_context 拿到 active ResearchRun"
                result = data_tools._get_fetcher_manager().get_fundamental_context(
                    "300260", include_sector_flows=not deep,
                )
                return str(result.get("market"))
            finally:
                research_dimension.reset(token)

        def current_research():
            from src.core.research_run import current_research as _current

            return _current.get()

        csd_before_parallel = csd_calls["count"]
        holders_before_parallel = holders_count()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(copy_context().run, run_scope, deep) for deep in (True, False)]
            markets = [future.result(timeout=60) for future in futures]
    assert markets == ["cn", "cn"], "两 scope 并行各自真实聚合成功"
    assert holders_count() == holders_before_parallel + 1, "并行后恰一次股东正调用（full 保留、深研零）"
    assert csd_calls["count"] > csd_before_parallel, "并行两 scope 各走真实聚合（不共享 same-fetch/缓存）"
