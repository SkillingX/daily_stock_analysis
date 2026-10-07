# -*- coding: utf-8 -*-
"""深度投研双轨引擎 · 11 维度输出契约 + 护栏事件 + LR 标定表。

三层防御：
- Layer 3：本文件全部 Pydantic v2 契约（frozen + Field 范围约束），维度产出在
  编排器边界强制校验，畸形即维度降级重试；
- Layer 2：`src/deep_research_dims/guardrail.py` 与 `bayesian_dim.py` 的 icontract；
- Layer 1：全量类型注解。

设计来源：``docs/deep-research-dual-track-agent-requirements.md``（§4/§5.3/§5.4）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from src.schemas.bayesian_framework import BayesianFramework, EvidenceItem
from src.schemas.investment_conclusion import InvestmentConclusion
from src.schemas.report_schema import (
    DataPerspective,
    Intelligence,
    PhaseDecision,
    PositionStrategy,
    SniperPoints,
)
from src.schemas.research_framework import ResearchFramework
from src.schemas.supply_chain import SupplyChain
from src.schemas.value_scenarios import ValueScenarios


DimId = Literal[
    "signal",
    "data",
    "intel",
    "plan",
    "phase",
    "history",
    "six_dim",
    "bayesian",
    "conclusion",
    "supply_chain",
    "scenarios",
    "fundamental",
    "sector",
    "technical",
    "capital",
    "sentiment",
    "ownership",
    "us_china",
    "business",
]

DIM_IDS: tuple[str, ...] = (
    "signal",
    "data",
    "intel",
    "plan",
    "phase",
    "history",
    "six_dim",
    "bayesian",
    "conclusion",
    "supply_chain",
    "scenarios",
    "fundamental",
    "sector",
    "technical",
    "capital",
    "sentiment",
    "ownership",
    "us_china",
    "business",
)

DimStatus = Literal["ok", "degraded", "skipped"]

Rating = Literal["买入", "增持", "中性", "减持"]
Confidence = Literal["高", "中", "低"]

# ---------------------------------------------------------------------------
# 通用信封 + 护栏事件
# ---------------------------------------------------------------------------


class DimEnvelope(BaseModel):
    """每个维度产出的统一信封。"""

    model_config = ConfigDict(frozen=True)

    dim: DimId
    status: DimStatus = "ok"
    as_of: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    degraded_reason: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def skipped(self) -> bool:
        return self.status == "skipped"


class GuardrailEvent(BaseModel):
    """护栏触发事件（规则 id、维度、处置、理由），随报告持久化并下发前端。"""

    model_config = ConfigDict(frozen=True)

    rule_id: str
    dim: str
    action: str
    reason: str


# ---------------------------------------------------------------------------
# 短线六件套
# ---------------------------------------------------------------------------


class SignalDim(DimEnvelope):
    """S1 信号：评级/目标价/置信度。由 S2+S3+L3 合成，护栏规则 7 裁决一致性。"""

    dim: Literal["signal"] = "signal"
    rating: Rating = "中性"
    target_price: Optional[float] = Field(None, gt=0)
    expected_value: Optional[float] = Field(None, gt=0)
    signal_type: str = ""
    confidence: Confidence = "中"
    one_sentence: str = ""
    narrative: str = ""


class DataDim(DimEnvelope):
    """S2 数据透视：趋势/价格/量能/筹码，纯规则装配。"""

    dim: Literal["data"] = "data"
    perspective: Optional[DataPerspective] = None
    ma250_deviation_pct: Optional[float] = None
    distance_from_52w_high_pct: Optional[float] = None
    narrative: str = ""


class RootCause(BaseModel):
    """盘面根因四要素（段二数据源）。"""

    model_config = ConfigDict(frozen=True)

    event: str = ""
    mechanism: str = ""
    magnitude: str = ""
    persistence: str = ""


class EventPlan(BaseModel):
    """未来事件 + 各结果交易预案（段三/段五消息面数据源）。"""

    model_config = ConfigDict(frozen=True)

    event: str = ""
    date: str = ""
    outcomes: List[str] = Field(default_factory=list)
    plans: List[str] = Field(default_factory=list)


class IntelDim(DimEnvelope):
    """S3 消息面（方案 v2.1 重定义）：探索型 Agent 三分区产出。

    三分区：情报摘要（含证据候选，供 L2 消费）/ 盘面根因四要素 / 未来事件日历+预案。
    """

    dim: Literal["intel"] = "intel"
    intelligence: Optional[Intelligence] = None
    evidence_items: List[EvidenceItem] = Field(default_factory=list)
    unverified_count: int = Field(0, ge=0)
    root_cause: Optional[RootCause] = None
    event_calendar: List[EventPlan] = Field(default_factory=list)
    narrative: str = ""


class PlanDim(DimEnvelope):
    """S4 作战计划：ATR 规则点位 + 风控仓位，LLM 仅叙述。"""

    dim: Literal["plan"] = "plan"
    sniper_points: Optional[SniperPoints] = None
    position_strategy: Optional[PositionStrategy] = None
    action_checklist: List[str] = Field(default_factory=list)
    atr14: Optional[float] = Field(None, gt=0)
    basis: str = ""
    narrative: str = ""


class PhaseDim(DimEnvelope):
    """S5 阶段决策：交易日历 + 盘中/盘后时段，纯规则。"""

    dim: Literal["phase"] = "phase"
    phase: Optional[PhaseDecision] = None
    trading_day: bool = True
    narrative: str = ""


class HistoryRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    report_id: str
    created_at: str
    rating_hint: str = ""
    one_sentence: str = ""


class HistoryDim(DimEnvelope):
    """S6 历史对比：历次报告观点与漂移标记，纯规则。"""

    dim: Literal["history"] = "history"
    rows: List[HistoryRow] = Field(default_factory=list)
    drift_flags: List[str] = Field(default_factory=list)
    narrative: str = ""


# ---------------------------------------------------------------------------
# 长线五段式
# ---------------------------------------------------------------------------


class SixDimDim(DimEnvelope):
    """L1 六维评分：scoring 引擎规则分 + LLM 主观键值（basis 标注）。"""

    dim: Literal["six_dim"] = "six_dim"
    framework: Optional[ResearchFramework] = None
    scoring_version: str = "v1"
    warnings: List[str] = Field(default_factory=list)
    narrative: str = ""


class BayesianDim(DimEnvelope):
    """L2 贝叶斯：先验（六维映射）→ 证据 LR（标定表校验）→ 后验。"""

    dim: Literal["bayesian"] = "bayesian"
    bayesian: Optional[BayesianFramework] = None
    evidence_rejected: List[str] = Field(default_factory=list)
    market_implied_basis: str = "industry_baseline"
    narrative: str = ""


class ConclusionDim(DimEnvelope):
    """L3 投资结论：六维+贝叶斯+数据/情报合成，护栏规则 2/3 降级。"""

    dim: Literal["conclusion"] = "conclusion"
    conclusion: Optional[InvestmentConclusion] = None
    rationale: str = ""
    narrative: str = ""


class SupplyChainDim(DimEnvelope):
    """L4 产业链：探索型 Agent + verify_supply_chain_evidence 双源校验。"""

    dim: Literal["supply_chain"] = "supply_chain"
    supply_chain: Optional[SupplyChain] = None
    verification_status: str = "not_applicable"
    narrative: str = ""


class ScenariosDim(DimEnvelope):
    """L5 情景：概率和=100% 机器校验 + EV 强制计算 + 估值口径声明。"""

    dim: Literal["scenarios"] = "scenarios"
    scenarios: Optional[ValueScenarios] = None
    probability_sum: float = Field(0.0, ge=0, le=2)
    expected_value: Optional[float] = Field(None, gt=0)
    valuation_basis: Literal["PE_TTM", "PB", "PS", "none"] = "none"
    current_pe_ttm: Optional[float] = None
    # 时间层级路径（段三）：每周期一句"条件→目标位"，由 LLM 生成机器不背书数字
    time_paths: Dict[str, str] = Field(default_factory=dict)
    narrative: str = ""


class FundamentalDim(DimEnvelope):
    """F1 财务与基本面（方案 v2.1 新增）：盈利/成长/安全/估值四框架详表。

    数据消费 fundamental_context 的 valuation/growth/institution 块；
    缺数据项按打分纪律记缺口，不编分。
    """

    dim: Literal["fundamental"] = "fundamental"
    profitability: Dict[str, Any] = Field(default_factory=dict)
    growth_quality: Dict[str, Any] = Field(default_factory=dict)
    financial_safety: Dict[str, Any] = Field(default_factory=dict)
    valuation_detail: Dict[str, Any] = Field(default_factory=dict)
    data_gaps: List[str] = Field(default_factory=list)
    health_score: Optional[float] = Field(None, ge=0, le=100)
    narrative: str = ""


class SectorDim(DimEnvelope):
    """F2 板块分析（方案 v2.1 新增）：政策倾向/行业基率/板块地位/景气。"""

    dim: Literal["sector"] = "sector"
    sector_hint: str = ""
    policy_lean: Optional[str] = None  # supportive / neutral / restrictive
    base_rate: Optional[float] = Field(None, ge=0, le=1)
    base_rate_basis: str = ""
    rankings: Dict[str, Any] = Field(default_factory=dict)
    data_gaps: List[str] = Field(default_factory=list)
    sector_score: Optional[float] = Field(None, ge=0, le=100)
    narrative: str = ""


class TechnicalDim(DimEnvelope):
    """技术研究员：缠论结构 + 支撑压力 + MACD/RSI/波浪。"""

    dim: Literal["technical"] = "technical"
    chanlun_summary: str = ""
    support: Optional[float] = None
    resistance: Optional[float] = None
    indicator_summary: str = ""
    wave_note: str = ""
    score: Optional[float] = Field(None, ge=0, le=100)
    basis: str = ""
    narrative: str = ""


class CapitalDim(DimEnvelope):
    """资金研究员：资金流向 + 机构/大户持仓变动 + 筹码成本结构。"""

    dim: Literal["capital"] = "capital"
    flow_summary: str = ""
    flow_score: Optional[float] = Field(None, ge=0, le=100)
    institution_summary: str = ""
    institution_score: Optional[float] = Field(None, ge=0, le=100)
    chip_summary: Optional[str] = None
    """筹码摘要文本：LLM 返回 dict（{text, status}）由 ``_coerce_researcher_value``
    提取 ``text`` 子字段；schema 用 str 与精校后路径一致（Pydantic strict 模式
    拒绝 dict→str 隐式转换，必须在精校阶段定型）。"""
    chip_score: Optional[float] = Field(None, ge=0, le=100)
    score: Optional[float] = Field(None, ge=0, le=100)
    narrative: str = ""


class SentimentDim(DimEnvelope):
    """情绪研究员：机构评价 + 社区评价（来源等级强制）。"""

    dim: Literal["sentiment"] = "sentiment"
    institute_view: str = ""
    institute_score: Optional[float] = Field(None, ge=0, le=100)
    community_view: str = ""
    community_score: Optional[float] = Field(None, ge=0, le=100)
    unverified_count: int = Field(0, ge=0)
    score: Optional[float] = Field(None, ge=0, le=100)
    narrative: str = ""


class HolderInfo(BaseModel):
    """股权信息条目：姓名 + 来源等级。"""
    name: str
    source: Optional[str] = None  # news/announcement/knowledge_base/inferred


class OwnershipDim(DimEnvelope):
    """股权高管研究员：实控人/十大股东/高管背景与变动（同花顺数据源）。"""

    dim: Literal["ownership"] = "ownership"
    controller: str = ""
    top_holders: List[HolderInfo] = Field(default_factory=list)
    executives: List[HolderInfo] = Field(default_factory=list)
    recent_changes: List[HolderInfo] = Field(default_factory=list)
    data_gaps: List[str] = Field(default_factory=list)
    score: Optional[float] = Field(None, ge=0, le=100)
    narrative: str = ""


class BusinessDim(DimEnvelope):
    """基本面研究员（业务画像）：经营模式/主营产品/竞争地位/与大盘和龙头对比。

    四字段为结构化 Dict（SCHEMA_VERSION 6 定稿）：研究员 LLM 输出 dict 经
    generic_parse 原样放行；渲染层 cn 过滤器递归中文化，杜绝 JSON 裸奔。
    """

    dim: Literal["business"] = "business"
    business_model: Dict[str, Any] = Field(default_factory=dict)
    main_products: Dict[str, Any] = Field(default_factory=dict)
    competitive_position: Dict[str, Any] = Field(default_factory=dict)
    vs_market_leader: Dict[str, Any] = Field(default_factory=dict)
    score: Optional[float] = Field(None, ge=0, le=100)
    data_gaps: List[str] = Field(default_factory=list)
    narrative: str = ""


class UsChinaDim(DimEnvelope):
    """中美竞争研究员：适用性/双链位置/出口管制/制裁风险/替代进度。"""

    dim: Literal["us_china"] = "us_china"
    applicability: str = ""
    role_cn: str = ""
    role_us: str = ""
    export_control: str = ""
    sanction_risk: str = ""
    substitution: str = ""
    score: Optional[float] = Field(None, ge=0, le=100)
    narrative: str = ""


DIM_MODELS: Dict[str, type[DimEnvelope]] = {
    "signal": SignalDim,
    "data": DataDim,
    "intel": IntelDim,
    "plan": PlanDim,
    "phase": PhaseDim,
    "history": HistoryDim,
    "six_dim": SixDimDim,
    "bayesian": BayesianDim,
    "conclusion": ConclusionDim,
    "supply_chain": SupplyChainDim,
    "scenarios": ScenariosDim,
    "fundamental": FundamentalDim,
    "sector": SectorDim,
    "technical": TechnicalDim,
    "capital": CapitalDim,
    "sentiment": SentimentDim,
    "ownership": OwnershipDim,
    "us_china": UsChinaDim,
    "business": BusinessDim,
}


def parse_dim(dim: str, payload: Dict[str, Any]) -> DimEnvelope:
    """按维度 id 解析契约（编排器边界统一入口）。"""
    model = DIM_MODELS.get(dim)
    if model is None:
        raise ValueError(f"未知维度: {dim}")
    return model.model_validate(payload)


# ---------------------------------------------------------------------------
# LR 标定表（§5.4-2：EvidenceItem.lr 的允许区间，机器校验越界即打回）
# ---------------------------------------------------------------------------

LR_RANGES: Dict[str, tuple[float, float]] = {
    "strong_positive": (2.0, 5.0),
    "weak_positive": (1.2, 2.0),
    "neutral": (0.9, 1.1),
    "weak_negative": (0.5, 0.9),
    "strong_negative": (0.2, 0.5),
}
