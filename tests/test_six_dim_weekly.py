# -*- coding: utf-8 -*-
"""周线口径与技术研究共享事实契约测试（issue #42）。

周收盘按交易日期 W-FRI 归并（每周最后有效收盘），不再对 5 日收盘求和；
共享快照已有行情优先复用，不足时按需有界补取，失败保留缺口。
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

import pandas as pd

from src.deep_research_dims import six_dim
from src.deep_research_dims.context import SharedContext


def _daily_history(days: int, price: float = 100.0, start: str = "2021-01-04", vary: bool = True) -> list[dict]:
    """按工作日生成日线；vary=True 时周内收盘递增（周五 = price+4），恒价用 vary=False。"""
    d = date.fromisoformat(start)
    out: list[dict] = []
    while len(out) < days:
        if d.weekday() < 5:
            close = price + d.weekday() if vary else price
            out.append(
                {"date": d.isoformat(), "open": price, "high": close + 1, "low": price - 1, "close": float(close), "volume": 1000}
            )
        d += timedelta(days=1)
    return out


def _ctx(history: list[dict], price: float = 100.0) -> SharedContext:
    ctx = SharedContext("600519", "fixture", "2026-10-23")
    ctx.quote = {"price": price}
    ctx.history = history
    return ctx


def _no_fetch(*args, **kwargs):
    raise AssertionError("共享快照充足时不应补取历史")


class TestWeeklyCloseBasis:
    def test_constant_price_weekly_close_is_price_not_sum(self):
        """恒价 100：周收盘 = 100，不再出现 5 日求和的 500。"""
        ctx = _ctx(_daily_history(1050, price=100.0, vary=False))
        weekly = six_dim._weekly_close_series(ctx)
        assert weekly is not None and len(weekly) >= 200
        assert (weekly == 100.0).all()

    def test_week_close_takes_friday_close(self):
        """周内收盘递增（周一 100 … 周五 104）→ 周收盘 = 104。"""
        ctx = _ctx(_daily_history(1050))
        weekly = six_dim._weekly_close_series(ctx)
        assert weekly is not None
        assert set(weekly.unique()) == {104.0}

    def test_holiday_short_week_uses_last_available_close(self):
        """一周只有周三、周四（周一二放假）→ 周收盘 = 周四收盘 103。"""
        history = (
            _daily_history(1050)
            + [
                # 2025-12-24 周三、12-25 周四；12-22/23 缺失
                {"date": "2025-12-24", "close": 102.0, "open": 100, "high": 103, "low": 99, "volume": 1},
                {"date": "2025-12-25", "close": 103.0, "open": 100, "high": 104, "low": 99, "volume": 1},
            ]
        )
        ctx = _ctx(history)
        weekly = six_dim._weekly_close_series(ctx)
        assert weekly is not None
        week_ending = weekly.loc["2025-12-26"] if "2025-12-26" in weekly.index else None
        assert week_ending == 103.0

    def test_unsorted_and_duplicate_and_missing_rows(self):
        base = _daily_history(1050)
        shuffled = list(reversed(base))
        shuffled.append({"date": base[-1]["date"], "close": 999.0, "open": 1, "high": 1, "low": 1, "volume": 1})
        shuffled.append({"date": "2025-06-30", "close": None, "open": 1, "high": 1, "low": 1, "volume": 1})
        ctx = _ctx(shuffled)
        weekly = six_dim._weekly_close_series(ctx)
        assert weekly is not None
        # 同日重复取后者：999 只影响该周，不污染其他周；缺值行被剔除
        assert (weekly == 999.0).sum() <= 1
        assert len(weekly) == len(weekly.dropna())

    def test_incomplete_current_week_included(self):
        """与 compass 服务同口径：当前未完成周也形成样本（该周最后已知收盘）。"""
        history = _daily_history(1050, start="2021-01-04")
        # 最后追加一个孤立周三（其后无数据），该周未闭合
        history.append({"date": "2026-10-21", "close": 120.0, "open": 1, "high": 1, "low": 1, "volume": 1})
        ctx = _ctx(history)
        weekly = six_dim._weekly_close_series(ctx)
        assert weekly is not None
        assert float(weekly.iloc[-1]) == 120.0


class TestCompassWeeklyScoring:
    def test_short_history_and_fetch_failure_keeps_gap(self):
        """共享 260 根 + 补取失败：回退共享样本按真实周数记缺口，不编数；日线研究不受影响。"""
        ctx = _ctx(_daily_history(260))
        with patch("src.services.history_loader.load_history_df", return_value=(None, "none")):
            result = six_dim._score_compass_weekly(ctx)
        assert result is not None and result["data_gap"] is True
        assert "周线样本不足" in result["summary"]

    def test_fetch_exception_falls_back_to_shared_snapshot(self):
        """补取抛异常时回退共享快照样本（已达门槛时仍可参与），不丢弃已有数据。"""
        ctx = _ctx(_daily_history(1010))  # 1010 < 1050 触发补取；回退后 202 周仍可参与
        with patch("src.services.history_loader.load_history_df", side_effect=RuntimeError("db down")):
            weekly = six_dim._weekly_close_series(ctx)
        assert weekly is not None and len(weekly) >= 200

    def test_short_cache_miss_triggers_bounded_fetch(self):
        """DB 只有 260 条时，min_records 门槛应使短缓存不命中（详见 test_history_loader）。"""
        fetched: dict = {}

        def fake_fetch(code, days=60, min_records=None, **kwargs):
            fetched["days"] = days
            fetched["min_records"] = min_records
            return pd.DataFrame(_daily_history(1050)), "db_cache"

        ctx = _ctx(_daily_history(260))
        with patch("src.services.history_loader.load_history_df", side_effect=fake_fetch):
            result = six_dim._score_compass_weekly(ctx)
        assert fetched["min_records"] == 1000
        assert result is not None and result["data_gap"] is False

    def test_long_history_participates_in_weekly_filter(self):
        ctx = _ctx(_daily_history(1100))
        result = six_dim._score_compass_weekly(ctx)
        assert result is not None
        assert result["data_gap"] is False

    def test_insufficient_listing_keeps_gap_without_filling(self):
        """上市不足 200 周：补取只拿到 120 根 → 缺口保留，不放宽门槛。"""
        ctx = _ctx(_daily_history(100))
        with patch("src.services.history_loader.load_history_df", return_value=(pd.DataFrame(_daily_history(120)), "akshare")):
            result = six_dim._score_compass_weekly(ctx)
        assert result is not None and result["data_gap"] is True


class TestSharedContextReuse:
    def test_shared_history_over_1050_bars_no_fetch(self):
        ctx = _ctx(_daily_history(1100))
        with patch("src.services.history_loader.load_history_df", side_effect=_no_fetch):
            weekly = six_dim._weekly_close_series(ctx)
        assert weekly is not None and len(weekly) >= 200
