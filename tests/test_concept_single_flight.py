# -*- coding: utf-8 -*-
"""概念独立取数与同报告资料复用（issue #51）。

真实组合工具与共享上下文并发：慢财务前置时概念仍有执行机会；
并发消费者只出站一次；冷/热/强刷与不同证券/财期隔离；
已知失败同报告复用但不是成功缓存；退休公开概念触发即失败。
"""
from __future__ import annotations

import threading
from time import monotonic, sleep
from types import SimpleNamespace

import pytest

from data_provider.cross_source_validator import SourceReadError
from src.core.research_run import ResearchRun


class TestConceptIndependentOfFinancialAggregation:
    def test_slow_financial_still_leaves_concept_an_execution_window(self, monkeypatch) -> None:
        """慢前置财务时，真实概念装配与其并行，不排在聚合链末尾。"""
        import src.deep_research_dims.context as ctx_mod
        from data_provider.ifind_fundamental_adapter import IfindFetcher
        from src.config import get_config
        from unittest.mock import patch

        order: list[str] = []
        config = get_config()
        monkeypatch.setattr(config, "enable_ifind_mcp", True, raising=False)
        monkeypatch.setattr(config, "ifind_mcp_endpoint", "https://fixture.invalid", raising=False)
        monkeypatch.setattr(config, "ifind_mcp_token", "fixture-token-value", raising=False)

        def slow_fundamental(code, ctx):
            order.append("financial_start")
            sleep(0.3)
            order.append("financial_end")
            return {"status": "partial"}

        async def outbound(self, tool, question, *, deadline=None):
            order.append("concept")
            return {"code": 1, "msg": "success", "data": {"answer": "|证券代码|所属概念|\n|---|---|\n|300260.SZ|芯片概念|\n"}}

        monkeypatch.setattr(IfindFetcher, "_async_query", outbound)
        with patch.object(ctx_mod, "_safe_fundamental", slow_fundamental), \
             patch.object(ctx_mod, "_safe_quote", lambda code, ctx: {}), \
             patch.object(ctx_mod, "_safe_history", lambda code, days, ctx: []), \
             patch.object(ctx_mod, "_safe_history_reports", lambda code, ctx: []), \
             patch("src.patches.eastmoney_patch.eastmoney_patch", lambda budget_only=True: None):
            shared = ctx_mod.build_shared_context("300260", "新莱应材", deadline=monotonic() + 5)
        assert shared.concept_membership["concepts"][0]["name"] == "芯片概念"
        assert order.index("concept") < order.index("financial_end")

    def test_concept_not_dispatched_when_budget_exhausted(self, monkeypatch) -> None:
        """预算已耗尽时概念未分发，不发起请求。"""
        import src.deep_research_dims.context as ctx_mod

        called = []
        monkeypatch.setattr(ctx_mod, "_concept_membership", lambda *a, **k: called.append(1), raising=False)
        run = ResearchRun(0)
        run.stop("report_budget")
        ctx = ctx_mod.SharedContext("300260", "新莱应材", "2026-10-26T00:00:00")
        with run.bind():
            result = ctx_mod._safe_concepts("300260", monotonic() + 5, ctx)
        assert result == {}
        assert called == []
        assert any("预算" in note for note in ctx.limitations)


class TestSameReportSingleFlight:
    def test_concurrent_consumers_fetch_once(self) -> None:
        """同一报告并发请求同一资料只出站一次，两边拿到同一结果。"""
        run = ResearchRun(10)
        outbound = {"count": 0}
        gate = threading.Event()

        def fetch() -> dict:
            outbound["count"] += 1
            gate.wait(2)
            return {"concepts": ["芯片概念"], "error": None}

        results: list[dict] = []

        def consumer() -> None:
            with run.bind():
                results.append(run.single_fetch("ifind|concepts|300260", fetch))

        threads = [threading.Thread(target=consumer) for _ in range(2)]
        for thread in threads:
            thread.start()
        sleep(0.1)
        gate.set()
        for thread in threads:
            thread.join(3)
        assert outbound["count"] == 1
        assert len(results) == 2
        assert all(item["concepts"] == ["芯片概念"] for item in results)

    def test_known_failure_is_shared_but_not_a_success_cache(self) -> None:
        """已知失败同报告复用，但不写入成功缓存、不阻止新报告重试。"""
        run = ResearchRun(10)
        attempts = {"count": 0}

        def fetch() -> dict:
            attempts["count"] += 1
            raise SourceReadError("ifind", "timeout")

        with run.bind():
            with pytest.raises(SourceReadError):
                run.single_fetch("ifind|concepts|300260", fetch)
            with pytest.raises(SourceReadError):
                run.single_fetch("ifind|concepts|300260", fetch)
        assert attempts["count"] == 1

        fresh = ResearchRun(10)
        with fresh.bind():
            with pytest.raises(SourceReadError):
                fresh.single_fetch("ifind|concepts|300260", fetch)
        assert attempts["count"] == 2

    def test_different_securities_and_periods_are_isolated(self, monkeypatch) -> None:
        """公开入口的不同证券与财期各自出站，键来自中央需求边界。"""
        from data_provider.base import DataFetcherManager
        from data_provider.ifind_fundamental_adapter import IfindFetcher
        from src.agent.tools import data_tools
        from src.config import get_config
        from src.core.research_run import supplier_need_key

        config = get_config()
        monkeypatch.setattr(config, "enable_ifind_mcp", True, raising=False)
        monkeypatch.setattr(config, "data_source_profile", "choice", raising=False)
        monkeypatch.setattr(config, "ifind_mcp_endpoint", "https://fixture.invalid", raising=False)
        monkeypatch.setattr(config, "ifind_mcp_token", "fixture-token-value", raising=False)
        seen: list[tuple[str, str]] = []

        async def fake(self, tool, question, *, deadline=None):
            seen.append((tool, question))
            raise SourceReadError("ifind", "timeout")

        monkeypatch.setattr(IfindFetcher, "_async_query", fake)

        class _Fetcher:
            name = "fixture"
            priority = 1

        run = ResearchRun(10)
        manager = DataFetcherManager(fetchers=[_Fetcher()])
        with run.bind():
            data_tools._concept_membership("300260", monotonic() + 5)
            data_tools._concept_membership("600519", monotonic() + 5)
            manager.get_ifind_research_context("300260", "products", period="2025-12-31", deadline=monotonic() + 5)
            manager.get_ifind_research_context("300260", "products", period="2024-12-31", deadline=monotonic() + 5)
        assert len(seen) == 4
        assert seen[0][1] != seen[1][1]
        assert "2025-12-31" in seen[2][1] and "2024-12-31" in seen[3][1]
        expected = {supplier_need_key("ifind", config.data_source_contract, tool, question, "") for tool, question in seen}
        assert expected == set(run._fetches)

    def test_nothing_revived_from_a_stopped_run(self) -> None:
        """已停止/历史运行不复活结果。"""
        run = ResearchRun(10)
        with run.bind():
            run.single_fetch("ifind|concepts|300260", lambda: {"concepts": ["旧结果"]})
        run.stop("cancelled")
        with run.bind(), pytest.raises(Exception):
            run.single_fetch("ifind|concepts|300260", lambda: {"concepts": ["新结果"]})


class TestReviewCounterexamples:
    """审查要点的反例：等待方阶段预算、实际出站计数、敏感值脱敏。"""

    def test_short_budget_waiter_is_not_held_by_long_owner(self) -> None:
        """短预算消费者不等到长 owner 完成，抛超时而不是被拖住。"""
        from concurrent.futures import TimeoutError as FutureTimeout

        run = ResearchRun(30)
        gate = threading.Event()

        def slow() -> dict:
            gate.wait(5)
            return {"value": "done"}

        def owner() -> None:
            with run.bind():
                run.single_fetch("slow", slow)

        thread = threading.Thread(target=owner)
        thread.start()
        sleep(0.1)
        with run.bind(), pytest.raises(FutureTimeout):
            run.single_fetch("slow", slow, timeout=0.2)
        gate.set()
        thread.join(3)

    def test_actual_supplier_query_counted_once_across_entry_points(self, monkeypatch) -> None:
        """阶段 0 与研究员直达 fetcher 的同一 question 只出站一次。"""
        from data_provider.ifind_fundamental_adapter import IfindFetcher, IfindData

        outbound = {"count": 0}

        async def fake_async(self, tool, question, *, deadline=None):
            outbound["count"] += 1
            return {"code": 1, "data": {"answer": "|证券代码|所属概念|\n|---|---|\n|300260.SZ|芯片概念|\n"}}

        monkeypatch.setattr(IfindFetcher, "_async_query", fake_async)
        fetcher = IfindFetcher(endpoint="https://fixture.invalid", token="fixture")
        run = ResearchRun(10)
        question = "300260.SZ 所属概念"
        with run.bind():
            first = fetcher.query("get_stock_info", question, deadline=monotonic() + 5)
            second = fetcher.query("get_stock_info", question, deadline=monotonic() + 5)
        assert outbound["count"] == 1
        assert isinstance(first, IfindData) and first.answer == second.answer

    def test_evidence_redacts_secret_values(self, caplog) -> None:
        """证据日志遮蔽 api_key/token/UserName 等敏感值并限长。"""
        import logging
        from src.services.mcp_client import _evidence, _safe_text

        caplog.set_level(logging.INFO, logger="src.services.mcp_client")
        leaked = "上游回显 api_key=SECRETVALUE123 token=tok-abcdef UserName=choiceuser"
        _evidence("cid-secret", "https://fixture.invalid/mcp", "get_stock_info",
                  phase="end", status="error", business_msg=_safe_text(leaked))
        line = caplog.records[-1].getMessage()
        assert "SECRETVALUE123" not in line
        assert "tok-abcdef" not in line
        assert "[REDACTED]" in line
        assert len(line) < 600

    """失败按原错误分类，不能伪造 HTTP 或上游状态。"""

    def test_is_error_is_business_error_not_http(self, monkeypatch) -> None:
        from src.agent.tools import data_tools
        from src.config import get_config

        monkeypatch.setattr(get_config(), "enable_ifind_mcp", True, raising=False)

        class FakeFetcher:
            def __init__(self, *args) -> None:
                pass

            def concept_membership(self, code, *, deadline):
                raise SourceReadError("ifind", "business_error")

        import data_provider.ifind_fundamental_adapter as adapter
        monkeypatch.setattr(adapter, "IfindFetcher", FakeFetcher)
        result = data_tools._concept_membership("300260", monotonic() + 5)
        assert result["error"] == "business_error"
        assert result["concepts"] == []

    def test_empty_result_and_field_error_stay_distinct(self) -> None:
        from data_provider.ifind_fundamental_adapter import _parse_concepts, IfindData

        empty = IfindData(answer="|证券代码|所属概念|\n|---|---|\n|300260.SZ| |\n")
        broken = IfindData(answer="|证券代码|证券简称|\n|---|---|\n|300260.SZ|新莱应材|\n")
        mismatch = IfindData(answer="|证券代码|所属概念|\n|---|---|\n|600519.SH|白酒概念|\n")
        assert _parse_concepts(empty, "300260").errors == ["no_concepts"]
        assert _parse_concepts(broken, "300260").errors == ["field_error"]
        assert _parse_concepts(mismatch, "300260").errors == ["security_mismatch"]

    def test_exception_chain_preserved_in_evidence(self, caplog) -> None:
        import logging
        from src.services.mcp_client import _evidence, _exception_chain

        caplog.set_level(logging.INFO, logger="src.services.mcp_client")
        try:
            raise ValueError("inner")
        except ValueError as exc:
            chain = _exception_chain(exc)
        _evidence("cid-chain", "https://fixture.invalid/mcp", "get_stock_info",
                  phase="end", status="error", exception_chain=chain)
        assert "ValueError" in caplog.records[-1].getMessage()

    def test_cancellation_does_not_dispatch(self) -> None:
        run = ResearchRun(10)
        run.stop("cancelled")
        dispatched = {"count": 0}
        with run.bind(), pytest.raises(Exception):
            run.single_fetch("ifind|concepts|300260", lambda: dispatched.__setitem__("count", 1))
        assert dispatched["count"] == 0

    def test_force_refresh_shares_within_report_but_not_across_runs(self, monkeypatch) -> None:
        """同一强刷标记在报告内共享，新报告不复活该次出站。"""
        from data_provider.ifind_fundamental_adapter import IfindFetcher
        from data_provider.mx_data_adapter import financial_refresh
        from src.agent.tools import data_tools
        from src.config import get_config
        from src.core.research_run import supplier_need_key

        config = get_config()
        monkeypatch.setattr(config, "enable_ifind_mcp", True, raising=False)
        monkeypatch.setattr(config, "ifind_mcp_endpoint", "https://fixture.invalid", raising=False)
        monkeypatch.setattr(config, "ifind_mcp_token", "fixture-token-value", raising=False)
        seen: list[str] = []

        async def fake(self, tool, question, *, deadline=None):
            seen.append(question)
            return {"code": 1, "msg": "success", "data": {"answer": "|证券代码|所属概念|\n|---|---|\n|300260.SZ|芯片概念|\n"}}

        monkeypatch.setattr(IfindFetcher, "_async_query", fake)
        run = ResearchRun(10)
        token = financial_refresh.set(("300260", "refresh-1"))
        try:
            with run.bind():
                data_tools._concept_membership("300260", monotonic() + 5)
                data_tools._concept_membership("300260", monotonic() + 5)
        finally:
            financial_refresh.reset(token)
        assert len(seen) == 1
        assert supplier_need_key("ifind", config.data_source_contract, "get_stock_info", seen[0], "refresh-1") in run._fetches
        fresh = ResearchRun(10)
        token = financial_refresh.set(("300260", "refresh-1"))
        try:
            with fresh.bind():
                data_tools._concept_membership("300260", monotonic() + 5)
        finally:
            financial_refresh.reset(token)
        assert len(seen) == 2
        assert supplier_need_key("ifind", config.data_source_contract, "get_stock_info", seen[0], "refresh-1") in fresh._fetches

    def test_retired_public_concept_probe_fails_on_trigger(self) -> None:
        """退休公开概念工具一旦触发即失败，不能被别名解析回探针。"""
        from src.agent.factory import get_tool_registry

        registry = get_tool_registry()
        assert registry.resolve("verify_supply_chain_evidence") is None
        with pytest.raises(KeyError):
            registry.execute("verify_supply_chain_evidence", stock_code="300260")


class TestConceptMembershipSingleFlightEntry:
    def test_tool_entry_single_flights_through_run(self, monkeypatch) -> None:
        """组合工具入口经 query 单飞：并发两次只出站一次，键与中央边界一致。"""
        from data_provider.ifind_fundamental_adapter import IfindFetcher
        from src.agent.tools import data_tools
        from src.config import get_config
        from src.core.research_run import supplier_need_key

        config = get_config()
        monkeypatch.setattr(config, "enable_ifind_mcp", True, raising=False)
        monkeypatch.setattr(config, "ifind_mcp_endpoint", "https://fixture.invalid", raising=False)
        monkeypatch.setattr(config, "ifind_mcp_token", "fixture-token-value", raising=False)
        seen: list[tuple[str, str]] = []

        async def fake(self, tool, question, *, deadline=None):
            seen.append((tool, question))
            sleep(0.05)
            return {"code": 1, "msg": "success", "data": {"answer": "|证券代码|所属概念|\n|---|---|\n|300260.SZ|芯片概念|\n"}}

        monkeypatch.setattr(IfindFetcher, "_async_query", fake)
        run = ResearchRun(10)
        results: list[dict] = []

        def consumer() -> None:
            with run.bind():
                results.append(data_tools._concept_membership("300260", monotonic() + 5))

        threads = [threading.Thread(target=consumer) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        assert len(seen) == 1
        assert supplier_need_key("ifind", config.data_source_contract, seen[0][0], seen[0][1], "") in run._fetches
        assert all(item["concepts"][0]["name"] == "芯片概念" for item in results)



class TestBusinessConsumers:
    def test_stage_context_is_not_adopted_for_another_security(self, monkeypatch) -> None:
        """300260 的阶段概念不能被 600519 的业务入口采用。"""
        from src.core.pipeline import StockAnalysisPipeline
        from src.deep_research_dims.context import SharedContext
        from src.agent.tools import data_tools

        fetched: list[str] = []

        def fetch(code: str, deadline: float) -> dict:
            fetched.append(code)
            return {"concepts": [{"name": "白酒概念"}], "source": "ifind", "as_of_nature": "latest", "error": None}

        monkeypatch.setattr(data_tools, "_concept_membership", fetch)
        run = ResearchRun(10)
        run.context = SharedContext("300260", "新莱应材", "2026-10-26T00:00:00")
        run.context.concept_membership = {
            "concepts": [{"name": "芯片概念"}], "source": "ifind", "as_of_nature": "latest", "error": None,
        }
        other: dict = {}
        same: dict = {}
        suffixed: dict = {}
        with run.bind():
            StockAnalysisPipeline._attach_concept_membership("600519", other)
            StockAnalysisPipeline._attach_concept_membership("300260", same)
            StockAnalysisPipeline._attach_concept_membership("300260.SZ", suffixed)
        assert other["concept_membership"]["concepts"][0]["name"] == "白酒概念"
        assert fetched == ["600519"]
        assert same["concept_membership"]["concepts"][0]["name"] == "芯片概念"
        assert suffixed["concept_membership"]["concepts"][0]["name"] == "芯片概念"

    def test_short_budget_waiter_is_not_held_by_query_owner(self, monkeypatch) -> None:
        """实际 query 消费者的 50ms 截止不被 300ms owner 延长。"""
        from data_provider.ifind_fundamental_adapter import IfindFetcher
        from src.patches.eastmoney_patch import request_deadline

        outbound = {"count": 0}
        gate = threading.Event()

        async def slow(self, tool, question, *, deadline=None):
            outbound["count"] += 1
            gate.wait(2)
            return {"code": 1, "msg": "success", "data": {"answer": "ok"}}

        monkeypatch.setattr(IfindFetcher, "_async_query", slow)
        fetcher = IfindFetcher("https://fixture.invalid", "fixture-token-value", 5)
        run = ResearchRun(10)
        question = "300260.SZ 所属概念"

        def owner() -> None:
            with run.bind():
                fetcher.query("get_stock_info", question, deadline=monotonic() + 5)

        thread = threading.Thread(target=owner)
        thread.start()
        sleep(0.05)
        started = monotonic()
        with run.bind():
            token = request_deadline.set(monotonic() + 0.05)
            try:
                with pytest.raises(SourceReadError) as caught:
                    fetcher.query("get_stock_info", question, deadline=monotonic() + 5)
            finally:
                request_deadline.reset(token)
        elapsed = monotonic() - started
        gate.set()
        thread.join(3)
        assert caught.value.reason_code == "timeout"
        assert elapsed < 0.2
        assert outbound["count"] == 1

    def test_same_supplier_question_is_shared_across_entry_points(self, monkeypatch) -> None:
        """阶段 0、直达 fetcher、分节工具和 IfindSource 共享同一次实际出站。"""
        from data_provider.base import DataFetcherManager
        from data_provider.ifind_fundamental_adapter import IfindFetcher, IfindSource
        from data_provider.mx_data_adapter import financial_refresh
        from src.agent.tools import data_tools
        from src.config import get_config

        config = get_config()
        monkeypatch.setattr(config, "enable_ifind_mcp", True, raising=False)
        monkeypatch.setattr(config, "data_source_profile", "choice", raising=False)
        monkeypatch.setattr(config, "ifind_mcp_endpoint", "https://fixture.invalid", raising=False)
        monkeypatch.setattr(config, "ifind_mcp_token", "fixture-token-value", raising=False)
        outbound: list[tuple[str, str]] = []

        async def fake(self, tool, question, *, deadline=None):
            outbound.append((tool, question))
            if "所属概念" in question:
                return {"code": 1, "msg": "success", "data": {"answer": "|证券代码|所属概念|\n|---|---|\n|300260.SZ|芯片概念|\n"}}
            if "主营构成" in question:
                raise SourceReadError("ifind", "timeout")
            return {"code": 1, "msg": "success", "data": {"answer": "|证券代码|营业收入|\n|---|---|\n|300260.SZ|1|\n"}}

        monkeypatch.setattr(IfindFetcher, "_async_query", fake)
        fetcher = IfindFetcher(config.ifind_mcp_endpoint, config.ifind_mcp_token, 5)
        run = ResearchRun(10)
        period = "2025-12-31"

        class _Fetcher:
            name = "fixture"
            priority = 1

        manager = DataFetcherManager(fetchers=[_Fetcher()])
        with run.bind():
            first = fetcher.concept_membership("300260", deadline=monotonic() + 5)
            second = data_tools._concept_membership("300260", monotonic() + 5)
            with pytest.raises(SourceReadError):
                fetcher.product_segments("300260", period, deadline=monotonic() + 5)
            again = manager.get_ifind_research_context("300260", "products", period=period, deadline=monotonic() + 5)
            fetcher.fetch("300260", "revenue", period, deadline=monotonic() + 5)
            IfindSource(fetcher).read("300260", "revenue", period, deadline=monotonic() + 5)
            other = fetcher.concept_membership("600519", deadline=monotonic() + 5)
        assert first.concepts == ["芯片概念"]
        assert second["concepts"][0]["name"] == "芯片概念"
        assert again["errors"]
        assert other.errors == ["security_mismatch"] or other.concepts != first.concepts
        concept_calls = [item for item in outbound if "所属概念" in item[1]]
        product_calls = [item for item in outbound if "主营构成" in item[1]]
        revenue_calls = [item for item in outbound if "营业收入" in item[1] and "主营" not in item[1]]
        assert len(concept_calls) == 2  # 300260 once, 600519 once
        assert len(product_calls) == 1
        assert len(revenue_calls) == 1
        assert product_calls and "timeout" in again["errors"][0]

        fresh = ResearchRun(10)
        with fresh.bind():
            token = financial_refresh.set(("300260", "refresh-1"))
            try:
                fetcher.concept_membership("300260", deadline=monotonic() + 5)
            finally:
                financial_refresh.reset(token)
        assert len([item for item in outbound if "所属概念" in item[1] and "300260" in item[1]]) == 2


class TestManagerCacheColdHotForced:
    def test_section_and_bundle_cache_follow_refresh_and_fresh_run(self, monkeypatch) -> None:
        """分区与整包走真实管理器缓存：冷取、热命中、强刷绕过，失败不缓存且新报告重试。"""
        from data_provider.base import DataFetcherManager
        from data_provider.choice_adapter import ChoiceAdapter
        from data_provider.ifind_fundamental_adapter import IfindFetcher
        from data_provider.mx_data_adapter import financial_refresh
        from src.config import get_config
        from src.core.research_run import supplier_need_key

        config = get_config()
        monkeypatch.setattr(config, "data_source_profile", "choice", raising=False)
        monkeypatch.setattr(config, "enable_ifind_mcp", True, raising=False)
        monkeypatch.setattr(config, "financial_primary_source", "choice", raising=False)
        monkeypatch.setattr(config, "ifind_mcp_endpoint", "https://fixture.invalid", raising=False)
        monkeypatch.setattr(config, "ifind_mcp_token", "fixture-token-value", raising=False)
        monkeypatch.setattr(config, "fundamental_cache_ttl_seconds", 3600, raising=False)
        products = {"count": 0}
        answer = (
            "|证券代码|日期|主营构成-项目名称(按产品)（排名：第1名）|主营构成-项目收入(按产品)（排名：第1名）|\n"
            "|---|---|---|---|\n"
            "|300260.SZ|2025-12-31|无菌包材|100|\n"
        )

        async def products_query(self, tool, question, *, deadline=None):
            products["count"] += 1
            if "2024-12-31" in question:
                raise SourceReadError("ifind", "timeout")
            return {"code": 1, "msg": "success", "data": {"answer": answer}}

        monkeypatch.setattr(IfindFetcher, "_async_query", products_query)

        class _Fetcher:
            name = "fixture"
            priority = 1

        manager = DataFetcherManager(fetchers=[_Fetcher()])
        run = ResearchRun(10)
        with run.bind():
            first = manager.get_ifind_research_context("300260", "products", period="2025-12-31", deadline=monotonic() + 5)
            second = manager.get_ifind_research_context("300260", "products", period="2025-12-31", deadline=monotonic() + 5)
        assert first["items"] and not first["errors"]
        assert second == first
        assert products["count"] == 1
        assert any("2025-12-31" in key for key in run._fetches)
        with ResearchRun(10).bind():
            hot = manager.get_ifind_research_context("300260", "products", period="2025-12-31", deadline=monotonic() + 5)
        assert hot == first and products["count"] == 1
        token = financial_refresh.set(("300260", "refresh-1"))
        try:
            forced_run = ResearchRun(10)
            with forced_run.bind():
                forced = manager.get_ifind_research_context("300260", "products", period="2025-12-31", deadline=monotonic() + 5)
                again = manager.get_ifind_research_context("300260", "products", period="2025-12-31", deadline=monotonic() + 5)
        finally:
            financial_refresh.reset(token)
        assert products["count"] == 2
        assert again["items"][0]["name"] == forced["items"][0]["name"] == "无菌包材"
        assert again["errors"] == forced["errors"] == []
        assert any(key.endswith("|refresh-1") for key in forced_run._fetches)
        with ResearchRun(10).bind():
            with pytest.raises(SourceReadError):
                IfindFetcher(config.ifind_mcp_endpoint, config.ifind_mcp_token, 5).product_segments("300260", "2024-12-31", deadline=monotonic() + 5)
            with pytest.raises(SourceReadError):
                IfindFetcher(config.ifind_mcp_endpoint, config.ifind_mcp_token, 5).product_segments("300260", "2024-12-31", deadline=monotonic() + 5)
        assert products["count"] == 3
        with ResearchRun(10).bind():
            with pytest.raises(SourceReadError):
                IfindFetcher(config.ifind_mcp_endpoint, config.ifind_mcp_token, 5).product_segments("300260", "2024-12-31", deadline=monotonic() + 5)
        assert products["count"] == 4
        section_keys = [key for key in manager._fundamental_cache if "ifind_products" in key]
        assert section_keys and all("2024-12-31" not in key for key in section_keys)

        monkeypatch.setattr(config, "enable_ifind_mcp", False, raising=False)
        snapshots = {"count": 0, "fail": True}

        def snapshot(self, code, *, deadline, omit_indicators=(), include_optional=True):
            snapshots["count"] += 1
            if snapshots["fail"]:
                raise SourceReadError("choice", "timeout")
            return {
                "growth": {"revenue": 1, "net_profit": 1, "roe": 1.0, "gross_margin": 1.0, "revenue_yoy": 1.0, "net_profit_yoy": 1.0},
                "valuation": {"pe_ratio": 1.0, "pb_ratio": 1.0, "total_mv": 1.0},
                "field_meta": {},
                "errors": [],
                "research": {"errors": []},
                "as_of": "2026-10-26",
                "stock_name": "fixture",
            }

        monkeypatch.setattr(ChoiceAdapter, "snapshot", snapshot)
        # 可选行业/公司查询已从 snapshot 拆出（#50）：native 边界必须同样隔离，
        # 否则 append_optional_research 会真起 Choice 子进程登录。可选数据成功才不注入
        # research 错误，成功快照才能按契约写入日缓存。
        optional_native: list[str] = []

        def native_request(method, args, options, deadline):
            indicators = args[1].split(",")
            optional_native.extend(indicators)
            return {"codes": [args[0]], "indicators": indicators, "dates": ["2026-10-26"], "data": {args[0]: ["可选资料"] * len(indicators)}}

        monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(native_request))
        monkeypatch.setattr(manager._fundamental_adapter, "get_fundamental_bundle", lambda *args, **kwargs: {"earnings": {}, "institution": {}, "errors": [], "source_chain": []})
        empty = {"status": "not_supported", "data": {}, "errors": [], "source_chain": []}
        monkeypatch.setattr(manager, "get_dragon_tiger_context", lambda *args, **kwargs: empty)
        monkeypatch.setattr(manager, "get_board_context", lambda *args, **kwargs: empty)
        monkeypatch.setattr(manager, "get_capital_flow_context", lambda *args, **kwargs: empty)
        failed = ResearchRun(10)
        with failed.bind():
            with pytest.raises(SourceReadError):
                manager.get_fundamental_context("300260", budget_seconds=5)
            with pytest.raises(SourceReadError):
                manager.get_fundamental_context("300260", budget_seconds=5)
        assert snapshots["count"] == 1
        assert not any(key.startswith("300260|") or "fundamental" in key for key in manager._fundamental_cache if "ifind_products" not in key)
        with ResearchRun(10).bind():
            with pytest.raises(SourceReadError):
                manager.get_fundamental_context("300260", budget_seconds=5)
        assert snapshots["count"] == 2
        snapshots["fail"] = False
        cold = ResearchRun(10)
        with cold.bind():
            built = manager.get_fundamental_context("300260", budget_seconds=5)
            shared = manager.get_fundamental_context("300260", budget_seconds=5)
        assert built["growth"]["data"]["roe"] == 1.0
        assert shared is not built or shared == built
        assert snapshots["count"] == 3
        assert supplier_need_key("choice", config.data_source_contract, "fundamental_full", "300260", "") in cold._fetches
        with ResearchRun(10).bind():
            manager.get_fundamental_context("300260", budget_seconds=5)
        assert snapshots["count"] == 3
        # 成功快照的缓存来自真实可选派发后的干净结果，而不是把可选阶段整个去掉。
        assert "HISSW2021" in optional_native and "MAINBUSINESS" in optional_native
        token = financial_refresh.set(("300260", "bundle-refresh"))
        try:
            forced_bundle = ResearchRun(10)
            with forced_bundle.bind():
                manager.get_fundamental_context("300260", budget_seconds=5)
                manager.get_fundamental_context("300260", budget_seconds=5)
        finally:
            financial_refresh.reset(token)
        assert snapshots["count"] == 4
        assert supplier_need_key("choice", config.data_source_contract, "fundamental_full", "300260", "bundle-refresh") in forced_bundle._fetches
