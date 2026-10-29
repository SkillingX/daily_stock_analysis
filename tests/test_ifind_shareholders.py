"""Shareholder identity, quantity and percentage use actual ranked supplier data."""
import json
from pathlib import Path
from data_provider.ifind_fundamental_adapter import IfindFetcher
from tests.test_ifind_choice_integration import ifind_transport
from tests.test_choice_integration import choice_transport


def holders_response(ifind_transport):
    config, response, calls = ifind_transport
    response.clear()
    response.update(json.loads((Path(__file__).parent/'fixtures/ifind_shareholders.json').read_text()))
    return response


def test_real_holder_names_quantities_and_ratios_join(ifind_transport):
    response = holders_response(ifind_transport)
    result = IfindFetcher('https://fixture.invalid','test-only').shareholders('300260','2025-12-31')
    assert len(result.items) == 10
    assert result.coverage == 'complete'
    assert result.items[0].name == '李水波'
    assert result.items[0].shares.value == 102200000
    assert result.items[0].ratio.value == 25.06
    assert result.items[0].shares.unit == 'shares'
    assert result.items[0].ratio.unit == 'percentage_point'
    assert result.items[0].ratio.value != 57.68


def test_latest_holders_are_not_locked_to_older_choice_period(ifind_transport):
    holders_response(ifind_transport)
    result = IfindFetcher('https://fixture.invalid', 'test-only').shareholders('300260')
    assert '最新已披露财期' in ifind_transport[2][-1][1]
    assert result.items[0].report_date == '2025-12-31'


def test_cross_period_holder_names_cannot_borrow_other_period_values(ifind_transport):
    response = holders_response(ifind_transport)
    for key, params in response['data']['indicators_params'].items():
        if '股东名称' in key:
            params['截止日期'] = '20241231'
    result = IfindFetcher('https://fixture.invalid','test-only').shareholders('300260','2025-12-31')
    assert result.coverage == 'partial'
    assert all(item.shares is None and item.ratio is None for item in result.items)
    assert all(item.report_date == '2024-12-31' for item in result.items)
