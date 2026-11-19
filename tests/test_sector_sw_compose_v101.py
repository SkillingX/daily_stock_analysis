# -*- coding: utf-8 -*-
"""申万口径研究编排（issue #101）：选定口径后研究恰好一次 + 完整性恢复契约。

测试缝（与既有板块测试约定一致）：
- `compose_report` 公开服务缝 + 研究支柱记录器：保留申万（`_sw_peers_pillar`
  侧记录）/ 东财成功切换（`_peers_pillar` 侧记录）/ 部分恢复 / 双失败保留
  四条编排路径下，同业/催化/综合**恰好一次**且只运行在选定口径之后
  （切换路径零被弃的申万研究）；
- 催化/综合替身形状变体驱动完整性矩阵（无合格催化 / 模型失败 / 混合输出
  拒绝 → partial 且原因可回查）；complete 契约由 API 全闭环真实链覆盖；
- 证据登记表日期纪律（参考口径行情 / 同业归属日 / 目标独立事实）在
  `_build_evidence_registry` + 服务端摘要缝上直接观察；#98 复审后参考口径
  行情与未确认成分独立目标事实**不可引用**（detached）：模型投影排除、
  对抗性引用按未知 ID 拒绝，服务端摘要保留带真实日期。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Tuple

import pytest
from unittest.mock import patch

from src.services import sector_sw_market as _sw_mod
from src.services.sector_analysis_service import (
    ReportBudget,
    _build_evidence_registry,
    compose_report,
)
from src.services.sector_research import (
    market_observation_relations,
    program_fact_summary,
)

from tests.test_sector_sw_compose_v99 import (
    _CoreRecorder,
    _ResearchStub,
    _catalysts_stub_payload,
    _em_core_stub,
    _peers_stub_payload,
    _qualified_row,
    _sw_core,
    _synthesis_stub_payload,
)


@pytest.fixture(autouse=True)
def _offline_reference_levels(monkeypatch):
    """单元编排测试离线缝（spec 复审拆缝）：二/三级参考链出站受控替身
    （同 compose_v99 基座；真实传输与时序由 compose_v100 sequencing 与
    API 闭环覆盖）。"""
    monkeypatch.setattr(_sw_mod, "sw_http_config",
                        lambda: ("sector-test-refresh-token", 10.0))
    monkeypatch.setattr(
        _sw_mod, "resolve_sw_reference_levels",
        lambda secucode, *, observation_date, refresh_token,
        timeout_seconds, deadline: {
            "l2_name": "煤炭开采", "l3_name": "动力煤", "gaps": []})


def _budget(seconds: float = 120.0) -> ReportBudget:
    return ReportBudget(seconds, finish_margin=min(5.0, seconds / 2.0))


def _sw_peers_stub_payload() -> Dict[str, Any]:
    payload = _peers_stub_payload()
    payload["industry_reference"] = False
    payload["target_independent_quote"] = None
    return payload


def _run101(sw_core: Dict[str, Any], em_core: Dict[str, Any], *,
            catalysts: Dict[str, Any], synthesis: Dict[str, Any],
            code: str = "600188", name: str = "兖矿能源",
            ) -> Tuple[Dict[str, Any], Any, Any, Any]:
    """compose_report 双侧研究记录器一体化安装（申万/东财口径各一组）。"""
    # 主行业已确认时注入最小受控成分集合（真实流程由 _enrich_constituents
    # 在保留申万后富化；替身只观察 _sw_peers_pillar 的调用次数）。
    if (sw_core.get("identity") or {}).get("status") == "confirmed" \
            and sw_core.get("constituents") is None:
        sw_core = dict(sw_core)
        sw_core["constituents"] = {"membership": {"status": "unknown"},
                                   "members": {}, "member_quotes": {}}
    sw_peers = _ResearchStub(_sw_peers_stub_payload())
    em_peers = _ResearchStub(_peers_stub_payload())
    cat = _ResearchStub(catalysts)
    syn = _ResearchStub(synthesis)
    with patch("src.services.sector_analysis_service._sw_market_chain",
               return_value=sw_core), \
            patch("src.services.sector_analysis_service._compose_eastmoney_core",
                  _CoreRecorder(em_core)), \
            patch("src.services.sector_analysis_service._sw_peers_pillar",
                  sw_peers), \
            patch("src.services.sector_analysis_service._peers_pillar",
                  em_peers), \
            patch("src.services.sector_analysis_service._catalysts_pillar",
                  cat), \
            patch("src.services.sector_analysis_service._synthesis_pillar",
                  syn), \
            patch("src.services.sector_analysis_service._resolve_sw_chain",
                  lambda c, t, *, budget: {"l1": "", "l2": "", "l3": "",
                                           "l3_code": "", "gaps": []}), \
            patch("src.services.sector_analysis_service._concepts_pillar",
                  lambda c, *, budget: {
                      "items": [], "source": "ifind", "total_returned": 0,
                      "truncated": False, "gaps": ["受控：概念阶段不执行"]}), \
            patch("src.services.sector_analysis_service._probe_article_date_evidence",
                  lambda url, budget: (None, "transport", None)):
        payload = compose_report(code, name, budget=_budget())
    return payload, sw_peers, cat, syn


class TestResearchRunsOnceAfterBasisSelection:
    """选定报告口径后研究（同业/催化/综合）恰好运行一次。"""

    def test_kept_sw_runs_research_exactly_once(self):
        """保留申万（门槛未触发）：研究在申万口径上运行恰好一次。"""
        sw_core = _sw_core(quote_target_ok=True, trend_ok=True)
        payload, sw_peers, cat, syn = _run101(
            sw_core, _em_core_stub(),
            catalysts=_catalysts_stub_payload(),
            synthesis=_synthesis_stub_payload())
        assert payload["research_outcome"] == "kept_sw_stage"
        assert sw_peers.calls == 1
        assert cat.calls == 1
        assert syn.calls == 1
        # 研究块随载荷透出（替身形状）
        assert payload["sector_catalysts"]["provider"] == "Bocha"
        assert payload["sector_synthesis"]["status"] == "ok"

    def test_switched_em_runs_research_exactly_once(self):
        """东财成功切换：研究只在东财口径上运行一次（零被弃申万研究）。"""
        sw_core = _sw_core(status="unavailable", quote_target_ok=False,
                           trend_ok=False, snapshot_mode="none")
        em_core = _em_core_stub(subject_status="confirmed", quote_change=1.5)
        payload, sw_peers, cat, syn = _run101(
            sw_core, em_core,
            catalysts=_catalysts_stub_payload(),
            synthesis=_synthesis_stub_payload())
        assert payload["research_outcome"] == "switched_fallback"
        assert sw_peers.calls == 0  # 切换路径零被弃的申万同业出站
        assert cat.calls == 1
        assert syn.calls == 1

    def test_dual_failure_keeps_sw_and_research_lands_in_report(self):
        """双失败保留申万：口径已选定（申万），研究运行一次并入报告。"""
        sw_core = _sw_core(quote_target_ok=False, trend_ok=False,
                           snapshot_mode="reference")
        em_core = _em_core_stub(subject_status="confirmed", quote_change=None,
                                trend_return=None)
        payload, sw_peers, cat, syn = _run101(
            sw_core, em_core,
            catalysts=_catalysts_stub_payload(),
            synthesis=_synthesis_stub_payload())
        assert payload["research_outcome"] == "kept_sw_stage"
        assert sw_peers.calls == 1
        assert cat.calls == 1
        assert syn.calls == 1

    def test_partial_recovery_runs_no_research(self):
        """东财部分恢复（未达切换门槛）：不运行将被丢弃的研究。"""
        sw_core = _sw_core(status="unavailable", quote_target_ok=False,
                           trend_ok=False, snapshot_mode="none")
        em_core = _em_core_stub(subject_status="confirmed", quote_change=None,
                                trend_return=None)
        payload, sw_peers, cat, syn = _run101(
            sw_core, em_core,
            catalysts=_catalysts_stub_payload(),
            synthesis=_synthesis_stub_payload())
        assert payload["research_outcome"] == "partial_recovery"
        assert sw_peers.calls == 0
        assert cat.calls == 0
        assert syn.calls == 0

    def test_em_core_transport_failure_keeps_sw_with_cause(self):
        """东财兜底核心阶段供应商传输失败：保留申万事实 + 原因可回查。

        issue #101 验收（兜底失败行为）：传输类供应商失败（requests
        RequestException）不作为程序错误 500，也不伪报总超时——与
        BudgetExhausted 同一保留申万纪律，实际失败类别随 gaps 可回查。
        """
        import requests as _rq

        sw_core = _sw_core(status="unavailable", quote_target_ok=False,
                           trend_ok=False, snapshot_mode="none")

        def _failing_core(code: str, *, budget: ReportBudget) -> Dict[str, Any]:
            raise _rq.ConnectionError("em transport down")

        sw_peers = _ResearchStub(_sw_peers_stub_payload())
        cat = _ResearchStub(_catalysts_stub_payload())
        syn = _ResearchStub(_synthesis_stub_payload())
        with patch("src.services.sector_analysis_service._sw_market_chain",
                   return_value=sw_core), \
                patch("src.services.sector_analysis_service._compose_eastmoney_core",
                      _failing_core), \
                patch("src.services.sector_analysis_service._sw_peers_pillar",
                      sw_peers), \
                patch("src.services.sector_analysis_service._catalysts_pillar",
                      cat), \
                patch("src.services.sector_analysis_service._synthesis_pillar",
                      syn):
            payload = compose_report("600188", "兖矿能源", budget=_budget())
        assert payload["research_basis"] == "sw2021_l1"
        assert payload["research_outcome"] == "kept_sw_stage"
        assert payload["identity"]["subject_status"] == "missing"
        assert payload["assessment"]["status"] == "insufficient_data"
        assert any("供应商传输失败" in g and "ConnectionError" in g
                   for g in payload["gaps"])
        assert any("兜底触发原因" in g for g in payload["gaps"])


class TestCompletenessMatrix:
    """完整性恢复矩阵（stub 研究形状变体）：partial 原因可回查。"""

    def _payload_with(self, *, catalysts: Dict[str, Any],
                      synthesis: Dict[str, Any]) -> Dict[str, Any]:
        sw_core = _sw_core(quote_target_ok=True, trend_ok=True)
        payload, _sp, _c, _s = _run101(
            sw_core, _em_core_stub(), catalysts=catalysts, synthesis=synthesis)
        return payload

    def test_membership_boundary_keeps_partial_with_reason(self):
        """受控链无 constituents → membership 未验证 → partial（边界纪律）；
        complete 契约由 API 全闭环（v101 API 测试，真实成分链）覆盖。"""
        payload = self._payload_with(catalysts=_catalysts_stub_payload(),
                                     synthesis=_synthesis_stub_payload())
        assert payload["assessment"]["completeness"] == "partial"
        reasons = " ".join(payload["assessment"]["reasons"])
        assert "成分状态未知" in reasons

    def test_catalysts_without_items_keeps_partial_with_reason(self):
        catalysts = _catalysts_stub_payload()
        catalysts["items"] = []
        catalysts["gaps"] = ["行业催化：全部检索失败（受控）"]
        payload = self._payload_with(catalysts=catalysts,
                                     synthesis=_synthesis_stub_payload())
        assert payload["assessment"]["completeness"] == "partial"
        assert any("行业催化未取得" in r
                   for r in payload["assessment"]["reasons"])

    def test_synthesis_model_error_keeps_partial_with_reason(self):
        synthesis = _synthesis_stub_payload()
        synthesis["status"] = "model_error"
        synthesis["gaps"] = ["证据化综合：模型服务错误（受控）"]
        payload = self._payload_with(catalysts=_catalysts_stub_payload(),
                                     synthesis=synthesis)
        assert payload["assessment"]["completeness"] == "partial"
        assert any("证据化综合未产出" in r
                   for r in payload["assessment"]["reasons"])

    def test_synthesis_rejected_output_keeps_partial_with_reason(self):
        synthesis = _synthesis_stub_payload()
        synthesis["rejected_count"] = 2
        payload = self._payload_with(catalysts=_catalysts_stub_payload(),
                                     synthesis=synthesis)
        assert payload["assessment"]["completeness"] == "partial"
        assert any("已拒绝的结论输出" in r
                   for r in payload["assessment"]["reasons"])

    def test_reference_mode_keeps_partial_even_with_full_research(self):
        """参考口径行情不是当日完整证据：研究全部合格仍 partial。"""
        sw_core = _sw_core(quote_target_ok=False, trend_ok=True,
                           snapshot_mode="reference")
        payload, _sp, _c, _s = _run101(
            sw_core, _em_core_stub(),
            catalysts=_catalysts_stub_payload(),
            synthesis=_synthesis_stub_payload())
        assert payload["assessment"]["completeness"] == "partial"
        assert payload["background"]["intraday"]["snapshot_mode"] == "reference"


# ---------------------------------------------------------------------------
# 证据登记表日期纪律（issue #101 验收 2：参考日不进入当日证据）
# ---------------------------------------------------------------------------


def _reference_registry_fixtures() -> Dict[str, Any]:
    """参考口径 intraday + 行业参考同业（含目标独立行情）的受控块形状。"""
    prosper = {
        "sector_found": True, "momentum": 40.0, "activity": None,
        "amount_activity": None, "breadth": 66.7, "rank_of": None,
        "total_boards": 31,
        "row": {"board_name": "煤炭", "board_code": "801950.SL",
                "change_pct": 1.2},
        "snapshot_date": "2026-11-18", "snapshot_mode": "reference",
        "snapshot_source": "iFinD 标准 HTTP 申万一级行业指数快照"
                           "（real_time_quotation，参考交易日 2026-11-18"
                           "；目标交易日合格行情未取得）",
        "gaps": [],
    }
    peers = {
        "eligible": True,
        "selection_rule": "同一成分快照按成交额前五（目标股指数成分状态未知，"
                          "同业为行业参考）",
        "items": [
            {"code": "601088", "name": "成员甲", "amount": 1.2e9,
             "change_pct": 0.5, "is_target": False,
             "as_of_date": "2026-11-19"},
            {"code": "600408", "name": "成员乙", "amount": 0.9e9,
             "change_pct": None, "is_target": False,
             "as_of_date": "2026-11-19"},
        ],
        "target_in_amount_top5": None, "target_change_pct_rank": None,
        "amount_provenance": "受控", "quotes_as_of": "2026-11-19 10:29:00",
        "industry_reference": True,
        "target_independent_quote": {
            "code": "920101", "name": "目标股", "amount": 4.0e7,
            "change_pct": 1.5, "is_target": True,
            "as_of_date": "2026-11-19",
        },
        "gaps": [],
    }
    ident = {"industry_em": None, "board_name": "煤炭",
             "board_code": "801950.SL", "membership": {"member_count": None}}
    return {"ident": ident, "prosper": prosper, "peers": peers,
            "trend": {"eligible": False}, "catalysts": {"items": [],
                                                        "background_items": []}}


class TestEvidenceRegistryDateDiscipline:
    """登记表与服务端摘要：参考日行情不表述为当日；独立目标事实不进同业。"""

    def _registry(self):
        fx = _reference_registry_fixtures()
        return _build_evidence_registry(
            "目标股", "920101", fx["ident"], fx["prosper"], fx["trend"],
            fx["peers"], fx["catalysts"],
        )

    def test_intraday_material_carries_reference_date_and_mode(self):
        reg = self._registry()
        m = json.loads(reg.get("intraday").material)
        assert m["snapshot_mode"] == "reference"
        assert m["snapshot_date"] == "2026-11-18"

    def test_summary_labels_reference_day_not_today(self):
        s = program_fact_summary(self._registry())
        assert "板块指数参考交易日（2026-11-18）涨跌幅 1.2%（上涨）" in s
        assert "板块指数当日涨跌幅" not in s
        # 广度归属日与涨跌幅同段同日（不冒充当日）
        assert "参考交易日（2026-11-18）上涨家数占比 66.7%" in s
        assert "当日上涨家数占比" not in s
        # 同业归属日独立呈现（目标交易日），不与参考日拼接
        assert "样本 2 家（同日行情 2026-11-19）" in s
        # 目标独立事实独立登记并携带真实日期
        assert "行情日（2026-11-19）涨跌幅 1.5%（上涨；独立事实，未确认成分" in s

    def test_relations_exclude_reference_day_and_independent_target(self):
        """#98 复审：参考口径行情与未确认成分独立目标行情**不可引用**
        （detached）——不进入模型投影（不得与成分同业事实并陈暗示当日
        走强/成分内强度）；服务端事实摘要保留独立呈现（带真实日期）。"""
        reg = self._registry()
        obs = {o["evidence_id"]: o["observation"]
               for o in market_observation_relations(reg)}
        # 参考日行情不作为可引用市场证据出现在模型 wire
        assert "intraday" not in obs
        assert not reg.citable("intraday")
        # 独立目标事实不作为可引用市场证据与成分同业事实并陈
        assert "target" not in obs
        assert not reg.citable("target")
        # 同业（成分）事实仍可引用，归属日表述保留
        assert "peers" in obs
        assert "行情日（2026-11-19）涨跌观测缺失" in obs["peers"]
        assert "方向一致" not in obs["peers"]

    def test_detached_facts_rejected_as_claim_evidence(self):
        """对抗性模型输出：引用 detached ID（intraday/target）的结论在
        引用资格校验被拒（unknown_evidence_ref），不靠 prompt 约束。"""
        from src.services.sector_research import validate_synthesis_claim

        reg = self._registry()
        verdict, reason = validate_synthesis_claim(
            {"kind": "interpretation",
             "text": "目标股领涨指数成分样本，板块延续走强",
             "evidence_refs": ["target", "peers"]},
            reg,
        )
        assert verdict is None
        assert reason == "unknown_evidence_ref:target"
        verdict, reason = validate_synthesis_claim(
            {"kind": "interpretation",
             "text": "板块指数昨日上涨，今日样本方向一致",
             "evidence_refs": ["intraday"]},
            reg,
        )
        assert verdict is None
        assert reason == "unknown_evidence_ref:intraday"
        # 合法引用（成分同业 + 身份）仍通过
        verdict, reason = validate_synthesis_claim(
            {"kind": "interpretation",
             "text": "行业政策新闻构成板块催化，成分样本涨跌方向存在不确定性",
             "evidence_refs": ["peers", "identity"]},
            reg,
        )
        assert verdict is not None and reason is None

    def test_target_independent_quote_is_registered_separately_from_peers(self):
        reg = self._registry()
        m = json.loads(reg.get("target").material)
        assert m["independent"] is True
        assert m["as_of_date"] == "2026-11-19"
        # peers 投影无目标行（行业参考同业不含目标）
        peers_m = json.loads(reg.get("peers").material)
        assert not any(i.get("is_target") for i in peers_m["items"])

    def test_target_dated_material_from_constituent_items(self):
        """included 路径：目标事实来自成分样本行，携带其归属交易日。"""
        peers = {
            "eligible": True, "selection_rule": "受控",
            "items": [{"code": "600188", "name": "兖矿能源", "amount": 5.0e8,
                       "change_pct": -0.11, "is_target": True,
                       "as_of_date": "2026-11-18"}],
            "target_independent_quote": None, "gaps": [],
        }
        prosper = {"sector_found": True, "momentum": 50.0, "breadth": 60.0,
                   "row": {"change_pct": -0.87}, "gaps": []}
        reg = _build_evidence_registry(
            "兖矿能源", "600188", {"board_name": "煤炭", "membership": {}},
            prosper, {"eligible": False}, peers, {"items": [],
                                                  "background_items": []},
        )
        m = json.loads(reg.get("target").material)
        assert m["snapshot_change_pct"] == pytest.approx(-0.11)
        assert m["as_of_date"] == "2026-11-18"
        assert "independent" not in m
        s = program_fact_summary(reg)
        assert "目标股兖矿能源行情日（2026-11-18）涨跌幅" in s


class TestMissingTargetQuoteNoMarketEvidence:
    """#102 复审 Spec 4（单元级）：``sector_found=True`` 但目标行涨跌幅
    缺失（其余指数有值的 30/31 覆盖态）不注册 intraday 市场证据——登记表
    无该块（不可引用），服务端事实摘要无板块涨跌幅段（不冒充目标快照）。"""

    def test_all_null_independent_observation_registers_no_target_fact(self):
        """#102 复审三轮（登记表级）：空值观测（行情行存在且时点合格、
        成交额与涨跌幅均缺失/无效）不发布块、**不登记任何目标事实**——
        无 detached target 条目、不可引用、摘要无目标段。"""
        peers = {
            "eligible": True, "selection_rule": "受控",
            "items": [{"code": "601088", "name": "成员甲", "amount": 1.2e9,
                       "change_pct": 0.5, "is_target": False,
                       "as_of_date": "2026-11-19"}],
            "target_in_amount_top5": None, "target_change_pct_rank": None,
            "amount_provenance": "受控", "quotes_as_of": "2026-11-19 10:29:00",
            "industry_reference": True,
            "target_independent_quote": None,
            "gaps": ["目标股独立行情未取得（当日行情观测存在但成交额与"
                     "涨跌幅均缺失/无效，无数值可发布）"],
        }
        reg = _build_evidence_registry(
            "目标股", "920101", {"board_name": "煤炭", "membership": {}},
            {"sector_found": False}, {"eligible": False}, peers,
            {"items": [], "background_items": []},
        )
        assert reg.get("target") is None  # 无任何登记目标事实
        assert not reg.citable("target")  # 引用校验按未知 ID 拒绝

    def test_sector_found_without_target_change_registers_no_intraday(self):
        prosper = {
            "sector_found": True, "momentum": None, "activity": None,
            "amount_activity": None, "breadth": None, "rank_of": None,
            "total_boards": 31,
            "row": {"board_name": "煤炭", "board_code": "801950.SL",
                    "change_pct": None},
            "snapshot_date": "2026-11-18", "snapshot_mode": "target",
            "snapshot_source": "受控来源", "gaps": ["覆盖缺口（受控）"],
        }
        peers = {"eligible": True, "items": [],
                 "target_independent_quote": None,
                 "amount_provenance": "受控", "gaps": []}
        reg = _build_evidence_registry(
            "兖矿能源", "600188", {"board_name": "煤炭", "membership": {}},
            prosper, {"eligible": False}, peers,
            {"items": [], "background_items": []},
        )
        assert reg.get("intraday") is None
        assert not reg.citable("intraday")
        summary = program_fact_summary(reg)
        assert "板块指数" not in summary


# ---------------------------------------------------------------------------
# #98 复审：快照覆盖计数只对 31 核定目录身份成立 + 确认申万缺趋势保持部分
# ---------------------------------------------------------------------------


class TestSnapshotDirectoryIdentityCounting:
    """快照覆盖计数先按目录过滤请求身份（当日与参考 cohort）：
    30 目录 + 未知码 ≠ 31/31 全量，未知行计数披露、覆盖缺口如实。"""

    def _snapshot_rows(self, *, qualified_codes, session_date: str
                       ) -> Dict[str, Dict[str, Any]]:
        def _row(code: str) -> Dict[str, Any]:
            return {"thscode": code, "trade_date": session_date,
                    "trade_time": "22:00:00", "change_pct": 1.0,
                    "amount": 1.0e9, "turnover": 2.0,
                    "up_count": 20.0, "down_count": 10.0}
        return {c: _row(c) for c in qualified_codes}

    def test_unknown_supplier_identity_never_counts_as_full_coverage(self):
        from src.services.sector_sw_market import SW_L1_DIRECTORY
        from src.services.sector_analysis_service import _sw_snapshot_pillar

        session_date = "2026-11-18"
        directory_codes = [c for c, _ in SW_L1_DIRECTORY]
        missing = directory_codes[0]  # 30 个目录行合格 + 1 个目录行缺失
        qualified = [c for c in directory_codes if c != missing]
        rows = self._snapshot_rows(
            qualified_codes=qualified + ["999999.SL"],  # +1 未知身份
            session_date=session_date)
        pillar = _sw_snapshot_pillar(
            {"session_date": session_date, "rows": rows,
             "reference_date": None, "reference_rows": {},
             "dates_seen": [session_date], "gaps": []},
            "煤炭", "801950.SL", budget=_budget())
        assert pillar["sector_found"] is True
        assert pillar["snapshot_mode"] == "target"
        gaps = " ".join(pillar["gaps"])
        # 未知身份计数披露（不采用、不进入全表口径）
        assert "非申万一级核定目录指数行" in gaps
        # 覆盖缺口如实：30/31（未知码不顶替缺失的目录行）
        assert "31 个申万一级指数中 1 个未取得" in gaps

    def test_reference_cohort_counting_filters_requested_identities(self):
        """参考口径提示的「其余 N 个指数取得当日合格行情」同样按目录过滤。"""
        from src.services.sector_sw_market import SW_L1_DIRECTORY
        from src.services.sector_analysis_service import _sw_snapshot_pillar

        session_date = "2026-11-18"
        ref_date = "2026-11-17"
        directory_codes = [c for c, _ in SW_L1_DIRECTORY]
        target = directory_codes[1]
        # 当日：目标指数缺行 + 1 个目录行缺失 + 1 个未知身份（其余合格）
        today_qualified = [c for c in directory_codes
                           if c not in (directory_codes[0], target)]
        rows = self._snapshot_rows(
            qualified_codes=today_qualified + ["999999.SL"],
            session_date=session_date)
        ref_rows = self._snapshot_rows(
            qualified_codes=[target],  # 参考日只有目标指数行（目录内）
            session_date=ref_date)
        pillar = _sw_snapshot_pillar(
            {"session_date": session_date, "rows": rows,
             "reference_date": ref_date, "reference_rows": ref_rows,
             "dates_seen": [session_date, ref_date], "gaps": []},
            "基础化工", target, budget=_budget())
        assert pillar["snapshot_mode"] == "reference"
        assert pillar["snapshot_date"] == ref_date
        gaps = " ".join(pillar["gaps"])
        assert "非申万一级核定目录指数行" in gaps
        # 目标指数参考行存在 → 参考口径激活；当日 cohort 计数为 29（过滤未知码）
        assert "其余 29 个指数取得当日合格行情" in gaps


class TestConfirmedMissingTrendKeepsPartial:
    """#98 复审：confirmed 报告的趋势缺失按**实际缺失事实**判定——
    严格交易日历不可用（eligible=False）与其他趋势失败同样保持部分，
    日历缺口原文保留（不再被 eligible 短路成可 complete）。

    本场景其余证据全部合格（目标日行情动量可算、成分已验证、同业/催化/
    综合均 ok）：修复前该组合会恢复 complete 并被 compose 契约
    （complete 必须有合格趋势区间收益）拒绝为 ViolationError；
    修复后如实 partial 且原因可回查。"""

    def test_calendar_unavailable_trend_records_missing_trend_reason(self):
        sw_core = _sw_core(quote_target_ok=True, trend_ok=False)
        # 两个合格目录行：快照动量可计算（否则动量缺失也强制 partial，
        # 无法单独暴露趋势缺失路径）
        sw_core["snapshot"]["rows"]["801120.SL"] = _qualified_row(
            "801120.SL", 0.5)
        # 模拟严格 20 交易日日历不可用：eligible=False + 日历缺口原文
        sw_core["trend"]["eligible"] = False
        sw_core["trend"]["gaps"] = [
            "板块趋势：严格交易日历不可用，无法确定最近 20 个已完成交易日，"
            "本次不查询行业/基准日线（不按自然日补齐）"]
        sw_core["trend_ok"] = False
        # 成分已验证（included）：其余完整性条件全部满足
        sw_core["constituents"] = {
            "membership": {"status": "included", "member_count": 6,
                           "target_secucode": "600188.SH",
                           "observation_date": "2026-11-18",
                           "completeness_evidence": None, "gaps": []},
        }
        payload, _sp, _c, _s = _run101(
            sw_core, _em_core_stub(),
            catalysts=_catalysts_stub_payload(),
            synthesis=_synthesis_stub_payload())
        assert payload["assessment"]["completeness"] == "partial"
        reasons = " ".join(payload["assessment"]["reasons"])
        assert "行业近期趋势未取得" in reasons
        assert "严格交易日历不可用" in reasons
        # 其余证据不受牵连：行情动量/成分/同业/催化/综合均不产生完整性原因
        assert payload["background"]["intraday"]["momentum"] is not None
        assert payload["identity"]["membership"]["verified"] is True
        assert payload["sector_synthesis"]["status"] == "ok"
        # 日历缺口在 trend 块原文保留
        assert any("严格交易日历不可用" in g
                   for g in payload["sector_trend"]["gaps"])


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
