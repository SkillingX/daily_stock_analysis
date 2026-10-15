"""Cancellation closes dispatch without discarding already completed source facts."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from time import monotonic
from types import SimpleNamespace

import pytest

from data_provider.base import DataFetcherManager
from data_provider.choice_adapter import ChoiceAdapter
from src.core.research_run import ResearchRun, ResearchStopped
from tests.test_choice_integration import choice_transport
from tests.test_ifind_choice_integration import ifind_transport
from tests.test_choice_annual_reports import annual_transport


@pytest.mark.parametrize("cancel_at", [1, 2])
def test_snapshot_cancellation_keeps_completed_financials(choice_transport, monkeypatch, cancel_at):
    run = ResearchRun(10)
    native = ChoiceAdapter._native_request
    calls = []

    def request(method, args, options, deadline):
        calls.append(args)
        response = native(method, args, options, deadline)
        if len(calls) == cancel_at:
            run.stop("cancelled")
        return response

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(request))
    with run.bind():
        result = ChoiceAdapter().snapshot("300260", deadline=monotonic() + 5)
    assert len(calls) == cancel_at
    assert run.stop_reason == "cancelled"
    assert result["stock_name"] == "新莱应材"
    assert result["report_date"] == "2026-06-30"
    assert not result["errors"]  # Cancellation is not a failed supplier response.
    if cancel_at == 2:
        assert result["growth"]["roe"] == 0.0
        assert result["growth"]["net_profit_yoy"] == -7.517907
        assert result["field_meta"]["roe"]["period"] == "2026-06-30"


def test_cancelled_manager_returns_financial_facts_without_new_supplements(choice_transport, monkeypatch):
    from data_provider.ifind_fundamental_adapter import IfindFetcher
    choice_transport.enable_ifind_mcp = True
    choice_transport.ifind_mcp_endpoint = "https://fixture.invalid/mcp"
    choice_transport.ifind_mcp_token = "test-only"
    run = ResearchRun(10)
    native = ChoiceAdapter._native_request
    calls = []

    def request(method, args, options, deadline):
        calls.append(args)
        response = native(method, args, options, deadline)
        if "ROEWA" in args[1]:
            run.stop("cancelled")
        return response

    def forbidden(*args, **kwargs):
        pytest.fail("Cancellation must stop subsequent supplier and public supplement requests")

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(request))
    monkeypatch.setattr(IfindFetcher, "_async_query", forbidden)
    manager = DataFetcherManager()
    monkeypatch.setattr(manager, "_run_with_timeout", forbidden)
    monkeypatch.setattr(manager, "get_board_context", forbidden)
    monkeypatch.setattr(manager, "get_dragon_tiger_context", forbidden)
    monkeypatch.setattr(manager, "get_capital_flow_context", forbidden)
    with run.bind():
        result = manager.get_fundamental_context("300260", budget_seconds=5)
    assert len(calls) == 2
    assert result["growth"]["data"]["roe"] == 0.0
    assert result["growth"]["data"]["net_profit_yoy"] == -7.517907
    assert result["status"] == "partial"
    assert not manager._fundamental_cache


def test_annual_cancellation_preserves_first_completed_year(annual_transport, monkeypatch):
    run = ResearchRun(10)
    native = ChoiceAdapter._native_request
    calls = []

    def request(method, args, options, deadline):
        calls.append(args)
        response = native(method, args, options, deadline)
        run.stop("cancelled")
        return response

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(request))
    with run.bind():
        result = ChoiceAdapter().annual_reports("300260", deadline=monotonic() + 5)
    assert len(calls) == 1
    assert len(result["years"]) == 1
    assert result["years"][0]["op_cash_flow"] == -3.0
    assert result["years"][0]["eps"] == 0.0
    assert result["status"] == "partial"
    assert not result["errors"]


def test_waiting_native_request_cancels_without_touching_another_run(monkeypatch):
    """Exercise the real lock/dispatch boundary with two independent research runs."""
    entered, release, waiting = Event(), Event(), Event()
    native_lock = Lock()
    sent = []

    class ObservedLock:
        def acquire(self, *, timeout):
            if native_lock.locked():
                waiting.set()
            return native_lock.acquire(timeout=timeout)

        def release(self):
            native_lock.release()

    class Connection:
        def send(self, payload):
            sent.append(payload)
            entered.set()

        def poll(self, timeout):
            return release.wait(timeout)

        def recv(self):
            return {"codes": ["300260.SZ"], "indicators": ["NAME"], "data": {"300260.SZ": ["新莱应材"]}}

    monkeypatch.setattr(ChoiceAdapter, "_lock", ObservedLock())
    monkeypatch.setattr(ChoiceAdapter, "_process", SimpleNamespace(is_alive=lambda: True))
    monkeypatch.setattr(ChoiceAdapter, "_connection", Connection())
    owner, cancelled = ResearchRun(5), ResearchRun(5)

    def query(run):
        with run.bind():
            return ChoiceAdapter().query("css", ("300260.SZ", "NAME"), "", deadline=monotonic() + 3)

    with ThreadPoolExecutor(max_workers=2) as pool:
        active = pool.submit(query, owner)
        assert entered.wait(1)
        blocked = pool.submit(query, cancelled)
        assert waiting.wait(1)
        cancelled.stop("cancelled")
        try:
            with pytest.raises(ResearchStopped):
                blocked.result(timeout=0.5)
        finally:
            release.set()
        assert active.result(timeout=1).data == {"300260.SZ": ["新莱应材"]}
    assert len(sent) == 1
    assert owner.stop_reason is None


def test_ifind_primary_cancellation_keeps_response_and_skips_choice(ifind_transport, monkeypatch):
    config, response, calls = ifind_transport
    config.financial_primary_source = "ifind"
    for params in response["data"]["indicators_params"].values():
        params.update({"币种": "CNY", "报表类型": "合并报表(调整)", "报告期": "20251231"})
    run = ResearchRun(10)
    from data_provider.ifind_fundamental_adapter import IfindFetcher
    original = IfindFetcher._async_query

    async def query(*args, **kwargs):
        completed = await original(*args, **kwargs)
        run.stop("cancelled")
        return completed

    def forbidden(*args, **kwargs):
        pytest.fail("A completed iFinD response must not trigger Choice after cancellation")

    monkeypatch.setattr(IfindFetcher, "_async_query", query)
    monkeypatch.setattr(ChoiceAdapter, "_native_request", forbidden)
    with run.bind():
        manager = DataFetcherManager()
        result = manager.get_fundamental_context("300260", budget_seconds=5)
    assert result["growth"]["field_meta"]["revenue"]["source"] == "ifind"
    assert result["growth"]["data"]["revenue"] == 2997710000
    assert len(calls) == 1
    assert not manager._fundamental_cache


def test_cancelled_stage_zero_retains_completed_financial_and_quote_facts(choice_transport, monkeypatch):
    from src.agent.tools import data_tools
    from src.deep_research_dims import context
    run = ResearchRun(10)
    native = ChoiceAdapter._native_request
    quote_done = Event()

    def quote(*args):
        quote_done.set()
        return {"price": 52.0, "name": "新莱应材"}

    def request(method, args, options, deadline):
        response = native(method, args, options, deadline)
        if "ROEWA" in args[1]:
            assert quote_done.wait(1)
            run.stop("cancelled")
        return response

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(request))
    monkeypatch.setattr(data_tools, "_fetcher_manager_singleton", DataFetcherManager())
    monkeypatch.setattr(context, "_safe_quote", quote)
    monkeypatch.setattr(context, "_safe_history", lambda *a: [{"date": "2026-10-15", "close": 52.0}])
    monkeypatch.setattr(context, "_safe_chip", lambda *a: {})
    with run.bind():
        result = context.build_shared_context("300260", "新莱应材")
        assert result is run.context
        with pytest.raises(ResearchStopped):
            run.check()
    assert run.context.fundamental["roe"] == 0.0
    assert run.context.fundamental["net_profit_yoy"] == -7.517907
    assert run.context.quote["price"] == 52.0


def test_cancellation_after_full_snapshot_does_not_save_success_cache(choice_transport, monkeypatch):
    from src.agent.tools import data_tools
    from src.deep_research_dims import context, dim_cache
    run = ResearchRun(10)
    native = ChoiceAdapter._native_request

    def request(method, args, options, deadline):
        response = native(method, args, options, deadline)
        if method == "css" and args[1] == "HOLDNAMER,HOLDPCTR":
            run.stop("cancelled")
        return response

    def forbidden(*args, **kwargs):
        pytest.fail("A cancelled snapshot must not populate the success cache")

    manager = DataFetcherManager()
    monkeypatch.setattr(data_tools, "_fetcher_manager_singleton", manager)
    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(request))
    monkeypatch.setattr(dim_cache, "save_snapshot", forbidden)
    with run.bind():
        fund = context._safe_fundamental("300260", context.SharedContext("300260", "新莱应材", "2026-10-15"))
    assert fund["pe_ttm"] == 121.655277
    assert fund["roe"] == 0
    assert not manager._fundamental_cache


def test_single_annual_result_is_partial_when_its_completion_cancels_run(annual_transport, monkeypatch):
    native = ChoiceAdapter._native_request
    run = ResearchRun(10)

    def request(*args):
        response = native(*args)
        run.stop("cancelled")
        return response

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(request))
    with run.bind():
        result = ChoiceAdapter().annual_reports("300260", limit=1, deadline=monotonic() + 5)
    assert len(result["years"]) == 1
    assert result["status"] == "partial"


def test_native_dispatch_rechecks_cancellation_after_lock_acquisition(monkeypatch):
    run = ResearchRun(5)
    released = []

    class LockCancelledOnAcquire:
        def acquire(self, *, timeout):
            run.stop("cancelled")
            return True

        def release(self):
            released.append(True)

    def forbidden(*args, **kwargs):
        pytest.fail("A stopped waiter must not initialize or close the shared SDK")

    monkeypatch.setattr(ChoiceAdapter, "_lock", LockCancelledOnAcquire())
    monkeypatch.setattr(ChoiceAdapter, "close", forbidden)
    with run.bind(), pytest.raises(ResearchStopped):
        ChoiceAdapter._native_request("css", ("300260.SZ", "NAME"), "", monotonic() + 5)
    assert released == [True]


def test_source_timeout_worker_propagates_research_stop_without_supplier_error(choice_transport):
    manager = DataFetcherManager()
    run = ResearchRun(5)

    def cancelled():
        run.stop("cancelled")
        run.check()

    with run.bind(), pytest.raises(ResearchStopped):
        manager._run_with_timeout(cancelled, 1, "fixture")


def test_cancellation_during_public_details_retains_completed_financials(choice_transport, monkeypatch):
    manager = DataFetcherManager()
    run = ResearchRun(5)

    def details(*args, **kwargs):
        run.stop("cancelled")
        return {"institution": {"institution_holding_change": 12.5}}

    def forbidden(*args, **kwargs):
        pytest.fail("The next auxiliary request must not start after cancellation")

    monkeypatch.setattr(manager._fundamental_adapter, "get_fundamental_bundle", details)
    monkeypatch.setattr(manager, "get_dragon_tiger_context", forbidden)
    with run.bind():
        result = manager.get_fundamental_context("300260", budget_seconds=3)
    assert result["growth"]["data"]["roe"] == 0
    assert result["institution"]["data"]["institution_holding_change"] == 12.5
    assert result["boards"]["status"] == "partial"
    assert not manager._fundamental_cache
