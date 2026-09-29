# -*- coding: utf-8 -*-
"""S2 数据透视：纯规则装配（MA/量比/支撑阻力/52周高点距离/筹码）。

数据源：SharedContext（阶段 0 快照）。无 LLM。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.deep_research_dims.context import SharedContext
from src.schemas.deep_research_dims import DataDim
from src.schemas.report_schema import (
    ChipStructure,
    DataPerspective,
    PricePosition,
    TrendStatus,
    VolumeAnalysis,
)


def _closes(history: List[Dict[str, Any]]) -> List[float]:
    return [float(r["close"]) for r in history if r.get("close") is not None]


def _ma(values: List[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    return round(sum(values[-n:]) / n, 4)


def _ma_alignment(closes: List[float]) -> str:
    ma5, ma10, ma20 = _ma(closes, 5), _ma(closes, 10), _ma(closes, 20)
    if ma5 is None or ma10 is None or ma20 is None:
        return "数据不足"
    if ma5 > ma10 > ma20:
        return "多头排列"
    if ma5 < ma10 < ma20:
        return "空头排列"
    return "纠缠"


def _volume_analysis(history: List[Dict[str, Any]]) -> VolumeAnalysis:
    vols = [float(r["volume"]) for r in history if r.get("volume") is not None]
    if len(vols) < 6:
        return VolumeAnalysis(volume_status="数据不足")
    base = sum(vols[-6:-1]) / 5
    ratio = round(vols[-1] / base, 2) if base else None
    status = "显著放量" if ratio and ratio >= 2 else "温和放量" if ratio and ratio >= 1.2 else "缩量" if ratio and ratio < 0.8 else "平量"
    return VolumeAnalysis(
        volume_ratio=ratio,
        volume_status=status,
        volume_meaning=f"量比 {ratio}（相对 5 日均量）" if ratio else "",
    )


def _support_resistance(history: List[Dict[str, Any]], window: int = 60) -> tuple[Optional[float], Optional[float]]:
    recent = history[-window:]
    lows = [float(r["low"]) for r in recent if r.get("low") is not None]
    highs = [float(r["high"]) for r in recent if r.get("high") is not None]
    return (round(min(lows), 2) if lows else None, round(max(highs), 2) if highs else None)


def _chip_structure(chip: Dict[str, Any]) -> ChipStructure:
    if not chip:
        return ChipStructure(chip_health="数据缺失")
    return ChipStructure(
        profit_ratio=chip.get("profit_ratio") or chip.get("winner_percent"),
        avg_cost=chip.get("avg_cost") or chip.get("average_cost"),
        concentration=chip.get("concentration") or chip.get("concentration_70"),
        chip_health="见数据" if chip else "数据缺失",
    )


def build_data_dim(ctx: SharedContext) -> DataDim:
    """S2：从快照装配数据透视。history 不足时维度降级。"""
    closes = _closes(ctx.history)
    if len(closes) < 20:
        return DataDim(
            status="degraded",
            degraded_reason="日线历史不足 20 条，无法计算均线与点位",
        )

    current = ctx.quote.get("price") or closes[-1]
    ma5, ma10, ma20, ma250 = _ma(closes, 5), _ma(closes, 10), _ma(closes, 20), _ma(closes, 250)
    bias = round((current - ma5) / ma5 * 100, 2) if ma5 else None
    support, resistance = _support_resistance(ctx.history)
    highs_52w = [
        float(r["high"]) for r in ctx.history[-250:] if r.get("high") is not None
    ]
    high_52w = max(highs_52w) if highs_52w else None
    dist_high = round((current - high_52w) / high_52w * 100, 2) if high_52w else None
    ma250_dev = round((current - ma250) / ma250 * 100, 2) if ma250 else None

    alignment = _ma_alignment(closes)
    trend_score = {"多头排列": 75, "纠缠": 50, "空头排列": 30}.get(alignment, 50)
    if bias is not None:
        trend_score += 10 if bias > 3 else -10 if bias < -5 else 0
    trend_score = max(0, min(100, trend_score))

    perspective = DataPerspective(
        trend_status=TrendStatus(
            ma_alignment=alignment,
            is_bullish=alignment == "多头排列",
            trend_score=trend_score,
        ),
        price_position=PricePosition(
            current_price=current,
            ma5=ma5,
            ma10=ma10,
            ma20=ma20,
            bias_ma5=bias,
            bias_status="偏高" if bias and bias > 5 else "偏低" if bias and bias < -5 else "正常",
            support_level=support,
            resistance_level=resistance,
        ),
        volume_analysis=_volume_analysis(ctx.history),
        chip_structure=_chip_structure(ctx.chip),
    )
    return DataDim(
        perspective=perspective,
        ma250_deviation_pct=ma250_dev,
        distance_from_52w_high_pct=dist_high,
        narrative=(
            f"均线{alignment}，现价 {current}，偏离 MA5 {bias}%，"
            f"距 52 周高点 {dist_high}%。" if dist_high is not None else f"均线{alignment}。"
        ),
    )
