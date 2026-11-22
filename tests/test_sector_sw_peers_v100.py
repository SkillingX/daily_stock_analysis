# -*- coding: utf-8 -*-
"""申万口径同业比较支柱（issue #100）：成分边界三态下的选取与名次资格。

测试缝：``_sw_peers_pillar``（纯计算）。输入为受控成分事实（wire 形状）、
成分边界判定输出、成员同日合格行情与目标独立行情；输出为
``SectorPeersBlock`` 兼容 dict。测试内容：

- included：目标股并入成交额前五样本（前五去重 / 第六家附加）；
- 全成分成交额前五资格：成分全集证据与报价全集**同时**可证才判定，
  缺一为 None（不冒充全体排名）；
- excluded/unknown：同业为纯成分前五并标注行业参考，成分内名次留空，
  目标股独立行情只进独立事实位（不混入 items 与排名）；
- 部分报价缺失：有效范围披露、样本内名次 withhold；零额成员保留但不进
  活跃前五；剥后缀后同码冲突整组剔除；行情时点来自供应商时间戳。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

import pytest

from src.services.sector_analysis_service import (
    ReportBudget,
    _sw_peers_pillar,
)


SESSION_DATE = "2026-11-18"
OBSERVED_AT = "2026-11-18T23:10:00"


def _budget(seconds: float = 120.0) -> ReportBudget:
    return ReportBudget(seconds, finish_margin=min(5.0, seconds / 2.0))


def _member_row(secucode: str, change: Optional[float], amount: Optional[float],
                *, stamp: str = "2026-11-18 22:00:46") -> Dict[str, Any]:
    latest = round(100.0 * (1 + (change or 0.0) / 100.0), 3)
    return {
        "thscode": secucode, "trade_date": SESSION_DATE,
        "trade_time": "22:00:46", "stamp": datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S"),
        "change_pct": change, "latest": latest, "pre_close": 100.0,
        "amount": amount, "turnover": None, "up_count": None, "down_count": None,
    }


def _members_out(codes: List[str], *, evidence: Optional[str] = None,
                 gaps: Optional[List[str]] = None,
                 rejected_identity_rows: int = 0,
                 conflicting_code_members: int = 0) -> Dict[str, Any]:
    return {
        "member_codes": list(codes),
        "member_names": {c: f"成员{c[:6]}" for c in codes},
        "dates_seen": [SESSION_DATE],
        "rows_total": len(codes) + rejected_identity_rows,
        "completeness_evidence": evidence,
        "rejected_identity_rows": rejected_identity_rows,
        "conflicting_code_members": conflicting_code_members,
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
    }


COAL_TARGET = "600188"
COAL_TARGET_SEC = "600188.SH"


def _coal_members() -> Dict[str, Any]:
    return _members_out(["000552.SZ", "000723.SZ", "600188.SH", "601225.SH",
                         "601898.SH", "002128.SZ"])


def _coal_quotes(amounts: Optional[Dict[str, float]] = None,
                 changes: Optional[Dict[str, float]] = None,
                 *,
                 drop: Optional[List[str]] = None,
                 ) -> Dict[str, Dict[str, Any]]:
    amounts = amounts if amounts is not None else {
        "000552.SZ": 3.0e8, "000723.SZ": 5.0e8, "600188.SH": 9.0e8,
        "601225.SH": 7.0e8, "601898.SH": 1.0e9, "002128.SZ": 2.0e8}
    changes = changes if changes is not None else {
        "000552.SZ": 1.1, "000723.SZ": -0.4, "600188.SH": -0.87,
        "601225.SH": 2.2, "601898.SH": 0.5, "002128.SZ": -1.3}
    quotes: Dict[str, Dict[str, Any]] = {}
    for code in amounts:
        if drop and code in drop:
            continue
        quotes[code] = _member_row(code, changes.get(code),
                                   amounts.get(code))
    return quotes


def _pillar(code: str, constituents: Dict[str, Any]) -> Dict[str, Any]:
    return _sw_peers_pillar(
        code, constituents=constituents,
        snapshot_observed_at=OBSERVED_AT, budget=_budget(),
    )


# ---------------------------------------------------------------------------
# included：目标股并入样本
# ---------------------------------------------------------------------------


class TestIncludedTarget:
    def _included_constituents(self, *, evidence: Optional[str] = None,
                               quotes: Optional[Dict[str, Dict[str, Any]]] = None,
                               quote_gaps: Optional[List[str]] = None,
                               members: Optional[Dict[str, Any]] = None):
        members = members if members is not None else _coal_members()
        membership = _membership("included", COAL_TARGET_SEC, evidence=evidence)
        return _constituents(
            membership=membership, members=members,
            member_quotes=quotes if quotes is not None else _coal_quotes(),
            quote_gaps=quote_gaps)

    def test_target_merged_into_amount_top5(self):
        out = _pillar(COAL_TARGET, self._included_constituents())
        assert out["eligible"] is True
        codes = [item["code"] for item in out["items"]]
        # 成交额降序前五：601898(1e9) > 600188(9e8) > 601225(7e8) > 000723(5e8) > 000552(3e8)
        assert codes == ["601898", "600188", "601225", "000723", "000552"]
        assert out["sample_size"] == 5
        target = next(i for i in out["items"] if i["is_target"])
        assert target["code"] == COAL_TARGET

    def test_target_outside_top5_appended_as_sixth(self):
        quotes = _coal_quotes(amounts={"000552.SZ": 3.0e8, "000723.SZ": 5.0e8,
                                       "600188.SH": 1.0e7, "601225.SH": 7.0e8,
                                       "601898.SH": 1.0e9, "002128.SZ": 2.0e8})
        out = _pillar(COAL_TARGET, self._included_constituents(quotes=quotes))
        codes = [item["code"] for item in out["items"]]
        assert codes[:5] == ["601898", "601225", "000723", "000552", "002128"]
        assert codes[5] == COAL_TARGET
        assert out["sample_size"] == 6

    def test_top5_verdict_requires_member_completeness_evidence(self):
        """现场常态（无完整性证据）：全成分前五资格不判定，样本内事实保留。"""
        out = _pillar(COAL_TARGET, self._included_constituents())
        assert out["target_in_amount_top5"] is None
        assert any("完整性" in g or "全集" in g for g in out["gaps"])

    def test_full_sets_allow_top5_verdict(self):
        """受控：成分全集证据 + 报价全集 → 前五资格可判 True。"""
        out = _pillar(COAL_TARGET, self._included_constituents(
            evidence="完整集合（受控样本，总数核验一致）"))
        assert out["target_in_amount_top5"] is True

    def test_full_sets_allow_false_verdict(self):
        quotes = _coal_quotes(amounts={"000552.SZ": 3.0e8, "000723.SZ": 5.0e8,
                                       "600188.SH": 1.0e7, "601225.SH": 7.0e8,
                                       "601898.SH": 1.0e9, "002128.SZ": 2.0e8})
        out = _pillar(COAL_TARGET, self._included_constituents(
            evidence="完整集合（受控样本）", quotes=quotes))
        assert out["target_in_amount_top5"] is False

    def test_zero_amount_target_false_requires_both_full_sets(self):
        """已知零额目标：仅报价全集（无成分全集证据）不得发布 False。"""
        quotes = _coal_quotes(amounts={"000552.SZ": 3.0e8, "000723.SZ": 5.0e8,
                                       "600188.SH": 0.0, "601225.SH": 7.0e8,
                                       "601898.SH": 1.0e9, "002128.SZ": 2.0e8})
        out = _pillar(COAL_TARGET, self._included_constituents(quotes=quotes))
        assert out["target_in_amount_top5"] is None
        controlled = self._included_constituents(
            evidence="完整集合（受控样本）", quotes=quotes)
        out2 = _pillar(COAL_TARGET, controlled)
        assert out2["target_in_amount_top5"] is False

    def test_quote_batch_gap_blocks_top5_verdict(self):
        """报价批次有缺口：报价全集不可证 → 前五资格 None。"""
        out = _pillar(COAL_TARGET, self._included_constituents(
            evidence="完整集合（受控样本）",
            quote_gaps=["成员行情第 2 批查询失败（HTTP 超时），该批成员行情缺失"]))
        assert out["target_in_amount_top5"] is None

    def test_in_sample_change_rank_with_full_coverage(self):
        out = _pillar(COAL_TARGET, self._included_constituents())
        # 样本内涨跌幅降序：601225(2.2) > 601898(0.5) > 000552(1.1)? 排序重算：
        # 2.2, 1.1, 0.5, -0.4, -0.87 → 600188(-0.87) 第 5
        assert out["target_change_pct_rank"] == 5

    def test_rank_withheld_when_peer_change_missing(self):
        """成交额第一的 amount-only 成员（涨跌幅缺失）保留在样本（不递补
        第六名），名次 withhold + 有效范围缺口（#102 复审 Spec 2 的支柱级
        语义；wire → 报告全实链回归见 test_sector_report_api_v100）。"""
        quotes = _coal_quotes(amounts={
            "000552.SZ": 3.0e8, "000723.SZ": 5.0e8, "600188.SH": 9.0e8,
            "601225.SH": 7.0e8, "601898.SH": 5.0e9, "002128.SZ": 2.0e8})
        quotes["601898.SH"] = _member_row("601898.SH", None, 5.0e9)
        out = _pillar(COAL_TARGET, self._included_constituents(quotes=quotes))
        codes = [i["code"] for i in out["items"]]
        assert "601898" in codes       # 保留在样本（有效金额参与选样）
        assert "002128" not in codes   # 第六名不递补
        top = next(i for i in out["items"] if i["code"] == "601898")
        assert top["amount"] == 5.0e9
        assert top["change_pct"] is None
        assert out["target_change_pct_rank"] is None
        assert any("有效比较范围" in g for g in out["gaps"])

    def test_missing_target_quote_keeps_membership_fields_none(self):
        quotes = _coal_quotes(drop=["600188.SH"])
        out = _pillar(COAL_TARGET, self._included_constituents(quotes=quotes))
        target = next(i for i in out["items"] if i["is_target"])
        assert target["amount"] is None and target["change_pct"] is None
        assert out["target_in_amount_top5"] is None
        assert out["target_change_pct_rank"] is None


# ---------------------------------------------------------------------------
# excluded / unknown：行业参考同业 + 目标独立展示
# ---------------------------------------------------------------------------


class TestNotIncludedBoundary:
    def _boundary_constituents(self, status: str, *,
                               target_quote: Optional[Dict[str, Any]] = "default",
                               target_quote_gaps: Optional[List[str]] = None,
                               members: Optional[Dict[str, Any]] = None,
                               quotes: Optional[Dict[str, Dict[str, Any]]] = None):
        members = members if members is not None else _coal_members()
        membership = _membership(status, "920101.BJ")
        tq = (_member_row("920101.BJ", 1.5, 4.0e7)
              if target_quote == "default" else target_quote)
        return _constituents(
            membership=membership, members=members,
            member_quotes=quotes if quotes is not None else _coal_quotes(),
            target_quote=tq, target_quote_gaps=target_quote_gaps)

    @pytest.mark.parametrize("status", ["unknown", "excluded"])
    def test_peers_are_pure_industry_reference(self, status):
        out = _pillar("920101", self._boundary_constituents(status))
        assert out["eligible"] is True
        assert out["industry_reference"] is True
        assert "行业参考" in out["selection_rule"]
        codes = [item["code"] for item in out["items"]]
        # 目标股（920101，非成分）不进 items；成分成员（如 600188）正常列示
        assert "920101" not in codes
        assert len(codes) == 5
        assert out["target_in_amount_top5"] is None
        assert out["target_change_pct_rank"] is None
        assert not any(i["is_target"] for i in out["items"])

    def test_target_independent_quote_not_in_items(self):
        out = _pillar("920101", self._boundary_constituents("unknown"))
        tiq = out["target_independent_quote"]
        assert tiq is not None
        assert tiq["code"] == "920101"
        assert tiq["amount"] == pytest.approx(4.0e7)
        assert tiq["as_of_date"] == SESSION_DATE
        codes = [item["code"] for item in out["items"]]
        assert "920101" not in codes

    def test_missing_target_quote_is_disclosed_gap(self):
        out = _pillar("920101", self._boundary_constituents(
            "unknown", target_quote=None,
            target_quote_gaps=["目标股行情查询失败（HTTP 超时），独立行情未取得"]))
        assert out["target_independent_quote"] is None
        assert any("独立行情未取得" in g or "目标股" in g for g in out["gaps"])

    def test_target_independent_negative_amount_is_invalid(self):
        """独立行情负额 = 无效观测：金额置 None + 缺口（不发布负额）。"""
        tq = _member_row("920101.BJ", 1.5, -5.0)
        out = _pillar("920101", self._boundary_constituents(
            "unknown", target_quote=tq))
        tiq = out["target_independent_quote"]
        assert tiq is not None
        assert tiq["amount"] is None
        assert any("无效" in g or "负" in g for g in out["gaps"])

    def test_target_independent_giant_raw_int_never_inf(self):
        """超大原始整数（10**400）不可表示为有限 float：金额置 None + 缺口，
        Markdown/API 边界不出现 inf（JSON/Markdown 一致性反例）。"""
        tq = _member_row("920101.BJ", 1.5, 10 ** 400)
        out = _pillar("920101", self._boundary_constituents(
            "unknown", target_quote=tq))
        tiq = out["target_independent_quote"]
        assert tiq is not None
        assert tiq["amount"] is None
        assert tiq["amount"] != float("inf")
        assert any("无效" in g for g in out["gaps"])
        from src.schemas.sector_analysis import SectorPeersBlock
        SectorPeersBlock.model_validate(out)

    def test_boundary_copy_distinguishes_no_response_from_incomplete_list(
            self):
        """成分未取得（无响应）：文案不得称「供应商响应未见目标股」。"""
        members = _members_out([], gaps=["成分查询失败（HTTP 超时）"])
        membership = _membership("unknown", "920101.BJ",
                                 gaps=["成分查询失败（HTTP 超时）"])
        out = _pillar("920101", _constituents(
            membership=membership, members=members, member_quotes={}))
        joined = " ".join(out["gaps"])
        assert "未取得" in joined
        assert "响应" not in joined.split("；")[0].split("。")[0]

    def test_boundary_copy_for_obtained_list_reports_no_metadata(self):
        """成分已取得但未见目标：文案说明响应无完整性元数据（如实区分）。"""
        members = _members_out(["000552.SZ", "601225.SH"])
        membership = _membership("unknown", "920101.BJ")
        out = _pillar("920101", _constituents(
            membership=membership, members=members,
            member_quotes={c: _member_row(c, 1.0, 3.0e8)
                           for c in members["member_codes"]}))
        joined = " ".join(out["gaps"])
        assert "无完整性元数据" in joined

    def test_over_limit_members_not_published_with_bounded_gap(self):
        """超大成分响应（超可发布上限）：snapshot_rows 有界、成员不发布、
        载荷严格校验通过。"""
        members = _members_out([])
        members["over_publish_limit"] = True
        members["rows_total"] = 15000
        members["gaps"] = ["成分响应 15000 行超出可发布成员上限 10000，"
                           "成员集合不发布（完整性不可证、不发布全集覆盖）"]
        membership = _membership("unknown", "920101.BJ")
        out = _pillar("920101", _constituents(
            membership=membership, members=members, member_quotes={}))
        assert out["snapshot_rows"] == 0
        assert out["items"] == []
        assert out["eligible"] is True
        assert any("15000" in g for g in out["gaps"])
        from src.schemas.sector_analysis import SectorPeersBlock
        SectorPeersBlock.model_validate(out)

    def test_exchange_lookalike_member_is_never_target(self):
        """unknown 态下成分中的同裸码异交易所成员（600188.SZ）：不是目标、
        不携带任何 target 证据。"""
        members = _members_out(["000552.SZ", "600188.SZ", "601225.SH"])
        quotes = {
            "000552.SZ": _member_row("000552.SZ", 1.0, 3.0e8),
            "600188.SZ": _member_row("600188.SZ", 2.0, 9.0e8),
            "601225.SH": _member_row("601225.SH", 0.5, 2.0e8),
        }
        membership = _membership("unknown", "600188.SH")
        out = _pillar("600188", _constituents(
            membership=membership, members=members, member_quotes=quotes,
            target_quote=_member_row("600188.SH", 1.5, 4.0e7)))
        assert out["industry_reference"] is True
        sz_item = next(i for i in out["items"] if i["code"] == "600188")
        assert sz_item["is_target"] is False
        assert not any(i["is_target"] for i in out["items"])
        assert out["target_change_pct_rank"] is None
        assert out["target_in_amount_top5"] is None
        tiq = out["target_independent_quote"]
        assert tiq is not None and tiq["code"] == "600188"


# ---------------------------------------------------------------------------
# 通用纪律（两态共用）
# ---------------------------------------------------------------------------


class TestSharedDiscipline:
    def test_partial_quotes_disclose_effective_range(self):
        quotes = _coal_quotes(drop=["002128.SZ", "000552.SZ"])
        membership = _membership("included", COAL_TARGET_SEC)
        out = _pillar(COAL_TARGET, _constituents(
            membership=membership, members=_coal_members(),
            member_quotes=quotes,
            quote_gaps=["成员行情第 3 批查询失败，2 只成员行情缺失"]))
        assert out["snapshot_rows"] == 6
        assert out["rankable_rows"] == 4
        assert any("4" in g and "6" in g for g in out["gaps"])
        assert out["target_in_amount_top5"] is None

    def test_zero_amount_member_kept_out_of_active_top5(self):
        quotes = _coal_quotes(amounts={"000552.SZ": 0.0, "000723.SZ": 5.0e8,
                                       "600188.SH": 9.0e8, "601225.SH": 7.0e8,
                                       "601898.SH": 1.0e9, "002128.SZ": 2.0e8})
        membership = _membership("included", COAL_TARGET_SEC)
        out = _pillar(COAL_TARGET, _constituents(
            membership=membership, members=_coal_members(),
            member_quotes=quotes))
        codes = [item["code"] for item in out["items"]]
        # 零额是已知零观测：不进正额活跃前五（目标 600188 在前五，样本 5 家）
        assert "000552" not in codes
        assert out["sample_size"] == 5
        assert any("零额" in g for g in out["gaps"])

    def test_duplicate_code_after_suffix_strip_rejected(self):
        members = _members_out(["000552.SZ", "000552.BJ", "600188.SH",
                                "601225.SH", "601898.SH", "002128.SZ"])
        quotes = _coal_quotes()
        quotes["000552.BJ"] = _member_row("000552.BJ", 0.8, 6.0e8)
        membership = _membership("included", COAL_TARGET_SEC)
        out = _pillar(COAL_TARGET, _constituents(
            membership=membership, members=members, member_quotes=quotes))
        codes = [item["code"] for item in out["items"]]
        assert "000552" not in codes
        assert any("000552" in g for g in out["gaps"])

    def test_quotes_as_of_from_provider_stamps(self):
        quotes = _coal_quotes()
        quotes["000552.SZ"] = _member_row(
            "000552.SZ", 1.1, 3.0e8, stamp="2026-11-18 21:30:00")
        membership = _membership("included", COAL_TARGET_SEC)
        out = _pillar(COAL_TARGET, _constituents(
            membership=membership, members=_coal_members(),
            member_quotes=quotes))
        assert out["quotes_as_of"] == "2026-11-18 21:30:00~2026-11-18 22:00:46"

    def test_items_carry_real_same_date_as_of_from_wire_rows(self):
        """items 的 as_of_date 来自被接受 wire 行的实际归属日（不伪造）。"""
        quotes = _coal_quotes()
        quotes["000552.SZ"] = dict(
            _member_row("000552.SZ", 1.1, 3.0e8), trade_date="2026-11-17")
        membership = _membership("included", COAL_TARGET_SEC)
        out = _pillar(COAL_TARGET, _constituents(
            membership=membership, members=_coal_members(),
            member_quotes=quotes))
        by_code = {i["code"]: i for i in out["items"]}
        assert by_code["600188"]["as_of_date"] == SESSION_DATE
        assert by_code["000552"]["as_of_date"] == "2026-11-17"

    def test_member_giant_raw_int_invalid_no_inf_no_full_set(self):
        """成员超大原始整数：Decimal 排序输入保留但发布前按公开边界判无效
        （invalid 计数 + 报价全集不可证；无效成员不进正额样本，items 无 inf）。"""
        quotes = _coal_quotes()
        quotes["000552.SZ"] = dict(
            _member_row("000552.SZ", 1.1, None), amount=10 ** 400)
        membership = _membership("included", COAL_TARGET_SEC)
        out = _pillar(COAL_TARGET, _constituents(
            membership=membership, members=_coal_members(),
            member_quotes=quotes))
        codes = [i["code"] for i in out["items"]]
        assert "000552" not in codes  # 无效金额不进正额排名，也不伪居样本
        for i in out["items"]:
            assert i["amount"] != float("inf")
            assert i["amount"] is not None
        assert out["target_in_amount_top5"] is None  # 报价全集不可证
        assert any("无效" in g for g in out["gaps"])
        from src.schemas.sector_analysis import SectorPeersBlock
        SectorPeersBlock.model_validate(out)

    def test_discarded_member_rows_block_full_universe_verdict(self):
        """被剔除的成员行（身份冲突/格式不合格）使报价全集分母不完整：
        即使剩余成员金额全部已知且有成分全集证据，全宇宙前五资格不判定。"""
        members = _members_out(
            ["000552.SZ", "000723.SZ", "600188.SH", "601225.SH", "601898.SH"],
            evidence="完整集合（受控样本）", conflicting_code_members=2)
        quotes = _coal_quotes()
        membership = _membership("included", COAL_TARGET_SEC,
                                 evidence="完整集合（受控样本）")
        out = _pillar(COAL_TARGET, _constituents(
            membership=membership, members=members, member_quotes=quotes))
        assert out["target_in_amount_top5"] is None
        assert any("剔除" in g or "覆盖" in g or "分母" in g
                   for g in out["gaps"])

    def test_no_member_quotes_means_empty_sample_with_gap(self):
        membership = _membership("included", COAL_TARGET_SEC)
        out = _pillar(COAL_TARGET, _constituents(
            membership=membership, members=_coal_members(),
            member_quotes={},
            quote_gaps=["成员行情全部批次查询失败，样本为空"]))
        assert out["eligible"] is True
        # 既有规则：目标成员资格保留（样本仅目标、无同业可比、不发名次）
        codes = [item["code"] for item in out["items"]]
        assert codes == [COAL_TARGET]
        assert out["sample_size"] == 1
        assert out["rankable_rows"] == 0
        assert any("样本为空" in g for g in out["gaps"])
        assert any("无成交额可排名" in g for g in out["gaps"])

    def test_members_not_obtained_keeps_eligible_with_gap(self):
        """成分未取得：资格成立但样本为空（数据未取得与资格不足分开）。"""
        membership = _membership("unknown", "600188.SH",
                                 gaps=["成分查询失败（HTTP 超时）"])
        members = _members_out([], gaps=["成分查询失败（HTTP 超时）"])
        out = _pillar(COAL_TARGET, _constituents(
            membership=membership, members=members, member_quotes={}))
        assert out["eligible"] is True
        assert out["items"] == []
        assert out["industry_reference"] is True
        assert any("成分查询失败" in g for g in out["gaps"])

    def test_source_and_provenance_carry_sw_wiring(self):
        out = _pillar(COAL_TARGET, _constituents(
            membership=_membership("included", COAL_TARGET_SEC),
            members=_coal_members(), member_quotes=_coal_quotes()))
        assert "p03473" in out["source"]
        assert "real_time_quotation" in out["source"]
        assert out["amount_unit"] == "元"
        assert "iFinD" in out["amount_provenance"]
        assert out["snapshot_observed_at"] == OBSERVED_AT


class TestUnderflowTwoLevelSemantics:
    """issue #105 复审：同一 wire 输入（6 条同日行情 amount=Decimal('1e-400')、
    合格涨跌幅）两级行为对照——一级恢复基线（正额参与选样，公开 float
    发布 0.0），二级名次语境判 invalid（不冒充正额）。有限/上溢护栏两级
    共用不变。"""

    def _underflow_constituents(self):
        amounts = {c: Decimal("1e-400") for c in (
            "000552.SZ", "000723.SZ", "600188.SH", "601225.SH",
            "601898.SH", "002128.SZ")}
        return _constituents(
            membership=_membership("included", COAL_TARGET_SEC),
            members=_coal_members(), member_quotes=_coal_quotes(amounts=amounts))

    def test_l1_keeps_legacy_sample_semantics(self):
        out = _pillar(COAL_TARGET, self._underflow_constituents())
        assert out["rankable_rows"] == 6
        assert out["sample_size"] == 5
        assert all(i["amount"] == 0.0 for i in out["items"])
        assert not any("不可排名" in g for g in out["gaps"])

    def test_l2_rank_context_treats_underflow_invalid(self):
        out = _sw_peers_pillar(
            COAL_TARGET, constituents=self._underflow_constituents(),
            snapshot_observed_at=OBSERVED_AT, budget=_budget(),
            amount_rank_enabled=True)
        assert out["rankable_rows"] == 0
        assert out["sample_size"] == 1
        target = next(i for i in out["items"] if i["is_target"])
        assert target["amount"] is None
        assert any("不可排名" in g and "无效" in g for g in out["gaps"])
