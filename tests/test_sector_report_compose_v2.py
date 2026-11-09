# -*- coding: utf-8 -*-
"""板块报告 v2 编排（issue #81）：

- 新报告不沿用旧综合总分/分档/行动建议：载荷无 sector_score/band/beta_meaning/weights。
- 正常「未评分」（unscored）与「资料不足」（insufficient_data）语义分开：
  主行业身份缺失/无法锁定 → insufficient_data；有主体 → unscored（可带缺口）。
- 固定反例：无行业、无政策、无动态景气、默认基率 0.5 → 不得产生 60 分「中性」。
- 当日行情四维准确命名（momentum/activity/amount_activity/breadth）；
  成交额维度是相对活跃度，不是资金净流入；不做名称近似/新浪口径混算。
- 相关概念仅列示（iFinD 已适配入口，有界读取；失败不拖垮主报告）。
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.services.sector_analysis_service import (
    BudgetExhausted,
    ReportBudget,
    _prosperity_pillar,
)


def _budget() -> ReportBudget:
    return ReportBudget(30.0, finish_margin=5.0)


def _trend_stub(subject_confirmed: bool) -> dict:
    """趋势支柱替身（issue #82）：eligible 严格跟随资格。"""
    if not subject_confirmed:
        return {
            "eligible": False, "window_completed_sessions": 20, "interval_count": 19,
            "start_date": None, "end_date": None, "expected_dates": [],
            "period_return_pct": None, "benchmark": None, "relative_return_pp": None,
            "source": None, "observed_at": "2026-11-05T14:00:02",
            "gaps": ["板块趋势：主行业身份未确认（confirmed 前置不满足），不执行板块趋势查询"],
        }
    return {
        "eligible": True, "window_completed_sessions": 20, "interval_count": 19,
        "start_date": "2026-10-09", "end_date": "2026-11-04",
        "expected_dates": ["2026-10-09", "2026-10-12"],
        "period_return_pct": 5.1234,
        "benchmark": {"name": "沪深300", "symbol": "sh000300",
                      "source": "东财指数日线", "start_date": "2026-10-09",
                      "end_date": "2026-11-04", "qualified_sessions": 20,
                      "period_return_pct": 3.8889, "gaps": []},
        "relative_return_pp": 1.2345,
        "source": "东财行业板块日线", "observed_at": "2026-11-05T14:00:02",
        "gaps": [],
    }


def _peers_stub(subject_confirmed: bool) -> dict:
    """同业支柱替身（issue #82）：eligible 严格跟随资格。"""
    if not subject_confirmed:
        return {
            "eligible": False,
            "selection_rule": "同一成分快照按成交额前五 + 目标股去重（成交活跃代表，非市值/基本面龙头）",
            "items": [], "target_in_amount_top5": None, "target_change_pct_rank": None,
            "sample_size": 0, "snapshot_observed_at": None, "source": None,
            "gaps": ["同业比较：主行业身份未确认（confirmed 前置不满足），不执行同业选取"],
        }
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
        "sample_size": 2, "snapshot_observed_at": "2026-11-05T14:00:01",
        "source": "东财行业板块成分（身份验证同一快照）", "gaps": [],
    }


def _catalysts_stub(subject_confirmed: bool) -> dict:
    """催化支柱替身（issue #83；资格严格跟随；准入/去重语义有专门测试文件）。"""
    from datetime import datetime

    if not subject_confirmed:
        return {
            "eligible": False, "query": None, "window_days": None,
            "window_start": None, "window_end": None, "observed_at": None,
            "provider": None, "items": [], "background_items": [],
            "dropped_duplicate_url": 0, "dropped_syndicated": 0,
            "dropped_offwindow": 0, "dropped_offtopic": 0,
            "dropped_invalid_source": 0, "source": None,
            "gaps": ["行业催化：主行业身份未确认（confirmed 前置不满足），不执行行业新闻搜索"],
        }
    today = datetime.now().date().isoformat()
    return {
        "eligible": True, "query": "白酒 行业 政策 新闻", "window_days": 3,
        "window_start": today, "window_end": today,
        "observed_at": "2026-11-05T14:00:03", "provider": "Bocha",
        "items": [{
            "evidence_id": "cat-1", "title": "白酒行业新政策发布",
            "url": "https://example.com/news/1", "source_domain": "example.com",
            "snippet": "行业新政策将于近期落地", "published_date": today,
            "date_basis": "provider_published", "date_basis_detail": None,
            "same_event_urls": [],
        }],
        "background_items": [],
        "dropped_duplicate_url": 0, "dropped_syndicated": 0,
        "dropped_offwindow": 0, "dropped_offtopic": 0,
        "dropped_invalid_source": 0,
        "source": "套餐搜索（通用行业查询）",
        "gaps": [],
    }


def _synthesis_stub(stock_name, code, ident, prosper, trend, peers, catalysts, *,
                    budget, subject_confirmed: bool = False) -> dict:
    """综合支柱替身（issue #83）：有合格催化 → ok + 引用已登记证据的结论。"""
    if not subject_confirmed:
        return {
            "eligible": False, "status": "skipped_identity", "summary": None,
            "claims": [], "uncertainty_note": None, "provider": None,
            "model": None, "observed_at": "2026-11-05T14:00:04",
            "gaps": ["证据化综合：主行业身份未确认（confirmed 前置不满足），不执行模型综合"],
        }
    if not (catalysts.get("items") or []):
        return {
            "eligible": True, "status": "no_qualified_facts", "summary": None,
            "claims": [], "uncertainty_note": None, "provider": None,
            "model": None, "observed_at": "2026-11-05T14:00:04",
            "gaps": ["证据化综合：无已证实近期催化事实，不执行模型综合"],
        }
    claims = [{
        "kind": "interpretation",
        "text": "白酒行业近期政策发布构成板块催化，目标股在同业样本中位置居前",
        "evidence_refs": ["cat-1", "peers"],
    }]
    return {
        "eligible": True, "status": "ok",
        "summary": claims[0]["text"], "claims": claims,
        "uncertainty_note": None, "provider": "openai", "model": "stub-model",
        "observed_at": "2026-11-05T14:00:04", "gaps": [],
    }


def _board_table(rows: list[tuple[str, str]]) -> pd.DataFrame:
    return pd.DataFrame(
        {"板块名称": [r[0] for r in rows], "板块代码": [r[1] for r in rows],
         "涨跌幅": [1.0] * len(rows), "换手率": [2.0] * len(rows),
         "上涨家数": [10] * len(rows), "下跌家数": [5] * len(rows)}
    )


class TestProsperityPillarV2:
    """当日行情支柱：同一东财板块口径，精确匹配，不近似、不混新浪口径。"""

    def test_exact_name_match(self, monkeypatch: pytest.MonkeyPatch):
        import akshare as ak

        monkeypatch.setattr(ak, "stock_board_industry_name_em",
                            lambda: _board_table([("白酒Ⅱ", "BK1277")]))
        out = _prosperity_pillar("白酒Ⅱ", budget=_budget(), board_code="BK1277")
        assert out["sector_found"] is True
        assert out["row"]["board_code"] == "BK1277"
        assert "amount_activity" in out  # 成交额维度新名
        assert "capital" not in out  # 旧「资金」命名不再出现（防净流入误读）

    def test_near_name_not_matched(self, monkeypatch: pytest.MonkeyPatch):
        """板块表只有近似名时不得命中（不近似取首行；名称-only 场景）。"""
        import akshare as ak

        monkeypatch.setattr(ak, "stock_board_industry_name_em",
                            lambda: _board_table([("白酒", "BK0001")]))
        out = _prosperity_pillar("白酒Ⅱ", budget=_budget())
        assert out["sector_found"] is False
        assert any("未精确命中" in g for g in out["gaps"])

    def test_known_board_code_does_not_fall_back_to_same_name(self, monkeypatch: pytest.MonkeyPatch):
        """身份锁定 BK1 后行情表只有同名 BK2：不得按名称回退绑 BK2 行。

        第一轮审查反例：代码未命中即按名称取首行，行情绑定同名不同代码板块。
        """
        import akshare as ak

        monkeypatch.setattr(ak, "stock_board_industry_name_em",
                            lambda: _board_table([("白酒Ⅱ", "BK9999")]))
        out = _prosperity_pillar("白酒Ⅱ", budget=_budget(), board_code="BK1277")
        assert out["sector_found"] is False
        assert out["row"].get("board_code") != "BK9999"
        assert any("不按名称回退" in g for g in out["gaps"])

    def test_board_table_snapshot_reused_no_refetch(self, monkeypatch: pytest.MonkeyPatch):
        """身份阶段取得的板块全表快照传入行情支柱：不再二次查询（同一快照）。"""
        import akshare as ak

        calls: list[str] = []

        def _boards():
            calls.append("fetched")
            return _board_table([("白酒Ⅱ", "BK1277")])

        monkeypatch.setattr(ak, "stock_board_industry_name_em", _boards)
        out = _prosperity_pillar("白酒Ⅱ", budget=_budget(), board_code="BK1277",
                                 board_table=_board_table([("白酒Ⅱ", "BK1277")]))
        assert out["sector_found"] is True
        assert calls == []  # 复用快照，不再出站

    def test_name_multi_rows_not_first_row(self, monkeypatch: pytest.MonkeyPatch):
        """无代码时名称命中多行：不可唯一锁定，不取首行。"""
        import akshare as ak

        monkeypatch.setattr(ak, "stock_board_industry_name_em",
                            lambda: _board_table([("电池", "BK1033"), ("电池", "BK8888")]))
        out = _prosperity_pillar("电池", budget=_budget())
        assert out["sector_found"] is False
        assert any("无法唯一锁定" in g for g in out["gaps"])

    def test_no_sina_fallback_mixing(self, monkeypatch: pytest.MonkeyPatch):
        """东财表失败且单标的快照亦不可用时，不混新浪证监会口径（身份必须同口径）。

        2026-11-07 收敛轮契约：全表不可用 + 板块代码可用时会先走东财单标的
        快照恢复（见 tests/test_sector_board_data_rebuild.py）；本反例固定
        快照同样不可用，验证新浪口径永不混入。
        """
        import src.services.sector_analysis_service as svc
        import akshare as ak

        def _em_down():
            raise RuntimeError("em down")

        def _sina_fail(*a, **k):
            raise AssertionError("当日行情不得回退新浪口径板块表")

        monkeypatch.setattr(ak, "stock_board_industry_name_em", _em_down)
        monkeypatch.setattr(ak, "stock_sector_spot", _sina_fail)
        monkeypatch.setattr(svc, "_fetch_board_quote", lambda *a, **k: None)
        out = _prosperity_pillar("白酒Ⅱ", budget=_budget(), board_code="BK1277")
        assert out["sector_found"] is False
        assert any("东财" in g for g in out["gaps"])

    def test_dims_none_or_score_range(self, monkeypatch: pytest.MonkeyPatch):
        import akshare as ak

        table = _board_table([("电池", "BK1033")])
        # 单一板块表中位数=自身，amount/activity 相对分 = 50*1.0
        monkeypatch.setattr(ak, "stock_board_industry_name_em", lambda: table)
        out = _prosperity_pillar("电池", budget=_budget(), board_code="BK1033")
        for dim in ("momentum", "activity", "amount_activity", "breadth"):
            assert out[dim] is None or 0.0 <= out[dim] <= 100.0


class TestConceptsPillar:
    """相关概念仅列示：失败记缺口不拖垮主报告。"""

    def test_concept_timeout_records_gap_not_fatal(self, monkeypatch: pytest.MonkeyPatch):
        """概念取数预算耗尽：记缺口返回，不向上传播 BudgetExhausted（可选阶段）。"""
        from data_provider.ifind_fundamental_adapter import IfindFetcher

        from src.config import Config
        from src.services.sector_analysis_service import _concepts_pillar

        monkeypatch.setenv("ENABLE_IFIND_MCP", "true")
        monkeypatch.setenv("IFIND_MCP_ENDPOINT", "http://127.0.0.1:9")
        monkeypatch.setenv("IFIND_MCP_TOKEN", "unused")
        Config.reset_instance()
        monkeypatch.setattr(IfindFetcher, "__init__", lambda self, *a, **k: None)
        monkeypatch.setattr(
            "data_provider.ifind_fundamental_adapter.IfindFetcher.concept_membership",
            lambda self, code, *, deadline=None: (_ for _ in ()).throw(BudgetExhausted("预算耗尽")),
        )
        try:
            out = _concepts_pillar("600519", budget=_budget())
            assert out["items"] == []
            assert any("预算" in g for g in out["gaps"])
        finally:
            Config.reset_instance()

    def test_concepts_over_display_cap_truncated_not_vendor_fault(self, monkeypatch: pytest.MonkeyPatch):
        """51 条合法概念：截取展示上限 50 条并如实标注总量与截取缺口，非供应商故障。"""
        from data_provider.ifind_fundamental_adapter import ConceptMembership, IfindFetcher

        from src.config import Config
        from src.services.sector_analysis_service import _concepts_pillar

        monkeypatch.setenv("ENABLE_IFIND_MCP", "true")
        monkeypatch.setenv("IFIND_MCP_ENDPOINT", "http://127.0.0.1:9")
        monkeypatch.setenv("IFIND_MCP_TOKEN", "unused")
        Config.reset_instance()
        membership = ConceptMembership(
            code="600519", concepts=[f"概念{i}" for i in range(51)],
            source="ifind", as_of_nature="latest", errors=[],
        )
        monkeypatch.setattr(IfindFetcher, "__init__", lambda self, *a, **k: None)
        monkeypatch.setattr(
            "data_provider.ifind_fundamental_adapter.IfindFetcher.concept_membership",
            lambda self, code, *, deadline=None: membership,
        )
        try:
            out = _concepts_pillar("600519", budget=_budget())
            assert len(out["items"]) == 50
            assert out["total_returned"] == 51
            assert out["truncated"] is True
            assert any("展示截取，非供应商故障" in g for g in out["gaps"])
        finally:
            Config.reset_instance()

    def test_disabled_entry_records_gap(self, monkeypatch: pytest.MonkeyPatch):
        from src.config import Config
        from src.services.sector_analysis_service import _concepts_pillar

        monkeypatch.setenv("ENABLE_IFIND_MCP", "false")
        Config.reset_instance()
        try:
            out = _concepts_pillar("600519", budget=_budget())
            assert out["items"] == []
            assert any("概念" in g for g in out["gaps"])
        finally:
            Config.reset_instance()

    def test_membership_listed_with_source(self, monkeypatch: pytest.MonkeyPatch):
        from data_provider.ifind_fundamental_adapter import ConceptMembership, IfindFetcher

        from src.config import Config
        from src.services.sector_analysis_service import _concepts_pillar

        monkeypatch.setenv("ENABLE_IFIND_MCP", "true")
        monkeypatch.setenv("IFIND_MCP_ENDPOINT", "http://127.0.0.1:9")
        monkeypatch.setenv("IFIND_MCP_TOKEN", "unused")
        Config.reset_instance()
        captured: dict = {}

        class _FakeFetcher:
            def __init__(self, *a, **k) -> None:
                pass

            def concept_membership(self, code, *, deadline=None):
                captured["code"] = code
                captured["deadline"] = deadline
                return ConceptMembership(code=code, concepts=["白酒概念", "超级品牌"],
                                         source="ifind", as_of_nature="latest", errors=[])

        monkeypatch.setattr(IfindFetcher, "__init__", lambda self, *a, **k: None)
        monkeypatch.setattr(
            "data_provider.ifind_fundamental_adapter.IfindFetcher.concept_membership",
            lambda self, code, *, deadline=None: _FakeFetcher().concept_membership(code, deadline=deadline),
        )
        try:
            out = _concepts_pillar("600519", budget=_budget())
            assert [i["name"] for i in out["items"]] == ["白酒概念", "超级品牌"]
            assert all(i["source"] == "ifind" for i in out["items"])
            assert captured["deadline"] is not None  # 绝对单调截止时刻传入适配器
        finally:
            Config.reset_instance()

    def test_supplier_failure_becomes_gap_not_error(self, monkeypatch: pytest.MonkeyPatch):
        from data_provider.ifind_fundamental_adapter import IfindFetcher

        from src.config import Config
        from src.services.sector_analysis_service import _concepts_pillar

        monkeypatch.setenv("ENABLE_IFIND_MCP", "true")
        monkeypatch.setenv("IFIND_MCP_ENDPOINT", "http://127.0.0.1:9")
        monkeypatch.setenv("IFIND_MCP_TOKEN", "unused")
        Config.reset_instance()

        def _boom(self, code, *, deadline=None):
            raise RuntimeError("mcp down")

        monkeypatch.setattr(IfindFetcher, "__init__", lambda self, *a, **k: None)
        monkeypatch.setattr(
            "data_provider.ifind_fundamental_adapter.IfindFetcher.concept_membership", _boom
        )
        try:
            out = _concepts_pillar("600519", budget=_budget())
            assert out["items"] == []
            assert any("概念" in g for g in out["gaps"])
        finally:
            Config.reset_instance()


class TestComposeReport:
    """compose_report v2 编排契约（支柱 mock 后离线验证）。"""

    @pytest.fixture(autouse=True)
    def _base_mocks(self, monkeypatch):
        self._identity: dict = {
            "industry_em": "白酒Ⅱ", "industry_cninfo": "", "business_scope": "",
            "board_name": "白酒Ⅱ", "board_code": "BK1277",
            "subject_status": "confirmed",
            "membership": {"verified": True, "member_count": 20},
            "observed_at": "2026-11-05T14:00:00", "vendor_as_of": None,
            "classification_system": "东财行业板块", "classification_level": None,
            "classification_version": None,
            "sw_chain": {"l1": "食品饮料", "l2": "白酒Ⅱ", "l3": "白酒Ⅲ",
                         "l3_code": "851251.SI", "gaps": []},
            "sector_name": "白酒Ⅱ",
            "gaps": [],
        }
        monkeypatch.setattr(
            "src.services.sector_analysis_service._identify_sector",
            lambda code, *, budget: dict(self._identity),
        )
        monkeypatch.setattr(
            "src.services.sector_analysis_service._policy_pillar",
            lambda text: {"lean": "supportive", "label": "支持", "gaps": []},
        )
        monkeypatch.setattr(
            "src.services.sector_analysis_service._base_rate_pillar",
            lambda text: {
                "base_rate": 0.55, "basis": "industry_base_rate:白酒",
                "hit_keyword": "白酒", "is_default": False,
                "table_version": "v1.0", "table_updated": "2026-09-29",
                "table_industry_count": 21, "percentile": 0.95, "all_rates": [],
            },
        )
        monkeypatch.setattr(
            "src.services.sector_analysis_service._prosperity_pillar",
            lambda name, *, budget, board_code=None, board_table=None: {
                "sector_found": True, "momentum": 80.0, "activity": 60.0,
                "amount_activity": 70.0, "breadth": 66.7,
                "rank_of": 135, "total_boards": 496,
                "row": {"board_name": "白酒Ⅱ", "board_code": "BK1277",
                        "change_pct": 1.65, "turnover_rate": 2.3, "amount": 3.1e9,
                        "up_count": 18, "down_count": 2},
                "gaps": [],
            },
        )
        monkeypatch.setattr(
            "src.services.sector_analysis_service._concepts_pillar",
            lambda code, *, budget: {
                "items": [{"name": "白酒概念", "source": "ifind", "as_of_nature": "latest"}],
                "gaps": [],
            },
        )
        # issue #82：趋势/同业支柱与申万链为 compose 直接依赖，一并替身
        # （fixture 行业名非空，不替身申万链会真实出网）
        monkeypatch.setattr(
            "src.services.sector_analysis_service._sector_trend_pillar",
            lambda code, name, *, budget, subject_confirmed=False, current_time=None:
                _trend_stub(subject_confirmed),
        )
        monkeypatch.setattr(
            "src.services.sector_analysis_service._peers_pillar",
            lambda code, cons, *, budget, snapshot_observed_at=None,
            subject_confirmed=False, **_kwargs: _peers_stub(subject_confirmed),
        )
        monkeypatch.setattr(
            "src.services.sector_analysis_service._resolve_sw_chain",
            lambda code, text, *, budget: {
                "l1": "食品饮料", "l2": "白酒Ⅱ", "l3": "白酒Ⅲ",
                "l3_code": "851251.SI", "gaps": []},
        )
        # issue #83：催化/综合支柱为 compose 直接依赖，一并替身（本文件编排
        # 契约验证；统一准入/登记表校验/真实模型链路有专门测试文件）
        monkeypatch.setattr(
            "src.services.sector_analysis_service._catalysts_pillar",
            lambda name, *, budget, subject_confirmed=False, stock_name="":
                _catalysts_stub(subject_confirmed),
        )
        monkeypatch.setattr(
            "src.services.sector_analysis_service._synthesis_pillar",
            _synthesis_stub,
        )

    def test_normal_report_is_unscored_without_legacy_composite(self):
        from src.services.sector_analysis_service import compose_report

        payload = compose_report("600519", "贵州茅台", budget=_budget())
        assert payload["payload_version"] == 2
        assert payload["assessment"]["status"] == "unscored"
        assert payload["assessment"]["score"] is None  # 显式未评分，不伪填
        # 旧综合总分/分档/β含义/权重：保留路径但显式退役（恒 None，不恢复旧结论）
        for legacy_key in ("sector_score", "band", "beta_meaning", "weights",
                           "prosperity_score"):
            assert legacy_key in payload  # 追加兼容：旧路径仍在
            assert payload[legacy_key] is None  # 退役语义：显式 None
        # 非评分事实路径投影可读（既有调用者兼容）
        assert payload["identification"]["industry_em"] == "白酒Ⅱ"
        assert payload["policy"]["lean"] == "supportive"
        assert payload["policy"]["score"] is None  # 政策档位分退役
        assert payload["prosperity"]["momentum"] == 80.0
        assert payload["prosperity"]["capital"] is None  # 旧「资金」维度退役
        assert payload["base_rate"]["base_rate"] == 0.55
        assert payload["identity"]["subject_status"] == "confirmed"
        assert payload["background"]["policy"]["label"] == "支持"
        assert payload["background"]["base_rate"]["is_default"] is False
        assert payload["background"]["intraday"]["amount_activity"] == 70.0
        assert payload["concepts"]["items"][0]["name"] == "白酒概念"

    def test_missing_everything_with_default_base_rate_is_insufficient(self):
        """固定反例：无行业、无政策、无动态景气、默认基率 0.5 → 资料不足，无 60 分中性。"""
        from src.services.sector_analysis_service import compose_report

        self._identity.update({
            "industry_em": "", "industry_cninfo": "", "board_name": "",
            "board_code": "", "subject_status": "missing",
            "membership": {"verified": False, "member_count": None},
            "sector_name": "", "gaps": ["板块识别失败：无行业数据"],
        })
        import src.services.sector_analysis_service as svc

        orig_policy = svc._policy_pillar
        svc._policy_pillar = lambda text: {  # type: ignore[assignment]
            "lean": None, "label": "未知", "gaps": ["政策倾向查询失败: 未命中"],
        }
        svc._base_rate_pillar = lambda text: {  # type: ignore[assignment]
            "base_rate": 0.5, "basis": "neutral_default", "hit_keyword": "",
            "is_default": True, "table_version": "v1.0", "table_updated": "2026-09-29",
            "table_industry_count": 21, "percentile": None, "all_rates": [],
        }
        svc._prosperity_pillar = lambda name, *, budget, board_code=None, board_table=None: {  # type: ignore[assignment]
            "sector_found": False, "momentum": None, "activity": None,
            "amount_activity": None, "breadth": None, "rank_of": None,
            "total_boards": None, "row": {}, "gaps": ["板块表为空"],
        }
        try:
            payload = compose_report("999999", "不存在的股票", budget=_budget())
        finally:
            svc._policy_pillar = orig_policy  # type: ignore[assignment]
        assert payload["assessment"]["status"] == "insufficient_data"
        assert payload["assessment"]["score"] is None
        assert payload["assessment"]["reasons"]  # 资料不足须给出具体原因
        assert payload["identity"]["subject_status"] == "missing"
        # 分档/行动结论退役：band/sector_score 显式 None，无任何「60 分中性」结论
        assert payload["band"] is None and payload["sector_score"] is None
        assert payload["assessment"]["completeness"] == "partial"

    def test_ambiguous_identity_is_insufficient(self):
        from src.services.sector_analysis_service import compose_report

        self._identity.update({
            "board_name": "", "board_code": "", "subject_status": "ambiguous",
            "membership": {"verified": False, "member_count": None},
            "gaps": ["板块表无「白酒Ⅱ」精确命中，存在近似名 ['白酒']（不近似取首行）"],
        })
        from src.services.sector_analysis_service import compose_report as compose

        payload = compose("600519", "贵州茅台", budget=_budget())
        assert payload["assessment"]["status"] == "insufficient_data"

    def test_unverified_membership_still_renders_with_gap(self):
        """板块锁定但成分未验证：部分报告继续（unscored + 明确缺口），不假成功。"""
        from src.services.sector_analysis_service import compose_report

        self._identity.update({
            "subject_status": "unverified",
            "membership": {"verified": False, "member_count": 20},
            "gaps": ["东财板块「白酒Ⅱ」（BK1277）成分 20 只不含目标股 600519，归属未确认"],
        })
        payload = compose_report("600519", "贵州茅台", budget=_budget())
        assert payload["assessment"]["status"] == "unscored"
        assert any("归属未确认" in g for g in payload["gaps"])

    def test_normal_complete_report_marks_completeness_complete(self):
        """正常主体+当日行情齐备：completeness=complete（未评分≠资料不足）。"""
        from src.services.sector_analysis_service import compose_report

        payload = compose_report("600519", "贵州茅台", budget=_budget())
        assert payload["assessment"]["status"] == "unscored"
        assert payload["assessment"]["completeness"] == "complete"

    def test_confirmed_but_intraday_missing_marks_partial_with_reason(self):
        """confirmed 但当日行情全缺：完整性 partial 并给出原因，不与正常报告无异。"""
        from src.services.sector_analysis_service import compose_report

        import src.services.sector_analysis_service as svc

        orig = svc._prosperity_pillar
        svc._prosperity_pillar = lambda name, *, budget, board_code=None, board_table=None: {  # type: ignore[assignment]
            "sector_found": False, "momentum": None, "activity": None,
            "amount_activity": None, "breadth": None, "rank_of": None,
            "total_boards": None, "row": {},
            "gaps": ["当日行情：东财行业板块表查询失败 down (fake)"],
        }
        try:
            payload = compose_report("600519", "贵州茅台", budget=_budget())
        finally:
            svc._prosperity_pillar = orig  # type: ignore[assignment]
        assert payload["assessment"]["status"] == "unscored"  # 主体在：仍是未评分
        assert payload["assessment"]["completeness"] == "partial"
        assert any("当日板块行情" in r for r in payload["assessment"]["reasons"])

    def test_optional_concepts_budget_exhausted_keeps_partial_report(self):
        """概念阶段预算耗尽：compose_report 记缺口返回，已取得事实不丢。"""
        from src.services.sector_analysis_service import compose_report

        import src.services.sector_analysis_service as svc

        orig = svc._concepts_pillar
        svc._concepts_pillar = lambda code, *, budget: {  # type: ignore[assignment]
            "items": [], "source": "ifind", "total_returned": 0, "truncated": False,
            "gaps": ["相关概念：取数预算不足，本次未读取（可选阶段，不影响主报告）"],
        }
        try:
            payload = compose_report("600519", "贵州茅台", budget=_budget())
        finally:
            svc._concepts_pillar = orig  # type: ignore[assignment]
        assert payload["assessment"]["status"] == "unscored"
        assert payload["background"]["intraday"]["sector_found"] is True
        assert any("预算不足" in g for g in payload["gaps"])

    def test_identity_carries_classification_metadata_unknown(self):
        """分类体系标注，层级/版本供应商未返回 → None（如实未知，不推算）。"""
        from src.services.sector_analysis_service import compose_report

        payload = compose_report("600519", "贵州茅台", budget=_budget())
        ident = payload["identity"]
        assert ident["classification_system"] == "东财行业板块"
        assert ident["classification_level"] is None
        assert ident["classification_version"] is None

    def test_mixed_rejected_output_marks_partial_with_reason(self):
        """边界终态（issue #83）：综合 ok 但存在被拒结论（rejected_count>0）
        → 完整性 partial + 显式 rejected-output 原因；合格结论保留。"""
        from src.services.sector_analysis_service import compose_report

        import src.services.sector_analysis_service as svc

        orig = svc._synthesis_pillar

        def _mixed_stub(stock_name, code, ident, prosper, trend, peers,
                        catalysts, *, budget, subject_confirmed=False):
            out = _synthesis_stub(stock_name, code, ident, prosper, trend,
                                  peers, catalysts, budget=budget,
                                  subject_confirmed=subject_confirmed)
            if out.get("status") == "ok":
                out["rejected_count"] = 2
                out["gaps"] = ["证据化综合：模型返回 2 条未通过校验的结论已整条拒绝"]
            return out

        svc._synthesis_pillar = _mixed_stub  # type: ignore[assignment]
        try:
            payload = compose_report("600519", "贵州茅台", budget=_budget())
        finally:
            svc._synthesis_pillar = orig  # type: ignore[assignment]
        assert payload["sector_synthesis"]["status"] == "ok"
        assert payload["sector_synthesis"]["claims"]  # 合格结论保留
        assert payload["sector_synthesis"]["rejected_count"] == 2
        assert payload["assessment"]["completeness"] == "partial"
        assert any("已拒绝的结论输出" in r and "完整性不为完整" in r
                   for r in payload["assessment"]["reasons"])

    def test_all_invalid_rejected_count_zero_keeps_malformed_partial(self):
        """全部被拒（malformed_output）：rejected_count 恒 0、partial 语义不变。"""
        from src.services.sector_analysis_service import compose_report

        import src.services.sector_analysis_service as svc

        orig = svc._synthesis_pillar

        def _all_invalid_stub(stock_name, code, ident, prosper, trend, peers,
                              catalysts, *, budget, subject_confirmed=False):
            return {
                "eligible": True, "status": "malformed_output",
                "summary": None, "claims": [], "uncertainty_note": None,
                "rejected_count": 0, "provider": None, "model": None,
                "observed_at": "2026-11-05T14:00:04",
                "gaps": ["证据化综合：模型结论均未通过结论/引用/结构与量值边界校验"],
            }

        svc._synthesis_pillar = _all_invalid_stub  # type: ignore[assignment]
        try:
            payload = compose_report("600519", "贵州茅台", budget=_budget())
        finally:
            svc._synthesis_pillar = orig  # type: ignore[assignment]
        assert payload["sector_synthesis"]["status"] == "malformed_output"
        assert payload["sector_synthesis"]["rejected_count"] == 0
        assert payload["assessment"]["completeness"] == "partial"
        assert any("证据化综合未产出" in r
                   for r in payload["assessment"]["reasons"])
