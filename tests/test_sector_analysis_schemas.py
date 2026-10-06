# -*- coding: utf-8 -*-
"""板块分析 Pydantic schema 测试 (Layer 3 三层防御)。

验证：
- 严格模式：拒绝隐式类型转换（str→int 等）
- frozen：禁止运行期赋值
- extra='forbid'：拒绝未声明字段
- 数值范围：ge/le 守门
- 模式 pattern：stock_code 必须是 6 位数字
- 字面量 Literal：band / lean 必须是白名单

不验证：
- 业务不变式（weights 和 = 1.0、score ∈ [0,100]）—— 由 icontract Layer 2 守门
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.schemas.sector_analysis import (
    BaseRatePillar,
    BaseRateRow,
    Band,
    IndustryIdentification,
    PolicyLean,
    PolicyPillar,
    ProsperityPillar,
    ProsperityRow,
    SectorAnalysis,
    SectorAnalysisDeleteResponse,
    SectorAnalysisDetailResponse,
    SectorAnalysisGenerateResponse,
    SectorAnalysisListResponse,
    SectorAnalysisReportItem,
    SectorAnalysisRequest,
    SectorAnalysisWeights,
    SWChain,
)


def _valid_analysis_dict() -> dict:
    """返回一个合法完整的 compose_analysis 输出 dict。"""
    return {
        "stock_code": "600519",
        "stock_name": "贵州茅台",
        "identification": {
            "industry_em": "白酒",
            "industry_cninfo": "酒、饮料和精制茶制造业",
            "business_scope": "白酒制造",
            "sector_name": "酒、饮料和精制茶制造业",
            "sw_chain": {"l1": "食品饮料", "l2": "白酒Ⅱ", "l3": "白酒Ⅲ",
                         "l3_code": "851251.SI", "gaps": []},
            "gaps": [],
        },
        "policy": {"lean": "neutral", "label": "中性", "score": 50.0, "gaps": []},
        "base_rate": {
            "base_rate": 0.55, "basis": "industry_base_rate:白酒", "hit_keyword": "白酒",
            "is_default": False, "table_version": "v1.0", "table_updated": "2026-09-29",
            "table_industry_count": 21, "percentile": 0.95,
            "all_rates": [{"keyword": "白酒", "base_rate": 0.55, "note": "test"}],
        },
        "prosperity": {
            "sector_found": True, "momentum": 80.0, "activity": 60.0,
            "capital": 40.0, "breadth": 100.0,
            "rank_of": 5, "total_boards": 86,
            "row": {"board_name": "白酒", "change_pct": 3.2,
                    "turnover_rate": 4.1, "up_count": 55, "down_count": 8},
            "gaps": [],
        },
        "prosperity_score": 70.0,
        "sector_score": 64.9,
        "band": "中性",
        "beta_meaning": "板块β基本中性",
        "weights": {"policy": 0.3, "base_rate": 0.3, "prosperity": 0.4},
        "gaps": [],
    }


# ========================================================================
# 基础守门：strict + frozen + extra='forbid'
# ========================================================================

class TestBaseBehavior:
    """所有 schema 共享 _StrictBase 的守门。"""

    def test_strict_rejects_implicit_type_conversion(self):
        """strict=True：拒绝 str → int 的隐式转换。"""
        with pytest.raises(ValidationError):
            SectorAnalysis.model_validate({**_valid_analysis_dict(),
                                            "sector_score": "65"})

    def test_extra_forbid_rejects_unknown_fields(self):
        with pytest.raises(ValidationError) as ei:
            SectorAnalysis.model_validate({**_valid_analysis_dict(),
                                            "extra_field": "x"})
        assert "extra_field" in str(ei.value)

    def test_frozen_prevents_attribute_reassignment(self):
        sa = SectorAnalysis.model_validate(_valid_analysis_dict())
        with pytest.raises(ValidationError):
            sa.sector_score = 99.0  # type: ignore[misc]


# ========================================================================
# 字面量 Literal
# ========================================================================

class TestLiterals:
    def test_band_valid_values(self):
        for b in ("顺风", "偏顺风", "中性", "逆风", "数据不足"):
            d = _valid_analysis_dict()
            d["band"] = b
            assert SectorAnalysis.model_validate(d).band == b

    def test_band_invalid_value_rejected(self):
        d = _valid_analysis_dict()
        d["band"] = "超级顺风"  # 不在白名单
        with pytest.raises(ValidationError):
            SectorAnalysis.model_validate(d)

    def test_policy_lean_valid_values(self):
        for lean in ("supportive", "neutral", "restrictive"):
            d = _valid_analysis_dict()
            d["policy"]["lean"] = lean
            assert SectorAnalysis.model_validate(d).policy.lean == lean

    def test_policy_lean_invalid_value_rejected(self):
        d = _valid_analysis_dict()
        d["policy"]["lean"] = "unknown"
        with pytest.raises(ValidationError):
            SectorAnalysis.model_validate(d)


# ========================================================================
# 数值范围守门
# ========================================================================

class TestNumericRanges:
    def test_sector_score_negative_rejected(self):
        d = _valid_analysis_dict()
        d["sector_score"] = -1
        with pytest.raises(ValidationError):
            SectorAnalysis.model_validate(d)

    def test_sector_score_above_100_rejected(self):
        d = _valid_analysis_dict()
        d["sector_score"] = 150
        with pytest.raises(ValidationError):
            SectorAnalysis.model_validate(d)

    def test_base_rate_out_of_range_rejected(self):
        d = _valid_analysis_dict()
        d["base_rate"]["base_rate"] = 1.5
        with pytest.raises(ValidationError):
            SectorAnalysis.model_validate(d)

    def test_prosperity_momentum_out_of_range_rejected(self):
        d = _valid_analysis_dict()
        d["prosperity"]["momentum"] = 150.0
        with pytest.raises(ValidationError):
            SectorAnalysis.model_validate(d)

    def test_prosperity_rank_negative_rejected(self):
        d = _valid_analysis_dict()
        d["prosperity"]["rank_of"] = 0
        with pytest.raises(ValidationError):
            SectorAnalysis.model_validate(d)


# ========================================================================
# Pattern / 字符串长度
# ========================================================================

class TestPatterns:
    def test_stock_code_6_digits_accepted(self):
        req = SectorAnalysisRequest(stock_code="600519")
        assert req.stock_code == "600519"

    def test_stock_code_5_digits_rejected(self):
        with pytest.raises(ValidationError):
            SectorAnalysisRequest(stock_code="12345")

    def test_stock_code_with_letter_rejected(self):
        with pytest.raises(ValidationError):
            SectorAnalysisRequest(stock_code="ABCDEF")

    def test_gap_too_long_rejected(self):
        d = _valid_analysis_dict()
        d["gaps"].append("x" * 201)  # max_length=200
        with pytest.raises(ValidationError):
            SectorAnalysis.model_validate(d)


# ========================================================================
# API I/O 边界 schema
# ========================================================================

class TestReportItem:
    def _valid_item(self):
        return {
            "id": "sa_202610060000",
            "stock_code": "600519",
            "stock_name": "贵州茅台",
            "created_at": "2026-10-06T22:35:00",
            "md_path": "reports/sector_analysis/sa_202610060000.md",
            "sector_score": 64.81,
            "analysis_json": None,
        }

    def test_valid_item_passes(self):
        item = SectorAnalysisReportItem.model_validate(self._valid_item())
        assert item.id == "sa_202610060000"
        assert item.stock_code == "600519"
        assert item.sector_score == 64.81

    def test_invalid_stock_code_rejected(self):
        bad = self._valid_item()
        bad["stock_code"] = "12345"  # 5 位
        with pytest.raises(ValidationError):
            SectorAnalysisReportItem.model_validate(bad)

    def test_score_out_of_range_rejected(self):
        bad = self._valid_item()
        bad["sector_score"] = 200
        with pytest.raises(ValidationError):
            SectorAnalysisReportItem.model_validate(bad)


class TestListResponse:
    def test_empty_list_passes(self):
        r = SectorAnalysisListResponse.model_validate({"success": True, "data": [], "total": 0})
        assert r.total == 0
        assert r.data == []

    def test_with_items_passes(self):
        r = SectorAnalysisListResponse.model_validate({
            "success": True,
            "data": [
                {"id": "sa_1", "stock_code": "600519", "stock_name": None,
                 "created_at": "2026-10-06T22:35:00",
                 "md_path": "reports/sector_analysis/sa_1.md",
                 "sector_score": 50.0, "analysis_json": None},
            ],
            "total": 1,
        })
        assert r.total == 1
        assert len(r.data) == 1


class TestGenerateResponse:
    def test_valid_response(self):
        r = SectorAnalysisGenerateResponse.model_validate({
            "report_id": "sa_202610062235",
            "stock_code": "600519",
            "stock_name": "贵州茅台",
            "markdown": "# 报告\n...",
            "analysis": _valid_analysis_dict(),
        })
        assert r.report_id == "sa_202610062235"
        assert r.analysis.band == "中性"

    def test_invalid_stock_code_in_analysis_breaks(self):
        bad = _valid_analysis_dict()
        bad["identification"]["industry_em"] = "x" * 65  # max_length=64
        with pytest.raises(ValidationError):
            SectorAnalysisGenerateResponse.model_validate({
                "report_id": "sa_1", "stock_code": "600519", "stock_name": None,
                "markdown": "x", "analysis": bad,
            })


class TestDeleteAndDetail:
    def test_delete_response(self):
        r = SectorAnalysisDeleteResponse.model_validate({"success": True, "deleted": "sa_202610062235"})
        assert r.deleted == "sa_202610062235"

    def test_detail_response(self):
        r = SectorAnalysisDetailResponse.model_validate({
            "success": True,
            "data": {"id": "sa_1", "stock_code": "600519", "stock_name": None,
                     "created_at": "2026-10-06T22:35:00",
                     "md_path": "x.md", "sector_score": None, "analysis_json": None},
        })
        assert r.data.id == "sa_1"


# ========================================================================
# 嵌套结构
# ========================================================================

class TestNestedStructures:
    def test_sw_chain_allows_none(self):
        sc = SWChain()
        assert sc.l1 is None and sc.l3 is None

    def test_base_rate_row_required_fields(self):
        with pytest.raises(ValidationError):
            BaseRateRow.model_validate({"keyword": "", "base_rate": 0.5})  # empty keyword

    def test_weights_sum_not_enforced_at_schema_level(self):
        """weights 和 = 1.0 由 icontract Layer 2 守门，schema 不重复。"""
        # 这里 schema 允许 weights 和 != 1.0，icontract 在 service 层拒绝
        d = _valid_analysis_dict()
        d["weights"] = {"policy": 0.5, "base_rate": 0.5, "prosperity": 0.5}
        sa = SectorAnalysis.model_validate(d)
        assert sa.weights.policy == 0.5
        assert sa.weights.base_rate == 0.5

    def test_optional_prosperity_score_accepted(self):
        d = _valid_analysis_dict()
        d["prosperity_score"] = None
        sa = SectorAnalysis.model_validate(d)
        assert sa.prosperity_score is None
