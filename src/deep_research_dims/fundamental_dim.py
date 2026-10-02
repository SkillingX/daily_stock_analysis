# -*- coding: utf-8 -*-
"""F1 财务与基本面维度：盈利/成长/安全/估值四框架详表（规则装配，不编分）。"""

from __future__ import annotations

from typing import Any, Dict, Optional

from src.deep_research_dims.context import SharedContext
from src.scoring.indicators_v2 import score_valuation
from src.schemas.deep_research_dims import FundamentalDim


def _band(value: Optional[float], bands: list[tuple[float, float, str]]) -> tuple[Optional[float], str]:
    """分档打分：(分数, 标签)；无数据 → (None, '数据缺失')。"""
    if value is None:
        return None, "数据缺失"
    for low, score, label in bands:
        if value >= low:
            return score, label
    return bands[-1][1] - 10 if bands else None, "极低"


def build_fundamental_dim(
    ctx: SharedContext,
    llm_scenarios: Optional[Dict[str, Any]] = None,
) -> FundamentalDim:
    """F1：消费 fundamental_context 快照（valuation/growth/institution 块）。"""
    fund = ctx.fundamental
    gaps: list[str] = []

    roe = fund.get("roe")
    roe_score, roe_label = _band(
        float(roe) if isinstance(roe, (int, float)) else None,
        [(20, 85.0, "优秀"), (15, 75.0, "良好"), (10, 60.0, "一般"), (5, 45.0, "偏弱")],
    )
    gm = fund.get("gross_margin")
    gm_score, gm_label = _band(
        float(gm) if isinstance(gm, (int, float)) else None,
        [(50, 82.0, "高毛利"), (30, 65.0, "中高"), (15, 50.0, "中等"), (0, 40.0, "低毛利")],
    )
    profitability = {
        "roe": {"value": roe, "score": roe_score, "label": roe_label},
        "gross_margin": {"value": gm, "score": gm_score, "label": gm_label},
    }
    if roe_score is None:
        gaps.append("ROE")
    if gm_score is None:
        gaps.append("毛利率")

    rev = fund.get("revenue_growth")
    rev_score, rev_label = _band(
        float(rev) if isinstance(rev, (int, float)) else None,
        [(30, 80.0, "高增长"), (10, 65.0, "稳健增长"), (0, 50.0, "微增"), (-100, 35.0, "负增长")],
    )
    np_yoy = fund.get("net_profit_yoy")
    np_score, np_label = _band(
        float(np_yoy) if isinstance(np_yoy, (int, float)) else None,
        [(30, 80.0, "高增长"), (10, 65.0, "稳健"), (0, 50.0, "微增"), (-100, 35.0, "下滑")],
    )
    scissors = None
    if rev_score is not None and np_score is not None:
        scissors = round(float(np_yoy) - float(rev), 2)
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
        fund.get("pe_ttm") if isinstance(fund.get("pe_ttm"), (int, float)) else None,
        fund.get("pb") if isinstance(fund.get("pb"), (int, float)) else None,
    )
    valuation_detail = {
        "pe_ttm": fund.get("pe_ttm"),
        "pb": fund.get("pb"),
        "market_cap": fund.get("market_cap"),
        "assessment": valuation["summary"] if valuation else "估值数据缺失",
        "score": valuation["score"] if valuation else None,
    }
    if valuation is None:
        gaps.append("估值")

    scores = [s for s in (roe_score, gm_score, rev_score, np_score, valuation["score"] if valuation else None) if s is not None]
    health = round(sum(scores) / len(scores), 2) if scores else None

    return FundamentalDim(
        profitability=profitability,
        growth_quality=growth_quality,
        financial_safety=financial_safety,
        valuation_detail=valuation_detail,
        data_gaps=gaps,
        health_score=health,
        narrative=(
            f"财务健康分 {health}/100（可得 {len(scores)} 项指标均值）；"
            f"缺口 {len(gaps)} 项：{'、'.join(gaps)}。"
        ),
    )
