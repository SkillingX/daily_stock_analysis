# -*- coding: utf-8 -*-
"""评分框架 v2 指标打分引擎：规则分 + LLM 依据句分，缺数据强制中性占位。

打分纪律（方案决策 #2）：
- 指标无数据 → 中性 50 + confidence=low + data_gap=True + 缺口说明（不编分）；
- LLM 分必须带依据句（summary），否则视为缺数据；
- 维度得分 = 按可用指标权重归一加权；全部缺数据 → 中性 50。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.scoring.weights_v2 import INDICATORS_V2

NEUTRAL_SCORE = 50.0


def gap_indicator(indicator_id: str, reason: str = "数据缺失") -> Dict[str, Any]:
    """缺数据指标的中性占位（打分纪律）。"""
    return {
        "id": indicator_id,
        "score": NEUTRAL_SCORE,
        "confidence": "low",
        "basis": "rule",
        "data_gap": True,
        "summary": f"数据缺口：{reason}（按纪律记中性分，不纳入有效信号）",
    }


def scored_indicator(
    indicator_id: str,
    score: float,
    summary: str,
    basis: str = "rule",
    confidence: str = "medium",
    data_gap: bool = False,
) -> Dict[str, Any]:
    """有效指标分（score 夹到 [0,100]，summary 必填）。"""
    return {
        "id": indicator_id,
        "score": max(0.0, min(100.0, float(score))),
        "confidence": confidence,
        "basis": basis,
        "data_gap": data_gap,
        "summary": summary,
    }


def aggregate_v2_dimensions(
    indicator_results: Dict[str, Dict[str, Optional[Dict[str, Any]]]],
) -> List[Dict[str, Any]]:
    """指标结果 → 六维得分（每维：indicators 明细 + 维度得分 + 缺口数）。

    Args:
        indicator_results: {维度: {指标 id: 结果 or None}}；None/缺项按纪律补中性占位。
    """
    dimensions: List[Dict[str, Any]] = []
    for dim, spec in INDICATORS_V2.items():
        indicators: List[Dict[str, Any]] = []
        for ind_id, ind_name, weight, _source in spec:
            result = (indicator_results.get(dim) or {}).get(ind_id)
            if not result:
                result = gap_indicator(ind_id, "该指标尚未接线数据源")
            indicators.append(
                {
                    "id": ind_id,
                    "name": ind_name,
                    "weight": weight,
                    "score": float(result["score"]),
                    "confidence": result.get("confidence"),
                    "basis": result.get("basis", "rule"),
                    "data_gap": bool(result.get("data_gap")),
                    "summary": str(result.get("summary") or ""),
                }
            )
        available = [i for i in indicators if not i["data_gap"]]
        weight_sum = sum(i["weight"] for i in available)
        if weight_sum > 0:
            dim_score = sum(i["score"] * i["weight"] for i in available) / weight_sum
        else:
            dim_score = NEUTRAL_SCORE
        gaps = sum(1 for i in indicators if i["data_gap"])
        dimensions.append(
            {
                "dimension": dim,
                "score": round(dim_score, 2),
                "indicators": indicators,
                "data_gaps": gaps,
            }
        )
    return dimensions


def framework_total_v2(dimensions: List[Dict[str, Any]]) -> float:
    """六维得分 × 维度权重 → 总分（单一评分源：段一评分 = 贝叶斯先验输入）。"""
    from src.scoring.weights_v2 import DIMENSION_WEIGHTS_V2

    total = 0.0
    for d in dimensions:
        total += float(d["score"]) * DIMENSION_WEIGHTS_V2[d["dimension"]]
    return round(total, 2)


# ---------------------------------------------------------------------------
# 规则打分器（技术面 / 资金面 / 基本面-估值）
# ---------------------------------------------------------------------------


def score_valuation(pe_ttm: Optional[float], pb: Optional[float]) -> Optional[Dict[str, Any]]:
    """估值分：PE 分档为主，亏损股切 PB 口径（与情景维度口径一致）。"""
    if pe_ttm is not None and pe_ttm > 0:
        if pe_ttm < 15:
            score, label = 78.0, "低估区"
        elif pe_ttm < 25:
            score, label = 66.0, "合理偏低"
        elif pe_ttm < 40:
            score, label = 52.0, "合理"
        elif pe_ttm < 60:
            score, label = 38.0, "偏高"
        else:
            score, label = 26.0, "高估区"
        return scored_indicator(
            "valuation", score, f"PE(TTM) {pe_ttm:.1f}x，处于{label}", "rule", "high"
        )
    if pb is not None and pb > 0:
        if pb < 1.5:
            score, label = 70.0, "PB 低位"
        elif pb < 3:
            score, label = 55.0, "PB 中性"
        elif pb < 6:
            score, label = 42.0, "PB 偏高"
        else:
            score, label = 30.0, "PB 高位"
        return scored_indicator(
            "valuation", score, f"PE 不可用，PB {pb:.2f}x，{label}（亏损股口径）", "rule", "medium"
        )
    return None


def score_chip_cost(
    current: Optional[float], avg_cost: Optional[float], profit_ratio: Optional[float]
) -> Optional[Dict[str, Any]]:
    """筹码成本结构：现价相对平均成本的位置 + 获利盘比例。"""
    if current and avg_cost and avg_cost > 0:
        dev = (current - avg_cost) / avg_cost * 100
        if profit_ratio is not None:
            summary = (
                f"现价较平均成本 {dev:+.1f}%，获利盘 {float(profit_ratio):.0f}%"
                + ("，上方套牢盘轻" if dev > 0 else "，现价处于成本区下方")
            )
        else:
            summary = f"现价较平均成本 {dev:+.1f}%"
        score = 65.0 if dev > 0 else 45.0
        return scored_indicator("chip_cost", score, summary, "rule", "medium")
    return None


def score_institution_change(institution_holding_change: Optional[Any]) -> Optional[Dict[str, Any]]:
    """机构/大户持仓变动（fundamental_context institution 块）。"""
    if institution_holding_change is None:
        return None
    try:
        change = float(institution_holding_change)
    except (TypeError, ValueError):
        return None
    if change > 5:
        score, label = 74.0, "机构显著增持"
    elif change > 0:
        score, label = 62.0, "机构小幅增持"
    elif change > -5:
        score, label = 46.0, "机构小幅减持"
    else:
        score, label = 34.0, "机构显著减持"
    return scored_indicator(
        "institution_change", score, f"{label}（变动 {change:+.1f}%）", "rule", "medium"
    )


def score_support_indicator(
    current: Optional[float],
    support: Optional[float],
    resistance: Optional[float],
    rsi14: Optional[float],
    macd_hist: Optional[float],
) -> Optional[Dict[str, Any]]:
    """支撑压力 + MACD/RSI 综合分（三要素可得其二即打分，否则缺数据）。"""
    parts: List[str] = []
    score_parts: List[float] = []
    if current and support and current > 0:
        dist = (current - support) / current * 100
        score_parts.append(62.0 if 1 < dist < 12 else 48.0)
        parts.append(f"距支撑 {dist:.1f}%")
    if current and resistance and resistance > current:
        up = (resistance - current) / current * 100
        score_parts.append(64.0 if up > 8 else 52.0)
        parts.append(f"距阻力 {up:.1f}%（空间{'充足' if up > 8 else '有限'}）")
    if rsi14 is not None:
        rsi = float(rsi14)
        score_parts.append(60.0 if 40 <= rsi <= 65 else 44.0)
        parts.append(f"RSI14 {rsi:.0f}")
    if macd_hist is not None:
        score_parts.append(62.0 if float(macd_hist) > 0 else 44.0)
        parts.append("MACD 柱" + ("转正" if float(macd_hist) > 0 else "为负"))
    if len(score_parts) < 2:
        return None
    return scored_indicator(
        "support_indicator",
        sum(score_parts) / len(score_parts),
        "；".join(parts),
        "rule",
        "medium",
    )
