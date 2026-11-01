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
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from icontract import ensure
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
    # issue #71：资产负债与六项财务同一锚点解析/资格层；期间与单季列语义一致。
    "assets",
    "liabilities",
}
# issue #71：资产负债列关键词，与六项财务共用 _parse_ifind_response 单列解析。
_BALANCE_FIELD_KEYWORDS: Dict[str, List[str]] = {
    "assets": ["资产总计（单位：元）", "资产总计"],
    "liabilities": ["负债合计（单位：元）", "负债合计"],
}
# ------------------------------------------------------------------
# 解析（纯函数，100% 可单测）
# ------------------------------------------------------------------


_IFIND_TABLE_SEPARATOR = re.compile(r"\s*\|[\s\-:|]+\|\s*")


def _ifind_prose(answer: str) -> str:
    """供应商散文文本（markdown 表格之外的行）：拒绝文案在这里，业务事实在表格里。"""
    lines = answer.splitlines()
    consumed: set[int] = set()
    for index, line in enumerate(lines):
        if index == 0 or index in consumed or not _IFIND_TABLE_SEPARATOR.fullmatch(line):
            continue
        start = index - 1
        end = index
        for row in lines[index + 1:]:
            if not row.strip().startswith("|") or _IFIND_TABLE_SEPARATOR.fullmatch(row):
                break
            end += 1
        consumed.update(range(start, end + 1))
    return "\n".join(line for index, line in enumerate(lines) if index not in consumed)


def _ifind_tables(text: str) -> list[list[dict[str, str]]]:
    """Preserve separate tables and empty cells; reject ragged rows."""
    tables: list[list[dict[str, str]]] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if index == 0 or not _IFIND_TABLE_SEPARATOR.fullmatch(line):
            continue
        headers = [cell.strip() for cell in lines[index - 1].strip().strip("|").split("|")]
        if len(set(headers)) != len(headers):
            raise SourceReadError("ifind", "invalid_response")
        rows = []
        for row in lines[index + 1:]:
            if not row.strip().startswith("|") or _IFIND_TABLE_SEPARATOR.fullmatch(row):
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
    sub_msg: str | None = Field(default=None, validation_alias="subMsg")
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


@ensure(lambda result: result[0] == result[1] - result[2], "Concentration change is current minus previous in percentage points")
def shareholder_concentration_change(current: IfindShareholders, previous: IfindShareholders) -> tuple[Decimal, Decimal, Decimal]:
    """Compare each period's complete top ten, without matching exits to zero holdings."""
    totals: list[Decimal] = []
    periods: list[str] = []
    for dataset in (current, previous):
        if dataset.coverage != "complete" or dataset.errors or {item.rank for item in dataset.items} != set(range(1, 11)):
            raise ValueError("top10_incomplete")
        actual = {item.report_date for item in dataset.items}
        if len(actual) != 1 or len({item.name for item in dataset.items}) != 10:
            raise ValueError("holder_identity_or_period_mismatch")
        period = next(iter(actual))
        if normalize_report_period(period)[0] != period or period > date.today().isoformat():
            raise ValueError("holder_period_invalid")
        ratios: list[Decimal] = []
        for item in dataset.items:
            ratio = item.ratio
            if ratio is None or ratio.unit != "percentage_point" or ratio.normalization_error or ratio.period != period:
                raise ValueError("holder_ratio_unqualified")
            value = Decimal(str(ratio.value))
            if not value.is_finite() or not 0 <= value <= 100:
                raise ValueError("holder_ratio_invalid")
            ratios.append(value)
        total = sum(ratios, Decimal(0))
        if total > 100:
            raise ValueError("top10_concentration_exceeds_100")
        totals.append(total)
        periods.append(period)
    if periods[0] <= periods[1]:
        raise ValueError("holder_period_not_increasing")
    return totals[0] - totals[1], totals[0], totals[1]


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


def _ifind_failure_reason(message: str) -> str | None:
    """Keep permission, authentication, throttling and quota distinct.

    HTTP status is not an input: a generic 429 is not quota, and a permission
    phrase is not a login failure.
    """
    lowered = message.lower()
    if any(term in message or term in lowered for term in ("用量已耗尽", "调用次数已达到上限", "额度耗尽", "quota")):
        return "quota_exhausted"
    if any(term in message or term in lowered for term in ("权限不足", "无权限", "permission denied", "permission_denied")):
        return "permission_denied"
    if any(term in message or term in lowered for term in ("未认证", "token已过期", "token invalid", "认证失败", "登录失败")):
        return "authentication_failed"
    if any(term in message or term in lowered for term in ("限流", "频率超限", "too many requests", "rate limit")):
        return "rate_limited"
    return None


def _mcp_payload(result: object) -> object | None:
    """Supplier envelope from structured content, filled from text if needed."""
    structured = getattr(result, "structured_content", None)
    if structured is None:
        structured = getattr(result, "structuredContent", None)
    from src.services.mcp_client import json_object_from_text
    texts: list[dict[str, object]] = []
    for block in getattr(result, "content", None) or []:
        parsed = json_object_from_text(getattr(block, "text", None) or "")
        if parsed is not None:
            texts.append(parsed)
    if isinstance(structured, dict):
        merged = dict(structured)
        for payload in texts:
            for key in ("code", "subCode", "sub_code", "msg", "subMsg", "sub_msg", "data"):
                if key not in merged and key in payload:
                    merged[key] = payload[key]
        return merged
    if texts:
        return texts[0]
    return structured if structured is not None else None


def _ifind_data(raw: object) -> IfindData:
    if isinstance(raw, dict) and ("sub_msg" in raw or "subMsg" in raw):
        payload = {key: value for key, value in raw.items() if key != "sub_msg"}
        chosen = payload.get("subMsg")
        alternate = raw.get("sub_msg")
        if (not isinstance(chosen, str) or not chosen) and isinstance(alternate, str) and alternate:
            payload["subMsg"] = alternate
        elif "subMsg" not in payload:
            payload["subMsg"] = alternate
        raw = payload
    try:
        reply = IfindReply.model_validate(raw)
    except ValidationError:
        raise SourceReadError("ifind", "invalid_response") from None
    # Classify the supplier status text plus refusal prose outside business tables.
    # A numeric subCode is not a reason, and table cells are business facts: a value
    # like 受限流通股 must not read as 限流 throttling, while a plain-text refusal
    # (e.g. 用量已耗尽) in the answer still classifies.
    prose = _ifind_prose(reply.data.answer) if reply.data else ""
    message = reply.msg + (reply.sub_msg or "") + prose
    reason = _ifind_failure_reason(message)
    if reason is not None:
        raise SourceReadError("ifind", reason)
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


def _parse_industry(data: IfindData, code: str) -> Dict[str, Any]:
    """正式申万行业：系统/级别/代码/名称/截止日期/来源全保留。

    只消费 所属申万行业/所属申万行业代码 列（不把 所属同花顺行业 或概念当
    行业）；供应商未回显分类版本，``classification_version`` 保持 None，
    不把请求文本里的版本写成返回事实。
    """
    errors: list[str] = []
    saw_security_column = False
    matched = False
    field_present = False
    candidates: list[Dict[str, Any]] = []
    for table in _ifind_tables(data.answer):
        for row in table:
            if "证券代码" in row:
                saw_security_column = True
            if not _same_security(row.get("证券代码"), code):
                continue
            matched = True
            name = str(row.get("所属申万行业", "")).strip()
            if not name:
                continue
            field_present = True
            name_params = data.indicators_params.get("所属申万行业", {})
            code_column = str(row.get("所属申万行业代码", "")).strip()
            code_params = data.indicators_params.get("所属申万行业代码", {})
            level = str(name_params.get("行业级别") or code_params.get("行业级别") or "").strip() or None
            name_date = normalize_report_period(name_params.get("截止日期"))[0]
            code_date = normalize_report_period(code_params.get("截止日期"))[0]
            if name_date and code_date and name_date != code_date:
                # 名称列与代码列声明互相矛盾的生效日期：整行不可采信
                errors.append("industry_metadata_conflict")
                continue
            candidates.append({"name": name, "code": code_column or None,
                               "level": level, "as_of": name_date or code_date})
    if not candidates:
        if not saw_security_column:
            errors.append("field_error")
        elif not matched:
            errors.append("security_mismatch")
        elif not field_present:
            errors.append("no_sw_industry")
        else:
            errors.append("industry_metadata_conflict")
        return {"source": "ifind", "system": "sw", "classification_version": None,
                "level": None, "code": None, "name": None, "as_of": None, "errors": errors}
    identities = {(item["name"], item["code"], item["level"], item["as_of"]) for item in candidates}
    if len(identities) > 1:
        # 同一证券同批返回互相矛盾的正式行业：整批拒绝，不取第一个当主值
        return {"source": "ifind", "system": "sw", "classification_version": None,
                "level": None, "code": None, "name": None, "as_of": None,
                "errors": ["duplicate_industry_conflict"]}
    found = candidates[0]
    if not found["level"] or not found["as_of"]:
        errors.append("industry_metadata_missing")
    return {"source": "ifind", "system": "sw", "classification_version": None,
            "level": found["level"], "code": found["code"], "name": found["name"],
            "as_of": found["as_of"], "errors": errors}


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
    """所属概念单元格 → 概念名列表。

    无表格/缺列、证券不匹配、空单元格、历史截止日期各自记不同错误。
    历史截止日期不能标成最新，也不补造日期。
    """
    errors: list[str] = []
    concepts: list[str] = []
    matched = False
    saw_security_column = False
    field_present = False
    historical = False
    tables = _ifind_tables(data.answer)
    if not tables:
        return ConceptMembership(code=code, errors=["field_error"])
    for table in tables:
        for row in table:
            if "证券代码" in row:
                saw_security_column = True
            if not _same_security(row.get("证券代码"), code):
                continue
            matched = True
            if "所属概念" not in row:
                continue
            field_present = True
            cutoff = (row.get("截止日期") or "").strip()
            if cutoff and cutoff not in {"最新", "最新日期", "-"} and re.search(r"\d{4}", cutoff):
                historical = True
                continue
            raw = row.get("所属概念", "").strip()
            if raw:
                concepts.extend(name.strip() for name in raw.split(",") if name.strip())
    if historical and not concepts:
        errors.append("historical_cutoff")
    elif not saw_security_column:
        errors.append("field_error")
    elif not matched:
        errors.append("security_mismatch")
    elif not field_present:
        errors.append("field_error")
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
        from src.core.research_run import ResearchStopped, current_research, supplier_need_key
        from src.patches.eastmoney_patch import request_deadline
        from src.services import mcp_client
        from data_provider.mx_data_adapter import financial_refresh
        run = current_research.get()
        outer = request_deadline.get()
        if outer is not None:
            deadline = outer if deadline is None else min(deadline, outer)
        if run is not None:
            deadline = run.deadline if deadline is None else min(deadline, run.deadline)
        if not self.available:
            raise SourceReadError("ifind", "invalid_configuration")
        timeout = min(self._timeout, deadline - monotonic()) if deadline is not None else self._timeout
        # Issue #51: every iFinD outbound passes this one boundary, so the same
        # report, tool, question, contract and refresh share one tools/call —
        # including the known failure. An already-expired or cancelled call still
        # enters the boundary so the undispatched attempt is recorded.
        def _once() -> IfindData:
            call_token = mcp_client.current_call_id.set(uuid4().hex[:12])
            try:
                coro = self._async_query(tool, question, deadline=deadline)
                raw = asyncio.run(coro if timeout <= 0 else asyncio.wait_for(coro, timeout))
                return _ifind_data(raw)
            finally:
                mcp_client.current_call_id.reset(call_token)
        try:
            stopped = run is not None and (run.stop_reason or run.finalized or run.remaining() <= 0)
            if run is None or stopped:
                return _once()
            refresh = financial_refresh.get()
            marker = refresh[1] if refresh else ""
            from src.config import get_config
            remaining = max(0.0, deadline - monotonic()) if deadline is not None else None
            key = supplier_need_key("ifind", get_config().data_source_contract, tool, question, marker)
            return run.single_fetch(key, _once, timeout=remaining)
        except asyncio.CancelledError:
            # A bare SDK cancellation is a user cancel only when the run's own
            # state says so (issue #58); run.check() translates that state,
            # otherwise the real cancellation keeps propagating instead of
            # becoming a source failure or a mislabelled cancel.
            if run := current_research.get():
                run.check()
            raise
        except ResearchStopped:
            raise
        except SourceReadError:
            raise
        except (Exception, BaseExceptionGroup) as exc:
            # A group carrying a system-exit member keeps propagating: the
            # cancellation/run-state translation below must not absorb it.
            from src.core.research_run import carries_fatal_group_member

            if isinstance(exc, BaseExceptionGroup) and carries_fatal_group_member(exc):
                raise
            if run := current_research.get():
                run.check()
            if isinstance(exc, (json.JSONDecodeError, ValidationError)):
                raise SourceReadError("ifind", "invalid_response") from None
            raise SourceReadError("ifind", source_failure_reason(exc)) from None

    async def _async_query(self, tool: str, question: str, *, deadline: float | None = None) -> object:
        from src.services.mcp_client import call_mcp_tool
        result = await call_mcp_tool(self._endpoint, self._token, tool, {"query": question}, timeout_seconds=self._timeout, deadline=deadline)
        payload = _mcp_payload(result)
        is_error = bool(getattr(result, "is_error", getattr(result, "isError", False)))
        if is_error:
            if payload is None:
                raise SourceReadError("ifind", "business_error")
            try:
                _ifind_data(payload)
            except SourceReadError:
                raise
            raise SourceReadError("ifind", "business_error")
        if payload is None:
            raise SourceReadError("ifind", "invalid_response")
        return payload

    def financial_snapshot(self, code: str, period: str | None, *, deadline: float) -> dict[str, AnchorReading]:
        from data_provider.choice_adapter import ChoiceAdapter
        today = date.today()
        target = f"报告期{period}" if period else f"查询{date(today.year - 1, today.month, 1)}至{today}每个报告期"
        query = f"{ChoiceAdapter.stock_code(code)} {target}的营业收入、归属于母公司所有者净利润、加权ROE、销售毛利率、营业收入同比增长率、归母净利润同比增长率、资产总计、负债合计，合并报表（调整）。结果每行列出证券代码、实际报告期，未披露期不输出；同时查询各期定期报告实际披露日期。明确单位、币种、报表类型、调整口径；无数据请保留空值，不推算。"
        data = self.query("get_stock_financials", query, deadline=deadline)
        raw = json.dumps({"code": 1, "data": data.model_dump()}, ensure_ascii=False)
        readings = {}
        for field in (*self.financial_fields, "assets", "liabilities"):
            keywords = _IFIND_ANCHOR_QUERIES[field][2] if field in _IFIND_ANCHOR_QUERIES else _BALANCE_FIELD_KEYWORDS[field]
            reading = _parse_ifind_response(raw, keywords, field, period, code=code, a_share_contract=True)
            if reading is not None:
                readings[field] = reading
        if not readings:
            raise SourceReadError("ifind", "invalid_response")
        return readings

    def current_info_snapshot(self, code: str, *, deadline: float | None = None, current_time: Any = None) -> Dict[str, Any]:
        """一次 get_stock_info 批次：实际交易日估值/市值与正式申万行业（issue #71）。

        请求日期来自本地严格交易日历的最近已完成交易日（不用 Choice 登录、不
        用服务器 UTC 或自然日兜底；日历不可用即不发批）；供应商仍须回显实际
        日期（行日期/交易日期参数），请求日期本身不替代返回事实。
        估值复用既有锚点解析；PB 仅在表头确为“最新”且该列参数 ``基准日=
        报表截止日期`` 同时成立时按供应商证据认定为 MRQ（root 批准的 P2 范围，
        不做全局“最新→MRQ”词表映射）；其他缺失口径如实保持未知。
        行业仅消费 所属申万行业 列，分类版本未回显则保持 None，代码列缺席
        时不补造。
        """
        from src.core.trading_calendar import recent_completed_trading_sessions
        from data_provider.choice_adapter import ChoiceAdapter
        sessions = recent_completed_trading_sessions("cn", 2, current_time=current_time)
        if not sessions:
            # 无法证明最近已完成交易日时不得用「最新交易日」措辞发批：
            # 实测供应商会回显字面「最新」，估值观测日期不可证明。
            raise SourceReadError("ifind", "calendar_unavailable")
        as_of = sessions[0].isoformat()
        session_window = {session.isoformat() for session in sessions}
        question = (f"{ChoiceAdapter.stock_code(code)} 截至{as_of}的市盈率PE(TTM)、市净率PB(最新)、总市值、流通市值，"
                    "以及所属申万行业（一级行业）名称与代码。每项注明实际交易日期、单位、币种和统计口径；"
                    "行业注明行业级别与截止日期。总市值与流通市值以人民币元返回；无数据请留空，不推算。")
        data = self.query("get_stock_info", question, deadline=deadline)
        raw = json.dumps({"code": 1, "data": data.model_dump()}, ensure_ascii=False)
        valuation: Dict[str, AnchorReading] = {}
        for field in ("pe_ratio", "pb_ratio", "total_mv", "circ_mv"):
            reading = _parse_ifind_response(raw, _IFIND_ANCHOR_QUERIES[field][2], field, None, code=code, a_share_contract=True)
            if reading is None:
                continue
            if field == "pb_ratio" and reading.caliber is None and "最新" in (reading.raw_label or ""):
                bare = re.sub(r"[（(][^()（）]*单位[^()（）]*[）)]", "", reading.raw_label).strip()
                basis = str(data.indicators_params.get(bare, {}).get("基准日") or "").strip()
                if basis == "报表截止日期":
                    reading = replace(reading, caliber="MRQ")
            # issue #71：供应商只回显字面「最新」等不可证明观测时间时，在源读数上
            # 持久标记 time_unknown（保留 raw observed_at）——所有消费层经同一
            # reading_input_reasons 得到一致资格结论，不依赖任何合并层补丁。
            # issue #71：本批估值的实际观测日期必须落在最近已完成交易日截至窗口
            # （严格日历 T / T-1 会话集合）内：缺失/非法/字面「最新」→ time_unknown，
            # 晚于截至日 → time_in_future，早于窗口 → stale；实际 observed_at 保留，
            # 请求日期不回填。所有消费层经同一 reading_input_reasons 得到一致结论。
            observed = normalize_report_period(reading.observed_at)[0]
            if not observed:
                reading = replace(reading, normalization_error=reading.normalization_error or "time_unknown")
            elif observed not in session_window:
                reading = replace(reading, normalization_error=reading.normalization_error or
                                  ("time_in_future" if observed > as_of else "stale"))
            valuation[field] = reading
        industry = _parse_industry(data, code)
        # issue #71：行业截止日期晚于严格日历请求截至日 = 供应商返回了未来日期，
        # 不可作为当前行业采用：保留实际返回日期，记明确拒绝原因（不填请求日期、
        # 不补造分类/版本），后续消费层经既有 errors 检查回落健康 Choice 补充。
        industry_as_of = str(industry.get("as_of") or "")
        if industry_as_of and industry_as_of > as_of:
            industry = {**industry, "errors": [*(industry.get("errors") or []), "industry_time_in_future"]}
        if not valuation and not industry.get("name"):
            raise SourceReadError("ifind", "invalid_response")
        return {"valuation": valuation, "industry": industry, "requested_date": as_of}

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
        from data_provider.choice_adapter import ChoiceAdapter
        data = self.query("get_stock_info", f"{ChoiceAdapter.stock_code(code)} 所属概念", deadline=deadline)
        return _parse_concepts(data, code)


    def fetch_main_inflow_series(self, code: str, days: int = 12, *, deadline: float | None = None) -> Optional[List[Tuple[str, float]]]:
        if not self.available:
            return None
        # 问句与已验证的逐日契约同语义（root 真实探针）：每个交易日逐行 + 元单位
        # + 不要累计/估算/填充；严格解析层另行强制证券身份与单日字段身份。
        question = (f"{code} 近{days}个交易日内每个交易日的主力净流入额，按交易日期逐行列出；"
                    "明确真实证券代码、实际交易日期、人民币元金额单位与统计口径；"
                    "不要累计值，不要估算或前向填充。")
        data = self.query("get_stock_performance", question, deadline=deadline)
        return _parse_main_inflow_series(data, code)


def _parse_main_inflow_series(data: IfindData, code: str) -> List[Tuple[str, float]]:
    """严格逐日主力净流入序列：证券身份、金额单位、日期与冲突校验后才返回。

    只接受 证券代码+日期+主力净流入额（单位：元） 形态的行；返回证券与请求
    证券不符、金额列缺失单位或带量/股、行级币种与人民币冲突、同日金额冲突
    一律按无效响应拒绝，不静默降级（issue #71 root 反例边界）。周末空白行
    不产生数据行；金额倍率（万/亿）经既有归一化只换算一次。
    """
    series: List[Tuple[str, float]] = []
    seen: Dict[str, float] = {}
    matched = False
    rejected_other_security = False
    # issue #71：逐日金额字段按正向身份确认——去单位注解/空白后的实际 bare 必须
    # 精确等于已确认的单日字段「主力净流入额」；近N日/累计等任何其他口径列因此
    # 天然不匹配（root 反例：近5日/近20日主力净流入额曾进入逐日窗口合计），
    # 不维护禁词清单。
    daily_field_identity = "主力净流入额"
    for table in _ifind_tables(data.answer):
        if not table:
            continue
        headers = list(table[0].keys())
        if "证券代码" not in headers:
            # 无证券身份列的表不能证明行归属：整体拒绝，不挑行采用
            raise SourceReadError("ifind", "invalid_response")
        value_column = next((h for h in headers
                             if re.sub(r"[（(][^()（）]*单位[^()（）]*[）)]", "", h).strip() == daily_field_identity), None)
        date_column = next((h for h in headers if h.strip() == "日期"), None)
        if value_column is None or date_column is None:
            continue
        annotation = re.search(r"单位[：:]\s*([^）)；|]+)", value_column)
        column_unit = annotation[1].strip() if annotation else None
        bare = re.sub(r"[（(][^()（）]*单位[^()（）]*[）)]", "", value_column).strip()
        declared = (str(data.indicators_params.get(bare, {}).get("单位") or "").strip() or column_unit or "").strip()
        if declared != "元":
            # 无法证明金额单位（缺单位、量/股、外币计价列）一律拒绝
            raise SourceReadError("ifind", "invalid_response")
        currency_raw = str(data.indicators_params.get(bare, {}).get("币种") or "").strip()
        if currency_raw and currency_raw not in {"人民币", "人民币元", "CNY", "RMB"}:
            raise SourceReadError("ifind", "invalid_response")
        for row in table:
            if not _same_security(row.get("证券代码"), code):
                rejected_other_security = True
                continue
            row_currency = str(row.get("currency") or "").strip()
            if row_currency and row_currency not in {"人民币", "人民币元", "CNY", "RMB"}:
                raise SourceReadError("ifind", "invalid_response")
            raw_date = normalize_report_period(row.get(date_column))[0]
            if raw_date is None or not str(row.get(value_column, "")).strip():
                continue
            value, _unit, _currency, error = _ifind_number(str(row[value_column]), value_column, declared, "main_inflow")
            if value is None or error:
                raise SourceReadError("ifind", "invalid_response")
            if raw_date in seen and seen[raw_date] != value:
                raise SourceReadError("ifind", "invalid_response")
            matched = True
            seen[raw_date] = value
            series.append((raw_date, value))
    if not matched or (rejected_other_security and not series):
        raise SourceReadError("ifind", "invalid_response")
    return series


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
