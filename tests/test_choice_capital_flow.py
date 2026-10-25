"""Native transport and LLM fixtures preserve real flow mapping and consumption."""
from types import SimpleNamespace

import pandas as pd
import pytest

from data_provider.base import DataFetcherManager
from data_provider.choice_adapter import ChoiceAdapter
from src.agent.tools import data_tools
from src.config import Config
from src.agent.deep_research import explore_agents
from src.deep_research.researchers.capital import parse

DATES = ["2026-10-13", "2026-10-12", "2026-10-09", "2026-09-30", "2026-09-29", "2026-09-28", "2026-09-25", "2026-09-24", "2026-09-23", "2026-09-22"]


@pytest.fixture
def native_flow(monkeypatch):
    config = Config(data_source_profile="choice", deep_research_cross_validate=False)
    monkeypatch.setattr(Config, "_instance", config)
    calls = []
    dates = list(reversed(DATES))
    def request(method, args, options, deadline):
        calls.append(method)
        if method == "tradedates":
            return {"data": DATES}
        assert method == "csd" and args[1] == "NETINFLOW"
        return {"codes": [args[0]], "indicators": ["NETINFLOW"], "dates": dates, "data": {args[0]: [[float(i-2) for i in range(len(dates))]]}}
    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(request))
    manager = DataFetcherManager()
    def sector_transport(candidates):
        assert all("individual" not in name and "main_fund" not in name for name, _ in candidates)
        return pd.DataFrame([{"板块名称": "半导体", "主力净流入": 123}]), "public_sector", []
    monkeypatch.setattr(manager._fundamental_adapter, "_call_df_candidates", sector_transport)
    monkeypatch.setattr(data_tools, "_fetcher_manager_singleton", manager)
    return calls, dates


def test_real_flow_tool_uses_yuan_and_requires_complete_trading_windows(native_flow):
    calls, dates = native_flow
    result = data_tools._handle_get_capital_flow("300260")
    assert calls == ["tradedates", "csd"]
    assert (result["main_net_inflow"], result["inflow_5d"], result["inflow_10d"]) == (7, 25, 25)
    assert result["source_unit"] == "CNY"
    assert result["field_meta"]["main_inflow"]["rule_eligible"]
    assert result["coverage"]["10"]["complete"]
    assert result["latest_date"] == DATES[0]
    assert result["sector_rankings"]["top_inflow_sectors"][0]["name"] == "半导体"
    assert "daily_series" not in result
    assert not result["errors"]
    dates.remove(DATES[0])
    fresh = data_tools._handle_get_capital_flow("300260")
    assert calls == ["tradedates", "csd"] * 2
    assert fresh["latest_date"] == DATES[1]
    assert not fresh["coverage"]["5"]["complete"]
    assert fresh["main_net_inflow"] is fresh["inflow_5d"] is fresh["inflow_10d"] is None


def test_actual_researcher_consumer_removes_unsupported_scores(native_flow, monkeypatch):
    native_flow[1].remove(DATES[0])
    monkeypatch.setattr(data_tools, "_handle_get_stock_info", lambda *a, **k: {"fundamental_context": {"institution": {"data": {"top10_holder_change": -1323}, "field_meta": {}}}})
    def generate(**kwargs):
        return SimpleNamespace(success=True, content='{"flow_score": 95, "score": 90, "institution_score": 95, "institution_summary": "机构减持", "flow_summary": "主力买入", "narrative": "可加仓", "chip_score": 60}', total_steps=1, error=None)
    monkeypatch.setattr(explore_agents, "run_agent_loop", generate)
    result = explore_agents.run_capital_agent("300260", "新莱应材", object())
    dim, _ = parse(result["data"], 1)
    assert dim.status == "degraded"
    assert dim.flow_score is None and dim.score is None
    # issue #46：模型返回的筹码分在解析层丢弃，不能形成分数。
    assert not hasattr(dim, "chip_score")
    assert dim.institution_score is None
    assert "机构减持" not in dim.institution_summary
    assert "窗口" in dim.narrative
    assert "加仓" not in dim.narrative


def test_failed_calendar_never_falls_back_to_weekdays_or_legacy(native_flow, monkeypatch):
    def denied(*args):
        return {"error": "permission_denied"}
    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(denied))
    result = data_tools._handle_get_capital_flow("300260")
    assert "choice:permission_denied" in result["errors"]
    assert result["inflow_10d"] is None and result["coverage"] == {}

from tests.test_choice_integration import choice_transport

def test_research_orchestration_force_refresh_reaches_real_supply_tool_cache(choice_transport, monkeypatch, tmp_path):
    from src.agent.tools import data_tools, supply_chain_tools
    from src.agent.deep_research import orchestrator, explore_agents
    from src.deep_research_dims.context import SharedContext
    from data_provider.choice_adapter import ChoiceAdapter
    from data_provider.mx_data_adapter import financial_refresh
    manager = DataFetcherManager()
    monkeypatch.setattr(data_tools, "_fetcher_manager_singleton", manager)
    # Unrelated price/name/board I/O is not the financial cache under test.
    monkeypatch.setattr(manager, "get_realtime_quote", lambda *a, **k: None)
    monkeypatch.setattr(manager, "get_stock_name", lambda *a, **k: "fixture")
    monkeypatch.setattr(manager, "get_belong_boards", lambda *a, **k: [])
    for key in ("_STOCK_INFO_CACHE", "_STOCK_INFO_PAYLOAD", "_STOCK_INFO_INFLIGHT"):
        monkeypatch.setattr(supply_chain_tools, key, {})
    old = supply_chain_tools._fetch_real_stock_info("300260")
    assert old["fundamental_context"]["growth"]["data"]["roe"] == 0
    request = ChoiceAdapter._native_request
    def changed(method, args, options, deadline):
        raw = request(method, args, options, deadline)
        if "ROEWA" in raw["indicators"]:
            raw["data"][args[0]][raw["indicators"].index("ROEWA")] = 1.23
        return raw
    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(changed))
    assert supply_chain_tools._fetch_real_stock_info("300260")["fundamental_context"]["growth"]["data"]["roe"] == 0
    observed = []
    def generate(**kwargs):
        observed.append(supply_chain_tools._fetch_real_stock_info("300260")["fundamental_context"]["growth"]["data"]["roe"])
        return SimpleNamespace(success=True, content='{"company_position":"fixture"}', total_steps=1, error=None)
    monkeypatch.setattr(explore_agents, "run_agent_loop", generate)
    monkeypatch.setattr(orchestrator, "build_shared_context", lambda *a: SharedContext("300260", "fixture", "2026-10-13"))
    orchestrator.run_dual_track("300260", "fixture", object(), dims_filter={"supply_chain"}, force_refresh=True, skip_narrations=True, write_cache=False)
    assert observed == [1.23]
    assert financial_refresh.get() is None
