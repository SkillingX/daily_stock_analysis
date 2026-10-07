# -*- coding: utf-8 -*-
"""财务分析模块 Pydantic v2 Schema（Layer 3 三层防御）。

对齐 `src/schemas/sector_analysis.py` 的「strict + frozen + validate-on-assign +
extra='forbid'」基类约定。

字段约束原则：
- Literal 用于有限枚举（status / 通用 band 文案）
- Field(ge/le/min_length/max_length) 用于数值范围和字符串长度
- extra='forbid' 防止未知字段污染；frozen=True 防止下游误改
- 财务字段（如 health_score）必须在 [0, 100]，与 icontract Layer 2 重复校验一次
  （不算重复 — 见 docs/type-contract-data-defense.md 第 3 节）
"""

from __future__ import annotations

from typing import Annotated, Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# 基类
# ---------------------------------------------------------------------------

class _StrictBase(BaseModel):
    """财务分析所有 schema 的基类。"""

    model_config = ConfigDict(
        strict=True,
        frozen=True,
        validate_assignment=True,
        extra="forbid",
    )


# ---------------------------------------------------------------------------
# Literal 枚举
# ---------------------------------------------------------------------------

GenerateStatus = Literal["success", "failed", "already_exists"]
"""生成报告状态：success（首次成功落库）/ failed（生成成功但落库失败）/ already_exists（同日已生成）。"""


# ---------------------------------------------------------------------------
# 四维度子结构
# ---------------------------------------------------------------------------

class FinancialDimension(_StrictBase):
    """单个维度（盈利/成长/安全/估值）的评分 + 标签。"""

    score: Optional[Annotated[float, Field(ge=0.0, le=100.0)]]
    label: Annotated[str, Field(min_length=1, max_length=200)]


class FinancialDims(_StrictBase):
    """四维度分项。"""

    profit: FinancialDimension
    growth: FinancialDimension
    safety: FinancialDimension
    valuation: FinancialDimension


class FinancialValuation(_StrictBase):
    """估值子结构（PE TTM / PB MRQ + 摘要 + 评分）。"""

    pe_ttm: Optional[float] = None
    pe_disp: Optional[Annotated[str, Field(max_length=32)]] = None
    pb: Optional[float] = None
    pb_disp: Optional[Annotated[str, Field(max_length=32)]] = None
    summary: Annotated[str, Field(min_length=1, max_length=200)]
    score: Optional[Annotated[float, Field(ge=0.0, le=100.0)]]


class FinancialAnalysis(_StrictBase):
    """_score_dims 返回字典的 Pydantic 契约。

    字段名与 service 实际一致（dims / health_score / gaps / years / rev_yoy /
    np_yoy / scissors / cash_quality / ocf / np / valuation）。
    """

    dims: Dict[Annotated[str, Field(min_length=1, max_length=32)], FinancialDimension]
    """服务实际键：profitability / growth / safety / valuation（用 dict 接收，避开字段名漂移）。"""
    health_score: Optional[Annotated[float, Field(ge=0.0, le=100.0)]]
    gaps: List[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list,
        max_length=20,
    )
    years: List[Dict[str, Any]] = Field(default_factory=list, max_length=20)
    rev_yoy: Optional[float] = None
    np_yoy: Optional[float] = None
    scissors: Optional[float] = None
    cash_quality: Optional[Annotated[float, Field(ge=-10.0, le=10.0)]] = None
    """OCF/NP 比率，经验范围 [-10, 10]；越界说明数据异常。"""
    ocf: Optional[float] = None
    np: Optional[float] = None
    # 预格式化字符串（模板渲染用，避开 |format() 与 % 冲突）
    rev_yoy_fmt: Optional[Annotated[str, Field(max_length=16)]] = None
    np_yoy_fmt: Optional[Annotated[str, Field(max_length=16)]] = None
    scissors_fmt: Optional[Annotated[str, Field(max_length=16)]] = None
    valuation: FinancialValuation


# ---------------------------------------------------------------------------
# API I/O 边界
# ---------------------------------------------------------------------------

class FinancialAnalysisRequest(_StrictBase):
    """POST /api/v1/financial-analysis/generate 请求体。"""

    stock_code: Annotated[str, Field(min_length=6, max_length=6, pattern=r"^\d{6}$")]
    stock_name: Optional[Annotated[str, Field(max_length=64)]] = None


class FinancialAnalysisGenerateResponse(_StrictBase):
    """POST /api/v1/financial-analysis/generate 响应。"""

    report_id: Optional[Annotated[str, Field(min_length=1, max_length=32)]] = None
    """None 表示生成或落库失败（status='failed'）。"""
    stock_code: Annotated[str, Field(min_length=6, max_length=6, pattern=r"^\d{6}$")]
    stock_name: Annotated[str, Field(min_length=1, max_length=64)]
    status: GenerateStatus
    markdown: Annotated[str, Field(min_length=1)]
    analysis: FinancialAnalysis


class FinancialAnalysisReportItem(_StrictBase):
    """报告元数据（list 端点 / 详情端点通用）。"""

    id: Annotated[str, Field(min_length=1, max_length=32)]
    stock_code: Annotated[str, Field(min_length=6, max_length=6, pattern=r"^\d{6}$")]
    stock_name: Optional[Annotated[str, Field(max_length=64)]] = None
    created_at: Annotated[str, Field(min_length=10, max_length=32)]
    """ISO 8601 datetime 字符串。"""
    md_path: Optional[Annotated[str, Field(min_length=1, max_length=512)]] = None
    health_score: Optional[Annotated[float, Field(ge=0.0, le=100.0)]]
    analysis_json: Optional[Dict[str, Any]] = Field(default=None)
    markdown: Optional[Annotated[str, Field(min_length=1)]] = None
    """markdown 全文：详情端点从 md_path 读取后填充；列表端点 None。"""


class FinancialAnalysisListResponse(_StrictBase):
    """GET /api/v1/financial-analysis/reports 响应。"""

    success: bool = True
    data: List[FinancialAnalysisReportItem] = Field(default_factory=list, max_length=200)
    total: Annotated[int, Field(ge=0)]


class FinancialAnalysisDetailResponse(_StrictBase):
    """GET /api/v1/financial-analysis/reports/{id} 响应。"""

    success: bool = True
    data: FinancialAnalysisReportItem


class FinancialAnalysisDeleteResponse(_StrictBase):
    """DELETE /api/v1/financial-analysis/reports/{id} 响应。"""

    success: bool = True
    deleted: Annotated[str, Field(min_length=1, max_length=32)]
