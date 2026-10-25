# -*- coding: utf-8 -*-
"""iFinD MCP adapter: bounded calls and evidence-preserving table mapping.
Choice uses one financial batch; legacy single-anchor/series interfaces remain available.
"""

from __future__ import annotations

import asyncio
from time import monotonic
import json
import logging
import os
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal, DecimalException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from typing import Annotated, Any, Dict, List, Literal, Optional, Tuple

from .cross_source_validator import SourceReadError, source_failure_reason, AnchorReading, caliber_from_label, normalize_anchor_value, observation_time_from_fields, reading_input_reasons, report_period_from_fields, select_report_period, normalize_report_period

logger = logging.getLogger(__name__)

# iFinD 锚点 → (tool, query 模板, 表头关键词列表)
# Phase 0 实测：自然语言 query + Markdown 表格返回，单位换算由 _safe_float 处理
_IFIND_ANCHOR_QUERIES: Dict[str, Tuple[str, str, List[str]]] = {
    "current_price": ("get_stock_info", "{code} 收盘价", ["收盘价"]),
    "pe_ratio": (
        "get_stock_info",
        "{code} 市盈率PE TTM",
        ["市盈率(PE,TTM)", "市盈率PE(TTM)"],
    ),
    "pb_ratio": (
        "get_stock_info",
        "{code} 市净率PB",
        ["市净率(PB,最新)", "市净率PB(最新)"],
    ),
    "total_mv": ("get_stock_info", "{code} 总市值", ["总市值（单位：元）", "总市值"]),
    "circ_mv": (
        "get_stock_info",
        "{code} 流通市值",
        ["流通市值（单位：元）", "流通市值"],
    ),
    "revenue": (
        "get_stock_financials",
        "{code} {period} 营业收入",
        ["营业收入（单位：元）", "营业收入"],
    ),
    "net_profit": (
        "get_stock_financials",
        "{code} {period} 归属于母公司所有者的净利润",
        ["归属于母公司所有者的净利润（单位：元）", "归属于母公司所有者的净利润"],
    ),
    "roe": (
        "get_stock_financials",
        "{code} {period} 净资产收益率ROE",
        [
            "净资产收益率ROE（单位：%）",
            "净资产收益率ROE(加权,公布值)（单位：%）",
            "净资产收益率ROE(加权,公布值)",
            "净资产收益率ROE",
        ],
    ),
    # 毛利率 / 营收同比：与 MX 互补，给 growth 块提供第二源（best-effort，受 period 影响）
    "gross_margin": (
        "get_stock_financials",
        "{code} {period} 销售毛利率",
        ["销售毛利率（单位：%）", "销售毛利率", "毛利率"],
    ),
    "revenue_yoy": (
        "get_stock_financials",
        "{code} {period} 营业收入同比增长率",
        [
            "营业收入(同比增长率)",
            "营业收入同比增长率（单位：%）",
            "营业收入同比增长率",
            "营业收入同比增长",
            "营收同比增长",
        ],
    ),
    "net_profit_yoy": (
        "get_stock_financials",
        "{code} {period} 净利润同比",
        [
            "归属母公司股东的净利润(同比增长率)（单位：%）",
            "归属母公司股东的净利润(同比增长率)",
            "净利润(同比增长率)（单位：%）",
            "净利润(同比增长率)",
        ],
    ),
    "margin_balance": (
        "get_stock_performance",
        "{code} 融资余额",
        ["融资余额（单位：元）", "融资余额"],
    ),
    # 主力净流入：「股」单位量级异常（Phase 0：-37631 股 vs MX -8.699亿），
    # direction 验证可参考方向一致性，量级比对仅供参考
    "main_inflow": (
        "get_stock_performance",
        "{code} 主力净流入额",
        [
            "主力净流入额（单位：万元）",
            "主力净流入额（单位：元）",
            "主力净流入额",
            "主力净流入量（单位：股）",
        ],
    ),
}
_PERIOD_FIELDS = {
    "revenue",
    "net_profit",
    "roe",
    "gross_margin",
    "revenue_yoy",
    "net_profit_yoy",
}
# ------------------------------------------------------------------
# 解析（纯函数，100% 可单测）
# ------------------------------------------------------------------


def _ifind_tables(text: str) -> list[list[dict[str, str]]]:
    """Preserve separate tables and empty cells; reject ragged rows."""
    tables: list[list[dict[str, str]]] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if index == 0 or not re.fullmatch(r"\s*\|[\s\-:|]+\|\s*", line):
            continue
        headers = [cell.strip() for cell in lines[index - 1].strip().strip("|").split("|")]
        if len(set(headers)) != len(headers):
            raise SourceReadError("ifind", "invalid_response")
        rows = []
        for row in lines[index + 1:]:
            if not row.strip().startswith("|") or re.fullmatch(r"\s*\|[\s\-:|]+\|\s*", row):
                break
            cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
            if len(cells) != len(headers) or cells == headers:
                break
            rows.append(dict(zip(headers, cells)))
        if rows:
            tables.append(rows)
    return tables


def _parse_ifind_markdown_table(text: str, period: Optional[str] = None, keywords: Optional[List[str]] = None, field: Optional[str] = None) -> Dict[str, str]:
    rows = [row for table in _ifind_tables(text) for row in table]
    if keywords is not None:
        rows = [row for row in rows if _extract_ifind_value(row, keywords, field=field)[0] is not None]
    selected = select_report_period([report_period_from_fields(row) for row in rows], period)
    return rows[selected if selected is not None else 0] if rows else {}


def _parse_ifind_markdown_series(
    text: str, value_kw: str, date_kw: str = "日期"
) -> List[Tuple[str, float]]:
    """解析 iFinD 多行 Markdown 表，收集 ``(日期, 值)`` 序列（最新在前）。

    与 :func:`_parse_ifind_markdown_table`（只取首行）互补，用于多日时序
    （如实测 ``"近5日主力净流入额"`` → 6 行每日金额）。``value_kw`` 定位金额列
    （传 ``主力净流入额`` 以避开 ``主力净流入量(单位:股)``——额外排除含「量/股」的列），
    ``date_kw`` 定位日期列。无表 / 无匹配列 / 全行不可解析 → ``[]``。
    """
    lines = (text or "").strip().splitlines()
    if not lines:
        return []
    sep_idx = -1
    for i, line in enumerate(lines):
        if re.match(r"^\|[\s\-:|]+\|$", line) or re.match(r"^\|[\s\-]+", line):
            sep_idx = i
            break
    if sep_idx < 0:
        return []
    headers = [h.strip() for h in lines[0].split("|") if h.strip()]
    val_idx = next(
        (
            i
            for i, h in enumerate(headers)
            if value_kw in h and "量" not in h and "股" not in h
        ),
        None,
    )
    date_idx = next((i for i, h in enumerate(headers) if date_kw in h), None)
    if val_idx is None or date_idx is None:
        return []
    series: List[Tuple[str, float]] = []
    for row in lines[sep_idx + 1 :]:
        cells = [c.strip() for c in row.split("|") if c.strip()]
        if (
            not cells
            or cells[0].startswith("#")
            or len(cells) <= max(val_idx, date_idx)
        ):
            continue
        val = _safe_float(cells[val_idx])
        if val is None:
            continue
        series.append((cells[date_idx], val))
    return series


def _safe_float(value: Any) -> Optional[float]:
    """容错转 float（处理数字/科学计数/中文金额单位）。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        if f != f:  # NaN
            return None
        return f
    s = str(value).strip().replace(",", "").replace("%", "").replace("倍", "")
    if not s or s in ("-", "--", "nan", "None", "null"):
        return None
    s = s.replace("元", "")  # 移除单位词「元」（保留量级单位「亿/万」）
    # 中文金额单位（最长匹配，顺序：万亿>千亿>百亿>十亿>亿>万）
    CN_UNIT_FACTORS: Dict[str, float] = {
        "万亿": 1e12,
        "千亿": 1e11,
        "百亿": 1e10,
        "十亿": 1e9,
        "亿": 1e8,
        "万": 1e4,
    }
    factor = 1.0
    for unit, mult in CN_UNIT_FACTORS.items():
        if s.endswith(unit):
            s = s[: -len(unit)].strip()
            factor = mult
            break
    try:
        return float(s) * factor
    except (TypeError, ValueError):
        return None


def _extract_ifind_value(
    table: Dict[str, str], keywords: List[str], field: Optional[str] = None, *, cumulative_only: bool = False,
) -> Tuple[Optional[float], str]:
    """从解析后的表中按关键词取数值。

    返回 (value, used_column)。找不到关键词 → (None, "")。
    中文金额单位（万亿/亿/万）由 ``_safe_float`` 统一换算。
    """
    fallback: Tuple[Optional[float], str] = (None, "")
    for kw in keywords:
        for col, val in table.items():
            if kw in col:
                if cumulative_only and field in _PERIOD_FIELDS and ("单季" in col or "单季度" in col):
                    continue
                v = normalize_anchor_value(field, val, col)[0] if field is not None else _safe_float(val)
                if v is not None:
                    if field is None:
                        return v, col
                    _, unit, currency, error = normalize_anchor_value(field, val, col, unit=table.get("unit"), currency=table.get("currency"))
                    if not reading_input_reasons(AnchorReading("ifind", v, caliber=caliber_from_label(field, col), unit=unit, currency=currency, normalization_error=error), field):
                        return v, col
                    if fallback[0] is None:
                        fallback = (v, col)
    return fallback


class IfindData(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, validate_assignment=True, allow_inf_nan=False)
    answer: str
    indicators_params: dict[str, dict[str, str | int | float | None]] = Field(default_factory=dict)


class IfindReply(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, validate_assignment=True, allow_inf_nan=False)
    code: int
    msg: str = ""
    data: IfindData | None = None


class ProductSegment(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, validate_assignment=True)
    name: Annotated[str, Field(min_length=1)]
    rank: Annotated[int, Field(ge=1)]
    report_date: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
    revenue: AnchorReading | None = None
    cost: AnchorReading | None = None
    gross_profit: AnchorReading | None = None
    gross_margin: AnchorReading | None = None


class ProductSegments(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, validate_assignment=True)
    source: Literal["ifind"] = "ifind"
    coverage: Literal["partial"] = "partial"
    items: list[ProductSegment] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    disclosed_at: str | None = None
    original_url: str | None = None


class IfindHolder(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, validate_assignment=True)
    name: Annotated[str, Field(min_length=1)]
    rank: Annotated[int, Field(ge=1, le=10)]
    report_date: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
    shares: AnchorReading | None = None
    ratio: AnchorReading | None = None


class IfindShareholders(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, validate_assignment=True)
    source: Literal["ifind"] = "ifind"
    coverage: Literal["partial", "complete"] = "partial"
    items: list[IfindHolder] = Field(default_factory=list, max_length=10)
    errors: list[str] = Field(default_factory=list)
    disclosed_at: str | None = None
    original_url: str | None = None


@dataclass(frozen=True)
class _RankedCell:
    raw: str
    label: str
    params: dict[str, str | int | float | None]


def confirmed_disclosed_period(*sources: object, as_of: str | None = None) -> str | None:
    """Latest period already disclosed by the analysis time. Never invents a date."""
    anchor = (as_of or date.today().isoformat())[:10]
    found: list[str] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        pairs: list[tuple[object, object]] = [
            (source.get("period"), source.get("disclosed_at")),
            (source.get("report_date"), source.get("disclosure_date")),
        ]
        holders = source.get("holders") if isinstance(source.get("holders"), dict) else {}
        growth = source.get("growth") if isinstance(source.get("growth"), dict) else {}
        growth_data = growth.get("data") if isinstance(growth.get("data"), dict) else growth
        pairs.extend([
            (holders.get("period"), holders.get("disclosed_at")),
            (growth_data.get("report_date"), growth_data.get("disclosure_date")),
        ])
        meta = source.get("field_meta") if isinstance(source.get("field_meta"), dict) else {}
        financial_meta = source.get("financial_field_meta") if isinstance(source.get("financial_field_meta"), dict) else {}
        for record in (*meta.values(), *financial_meta.values()):
            if isinstance(record, dict):
                pairs.append((record.get("period"), record.get("disclosed_at")))
        for period_raw, disclosed_raw in pairs:
            period = normalize_report_period(period_raw)[0]
            disclosed = normalize_report_period(disclosed_raw)[0]
            if not period or not period.endswith(("-03-31", "-06-30", "-09-30", "-12-31")):
                continue
            if anchor and period > anchor:
                continue
            if disclosed and anchor and disclosed > anchor:
                continue
            found.append(period)
    if not found:
        logger.info("confirmed_disclosed_period: 截至 %s 未找到已披露财期，股东查询退回最新一期并要求实际财期", anchor)
        return None
    return max(found)


def _ranked_cells(data: IfindData, code: str, fields: dict[str, str]) -> tuple[dict[tuple[str, int], dict[str, _RankedCell]], list[str]]:
    """Join ranked supplier cells only by returned security, explicit period and rank."""
    joined: dict[tuple[str, int], dict[str, _RankedCell]] = {}
    rejected: set[tuple[str, int]] = set()
    errors: list[str] = []
    for table in _ifind_tables(data.answer):
        for row in table:
            if not _same_security(row.get("证券代码"), code):
                continue
            row_period = normalize_report_period(row.get("日期") or row.get("报告期"))[0]
            for label, raw in row.items():
                stem = re.sub(r"[（(][^()（）]*(?:排名|单位)[^()（）]*[）)]", "", label).strip()
                field = next((name for name, prefix in fields.items() if stem == prefix), None)
                rank_match = re.search(r"第(\d+)名", label)
                if field is None or rank_match is None or not raw.strip():
                    continue
                rank = int(rank_match[1])
                if rank < 1:
                    errors.append("invalid_rank")
                    continue
                options = [params for key, params in data.indicators_params.items() if key.split("__")[0] == stem and str(params.get("排名") or params.get("大股东排名")) == f"第{rank}名"]
                if len(options) > 1:
                    errors.append("ambiguous_indicator")
                    continue
                params = options[0] if options else {}
                parameter_period = normalize_report_period(params.get("报告期") or params.get("截止日期"))[0]
                period = row_period or parameter_period
                if period is None:
                    errors.append("period_unknown")
                    continue
                key = period, rank
                if row_period and parameter_period and row_period != parameter_period:
                    rejected.add(key)
                    errors.append("period_conflict")
                    continue
                cell = _RankedCell(raw, label, params)
                existing = joined.setdefault(key, {}).get(field)
                if existing is not None and existing != cell:
                    rejected.add(key)
                    errors.append("duplicate_rank_conflict")
                else:
                    joined[key][field] = cell
    return {key: cells for key, cells in joined.items() if key not in rejected}, list(dict.fromkeys(errors))


def _same_security(returned: str | None, requested: str) -> bool:
    """Accept a bare A-share code, but never an explicitly different exchange."""
    from data_provider.choice_adapter import ChoiceAdapter
    expected = ChoiceAdapter.stock_code(requested)
    return returned in {expected, expected.split(".")[0]}


def _detail_reading(cell: _RankedCell, period: str, field: str) -> AnchorReading | None:
    unit = str(cell.params.get("单位") or "").strip() or None
    raw_currency = str(cell.params.get("币种") or "").strip() or None
    currency = raw_currency or ("CNY" if unit == "元" else None)
    value, normalized, currency, error = _ifind_number(cell.raw, cell.label, unit, field, currency)
    if value is None:
        return None
    return AnchorReading("ifind", value, period=period, unit=normalized, currency=currency,
                         raw_value=cell.raw, raw_label=cell.label, raw_unit=unit, raw_currency=raw_currency,
                         normalization_error=error, fetched_at=datetime.now(timezone.utc).isoformat())


def _ifind_data(raw: object) -> IfindData:
    try:
        reply = IfindReply.model_validate(raw)
    except ValidationError:
        raise SourceReadError("ifind", "invalid_response") from None
    message = reply.msg + (reply.data.answer if reply.data else "")
    if any(term in message for term in ("用量已耗尽", "调用次数已达到上限", "额度耗尽", "quota")):
        raise SourceReadError("ifind", "quota_exhausted")
    if any(term in message for term in ("未认证", "无权限", "权限不足", "Token已过期", "token invalid")):
        raise SourceReadError("ifind", "authentication_failed")
    if reply.code != 1:
        raise SourceReadError("ifind", "business_error")
    if reply.data is None or not reply.data.answer.strip():
        raise SourceReadError("ifind", "invalid_response")
    return reply.data


def _ifind_number(raw: str, label: str, unit: str | None, field: str, currency: str | None = None) -> tuple[float | None, str | None, str | None, str | None]:
    # The cell's scale abbreviates a value in base units: 元 + 1.2亿 is 1.2e8元.
    # A non-base declared scale (万元 + 1.2亿) is ambiguous and stays a conflict.
    text = raw.strip().replace(",", "")
    match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)(万亿|千亿|百亿|十亿|百万|亿|万|千)", text)
    declared = (unit or "").strip()
    if match and (declared in {"元", "股"} or not declared and re.search(r"单位[：:]\s*(元|股)[）)]", label)):
        scale = {"万亿": 10**12, "千亿": 10**11, "百亿": 10**10, "十亿": 10**9, "百万": 10**6, "亿": 10**8, "万": 10**4, "千": 10**3}[match[2]]
        try:
            text = str(Decimal(match[1]) * scale)
        except DecimalException:
            return None, None, currency, "invalid_value"
    annotation = re.search(r"单位[：:]\s*([^）)；;]+)", label)
    numeric_label = f"(单位：{annotation[1]})" if annotation else label
    value, normalized, currency, error = normalize_anchor_value(field, text, numeric_label, unit=declared or None, currency=currency)
    return value, normalized, currency, error


def _parse_ifind_response(raw_text: str, keywords: List[str], field: str, period: Optional[str], *, code: str | None = None, a_share_contract: bool = False) -> Optional[AnchorReading]:
    try:
        raw = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        return None
    # Historical parser fixtures may contain only the data envelope; wire replies are validated in query().
    data = IfindData.model_validate(raw["data"]) if isinstance(raw, dict) and "code" not in raw and "data" in raw else _ifind_data(raw)
    candidates: list[AnchorReading] = []
    for table in _ifind_tables(data.answer):
        for row in table:
            if code and not _same_security(row.get("证券代码"), code):
                continue
            value, col = _extract_ifind_value(row, keywords, field=field, cumulative_only=a_share_contract)
            if value is None or field in _PERIOD_FIELDS and caliber_from_label(field, col) is None:
                continue
            bare = re.sub(r"[（(][^()（）]*单位[^()（）]*[）)]", "", col).strip()
            params = data.indicators_params.get(bare, {})
            unit = str(params["单位"]).strip() if params.get("单位") else row.get("unit")
            currency = str(params.get("币种") or row.get("currency") or "").strip() or None
            currency = {"人民币": "CNY", "人民币元": "CNY", "美元": "USD", "港币": "HKD"}.get(currency, currency)
            raw_currency = currency
            # Explicit deployment decision for this A-share MCP batch, not supplier metadata.
            if a_share_contract and currency is None and (unit == "元" or re.search(r"单位[：:]\s*元[）)]", col)):
                currency = "CNY"
            actual, basis = report_period_from_fields(row, col)
            if actual is None and field in _PERIOD_FIELDS:
                actual, basis = normalize_report_period(row.get("日期"))
            parameter_period, parameter_basis = normalize_report_period(params.get("报告期"))
            error = None
            if actual and parameter_period and actual != parameter_period:
                error = "period_conflict"
            actual = actual or parameter_period
            if basis == "unknown":
                basis = parameter_basis
            # Financial statement totals are cumulative; single-quarter columns remain distinct.
            if actual and basis == "unknown" and field in _PERIOD_FIELDS:
                basis = "single_quarter" if "单季度" in col or "单季" in col else "annual" if actual.endswith("12-31") else "YTD"
            scope = str(params.get("报表类型") or row.get("报表类型") or "")
            adjustment = str(params.get("调整口径") or params.get("数据调整") or row.get("调整口径") or "")
            number, normalized_unit, currency, number_error = _ifind_number(row[col], col, unit, field, currency)
            if number is None:
                continue
            disclosure_period = normalize_report_period(data.indicators_params.get("定期报告实际披露日期", {}).get("报告期"))[0]
            disclosed = next((r.get("定期报告实际披露日期") for t in _ifind_tables(data.answer) for r in t
                              if r.get("定期报告实际披露日期") and (not code or _same_security(r.get("证券代码"), code))
                              and (report_period_from_fields(r)[0] or disclosure_period) == actual
                              and (not disclosure_period or disclosure_period == actual)), None)
            candidates.append(AnchorReading(
                "ifind", number, caliber=caliber_from_label(field, col),
                period=actual if field in _PERIOD_FIELDS else None, period_basis=basis if field in _PERIOD_FIELDS else "unknown",
                requested_period=period if field in _PERIOD_FIELDS else None,
                observed_at=None if field in _PERIOD_FIELDS else observation_time_from_fields(row) or str(params.get("交易日期") or "") or None,
                fetched_at=datetime.now(timezone.utc).isoformat(), unit=normalized_unit, currency=currency,
                raw_value=row[col], raw_label=col, raw_unit=unit, raw_currency=raw_currency,
                normalization_error=error or number_error,
                statement_scope="consolidated" if scope in {"合并报表", "合并报表(调整)", "合并报表（调整）"} else "parent" if scope == "母公司报表" else "consolidated" if a_share_contract and not scope and field in {"roe", "gross_margin", "revenue_yoy", "net_profit_yoy"} else None,
                adjustment="restated" if adjustment in {"重述", "重述后", "调整后"} or scope in {"合并报表(调整)", "合并报表（调整）"} else "unadjusted" if adjustment in {"调整前", "未调整"} else None,
                disclosed_at=normalize_report_period(disclosed)[0],
            ))
    from .cross_source_validator import has_current_financial_period
    selected = select_report_period([(r.period, r.period_basis) if not a_share_contract or has_current_financial_period(r) else (None, "unknown") for r in candidates], period)
    if not candidates:
        return None
    reading = candidates[selected if selected is not None else 0]
    identity = (reading.period, reading.period_basis, reading.caliber, reading.observed_at)
    evidence = (reading.value, reading.unit, reading.currency, reading.statement_scope, reading.adjustment, reading.normalization_error)
    if any((other.period, other.period_basis, other.caliber, other.observed_at) == identity
           and (other.value, other.unit, other.currency, other.statement_scope, other.adjustment, other.normalization_error) != evidence
           for other in candidates):
        raise SourceReadError("ifind", "invalid_response")
    return reading


class ConceptMembership(BaseModel):
    """个股最新概念归属（iFinD，issue #45）。

    只证明查询时点的最新归属：不补造历史日期，不混用行业分类。
    """
    model_config = ConfigDict(strict=True, frozen=True, validate_assignment=True)
    code: str
    concepts: list[str] = Field(default_factory=list)
    source: Literal["ifind"] = "ifind"
    as_of_nature: Literal["latest"] = "latest"
    errors: list[str] = Field(default_factory=list)


def _parse_concepts(data: IfindData, code: str) -> ConceptMembership:
    """所属概念单元格 → 概念名列表；证券不匹配或字段缺失记错误。"""
    errors: list[str] = []
    concepts: list[str] = []
    matched = False
    for table in _ifind_tables(data.answer):
        for row in table:
            if not _same_security(row.get("证券代码"), code):
                continue
            matched = True
            raw = (row.get("所属概念") or "").strip()
            if raw:
                concepts.extend(name.strip() for name in raw.split(",") if name.strip())
    if not matched:
        errors.append("security_mismatch")
    elif not concepts:
        errors.append("no_concepts")
    return ConceptMembership(code=code, concepts=list(dict.fromkeys(concepts)), errors=errors)



class IfindFetcher:
    """One fresh MCP session per bounded batch; no background loop or hidden retry."""
    _instance: Optional["IfindFetcher"] = None
    financial_fields = ("revenue", "net_profit", "roe", "gross_margin", "revenue_yoy", "net_profit_yoy")
    contract_version = "ifind-financial-v1"

    def __init__(self, endpoint: str | None = None, token: str | None = None, timeout_seconds: float | None = None) -> None:
        self._endpoint = (os.getenv("IFIND_MCP_ENDPOINT", "") if endpoint is None else endpoint).strip()
        self._token = (os.getenv("IFIND_MCP_TOKEN", "") if token is None else token).strip()
        self._timeout = float(os.getenv("IFIND_MCP_TIMEOUT_SECONDS") or "30") if timeout_seconds is None else timeout_seconds

    @property
    def available(self) -> bool:
        return bool(self._endpoint and self._token)

    @classmethod
    def get_instance(cls) -> "IfindFetcher":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def query(self, tool: str, question: str, *, deadline: float | None = None) -> IfindData:
        from src.core.research_run import current_research, ResearchStopped
        if run := current_research.get():
            run.check()
        if not self.available:
            raise SourceReadError("ifind", "invalid_configuration")
        timeout = min(self._timeout, deadline - monotonic()) if deadline is not None else self._timeout
        if timeout <= 0:
            raise SourceReadError("ifind", "timeout")
        try:
            raw = asyncio.run(asyncio.wait_for(self._async_query(tool, question, deadline=deadline), timeout))
            return _ifind_data(raw)
        except ResearchStopped:
            raise
        except Exception as exc:
            if run := current_research.get():
                run.check()
            raise SourceReadError("ifind", source_failure_reason(exc)) from None

    async def _async_query(self, tool: str, question: str, *, deadline: float | None = None) -> object:
        from src.services.mcp_client import call_mcp_tool
        from src.core.research_run import current_research
        if run := current_research.get():
            run.check()
        result = await call_mcp_tool(self._endpoint, self._token, tool, {"query": question}, timeout_seconds=self._timeout, deadline=deadline)
        if getattr(result, "is_error", getattr(result, "isError", False)):
            raise SourceReadError("ifind", "business_error")
        structured = getattr(result, "structured_content", getattr(result, "structuredContent", None))
        if structured is not None:
            return structured
        for block in result.content:
            if getattr(block, "text", None):
                return json.loads(block.text)
        raise SourceReadError("ifind", "invalid_response")

    def financial_snapshot(self, code: str, period: str | None, *, deadline: float) -> dict[str, AnchorReading]:
        from data_provider.choice_adapter import ChoiceAdapter
        today = date.today()
        target = f"报告期{period}" if period else f"查询{date(today.year - 1, today.month, 1)}至{today}每个报告期"
        query = f"{ChoiceAdapter.stock_code(code)} {target}的营业收入、归属于母公司所有者净利润、加权ROE、销售毛利率、营业收入同比增长率、归母净利润同比增长率，合并报表（调整）。结果每行列出证券代码、实际报告期，未披露期不输出；同时查询各期定期报告实际披露日期。明确单位、币种、报表类型、调整口径；无数据请保留空值，不推算。"
        data = self.query("get_stock_financials", query, deadline=deadline)
        raw = json.dumps({"code": 1, "data": data.model_dump()}, ensure_ascii=False)
        readings = {}
        for field in self.financial_fields:
            reading = _parse_ifind_response(raw, _IFIND_ANCHOR_QUERIES[field][2], field, period, code=code, a_share_contract=True)
            if reading is not None:
                readings[field] = reading
        if not readings:
            raise SourceReadError("ifind", "invalid_response")
        return readings

    def product_segments(self, code: str, period: str | None = None, *, deadline: float | None = None) -> ProductSegments:
        target = period or f"{date.today().year - 1}-12-31"
        data = self.query("get_stock_financials", f"{code} 报告期{target} 主营构成(按产品)：项目名称、收入、成本、毛利金额、毛利率，列出排名与实际财期、各字段单位、披露日期及原文链接；空值不要推算。", deadline=deadline)
        cells, errors = _ranked_cells(data, code, {"name": "主营构成-项目名称(按产品)", "revenue": "主营构成-项目收入(按产品)", "cost": "主营构成-项目成本(按产品)", "gross_profit": "主营构成-项目毛利(按产品)", "gross_margin": "主营构成-项目毛利率(按产品)"})
        if not cells and not errors:
            raise SourceReadError("ifind", "invalid_response")
        names = [(period, row["name"].raw) for (period, rank), row in cells.items() if "name" in row]
        items = []
        for (actual, rank), row in sorted(cells.items()):
            name = row.get("name")
            if name is None or names.count((actual, name.raw)) != 1:
                errors.append("product_identity_missing_or_duplicate")
                continue
            values = {field: _detail_reading(row[field], actual, "gross_margin" if field == "gross_margin" else "revenue") for field in ("revenue", "cost", "gross_profit", "gross_margin") if field in row}
            if not any(value is not None for value in values.values()):
                errors.append("product_values_missing")
                continue
            items.append(ProductSegment(name=name.raw, rank=rank, report_date=actual, **values))
        return ProductSegments(items=items, errors=list(dict.fromkeys(errors)))

    def shareholders(self, code: str, period: str | None = None, *, deadline: float | None = None) -> IfindShareholders:
        requested = normalize_report_period(period)[0] if period else None
        if requested:
            question = f"{code} 报告期{requested} 前十大股东：各排名的股东名称、持股数量(单位股)、持股比例(单位%)；明确实际截止日期、披露日和原文链接，合计比例另列，空值不要推算。"
        else:
            question = f"{code} 截止最新已披露财期 前十大股东：各排名的股东名称、持股数量(单位股)、持股比例(单位%)；明确实际财期、披露日和原文链接，合计比例另列，空值不要推算。"
        data = self.query("get_stock_shareholders", question, deadline=deadline)
        cells, errors = _ranked_cells(data, code, {"name": "股东名称", "shares": "股东持股数量", "ratio": "股东持股比例"})
        if not cells and not errors:
            raise SourceReadError("ifind", "invalid_response")
        periods = {actual for (actual, rank), row in cells.items() if "name" in row}
        actual = max(periods) if periods else None
        names = [row["name"].raw for (row_period, rank), row in cells.items() if row_period == actual and "name" in row]
        items = []
        for (row_period, rank), row in sorted(cells.items()):
            if row_period != actual or not 1 <= rank <= 10:
                continue
            name = row.get("name")
            if name is None or names.count(name.raw) != 1:
                errors.append("holder_identity_missing_or_duplicate")
                continue
            shares = _detail_reading(row["shares"], row_period, "share_count") if "shares" in row else None
            ratio = _detail_reading(row["ratio"], row_period, "gross_margin") if "ratio" in row else None
            if shares is not None and (shares.unit != "shares" or shares.value < 0):
                shares = replace(shares, normalization_error=shares.normalization_error or "invalid_holder_quantity")
            if ratio is not None and (ratio.unit != "percentage_point" or not 0 <= ratio.value <= 100):
                ratio = replace(ratio, normalization_error=ratio.normalization_error or "invalid_holder_ratio")
            items.append(IfindHolder(name=name.raw, rank=rank, report_date=row_period, shares=shares, ratio=ratio))
        if requested and actual and actual != requested:
            errors.append("period_mismatch")
        complete = {item.rank for item in items} == set(range(1, 11)) and all(item.shares is not None and item.ratio is not None and not item.shares.normalization_error and not item.ratio.normalization_error for item in items) and not errors
        return IfindShareholders(items=items, coverage="complete" if complete else "partial", errors=list(dict.fromkeys(errors)))



    def fetch(self, code: str, field: str, period: str | None = None, *, deadline: float | None = None) -> AnchorReading | None:
        if not self.available or field not in _IFIND_ANCHOR_QUERIES:
            return None
        tool, template, keywords = _IFIND_ANCHOR_QUERIES[field]
        data = self.query(tool, template.format(code=code, period=period or ""), deadline=deadline)
        return _parse_ifind_response(json.dumps({"code": 1, "data": data.model_dump()}), keywords, field, period, code=code)

    def concept_membership(self, code: str, *, deadline: float | None = None) -> ConceptMembership:
        """个股最新所属概念（get_stock_info 适配入口，issue #45）。

        只证明最新归属；空结果、字段错误、证券不匹配、鉴权/额度拒绝、超时
        由 SourceReadError 或 ConceptMembership.errors 区分，不补造日期。
        """
        data = self.query("get_stock_info", f"{code} 所属概念", deadline=deadline)
        return _parse_concepts(data, code)


    def fetch_main_inflow_series(self, code: str, days: int = 12, *, deadline: float | None = None) -> Optional[List[Tuple[str, float]]]:
        if not self.available:
            return None
        data = self.query("get_stock_performance", f"{code} 近{days}个交易日主力净流入额", deadline=deadline)
        return _parse_ifind_markdown_series(data.answer, "主力净流入额")


class IfindSource:
    """iFinD 数据源适配器（实现 :class:`SourceAdapter`）。

    依赖注入 fetcher：测试注入同步假 fetcher 覆盖全部映射逻辑；真实
    :class:`IfindFetcher` 通过 ``IfindFetcher.get_instance()`` 注入。
    """

    name = "ifind"

    def __init__(self, fetcher: Optional[IfindFetcher] = None, *, readings: dict[str, AnchorReading] | None = None, error: str | None = None) -> None:
        self._fetcher = fetcher
        self._readings = readings
        self._error = error

    @property
    def available(self) -> bool:
        return bool(self._fetcher and getattr(self._fetcher, "available", False))

    def read(
        self, code: str, field: str, period: Optional[str] = None, *, deadline: Optional[float] = None
    ) -> Optional[AnchorReading]:
        """同步读取。无 fetcher / 未知字段 / 失败 → None（fail-open）。"""
        if self._error:
            raise SourceReadError("ifind", self._error)
        if self._readings is not None:
            return self._readings.get(field)
        if self._fetcher is None:
            return None
        if field not in _IFIND_ANCHOR_QUERIES:
            return None
        from src.core.research_run import ResearchStopped
        try:
            reading = self._fetcher.fetch(code, field, period, **({"deadline": deadline} if deadline is not None else {}))
        except ResearchStopped:
            raise
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.debug("[IfindSource] read %s/%s failed: %s", code, field, source_failure_reason(exc))
            raise SourceReadError("ifind", source_failure_reason(exc)) from None
        if reading is None:
            return None
        return replace(reading, source="ifind", requested_period=reading.requested_period if reading.requested_period is not None else period if field in _PERIOD_FIELDS else None)
