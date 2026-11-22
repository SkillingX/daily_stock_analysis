# -*- coding: utf-8 -*-
"""二级成员行情同报告复用（issue #105 修复回归）。

修复规格：二级成分名单独立核验后，成员行情按**带交易所完整代码、报告目标
交易日**复用同一报告上下文中一级已合格的报价（保持原供应商行情时点），仅
查缺失/不合格成员；复用候选按**本次接收钟**复核会话时间资格（收盘边界后
盘中旧报价不冒充收盘后合格报价）；补查新行至少有一项可用事实才**整行替换**
（不拼接不同观测时点的字段）；补查失败/无该码/资格拒绝/新行两字段均不可用
时保留仍满足本次会话时间资格的旧可用行。名单外、异日、异交易所不复用；
目标独立行情不复用（保留既有独立查询顺序）；一级临时失败的批次诊断不整体
继承到二级；一级取数过程不需要了解二级（默认参数行为不变）。

覆盖（修复方案第 2 条逐项）：
- 全复用零二级行情批次（fetch_index_quotes 零调用）；
- 部分仅查缺项（补查批次只含缺失成员）；
- 补查失败保留旧可用行 + 全量资格正确降范围（门槛 4 拒绝）；
- 补查成功但没有可用新行时保留旧行（整行替换门）；
- 补查有金额无涨跌幅时不拼接旧涨跌幅（整行替换）；
- 同码异交易所与异日不可复用；
- 同日报价真零复用（合法零观测）；
- 收盘前 14:59 候选在 15:00 后不能复用且只补该成员；
- 目标未确认纳入时目标独立行情照常独立查询（不复用）。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

import pytest

from src.services import sector_analysis_service as svc
from src.services import sector_sw_market as sw
from src.services.sector_analysis_service import (
    ReportBudget,
    _fetch_constituents,
    _verify_full_universe,
)
from src.services.sector_sw_market import SwSectorFetchError

SESSION_DATE = "2026-11-18"
SESSION_OPEN = datetime(2026, 11, 18, 9, 30)
SESSION_CLOSE = datetime(2026, 11, 18, 15, 0)
AFTER_CLOSE = datetime(2026, 11, 18, 23, 0, 0)   # 盘后接收（22:00 推送合法）
MID_SESSION = datetime(2026, 11, 18, 14, 0, 0)   # 盘中接收
L2_INDEX = "801951.SL"
TARGET = "600188"
TARGET_SEC = "600188.SH"
MEMBERS = ["000552.SZ", "000723.SZ", "600188.SH", "601225.SH", "601898.SH"]


def _budget(seconds: float = 120.0) -> ReportBudget:
    return ReportBudget(seconds, finish_margin=min(5.0, seconds / 2.0))


def _session() -> "svc.SessionContext":
    return svc.SessionContext(session_date=date(2026, 11, 18),
                              session_open=SESSION_OPEN,
                              session_close=SESSION_CLOSE)


def _l1_row(secucode: str, *, amount: Optional[float] = 5.0e8,
            change: Optional[float] = 1.0, stamp: Optional[datetime] = None,
            trade_date: str = SESSION_DATE) -> Dict[str, Any]:
    """一级已合格成员行（带交易所完整代码；供应商时间戳为行情时点）。"""
    latest = round(100.0 * (1 + (change or 0.0) / 100.0), 3)
    return {
        "thscode": secucode, "trade_date": trade_date,
        "trade_time": "22:00:46",
        "stamp": stamp or datetime(2026, 11, 18, 22, 0, 46),
        "change_pct": change, "amount": amount,
        "latest": latest, "pre_close": 100.0,
    }


def _wire_row(secucode: str, *, amount: Optional[float],
              change: Optional[float]) -> Dict[str, Any]:
    """已解析的成员行情行（fetch_index_quotes 出口形状，资格层输入）。"""
    latest = round(100.0 * (1 + (change or 0.0) / 100.0), 3)
    return {
        "thscode": secucode, "trade_date": SESSION_DATE,
        "trade_time": "22:00:46",
        "stamp": datetime(2026, 11, 18, 22, 0, 46),
        "change_pct": change, "amount": amount,
        "latest": latest, "pre_close": 100.0,
        "turnover": 2.0, "up_count": 20.0, "down_count": 10.0,
    }


def _run_fetch(monkeypatch, *, l1_quotes: Dict[str, Dict[str, Any]],
               wire_rows: Optional[List[Dict[str, Any]]] = None,
               quote_error: bool = False, received: datetime = AFTER_CLOSE,
               member_codes: Optional[List[str]] = None,
               ) -> tuple:
    """在受控缝下运行 ``_fetch_constituents``（二级审计模式 + 复用）。

    返回 ``(out, quote_calls)``：quote_calls 记录每次成员行情出站的代码
    批次（目标独立行情出站单独记录在 out["target_quote_gaps"] 之外）。
    """
    calls: List[List[str]] = []
    codes = member_codes or list(MEMBERS)

    def fake_fetch_members(index_code, observation_date, *, refresh_token,
                           timeout_seconds, deadline,
                           full_constituent_audit=False):
        assert full_constituent_audit is True, "二级阶段必须启用全量资格审计"
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

    def fake_fetch_quotes(codes_str, *, refresh_token, timeout_seconds,
                          deadline, indicators, raw_amount):
        batch = codes_str.split(",")
        calls.append(batch)
        if quote_error:
            raise SwSectorFetchError("受控传输失败")
        return [r for r in (wire_rows or [])
                if r.get("thscode") in set(batch)]

    monkeypatch.setattr(sw, "fetch_index_members", fake_fetch_members)
    monkeypatch.setattr(sw, "fetch_index_quotes", fake_fetch_quotes)
    monkeypatch.setattr(svc, "_received_at_now", lambda: received)
    out = _fetch_constituents(
        TARGET, L2_INDEX, SESSION_DATE, _session(), name="兖矿能源",
        refresh_token="sector-test-refresh-token", timeout_seconds=10.0,
        budget=_budget(), full_constituent_audit=True,
        reuse_member_quotes=l1_quotes or None)
    return out, calls


class TestFullReuseZeroQuoteBatches:
    """全复用：二级名单全部成员命中一级合格行情 → 零行情批次出站。"""

    def test_all_members_reused_no_quote_outbound(self, monkeypatch) -> None:
        l1_quotes = {c: _l1_row(c) for c in MEMBERS}
        out, calls = _run_fetch(monkeypatch, l1_quotes=l1_quotes)
        assert calls == []  # 零二级行情批次
        assert set(out["member_quotes"].keys()) == set(MEMBERS)
        # 原供应商行情时点保持（复用行是同一对象）
        for c in MEMBERS:
            assert out["member_quotes"][c] is l1_quotes[c]
        assert out["member_quote_gaps"] == []
        # 成员名单/成分计数照常独立核验（名单来自本次 p03473）
        assert out["membership"]["status"] == "included"


class TestPartialReuseFetchOnlyMissing:
    """部分复用：仅查缺项（补查批次只含缺失成员）。"""

    def test_only_missing_members_fetched(self, monkeypatch) -> None:
        keep = MEMBERS[:4]
        missing = MEMBERS[4]
        l1_quotes = {c: _l1_row(c) for c in keep}
        out, calls = _run_fetch(
            monkeypatch, l1_quotes=l1_quotes,
            wire_rows=[_wire_row(missing, amount=1.0e9, change=2.0)])
        assert len(calls) == 1
        assert calls[0] == [missing]  # 只补该成员
        assert out["member_quotes"][missing]["amount"] == 1.0e9
        assert out["member_quotes"][keep[0]] is l1_quotes[keep[0]]

    def test_target_independent_quote_not_reused(self, monkeypatch) -> None:
        """目标未确认纳入（不在二级名单）：目标独立行情保留既有独立查询
        顺序（不复用一级已合格行情）。"""
        members_without_target = [c for c in MEMBERS if c != TARGET_SEC]
        l1_quotes = {c: _l1_row(c) for c in members_without_target}
        l1_quotes[TARGET_SEC] = _l1_row(TARGET_SEC)  # 目标一级行情在复用源
        out, calls = _run_fetch(
            monkeypatch, l1_quotes=l1_quotes,
            wire_rows=[_wire_row(TARGET_SEC, amount=9.0e8, change=3.0)],
            member_codes=members_without_target)
        # 目标独立行情出站恰好一次（不复用）；成员零批次（全复用）
        assert len(calls) == 1
        assert calls[0] == [TARGET_SEC]
        assert out["target_quote"] is not None
        assert out["target_quote"]["thscode"] == TARGET_SEC
        assert set(out["member_quotes"].keys()) == set(members_without_target)


class TestRefetchFailureKeepsOldRows:
    """补查失败：保留仍满足本次会话时间资格的旧可用行，全量资格正确降范围。"""

    def _verify(self, constituents: Dict[str, Any],
                monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
        def fake_count(index_code, observation_date, *, refresh_token,
                       timeout_seconds, deadline):
            return {"index_code": index_code,
                    "observation_date": observation_date,
                    "count": len(MEMBERS), "fdate_echo": "20261118",
                    "gaps": []}

        monkeypatch.setattr(sw, "fetch_index_member_count", fake_count)
        return _verify_full_universe(
            L2_INDEX, SESSION_DATE, constituents=constituents,
            refresh_token="sector-test-refresh-token",
            timeout_seconds=10.0, budget=_budget())

    def test_batch_error_keeps_change_only_old_row_and_downgrades(
            self, monkeypatch) -> None:
        # 旧行仅涨跌幅可用（金额缺失）→ 纳入缺项补查；补查批次失败 →
        # 旧行保留（change 有、amount None）
        old = _l1_row(MEMBERS[4], amount=None, change=1.5)
        l1_quotes = {c: _l1_row(c) for c in MEMBERS[:4]}
        l1_quotes[MEMBERS[4]] = old
        out, calls = _run_fetch(monkeypatch, l1_quotes=l1_quotes,
                                quote_error=True)
        assert len(calls) == 1 and calls[0] == [MEMBERS[4]]
        kept = out["member_quotes"][MEMBERS[4]]
        assert kept is old
        assert kept["change_pct"] == 1.5 and kept["amount"] is None
        assert any("查询失败" in g for g in out["member_quote_gaps"])
        # 全量资格正确降范围：门槛 4（全体成员同日成交额状态可判定）拒绝
        verification = self._verify(out, monkeypatch)
        assert verification["verified"] is False
        assert verification["verified_member_count"] == len(MEMBERS)
        assert any("成交额状态不可判定" in g for g in verification["gaps"])

    def test_new_row_with_no_usable_fact_keeps_old_row(
            self, monkeypatch) -> None:
        """补查成功但新行两字段均不可用（资格合格的全空行）→ 不替换，
        保留旧可用行（不拼接、不用空行顶替已有事实）。"""
        old = _l1_row(MEMBERS[4], amount=None, change=1.5)
        l1_quotes = {c: _l1_row(c) for c in MEMBERS[:4]}
        l1_quotes[MEMBERS[4]] = old
        out, calls = _run_fetch(
            monkeypatch, l1_quotes=l1_quotes,
            wire_rows=[_wire_row(MEMBERS[4], amount=None, change=None)])
        assert len(calls) == 1
        assert out["member_quotes"][MEMBERS[4]] is old
        assert out["member_quotes"][MEMBERS[4]]["change_pct"] == 1.5

    def test_unqualified_new_row_missing_code_keeps_old_row(
            self, monkeypatch) -> None:
        """补查响应无该码（资格拒绝/缺失）→ 旧行保留、如实缺失不补造。"""
        old = _l1_row(MEMBERS[4], amount=None, change=1.5)
        l1_quotes = {c: _l1_row(c) for c in MEMBERS[:4]}
        l1_quotes[MEMBERS[4]] = old
        out, calls = _run_fetch(monkeypatch, l1_quotes=l1_quotes,
                                wire_rows=[])  # 响应无该成员行
        assert len(calls) == 1
        assert out["member_quotes"][MEMBERS[4]] is old

    def test_intraday_candidate_not_reused_after_close_and_refetched(
            self, monkeypatch) -> None:
        """收盘边界：一级盘中（14:00 接收）取到的 14:59 时间戳候选，在
        15:00 后（会话完结）不能复用——盘中旧报价不能冒充收盘后合格报价；
        只补该成员。"""
        intraday_old = _l1_row(
            MEMBERS[4], stamp=datetime(2026, 11, 18, 14, 59, 0))
        l1_quotes = {c: _l1_row(c) for c in MEMBERS[:4]}
        l1_quotes[MEMBERS[4]] = intraday_old
        # 接收在盘后（23:00）：14:59 < 收盘 → 不可复用
        out, calls = _run_fetch(
            monkeypatch, l1_quotes=l1_quotes, received=AFTER_CLOSE,
            wire_rows=[_wire_row(MEMBERS[4], amount=2.0e9, change=2.5)])
        assert len(calls) == 1 and calls[0] == [MEMBERS[4]]
        assert out["member_quotes"][MEMBERS[4]]["amount"] == 2.0e9

    def test_intraday_session_candidate_reused_within_session(
            self, monkeypatch) -> None:
        """同一盘中会话内编排未跨收盘：一级 10:30 行情在 14:00 接收钟复核
        （stamp ≥ 开盘）可复用，零补查批次。"""
        intraday_old = _l1_row(
            MEMBERS[4], stamp=datetime(2026, 11, 18, 10, 30, 0),
            amount=7.0e8)
        l1_quotes = {c: _l1_row(
            c, stamp=datetime(2026, 11, 18, 10, 30, 0)) for c in MEMBERS[:4]}
        l1_quotes[MEMBERS[4]] = intraday_old
        out, calls = _run_fetch(
            monkeypatch, l1_quotes=l1_quotes, received=MID_SESSION)
        assert calls == []
        assert out["member_quotes"][MEMBERS[4]] is intraday_old


class TestWholeRowReplacementSemantics:
    """整行替换门：新行有金额无涨跌幅 → 整行采用，不拼接旧涨跌幅。"""

    def test_amount_only_new_row_replaces_without_splicing(
            self, monkeypatch) -> None:
        old = _l1_row(MEMBERS[4], amount=None, change=1.5)
        l1_quotes = {c: _l1_row(c) for c in MEMBERS[:4]}
        l1_quotes[MEMBERS[4]] = old
        out, calls = _run_fetch(
            monkeypatch, l1_quotes=l1_quotes,
            wire_rows=[_wire_row(MEMBERS[4], amount=3.0e8, change=None)])
        assert len(calls) == 1
        new = out["member_quotes"][MEMBERS[4]]
        assert new is not old
        assert new["amount"] == 3.0e8
        assert new["change_pct"] is None  # 不拼接旧涨跌幅（跨时点拼接禁止）


class TestReuseBoundaries:
    """复用边界：名单外、同码异交易所、异日均不复用；真零与正额可复用。"""

    def test_same_bare_code_other_exchange_not_reused(
            self, monkeypatch) -> None:
        # 名单成员 600188.SZ（同裸码异交易所），复用源只有 600188.SH
        members = ["600188.SH", "600188.SZ"]
        l1_quotes = {TARGET_SEC: _l1_row(TARGET_SEC)}
        out, calls = _run_fetch(
            monkeypatch, l1_quotes=l1_quotes,
            wire_rows=[_wire_row("600188.SZ", amount=4.0e8, change=1.0)],
            member_codes=members)
        assert calls == [[c for c in members if c != TARGET_SEC]]
        assert out["member_quotes"][TARGET_SEC] is l1_quotes[TARGET_SEC]
        assert out["member_quotes"]["600188.SZ"]["amount"] == 4.0e8

    def test_off_date_row_not_reused(self, monkeypatch) -> None:
        old = _l1_row(MEMBERS[4], trade_date="2026-11-17")  # 异日行
        l1_quotes = {c: _l1_row(c) for c in MEMBERS[:4]}
        l1_quotes[MEMBERS[4]] = old
        out, calls = _run_fetch(
            monkeypatch, l1_quotes=l1_quotes,
            wire_rows=[_wire_row(MEMBERS[4], amount=2.0e9, change=2.0)])
        assert calls == [[MEMBERS[4]]]  # 异日不复用，照常补查
        assert out["member_quotes"][MEMBERS[4]]["amount"] == 2.0e9

    def test_zero_amount_quote_reused(self, monkeypatch) -> None:
        """同日报价真零（合法零观测）：可复用，不重复出站。"""
        l1_quotes = {c: _l1_row(c) for c in MEMBERS}
        l1_quotes[MEMBERS[4]] = _l1_row(MEMBERS[4], amount=0.0)
        out, calls = _run_fetch(monkeypatch, l1_quotes=l1_quotes)
        assert calls == []
        assert out["member_quotes"][MEMBERS[4]]["amount"] == 0.0

    def test_l1_diagnostics_not_inherited(self, monkeypatch) -> None:
        """一级临时失败的批次诊断不整体继承到二级：复用源只带合格行，
        二级缺口只含本次阶段自己的诊断。"""
        l1_quotes = {c: _l1_row(c) for c in MEMBERS}
        out, calls = _run_fetch(monkeypatch, l1_quotes=l1_quotes)
        assert calls == []
        assert out["member_quote_gaps"] == []
        assert not any("一级" in g or "批查询失败" in g
                       for g in out["member_quote_gaps"])
