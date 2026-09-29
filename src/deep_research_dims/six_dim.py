# -*- coding: utf-8 -*-
"""L1 六维评分：scoring 引擎规则分（缺数据→中性 50 + 警告），LLM 仅写维度叙述。

六维权重冻结为 DEFAULT_DIMENSION_WEIGHTS（产业链 25/基本面 25/资金 15/
技术 10/情绪 15/宏观 10）。规则分与 LLM 主观键值的背离由护栏规则 10 裁决。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.deep_research_dims.context import SharedContext
from src.scoring.engine import aggregate_framework
from src.scoring.indicators import (
    score_capital,
    score_fundamental,
    score_macro,
    score_sentiment,
    score_supply_chain,
    score_technical,
)
from src.scoring.weights import get_default_weights, get_dimension_order
from src.schemas.deep_research_dims import SixDimDim
from src.schemas.research_framework import DimensionScore, ResearchFramework


def _alignment_en(alignment: str) -> str:
    return {"多头排列": "bullish", "空头排列": "bearish"}.get(alignment, "neutral")


def build_six_dim(ctx: SharedContext, data_dim_payload: Optional[Dict[str, Any]] = None) -> SixDimDim:
    """L1：从快照装配六维输入，规则评分后聚合。"""
    data = (data_dim_payload or {}).get("perspective") or {}
    trend = data.get("trend_status") or {}
    volume = data.get("volume_analysis") or {}
    chip = data.get("chip_structure") or {}
    fund = ctx.fundamental

    tech = score_technical(
        ma_alignment=_alignment_en(str(trend.get("ma_alignment") or "")),
        price_vs_ma250=(data_dim_payload or {}).get("ma250_deviation_pct"),
        volume_trend="increasing"
        if "放量" in str(volume.get("volume_status") or "")
        else "stable",
        distance_from_high=(data_dim_payload or {}).get("distance_from_52w_high_pct"),
        evidence="S2 数据透视（规则）",
    )
    fundamental = score_fundamental(
        roe=fund.get("roe"),
        revenue_growth=fund.get("revenue_growth"),
        gross_margin=fund.get("gross_margin"),
        evidence="阶段0 基本面快照（规则）",
    )
    capital = score_capital(
        chip_concentration=chip.get("concentration"),
        evidence="筹码集中度（北向/融资数据缺失则中性，见 data_limitations）",
    )
    supply_chain = score_supply_chain(
        evidence="产业链维度 L4 并行产出，本表该项为中性占位",
    )
    sentiment = score_sentiment(
        evidence="情报维度 S3 并行产出，本表该项为中性占位",
    )
    macro = score_macro(
        evidence="宏观与地缘为定性维度，无自动数据源，中性占位",
    )

    results = [tech, fundamental, capital, supply_chain, sentiment, macro]
    weights = get_default_weights()
    ordered: List[Dict[str, Any]] = []
    by_name = {r["dimension"]: r for r in results}
    for name in get_dimension_order():
        entry = by_name.get(name)
        if entry is None:
            entry = {"dimension": name, "score": 50.0, "indicators": []}
        ordered.append(
            {
                "dimension": name,
                "weight": weights[name],
                "score": entry.get("score"),
                "indicators": entry.get("indicators") or [],
            }
        )

    framework_score = aggregate_framework(ordered, version="v1")
    dimensions = [
        DimensionScore(
            dimension=d.dimension,
            weight=d.weight,
            score=round(d.score, 2),
            indicators=[
                {
                    "name": i.name,
                    "score": i.score,
                    "weight": i.weight,
                    "basis": i.basis,
                    "confidence": i.confidence,
                    "summary": i.summary,
                }
                for i in d.indicators
            ],
        )
        for d in framework_score.dimensions
    ]
    framework = ResearchFramework(
        dimension_total=round(framework_score.dimension_total, 2),
        dimensions=dimensions,
        scoring_version=framework_score.version,
        warnings=list(framework_score.warnings),
    )
    return SixDimDim(
        framework=framework,
        scoring_version=framework_score.version,
        warnings=list(framework_score.warnings),
        narrative=(
            f"六维总分 {framework.dimension_total:.1f}/100（{framework.scoring_version}），"
            f"产业链与基本面合计权重 50%，技术面 10%（刻意降权）。"
        ),
    )
