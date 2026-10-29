# -*- coding: utf-8 -*-
"""L3 投资结论：六维+贝叶斯+数据/情报合成，六选一行动。叙述型 LLM。"""

from __future__ import annotations

from typing import Any, Dict

from src.deep_research_dims.context import SharedContext
from src.deep_research_dims.scenarios_dim import (
    NON_VALUATION_SCOPE,
    has_substantive_qualitative_content,
    with_valuation_cascade,
)
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
    """L3：规则合成结论；rationale 由 LLM 叙述（narrate 层），数字零 LLM。

    issue #64：合法非估值分支（情景 ok 且口径 none）下结论同样 ok——行动保持
    观察/条件跟踪，不因六维高分放行建仓/加仓，也不生成估值驱动结论。
    """
    framework = (six_dim_payload or {}).get("framework") or {}
    bay = (bayesian_payload or {}).get("bayesian") or {}
    raw_total = framework.get("dimension_total")
    raw_posterior = bay.get("posterior_p")
    # 输入有效性（issue #64/R7）：结论消费真实结构而非默认值——六维必须有维度
    # 明细（真实构建恒含六维行），贝叶斯必须有后验结构。空对象/仅默认 50/0.5
    # 的填充值不构成有效结论输入；真实计算出的 50 分同样合法。
    six_structure_valid = (
        isinstance(framework.get("dimensions"), list) and len(framework["dimensions"]) > 0
        and isinstance(raw_total, (int, float)) and not isinstance(raw_total, bool)
    )
    bayes_valid = isinstance(bay, dict) and bool(bay) and isinstance(raw_posterior, (int, float)) and not isinstance(raw_posterior, bool)
    inputs_valid = six_structure_valid and bayes_valid
    six_total = float(raw_total) if isinstance(raw_total, (int, float)) and not isinstance(raw_total, bool) else 50.0
    posterior = float(raw_posterior) if isinstance(raw_posterior, (int, float)) and not isinstance(raw_posterior, bool) else 0.5
    edge = float(bay.get("edge") or 0.0)
    valuation_available = scenarios_payload.get("status") == "ok" and (scenarios_payload.get("target_price") is not None or scenarios_payload.get("expected_value") is not None)
    # 非估值资格（issue #64/R7/B5）：与情景维度装配层共用同一证据谓词——情景
    # scenarios 载荷即证据包（industry_space/competitive_evolution/catalysts/
    # risks），任一实际非空文本/条件/风险即算实质内容；只有状态字段或空白
    # 内容的空壳不放行，维持真实降级。
    scenarios_scenarios = (scenarios_payload.get("scenarios") or {})
    qualitative_content_valid = bool(
        str(scenarios_payload.get("narrative") or "").strip()
    ) and has_substantive_qualitative_content({
        "industry_space": scenarios_scenarios.get("industry_space"),
        "competitive_evolution": scenarios_scenarios.get("competitive_evolution"),
        "catalysts": scenarios_scenarios.get("catalysts"),
        "risks": scenarios_scenarios.get("risks"),
    })
    qualitative_ready = (
        scenarios_payload.get("status") == "ok"
        and scenarios_payload.get("valuation_basis") == "none"
        and not valuation_available
        and inputs_valid
        and qualitative_content_valid
    )
    action = _map_action(posterior, edge, six_total) if valuation_available else "观察"
    target = scenarios_payload.get("target_price") or scenarios_payload.get("expected_value")
    current = ctx.quote.get("price")
    if action in {"建仓", "加仓"} and (not isinstance(current, (int, float)) or not isinstance(target, (int, float)) or current <= 0 or target <= current):
        action = "观察"

    horizons = ((scenarios_payload or {}).get("scenarios") or {}).get("horizons") or {}
    if not valuation_available:
        horizons = {}
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
    if valuation_available:
        status, degraded_reason = "ok", None
        narrative = (
            f"六维 {six_total:.0f} 分 → 后验 {posterior:.2f} / Edge {edge:+.2f} "
            f"→ 行动「{action}」（机器映射，护栏复核见附录）。"
        )
    elif qualitative_ready:
        status, degraded_reason = "ok", None
        narrative = (
            f"{NON_VALUATION_SCOPE}六维 {six_total:.0f} 分、后验 {posterior:.2f} / "
            f"Edge {edge:+.2f} 仅作观察条件引用；行动保持「观察/条件跟踪」，"
            "不以高分放行建仓或加仓，后续验证条件见各维度与作战计划。"
        )
    else:
        status = "degraded"
        if not inputs_valid:
            degraded_reason = "六维评分或贝叶斯证据链结构无效，无法合成结论"
        else:
            degraded_reason = with_valuation_cascade("估值依据不足，无法形成估值驱动的行动结论")
        narrative = (
            f"六维 {six_total:.0f} 分 → 后验 {posterior:.2f} / Edge {edge:+.2f} → "
            "行动保持「观察」。"
        )
    return ConclusionDim(
        status=status,
        degraded_reason=degraded_reason,
        conclusion=conclusion,
        rationale="",
        narrative=narrative,
    )
