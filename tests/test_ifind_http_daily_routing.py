# -*- coding: utf-8 -*-
"""issue #91：DataFetcherManager 的标准 HTTP 日线预置次序与约束边界（离线）。

覆盖：MCP 优先、MCP 失败后 HTTP 在既有候选链（东财优先）之前、两者都失败
按既有相对次序兜底、约束为任一 iFinD 传输时另一传输不出站（HTTP 和 MCP
不混拼）、HTTP 默认关闭时行为与现状逐位一致、健康熔断按独立来源键隔离。
"""
from __future__ import annotations

import json

import pytest

from data_provider.base import DataFetchError, DataFetcherManager
from data_provider.realtime_types import CircuitBreaker
from tests.test_ifind_http_daily_adapter import fresh_token_cache  # noqa: F401 — pytest fixture
from tests.ifind_daily_testutil import (
    RecordingIfindStub,
    enabled_config,
    http_enabled_config,
    std_frame,
    strict_calendar,
)


@pytest.fixture(autouse=True)
def isolated_daily_source_health(monkeypatch):
    health = DataFetcherManager._daily_source_health
    monkeypatch.setattr(
        DataFetcherManager,
        "_daily_source_health",
        CircuitBreaker(
            failure_threshold=health.failure_threshold,
            cooldown_seconds=health.cooldown_seconds,
            half_open_max_calls=health.half_open_max_calls,
        ),
    )


class _LegacyFetcher:
    name = "LegacyOneFetcher"
    priority = 10

    def __init__(self) -> None:
        self.calls: list[dict] = []

    @property
    def available(self) -> bool:
        return True

    def is_available_for_request(self, capability: str = "") -> bool:
        return True

    def get_daily_data(self, stock_code, start_date=None, end_date=None, days=30):
        self.calls.append({"stock_code": stock_code, "end_date": end_date})
        return std_frame()


class RecordingHttpStub:
    """manager 注入用的 IfindHttpDailyFetcher 桩：记录调用并可编排结果。"""

    name = "IfindHttpDailyFetcher"
    priority = 0
    allow_empty_daily_data = False

    def __init__(self, df=None, error: Exception | None = None) -> None:
        self.df = df
        self.error = error
        self.calls: list[dict] = []
        self._available = True

    @property
    def available(self) -> bool:
        return self._available

    def is_available_for_request(self, capability: str = "") -> bool:
        return self._available

    def get_daily_data(self, stock_code, start_date=None, end_date=None, days=30):
        self.calls.append({
            "stock_code": stock_code, "start_date": start_date,
            "end_date": end_date, "days": days,
        })
        if self.error is not None:
            raise self.error
        return self.df


@pytest.fixture()
def provider_runs(monkeypatch):
    """拦截 data_provider.base.record_provider_run，记录供应商诊断实参。"""
    import data_provider.base as base_module

    runs: list[dict] = []

    def _record(**kwargs):
        runs.append(kwargs)

    monkeypatch.setattr(base_module, "record_provider_run", _record)
    return runs


@pytest.fixture()
def manager(monkeypatch):
    """深研语境 fixture：HTTP 预置为个股深研执行上下文行为（第二十二轮），
    预置相关测试统一在 ``current_research`` 活跃下断言；非深研行为由
    ``test_*_without_deep_research_scope`` 反例组专门锁定。"""
    from src.core.research_run import ResearchRun, current_research

    strict_calendar(monkeypatch)
    http_enabled_config(monkeypatch, mcp_enabled=True)
    legacy = _LegacyFetcher()
    mgr = DataFetcherManager(fetchers=[legacy])
    mgr._ifind_daily_fetcher = None
    mgr._ifind_http_daily_fetcher = None
    run = ResearchRun(timeout_seconds=30)
    research_token = current_research.set(run)
    try:
        yield mgr, legacy
    finally:
        current_research.reset(research_token)


def test_mcp_failure_then_http_succeeds_before_legacy(manager):
    """MCP 失败 → 标准 HTTP 在东财等既有候选之前接管，legacy 不出站。"""
    mgr, legacy = manager
    mgr._ifind_daily_fetcher = RecordingIfindStub(error=DataFetchError("MCP 导出失败"))
    http_stub = RecordingHttpStub(df=std_frame())
    mgr._ifind_http_daily_fetcher = http_stub
    df, source = mgr.get_daily_data("600519")
    assert source == "IfindHttpDailyFetcher"
    assert len(http_stub.calls) == 1
    assert http_stub.calls[0]["stock_code"] == "600519.SH"
    assert http_stub.calls[0]["end_date"] == "2026-11-06"
    assert legacy.calls == []


def test_mcp_success_keeps_http_idle(manager):
    """MCP 成功时标准 HTTP 不出站（一次日线需求只采纳一个来源）。"""
    mgr, legacy = manager
    mgr._ifind_daily_fetcher = RecordingIfindStub(df=std_frame())
    http_stub = RecordingHttpStub(df=std_frame())
    mgr._ifind_http_daily_fetcher = http_stub
    _df, source = mgr.get_daily_data("600519")
    assert source == "IfindDailyFetcher"
    assert http_stub.calls == []


def test_both_ifind_transports_fail_falls_back_in_existing_order(manager):
    """两段 iFinD 候选都失败 → 按既有候选相对次序兜底，真实原因保留。"""
    mgr, legacy = manager
    mgr._ifind_daily_fetcher = RecordingIfindStub(error=DataFetchError("MCP 失败"))
    mgr._ifind_http_daily_fetcher = RecordingHttpStub(error=DataFetchError("HTTP supplier errorcode 7"))
    _df, source = mgr.get_daily_data("600519")
    assert source == "LegacyOneFetcher"
    assert len(legacy.calls) == 1


def test_mcp_fallback_not_http_when_http_disabled(manager, provider_runs, monkeypatch):
    """反例（第十三轮语义保持，第十九轮段级改写）：HTTP 候选**开关关闭**
    时 MCP 失败诊断的下一跳按**当前证券实际可参与性**计算：HTTP 不参与，
    不写 HTTP，直接走通用源（原实现只看全局开关会误写下一跳 HTTP）。段级
    资格后「白名单外」反例不复存在，可参与性由开关/熔断/资格共同决定。"""
    mgr, legacy = manager
    mgr._ifind_daily_fetcher = RecordingIfindStub(error=DataFetchError("MCP 失败"))
    mgr._ifind_http_daily_fetcher = RecordingHttpStub(error=DataFetchError("should not run"))
    # 中途关 HTTP（enabled_config 只配 MCP，无 HTTP 凭据 → ifind_http_daily_enabled False）。
    from tests.ifind_daily_testutil import enabled_config

    enabled_config(monkeypatch, enabled=True)
    _df, source = mgr.get_daily_data("000002")
    assert source == "LegacyOneFetcher"
    mcp_runs = [r for r in provider_runs if r.get("provider") == "IfindDailyFetcher"]
    assert mcp_runs and mcp_runs[0]["success"] is False
    assert mcp_runs[0]["fallback_to"] == "LegacyOneFetcher"
    assert mgr._ifind_http_daily_fetcher.calls == []  # HTTP 关闭零出站


def test_mcp_fallback_not_http_when_http_circuit_open(manager, provider_runs):
    """熔断感知（第十三轮）：HTTP 候选健康熔断 open 时，MCP 失败诊断按实际
    可用性计算——不写下一跳 HTTP（实际直接走通用源）。"""
    mgr, legacy = manager
    mgr._ifind_daily_fetcher = RecordingIfindStub(error=DataFetchError("MCP 失败"))
    mgr._ifind_http_daily_fetcher = RecordingHttpStub(error=DataFetchError("HTTP 失败"))
    health = DataFetcherManager._daily_source_health
    for _ in range(2):  # failure_threshold=2：触发 HTTP 健康键熔断
        health.record_failure("daily_data:cn:IfindHttpDailyFetcher")
    assert not health.is_available("daily_data:cn:IfindHttpDailyFetcher")
    _df, source = mgr.get_daily_data("600519")
    assert source == "LegacyOneFetcher"
    mcp_runs = [r for r in provider_runs if r.get("provider") == "IfindDailyFetcher"]
    assert mcp_runs and mcp_runs[0]["fallback_to"] == "LegacyOneFetcher"
    assert len(legacy.calls) == 1


# ---------------------------------------------------------------------------
# 第十四轮：诊断零副作用（peek/is_available 观察与预约分离）——熔断器
# HALF_OPEN 探测名额只被真正出站消耗，诊断/MCP 成功路径不占位。
# ---------------------------------------------------------------------------
def test_peek_available_lifecycle_with_real_breaker():
    """真实熔断器全生命周期：CLOSED→OPEN→HALF_OPEN→探测成功恢复 CLOSED；
    各态 peek 与 is_available 的取号差异符合观察/预约分离契约。"""
    from data_provider.realtime_types import CircuitBreaker

    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=0.0, half_open_max_calls=1)
    key = "probe-key"
    # CLOSED：peek 与 is_available 一致且无状态迁移。
    assert breaker.peek_available(key) is True
    breaker.record_failure(key)
    breaker.record_failure(key)
    assert breaker.is_available(key) is True  # 冷却已过 → HALF_OPEN 取号（名额 1/1）
    breaker.record_failure(key)  # 探测失败 → 回 OPEN
    assert breaker.is_available(key) is True  # 冷却已过 → 重新半开取号
    breaker.record_success(key)  # 半开探测成功 → CLOSED
    assert breaker.peek_available(key) is True
    assert breaker.is_available(key) is True


def test_peek_vs_is_available_half_open_semantics():
    """直接构造半开态（迁移后冷却窗口内）：peek 多次 True 且名额不变；
    is_available 取号一次后耗尽（如实 False 且不重置）。"""
    import time as _t

    from data_provider.realtime_types import CircuitBreaker

    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=300.0, half_open_max_calls=1)
    key = "half-open-key"
    breaker.record_failure(key)  # OPEN
    state = breaker._states[key]
    # 手工推进到「刚迁移的半开、名额未用」形态（绕过 is_available 取号）。
    state["state"] = CircuitBreaker.HALF_OPEN
    state["half_open_calls"] = 0
    state["last_failure_time"] = _t.time() - 10.0  # 迁移后 10s，冷却 300s 内
    # peek 纯读：多次观察结果一致，名额不消耗。
    assert breaker.peek_available(key) is True
    assert breaker.peek_available(key) is True
    assert state["half_open_calls"] == 0
    # is_available 取号：一次后名额耗尽（冷却窗口内不重置）。
    assert breaker.is_available(key) is True
    assert state["half_open_calls"] == 1
    assert breaker.is_available(key) is False
    # peek 在名额耗尽态如实 False，且不重置名额。
    assert breaker.peek_available(key) is False
    assert state["half_open_calls"] == 1


def test_mcp_success_does_not_consume_http_probe(monkeypatch):
    """MCP 成功路径零占位：HTTP 处于可探测半开态时，MCP 成功不消耗探测名额
    （原实现诊断路径 is_available 会提前占位，后续真实需求被拒）。"""
    import time as _t

    strict_calendar(monkeypatch)
    http_enabled_config(monkeypatch, mcp_enabled=True)
    legacy = _LegacyFetcher()
    mgr = DataFetcherManager(fetchers=[legacy])
    mgr._ifind_daily_fetcher = RecordingIfindStub(df=std_frame())
    mgr._ifind_http_daily_fetcher = RecordingHttpStub(df=std_frame())
    http_stub = mgr._ifind_http_daily_fetcher
    key = "daily_data:cn:IfindHttpDailyFetcher"
    health = DataFetcherManager._daily_source_health
    health.record_failure(key)
    health.record_failure(key)  # OPEN
    # 时间推进过冷却：诊断 peek（纯读）与（若发生）attempt 取号都看到可探测。
    original_time = _t.time
    monkeypatch.setattr(_t, "time", lambda: original_time() + 400.0)
    _df, source = mgr.get_daily_data("600519")
    assert source == "IfindDailyFetcher"  # MCP 成功
    assert http_stub.calls == []  # HTTP 不出站
    # MCP 成功未消耗 HTTP 探测名额：下一个真实需求仍可取号出站。
    assert health.is_available(key) is True
    assert http_stub.calls == []


def test_mcp_failure_http_actually_calls_in_half_open(monkeypatch):
    """MCP 失败 → HTTP 真正出站（半开态取号一次成功），诊断与出站一致。"""
    import time as _t
    from src.core.research_run import ResearchRun, current_research

    strict_calendar(monkeypatch)
    http_enabled_config(monkeypatch, mcp_enabled=True)
    legacy = _LegacyFetcher()
    mgr = DataFetcherManager(fetchers=[legacy])
    mgr._ifind_daily_fetcher = RecordingIfindStub(error=DataFetchError("MCP 失败"))
    http_stub = RecordingHttpStub(df=std_frame())
    mgr._ifind_http_daily_fetcher = http_stub
    research_token = current_research.set(ResearchRun(timeout_seconds=30))
    key = "daily_data:cn:IfindHttpDailyFetcher"
    health = DataFetcherManager._daily_source_health
    health.record_failure(key)
    health.record_failure(key)  # OPEN
    original_time = _t.time
    monkeypatch.setattr(_t, "time", lambda: original_time() + 400.0)
    try:
        _df, source = mgr.get_daily_data("600519")
        assert source == "IfindHttpDailyFetcher"
        assert len(http_stub.calls) == 1  # 半开探测名额被真正出站消耗（一次）
    finally:
        current_research.reset(research_token)


def test_http_only_half_open_probe_not_double_consumed(monkeypatch):
    """HTTP-only 反例（原 P1 场景）：诊断观察 + 出站取号各司其职——半开态
    名额不被诊断预消费，HTTP 真正出站（原实现双 is_available 会吃掉名额
    跳过 HTTP）。"""
    import time as _t
    from src.core.research_run import ResearchRun, current_research

    strict_calendar(monkeypatch)
    http_enabled_config(monkeypatch)  # MCP 关闭
    legacy = _LegacyFetcher()
    mgr = DataFetcherManager(fetchers=[legacy])
    http_stub = RecordingHttpStub(df=std_frame())
    mgr._ifind_http_daily_fetcher = http_stub
    research_token = current_research.set(ResearchRun(timeout_seconds=30))
    key = "daily_data:cn:IfindHttpDailyFetcher"
    health = DataFetcherManager._daily_source_health
    health.record_failure(key)
    health.record_failure(key)  # OPEN
    original_time = _t.time
    monkeypatch.setattr(_t, "time", lambda: original_time() + 400.0)
    try:
        _df, source = mgr.get_daily_data("600519")
        assert source == "IfindHttpDailyFetcher"
        assert len(http_stub.calls) == 1
        assert legacy.calls == []
    finally:
        current_research.reset(research_token)


def test_cache_and_non_qualifying_and_cancel_unaffected(monkeypatch):
    """保持性核查（第十九轮段级改写）：非资格证券预置不出站、取消信号穿透
    诊断观察段。"""
    from src.core.research_run import ResearchRun, ResearchStopped, current_research

    strict_calendar(monkeypatch)
    http_enabled_config(monkeypatch, mcp_enabled=True)
    legacy = _LegacyFetcher()
    mgr = DataFetcherManager(fetchers=[legacy])
    mgr._ifind_daily_fetcher = RecordingIfindStub(error=DataFetchError("MCP 失败"))
    http_stub = RecordingHttpStub(df=std_frame())
    mgr._ifind_http_daily_fetcher = http_stub
    # 非资格（上证指数）：两段 iFinD 预置 prepare 均 None，诊断与出站都不涉及。
    _df, source = mgr.get_daily_data("000001.SH")
    assert source == "LegacyOneFetcher"
    assert http_stub.calls == []
    # 取消信号：诊断观察段 check/peek 之后 attempt 内照常传播停止。
    run = ResearchRun(timeout_seconds=30)
    token = current_research.set(run)
    try:
        import unittest.mock as _mock

        with _mock.patch.object(ResearchRun, "check", side_effect=ResearchStopped("cancelled")):
            with pytest.raises(ResearchStopped):
                mgr.get_daily_data("600519")
    finally:
        current_research.reset(token)


def test_http_preset_failure_diagnostics_counted_with_fallback_to(manager, provider_runs):
    """真实出站的供应商诊断把两段预置尝试都计入，fallback_to 按真实次序。

    第五轮修正：预置源不在通用链 fetchers 内——MCP 失败后的实际下一跳是
    标准 HTTP 预置尝试（不是通用首源），HTTP 失败后的下一跳才是通用链首
    源；约束生效时不虚构 fallback（既有 #94 语义）。
    """
    mgr, legacy = manager
    mgr._ifind_daily_fetcher = RecordingIfindStub(error=DataFetchError("MCP 失败"))
    mgr._ifind_http_daily_fetcher = RecordingHttpStub(error=DataFetchError("HTTP supplier errorcode 7"))
    _df, source = mgr.get_daily_data("600519")
    assert source == "LegacyOneFetcher"
    mcp_runs = [r for r in provider_runs if r.get("provider") == "IfindDailyFetcher"]
    assert len(mcp_runs) == 1
    assert mcp_runs[0]["success"] is False
    assert mcp_runs[0]["fallback_to"] == "IfindHttpDailyFetcher"
    http_runs = [r for r in provider_runs if r.get("provider") == "IfindHttpDailyFetcher"]
    assert len(http_runs) == 1
    assert http_runs[0]["success"] is False
    assert http_runs[0]["fallback_to"] == "LegacyOneFetcher"
    assert "HTTP supplier errorcode 7" in str(http_runs[0].get("error_message"))


def test_mcp_failure_http_success_diagnostics_chain(manager, provider_runs):
    """MCP 失败 → HTTP 接管成功：MCP 诊断 fallback_to=HTTP，无虚构兜底。"""
    mgr, legacy = manager
    mgr._ifind_daily_fetcher = RecordingIfindStub(error=DataFetchError("MCP 失败"))
    mgr._ifind_http_daily_fetcher = RecordingHttpStub(df=std_frame())
    _df, source = mgr.get_daily_data("600519")
    assert source == "IfindHttpDailyFetcher"
    assert legacy.calls == []
    mcp_runs = [r for r in provider_runs if r.get("provider") == "IfindDailyFetcher"]
    assert len(mcp_runs) == 1
    assert mcp_runs[0]["success"] is False
    assert mcp_runs[0]["fallback_to"] == "IfindHttpDailyFetcher"
    http_runs = [r for r in provider_runs if r.get("provider") == "IfindHttpDailyFetcher"]
    assert len(http_runs) == 1
    assert http_runs[0]["success"] is True


def test_mcp_only_keeps_legacy_fallback_target(monkeypatch, provider_runs):
    """HTTP 关闭（仅 MCP）时 MCP 失败的 fallback_to 仍是通用链首源（既有语义）。"""
    strict_calendar(monkeypatch)
    enabled_config(monkeypatch)  # MCP 开、标准 HTTP 关
    legacy = _LegacyFetcher()
    mgr = DataFetcherManager(fetchers=[legacy])
    mgr._ifind_daily_fetcher = RecordingIfindStub(error=DataFetchError("MCP 失败"))
    mgr._ifind_http_daily_fetcher = None
    _df, source = mgr.get_daily_data("600519")
    assert source == "LegacyOneFetcher"
    mcp_runs = [r for r in provider_runs if r.get("provider") == "IfindDailyFetcher"]
    assert len(mcp_runs) == 1
    assert mcp_runs[0]["fallback_to"] == "LegacyOneFetcher"


def test_constraint_mcp_failure_has_no_fallback_target(manager, provider_runs):
    """约束为 MCP 时失败诊断 fallback_to=None：HTTP 与通用链都不出站，不虚构。"""
    mgr, _legacy = manager
    mgr._ifind_daily_fetcher = RecordingIfindStub(error=DataFetchError("MCP 失败"))
    mgr._ifind_http_daily_fetcher = RecordingHttpStub(error=DataFetchError("should not run"))
    with pytest.raises(DataFetchError):
        mgr.get_daily_data("600519", constraint_source="IfindDailyFetcher")
    mcp_runs = [r for r in provider_runs if r.get("provider") == "IfindDailyFetcher"]
    assert mcp_runs and all(r.get("fallback_to") is None for r in mcp_runs)
    assert mgr._ifind_http_daily_fetcher.calls == []


def test_constraint_to_mcp_blocks_http_outbound(manager):
    """已采用窗口来源是 MCP 时，扩窗约束下标准 HTTP 不出站（不混拼）。"""
    mgr, legacy = manager
    mgr._ifind_daily_fetcher = RecordingIfindStub(df=std_frame())
    http_stub = RecordingHttpStub(error=DataFetchError("should not be called"))
    mgr._ifind_http_daily_fetcher = http_stub
    _df, source = mgr.get_daily_data("600519", constraint_source="IfindDailyFetcher")
    assert source == "IfindDailyFetcher"
    assert http_stub.calls == []


def test_constraint_to_http_blocks_mcp_and_legacy(manager):
    """已采用窗口来源是标准 HTTP 时，只有 HTTP 参与扩窗尝试。"""
    mgr, legacy = manager
    mgr._ifind_daily_fetcher = RecordingIfindStub(error=DataFetchError("should not be called"))
    http_stub = RecordingHttpStub(df=std_frame())
    mgr._ifind_http_daily_fetcher = http_stub
    df, source = mgr.get_daily_data("600519", constraint_source="IfindHttpDailyFetcher")
    assert source == "IfindHttpDailyFetcher"
    assert http_stub.calls != []
    assert legacy.calls == []
    assert mgr._ifind_daily_fetcher.calls == []


def test_constraint_to_http_failure_reports_no_ghost_fallback(manager):
    """约束为 HTTP 且失败时，不为被跳过的来源预留预算或报告 fallback。"""
    mgr, _legacy = manager
    mgr._ifind_daily_fetcher = RecordingIfindStub(error=DataFetchError("should not be called"))
    mgr._ifind_http_daily_fetcher = RecordingHttpStub(error=DataFetchError("HTTP 失败"))
    with pytest.raises(DataFetchError) as excinfo:
        mgr.get_daily_data("600519", constraint_source="IfindHttpDailyFetcher")
    message = str(excinfo.value)
    assert "IfindHttpDailyFetcher" in message
    assert mgr._ifind_daily_fetcher.calls == []


def test_http_disabled_keeps_existing_routing_bit_for_bit(monkeypatch):
    """默认关闭（含凭据缺失）时 HTTP 不出站，链路与启用前一致。"""
    strict_calendar(monkeypatch)
    enabled_config(monkeypatch, enabled=False)  # MCP 与 HTTP 均关闭
    legacy = _LegacyFetcher()
    mgr = DataFetcherManager(fetchers=[legacy])
    df, source = mgr.get_daily_data("600519")
    assert source == "LegacyOneFetcher"
    assert len(legacy.calls) == 1


def test_http_health_isolated_per_source_key(manager):
    """标准 HTTP 的供应商故障只计入自己的健康键，不与其他来源互相污染。"""
    mgr, legacy = manager
    mgr._ifind_daily_fetcher = RecordingIfindStub(error=DataFetchError("MCP 失败"))
    mgr._ifind_http_daily_fetcher = RecordingHttpStub(error=DataFetchError("HTTP 失败"))
    for _ in range(2):  # failure_threshold=2：两个键各自独立计数并各自熔断
        _df, source = mgr.get_daily_data("600519")
        assert source == "LegacyOneFetcher"
    health = DataFetcherManager._daily_source_health
    assert not health.is_available("daily_data:cn:IfindHttpDailyFetcher")
    assert not health.is_available("daily_data:cn:IfindDailyFetcher")
    assert health.is_available("daily_data:cn:LegacyOneFetcher")
    assert len(legacy.calls) == 2


def test_run_context_single_fetch_shares_http_window(monkeypatch, fresh_token_cache):
    """深研 run 内同需求共享一次 HTTP 窗口尝试（single_fetch 键含传输身份）。

    一次窗口尝试 = 两笔数据出站（CPS=6 取数 + CPS=1 单位对照）+ 首次
    get_access_token 换取；同键共享整次尝试，不同窗口键重新出站。
    """
    from src.core.research_run import ResearchRun, current_research

    from data_provider import ifind_http_daily_adapter as H
    from tests.test_ifind_http_daily_adapter import install_cps_dispatch

    strict_calendar(monkeypatch)
    http_enabled_config(monkeypatch)
    calls: list[dict] = []
    install_cps_dispatch(monkeypatch, calls)
    fetcher = H.IfindHttpDailyFetcher()
    run = ResearchRun(timeout_seconds=30)
    token = current_research.set(run)
    try:
        df1 = fetcher._fetch_raw_data("600519.SH", "2026-11-04", "2026-11-06")
        df2 = fetcher._fetch_raw_data("600519.SH", "2026-11-04", "2026-11-06")
        assert len(df1) == 3 and len(df2) == 3
        data_calls = [c for c in calls if c["url"] != H._TOKEN_ENDPOINT]
        assert len(calls) == 3  # 一次换取 + 共享的两次数据出站（同键共享整次尝试）
        assert [json.loads(c["body"])["functionpara"]["CPS"] for c in data_calls] == ["6", "1"]
        df3 = fetcher._fetch_raw_data("600519.SH", "2026-11-05", "2026-11-06")
        assert len(df3) == 2
        data_calls = [c for c in calls if c["url"] != H._TOKEN_ENDPOINT]
        assert len(data_calls) == 4  # 不同窗口键不同，重新出站
    finally:
        current_research.reset(token)


# ---------------------------------------------------------------------------
# 第二十二轮：HTTP 预置限定个股深研执行上下文（非深研零出站反例组）
# ---------------------------------------------------------------------------
def test_http_preset_requires_deep_research_scope(provider_runs, monkeypatch):
    """非深研（无 ResearchRun）：HTTP 预置零出站，MCP 仍按既有顺序参与。

    alert/backtest/compass 等服务经同一 ``get_daily_data`` 入口，启用 HTTP
    后其数据源顺序必须保持原样：MCP 成功照常接管、MCP 失败落到通用链首源
    （不是 HTTP），且无 HTTP 诊断记录。自建 manager（不带深研 fixture）。"""
    from src.core.research_run import current_research

    assert current_research.get() is None  # 反例前置：确无深研上下文
    strict_calendar(monkeypatch)
    http_enabled_config(monkeypatch, mcp_enabled=True)
    legacy = _LegacyFetcher()
    mgr = DataFetcherManager(fetchers=[legacy])
    mgr._ifind_daily_fetcher = RecordingIfindStub(df=std_frame())
    http_stub = RecordingHttpStub(df=std_frame())
    mgr._ifind_http_daily_fetcher = http_stub
    _df, source = mgr.get_daily_data("600519")
    assert source == "IfindDailyFetcher"  # MCP 既有顺序保持
    assert http_stub.calls == []  # HTTP 零出站
    mgr._ifind_daily_fetcher = RecordingIfindStub(error=DataFetchError("MCP 失败"))
    _df, source = mgr.get_daily_data("000002")
    assert source == "LegacyOneFetcher"  # MCP 失败落通用首源，不落 HTTP
    assert http_stub.calls == []
    http_runs = [r for r in provider_runs if r.get("provider") == "IfindHttpDailyFetcher"]
    assert http_runs == []  # 无 HTTP 诊断（无出站即无 record）


def test_http_constraint_outside_deep_research_stays_gated(monkeypatch):
    """约束路径不构成绕过：非深研 + constraint_source 点名 HTTP 仍零出站。

    核实（第二十二轮）：生产 constraint_source 仅由 history_loader 深研
    作用域（slot/retained）设置，不存在「非深研 + 显式约束 HTTP」调用；
    本反例锁定即使出现该形态也不绕过深研门（深研扩窗内约束行为由
    ``test_constraint_to_http_blocks_mcp_and_legacy`` 等既有测试在
    manager fixture 深研语境下覆盖）。"""
    from src.core.research_run import current_research

    assert current_research.get() is None
    strict_calendar(monkeypatch)
    http_enabled_config(monkeypatch, mcp_enabled=True)
    legacy = _LegacyFetcher()
    mgr = DataFetcherManager(fetchers=[legacy])
    http_stub = RecordingHttpStub(df=std_frame())
    mgr._ifind_http_daily_fetcher = http_stub
    # 约束语义（issue #94）：点名源不可用时显式失败，被排除候选零出站。
    with pytest.raises(DataFetchError):
        mgr.get_daily_data("600519", constraint_source="IfindHttpDailyFetcher")
    assert http_stub.calls == []
    assert legacy.calls == []


def test_alert_style_thread_call_zero_http_egress(monkeypatch):
    """alert 业务入口形态（asyncio.to_thread 独立线程、无深研上下文）：
    HTTP 零出站、MCP 失败后东财等既有候选照常接管。"""
    import asyncio
    from src.core.research_run import current_research

    strict_calendar(monkeypatch)
    http_enabled_config(monkeypatch, mcp_enabled=True)
    legacy = _LegacyFetcher()
    mgr = DataFetcherManager(fetchers=[legacy])
    mgr._ifind_daily_fetcher = RecordingIfindStub(error=DataFetchError("MCP 失败"))
    http_stub = RecordingHttpStub(df=std_frame())
    mgr._ifind_http_daily_fetcher = http_stub

    async def _alert_like_fetch():
        # 复刻 alert_service._evaluate_volume 的取数形态：
        # asyncio.to_thread(DataFetcherManager().get_daily_data, code, days=20)
        assert current_research.get() is None
        return await asyncio.to_thread(mgr.get_daily_data, "600519", days=20)

    df, source = asyncio.run(_alert_like_fetch())
    assert source == "LegacyOneFetcher"
    assert http_stub.calls == []
    assert len(legacy.calls) == 1  # 既有候选照常（MCP 失败后原顺序）


def test_sector_llm_thread_run_does_not_leak_to_other_threads(monkeypatch):
    """板块分析反例：sector LLM 线程内 set 的 ResearchRun 不泄漏到其他执行
    流——另一线程（to_thread 独立上下文）的日线获取零 HTTP 出站。"""
    import asyncio
    from src.core.research_run import ResearchRun, current_research

    strict_calendar(monkeypatch)
    http_enabled_config(monkeypatch, mcp_enabled=True)
    legacy = _LegacyFetcher()
    mgr = DataFetcherManager(fetchers=[legacy])
    http_stub = RecordingHttpStub(df=std_frame())
    mgr._ifind_http_daily_fetcher = http_stub

    async def _sector_like_llm_thread():
        # 复刻 sector_analysis_service._call_model：线程内局部 set run（LLM 预算）。
        run = ResearchRun(timeout_seconds=30)
        token = current_research.set(run)
        try:
            assert current_research.get() is run
        finally:
            current_research.reset(token)

    async def _independent_fetch_thread():
        # 独立取数线程：context 不含上述 run（线程隔离）。
        assert current_research.get() is None
        return await asyncio.to_thread(mgr.get_daily_data, "600519", days=20)

    asyncio.run(_sector_like_llm_thread())
    df, source = asyncio.run(_independent_fetch_thread())
    assert source == "LegacyOneFetcher"
    assert http_stub.calls == []


def test_non_cn_market_http_preset_stays_gated_even_in_deep_research(manager):
    """港美股反例：深研上下文内 hk 市场仍不参与 iFinD 预置（市场门原样）。"""
    mgr, legacy = manager
    http_stub = RecordingHttpStub(df=std_frame())
    mgr._ifind_http_daily_fetcher = http_stub
    df, source = mgr.get_daily_data("hk00700")
    assert http_stub.calls == []
    assert source == "LegacyOneFetcher"
