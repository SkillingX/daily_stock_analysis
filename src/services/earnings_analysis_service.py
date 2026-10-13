# -*- coding: utf-8 -*-
"""A股业绩分析报告服务。

参考 anthropics/financial-services 的数据完整性规则设计：
1. 年度快照与计算 CSV 一并保存，便于逐项核对
2. 使用原始科目作算术验证，保留缺口
3. 报告与导出文件使用同一份已取得的数据

数据源由部署策略选择；来源、财期和报表口径随年度数据保留。
"""

from __future__ import annotations

import csv
from decimal import Decimal
import json
import logging
from data_provider.cross_source_validator import SourceReadError
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

def _write_company_profile(stock_code: str, profile: Dict[str, Any], output_dir: Path | None = None) -> Path:
    """Query 1: 写入公司基本信息"""
    path = (output_dir or get_tmp_dir()) / "company-profile.txt"
    lines = [f"# 公司基本信息 ({stock_code})", f"# 生成时间: {datetime.now().isoformat()}"]
    for key, value in profile.items():
        lines.append(f"{key}: {value}")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _write_financials_csv(stock_code: str, financials: Dict[str, Any], output_dir: Path | None = None) -> Path:
    """Export all three statements with their actual annual evidence."""
    path = (output_dir or get_tmp_dir()) / f"{stock_code}_financials.csv"
    rows: list[list[str]] = [["period", "statement", "line_item", "value", "source", "currency", "unit", "scope", "adjustment", "disclosure_date"]]
    for statement in ("income_statement", "balance_sheet", "cash_flow"):
        for period, data in financials.get(statement, {}).items():
            metadata = (financials.get("metadata") or {}).get(period) or {}
            for item, value in data.items():
                if value is not None:
                    rows.append([period, statement, item, str(value), str(metadata.get("source") or "unknown"), str(metadata.get("currency") or "unknown"), "percentage_point" if item == "weighted_roe" else "CNY/share" if item == "eps" else "currency_base", str(metadata.get("statement_scope") or "unknown"), str(metadata.get("adjustment") or "unknown"), str(metadata.get("disclosure_date") or "unknown")])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as output:
        csv.writer(output).writerows(rows)
    return path


def _write_calculations_csv(calculations: List[Dict[str, str]], output_dir: Path | None = None) -> Path:
    """Step 3b: 写入派生指标计算结果"""
    path = (output_dir or get_tmp_dir()) / "calculations.csv"
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
    """Compute monetary ratios in Decimal; ROE uses the reported weighted metric."""
    income = financials.get("income_statement", {})
    cash = financials.get("cash_flow", {})
    periods = sorted(income, reverse=True)
    if not periods:
        return []
    current = periods[0]
    prior = periods[1] if len(periods) > 1 else None
    calcs: list[dict[str, str]] = []
    def number(row: dict[str, Any], key: str) -> Decimal | None:
        value = _parse_float(row.get(key))
        return Decimal(str(value)) if value is not None and Decimal(str(value)).is_finite() else None
    revenue = number(income[current], "revenue")
    gross = number(income[current], "gross_profit")
    profit = number(income[current], "net_profit")
    ocf = number(cash.get(current, {}), "operating_cash_flow")
    weighted_roe = number(income[current], "weighted_roe")
    for metric, numerator in (("gross_margin", gross), ("net_margin", profit)):
        if revenue is not None and revenue != 0 and numerator is not None:
            calcs.append({"metric": f"{metric}_{current}", "value": f"{numerator/revenue*100:.2f}%", "formula": f"{'gross_profit' if metric == 'gross_margin' else 'net_profit'} / revenue", "components": f"{numerator:.2f} / {revenue:.2f}"})
    if prior is not None:
        metadata = financials.get("metadata", {})
        current_meta, prior_meta = metadata.get(current, {}), metadata.get(prior, {})
        compatible = all(current_meta.get(key) == prior_meta.get(key) for key in ("currency", "statement_scope", "adjustment"))
        # Only adjacent years form a year-on-year comparison.
        adjacent = current[:4].isdigit() and prior[:4].isdigit() and int(current[:4])-int(prior[:4]) == 1
        if compatible and adjacent:
            for metric, key, current_value in (("revenue_yoy", "revenue", revenue), ("gross_profit_yoy", "gross_profit", gross), ("net_profit_yoy", "net_profit", profit)):
                previous = number(income[prior], key)
                if current_value is not None and previous is not None and previous != 0:
                    calcs.append({"metric": f"{metric}_{current}", "value": f"{(current_value-previous)/abs(previous)*100:.2f}%", "formula": "(current - prior) / |prior|", "components": f"({current_value:.2f} - {previous:.2f}) / {abs(previous):.2f}"})
    if weighted_roe is not None:
        calcs.append({"metric": f"roe_{current}", "value": f"{weighted_roe:.2f}%", "formula": "weighted_roe (reported)", "components": str(weighted_roe)})
    if ocf is not None and profit is not None and profit > 0:
        calcs.append({"metric": f"cash_quality_{current}", "value": f"{ocf/profit:.2f}", "formula": "operating_cash_flow / parent_net_profit", "components": f"{ocf:.2f} / {profit:.2f}"})
    return calcs


def _statements_from_series(series: dict[str, Any]) -> dict[str, dict[str, Any]]:
    from data_provider.choice_adapter import AnnualReport
    statements: dict[str, dict[str, Any]] = {"income_statement": {}, "balance_sheet": {}, "cash_flow": {}, "metadata": {}}
    for entry in series.get("years") or []:
        period = str(entry.get("report_date") or entry.get("period") or entry.get("year"))
        if entry.get("source") == "choice":
            entry = AnnualReport.from_record(entry).as_record()
        statements["income_statement"][period] = {"revenue": entry.get("revenue"), "operating_cost": entry.get("operating_cost"), "gross_profit": entry.get("gross_profit"), "net_profit": entry.get("net_profit"), "eps": entry.get("eps"), "weighted_roe": entry.get("roe") if entry.get("roe_method") == "weighted_roe" else None}
        statements["balance_sheet"][period] = {"total_equity": entry.get("equity_total"), "total_assets": entry.get("assets_total"), "total_liabilities": entry.get("total_debt")}
        statements["cash_flow"][period] = {"operating_cash_flow": entry.get("op_cash_flow")}
        statements["metadata"][period] = {key: entry.get(key) for key in ("source", "currency", "statement_scope", "adjustment", "disclosure_date")}
    return statements


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
    operating_cf_cur = fv((financials.get("cash_flow", {}).get(current) or {}).get("operating_cash_flow"))

    # 毛利率（从计算结果）
    gm = calc_dict.get(f"gross_margin_{current}", "N/A")
    nm = calc_dict.get(f"net_margin_{current}", "N/A")
    roe_v = calc_dict.get(f"roe_{current}", "N/A")
    cash_q = calc_dict.get(f"cash_quality_{current}", "N/A")
    rev_yoy = calc_dict.get(f"revenue_yoy_{current}", "N/A")
    np_yoy = calc_dict.get(f"net_profit_yoy_{current}", "N/A")

    # EPS（如有）
    eps_cur = cur_data.get("eps", "N/A")

    lines = [
        f"# 【{company_name}】（{stock_code}）{current}业绩分析报告",
        "",
        f"> **报告日期：** {datetime.now().strftime('%Y-%m-%d')}｜",
        f"> **股票代码：** {stock_code}｜**报告期：** {current}｜",
        f"> 数据来源：{' / '.join(sorted({'Choice' if row.get('source') == 'choice' else str(row.get('source') or 'unknown') for row in financials.get('metadata', {}).values()})) or 'unknown'}；报表口径详见 CSV",
        "",
        "---",
        "",
        "## 一、业绩概览",
        "",
        f"| 指标 | {current} | {prior} | 变化 |",
        f"|------|------|------|------|",
        f"| 营收 | {fmt(revenue_cur)} | {fmt(revenue_prior)} | {rev_yoy} |",
        f"| 毛利 | {fmt(gross_profit_cur)} | {fmt(fv(prior_data.get('gross_profit')))} | {calc_dict.get(f'gross_profit_yoy_{current}', 'N/A')} |",
        f"| 净利润 | {fmt(net_profit_cur)} | {fmt(fv(prior_data.get('net_profit')))} | {np_yoy} |",
        f"| 毛利率 | {gm} | {'N/A'} | — |",
        f"| 净利率 | {nm} | {'N/A'} | — |",
        f"| 加权 ROE | {roe_v} | {'N/A'} | — |",
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
    *,
    force_refresh: bool = False,
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
    from data_provider.base import normalize_stock_code
    from data_provider.mx_data_adapter import financial_refresh
    from uuid import uuid4
    stock_code = normalize_stock_code(stock_code)
    refresh_token = financial_refresh.set((stock_code, uuid4().hex)) if force_refresh else None
    try:
        from src.core.market_policy import require_stock_market
        require_stock_market(stock_code)

        ts = datetime.now()
        report_id = f"ea_{ts.strftime('%Y%m%d%H%M%S%f')}"
        tmp_dir = get_tmp_dir() / report_id
        tmp_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"[EarningsAnalysis] 开始分析 {stock_code} {period}")

        # Step 1: 获取公司基本信息
        try:
            from src.agent.tools.data_tools import _handle_get_stock_info
            from src.config import get_config
            if get_config().data_source_profile == "choice":
                from src.agent.tools.data_tools import _get_fetcher_manager
                context = _get_fetcher_manager().get_fundamental_context(stock_code)
                profile = {"name": context.get("stock_name") or stock_code, "stock_code": stock_code, "fundamental_context": context}
            else:
                profile = _handle_get_stock_info(stock_code)
            company_name = company_name or profile.get("name", stock_code)
            _write_company_profile(stock_code, profile, tmp_dir)
        except Exception as e:
            logger.warning(f"[EarningsAnalysis] 获取公司信息失败: {e}")
            profile = {"name": company_name or stock_code, "stock_code": stock_code}

        # Step 2: 获取财务数据
        try:
            from src.deep_research_dims.context import fetch_fuyao_financial_series
            # fetch_fuyao_financial_series 返回 {"years": [...], "valuation": {...}}
            raw = fetch_fuyao_financial_series(stock_code, limit=4)
            if period and period not in {"最新", "最新一期"}:
                import re
                match = re.fullmatch(r"(\d{4})(?:年报|-12-31)?", period)
                if match is None:
                    raise ValueError("Only disclosed annual reports are supported")
                requested_year = int(match[1])
                if not any(entry.get("year") == requested_year for entry in raw.get("years") or []):
                    raise ValueError("Requested annual report is not available in the retrieved years")
                raw = {**raw, "years": [entry for entry in raw["years"] if entry["year"] <= requested_year]}
            financials = _statements_from_series(raw)
            financials_path = _write_financials_csv(stock_code, financials, tmp_dir)
        except SourceReadError as e:
            logger.warning(f"[EarningsAnalysis] 获取财务数据失败: {e}")
            financials = {}
            raw = {"status": "partial"}
            financials_path = _write_financials_csv(stock_code, financials, tmp_dir)

        # Step 3: 计算派生指标（算术验证）
        calculations = _validate_and_compute(financials)
        calculations_path = _write_calculations_csv(calculations, tmp_dir)

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
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_content, encoding="utf-8")

        logger.info(f"[EarningsAnalysis] 报告已生成: {report_path}")

        return {
            "report_id": report_id,
            "report_path": str(report_path),
            "status": "ok" if financials and raw.get("status", "ok") == "ok" else "partial",
            "company_name": company_name,
            "period": period or "最新一期",
            "calculations": calculations,
            "financials_csv": str(financials_path),
            "calculations_csv": str(calculations_path),
        }
    finally:
        if refresh_token is not None:
            financial_refresh.reset(refresh_token)
