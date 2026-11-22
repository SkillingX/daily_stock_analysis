# -*- coding: utf-8 -*-
"""二级同业与全成分成交额名次（issue #104）：核验门槛与名次发布语义。

测试缝（与既有板块测试约定一致）：

- ``_sw_peers_pillar``（纯计算，新 ``amount_rank_enabled`` 缝）：成交额
  名次字段的发布语义——全量资格（双接口核验一致 + 报价全集）解锁
  ``full_constituents`` 范围；核验不通过降级 ``qualified_sample`` 样本名次；
  并列金额同名次、已知零额不进正额分母；缺涨跌幅同行只 withhold 涨跌幅
  名次，不阻断成交额名次（验收 4）；开关关闭（一级既有口径）新字段恒
  None（追加兼容）。
- ``_verify_full_universe``（核验缝，mock 供应商计数 wire）：原始行审计
  （空日期/异日/畸形/冲突/表形状/回显冲突任一存在即拒绝——**表观计数
  吻合不豁免脏行**）、计数一致性、报价全集（缺额拒绝、合法零通过）。

不覆盖：L2 编排接线与预算门控（compose 层测试）、API 闭环（API 层测试）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from decimal import Decimal

import pytest

from src.services.sector_analysis_service import (
    ReportBudget,
    _sw_peers_pillar,
    _verify_full_universe,
)
from src.services.sector_sw_market import SwSectorFetchError


SESSION_DATE = "2026-11-20"
OBSERVED_AT = "2026-11-20T23:30:00"
INS_TARGET = "601318"
INS_TARGET_SEC = "601318.SH"
EVIDENCE_5 = (
    "iFinD 双接口核验一致：p03473 同日名单 5 只 = ths_constituent_num_index "
    "计数 5（FDATE 20261120 回显一致）；全体成员同日成交额状态可判定")


def _budget(seconds: float = 120.0) -> ReportBudget:
    return ReportBudget(seconds, finish_margin=min(5.0, seconds / 2.0))


def _member_row(secucode: str, change: Optional[float], amount: Optional[float],
                *, stamp: str = "2026-11-20 22:00:46") -> Dict[str, Any]:
    latest = round(100.0 * (1 + (change or 0.0) / 100.0), 3)
    return {
        "thscode": secucode, "trade_date": SESSION_DATE,
        "trade_time": "22:00:46",
        "stamp": datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S"),
        "change_pct": change, "latest": latest, "pre_close": 100.0,
        "amount": amount, "turnover": None, "up_count": None, "down_count": None,
    }


def _members_out(codes: List[str], *, evidence: Optional[str] = None,
                 gaps: Optional[List[str]] = None,
                 rejected_identity_rows: int = 0,
                 conflicting_code_members: int = 0,
                 blank_date_rows: int = 0,
                 off_date_rows: int = 0,
                 identity_echo_conflict: bool = False,
                 shape_gap_count: int = 0,
                 conflicting_name_rows: int = 0,
                 over_publish_limit: bool = False) -> Dict[str, Any]:
    return {
        "member_codes": list(codes),
        "member_names": {c: f"成员{c[:6]}" for c in codes},
        "dates_seen": [SESSION_DATE],
        "rows_total": (len(codes) + rejected_identity_rows
                       + blank_date_rows + off_date_rows),
        "completeness_evidence": evidence,
        "rejected_identity_rows": rejected_identity_rows,
        "conflicting_code_members": conflicting_code_members,
        "blank_date_rows": blank_date_rows,
        "off_date_rows": off_date_rows,
        "identity_echo_conflict": identity_echo_conflict,
        "shape_gap_count": shape_gap_count,
        "conflicting_name_rows": conflicting_name_rows,
        "over_publish_limit": over_publish_limit,
        "gaps": gaps or [],
    }


def _membership(status: str, target: str, *,
                evidence: Optional[str] = None,
                gaps: Optional[List[str]] = None) -> Dict[str, Any]:
    return {
        "status": status, "target_secucode": target,
        "observation_date": SESSION_DATE, "member_count": None,
        "completeness_evidence": evidence, "gaps": gaps or [],
    }


def _constituents(*, membership: Dict[str, Any], members: Dict[str, Any],
                  member_quotes: Optional[Dict[str, Dict[str, Any]]] = None,
                  quote_gaps: Optional[List[str]] = None,
                  target_quote: Optional[Dict[str, Any]] = None,
                  target_quote_gaps: Optional[List[str]] = None,
                  ) -> Dict[str, Any]:
    return {
        "membership": membership,
        "members": members,
        "member_quotes": member_quotes or {},
        "member_quote_gaps": quote_gaps or [],
        "target_quote": target_quote,
        "target_quote_gaps": target_quote_gaps or [],
        "target_name": "目标股名称",
    }


INS_CODES = ["601318.SH", "601319.SH", "601336.SH", "601601.SH", "601628.SH"]


def _ins_quotes(*, amounts: Optional[Dict[str, float]] = None,
                changes: Optional[Dict[str, Optional[float]]] = None,
                drop: Optional[List[str]] = None,
                ) -> Dict[str, Dict[str, Any]]:
    """保险Ⅱ 5 成员同日行情（中国平安 28.1 亿居首的量级关系）。"""
    amounts = amounts if amounts is not None else {
        "601318.SH": 2.81e9, "601319.SH": 3.12e8, "601336.SH": 4.32e8,
        "601601.SH": 6.26e8, "601628.SH": 3.07e8}
    changes = changes if changes is not None else {
        "601318.SH": -0.73, "601319.SH": -1.39, "601336.SH": -1.91,
        "601601.SH": -0.06, "601628.SH": -0.60}
    quotes: Dict[str, Dict[str, Any]] = {}
    for code in amounts:
        if drop and code in drop:
            continue
        quotes[code] = _member_row(code, changes.get(code), amounts.get(code))
    return quotes


def _ins_constituents(*, evidence: Optional[str] = None,
                      quotes: Optional[Dict[str, Dict[str, Any]]] = None,
                      quote_gaps: Optional[List[str]] = None,
                      members: Optional[Dict[str, Any]] = None,
                      ) -> Dict[str, Any]:
    return _constituents(
        membership=_membership("included", INS_TARGET_SEC, evidence=evidence),
        members=members if members is not None else _members_out(INS_CODES),
        member_quotes=quotes if quotes is not None else _ins_quotes(),
        quote_gaps=quote_gaps)


def _pillar(code: str, constituents: Dict[str, Any], *,
            amount_rank_enabled: bool = True) -> Dict[str, Any]:
    return _sw_peers_pillar(
        code, constituents=constituents,
        snapshot_observed_at=OBSERVED_AT, budget=_budget(),
        amount_rank_enabled=amount_rank_enabled)


# ---------------------------------------------------------------------------
# 成交额名次发布（支柱）
# ---------------------------------------------------------------------------


class TestAmountRankPublication:
    def test_disabled_by_default_keeps_l1_shape(self):
        """开关关闭（一级既有口径）：新字段恒 None，既有行为不变。"""
        out = _pillar(INS_TARGET, _ins_constituents(
            evidence=EVIDENCE_5), amount_rank_enabled=False)
        assert out["target_amount_rank"] is None
        assert out["amount_rank_denominator"] is None
        assert out["amount_rank_scope"] is None
        assert out["amount_rank_evidence"] is None
        # 既有字段照常
        assert out["eligible"] is True

    def test_full_universe_scope_with_evidence(self):
        """双接口核验一致 + 报价全集：全成分范围名次 + 核验证据透传。"""
        out = _pillar(INS_TARGET, _ins_constituents(evidence=EVIDENCE_5))
        # 28.1 亿居首：1 + 0 个严格更高 = 1；分母 = 5 只正额成员
        assert out["target_amount_rank"] == 1
        assert out["amount_rank_denominator"] == 5
        assert out["amount_rank_scope"] == "full_constituents"
        assert out["amount_rank_evidence"] == EVIDENCE_5

    def test_sample_scope_when_no_evidence(self):
        """核验证据缺失（计数缺失/不符/脏行后未写入）：样本范围名次。"""
        out = _pillar(INS_TARGET, _ins_constituents(evidence=None))
        assert out["target_amount_rank"] == 1
        assert out["amount_rank_denominator"] == 5
        assert out["amount_rank_scope"] == "qualified_sample"
        assert out["amount_rank_evidence"] is None

    def test_target_midfield_rank_sample_scope(self):
        """目标非榜首：样本内按严格更高数 + 1。"""
        quotes = _ins_quotes(amounts={
            "601318.SH": 3.0e8, "601319.SH": 3.12e8, "601336.SH": 4.32e8,
            "601601.SH": 6.26e8, "601628.SH": 3.07e8})
        out = _pillar(INS_TARGET, _ins_constituents(quotes=quotes))
        # 601601(6.26) > 601336(4.32) > 601319(3.12) > 601628(3.07) > 601318(3.0)
        # 目标第 5、在前五内 → picked = 前 5（无附加），样本分母 5
        assert out["target_amount_rank"] == 5
        assert out["amount_rank_denominator"] == 5
        assert out["items"][0]["code"] == "601601"

    def test_sample_scope_denominator_is_picked_not_all_rankable(self):
        """反馈 1 反例：28 只正额、计数不可得、目标第 10——样本范围名次是
        入选样本（前五 + 目标附加）内的 6/6，不是 10/28（五个隐藏名次）。"""
        codes = [f"60{i:04d}.SH" for i in range(100, 128)]  # 28 只正额成员
        codes[9] = INS_TARGET_SEC  # 目标在全体正额中排第 10
        amounts = {c: 5.0e8 - i * 1.0e6 for i, c in enumerate(codes)}
        amounts[INS_TARGET_SEC] = 5.0e8 - 9 * 1.0e6  # 全体第 10 高
        quotes = {c: _member_row(c, 1.0, amounts[c]) for c in codes}
        members = _members_out(codes)
        out = _pillar(INS_TARGET, _ins_constituents(
            quotes=quotes, members=members, evidence=None))
        assert out["target_amount_rank"] == 6
        assert out["amount_rank_denominator"] == 6
        assert out["amount_rank_scope"] == "qualified_sample"
        # 样本条目仍是 6 家（前五 + 目标附加）
        assert out["sample_size"] == 6

    def test_amount_rank_is_ordinal_tie_broken_by_code(self):
        """正额同值：名次按排序契约（降序、同额代码升序）给**顺序名次**
        （不并列）——并列首位的后位代码得 2；涨跌幅名次才并列同名次。"""
        quotes = _ins_quotes(amounts={
            "601318.SH": 5.0e8, "601319.SH": 5.0e8, "601336.SH": 3.0e8,
            "601601.SH": 2.0e8, "601628.SH": 1.0e8})
        out = _pillar(INS_TARGET, _ins_constituents(quotes=quotes))
        # 601318 与 601319 同额最高：601318 代码更小排第 1，目标（601318）
        # 得 1；若目标为 601319 则应得 2。此处目标 601318 code < 601319
        assert out["target_amount_rank"] == 1
        assert out["amount_rank_denominator"] == 5

    def test_amount_tie_later_code_gets_ordinal_two(self):
        """并列首位的后位代码（目标）得顺序名次 2，不是并列 1。"""
        constituents = _ins_constituents(evidence=EVIDENCE_5)
        # 目标 601318 与 601319 同额 5.0e8：排序 (降额, 升码) →
        # 601318 先于 601319；把目标换成 601319（代码更大）验证得 2
        codes = ["601319.SH", "601318.SH", "601336.SH", "601601.SH",
                 "601628.SH"]
        members = _members_out(codes)
        quotes = _ins_quotes(amounts={
            "601318.SH": 5.0e8, "601319.SH": 5.0e8, "601336.SH": 3.0e8,
            "601601.SH": 2.0e8, "601628.SH": 1.0e8})
        constituents = _constituents(
            membership=_membership("included", "601319.SH",
                                   evidence=EVIDENCE_5),
            members=members, member_quotes=quotes)
        out = _sw_peers_pillar("601319", constituents=constituents,
                               snapshot_observed_at=OBSERVED_AT,
                               budget=_budget(), amount_rank_enabled=True)
        assert out["target_amount_rank"] == 2
        assert out["amount_rank_denominator"] == 5

    def test_amount_ties_order_by_code_within_sample(self):
        """正额同值：展示序按代码升序（spec 排序契约）；目标样本名次按
        顺序位（非并列）。"""
        quotes = _ins_quotes(amounts={
            "601318.SH": 2.0e8, "601319.SH": 5.0e8, "601336.SH": 5.0e8,
            "601601.SH": 5.0e8, "601628.SH": 1.0e8})
        out = _pillar(INS_TARGET, _ins_constituents(quotes=quotes))
        # 三只并列 5.0e8 按代码升序占 1-3；目标 2.0e8 顺序位 4；1.0e8 第 5
        assert out["target_amount_rank"] == 4
        assert out["amount_rank_denominator"] == 5
        top_codes = [i["code"] for i in out["items"][:3]]
        assert top_codes == sorted(["601319", "601336", "601601"])

    def test_zero_amount_excluded_from_rank_and_denominator(self):
        """已知零额成员：计入报价覆盖（报价全集不破），不进正额分母/名次。"""
        quotes = _ins_quotes(amounts={
            "601318.SH": 2.81e9, "601319.SH": 3.12e8, "601336.SH": 4.32e8,
            "601601.SH": 6.26e8, "601628.SH": 0.0})
        out = _pillar(INS_TARGET, _ins_constituents(
            quotes=quotes, evidence=EVIDENCE_5))
        assert out["target_amount_rank"] == 1
        # 分母只含 4 只正额成员（零额计入覆盖但不在正额范围）
        assert out["amount_rank_denominator"] == 4
        assert out["amount_rank_scope"] == "full_constituents"

    def test_zero_amount_target_not_ranked(self):
        """已知零额目标：不参与正额名次（None + 缺口），前五资格 False。"""
        quotes = _ins_quotes(amounts={
            "601318.SH": 0.0, "601319.SH": 3.12e8, "601336.SH": 4.32e8,
            "601601.SH": 6.26e8, "601628.SH": 3.07e8})
        out = _pillar(INS_TARGET, _ins_constituents(
            quotes=quotes, evidence=EVIDENCE_5))
        assert out["target_amount_rank"] is None
        assert out["amount_rank_denominator"] is None
        assert out["amount_rank_scope"] is None
        assert out["target_in_amount_top5"] is False
        assert any("零" in g for g in out["gaps"])

    def test_missing_change_member_withholds_change_only(self):
        """验收 4：一只成员缺同日涨跌幅——涨跌幅名次 withhold 给有效覆盖，
        已符合资格的全成分成交额名次照常发布。"""
        quotes = _ins_quotes(changes={
            "601318.SH": -0.73, "601319.SH": None, "601336.SH": -1.91,
            "601601.SH": -0.06, "601628.SH": -0.60})
        out = _pillar(INS_TARGET, _ins_constituents(
            quotes=quotes, evidence=EVIDENCE_5))
        assert out["target_amount_rank"] == 1
        assert out["amount_rank_scope"] == "full_constituents"
        assert out["target_change_pct_rank"] is None
        assert any("涨跌幅缺失" in g and "有效比较范围" in g for g in out["gaps"])

    def test_target_missing_quote_no_rank(self):
        """目标缺同日行情（批次缺失）：无成交额名次，成员资格保留。"""
        quotes = _ins_quotes(drop=["601318.SH"])
        out = _pillar(INS_TARGET, _ins_constituents(
            quotes=quotes, evidence=EVIDENCE_5))
        assert out["target_amount_rank"] is None
        assert out["amount_rank_denominator"] is None
        assert out["target_in_amount_top5"] is None
        assert any("601318" in g for g in out["gaps"])

    def test_qualification_gaps_carry_precise_failure(self):
        """反馈 3：核验精确缺口经 qualification_gaps 进入块缺口（替代泛化
        资格缺口，不重复证据）。"""
        out = _sw_peers_pillar(
            INS_TARGET, constituents=_ins_constituents(evidence=None),
            snapshot_observed_at=OBSERVED_AT, budget=_budget(),
            amount_rank_enabled=True,
            qualification_gaps=["二级同业全量核验未通过：计数不一致：供应商"
                                "计数 4 ≠ 同日名单唯一成员数 5，拒绝全成分资格"])
        assert out["amount_rank_scope"] == "qualified_sample"
        assert any("计数不一致" in g for g in out["gaps"])
        # 泛化资格缺口被精确缺口替代（不并存重复）
        assert not any("全成分成交额前五资格需要成分全集证据与报价全集"
                       "同时可证" in g for g in out["gaps"])

    def test_industry_reference_no_amount_rank(self):
        """目标未确认纳入（行业参考）：名次字段组全 None（成分内名次契约）。"""
        constituents = _constituents(
            membership=_membership("unknown", INS_TARGET_SEC),
            members=_members_out(INS_CODES),
            member_quotes=_ins_quotes(),
            target_quote=_member_row(INS_TARGET_SEC, -0.73, 2.81e9))
        out = _pillar(INS_TARGET, constituents)
        assert out["industry_reference"] is True
        assert out["target_amount_rank"] is None
        assert out["amount_rank_denominator"] is None
        assert out["amount_rank_scope"] is None
        assert out["amount_rank_evidence"] is None
        assert out["target_independent_quote"] is not None


# ---------------------------------------------------------------------------
# 全量资格核验（_verify_full_universe）
# ---------------------------------------------------------------------------


class _CountStub:
    """替换计数 wire 缝：可预置成功结果或异常。"""

    def __init__(self, result: Optional[Dict[str, Any]],
                 error: Optional[str] = None) -> None:
        self.result = result
        self.error = error
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, index_code: str, observation_date: str, *,
                 refresh_token: str, timeout_seconds: float,
                 deadline: Optional[float]) -> Dict[str, Any]:
        self.calls.append({"index_code": index_code,
                           "observation_date": observation_date})
        if self.error:
            raise SwSectorFetchError(self.error)
        assert self.result is not None
        return self.result


@pytest.fixture()
def count_stub(monkeypatch):
    def _install(result: Optional[Dict[str, Any]],
                 error: Optional[str] = None) -> _CountStub:
        stub = _CountStub(result, error)
        monkeypatch.setattr(
            "src.services.sector_sw_market.fetch_index_member_count", stub)
        return stub
    return _install


def _clean_members() -> Dict[str, Any]:
    return _members_out(INS_CODES)


def _verify(constituents: Dict[str, Any]) -> Dict[str, Any]:
    return _verify_full_universe(
        "801194.SL", SESSION_DATE, constituents=constituents,
        refresh_token="sector-test-refresh-token", timeout_seconds=10.0,
        budget=_budget())


class TestVerifyFullUniverse:
    def _count_ok(self, count: int = 5) -> Dict[str, Any]:
        return {"index_code": "801194.SL", "observation_date": SESSION_DATE,
                "count": count, "fdate_echo": "20261120", "gaps": []}

    def test_clean_lists_and_matching_count_verified(self, count_stub) -> None:
        """干净名单 + 计数一致 + 报价全集：核验通过，证据写入 membership。"""
        stub = count_stub(self._count_ok(5))
        constituents = _ins_constituents(evidence=None)
        out = _verify(constituents)
        assert out["verified"] is True
        assert out["vendor_count"] == 5
        assert out["evidence"] is not None
        assert "双接口" in out["evidence"]
        # 单指数绑定：计数请求只携带本指数与同一交易日
        assert stub.calls == [{"index_code": "801194.SL",
                               "observation_date": SESSION_DATE}]
        assert constituents["membership"]["completeness_evidence"] == \
            out["evidence"]

    def test_blank_extra_row_same_apparent_count_rejected(self, count_stub) -> None:
        """验收 3 反例：名单 5 条合格 + 1 条空日期，计数恰为 5——表观吻合
        不豁免脏行，拒绝全量资格，证据不写入。"""
        count_stub(self._count_ok(5))
        members = _members_out(INS_CODES, blank_date_rows=1)
        constituents = _ins_constituents(members=members)
        out = _verify(constituents)
        assert out["verified"] is False
        assert out["evidence"] is None
        assert any("空日期" in g for g in out["gaps"])
        assert constituents["membership"]["completeness_evidence"] is None

    def test_off_date_extra_row_rejected(self, count_stub) -> None:
        count_stub(self._count_ok(5))
        members = _members_out(INS_CODES, off_date_rows=1)
        out = _verify(_ins_constituents(members=members))
        assert out["verified"] is False
        assert any("异日" in g or "非观察日" in g for g in out["gaps"])

    def test_malformed_extra_row_rejected(self, count_stub) -> None:
        count_stub(self._count_ok(5))
        members = _members_out(INS_CODES, rejected_identity_rows=1)
        out = _verify(_ins_constituents(members=members))
        assert out["verified"] is False
        assert any("身份格式不合格" in g for g in out["gaps"])

    def test_conflicting_extra_row_rejected(self, count_stub) -> None:
        count_stub(self._count_ok(5))
        members = _members_out(INS_CODES, conflicting_code_members=1)
        out = _verify(_ins_constituents(members=members))
        assert out["verified"] is False
        assert any("矛盾" in g for g in out["gaps"])

    def test_shape_gap_table_rejected(self, count_stub) -> None:
        count_stub(self._count_ok(5))
        members = _members_out(INS_CODES, shape_gap_count=1,
                               gaps=["成分响应列形状异常（p03473 三列不对齐），"
                                     "该表不采用（不按位置猜值）"])
        out = _verify(_ins_constituents(members=members))
        assert out["verified"] is False
        assert any("形状" in g for g in out["gaps"])

    def test_identity_echo_conflict_rejected(self, count_stub) -> None:
        count_stub(self._count_ok(5))
        members = _members_out(INS_CODES, identity_echo_conflict=True,
                               gaps=["成分响应表回显身份（801790.SL）与请求"
                                     "绑定指数 801194.SL 冲突，身份绑定不可信"])
        out = _verify(_ins_constituents(members=members))
        assert out["verified"] is False
        assert any("回显" in g for g in out["gaps"])

    def test_conflicting_duplicate_names_rejected(self, count_stub) -> None:
        """同码重复行名称矛盾：供应商对同一身份给出矛盾事实，拒绝全量资格
        （一致重复不受影响）。"""
        count_stub(self._count_ok(5))
        members = _members_out(INS_CODES, conflicting_name_rows=1,
                               gaps=["成分响应 1 行同码重复名称矛盾（计数披露），"
                                     "不采用"])
        out = _verify(_ins_constituents(members=members))
        assert out["verified"] is False
        assert any("名称矛盾" in g for g in out["gaps"])

    def test_over_publish_limit_rejected(self, count_stub) -> None:
        count_stub(self._count_ok(5))
        members = _members_out(INS_CODES, over_publish_limit=True,
                               gaps=["成分响应 20001 行超出可发布成员上限"])
        out = _verify(_ins_constituents(members=members))
        assert out["verified"] is False
        assert any("上限" in g for g in out["gaps"])

    def test_count_query_failure_rejected_with_reason(self, count_stub) -> None:
        stub = count_stub(None, error="errorcode=-1 errmsg=mock 失败")
        out = _verify(_ins_constituents())
        assert out["verified"] is False
        assert out["vendor_count"] is None
        assert any("mock 失败" in g for g in out["gaps"])
        assert stub.calls[0]["index_code"] == "801194.SL"

    def test_count_mismatch_rejected(self, count_stub) -> None:
        """计数 4 ≠ 同日唯一成员数 5：一致性不成立，拒绝。"""
        count_stub(self._count_ok(4))
        out = _verify(_ins_constituents())
        assert out["verified"] is False
        assert out["vendor_count"] == 4
        assert any("4" in g and "5" in g for g in out["gaps"])

    def test_quote_gap_missing_rows_rejected(self, count_stub) -> None:
        """批次报价失败的实质是成员缺行（issue #105 复审 P2：资格按名单
        内实际全集判定，不再按缺口字符串提前拒绝）——整批缺失即全体
        成交额状态不可判定，拒绝。"""
        count_stub(self._count_ok(5))
        constituents = _ins_constituents(
            quotes=_ins_quotes(drop=INS_CODES),
            quote_gaps=["成员行情第 1/1 批查询失败（HTTP 超时），该批 5 只"
                        "成员行情缺失"])
        out = _verify(constituents)
        assert out["verified"] is False
        assert any("成交额" in g for g in out["gaps"])

    def test_out_of_batch_noise_gap_keeps_full_universe(
            self, count_stub) -> None:
        """名单外干扰行（已被资格过滤隔离）：诊断随 quote_gaps 透出，
        名单内全体成员金额到手可判定 → 全量资格不因干扰行拒绝
        （issue #105 复审 P2）。"""
        count_stub(self._count_ok(5))
        constituents = _ins_constituents(
            quote_gaps=["成员行情第 1/1 批响应含 1 个非本批请求身份的行，"
                        "不采用、不覆盖先批已采用行（原始值不回显）"])
        out = _verify(constituents)
        assert out["verified"] is True

    def test_missing_member_quote_rejected(self, count_stub) -> None:
        """一只成员无行情行：全体状态不可判定，拒绝（零额豁免不适用缺失）。"""
        count_stub(self._count_ok(5))
        constituents = _ins_constituents(quotes=_ins_quotes(
            drop=["601628.SH"]))
        out = _verify(constituents)
        assert out["verified"] is False
        assert any("601628" in g for g in out["gaps"])

    def test_zero_amount_passes_quote_coverage(self, count_stub) -> None:
        """合法零额：成交额状态可判定（已知零），不阻断全量资格。"""
        count_stub(self._count_ok(5))
        constituents = _ins_constituents(quotes=_ins_quotes(
            amounts={"601318.SH": 2.81e9, "601319.SH": 3.12e8,
                     "601336.SH": 4.32e8, "601601.SH": 6.26e8,
                     "601628.SH": 0.0}))
        out = _verify(constituents)
        assert out["verified"] is True

    def test_target_not_included_rejected(self, count_stub) -> None:
        """目标未确认在名单（unknown）：无目标名次语境，拒绝全量资格。"""
        count_stub(self._count_ok(5))
        constituents = _constituents(
            membership=_membership("unknown", INS_TARGET_SEC),
            members=_clean_members(), member_quotes=_ins_quotes(),
            target_quote=_member_row(INS_TARGET_SEC, -0.73, 2.81e9))
        out = _verify(constituents)
        assert out["verified"] is False
        assert any("成分" in g for g in out["gaps"])

    def test_empty_member_list_rejected(self, count_stub) -> None:
        count_stub(self._count_ok(0))
        members = _members_out([], gaps=["成分响应共 3 行但无 2026-11-20 同日"
                                         "合格行，未取得该日成分"])
        out = _verify(_ins_constituents(members=members))
        assert out["verified"] is False


# ---------------------------------------------------------------------------
# issue #104 复审：行情覆盖与排名分母分开、涨跌幅覆盖一次统计、
# 已验证成分数独立表达
# ---------------------------------------------------------------------------


class TestReviewCoverageFields:
    """复审字段语义（`valid_quote_rows` / `change_pct_qualified_rows` /
    `verified_constituent_num`，追加兼容；一级既有口径不变）。"""

    def test_valid_quote_rows_counts_positive_plus_known_zero(self) -> None:
        """复审样例：四正一已知零全覆盖——有效报价 5、正额可排名 4、
        名次正额分母 4（覆盖与分母分开表达）。"""
        quotes = _ins_quotes(amounts={
            "601318.SH": 2.81e9, "601319.SH": 3.12e8, "601336.SH": 4.32e8,
            "601601.SH": 6.26e8, "601628.SH": 0.0})
        out = _pillar(INS_TARGET, _ins_constituents(
            evidence=EVIDENCE_5, quotes=quotes))
        assert out["snapshot_rows"] == 5
        assert out["valid_quote_rows"] == 5
        assert out["rankable_rows"] == 4
        assert out["amount_rank_denominator"] == 4
        assert out["target_amount_rank"] == 1

    def test_valid_quote_rows_excludes_missing_quotes(self) -> None:
        """缺额/缺行情成员不计入覆盖（未知 ≠ 已知零），缺口承载。"""
        quotes = _ins_quotes(drop=["601628.SH"])
        out = _pillar(INS_TARGET, _ins_constituents(quotes=quotes))
        assert out["valid_quote_rows"] == 4  # 4 正 + 0 已知零
        assert any("缺失" in g or "未取得" in g for g in out["gaps"])

    def test_change_pct_coverage_when_target_missing_change(self) -> None:
        """复审样例：五个入选、四个合格、目标缺幅——名次不发布，
        有效涨跌幅覆盖 4/5 两个表面可见（一次统计）。"""
        changes = {"601318.SH": None, "601319.SH": -1.39,
                   "601336.SH": -1.91, "601601.SH": -0.06,
                   "601628.SH": -0.60}
        quotes = _ins_quotes(changes=changes)
        out = _pillar(INS_TARGET, _ins_constituents(
            evidence=EVIDENCE_5, quotes=quotes))
        assert out["target_change_pct_rank"] is None
        assert out["sample_size"] == 5
        assert out["change_pct_qualified_rows"] == 4

    def test_change_pct_coverage_target_only_sample(self) -> None:
        """只有目标（全员零额无正额代表）：覆盖 1/1 如实（目标自身有幅）。"""
        zero = {c: 0.0 for c in INS_CODES}
        quotes = _ins_quotes(amounts=zero)
        out = _pillar(INS_TARGET, _ins_constituents(
            evidence=EVIDENCE_5, quotes=quotes))
        assert out["sample_size"] == 1
        assert out["target_amount_rank"] is None  # 已知零额目标不参与正额名次
        assert out["change_pct_qualified_rows"] == 1

    def test_change_pct_coverage_reference_sample(self) -> None:
        """行业参考样本（目标未确认成分）：纯成分前五的覆盖事实照常统计。"""
        constituents = _constituents(
            membership=_membership("unknown", "920101.BJ"),
            members=_members_out(INS_CODES),
            member_quotes=_ins_quotes(),
            target_quote=_member_row(INS_TARGET_SEC, -0.73, 2.81e9))
        out = _pillar(INS_TARGET, constituents)
        assert out["industry_reference"] is True
        assert out["sample_size"] == 5
        assert out["change_pct_qualified_rows"] == 5

    def test_verified_constituent_num_survives_quote_failure(self) -> None:
        """行情失败（门槛 4 拒）：名次降级样本范围，已核实成分数照发。"""
        quotes = _ins_quotes(drop=["601628.SH"])
        stage_gaps = ["二级同业全量核验未通过：1 只成员同日成交额状态不可判定"]
        out = _sw_peers_pillar(
            INS_TARGET, constituents=_ins_constituents(quotes=quotes),
            snapshot_observed_at=OBSERVED_AT, budget=_budget(),
            amount_rank_enabled=True, qualification_gaps=stage_gaps,
            verified_constituent_num=5)
        assert out["amount_rank_scope"] == "qualified_sample"
        assert out["amount_rank_evidence"] is None
        assert out["verified_constituent_num"] == 5

    def test_zero_amount_target_keeps_verified_constituent_num(self) -> None:
        """成员等数且目标为零额：名次组不发布，已验证成分数照发（核验
        事实与才允许发布的全量名次分别表达）。"""
        quotes = _ins_quotes(amounts={
            "601318.SH": 0.0, "601319.SH": 3.12e8, "601336.SH": 4.32e8,
            "601601.SH": 6.26e8, "601628.SH": 3.07e8})
        out = _sw_peers_pillar(
            INS_TARGET, constituents=_ins_constituents(
                evidence=EVIDENCE_5, quotes=quotes),
            snapshot_observed_at=OBSERVED_AT, budget=_budget(),
            amount_rank_enabled=True, verified_constituent_num=5)
        assert out["target_amount_rank"] is None
        assert out["amount_rank_denominator"] is None
        assert out["amount_rank_scope"] is None
        assert out["verified_constituent_num"] == 5

    def test_l1_scope_keeps_rank_and_verification_fields_none(self) -> None:
        """一级既有口径不变（issue #105 修复）：不启用名次时不传核验数——
        名次组、verified_constituent_num 与 #104 覆盖统计
        （valid_quote_rows/change_pct_qualified_rows）恒 None——一级不发布
        #104 的统计结果，旧键契约 schema 兼容默认 None。"""
        out = _sw_peers_pillar(
            INS_TARGET, constituents=_ins_constituents(evidence=EVIDENCE_5),
            snapshot_observed_at=OBSERVED_AT, budget=_budget())
        assert out["target_amount_rank"] is None
        assert out["amount_rank_scope"] is None
        assert out["verified_constituent_num"] is None
        assert out["valid_quote_rows"] is None
        assert out["change_pct_qualified_rows"] is None
        # 旧键（基线一级既有）照常发布
        assert out["rankable_rows"] == 5

    @staticmethod
    def _count_ok(count: int = 5) -> Dict[str, Any]:
        return {"index_code": "801194.SL", "observation_date": SESSION_DATE,
                "count": count, "fdate_echo": "20261120", "gaps": []}

    def test_verified_member_count_independent_of_quote_gate(
            self, count_stub) -> None:
        """核验返回：门槛 3 过、门槛 4 拒——verified=False 但
        verified_member_count=5（已核实名单事实与报价状态独立）。"""
        count_stub(self._count_ok(5))
        quotes = _ins_quotes(drop=["601628.SH"])
        out = _verify(_ins_constituents(quotes=quotes))
        assert out["verified"] is False
        assert out["verified_member_count"] == 5

    def test_verified_member_count_none_before_count_gate(
            self, count_stub) -> None:
        """门槛 3 之前拒绝（脏行/未纳入）：无已核实成分数（None）。"""
        count_stub(self._count_ok(5))
        members = _members_out(INS_CODES, blank_date_rows=1)
        out = _verify(_ins_constituents(members=members))
        assert out["verified"] is False
        assert out["verified_member_count"] is None

    def test_verified_member_count_on_success(self, count_stub) -> None:
        """核验全过：verified_member_count 等于唯一成员数（与名次发布独立）。"""
        count_stub(self._count_ok(5))
        out = _verify(_ins_constituents(evidence=None))
        assert out["verified"] is True
        assert out["verified_member_count"] == 5


class TestAmountRankPrecisionOrdering:
    """issue #104 四轮：成交额排序用 ``Decimal.copy_negate()`` 精确降序。

    反例（修复前）：目标 600000.SH 金额 ``100000000.000000000000000000001``、
    600001-600005 各 ``100000000.000000000000000000002``（更高）——上下文
    精度的 ``-amount`` 取负把 38 位有效数字差截断成同值：全量 6/6 却
    ``target_in_amount_top5=True``，样本级应选五只更高同行加目标（6/6）
    却只剩 5/5。金额经 wire 准入（``_decimal_amount`` str→Decimal）后仍是
    精确 Decimal，排序不得引入上下文舍入。"""

    LO = Decimal("100000000.000000000000000000001")
    HI = Decimal("100000000.000000000000000000002")

    def _precision_constituents(self, *, evidence: Optional[str]) -> Dict[str, Any]:
        codes = ["600000.SH", "600001.SH", "600002.SH", "600003.SH",
                 "600004.SH", "600005.SH"]
        amounts = {"600000.SH": self.LO}
        amounts.update({f"60000{i}.SH": self.HI for i in range(1, 6)})
        quotes = {c: {"thscode": c, "trade_date": SESSION_DATE,
                      "trade_time": "22:00:46", "change_pct": 1.0,
                      "amount": amounts[c]} for c in codes}
        return _constituents(
            membership=_membership("included", "600000.SH", evidence=evidence),
            members=_members_out(codes),
            member_quotes=quotes)

    def test_full_universe_target_rank_six_not_in_top5(self) -> None:
        """全量 6/6：五只更高同行在前五，目标第 6、不在 top5。"""
        out = _sw_peers_pillar(
            "600000", constituents=self._precision_constituents(
                evidence=EVIDENCE_5),
            snapshot_observed_at=OBSERVED_AT, budget=_budget(),
            amount_rank_enabled=True)
        assert out["target_in_amount_top5"] is False
        assert out["target_amount_rank"] == 6
        assert out["amount_rank_denominator"] == 6
        assert out["amount_rank_denominator"] == out["rankable_rows"]
        assert out["amount_rank_scope"] == "full_constituents"
        # 样本前五 = 五只更高同行（金额降序，同额按码升序）
        assert [i["code"] for i in out["items"][:5]] == [
            "600001", "600002", "600003", "600004", "600005"]
        assert out["sample_size"] == 6

    def test_sample_scope_selects_all_five_higher_peers_plus_target(self) -> None:
        """样本级 6/6：前五=五只更高同行、目标第 6 加入，样本名次 6/6。"""
        out = _sw_peers_pillar(
            "600000", constituents=self._precision_constituents(evidence=None),
            snapshot_observed_at=OBSERVED_AT, budget=_budget(),
            amount_rank_enabled=True)
        assert out["amount_rank_scope"] == "qualified_sample"
        assert out["sample_size"] == 6
        assert [i["code"] for i in out["items"][:5]] == [
            "600001", "600002", "600003", "600004", "600005"]
        assert out["target_amount_rank"] == 6
        assert out["amount_rank_denominator"] == 6
