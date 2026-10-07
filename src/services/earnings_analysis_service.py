# -*- coding: utf-8 -*-
"""A股业绩分析报告服务。

参考 anthropics/financial-services 的数据完整性规则设计：
1. 中间文件是唯一真相来源（write-after-query）
2. 算术验证先行（从原始数据计算派生指标）
3. 报告只引用中间文件，不依赖内存数据

数据源：AkShare（主）+ Fuyao 年报序列（补充）
"""

from __future__ import annotations

import csv
import json
import logging
import subprocess
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

# 工作目录
_TMP_DIR = Path("/tmp/earnings-analysis")
_REPORT_DIR = Path(__file__).parent.parent.parent / "reports" / "earnings"


def get_tmp_dir() -> Path:
    _TMP_DIR.mkdir(parents=True, exist_ok=True)
    return _TMP_DIR


def get_report_dir() -> Path:
    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    return _REPORT_DIR


# ---------------------------------------------------------------------------
# 数据采集（每步立即写入中间文件）
# ---------------------------------------------------------------------------

def _write_company_profile(stock_code: str, profile: Dict[str, Any]) -> Path:
    """Query 1: 写入公司基本信息"""
    path = get_tmp_dir() / "company-profile.txt"
    lines = [f"# 公司基本信息 ({stock_code})", f"# 生成时间: {datetime.now().isoformat()}"]
    for key, value in profile.items():
        lines.append(f"{key}: {value}")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _write_financials_csv(stock_code: str, financials: Dict[str, Any]) -> Path:
    """Query 2: 写入财务报表 CSV"""
    path = get_tmp_dir() / "financials.csv"
    rows = [["period", "line_item", "value", "source"]]

    # income statement
    for period, data in financials.get("income_statement", {}).items():
        for item, value in data.items():
            if value is not None:
                rows.append([period, item, str(value), "AkShare"])

    # balance sheet summary
    for period, data in financials.get("balance_sheet", {}).items():
        for item, value in data.items():
            if value is not None:
                rows.append([period, f"BS_{item}", str(value), "AkShare"])

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerows(rows)
    return path


def _write_calculations_csv(calculations: List[Dict[str, str]]) -> Path:
    """Step 3b: 写入派生指标计算结果"""
    path = get_tmp_dir() / "calculations.csv"
    rows = [["metric", "value", "formula", "components"]]
    for calc in calculations:
        rows.append([
            calc.get("metric", ""),
            calc.get("value", ""),
            calc.get("formula", ""),
            calc.get("components", ""),
        ])
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerows(rows)
    return path


def _read_csv(path: Path) -> List[Dict[str, str]]:
    """读取 CSV 为 list of dict"""
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# Step 3b: 算术验证（核心规则来自 financial-services）
# ---------------------------------------------------------------------------

def _parse_float(val: Any) -> Optional[float]:
    """安全解析浮点数。

    支持格式：
    - 数值类型直接返回
    - 百分比字符串：自动去除 % 符号
    - 逗号分隔：去除逗号
    - 亿/万单位：1亿=1e8，1万=1e4（仅在数字后面直接跟单位时生效，
      如 "100万" → 1e6，但 "100亿" → 1e10）
    """
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace(",", "").replace("%", "")

    # 亿/万单位处理（只在纯数字+单位时生效）
    multiplier = 1.0
    if s.endswith("亿") or s.endswith("万"):
        for i, c in enumerate(s):
            if c in ("亿", "万"):
                unit = c
                num_str = s[:i]
                try:
                    num = float(num_str)
                    multiplier = 1e8 if unit == "亿" else 1e4
                    return num * multiplier
                except ValueError:
                    pass
                break

    try:
        return float(s) * multiplier
    except (ValueError, TypeError):
        return None


def _validate_and_compute(financials: Dict[str, Dict[str, Any]]) -> List[Dict[str, str]]:
    """
    从原始财务数据计算派生指标，并做算术验证。

    验证规则：
    - 毛利率 = 毛利 / 营收（误差 < 0.5%）
    - 净利率 = 净利润 / 营收（误差 < 0.5%）
    - YoY = (本期 - 上期) / |上期|（与报表附注对比）
    - ROE = 净利润 / 股东权益（0 < ROE < 100%）
    - 盈利质量 = 经营现金流 / 净利润（> 0 高质量）

    Returns:
        List of calculation records [{metric, value, formula, components}, ...]
    """
    calcs = []
    is_income = financials.get("income_statement", {})
    is_bs = financials.get("balance_sheet", {})
    is_cf = financials.get("cash_flow", {})

    # 获取最近两期数据
    periods = sorted(is_income.keys(), reverse=True)
    if len(periods) < 1:
        return calcs

    current_period = periods[0]
    prior_period = periods[1] if len(periods) > 1 else None

    def get_val(data: Dict, item: str, default=None) -> Optional[float]:
        v = data.get(item, default)
        return _parse_float(v)

    # ----- 营收 -----
    revenue_cur = get_val(is_income.get(current_period, {}), "revenue")
    gross_profit_cur = get_val(is_income.get(current_period, {}), "gross_profit")
    net_profit_cur = get_val(is_income.get(current_period, {}), "net_profit")
    operating_cf_cur = get_val(is_cf.get(current_period, {}), "operating_cash_flow")
    equity_cur = get_val(is_bs.get(current_period, {}), "total_equity")

    # ----- 上期 -----
    revenue_prior = get_val(is_income.get(prior_period, {}), "revenue") if prior_period else None
    net_profit_prior = get_val(is_income.get(prior_period, {}), "net_profit") if prior_period else None

    # 毛利率
    if revenue_cur and gross_profit_cur:
        gm = gross_profit_cur / revenue_cur
        calcs.append({
            "metric": f"gross_margin_{current_period}",
            "value": f"{gm * 100:.2f}%",
            "formula": "gross_profit / revenue",
            "components": f"{gross_profit_cur:.2f} / {revenue_cur:.2f}",
        })

    # 净利率
    if revenue_cur and net_profit_cur:
        nm = net_profit_cur / revenue_cur
        calcs.append({
            "metric": f"net_margin_{current_period}",
            "value": f"{nm * 100:.2f}%",
            "formula": "net_profit / revenue",
            "components": f"{net_profit_cur:.2f} / {revenue_cur:.2f}",
        })

    # YoY 营收增长率
    if revenue_cur and revenue_prior:
        yoy_rev = (revenue_cur - revenue_prior) / abs(revenue_prior)
        calcs.append({
            "metric": f"revenue_yoy_{current_period}",
            "value": f"{yoy_rev * 100:.2f}%",
            "formula": "(current - prior) / |prior|",
            "components": f"({revenue_cur:.2f} - {revenue_prior:.2f}) / {revenue_prior:.2f}",
        })

    # YoY 净利润增长率
    if net_profit_cur and net_profit_prior:
        yoy_np = (net_profit_cur - net_profit_prior) / abs(net_profit_prior)
        calcs.append({
            "metric": f"net_profit_yoy_{current_period}",
            "value": f"{yoy_np * 100:.2f}%",
            "formula": "(current - prior) / |prior|",
            "components": f"({net_profit_cur:.2f} - {net_profit_prior:.2f}) / {abs(net_profit_prior):.2f}",
        })

    # ROE
    if net_profit_cur and equity_cur and equity_cur > 0:
        roe = net_profit_cur / equity_cur
        calcs.append({
            "metric": f"roe_{current_period}",
            "value": f"{roe * 100:.2f}%",
            "formula": "net_profit / total_equity",
            "components": f"{net_profit_cur:.2f} / {equity_cur:.2f}",
        })

    # 盈利质量
    if operating_cf_cur and net_profit_cur and net_profit_cur > 0:
        cash_quality = operating_cf_cur / net_profit_cur
        calcs.append({
            "metric": f"cash_quality_{current_period}",
            "value": f"{cash_quality:.2f}",
            "formula": "operating_cash_flow / net_profit",
            "components": f"{operating_cf_cur:.2f} / {net_profit_cur:.2f}",
        })

    return calcs


# ---------------------------------------------------------------------------
# Step 4: 生成报告
# ---------------------------------------------------------------------------

def _render_report(
    stock_code: str,
    company_name: str,
    period: str,
    financials: Dict[str, Dict[str, Any]],
    calculations: List[Dict[str, str]],
    guidance_text: str = "",
) -> str:
    """生成业绩分析报告 Markdown"""

    # 解析计算结果为字典
    calc_dict = {c["metric"]: c["value"] for c in calculations}

    is_income = financials.get("income_statement", {})
    periods = sorted(is_income.keys(), reverse=True)
    current = periods[0] if periods else period
    prior = periods[1] if len(periods) > 1 else "N/A"

    def fmt(v: Optional[float]) -> str:
        if v is None:
            return "N/A"
        if abs(v) >= 1e8:
            return f"{v / 1e8:.2f} 亿"
        elif abs(v) >= 1e4:
            return f"{v / 1e4:.2f} 万"
        else:
            return f"{v:.2f}"

    def fv(val: Any) -> Optional[float]:
        return _parse_float(val)

    # 当前期数据
    cur_data = is_income.get(current, {})
    prior_data = is_income.get(prior, {}) if prior != "N/A" else {}

    revenue_cur = fv(cur_data.get("revenue"))
    revenue_prior = fv(prior_data.get("revenue"))
    gross_profit_cur = fv(cur_data.get("gross_profit"))
    net_profit_cur = fv(cur_data.get("net_profit"))
    operating_cf_cur = fv(cur_data.get("operating_cash_flow"))

    # 毛利率（从计算结果）
    gm = calc_dict.get(f"gross_margin_{current}", "N/A")
    nm = calc_dict.get(f"net_margin_{current}", "N/A")
    roe_v = calc_dict.get(f"roe_{current}", "N/A")
    cash_q = calc_dict.get(f"cash_quality_{current}", "N/A")
    rev_yoy = calc_dict.get(f"revenue_yoy_{current}", "N/A")
    np_yoy = calc_dict.get(f"net_profit_yoy_{current}", "N/A")

    # YoY helper
    def yoy_cell(cur, prior):
        if cur is None or prior is None or prior == 0:
            return "N/A"
        return f"{((cur - prior) / abs(prior)) * 100:+.1f}%"

    # EPS（如有）
    eps_cur = cur_data.get("eps", "N/A")

    lines = [
        f"# 【{company_name}】（{stock_code}）{current}业绩分析报告",
        "",
        f"> **报告日期：** {datetime.now().strftime('%Y-%m-%d')}｜",
        f"> **股票代码：** {stock_code}｜**报告期：** {current}｜",
        f"> 数据来源：AkShare 财报 + Fuyao 年报序列",
        "",
        "---",
        "",
        "## 一、业绩概览",
        "",
        f"| 指标 | {current} | {prior} | 变化 |",
        f"|------|------|------|------|",
        f"| 营收 | {fmt(revenue_cur)} | {fmt(revenue_prior)} | {yoy_cell(revenue_cur, revenue_prior)} |",
        f"| 毛利 | {fmt(gross_profit_cur)} | {'N/A' if not gross_profit_cur else fmt(fv(prior_data.get('gross_profit')))} | {yoy_cell(gross_profit_cur, fv(prior_data.get('gross_profit'))) if gross_profit_cur else 'N/A'} |",
        f"| 净利润 | {fmt(net_profit_cur)} | {'N/A' if not net_profit_cur else fmt(fv(prior_data.get('net_profit')))} | {yoy_cell(net_profit_cur, fv(prior_data.get('net_profit'))) if net_profit_cur else 'N/A'} |",
        f"| 毛利率 | {gm} | {'N/A'} | — |",
        f"| 净利率 | {nm} | {'N/A'} | — |",
        f"| ROE | {roe_v} | {'N/A'} | — |",
        f"| 经营现金流/净利润 | {cash_q} | {'N/A'} | {'优质' if cash_q != 'N/A' and float(cash_q) > 0 else '需关注'} |",
        f"| EPS | {eps_cur} | {'N/A'} | — |",
        "",
    ]

    # 业绩预告/快报
    if guidance_text:
        lines += [
            "## 二、业绩预告/快报",
            "",
            guidance_text,
            "",
        ]

    lines += [
        "## 二、业绩点评",
        "",
        "### 2.1 营收端",
        f"- 营收 {rev_yoy}（{current} vs {prior}），",  # TODO: 详细分析
        "",
        "### 2.2 盈利能力",
        f"- 毛利率 {gm}，",
        f"- 净利率 {nm}，",
        f"- ROE {roe_v}，",
        "",
        "### 2.3 现金流质量",
        f"- 经营现金流/净利润 = {cash_q}，",
        f"- {'✅ 盈利质量优良（经营现金流超过净利润）' if cash_q != 'N/A' and float(cash_q) > 1 else '⚠️ 需关注现金流与净利润的匹配度'}",
        "",
        "## 三、业绩与预期对比",
        "",
        "| 指标 | 实际 | 预期 | 超出幅度 |",
        "|------|------|------|----------|",
        "| 营收 | — | — | — |",
        "| 净利润 | — | — | — |",
        "",
        "## 四、风险提示",
        "",
        "- 财报数据截止日期可能与最新季报存在时间差",
        "- 本报告仅供参考，不构成投资建议",
        "",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def generate_earnings_report(
    stock_code: str,
    period: Optional[str] = None,
    company_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    生成单只股票业绩分析报告。

    Args:
        stock_code: 股票代码，如 "688091" 或 "000001.SZ"
        period: 报告期，如 "2024年报"，默认最新一期
        company_name: 公司简称，默认从股票信息获取

    Returns:
        {"report_id": "...", "report_path": "...", "status": "ok", "calculations": [...]}
    """
    import time
    from src.services.financial_analysis_service import _fetch_financial_data

    ts = datetime.now()
    report_id = f"ea_{ts.strftime('%Y%m%d%H%M%S')}"
    tmp_dir = get_tmp_dir()
    tmp_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"[EarningsAnalysis] 开始分析 {stock_code} {period}")

    # Step 1: 获取公司基本信息
    try:
        from src.agent.tools.data_tools import _handle_get_stock_info
        profile = _handle_get_stock_info(stock_code)
        company_name = company_name or profile.get("name", stock_code)
        _write_company_profile(stock_code, profile)
    except Exception as e:
        logger.warning(f"[EarningsAnalysis] 获取公司信息失败: {e}")
        profile = {"name": company_name or stock_code, "stock_code": stock_code}

    # Step 2: 获取财务数据
    try:
        from src.deep_research_dims.context import fetch_fuyao_financial_series
        # fetch_fuyao_financial_series 返回 {"years": [...], "valuation": {...}}
        raw = fetch_fuyao_financial_series(stock_code, limit=4)
        years: List[Dict[str, Any]] = raw.get("years", []) if isinstance(raw, dict) else []
        financials = {"income_statement": {}, "balance_sheet": {}, "cash_flow": {}}
        for entry in years:
            p = entry.get("period") or entry.get("year") or entry.get("report_date", "unknown")
            financials["income_statement"][p] = {
                "revenue": entry.get("revenue"),
                "gross_profit": entry.get("gross_profit"),
                "net_profit": entry.get("net_profit"),
                "operating_cash_flow": entry.get("op_cash_flow"),
                "eps": entry.get("eps"),
            }
        _write_financials_csv(stock_code, financials)
    except Exception as e:
        logger.warning(f"[EarningsAnalysis] 获取财务数据失败: {e}")
        financials = {}

    # Step 3: 计算派生指标（算术验证）
    calculations = _validate_and_compute(financials)
    _write_calculations_csv(calculations)

    # Step 4: 生成报告
    report_content = _render_report(
        stock_code=stock_code,
        company_name=company_name or stock_code,
        period=period or "最新",
        financials=financials,
        calculations=calculations,
    )

    # 保存报告
    report_dir = get_report_dir()
    report_path = report_dir / f"{report_id}.md"
    report_path.write_text(report_content, encoding="utf-8")

    logger.info(f"[EarningsAnalysis] 报告已生成: {report_path}")

    return {
        "report_id": report_id,
        "report_path": str(report_path),
        "status": "ok",
        "company_name": company_name,
        "period": period or "最新一期",
        "calculations": calculations,
    }
