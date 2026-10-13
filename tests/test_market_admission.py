"""Market restrictions at submission, quote and portfolio boundaries."""

from concurrent.futures import ThreadPoolExecutor, Future
from datetime import date

import pandas as pd

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import get_config_dep
from src.config import Config


@pytest.fixture
def restricted_client(monkeypatch, tmp_path):
    from src.storage import DatabaseManager
    from src.services.task_queue import AnalysisTaskQueue
    monkeypatch.setattr(AnalysisTaskQueue, "_instance", None)
    DatabaseManager.reset_instance()
    config = Config(stock_list=["600519"], database_path=str(tmp_path / "test.db"), enabled_markets=("cn",))
    monkeypatch.setattr(Config, "_instance", config)
    monkeypatch.setenv("ADMIN_AUTH_ENABLED", "false")
    app = create_app(static_dir=tmp_path / "static")
    app.dependency_overrides[get_config_dep] = lambda: config
    yield TestClient(app)
    DatabaseManager.reset_instance()


def test_disabled_analysis_is_rejected_before_enqueuing(restricted_client, monkeypatch):
    def forbidden_submit(*args, **kwargs):
        pytest.fail("Disabled stocks must never enter the executor")

    monkeypatch.setattr(ThreadPoolExecutor, "submit", forbidden_submit)
    response = restricted_client.post(
        "/api/v1/analysis/analyze",
        json={"stock_codes": ["600519", "HK00700", "AAPL"], "async_mode": True},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "market_disabled"


@pytest.mark.parametrize("code", ["HK00700", "00700.HK", "AAPL", "BRK.B"])
def test_direct_quote_and_watchlist_cannot_bypass_market(restricted_client, code):
    for response in (
        restricted_client.get(f"/api/v1/stocks/{code}/quote"),
        restricted_client.get(f"/api/v1/stocks/{code}/history"),
        restricted_client.post("/api/v1/stocks/watchlist/add", json={"stock_code": code}),
    ):
        assert response.status_code == 400
        assert response.json()["error"] == "market_disabled"


def test_public_health_exposes_admitted_markets(restricted_client):
    assert restricted_client.get("/api/v1/health").json()["enabled_markets"] == ["cn"]


@pytest.mark.parametrize("markets", [(), ("cn", "other")])
def test_invalid_market_configuration_fails(markets):
    with pytest.raises(ValueError, match="ENABLED_MARKETS"):
        Config(enabled_markets=markets)


def test_declared_cn_cannot_disguise_a_us_trade(restricted_client):
    account = restricted_client.post("/api/v1/portfolio/accounts", json={"name": "test", "market": "cn", "base_currency": "CNY"}).json()
    response = restricted_client.post("/api/v1/portfolio/trades", json={"account_id": account["id"], "symbol": "AAPL", "market": "cn", "trade_date": "2026-10-12", "side": "buy", "quantity": 1, "price": 10})
    assert response.status_code == 400
    assert response.json()["error"] == "market_disabled"
    assert restricted_client.get("/api/v1/portfolio/trades").json()["items"] == []


def test_disabled_portfolio_is_readable_without_live_requests(restricted_client, monkeypatch):
    from src.storage import get_db
    from data_provider.base import DataFetcherManager
    from src.config import get_config
    config = get_config()
    config.enabled_markets = ("cn", "hk", "us")
    account = restricted_client.post("/api/v1/portfolio/accounts", json={"name": "historic", "market": "hk", "base_currency": "HKD"}).json()
    trade = restricted_client.post("/api/v1/portfolio/trades", json={"account_id": account["id"], "symbol": "00700", "market": "hk", "trade_date": "2026-01-01", "side": "buy", "quantity": 1, "price": 10})
    assert trade.status_code == 200
    get_db().save_daily_data(pd.DataFrame([{"date": date.today(), "open": 12, "high": 12, "low": 12, "close": 12, "volume": 1}]), code="00700", data_source="history")
    config.enabled_markets = ("cn",)
    def forbidden_quote(*args, **kwargs):
        pytest.fail("Historical portfolio reads cannot fetch a disabled live price")
    monkeypatch.setattr(DataFetcherManager, "get_realtime_quote", forbidden_quote)
    response = restricted_client.get("/api/v1/portfolio/snapshot", params={"account_id": account["id"]})
    assert response.status_code == 200
    position = response.json()["accounts"][0]["positions"][0]
    assert position["last_price"] == 12
    assert position["price_source"] == "history_close"


def test_queued_task_rechecks_market_before_execution(restricted_client, monkeypatch):
    from src.config import get_config
    from src.services.task_queue import get_task_queue
    captured = []
    def hold_submit(self, fn, *args, **kwargs):
        captured.append((fn, args, kwargs))
        return Future()
    monkeypatch.setattr(ThreadPoolExecutor, "submit", hold_submit)
    get_config().enabled_markets = ("cn", "hk", "us")
    response = restricted_client.post("/api/v1/analysis/analyze", json={"stock_code": "AAPL", "async_mode": True})
    assert response.status_code == 202
    get_config().enabled_markets = ("cn",)
    fn, args, kwargs = captured[0]
    fn(*args, **kwargs)
    task = get_task_queue().get_task(response.json()["task_id"])
    assert task.status.value == "failed"
    assert "us" in task.error


@pytest.mark.parametrize("handler", ["_handle_analyze_trend", "_handle_calculate_ma", "_handle_get_volume_analysis", "_handle_analyze_pattern"])
def test_technical_tools_reject_disabled_market_before_history(restricted_client, handler):
    from src.agent.tools import analysis_tools
    from src.core.market_policy import MarketDisabledError
    with pytest.raises(MarketDisabledError):
        getattr(analysis_tools, handler)("AAPL")


def test_supply_chain_and_alert_cannot_fetch_disabled_market(restricted_client):
    from src.agent.tools import supply_chain_tools
    from src.core.market_policy import MarketDisabledError
    with pytest.raises(MarketDisabledError):
        supply_chain_tools._fetch_real_stock_info("AAPL")
    response = restricted_client.post("/api/v1/alerts/rules", json={"name": "test", "target_scope": "single_symbol", "target": "AAPL", "alert_type": "price_cross", "parameters": {"direction": "above", "price": 10}})
    assert response.status_code == 400


def test_backtest_new_operations_are_restricted_but_history_remains_readable(restricted_client):
    response = restricted_client.post("/api/v1/backtest/run", json={"code": "AAPL"})
    assert response.status_code == 400
    assert restricted_client.get("/api/v1/backtest/results?code=AAPL").status_code == 200


def test_agent_can_read_disabled_market_history_without_acquiring_data(restricted_client, monkeypatch):
    from src.agent.tools import data_tools
    from src.storage import get_db
    get_db().save_daily_data(pd.DataFrame([
        {"date": date.today(), "open": 12, "high": 13, "low": 11, "close": 12, "volume": 100},
    ]), code="AAPL", data_source="history")
    monkeypatch.setattr(data_tools, "_get_fetcher_manager", lambda: pytest.fail("Historical read must not acquire data"))
    result = data_tools.get_analysis_context_tool.handler(stock_code="AAPL")
    assert result["code"] == "AAPL"
    assert result["today"]["close"] == 12
