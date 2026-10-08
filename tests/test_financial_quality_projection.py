"""Verify current conflicts survive timestamp annotation and real tool projections.
Run: python -m pytest tests/test_financial_quality_projection.py
Requires project dependencies; synthetic source evidence and isolated transport boundaries.
"""
from datetime import datetime, timezone
from typing import Any
import pytest
from tests.test_fundamentals_data_quality import financial_sources
from data_provider.cross_source_validator import AnchorReading, AnchorQuality, adopted_field_record
from src.agent.tools import data_tools


def test_current_conflict_survives_real_manager_to_stock_tool(financial_sources: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    quote = financial_sources['quote']
    quote.pe_ratio = 999.0
    quote.pb_ratio = 2.0
    quote.provider_timestamp = datetime.now(timezone.utc).isoformat()
    quote.currency = 'CNY'
    quote.field_meta = {'pe_ratio': adopted_field_record('pe_ratio', AnchorReading('efinance', 999.0, caliber='TTM', unit='multiple', observed_at=quote.provider_timestamp), AnchorQuality(status='conflict'))}
    manager = data_tools._get_fetcher_manager()
    monkeypatch.setattr(manager, 'get_belong_boards', lambda *args: [])
    monkeypatch.setattr(manager, 'get_stock_name', lambda *args: '样本')
    raw = manager.get_fundamental_context('600519')
    assert raw['valuation']['field_meta']['pe_ratio']['quality']['status'] == 'conflict'
    assert not raw['valuation']['field_meta']['pe_ratio']['rule_eligible']
    response = data_tools._handle_get_stock_info('600519')
    record = response['fundamental_context']['valuation']['field_meta']['pe_ratio']
    assert record['value'] == 999
    assert record['quality']['status'] == 'conflict', record
    assert not record['rule_eligible']


def test_stock_tool_retains_conflict_from_acquired_context(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace
    from src.config import Config
    record = adopted_field_record('pe_ratio', AnchorReading('mx', 999, caliber='TTM', unit='multiple'), AnchorQuality(status='conflict'))
    raw = {'market': 'cn', 'status': 'partial', 'valuation': {'data': {'pe_ratio': 999, 'pb_ratio': 2, 'total_mv': 100, 'circ_mv': 50}, 'field_meta': {'pe_ratio': record}}}
    manager = SimpleNamespace(get_fundamental_context=lambda *args: raw, get_belong_boards=lambda *args: [], get_stock_name=lambda *args: '样本')
    monkeypatch.setattr(data_tools, '_fetcher_manager_singleton', manager)
    monkeypatch.setattr('src.config.get_config', lambda: Config(deep_research_cross_validate=False))
    response = data_tools._handle_get_stock_info('600519')
    current = response['fundamental_context']['valuation']['field_meta']['pe_ratio']
    assert current['quality']['status'] == 'conflict', current
    assert not current['rule_eligible']


def test_capital_tool_retains_conflict_from_acquired_context(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace
    from src.config import Config
    record = adopted_field_record('main_inflow', AnchorReading('mx', 100, unit='currency_base', currency='CNY', observed_at=datetime.now(timezone.utc).isoformat()), AnchorQuality(status='conflict'))
    raw = {'status': 'ok', 'data': {'stock_flow': {'main_net_inflow': 100, 'field_meta': {'main_inflow': record}}}}
    manager = SimpleNamespace(get_capital_flow_context=lambda *args: raw)
    monkeypatch.setattr(data_tools, '_fetcher_manager_singleton', manager)
    monkeypatch.setattr('src.config.get_config', lambda: Config(deep_research_cross_validate=False))
    response = data_tools._handle_get_capital_flow('600519')
    current = response['field_meta']['main_inflow']
    assert current['quality']['status'] == 'conflict', current
    assert not current['rule_eligible']
