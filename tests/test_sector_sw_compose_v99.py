# -*- coding: utf-8 -*-
"""板块申万口径编排（issue #99 + amend）：兜底门槛、切换/部分恢复与研究缝测试。

测试缝（与既有板块测试约定一致）：
- `compose_report` 公开服务缝：兜底门槛判定（父规格 #98 决策 6）、东财核心
  事实缝先行（`_compose_eastmoney_core`）→ 确认恢复核心行情才运行研究
  （同业/催化/综合恰好一次）、部分恢复（主体一致锁定但无核心行情）与
  双失败保留申万事实的显式区分（`research_outcome`）、阶段载荷形状；
- 东财链以 `_compose_eastmoney_core` 缝替身 + 同业/催化/综合支柱记录器
  观察（EM 链自身行为有独立测试覆盖）；
- 切换路径的载荷组装/严格校验/渲染真实执行（支柱替身形状取自既有
  编排测试的已验收形状）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from src.schemas.sector_analysis import SectorReportPayload
from src.services import sector_sw_market as _sw_mod
from src.services.sector_analysis_service import (
    ReportBudget,
    compose_report,
)


@pytest.fixture(autouse=True)
def _offline_reference_levels(monkeypatch):
    """单元编排测试离线缝（spec 复审拆缝）：二/三级参考链出站受控替身。

    保留申万路径的载荷组装会在必需研究后补齐参考链——单元编排测试统一
    走受控解析（不读本地凭据、不真实出站）；真实传输与发起时序见
    compose_v100 ``TestOptionalReferenceLevelSequencing`` 与 v99 模块缝/
    API 闭环。替身值与受控身份（煤炭开采/动力煤）一致，断言语义不变。"""
    monkeypatch.setattr(_sw_mod, "sw_http_config",
                        lambda: ("sector-test-refresh-token", 10.0))
    monkeypatch.setattr(
        _sw_mod, "resolve_sw_reference_levels",
        lambda secucode, *, observation_date, refresh_token,
        timeout_seconds, deadline: {
            "l2_name": "煤炭开采", "l3_name": "动力煤", "gaps": []})
    # issue #103 多窗口趋势（新可选出站）受控替身：单元编排测试不真实出站
    monkeypatch.setattr(
        "src.services.sector_analysis_service._sw_trend_windows_pillar",
        lambda *a, **k: {"windows": [], "gaps": []})


def _budget(seconds: float = 120.0) -> ReportBudget:
    return ReportBudget(seconds, finish_margin=min(5.0, seconds / 2.0))


# ---------------------------------------------------------------------------
# 受控申万市场数据链输出（_sw_market_chain 缝形状）
# ---------------------------------------------------------------------------


def _sw_identity(status: str = "confirmed", *, index_code: str = "801950.SL",
                 index_name: str = "煤炭") -> Dict[str, Any]:
    return {
        "status": status,
        "classification_system": ("申万 2021 一级行业" if status == "confirmed"
                                  else None),
        "classification_level": "L1" if status == "confirmed" else None,
        "classification_version": ("sw2021-l1-20261118" if status == "confirmed"
                                   else None),
        "index_code": index_code if status == "confirmed" else None,
        "index_name": index_name if status == "confirmed" else None,
        "vendor_as_of": "2026-11-18" if status == "confirmed" else None,
        "l2_name": "煤炭开采" if status == "confirmed" else None,
        "l3_name": "动力煤" if status == "confirmed" else None,
        "gaps": ([] if status == "confirmed"
                 else ["申万主行业不可确认（受控测试）"]),
    }


def _qualified_row(code: str, change: float) -> Dict[str, Any]:
    return {"thscode": code, "trade_date": "2026-11-18", "trade_time": "22:00:46",
            "change_pct": change, "latest": 100.0 * (1 + change / 100.0),
            "pre_close": 100.0, "amount": 1.0e9, "turnover": 2.0,
            "up_count": 20.0, "down_count": 10.0}


def _sw_core(*, status: str = "confirmed", quote_target_ok: bool = True,
             trend_ok: bool = True, target_change: float = -0.867,
             snapshot_mode: str = "target",
             identity: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """受控申万链输出；snapshot_mode: target=同日合格 / reference=仅前一日。"""
    ident = identity or _sw_identity(status)
    session_date = "2026-11-18"
    if snapshot_mode == "target":
        rows = {ident["index_code"] or "801950.SL": _qualified_row(
            ident["index_code"] or "801950.SL", target_change)}
        snapshot = {
            "session_date": session_date, "qualified_date": session_date,
            "rows": rows, "reference_date": None, "reference_rows": {},
            "target_date_valid": len(rows), "dates_seen": [session_date],
            "gaps": [],
        }
    elif snapshot_mode == "reference":
        ref_date = "2026-11-17"
        ref_rows = {ident["index_code"] or "801950.SL": dict(
            _qualified_row(ident["index_code"] or "801950.SL", 1.2),
            trade_date=ref_date)}
        snapshot = {
            "session_date": session_date, "qualified_date": ref_date,
            "rows": {}, "reference_date": ref_date, "reference_rows": ref_rows,
            "target_date_valid": 0, "dates_seen": [ref_date],
            "gaps": ["受控：目标交易日行情未取得"],
        }
    else:
        snapshot = {
            "session_date": session_date, "qualified_date": None,
            "rows": {}, "reference_date": None, "reference_rows": {},
            "target_date_valid": 0, "dates_seen": [],
            "gaps": ["受控：快照未取得"],
        }
    trend: Dict[str, Any] = {
        "eligible": status == "confirmed",
        "window_completed_sessions": 20, "interval_count": 19,
        "start_date": "2026-10-22", "end_date": "2026-11-18",
        "expected_dates": [], "qualified_sessions": None,
        "observations": [], "period_return_pct": None, "benchmark": None,
        "relative_return_pp": None, "source": None, "observed_at": None,
        "gaps": [],
    }
    if trend_ok:
        trend.update({
            "qualified_sessions": 20,
            "period_return_pct": -0.62,
            "benchmark": {"name": "沪深300", "symbol": "000300.SH",
                          "source": "iFinD 标准HTTP指数日线",
                          "start_date": "2026-10-22", "end_date": "2026-11-18",
                          "qualified_sessions": 20, "observations": [],
                          "period_return_pct": -1.30, "gaps": []},
            "relative_return_pp": 0.68,
            "source": "iFinD 标准HTTP指数日线",
            "observed_at": "2026-11-18T23:00:00",
        })
    else:
        trend["gaps"].append("受控：趋势未取得")
    return {
        "identity": ident, "snapshot": snapshot, "trend": trend,
        "quote_target_ok": quote_target_ok, "trend_ok": trend_ok,
        "gaps": list(ident["gaps"]),
    }


# ---------------------------------------------------------------------------
# 东财核心缝替身 + 研究支柱记录器（切换决策与"研究恰好一次"观察）
# ---------------------------------------------------------------------------


def _em_core_stub(*, subject_status: str = "confirmed",
                  quote_change: Optional[float] = 1.5,
                  trend_return: Optional[float] = None,
                  board_locked: bool = True,
                  ident_gaps: Optional[List[str]] = None) -> Dict[str, Any]:
    """东财核心事实缝替身（身份 + 趋势 + 当日行情，形状对齐真实 core）。"""
    locked_name = "白酒Ⅱ" if board_locked else ""
    locked_code = "BK1277" if board_locked else ""
    return {
        "ident": {
            "industry_em": "白酒Ⅱ", "industry_cninfo": "", "business_scope": "",
            "board_name": locked_name, "board_code": locked_code,
            "subject_status": subject_status,
            "membership": {"verified": subject_status == "confirmed",
                           "member_count": 20 if subject_status == "confirmed" else None,
                           "verified_source": ("cons_table" if subject_status == "confirmed"
                                               else None)},
            "observed_at": "2026-11-05T14:00:00", "vendor_as_of": None,
            "classification_system": "东财行业板块", "classification_level": None,
            "classification_version": None,
            "sw_chain": {"l1": "", "l2": "", "l3": "", "l3_code": "", "gaps": []},
            "sector_name": locked_name or "白酒Ⅱ", "gaps": ident_gaps or [],
        },
        "board_snapshot": None, "cons_snapshot": None, "cons_observed_at": None,
        "cons_batch": None, "f10_members_source": False,
        "confirmed": subject_status == "confirmed",
        "trend": {"eligible": subject_status == "confirmed",
                  "window_completed_sessions": 20, "interval_count": 19,
                  "start_date": None, "end_date": None, "expected_dates": [],
                  "qualified_sessions": None, "observations": [],
                  "period_return_pct": trend_return, "benchmark": None,
                  "relative_return_pp": None, "source": None,
                  "observed_at": "2026-11-05T14:00:02", "gaps": []},
        "prosper": {"sector_found": quote_change is not None,
                    "momentum": None if quote_change is None else 50.0,
                    "activity": 50.0, "amount_activity": 50.0, "breadth": 90.0,
                    "rank_of": 1, "total_boards": 31,
                    "row": {"board_name": locked_name, "board_code": locked_code,
                            "change_pct": quote_change, "turnover_rate": 2.3,
                            "total_market_cap": None, "amount": 3.1e9,
                            "up_count": 18, "down_count": 2,
                            "leader": "", "leader_change_pct": None},
                    "snapshot_source": None, "gaps": []},
    }


class _CoreRecorder:
    def __init__(self, payload: Dict[str, Any]) -> None:
        self.payload = payload
        self.calls: List[str] = []

    def __call__(self, code: str, *, budget: ReportBudget) -> Dict[str, Any]:
        self.calls.append(code)
        return self.payload


class _ResearchStub:
    """研究支柱替身：记录调用次数（切换=恰好一次；未切换=零次）。"""

    def __init__(self, payload: Dict[str, Any]) -> None:
        self.payload = payload
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        self.calls += 1
        return dict(self.payload)


def _peers_stub_payload() -> Dict[str, Any]:
    return {
        "eligible": True,
        "selection_rule": "同一成分快照按成交额前五 + 目标股去重（成交活跃代表，非市值/基本面龙头）",
        "items": [
            {"code": "600519", "name": "贵州茅台", "amount": 3.1e9,
             "change_pct": 1.65, "is_target": True},
            {"code": "000858", "name": "五粮液", "amount": 2.2e9,
             "change_pct": 0.9, "is_target": False},
        ],
        "target_in_amount_top5": True, "target_change_pct_rank": 1,
        "amount_unit": "元", "amount_provenance": "受控测试口径",
        "snapshot_rows": 2, "rankable_rows": 2, "sample_size": 2,
        "snapshot_observed_at": "2026-11-05T14:00:01",
        "source": "东财行业板块成分（身份验证同一快照）", "quotes_as_of": None,
        "gaps": [],
    }


def _catalysts_stub_payload() -> Dict[str, Any]:
    from datetime import date

    today = date.today().isoformat()
    return {
        "eligible": True, "query": "白酒 行业 政策 新闻", "window_days": 3,
        "window_start": today, "window_end": today,
        "observed_at": "2026-11-05T14:00:03", "provider": "Bocha",
        "items": [{
            "evidence_id": "cat-1", "title": "白酒行业新政策发布",
            "url": "https://example.com/news/1", "source_domain": "example.com",
            "snippet": "行业新政策将于近期落地", "published_date": today,
            "date_basis": "provider_published", "date_basis_detail": None,
            "same_event_urls": [], "final_url": None,
        }],
        "background_items": [],
        "dropped_duplicate_url": 0, "dropped_syndicated": 0,
        "dropped_offwindow": 0, "dropped_offtopic": 0,
        "dropped_invalid_source": 0, "dropped_invalid_field": 0,
        "source": "套餐搜索（通用行业查询）", "gaps": [],
    }


def _synthesis_stub_payload() -> Dict[str, Any]:
    return {
        "eligible": True, "status": "ok",
        "summary": "白酒行业近期政策发布构成板块催化，目标股在同业样本中位置居前",
        "claims": [{
            "kind": "interpretation",
            "text": "白酒行业近期政策发布构成板块催化，目标股在同业样本中位置居前",
            "evidence_refs": ["cat-1", "peers"],
        }],
        "uncertainty_note": None, "provider": "openai", "model": "stub-model",
        "observed_at": "2026-11-05T14:00:04", "rejected_count": 0,
        "summary_origin": "server_facts", "gaps": [],
    }


class _ResearchHarness:
    """core 替身 + 三研究支柱记录器的一体化安装（patch 上下文管理）。"""

    def __init__(self, core_payload: Dict[str, Any]) -> None:
        self.core = _CoreRecorder(core_payload)
        self.peers = _ResearchStub(_peers_stub_payload())
        self.catalysts = _ResearchStub(_catalysts_stub_payload())
        self.synthesis = _ResearchStub(_synthesis_stub_payload())
        self._patches: List[Any] = []

    def __enter__(self) -> "_ResearchHarness":
        self._patches = [
            patch("src.services.sector_analysis_service._sw_market_chain",
                  return_value=self._sw_core_value),
            patch("src.services.sector_analysis_service._compose_eastmoney_core",
                  self.core),
            patch("src.services.sector_analysis_service._peers_pillar", self.peers),
            patch("src.services.sector_analysis_service._catalysts_pillar",
                  self.catalysts),
            patch("src.services.sector_analysis_service._synthesis_pillar",
                  self.synthesis),
            # 东财链的可选申万参考链（akshare 出站）一并替身：切换路径的
            # 组装真实执行，可选参考不在本测试关注面（零真实出网）
            patch("src.services.sector_analysis_service._resolve_sw_chain",
                  lambda code, text, *, budget: {
                      "l1": "", "l2": "", "l3": "", "l3_code": "", "gaps": []}),
            patch("src.services.sector_analysis_service._concepts_pillar",
                  lambda code, *, budget: {
                      "items": [], "source": "ifind", "total_returned": 0,
                      "truncated": False, "gaps": ["受控：概念阶段不执行"]}),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        for p in self._patches:
            p.stop()

    def with_sw(self, sw_core: Dict[str, Any]) -> "_ResearchHarness":
        self._sw_core_value = sw_core
        return self

    @property
    def research_calls(self) -> int:
        return self.peers.calls + self.catalysts.calls + self.synthesis.calls


def _run(sw_core: Dict[str, Any], em_core: Dict[str, Any]) -> tuple:
    with _ResearchHarness(em_core).with_sw(sw_core) as h:
        payload = compose_report("600188", "兖矿能源", budget=_budget())
    return payload, h


# ---------------------------------------------------------------------------
# 兜底门槛（决策 6：仅主行业不可确认 或 行情+趋势均不可用）
# ---------------------------------------------------------------------------


class TestSwFallbackGate:
    def test_confirmed_with_quote_no_eastmoney_fallback(self):
        """申万主行业确认 + 目标日行情有效：不触发东财（1/31 但目标有效同型）。

        issue #101 起保留申万路径运行研究（催化/综合记录器各一次；受控链
        无成分集合，同业记录器桩不被调用——申万同业走 _sw_peers_pillar）。
        """
        sw_core = _sw_core(quote_target_ok=True, trend_ok=False)
        em_core = _em_core_stub()
        payload, h = _run(sw_core, em_core)
        assert h.core.calls == []  # 东财核心缝未执行
        assert h.peers.calls == 0
        assert h.catalysts.calls == 1  # 选定申万口径后检索一次（issue #101）
        assert h.synthesis.calls == 1
        assert payload["research_basis"] == "sw2021_l1"
        assert payload["research_outcome"] == "kept_sw_stage"
        assert payload["payload_version"] == 3

    def test_confirmed_with_trend_only_no_fallback(self):
        """趋势合格但目标日行情缺失：任一核心行情可用即不兜底。"""
        sw_core = _sw_core(quote_target_ok=False, trend_ok=True,
                           snapshot_mode="reference")
        payload, h = _run(sw_core, _em_core_stub())
        assert h.core.calls == []
        assert payload["research_outcome"] == "kept_sw_stage"

    def test_main_industry_unconfirmable_triggers_fallback_and_switch(self):
        sw_core = _sw_core(status="unavailable", quote_target_ok=False,
                           trend_ok=False)
        em_core = _em_core_stub(subject_status="confirmed", quote_change=1.5)
        payload, h = _run(sw_core, em_core)
        assert len(h.core.calls) == 1
        assert h.research_calls == 3  # 切换：同业/催化/综合恰好各一次
        assert payload["research_basis"] == "eastmoney_board"
        assert payload["research_outcome"] == "switched_fallback"
        assert payload["payload_version"] == 2
        assert any("兜底" in g and "切换" in g for g in payload["gaps"])

    def test_reference_quote_with_failed_trend_triggers_fallback(self):
        """盘中前收参考 + 19/20 趋势：两项核心行情均不可用 → 尝试东财。"""
        sw_core = _sw_core(quote_target_ok=False, trend_ok=False,
                           snapshot_mode="reference")
        em_core = _em_core_stub(subject_status="confirmed", quote_change=1.5)
        payload, h = _run(sw_core, em_core)
        assert len(h.core.calls) == 1
        assert payload["research_outcome"] == "switched_fallback"

    def test_transport_unavailable_triggers_fallback(self):
        """HTTP 传输不可用 = 主行业不可确认 → 同一预算内尝试东财。"""
        sw_core = _sw_core(
            identity=_sw_identity("unavailable"),
            quote_target_ok=False, trend_ok=False, snapshot_mode="none")
        em_core = _em_core_stub(subject_status="confirmed", quote_change=None,
                                trend_return=-1.0)
        payload, h = _run(sw_core, em_core)
        assert len(h.core.calls) == 1
        assert payload["research_outcome"] == "switched_fallback"


# ---------------------------------------------------------------------------
# 东财切换 / 部分恢复 / 双失败（核心缝先行，研究只运行一次或不运行）
# ---------------------------------------------------------------------------


class TestEastmoneySwitchConditions:
    def test_switches_when_em_confirmed_and_quote_recovered(self):
        sw_core = _sw_core(quote_target_ok=False, trend_ok=False,
                           snapshot_mode="reference")
        em_core = _em_core_stub(subject_status="confirmed", quote_change=1.5)
        payload, h = _run(sw_core, em_core)
        assert payload["research_outcome"] == "switched_fallback"
        assert payload["sector_peers"] is not None  # 完整东财报告
        assert h.research_calls == 3

    def test_switches_when_em_confirmed_and_trend_recovered_only(self):
        sw_core = _sw_core(quote_target_ok=False, trend_ok=False)
        em_core = _em_core_stub(subject_status="confirmed", quote_change=None,
                                trend_return=-1.0)
        payload, h = _run(sw_core, em_core)
        assert payload["research_outcome"] == "switched_fallback"
        assert h.research_calls == 3

    def test_em_confirmed_without_recovery_is_partial_recovery_no_research(self):
        """申万不可确认 + 东财主体确认但核心行情均未恢复：部分恢复（≠切换），
        研究零出站，保留东财口径既有身份（#98：无可保留申万主体时保留
        能建立一致主体的已有资料）。"""
        sw_core = _sw_core(status="unavailable", quote_target_ok=False,
                           trend_ok=False, snapshot_mode="none")
        em_core = _em_core_stub(subject_status="confirmed", quote_change=None,
                                trend_return=None)
        payload, h = _run(sw_core, em_core)
        assert h.research_calls == 0  # 不运行将被丢弃的研究
        assert payload["research_basis"] == "eastmoney_board"
        assert payload["research_outcome"] == "partial_recovery"
        assert payload["payload_version"] == 2
        assert payload["identity"]["subject_status"] == "confirmed"
        assert payload["sector_peers"] is None
        assert payload["sector_catalysts"] is None
        assert payload["sector_synthesis"] is None
        assert payload["assessment"]["completeness"] == "partial"
        assert any("部分恢复" in g and "切换" in g for g in payload["gaps"])

    def test_em_unverified_board_locked_is_partial_recovery_no_research(self):
        """东财板块锁定但成分未验证：可保留一致主体 → 部分恢复，研究零出站。"""
        sw_core = _sw_core(status="unavailable", quote_target_ok=False,
                           trend_ok=False, snapshot_mode="none")
        em_core = _em_core_stub(subject_status="unverified", quote_change=1.5)
        payload, h = _run(sw_core, em_core)
        assert h.research_calls == 0
        assert payload["research_outcome"] == "partial_recovery"
        assert payload["identity"]["subject_status"] == "unverified"
        assert payload["assessment"]["completeness"] == "partial"
        assert any("部分恢复" in g for g in payload["gaps"])

    def test_dual_failure_keeps_sw_facts_when_sw_confirmed(self):
        """双失败且申万主行业已确认：保留申万事实（不切东财、不部分恢复）。

        issue #101：口径已选定（申万），研究运行一次并入报告（不产生被弃
        研究）；东财研究零执行。
        """
        sw_core = _sw_core(quote_target_ok=False, trend_ok=False,
                           snapshot_mode="reference")
        em_core = _em_core_stub(subject_status="confirmed", quote_change=None,
                                trend_return=None)
        payload, h = _run(sw_core, em_core)
        assert h.peers.calls == 0
        assert h.catalysts.calls == 1
        assert h.synthesis.calls == 1
        assert payload["research_basis"] == "sw2021_l1"
        assert payload["research_outcome"] == "kept_sw_stage"
        assert payload["identity"]["classification_system"] == "申万 2021 一级行业"
        assert any("兜底" in g and "东财" in g for g in payload["gaps"])
        assert any("核心行情恢复" in g for g in payload["gaps"])  # 实际失败原因可回查

    def test_no_cohesive_subject_keeps_sw_payload_with_causes(self):
        """申万不可确认 + 东财无一致主体（未锁定）：保留申万载荷 + 双失败原因。

        issue #101：主体不可确认时研究支柱仍产出「未执行」块（替身记录到
        催化/综合各一次；东财同业桩零调用——无一致东财主体）。
        """
        sw_core = _sw_core(status="unavailable", quote_target_ok=False,
                           trend_ok=False, snapshot_mode="none")
        em_core = _em_core_stub(subject_status="missing", board_locked=False,
                                quote_change=None, trend_return=None)
        payload, h = _run(sw_core, em_core)
        assert h.peers.calls == 0
        assert h.catalysts.calls == 1
        assert h.synthesis.calls == 1
        assert payload["research_basis"] == "sw2021_l1"
        assert payload["research_outcome"] == "kept_sw_stage"
        assert payload["identity"]["subject_status"] == "missing"
        assert payload["assessment"]["status"] == "insufficient_data"
        assert any("东财主行业状态 missing" in g for g in payload["gaps"])


# ---------------------------------------------------------------------------
# 申万阶段载荷形状（严格校验 + 阶段范围标注）
# ---------------------------------------------------------------------------


class TestSwStagePayload:
    def _build(self, sw_core: Dict[str, Any],
               em_core: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload, _h = _run(sw_core, em_core or _em_core_stub())
        return payload

    def test_payload_passes_strict_schema(self):
        payload = self._build(_sw_core())
        validated = SectorReportPayload.model_validate(payload)
        assert validated.payload_version == 3
        assert validated.research_basis == "sw2021_l1"
        assert validated.research_outcome == "kept_sw_stage"

    def test_switched_payload_passes_strict_schema(self):
        sw_core = _sw_core(quote_target_ok=False, trend_ok=False,
                           snapshot_mode="reference")
        payload, _h = _run(sw_core, _em_core_stub(subject_status="confirmed",
                                                  quote_change=1.5))
        validated = SectorReportPayload.model_validate(payload)
        assert validated.payload_version == 2
        assert validated.research_outcome == "switched_fallback"

    def test_stage_scope_labeled_in_reasons_and_no_research_blocks(self):
        """研究块随载荷透出（issue #101）：替身研究形状可见，缺口如实。

        issue #100 起同业/成分已接入：受控链无 constituents 时同业为降级
        块（eligible=False + 缺口）；issue #101 起催化/综合在选定口径后
        运行一次（替身形状），不再是 None，缺口随块与 assessment 透出。
        """
        payload = self._build(_sw_core())
        peers = payload["sector_peers"]
        assert peers is not None and peers["eligible"] is False
        assert peers["items"] == [] and peers["gaps"]
        cat = payload["sector_catalysts"]
        syn = payload["sector_synthesis"]
        assert cat is not None and cat["eligible"] is True
        assert cat["items"]  # 替身返回一条合格催化
        assert syn is not None and syn["status"] == "ok"
        # assessment reasons 汇报实际缺口（成分边界），不再有「待接入」阶段标签
        reasons = " ".join(payload["assessment"]["reasons"])
        assert "待后续版本接入" not in reasons
        assert "成分状态未知" in reasons

    def test_identity_carries_sw_classification_metadata(self):
        payload = self._build(_sw_core())
        ident = payload["identity"]
        assert ident["subject_status"] == "confirmed"
        assert ident["classification_system"] == "申万 2021 一级行业"
        assert ident["classification_level"] == "L1"
        assert ident["classification_version"] == "sw2021-l1-20261118"
        assert ident["board_code"] == "801950.SL"
        assert ident["board_name"] == "煤炭"
        assert ident["vendor_as_of"] == "2026-11-18"
        # 成分验证未在本票范围：membership 未验证 + 缺口标注
        assert ident["membership"]["verified"] is False

    def test_snapshot_pillar_has_real_date_and_target_row(self):
        """部分覆盖（仅目标行业合格）：保留事实与日期，不给全量名次。"""
        payload = self._build(_sw_core())
        intraday = payload["background"]["intraday"]
        assert intraday["sector_found"] is True
        assert intraday["snapshot_date"] == "2026-11-18"
        assert intraday["snapshot_mode"] == "target"
        assert intraday["total_boards"] == 31
        assert intraday["rank_of"] is None  # 31/31 同日合格才给全量名次
        assert intraday["row"]["change_pct"] == pytest.approx(-0.867)
        assert "iFinD" in (intraday["snapshot_source"] or "")
        assert any("全量名次" in g for g in intraday["gaps"])

    def test_full_31_snapshot_shows_full_rank(self):
        """31/31 同日合格：显示全量名次（rank/31）。"""
        from src.services.sector_sw_market import SW_L1_DIRECTORY

        target = "801950.SL"
        others = [(c, n) for c, n in SW_L1_DIRECTORY if c != target]
        # 30 个非目标行业各不相同且已知，名次可独立复核
        other_changes = [-15.0 + float(i) for i in range(30)]  # -15.0 … 14.0
        rows = {}
        for (dir_code, _name), ch in zip(others, other_changes):
            rows[dir_code] = _qualified_row(dir_code, ch)
        rows[target] = _qualified_row(target, -0.867)
        sw_core = _sw_core()
        sw_core["snapshot"]["rows"] = rows
        sw_core["snapshot"]["target_date_valid"] = 31
        payload = self._build(sw_core)
        intraday = payload["background"]["intraday"]
        assert intraday["rank_of"] is not None
        assert intraday["total_boards"] == 31
        # 名次 = 严格更高个数 + 1（other_changes > -0.867 的是 0..14 共 15 个）
        strictly_higher = sum(1 for c in other_changes if c > -0.867)
        assert strictly_higher == 15
        assert intraday["rank_of"] == strictly_higher + 1

    def test_reference_snapshot_marks_partial_with_date(self):
        """目标行缺失、上一已完成交易日有合格行：参考口径 + 日期 + 部分。"""
        payload = self._build(_sw_core(quote_target_ok=True, trend_ok=True,
                                       snapshot_mode="reference"))
        intraday = payload["background"]["intraday"]
        assert intraday["snapshot_date"] == "2026-11-17"
        assert intraday["snapshot_mode"] == "reference"
        assert intraday["row"]["change_pct"] == pytest.approx(1.2)
        gaps = " ".join(intraday["gaps"]) + " ".join(
            payload["assessment"]["reasons"])
        assert "2026-11-17" in gaps
        assert payload["assessment"]["completeness"] == "partial"

    def test_trend_block_from_sw_chain(self):
        payload = self._build(_sw_core())
        trend = payload["sector_trend"]
        assert trend is not None
        assert trend["eligible"] is True
        assert trend["period_return_pct"] == pytest.approx(-0.62)
        assert trend["benchmark"]["period_return_pct"] == pytest.approx(-1.30)
        assert trend["relative_return_pp"] == pytest.approx(0.68)
        assert trend["source"] == "iFinD 标准HTTP指数日线"

    def test_unconfirmed_subject_no_cohesive_em_is_insufficient_data(self):
        sw_core = _sw_core(status="unavailable", quote_target_ok=False,
                           trend_ok=False, snapshot_mode="none")
        em_core = _em_core_stub(subject_status="missing", board_locked=False)
        payload, _h = _run(sw_core, em_core)
        assert payload["assessment"]["status"] == "insufficient_data"
        assert payload["identity"]["subject_status"] == "missing"

    def test_stage_completeness_is_partial_even_when_market_data_full(self):
        """市场数据齐全但成分边界未确认（受控链无 constituents）：如实部分。

        issue #101 起完整性按实际取得的合格证据判定：membership 未验证的
        边界差异本身保持部分（成分状态未知，同业为行业参考）。
        """
        payload = self._build(_sw_core())
        assert payload["assessment"]["completeness"] == "partial"


# ---------------------------------------------------------------------------
# amend 三轮：取数截止下的兜底纪律（保留申万部分，不伪报总超时）
# ---------------------------------------------------------------------------


class TestFallbackBudgetCutoff:
    def test_fetch_cutoff_elapsed_skips_fallback_start(self, monkeypatch):
        """取数截止已过（收尾余量仍活）：不启动东财兜底，保留申万部分报告。

        issue #101：申万口径已选定，研究块按预算内的有界降级产出（替身
        不执行预算纪律，记录器照常收到一次调用；真实支柱的预算纪律由
        research_budget/模块测试覆盖）。
        """
        import time as _time

        sw_core = _sw_core(quote_target_ok=False, trend_ok=False,
                           snapshot_mode="reference")
        em_core = _CoreRecorder(_em_core_stub(subject_status="confirmed",
                                              quote_change=1.5))
        monkeypatch.setattr(ReportBudget, "fetch_deadline",
                            property(lambda self: _time.monotonic() - 1.0))
        with _ResearchHarness(em_core.payload).with_sw(sw_core) as h:
            payload = compose_report("600188", "兖矿能源", budget=_budget())
        assert h.core.calls == []  # 兜底未启动
        assert h.peers.calls == 0
        assert h.catalysts.calls == 1
        assert h.synthesis.calls == 1
        assert payload["research_outcome"] == "kept_sw_stage"
        assert any("兜底未启动" in g for g in payload["gaps"])

    def test_budget_exhausted_during_em_core_preserves_sw_facts(self):
        """兜底 core 阶段撞取数截止：保留既有申万事实，不伪报总超时。"""
        sw_core = _sw_core(quote_target_ok=False, trend_ok=False,
                           snapshot_mode="reference")

        def _exhausted_core(code, *, budget):
            from src.services.sector_analysis_service import BudgetExhausted

            raise BudgetExhausted("取数预算已耗尽")

        with patch("src.services.sector_analysis_service._sw_market_chain",
                   return_value=sw_core), \
                patch("src.services.sector_analysis_service._compose_eastmoney_core",
                      _exhausted_core):
            payload = compose_report("600188", "兖矿能源", budget=_budget())
        assert payload["research_basis"] == "sw2021_l1"
        assert payload["research_outcome"] == "kept_sw_stage"
        assert payload["identity"]["subject_status"] == "confirmed"
        assert any("核心阶段到达取数截止" in g for g in payload["gaps"])


# ---------------------------------------------------------------------------
# amend 四：参考口径按实际选用数据推导 + 趋势渲染真源 + 缺口优先级
# ---------------------------------------------------------------------------


class TestReferenceModeDerivation:
    def test_reference_rows_without_target_row_never_marks_reference(self):
        """参考 cohort 无目标指数行：不给参考口径（无主体参考），模式如实。"""
        sw_core = _sw_core(quote_target_ok=False, trend_ok=True,
                           snapshot_mode="reference")
        sw_core["snapshot"]["reference_rows"] = {
            "801120.SL": _qualified_row("801120.SL", 0.5)}  # 只有无关行
        payload, _h = _run(sw_core, _em_core_stub())
        intraday = payload["background"]["intraday"]
        assert intraday["snapshot_mode"] != "reference"
        assert intraday["sector_found"] is False
        assert not intraday["row"]


class TestSwTrendRendering:
    def _render(self, payload: Dict[str, Any]) -> str:
        from src.services.sector_analysis_service import _render

        return _render("兖矿能源", "600188", payload["generated_at"],
                       "sa_rendercheck", payload)

    def test_calendar_unavailable_renders_actual_reason_not_identity_boilerplate(self):
        """已知主体但日历不可用：趋势节渲染实际缺口，不冒称身份未确认。"""
        sw_core = _sw_core(quote_target_ok=True, trend_ok=False)
        sw_core["trend"]["eligible"] = False
        sw_core["trend"]["gaps"] = [
            "板块趋势：严格交易日历不可用，无法确定最近 20 个已完成交易日，"
            "本次不查询行业/基准日线（不按自然日补齐）"]
        payload, _h = _run(sw_core, _em_core_stub())
        md = self._render(payload)
        assert "严格交易日历不可用" in md
        assert "不发起板块/基准行情查询" not in md  # 身份未确认话术不适用

    def test_partial_observations_distinguish_acquisition_from_qualified_return(self):
        """19 个实际 iFinD 收盘已取得但收益不合格：来源真源区分获取与合格。"""
        sw_core = _sw_core(quote_target_ok=True, trend_ok=False)
        sw_core["trend"].update({
            "eligible": True,
            "qualified_sessions": 19,
            "observations": [{"date": "2026-10-22", "close": 2000.0},
                             {"date": "2026-11-18", "close": 2001.0}],
            "source": None,  # 区间收益不合格 → source 如实 None
            "benchmark": {"name": "沪深300", "symbol": "000300.SH",
                          "source": "iFinD 标准HTTP指数日线",
                          "start_date": "2026-10-22", "end_date": "2026-11-18",
                          "qualified_sessions": 20, "observations": [],
                          "period_return_pct": -1.30, "gaps": []},
            "gaps": ["行业日线（煤炭 801950.SL·iFinD）：期望 20 个已完成交易日中"
                     "取得 19 天合格收盘，缺失 1 天（2026-11-18），不取交集/旧日"
                     "凑数，区间收益不计算"],
        })
        payload, _h = _run(sw_core, _em_core_stub())
        md = self._render(payload)
        assert "数据获取与合格区间收益分开标注" in md
        assert "19/20" in md
        assert "东财行业板块日线" not in md  # 申万路由不冒称东财来源
        assert "iFinD" in md  # 实际获取来源如实标注
        # 基准来源独立标注（板块侧不合格不隐藏合格基准来源；盘古排版下
        # 断言用无 CJK/ASCII 边界歧义的子串）
        assert "来源 iFinD" in md and "独立标注" in md and "000300.SH" in md

    def test_zero_industry_observations_with_full_benchmark_copy(self):
        """基准 20 观测、板块零观测：文案为「未取得板块本身合格日线观测」，
        基准来源独立标注且不冒充板块侧已取得。"""
        sw_core = _sw_core(quote_target_ok=True, trend_ok=True)
        sw_core["trend"].update({
            "qualified_sessions": 0,
            "observations": [],
            "source": None,
            "benchmark": {"name": "沪深300", "symbol": "000300.SH",
                          "source": "iFinD 标准HTTP指数日线",
                          "start_date": "2026-10-22", "end_date": "2026-11-18",
                          "qualified_sessions": 20, "observations": [
                              {"date": "2026-10-22", "close": 3200.0}],
                          "period_return_pct": None, "gaps": []},
            "gaps": ["行业日线（煤炭 801950.SL·iFinD）：响应缺行业数据行，趋势未取得"],
        })
        payload, _h = _run(sw_core, _em_core_stub())
        md = self._render(payload)
        assert "未取得板块本身合格日线观测" in md
        assert "来源 iFinD" in md and "000300.SH" in md and "独立标注" in md
        assert "不代表板块侧已取得" in md


class TestGapPriority:
    def test_route_reason_and_market_gaps_survive_gap_cap(self):
        """兜底路由原因与行情/趋势缺口排在最前，背景噪声垫底（上限内存活）。"""
        sw_core = _sw_core(quote_target_ok=False, trend_ok=False,
                           snapshot_mode="reference")
        em_core = _em_core_stub(subject_status="confirmed", quote_change=None,
                                trend_return=None)
        with _ResearchHarness(em_core).with_sw(sw_core), \
                patch("src.services.sector_analysis_service._policy_pillar",
                      lambda text: {"lean": None, "label": "未知",
                                    "gaps": [f"政策噪声 {i}" for i in range(15)]}), \
                patch("src.services.sector_analysis_service._concepts_pillar",
                      lambda code, *, budget: {
                          "items": [], "source": "ifind", "total_returned": 0,
                          "truncated": False,
                          "gaps": [f"概念噪声 {i}" for i in range(15)]}):
            payload = compose_report("600188", "兖矿能源", budget=_budget())
        gaps = payload["gaps"]
        assert "兜底" in gaps[0]  # 路由原因最优先
        joined_head = " ".join(gaps[:4])
        assert "核心行情未恢复" in joined_head or "东财主行业状态" in joined_head
        # 背景噪声被有界截断，不挤占诊断
        assert gaps[-1].startswith("（另有") or "概念噪声" not in gaps[-1]
