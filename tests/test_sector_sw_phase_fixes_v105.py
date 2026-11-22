# -*- coding: utf-8 -*-
"""PR #105 修复二轮：接收时钟相位、无可用字段观测基线与两级排序键回归。

复审反例（真实 qualifier + 推进时钟，不 stub 资格谓词）：
- L1 每批接收钟：请求 13:59:58、响应 14:00:02 到达、报价戳 14:00:00——
  基线采用（接收钟在响应后取，不是请求前）；14:59:56 报价 15:00:02 到达
  ——会话已完结，盘中旧报价拒绝（曾被请求前单钟误当合格盘中价）。
- L2 相位钉定：14:59:58 复用 A（戳 14:59:56），补查 B 期间跨收盘
  （B 15:00:02 到达、戳 15:00:01）——保留 B、撤销 A 并披露缺口；B 查询
  失败同样不留不再合格的旧盘中事实。
- 无可用字段观测（amount=-1/change=None、同日有效时间与身份）：基线保留
  观测（归属日、quotes_as_of、「无效 1」），不丢为「缺失」。
- 排序键两级口径：同一组切换第 5/6 名边界的金额数据（差异超出 Decimal
  上下文精度），一级基线 ``-amount``（上下文取负截断 → 并列按代码）与
  二级 ``copy_negate``（精确降序）各自给出基线/精确样本。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

import pytest

from src.services import sector_analysis_service as svc
from src.services import sector_sw_market as sw
from src.services.sector_analysis_service import (
    ReportBudget,
    _fetch_constituents,
    _sw_peers_pillar,
)
from src.services.sector_sw_market import SwSectorFetchError

SESSION_DATE = "2026-11-18"
SESSION_OPEN = datetime(2026, 11, 18, 9, 30)
SESSION_CLOSE = datetime(2026, 11, 18, 15, 0)
L2_INDEX = "801951.SL"
TARGET = "601318"
TARGET_SEC = "601318.SH"
MEMBERS = ["000552.SZ", "000723.SZ", "601318.SH", "601225.SH", "601898.SH"]


def _budget(seconds: float = 120.0) -> ReportBudget:
    return ReportBudget(seconds, finish_margin=min(5.0, seconds / 2.0))


def _session() -> "svc.SessionContext":
    return svc.SessionContext(session_date=date(2026, 11, 18),
                              session_open=SESSION_OPEN,
                              session_close=SESSION_CLOSE)


def _row(secucode: str, *, amount: Any = 5.0e8, change: Optional[float] = 1.0,
         stamp: datetime, trade_date: str = SESSION_DATE) -> Dict[str, Any]:
    """已解析行情行（fetch_index_quotes 出口形状；资格谓词真实执行）。"""
    latest = round(100.0 * (1 + (change or 0.0) / 100.0), 3)
    return {
        "thscode": secucode, "trade_date": trade_date,
        "trade_time": stamp.strftime("%H:%M:%S"), "stamp": stamp,
        "change_pct": change, "amount": amount,
        "latest": latest, "pre_close": 100.0,
        "turnover": 2.0, "up_count": 20.0, "down_count": 10.0,
    }


class _Clock:
    """推进时钟：fake 出站在返回前推进（模拟响应晚于请求到达）。"""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def advance(self, delta: timedelta) -> None:
        self.now = self.now + delta


def _install(monkeypatch: pytest.MonkeyPatch, clock: _Clock, *,
             rows_by_code: Optional[Dict[str, Dict[str, Any]]] = None,
             fail: bool = False, member_codes: Optional[List[str]] = None,
             ) -> List[List[str]]:
    """安装受控缝：成分名单固定、行情出站按推进时钟返回/失败。

    返回出站批次记录（每批代码列表）。资格判定（qualify_quote_rows 与
    复用复核）全部走真实实现。
    """
    calls: List[List[str]] = []
    codes = member_codes or list(MEMBERS)

    def fake_members(index_code, observation_date, *, refresh_token,
                     timeout_seconds, deadline, full_constituent_audit=False):
        return {
            "member_codes": list(codes),
            "member_names": {c: f"成员{c[:6]}" for c in codes},
            "dates_seen": [SESSION_DATE], "rows_total": len(codes),
            "completeness_evidence": None,
            "rejected_identity_rows": 0, "conflicting_code_members": 0,
            "blank_date_rows": 0, "off_date_rows": 0,
            "conflicting_name_rows": 0, "identity_echo_conflict": False,
            "shape_gap_count": 0, "over_publish_limit": False, "gaps": [],
        }

    def fake_quotes(codes_str, *, refresh_token, timeout_seconds, deadline,
                    indicators, raw_amount):
        batch = codes_str.split(",")
        calls.append(batch)
        clock.advance(timedelta(seconds=4))  # 响应晚于请求到达
        if fail:
            raise SwSectorFetchError("受控传输失败")
        return [row for code, row in (rows_by_code or {}).items()
                if code in set(batch)]

    monkeypatch.setattr(sw, "fetch_index_members", fake_members)
    monkeypatch.setattr(sw, "fetch_index_quotes", fake_quotes)
    monkeypatch.setattr(svc, "_received_at_now", lambda: clock.now)
    return calls


class TestL1PerResponseReceivedClock:
    """L1 每批接收钟（基线语义恢复）：请求前/单钟不能充当响应接收时刻。"""

    def test_quote_stamped_between_request_and_response_accepted(self,
                                                                 monkeypatch):
        """反例 A：报价戳 14:00:00，请求 13:59:58、响应 14:00:02 到达——
        基线采用（盘中、戳 ≥ 开盘且 ≤ 实际接收）；请求前单钟曾误拒为
        「未来行情」。"""
        clock = _Clock(datetime(2026, 11, 18, 13, 59, 58))
        row = _row(TARGET_SEC, stamp=datetime(2026, 11, 18, 14, 0, 0))
        _install(monkeypatch, clock, rows_by_code={TARGET_SEC: row},
                 member_codes=[TARGET_SEC])
        out = _fetch_constituents(
            TARGET, L2_INDEX, SESSION_DATE, _session(), name="中国平安",
            refresh_token="sector-test-refresh-token", timeout_seconds=10.0,
            budget=_budget())
        assert out["member_quotes"].get(TARGET_SEC) is row
        assert out["member_quote_gaps"] == []

    def test_intraday_stamp_arriving_after_close_rejected(self, monkeypatch):
        """反例 B：报价戳 14:59:56、15:00:02 到达——会话已完结，戳 < 收盘
        → 拒绝（请求前单钟曾误当合格盘中价）；成员如实缺失。"""
        clock = _Clock(datetime(2026, 11, 18, 14, 59, 58))
        row = _row(TARGET_SEC, stamp=datetime(2026, 11, 18, 14, 59, 56))
        _install(monkeypatch, clock, rows_by_code={TARGET_SEC: row},
                 member_codes=[TARGET_SEC])
        out = _fetch_constituents(
            TARGET, L2_INDEX, SESSION_DATE, _session(), name="中国平安",
            refresh_token="sector-test-refresh-token", timeout_seconds=10.0,
            budget=_budget())
        assert TARGET_SEC not in out["member_quotes"]


class TestL2PhasePinningAcrossClose:
    """L2 相位钉定：补查期间（含查询失败）跨收盘 → 剔除旧盘中行、留收盘行。"""

    A_SEC = "601318.SH"
    B_SEC = "601898.SH"

    def _run(self, monkeypatch, *, b_fails: bool) -> Dict[str, Any]:
        # 14:59:58 复用检查钟：A（戳 14:59:56，盘中合格）可复用；B 无一级
        # 行情 → 纳入缺项补查。补查响应 4 秒后（15:00:02）到达，跨收盘。
        clock = _Clock(datetime(2026, 11, 18, 14, 59, 58))
        a_row = _row(self.A_SEC, stamp=datetime(2026, 11, 18, 14, 59, 56))
        b_row = _row(self.B_SEC, amount=2.0e9, change=2.5,
                     stamp=datetime(2026, 11, 18, 15, 0, 1))
        rows = {self.B_SEC: b_row} if not b_fails else {}
        _install(monkeypatch, clock, rows_by_code=rows, fail=b_fails,
                 member_codes=[self.A_SEC, self.B_SEC])
        return _fetch_constituents(
            TARGET, L2_INDEX, SESSION_DATE, _session(), name="中国平安",
            refresh_token="sector-test-refresh-token", timeout_seconds=10.0,
            budget=_budget(), full_constituent_audit=True,
            reuse_member_quotes={self.A_SEC: a_row})

    def test_close_crossing_keeps_post_close_row_revokes_intraday_reuse(
            self, monkeypatch):
        out = self._run(monkeypatch, b_fails=False)
        quotes = out["member_quotes"]
        # B：收盘后到达且戳 ≥ 收盘 → 保留
        assert quotes.get(self.B_SEC) is not None
        assert quotes[self.B_SEC]["amount"] == 2.0e9
        # A：补查期间会话跨收盘 → 旧盘中行不再合格，撤销并披露缺口
        assert self.A_SEC not in quotes
        gaps = " ".join(out["member_quote_gaps"])
        assert "跨收盘" in gaps and "1 只" in gaps
        assert "601318" in gaps

    def test_close_crossing_with_failed_refetch_leaves_no_stale_intraday(
            self, monkeypatch):
        out = self._run(monkeypatch, b_fails=True)
        quotes = out["member_quotes"]
        # B 查询失败：如实缺失；A 旧盘中事实同样不留（跨收盘不再合格）
        assert self.B_SEC not in quotes
        assert self.A_SEC not in quotes
        gaps = " ".join(out["member_quote_gaps"])
        assert "跨收盘" in gaps
        assert any("查询失败" in g for g in out["member_quote_gaps"])

    def test_stable_session_keeps_reuse_semantics(self, monkeypatch):
        """稳定会话（未跨收盘）：零补查/只查缺项语义不变——盘中相位内
        复用行保留，无相位剔除缺口。"""
        clock = _Clock(datetime(2026, 11, 18, 14, 0, 0))
        a_row = _row(self.A_SEC, stamp=datetime(2026, 11, 18, 13, 45, 0))
        _install(monkeypatch, clock, rows_by_code={},
                 member_codes=[self.A_SEC])
        out = _fetch_constituents(
            TARGET, L2_INDEX, SESSION_DATE, _session(), name="中国平安",
            refresh_token="sector-test-refresh-token", timeout_seconds=10.0,
            budget=_budget(), full_constituent_audit=True,
            reuse_member_quotes={self.A_SEC: a_row})
        assert out["member_quotes"].get(self.A_SEC) is a_row
        assert not any("跨收盘" in g for g in out["member_quote_gaps"])


class TestNoUsableFactRowBaseline:
    """合格但无可用字段（amount=-1/change=None）的观测按基线保留。"""

    def test_qualified_no_fact_row_kept_with_diagnostics(self, monkeypatch):
        """同日有效时间与身份、金额 -1/涨跌幅缺失：资格接纳 → 行保留
        （归属日进 as_of_date、时点进 quotes_as_of、支柱按「无效 1」披露），
        不丢日期、不改报「缺失」。"""
        clock = _Clock(datetime(2026, 11, 18, 23, 0, 0))
        row = _row(TARGET_SEC, amount=-1, change=None,
                   stamp=datetime(2026, 11, 18, 22, 0, 46))
        _install(monkeypatch, clock, rows_by_code={TARGET_SEC: row},
                 member_codes=[TARGET_SEC])
        out = _fetch_constituents(
            TARGET, L2_INDEX, SESSION_DATE, _session(), name="中国平安",
            refresh_token="sector-test-refresh-token", timeout_seconds=10.0,
            budget=_budget())
        kept = out["member_quotes"].get(TARGET_SEC)
        assert kept is row  # 基线保留（观测事实不丢）
        assert kept["trade_date"] == SESSION_DATE
        # 支柱诊断：无效金额计数披露（不是缺失），行情时点保留
        pillar = _sw_peers_pillar(
            TARGET, constituents=out,
            snapshot_observed_at="2026-11-18T23:00:00", budget=_budget())
        assert pillar["quotes_as_of"] == "2026-11-18 22:00:46"
        assert "无效 1" in " ".join(pillar["gaps"])
        items = pillar["items"]
        assert items and items[0]["as_of_date"] == SESSION_DATE
        assert items[0]["amount"] is None  # 无效金额不发布数值


class TestTwoLevelAmountSortKey:
    """排序键两级口径：第 5/6 名边界在上下文精度外切换。"""

    P_SEC = "601899.SH"   # 精确较大（33 位有效数字）
    Q_SEC = "600188.SH"   # 数值较小、裸码字典序在前
    P_AMOUNT = Decimal("5.0000000000000000000000000000001E8")
    Q_AMOUNT = Decimal("5.0E8")

    def _constituents(self) -> Dict[str, Any]:
        codes = ["601318.SH", "601601.SH", "601336.SH", "601628.SH",
                 self.P_SEC, self.Q_SEC]
        amounts = {"601318.SH": 9.0e8, "601601.SH": 8.0e8,
                   "601336.SH": 7.0e8, "601628.SH": 6.0e8,
                   self.P_SEC: self.P_AMOUNT, self.Q_SEC: self.Q_AMOUNT}
        members = {
            "member_codes": list(codes),
            "member_names": {c: f"成员{c[:6]}" for c in codes},
            "dates_seen": [SESSION_DATE], "rows_total": len(codes),
            "completeness_evidence": None,
            "rejected_identity_rows": 0, "conflicting_code_members": 0,
            "blank_date_rows": 0, "off_date_rows": 0,
            "conflicting_name_rows": 0, "identity_echo_conflict": False,
            "shape_gap_count": 0, "over_publish_limit": False, "gaps": [],
        }
        membership = sw.classify_membership(
            TARGET_SEC, members, observation_date=SESSION_DATE)
        quotes = {c: _row(c, amount=a, change=1.0,
                          stamp=datetime(2026, 11, 18, 22, 0, 46))
                  for c, a in amounts.items()}
        return {"membership": membership, "members": members,
                "member_quotes": quotes, "member_quote_gaps": [],
                "target_quote": None, "target_quote_gaps": [],
                "target_name": "中国平安"}

    def test_l1_baseline_context_negation_tie_breaks_by_code(self):
        """一级基线 ``-amount``：上下文取负把 33 位差异截断为同值 → 并列
        按代码升序（600188 < 601899）→ 第 5 名是 Q（600188 入样本）。"""
        pillar = _sw_peers_pillar(
            TARGET, constituents=self._constituents(),
            snapshot_observed_at="2026-11-18T23:00:00", budget=_budget())
        sample_codes = [i["code"] for i in pillar["items"]]
        assert "600188" in sample_codes
        assert "601899" not in sample_codes

    def test_l2_exact_copy_negate_preserves_precision_order(self):
        """二级 ``copy_negate``：精确降序保留上下文外差异 → 第 5 名是
        P（601899 入样本）；与一级同一数据、不同口径的排序结果义务。"""
        constituents = self._constituents()
        constituents["membership"]["completeness_evidence"] = "受控核验证据"
        pillar = _sw_peers_pillar(
            TARGET, constituents=constituents,
            snapshot_observed_at="2026-11-18T23:00:00", budget=_budget(),
            amount_rank_enabled=True)
        sample_codes = [i["code"] for i in pillar["items"]]
        assert "601899" in sample_codes
        assert "600188" not in sample_codes
