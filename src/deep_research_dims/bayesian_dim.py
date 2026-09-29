# -*- coding: utf-8 -*-
"""L2 贝叶斯：先验（六维映射）→ 证据 LR（标定表机器校验）→ 后验。纯规则，无 LLM。

要点（需求 §5.4）：
- market_implied_p 机器定义：行业基率暂回退板块中性 0.5 并标记 basis；
- LR 越出标定区间即拒绝该证据（记入 evidence_rejected，不进入后验）；
- 证据新鲜度由护栏规则 9 裁决（不在这里阻断）。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from icontract import ensure, require

from src.scoring.bayesian import calculate_bayesian
from src.schemas.bayesian_framework import BayesianFramework, EvidenceItem
from src.schemas.deep_research_dims import LR_RANGES, BayesianDim


@require(lambda strength: strength in LR_RANGES, "strength 必须是五档枚举之一")
@ensure(lambda result: result[0] > 0, "LR 必须为正")
def calibrate_lr(strength: str, raw_lr: Optional[float]) -> Tuple[float, bool]:
    """把 LLM/探索 Agent 给的证据强度映射为标定区间内的 LR。

    Returns:
        (lr, clamped)：raw_lr 在标定区间内则采用；越界或缺失则取区间中位并标记 clamped=True。
    """
    low, high = LR_RANGES[strength]
    if raw_lr is not None and low <= raw_lr <= high:
        return raw_lr, False
    return round((low + high) / 2, 4), True


def _evidence_fresh_enough(evidence_date: str, today: Optional[date] = None) -> bool:
    try:
        day = datetime.strptime(evidence_date[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False
    today = today or date.today()
    return (today - day).days <= 90


def build_bayesian_dim(
    six_dim_payload: Dict[str, Any],
    evidence_items: List[Dict[str, Any]],
) -> BayesianDim:
    """L2：先验 ← 六维总分；后验 ← 合格证据依次连乘 LR（cap 0.02–0.98）。"""
    framework = (six_dim_payload or {}).get("framework") or {}
    dimension_total = float(framework.get("dimension_total") or 50.0)

    accepted: List[EvidenceItem] = []
    rejected: List[str] = []
    lr_product = 1.0
    strong_negative = False
    for raw in evidence_items or []:
        strength = str(raw.get("strength") or "neutral")
        if strength not in LR_RANGES:
            rejected.append(f"未知证据强度 {strength}：{raw.get('evidence', '')[:40]}")
            continue
        lr, clamped = calibrate_lr(strength, raw.get("lr"))
        if not _evidence_fresh_enough(str(raw.get("date") or "")):
            rejected.append(f"证据过期(>90d)：{raw.get('evidence', '')[:40]}")
            continue
        if clamped:
            rejected.append(
                f"LR 越界已按标定中位修正：{raw.get('evidence', '')[:40]}（{strength}→{lr}）"
            )
        posterior_step = min(0.98, max(0.02, lr_product * lr / (1 + lr_product * lr)))
        lr_product *= lr
        accepted.append(
            EvidenceItem(
                evidence=str(raw.get("evidence") or ""),
                strength=strength,  # type: ignore[arg-type]
                lr=lr,
                posterior_p=round(posterior_step, 4),
                date=str(raw.get("date") or "")[:10],
            )
        )
        if strength == "strong_negative":
            strong_negative = True

    # 综合 LR = 后验 odds / 先验 odds，交给 calculate_bayesian 复算（保持单一计算源）
    result = calculate_bayesian(
        dimension_total=dimension_total,
        market_implied_p=0.5,  # 行业基率暂回退中性，见模块 docstring
        lr=lr_product,
        strong_negative_evidence=strong_negative,
    )
    bayesian = BayesianFramework(
        prior_p=round(result.prior_p, 4),
        market_implied_p=result.market_implied_p,
        edge=round(result.edge, 6),
        posterior_p=round(result.posterior_p, 4),
        position_suggestion=result.position_suggestion,
        confidence="高" if len(accepted) >= 3 else "中" if accepted else "观察",
        evidence_log=accepted,
        stop_conditions=result.stop_conditions,
    )
    return BayesianDim(
        bayesian=bayesian,
        evidence_rejected=rejected,
        market_implied_basis="industry_baseline_unavailable_neutral_0.5",
        narrative=(
            f"先验 {bayesian.prior_p:.2f}（六维 {dimension_total:.0f} 分映射）→ "
            f"后验 {bayesian.posterior_p:.2f}（{len(accepted)} 条证据，LR 乘积 {lr_product:.2f}）。"
        ),
    )
