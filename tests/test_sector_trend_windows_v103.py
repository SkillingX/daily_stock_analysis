# -*- coding: utf-8 -*-
"""多窗口趋势（5/20/60 日）与二级指数身份接线（issue #103；工单 01）。

测试缝：
- ``_sw_trend_windows_pillar`` 公共支柱缝：严格交易日历 6/21/61 个收盘点
  的窗口切分、共同终点、每序列独立合格性（仅长窗缺点时该序列长窗留空、
  短窗照常）、单序列缺行隔离、取数截止不出站、供应商失败降缺口；
- 中国平安实测快照复核（验收例）：2026-11-19 终点的 61 个收盘点，60 日
  收益保险Ⅱ +3.5682%、非银金融 -4.3282%、沪深 300 -13.1596%（实测留存
  fixture，公开指数收盘点位，不含凭据）；
- ``compose_report`` 编排缝：多窗口/二级身份出站在必需研究（催化+综合）
  之后、二级映射缺口不触发东财兜底、多窗口失败保持原一级事实与完整性
  判定、v3 载荷携带新键且通过严格 schema 校验。

供应商边界全部 mock；目录匹配、窗口切分、收益复合等本地逻辑不 mock。
"""

from __future__ import annotations

import contextlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

import pytest

from src.services import sector_analysis_service as svc
from src.services import sector_sw_market as _sw_mod
from src.services.sector_analysis_service import (
    ReportBudget,
    _sw_trend_windows_pillar,
    compose_report,
)
from src.services.sector_sw_market import SwSectorFetchError

from tests.test_sector_sw_compose_v99 import (
    _ResearchHarness,
    _budget,
    _em_core_stub,
    _sw_core,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "ifind_sw_trend_61pt_20261119.json"


def _sessions(n: int) -> List[date]:
    """n 个连续工作日的合成严格交易日历（跳过周末；窗口切分只依赖序）。"""
    out: List[date] = []
    d = date(2026, 8, 3)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _frame(dates: List[date], closes: Optional[List[float]] = None) -> "object":
    import pandas as pd

    if closes is None:
        closes = [100.0 + i for i in range(len(dates))]
    return pd.DataFrame({
        "date": [d.isoformat() for d in dates],
        "close": closes,
    })


def _full_history(sessions: List[date], *, l2_close: Optional[List[float]] = None,
                  ) -> Dict[str, "object"]:
    """三序列全合格日线响应（同一期望日期集）。"""
    return {
        "801194.SL": _frame(sessions),
        "801790.SL": _frame(sessions),
        "000300.SH": _frame(sessions),
        **({"__l2__": _frame(sessions, l2_close)} if l2_close is not None else {}),
    }


def _patch_pillar_env(monkeypatch, sessions: List[date],
                      frames: Optional[Dict[str, Any]] = None,
                      error: Optional[Exception] = None):
    calls: Dict[str, Any] = {"history": 0, "codes": None}

    def fake_history(codes: str, start_date: str, end_date: str, *,
                     refresh_token: str, timeout_seconds: float,
                     deadline: Optional[float], **kwargs: Any):
        calls["history"] += 1
        calls["codes"] = codes
        calls["window"] = (start_date, end_date)
        calls.setdefault("kwargs", []).append(kwargs)
        if error is not None:
            raise error
        return frames or {}

    monkeypatch.setattr(svc, "recent_completed_trading_sessions",
                        lambda market, n, current_time=None: sessions[:n])
    monkeypatch.setattr(_sw_mod, "fetch_index_history", fake_history)
    return calls


class TestTrendWindowsPillarShape:
    def test_three_windows_with_common_end_and_correct_points(self, monkeypatch):
        sessions = _sessions(61)
        calls = _patch_pillar_env(monkeypatch, sessions,
                                  _full_history(sessions))
        out = _sw_trend_windows_pillar(
            "801194.SL", "保险Ⅱ", "801790.SL", "非银金融",
            budget=_budget(), refresh_token="t", timeout_seconds=5.0)
        assert out is not None
        windows = out["windows"]
        assert [w["window_days"] for w in windows] == [5, 20, 60]
        assert [w["point_count"] for w in windows] == [6, 21, 61]
        for w in windows:
            assert w["end_date"] == sessions[-1].isoformat()  # 共同终点
            expected_n = w["point_count"]
            assert len(w["expected_dates"]) == expected_n
            assert w["expected_dates"][-1] == sessions[-1].isoformat()
            assert w["expected_dates"][0] == sessions[-expected_n].isoformat()
            roles = [s["role"] for s in w["series"]]
            assert roles == ["l2_industry", "l1_industry", "benchmark"]
            for s in w["series"]:
                assert s["qualified_sessions"] == expected_n
                assert s["expected_sessions"] == expected_n
                assert s["period_return_pct"] is not None
        # 单次出站覆盖全部序列与最长窗口（窗口切换不重新取数）
        assert calls["history"] == 1
        assert calls["codes"] == "801194.SL,801790.SL,000300.SH"
        assert calls["window"] == (sessions[-61].isoformat(),
                                   sessions[-1].isoformat())

    def test_returns_match_manual_computation(self, monkeypatch):
        sessions = _sessions(6)
        closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
        frames = {
            "801194.SL": _frame(sessions, closes),
            "801790.SL": _frame(sessions, closes),
            "000300.SH": _frame(sessions, closes),
        }
        _patch_pillar_env(monkeypatch, sessions, frames)
        out = _sw_trend_windows_pillar(
            "801194.SL", "保险Ⅱ", "801790.SL", "非银金融",
            budget=_budget(), refresh_token="t", timeout_seconds=5.0)
        w5 = out["windows"][0]
        assert w5["point_count"] == 6
        for s in w5["series"]:
            # (105/100 - 1) × 100 = 5.0%（5 个间隔复合）
            assert s["period_return_pct"] == pytest.approx(5.0, abs=1e-9)

    def test_l2_none_still_fetches_l1_and_benchmark(self, monkeypatch):
        sessions = _sessions(61)
        frames = {"801790.SL": _frame(sessions), "000300.SH": _frame(sessions)}
        calls = _patch_pillar_env(monkeypatch, sessions, frames)
        out = _sw_trend_windows_pillar(
            None, None, "801790.SL", "非银金融",
            budget=_budget(), refresh_token="t", timeout_seconds=5.0)
        assert calls["codes"] == "801790.SL,000300.SH"
        for w in out["windows"]:
            assert [s["role"] for s in w["series"]] == ["l1_industry",
                                                        "benchmark"]

    def test_fetch_expired_does_not_call_vendor(self, monkeypatch):
        sessions = _sessions(61)
        calls = _patch_pillar_env(monkeypatch, sessions, _full_history(sessions))
        budget = ReportBudget(0.001, finish_margin=10.0)  # 取数截止已在过去
        assert budget.fetch_expired()
        out = _sw_trend_windows_pillar(
            "801194.SL", "保险Ⅱ", "801790.SL", "非银金融",
            budget=budget, refresh_token="t", timeout_seconds=5.0)
        assert calls["history"] == 0
        assert out["windows"] == []
        assert out["gaps"]

    def test_vendor_failure_records_gap(self, monkeypatch):
        sessions = _sessions(61)
        _patch_pillar_env(monkeypatch, sessions,
                          error=SwSectorFetchError("受控失败"))
        out = _sw_trend_windows_pillar(
            "801194.SL", "保险Ⅱ", "801790.SL", "非银金融",
            budget=_budget(), refresh_token="t", timeout_seconds=5.0)
        assert out["windows"] == []
        assert any("查询失败" in g for g in out["gaps"])

    def test_reference_clock_passed_by_orchestration(self, monkeypatch):
        """多窗口出站必须沿用链入口的报告参考钟（review 修正：14:59 开始的
        报告不得在必需研究后跨收盘重取钟切换终点）。"""
        seen_times: List[Any] = []

        def spy(l2_index_code, l2_name, l1_index_code, l1_name, *, budget,
                refresh_token, timeout_seconds, current_time=None):
            seen_times.append(current_time)
            return {"windows": [], "gaps": []}

        monkeypatch.setattr(svc, "_sw_trend_windows_pillar", spy)
        anchor = datetime(2026, 11, 19, 14, 59, 0)
        from tests.test_sector_sw_compose_v99 import _sw_identity

        base = _sw_core()
        ident = dict(_sw_identity("confirmed", index_code="801790.SL",
                                  index_name="非银金融"))
        ident["l2_name"] = "保险Ⅱ"
        base["identity"] = ident
        base["reference_now"] = anchor
        monkeypatch.setattr(
            _sw_mod, "resolve_sw_reference_levels",
            lambda secucode, *, observation_date, refresh_token,
            timeout_seconds, deadline: {
                "l2_name": "保险Ⅱ", "l3_name": None, "gaps": []})
        monkeypatch.setattr(
            _sw_mod, "sw_http_config",
            lambda: ("sector-test-refresh-token", 10.0))
        monkeypatch.setattr(
            _sw_mod, "fetch_index_members",
            lambda *a, **k: (_ for _ in ()).throw(
                SwSectorFetchError("受控：成分不出站")))
        with _ResearchHarness(_em_core_stub()).with_sw(base):
            compose_report("601318", "中国平安", budget=_budget())
        assert seen_times == [anchor]


class TestWindowQualificationTightened:
    """review 修正回归：完整 point_count 才有收益；窗口缺口 scoped。"""

    def test_shortened_calendar_long_window_blank_short_valid(self, monkeypatch):
        """日历只有 50 天：60 日窗（期望 61 点）收益留空 + 窗口缺口；
        20/5 日窗照常合格，且不出现「缺长窗口」类警告（全集缺口不复制）。"""
        sessions = _sessions(50)
        frames = _full_history(sessions)
        _patch_pillar_env(monkeypatch, sessions, frames)
        out = _sw_trend_windows_pillar(
            "801194.SL", "保险Ⅱ", "801790.SL", "非银金融",
            budget=_budget(), refresh_token="t", timeout_seconds=5.0)
        by_days = {w["window_days"]: w for w in out["windows"]}
        w60 = by_days[60]
        assert len(w60["expected_dates"]) == 50
        assert w60["gaps"] and "窗口期望不足" in w60["gaps"][0]
        for s in w60["series"]:
            assert s["period_return_pct"] is None
            assert s["qualified_sessions"] == 50
            assert s["expected_sessions"] == 50
        for days in (5, 20):
            w = by_days[days]
            assert len(w["expected_dates"]) == w["point_count"]
            for s in w["series"]:
                assert s["period_return_pct"] is not None
                assert s["gaps"] == []
        joined = json.dumps(out, ensure_ascii=False)
        assert "缺长窗口" not in joined

    def test_full_set_gap_scoped_out_of_short_windows(self, monkeypatch):
        """61 点全集仅缺最早一天：长窗收益留空+缺口、短窗完整合格且
        series gaps 为空（缺口不逐窗复制）。"""
        sessions = _sessions(61)
        frames = _full_history(sessions)
        import pandas as pd

        frames["801194.SL"] = frames["801194.SL"][
            frames["801194.SL"]["date"] != sessions[0].isoformat()]
        assert isinstance(frames["801194.SL"], pd.DataFrame)
        _patch_pillar_env(monkeypatch, sessions, frames)
        out = _sw_trend_windows_pillar(
            "801194.SL", "保险Ⅱ", "801790.SL", "非银金融",
            budget=_budget(), refresh_token="t", timeout_seconds=5.0)
        by_days = {w["window_days"]: w for w in out["windows"]}
        l2_60 = next(s for s in by_days[60]["series"]
                     if s["role"] == "l2_industry")
        assert l2_60["period_return_pct"] is None
        assert any("60 日窗口缺合格收盘点" in g for g in l2_60["gaps"])
        for days in (5, 20):
            l2 = next(s for s in by_days[days]["series"]
                      if s["role"] == "l2_industry")
            assert l2["period_return_pct"] is not None
            assert l2["gaps"] == []


class TestTrendWindowsQualification:
    def test_long_window_gap_leaves_series_blank_short_windows_intact(
            self, monkeypatch):
        """仅长窗缺点：该序列 60 日收益与曲线留空，5/20 窗照常（验收例）。"""
        sessions = _sessions(61)
        frames = _full_history(sessions)
        # L2 序列仅缺长窗首个收盘点（sessions[-61]）
        import pandas as pd

        frames["801194.SL"] = frames["801194.SL"][
            frames["801194.SL"]["date"] != sessions[-61].isoformat()]
        assert isinstance(frames["801194.SL"], pd.DataFrame)
        _patch_pillar_env(monkeypatch, sessions, frames)
        out = _sw_trend_windows_pillar(
            "801194.SL", "保险Ⅱ", "801790.SL", "非银金融",
            budget=_budget(), refresh_token="t", timeout_seconds=5.0)
        by_days = {w["window_days"]: w for w in out["windows"]}
        l2_60 = next(s for s in by_days[60]["series"]
                     if s["role"] == "l2_industry")
        assert l2_60["period_return_pct"] is None
        assert l2_60["qualified_sessions"] == 60
        assert l2_60["expected_sessions"] == 61
        assert any(str(sessions[-61].isoformat()) in g for g in l2_60["gaps"])
        # 短窗照常
        for days in (5, 20):
            l2_short = next(s for s in by_days[days]["series"]
                            if s["role"] == "l2_industry")
            assert l2_short["period_return_pct"] is not None
        # 其他序列三窗全部合格
        for days in (5, 20, 60):
            for role in ("l1_industry", "benchmark"):
                s = next(x for x in by_days[days]["series"] if x["role"] == role)
                assert s["period_return_pct"] is not None

    def test_missing_l2_frame_isolates_series(self, monkeypatch):
        sessions = _sessions(61)
        frames = {"801790.SL": _frame(sessions), "000300.SH": _frame(sessions)}
        _patch_pillar_env(monkeypatch, sessions, frames)
        out = _sw_trend_windows_pillar(
            "801194.SL", "保险Ⅱ", "801790.SL", "非银金融",
            budget=_budget(), refresh_token="t", timeout_seconds=5.0)
        for w in out["windows"]:
            roles = [s["role"] for s in w["series"]]
            assert roles == ["l1_industry", "benchmark"]
        assert any("801194.SL" in g for g in out["gaps"])


class TestConflictingDuplicateDateIsolation:
    """issue #103 复审回归：61 点查询内同码同日收盘矛盾只隔离该无效日期。

    反例（修复前）：三个角色各 61 日合格收盘，二级 date[20] 加冲突收盘 →
    解析层整码拒绝 → 5/20/60 三窗全丢二级。修复后：只隔离该日；冲突日落在
    60 日独有区间时只影响 60 日窗口，5/20 照常生成；实际请求仍为同一批
    共享请求。整合测试经过真实 ``fetch_index_history``，只 mock 供应商
    传输与交易日历；旧入口 20 点路径的整码拒绝规则由
    ``test_sector_sw_market_v99.py`` 既有用例守护（不因本改动变化）。
    """

    @staticmethod
    def _wire(monkeypatch, tables: List[Dict[str, Any]]) -> None:
        """mock 供应商传输层（fetch_index_history 真实解析不 mock）。"""
        payload = {"errorcode": 0, "errmsg": "", "tables": tables}

        def fake_post(url: str, headers: Dict[str, str],
                      body: Optional[bytes], *, label: str) -> Tuple[int, bytes]:
            assert body is not None
            return 200, json.dumps(payload).encode()

        monkeypatch.setattr(_sw_mod, "_http_api_curl_post", fake_post)
        monkeypatch.setattr(_sw_mod, "_resolve_access_token",
                            lambda rt, *, force_refresh=False: "stub-access-token")

    @staticmethod
    def _table(code: str, sessions: List[date]) -> Dict[str, Any]:
        return {"thscode": code,
                "time": [d.isoformat() for d in sessions],
                "table": {"close": [100.0 + i for i in range(len(sessions))]}}

    def _run_pillar(self, monkeypatch, tables: List[Dict[str, Any]],
                    sessions: List[date]) -> Dict[str, Any]:
        self._wire(monkeypatch, tables)
        monkeypatch.setattr(svc, "recent_completed_trading_sessions",
                            lambda market, n, current_time=None: sessions[:n])
        return _sw_trend_windows_pillar(
            "801194.SL", "保险Ⅱ", "801790.SL", "非银金融",
            budget=_budget(), refresh_token="t", timeout_seconds=5.0)

    def test_conflict_in_long_only_region_blanks_only_60d_window(
            self, monkeypatch):
        """冲突日 sessions[20]（60 日窗口内、5/20 日窗口外）：仅 60 日窗该
        序列缺该日、收益留空；5/20 窗与两条其他序列三窗全部照常（修复前
        该反例 5/20/60 全丢二级）。"""
        sessions = _sessions(61)
        conflict_day = sessions[20]
        conflict = {"thscode": "801194.SL",
                    "time": [conflict_day.isoformat()],
                    "table": {"close": [99999.0]}}
        tables = [self._table("801194.SL", sessions),
                  self._table("801790.SL", sessions),
                  self._table("000300.SH", sessions), conflict]
        out = self._run_pillar(monkeypatch, tables, sessions)
        by_days = {w["window_days"]: w for w in out["windows"]}
        assert set(by_days) == {5, 20, 60}  # 三窗照常生成（不整链丢失）
        l2_60 = next(s for s in by_days[60]["series"]
                     if s["role"] == "l2_industry")
        assert l2_60["period_return_pct"] is None
        assert l2_60["qualified_sessions"] == 60
        assert len(l2_60["observations"]) == 60  # 仅冲突日被隔离
        assert any(conflict_day.isoformat() in g for g in l2_60["gaps"])
        for days in (5, 20):
            l2 = next(s for s in by_days[days]["series"]
                      if s["role"] == "l2_industry")
            assert l2["period_return_pct"] is not None
            assert l2["gaps"] == []
        for days in (5, 20, 60):
            for role in ("l1_industry", "benchmark"):
                s = next(x for x in by_days[days]["series"]
                         if x["role"] == role)
                assert s["period_return_pct"] is not None

    def test_conflict_at_common_end_isolates_date_not_series(
            self, monkeypatch):
        """冲突日为三窗共同终点：三条窗口该序列均缺该日、收益全部留空，
        但观测行保留（5/6、20/21、60/61）——日期级隔离，不整码丢失。"""
        sessions = _sessions(61)
        conflict = {"thscode": "801194.SL",
                    "time": [sessions[-1].isoformat()],
                    "table": {"close": [99999.0]}}
        tables = [self._table("801194.SL", sessions),
                  self._table("801790.SL", sessions),
                  self._table("000300.SH", sessions), conflict]
        out = self._run_pillar(monkeypatch, tables, sessions)
        by_days = {w["window_days"]: w for w in out["windows"]}
        expected = {5: 6, 20: 21, 60: 61}
        for days, points in expected.items():
            l2 = next(s for s in by_days[days]["series"]
                      if s["role"] == "l2_industry")
            assert l2["period_return_pct"] is None
            assert l2["qualified_sessions"] == points - 1
            assert len(l2["observations"]) == points - 1

    def test_wire_isolation_keeps_remaining_dates_and_default_rejects_code(
            self, monkeypatch):
        """wire 层直测：``isolate_conflicting_dates=True`` 保留除冲突日外
        全部日期；默认模式（False）整码拒绝（旧入口既有规则不变）。"""
        from src.services.sector_sw_market import fetch_index_history

        days = ["2026-11-03", "2026-11-04", "2026-11-05"]
        good = {"thscode": "801950.SL", "time": days,
                "table": {"close": [2500.0, 2501.0, 2502.0]}}
        bad = {"thscode": "801950.SL", "time": ["2026-11-04"],
               "table": {"close": [9999.0]}}
        self._wire(monkeypatch, [good, bad])
        frames = fetch_index_history(
            "801950.SL", "2026-11-03", "2026-11-05",
            refresh_token="sector-test-refresh-token", timeout_seconds=10.0,
            deadline=None, isolate_conflicting_dates=True)
        assert list(frames["801950.SL"]["date"]) == ["2026-11-03", "2026-11-05"]
        with pytest.raises(SwSectorFetchError):
            fetch_index_history(
                "801950.SL", "2026-11-03", "2026-11-05",
                refresh_token="sector-test-refresh-token",
                timeout_seconds=10.0, deadline=None)  # 默认模式整码拒绝


class TestExtremeClosePublishBoundary:
    """issue #103 复审二轮：极值收盘的发布边界（真实 wire 整合，只 mock
    供应商传输与交易日历）。

    反例（修复前）：仅进入 60 日窗口的最早 L2 收盘为正数数字串 "1e-40"
    （资格层按有限正 Decimal 采纳），窗口收益 Decimal round 引发
    InvalidOperation，新增可选块直接拖成 500。修复后：该序列 60 日收益与
    曲线留空并注明原因，合格观察保留，5/20 窗与两条其他序列照常，无异常
    上抛；float 下溢（1E-400 → 0.0）的收盘点按缺失语义隔离。
    """

    def test_extreme_small_close_isolates_return_not_series(self, monkeypatch):
        sessions = _sessions(61)
        conflict_day = sessions[-61]  # 仅 60 日窗口含该点
        tables = []
        for code in ("801194.SL", "801790.SL", "000300.SH"):
            closes = [100.0 + i for i in range(len(sessions))]
            if code == "801194.SL":
                closes[0] = "1e-40"  # 供应商正数数字串（Decimal 可解析）
            tables.append({"thscode": code,
                           "time": [d.isoformat() for d in sessions],
                           "table": {"close": closes}})
        TestConflictingDuplicateDateIsolation._wire(monkeypatch, tables)
        monkeypatch.setattr(svc, "recent_completed_trading_sessions",
                            lambda market, n, current_time=None: sessions[:n])
        out = _sw_trend_windows_pillar(
            "801194.SL", "保险Ⅱ", "801790.SL", "非银金融",
            budget=_budget(), refresh_token="t", timeout_seconds=5.0)
        by_days = {w["window_days"]: w for w in out["windows"]}
        assert set(by_days) == {5, 20, 60}  # 三窗照常生成（不整链丢失）
        l2_60 = next(s for s in by_days[60]["series"]
                     if s["role"] == "l2_industry")
        assert l2_60["period_return_pct"] is None  # 收益留空（不 500）
        assert l2_60["qualified_sessions"] == 61
        assert len(l2_60["observations"]) == 61  # 合格观察保留（float 可表示）
        assert any("超出可安全表示范围" in g for g in l2_60["gaps"])
        assert svc._trend_window_series_invariants(out)
        # 5/20 窗不含极值点：收益照常
        for days in (5, 20):
            l2 = next(s for s in by_days[days]["series"]
                      if s["role"] == "l2_industry")
            assert l2["period_return_pct"] is not None
            assert l2["gaps"] == []
        # 其他序列三窗全部正常
        for days in (5, 20, 60):
            for role in ("l1_industry", "benchmark"):
                s = next(x for x in by_days[days]["series"]
                         if x["role"] == role)
                assert s["period_return_pct"] is not None

    def test_float_underflow_close_isolated_as_missing(self, monkeypatch):
        """1E-400：float 转换下溢为 0.0——观测点不可发布，按缺失处理。"""
        sessions = _sessions(61)
        boundary_day = sessions[-61]
        tables = []
        for code in ("801194.SL", "801790.SL", "000300.SH"):
            closes = [100.0 + i for i in range(len(sessions))]
            if code == "801194.SL":
                closes[0] = "1e-400"  # Decimal 有限正，float 下溢为 0
            tables.append({"thscode": code,
                           "time": [d.isoformat() for d in sessions],
                           "table": {"close": closes}})
        TestConflictingDuplicateDateIsolation._wire(monkeypatch, tables)
        monkeypatch.setattr(svc, "recent_completed_trading_sessions",
                            lambda market, n, current_time=None: sessions[:n])
        out = _sw_trend_windows_pillar(
            "801194.SL", "保险Ⅱ", "801790.SL", "非银金融",
            budget=_budget(), refresh_token="t", timeout_seconds=5.0)
        by_days = {w["window_days"]: w for w in out["windows"]}
        assert any("超出可安全表示范围" in g and boundary_day.isoformat() in g
                   for g in out["gaps"])  # 块级缺口注明具体日期
        l2_60 = next(s for s in by_days[60]["series"]
                     if s["role"] == "l2_industry")
        assert l2_60["period_return_pct"] is None  # 窗口不完整 → 收益留空
        assert l2_60["qualified_sessions"] == 60
        assert len(l2_60["observations"]) == 60
        assert any("60 日窗口缺合格收盘点" in g for g in l2_60["gaps"])
        assert all(o["close"] > 0 for s in
                   (s for w in out["windows"] for s in w["series"])
                   for o in s["observations"])  # 不发布下溢为 0 的收盘
        for days in (5, 20):
            l2 = next(s for s in by_days[days]["series"]
                      if s["role"] == "l2_industry")
            assert l2["period_return_pct"] is not None


class TestLiveAcceptanceReturns:
    """中国平安实测快照复核（验收例；61 个收盘点，2026-08-19 → 2026-11-19）。"""

    def test_60day_returns_match_acceptance(self, monkeypatch):
        data = json.loads(_FIXTURE.read_text())
        import pandas as pd

        frames = {}
        for code, s in data["series"].items():
            frames[code] = pd.DataFrame({"date": s["dates"], "close": s["closes"]})
        sessions = [date.fromisoformat(d) for d in data["series"]["801194.SL"]["dates"]]
        assert len(sessions) == 61
        _patch_pillar_env(monkeypatch, sessions, frames)
        out = _sw_trend_windows_pillar(
            "801194.SL", "保险Ⅱ", "801790.SL", "非银金融",
            budget=_budget(), refresh_token="t", timeout_seconds=5.0)
        w60 = next(w for w in out["windows"] if w["window_days"] == 60)
        assert w60["start_date"] == "2026-08-19"
        assert w60["end_date"] == "2026-11-19"
        actual = {s["role"]: s["period_return_pct"] for s in w60["series"]}
        assert actual["l2_industry"] == pytest.approx(3.5682, abs=0.01)
        assert actual["l1_industry"] == pytest.approx(-4.3282, abs=0.01)
        assert actual["benchmark"] == pytest.approx(-13.1596, abs=0.01)


# ---------------------------------------------------------------------------
# 编排接线（compose_report 保留申万路径）
# ---------------------------------------------------------------------------


class _WindowsRecorder:
    """多窗口支柱记录器：记录调用与产出（编排顺序/失败隔离观察）。"""

    def __init__(self, payload: Optional[Dict[str, Any]]) -> None:
        self.payload = payload
        self.calls = 0
        self.seen_l2_codes: List[Optional[str]] = []
        self.order: List[str] = []
        self._shared: Dict[str, List[str]] = {}

    def bind_order(self, shared: Dict[str, List[str]]) -> "_WindowsRecorder":
        self._shared = shared
        return self

    def __call__(self, l2_index_code: Optional[str], *args: Any,
                 **kwargs: Any) -> Optional[Dict[str, Any]]:
        self.calls += 1
        self.seen_l2_codes.append(l2_index_code)
        self._shared.setdefault("order", []).append("windows")
        return dict(self.payload) if self.payload is not None else None


def _windows_payload() -> Dict[str, Any]:
    return {
        "windows": [{
            "window_days": 5, "point_count": 6,
            "start_date": "2026-11-12", "end_date": "2026-11-18",
            "expected_dates": ["2026-11-12", "2026-11-13", "2026-11-14",
                               "2026-11-17", "2026-11-18", "2026-11-19"],
            "series": [{
                "role": "l2_industry", "name": "保险Ⅱ", "index_code": "801194.SL",
                "source": "iFinD 标准HTTP指数日线",
                "expected_sessions": 6, "qualified_sessions": 6,
                "observations": [{"date": "2026-11-19", "close": 1000.0}],
                "period_return_pct": 1.5, "gaps": [],
            }],
            "observed_at": "2026-11-19T15:00:00", "gaps": [],
        }],
        "gaps": [],
    }


def _identity_with_l2(l2_name: Optional[str]) -> Dict[str, Any]:
    from tests.test_sector_sw_compose_v99 import _sw_identity

    base = _sw_core()
    ident = dict(_sw_identity(
        "confirmed", index_code="801790.SL", index_name="非银金融"))
    ident["l2_name"] = l2_name
    base["identity"] = ident
    return base


class TestComposeWiring:
    def test_windows_run_after_required_research(self, monkeypatch):
        """多窗口出站在必需研究（催化+综合）之后（spec 决定 6 顺序契约）。"""
        from tests.test_sector_sw_compose_v99 import (
            _catalysts_stub_payload,
            _synthesis_stub_payload,
        )

        shared: Dict[str, List[str]] = {"order": []}
        win = _WindowsRecorder(_windows_payload()).bind_order(shared)

        def cat(*a: Any, **k: Any) -> Dict[str, Any]:
            shared["order"].append("catalysts")
            return _catalysts_stub_payload()

        def syn(*a: Any, **k: Any) -> Dict[str, Any]:
            shared["order"].append("synthesis")
            return _synthesis_stub_payload()

        payload = self._compose(
            monkeypatch, l2_name="保险Ⅱ",
            extra_patches=[
                (svc, "_sw_trend_windows_pillar", win),
                (svc, "_catalysts_pillar", cat),
                (svc, "_synthesis_pillar", syn),
            ])
        assert payload["research_outcome"] == "kept_sw_stage"
        assert shared["order"].index("windows") > shared["order"].index("catalysts")
        assert shared["order"].index("windows") > shared["order"].index("synthesis")

    def test_windows_emitted_with_l2_confirmed(self, monkeypatch):
        win = _WindowsRecorder(_windows_payload())
        monkeypatch.setattr(svc, "_sw_trend_windows_pillar", win)
        payload = self._compose(monkeypatch, l2_name="保险Ⅱ")
        assert payload["research_outcome"] == "kept_sw_stage"
        ident = payload.get("sector_l2_identity")
        assert ident and ident["status"] == "confirmed"
        assert ident["l2_index_code"] == "801194.SL"
        assert ident["l1_parent_name"] == "非银金融"
        assert ident["directory_version"] == _sw_mod.SW_L2_DIRECTORY_VERSION
        windows = payload.get("sector_trend_windows")
        assert windows and windows[0]["window_days"] == 5

    def test_l2_mapping_gap_keeps_kept_sw_stage(self, monkeypatch):
        """二级归属缺失：只报缺口，不触发东财兜底（工单 01 预算与隔离）。"""
        win = _WindowsRecorder(_windows_payload())
        payload = self._compose(monkeypatch, l2_name=None,
                                extra_patches=[(svc, "_sw_trend_windows_pillar", win)])
        assert payload["research_outcome"] == "kept_sw_stage"
        ident = payload.get("sector_l2_identity")
        assert ident and ident["status"] == "missing"
        assert ident["l2_index_code"] is None
        assert ident["gaps"]
        # 映射失败时多窗口支柱收到 l2_index_code=None（两条 L1/基准序列照常）
        assert win.calls == 1
        assert win.seen_l2_codes == [None]

    def test_windows_failure_keeps_l1_facts_and_completeness(self, monkeypatch):
        """多窗口失败：原一级事实与完整性判定与无该故障时一致（验收例）。"""
        win_ok = _WindowsRecorder(_windows_payload())
        monkeypatch.setattr(svc, "_sw_trend_windows_pillar", win_ok)
        baseline = self._compose(monkeypatch, l2_name="保险Ⅱ")
        win_fail = _WindowsRecorder(None)
        monkeypatch.setattr(svc, "_sw_trend_windows_pillar", win_fail)
        failed = self._compose(monkeypatch, l2_name="保险Ⅱ")
        assert failed.get("sector_trend_windows") is None
        assert failed.get("sector_l2_identity") == baseline.get(
            "sector_l2_identity")
        assert failed["assessment"]["completeness"] == \
            baseline["assessment"]["completeness"]
        assert failed["sector_trend"]["period_return_pct"] == \
            baseline["sector_trend"]["period_return_pct"]
        assert failed["research_outcome"] == "kept_sw_stage"

    def test_partial_windows_keep_block_gaps(self, monkeypatch):
        """有窗口+缺一条序列（issue #103 复审反例）：块级缺口随载荷保留，
        不因其他窗口可用而丢失——Web/Markdown 趋势块据此展示一次。"""
        stub = _windows_payload()
        stub["gaps"] = ["多窗口趋势：iFinD 日线响应缺 保险Ⅱ 801194.SL 数据行，"
                        "该序列各窗口未取得"]
        win = _WindowsRecorder(stub)
        payload = self._compose(
            monkeypatch, l2_name="保险Ⅱ",
            extra_patches=[(svc, "_sw_trend_windows_pillar", win)])
        assert payload.get("sector_trend_windows")
        assert payload.get("sector_trend_windows_gaps") == stub["gaps"]

    def test_no_windows_keep_block_gaps(self, monkeypatch):
        """全无窗口（如凭据未配置）：块级缺口随载荷保留（既有语义回归）。"""
        stub = {"windows": [], "gaps": [
            "多窗口趋势：iFinD 凭据未配置，5/20/60 日新增查询未发起"]}
        win = _WindowsRecorder(stub)
        payload = self._compose(
            monkeypatch, l2_name="保险Ⅱ",
            extra_patches=[(svc, "_sw_trend_windows_pillar", win)])
        assert payload.get("sector_trend_windows") is None
        assert payload.get("sector_trend_windows_gaps") == stub["gaps"]

    def test_compose_payload_validates_with_new_blocks(self, monkeypatch):
        from src.schemas.sector_analysis import SectorReportPayload

        win = _WindowsRecorder(_windows_payload())
        payload = self._compose(
            monkeypatch, l2_name="保险Ⅱ",
            extra_patches=[(svc, "_sw_trend_windows_pillar", win)])
        validated = SectorReportPayload.model_validate(payload)
        assert validated.sector_l2_identity is not None
        assert validated.sector_trend_windows is not None

    def _compose(self, monkeypatch, *, l2_name: Optional[str],
                 extra_patches: Optional[list] = None):
        monkeypatch.setattr(
            _sw_mod, "resolve_sw_reference_levels",
            lambda secucode, *, observation_date, refresh_token,
            timeout_seconds, deadline: {
                "l2_name": l2_name, "l3_name": None, "gaps": []})
        monkeypatch.setattr(
            _sw_mod, "sw_http_config",
            lambda: ("sector-test-refresh-token", 10.0))
        monkeypatch.setattr(
            _sw_mod, "fetch_index_members",
            lambda *a, **k: (_ for _ in ()).throw(
                SwSectorFetchError("受控：成分不出站")))
        sw_core = _identity_with_l2(l2_name)
        # extra patches 用嵌套 patch 上下文（RAII）：harness 的 patch.start/stop
        # 与 monkeypatch teardown 的恢复顺序在此场景会互相覆盖（monkeypatch
        # teardown 晚于 harness stop，会把 harness stub 永久留在模块属性上，
        # 泄漏到后续 testserver 测试）；嵌套 with 保证逆序恢复。
        with _ResearchHarness(_em_core_stub()).with_sw(sw_core) as h, \
                contextlib.ExitStack() as stack:
            for target, name, repl in (extra_patches or []):
                # new 显式给出时不创建 MagicMock（目标属性必然存在）
                stack.enter_context(patch.object(target, name, repl))
            payload = compose_report("601318", "中国平安", budget=_budget())
        assert h.core.calls == []  # 二级缺口不触发东财兜底
        return payload


# ---------------------------------------------------------------------------
# 报告速览（首屏）Markdown 渲染
# ---------------------------------------------------------------------------


def _v3_payload_for_render(*, with_new_blocks: bool) -> Dict[str, Any]:
    from src.schemas.sector_analysis import SectorReportPayload

    payload: Dict[str, Any] = {
        "payload_version": 3,
        "research_basis": "sw2021_l1",
        "research_outcome": "kept_sw_stage",
        "stock_code": "601318",
        "stock_name": "中国平安",
        "generated_at": "2026-11-19T15:30:00",
        "identity": {
            "subject_status": "confirmed",
            "membership": {"verified": False},
            "board_name": "非银金融", "board_code": "801790.SL",
            "observed_at": "2026-11-19T15:30:00",
            "vendor_as_of": "2026-11-19",
            "classification_system": "申万 2021 一级行业",
            "classification_level": "L1",
            "classification_version": "sw2021-l1-20261118",
            "sw_chain": {"l2": "保险Ⅱ", "gaps": []},
            "gaps": [],
        },
        "background": {
            "policy": {"lean": "neutral", "label": "中性"},
            "base_rate": {"base_rate": 0.5, "is_default": True,
                          "table_version": "v1", "table_updated": "2026-01-01",
                          "table_industry_count": 31},
            "intraday": {"sector_found": True, "momentum": 55.0,
                         "snapshot_date": "2026-11-19",
                         "snapshot_mode": "target",
                         "rank_of": 5, "total_boards": 31,
                         "row": {"board_name": "非银金融",
                                 "change_pct": -0.5}},
        },
        "concepts": {"items": [], "total_returned": 0, "truncated": False,
                     "gaps": []},
        "sector_trend": {"eligible": True, "window_completed_sessions": 20,
                         "interval_count": 19, "gaps": []},
        "sector_peers": None,
        "sector_catalysts": {
            "eligible": True, "query": "非银金融 行业 政策 新闻",
            "window_days": 7, "window_start": "2026-11-13",
            "window_end": "2026-11-19", "observed_at": "2026-11-19T15:00:00",
            "provider": "Bocha",
            "items": [{
                "evidence_id": "cat-1", "title": "保险行业监管新规发布",
                "url": "https://example.com/news/1",
                "source_domain": "example.com", "snippet": None,
                "published_date": "2026-11-18",
                "date_basis": "provider_published", "same_event_urls": [],
            }],
            "background_items": [],
            "source": "套餐搜索（通用行业查询）", "gaps": [],
        },
        "sector_synthesis": {"eligible": True, "status": "ok",
                             "summary": "受控摘要", "claims": [],
                             "summary_origin": "server_facts",
                             "rejected_count": 0, "gaps": []},
        "assessment": {"status": "unscored", "completeness": "partial",
                       "score": None, "reasons": []},
        "identification": {"sector_name": "非银金融", "sw_chain": {"l2": "保险Ⅱ",
                                                                  "gaps": []},
                           "gaps": []},
        "policy": {"lean": "neutral", "label": "中性", "score": None},
        "base_rate": {"base_rate": 0.5, "is_default": True,
                      "table_version": "v1", "table_updated": "2026-01-01",
                      "table_industry_count": 31},
        "prosperity": {"sector_found": True, "momentum": 55.0,
                       "row": {"board_name": "非银金融", "change_pct": -0.5}},
    }
    if with_new_blocks:
        payload["sector_l2_identity"] = {
            "status": "confirmed", "l2_name": "保险Ⅱ",
            "l2_index_code": "801194.SL", "l1_parent_name": "非银金融",
            "directory_version": _sw_mod.SW_L2_DIRECTORY_VERSION,
            "observation_date": "2026-11-19", "gaps": [],
        }
        payload["sector_trend_windows"] = [{
            "window_days": 60, "point_count": 61,
            "start_date": "2026-08-19", "end_date": "2026-11-19",
            "expected_dates": ["2026-08-19", "2026-11-19"],
            "series": [
                {"role": "l2_industry", "name": "保险Ⅱ",
                 "index_code": "801194.SL",
                 "source": "iFinD 标准HTTP指数日线",
                 "expected_sessions": 61, "qualified_sessions": 61,
                 "observations": [{"date": "2026-11-19", "close": 1000.0}],
                 "period_return_pct": 3.5682, "gaps": []},
                {"role": "l1_industry", "name": "非银金融",
                 "index_code": "801790.SL",
                 "source": "iFinD 标准HTTP指数日线",
                 "expected_sessions": 61, "qualified_sessions": 60,
                 "observations": [], "period_return_pct": None,
                 "gaps": ["60 日窗口缺合格收盘点（2026-08-19），"
                          "该序列本窗口收益与曲线留空（不取旧日凑数）"]},
                {"role": "benchmark", "name": "沪深300",
                 "index_code": "000300.SH",
                 "source": "iFinD 标准HTTP指数日线",
                 "expected_sessions": 61, "qualified_sessions": 61,
                 "observations": [{"date": "2026-11-19", "close": 4000.0}],
                 "period_return_pct": -13.1596, "gaps": []},
            ],
            "observed_at": "2026-11-19T15:29:00", "gaps": [],
        }]
    return SectorReportPayload.model_validate(payload).model_dump()


class TestFirstScreenMarkdown:
    def _render(self, payload: Dict[str, Any]) -> str:
        from src.services.sector_analysis_service import _render

        return _render("中国平安", "601318", payload["generated_at"],
                       "sa_rendercheck_103", payload)

    def test_first_screen_renders_l1_quote_rank_and_source(self):
        payload = _v3_payload_for_render(with_new_blocks=True)
        md = self._render(payload)
        assert "报告速览" in md
        # L1 当日行情行（target 口径：当日措辞 + 31 口径排名 + 行情日期）
        assert "当日涨跌幅 -0.5%" in md
        assert "当日排名 5 / 31" in md
        assert "行情日期 2026-11-19" in md
        # iFinD 数据源随首屏数值标出（渲染后中英间有盘古空格）
        assert "数据来源：" in md
        assert "iFinD 标准 HTTP 指数日线" in md
        assert "不重新取数" in md

    def test_first_screen_reference_mode_uses_reference_wording(self):
        payload = _v3_payload_for_render(with_new_blocks=True)
        intraday = payload["background"]["intraday"]
        intraday["snapshot_mode"] = "reference"
        intraday["snapshot_date"] = "2026-11-18"
        md = self._render(payload)
        assert "参考日涨跌幅 -0.5%" in md
        assert "参考日排名 5 / 31" in md
        assert "参考口径 2026-11-18" in md
        assert "当日排名" not in md

    def test_first_screen_renders_l2_identity_and_window_returns(self):
        md = self._render(_v3_payload_for_render(with_new_blocks=True))
        assert "保险Ⅱ（801194.SL）" in md
        assert "非银金融" in md
        assert "3.5682%" in md
        assert "-13.1596%" in md
        assert "留空" in md  # 一级长窗缺口序列收益留空
        assert "60 日窗口缺合格收盘点" in md
        # 催化首条（结构化 found 摘要）
        assert "保险行业监管新规发布" in md
        assert "https://example.com/news/1" in md
        assert "2026-11-18" in md

    def test_whole_window_failure_surfaces_in_summary(self):
        payload = _v3_payload_for_render(with_new_blocks=True)
        payload["sector_trend_windows"] = None
        payload["sector_trend_windows_gaps"] = [
            "多窗口趋势：iFinD 指数日线查询失败（受控失败），"
            "5/20/60 日窗口未取得（新增可选查询，只降本块，不影响一级事实"
            "与完整性判定）"]
        md = self._render(payload)
        assert "多窗口趋势未取得" in md
        assert "受控失败" in md

    def test_block_gap_renders_once_with_windows(self):
        """有窗口+缺一条序列（issue #103 复审）：块级缺口随趋势块展示一次，
        不逐窗口重复、不在首屏重复描述。"""
        payload = _v3_payload_for_render(with_new_blocks=True)
        payload["sector_trend_windows_gaps"] = [
            "多窗口趋势：iFinD 日线响应缺 保险Ⅱ 801194.SL 数据行，"
            "该序列各窗口未取得"]
        md = self._render(payload)
        assert "多窗口趋势缺口" in md
        assert md.count("该序列各窗口未取得") == 1

    def test_catalyst_states_render_structured_summary(self):
        """三态呈现共用结构化 first_screen（不解析 gaps 文案）。"""
        cases = {
            "none_found": "未发现合格催化",
            "search_failed": "全部检索失败",
            "partial_failed": "部分检索渠道失败",
            "search_unavailable": "搜索入口未配置",
            "budget_exhausted": "取数预算不足",
        }
        for state, expected in cases.items():
            payload = _v3_payload_for_render(with_new_blocks=True)
            payload["sector_catalysts"]["items"] = []
            payload["sector_catalysts"]["first_screen"] = {
                "state": state, "headline_evidence_id": None}
            md = self._render(payload)
            assert expected in md, state
            # 不把 worker/provider 失败诊断当首行展示
            assert "gaps[0]" not in md

    def test_missing_l2_identity_renders_gap(self):
        payload = _v3_payload_for_render(with_new_blocks=False)
        payload["sector_l2_identity"] = {
            "status": "missing", "l2_name": None, "l2_index_code": None,
            "l1_parent_name": None,
            "directory_version": _sw_mod.SW_L2_DIRECTORY_VERSION,
            "observation_date": "2026-11-19",
            "gaps": ["申万二级归属未取得（参考链未查询/失败或取数预算已耗尽），"
                     "二级对照未采纳"],
        }
        md = self._render(payload)
        assert "二级对照未采纳" in md

    def test_legacy_payload_without_new_keys_skips_first_screen(self):
        md = self._render(_v3_payload_for_render(with_new_blocks=False))
        assert "报告速览" not in md
        # 既有章节不受影响
        assert "主行业定位" in md

    # ---- 二级同业位置速览（issue #105 修复）：趋势与催化之间 ----

    @staticmethod
    def _l2_peers_block(**overrides: Any) -> Dict[str, Any]:
        block = {
            "eligible": True,
            "selection_rule": "同一成分快照按成交额前五 + 目标股去重",
            "items": [
                {"code": "601318", "name": "中国平安", "amount": 9.0e8,
                 "change_pct": 1.2, "is_target": True},
                {"code": "601601", "name": "中国太保", "amount": 5.0e8,
                 "change_pct": 0.8, "is_target": False},
            ],
            "target_in_amount_top5": True, "target_change_pct_rank": 1,
            "target_amount_rank": 1, "amount_rank_denominator": 2,
            "amount_rank_scope": "full_constituents",
            "amount_rank_evidence": "iFinD 双接口核验一致（受控证据）",
            "amount_unit": "元", "rankable_rows": 2, "valid_quote_rows": 2,
            "change_pct_qualified_rows": 2, "verified_constituent_num": 2,
            "sample_size": 2, "snapshot_observed_at": "2026-11-19T15:29:00",
            "source": "iFinD 申万指数成分（p03473）+ 成员行情",
            "quotes_as_of": "2026-11-19 22:00:46",
            "industry_reference": False,
            "target_independent_quote": None, "gaps": [],
        }
        block.update(overrides)
        return block

    def test_l2_peers_summary_between_trend_and_catalyst(self):
        """有同业：速览在趋势之后、催化之前给出二级同业位置（口径、分母、
        名次与涨跌幅相对位置）；小节以三级标题承载（不破坏网页以二级标题
        为边界的裁剪）。"""
        payload = _v3_payload_for_render(with_new_blocks=True)
        payload["sector_l2_peers"] = self._l2_peers_block()
        md = self._render(payload)
        assert "二级同业位置" in md
        assert "成交额名次第 **1** / 2" in md
        assert "全成分正额范围" in md
        assert "涨跌幅相对位置第 **1** / 2" in md
        assert "正额可排名 2 行" in md
        assert "成交活跃代表" in md
        # 顺序：速览节内 同业位置 在趋势之后、催化之前
        i_summary = md.index("二级同业位置")
        i_trend = md.index("最近 60 个交易日")
        i_cat = md.index("近七天催化")
        assert i_trend < i_summary < i_cat
        # 三级标题：仍在「报告速览」二级标题节内（网页裁剪语义保持）
        first_screen_section = md.split("## 报告速览", 1)[1].split("\n## ", 1)[0]
        assert "二级同业位置" in first_screen_section
        assert "近七天催化" in first_screen_section

    def test_l2_peers_summary_industry_reference_no_comparable(self):
        """无可比资格（行业参考）：位置行如实标注不适用，不给成分内名次。"""
        payload = _v3_payload_for_render(with_new_blocks=True)
        payload["sector_l2_peers"] = self._l2_peers_block(
            industry_reference=True, target_amount_rank=None,
            amount_rank_denominator=None, amount_rank_scope=None,
            target_change_pct_rank=None)
        md = self._render(payload)
        assert "二级同业位置" in md
        assert "行业参考" in md
        assert "不适用（行业参考" in md

    def test_l2_peers_chain_gap_renders_in_first_screen(self):
        """二级同业整链未取得：速览给出缺口提示（首条），详细诊断留正文。"""
        payload = _v3_payload_for_render(with_new_blocks=True)
        payload["sector_l2_peers"] = None
        payload["sector_l2_peers_gaps"] = [
            "二级同业：取数预算已耗尽，二级成分与全量核验未发起"]
        md = self._render(payload)
        i_summary = md.index("二级同业未取得")
        i_cat = md.index("近七天催化")
        assert i_summary < i_cat
        assert "取数预算已耗尽" in md

    def test_legacy_payload_without_l2_peers_key_no_summary(self):
        """旧存档路径：无 sector_l2_peers 键（且无链缺口）→ 速览无二级
        同业小节，其余速览内容不变。"""
        payload = _v3_payload_for_render(with_new_blocks=True)
        assert payload.get("sector_l2_peers") is None
        md = self._render(payload)
        assert "二级同业位置" not in md
        assert "报告速览" in md
        assert "近七天催化" in md

    def test_catalyst_found_with_partial_search_failure_marked(self):
        """issue #105 修复：已找到催化时部分检索失败并列标注（state 保持
        found、原文与日期照常展示）。"""
        payload = _v3_payload_for_render(with_new_blocks=True)
        payload["sector_catalysts"]["first_screen"] = {
            "state": "found", "headline_evidence_id": "cat-1",
            "partial_search_failures": 1}
        md = self._render(payload)
        assert "保险行业监管新规发布" in md  # 原文与日期照常
        assert "2026-11-18" in md
        assert "部分检索渠道失败 1 个" in md
        # 旧存档（无该字段）不标注
        payload2 = _v3_payload_for_render(with_new_blocks=True)
        payload2["sector_catalysts"]["first_screen"] = {
            "state": "found", "headline_evidence_id": "cat-1"}
        md2 = self._render(payload2)
        assert "部分检索渠道失败" not in md2
        assert "保险行业监管新规发布" in md2


class TestCatalystFirstScreenSummary:
    """首屏催化结构化摘要（review 修正）：state 在既有检索分支处发布，
    呈现层共用结构化字段，不解析 gap 文案（worker 诊断与聚合诊断的 gaps
    顺序不代表检索结论）。"""

    class _FakeProvider:
        name = "SyntheticSearch"

        def __init__(self, rows, *, success=True, error_message=None):
            self._rows = rows
            self._success = success
            self._error_message = error_message

        @property
        def is_available(self) -> bool:
            return True

        def search(self, query, max_results=5, days=7):
            from src.search_service import SearchResult

            results = [SearchResult(**row) for row in self._rows]
            return type("R", (), {
                "query": query, "results": results, "provider": self.name,
                "success": self._success, "error_message": self._error_message,
                "search_time": 0.01,
            })()

    def _run(self, specs: List[Dict[str, Any]]) -> Dict[str, Any]:
        """specs: 每项一个受控 provider（rows/success/error_message）；
        未给 rows 的 provider 默认带一条窗口内合格结果。"""
        from datetime import date as _date

        from src.search_service import get_search_service
        from src.services.sector_analysis_service import _catalysts_pillar

        today = _date.today().isoformat()
        providers = []
        for spec in specs:
            default_rows = [{"title": "白酒行业新政策发布", "snippet": "新规落地",
                             "url": "https://example.com/news/1",
                             "source": "example.com",
                             "published_date": today}]
            # rows 显式给出（含空列表=成功零行场景）时不得被默认行顶替
            raw_rows = spec.get("rows")
            if raw_rows is None:
                raw_rows = default_rows
            spec_rows = [dict(r) for r in raw_rows]
            for r in spec_rows:
                r.setdefault("published_date", today)
                r.setdefault("title", "白酒行业新政策发布")
                r.setdefault("snippet", "新规落地")
                r.setdefault("url", f"https://example.com/news/{len(providers)}")
                r.setdefault("source", "example.com")
            providers.append(self._FakeProvider(
                spec_rows, success=spec.get("success", True),
                error_message=spec.get("error_message")))
        monkeypatch_target = get_search_service()
        original = monkeypatch_target._providers
        monkeypatch_target._providers = providers
        try:
            return _catalysts_pillar(
                "白酒Ⅱ", budget=_budget(), subject_confirmed=True,
                stock_name="贵州茅台")
        finally:
            monkeypatch_target._providers = original

    def test_found_state_with_headline(self):
        out = self._run([{}])
        fs = out["first_screen"]
        assert fs["state"] == "found"
        assert fs["headline_evidence_id"] == out["items"][0]["evidence_id"]

    def test_none_found_state_when_search_succeeds_empty(self):
        out = self._run([{"rows": []}])
        fs = out["first_screen"]
        assert fs["state"] == "none_found"
        assert fs["headline_evidence_id"] is None
        assert out["items"] == []

    def test_search_failed_state_when_all_providers_fail(self):
        out = self._run([{"success": False, "error_message": "受控失败"}])
        assert out["first_screen"]["state"] == "search_failed"

    def test_partial_failed_state_with_mixed_outcomes(self):
        """成功零行 + 失败渠道：partial_failed（不是 search_failed）。"""
        out = self._run([{"rows": []},
                         {"success": False, "error_message": "受控失败"}])
        assert out["first_screen"]["state"] == "partial_failed"

    def test_search_unavailable_when_no_provider(self):
        from src.search_service import get_search_service

        from src.services.sector_analysis_service import _catalysts_pillar

        svc_instance = get_search_service()
        original = svc_instance._providers
        svc_instance._providers = []
        try:
            out = _catalysts_pillar(
                "白酒Ⅱ", budget=_budget(), subject_confirmed=True,
                stock_name="贵州茅台")
        finally:
            svc_instance._providers = original
        assert out["first_screen"]["state"] == "search_unavailable"
