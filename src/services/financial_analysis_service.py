# -*- coding: utf-8 -*-
"""个股财务分析模块：盈利/成长/安全/估值四维度专项报告。

数据源：基本面快照（主链+fuyao兜底）+ fuyao 多年年报序列（24h 快照控频）。
分档打分复用 fundamental_dim 的分档逻辑；缺数据记缺口不硬算。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_REPORT_DIR = Path(__file__).parent.parent.parent / "reports" / "financial_analysis"
_ID_PATTERN = "fa_{ts:%Y%m%d%H%M}"

# 健康分维度权重（审计 A3）
_DIM_WEIGHTS = {"profitability": 0.30, "growth": 0.30, "safety": 0.20, "valuation": 0.20}


def get_report_dir() -> Path:
    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    return _REPORT_DIR


def _resolve_unique_id(base: str) -> str:
    from src.storage import get_db

    rid = base
    seq = 1
    while get_db().get_financial_analysis_report(rid) is not None:
        rid = f"{base}_{seq}"
        seq += 1
    return rid


def _band(value: Optional[float], bands: list) -> tuple[Optional[float], str]:
    from src.deep_research_dims.fundamental_dim import _band

    return _band(value, bands)


def _score_dims(series: Dict[str, Any], fund: Dict[str, Any]) -> Dict[str, Any]:
    """四维度分档打分（多年序列优先，快照补充）。"""
    years = series.get("years") or []
    latest = years[0] if years else {}
    prev = years[1] if len(years) > 1 else {}
    gaps: list[str] = []

    roe_v = latest.get("roe")
    roe_score, roe_label = _band(
        roe_v, [(20, 85.0, "优秀"), (15, 75.0, "良好"), (10, 60.0, "一般"), (0, 45.0, "偏弱"), (-100, 30.0, "亏损侵蚀")]
    )
    gm_v = latest.get("gross_margin")
    gm_score, gm_label = _band(
        gm_v, [(50, 82.0, "高毛利"), (30, 65.0, "中高"), (15, 50.0, "中等"), (0, 40.0, "低毛利")]
    )

    rev_yoy = None
    np_yoy = None
    if latest.get("revenue") and prev.get("revenue"):
        rev_yoy = round((latest["revenue"] - prev["revenue"]) / prev["revenue"] * 100, 2)
    if latest.get("net_profit") is not None and prev.get("net_profit"):
        np_yoy = round((latest["net_profit"] - prev["net_profit"]) / abs(prev["net_profit"]) * 100, 2)
    scissors = round(np_yoy - rev_yoy, 2) if rev_yoy is not None and np_yoy is not None else None
    growth_score = None
    if rev_yoy is not None and np_yoy is not None:
        base = 60.0
        base += 10 if rev_yoy >= 10 else 5 if rev_yoy >= 0 else -10
        base += 10 if np_yoy >= 15 else 5 if np_yoy >= 0 else -15
        if scissors is not None and scissors < -10:
            base -= 10  # 增收不增利
        growth_score = max(0.0, min(100.0, base))
    if rev_yoy is None:
        gaps.append("营收增速（需至少两期年报）")
    if np_yoy is None:
        gaps.append("净利增速")

    debt_v = latest.get("debt_ratio")
    debt_score, debt_label = _band(
        debt_v, [(0, 80.0, "低杠杆"), (30, 70.0, "适中"), (60, 50.0, "偏高"), (100, 30.0, "高杠杆")]
    ) if debt_v is not None else (None, "数据缺失")
    ocf = latest.get("op_cash_flow")
    np_ = latest.get("net_profit")
    cash_quality = None
    if ocf is not None and np_:
        cash_quality = round(ocf / np_, 2)
    if debt_v is None:
        gaps.append("资产负债率")
    if ocf is None:
        gaps.append("经营现金流")

    from src.scoring.indicators_v2 import score_valuation

    snap = fund or {}
    pe_ttm = (series.get("valuation") or {}).get("pe_ttm")
    pb = (series.get("valuation") or {}).get("pb_mrq") or snap.get("pb")
    valuation = score_valuation(pe_ttm, pb) or {"score": None, "summary": "估值数据缺失"}

    dims = {
        "profitability": {"score": round((roe_score + gm_score) / 2, 1) if roe_score and gm_score else (roe_score or gm_score), "label": f"ROE {roe_label} / 毛利率 {gm_label}"},
        "growth": {"score": growth_score, "label": f"剪刀差 {scissors}%" if scissors is not None else "增速数据不足"},
        "safety": {"score": debt_score, "label": f"资产负债率 {debt_label}" + (f"，经营现金流/净利润 {cash_quality}" if cash_quality is not None else "")},
        "valuation": {"score": valuation["score"], "label": valuation["summary"]},
    }
    available = [(d["score"], w) for d, w in zip(dims.values(), _DIM_WEIGHTS.values()) if d["score"] is not None]
    health = round(sum(s * w for s, w in available) / sum(w for _, w in available), 1) if available else None
    return {
        "dims": dims, "health_score": health, "gaps": gaps,
        "years": years, "rev_yoy": rev_yoy, "np_yoy": np_yoy, "scissors": scissors,
        "valuation": {"pe_ttm": pe_ttm, "pb": pb, "summary": valuation["summary"], "score": valuation["score"]},
    }


def generate_report(raw_code: str, raw_name: Optional[str] = None) -> Dict[str, Any]:
    """生成个股财务分析专项报告。"""
    from src.services.stock_code_utils import normalize_code

    raw = str(raw_code or "").strip()
    code = normalize_code(raw) if raw else ""
    if not code or not code.isdigit() or len(code) != 6:
        raise ValueError(f"仅支持 A 股 6 位代码：{raw_code}")
    name = (raw_name or "").strip()
    if name and code in name:
        name = name.replace(code, "").replace(".SZ", "").replace(".SH", "").strip()
    if not name:
        name = code
        try:
            from src.agent.tools.data_tools import _get_fetcher_manager

            quote = _get_fetcher_manager().get_realtime_quote(code)
            name = str(getattr(quote, "name", "") or "").strip() or code
        except Exception:  # noqa: BLE001
            pass

    from src.deep_research_dims.context import build_shared_context, fetch_fuyao_financial_series

    ctx = build_shared_context(code, name)
    series = fetch_fuyao_financial_series(code)
    analysis = _score_dims(series, ctx.fundamental)

    as_of = datetime.now().isoformat(timespec="seconds")
    rid = _resolve_unique_id(_ID_PATTERN.format(ts=datetime.now()))
    md = _render(name, code, as_of, rid, analysis)
    md_path = get_report_dir() / f"{rid}.md"
    md_path.write_text(md, encoding="utf-8")
    ok = False
    try:
        from src.storage import get_db

        ok = get_db().save_financial_analysis_report(
            {
                "id": rid, "stock_code": code, "stock_name": name,
                "md_path": str(md_path), "health_score": analysis["health_score"],
                "analysis_json": json.dumps(analysis, ensure_ascii=False, default=str),
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[FinAnalysis] 落库失败: %s", exc)
    return {"report_id": rid if ok else None, "stock_code": code, "stock_name": name,
            "status": "success" if ok else "failed", "markdown": md, "analysis": analysis}


def _render(name: str, code: str, as_of: str, rid: str, a: Dict[str, Any]) -> str:
    from jinja2 import Environment, FileSystemLoader

    env = Environment(
        loader=FileSystemLoader(Path(__file__).parent.parent.parent / "templates"),
        autoescape=False, trim_blocks=True, lstrip_blocks=True,
    )
    md = env.get_template("financial_analysis_report.j2").render(
        stock_name=name, stock_code=code, as_of=as_of, report_id=rid, a=a,
    )
    import re as _re

    md = _re.sub(r"(?<=[\u4e00-\u9fff])(?=[A-Za-z0-9])", " ", md)
    md = _re.sub(r"(?<=[A-Za-z0-9%])(?=[\u4e00-\u9fff])", " ", md)
    return _re.sub(r"🔴 \*\*([^*\n]+)\*\*", r'<font color="#e03131">🔴 **\1**</font>', md)
