"""Runtime contracts affected by the governed-module type repair."""

from datetime import date
from threading import Event
from unittest.mock import MagicMock

import pytest

from src.agent.llm_adapter import LLMResponse, LLMToolAdapter, ToolCall
from src.agent.runner import run_agent_loop
from src.agent.tools.registry import ToolDefinition, ToolParameter, ToolRegistry
from src.agent.tools.supply_chain_tools import _format_fundamental_lines
from src.repositories.portfolio_repo import PortfolioRepository
from src.storage import (
    DatabaseManager,
    PortfolioCashLedger,
    PortfolioCorporateAction,
    PortfolioDailySnapshot,
    PortfolioPosition,
    PortfolioTrade,
)


def test_portfolio_queries_preserve_dates_accounts_and_nonzero_positions() -> None:
    DatabaseManager.reset_instance()
    db = DatabaseManager(db_url="sqlite:///:memory:")
    repo = PortfolioRepository(db)
    try:
        account = repo.create_account(name="active", broker=None, market="cn", base_currency="CNY")
        other = repo.create_account(name="other", broker=None, market="cn", base_currency="CNY")
        with db.get_session() as session:
            session.add_all([
                PortfolioTrade(account_id=account.id, symbol="600519", trade_date=date(2026, 1, 8), side="buy", quantity=1, price=10),
                PortfolioTrade(account_id=account.id, symbol="600519", trade_date=date(2026, 1, 12), side="sell", quantity=1, price=11),
                PortfolioTrade(account_id=other.id, symbol="600519", trade_date=date(2026, 1, 1), side="buy", quantity=1, price=9),
                PortfolioCashLedger(account_id=account.id, event_date=date(2026, 1, 5), direction="in", amount=100),
                PortfolioCorporateAction(account_id=account.id, symbol="600519", effective_date=date(2026, 1, 7), action_type="cash_dividend"),
                PortfolioDailySnapshot(account_id=account.id, snapshot_date=date(2026, 1, 8)),
                PortfolioDailySnapshot(account_id=account.id, snapshot_date=date(2026, 1, 12)),
                PortfolioPosition(account_id=account.id, symbol="600519", quantity=1),
                PortfolioPosition(account_id=account.id, symbol="000001", quantity=0),
                PortfolioPosition(account_id=other.id, symbol="000002", quantity=2),
            ])
            session.commit()
        assert repo.get_first_activity_date(account_id=account.id, as_of=date(2026, 1, 4)) is None
        assert repo.get_first_activity_date(account_id=account.id, as_of=date(2026, 1, 10)) == date(2026, 1, 5)
        assert len(repo.list_trades(account.id, date(2026, 1, 10))) == 1
        assert len(repo.list_cash_ledger(account.id, date(2026, 1, 10))) == 1
        assert len(repo.list_corporate_actions(account.id, date(2026, 1, 10))) == 1
        common = dict(account_id=account.id, date_from=date(2026, 1, 5), date_to=date(2026, 1, 8), page=1, page_size=10)
        assert repo.query_trades(**common, symbols=["600519"], side="buy")[1] == 1
        assert repo.query_cash_ledger(**common, direction="in")[1] == 1
        assert repo.query_corporate_actions(**common, symbols=["600519"], action_type="cash_dividend")[1] == 1
        assert repo.list_cached_position_identities(account_id=account.id) == [("cn", "600519")]
        snapshots = repo.list_daily_snapshots_for_risk(as_of=date(2026, 1, 10), cost_method="fifo", account_id=account.id)
        assert [row.snapshot_date for row in snapshots] == [date(2026, 1, 8)]
        for day in (8, 12):
            repo.save_fx_rate(from_currency="USD", to_currency="CNY", rate_date=date(2026, 1, day), rate=7, source="test")
        fx = repo.get_latest_fx_rate(from_currency="USD", to_currency="CNY", as_of=date(2026, 1, 10))
        assert fx is not None and fx.rate_date == date(2026, 1, 8)
    finally:
        DatabaseManager.reset_instance()


@pytest.mark.parametrize("bad_name", [None, "", 42, True, ["invalid"], {"invalid": "name"}])
def test_concept_names_keep_valid_strings_and_source(bad_name: object) -> None:
    parts: list[str] = []
    _format_fundamental_lines({"concept_membership": {
        "source": "ifind", "as_of_nature": "latest",
        "concepts": [{"name": "CPO"}, {}, {"name": bad_name}, {"name": "AI"}],
    }}, parts)
    assert parts == ["最新概念归属=CPO,AI（ifind，latest）"]


@pytest.mark.parametrize("count,exhausted", [(1, False), (2, False), (2, True)])
def test_runner_keeps_tool_call_identity_in_normal_and_budget_branches(monkeypatch: pytest.MonkeyPatch, count: int, exhausted: bool) -> None:
    registry = ToolRegistry()
    fast_done = Event()
    completed: list[str] = []

    def echo(message: str) -> dict[str, str]:
        if message == "first" and count == 2:
            assert fast_done.wait(2)
        completed.append(message)
        if message == "second":
            fast_done.set()
        return {"echo": message}

    registry.register(ToolDefinition(name="echo", description="echo", parameters=[ToolParameter(name="message", type="string", description="message")], handler=echo))
    calls = [ToolCall(id=name, name="echo", arguments={"message": name}) for name in ["first", "second"][:count]]
    clock = {"now": 0.0}
    adapter = MagicMock(spec=LLMToolAdapter)

    def respond(*args: object, **kwargs: object) -> LLMResponse:
        if adapter.call_with_tools.call_count == 1:
            clock["now"] = 2.0
            return LLMResponse(content="gather", tool_calls=calls, provider="test")
        return LLMResponse(content="done", tool_calls=[], provider="test")

    adapter.call_with_tools.side_effect = respond
    if exhausted:
        monkeypatch.setattr("src.agent.runner.time.monotonic", lambda: clock["now"])
    result = run_agent_loop(messages=[], tool_registry=registry, llm_adapter=adapter, max_steps=3, research_deadline=1.0 if exhausted else None)
    assert result.success
    messages = [message for message in result.messages if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in messages] == [call.id for call in calls]
    assert all(message["name"] == "echo" for message in messages)
    if exhausted:
        assert completed == []
        assert all("Research budget exhausted" in message["content"] for message in messages)
    else:
        assert completed == (["second", "first"] if count == 2 else ["first"])
