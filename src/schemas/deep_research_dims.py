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
)

DimStatus = Literal["ok", "degraded"]

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


class IntelDim(DimEnvelope):
    """S3 情报：探索型 Agent 产出，含证据候选（供 L2 消费）。"""

    dim: Literal["intel"] = "intel"
    intelligence: Optional[Intelligence] = None
    evidence_items: List[EvidenceItem] = Field(default_factory=list)
    unverified_count: int = Field(0, ge=0)
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
