# -*- coding: utf-8 -*-
"""个股板块分析模块测试（v2 载荷，issue #81）：

- compose_report 编排：支柱 mock 后验证 v2 载荷与资料状态语义
- 模板渲染：v2 载荷 → Markdown（不含旧综合分档）
- 辅助函数：中位数/数值解析/真实基率表（本地资产，离线可用）
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from src.services.sector_analysis_service import ReportBudget
from src.services import sector_analysis_service as svc


def _budget() -> ReportBudget:
    return ReportBudget(30.0)


def _confirmed_identity() -> Dict[str, Any]:
    return {
        "industry_em": "白酒Ⅱ", "industry_cninfo": "", "business_scope": "",
        "board_name": "白酒Ⅱ", "board_code": "BK1277",
        "subject_status": "confirmed",
        "membership": {"verified": True, "member_count": 20},
        "observed_at": "2026-11-05T14:00:00", "vendor_as_of": None,
        "sw_chain": {"l1": "食品饮料", "l2": "白酒Ⅱ", "l3": "白酒Ⅲ",
                     "l3_code": "851251.SI", "gaps": []},
        "sector_name": "白酒Ⅱ",
        "classification_system": "东财行业板块", "classification_level": None,
        "classification_version": None,
        "gaps": [],
        "_board_table": None,
    }


def _trend_stub(subject_confirmed: bool) -> Dict[str, Any]:
    """趋势支柱替身：eligible 严格跟随资格（契约一致），confirmed 给合格窗口。"""
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


def _peers_stub(subject_confirmed: bool) -> Dict[str, Any]:
    """同业支柱替身：eligible 严格跟随资格；confirmed 给 6 家样本。"""
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


def _catalysts_stub(subject_confirmed: bool) -> Dict[str, Any]:
    """催化支柱替身（issue #83；资格严格跟随；准入/去重语义有专门测试文件）。

    confirmed 时给 1 条已证实催化（发布日期=运行当天，恒在窗口内）+ 1 条
    背景线索；unconfirmed 时不执行。"""
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
        "window_start": today, "window_end": today, "observed_at": "2026-11-05T14:00:03",
        "provider": "Bocha",
        "items": [{
            "evidence_id": "cat-1", "title": "白酒行业新政策发布",
            "url": "https://example.com/news/1", "source_domain": "example.com",
            "snippet": "行业新政策将于近期落地", "published_date": today,
            "date_basis": "provider_published", "date_basis_detail": None,
            "same_event_urls": [],
        }],
        "background_items": [{
            "evidence_id": "bg-1", "title": "白酒行业旧事回顾",
            "url": "https://example.org/news/2", "source_domain": "example.org",
            "snippet": "2020年行业旧事回顾", "published_date": today,
            "downgrade_reason": "标题主题时点早于窗口起始，历史回顾属性，仅背景",
        }],
        "dropped_duplicate_url": 0, "dropped_syndicated": 0,
        "dropped_offwindow": 0, "dropped_offtopic": 0,
        "dropped_invalid_source": 0,
        "source": "套餐搜索（通用行业查询）",
        "gaps": [],
    }


def _synthesis_stub(stock_name: str, code: str, ident: Dict[str, Any],
                    prosper: Dict[str, Any], trend: Dict[str, Any],
                    peers: Dict[str, Any], catalysts: Dict[str, Any], *,
                    budget: ReportBudget, subject_confirmed: bool = False,
                    ) -> Dict[str, Any]:
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
        "text": "白酒行业近期政策发布构成板块催化，当前板块处于同业成交活跃区间",
        "evidence_refs": ["cat-1", "peers"],
    }]
    return {
        "eligible": True, "status": "ok",
        "summary": claims[0]["text"], "claims": claims,
        "uncertainty_note": None, "provider": "openai", "model": "stub-model",
        "observed_at": "2026-11-05T14:00:04", "gaps": [],
    }


class TestComposeReport:
    """compose_report v2 编排（支柱 mock，离线验证）。"""

    @pytest.fixture(autouse=True)
    def _mock_pillars(self, monkeypatch):
        monkeypatch.setattr(
            svc, "_identify_sector", lambda code, *, budget: _confirmed_identity()
        )  # _board_table=None → 行情支柱自行取表（本文件 mock 后无出站）
        # issue #82：趋势/同业支柱与申万链为 compose 直接依赖，一并替身
        # （fixture 行业名非空，不替身申万链会真实出网）
        monkeypatch.setattr(
            svc, "_sector_trend_pillar",
            lambda code, name, *, budget, subject_confirmed=False, current_time=None:
                _trend_stub(subject_confirmed),
        )
        monkeypatch.setattr(
            svc, "_peers_pillar",
            lambda code, cons, *, budget, snapshot_observed_at=None,
            subject_confirmed=False: _peers_stub(subject_confirmed),
        )
        monkeypatch.setattr(
            svc, "_resolve_sw_chain",
            lambda code, text, *, budget: {
                "l1": "食品饮料", "l2": "白酒Ⅱ", "l3": "白酒Ⅲ",
                "l3_code": "851251.SI", "gaps": []},
        )
        monkeypatch.setattr(
            svc, "_policy_pillar",
            lambda text: {"lean": "supportive", "label": "支持", "gaps": []},
        )
        monkeypatch.setattr(
            svc, "_base_rate_pillar",
            lambda text: {
                "base_rate": 0.55, "basis": "industry_base_rate:白酒",
                "hit_keyword": "白酒", "is_default": False,
                "table_version": "v1.0", "table_updated": "2026-09-29",
                "table_industry_count": 21, "percentile": 0.95, "all_rates": [],
            },
        )
        monkeypatch.setattr(
            svc, "_prosperity_pillar",
            lambda name, *, budget, board_code=None, board_table=None: {
                "sector_found": True,
                "momentum": 72.9, "activity": 60.0, "amount_activity": 50.0, "breadth": 90.0,
                "rank_of": 135, "total_boards": 496,
                "row": {"board_name": "白酒Ⅱ", "board_code": "BK1277",
                        "change_pct": 1.65, "turnover_rate": 2.3, "amount": 3.1e9,
                        "up_count": 18, "down_count": 2},
                "gaps": [],
            },
        )
        monkeypatch.setattr(
            svc, "_concepts_pillar",
            lambda code, *, budget: {
                "items": [{"name": "白酒概念", "source": "ifind", "as_of_nature": "latest"}],
                "source": "ifind", "total_returned": 1, "truncated": False, "gaps": [],
            },
        )
        # issue #83：催化/综合支柱为 compose 直接依赖，一并替身（本文件编排
        # 语义验证；统一准入/登记表校验/真实模型链路有专门测试文件）
        monkeypatch.setattr(
            svc, "_catalysts_pillar",
            lambda name, *, budget, subject_confirmed=False, stock_name="":
                _catalysts_stub(subject_confirmed),
        )
        monkeypatch.setattr(svc, "_synthesis_pillar", _synthesis_stub)

    def test_normal_payload_is_unscored_v2(self):
        payload = svc.compose_report("600519", "贵州茅台", budget=_budget())
        assert payload["payload_version"] == 2
        assert payload["assessment"]["status"] == "unscored"
        assert payload["assessment"]["score"] is None
        assert payload["identity"]["subject_status"] == "confirmed"
        assert payload["background"]["policy"]["lean"] == "supportive"
        assert payload["background"]["intraday"]["amount_activity"] == 50.0
        assert payload["concepts"]["items"][0]["name"] == "白酒概念"
        assert payload["gaps"] == []

    def test_base_rate_default_adds_background_gap(self, monkeypatch):
        monkeypatch.setattr(
            svc, "_base_rate_pillar",
            lambda text: {
                "base_rate": 0.5, "basis": "neutral_default", "hit_keyword": "",
                "is_default": True, "table_version": "v1.0", "table_updated": "2026-09-29",
                "table_industry_count": 21, "percentile": None, "all_rates": [],
            },
        )
        payload = svc.compose_report("000001", "平安银行", budget=_budget())
        assert any("默认先验" in g or "0.5" in g for g in payload["gaps"])
        # 默认基率不改变资料状态（主体存在仍是 unscored，也不是任何评分）
        assert payload["assessment"]["status"] == "unscored"
        assert payload["assessment"]["score"] is None

    def test_board_code_passed_to_intraday(self, monkeypatch):
        """当日行情支柱收到身份锁定的板块代码（同键命中）。"""
        seen: Dict[str, Any] = {}

        def _spy(name, *, budget, board_code=None, board_table=None):
            seen["board_code"] = board_code
            seen["board_table"] = board_table
            seen["name"] = name
            return {"sector_found": False, "momentum": None, "activity": None,
                    "amount_activity": None, "breadth": None, "rank_of": None,
                    "total_boards": None, "row": {}, "gaps": ["x"]}

        monkeypatch.setattr(svc, "_prosperity_pillar", _spy)
        svc.compose_report("600519", "贵州茅台", budget=_budget())
        assert seen["board_code"] == "BK1277"
        assert seen["name"] == "白酒Ⅱ"

    def test_concepts_failure_recorded_not_fatal(self, monkeypatch):
        monkeypatch.setattr(
            svc, "_concepts_pillar",
            lambda code, *, budget: {"items": [], "source": "ifind",
                                     "total_returned": 0, "truncated": False,
                                     "gaps": ["相关概念查询失败: mcp down"]},
        )
        payload = svc.compose_report("600519", "贵州茅台", budget=_budget())
        assert any("相关概念" in g for g in payload["gaps"])
        assert payload["assessment"]["status"] == "unscored"


class TestProsperityPillarBehavior:
    """当日行情支柱行为（真实函数，mock 网络边界；基线行为保留）。

    基线 ``test_prosperity_partial_dims``/``test_prosperity_all_missing`` 测的是
    已退役的 ``_prosperity_score`` 聚合函数；其「部分维度产出/全缺失记缺口」
    行为语义按 v2 口径保留在此（不产分、只标缺口）。
    """

    def test_prosperity_partial_dims_records_gaps(self, monkeypatch):
        """行情支柱部分维度缺列：可用维度产出，缺失维度记缺口。"""
        import akshare as ak
        import pandas as pd

        df = pd.DataFrame({
            "板块名称": ["白酒Ⅱ", "电池"], "板块代码": ["BK1277", "BK1033"],
            "涨跌幅": [1.65, 2.0],
            # 换手率/成交额/上涨家数/下跌家数全缺 → activity/amount_activity/breadth 缺口
        })
        monkeypatch.setattr(ak, "stock_board_industry_name_em", lambda: df)
        out = svc._prosperity_pillar("白酒Ⅱ", budget=_budget(), board_code="BK1277")
        assert out["sector_found"] is True
        assert out["momentum"] is not None  # 可用维度产出
        for dim in ("activity", "amount_activity", "breadth"):
            assert out[dim] is None
        assert len(out["gaps"]) >= 3  # 缺失维度逐项记缺口

    def test_prosperity_all_missing_not_found(self, monkeypatch):
        """行情支柱全缺失：sector_found=False 不产任何维度分。"""
        import akshare as ak

        def _em_down():
            raise RuntimeError("em down (fake)")

        monkeypatch.setattr(ak, "stock_board_industry_name_em", _em_down)
        out = svc._prosperity_pillar("白酒Ⅱ", budget=_budget(), board_code="BK1277")
        assert out["sector_found"] is False
        for dim in ("momentum", "activity", "amount_activity", "breadth"):
            assert out[dim] is None
        assert out["gaps"]


class TestMedianAndNum:
    def test_median(self):
        assert svc._median([3, 1, 2]) == 2
        assert svc._median([4, 1, 3, 2]) == 2.5
        assert svc._median([]) is None
        assert svc._median([None, 5]) == 5

    def test_num(self):
        assert svc._num("3.2") == 3.2
        assert svc._num("-") is None
        assert svc._num("") is None
        assert svc._num("abc") is None


class TestBaseRateTableHelpers:
    """真实基率表辅助函数（本地资产，离线可用）。"""

    def test_table_meta_and_percentile(self):
        from src.deep_research_dims.industry_base_rate import (
            all_rates, rate_percentile, table_meta,
        )

        meta = table_meta()
        assert meta["version"] == "v1.0"
        assert meta["industry_count"] >= 20
        rates = all_rates()
        assert any(r["keyword"] == "半导体" and r["base_rate"] == 0.45 for r in rates)
        p = rate_percentile(0.45)
        assert 0.1 <= p <= 0.3
        assert rate_percentile(0.99) == 1.0


class TestRenderReport:
    def test_markdown_renders_v2_sections(self, tmp_path, monkeypatch):
        monkeypatch.setattr(svc, "get_report_dir", lambda: tmp_path)
        monkeypatch.setattr(svc, "_identify_sector",
                            lambda code, *, budget: _confirmed_identity())
        # issue #82：compose 直接依赖的趋势/同业支柱与申万链替身（防真实出网）
        monkeypatch.setattr(
            svc, "_sector_trend_pillar",
            lambda code, name, *, budget, subject_confirmed=False, current_time=None:
                _trend_stub(subject_confirmed),
        )
        monkeypatch.setattr(
            svc, "_peers_pillar",
            lambda code, cons, *, budget, snapshot_observed_at=None,
            subject_confirmed=False: _peers_stub(subject_confirmed),
        )
        monkeypatch.setattr(
            svc, "_resolve_sw_chain",
            lambda code, text, *, budget: {
                "l1": "食品饮料", "l2": "白酒Ⅱ", "l3": "白酒Ⅲ",
                "l3_code": "851251.SI", "gaps": []},
        )
        monkeypatch.setattr(
            svc, "_policy_pillar",
            lambda text: {"lean": "neutral", "label": "中性", "gaps": []},
        )
        monkeypatch.setattr(
            svc, "_base_rate_pillar",
            lambda text: {
                "base_rate": 0.55, "basis": "industry_base_rate:白酒",
                "hit_keyword": "白酒", "is_default": False,
                "table_version": "v1.0", "table_updated": "2026-09-29",
                "table_industry_count": 21, "percentile": 0.95,
                "all_rates": [
                    {"keyword": "白酒", "base_rate": 0.55, "note": "品牌壁垒"},
                    {"keyword": "半导体", "base_rate": 0.45, "note": "高波动强周期"},
                ],
            },
        )
        monkeypatch.setattr(
            svc, "_prosperity_pillar",
            lambda name, *, budget, board_code=None, board_table=None: {
                "sector_found": True, "momentum": 72.9, "activity": 60.0,
                "amount_activity": 50.0, "breadth": 90.0, "rank_of": 135, "total_boards": 496,
                "row": {"board_name": "白酒Ⅱ", "board_code": "BK1277",
                        "change_pct": 1.65, "turnover_rate": 2.3, "amount": 3.1e9,
                        "up_count": 18, "down_count": 2, "leader": "样本股",
                        "leader_change_pct": 5.5},
                "gaps": [],
            },
        )
        monkeypatch.setattr(
            svc, "_concepts_pillar",
            lambda code, *, budget: {"items": [], "gaps": ["相关概念：iFinD 概念入口未启用，本次未列示"]},
        )
        # 落库 monkeypatch：测试不得写真实 DB（id 分钟级会与本机 E2E 冲突）
        from src.storage import get_db

        monkeypatch.setattr(
            get_db(), "save_sector_analysis_report", lambda record, deadline=None: True
        )
        result = svc.generate_report("600519", "贵州茅台")
        assert result["status"] == "success"
        assert result["stock_code"] == "600519"
        md = result["markdown"]
        assert "# 贵州茅台（600519）板块分析报告" in md
        assert "白酒Ⅱ（BK1277）" in md  # 板块名称+代码双确认
        assert "未评分" in md  # 首版不产出综合评分
        assert "不是资金净流入" in md  # 成交额口径澄清
        assert "不代表产业景气" in md  # 当日行情非产业景气
        assert "不是实测胜率" in md  # 基率口径澄清
        assert "v1.0" in md and "2026-09-29" in md  # 基率表版本透出
        assert "⬅" not in md  # 旧全表命中标注已随 v1 模板移除
        assert "综合板块得分" not in md and "顺风" not in md  # 旧分档不再出现
        assert "概念入口未启用" in md  # 概念缺口如实透出
        assert "数据缺口" not in md or "缺口" in md
        assert (tmp_path / f"{result['report_id']}.md").exists()
        # 存档 record 不写旧综合分
        assert result["payload"]["assessment"]["score"] is None

    def test_invalid_code_raises(self):
        with pytest.raises(ValueError):
            svc.generate_report("ABC")
