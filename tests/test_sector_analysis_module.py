# -*- coding: utf-8 -*-
"""个股板块分析模块测试：三支柱评分/基率版本透出/综合权重/模板渲染（全离线 mock）。"""

from __future__ import annotations

import pytest

from src.services import sector_analysis_service as svc


class TestComposeAnalysis:
    """三支柱 mock 后的综合编排验证。"""

    @pytest.fixture(autouse=True)
    def _mock_pillars(self, monkeypatch):
        monkeypatch.setattr(
            svc, "_identify_sector",
            lambda code: {
                "industry_em": "半导体设备",
                "industry_cninfo": "", "sw_chain": {"l1": "", "l2": "", "l3": "", "l3_code": "", "gaps": []},
                "sector_name": "半导体",
                "gaps": ["申万三级链：接口不可用"],
            },
        )
        monkeypatch.setattr(
            svc, "_policy_pillar",
            lambda text: {"lean": "supportive", "label": "支持", "score": 66.0, "gaps": []},
        )
        monkeypatch.setattr(
            svc, "_base_rate_pillar",
            lambda text: {
                "base_rate": 0.45, "basis": "industry_base_rate:半导体",
                "hit_keyword": "半导体", "is_default": False,
                "table_version": "v1.0", "table_updated": "2026-09-29",
                "table_industry_count": 21, "percentile": 0.4,
                "all_rates": [{"keyword": "半导体", "base_rate": 0.45, "note": "test"}],
            },
        )
        monkeypatch.setattr(
            svc, "_prosperity_pillar",
            lambda name: {
                "sector_found": True,
                "momentum": 80.0, "activity": 60.0, "capital": 40.0, "breadth": 100.0,
                "rank_of": 12, "total_boards": 86,
                "row": {"board_name": "半导体", "change_pct": 3.2, "turnover_rate": 4.1,
                        "up_count": 55, "down_count": 8},
                "gaps": [],
            },
        )

    def test_full_compose_weights(self):
        a = svc.compose_analysis("603690", "至纯科技")
        # 景气分 = (80+60+40+100)/4 = 70
        assert a["prosperity_score"] == 70.0
        # 综合 = 66*0.3 + (30+0.45*60)*0.3 + 70*0.4 = 19.8 + 17.1 + 28 = 64.9
        assert a["sector_score"] == pytest.approx(64.9, abs=0.01)
        assert a["band"] == "中性"  # 64.9 < 65，落在 50-65 中性档
        assert a["identification"]["industry_em"] == "半导体设备"
        assert a["base_rate"]["table_version"] == "v1.0"
        assert a["base_rate"]["is_default"] is False

    def test_verdict_bands(self):
        assert svc._verdict(80)[0] == "顺风"
        assert svc._verdict(70)[0] == "偏顺风"
        assert svc._verdict(60)[0] == "中性"
        assert svc._verdict(40)[0] == "逆风"
        assert svc._verdict(None)[0] == "数据不足"

    def test_base_rate_default_adds_gap(self, monkeypatch):
        monkeypatch.setitem(
            svc._base_rate_pillar("__probe__") if False else {}, "", ""
        )  # placeholder 防误用
        monkeypatch.setattr(
            svc, "_base_rate_pillar",
            lambda text: {
                "base_rate": 0.5, "basis": "neutral_default", "hit_keyword": "",
                "is_default": True, "table_version": "v1.0", "table_updated": "2026-09-29",
                "table_industry_count": 21, "percentile": None, "all_rates": [],
            },
        )
        a = svc.compose_analysis("000001", "平安银行")
        assert any("可迭代基率表" in g for g in a["gaps"])

    def test_prosperity_partial_dims(self):
        """部分维度缺列：用可用维度均值，缺口列出。"""
        p = {
            "sector_found": True, "momentum": 80.0, "activity": None,
            "capital": None, "breadth": 100.0, "gaps": ["x"],
        }
        assert svc._prosperity_score(p) == 90.0

    def test_prosperity_all_missing(self):
        assert svc._prosperity_score({"sector_found": False}) is None


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
        # 半导体 0.45 高于表内 0.4/0.3 的行业（新能源/煤炭钢铁/地产/环保建筑 等），低于 0.5/0.55
        p = rate_percentile(0.45)
        assert 0.1 <= p <= 0.3
        assert rate_percentile(0.99) == 1.0


class TestRenderReport:
    def test_markdown_renders_all_sections(self, tmp_path, monkeypatch):
        monkeypatch.setattr(svc, "get_report_dir", lambda: tmp_path)
        monkeypatch.setattr(
            svc, "_identify_sector",
            lambda code: {"industry_em": "半导体", "industry_cninfo": "", "sw_chain": {"l1": "", "l2": "", "l3": "", "l3_code": "", "gaps": []}, "sector_name": "半导体", "gaps": []},
        )
        monkeypatch.setattr(
            svc, "_policy_pillar",
            lambda text: {"lean": "neutral", "label": "中性", "score": 50.0, "gaps": []},
        )
        monkeypatch.setattr(
            svc, "_base_rate_pillar",
            lambda text: {
                "base_rate": 0.45, "basis": "industry_base_rate:半导体",
                "hit_keyword": "半导体", "is_default": False,
                "table_version": "v1.0", "table_updated": "2026-09-29",
                "table_industry_count": 21, "percentile": 0.4,
                "all_rates": [
                    {"keyword": "半导体", "base_rate": 0.45, "note": "高波动强周期"},
                    {"keyword": "白酒", "base_rate": 0.55, "note": "品牌壁垒"},
                ],
            },
        )
        monkeypatch.setattr(
            svc, "_prosperity_pillar",
            lambda name: {
                "sector_found": True, "momentum": 70.0, "activity": 60.0,
                "capital": 50.0, "breadth": 80.0, "rank_of": 20, "total_boards": 86,
                "row": {"board_name": "半导体", "change_pct": 2.1, "turnover_rate": 3.5,
                        "total_market_cap": 2.5e12, "amount": 8e10,
                        "up_count": 40, "down_count": 20, "leader": "中芯国际",
                        "leader_change_pct": 5.5},
                "gaps": [],
            },
        )
        # 落库 monkeypatch：测试不得写真实 DB（id 分钟级会与本机 E2E 冲突）
        from src.storage import get_db

        monkeypatch.setattr(
            get_db(), "save_sector_analysis_report", lambda record: True
        )
        result = svc.generate_report("603690", "至纯科技")
        assert result["stock_code"] == "603690"
        md = result["markdown"]
        assert "# 至纯科技（603690）板块分析报告" in md
        assert "半导体" in md
        assert "v1.0" in md and "2026-09-29" in md  # 基率表版本透出
        assert "⬅ 本次命中" in md  # 全表命中标注
        assert "政策" in md and "景气" in md
        assert "数据缺口" in md
        assert result["analysis"]["sector_score"] is not None

    def test_invalid_code_raises(self):
        with pytest.raises(ValueError):
            svc.generate_report("ABC")
