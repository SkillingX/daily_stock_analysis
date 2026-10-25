# -*- coding: utf-8 -*-
"""
Capital Flow Scoring (Rule-based).

This dimension evaluates:
1. Institutional holdings changes
2. Northbound (陆股通) flow
3. Margin financing (融资融券) balance
"""

from typing import Optional, Dict, Any

DEFAULT_NEUTRAL_SCORE = 50.0


def score_capital(
    institutional_holding_change: Optional[float] = None,
    northbound_flow_20d: Optional[float] = None,
    margin_balance_change: Optional[float] = None,
    foreign_ratio: Optional[float] = None,
    evidence: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Score capital flow dimension.

    Args:
        institutional_holding_change: Change in institutional holdings (%) over quarter
        northbound_flow_20d: Northbound flow in last 20 trading days (billion CNY)
        margin_balance_change: Margin balance change (%) over 20 days
        foreign_ratio: Foreign investor ratio (%), if available
        evidence: Additional evidence summary

    Returns:
        Dict with score, indicators, and details
    """
    indicators = []
    total_score = 0.0
    total_weight = 0.0

    if institutional_holding_change is not None:
        inst_score = _score_institutional_change(institutional_holding_change)
        indicators.append(
            {
                "name": "机构持仓变化",
                "score": inst_score,
                "weight": 0.30,
                "basis": "rule",
                "summary": f"机构持仓变化{institutional_holding_change:+.1f}%",
            }
        )
        total_score += inst_score * 0.30
        total_weight += 0.30

    if northbound_flow_20d is not None:
        northbound_score = _score_northbound_flow(northbound_flow_20d)
        indicators.append(
            {
                "name": "北向资金",
                "score": northbound_score,
                "weight": 0.25,
                "basis": "rule",
                "summary": f"20日北向净流入{northbound_flow_20d:+.2f}亿",
            }
        )
        total_score += northbound_score * 0.25
        total_weight += 0.25

    if margin_balance_change is not None:
        margin_score = _score_margin_change(margin_balance_change)
        indicators.append(
            {
                "name": "融资余额变化",
                "score": margin_score,
                "weight": 0.20,
                "basis": "rule",
                "summary": f"融资余额变化{margin_balance_change:+.1f}%",
            }
        )
        total_score += margin_score * 0.20
        total_weight += 0.20

    if foreign_ratio is not None:
        foreign_score = _score_foreign_ratio(foreign_ratio)
        indicators.append(
            {
                "name": "外资占比",
                "score": foreign_score,
                "weight": 0.10,
                "basis": "rule",
                "summary": f"外资持股比{foreign_ratio:.2f}%",
            }
        )
        total_score += foreign_score * 0.10
        total_weight += 0.10

    if total_weight > 0:
        final_score = total_score / total_weight
    else:
        final_score = DEFAULT_NEUTRAL_SCORE
        indicators.append(
            {
                "name": "资金面",
                "score": DEFAULT_NEUTRAL_SCORE,
                "weight": 1.0,
                "basis": "rule",
                "summary": "数据缺失，使用中性分",
            }
        )

    return {
        "dimension": "资金面",
        "score": final_score,
        "weight": 0.15,
        "indicators": indicators,
        "evidence": evidence,
    }


def _score_institutional_change(change: float) -> float:
    """Score institutional holding change. Positive = good."""
    if change > 5:
        return 90
    elif change > 2:
        return 75
    elif change > 0:
        return 60
    elif change > -2:
        return 45
    elif change > -5:
        return 30
    else:
        return 15


def _score_northbound_flow(flow: float) -> float:
    """Score northbound flow. Positive = good (in billion CNY)."""
    if flow > 5:
        return 90
    elif flow > 2:
        return 75
    elif flow > 0:
        return 60
    elif flow > -2:
        return 45
    elif flow > -5:
        return 30
    else:
        return 15


def _score_margin_change(change: float) -> float:
    """Score margin balance change. Moderate increase = good, excessive = warning."""
    if change > 30:
        return 40
    elif change > 15:
        return 70
    elif change > 5:
        return 60
    elif change > -5:
        return 50
    elif change > -15:
        return 40
    else:
        return 25



def _score_foreign_ratio(ratio: float) -> float:
    """Score foreign ratio. Higher generally = more stable."""
    if ratio > 30:
        return 80
    elif ratio > 20:
        return 70
    elif ratio > 10:
        return 60
    elif ratio > 5:
        return 50
    else:
        return 40
