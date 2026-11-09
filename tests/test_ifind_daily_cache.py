# -*- coding: utf-8 -*-
"""issue #86：共享窗口作用域、持久缓存权威边界与 revision-2 污染守卫（离线）。

覆盖：启用范围内 DB 命中不作依据、失败/迟到结果不覆盖已采用帧、换源扩窗
降级保留、并发同需求只完整获取一次、深研 run 槽位复用、关闭/未启用模式下
仅含 iFinD 遗留行的窗口被拒而纯旧源混存缓存保持原命中规则、普通 pipeline
fetch/趋势/今日昨日上下文/Agent 历史共用同一合格窗口。
"""
from __future__ import annotations

import threading
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.services import history_loader as H
from tests.ifind_daily_testutil import (
    LATEST_SESSION,
    enabled_config,
    std_frame,
    strict_calendar,
)


class _DailyRow:
    def __init__(self, code: str, row_date: date, close: float, source: str) -> None:
        self.code = code
        self.date = row_date
        self.open = close - 1
        self.high = close + 1
        self.low = close - 2
        self.close = close
        self.volume = 1000
        self.amount = 10000
        self.pct_chg = 1.0
        self.ma5 = close
        self.ma10 = close
        self.ma20 = close
        self.volume_ratio = 1.0
        self.data_source = source

    def to_dict(self):
        return {k: getattr(self, k) for k in (
            "code", "date", "open", "high", "low", "close", "volume", "amount",
            "pct_chg", "ma5", "ma10", "ma20", "volume_ratio", "data_source",
        )}


def _db_rows(code: str, count: int, sources, end: date = LATEST_SESSION):
    rows = []
    for offset in range(count):
        day = end - timedelta(days=offset)
        source = sources[offset % len(sources)]
        rows.append(_DailyRow(code, day, 100.0 - offset, source))
    return rows


class _FakeDb:
    def __init__(self, rows=None) -> None:
        self.rows = rows or []
        self.get_data_range = MagicMock(side_effect=self._range)
        self.save_daily_data = MagicMock(return_value=0)
        self.get_analysis_context = MagicMock(return_value=None)
        self.has_today_data = MagicMock(return_value=True)
        self._analyze_ma_status = staticmethod(lambda data: "震荡整理 ↔")

    def _range(self, code, start, end):
        return sorted(
            [r for r in self.rows if r.code == code and start <= r.date <= end],
            key=lambda r: r.date,
        )

    def get_latest_data(self, code, days=2):
        matched = sorted(
            [r for r in self.rows if r.code == code],
            key=lambda r: r.date,
            reverse=True,
        )
        return matched[:days]


class _FakeManager:
    def __init__(self, frames, delay: float = 0.0, error: Exception | None = None) -> None:
        self.get_stock_name = lambda code, allow_realtime=False: code
        self.frames = list(frames)
        self.delay = delay
        self.error = error
        self.calls: list[dict] = []

    def get_daily_data(self, stock_code, start_date=None, end_date=None, days=30):
        self.calls.append({
            "stock_code": stock_code, "start_date": start_date,
            "end_date": end_date, "days": days,
        })
        if self.delay:
            threading.Event().wait(self.delay)
        if self.error is not None:
            raise self.error
        entry = self.frames.pop(0) if self.frames else std_frame()
        if isinstance(entry, tuple):
            return entry
        return entry, "IfindDailyFetcher"


@pytest.fixture()
def offline(monkeypatch):
    strict_calendar(monkeypatch)
    monkeypatch.setattr(H, "_fetcher_singleton", None)


def _install(monkeypatch, db: _FakeDb, manager: _FakeManager):
    monkeypatch.setattr("src.storage.get_db", lambda: db)
    monkeypatch.setattr(H, "_get_fetcher_manager", lambda: manager)
    return manager


# ---------------------------------------------------------------------------
# revision-2 守卫：纯旧源混存命中保留；含 iFinD 遗留行（含混合）拒绝
# ---------------------------------------------------------------------------
def test_disabled_mode_pure_legacy_mixed_cache_still_hits(monkeypatch, offline):
    enabled_config(monkeypatch, enabled=False)
    db = _FakeDb(_db_rows("600519", 40, ["EfinanceFetcher", "AkshareFetcher"]))
    manager = _install(monkeypatch, db, _FakeManager([std_frame()]))
    df, source = H.load_history_df("600519", days=30, target_date=LATEST_SESSION)
    assert source == "db_cache" and df is not None
    assert manager.calls == []


def test_disabled_mode_rejects_ifind_contaminated_mixed_cache(monkeypatch, offline):
    enabled_config(monkeypatch, enabled=False)
    rows = _db_rows("600519", 40, ["EfinanceFetcher", "IfindDailyFetcher"])
    db = _FakeDb(rows)
    manager = _install(monkeypatch, db, _FakeManager([std_frame()]))
    df, source = H.load_history_df("600519", days=30, target_date=LATEST_SESSION)
    assert source == "IfindDailyFetcher"
    assert df is not None and len(manager.calls) == 1


def test_disabled_mode_rejects_pure_ifind_cache(monkeypatch, offline):
    enabled_config(monkeypatch, enabled=False)
    db = _FakeDb(_db_rows("600519", 40, ["IfindDailyFetcher"]))
    manager = _install(monkeypatch, db, _FakeManager([std_frame()]))
    _df, source = H.load_history_df("600519", days=30, target_date=LATEST_SESSION)
    assert source == "IfindDailyFetcher" and len(manager.calls) == 1


def test_enabled_scope_bypasses_db_authority_even_when_complete(monkeypatch, offline):
    enabled_config(monkeypatch)
    db = _FakeDb(_db_rows("600519", 60, ["EfinanceFetcher"]))
    manager = _install(monkeypatch, db, _FakeManager([std_frame(rows=40)]))
    df, source = H.load_history_df("600519", days=30, target_date=LATEST_SESSION)
    assert source == "IfindDailyFetcher"
    assert df is not None
    assert len(manager.calls) == 1  # DB 完整也不再作为命中依据
    assert manager.calls[0]["end_date"] == LATEST_SESSION.isoformat()


def test_enabled_mode_ineligible_stock_keeps_db_cache(monkeypatch, offline):
    enabled_config(monkeypatch)
    db = _FakeDb(_db_rows("510300", 40, ["EfinanceFetcher", "AkshareFetcher"]))
    manager = _install(monkeypatch, db, _FakeManager([std_frame()]))
    _df, source = H.load_history_df("510300", days=30, target_date=LATEST_SESSION)
    assert source == "db_cache"
    assert manager.calls == []


def test_disabled_mode_fresh_window_then_legacy_sources_saved(monkeypatch, offline):
    """关闭模式下无 DB 数据：原链网络获取语义保持（含 end_date 透传）。"""
    enabled_config(monkeypatch, enabled=False)
    db = _FakeDb([])
    manager = _install(monkeypatch, db, _FakeManager([std_frame()]))
    _df, source = H.load_history_df("600519", days=30, target_date=date(2026, 11, 5))
    assert source == "IfindDailyFetcher" or source == "EfinanceFetcher"
    assert manager.calls[0]["end_date"] == "2026-11-05"


# ---------------------------------------------------------------------------
# 作用域：共享、副本、失败复用、同源扩窗、换源降级
# ---------------------------------------------------------------------------
def _open_scope(stock="600519", target=LATEST_SESSION):
    return H.open_current_stock_scope(stock, target_date=target)


def test_scope_reuses_complete_window_and_returns_copies(monkeypatch, offline):
    enabled_config(monkeypatch)
    db = _FakeDb([])
    manager = _install(monkeypatch, db, _FakeManager([std_frame(rows=40)]))
    token, _scope = _open_scope()
    try:
        df1, source1 = H.load_history_df("600519", days=30)
        df1.iloc[0, df1.columns.get_loc("close")] = 999.0  # 消费者修改不得影响共享帧
        df2, source2 = H.load_history_df("600519", days=30)
        assert source1 == source2 == "IfindDailyFetcher"
        assert len(manager.calls) == 1
        assert float(df2.iloc[0]["close"]) != 999.0
    finally:
        H.close_current_stock_scope(token)


def test_scope_same_source_longer_consistent_expansion_replaces(monkeypatch, offline):
    enabled_config(monkeypatch)
    db = _FakeDb([])
    short = std_frame(rows=40)
    long_frame = std_frame(rows=120)
    manager = _install(monkeypatch, db, _FakeManager([short, long_frame]))
    token, _scope = _open_scope()
    try:
        _df1, source1 = H.load_history_df("600519", days=30)
        df2, source2 = H.load_history_df("600519", days=90)
        assert source1 == source2 == "IfindDailyFetcher"
        assert len(df2) == 120 and len(manager.calls) == 2
        # 已替换后，较短需求继续复用
        df3, _source3 = H.load_history_df("600519", days=30)
        assert len(df3) == 120 and len(manager.calls) == 2
    finally:
        H.close_current_stock_scope(token)


def test_scope_consumed_window_not_replaced_by_other_source(monkeypatch, offline):
    enabled_config(monkeypatch)
    db = _FakeDb([])
    ifind_frame = std_frame(rows=40)
    em_frame = std_frame(rows=120)
    manager = _install(monkeypatch, db, _FakeManager([ifind_frame, (em_frame, "EfinanceFetcher")]))
    token, _scope = _open_scope()
    try:
        _df1, source1 = H.load_history_df("600519", days=30)  # 消费 iFinD 窗口
        df2, source2 = H.load_history_df("600519", days=90)   # iFinD 失败→东财更长
        assert (source1, source2) == ("IfindDailyFetcher", "IfindDailyFetcher")
        assert len(df2) == 40  # 换源扩窗降级：保留已采用窗口，不把旧趋势重标新来源
        assert len(manager.calls) == 2
    finally:
        H.close_current_stock_scope(token)


def test_scope_late_shorter_same_source_never_overwrites(monkeypatch, offline):
    enabled_config(monkeypatch)
    db = _FakeDb([])
    long_frame = std_frame(rows=120)
    short = std_frame(rows=40)
    manager = _install(monkeypatch, db, _FakeManager([long_frame, short]))
    token, _scope = _open_scope()
    try:
        _df1, _s1 = H.load_history_df("600519", days=90)
        df2, _s2 = H.load_history_df("600519", days=90)
        assert len(df2) == 120
    finally:
        H.close_current_stock_scope(token)


def test_scope_overlap_conflict_retains_adopted_frame(monkeypatch, offline):
    enabled_config(monkeypatch)
    db = _FakeDb([])
    first = std_frame(rows=40)
    conflicting = std_frame(rows=120, close=55.5)  # 同源但重叠值冲突（口径漂移）
    manager = _install(monkeypatch, db, _FakeManager([first, conflicting]))
    token, _scope = _open_scope()
    try:
        _df1, _s1 = H.load_history_df("600519", days=30)
        df2, _s2 = H.load_history_df("600519", days=90)
        assert len(df2) == 40
        assert float(df2.iloc[0]["close"]) == pytest.approx(10.0)
    finally:
        H.close_current_stock_scope(token)


def test_scope_failure_memo_prevents_repeat_fetch(monkeypatch, offline):
    enabled_config(monkeypatch)
    db = _FakeDb([])
    manager = _FakeManager([], error=RuntimeError("所有数据源获取失败"))
    _install(monkeypatch, db, manager)
    token, _scope = _open_scope()
    try:
        df1, s1 = H.load_history_df("600519", days=30)
        df2, s2 = H.load_history_df("600519", days=30)
        assert (df1, s1) == (None, "none") and (df2, s2) == (None, "none")
        assert len(manager.calls) == 1  # 同任务同需求失败只获取一次
    finally:
        H.close_current_stock_scope(token)


def test_scope_concurrent_same_need_fetches_once(monkeypatch, offline):
    enabled_config(monkeypatch)
    db = _FakeDb([])
    manager = _FakeManager([std_frame(rows=40)], delay=0.25)
    _install(monkeypatch, db, manager)
    token, _scope = _open_scope()
    results: list = []

    import contextvars

    def worker():
        results.append(H.load_history_df("600519", days=30))

    try:
        # 与 agent 工具线程一致：经 copy_context 传播共享窗口作用域
        contexts = [contextvars.copy_context() for _ in range(3)]
        threads = [threading.Thread(target=ctx.run, args=(worker,)) for ctx in contexts]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert all(r[0] is not None for r in results)
        assert len(manager.calls) == 1  # 完整获取（MCP+CSV+解析）并发只执行一次
    finally:
        H.close_current_stock_scope(token)


def test_scope_force_refresh_refetches(monkeypatch, offline):
    enabled_config(monkeypatch)
    db = _FakeDb([])
    manager = _install(monkeypatch, db, _FakeManager([std_frame(rows=40), std_frame(rows=40)]))
    token, _scope = _open_scope()
    try:
        H.load_history_df("600519", days=30)
        H.load_history_df("600519", days=30, force_refresh=True)
        assert len(manager.calls) == 2
    finally:
        H.close_current_stock_scope(token)


# ---------------------------------------------------------------------------
# 深研 run 槽位
# ---------------------------------------------------------------------------
def test_run_slot_reuse_across_loads(monkeypatch, offline):
    from src.core.research_run import ResearchRun, current_research

    enabled_config(monkeypatch)
    db = _FakeDb([])
    manager = _install(monkeypatch, db, _FakeManager([std_frame(rows=120)]))
    run = ResearchRun(timeout_seconds=30)
    research_token = current_research.set(run)
    try:
        _df1, source1 = H.load_history_df("600519", days=60)
        _df2, source2 = H.load_history_df("600519", days=60)
        assert source1 == source2 == "IfindDailyFetcher"
        assert len(manager.calls) == 1
        slot = run.published_daily_history()
        assert slot is not None
        assert slot["source"] == "IfindDailyFetcher"
    finally:
        current_research.reset(research_token)


def test_run_slot_consumed_not_replaced_by_other_source(monkeypatch, offline):
    from src.core.research_run import ResearchRun, current_research

    enabled_config(monkeypatch)
    db = _FakeDb([])
    manager = _install(
        monkeypatch, db,
        _FakeManager([std_frame(rows=60), (std_frame(rows=120), "EfinanceFetcher")]),
    )
    run = ResearchRun(timeout_seconds=30)
    research_token = current_research.set(run)
    try:
        _df1, source1 = H.load_history_df("600519", days=60)
        df2, source2 = H.load_history_df("600519", days=90)
        assert source1 == source2 == "IfindDailyFetcher"
        assert len(df2) == 60  # 保留已消费深研窗口
        slot = run.published_daily_history()
        assert slot is not None and slot["source"] == "IfindDailyFetcher"
    finally:
        current_research.reset(research_token)


def test_run_slot_same_source_longer_consistent_replaces(monkeypatch, offline):
    from src.core.research_run import ResearchRun, current_research

    enabled_config(monkeypatch)
    db = _FakeDb([])
    manager = _install(monkeypatch, db, _FakeManager([std_frame(rows=60), std_frame(rows=120)]))
    run = ResearchRun(timeout_seconds=30)
    research_token = current_research.set(run)
    try:
        _df1, _s1 = H.load_history_df("600519", days=60)
        df2, _s2 = H.load_history_df("600519", days=90)
        assert len(df2) == 120
        slot = run.published_daily_history()
        assert slot is not None and len(slot["records"]) == 120
    finally:
        current_research.reset(research_token)


# ---------------------------------------------------------------------------
# 普通 pipeline：fetch/resume、趋势、今日昨日上下文、Agent 历史
# ---------------------------------------------------------------------------
def _pipeline(monkeypatch, db, manager, *, enabled=True):
    from src.core.pipeline import StockAnalysisPipeline

    enabled_config(monkeypatch, enabled=enabled)
    _install(monkeypatch, db, manager)
    pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
    pipeline.db = db
    pipeline.fetcher_manager = manager
    pipeline.config = SimpleNamespace(
        enable_realtime_quote=False, report_language="zh",
        daily_market_context_enabled=False, market_review_enabled=False,
    )
    return pipeline


def test_fetch_and_save_skips_resume_shortcut_in_enabled_scope(monkeypatch, offline):
    db = _FakeDb(_db_rows("600519", 40, ["EfinanceFetcher"]))
    manager = _FakeManager([std_frame(rows=40)])
    pipeline = _pipeline(monkeypatch, db, manager, enabled=True)
    token, _scope = _open_scope()
    try:
        ok, error = pipeline.fetch_and_save_stock_data("600519", current_time=None)
        assert ok is True and error is None
        assert len(manager.calls) == 1  # DB 已有当日数据仍完整获取
        db.save_daily_data.assert_called_once()
    finally:
        H.close_current_stock_scope(token)


def test_fetch_and_save_keeps_resume_shortcut_when_disabled(monkeypatch, offline):
    db = _FakeDb(_db_rows("600519", 40, ["EfinanceFetcher"]))
    manager = _FakeManager([std_frame(rows=40)])
    pipeline = _pipeline(monkeypatch, db, manager, enabled=False)
    ok, _error = pipeline.fetch_and_save_stock_data("600519", current_time=None)
    assert ok is True
    assert manager.calls == []  # 断点续传捷径保持
    db.save_daily_data.assert_not_called()


def test_trend_reads_shared_window_not_db(monkeypatch, offline):
    db = _FakeDb(_db_rows("600519", 10, ["EfinanceFetcher"]))
    manager = _FakeManager([std_frame(rows=120)])
    pipeline = _pipeline(monkeypatch, db, manager, enabled=True)
    analyzed: dict = {}

    class _Analyzer:
        def analyze(self, df, code):
            analyzed["rows"] = len(df)
            return SimpleNamespace(
                trend_status=SimpleNamespace(value="上升"), buy_signal=SimpleNamespace(value="买入"),
                signal_score=80,
            )

    pipeline.trend_analyzer = _Analyzer()
    token, _scope = _open_scope()
    try:
        H.load_history_df("600519", days=90)  # 任务先取得共享窗口
        trend = pipeline._compute_trend("600519", "贵州茅台", realtime_quote=None)
        assert trend is not None
        assert analyzed["rows"] >= 60
        db.get_data_range.assert_not_called()  # 不再「新取 30 日 + 旧 DB 行」混算
    finally:
        H.close_current_stock_scope(token)


def test_trend_enabled_scope_absent_never_reads_db(monkeypatch, offline):
    """修正组1：启用+合格证券共享窗口缺失（获取失败）不得以旧 DB 行复活趋势。"""
    db = _FakeDb(_db_rows("600519", 40, ["EfinanceFetcher"]))
    manager = _FakeManager([std_frame(rows=40)])
    pipeline = _pipeline(monkeypatch, db, manager, enabled=True)
    pipeline.trend_analyzer = MagicMock()
    token = H.set_frozen_target_date(LATEST_SESSION)
    try:
        trend = pipeline._compute_trend("600519", "贵州茅台", realtime_quote=None)
        assert trend is None
        db.get_data_range.assert_not_called()
        pipeline.trend_analyzer.analyze.assert_not_called()
    finally:
        H.reset_frozen_target_date(token)


def test_trend_disabled_reads_db_with_contamination_guard(monkeypatch, offline):
    """修正组1：未启用时 DB 回退保留，但含遗留 iFinD 行的窗口整窗原链重取。"""
    db = _FakeDb(_db_rows("600519", 40, ["EfinanceFetcher", "IfindDailyFetcher"]))
    manager = _FakeManager([std_frame(rows=120)])
    pipeline = _pipeline(monkeypatch, db, manager, enabled=False)
    analyzed: dict = {}

    class _Analyzer:
        def analyze(self, df, code):
            analyzed["rows"] = len(df)
            return SimpleNamespace(
                trend_status=SimpleNamespace(value="震荡"), buy_signal=SimpleNamespace(value="观望"),
                signal_score=50,
            )

    pipeline.trend_analyzer = _Analyzer()
    token = H.set_frozen_target_date(LATEST_SESSION)
    try:
        trend = pipeline._compute_trend("600519", "贵州茅台", realtime_quote=None)
        assert trend is not None
        assert analyzed["rows"] == 120  # 拒绝污染 DB 窗口后经共享入口整窗重取
        assert len(manager.calls) == 1
    finally:
        H.reset_frozen_target_date(token)


def test_trend_disabled_pure_legacy_db_keeps_original_path(monkeypatch, offline):
    db = _FakeDb(_db_rows("600519", 40, ["EfinanceFetcher"]))
    manager = _FakeManager([std_frame(rows=40)])
    pipeline = _pipeline(monkeypatch, db, manager, enabled=False)
    analyzed: dict = {}

    class _Analyzer:
        def analyze(self, df, code):
            analyzed["rows"] = len(df)
            return SimpleNamespace(
                trend_status=SimpleNamespace(value="震荡"), buy_signal=SimpleNamespace(value="观望"),
                signal_score=50,
            )

    pipeline.trend_analyzer = _Analyzer()
    token = H.set_frozen_target_date(LATEST_SESSION)
    try:
        trend = pipeline._compute_trend("600519", "贵州茅台", realtime_quote=None)
        assert trend is not None
        assert analyzed["rows"] > 0
        db.get_data_range.assert_called_once()
        assert manager.calls == []  # 纯旧源混存缓存保持既有规则：不出网
    finally:
        H.reset_frozen_target_date(token)


def test_analysis_context_today_yesterday_from_shared_window(monkeypatch, offline):
    db = _FakeDb(_db_rows("600519", 5, ["EfinanceFetcher"]))
    manager = _FakeManager([std_frame(rows=40)])
    pipeline = _pipeline(monkeypatch, db, manager, enabled=True)
    token, _scope = _open_scope()
    try:
        H.load_history_df("600519", days=30)
        window = H.current_window_frame("600519", min_rows=2)
        assert window is not None
        records = [{**r, "date": str(r["date"])[:10]} for r in window[0].to_dict(orient="records")]
        context = H.analysis_context_from_window(
            "600519", records, analyze_ma=db._analyze_ma_status
        )
        assert context is not None
        assert context["today"]["close"] == pytest.approx(10.0)
        assert "yesterday" in context and "price_change_ratio" in context
        db.get_analysis_context.assert_not_called()
    finally:
        H.close_current_stock_scope(token)


def test_agent_history_covered_by_scope_without_refetch(monkeypatch, offline):
    db = _FakeDb([])
    long_frame = std_frame(rows=250)
    manager = _FakeManager([long_frame])
    pipeline = _pipeline(monkeypatch, db, manager, enabled=True)
    token, _scope = _open_scope()
    try:
        pipeline._ensure_agent_history("600519", min_days=240)  # 首次扩窗获取
        assert len(manager.calls) == 1
        pipeline._ensure_agent_history("600519", min_days=240)  # 已覆盖不再取
        assert len(manager.calls) == 1
        db.save_daily_data.assert_called_once()
    finally:
        H.close_current_stock_scope(token)


def test_no_scope_direct_entry_creates_temporary_scope(monkeypatch, offline):
    enabled_config(monkeypatch)
    db = _FakeDb([])
    manager = _install(monkeypatch, db, _FakeManager([std_frame(rows=40)]))
    df, source = H.load_history_df("600519", days=30, target_date=LATEST_SESSION)
    assert df is not None and source == "IfindDailyFetcher"
    assert H._current_stock_window.get() is None  # 临时作用域已清理


def test_scope_research_stop_propagates_not_swallowed(monkeypatch, offline):
    """停止信号穿过新增启用路径：不记失败 memo、不伪装成空窗口。"""
    from src.core.research_run import ResearchRun, ResearchStopped, current_research

    enabled_config(monkeypatch)
    db = _FakeDb([])
    manager = _FakeManager([], error=ResearchStopped("用户已取消本次研究"))
    _install(monkeypatch, db, manager)
    token, scope = _open_scope()
    try:
        with pytest.raises(ResearchStopped):
            H.load_history_df("600519", days=30)
        assert scope.failure_for("600519|2026-11-06|30|") is None  # 停止不算源失败
    finally:
        H.close_current_stock_scope(token)
    # 深研路径同样传播
    run = ResearchRun(timeout_seconds=30)
    research_token = current_research.set(run)
    try:
        with pytest.raises(ResearchStopped):
            H.load_history_df("600519", days=30)
    finally:
        current_research.reset(research_token)


# ---------------------------------------------------------------------------
# 修正组1：污染守卫覆盖全部普通消费入口（revision-2 全域边界）
# ---------------------------------------------------------------------------
def test_disabled_resume_rejects_ifind_target_row(monkeypatch, offline):
    """断点续传：目标日 DB 行含 iFinD 来源时不走捷径，整窗重取。"""
    db = _FakeDb(_db_rows("600519", 40, ["EfinanceFetcher"]))
    # 目标日（最新）行为 iFinD 遗留行
    db.rows[0].data_source = "IfindDailyFetcher"
    manager = _FakeManager([std_frame(rows=40)])
    pipeline = _pipeline(monkeypatch, db, manager, enabled=False)
    ok, error = pipeline.fetch_and_save_stock_data("600519")
    assert ok is True and error is None
    assert len(manager.calls) == 1  # 拒绝捷径 → 原链整窗重取
    db.save_daily_data.assert_called_once()


def test_disabled_resume_pure_legacy_target_row_keeps_shortcut(monkeypatch, offline):
    db = _FakeDb(_db_rows("600519", 40, ["EfinanceFetcher", "AkshareFetcher"]))
    manager = _FakeManager([std_frame(rows=40)])
    pipeline = _pipeline(monkeypatch, db, manager, enabled=False)
    ok, _error = pipeline.fetch_and_save_stock_data("600519")
    assert ok is True
    assert manager.calls == []  # 纯旧源混存：原断点续传规则逐字保留
    db.save_daily_data.assert_not_called()


def test_qualified_context_rejects_ifind_latest_rows(monkeypatch, offline):
    """今日/昨日上下文：DB 最新两天含 iFinD 行 → 整窗原链重取后派生。"""
    enabled_config(monkeypatch, enabled=False)
    db = _FakeDb(_db_rows("600519", 40, ["EfinanceFetcher"]))
    db.rows[0].data_source = "IfindDailyFetcher"  # 最新一行为 iFinD 遗留行

    ctx_db = _FakeDb(db.rows)

    def _ctx(code):
        rows = ctx_db._range(code, LATEST_SESSION - timedelta(days=5), LATEST_SESSION)
        if not rows:
            return None
        return {"code": code, "date": rows[-1].date.isoformat(), "today": rows[-1].to_dict()}

    ctx_db.get_analysis_context = _ctx
    manager = _FakeManager([std_frame(rows=40)])
    _install(monkeypatch, ctx_db, manager)
    context = H.qualified_analysis_context("600519", ctx_db)
    assert context is not None
    assert context["daily_history_source"] == "IfindDailyFetcher"
    assert len(manager.calls) == 1


def test_qualified_context_pure_legacy_keeps_db_semantics(monkeypatch, offline):
    enabled_config(monkeypatch, enabled=False)
    db = _FakeDb(_db_rows("600519", 40, ["EfinanceFetcher", "AkshareFetcher"]))

    ctx_db = _FakeDb(db.rows)

    def _ctx(code):
        rows = ctx_db._range(code, LATEST_SESSION - timedelta(days=5), LATEST_SESSION)
        if not rows:
            return None
        return {"code": code, "date": rows[-1].date.isoformat(), "today": rows[-1].to_dict()}

    ctx_db.get_analysis_context = _ctx
    manager = _FakeManager([std_frame(rows=40)])
    _install(monkeypatch, ctx_db, manager)
    context = H.qualified_analysis_context("600519", ctx_db)
    assert context is not None
    assert "daily_history_source" not in context  # 纯旧源：既有 DB 语义不变
    assert manager.calls == []


def test_qualified_context_enabled_no_window_returns_none(monkeypatch, offline):
    """修正组1：启用+合格证券共享窗口缺失（获取失败）不以旧 DB 行复活。"""
    enabled_config(monkeypatch)
    db = _FakeDb(_db_rows("600519", 40, ["EfinanceFetcher"]))
    manager = _FakeManager([], error=RuntimeError("all sources failed"))
    _install(monkeypatch, db, manager)
    token, _scope = _open_scope()
    try:
        H.load_history_df("600519", days=30)  # 获取失败（memo 已记）
        assert H.qualified_analysis_context("600519", db) is None
    finally:
        H.close_current_stock_scope(token)


def test_agent_history_disabled_rejects_ifind_sufficient_rows(monkeypatch, offline):
    """Agent 预取：DB 行数判足同样过污染边界——含 iFinD 行不视为充足。"""
    db = _FakeDb(_db_rows("600519", 250, ["EfinanceFetcher", "IfindDailyFetcher"]))
    manager = _FakeManager([std_frame(rows=250)])
    pipeline = _pipeline(monkeypatch, db, manager, enabled=False)
    pipeline._ensure_agent_history("600519", min_days=240)
    assert len(manager.calls) == 1  # 整窗重取
    db.save_daily_data.assert_called_once()


def test_agent_history_disabled_pure_legacy_sufficient_skips_fetch(monkeypatch, offline):
    db = _FakeDb(_db_rows("600519", 250, ["EfinanceFetcher", "AkshareFetcher"]))
    manager = _FakeManager([std_frame(rows=250)])
    pipeline = _pipeline(monkeypatch, db, manager, enabled=False)
    pipeline._ensure_agent_history("600519", min_days=240)
    assert manager.calls == []


# ---------------------------------------------------------------------------
# 修正组2：停止信号穿越所有阶段与等待者
# ---------------------------------------------------------------------------
def test_scope_waiter_unblocks_when_run_cancelled_without_deadline(monkeypatch, offline):
    """已取消的 run（无外层 deadline）不得让等待者无限等待。"""
    import contextvars

    from src.core.research_run import ResearchRun, ResearchStopped, current_research

    enabled_config(monkeypatch)
    release = threading.Event()
    started = threading.Event()

    def slow_fetch():
        started.set()
        release.wait(timeout=5)
        return std_frame(rows=40), "IfindDailyFetcher"

    token, scope = _open_scope()
    run = ResearchRun(timeout_seconds=60)
    research_token = current_research.set(run)
    outcome: dict = {}
    try:
        owner_ctx = contextvars.copy_context()

        def owner():
            try:
                scope.fetch_once("need", slow_fetch)
            except BaseException as exc:  # noqa: BLE001
                outcome["owner"] = exc

        owner_thread = threading.Thread(target=owner_ctx.run, args=(owner,))
        owner_thread.start()
        assert started.wait(timeout=2)
        waiter_ctx = contextvars.copy_context()

        def waiter():
            try:
                scope.fetch_once("need", lambda: (_ for _ in ()).throw(AssertionError()))
            except BaseException as exc:  # noqa: BLE001
                outcome["waiter"] = exc

        waiter_thread = threading.Thread(target=waiter_ctx.run, args=(waiter,))
        waiter_thread.start()
        import time as _time

        _time.sleep(0.15)
        run.stop("cancelled")
        waiter_thread.join(timeout=3)
        release.set()
        owner_thread.join(timeout=3)
        assert not waiter_thread.is_alive(), "等待者在 run 取消后仍被阻塞"
        assert isinstance(outcome.get("waiter"), (ResearchStopped, TimeoutError))
    finally:
        release.set()
        current_research.reset(research_token)
        H.close_current_stock_scope(token)


def test_scope_owner_success_after_stop_not_published_or_cached(monkeypatch, offline):
    """owner 在 fetch 内停止后返回数据：不发布、不写成功 future。"""
    from src.core.research_run import ResearchRun, ResearchStopped, current_research

    enabled_config(monkeypatch)
    db = _FakeDb([])
    manager = _FakeManager([std_frame(rows=40)])
    _install(monkeypatch, db, manager)
    original_call = manager.get_daily_data
    run = ResearchRun(timeout_seconds=60)

    def fetch_then_stop(stock_code, start_date=None, end_date=None, days=30):
        result = original_call(stock_code, start_date=start_date, end_date=end_date, days=days)
        run.stop("cancelled")  # fetch 完成前用户取消
        return result

    manager.get_daily_data = fetch_then_stop
    research_token = current_research.set(run)
    token, scope = _open_scope()
    try:
        with pytest.raises(ResearchStopped):
            H.load_history_df("600519", days=30)
        assert scope.peek() is None  # 停止后不发布迟到窗口
        assert scope.failure_for("600519|2026-11-06|30|") is None  # 停止不是源失败
    finally:
        H.close_current_stock_scope(token)
        current_research.reset(research_token)


def test_no_run_expired_deadline_never_publishes(monkeypatch, offline):
    """无 run 的普通路径：外层 deadline 过期必须传出，且迟到结果不发布。"""
    from time import monotonic as _monotonic

    from src.patches import eastmoney_patch

    enabled_config(monkeypatch)
    db = _FakeDb([])
    manager = _FakeManager([std_frame(rows=40)], delay=0.15)
    _install(monkeypatch, db, manager)
    token, scope = _open_scope()
    deadline_token = eastmoney_patch.request_deadline.set(_monotonic() + 0.05)
    try:
        with pytest.raises(TimeoutError):
            H.load_history_df("600519", days=30)
        assert scope.peek() is None  # 过期后不采纳迟到窗口
    finally:
        eastmoney_patch.request_deadline.reset(deadline_token)
        H.close_current_stock_scope(token)


def test_pipeline_fetch_and_save_propagates_research_stopped(monkeypatch, offline):
    """修正组2/3：停止信号穿透 fetch_and_save，不转成 (False, error) 后继续分析。"""
    from src.core.research_run import ResearchStopped

    db = _FakeDb([])
    manager = _FakeManager([], error=ResearchStopped("用户已取消本次研究"))
    pipeline = _pipeline(monkeypatch, db, manager, enabled=True)
    token, _scope = _open_scope()
    try:
        with pytest.raises(ResearchStopped):
            pipeline.fetch_and_save_stock_data("600519")
    finally:
        H.close_current_stock_scope(token)
    # 未启用路径同样传播
    db2 = _FakeDb([])
    db2.has_today_data = MagicMock(return_value=False)
    manager2 = _FakeManager([], error=ResearchStopped("用户已取消本次研究"))
    pipeline2 = _pipeline(monkeypatch, db2, manager2, enabled=False)
    with pytest.raises(ResearchStopped):
        pipeline2.fetch_and_save_stock_data("600519")


# ---------------------------------------------------------------------------
# 修正组6：真实来源贯通普通输出（记录/上下文/报告上下文包）
# ---------------------------------------------------------------------------
def test_window_records_carry_data_source_and_context_provenance(monkeypatch, offline):
    """共享窗口记录携带 data_source；派生上下文携带来源/口径/实际截止日。"""
    enabled_config(monkeypatch)
    db = _FakeDb([])
    manager = _FakeManager([std_frame(rows=40)])
    _install(monkeypatch, db, manager)
    token, _scope = _open_scope()
    try:
        df, source = H.load_history_df("600519", days=30)
        assert source == "IfindDailyFetcher"
        assert set(df["data_source"]) == {"IfindDailyFetcher"}  # 记录级来源
        context = H.analysis_context_from_window(
            "600519", df.to_dict(orient="records"),
            analyze_ma=db._analyze_ma_status, source=source,
        )
        assert context["daily_history_source"] == "IfindDailyFetcher"
        assert context["daily_price_mode"] == "前复权(现金分红)"
        assert context["daily_history_as_of"] == "2026-11-06"
        assert context["today"]["data_source"] == "IfindDailyFetcher"
    finally:
        H.close_current_stock_scope(token)


def test_window_provenance_honest_for_fallback_source(monkeypatch, offline):
    """回退来源（东财）不带 cash-qfq 口径标签：不冒充 iFinD 现金分红窗口。"""
    enabled_config(monkeypatch)
    db = _FakeDb([])
    manager = _FakeManager([(std_frame(rows=40), "EfinanceFetcher")])
    _install(monkeypatch, db, manager)
    token, _scope = _open_scope()
    try:
        df, source = H.load_history_df("600519", days=30)
        assert source == "EfinanceFetcher"
        context = H.analysis_context_from_window(
            "600519", df.to_dict(orient="records"), source=source,
        )
        assert context["daily_history_source"] == "EfinanceFetcher"
        assert "daily_price_mode" not in context
    finally:
        H.close_current_stock_scope(token)


def test_context_builder_labels_actual_daily_source():
    """报告上下文包按真实日线来源标注，不再一律 storage.get_analysis_context。"""
    from src.services.analysis_context_builder import _build_daily_bars_block

    artifacts = SimpleNamespace(
        base_context={
            "date": "2026-11-06",
            "today": {"close": 10.0},
            "yesterday": {"close": 9.9},
            "daily_history_source": "IfindDailyFetcher",
            "daily_price_mode": "前复权(现金分红)",
            "daily_history_as_of": "2026-11-06",
        }
    )
    block = _build_daily_bars_block(artifacts)
    assert block.source == "IfindDailyFetcher"
    assert block.metadata.get("daily_price_mode") == "前复权(现金分红)"
    assert all(item.source == "IfindDailyFetcher" for item in block.items.values())

    legacy = _build_daily_bars_block(SimpleNamespace(base_context={"date": "2026-11-06", "today": {"close": 1}}))
    assert legacy.source == "storage.get_analysis_context"  # 无来源标注保持既有标签


def test_cancelled_run_does_not_reuse_cached_window(monkeypatch, offline):
    from src.core.research_run import ResearchRun, ResearchStopped, current_research

    enabled_config(monkeypatch)
    db = _FakeDb([])
    manager = _FakeManager([std_frame(rows=100)])
    _install(monkeypatch, db, manager)
    token, _scope = _open_scope()
    run = ResearchRun(timeout_seconds=60)
    research_token = current_research.set(run)
    try:
        H.load_history_df("600519", days=90)
        run.stop("cancelled")
        with pytest.raises(ResearchStopped):
            H.load_history_df("600519", days=30)
        assert len(manager.calls) == 1
    finally:
        current_research.reset(research_token)
        H.close_current_stock_scope(token)


def test_expired_deadline_does_not_save_cached_window(monkeypatch, offline):
    from time import monotonic

    from src.patches import eastmoney_patch

    enabled_config(monkeypatch)
    db = _FakeDb([])
    manager = _FakeManager([std_frame(rows=100)])
    pipeline = _pipeline(monkeypatch, db, manager, enabled=True)
    token, scope = _open_scope()
    try:
        scope.adopt_and_serve(std_frame(rows=100).to_dict(orient="records"), "IfindDailyFetcher")
        deadline_token = eastmoney_patch.request_deadline.set(monotonic() - 1)
        try:
            with pytest.raises(TimeoutError):
                pipeline.fetch_and_save_stock_data("600519")
        finally:
            eastmoney_patch.request_deadline.reset(deadline_token)
        db.save_daily_data.assert_not_called()
    finally:
        H.close_current_stock_scope(token)


def test_concurrent_different_source_keeps_adopted_frame(monkeypatch, offline):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from src.core.research_run import ResearchRun, current_research

    enabled_config(monkeypatch)
    run = ResearchRun(timeout_seconds=60)
    barrier = threading.Barrier(2)
    small_returned = threading.Event()

    def worker(days, source):
        token = current_research.set(run)
        try:
            if source == "EfinanceFetcher":
                barrier.wait(timeout=3)
                assert small_returned.wait(3)
            out = H._enabled_network_window(
                "600519", days=days, required=LATEST_SESSION, scope=None,
                force_refresh=False, fetch=lambda: (std_frame(rows=days), source),
            )
            if days == 40:
                small_returned.set()
                barrier.wait(timeout=3)
            return len(out[0]), out[1]
        finally:
            current_research.reset(token)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(worker, 40, "IfindDailyFetcher")
        second = pool.submit(worker, 120, "EfinanceFetcher")
        assert first.result() == (40, "IfindDailyFetcher")
        assert second.result() == (40, "IfindDailyFetcher")
    assert run.daily_history["source"] == "IfindDailyFetcher"


def test_scope_conflicting_extension_returns_one_adopted_frame():
    """无 run：冲突扩窗在同一把锁里采纳，两个消费者拿到同一帧。"""
    from concurrent.futures import ThreadPoolExecutor

    scope = H._StockWindowScope("600519", LATEST_SESSION.isoformat(), "")
    barrier = threading.Barrier(2)

    def adopt(rows: int, source: str):
        barrier.wait(timeout=3)
        frame = std_frame(rows=rows).to_dict(orient="records")
        served, served_source = scope.adopt_and_serve(frame, source)
        return len(served), served_source

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(adopt, 40, "IfindDailyFetcher")
        second = pool.submit(adopt, 120, "EfinanceFetcher")
        assert first.result() == second.result()
    assert scope.peek() is not None
    assert scope.peek().source == first.result()[1]


def test_active_run_is_authoritative_over_scope(monkeypatch, offline):
    """run 槽已消费 40 行 iFinD 时，作用域扩窗不得返回另一来源。"""
    from src.core.research_run import ResearchRun, current_research

    enabled_config(monkeypatch)
    run = ResearchRun(timeout_seconds=60)
    research_token = current_research.set(run)
    token, scope = _open_scope()
    try:
        adopted = run.adopt_daily_history(
            LATEST_SESSION.isoformat(),
            std_frame(rows=40).to_dict(orient="records"),
            "IfindDailyFetcher",
        )
        assert adopted is not None
        scope.adopt_and_serve(
            std_frame(rows=120).to_dict(orient="records"), "EfinanceFetcher",
        )
        df, source = H.load_history_df("600519", days=30)
        assert source == "IfindDailyFetcher"
        assert len(df) == 40
        slot = run.published_daily_history()
        assert slot is not None
        assert slot["source"] == "IfindDailyFetcher"
        assert len(slot["records"]) == 40
    finally:
        current_research.reset(research_token)
        H.close_current_stock_scope(token)


def test_expired_deadline_context_and_trend_propagate(monkeypatch, offline):
    from time import monotonic

    from src.patches import eastmoney_patch

    enabled_config(monkeypatch)
    db = _FakeDb([])
    manager = _FakeManager([std_frame(rows=100)])
    pipeline = _pipeline(monkeypatch, db, manager, enabled=True)
    token, scope = _open_scope()
    scope.adopt_and_serve(std_frame(rows=100).to_dict(orient="records"), "IfindDailyFetcher")
    deadline = eastmoney_patch.request_deadline.set(monotonic() - 1)
    try:
        with pytest.raises(TimeoutError):
            H.qualified_analysis_context("600519", db)
        with pytest.raises(TimeoutError):
            pipeline._compute_trend("600519", "茅台", None)
    finally:
        eastmoney_patch.request_deadline.reset(deadline)
        H.close_current_stock_scope(token)


def test_tool_cancel_after_real_load_does_not_persist(monkeypatch, offline):
    from src.agent.tools import data_tools
    from src.core.research_run import ResearchRun, ResearchStopped, current_research

    enabled_config(monkeypatch)
    db = _FakeDb([])
    manager = _FakeManager([std_frame(rows=40)])
    _install(monkeypatch, db, manager)
    monkeypatch.setattr(data_tools, "_get_db", lambda: db)
    token, _scope = _open_scope()
    run = ResearchRun(timeout_seconds=60)
    research_token = current_research.set(run)
    original = H.load_history_df

    def cancel_after(*args, **kwargs):
        result = original(*args, **kwargs)
        run.stop("cancelled")
        return result

    monkeypatch.setattr(H, "load_history_df", cancel_after)
    try:
        with pytest.raises(ResearchStopped):
            data_tools._handle_get_daily_history("600519", days=30)
        db.save_daily_data.assert_not_called()
    finally:
        current_research.reset(research_token)
        H.close_current_stock_scope(token)


def test_contended_adoption_does_not_publish_after_deadline(monkeypatch, offline):
    import time

    from src.patches import eastmoney_patch

    enabled_config(monkeypatch)
    token, scope = _open_scope()
    entered = threading.Event()

    def fetch_with_contended_adoption(*_args, **_kwargs):
        def hold():
            with scope._lock:
                entered.set()
                time.sleep(0.35)

        thread = threading.Thread(target=hold)
        thread.start()
        assert entered.wait(2)
        thread.join()
        return std_frame(rows=5), "IfindDailyFetcher"

    monkeypatch.setattr(
        H, "_get_fetcher_manager",
        lambda: SimpleNamespace(get_daily_data=fetch_with_contended_adoption),
    )
    deadline = eastmoney_patch.request_deadline.set(time.monotonic() + 0.05)
    try:
        with pytest.raises(TimeoutError):
            H.load_history_df("600519", days=1, target_date=LATEST_SESSION)
        assert scope.peek() is None
    finally:
        eastmoney_patch.request_deadline.reset(deadline)
        H.close_current_stock_scope(token)


def test_legacy_local_timeout_returns_none_without_control_budget(monkeypatch, offline):
    from unittest.mock import Mock

    from src.core.research_run import current_research
    from src.patches.eastmoney_patch import request_deadline

    db = _FakeDb(_db_rows("600519", 40, ["EfinanceFetcher"]))
    pipeline = _pipeline(monkeypatch, db, _FakeManager([]), enabled=False)
    pipeline.trend_analyzer = SimpleNamespace(analyze=Mock(side_effect=TimeoutError("local analysis timeout")))
    assert current_research.get() is None
    assert request_deadline.get() is None
    assert pipeline._compute_trend("600519", "sample", None) is None


def test_run_slot_lock_cancel_propagates_through_consumers(monkeypatch, offline):
    """真实 run 锁等待期间取消：槽仍返回 None，消费方必须传出停止而不是缺失。"""
    from src.agent.tools import data_tools
    from src.core.research_run import ResearchRun, ResearchStopped, current_research

    enabled_config(monkeypatch)
    run = ResearchRun(timeout_seconds=60)
    run.adopt_daily_history(
        LATEST_SESSION.isoformat(),
        std_frame(rows=40).to_dict(orient="records"),
        "IfindDailyFetcher",
    )
    research_token = current_research.set(run)
    monkeypatch.setattr(data_tools, "_get_db", lambda: _FakeDb([]))

    def blocked(call):
        entered = threading.Event()

        def hold():
            with run._lock:
                entered.set()
                threading.Event().wait(0.2)
                run.stop("cancelled")

        worker = threading.Thread(target=hold)
        worker.start()
        assert entered.wait(2)
        try:
            with pytest.raises(ResearchStopped):
                call()
        finally:
            worker.join()
            run.stop_reason = None

    try:
        blocked(lambda: H.current_window_frame("600519", min_rows=2))
        blocked(lambda: H.qualified_analysis_context("600519", _FakeDb([])))
        blocked(lambda: data_tools._handle_get_analysis_context("600519"))
    finally:
        current_research.reset(research_token)


def test_run_slot_lock_valid_release_returns_authoritative_frame(monkeypatch, offline):
    from src.core.research_run import ResearchRun, current_research

    enabled_config(monkeypatch)
    run = ResearchRun(timeout_seconds=60)
    run.adopt_daily_history(
        LATEST_SESSION.isoformat(),
        std_frame(rows=40).to_dict(orient="records"),
        "IfindDailyFetcher",
    )
    research_token = current_research.set(run)
    entered = threading.Event()

    def hold():
        with run._lock:
            entered.set()
            threading.Event().wait(0.2)

    worker = threading.Thread(target=hold)
    worker.start()
    assert entered.wait(2)
    try:
        frame, source = H.current_window_frame("600519", min_rows=2)
        assert len(frame) == 40
        assert source == "IfindDailyFetcher"
    finally:
        worker.join()
        current_research.reset(research_token)


def test_run_slot_lock_expired_deadline_propagates(monkeypatch, offline):
    import time

    from src.core.research_run import ResearchRun, current_research
    from src.patches import eastmoney_patch

    enabled_config(monkeypatch)
    run = ResearchRun(timeout_seconds=60)
    run.adopt_daily_history(
        LATEST_SESSION.isoformat(),
        std_frame(rows=40).to_dict(orient="records"),
        "IfindDailyFetcher",
    )
    research_token = current_research.set(run)
    entered = threading.Event()
    deadline = eastmoney_patch.request_deadline.set(time.monotonic() + 0.05)

    def hold():
        with run._lock:
            entered.set()
            threading.Event().wait(0.3)

    worker = threading.Thread(target=hold)
    worker.start()
    assert entered.wait(2)
    try:
        with pytest.raises(TimeoutError):
            H.current_window_frame("600519", min_rows=2)
    finally:
        worker.join()
        eastmoney_patch.request_deadline.reset(deadline)
        current_research.reset(research_token)
