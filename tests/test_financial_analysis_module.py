# -*- coding: utf-8 -*-
"""个股财务分析模块单测：评分逻辑 + service 编排 + 存储。"""

from __future__ import annotations

FAKE_ANALYSIS = {
    "dims": {
        "profitability": {"score": 70.0, "label": "ROE 良好 / 毛利率 中高"},
        "growth": {"score": 55.0, "label": "剪刀差 -5.0%"},
        "safety": {"score": 70.0, "label": "资产负债率 适中"},
        "valuation": {"score": 66.0, "label": "PE 19x 合理偏低"},
    },
    "health_score": 64.6, "gaps": [],
    "years": [{"year": 2025, "revenue": 1.5e10, "net_profit": 8.6e10,
               "gross_margin": 89.56, "roe": 16.8, "debt_ratio": 25.0,
               "op_cash_flow": 9.2e10, "eps": 68.5}],
    "rev_yoy": 1.3, "np_yoy": -1.9, "scissors": -3.2,
    "valuation": {"pe_ttm": 19.09, "pb": 6.19, "summary": "PE(TTM) 19.1x，合理偏低", "score": 66.0},
}


class TestScoreDims:
    def test_scissors_penalty(self):
        from src.services.financial_analysis_service import _score_dims

        series = {"years": [
            {"year": 2025, "revenue": 120.0, "net_profit": 10.0, "gross_margin": 30.0,
             "roe": 10.0, "debt_ratio": 40.0, "op_cash_flow": 12.0},
            {"year": 2024, "revenue": 100.0, "net_profit": 15.0, "gross_margin": 32.0,
             "roe": 14.0, "debt_ratio": 38.0, "op_cash_flow": 14.0},
        ], "valuation": {"pe_ttm": 20.0, "pb_mrq": 3.0}}
        out = _score_dims(series, {})
        assert out["scissors"] == round((10.0 - 15.0) / 15.0 * 100 - 20.0, 2)
        assert out["scissors"] < -10
        assert out["gaps"] == []

    def test_loss_maker_pb_switch(self):
        from src.services.financial_analysis_service import _score_dims

        series = {"years": [{"year": 2025, "revenue": 100.0, "net_profit": -5.0,
                             "gross_margin": 10.0, "roe": -5.0, "debt_ratio": 60.0}],
                  "valuation": {"pe_ttm": -8.0, "pb_mrq": 2.0}}
        out = _score_dims(series, {})
        assert "PB" in out["valuation"]["summary"]


class TestService:
    def test_generate_with_fake(self, monkeypatch, tmp_path):
        import src.services.financial_analysis_service as svc
        from src.storage import get_db

        get_db().delete_financial_analysis_report("fa_test001")
        monkeypatch.setattr(svc, "_REPORT_DIR", tmp_path)
        monkeypatch.setattr(svc, "_resolve_unique_id", lambda base: "fa_test001")
        monkeypatch.setattr(svc, "_score_dims", lambda series, fund: FAKE_ANALYSIS)
        import src.deep_research_dims.context as ctx_mod

        from types import SimpleNamespace
        monkeypatch.setattr(ctx_mod, "build_shared_context", lambda c, n: SimpleNamespace(fundamental={}))
        monkeypatch.setattr(ctx_mod, "fetch_fuyao_financial_series", lambda c: FAKE_ANALYSIS["years"] and {"years": FAKE_ANALYSIS["years"], "valuation": {}})
        out = svc.generate_report("600519", "贵州茅台")
        assert out["report_id"] == "fa_test001"
        md = out["markdown"]
        for kw in ["财务健康分", "盈利质量", "剪刀差", "资产负债率", "PB", "64.6", "e03131", "font color"]:
            assert kw in md, kw
        get_db().delete_financial_analysis_report("fa_test001")

    def test_non_a_share(self):
        import pytest

        import src.services.financial_analysis_service as svc
        with pytest.raises(ValueError):
            svc.generate_report("TSLA")


class TestStorage:
    def test_roundtrip(self):
        from src.storage import get_db

        db = get_db()
        assert db.save_financial_analysis_report(
            {"id": "fa_rt", "stock_code": "000001", "stock_name": "平安银行",
             "md_path": "/tmp/none.md", "health_score": 64.5, "analysis_json": "{}"}
        )
        assert db.get_financial_analysis_report("fa_rt")["health_score"] == 64.5
        rows, total = db.list_financial_analysis_reports(stock_code="000001")
        assert any(r["id"] == "fa_rt" for r in rows)
        assert db.delete_financial_analysis_report("fa_rt") is not None
        assert db.get_financial_analysis_report("fa_rt") is None
