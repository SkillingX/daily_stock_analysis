# -*- coding: utf-8 -*-
"""财务分析 Pydantic schema 测试 (Layer 3 三层防御)。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.schemas.financial_analysis import (
    FinancialAnalysis,
    FinancialAnalysisDeleteResponse,
    FinancialAnalysisDetailResponse,
    FinancialDimension,
    FinancialAnalysisGenerateResponse,
    FinancialAnalysisListResponse,
    FinancialAnalysisReportItem,
    FinancialAnalysisRequest,
    FinancialValuation,
)


def _valid_dimension(score=50.0, label="OK") -> dict:
    return {"score": score, "label": label}


def _valid_analysis() -> dict:
    return {
        "dims": {
            "profitability": _valid_dimension(70.0, "ROE 良好"),
            "growth": _valid_dimension(60.0, "增速稳健"),
            "safety": _valid_dimension(80.0, "低杠杆"),
            "valuation": _valid_dimension(50.0, "估值合理"),
        },
        "health_score": 65.0,
        "gaps": [],
        "years": [{"year": 2024, "code": "600519", "revenue": 100.0}],
        "rev_yoy": 5.0,
        "np_yoy": 8.0,
        "scissors": 3.0,
        "cash_quality": 1.2,
        "ocf": 12.0,
        "np": 10.0,
        "valuation": {
            "pe_ttm": 25.0,
            "pe_disp": "25.00",
            "pb": 5.0,
            "pb_disp": "5.00",
            "summary": "PE 25 / PB 5",
            "score": 50.0,
        },
    }


# 基类守门

class TestBaseBehavior:
    def test_strict_rejects_type_coercion(self):
        with pytest.raises(ValidationError):
            FinancialAnalysisGenerateResponse.model_validate({
                "report_id": "fa_1",
                "stock_code": "600519",
                "stock_name": "测试",
                "status": "success",
                "markdown": "x",
                "analysis": "not a dict",  # should be dict
            })

    def test_extra_forbid_rejects_unknown(self):
        with pytest.raises(ValidationError):
            FinancialAnalysis.model_validate({**_valid_analysis(), "extra": "x"})

    def test_frozen_prevents_reassignment(self):
        d = FinancialDimension.model_validate(_valid_dimension())
        with pytest.raises(ValidationError):
            d.score = 99.0  # type: ignore[misc]


# 数值范围

class TestNumericRanges:
    def test_health_score_negative_rejected(self):
        with pytest.raises(ValidationError):
            FinancialAnalysis.model_validate({**_valid_analysis(), "health_score": -1})

    def test_health_score_above_100_rejected(self):
        with pytest.raises(ValidationError):
            FinancialAnalysis.model_validate({**_valid_analysis(), "health_score": 150})

    def test_dimension_score_out_of_range_rejected(self):
        bad = _valid_analysis()
        bad["dims"]["profitability"]["score"] = 150
        with pytest.raises(ValidationError):
            FinancialAnalysis.model_validate(bad)

    def test_cash_quality_extreme_rejected(self):
        with pytest.raises(ValidationError):
            FinancialAnalysis.model_validate({**_valid_analysis(), "cash_quality": 50.0})


# 枚举与pattern

class TestEnumsAndPatterns:
    def test_status_valid_values(self):
        for s in ("success", "failed", "already_exists"):
            r = FinancialAnalysisGenerateResponse.model_validate({
                "report_id": "fa_1",
                "stock_code": "600519",
                "stock_name": "测试",
                "status": s,
                "markdown": "x",
                "analysis": _valid_analysis(),
            })
            assert r.status == s

    def test_status_invalid_rejected(self):
        with pytest.raises(ValidationError):
            FinancialAnalysisGenerateResponse.model_validate({
                "report_id": "fa_1",
                "stock_code": "600519",
                "stock_name": "测试",
                "status": "weird_status",
                "markdown": "x",
                "analysis": _valid_analysis(),
            })

    def test_stock_code_pattern(self):
        # 5 位
        with pytest.raises(ValidationError):
            FinancialAnalysisRequest(stock_code="12345")
        # 含字母
        with pytest.raises(ValidationError):
            FinancialAnalysisRequest(stock_code="ABCDEF")


# API I/O

class TestReportItem:
    def _valid_item(self):
        return {
            "id": "fa_202610070000",
            "stock_code": "600519",
            "stock_name": "贵州茅台",
            "created_at": "2026-10-07T10:00:00",
            "md_path": "reports/financial_analysis/fa_1.md",
            "health_score": 65.0,
            "analysis_json": {"any": "dict"},
        }

    def test_valid_item_passes(self):
        item = FinancialAnalysisReportItem.model_validate(self._valid_item())
        assert item.id == "fa_202610070000"
        assert item.health_score == 65.0

    def test_optional_md_path_accepted(self):
        bad = self._valid_item()
        bad["md_path"] = None  # list 端点可能不返回
        item = FinancialAnalysisReportItem.model_validate(bad)
        assert item.md_path is None

    def test_health_score_out_of_range_rejected(self):
        bad = self._valid_item()
        bad["health_score"] = 200
        with pytest.raises(ValidationError):
            FinancialAnalysisReportItem.model_validate(bad)


class TestListAndGenerate:
    def test_list_with_items(self):
        r = FinancialAnalysisListResponse.model_validate({
            "success": True,
            "data": [self._valid_item_dict()],
            "total": 1,
        })
        assert r.total == 1
        assert len(r.data) == 1

    def _valid_item_dict(self):
        return {
            "id": "fa_1",
            "stock_code": "600519",
            "stock_name": None,
            "created_at": "2026-10-07T10:00:00",
            "md_path": None,
            "health_score": 50.0,
            "analysis_json": None,
        }

    def test_generate_response_with_failed_status(self):
        """report_id 可以为 None（status='failed'）。"""
        r = FinancialAnalysisGenerateResponse.model_validate({
            "report_id": None,
            "stock_code": "600519",
            "stock_name": "测试",
            "status": "failed",
            "markdown": "x",
            "analysis": _valid_analysis(),
        })
        assert r.report_id is None
        assert r.status == "failed"

    def test_delete_response(self):
        r = FinancialAnalysisDeleteResponse.model_validate({"success": True, "deleted": "fa_20261007"})
        assert r.deleted == "fa_20261007"
