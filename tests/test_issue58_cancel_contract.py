# -*- coding: utf-8 -*-
"""Issue #58 取消契约回归：终态恰好一次、取消不被误分类、预算不被误报。

覆盖真实入口：API SSE 生命周期（``api.v1.endpoints.deep_research`` 的真实
``generate_report`` finally、线程池与清理链）、iFinD 同步包装
（``IfindFetcher.query`` 与在途 SDK 边界）、共享 MCP 边界与阶段 0 真实收尾
（``build_shared_context``）。SDK 取消形态异常只有在 ResearchRun 自身状态
（显式 stop_reason / 真实预算耗尽）支持下才解释为用户取消；意外取消保留
真实错误语义；取消成员永不吸收同组的致命成员。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from data_provider.base import BaseFetcher, DataFetchError
from src.core.research_run import ResearchRun, ResearchStopped


class _CancelledFetcher(BaseFetcher):
    """日线源直接抛 ResearchStopped 的最小夹具（真实 get_daily_data 入口）。"""

    name = "fixture-cancelled"

    def _fetch_raw_data(self, *args, **kwargs):
        raise ResearchStopped("用户已取消本次研究")

    def _normalize_data(self, data, stock_code):  # pragma: no cover - 取消后不得继续
        raise AssertionError("cancelled work must not continue")


def test_daily_fetcher_preserves_research_stopped_without_source_failure():
    """取消穿透日线包装：既不包装为 DataFetchError，也不触发来源失败日志路径。"""
    fetcher = _CancelledFetcher()
    try:
        fetcher.get_daily_data("600519", "2026-10-01", "2026-10-27")
    except ResearchStopped:
        pass
    except DataFetchError as exc:  # pragma: no cover - 回归即失败
        raise AssertionError(f"ResearchStopped was wrapped: {exc!r}") from exc
    else:  # pragma: no cover - 取消不应成功返回
        raise AssertionError("cancelled request unexpectedly succeeded")


def test_ifind_wrapped_cancel_translates_to_research_stopped_by_run_state():
    """真实 iFinD 边界的在途取消：SDK 调用期间停止 → ResearchStopped，恰好一次出站。"""
    from data_provider.ifind_fundamental_adapter import IfindFetcher

    fetcher = IfindFetcher("http://127.0.0.1:1/mcp", "fixture-only", 1)
    sdk_calls: list[int] = []
    run = ResearchRun(60)

    def cancel_mid_flight(*args, **kwargs):
        # 取消在 SDK 边界在途期间建立，与生产观测一致（非入口预检退出）。
        sdk_calls.append(1)
        run.stop("cancelled")
        raise asyncio.CancelledError("synthetic production MCP cancellation")

    with run.bind():
        with patch.object(IfindFetcher, "_async_query", cancel_mid_flight):
            try:
                fetcher.query("get_stock_info", "fixture")
            except ResearchStopped:
                pass
            except BaseException as exc:  # pragma: no cover - 回归即失败
                raise AssertionError(f"expected ResearchStopped, got {exc!r}") from exc
            else:  # pragma: no cover
                raise AssertionError("cancelled MCP call unexpectedly succeeded")
    assert sdk_calls == [1], "the real SDK boundary must be reached exactly once"

    def cancel_without_stop(*args, **kwargs):
        sdk_calls.append(1)
        raise asyncio.CancelledError("SDK timeout shaped as cancellation")

    other = ResearchRun(60)
    with other.bind():
        with patch.object(IfindFetcher, "_async_query", cancel_without_stop):
            try:
                fetcher.query("get_stock_info", "fixture")
            except asyncio.CancelledError:
                pass  # 无用户取消：真实取消语义保留，不冒充来源错误也不冒充取消终态
            except ResearchStopped as exc:  # pragma: no cover
                raise AssertionError(f"non-user cancel mislabelled: {exc!r}") from exc


def test_stage_zero_worker_cancelled_error_follows_run_state():
    """阶段 0 真实收尾读取资料 future：取消在受控 worker 回调内建立（不预停），
    worker 真实到达、future 完成后由收尾收集，取消形态异常按 run 状态收敛；
    无关外部 sibling（日线/概念/历史报告）全部 stub，保持离线确定性。"""
    from src.deep_research_dims import context as context_module

    worker_calls: list[int] = []

    def cancel_in_flight(*args):
        # 取消在 worker/SDK 边界在途期间建立——入口 run.check() 已经过，
        # 收尾的 future 收集路径被真实执行。
        worker_calls.append(1)
        current_research.get().stop("cancelled")
        raise asyncio.CancelledError("worker transport cancel")

    from src.core.research_run import current_research

    # run 在 worker 内才取消：入口检查通过，future 收集被真实走到；
    # 收尾抛 ResearchStopped（编排层收敛中断结果），不穿透裸取消。
    run = ResearchRun(60)
    with run.bind():
        with (
            patch.object(context_module, "_safe_quote", cancel_in_flight),
            patch.object(context_module, "_safe_fundamental", lambda *args: {}),
            patch.object(context_module, "_safe_history", lambda *args: []),
            patch.object(context_module, "_safe_concepts", lambda *args: {}),
            patch.object(context_module, "_safe_history_reports", lambda *args: []),
        ):
            try:
                context_module.build_shared_context("600519", "样本")
            except ResearchStopped:
                pass
            except BaseException as exc:  # pragma: no cover - 回归即失败
                raise AssertionError(f"expected ResearchStopped, got {exc!r}") from exc
            else:  # pragma: no cover
                raise AssertionError("stopped stage zero unexpectedly succeeded")
    assert worker_calls == [1], "the cancelled worker must have been reached exactly once"

    # run 未取消（真实传输中断）：worker 只抛真实取消、不停 run；
    # 阶段 0 保持 fail-open，如实记录 limitation。
    def interrupt_in_flight(*args):
        worker_calls.append(1)
        raise asyncio.CancelledError("worker transport cancel")

    other = ResearchRun(60)
    with other.bind():
        with (
            patch.object(context_module, "_safe_quote", interrupt_in_flight),
            patch.object(context_module, "_safe_fundamental", lambda *args: {}),
            patch.object(context_module, "_safe_history", lambda *args: []),
            patch.object(context_module, "_safe_concepts", lambda *args: {}),
            patch.object(context_module, "_safe_history_reports", lambda *args: []),
        ):
            ctx = context_module.build_shared_context("600519", "样本")
    assert worker_calls == [1, 1], "the interrupted worker must have been reached again"
    assert any("阶段0装配被中断" in note for note in ctx.limitations)


def test_stage_zero_fatal_group_member_is_preserved():
    """取消成员永不吸收同组的 SystemExit：真实 build_shared_context 收尾保留致命组，
    且 deadline/池清理在该收集路径上无条件执行。"""
    from src.deep_research_dims import context as context_module
    from src.patches.eastmoney_patch import request_deadline

    def fatal_concepts(*args):
        raise BaseExceptionGroup("mixed SDK failure", [asyncio.CancelledError(), SystemExit(9)])

    run = ResearchRun(10)
    with run.bind():
        bound_deadline = request_deadline.get()
        with (
            patch.object(context_module, "_safe_quote", lambda *args: {}),
            patch.object(context_module, "_safe_history", lambda *args: []),
            patch.object(context_module, "_safe_concepts", fatal_concepts),
            patch.object(context_module, "_safe_fundamental", lambda *args: {}),
            patch.object(context_module, "_safe_history_reports", lambda *args: []),
        ):
            with pytest.raises(BaseExceptionGroup) as excinfo:
                context_module.build_shared_context("300260", "样本")
        # 阶段 0 收尾的 reset 必须已在致命组传播前执行（不能残留 stage deadline）。
        assert request_deadline.get() == bound_deadline, "fatal-group collection must still restore the request deadline"
    assert excinfo.value.subgroup(SystemExit) is not None, "fatal member must keep propagating"


def _run_endpoint_stream(side_effect, *, expect_eof: bool = True) -> dict:
    """驱动真实 ``generate_stream``（含真实 service finally/清理链）。

    消费 SSE 到自然 EOF（fatal 场景流永不自然结束，改为有界采样），数终态
    事件数并断言清理/门禁释放跟随真实 service finally。
    """
    from api.v1.endpoints import deep_research as endpoint

    run_id = uuid4().hex
    config = SimpleNamespace(deep_research_timeout_seconds=30, is_agent_available=lambda: True)
    with (
        patch.object(endpoint, "get_config", return_value=config),
        patch.object(endpoint.deep_research_service, "_generate_report", side_effect=side_effect),
    ):
        outcome = asyncio.run(_consume(endpoint, run_id, expect_eof=expect_eof))
    return outcome


async def _consume(endpoint, run_id: str, *, expect_eof: bool) -> dict:
    response = await endpoint.generate_stream(
        endpoint.DeepResearchRequest(stock_code="600519", run_id=run_id)
    )
    stream = response.body_iterator
    raw_events: list[str] = []
    reached_eof = False
    try:
        while True:
            try:
                raw = await asyncio.wait_for(anext(stream), timeout=5.0)
            except StopAsyncIteration:
                reached_eof = True
                break
            raw_events.append(raw)
            if not expect_eof:
                # The fatal-group path never delivers a terminal; sample the
                # stream for a bounded window to prove none shows up.
                try:
                    raw_events.append(await asyncio.wait_for(anext(stream), timeout=0.5))
                except (asyncio.TimeoutError, StopAsyncIteration):
                    pass
                break
    finally:
        await stream.aclose()
    parsed = [json.loads(raw[6:]) for raw in raw_events if raw.startswith("data: ")]
    terminals = [event for event in parsed if event.get("type") in ("done", "error")]
    record = endpoint._RUNS[run_id]
    for _ in range(400):
        if record.cleaned.is_set():
            break
        await asyncio.sleep(0.005)
    return {
        "accepted": any(event.get("type") == "accepted" for event in parsed),
        "reached_eof": reached_eof,
        "terminals": terminals,
        "terminal_count": len(terminals),
        "cleaned": record.cleaned.is_set(),
        "gate_released": run_id not in endpoint._IN_FLIGHT_REQUESTS.values(),
        "finalized": record.control.finalized,
        "control": record.control,
    }


def test_user_cancel_at_sdk_boundary_delivers_exactly_one_terminal():
    """用户取消在 SDK 边界炸出裸 CancelledError：恰好一个取消终态 + 自然 EOF + 门禁释放。

    只替换业务体（``_generate_report``），真实 ``generate_report`` 的
    ``finally: run.finish()`` 与线程池/清理链全部保留——finalized 置位下的
    取消归因必须仍然正确。
    """
    from data_provider.ifind_fundamental_adapter import IfindFetcher
    from src.core.research_run import current_research

    def generate_with_cancelled_supplier(*args, **kwargs):
        run = current_research.get()
        run.stop("cancelled")
        return IfindFetcher("http://127.0.0.1:1/mcp", "fixture-only", 1).query(
            "get_stock_info", "fixture"
        )

    outcome = _run_endpoint_stream(generate_with_cancelled_supplier)
    assert outcome["accepted"] and outcome["reached_eof"], "the stream must end naturally"
    assert outcome["terminal_count"] == 1, "exactly one terminal event must be delivered"
    terminal = outcome["terminals"][0]
    assert terminal.get("type") == "error" and "用户已取消" in terminal.get("message", "")
    assert outcome["cleaned"] and outcome["gate_released"]
    assert outcome["finalized"], "the real service finally must have run"
    assert outcome["control"].stop_reason == "cancelled"


def test_unexpected_sdk_cancellation_keeps_honest_error_wording():
    """无用户停止的意外取消：不冒充取消/预算耗尽，保留真实失败语义。

    只替换业务体，真实 ``generate_report`` finally 仍把 run 置为 finalized
    ——finalized 单独不构成预算耗尽证据（root 复现口径）。
    """
    def unexpected_sdk_cancellation(*args, **kwargs):
        raise asyncio.CancelledError("fixture SDK interruption; no run stop")

    outcome = _run_endpoint_stream(unexpected_sdk_cancellation)
    assert outcome["accepted"] and outcome["reached_eof"]
    assert outcome["terminal_count"] == 1
    terminal = outcome["terminals"][0]
    message = terminal.get("message", "")
    assert terminal.get("type") == "error" and message
    assert "预算" not in message and "用户已取消" not in message
    assert outcome["cleaned"] and outcome["finalized"]
    assert outcome["control"].stop_reason is None
    assert outcome["control"].remaining() > 20


def test_endpoint_never_turns_fatal_group_into_terminal():
    """携带 SystemExit 的取消组不得被终态处理器吞成 error 终态。"""
    def fatal_mixed_group(*args, **kwargs):
        raise BaseExceptionGroup("mixed", [asyncio.CancelledError(), SystemExit(9)])

    outcome = _run_endpoint_stream(fatal_mixed_group, expect_eof=False)
    assert outcome["accepted"]
    assert outcome["terminal_count"] == 0, "fatal group must not become a swallowed terminal"
    assert outcome["cleaned"], "the real service finally still finished and cleaned the run"
