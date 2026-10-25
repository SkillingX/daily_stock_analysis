# -*- coding: utf-8 -*-
"""
Supply Chain Positioning Scoring (P2: with real data integration).

This dimension evaluates:
1. Position in supply chain (upstream bottleneck vs downstream commodity)
2. Moat type and strength
3. Customer concentration
4. US-China dual chain risk
5. Concept board performance (P2: from akshare)
6. Institutional holdings (P2: from akshare)
"""

from typing import Optional, Dict, Any, List

DEFAULT_NEUTRAL_SCORE = 50.0


def score_supply_chain(
    chain_position: Optional[str] = None,
    moat_type: Optional[str] = None,
    moat_strength: Optional[str] = None,
    customer_concentration: Optional[float] = None,
    us_china_risk: Optional[str] = None,
    chokepoint_type: Optional[str] = None,
    evidence: Optional[str] = None,
    stock_code: Optional[str] = None,
    institutional_score: Optional[float] = None,
    concept_performance: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Score supply chain positioning dimension.

    Args:
        chain_position: upstream/bottleneck/midstream/downstream/commodity
        moat_type: patent/technology/brand/network/switching_cost/license/regulatory
        moat_strength: strong/moderate/weak/none
        customer_concentration: Herfindahl index (0-1), higher = more concentrated
        us_china_risk: high/medium/low/none
        chokepoint_type: patent/capacity/geo/tech/cert
        evidence: LLM-provided evidence summary
        stock_code: Stock code for data enrichment (P2)
        institutional_score: Institutional holdings score from data provider (P2)
        concept_performance: Concept board performance score from data provider (P2)

    Returns:
        Dict with score, indicators, and details
    """
    indicators = []
    total_score = 0.0
    total_weight = 0.0

    if chain_position:
        position_score = _score_chain_position(chain_position)
        indicators.append(
            {
                "name": "产业链位置",
                "score": position_score,
                "weight": 0.25,
                "basis": "llm",
                "summary": f"处于{chain_position}",
            }
        )
        total_score += position_score * 0.25
        total_weight += 0.25

    if moat_type and moat_strength:
        moat_score = _score_moat(moat_type, moat_strength)
        indicators.append(
            {
                "name": "护城河",
                "score": moat_score,
                "weight": 0.25,
                "basis": "llm",
                "summary": f"{moat_type}型护城河，{moat_strength}",
            }
        )
        total_score += moat_score * 0.25
        total_weight += 0.25

    if customer_concentration is not None:
        concentration_score = _score_customer_concentration(customer_concentration)
        indicators.append(
            {
                "name": "客户集中度",
                "score": concentration_score,
                "weight": 0.15,
                "basis": "rule",
                "summary": f"HHI={customer_concentration:.2f}",
            }
        )
        total_score += concentration_score * 0.15
        total_weight += 0.15

    if us_china_risk:
        risk_score = _score_us_china_risk(us_china_risk)
        indicators.append(
            {
                "name": "中美链风险",
                "score": risk_score,
                "weight": 0.15,
                "basis": "llm",
                "summary": f"中美链风险:{us_china_risk}",
            }
        )
        total_score += risk_score * 0.15
        total_weight += 0.15

    if institutional_score is not None:
        indicators.append(
            {
                "name": "机构持仓",
                "score": institutional_score,
                "weight": 0.10,
                "basis": "data_provider",
                "summary": f"机构得分:{institutional_score:.1f}",
            }
        )
        total_score += institutional_score * 0.10
        total_weight += 0.10

    if concept_performance is not None:
        indicators.append(
            {
                "name": "概念板块表现",
                "score": concept_performance,
                "weight": 0.10,
                "basis": "data_provider",
                "summary": f"板块得分:{concept_performance:.1f}",
            }
        )
        total_score += concept_performance * 0.10
        total_weight += 0.10

    if total_weight > 0:
        final_score = total_score / total_weight
        warnings = []
    else:
        final_score = DEFAULT_NEUTRAL_SCORE
        warnings = ["产业链数据缺失，使用中性分"]
        indicators.append(
            {
                "name": "产业链定位",
                "score": DEFAULT_NEUTRAL_SCORE,
                "weight": 1.0,
                "basis": "rule",
                "summary": "数据缺失，使用中性分",
            }
        )

    return {
        "dimension": "产业链定位",
        "score": final_score,
        "weight": 0.25,
        "indicators": indicators,
        "warnings": warnings,
        "evidence": evidence,
    }


def _score_chain_position(position: str) -> float:
    """Score chain position."""
    position_map = {
        "upstream": 90,
        "bottleneck": 95,
        "midstream": 60,
        "downstream": 40,
        "commodity": 20,
    }
    return float(position_map.get(position.lower(), DEFAULT_NEUTRAL_SCORE))


def _score_moat(moat_type: str, strength: str) -> float:
    """Score moat based on type and strength."""
    type_score_map = {
        "patent": 85,
        "technology": 80,
        "brand": 75,
        "network": 85,
        "switching_cost": 70,
        "license": 90,
        "regulatory": 80,
    }
    strength_map = {
        "strong": 1.0,
        "moderate": 0.7,
        "weak": 0.4,
        "none": 0.1,
    }

    base = type_score_map.get(moat_type.lower(), 50)
    multiplier = strength_map.get(strength.lower(), 0.5)
    return base * multiplier


def _score_customer_concentration(hhi: float) -> float:
    """Score customer concentration (HHI). Lower is better for buyers."""
    if hhi < 0.15:
        return 80
    elif hhi < 0.25:
        return 60
    elif hhi < 0.40:
        return 40
    else:
        return 20


def _score_us_china_risk(risk: str) -> float:
    """Score US-China risk. Lower risk = higher score."""
    risk_map = {
        "none": 100,
        "low": 80,
        "medium": 50,
        "high": 25,
    }
    return float(risk_map.get(risk.lower(), DEFAULT_NEUTRAL_SCORE))


def score_supply_chain_with_data(
    stock_code: str,
    chain_position: Optional[str] = None,
    moat_type: Optional[str] = None,
    moat_strength: Optional[str] = None,
    customer_concentration: Optional[float] = None,
    us_china_risk: Optional[str] = None,
    evidence: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Score supply chain with real data from P2 data providers.

    This function fetches data from:
    - ConceptBoardProvider: for concept board performance
    - InstitutionalHoldingsProvider: for institutional score
    - NorthboundFlowProvider: for capital flow data

    Args:
        stock_code: Stock code to fetch data for
        chain_position: LLM-provided chain position
        moat_type: LLM-provided moat type
        moat_strength: LLM-provided moat strength
        customer_concentration: Herfindahl index
        us_china_risk: US-China risk level
        evidence: LLM evidence summary

    Returns:
        Dict with score, indicators, and data enrichment info
    """
    institutional_score = None
    # 概念板块表现取数（东财公开概念接口）已随 issue #45 退役，不再参与评分。
    concept_score = None

    try:
        from data_provider.supply_chain import InstitutionalHoldingsProvider

        institutional_provider = InstitutionalHoldingsProvider()

        institutional_data = institutional_provider.calculate_institutional_score(
            stock_code
        )
        if institutional_data:
            institutional_score = institutional_data.get("score")

    except ImportError:
        pass
    except Exception:
        pass

    return score_supply_chain(
        chain_position=chain_position,
        moat_type=moat_type,
        moat_strength=moat_strength,
        customer_concentration=customer_concentration,
        us_china_risk=us_china_risk,
        evidence=evidence,
        stock_code=stock_code,
        institutional_score=institutional_score,
        concept_performance=concept_score,
    )


def _score_concept_performance(change_pct: float) -> float:
    """
    Score concept board performance.

    Args:
        change_pct: Average change percentage of concept boards

    Returns:
        Score from 0-100
    """
    if change_pct >= 5:
        return 90
    elif change_pct >= 3:
        return 80
    elif change_pct >= 1:
        return 70
    elif change_pct >= 0:
        return 60
    elif change_pct >= -1:
        return 50
    elif change_pct >= -3:
        return 40
    elif change_pct >= -5:
        return 30
    else:
        return 20
