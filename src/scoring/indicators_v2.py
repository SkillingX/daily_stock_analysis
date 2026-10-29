# -*- coding: utf-8 -*-
"""评分框架 v2 指标打分引擎：规则分 + LLM 依据句分，缺数据强制中性占位。

打分纪律（方案决策 #2）：
- 指标无数据 → 中性 50 + confidence=low + data_gap=True + 缺口说明（不编分）；
- LLM 分必须带依据句（summary），否则视为缺数据；
- 维度得分 = 按可用指标权重归一加权；全部缺数据 → 中性 50。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional

from icontract import ensure

from src.scoring.weights_v2 import DIMENSION_WEIGHTS_V2, INDICATORS_V2

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


def _weighted_dimension_results_valid(dimensions: List[Dict[str, Any]]) -> bool:
    """分维聚合后置条件谓词（Layer 2 跨字段不变式，供 @ensure 挂载）。

    数值守卫，只管归一聚合：每个维度得分必须等于其明细中非缺口指标按既有
    权重的精确归一加权，并经既有的 ``round(·, 2)`` 输出语义（跟随当前 Decimal
    上下文，不另定舍入模式）取整后精确相等；全部缺口的维度必须中性 50。
    谓词以返回的明细为独立输入 Decimal 复算，不信任最终得分——归一错误
    （如 80/60/40 被误算成 49）即使总分仍在 ``framework_total_v2`` 的范围
    ensure 内也在此拒绝。
    """
    for dim in dimensions:
        active = [entry for entry in dim["indicators"] if not entry["data_gap"]]
        if active:
            expected = (
                sum(
                    (
                        Decimal(str(entry["score"])) * Decimal(str(entry["weight"]))
                        for entry in active
                    ),
                    Decimal(0),
                )
                / sum((Decimal(str(entry["weight"])) for entry in active), Decimal(0))
            )
        else:
            expected = Decimal(str(NEUTRAL_SCORE))
        if Decimal(str(dim["score"])) != round(expected, 2):
            return False
    return True


@ensure(
    lambda result: _weighted_dimension_results_valid(result),
    "分维得分 = 非缺口指标按既有权重的精确归一加权（round(·,2) 后精确相等），"
    "全缺口维度中性 50（归一错误即使总分未越界也必须拒绝）",
)
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
            if not result or result.get("score") is None:
                result = gap_indicator(ind_id, "当前未形成可用于该项评分的输入")
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
        # 金融计算纪律：加权求和用 Decimal（issue #64 权重 31.25/43.75/25 为
        # 精确十进制分数，float 累积漂移不得进入评分）。
        weight_sum = sum((Decimal(str(i["weight"])) for i in available), Decimal(0))
        if weight_sum > 0:
            dim_score = sum(
                (Decimal(str(i["score"])) * Decimal(str(i["weight"])) for i in available),
                Decimal(0),
            ) / weight_sum
        else:
            dim_score = Decimal(str(NEUTRAL_SCORE))
        gaps = sum(1 for i in indicators if i["data_gap"])
        dimensions.append(
            {
                "dimension": dim,
                "score": float(round(dim_score, 2)),
                "indicators": indicators,
                "data_gaps": gaps,
            }
        )
    return dimensions


@ensure(
    lambda dimensions, result: result is not None
    and (
        not dimensions
        or (
            min(float(d["score"]) for d in dimensions) - 0.01
            <= result
            <= max(float(d["score"]) for d in dimensions) + 0.01
        )
    ),
    "总分必须落在各维度得分范围内（跨字段不变式：加权不得越界）",
)
def framework_total_v2(dimensions: List[Dict[str, Any]]) -> float:
    """六维得分 × 维度权重 → 总分（单一评分源：段一评分 = 贝叶斯先验输入）。"""
    total = sum(
        (
            Decimal(str(d["score"])) * Decimal(str(DIMENSION_WEIGHTS_V2[d["dimension"]]))
            for d in dimensions
        ),
        Decimal(0),
    )
    return float(round(total, 2))


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
