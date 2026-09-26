# -*- coding: utf-8 -*-
"""
Supply Chain Schema (v2 deep-optimization).

类型-契约-数据三层防御（按 docs/type-contract-data-defense.md）：
- 类型：所有公共字段加类型注解 + 不用裸 tuple/dict/list
- 数据：Pydantic v2 + ConfigDict(strict=True, frozen=True, validate_assignment=True)
- 契约：ChainNodeV3 / SupplyChainGraph / SupplyChainV2 用 @model_validator 守业务不变式

与 v1 兼容：保留 ChainNode / Chokepoint / USChinaChain / SupplyChain 不删，
SupplyChainDataService.fetch_all 默认返回 SupplyChainV2，legacy=True 走旧结构。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Literal, Optional, TypeAlias

import icontract
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)


# ============================================================
# v1 兼容：保留旧 schema
# ============================================================


class ChainNode(BaseModel):
    """[v1] Supply chain node (保留向后兼容)"""

    level: str = Field(..., description="Level")
    companies: List[str] = Field(default_factory=list)
    concentration: Optional[str] = Field(None)


class Chokepoint(BaseModel):
    """[v1+v2] Bottleneck/chokepoint"""

    type: Literal["patent", "capacity", "geo", "tech", "cert"] = Field(...)
    description: str = Field(...)
    confidence: Literal["high", "medium", "low"] = Field("medium")


class USChinaChain(BaseModel):
    """[v1+v2] US-China dual chain"""

    role: str = Field(..., description="Role in dual chain")
    substitution_progress: Optional[str] = Field(None)
    sanction_risk: Optional[str] = Field(None)
    dual_chain_impact: Optional[str] = Field(None)


class SupplyChain(BaseModel):
    """[v1] Supply chain analysis (保留向后兼容)"""

    chain_map: List[ChainNode] = Field(default_factory=list)
    chokepoints: List[Chokepoint] = Field(default_factory=list)
    company_position: str = Field(..., description="Company's position in chain")
    upstream: List[str] = Field(default_factory=list)
    downstream: List[str] = Field(default_factory=list)
    bargaining_power: Optional[str] = Field(None)
    us_china_chain: Optional[USChinaChain] = Field(None)


# ============================================================
# v2 新增：结构化供应链节点
# ============================================================


# 字段来源标注（v2 关键血缘追踪）
FieldSource = Literal["kb", "llm", "tool", "industry_default", "unknown"]

EvidenceStrength = Literal[
    "primary",  # 交易所文件/年报/电话会/官方订单
    "media",  # 可信媒体/行业刊
    "analysis",  # 一级研究 / 深度分析
    "social",  # 社交媒体
    "rumor",  # 传闻
    "kb_doc",  # 用户知识库文档
]


class ChainNodeV3(BaseModel):
    """[v2] 结构化供应链节点。

    与 v1 ChainNode 区别：
    - 每个字段标注来源（kb/llm/tool/industry_default/unknown）
    - 关系强度量化（relationship + concentration_pct + substitutability）
    - 证据强度 + 衰减时间戳
    - 字段来源一致性契约（@model_validator 守）

    字段优先级：4 个核心字段（name/code/concentration_pct/geographic_distribution）
    其他字段可选，缺字段时报告「数据完整性披露」章节显式说明。
    """

    model_config = ConfigDict(
        strict=True,
        frozen=True,
        validate_assignment=True,
        extra="forbid",
    )

    # ---- 基础识别 ----
    name: str = Field(..., min_length=1, max_length=80, description="公司/品类名")
    code: Optional[str] = Field(
        default=None,
        pattern=r"^(\d{6}|[A-Z]{1,5}(\.[A-Z])?)$",
        description="股票代码（6 位 A 股 / 美股 ticker）",
    )
    layer: Literal["upstream", "midstream", "downstream"] = Field(...)
    sub_layer: Optional[str] = Field(
        default=None, max_length=40, description="细分子层（如『硅片』『光刻胶』）"
    )

    # ---- 关系强度（v2 关键：v1 完全缺失的量化字段） ----
    relationship: Literal["核心", "重要", "一般", "潜在"] = Field(default="一般")
    concentration_pct: Optional[float] = Field(
        default=None, ge=0, le=100, description="占该环节供应商/客户的比例（0-100）"
    )
    substitutability: Literal["高", "中", "低", "不可替代", "未知"] = Field(
        default="未知"
    )
    geographic_distribution: List[str] = Field(
        default_factory=list, description="产地/市场地理分布"
    )

    # ---- 来源血缘（v2 关键：每个字段可追溯） ----
    name_source: FieldSource = Field(default="llm")
    name_source_doc_id: Optional[str] = Field(default=None, description="KB 文档 ID")

    concentration_source: Optional[FieldSource] = Field(
        default=None, description="concentration_pct 来源"
    )
    concentration_doc_id: Optional[str] = Field(default=None)
    concentration_tool: Optional[str] = Field(
        default=None, description="调用了哪个工具（如 tushare.top10_holders）"
    )

    # ---- 证据链 ----
    evidence_strength: EvidenceStrength = Field(default="analysis")
    source_url: Optional[str] = Field(default=None, max_length=2048)
    confidence: Literal["high", "medium", "low"] = Field(default="medium")

    # ---- 衰减（v2 新增） ----
    kb_doc_id: Optional[str] = Field(
        default=None, description="来自知识库的关联文档 ID"
    )
    kb_doc_age_days: Optional[int] = Field(
        default=None, ge=0, description="KB 文档距今天数（用于衰减）"
    )

    @model_validator(mode="after")
    def _check_field_source_consistency(self) -> "ChainNodeV3":
        """契约：concentration_pct 非空时必须标注来源。"""
        if self.concentration_pct is not None and self.concentration_source is None:
            raise ValueError(
                "ChainNodeV3 契约违反：concentration_pct 非空时必须标注 "
                "concentration_source（kb/llm/tool/industry_default）"
            )
        if (
            self.name_source == "kb"
            and not self.name_source_doc_id
            and not self.kb_doc_id
        ):
            raise ValueError(
                "ChainNodeV3 契约违反：name_source=kb 时必须填 name_source_doc_id 或 kb_doc_id"
            )
        return self


# ============================================================
# v2 新增：供应链图谱（报告骨架）
# ============================================================


class KBHitRef(BaseModel):
    """知识库命中引用（用于报告「知识库参考」小节）"""

    model_config = ConfigDict(strict=True, frozen=True, validate_assignment=True)

    document_id: str = Field(...)
    document_title: str = Field(...)
    chunk_id: str = Field(...)
    content: str = Field(..., max_length=2000)
    score: float = Field(..., ge=0.0, le=1.0, description="加权后最终得分（0-1）")
    raw_score: float = Field(..., description="FTS5 原始 bm25 得分（负数，越小越相关）")
    tag_weight: float = Field(1.0, ge=0.0, description="tag 加权")
    stock_match_weight: float = Field(1.0, ge=0.0, description="股票匹配加权")
    recency_weight: float = Field(1.0, ge=0.0, description="时间衰减")
    source_url: Optional[str] = Field(None, max_length=2048)
    validation_status: Literal[
        "已被公告验证", "与公开数据冲突", "仅用户资料支持", "待核验"
    ] = Field("待核验")
    kb_doc_age_days: Optional[int] = Field(None, ge=0)


class DataCompleteness(BaseModel):
    """数据完整度披露（v2 新增：让报告可信度可验证）。"""

    model_config = ConfigDict(strict=True, frozen=True, validate_assignment=True)

    upstream_total: int = Field(default=0, ge=0)
    upstream_with_concentration: int = Field(default=0, ge=0)
    upstream_with_geo: int = Field(default=0, ge=0)
    upstream_with_substitutability: int = Field(default=0, ge=0)
    upstream_with_code: int = Field(default=0, ge=0)

    downstream_total: int = Field(default=0, ge=0)
    downstream_with_concentration: int = Field(default=0, ge=0)
    downstream_with_geo: int = Field(default=0, ge=0)

    kb_hit_count: int = Field(default=0, ge=0, description="KB 命中 chunk 数")
    kb_coverage_score: float = Field(default=0.0, ge=0.0, le=1.0)
    aggregate_confidence: Literal["high", "medium", "low"] = Field(default="low")

    def summary(self) -> Dict[str, Any]:
        """供报告 Markdown 表格使用的紧凑摘要。"""
        up_pct = (
            round(self.upstream_with_concentration / self.upstream_total * 100, 1)
            if self.upstream_total
            else 0.0
        )
        down_pct = (
            round(self.downstream_with_concentration / self.downstream_total * 100, 1)
            if self.downstream_total
            else 0.0
        )
        return {
            "upstream_total": self.upstream_total,
            "upstream_with_concentration_pct": up_pct,
            "downstream_total": self.downstream_total,
            "downstream_with_concentration_pct": down_pct,
            "kb_hit_count": self.kb_hit_count,
            "kb_coverage_score": self.kb_coverage_score,
            "aggregate_confidence": self.aggregate_confidence,
        }


def _make_empty_data_completeness() -> "DataCompleteness":
    """[v2] Module-level factory for DataCompleteness default.

    在 default_factory 中用模块级函数（而非 lambda 或类引用）让 pyright
    能正确推导返回类型，避免 strict 模式下 "Arguments missing" 误报。
    """
    return DataCompleteness()


class SupplyChainGraph(BaseModel):
    """[v2] 供应链图谱（替代 v1 SupplyChain 的 List[str] 结构）。"""

    model_config = ConfigDict(
        strict=True,
        frozen=True,
        validate_assignment=True,
    )

    ticker: str = Field(..., pattern=r"^[\w\.\-]{1,16}$")
    company: str = Field(..., min_length=1, max_length=80)
    industry: str = Field(..., min_length=1, max_length=40)
    position: str = Field(..., min_length=1, max_length=200)

    upstream: List[ChainNodeV3] = Field(default_factory=list)
    downstream: List[ChainNodeV3] = Field(default_factory=list)
    chokepoints: List[Chokepoint] = Field(default_factory=list)
    us_china_chain: Optional[USChinaChain] = Field(None)

    upstream_depth: int = Field(0, ge=0, le=10)
    downstream_depth: int = Field(0, ge=0, le=10)

    # v2 新增
    kb_coverage_score: float = Field(0.0, ge=0.0, le=1.0)
    aggregate_confidence: Literal["high", "medium", "low"] = Field("low")
    data_completeness: DataCompleteness = Field(
        default_factory=_make_empty_data_completeness
    )

    @model_validator(mode="after")
    def _check_depth(self) -> "SupplyChainGraph":
        """契约：upstream_depth 至少能容纳上游节点的最大子层深度。"""
        # 简化校验：至少 1 个上游时 depth >= 1
        if self.upstream and self.upstream_depth < 1:
            raise ValueError(
                "SupplyChainGraph 契约违反：有上游节点时 upstream_depth 必须 >= 1"
            )
        if self.downstream and self.downstream_depth < 1:
            raise ValueError(
                "SupplyChainGraph 契约违反：有下游节点时 downstream_depth 必须 >= 1"
            )
        return self


class SupplyChainV2(BaseModel):
    """[v2] 报告输入（fetch_all 返回值）。

    同时容纳 v1 字段（company_position / upstream / downstream 字符串数组）
    和 v2 字段（graph 结构化 / kb_evidence 引用 / llm_signals / serenity_score），
    保证调用方渐进迁移。
    """

    model_config = ConfigDict(strict=True, frozen=True, validate_assignment=True)

    # v1 兼容
    data_sources: List[str] = Field(default_factory=list)
    company_position: str = Field("", description="公司在产业链中的位置（短描述）")
    upstream: List[str] = Field(default_factory=list, description="[v1] 上游节点名列表")
    downstream: List[str] = Field(
        default_factory=list, description="[v1] 下游节点名列表"
    )
    chokepoints: List[Chokepoint] = Field(default_factory=list)
    us_china_chain: Optional[USChinaChain] = Field(None)
    industry_drivers: List[str] = Field(default_factory=list)

    # v2 新增
    graph: Optional[SupplyChainGraph] = Field(None, description="[v2] 结构化图谱")
    kb_evidence: List[KBHitRef] = Field(
        default_factory=list, description="[v2] 知识库命中引用"
    )
    llm_signals: Dict[str, float] = Field(
        default_factory=dict, description="[v2] LLM 因子信号 0-1"
    )
    data_completeness: DataCompleteness = Field(
        default_factory=_make_empty_data_completeness
    )

    # Serenity 评分（v2 统一入口返回）
    serenity_score: Optional[int] = Field(None, ge=0, le=100)
    serenity_verdict: Optional[str] = Field(None)
    serenity_factor_details: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    serenity_penalty_details: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    serenity_kb_bonus_applied: Dict[str, float] = Field(
        default_factory=dict, description="[v2] 哪些因子应用了 KB 加成"
    )

    fetched_at: Optional[datetime] = Field(None, description="数据拉取时间（用于审计）")


# ============================================================
# Serenity 评分卡结果（v2 统一入口返回值）
# ============================================================


FactorKey = Literal[
    "demand_inflection",
    "architecture_coupling",
    "chokepoint_severity",
    "supplier_concentration",
    "expansion_difficulty",
    "evidence_quality",
    "valuation_disconnect",
    "catalyst_timing",
]

PenaltyKey = Literal[
    "dilution_financing",
    "governance",
    "geopolitics",
    "liquidity",
    "hype_risk",
    "accounting_quality",
    "cyclicality",
    "alternative_design_risk",
]


class SerenityFactorScore(BaseModel):
    """单个 Serenity 因子的评分 + 证据链"""

    model_config = ConfigDict(strict=True, frozen=True, validate_assignment=True)

    key: FactorKey = Field(...)
    rating: float = Field(..., ge=0.0, le=5.0, description="原始 0-5 评分")
    points: float = Field(..., description="加权后得分")
    weight: float = Field(..., ge=0.0, description="因子权重")
    kb_relevance: float = Field(0.0, ge=0.0, le=1.0, description="KB 相关度 0-1")
    llm_signal: float = Field(0.0, ge=0.0, le=1.0, description="LLM 信号 0-1")
    industry_prior: float = Field(0.0, ge=0.0, le=1.0, description="行业先验 0-1")
    kb_bonus_applied: float = Field(
        0.0, ge=0.0, le=0.2, description="KB 加成（上限 0.2）"
    )
    contributing_kb_doc_ids: List[str] = Field(default_factory=list)


class SerenityPenaltyScore(BaseModel):
    """单个惩罚项的评分"""

    model_config = ConfigDict(strict=True, frozen=True, validate_assignment=True)

    key: PenaltyKey = Field(...)
    rating: float = Field(..., ge=0.0, le=5.0)
    points: float = Field(..., description="扣分（通常 <= 0）")
    weight: float = Field(..., ge=0.0)


class SerenityScoreResult(BaseModel):
    """[v2] Serenity 评分结果（统一入口返回值）。

    两条调用路径（SupplyChainDataService / SupplyChainExecutor）共享同一 schema。
    """

    model_config = ConfigDict(strict=True, frozen=True, validate_assignment=True)

    ticker: str = Field(...)
    company: str = Field(...)
    market: str = Field("")
    factors: Dict[FactorKey, SerenityFactorScore] = Field(default_factory=dict)
    penalties: Dict[PenaltyKey, SerenityPenaltyScore] = Field(default_factory=dict)
    raw_factor_points: float = Field(default=0.0)
    penalty_points: float = Field(default=0.0)
    final_score: int = Field(default=0, ge=0, le=100)
    verdict: str = Field("", description="中文评级，如『顶级研究优先级』")
    notes: str = Field("", description="备注")

    def get(self, key: str, default: Any = None) -> Any:
        """兼容 serenity_scorecard 原生 dict 风格调用。"""
        if key in {"factors", "penalties"}:
            return getattr(self, key)
        return getattr(self, key, default)


# ============================================================
# v3 新增：供应链深度小节（产品·客户·竞争·前景 五维补强）
# ============================================================
#
# 设计原则（按 docs/type-contract-data-defense.md 三层防御）：
# - 类型：所有公共字段加类型注解 + 不用裸 tuple/dict/list
# - 数据：Pydantic v2 + ConfigDict(strict=True, frozen=True, validate_assignment=True, extra="forbid")
# - 契约：@model_validator 守业务不变式（一致性校验、TAM 不崩、占比加和容忍）
#
# 与 v1/v2 兼容：6 个新模型全部追加在文件末尾，不删不改任何现有 schema。
# 触发灰度开关：SERENITY_DEEP_DIVE_V3_ENABLED（默认 false）


# ============================================================
# §6 产品矩阵与定位
# ============================================================


ProductCategory = Literal[
    "core",  # 核心（当前主业）
    "growth",  # 增长（重点投入）
    "legacy",  # 传统（持续但非重点）
    "exploratory",  # 探索（新业务/未规模化）
]


class ProductLineV3(BaseModel):
    """[v3 §6] 产品/产品线画像。

    渲染为表格：产品/战略定位/营收占比/毛利率/目标市场/价格带/差异化卖点/证据强度。
    工具来源：analyze_product_matrix
    """

    model_config = ConfigDict(
        strict=True, frozen=True, validate_assignment=True, extra="forbid"
    )

    name: str = Field(..., min_length=1, max_length=80, description="产品/产品线名")
    category: ProductCategory = Field(..., description="战略定位")
    revenue_share_pct: Optional[float] = Field(default=None, ge=0, le=100)
    gross_margin_pct: Optional[float] = Field(default=None, ge=-100, le=100)
    target_market: List[str] = Field(
        default_factory=list, description="目标市场（行业/区域/客户群）"
    )
    price_band: Optional[str] = Field(
        default=None, max_length=80, description="价格带（如『高端』/『30-50 元』）"
    )
    differentiators: List[str] = Field(default_factory=list, max_length=10)
    evidence_strength: EvidenceStrength = Field(default="analysis")
    source_url: Optional[str] = Field(default=None, max_length=2048)
    kb_doc_id: Optional[str] = Field(default=None, max_length=128)


# ============================================================
# §7 市场地位与占有率
# ============================================================


ShareTrend = Literal["rising", "stable", "falling", "volatile", "unknown"]
SubstitutionRisk = Literal["low", "medium", "high", "unknown"]


class MarketPositionV3(BaseModel):
    """[v3 §7] 市场地位与占有率画像。

    渲染为表格：公司/子赛道/份额/排名/CR3/CR5/份额趋势/3 年变化/主要竞品/替代风险/证据。
    工具来源：analyze_market_position
    """

    model_config = ConfigDict(
        strict=True, frozen=True, validate_assignment=True, extra="forbid"
    )

    subsegment: str = Field(
        ...,
        min_length=1,
        max_length=80,
        description="子赛道（如『高端白酒 800 元以上』）",
    )
    market_share_pct: Optional[float] = Field(default=None, ge=0, le=100)
    market_rank: Optional[int] = Field(default=None, ge=1, le=1000)
    cr3_pct: Optional[float] = Field(default=None, ge=0, le=100, description="行业 CR3")
    cr5_pct: Optional[float] = Field(default=None, ge=0, le=100)
    cr10_pct: Optional[float] = Field(default=None, ge=0, le=100)
    share_trend: ShareTrend = Field(default="unknown")
    share_change_3y_pct: Optional[float] = Field(
        default=None, description="近 3 年份额变化（百分点）"
    )
    top_competitors: List[str] = Field(default_factory=list, max_length=10)
    substitution_risk: SubstitutionRisk = Field(default="unknown")
    evidence_strength: EvidenceStrength = Field(default="analysis")
    source_url: Optional[str] = Field(default=None, max_length=2048)
    kb_doc_id: Optional[str] = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _check_consistency(self) -> "MarketPositionV3":
        """契约：market_rank 非空时建议提供 market_share_pct（便于交叉验证）。"""
        if self.market_rank is not None and self.market_share_pct is None:
            raise ValueError(
                "MarketPositionV3 契约违反：market_rank 非空时建议提供 "
                "market_share_pct（便于交叉验证）。如确实缺失，请两者都置空。"
            )
        return self


# ============================================================
# §8 关键客户与供应商
# ============================================================


PartnerSide = Literal["customer", "supplier"]
PartnerSource = Literal[
    "annual_report",
    "prospectus",
    "inquiry_letter",
    "convertible_bond",
    "investor_relations",
    "news",
    "kb",
]


class KeyPartnerV3(BaseModel):
    """[v3 §8] 关键客户/供应商画像。

    渲染为两张表（customers / suppliers），列：名称/份额(%)/关联方/匿名/合作年限/披露来源/证据强度。
    工具来源：extract_key_partners
    """

    model_config = ConfigDict(
        strict=True, frozen=True, validate_assignment=True, extra="forbid"
    )

    side: PartnerSide = Field(...)
    name: str = Field(..., min_length=1, max_length=120)
    share_pct: Optional[float] = Field(default=None, ge=0, le=100)
    is_related_party: bool = Field(default=False)
    is_anonymous: bool = Field(
        default=False, description="年报披露为『客户A』/『供应商一』"
    )
    revenue_or_cost_share: Optional[str] = Field(
        default=None,
        max_length=40,
        description="占总营收/采购的比例文字描述",
    )
    years_partnered: Optional[int] = Field(default=None, ge=0, le=50)
    public_source: PartnerSource = Field(default="news")
    evidence_strength: EvidenceStrength = Field(default="analysis")
    source_url: Optional[str] = Field(default=None, max_length=2048)


# ============================================================
# §9 行业前景与需求驱动
# ============================================================


TimeWindow = Literal[
    "near_term_3_6m",
    "mid_term_6_12m",
    "long_term_12_36m",
]
SubsegmentStatus = Literal["growing", "stable", "declining", "transforming"]


# ============================================================
# §10.b 产能展望 Literal 类型
# ============================================================

VALID_DEMAND_SIGNALS: frozenset[str] = frozenset({
    "下游订单饱满",
    "行业出货量增长",
    "在手订单充裕",
    "季节性旺季",
    "扩产产能释放",
    "需求回落",
    "限产检修",
})

VALID_CAPACITY_CHANGE_FACTORS: frozenset[str] = frozenset({
    "新建产能释放",
    "爬坡良率提升",
    "季节性检修",
    "限产政策",
    "设备升级改造",
    "外协加工",
})

DemandSignal: TypeAlias = Literal[tuple(VALID_DEMAND_SIGNALS)]  # type: ignore[valid-type]
CapacityChangeFactor: TypeAlias = Literal[tuple(VALID_CAPACITY_CHANGE_FACTORS)]  # type: ignore[valid-type]


class IndustryOutlookV3(BaseModel):
    """[v3 §9] 行业前景与需求驱动画像。

    渲染为表：子赛道/2024 TAM/2027E TAM/CAGR/中国份额/需求驱动/政策催化/替代风险/海外空间/时间窗。
    工具来源：analyze_industry_outlook
    """

    model_config = ConfigDict(
        strict=True, frozen=True, validate_assignment=True, extra="forbid"
    )

    subsegment: str = Field(..., min_length=1, max_length=80)
    subsegment_status: SubsegmentStatus = Field(
        default="stable",
        description="子赛道状态（用于 TAM 校验豁免）",
    )
    tam_2024_usd_bn: Optional[float] = Field(
        default=None, ge=0, description="全球 TAM（十亿美元）"
    )
    tam_2027e_usd_bn: Optional[float] = Field(default=None, ge=0)
    cagr_2024_2027_pct: Optional[float] = Field(default=None, ge=-50, le=100)
    china_share_pct: Optional[float] = Field(default=None, ge=0, le=100)
    demand_drivers: List[str] = Field(default_factory=list, max_length=10)
    policy_catalysts: List[str] = Field(default_factory=list, max_length=10)
    substitution_threats: List[str] = Field(default_factory=list, max_length=10)
    overseas_addressable: Optional[str] = Field(
        default=None, max_length=200, description="海外可触达空间"
    )
    time_window: TimeWindow = Field(default="mid_term_6_12m")
    evidence_strength: EvidenceStrength = Field(default="analysis")
    source_url: Optional[str] = Field(default=None, max_length=2048)
    kb_doc_id: Optional[str] = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _check_tam_consistency(self) -> "IndustryOutlookV3":
        """契约：2027E TAM 不应小于 2024 TAM 的 50%。

        衰退行业豁免：subsegment_status="declining" 时不强制校验；
        转型行业（"transforming"）按 60% 阈值校验（避免误报但仍保持基本约束）。
        """
        if self.tam_2024_usd_bn is None or self.tam_2027e_usd_bn is None:
            return self
        if self.subsegment_status == "declining":
            return self
        threshold = 0.6 if self.subsegment_status == "transforming" else 0.5
        if self.tam_2027e_usd_bn < self.tam_2024_usd_bn * threshold:
            raise ValueError(
                f"IndustryOutlookV3 契约违反：2027E TAM ({self.tam_2027e_usd_bn}) "
                f"小于 2024 TAM ({self.tam_2024_usd_bn}) 的 {threshold * 100:.0f}%，"
                f"且 subsegment_status={self.subsegment_status}。"
                f"衰退行业请将 subsegment_status 设为 'declining'，"
                f"转型行业设 'transforming'（按 60% 阈值校验）。"
            )
        return self


# ============================================================
# §10 财务质量与产能跟踪
# ============================================================


class FinancialQualityV3(BaseModel):
    """[v3 §10] 财务质量与产能跟踪画像。

    渲染为表：period/营收同比/毛利率/同比变化/经营现金流同比/应收/营收(%)/
    存货天数/合同负债同比/capex 强度/产能利用率/分业务收入占比/red_flags。
    工具来源：analyze_financial_quality

    工具失败时返回空列表，渲染层展示"待核验（财务数据未获取）"。
    """

    model_config = ConfigDict(
        strict=True, frozen=True, validate_assignment=True, extra="forbid"
    )

    period: str = Field(
        ..., pattern=r"^\d{4}Q[1-4]$|^\d{4}$", description="2024Q3 / 2024"
    )
    revenue_yoy_pct: Optional[float] = Field(default=None, ge=-100, le=500)
    gross_margin_pct: Optional[float] = Field(default=None, ge=-100, le=100)
    gross_margin_change_yoy_pct: Optional[float] = Field(default=None)
    operating_cash_flow_yoy_pct: Optional[float] = Field(default=None)
    ar_to_revenue_pct: Optional[float] = Field(default=None, ge=0, le=200)
    inventory_days: Optional[int] = Field(default=None, ge=0, le=730)
    contract_liability_yoy_pct: Optional[float] = Field(
        default=None, description="合同负债同比"
    )
    capex_intensity_pct: Optional[float] = Field(default=None, description="capex/营收")
    capacity_utilization_pct: Optional[float] = Field(default=None, ge=0, le=100)
    revenue_segments: Dict[str, float] = Field(
        default_factory=dict, description="分业务收入占比"
    )
    red_flags: List[str] = Field(default_factory=list, max_length=10)
    evidence_strength: EvidenceStrength = Field(default="primary")
    source_url: Optional[str] = Field(default=None, max_length=2048)

    # §10.b 扩产进度（用于产能展望预测参考）
    expansion_projects: List["ExpansionProjectV3"] = Field(
        default_factory=list,
        description="已知扩产项目列表（用于 §10.b 产能展望预测）"
    )
    expansion_status_notes: Optional[str] = Field(
        default=None, max_length=200,
        description="扩产整体状态备注"
    )

    @model_validator(mode="after")
    def _check_segment_consistency(self) -> "FinancialQualityV3":
        """契约：revenue_segments 占比合计与 100% 偏差不超过 5%。

        A 股公司分业务收入占比加和不严格等于 100%（含分部间抵消），
        容忍偏差 5% 避免误报。
        """
        if not self.revenue_segments:
            return self
        total = sum(self.revenue_segments.values())
        if total <= 0:
            return self
        if abs(total - 100.0) > 5.0:
            raise ValueError(
                f"FinancialQualityV3 契约违反：revenue_segments 占比合计 "
                f"{total:.1f}%，与 100% 偏差超过 5%。"
                f"请检查是否含分部间抵消项或单位错误。"
            )
        return self


# ============================================================
# §10.b 产能展望 Schema
# ============================================================


class ExpansionProjectV3(BaseModel):
    """[v3 §10.b] 扩产项目跟踪。

    用于记录公司在建/规划/完成的扩产项目，为产能预测提供输入。
    """

    model_config = ConfigDict(
        strict=True, frozen=True, validate_assignment=True, extra="forbid"
    )

    project_name: str = Field(..., min_length=1, max_length=120)
    expected_completion: Optional[str] = Field(
        default=None, description="预计投产时间（YYYYQM格式，如'2026Q3'）"
    )
    expected_capacity_addition: Optional[str] = Field(
        default=None, max_length=80,
        description="新增产能（如'1.5万千升/年'、'2GW/年'）"
    )
    progress_status: Literal["planning", "construction", "ramping", "completed"] = Field(
        default="planning"
    )
    source: str = Field(default="年报披露", description="信息来源")
    evidence_strength: EvidenceStrength = Field(default="analysis")


class CapacityForecastPeriodV3(BaseModel):
    """[v3 §10.b] 单期产能预测（短期+中期统一Schema）。

    使用 time_window 区分预测期限：
    - near_term_3_6m: 未来1-6个月（短期）
    - mid_term_6_12m: 未来6-12个月（中期）

    所有数值字段使用 Decimal 类型（按三层防御要求）。
    """

    model_config = ConfigDict(
        strict=True, frozen=True, validate_assignment=True, extra="forbid"
    )

    time_window: TimeWindow = Field(...)
    period_label: str = Field(
        ..., description="人类可读标签（如'2026-10'、'2026Q4'）"
    )

    predicted_utilization_pct: Optional[Decimal] = Field(
        default=None, ge=Decimal("0"), le=Decimal("200"),
        description="预测产能利用率（可>100%超产）"
    )
    predicted_output_volume: Optional[Decimal] = Field(
        default=None, ge=Decimal("0"),
        description="预测产量绝对值"
    )
    predicted_output_unit: Optional[str] = Field(
        default=None, max_length=20,
        description="产量单位（如'万台'、'万片'、'GWh'）"
    )

    inference_basis: str = Field(
        ..., description="推断依据摘要"
    )
    demand_signals: List[DemandSignal] = Field(
        default_factory=list, max_length=5
    )
    capacity_change_factors: List[CapacityChangeFactor] = Field(
        default_factory=list, max_length=5
    )

    confidence: Literal["high", "medium", "low"] = Field(default="medium")
    evidence_strength: EvidenceStrength = Field(default="analysis")

    @model_validator(mode="after")
    def _check_volume_unit_consistency(self) -> "CapacityForecastPeriodV3":
        """契约：predicted_output_volume 非空时必须提供 predicted_output_unit。"""
        if self.predicted_output_volume is not None and self.predicted_output_unit is None:
            raise ValueError(
                "CapacityForecastPeriodV3 契约违反：predicted_output_volume 非空时 "
                "必须提供 predicted_output_unit"
            )
        return self


# AggregateConfidence 在 CapacityOutlookV3 之前定义（避免 forward reference）
AggregateConfidence = Literal["high", "medium", "low"]


class CapacityOutlookV3(BaseModel):
    """[v3 §10.b] 产能展望与预测。

    定位：§10 财务质量与产能跟踪的扩展子节，
    复用已有的 historical periods 数据，补充未来预测。

    与 §10 FinancialQualityV3 的关系：
    - §10 存储历史产能数据（capacity_utilization_pct 等）
    - §10.b 存储预测数据（基于 §10 历史 + 需求信号推断）
    """

    model_config = ConfigDict(
        strict=True, frozen=True, validate_assignment=True, extra="forbid"
    )

    ticker: str = Field(..., pattern=r"^[\w\.\-]{1,16}$")
    company: str = Field(..., min_length=1, max_length=80)
    fetched_at: Optional[datetime] = Field(default=None)

    industry_unit_hint: Optional[str] = Field(
        default=None, max_length=20,
        description="行业推荐产量单位（来自行业模板，如'万片/月'、'GWh/年'）"
    )

    historical_summary: str = Field(
        default="",
        description="历史产能利用率摘要（如'近3期均值为 85.3%'）"
    )
    historical_data_quality: Literal["complete", "partial", "sparse", "none"] = Field(
        default="none"
    )

    forecasts: List[CapacityForecastPeriodV3] = Field(
        default_factory=list,
        description="产能预测列表（含短期1-6月和中长期6-12月）"
    )

    trend: Literal["rising", "stable", "falling", "volatile", "insufficient_data"] = Field(
        default="insufficient_data"
    )
    trend_rationale: str = Field(default="", description="趋势判断依据")

    capacity_bottleneck_risk: Literal["high", "medium", "low", "unknown"] = Field(
        default="unknown"
    )
    demand_supply_balance: Literal["tight", "balanced", "loose", "unknown"] = Field(
        default="unknown"
    )

    expansion_plans: List[ExpansionProjectV3] = Field(
        default_factory=list, max_length=5,
        description="已知扩产项目"
    )

    data_source_notes: str = Field(
        default="",
        description="数据来源说明（如'年报披露+行业数据'、'行业均值填充，待核验'）"
    )
    confidence: AggregateConfidence = Field(default="low")

    @model_validator(mode="after")
    def _check_forecast_consistency(self) -> "CapacityOutlookV3":
        """契约：当 forecasts 和 historical_summary 都为空时，trend 须为 insufficient_data。

        frozen=True 模型不能修改字段，因此通过 raise ValueError 来强制约束。
        """
        if not self.forecasts and not self.historical_summary:
            if self.trend != "insufficient_data":
                raise ValueError(
                    "CapacityOutlookV3 契约违反：forecasts 和 historical_summary "
                    "均为空时，trend 必须为 'insufficient_data'。"
                )
        return self


# ============================================================
# 顶层容器 SupplyChainDeepDiveV3
# ============================================================


DeepDiveSection = Literal[
    "product_matrix",
    "market_position",
    "key_partners",
    "industry_outlook",
    "financial_quality",
    "capacity_outlook",
]


class SupplyChainDeepDiveV3(BaseModel):
    """[v3] 报告深度小节聚合（追加在 9 节骨架之后）。

    由 SupplyChainReportService 解析 executor 输出后构造，存入 SQLite 深字段。
    sections_executed / sections_skipped 用于"数据完整性披露"脚注渲染。
    """

    model_config = ConfigDict(
        strict=True, frozen=True, validate_assignment=True, extra="forbid"
    )

    ticker: str = Field(..., pattern=r"^[\w\.\-]{1,16}$")
    company: str = Field(..., min_length=1, max_length=80)
    fetched_at: Optional[datetime] = Field(default=None, description="工具实际执行时间")

    product_matrix: List[ProductLineV3] = Field(default_factory=list)
    market_position: List[MarketPositionV3] = Field(default_factory=list)
    key_customers: List[KeyPartnerV3] = Field(default_factory=list)
    key_suppliers: List[KeyPartnerV3] = Field(default_factory=list)
    industry_outlook: List[IndustryOutlookV3] = Field(default_factory=list)
    financial_quality: List[FinancialQualityV3] = Field(default_factory=list)

    # §10.b 产能展望（作为 §10 的扩展子节）
    capacity_outlook: Optional["CapacityOutlookV3"] = Field(default=None)

    sections_executed: List[DeepDiveSection] = Field(default_factory=list)
    sections_skipped: List[str] = Field(default_factory=list)
    aggregate_confidence: AggregateConfidence = Field(default="low")

    def section_status(self) -> Dict[str, str]:
        """供报告脚注使用的状态摘要：executed / skipped。"""
        all_secs = [
            "product_matrix",
            "market_position",
            "key_partners",
            "industry_outlook",
            "financial_quality",
            "capacity_outlook",
        ]
        return {
            sec: ("executed" if sec in self.sections_executed else "skipped")
            for sec in all_secs
        }

    @icontract.ensure(
        lambda result: result in {"high", "medium", "low"},
        "compute_aggregate_confidence 必须返回 high/medium/low",
    )
    def compute_aggregate_confidence(self) -> AggregateConfidence:
        """推导 aggregate_confidence（纯函数，不修改 self）。

        因 Pydantic v2 frozen=True，本方法不能写回 self.aggregate_confidence；
        返回新值，调用方按需通过 ``model_copy(update={...})`` 构造新实例。

        规则：
        - ≥4 节 executed 且 ≥3 节 evidence_strength∈{primary,media,analysis,kb_doc} → high
        - ≥3 节 executed → medium
        - 否则 → low
        """
        if len(self.sections_executed) < 3:
            return "low"
        strong_levels = {"primary", "media", "analysis", "kb_doc"}
        analysis_count = sum(
            1
            for sec in self.sections_executed
            if self._section_has_strong_evidence(sec, strong_levels)
        )
        if len(self.sections_executed) >= 4 and analysis_count >= 3:
            return "high"
        return "medium"

    def _section_has_strong_evidence(
        self, section: str, strong_levels: "set[str]"
    ) -> bool:
        """判断单 section 是否含 strong_levels 之一（拆分 compute_aggregate_confidence 用）。

        items 显式标注为 Sequence[Any] 是为了避免 mypy 在 5 个 elif 分支上做
        不正确的类型收窄（每个分支的 list 元素 schema 不同，无法用 union 表达）。
        运行时所有元素都有 ``evidence_strength`` 字段（getattr 防御性兜底）。
        """
        items: Any
        if section == "product_matrix":
            items = self.product_matrix
        elif section == "market_position":
            items = self.market_position
        elif section == "key_partners":
            items = list(self.key_customers) + list(self.key_suppliers)
        elif section == "industry_outlook":
            items = self.industry_outlook
        elif section == "financial_quality":
            items = self.financial_quality
        elif section == "capacity_outlook":
            items = [self.capacity_outlook] if self.capacity_outlook else []
        else:
            return False
        return any(
            getattr(item, "evidence_strength", None) in strong_levels for item in items
        )
