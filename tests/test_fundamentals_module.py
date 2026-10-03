# -*- coding: utf-8 -*-
"""基本面分析模块单测：service 编排（fake 研究员）+ 模板渲染 + 存储。"""

from __future__ import annotations


class TestFundamentalsService:
    def test_generate_with_fake_researchers(self, monkeypatch, tmp_path):
        import src.services.fundamentals_service as svc

        monkeypatch.setattr(svc, "_REPORT_DIR", tmp_path)
        from src.storage import get_db

        get_db().delete_fundamentals_report("fd_test001")  # 清理历史残留
        monkeypatch.setattr(
            svc, "_resolve_unique_id", lambda base: "fd_test001"
        )

        def _fake_research(dim_id, code, name):
            return {
                "dim": dim_id, "status": "ok", "narrative": f"{dim_id} 分析结论",
                "business_model": {"经营模式": "测试模式（inferred）"},
                "main_products": {"产品": "测试产品"},
                "competitive_position": {"行业地位": "行业第一"},
                "vs_market_leader": {"龙头对比": "市值领先（inferred）"},
                "profitability": {"roe": {"value": 16.8, "label": "优秀"},
                                  "gross_margin": {"value": 89.6, "label": "高毛利"}},
                "growth_quality": {"revenue_yoy": {"value": 1.3, "label": "微增"},
                                   "net_profit_yoy": {"value": -1.9, "label": "下滑"}},
                "valuation_detail": {"assessment": "PE 19x 合理偏低"},
                "data_gaps": [],
            }

        monkeypatch.setattr(svc, "_research_dim", _fake_research)
        monkeypatch.setattr(svc, "_rule_dim", lambda dim_id, code, name, ctx, position_text="": _fake_research(dim_id, code, name))
        import src.deep_research_dims.context as ctx_mod
        monkeypatch.setattr(ctx_mod, "build_shared_context", lambda code, name: object())
        out = svc.generate_fundamentals_report("600519", "贵州茅台")
        assert out["report_id"] == "fd_test001"
        md = out["markdown"]
        for kw in ["经营模式", "主营产品", "行业地位", "龙头", "财务体检", "ROE 16.8", "不构成投资建议"]:
            assert kw in md, kw
        assert (tmp_path / "fd_test001.md").exists()
        from src.storage import get_db

        get_db().delete_fundamentals_report("fd_test001")  # 测试后清理

    def test_code_normalization(self, monkeypatch, tmp_path):
        """300260.SZ / 名称夹代码 应归一化后正常生成（用户实测 bug 回归）。"""
        import src.services.fundamentals_service as svc

        monkeypatch.setattr(svc, "_REPORT_DIR", tmp_path)
        monkeypatch.setattr(svc, "_resolve_unique_id", lambda base: "fd_norm001")
        captured: dict = {}

        def _fake(dim_id, code, name):
            captured["code"] = code
            captured["name"] = name
            return {"dim": dim_id, "status": "degraded", "degraded_reason": "fake"}

        monkeypatch.setattr(svc, "_research_dim", _fake)
        from src.storage import get_db

        get_db().delete_fundamentals_report("fd_norm001")
        out = svc.generate_fundamentals_report("300260.SZ", "新莱应材 300260.SZ")
        assert out["stock_code"] == "300260"
        assert captured["code"] == "300260"
        assert "300260" not in (captured["name"] or "")
        get_db().delete_fundamentals_report("fd_norm001")

    def test_non_a_share_rejected(self):
        import pytest

        import src.services.fundamentals_service as svc
        with pytest.raises(ValueError):
            svc.generate_fundamentals_report("AAPL")


class TestFundamentalsStorage:
    def test_roundtrip(self):
        from src.storage import get_db

        db = get_db()
        ok = db.save_fundamentals_report(
            {"id": "fd_roundtrip", "stock_code": "000001", "stock_name": "平安银行",
             "md_path": "/tmp/none.md", "dims_json": "{}"}
        )
        assert ok
        got = db.get_fundamentals_report("fd_roundtrip")
        assert got and got["stock_name"] == "平安银行"
        rows, total = db.list_fundamentals_reports(stock_code="000001")
        assert any(r["id"] == "fd_roundtrip" for r in rows)
        paths = db.delete_fundamentals_report("fd_roundtrip")
        assert paths is not None
        assert db.get_fundamentals_report("fd_roundtrip") is None
