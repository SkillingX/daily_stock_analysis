# -*- coding: utf-8 -*-
"""板块近期趋势与同业支柱（issue #82）单元级测试。

供应商边界（akshare 东财行业日线 / 沪深300 指数日线）按真实返回形状离线 mock；
严格交易日历用真实 exchange-calendars + 固定 current_time 控制盘中/盘后/周末；
区间收益（Decimal、19 间隔）、日期对齐、同业选取与目标位置等比较逻辑**不 mock**。
资格（confirmed 前置）在编排层测试（test_sector_report_api_v2.py 公开 API 主缝）。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

import pandas as pd
import pytest

from src.core.trading_calendar import recent_completed_trading_sessions
from src.services.sector_analysis_service import (
    BudgetExhausted,
    ReportBudget,
    _bound_gaps,
    _peers_pillar,
    _period_return_pct,
    _qualify_daily_closes,
    _sector_trend_pillar,
)


def _budget(seconds: float = 30.0) -> ReportBudget:
    return ReportBudget(seconds, finish_margin=min(5.0, seconds / 2.0))


# ---------------------------------------------------------------------------
# 观测时刻：2026-11-05（周四）为基准日；盘后 15:05、盘中 10:00、周末 11-07。
# 期望窗口一律从真实严格日历取（真实日历数据是本票被测依赖的一部分）。
# ---------------------------------------------------------------------------

_POST_CLOSE = datetime(2026, 11, 5, 15, 5, 0)
_INTRADAY = datetime(2026, 11, 5, 10, 0, 0)
_WEEKEND = datetime(2026, 11, 7, 12, 0, 0)


def _expected_sessions(current_time: datetime, count: int = 20) -> List[Any]:
    sessions = recent_completed_trading_sessions("cn", count, current_time=current_time)
    assert sessions is not None, "真实 XSHG 日历应可用（离线数据）"
    return sorted(sessions)


def _closes_df(expected: List[Any], base: float = 100.0, step: float = 1.0,
               date_col: str = "日期", close_col: str = "收盘",
               overrides: Optional[Dict[Any, Any]] = None) -> pd.DataFrame:
    """按期望日期构造升序日线（str 日期 + float 收盘，与东财真实形状一致）。"""
    rows = []
    val = base
    for i, d in enumerate(expected):
        close = val
        if overrides and d in overrides:
            close = overrides[d]
        rows.append({date_col: d.strftime("%Y-%m-%d"), close_col: close})
        val += step
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 区间收益（Decimal，19 间隔复合）
# ---------------------------------------------------------------------------


class TestPeriodReturn:
    def test_nineteen_intervals_compound_exactly(self):
        """19 个 +1% 间隔复合 = (1.01^19 − 1)×100，Decimal 精确（非 float 累积）。"""
        closes = [Decimal("100.0")] + [Decimal("101.0")] * 19
        # 末收盘 101 / 首收盘 100：精确 1.0%
        assert _period_return_pct(closes) == Decimal("1.0")

    def test_decimal_precision_not_float_accumulation(self):
        closes = [Decimal("3333.02"), Decimal("3334.89"), Decimal("3336.5")]
        # 手算：(3336.5/3333.02 − 1)×100 = 0.104401...
        got = _period_return_pct(closes)
        assert Decimal("0.1044") <= got <= Decimal("0.1045")

    def test_requires_two_positive_finite(self):
        from icontract import ViolationError

        with pytest.raises(ViolationError):
            _period_return_pct([Decimal("100")])
        with pytest.raises(ViolationError):
            _period_return_pct([Decimal("100"), Decimal("0")])
        with pytest.raises(ViolationError):
            _period_return_pct([Decimal("100"), Decimal("-5")])


# ---------------------------------------------------------------------------
# 日线合格性（_qualify_daily_closes）：日期解析/唯一/窗口内/正有限收盘
# ---------------------------------------------------------------------------


class TestQualifyDailyCloses:
    def test_normal_20_of_20(self):
        expected = _expected_sessions(_POST_CLOSE)
        df = _closes_df(expected)
        obs, qualified, gaps = _qualify_daily_closes(df, "日期", "收盘", expected, "板块日线")
        assert qualified is True and len(obs) == 20
        assert all(isinstance(c, Decimal) for c in obs.values())
        assert gaps == []

    def test_partial_19_keeps_qualified_observations(self):
        """19/20：不给区间收益，但 19 天实际合格观测保留（无发明值、缺口注明缺失日）。"""
        expected = _expected_sessions(_POST_CLOSE)
        df = _closes_df(expected).iloc[1:]  # 缺最早一天
        obs, qualified, gaps = _qualify_daily_closes(df, "日期", "收盘", expected, "板块日线")
        assert qualified is False
        assert len(obs) == 19
        assert expected[0] not in obs
        assert obs[expected[1]] == Decimal("101.0")
        assert any(expected[0].isoformat() in g for g in gaps), gaps

    def test_extra_old_day_cannot_fill_missing(self):
        """行业缺 1 天但供应商多给 1 个旧日：不得凑成 20（不取交集/窗口外凑数）。"""
        expected = _expected_sessions(_POST_CLOSE)
        df = _closes_df(expected).iloc[1:].copy()  # 缺最早一天
        old = expected[0]
        import datetime as _dt

        extra = _dt.date(old.year - 1, old.month, old.day)  # 一年前的旧日
        extra_row = pd.DataFrame(
            [{"日期": extra.strftime("%Y-%m-%d"), "收盘": 99.0}]
        )
        df = pd.concat([extra_row, df], ignore_index=True)
        obs, qualified, gaps = _qualify_daily_closes(df, "日期", "收盘", expected, "板块日线")
        assert qualified is False and len(obs) == 19
        assert any("凑数" in g for g in gaps), gaps

    def test_duplicate_date_conflict_rejects_source_keeps_other_days(self):
        """重复冲突：冲突日两个值都不取其一（整源不给区间收益），其余合格观测保留展示。"""
        expected = _expected_sessions(_POST_CLOSE)
        df = _closes_df(expected)
        dup = pd.DataFrame(
            [{"日期": expected[5].strftime("%Y-%m-%d"), "收盘": 123.0}]
        )
        df = pd.concat([df, dup], ignore_index=True)
        obs, qualified, gaps = _qualify_daily_closes(df, "日期", "收盘", expected, "板块日线")
        assert qualified is False
        assert expected[5] not in obs  # 冲突日不取其一
        assert len(obs) == 19  # 其余无冲突日期的真实观测保留
        assert any("重复" in g for g in gaps)

    def test_out_of_order_rows_still_align(self):
        """乱序返回：按期望日期对齐（顺序不携带语义），对齐后合格。"""
        expected = _expected_sessions(_POST_CLOSE)
        df = _closes_df(expected).sample(frac=1.0, random_state=7)
        obs, qualified, gaps = _qualify_daily_closes(df, "日期", "收盘", expected, "板块日线")
        assert qualified is True
        # 对齐正确性：最旧期望日 = 基础收盘
        assert obs[expected[0]] == Decimal(str(100.0))

    def test_nan_inf_zero_negative_close_invalidates_that_date(self):
        expected = _expected_sessions(_POST_CLOSE)
        overrides = {expected[3]: float("nan"), expected[7]: float("inf"),
                     expected[11]: 0.0, expected[15]: -12.0}
        df = _closes_df(expected, overrides=overrides)
        obs, qualified, gaps = _qualify_daily_closes(df, "日期", "收盘", expected, "板块日线")
        assert qualified is False and len(obs) == 16
        for d in (expected[3], expected[7], expected[11], expected[15]):
            assert d not in obs
            assert any(d.isoformat() in g for g in gaps), gaps

    def test_unparseable_date_ignored(self):
        """解析失败行被忽略并注明；窗口若仍 20/20 对齐则合格（解析行不在窗口内）。"""
        expected = _expected_sessions(_POST_CLOSE)
        df = _closes_df(expected)
        bad = pd.DataFrame([{"日期": "not-a-date", "收盘": 100.0}])
        df = pd.concat([bad, df], ignore_index=True)
        obs, qualified, gaps = _qualify_daily_closes(df, "日期", "收盘", expected, "板块日线")
        assert qualified is True and len(obs) == 20
        assert any("无法解析" in g for g in gaps)

    def test_empty_and_missing_columns(self):
        expected = _expected_sessions(_POST_CLOSE)
        obs, qualified, gaps = _qualify_daily_closes(None, "日期", "收盘", expected, "板块日线")
        assert qualified is False and obs == {} and gaps
        obs, qualified, gaps = _qualify_daily_closes(
            pd.DataFrame({"其他": [1]}), "日期", "收盘", expected, "板块日线"
        )
        assert qualified is False and any("缺日期" in g for g in gaps)

    def test_wrong_shape_not_dataframe(self):
        """dict 等错误形状按源不合格返回（不抛 AttributeError，错误形状在边界适配）。"""
        expected = _expected_sessions(_POST_CLOSE)
        obs, qualified, gaps = _qualify_daily_closes(
            {"日期": ["2026-10-09"], "收盘": [100.0]}, "日期", "收盘", expected, "板块日线"
        )
        assert qualified is False and obs == {}
        assert any("形状异常" in g for g in gaps)

    def test_duplicate_date_column_shape_is_source_gap_not_exception(self):
        """重复「日期」列（df[col] 为 DataFrame）：源形状缺口降块，不抛 ValueError
        逃出边界（旧版逃逸后被 endpoint 误报为用户 400）。"""
        expected = _expected_sessions(_POST_CLOSE)
        df = _closes_df(expected)
        df = pd.concat([df, df[["日期"]]], axis=1)  # 重复日期列（真实畸形形状）
        obs, qualified, gaps = _qualify_daily_closes(df, "日期", "收盘", expected, "板块日线")
        assert qualified is False and obs == {}
        assert any("形状异常" in g for g in gaps), gaps

    def test_five_duplicate_dates_gap_bounded_and_compact(self):
        """valid20 + 重复前 5 天：缺口单项 ≤200、日期 isoformat 紧凑（不炸 schema）。"""
        expected = _expected_sessions(_POST_CLOSE)
        df = pd.concat([_closes_df(expected), _closes_df(expected)[:5]],
                       ignore_index=True)
        obs, qualified, gaps = _qualify_daily_closes(df, "日期", "收盘", expected, "板块日线")
        assert qualified is False and len(obs) == 15
        dup_gaps = [g for g in gaps if "重复日期冲突" in g]
        assert len(dup_gaps) == 1
        assert len(dup_gaps[0]) <= 200, dup_gaps[0]
        assert "datetime.date(" not in dup_gaps[0]  # 紧凑 isoformat，非全 repr
        assert "5 天重复日期冲突" in dup_gaps[0]
        for d in expected[:5]:
            assert d.isoformat() in dup_gaps[0]

    def test_compound_dup_invalid_missing_independent_gaps(self):
        """复合源缺陷（缺 1 天 + 1 天收盘 0 + 1 天重复）：三类缺口独立可见，
        重复日不短路其他缺口；缺口气泡互不冒充同一类限制。"""
        expected = _expected_sessions(_POST_CLOSE)
        df = _closes_df(expected).iloc[1:].copy()  # 缺最早一天
        df.loc[df.index[0], "收盘"] = 0.0          # 次日收盘无效
        dup = pd.DataFrame(
            [{"日期": expected[3].strftime("%Y-%m-%d"), "收盘": 555.0}]
        )
        df = pd.concat([df, dup], ignore_index=True)
        obs, qualified, gaps = _qualify_daily_closes(df, "日期", "收盘", expected, "板块日线")
        assert qualified is False and len(obs) == 17
        assert any("重复日期冲突" in g and expected[3].isoformat() in g for g in gaps)
        assert any("收盘无效或非正" in g and expected[1].isoformat() in g for g in gaps)
        assert any("缺失 1 天" in g and expected[0].isoformat() in g for g in gaps)
        # 重复缺口不吞掉其他事实：缺失/无效各占独立一条
        assert len([g for g in gaps if "区间收益不计算" in g]) == 2


# ---------------------------------------------------------------------------
# 板块趋势支柱（_sector_trend_pillar）
# ---------------------------------------------------------------------------


class _AkStub:
    """akshare 边界替身：按调用名记录出站次数并返回预置 DataFrame。"""

    def __init__(self, board_df: Optional[pd.DataFrame],
                 bench_df: Optional[pd.DataFrame],
                 board_exc: Optional[Exception] = None,
                 bench_exc: Optional[Exception] = None) -> None:
        self.board_df = board_df
        self.bench_df = bench_df
        self.board_exc = board_exc
        self.bench_exc = bench_exc
        self.board_calls = 0
        self.bench_calls = 0
        self.board_kwargs: Dict[str, Any] = {}
        # 腾讯同指数 fallback 替身（2026-11-07 收敛轮）：默认未配置即断言失败
        # ——fallback 被意外触发时立刻暴露，不产生真实出网
        self.bench_tx_df: Optional[pd.DataFrame] = None
        self.bench_tx_exc: Optional[Exception] = None
        self.bench_tx_calls = 0

    def stock_board_industry_hist_em(self, **kwargs: Any) -> pd.DataFrame:
        self.board_calls += 1
        self.board_kwargs = kwargs
        if self.board_exc is not None:
            raise self.board_exc
        assert self.board_df is not None
        return self.board_df

    def stock_zh_index_daily_em(self, **kwargs: Any) -> pd.DataFrame:
        self.bench_calls += 1
        if self.bench_exc is not None:
            raise self.bench_exc
        assert self.bench_df is not None
        return self.bench_df

    def stock_zh_index_daily_tx(self, **kwargs: Any) -> pd.DataFrame:
        self.bench_tx_calls += 1
        if self.bench_tx_exc is not None:
            raise self.bench_tx_exc
        assert self.bench_tx_df is not None, "腾讯基准替身未配置却被触发（意外 fallback）"
        return self.bench_tx_df


def _patch_ak(monkeypatch: pytest.MonkeyPatch, stub: _AkStub) -> None:
    import akshare as ak

    monkeypatch.setattr(ak, "stock_board_industry_hist_em",
                        stub.stock_board_industry_hist_em)
    monkeypatch.setattr(ak, "stock_zh_index_daily_em",
                        stub.stock_zh_index_daily_em)
    monkeypatch.setattr(ak, "stock_zh_index_daily_tx",
                        stub.stock_zh_index_daily_tx)


class TestSectorTrendPillar:
    def test_normal_post_close_full_window(self, monkeypatch):
        """盘后：20 个已完成交易日全对齐；板块与基准各自区间收益 + 相对差（百分点）。"""
        expected = _expected_sessions(_POST_CLOSE)
        stub = _AkStub(
            board_df=_closes_df(expected, base=1000.0, step=5.0),
            bench_df=_closes_df(expected, base=4000.0, step=4.0,
                                date_col="date", close_col="close"),
        )
        _patch_ak(monkeypatch, stub)
        out = _sector_trend_pillar("BK1277", "白酒Ⅱ", budget=_budget(),
                                   subject_confirmed=True, current_time=_POST_CLOSE)
        assert out["eligible"] is True
        assert out["period_return_pct"] is not None
        assert out["benchmark"] is not None
        assert out["benchmark"]["period_return_pct"] is not None
        # 相对差 = 板块区间收益 − 基准区间收益（Decimal 精度内）
        sector = Decimal(str(out["period_return_pct"]))
        bench = Decimal(str(out["benchmark"]["period_return_pct"]))
        assert abs(Decimal(str(out["relative_return_pp"])) - (sector - bench)) < Decimal("0.0001")
        # 实际日线观测：20+20 进载荷（日期升序、真实收盘、无发明值）
        assert out["qualified_sessions"] == 20
        assert len(out["observations"]) == 20
        assert out["observations"][0] == {"date": expected[0].isoformat(), "close": 1000.0}
        assert [o["date"] for o in out["observations"]] == [d.isoformat() for d in expected]
        assert out["benchmark"]["qualified_sessions"] == 20
        assert len(out["benchmark"]["observations"]) == 20
        assert out["start_date"] == expected[0].isoformat()
        assert out["end_date"] == expected[-1].isoformat()
        assert len(out["expected_dates"]) == 20
        assert out["gaps"] == []
        # 出站窗口参数与期望窗口一致（YYYYMMDD）
        assert stub.board_kwargs["start_date"] == expected[0].strftime("%Y%m%d")
        assert stub.board_kwargs["end_date"] == expected[-1].strftime("%Y%m%d")
        assert stub.board_kwargs["symbol"] == "BK1277"
        assert stub.board_calls == 1 and stub.bench_calls == 1

    def test_intraday_excludes_unclosed_session(self, monkeypatch):
        """盘中：当日未收盘 K 线必须被剔除（期望窗口止于上一已完成交易日）。"""
        expected_intraday = _expected_sessions(_INTRADAY)
        assert expected_intraday[-1] < _INTRADAY.date()  # 窗口不含当日
        stub = _AkStub(
            board_df=_closes_df(expected_intraday, base=1000.0, step=1.0),
            bench_df=_closes_df(expected_intraday, base=4000.0, step=1.0,
                                date_col="date", close_col="close"),
        )
        _patch_ak(monkeypatch, stub)
        out = _sector_trend_pillar("BK1277", "白酒Ⅱ", budget=_budget(),
                                   subject_confirmed=True, current_time=_INTRADAY)
        assert out["period_return_pct"] is not None
        assert out["end_date"] == expected_intraday[-1].isoformat()

    def test_weekend_uses_last_completed_session(self, monkeypatch):
        expected = _expected_sessions(_WEEKEND)
        stub = _AkStub(
            board_df=_closes_df(expected, base=1000.0, step=1.0),
            bench_df=_closes_df(expected, base=4000.0, step=1.0,
                                date_col="date", close_col="close"),
        )
        _patch_ak(monkeypatch, stub)
        out = _sector_trend_pillar("BK1277", "白酒Ⅱ", budget=_budget(),
                                   subject_confirmed=True, current_time=_WEEKEND)
        assert out["gaps"] == []
        assert out["end_date"] == expected[-1].isoformat()

    def test_calendar_unavailable_no_outbound(self, monkeypatch):
        """严格日历不可用：明确缺口，不发任何出站（不自然日 fallback）。"""
        import src.services.sector_analysis_service as svc

        stub = _AkStub(None, None)
        _patch_ak(monkeypatch, stub)
        orig = svc.recent_completed_trading_sessions
        svc.recent_completed_trading_sessions = lambda *a, **k: None  # type: ignore[assignment]
        try:
            out = _sector_trend_pillar("BK1277", "白酒Ⅱ", budget=_budget(),
                                       subject_confirmed=True, current_time=_POST_CLOSE)
        finally:
            svc.recent_completed_trading_sessions = orig  # type: ignore[assignment]
        assert out["period_return_pct"] is None
        assert out["benchmark"] is None
        assert any("交易日历" in g for g in out["gaps"])
        assert stub.board_calls == 0 and stub.bench_calls == 0

    def test_board_missing_day_bench_full_each_keeps_own(self, monkeypatch):
        """板块缺 1 天而基准 20/20：各自保留有资格部分，不算相对比较。

        基准合格事实独立保留（含 20 天实际观测），不因板块残缺而隐藏。
        """
        expected = _expected_sessions(_POST_CLOSE)
        stub = _AkStub(
            board_df=_closes_df(expected, base=1000.0, step=1.0).iloc[1:],
            bench_df=_closes_df(expected, base=4000.0, step=1.0,
                                date_col="date", close_col="close"),
        )
        _patch_ak(monkeypatch, stub)
        out = _sector_trend_pillar("BK1277", "白酒Ⅱ", budget=_budget(),
                                   subject_confirmed=True, current_time=_POST_CLOSE)
        assert out["period_return_pct"] is None  # 板块 19/20 不给区间收益
        assert out["qualified_sessions"] == 19  # 实际覆盖如实
        assert len(out["observations"]) == 19  # 部分合格观测保留展示
        assert any("板块日线" in g for g in out["gaps"])
        assert out["benchmark"] is not None
        assert out["benchmark"]["period_return_pct"] is not None  # 基准 20/20 保留
        assert out["benchmark"]["qualified_sessions"] == 20
        assert len(out["benchmark"]["observations"]) == 20
        assert out["relative_return_pp"] is None  # 不伪相对比较

    def test_benchmark_date_set_differs_no_relative(self, monkeypatch):
        """基准日期集与期望不一致（错日）：基准块不合格，不算相对。"""
        import datetime as _dt

        expected = _expected_sessions(_POST_CLOSE)
        bench_expected = expected[:-1] + [_dt.date(2026, 1, 2)]  # 末位换成旧日期
        stub = _AkStub(
            board_df=_closes_df(expected, base=1000.0, step=1.0),
            bench_df=_closes_df(sorted(bench_expected), base=4000.0, step=1.0,
                                date_col="date", close_col="close"),
        )
        _patch_ak(monkeypatch, stub)
        out = _sector_trend_pillar("BK1277", "白酒Ⅱ", budget=_budget(),
                                   subject_confirmed=True, current_time=_POST_CLOSE)
        assert out["period_return_pct"] is not None  # 板块自身合格
        assert out["benchmark"]["period_return_pct"] is None
        assert out["relative_return_pp"] is None

    def test_both_missing_same_day_no_relative_no_pad(self, monkeypatch):
        """双方缺同一日：各 19/20，都无区间收益，更无相对（不凑 20）。"""
        expected = _expected_sessions(_POST_CLOSE)
        stub = _AkStub(
            board_df=_closes_df(expected, base=1000.0, step=1.0).iloc[1:],
            bench_df=_closes_df(expected, base=4000.0, step=1.0,
                                date_col="date", close_col="close").iloc[1:],
        )
        _patch_ak(monkeypatch, stub)
        out = _sector_trend_pillar("BK1277", "白酒Ⅱ", budget=_budget(),
                                   subject_confirmed=True, current_time=_POST_CLOSE)
        assert out["period_return_pct"] is None
        assert out["benchmark"]["period_return_pct"] is None
        assert out["relative_return_pp"] is None

    def test_board_vendor_failure_is_block_gap(self, monkeypatch):
        """供应商失败只降趋势块（TimeoutError 也不例外），不向上传播。"""
        stub = _AkStub(None, None,
                       board_exc=TimeoutError("vendor timeout"),
                       bench_exc=TimeoutError("vendor timeout"))
        _patch_ak(monkeypatch, stub)
        out = _sector_trend_pillar("BK1277", "白酒Ⅱ", budget=_budget(),
                                   subject_confirmed=True, current_time=_POST_CLOSE)
        assert out["period_return_pct"] is None
        assert any("失败" in g or "超时" in g for g in out["gaps"])

    def test_fetch_budget_exhausted_degrades_block(self, monkeypatch):
        """取数预算耗尽：趋势块降级记缺口，不向上抛（已取得内容保留收尾）。"""
        expected = _expected_sessions(_POST_CLOSE)
        stub = _AkStub(
            board_df=_closes_df(expected, base=1000.0, step=1.0),
            bench_df=_closes_df(expected, base=4000.0, step=1.0,
                                date_col="date", close_col="close"),
        )

        def _exhausted(**kwargs: Any) -> pd.DataFrame:
            raise BudgetExhausted("取数预算已耗尽")

        stub.stock_board_industry_hist_em = _exhausted  # type: ignore[assignment]
        _patch_ak(monkeypatch, stub)
        out = _sector_trend_pillar("BK1277", "白酒Ⅱ", budget=_budget(),
                                   subject_confirmed=True, current_time=_POST_CLOSE)
        assert out["period_return_pct"] is None
        assert any("预算" in g for g in out["gaps"])

    def test_board_ok_bench_budget_exhausted_keeps_board(self, monkeypatch):
        """板块日线取得后基准阶段预算耗尽：板块区间收益保留，相对为 None。"""
        expected = _expected_sessions(_POST_CLOSE)
        stub = _AkStub(
            board_df=_closes_df(expected, base=1000.0, step=1.0),
            bench_df=None,
        )

        def _exhausted(**kwargs: Any) -> pd.DataFrame:
            raise BudgetExhausted("取数预算已耗尽")

        stub.stock_zh_index_daily_em = _exhausted  # type: ignore[assignment]
        stub.stock_zh_index_daily_tx = _exhausted  # type: ignore[assignment]
        _patch_ak(monkeypatch, stub)
        out = _sector_trend_pillar("BK1277", "白酒Ⅱ", budget=_budget(),
                                   subject_confirmed=True, current_time=_POST_CLOSE)
        assert out["period_return_pct"] is not None
        assert out["benchmark"] is None or out["benchmark"]["period_return_pct"] is None
        assert out["relative_return_pp"] is None
        assert any("基准" in g for g in out["gaps"])

    def test_wrong_shape_board_history_is_source_gap_not_crash(self, monkeypatch):
        """供应商日线错误形状（dict）：源错误降块记缺口，不抛 AttributeError。"""
        expected = _expected_sessions(_POST_CLOSE)
        stub = _AkStub(
            board_df={"日期": ["2026-10-09"], "收盘": [1000.0]},  # type: ignore[arg-type]
            bench_df=_closes_df(expected, base=4000.0, step=1.0,
                                date_col="date", close_col="close"),
        )
        _patch_ak(monkeypatch, stub)
        out = _sector_trend_pillar("BK1277", "白酒Ⅱ", budget=_budget(),
                                   subject_confirmed=True, current_time=_POST_CLOSE)
        assert out["period_return_pct"] is None
        assert out["observations"] == []
        assert any("形状异常" in g for g in out["gaps"]), out["gaps"]
        assert out["benchmark"]["period_return_pct"] is not None  # 基准合格独立保留

    def test_wrong_shape_bench_history_is_source_gap_not_crash(self, monkeypatch):
        """东财基准错误形状（dict）：源错误降块记缺口，不抛异常。

        2026-11-07 收敛轮契约：东财源形状异常时切换腾讯同一指数（sh000300）
        ——腾讯合格时基准区间收益照常产出（来源在基准块如实标注），东财源
        形状异常缺口保留（不因 fallback 成功清空）；两源对同一期望窗口均
        20/20 合格，相对差照常计算。
        """
        expected = _expected_sessions(_POST_CLOSE)
        stub = _AkStub(
            board_df=_closes_df(expected, base=1000.0, step=1.0),
            bench_df="garbage",  # type: ignore[arg-type]
        )
        stub.bench_tx_df = _closes_df(expected, base=4000.0, step=1.0,
                                      date_col="date", close_col="close")
        _patch_ak(monkeypatch, stub)
        out = _sector_trend_pillar("BK1277", "白酒Ⅱ", budget=_budget(),
                                   subject_confirmed=True, current_time=_POST_CLOSE)
        assert out["period_return_pct"] is not None  # 板块合格不受基准形状影响
        bench = out["benchmark"]
        assert bench is not None and bench["source"] == "腾讯指数日线"
        assert bench["period_return_pct"] is not None  # 腾讯同一指数合格产出收益
        assert out["relative_return_pp"] is not None  # 同窗口双侧合格 → 相对差可算
        # 东财源错误缺口保留在基准块内（不因 fallback 成功清空）；顶层有切换说明
        assert any("形状异常" in g for g in (bench["gaps"] or [])), bench["gaps"]
        assert any("腾讯" in g for g in out["gaps"]), out["gaps"]


# ---------------------------------------------------------------------------
# 同业支柱（_peers_pillar）：成交额前五 + 目标股去重（同一成分快照）
# ---------------------------------------------------------------------------


def _cons_snapshot(codes_amounts: List[tuple[str, Optional[float]]],
                   changes: Optional[Dict[str, float]] = None,
                   target_code: str = "600519",
                   target_change: float = 1.65) -> pd.DataFrame:
    rows = []
    for code, amount in codes_amounts:
        name = "贵州茅台" if code == target_code else f"股票{code}"
        rows.append({"代码": code, "名称": name, "成交额": amount,
                     "涨跌幅": (changes or {}).get(
                         code, target_change if code == target_code else 0.5)})
    return pd.DataFrame(rows)


class TestPeersPillar:
    def test_target_not_in_top5_sample_is_six(self):
        cons = _cons_snapshot(
            [("600001", 9e8), ("600002", 8e8), ("600003", 7e8),
             ("600004", 6e8), ("600005", 5e8), ("600519", 4e8)]
        )
        out = _peers_pillar("600519", cons, budget=_budget(), subject_confirmed=True,
                            snapshot_observed_at="2026-11-05T14:00:01")
        assert out["eligible"] is True
        assert out["sample_size"] == 6
        assert out["target_in_amount_top5"] is False
        codes = [i["code"] for i in out["items"]]
        assert codes[:5] == ["600001", "600002", "600003", "600004", "600005"]
        assert codes[5] == "600519"  # 目标殿后加入
        assert out["items"][-1]["is_target"] is True

    def test_target_in_top5_no_duplicate(self):
        """目标已在前五：样本恰 5 家且不重复。"""
        cons = _cons_snapshot(
            [("600001", 9e8), ("600002", 8e8), ("600003", 7e8),
             ("600004", 6e8), ("600519", 5e8), ("600006", 4e8)]
        )
        out = _peers_pillar("600519", cons, budget=_budget(), subject_confirmed=True,
                            snapshot_observed_at="2026-11-05T14:00:01")
        assert out["sample_size"] == 5
        assert out["target_in_amount_top5"] is True
        codes = [i["code"] for i in out["items"]]
        assert len(codes) == len(set(codes)) == 5
        assert "600519" in codes and "600006" not in codes

    def test_amount_desc_order_with_code_tiebreak(self):
        """并列成交额按代码升序（稳定可解释）。"""
        cons = _cons_snapshot(
            [("600010", 5e8), ("600002", 5e8), ("600005", 5e8),
             ("600003", 5e8), ("600001", 5e8), ("600519", 1e8)]
        )
        out = _peers_pillar("600519", cons, budget=_budget(), subject_confirmed=True,
                            snapshot_observed_at="2026-11-05T14:00:01")
        codes = [i["code"] for i in out["items"]][:5]
        assert codes == ["600001", "600002", "600003", "600005", "600010"]

    def test_target_change_pct_rank_desc_strict_higher_plus_one(self):
        """目标位置按当日涨跌幅降序：名次 = 严格更高个数 + 1（并列同名次）。"""
        cons = _cons_snapshot(
            [("600001", 9e8), ("600002", 8e8), ("600003", 7e8),
             ("600004", 6e8), ("600005", 5e8), ("600519", 4e8)],
            changes={"600001": 3.0, "600002": 2.0, "600003": 1.65,
                     "600004": 1.65, "600005": 0.1},
            target_change=1.65,
        )
        out = _peers_pillar("600519", cons, budget=_budget(), subject_confirmed=True,
                            snapshot_observed_at="2026-11-05T14:00:01")
        # 严格更高：3.0、2.0 → 名次 3（600003 并列 1.65 同为 3）
        assert out["target_change_pct_rank"] == 3

    def test_invalid_and_zero_amount_excluded_with_gap(self):
        """成交额缺失/负/非有限行不参与排名（未知），合法零额不进活跃前五。

        2026-11-07 final 收敛轮语义：0=已知零观测（不参与活跃排名、缺口单列
        「合法零额」），负/非数值=无效（未知语义、缺口单列「负值/非数值」）——
        不再合并称「零或负无效」。
        """
        cons = _cons_snapshot(
            [("600001", 9e8), ("600002", None), ("600003", 0.0),
             ("600004", float("nan")), ("600005", -5.0), ("600006", 2e8),
             ("600519", 1e8)]
        )
        out = _peers_pillar("600519", cons, budget=_budget(), subject_confirmed=True,
                            snapshot_observed_at="2026-11-05T14:00:01")
        codes = [i["code"] for i in out["items"]]
        assert "600002" not in codes and "600003" not in codes
        assert "600004" not in codes and "600005" not in codes
        assert codes == ["600001", "600006", "600519"]
        assert out["sample_size"] == 3
        assert out["snapshot_rows"] == 7
        assert out["rankable_rows"] == 3
        # 合法零额单列（已知零观测，不参与活跃前五）
        assert any("合法零额" in g and "1 行" in g for g in out["gaps"]), out["gaps"]
        # 无效单列（负值/非数值；缺失与 NaN 归 missing）
        invalid_gaps = [g for g in out["gaps"] if "成交额无效" in g]
        assert invalid_gaps, out["gaps"]
        assert "缺失 2" in invalid_gaps[0] and "负值/非数值 1" in invalid_gaps[0]
        assert "观测 7 行" in invalid_gaps[0] and "可排名 3 行" in invalid_gaps[0]
        assert any("不足 5" in g for g in out["gaps"])

    def test_invalid_amount_gap_even_when_five_valid_remain(self):
        """剔除无效成交额后仍有充足可排名行：限制不被剩余行数吞掉，缺口仍发。"""
        cons = _cons_snapshot(
            [("600001", 9e8), ("600002", 8e8), ("600003", 7e8),
             ("600004", 6e8), ("600005", 5e8), ("600006", 4e8),
             ("600519", float("nan"))]
        )
        out = _peers_pillar("600519", cons, budget=_budget(), subject_confirmed=True,
                            snapshot_observed_at="2026-11-05T14:00:01")
        # 6 家可排名（前5进样本）+ 目标（amount NaN 但成员保留殿后）= 6 家样本
        codes = [i["code"] for i in out["items"]]
        assert len(codes) == 6 and codes[:5] == ["600001", "600002", "600003", "600004", "600005"]
        assert out["sample_size"] == 6
        assert any("成交额无效" in g for g in out["gaps"]), out["gaps"]
        assert out["rankable_rows"] == 6 and out["snapshot_rows"] == 7
        target_item = next(i for i in out["items"] if i["is_target"])
        assert target_item["amount"] is None
        assert out["target_in_amount_top5"] is None

    def test_decimal_sort_of_large_exact_amounts(self):
        """大数成交额用原始值 Decimal 排序：超出 float 尾数精度的差异不失真。"""
        # 9_007_199_254_740_991 与 9_007_199_254_740_992 在 float64 下不可区分
        big_a = "9007199254740991"
        big_b = "9007199254740992"
        cons = pd.DataFrame({
            "代码": ["600001", "600002", "600519"],
            "名称": ["甲", "乙", "贵州茅台"],
            "涨跌幅": [1.0, 2.0, 1.5],
            "成交额": [big_a, big_b, "100"],
        })
        out = _peers_pillar("600519", cons, budget=_budget(), subject_confirmed=True,
                            snapshot_observed_at="2026-11-05T14:00:01")
        codes = [i["code"] for i in out["items"]]
        # 600002（…992）> 600001（…991），float 中转会并列后按代码序颠倒
        assert codes[0] == "600002" and codes[1] == "600001"

    def test_target_amount_missing_retained_member(self):
        """目标股成交额缺失：成员保留（amount=None + 缺口），不误报「非成员」。"""
        cons = _cons_snapshot(
            [("600001", 9e8), ("600002", 8e8), ("600003", 7e8),
             ("600004", 6e8), ("600005", 5e8), ("600006", 4e8),
             ("600519", float("nan"))]
        )
        out = _peers_pillar("600519", cons, budget=_budget(), subject_confirmed=True,
                            snapshot_observed_at="2026-11-05T14:00:01")
        target_items = [i for i in out["items"] if i["is_target"]]
        assert len(target_items) == 1  # 目标成员保留
        assert target_items[0]["amount"] is None
        assert out["target_in_amount_top5"] is None  # 无法判定，不误报 False
        assert any(
            "600519" in g and "不参与成交额排名" in g and "成员保留" in g
            for g in out["gaps"]
        ), out["gaps"]

    def test_peer_change_missing_withholds_rank_with_coverage(self):
        """同行涨跌幅缺失：不发全样本名次（withhold）+ 有效比较范围缺口。"""
        cons = _cons_snapshot(
            [("600001", 9e8), ("600002", 8e8), ("600003", 7e8),
             ("600004", 6e8), ("600005", 5e8), ("600519", 4e8)]
        )
        cons.loc[cons["代码"] != "600519", "涨跌幅"] = float("nan")
        out = _peers_pillar("600519", cons, budget=_budget(), subject_confirmed=True,
                            snapshot_observed_at="2026-11-05T14:00:01")
        assert out["target_change_pct_rank"] is None  # 不伪 1/6
        coverage = [g for g in out["gaps"] if "有效比较范围" in g]
        assert coverage, out["gaps"]
        assert "1/6" in coverage[0]
        # 目标成员与涨跌幅仍如实展示
        target_item = next(i for i in out["items"] if i["is_target"])
        assert target_item["change_pct"] == 1.65

    def test_all_changes_valid_rank_normal(self):
        """全部样本涨跌幅有效：名次照常输出（不受新增守卫影响）。"""
        cons = _cons_snapshot(
            [("600001", 9e8), ("600002", 8e8), ("600003", 7e8),
             ("600004", 6e8), ("600005", 5e8), ("600519", 4e8)],
            changes={"600001": 3.0, "600002": 2.0, "600003": 1.65,
                     "600004": 1.65, "600005": 0.1},
            target_change=1.65,
        )
        out = _peers_pillar("600519", cons, budget=_budget(), subject_confirmed=True,
                            snapshot_observed_at="2026-11-05T14:00:01")
        assert out["target_change_pct_rank"] == 3

    def test_blank_supplier_name_adapts_to_code_display(self):
        """空白供应商名称：边界适配为代码显示 + 缺口，其余有效成员不受影响。"""
        cons = _cons_snapshot(
            [("600001", 9e8), ("600002", 8e8), ("600003", 7e8),
             ("600004", 6e8), ("600005", 5e8), ("600519", 4e8)]
        )
        cons.loc[cons["代码"] == "600001", "名称"] = "  "
        out = _peers_pillar("600519", cons, budget=_budget(), subject_confirmed=True,
                            snapshot_observed_at="2026-11-05T14:00:01")
        assert out["items"][0]["code"] == "600001"
        assert out["items"][0]["name"] == "600001"  # 代码显示
        assert any("名称空白" in g for g in out["gaps"]), out["gaps"]
        # 其余成员名称保留
        assert out["items"][1]["name"] == "股票600002"
        assert out["target_change_pct_rank"] is not None

    def test_unit_and_provenance_fields_present(self):
        """成交额单位与 provenance 随载荷输出（诚实标注推断依据）。"""
        cons = _cons_snapshot([("600519", 9e8)])
        out = _peers_pillar("600519", cons, budget=_budget(), subject_confirmed=True,
                            snapshot_observed_at="2026-11-05T14:00:01")
        assert out["amount_unit"] == "元"
        assert "推断" in out["amount_provenance"]
        assert "f6" in out["amount_provenance"]
        assert out["snapshot_rows"] == 1 and out["rankable_rows"] == 1

    def test_duplicate_code_conflict_rows_dropped(self):
        """重复代码冲突：整组剔除记缺口，不静默取行。"""
        base = _cons_snapshot([("600001", 9e8), ("600002", 8e8), ("600003", 7e8),
                               ("600004", 6e8), ("600005", 5e8), ("600519", 4e8)])
        dup = pd.DataFrame([{"代码": "600002", "名称": "股票600002乙",
                             "成交额": 1e9, "涨跌幅": 0.2}])
        cons = pd.concat([base, dup], ignore_index=True)
        out = _peers_pillar("600519", cons, budget=_budget(), subject_confirmed=True,
                            snapshot_observed_at="2026-11-05T14:00:01")
        codes = [i["code"] for i in out["items"]]
        assert "600002" not in codes
        assert any("重复" in g for g in out["gaps"])

    def test_target_row_duplicated_target_not_sampled(self):
        """目标股行重复冲突：目标不加入样本、位置 None，缺口如实。"""
        base = _cons_snapshot([("600001", 9e8), ("600002", 8e8), ("600003", 7e8),
                               ("600004", 6e8), ("600005", 5e8), ("600519", 4e8)])
        dup = pd.DataFrame([{"代码": "600519", "名称": "贵州茅台乙",
                             "成交额": 9e9, "涨跌幅": 9.9}])
        cons = pd.concat([base, dup], ignore_index=True)
        out = _peers_pillar("600519", cons, budget=_budget(), subject_confirmed=True,
                            snapshot_observed_at="2026-11-05T14:00:01")
        targets = [i for i in out["items"] if i["is_target"]]
        assert targets == []
        assert out["target_change_pct_rank"] is None
        assert any("重复" in g for g in out["gaps"])

    def test_bad_code_rows_dropped(self):
        cons = _cons_snapshot([("600001", 9e8), ("600519", 4e8)])
        bad = pd.DataFrame([{"代码": "BKA001", "名称": "非法", "成交额": 9e9,
                             "涨跌幅": 1.0}])
        cons = pd.concat([cons, bad], ignore_index=True)
        out = _peers_pillar("600519", cons, budget=_budget(), subject_confirmed=True,
                            snapshot_observed_at="2026-11-05T14:00:01")
        codes = [i["code"] for i in out["items"]]
        assert "BKA001" not in codes

    def test_no_amount_column_records_gap(self):
        """快照无成交额列：成员资格与金额指标资格分开——已确认目标股保留
        （代码/名称/涨跌幅等实际字段），无成交额数据不产金额 top5，
        目标-only 样本不发 1/1 伪名次（审查反例：旧版整丢目标）。"""
        cons = pd.DataFrame({"代码": ["600519", "000568"], "名称": ["贵州茅台", "五粮液"],
                             "涨跌幅": [1.65, 2.1]})
        out = _peers_pillar("600519", cons, budget=_budget(), subject_confirmed=True,
                            snapshot_observed_at="2026-11-05T14:00:01")
        assert out["items"] == [{
            "code": "600519", "name": "贵州茅台", "amount": None,
            "change_pct": 1.65, "is_target": True,
        }]
        assert out["sample_size"] == 1
        assert out["rankable_rows"] == 0
        assert out["target_in_amount_top5"] is None
        assert out["target_change_pct_rank"] is None  # 无可比同行不发伪名次
        assert any("无成交额列" in g for g in out["gaps"])
        assert any("无可比" in g for g in out["gaps"])
        # 不把未知金额同行列成伪 top5
        assert all(not i["is_target"] or i["code"] == "600519" for i in out["items"])
        assert [i["code"] for i in out["items"]] == ["600519"]

    def test_none_snapshot_records_gap(self):
        out = _peers_pillar("600519", None, budget=_budget(), subject_confirmed=True,
                            snapshot_observed_at=None)
        assert out["items"] == []
        assert out["gaps"]

    def test_target_missing_from_snapshot_gap_no_fake(self):
        """目标不在快照（防御：资格不符路径）：如实缺口，不补行。"""
        cons = _cons_snapshot([("600001", 9e8), ("600002", 8e8)])
        out = _peers_pillar("600519", cons, budget=_budget(), subject_confirmed=True,
                            snapshot_observed_at="2026-11-05T14:00:01")
        assert all(not i["is_target"] for i in out["items"])
        assert out["target_change_pct_rank"] is None
        assert out["target_in_amount_top5"] is None
        assert any("600519" in g for g in out["gaps"])

    def test_target_change_pct_missing_rank_none(self):
        """目标涨跌幅缺失：保留成员资格，位置 None + 缺口。"""
        cons = _cons_snapshot([("600519", 9e8), ("600002", 8e8)])
        cons.loc[cons["代码"] == "600519", "涨跌幅"] = float("nan")
        out = _peers_pillar("600519", cons, budget=_budget(), subject_confirmed=True,
                            snapshot_observed_at="2026-11-05T14:00:01")
        assert out["items"][0]["is_target"] is True
        assert out["items"][0]["change_pct"] is None
        assert out["target_change_pct_rank"] is None

    def test_snapshot_columns_kept_not_dialected(self):
        """同业字段只用快照已有列（代码/名称/成交额/涨跌幅）。"""
        cons = _cons_snapshot([("600519", 9e8)])
        out = _peers_pillar("600519", cons, budget=_budget(), subject_confirmed=True,
                            snapshot_observed_at="2026-11-05T14:00:01")
        assert out["items"][0] == {
            "code": "600519", "name": "贵州茅台", "amount": 9e8,
            "change_pct": 1.65, "is_target": True,
        }
        assert out["source"] and "成分" in out["source"]


# ---------------------------------------------------------------------------
# 缺口有界化（组合多缺口不炸 schema max_length → 不产生最终 500）
# ---------------------------------------------------------------------------


class TestBoundGaps:
    def test_within_cap_untouched(self):
        gaps = ["g1", "g2"]
        assert _bound_gaps(gaps, 14) == gaps

    def test_over_cap_truncated_with_note(self):
        gaps = [f"缺口{i}" for i in range(20)]
        bounded = _bound_gaps(gaps, 14)
        assert len(bounded) == 14
        assert bounded[:13] == gaps[:13]
        assert "7 条" in bounded[-1]  # 20 - 13 = 7 条被省略并注明
        assert bounded[-1].startswith("（另有")
