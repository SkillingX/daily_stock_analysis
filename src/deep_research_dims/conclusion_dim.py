# -*- coding: utf-8 -*-
"""L3 投资结论：六维+贝叶斯+数据/情报合成，六选一行动。叙述型 LLM。"""

from __future__ import annotations

from typing import Any, Dict

from src.deep_research_dims.context import SharedContext
from src.schemas.deep_research_dims import ConclusionDim
from src.schemas.investment_conclusion import InvestmentConclusion


def _map_action(posterior: float, edge: float, six_total: float) -> str:
    """机器行动映射（护栏规则 2/3 在此前的 guardrail 层再降级）。"""
    if edge <= 0:
        return "观察"
    if posterior >= 0.65 and six_total >= 70:
        return "建仓"
    if posterior >= 0.55:
        return "加仓" if edge > 0.15 else "持有"
    if posterior >= 0.45:
        return "持有"
    return "观察"


def build_conclusion_dim(
    ctx: SharedContext,
    six_dim_payload: Dict[str, Any],
    bayesian_payload: Dict[str, Any],
    scenarios_payload: Dict[str, Any],
) -> ConclusionDim:
    """L3：规则合成结论；rationale 由 LLM 叙述（narrate 层），数字零 LLM。"""
    framework = (six_dim_payload or {}).get("framework") or {}
    bay = (bayesian_payload or {}).get("bayesian") or {}
    six_total = float(framework.get("dimension_total") or 50.0)
    posterior = float(bay.get("posterior_p") or 0.5)
    edge = float(bay.get("edge") or 0.0)
    action = _map_action(posterior, edge, six_total)

    horizons = ((scenarios_payload or {}).get("scenarios") or {}).get("horizons") or {}
    conclusion = InvestmentConclusion(
        prior_p=bay.get("prior_p"),
        market_implied_p=bay.get("market_implied_p"),
        edge=bay.get("edge"),
        position=action,
        action=action,  # type: ignore[arg-type]
        value_range_1y=horizons.get("horizon_1y"),
        value_range_3y=horizons.get("horizon_3y"),
        value_range_5y=horizons.get("horizon_5y"),
        rationale="",
    )
    return ConclusionDim(
        conclusion=conclusion,
        rationale="",
        narrative=(
            f"六维 {six_total:.0f} 分 → 后验 {posterior:.2f} / Edge {edge:+.2f} "
            f"→ 行动「{action}」（机器映射，护栏复核见附录）。"
        ),
    )
