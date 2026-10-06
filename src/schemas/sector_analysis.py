# -*- coding: utf-8 -*-
"""板块分析模块 Pydantic v2 Schema（Type-Contract-Data Defense Layer 3）。

参考 `docs/type-contract-data-defense.md` 第 4 节「Pydantic 配置模式」，
对齐 `src/schemas/compass.py` 的「strict + frozen + validate_assignment + extra=forbid」基类。

本文件只覆盖「API I/O 边界 + 跨模块数据契约」字段：
- 不重复 icontract 已管的运行期不变式（如 weights 和 = 1.0、score ∈ [0, 100]）
- 不替代 compose_analysis 内部的 None 字段（保留 Optional）
- 不引入新依赖：复用项目已用的 pydantic v2

字段约束原则：
- Literal 用于有限枚举（band / lean / verdict 文本）
- Field(ge/le/min_length/max_length) 用于数值范围和字符串长度
- extra="forbid" 防止未知字段污染
- frozen=True 防止下游误改
- validate_assignment=True 防止运行时赋值绕过校验
"""

from __future__ import annotations

from typing import Annotated, Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# 基类
# ---------------------------------------------------------------------------

class _StrictBase(BaseModel):
    """板块分析所有 schema 的基类：strict + frozen + validate-on-assign + extra=forbid。

    与 compass._StrictBase 同源；保持一致以便后续合并 / 抽取。
    """

    model_config = ConfigDict(
        strict=True,
        frozen=True,
        validate_assignment=True,
        extra="forbid",
    )


# ---------------------------------------------------------------------------
# Literal 枚举
# ---------------------------------------------------------------------------

PolicyLean = Literal["supportive", "neutral", "restrictive"]
"""政策倾向三档分（与 src.services.sector_analysis_service._POLICY_LABEL 对齐）。"""

Band = Literal["顺风", "偏顺风", "中性", "逆风", "数据不足"]
"""综合档位（与 _verdict 输出对齐）。"""


# ---------------------------------------------------------------------------
# 三支柱子结构
# ---------------------------------------------------------------------------

class SWChain(_StrictBase):
    """申万一/二/三级行业链（成分验证产物）。"""

    l1: Optional[str] = Field(default=None, max_length=64)
    l2: Optional[str] = Field(default=None, max_length=64)
    l3: Optional[str] = Field(default=None, max_length=64)
    l3_code: Optional[str] = Field(default=None, max_length=32)
    gaps: List[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list,
        max_length=10,
    )


class IndustryIdentification(_StrictBase):
    """个股板块识别结果（识别阶段产物）。"""

    industry_em: Optional[str] = Field(default=None, max_length=64)
    industry_cninfo: Optional[str] = Field(default=None, max_length=64)
    business_scope: Optional[str] = Field(default=None, max_length=200)
    sector_name: Optional[str] = Field(default=None, max_length=64)
    sw_chain: SWChain = Field(default_factory=SWChain)
    gaps: List[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list,
        max_length=10,
    )


class PolicyPillar(_StrictBase):
    """政策倾向支柱输出。"""

    lean: Optional[PolicyLean]
    label: Optional[str] = Field(default=None, max_length=32)
    # 分数 ∈ [0, 100] 由 icontract 守门，本 schema 重复一次用于 API I/O 边界
    score: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    gaps: List[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list,
        max_length=10,
    )


class BaseRateRow(_StrictBase):
    """基率表单行（all_rates 与 requires 透出用）。"""

    keyword: Annotated[str, Field(min_length=1, max_length=32)]
    base_rate: Annotated[float, Field(ge=0.0, le=1.0)]
    note: Optional[str] = Field(default=None, max_length=200)


class BaseRatePillar(_StrictBase):
    """行业基率支柱输出。"""

    base_rate: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    basis: Optional[str] = Field(default=None, max_length=64)
    hit_keyword: Optional[str] = Field(default=None, max_length=32)
    is_default: bool = False
    table_version: Annotated[str, Field(min_length=1, max_length=16)]
    table_updated: Annotated[str, Field(min_length=1, max_length=32)]
    table_industry_count: Annotated[int, Field(ge=0, le=10000)]
    percentile: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    all_rates: List[BaseRateRow] = Field(default_factory=list, max_length=100)


class ProsperityRow(_StrictBase):
    """板块景气行数据（来自东财/新浪板块表单行）。"""

    board_name: Optional[str] = Field(default=None, max_length=64)
    change_pct: Optional[float] = None
    turnover_rate: Optional[float] = None
    total_market_cap: Optional[float] = None
    amount: Optional[float] = None
    up_count: Optional[float] = None
    down_count: Optional[float] = None
    leader: Optional[str] = Field(default=None, max_length=32)
    leader_change_pct: Optional[float] = None
    volume: Optional[float] = None
    company_count: Optional[float] = None


class ProsperityPillar(_StrictBase):
    """板块景气支柱输出。"""

    sector_found: bool = False
    momentum: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    activity: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    capital: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    breadth: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    rank_of: Optional[int] = Field(default=None, ge=1, le=200)
    total_boards: Optional[int] = Field(default=None, ge=1, le=200)
    row: ProsperityRow = Field(default_factory=ProsperityRow)
    gaps: List[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list,
        max_length=10,
    )


class SectorAnalysisWeights(_StrictBase):
    """三支柱权重（必须归一为 1.0，由 icontract 守门）。

    字段名与 service 实际一致：policy / base_rate / prosperity。
    """

    policy: Annotated[float, Field(ge=0.0, le=1.0)]
    base_rate: Annotated[float, Field(ge=0.0, le=1.0)]
    prosperity: Annotated[float, Field(ge=0.0, le=1.0)]


# ---------------------------------------------------------------------------
# 主契约：compose_analysis 返回值
# ---------------------------------------------------------------------------

class SectorAnalysis(_StrictBase):
    """compose_analysis 返回字典的 Pydantic 契约。

    严格模式（strict=True）拒绝隐式类型转换（如 str → float）；
    额外字段（extra='forbid'）防止下游误传字段；
    frozen=True 防止 API 响应被下游错误地改写。

    注：部分数值字段范围（[0,100] / [0,1]）由 icontract 在 service 层守门，
    本 schema 重复一次用于 API I/O 边界 — 不算重复校验（详见
    docs/type-contract-data-defense.md 第 3 节「决策树」）。
    """

    stock_code: Annotated[str, Field(min_length=6, max_length=6, pattern=r"^\d{6}$")]
    stock_name: Annotated[str, Field(min_length=1, max_length=64)]
    identification: IndustryIdentification
    policy: PolicyPillar
    base_rate: BaseRatePillar
    prosperity: ProsperityPillar
    prosperity_score: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    sector_score: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    band: Band
    beta_meaning: Annotated[str, Field(min_length=1, max_length=200)]
    weights: SectorAnalysisWeights
    gaps: List[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list,
        max_length=20,
    )




# ---------------------------------------------------------------------------
# API I/O 边界
# ---------------------------------------------------------------------------

class SectorAnalysisRequest(_StrictBase):
    """POST /api/v1/sector-analysis/generate 请求体。

    FastAPI 自动从 query 参数解析，所以这里仅供内部调用与测试使用。
    """

    stock_code: Annotated[str, Field(min_length=6, max_length=6, pattern=r"^\d{6}$")]
    stock_name: Optional[Annotated[str, Field(max_length=64)]] = None


class SectorAnalysisGenerateResponse(_StrictBase):
    """POST /api/v1/sector-analysis/generate 响应。"""

    report_id: Annotated[str, Field(min_length=1, max_length=32)]
    stock_code: Annotated[str, Field(min_length=6, max_length=6, pattern=r"^\d{6}$")]
    stock_name: Optional[Annotated[str, Field(max_length=64)]] = None
    markdown: Annotated[str, Field(min_length=1)]
    analysis: SectorAnalysis


class SectorAnalysisReportItem(_StrictBase):
    """报告元数据 + 可选 markdown 全文。

    list 端点：只填充核心字段（id/code/name/created_at/score），md_path/markdown/analysis_json 均为 None。
    详情端点：填充所有字段，包括从 .md 文件读取的 markdown 全文。
    """

    id: Annotated[str, Field(min_length=1, max_length=32)]
    stock_code: Annotated[str, Field(min_length=6, max_length=6, pattern=r"^\d{6}$")]
    stock_name: Optional[Annotated[str, Field(max_length=64)]] = None
    created_at: Annotated[str, Field(min_length=10, max_length=32)]
    """ISO 8601 datetime 字符串（storage 层返回 `datetime.isoformat()` 格式）。

    不用 `datetime` 类型是因为 _StrictBase 强制 strict=True，不接受 ISO 字符串自动解析；
    显式用 str + ISO 字符串格式约束，与 storage 契约一致。
    """
    md_path: Optional[Annotated[str, Field(min_length=1, max_length=512)]] = None
    """md_path：详情端点有，列表端点无（列表只展示元数据）。"""
    sector_score: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    analysis_json: Optional[Dict[str, Any]] = Field(default=None)
    """分析 JSON。storage 层以 JSON 字符串持久化，详情端点反序列化后填充。"""
    markdown: Optional[Annotated[str, Field(min_length=1)]] = None
    """markdown 全文：详情端点从 md_path 读取后填充；列表端点不返回（前端按需 GET markdown 端点）。"""


class SectorAnalysisListResponse(_StrictBase):
    """GET /api/v1/sector-analysis/reports 响应。"""

    success: bool = True
    data: List[SectorAnalysisReportItem] = Field(default_factory=list, max_length=200)
    total: Annotated[int, Field(ge=0)]


class SectorAnalysisDetailResponse(_StrictBase):
    """GET /api/v1/sector-analysis/reports/{id} 响应。"""

    success: bool = True
    data: SectorAnalysisReportItem


class SectorAnalysisDeleteResponse(_StrictBase):
    """DELETE /api/v1/sector-analysis/reports/{id} 响应。"""

    success: bool = True
    deleted: Annotated[str, Field(min_length=1, max_length=32)]
