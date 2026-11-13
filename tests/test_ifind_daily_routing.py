# -*- coding: utf-8 -*-
"""issue #86：DataFetcherManager 的 iFinD 日线路由与兜底边界（离线）。

覆盖：合格身份优先尝试、失败按既有候选相对次序兜底、未资格证券/历史目标/
日历不可用在出站前跳过且不计供应商故障、未来结束日约束、停止信号传播不
触发兜底、短期熔断只影响 iFinD 自身键。
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager

import pytest


from data_provider.base import BaseFetcher, DataFetchError, DataFetcherManager
from data_provider.realtime_types import CircuitBreaker
from src.core.research_run import ResearchRun, ResearchStopped
from src.patches.eastmoney_patch import request_deadline
from tests.ifind_daily_testutil import (
    LATEST_SESSION,
    RecordingIfindStub,
    enabled_config,
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


class _LegacyFetcher(BaseFetcher):
    def __init__(self, name: str, df=None, error: Exception | None = None):
        self.name = name
        self.priority = 10
        self.df = df
        self.error = error
        self.calls: list[dict] = []
        # 调用瞬间生效的 request_deadline 剩余量：观测尝试级预算截断。
        self.observed_remaining: list[float] = []

    def _fetch_raw_data(self, stock_code, start_date, end_date):
        raise NotImplementedError

    def _normalize_data(self, df, stock_code):
        return df

    def get_daily_data(self, stock_code, start_date=None, end_date=None, days=30):
        deadline = request_deadline.get()
        self.observed_remaining.append(
            (deadline - time.monotonic()) if deadline is not None else float("inf")
        )
        self.calls.append({
            "stock_code": stock_code, "start_date": start_date,
            "end_date": end_date, "days": days,
        })
        if self.error is not None:
            raise self.error
        if self.df is None:
            return std_frame()
        return self.df


class _BudgetIfindStub(RecordingIfindStub):
    """在 RecordingIfindStub 上叠加 request_deadline 剩余量观测。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.observed_remaining: list[float] = []

    def get_daily_data(self, stock_code, start_date=None, end_date=None, days=30):
        deadline = request_deadline.get()
        self.observed_remaining.append(
            (deadline - time.monotonic()) if deadline is not None else float("inf")
        )
        return super().get_daily_data(stock_code, start_date=start_date, end_date=end_date, days=days)


@contextmanager
def daily_deadline(seconds: float):
    """为一次 manager 调用设定外层 request_deadline（离开即还原）。"""
    token = request_deadline.set(time.monotonic() + seconds)
    try:
        yield
    finally:
        request_deadline.reset(token)


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
    strict_calendar(monkeypatch)
    enabled_config(monkeypatch)
    first = _LegacyFetcher("LegacyOneFetcher")
    second = _LegacyFetcher("LegacyTwoFetcher")
    mgr = DataFetcherManager(fetchers=[first, second])
    mgr._ifind_daily_fetcher = None
    return mgr, first, second


def test_qualified_stock_uses_ifind_first_without_legacy_call(manager):
    mgr, first, second = manager
    stub = RecordingIfindStub(df=std_frame())
    mgr._ifind_daily_fetcher = stub
    df, source = mgr.get_daily_data("600519.SH")
    assert source == "IfindDailyFetcher"
    assert len(df) == 5
    assert len(stub.calls) == 1
    assert stub.calls[0]["stock_code"] == "600519.SH"
    assert stub.calls[0]["end_date"] == LATEST_SESSION.isoformat()
    assert first.calls == [] and second.calls == []


def test_bare_cn_code_infers_exchange_identity(manager):
    mgr, _first, _second = manager
    stub = RecordingIfindStub(df=std_frame())
    mgr._ifind_daily_fetcher = stub
    _df, source = mgr.get_daily_data("000001")
    assert source == "IfindDailyFetcher"
    assert stub.calls[0]["stock_code"] == "000001.SZ"


def test_ifind_failure_falls_back_with_unchanged_candidate_order(manager):
    mgr, first, second = manager
    stub = RecordingIfindStub(error=DataFetchError("ifind 现金复权元数据不合格，整窗不采纳"))
    mgr._ifind_daily_fetcher = stub
    df, source = mgr.get_daily_data("600519")
    assert source == "LegacyOneFetcher"
    assert len(first.calls) == 1 and second.calls == []
    assert stub.calls[0]["stock_code"] == "600519.SH"


@pytest.mark.parametrize("code", ["000001.SH", "399001.SZ", "399006.SZ", "510300", "510300.SH", "920748", "HK00700", "900901"])
def test_unsupported_codes_never_dispatch_ifind(manager, code):
    mgr, first, _second = manager
    stub = RecordingIfindStub(df=std_frame())
    mgr._ifind_daily_fetcher = stub
    _df, source = mgr.get_daily_data(code)
    assert stub.calls == []
    assert source == "LegacyOneFetcher"
    # 未资格证券不出站也不计 iFinD 供应商故障
    assert DataFetcherManager._daily_source_health.is_available("daily_data:cn:IfindDailyFetcher")


def test_historical_target_skips_ifind_and_passes_real_cutoff(manager):
    mgr, first, _second = manager
    stub = RecordingIfindStub(df=std_frame())
    mgr._ifind_daily_fetcher = stub
    _df, source = mgr.get_daily_data("600519", end_date="2026-11-05")
    assert stub.calls == []
    assert source == "LegacyOneFetcher"
    # 真实目标结束日传入原源，不再「先取今日再裁剪」
    assert first.calls[0]["end_date"] == "2026-11-05"


def test_future_end_clamped_to_latest_completed_session(manager):
    mgr, _first, _second = manager
    stub = RecordingIfindStub(df=std_frame())
    mgr._ifind_daily_fetcher = stub
    _df, _source = mgr.get_daily_data("600519", end_date="2026-11-10")
    assert stub.calls[0]["end_date"] == LATEST_SESSION.isoformat()


def test_disabled_config_keeps_original_routing(monkeypatch):
    strict_calendar(monkeypatch)
    enabled_config(monkeypatch, enabled=False)
    first = _LegacyFetcher("LegacyOneFetcher")
    mgr = DataFetcherManager(fetchers=[first])
    stub = RecordingIfindStub(df=std_frame())
    mgr._ifind_daily_fetcher = stub
    _df, source = mgr.get_daily_data("600519")
    assert stub.calls == []
    assert source == "LegacyOneFetcher"


def test_calendar_unavailable_skips_ifind(monkeypatch):
    from src.core import trading_calendar as tc

    enabled_config(monkeypatch)
    monkeypatch.setattr(tc, "recent_completed_trading_sessions", lambda *a, **k: None)
    first = _LegacyFetcher("LegacyOneFetcher")
    mgr = DataFetcherManager(fetchers=[first])
    stub = RecordingIfindStub(df=std_frame())
    mgr._ifind_daily_fetcher = stub
    _df, source = mgr.get_daily_data("600519")
    assert stub.calls == []
    assert source == "LegacyOneFetcher"


# ---------------------------------------------------------------------------
# issue #94：来源约束——已采用窗口的扩窗需求只尝试同源候选，其余不出站
# ---------------------------------------------------------------------------
def test_constraint_ifind_success_never_touches_legacy(manager):
    mgr, first, second = manager
    stub = RecordingIfindStub(df=std_frame(rows=120))
    mgr._ifind_daily_fetcher = stub
    df, source = mgr.get_daily_data("600519", constraint_source="IfindDailyFetcher")
    assert source == "IfindDailyFetcher" and len(df) == 120
    assert first.calls == [] and second.calls == []


def test_constraint_ifind_failure_skips_legacy_outbound(manager):
    mgr, first, second = manager
    stub = RecordingIfindStub(error=DataFetchError("ifind 完整窗口缺少交易日"))
    mgr._ifind_daily_fetcher = stub
    with pytest.raises(DataFetchError) as excinfo:
        mgr.get_daily_data("600519", constraint_source="IfindDailyFetcher")
    assert len(stub.calls) == 1
    assert first.calls == [] and second.calls == []
    message = str(excinfo.value)
    assert "IfindDailyFetcher" in message
    assert "LegacyOneFetcher" in message and "LegacyTwoFetcher" in message
    assert "未出站" in message


def test_constraint_limits_cn_attempts_to_named_legacy(manager):
    mgr, first, second = manager
    stub = RecordingIfindStub(df=std_frame())
    mgr._ifind_daily_fetcher = stub
    df, source = mgr.get_daily_data("600519", constraint_source="LegacyOneFetcher")
    assert source == "LegacyOneFetcher"
    assert len(first.calls) == 1
    # 非同源候选（含 iFinD 预置尝试）一律不出站
    assert stub.calls == [] and second.calls == []


def test_constraint_unknown_source_fails_without_outbound(manager):
    mgr, first, second = manager
    stub = RecordingIfindStub(df=std_frame())
    mgr._ifind_daily_fetcher = stub
    with pytest.raises(DataFetchError) as excinfo:
        mgr.get_daily_data("600519", constraint_source="GhostFetcher")
    assert stub.calls == [] and first.calls == [] and second.calls == []
    assert "GhostFetcher" in str(excinfo.value)


def test_research_stop_propagates_without_fallback(manager):
    mgr, first, _second = manager
    stub = RecordingIfindStub(error=ResearchStopped("用户已取消本次研究"))
    mgr._ifind_daily_fetcher = stub
    with pytest.raises(ResearchStopped):
        mgr.get_daily_data("600519")
    assert first.calls == []
    # 停止后的失败不计入熔断
    assert DataFetcherManager._daily_source_health.is_available("daily_data:cn:IfindDailyFetcher")


def test_circuit_breaker_only_gates_ifind_key(manager):
    mgr, first, _second = manager
    stub = RecordingIfindStub(error=DataFetchError("ifind 完整窗口缺少交易日"))
    mgr._ifind_daily_fetcher = stub
    for _ in range(2):
        _df, source = mgr.get_daily_data("600519")
        assert source == "LegacyOneFetcher"
    assert len(stub.calls) == 2
    # 第三次：iFinD 日线键熔断，直接走既有候选
    _df, source = mgr.get_daily_data("600519")
    assert source == "LegacyOneFetcher"
    assert len(stub.calls) == 2
    assert len(first.calls) == 3
    assert first.calls[0]["stock_code"] == "600519"


def test_expired_outer_deadline_during_query_is_not_health_failure(monkeypatch):
    import time
    from types import SimpleNamespace

    from data_provider.ifind_daily_adapter import IfindDailyFetcher
    from src.patches.eastmoney_patch import request_deadline
    from src.services import history_loader as history

    strict_calendar(monkeypatch)
    enabled_config(monkeypatch)
    monkeypatch.setattr(
        DataFetcherManager,
        "_daily_source_health",
        CircuitBreaker(failure_threshold=2, cooldown_seconds=60, half_open_max_calls=1),
    )
    legacy = _LegacyFetcher("LegacyOneFetcher")
    mgr = DataFetcherManager(fetchers=[legacy])
    calls = []

    def query(*_args, **_kwargs):
        calls.append("query")
        time.sleep(0.2)
        return SimpleNamespace(answer="", indicators_params={})

    mgr._ifind_daily_fetcher = IfindDailyFetcher(SimpleNamespace(available=True, query=query))
    monkeypatch.setattr(history, "_get_fetcher_manager", lambda: mgr)
    token, scope = history.open_current_stock_scope("600519", target_date=LATEST_SESSION)
    deadline = request_deadline.set(time.monotonic() + 0.05)
    try:
        with pytest.raises(TimeoutError):
            history.load_history_df("600519", days=1, target_date=LATEST_SESSION)
        assert calls == ["query"]
        assert legacy.calls == []
        assert scope.peek() is None
        assert DataFetcherManager._daily_source_health.is_available("daily_data:cn:IfindDailyFetcher")
    finally:
        request_deadline.reset(deadline)
        history.close_current_stock_scope(token)


def test_local_attempt_timeout_with_healthy_outer_budget_falls_back(manager):
    mgr, first, _second = manager
    stub = RecordingIfindStub(error=TimeoutError("daily attempt deadline exhausted"))
    mgr._ifind_daily_fetcher = stub
    _df, source = mgr.get_daily_data("600519", end_date=LATEST_SESSION.isoformat())
    assert source == "LegacyOneFetcher"
    assert len(first.calls) == 1


def test_deadline_expired_before_dispatch_sends_nothing(manager):
    import time

    from src.patches.eastmoney_patch import request_deadline

    mgr, first, _second = manager
    stub = RecordingIfindStub(df=std_frame())
    mgr._ifind_daily_fetcher = stub
    deadline = request_deadline.set(time.monotonic() - 1)
    try:
        with pytest.raises(TimeoutError):
            mgr.get_daily_data("600519", end_date=LATEST_SESSION.isoformat())
        assert stub.calls == []
        assert first.calls == []
    finally:
        request_deadline.reset(deadline)


# ---------------------------------------------------------------------------
# issue #94 复审收敛：预算与 fallback 诊断按受约束后的真实候选链计算。
# 反例：仅剩 iFinD 可尝试且剩余 20 秒时，为不存在的 fallback 预留预算
# （min(8, 20×0.3)=6 秒）会把本可在 16 秒完成的请求提前截断到 14 秒；
# 失败诊断同时把 fallback_to / 数据源切换 / 剩余计数报告给被跳过的源。
# 无约束路径的既有行为由对照测试钉住。
# ---------------------------------------------------------------------------
def test_constraint_ifind_attempt_keeps_full_deadline(manager):
    mgr, first, second = manager
    stub = _BudgetIfindStub(error=DataFetchError("ifind 完整窗口缺少交易日"))
    mgr._ifind_daily_fetcher = stub
    with daily_deadline(20.0), pytest.raises(DataFetchError):
        mgr.get_daily_data("600519", constraint_source="IfindDailyFetcher")
    assert len(stub.calls) == 1
    # 约束 iFinD 时通用链无任何同源候选：不为幻影 fallback 预留预算
    assert stub.observed_remaining[0] > 19.0
    assert first.calls == [] and second.calls == []


def test_unconstrained_ifind_attempt_still_reserves_fallback_budget(manager):
    mgr, first, second = manager
    stub = _BudgetIfindStub(error=DataFetchError("ifind 完整窗口缺少交易日"))
    mgr._ifind_daily_fetcher = stub
    with daily_deadline(20.0):
        _df, source = mgr.get_daily_data("600519")
    assert source == "LegacyOneFetcher"
    # 无约束：既有行为——为 legacy 兜底预留（20 秒预留 min(8, 6)=6 秒）
    assert 10.0 < stub.observed_remaining[0] < 19.0


def test_constraint_named_legacy_attempt_keeps_full_deadline(manager):
    mgr, first, second = manager
    first.error = RuntimeError("legacy one 失败")
    mgr._ifind_daily_fetcher = _BudgetIfindStub(df=std_frame())
    with daily_deadline(20.0), pytest.raises(DataFetchError):
        mgr.get_daily_data("600519", constraint_source="LegacyOneFetcher")
    assert len(first.calls) == 1
    # 同名候选之后全是被约束排除的源：不为其后的幻影 fallback 预留预算
    assert first.observed_remaining[0] > 19.0
    assert second.calls == []


def test_unconstrained_general_loop_reserves_then_full_last_attempt(manager):
    mgr, first, second = manager
    first.error = RuntimeError("legacy one 失败")
    mgr._ifind_daily_fetcher = _BudgetIfindStub(error=DataFetchError("x"))
    with daily_deadline(20.0):
        _df, source = mgr.get_daily_data("600519")
    assert source == "LegacyTwoFetcher"
    # 无约束：既有行为——非末位候选预留、末位候选不预留
    assert 10.0 < first.observed_remaining[0] < 19.0
    assert second.observed_remaining[0] > 19.0


def test_constraint_ifind_failure_diagnostics_have_no_phantom_fallback(
    manager, provider_runs, caplog
):
    mgr, first, second = manager
    stub = RecordingIfindStub(error=DataFetchError("ifind 完整窗口缺少交易日"))
    mgr._ifind_daily_fetcher = stub
    with pytest.raises(DataFetchError) as excinfo:
        mgr.get_daily_data("600519", constraint_source="IfindDailyFetcher")
    ifind_runs = [r for r in provider_runs if r.get("provider") == "IfindDailyFetcher"]
    assert ifind_runs, "iFinD 失败必须留有供应商诊断记录"
    assert all(r.get("fallback_to") is None for r in ifind_runs)
    # 「继续既有候选」在约束 iFinD 时不成立：没有既有候选会出站
    assert "继续既有候选" not in caplog.text
    message = str(excinfo.value)
    assert "未出站" in message and "LegacyOneFetcher" in message


def test_constraint_named_legacy_failure_diagnostics_have_no_phantom_fallback(
    manager, provider_runs, caplog
):
    mgr, first, second = manager
    first.error = RuntimeError("legacy one 失败")
    mgr._ifind_daily_fetcher = RecordingIfindStub(df=std_frame())
    caplog.set_level(logging.INFO)
    with pytest.raises(DataFetchError) as excinfo:
        mgr.get_daily_data("600519", constraint_source="LegacyOneFetcher")
    first_runs = [r for r in provider_runs if r.get("provider") == "LegacyOneFetcher"]
    assert first_runs, "同名候选失败必须留有供应商诊断记录"
    assert all(r.get("fallback_to") is None for r in first_runs)
    # 不会切换到被约束排除的 LegacyTwoFetcher
    assert "数据源切换" not in caplog.text
    message = str(excinfo.value)
    assert "LegacyTwoFetcher" in message and "未出站" in message


def test_unconstrained_failure_diagnostics_keep_fallback_chain(
    manager, provider_runs, caplog
):
    mgr, first, second = manager
    first.error = RuntimeError("legacy one 失败")
    mgr._ifind_daily_fetcher = RecordingIfindStub(error=DataFetchError("x"))
    caplog.set_level(logging.INFO)
    _df, source = mgr.get_daily_data("600519")
    assert source == "LegacyTwoFetcher"
    fallback_by_provider: dict[str, list] = {}
    for run in provider_runs:
        fallback_by_provider.setdefault(run.get("provider"), []).append(
            run.get("fallback_to")
        )
    # 无约束：既有 fallback 链诊断保持不变
    assert fallback_by_provider.get("IfindDailyFetcher") == ["LegacyOneFetcher"]
    assert fallback_by_provider.get("LegacyOneFetcher") == ["LegacyTwoFetcher"]
    assert "继续既有候选" in caplog.text
    assert "数据源切换" in caplog.text


def test_constraint_budget_exhaustion_counts_only_eligible(manager):
    mgr, first, second = manager
    mgr._ifind_daily_fetcher = RecordingIfindStub(df=std_frame())
    with daily_deadline(-1.0), pytest.raises(DataFetchError) as excinfo:
        mgr.get_daily_data("600519", constraint_source="LegacyOneFetcher")
    # 预算耗尽时只计入真实可尝试候选（LegacyTwoFetcher 已被约束排除）
    assert "剩余 1 个数据源未尝试" in str(excinfo.value)
    assert first.calls == [] and second.calls == []


def test_unconstrained_budget_exhaustion_counts_all_remaining(manager):
    mgr, first, second = manager
    stub = RecordingIfindStub(df=std_frame())
    stub._available = False  # iFinD 不可用 → 跳过预置尝试，走既有通用链
    mgr._ifind_daily_fetcher = stub
    with daily_deadline(-1.0), pytest.raises(DataFetchError) as excinfo:
        mgr.get_daily_data("600519")
    # 无约束：既有行为——剩余计数按全量候选
    assert "剩余 2 个数据源未尝试" in str(excinfo.value)
    assert stub.calls == [] and first.calls == [] and second.calls == []


def test_constraint_ifind_open_breaker_skips_prechain(manager):
    mgr, first, second = manager
    stub = RecordingIfindStub(df=std_frame())
    mgr._ifind_daily_fetcher = stub
    health = DataFetcherManager._daily_source_health
    health.record_failure("daily_data:cn:IfindDailyFetcher", error="boom")
    health.record_failure("daily_data:cn:IfindDailyFetcher", error="boom")
    with pytest.raises(DataFetchError) as excinfo:
        mgr.get_daily_data("600519", constraint_source="IfindDailyFetcher")
    message = str(excinfo.value)
    assert "CircuitOpen" in message
    assert stub.calls == [] and first.calls == [] and second.calls == []


def test_constraint_named_legacy_open_breaker_skips_outbound(manager):
    mgr, first, second = manager
    mgr._ifind_daily_fetcher = RecordingIfindStub(df=std_frame())
    health = DataFetcherManager._daily_source_health
    health.record_failure("daily_data:cn:LegacyOneFetcher", error="boom")
    health.record_failure("daily_data:cn:LegacyOneFetcher", error="boom")
    with pytest.raises(DataFetchError) as excinfo:
        mgr.get_daily_data("600519", constraint_source="LegacyOneFetcher")
    message = str(excinfo.value)
    assert "CircuitOpen" in message and "LegacyOneFetcher" in message
    assert first.calls == [] and second.calls == []
    assert mgr._ifind_daily_fetcher.calls == []


def test_constraint_ifind_stop_propagates_without_fallback(manager):
    mgr, first, second = manager
    stub = RecordingIfindStub(error=ResearchStopped("用户已取消本次研究"))
    mgr._ifind_daily_fetcher = stub
    with daily_deadline(20.0), pytest.raises(ResearchStopped):
        mgr.get_daily_data("600519", constraint_source="IfindDailyFetcher")
    assert len(stub.calls) == 1
    assert first.calls == [] and second.calls == []


def test_constraint_named_legacy_stop_propagates(manager):
    mgr, first, second = manager
    first.error = ResearchStopped("用户已取消本次研究")
    mgr._ifind_daily_fetcher = RecordingIfindStub(df=std_frame())
    with pytest.raises(ResearchStopped):
        mgr.get_daily_data("600519", constraint_source="LegacyOneFetcher")
    assert len(first.calls) == 1 and second.calls == []
