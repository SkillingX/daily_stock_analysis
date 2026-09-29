# -*- coding: utf-8 -*-
"""S6 历史对比：历次报告观点提取与漂移标记。纯规则，无 LLM。"""

from __future__ import annotations

import re

from src.deep_research_dims.context import SharedContext
from src.schemas.deep_research_dims import HistoryDim, HistoryRow


_RATING_RE = re.compile(r"评级[:：]?\s*(买入|增持|中性|减持)")
_TARGET_RE = re.compile(r"目标价[:：]?\s*([0-9]+(?:\.[0-9]+)?)")
_SENTENCE_RE = re.compile(r"一句话[^\n]*[:：]\s*([^\n]+)")


def _extract(path_md: str) -> tuple[str, str]:
    try:
        from pathlib import Path

        text = Path(path_md).read_text(encoding="utf-8")
    except OSError:
        return "", ""
    rating = _RATING_RE.search(text)
    target = _TARGET_RE.search(text)
    sentence = _SENTENCE_RE.search(text)
    rating_hint = rating.group(1) if rating else ""
    if target:
        rating_hint += f"·目标价{target.group(1)}"
    return rating_hint, sentence.group(1).strip() if sentence else ""


def _drift(rows: list[HistoryRow]) -> list[str]:
    flags: list[str] = []
    if len(rows) < 2:
        return flags
    newer, older = rows[0], rows[1]
    buy_side = ("买入", "增持")
    sell_side = ("减持", "中性")
    if any(k in newer.rating_hint for k in buy_side) and any(
        k in older.rating_hint for k in sell_side
    ):
        flags.append(f"评级上调：{older.rating_hint or '无'} → {newer.rating_hint or '无'}")
    if any(k in newer.rating_hint for k in sell_side) and any(
        k in older.rating_hint for k in buy_side
    ):
        flags.append(f"评级下调：{older.rating_hint or '无'} → {newer.rating_hint or '无'}")
    return flags


def build_history_dim(ctx: SharedContext) -> HistoryDim:
    """S6：提取最近 10 份历史报告的评级/结论，标注观点漂移。"""
    rows: list[HistoryRow] = []
    for record in ctx.history_reports[:10]:
        rating_hint, sentence = _extract(str(record.get("md_path") or ""))
        rows.append(
            HistoryRow(
                report_id=str(record.get("id") or ""),
                created_at=str(record.get("created_at") or "")[:16],
                rating_hint=rating_hint,
                one_sentence=sentence,
            )
        )
    return HistoryDim(
        rows=rows,
        drift_flags=_drift(rows),
        narrative=(
            f"共 {len(rows)} 份历史报告。" + ("；".join(_drift(rows)) if _drift(rows) else "观点无显著漂移。")
        ),
    )
