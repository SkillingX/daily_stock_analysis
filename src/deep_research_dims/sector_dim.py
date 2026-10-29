# -*- coding: utf-8 -*-
"""F2 板块分析维度：政策倾向 / 行业基率 / 板块地位 / 景气（规则装配，不编分）。"""

from __future__ import annotations

from typing import Optional

from src.deep_research_dims.context import SharedContext
from src.deep_research_dims.industry_base_rate import lookup_base_rate
from src.schemas.deep_research_dims import SectorDim


def _score_policy_lean(lean: Optional[str]) -> Optional[float]:
    return {"supportive": 66.0, "neutral": 50.0, "restrictive": 34.0}.get(lean or "")


def build_sector_dim(
    ctx: SharedContext, position_text: Optional[str] = None
) -> SectorDim:
    """F2：行业提示 → 政策倾向（industry_dna）+ 基率（规则装配）。

    position_text：产业链定位文本（含行业关键词，如"白酒酿造商"），用于行业提示
    缺失时提高基率/政策倾向命中率。

    issue #64：行业资金榜退役——rankings 字段仅为历史报告解析保留，新报告不再
    消费、不再产生"板块资金排行未取得"缺口提示。
    """
    hint = str(ctx.fundamental.get("industry_hint") or "")
    gaps: list[str] = []
    if not hint:
        gaps.append("所属行业未取得")

    policy_lean = None
    try:
        from src.services.supply_chain.industry_dna_loader import (
            lookup_industry_policy_lean,
        )

        policy_lean = lookup_industry_policy_lean(hint)
    except Exception as exc:  # noqa: BLE001
        gaps.append(f"政策倾向查询失败: {exc}")

    lookup_text = f"{hint} {position_text or ''} {ctx.stock_name}"
    base_rate, basis = lookup_base_rate(lookup_text)
    if basis == "neutral_default" and position_text:
        # 名称匹配失败时用定位文本单独再试一次（如"贵州茅台"→"白酒酿造商"）
        base_rate, basis = lookup_base_rate(str(position_text))

    score_parts = [_score_policy_lean(policy_lean)]
    if base_rate is not None:
        score_parts.append(round(30.0 + base_rate * 60.0, 2))  # 基率 0-1 → 30-90
    available = [s for s in score_parts if s is not None]
    sector_score = round(sum(available) / len(available), 2) if available else None

    return SectorDim(
        status="ok" if hint else "degraded",
        degraded_reason=None if hint else "所属行业未取得，不能用公司名代替板块",
        sector_hint=hint,
        policy_lean=policy_lean,
        base_rate=base_rate,
        base_rate_basis=basis,
        rankings={},
        data_gaps=gaps,
        sector_score=sector_score,
        narrative=(
            f"板块「{hint or '未识别'}」：政策倾向 {policy_lean or '未知'}，行业基率 {base_rate}（{basis}）；"
            f"板块得分 {sector_score or '缺数据'}。"
        ),
    )
