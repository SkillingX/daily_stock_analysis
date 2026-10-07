# -*- coding: utf-8 -*-
"""个股财务分析模块：盈利/成长/安全/估值四维度专项报告。

数据源：Fuyao 年报序列（主）+ AkShare 财务指标兜底（gross_margin）+ 快照估值。
分档打分复用 fundamental_dim 的分档逻辑；缺数据记缺口不硬算。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from icontract import ensure, require

logger = logging.getLogger(__name__)

_REPORT_DIR = Path(__file__).parent.parent.parent / "reports" / "financial_analysis"
_ID_PATTERN = "fa_{ts:%Y%m%d%H%M%S}"   # Fix 12: 精确到秒，避免并发冲突

# 健康分维度权重
_DIM_WEIGHTS = {"profitability": 0.30, "growth": 0.30, "safety": 0.20, "valuation": 0.20}


def get_report_dir() -> Path:
    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    return _REPORT_DIR


# -------------------------------------------------------------------
# Fix 11: 先查 DB 再尝试锁文件，双重保险防并发重复生成
# -------------------------------------------------------------------
_DEDUP_LOCK: Dict[str, float] = {}   # code -> 请求到达时间戳


def _same_day_dedup_report(code: str) -> Optional[Dict[str, Any]]:
    """当天同代码已生成过报告则返回已有记录，避免重复生成。"""
    today = datetime.now().date().isoformat()[:10]
    try:
        from src.storage import get_db
        db = get_db()
        rows, _ = db.list_financial_analysis_reports(code, limit=10, offset=0)
        for r in rows:
            created = str(r.get("created_at") or "")
            if created.startswith(today):
                full = db.get_financial_analysis_report(r["id"])
                if full:
                    return full
    except Exception:  # noqa: BLE001
        pass
    return None


def _try_acquire_dedup_lock(code: str) -> bool:
    """尝试获取去重锁；返回 True 表示拿到锁（可继续生成），False 表示已有并发请求在生成中。"""
    import time
    now = time.time()
    last = _DEDUP_LOCK.get(code, 0)
    if now - last < 10:          # 10 秒内有同名请求在处理中
        return False
    _DEDUP_LOCK[code] = now
    return True


def _release_dedup_lock(code: str) -> None:
    _DEDUP_LOCK.pop(code, None)


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


# -------------------------------------------------------------------
# Fix 1: 从 AkShare 财务指标表兜底取毛利率（Fuyao 年报序列无此字段）
# -------------------------------------------------------------------
def _fetch_gross_margin_akshare(code: str, years: list[int]) -> dict[int, float]:
    """返回 {年份: 毛利率}，失败返回空 dict。"""
    try:
        import akshare as ak
        df = ak.stock_financial_analysis_indicator(symbol=code, start_year=str(years[-1]))
        if df is None or df.empty:
            return {}
        col_map = {c: c for c in df.columns}
        gm_col = next((c for c in df.columns if "销售毛利率" in c), None)
        if gm_col is None:
            return {}
        result: dict[int, float] = {}
        for _, row in df.iterrows():
            date_val = row.get("日期", "")
            if not date_val:
                continue
            yr = int(str(date_val)[:4])
            val = row.get(gm_col)
            if val is not None and not (isinstance(val, float) and str(val) == "nan"):
                result[yr] = float(val)
        return result
    except Exception:  # noqa: BLE001
        return {}


@require(
    lambda series: isinstance(series, dict),
    "series must be a dict",
)
@require(
    lambda fund: isinstance(fund, dict),
    "fund must be a dict",
)
@ensure(
    lambda result: (
        isinstance(result, dict)
        and {"dims", "health_score", "gaps", "years", "valuation"}.issubset(result.keys())
    ),
    "_score_dims must return the documented dict contract",
)
@ensure(
    lambda result: (
        result["health_score"] is None
        or 0.0 <= result["health_score"] <= 100.0
    ),
    "health_score must be None or in [0, 100]",
)
def _score_dims(series: Dict[str, Any], fund: Dict[str, Any]) -> Dict[str, Any]:
    """四维度分档打分（多年序列优先，AkShare 兜底毛利率）。"""
    years_data: list[Dict[str, Any]] = series.get("years") or []
    latest = years_data[0] if years_data else {}
    prev = years_data[1] if len(years_data) > 1 else {}
    gaps: list[str] = []

    # ---- Fix 1: gross_margin fallback ----
    gm_v = latest.get("gross_margin")
    if gm_v is None and years_data:
        # 用 AkShare 财务指标表兜底
        avail_years: list[int] = [int(y.get("year", 0)) for y in years_data if y.get("year")]
        code_for_ak = latest.get("code") or ""
        if avail_years and code_for_ak:
            ak_gm = _fetch_gross_margin_akshare(code_for_ak, avail_years)
            gm_for_year = ak_gm.get(latest.get("year"))  # type: ignore[arg-type]
            if gm_for_year is not None:
                gm_v = gm_for_year
                # 把兜底的毛利率回填到 years_data[0] 供模板渲染
                years_data[0]["gross_margin"] = gm_v

    # ---- 盈利质量 ----
    roe_v = latest.get("roe")
    roe_score, roe_label = _band(
        roe_v, [(20, 85.0, "优秀"), (15, 75.0, "良好"), (10, 60.0, "一般"), (0, 45.0, "偏弱"), (-100, 30.0, "亏损侵蚀")]
    )
    gm_score, gm_label = _band(
        gm_v, [(50, 82.0, "高毛利"), (30, 65.0, "中高"), (15, 50.0, "中等"), (0, 40.0, "低毛利")]
    ) if gm_v is not None else (None, "数据缺失")

    # ---- Fix 4: 盈利质量标签清晰 ----
    roe_disp = f"{roe_v:.2f}%" if roe_v is not None else "缺失"
    gm_disp = f"{gm_v:.1f}%" if gm_v is not None else "缺失"
    profitability_label = f"ROE {roe_label}({roe_disp}) / 毛利率 {gm_label}({gm_disp})"

    # ---- 成长质量 ----
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
        # Fix 7: 营收净利双降 → 降分
        if rev_yoy < 0 and np_yoy < 0:
            base -= 10   # 双降警示
        growth_score = max(0.0, min(100.0, base))

    if rev_yoy is None:
        gaps.append("营收增速（需至少两期年报）")
    if np_yoy is None:
        gaps.append("净利增速")

    # ---- Fix 2+3: 安全维度 ----
    debt_v = latest.get("debt_ratio")
    if debt_v is None:
        total_debt = latest.get("total_debt")
        total_assets = latest.get("assets_total")
        if total_debt is not None and total_assets:
            debt_v = round(total_debt / total_assets * 100, 2)
    debt_score, debt_label = _band(
        debt_v, [(0, 80.0, "低杠杆"), (30, 70.0, "适中"), (60, 50.0, "偏高"), (100, 30.0, "高杠杆")]
    ) if debt_v is not None else (None, "数据缺失")

    ocf = latest.get("act_cash_flow_net") or latest.get("op_cash_flow")
    if ocf is None:
        for k in ("operating_cash_flow", "cash_flow_from_operations", "net_op_cash_flow"):
            ocf = latest.get(k)
            if ocf is not None:
                break
    np_ = latest.get("net_profit")
    # Fix 3: cash_quality 保留 2 位小数
    cash_quality = round(ocf / np_, 2) if ocf is not None and np_ and np_ != 0 else None

    # Fix 2: safety.label 只描述债务水位，ocf 单独出
    safety_label = f"资产负债率 {debt_label}"
    if debt_v is not None:
        safety_label += f"（{debt_v:.1f}%）"

    if debt_v is None:
        gaps.append("资产负债率")
    if ocf is None:
        gaps.append("经营现金流")

    # ---- 估值 ----
    from src.scoring.indicators_v2 import score_valuation
    snap = fund or {}
    pe_ttm = (series.get("valuation") or {}).get("pe_ttm")
    pb = (series.get("valuation") or {}).get("pb_mrq") or snap.get("pb")
    valuation = score_valuation(pe_ttm, pb) or {"score": None, "summary": "估值数据缺失"}

    # Fix 5: PE/PB 保留 2 位小数
    pe_disp = f"{pe_ttm:.2f}" if pe_ttm is not None else None
    pb_disp = f"{pb:.2f}" if pb is not None else None

    # ---- 综合 ----
    dims = {
        "profitability": {
            "score": round((roe_score + (gm_score or 0)) / 2, 1) if roe_score is not None and gm_score is not None
                      else (roe_score or gm_score or None),
            "label": profitability_label,
        },
        "growth": {
            "score": growth_score,
            "label": (f"剪刀差 {scissors:+.2f}%" if scissors is not None
                      else "增速数据不足"),
        },
        "safety": {
            "score": debt_score,
            "label": safety_label,
        },
        "valuation": {
            "score": valuation.get("score"),
            "label": valuation.get("summary", "估值数据缺失"),
        },
    }

    available = [(d["score"], w) for d, w in zip(dims.values(), _DIM_WEIGHTS.values()) if d["score"] is not None]
    health = round(sum(s * w for s, w in available) / sum(w for _, w in available), 1) if available else None

    # Fix: 预格式化增长率，避免在模板里用 |format() 与 % 格式化冲突
    rev_yoy_fmt = f"{rev_yoy:+.2f}" if rev_yoy is not None else None
    np_yoy_fmt = f"{np_yoy:+.2f}" if np_yoy is not None else None
    scissors_fmt = f"{scissors:+.2f}" if scissors is not None else None

    return {
        "dims": dims,
        "health_score": health,
        "gaps": gaps,
        "years": years_data,
        "rev_yoy": rev_yoy,
        "rev_yoy_fmt": rev_yoy_fmt,
        "np_yoy": np_yoy,
        "np_yoy_fmt": np_yoy_fmt,
        "scissors": scissors,
        "scissors_fmt": scissors_fmt,
        "cash_quality": cash_quality,
        "ocf": ocf,
        "np": np_,
        "valuation": {
            "pe_ttm": pe_ttm,
            "pe_disp": pe_disp,
            "pb": pb,
            "pb_disp": pb_disp,
            "summary": valuation.get("summary", "估值数据缺失"),
            "score": valuation.get("score"),
        },
    }


def _raw_code_valid(raw_code: Any) -> bool:
    """icontract 安全的 precondition：先判 isinstance 再 len()，避免 int 上 len 抛错。"""
    return isinstance(raw_code, str) and len(raw_code) > 0


@require(
    lambda raw_code: _raw_code_valid(raw_code),
    "raw_code must be a non-empty string",
)
@ensure(
    lambda result: (
        isinstance(result, dict)
        and {"stock_code", "stock_name", "status", "markdown", "analysis"}.issubset(result.keys())
    ),
    "generate_report must return the documented dict contract",
)
@ensure(
    lambda result: result["status"] in ("success", "failed", "already_exists"),
    "status must be one of success/failed/already_exists",
)
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

    # Fix 11: 并发锁
    if not _try_acquire_dedup_lock(code):
        existing = _same_day_dedup_report(code)
        if existing is not None:
            try:
                md = Path(existing["md_path"]).read_text(encoding="utf-8")
            except OSError:
                md = ""
            return {
                "report_id": existing["id"],
                "stock_code": code,
                "stock_name": existing.get("stock_name") or code,
                "status": "already_exists",
                "markdown": md,
                "analysis": json.loads(existing.get("analysis_json") or "{}"),
            }

    try:
        # 当天已生成过则直接返回已有报告
        existing = _same_day_dedup_report(code)
        if existing is not None:
            _release_dedup_lock(code)
            try:
                md = Path(existing["md_path"]).read_text(encoding="utf-8")
            except OSError:
                md = ""
            return {
                "report_id": existing["id"],
                "stock_code": code,
                "stock_name": existing.get("stock_name") or code,
                "status": "already_exists",
                "markdown": md,
                "analysis": json.loads(existing.get("analysis_json") or "{}"),
            }

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
        # 把 code 注入 series years，方便 AkShare fallback 查毛利率
        for y in series.get("years") or []:
            y["code"] = code

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
        return {
            "report_id": rid if ok else None,
            "stock_code": code, "stock_name": name,
            "status": "success" if ok else "failed",
            "markdown": md, "analysis": analysis,
        }
    finally:
        _release_dedup_lock(code)


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
    return _re.sub(r"🔴 \*\*([^*:\n]+)\*\*", r'<font color="#e03131">🔴 **\1**</font>', md)
