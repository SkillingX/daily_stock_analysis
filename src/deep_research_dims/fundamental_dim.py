# -*- coding: utf-8 -*-
"""F1 财务与基本面维度：盈利/成长/安全/估值四框架详表（规则装配，不编分）。"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Dict, Optional

from icontract import ensure
from data_provider.cross_source_validator import AnchorQuality, adopted_field_record, reading_from_field_record

from src.deep_research_dims.context import SharedContext
from src.scoring.indicators_v2 import score_valuation
from src.schemas.deep_research_dims import FinancialDataQuality, FinancialFieldQuality, FundamentalDim


_FUNDAMENTAL_FIELDS = {"pe_ratio": "pe_ttm", "pb_ratio": "pb", "roe": "roe", "gross_margin": "gross_margin", "revenue_yoy": "revenue_growth", "net_profit_yoy": "net_profit_yoy"}


def financial_field_quality(fund: Dict[str, Any], field: str) -> FinancialFieldQuality:
    """Reuse the current adopted record; a legacy scalar supplies no semantic proof."""
    metadata = (fund.get("field_meta") or {}).get(field) or {}
    if metadata.get("value", fund.get(_FUNDAMENTAL_FIELDS[field])) != fund.get(_FUNDAMENTAL_FIELDS[field]):
        metadata = {}
    reading = reading_from_field_record(fund.get(_FUNDAMENTAL_FIELDS[field]), metadata)
    if reading is None:
        return FinancialFieldQuality(quality=AnchorQuality(status="missing"))
    quality = AnchorQuality.model_validate_json(json.dumps(metadata.get("quality") or {"status": "unverified"})) if reading.normalization_error != "metadata_invalid" else AnchorQuality()
    record = adopted_field_record(field, reading, quality, selection_reason=metadata.get("selection_reason", "unknown"))
    return FinancialFieldQuality(reading=reading, quality=quality, rule_eligible=record["rule_eligible"], input_reasons=tuple(record["input_reasons"]), selection_reason=record["selection_reason"])


def eligible_financial_value(fund: Dict[str, Any], field: str) -> Optional[float]:
    """All named rule consumers use the same field eligibility."""
    evidence = financial_field_quality(fund, field)
    return evidence.reading.value if evidence.rule_eligible and evidence.reading is not None else None


def _band(value: Optional[float], bands: list[tuple[float, float, str]]) -> tuple[Optional[float], str]:
    """分档打分：(分数, 标签)；无数据 → (None, '数据缺失')。"""
    if value is None:
        return None, "数据缺失"
    for low, score, label in bands:
        if value >= low:
            return score, label
    return bands[-1][1] - 10 if bands else None, "极低"


@ensure(lambda result: result.data_quality is not None and result.data_quality.rule_usable_count <= result.data_quality.obtained_count, "Usable fields must have been acquired")
@ensure(lambda result: result.data_quality is not None and (result.health_score is None) == (result.data_quality.health_components == 0), "Health exists exactly when a component participates")
def build_fundamental_dim(
    ctx: SharedContext,
    llm_scenarios: Optional[Dict[str, Any]] = None,
) -> FundamentalDim:
    """F1：消费 fundamental_context 快照（valuation/growth/institution 块）。"""
    fund = ctx.fundamental
    gaps: list[str] = []
    fields = {field: financial_field_quality(fund, field) for field in _FUNDAMENTAL_FIELDS}
    usable = {field: evidence.reading.value if evidence.rule_eligible and evidence.reading is not None else None for field, evidence in fields.items()}
    obtained = {field: evidence.reading.value if evidence.reading is not None else None for field, evidence in fields.items()}

    roe = obtained["roe"]
    roe_score, roe_label = _band(
        usable["roe"],
        [(20, 85.0, "优秀"), (15, 75.0, "良好"), (10, 60.0, "一般"), (5, 45.0, "偏弱")],
    )
    gm = obtained["gross_margin"]
    gm_score, gm_label = _band(
        usable["gross_margin"],
        [(50, 82.0, "高毛利"), (30, 65.0, "中高"), (15, 50.0, "中等"), (0, 40.0, "低毛利")],
    )
    if roe is not None and roe_score is None:
        roe_label = "不可用于规则"
    if gm is not None and gm_score is None:
        gm_label = "不可用于规则"
    profitability = {
        "roe": {"value": roe, "score": roe_score, "label": roe_label},
        "gross_margin": {"value": gm, "score": gm_score, "label": gm_label},
    }
    if roe_score is None:
        gaps.append("ROE")
    if gm_score is None:
        gaps.append("毛利率")

    rev = obtained["revenue_yoy"]
    rev_score, rev_label = _band(
        usable["revenue_yoy"],
        [(30, 80.0, "高增长"), (10, 65.0, "稳健增长"), (0, 50.0, "微增"), (-100, 35.0, "负增长")],
    )
    np_yoy = obtained["net_profit_yoy"]
    np_score, np_label = _band(
        usable["net_profit_yoy"],
        [(30, 80.0, "高增长"), (10, 65.0, "稳健"), (0, 50.0, "微增"), (-100, 35.0, "下滑")],
    )
    scissors = None
    rev_reading, np_reading = fields["revenue_yoy"].reading, fields["net_profit_yoy"].reading
    if rev_score is not None and np_score is not None and rev_reading is not None and np_reading is not None and rev_reading.period is not None and rev_reading.period == np_reading.period and rev_reading.period_basis != "unknown" and rev_reading.period_basis == np_reading.period_basis:
        scissors = float(round(Decimal(str(np_reading.value)) - Decimal(str(rev_reading.value)), 2))
    if rev is not None and rev_score is None:
        rev_label = "不可用于规则"
    if np_yoy is not None and np_score is None:
        np_label = "不可用于规则"
    growth_quality = {
        "revenue_yoy": {"value": rev, "score": rev_score, "label": rev_label},
        "net_profit_yoy": {"value": np_yoy, "score": np_score, "label": np_label},
        "scissors": scissors,  # 净利-营收增速剪刀差：负值=增收不增利
    }
    if rev_score is None:
        gaps.append("营收增速")
    if np_score is None:
        gaps.append("净利增速")

    inst = fund.get("institution_holding_change")
    financial_safety = {
        "institution_holding_change": inst,
        "leverage": None,  # 数据源暂无 → 缺口
    }
    gaps.append("资产负债结构")

    valuation = score_valuation(
        usable["pe_ratio"],
        usable["pb_ratio"],
    )
    valuation_detail = {
        "pe_ttm": obtained["pe_ratio"],
        "pb": obtained["pb_ratio"],
        "market_cap": fund.get("market_cap"),
        "assessment": valuation["summary"] if valuation else "估值输入不可用于规则" if obtained["pe_ratio"] is not None or obtained["pb_ratio"] is not None else "估值数据缺失",
        "score": valuation["score"] if valuation else None,
    }
    if valuation is None:
        gaps.append("估值")

    scores = [s for s in (roe_score, gm_score, rev_score, np_score, valuation["score"] if valuation else None) if s is not None]
    health = float(round(sum(Decimal(str(score)) for score in scores) / Decimal(len(scores)), 2)) if scores else None
    n = sum(evidence.reading is not None for evidence in fields.values())
    m = sum(evidence.rule_eligible for evidence in fields.values())
    complete = m == 6 and len(scores) == 5
    warnings = ["各字段仅按自身证据使用，组件完整不代表同期财报。", "规则评分置信度与跨源核验状态分别解释。"]
    if not all(evidence.quality.status == "verified" for evidence in fields.values()):
        warnings.append("财务字段尚未全部独立核验；未核验、单源、不可比或冲突限制详见字段证据。")
    if not complete:
        warnings.append("当前仅可进行获准指标的局部评估，不能作为完整财务健康结论。")
    quality = FinancialDataQuality(state="none" if n == 0 else "complete" if n == 6 else "partial", obtained_count=n, rule_usable_count=m, health_components=len(scores), fields=fields, warnings=tuple(warnings))

    return FundamentalDim(
        status="ok" if complete else "degraded",
        degraded_reason=None if complete else "财务指标缺失或存在不可用于规则的输入",
        data_quality=quality,
        profitability=profitability,
        growth_quality=growth_quality,
        financial_safety=financial_safety,
        valuation_detail=valuation_detail,
        data_gaps=gaps,
        health_score=health,
        narrative=(
            f"{'财务组件完整评估' if complete else '局部指标评估'}：评分 {health if health is not None else '无可用评分'}（参与 {len(scores)}/5 组件）；"
            f"缺口 {len(gaps)} 项：{'、'.join(gaps)}。"
        ),
    )
