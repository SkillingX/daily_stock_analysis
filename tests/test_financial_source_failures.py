"""Source failure evidence and recovery through public financial query/verification.
Run: python -m pytest tests/test_financial_source_failures.py
External HTTP/MCP responses are isolated; real adapters and validator are retained.
"""
from unittest.mock import Mock

from data_provider.mx_data_adapter import MXClient, MXSource
from data_provider.cross_source_validator import CrossSourceValidator


def test_mx_quota_error_is_visible_and_not_cached(monkeypatch):
    from data_provider import mx_data_adapter as mx
    post = Mock(return_value={'success': False, 'status': 113, 'code': 113, 'message': '账户休眠，调用次数已达到上限', 'data': None})
    monkeypatch.setattr(mx, '_post', post)
    client = MXClient(api_key='fixture')
    assert client.query_financials('300260', None).get('error') == 'mx:quota_exhausted'
    validator = CrossSourceValidator([MXSource(client)])
    try:
        result = validator.verify('300260', 'roe')
        assert ('mx', 'quota_exhausted') in result.source_errors
        assert post.call_count == 2
    finally:
        validator._pool.shutdown()


def test_ifind_quota_response_survives_verification(monkeypatch):
    import json
    from data_provider.ifind_fundamental_adapter import IfindSource, _parse_ifind_response
    fetcher = Mock(available=True)
    fetcher.fetch.side_effect = lambda *args, **kwargs: _parse_ifind_response(json.dumps({'code': 1, 'msg': 'success', 'data': {'answer': '当前账户MCP请求用量已耗尽，可以升级权益。'}}), ['ROE'], 'roe', None)
    validator = CrossSourceValidator([IfindSource(fetcher)])
    try:
        result = validator.verify('300260', 'roe')
        assert ('ifind', 'quota_exhausted') in result.source_errors
        assert 'quota_exhausted' in result.quality.reason_codes
    finally:
        validator._pool.shutdown()


def test_fuyao_current_api_request_and_indicator_response(monkeypatch):
    import json
    from data_provider.fuyao_adapter import FuyaoFetcher, FuyaoSource
    response = Mock(status_code=200, text=json.dumps({'code': 0, 'message': 'success', 'data': {'thscode': '300260.SZ', 'report': '2025-4', 'abilities': [{'ability': 'profitability', 'indicators': [{'index_id': 'index_weighted_avg_roe', 'value': '4.56'}]}]}}))
    session = Mock()
    session.get.return_value = response
    monkeypatch.setattr(FuyaoFetcher, '_build_session', staticmethod(lambda: session))
    reading = FuyaoSource(FuyaoFetcher(endpoint='https://fixture', api_key='private-fixture')).read('300260', 'roe', '2025年报')
    assert reading.value == 4.56
    assert reading.period == '2025-12-31'
    assert reading.caliber == 'weighted_roe'
    assert reading.unit == 'percentage_point'
    args, kwargs = session.get.call_args
    assert args[0] == 'https://fixture/api/a-share/financials/indicators'
    assert kwargs['params'] == {'thscode': '300260.SZ', 'report': '2025-4'}
    assert kwargs['headers']['X-api-key'] == 'private-fixture'
    session.post.assert_not_called()


def test_mcp_nested_http_failure_preserves_safe_reason():
    import httpx
    from data_provider.mx_mcp_adapter import MxMcpSource
    response = httpx.Response(405, request=httpx.Request('POST', 'https://fixture'))
    error = ExceptionGroup('transport failed', [httpx.HTTPStatusError('private response detail', request=response.request, response=response)])
    fetcher = Mock(available=True)
    fetcher.fetch.side_effect = error
    validator = CrossSourceValidator([MxMcpSource(fetcher)])
    try:
        result = validator.verify('300260', 'roe')
        assert result.source_errors == (('mx_mcp', 'http_405'),)
        assert 'private response detail' not in str(result.to_compact())
    finally:
        validator._pool.shutdown()


def test_mx_http_auth_error_is_safe_and_isolated(monkeypatch):
    import requests
    response = Mock(status_code=401, text='authorization failed; token=PRIVATE_FIXTURE')
    monkeypatch.setattr(requests, 'post', lambda *args, **kwargs: response)
    validator = CrossSourceValidator([MXSource(MXClient(api_key='fixture'))])
    try:
        result = validator.verify('300260', 'roe')
        assert result.source_errors == (('mx', 'authentication_failed'),)
        assert 'PRIVATE_FIXTURE' not in str(result.to_compact())
    finally:
        validator._pool.shutdown()


def test_ifind_series_timeout_keeps_existing_capital_flow_contract(monkeypatch):
    from unittest.mock import AsyncMock
    from data_provider.ifind_fundamental_adapter import IfindFetcher
    from data_provider.capital_flow_provider import get_main_inflow_cumulative
    fetcher = IfindFetcher(endpoint='https://fixture', token='fixture')
    monkeypatch.setattr(fetcher, '_async_query', AsyncMock(side_effect=TimeoutError('private-failure')))
    monkeypatch.setattr(IfindFetcher, 'get_instance', classmethod(lambda cls: fetcher))
    assert get_main_inflow_cumulative('300260') == {}


def test_ifind_quota_is_visible_without_cross_validation():
    from data_provider.cross_source_validator import SourceReadError
    from data_provider.tushare_ifind_fundamental_adapter import TushareIfindFundamentalAdapter
    source = Mock(available=True)
    source.read.side_effect = SourceReadError('ifind', 'quota_exhausted')
    adapter = TushareIfindFundamentalAdapter(ifind_source=source)
    try:
        result = adapter.get_fundamental_bundle('300260', include_institution=False)
        assert result['status'] == 'not_supported'
        assert result['errors'] == [f'ifind:{field}=quota_exhausted' for field in ('revenue_yoy', 'net_profit_yoy', 'roe', 'gross_margin')]
    finally:
        adapter._pool.shutdown()


def test_fuyao_official_income_statement_metadata_and_period_selection():
    import json
    from data_provider.fuyao_adapter import _parse_fuyao_response
    from data_provider.cross_source_validator import adopted_field_record
    payload = {'code': 0, 'data': {'timestamp': 1735574400000, 'item': [
        {'thscode': '600519.SH', 'period': 'annual', 'period_end_ms': 1735574400000, 'currency': 'CNY', 'operating_income': 174144000000, 'parent_holder_net_profit': 86000000000},
        {'thscode': '600519.SH', 'period': 'quarterly', 'period_end_ms': 1719676800000, 'currency': 'CNY', 'operating_income': 81930000000, 'parent_holder_net_profit': 41700000000},
    ]}}
    for field, value, caliber in (('revenue', 174144000000, 'operating_revenue'), ('net_profit', 86000000000, 'parent_net_profit')):
        reading = _parse_fuyao_response(json.dumps(payload), field, '2025年报')
        assert reading.value == value
        assert reading.period == '2024-12-31' and reading.period_basis == 'annual'
        assert reading.unit == 'currency_base' and reading.currency == 'CNY'
        assert reading.caliber == caliber and reading.normalization_error is None
        assert adopted_field_record(field, reading)['rule_eligible'] is True
    quarterly = _parse_fuyao_response(json.dumps(payload), 'revenue', '2024中报')
    assert quarterly.period == '2024-06-30'
    assert quarterly.period_basis == 'unknown'
