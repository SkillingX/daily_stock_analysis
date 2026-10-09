# -*- coding: utf-8 -*-
"""Cross-source evidence for the adopted anchor value.

All comparable pairs participate, including same-family and unknown-family readings.
Only a proven family independent of the main reading supports verified/high/true.
Source failures are isolated and disclosed; conflicts never replace the adopted value.
Financial comparisons require confirmed period, basis and definition. Quote comparisons
require supplier observation time within the existing realtime cache window.
"""

from __future__ import annotations

import logging
import math
import re
import json
from inspect import signature
from concurrent.futures import ThreadPoolExecutor, wait
from contextvars import copy_context
from dataclasses import asdict, dataclass, field as dataclass_field, fields as dataclass_fields, replace
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, Overflow
from time import monotonic
from threading import BoundedSemaphore
from typing import Annotated, Any, Dict, Literal, Optional, Protocol, Sequence, Tuple
from icontract import ensure
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# 验证模式
# ------------------------------------------------------------------
MODE_NUMERIC = "numeric"  # 数值容差比对（PE/PB/市值/营收/净利/ROE/当前价/融资余额）
MODE_DIRECTION = "direction"  # 方向+量级比对（主力净流入；两源算法口径不同）
PeriodBasis = Literal["annual", "YTD", "single_quarter", "unknown"]
FINANCIAL_ANCHORS = frozenset({"revenue", "net_profit", "roe", "gross_margin", "revenue_yoy", "net_profit_yoy"})
PERCENTAGE_ANCHORS = frozenset({"roe", "gross_margin", "revenue_yoy", "net_profit_yoy"})
MONEY_ANCHORS = frozenset({"current_price", "total_mv", "circ_mv", "revenue", "net_profit", "main_inflow", "margin_balance"})
Unit = Literal["currency_base", "percentage_point", "multiple", "shares"]
_CURRENCY_MARKERS = {
    "CNY": ("CNY", "RMB", "人民币"), "USD": ("USD", "美元"),
    "HKD": ("HKD", "港元", "港币"), "EUR": ("EUR", "欧元"),
    "JPY": ("JPY", "日元"), "GBP": ("GBP", "英镑"),
    "SGD": ("SGD", "新加坡元"), "AUD": ("AUD", "澳元"),
    "CAD": ("CAD", "加元"), "CHF": ("CHF", "瑞士法郎"),
}
_SOURCE_FAMILIES = {"mx": "eastmoney", "mx_mcp": "eastmoney", "ifind": "ths", "fuyao": "ths", "efinance": "eastmoney"}
QualityStatus = Literal["missing", "unverified", "single_source", "not_comparable", "verified", "conflict"]


class AnchorQuality(BaseModel):
    """Stable, low sensitivity quality state at the context output boundary."""
    model_config = ConfigDict(strict=True, frozen=True, validate_assignment=True)
    status: QualityStatus = "unverified"
    reason_codes: tuple[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")], ...] = ()


@ensure(lambda result: result[0] is None or math.isfinite(result[0]), "JSON numeric values must be finite")
@ensure(lambda result: result[3] is None or result[1] is None, "Invalid normalization cannot declare a canonical unit")
def normalize_anchor_value(
    field: str, raw: object, label: str = "", *, unit: Optional[str] = None,
    currency: Optional[str] = None,
) -> tuple[Optional[float], Optional[Unit], Optional[str], Optional[str]]:
    """Normalize supplier scales with Decimal; missing evidence never implies a unit."""
    text = str(raw).strip().replace(",", "")
    number = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)(.*)", text)
    if isinstance(raw, bool) or number is None:
        return None, None, currency, "invalid_value"
    try:
        value = Decimal(number[1])
    except InvalidOperation:
        return None, None, currency, "invalid_value"
    scales = {"": Decimal(1), "千": Decimal(1000), "万": Decimal(10000), "百万": Decimal(1000000), "亿": Decimal(100000000), "十亿": Decimal(1000000000), "百亿": Decimal(10000000000), "千亿": Decimal(100000000000), "万亿": Decimal(1000000000000)}

    def declared(text: str, *, standalone: bool = False) -> tuple[Optional[Unit], Decimal]:
        if text in ("currency_base", "percentage_point", "multiple", "shares"):
            return text, Decimal(1)  # type: ignore[return-value]
        if "ratio" in text.lower() or "比例" in text:
            return "percentage_point", Decimal(100)
        if "%" in text or "％" in text or "百分点" in text:
            return "percentage_point", Decimal(1)
        if "倍" in text:
            return "multiple", Decimal(1)
        if not standalone:
            annotations = [part for part in re.findall(r"[（(]([^（）()]*)[）)]", text)
                           if re.search(r"元|股|英镑|法郎", part)]
            if annotations:
                declared_units = [declared(part, standalone=True) for part in annotations]
                return declared_units[0] if all(item == declared_units[0] for item in declared_units) else (None, Decimal(1))
        currency_names = "|".join(re.escape(marker) for markers in _CURRENCY_MARKERS.values() for marker in markers)
        money_units = "|".join(
            re.escape(marker) for marker in (
                "人民币元", *(marker for markers in _CURRENCY_MARKERS.values() for marker in markers
                            if not marker.isascii() and marker not in {"人民币", "港币"})
            )
        )
        pattern = (
            rf"(?:(?:单位|单季|累计|公布值)[\s,:：，]*)*(?:(?:{currency_names})\s*)?"
            rf"(万亿|千亿|百亿|十亿|百万|亿|万|千)?\s*({money_units}|元|股)"
            rf"(?:[\s,，]*(?:{currency_names}|单季|累计|公布值))*"
        )
        match = re.fullmatch(pattern, text.strip(), re.IGNORECASE) if standalone else re.search(pattern, text, re.IGNORECASE)
        if match:
            if not standalone and re.search(
                r"(?:[一二三四五六七八九零十百\d]|万亿|千亿|百亿|十亿|百万|亿|万|千|million|billion|trillion|thousand)\s*[^()（）,，:：\s]*$",
                text[:match.start()], re.IGNORECASE,
            ):
                return None, Decimal(1)
            return ("shares" if match[2] == "股" else "currency_base"), scales[match[1] or ""]
        if text.strip() in scales and text.strip():
            return "currency_base", scales[text.strip()]
        return None, Decimal(1)

    header = declared(label)
    explicit = declared(unit, standalone=True) if unit is not None else header
    cell = declared(number[2], standalone=True)
    currencies = {code for code, markers in _CURRENCY_MARKERS.items() if any(marker in f"{label} {number[2]} {unit or ''}".upper() for marker in markers)}
    error: Optional[str] = None
    if currency is not None:
        explicit_currency = str(currency).strip().upper()
        if explicit_currency in _CURRENCY_MARKERS:
            currency = explicit_currency
            currencies.add(currency)
        else:
            currency = None
            error = "currency_unknown"
    if len(currencies) > 1:
        error = "currency_conflict"
    elif currency is None and len(currencies) == 1:
        currency = next(iter(currencies))
    if field not in MONEY_ANCHORS:
        currency, error = None, None
    if unit is not None and header[0] is not None and explicit != header:
        error = "unit_conflict"
    header = explicit
    if header[0] is not None and cell[0] is not None and header != cell:
        error = "unit_conflict"
    canonical, factor = cell if cell[0] is not None else header
    if canonical is None and field in {"pe_ratio", "pb_ratio"} and not number[2].strip() and unit is None:
        canonical = "multiple"
    expected: Unit = "percentage_point" if field in PERCENTAGE_ANCHORS else "multiple" if field in {"pe_ratio", "pb_ratio"} else "currency_base"
    if canonical != expected:
        error = error or ("unit_unknown" if canonical is None else "unit_incompatible")
    if number[2].strip() and cell[0] is None:
        error = error or "unit_unknown"
    if unit is not None and header[0] is None:
        error = error or "unit_unknown"
    try:
        normalized = float(value * factor)
    except (InvalidOperation, Overflow, OverflowError):
        return None, None, currency, "invalid_value"
    if not math.isfinite(normalized):
        return None, None, currency, "invalid_value"
    return normalized, canonical if error is None else None, currency, error


def normalize_report_period(value: object) -> tuple[Optional[str], PeriodBasis]:
    """Normalize an explicit response period; query text is never period evidence."""
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d"), "unknown"
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        return None, "unknown"
    text = value.strip()
    chinese = re.fullmatch(r"(\d{4})\s*(?:年\s*)?(一季报|一季度报告|中报|半年报|半年度报告|三季报|三季度报告|年报|年度|年度报告)", text)
    if chinese:
        suffix = {"一季报": "03-31", "一季度报告": "03-31", "中报": "06-30", "半年报": "06-30", "半年度报告": "06-30", "三季报": "09-30", "三季度报告": "09-30", "年报": "12-31", "年度": "12-31", "年度报告": "12-31"}[chinese[2]]
        return f"{chinese[1]}-{suffix}", "annual" if suffix == "12-31" else "YTD"
    pieces = text.split()
    basis: PeriodBasis = "unknown"
    if len(pieces) == 2 and pieces[1] in ("annual", "YTD", "single_quarter"):
        basis = pieces[1]  # type: ignore[assignment]
        text = pieces[0]
    if re.fullmatch(r"\d{8}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    match = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})([ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)?", text)
    if match is None:
        return None, "unknown"
    try:
        parsed = datetime.fromisoformat(f"{match[1]}-{int(match[2]):02d}-{int(match[3]):02d}{match[4] or ''}")
    except ValueError:
        return None, "unknown"
    return parsed.date().isoformat(), basis


def report_period_from_fields(fields: Dict[str, Any], label: str = "") -> tuple[Optional[str], PeriodBasis]:
    """Read explicit response metadata, excluding request/query text."""
    raw = next((fields[key] for key in ("period_end", "report_period", "报告期", "期末日期", "report_date", "报告日期") if fields.get(key) is not None), None)
    actual, basis = normalize_report_period(raw)
    explicit = fields.get("period_basis")
    if explicit is None:
        if "单季" in label or "single_quarter" in label:
            explicit = "single_quarter"
        elif "累计" in label or "YTD" in label:
            explicit = "YTD"
    if explicit == "annual":
        basis = "annual"
    elif explicit in ("YTD", "累计"):
        basis = "YTD"
    elif explicit in ("single_quarter", "单季"):
        basis = "single_quarter"
    return actual, basis


def observation_time_from_fields(fields: Dict[str, Any], column: object = None) -> Optional[str]:
    """Keep supplier observation timestamps separate from financial report dates."""
    value = next((fields[key] for key in ("observed_at", "provider_timestamp", "数据时间", "时间", "日期") if fields.get(key) is not None), None)
    if value is not None:
        return str(value)
    if isinstance(column, str):
        actual, basis = normalize_report_period(re.sub(r"[（(]日[）)]$", "", column))
        if actual is not None and basis == "unknown":
            return column
    return None


def select_report_period(periods: Sequence[tuple[Optional[str], PeriodBasis]], requested: Optional[str] = None) -> Optional[int]:
    """Select a confirmed requested period, otherwise the confirmed latest period."""
    confirmed = [(i, end, basis) for i, (end, basis) in enumerate(periods) if end is not None]
    target, target_basis = normalize_report_period(requested)
    matches = [item for item in confirmed if item[1] == target and (target_basis == "unknown" or item[2] in (target_basis, "unknown"))]
    if matches:
        return matches[0][0]
    return max(confirmed, key=lambda item: item[1])[0] if confirmed else None


def caliber_from_label(field: str, label: str) -> Optional[str]:
    """Use the returned field identity, never the natural-language request."""
    text = re.sub(r"[\s()（）,，:：]", "", label.rsplit(".", 1)[-1]).lower()
    currencies = "|".join(re.escape(marker.lower()) for markers in _CURRENCY_MARKERS.values() for marker in markers)
    text = re.sub(currencies, "", text)
    text = re.sub(
        r"单位|公布值|单季|累计|年度|全年|annual|ytd|single_quarter|percentage_point|ratio|百分点|[%％]|倍|"
        r"万亿|千亿|百亿|十亿|百万|亿|万|千|元", "", text,
    )
    if field == "pe_ratio":
        return "TTM" if re.fullmatch(r"(?:市盈率(?:pe)?|市盈|pe)(?:ttm|滚动)|(?:ttm|滚动)(?:市盈率|pe)|pe_ttm", text) else None
    if field == "pb_ratio":
        return "MRQ" if re.fullmatch(r"(?:市净率(?:pb)?|市净|pb)mrq|mrq(?:市净率|pb)|pb_mrq", text) else None
    suffix = r"(?:同比|yoy)(?:增长率|增长|增速|变化率)?" if field.endswith("_yoy") else ""
    if field in {"net_profit", "net_profit_yoy"}:
        parent_profit = r"(?:归母净利润|归属(?:于)?母公司(?:股东|所有者)?的?净利润|母公司(?:股东|所有者)净利润|parent_holder_net_profit)"
        if re.fullmatch(parent_profit + suffix, text):
            return "parent_net_profit_yoy" if field.endswith("yoy") else "parent_net_profit"
        return None
    if field == "roe":
        return "weighted_roe" if re.fullmatch(r"(?:净资产收益率(?:roe)?|roe)(?:加权(?:平均)?|weighted)|(?:加权(?:平均)?|weighted)(?:净资产收益率(?:roe)?|roe)|weighted_roe", text) else None
    if field == "gross_margin":
        return "gross_margin" if re.fullmatch(r"(?:销售)?毛利率|gross_margin", text) else None
    if field in {"revenue", "revenue_yoy"}:
        if re.fullmatch(r"(?:营业总收入|营业收入|营收)" + suffix, text):
            stem = "total_operating_revenue" if text.startswith("营业总收入") else "operating_revenue"
            return stem + "_yoy" if field.endswith("yoy") else stem
    return None


@dataclass(frozen=True)
class AnchorSpec:
    """单个锚点的验证规格。"""

    field: str  # 标准字段名，如 "pe_ratio"
    mode: str  # numeric | direction
    tolerance_pct: float  # numeric 模式的相对容差（百分比，10.0 = ±10%）
    caliber_aware: bool = (
        True  # 是否要求口径一致才做数值比对（财务/估值 True；行情/资金 False）
    )


# 锚点规格表（对齐方案第三节）。key = 标准字段名。
ANCHOR_SPECS: Dict[str, AnchorSpec] = {
    "current_price": AnchorSpec(
        "current_price", MODE_NUMERIC, 1.0, caliber_aware=False
    ),
    "pe_ratio": AnchorSpec("pe_ratio", MODE_NUMERIC, 10.0),
    "pb_ratio": AnchorSpec("pb_ratio", MODE_NUMERIC, 10.0),
    "total_mv": AnchorSpec("total_mv", MODE_NUMERIC, 5.0),
    "circ_mv": AnchorSpec("circ_mv", MODE_NUMERIC, 5.0),
    "revenue": AnchorSpec("revenue", MODE_NUMERIC, 3.0),
    "net_profit": AnchorSpec("net_profit", MODE_NUMERIC, 3.0),
    "roe": AnchorSpec("roe", MODE_NUMERIC, 3.0),
    # 保留既有派生指标容差；财务期间与定义由统一判定检查。
    "gross_margin": AnchorSpec("gross_margin", MODE_NUMERIC, 15.0, caliber_aware=False),
    "revenue_yoy": AnchorSpec("revenue_yoy", MODE_NUMERIC, 20.0, caliber_aware=False),
    # 净利润同比：派生指标，波动较大，容差放宽至 25%
    "net_profit_yoy": AnchorSpec(
        "net_profit_yoy", MODE_NUMERIC, 25.0, caliber_aware=False
    ),
    # 融资余额：交易所每日确定数据，应严格一致
    "margin_balance": AnchorSpec(
        "margin_balance", MODE_NUMERIC, 0.5, caliber_aware=False
    ),
    # 主力净流入：东财/同花顺算法口径不同，比对方向非数值
    "main_inflow": AnchorSpec("main_inflow", MODE_DIRECTION, 0.0, caliber_aware=False),
}


# ------------------------------------------------------------------
# 数据结构（不可变）
# ------------------------------------------------------------------
@dataclass(frozen=True)
class AnchorReading:
    """单个数据源对某锚点的一次读数。"""

    source: str  # "mx" | "ifind" | "akshare" | ...
    value: float
    caliber: Optional[str] = None  # 口径，如 "TTM"/"static"；None=未知/不适用
    period: Optional[str] = None  # 报告期，如 "2024年报"；None=不适用（行情/资金）
    requested_period: Optional[str] = None
    period_basis: PeriodBasis = "unknown"
    observed_at: Optional[str] = None
    fetched_at: Optional[str] = None
    unit: Optional[Unit] = None
    currency: Optional[str] = None
    raw_value: Optional[str] = None
    raw_label: Optional[str] = None
    raw_unit: Optional[str] = None
    raw_currency: Optional[str] = None
    normalization_error: Optional[str] = None
    source_family: Optional[str] = None
    is_stale: Optional[bool] = None

    def __post_init__(self) -> None:
        actual, inferred_basis = normalize_report_period(self.period)
        object.__setattr__(self, "period", actual)
        if self.period_basis == "unknown":
            object.__setattr__(self, "period_basis", inferred_basis)
        if self.source_family is None:
            object.__setattr__(self, "source_family", _SOURCE_FAMILIES.get(self.source.lower()))


@dataclass(frozen=True)
class AnchorVerification:
    """某锚点的跨源验证结果（不可变，进 LLM 上下文前再 compact）。"""

    field: str
    value: Optional[float]  # 采纳值（主源值）；缺失/未知锚点为 None（不编造 0）
    confidence: str  # high | medium | low
    sources: Tuple[str, ...]
    agreed: bool  # 是否通过判定（容差内 / 方向一致）
    discrepancy_pct: Optional[float] = None
    caliber: Optional[str] = None
    period: Optional[str] = None
    note: str = ""
    requested_period: Optional[str] = None
    period_basis: PeriodBasis = "unknown"
    observed_at: Optional[str] = None
    fetched_at: Optional[str] = None
    unit: Optional[Unit] = None
    currency: Optional[str] = None
    raw_value: Optional[str] = None
    raw_label: Optional[str] = None
    raw_unit: Optional[str] = None
    raw_currency: Optional[str] = None
    normalization_error: Optional[str] = None
    quality: AnchorQuality = dataclass_field(default_factory=AnchorQuality)
    readings: tuple[AnchorReading, ...] = ()
    reading_reasons: tuple[tuple[str, ...], ...] = ()
    conflicts: tuple[tuple[str, str], ...] = ()
    source_errors: tuple[tuple[str, str], ...] = ()

    def to_compact(self) -> Dict[str, Any]:
        """压缩为 LLM 友好的 dict（省 token）。"""
        payload: Dict[str, Any] = {
            "v": self.value,
            "conf": self.confidence,
            "src": list(self.sources),
            "agreed": self.agreed,
            "quality": self.quality.model_dump(mode="json"),
        }
        if self.discrepancy_pct is not None:
            payload["diff"] = round(self.discrepancy_pct, 2)
        if self.caliber:
            payload["caliber"] = self.caliber
        if self.period:
            payload["period"] = self.period
        if self.requested_period is not None:
            payload["requested_period"] = self.requested_period
        if self.period is not None:
            payload["period_basis"] = self.period_basis
        if self.observed_at is not None:
            payload["observed_at"] = self.observed_at
        if self.fetched_at is not None:
            payload["fetched_at"] = self.fetched_at
        if self.note:
            payload["note"] = self.note
        for key in ("unit", "currency", "raw_value", "raw_label", "raw_unit", "raw_currency", "normalization_error"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        conflicted = {name for pair in self.conflicts for name in pair}
        payload["readings"] = [
            {**asdict(reading), "relation": "conflict" if reading.source in conflicted else "not_comparable" if reasons else "single_source" if len(self.readings) == 1 else "consistent", "reason_codes": list(reasons) + (["family_unknown"] if reading.source_family is None else [])}
            for reading, reasons in zip(self.readings, self.reading_reasons)
        ]
        payload["conflicts"] = [list(pair) for pair in self.conflicts]
        payload["source_errors"] = [{"source": name, "reason_code": reason} for name, reason in self.source_errors]
        return payload


# ------------------------------------------------------------------
# SourceAdapter Protocol（N 源可插拔）
# ------------------------------------------------------------------
class SourceAdapter(Protocol):
    """数据源适配器协议。实现 ``read`` 即可被 validator 调用。"""

    name: str

    def read(
        self, code: str, field: str, period: Optional[str] = None, *, deadline: Optional[float] = None
    ) -> Optional[AnchorReading]:
        """读取某股票某锚点。失败返回 None（fail-open），不抛异常。"""
        ...


# ------------------------------------------------------------------
# 判定纯函数
# ------------------------------------------------------------------

# 股票代码白名单（code 经自然语言查询送外部 API，拒绝非法格式防注入 + 防配额浪费）。
# A 股 6 位数字、港股 5 位数字、美股 1-6 位字母、可带 SH/SZ/HK 前缀或 .SH 后缀。
_CODE_RE = re.compile(
    r"^(?:SH|SZ|BJ|sh|sz|bj)?\d{5,6}$|^(?:HK|hk)?\d{5}$|^[A-Za-z]{1,6}$|^\d{5,6}\.(SH|SZ|BJ|HK)$"
)


def _is_valid_code(code: str) -> bool:
    """校验股票代码格式。非法/空 → False（验证层直接 fail-open missing）。"""
    if not isinstance(code, str) or not code.strip():
        return False
    # 去掉常见前缀后再校验纯代码部分
    return bool(_CODE_RE.match(code.strip()))


@ensure(lambda result: 0.0 <= result <= 200.0, "Relative difference on finite readings cannot exceed 200 percent")
def _discrepancy_pct(a: float, b: float) -> float:
    """相对差异百分比（以较大绝对值为基准）。"""
    left, right = Decimal(str(a)), Decimal(str(b))
    base = max(abs(left), abs(right))
    if base == 0:
        return 0.0
    return float(abs(left - right) / base * Decimal(100))


def _within_tolerance(a: float, b: float, tol_pct: float) -> bool:
    """相对容差判定。"""
    left, right = Decimal(str(a)), Decimal(str(b))
    return abs(left - right) * Decimal(100) <= Decimal(str(tol_pct)) * max(abs(left), abs(right))


def _magnitude_tier(value: float) -> int:
    """金额量级档位 = floor(log10(|value|))。亿=8、千万=7、万=4。

    相邻档（差 ≤1）视为同档，用于主力净流入「量级同档」判定。
    """
    if value == 0:
        return 0
    return int(math.floor(math.log10(abs(value))))


def reading_input_reasons(reading: AnchorReading, field: str) -> tuple[str, ...]:
    """Intrinsic input validity; unknown financial periods still allow limited local use."""
    if not math.isfinite(reading.value):
        return ("invalid_value",)
    if reading.is_stale:
        return ("stale",)
    if reading.normalization_error:
        return (reading.normalization_error,)
    expected = "percentage_point" if field in PERCENTAGE_ANCHORS else "multiple" if field in {"pe_ratio", "pb_ratio"} else "currency_base"
    if reading.unit is None:
        return ("unit_unknown",)
    if reading.unit != expected:
        return ("unit_incompatible",)
    if field in MONEY_ANCHORS and reading.currency not in _CURRENCY_MARKERS:
        return ("currency_unknown",)
    if field in FINANCIAL_ANCHORS | {"pe_ratio", "pb_ratio"} and reading.caliber is None:
        return ("caliber_unknown",)
    if field == "pe_ratio" and reading.caliber != "TTM":
        return ("pe_not_ttm",)
    if field == "roe" and reading.caliber != "weighted_roe":
        return ("roe_method_unknown",)
    if field == "pb_ratio" and reading.caliber != "MRQ":
        return ("pb_method_unknown",)
    if field == "revenue_yoy" and reading.caliber not in {"operating_revenue_yoy", "total_operating_revenue_yoy"}:
        return ("revenue_definition_unknown",)
    if field == "net_profit_yoy" and reading.caliber != "parent_net_profit_yoy":
        return ("profit_definition_unknown",)
    return ()


def reading_from_field_record(value: object, record: Optional[Dict[str, Any]] = None) -> Optional[AnchorReading]:
    """Decode current field evidence; stale metadata cannot label a different scalar."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    record = record or {}
    if record.get("value", value) != value:
        record = {}
    metadata = {item.name: record[item.name] for item in dataclass_fields(AnchorReading) if item.name in record and item.name not in {"source", "value"}}
    payload = {"source": str(record.get("source") or "unknown"), "value": float(value), **metadata}
    try:
        return TypeAdapter(AnchorReading).validate_json(json.dumps(payload), strict=True)
    except (ValidationError, TypeError, ValueError):
        return AnchorReading("unknown", float(value), normalization_error="metadata_invalid")


def adopted_field_record(field: str, reading: AnchorReading, quality: Optional[AnchorQuality] = None, selection_reason: str = "primary", *, previous_record: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One field record projects its scalar and consumer eligibility from existing evidence."""
    previous = previous_record or {}
    current_conflict = previous.get("value") == reading.value and (previous.get("quality") or {}).get("status") == "conflict"
    quality = quality or AnchorQuality(status="conflict" if current_conflict else "unverified")
    reasons = reading_input_reasons(reading, field)
    if field not in FINANCIAL_ANCHORS and field in ANCHOR_SPECS and reading.observed_at is not None:
        from src.config import get_config
        limits = _comparison_reasons(reading, ANCHOR_SPECS[field], None, get_config().realtime_cache_ttl)
        reasons += tuple(reason for reason in limits if reason in {"stale", "time_in_future"} and reason not in reasons)
    return {
        "field": field, **asdict(reading), "quality": quality.model_dump(mode="json"),
        "input_reasons": list(reasons), "rule_eligible": not reasons and quality.status != "conflict",
        "selection_reason": selection_reason,
    }


def _comparison_reasons(reading: AnchorReading, spec: AnchorSpec, code: Optional[str], ttl: int) -> tuple[str, ...]:
    reasons = reading_input_reasons(reading, spec.field)
    if reasons:
        return reasons
    if spec.field in FINANCIAL_ANCHORS:
        if reading.period is None:
            return ("period_unknown",)
        if reading.period_basis == "unknown":
            return ("period_basis_unknown",)
        return ()
    if reading.observed_at is None:
        return ("time_unknown",)
    from src.core.trading_calendar import get_effective_trading_date, get_market_for_stock, get_market_now

    market = get_market_for_stock(code or "")
    now = get_market_now(market) if market else datetime.now(timezone.utc)
    text = re.sub(r"[（(]日[）)]$", "", reading.observed_at)
    try:
        observed = datetime.fromisoformat(text.replace("/", "-"))
    except ValueError:
        actual, _basis = normalize_report_period(text)
        if actual is None:
            return ("time_unknown",)
        observed = datetime.fromisoformat(actual)
    if observed.tzinfo is None:
        if market is None and ":" in text:
            return ("time_zone_unknown",)
        observed = observed.replace(tzinfo=now.tzinfo)
    if spec.field == "margin_balance":
        return () if observed.astimezone(now.tzinfo).date() == get_effective_trading_date(market, current_time=now) else ("stale",)
    if ":" not in text:
        return ("time_unknown",)
    age = (now - observed).total_seconds()
    if age < 0:
        return ("time_in_future",)
    return ("stale",) if age > ttl else ()


def _pair_reasons(left: AnchorReading, right: AnchorReading, spec: AnchorSpec) -> tuple[str, ...]:
    if left.unit != right.unit:
        return ("unit_mismatch",)
    if spec.field in MONEY_ANCHORS and left.currency != right.currency:
        return ("currency_mismatch",)
    if spec.field in FINANCIAL_ANCHORS:
        if left.period != right.period:
            return ("period_mismatch",)
        if left.period_basis != right.period_basis:
            return ("period_basis_mismatch",)
    if (spec.caliber_aware or spec.field in FINANCIAL_ANCHORS) and left.caliber != right.caliber:
        return ("caliber_mismatch",)
    return ()


@ensure(lambda result: result.agreed == (result.quality.status == "verified"), "Only verified evidence may claim agreement")
@ensure(lambda result: result.confidence == ("high" if result.quality.status == "verified" else "low" if result.quality.status in {"conflict", "missing"} else "medium"), "Quality and legacy confidence must describe the same decision")
def _judge_readings(
    readings: Sequence[AnchorReading], spec: AnchorSpec, *, enabled: bool = True,
    code: Optional[str] = None, ttl: Optional[int] = None,
    source_errors: tuple[tuple[str, str], ...] = (),
) -> AnchorVerification:
    """Check all semantically relevant pairs; family controls support, never membership."""
    from src.config import get_config
    ttl = int(get_config().realtime_cache_ttl) if ttl is None else ttl
    if not readings:
        return AnchorVerification(spec.field, None, "low", (), False, note="所有数据源均无该锚点数据", quality=AnchorQuality(status="missing", reason_codes=("missing",)), source_errors=source_errors)
    primary = readings[0]
    reasons = [_comparison_reasons(reading, spec, code, ttl) for reading in readings]
    if spec.mode == MODE_DIRECTION:
        reasons = [reason or (("direction_zero",) if reading.value == 0 else ()) for reading, reason in zip(readings, reasons)]
    for index, reading in enumerate(readings[1:], 1):
        # Known main semantics constrain relevance; missing evidence does not hide other explicit mismatches.
        mismatches: list[str] = []
        if primary.period is not None and reading.period is not None and spec.field in FINANCIAL_ANCHORS and reading.period != primary.period:
            mismatches.append("period_mismatch")
        if primary.caliber is not None and reading.caliber is not None and (spec.caliber_aware or spec.field in FINANCIAL_ANCHORS) and reading.caliber != primary.caliber:
            mismatches.append("caliber_mismatch")
        if primary.unit is not None and reading.unit is not None and reading.unit != primary.unit:
            mismatches.append("unit_mismatch")
        if primary.currency is not None and reading.currency is not None and spec.field in MONEY_ANCHORS and reading.currency != primary.currency:
            mismatches.append("currency_mismatch")
        if primary.period_basis != "unknown" and reading.period_basis != "unknown" and spec.field in FINANCIAL_ANCHORS and reading.period_basis != primary.period_basis:
            mismatches.append("period_basis_mismatch")
        reasons[index] += tuple(mismatches)
    group = [index for index, reason in enumerate(reasons) if not reason]
    conflicts: list[tuple[str, str]] = []
    limitations: set[str] = set()
    differences: list[float] = []
    for position, left_index in enumerate(group):
        for right_index in group[position + 1:]:
            left, right = readings[left_index], readings[right_index]
            pair_reasons = _pair_reasons(left, right, spec)
            if pair_reasons:
                limitations.update(pair_reasons)
                reasons[left_index] += pair_reasons
                reasons[right_index] += pair_reasons
                continue
            differences.append(_discrepancy_pct(left.value, right.value))
            if spec.mode == MODE_DIRECTION:
                if left.value and right.value and (left.value > 0) != (right.value > 0):
                    conflicts.append((left.source, right.source))
                elif left.value == 0 or right.value == 0:
                    limitations.add("direction_zero")
                elif abs(_magnitude_tier(left.value) - _magnitude_tier(right.value)) > 1:
                    limitations.add("magnitude_mismatch")
                    reasons[left_index] += ("magnitude_mismatch",)
                    reasons[right_index] += ("magnitude_mismatch",)
            elif not _within_tolerance(left.value, right.value, spec.tolerance_pct):
                conflicts.append((left.source, right.source))
    independent = primary.source_family is not None and any(
        index != 0 and readings[index].source_family is not None and readings[index].source_family != primary.source_family
        and not _pair_reasons(primary, readings[index], spec) for index in group
    )
    status: QualityStatus
    if not enabled:
        status = "unverified"
    elif conflicts:
        status = "conflict"
    elif len(readings) == 1 and primary.source_family is not None and not reading_input_reasons(primary, spec.field) and not {"stale", "time_in_future", "direction_zero"}.intersection(reasons[0]):
        status = "single_source"
    elif reasons[0]:
        status = "not_comparable"
    elif independent and not limitations:
        status = "verified"
    elif primary.source_family is not None and len(group) == len(readings) and all(reading.source_family == primary.source_family for reading in readings) and not limitations:
        status = "single_source"
    else:
        status = "not_comparable"
    codes = {reason for item in reasons for reason in item} | limitations | {reason for _, reason in source_errors}
    if any(reading.source_family is None for reading in readings):
        codes.add("family_unknown")
    if conflicts:
        codes.add("direction_conflict" if spec.mode == MODE_DIRECTION else "numeric_conflict")
    if status == "single_source":
        codes.add("single_family")
    if not enabled:
        codes.add("validation_disabled")
    messages = {"missing": "缺失", "unverified": "未核验", "single_source": "单源家族，未独立交叉验证", "not_comparable": "不可比或独立性未证明", "verified": "独立来源佐证通过", "conflict": "方向冲突" if spec.mode == MODE_DIRECTION else "数据冲突"}
    note = messages[status]
    if conflicts:
        note += "：" + "; ".join(f"{left}/{right}" for left, right in conflicts)
    if status == "verified" and spec.mode == MODE_DIRECTION:
        note += "（方向相同、量级差≤1档，不证明金额相同）"
    if codes:
        note += "；" + ", ".join(sorted(codes))
    return AnchorVerification(
        spec.field, primary.value, "high" if status == "verified" else "low" if status == "conflict" else "medium",
        tuple(reading.source for reading in readings), status == "verified", max(differences) if differences else None,
        primary.caliber, primary.period, note, requested_period=primary.requested_period, period_basis=primary.period_basis,
        observed_at=primary.observed_at, fetched_at=primary.fetched_at, unit=primary.unit, currency=primary.currency,
        raw_value=primary.raw_value, raw_label=primary.raw_label, raw_unit=primary.raw_unit, raw_currency=primary.raw_currency,
        normalization_error=primary.normalization_error, quality=AnchorQuality(status=status, reason_codes=tuple(sorted(codes))),
        readings=tuple(readings), reading_reasons=tuple(tuple(sorted(set(reason))) for reason in reasons), conflicts=tuple(conflicts), source_errors=source_errors,
    )


def _judge_numeric(primary: AnchorReading, secondary: AnchorReading, spec: AnchorSpec, tertiary: Optional[AnchorReading] = None) -> AnchorVerification:
    return _judge_readings((primary, secondary) if tertiary is None else (primary, secondary, tertiary), spec)


def _judge_direction(primary: AnchorReading, secondary: AnchorReading, field: str, tertiary: Optional[AnchorReading] = None) -> AnchorVerification:
    return _judge_readings((primary, secondary) if tertiary is None else (primary, secondary, tertiary), ANCHOR_SPECS[field])


def _judge_single(reading: AnchorReading, field: str) -> AnchorVerification:
    return _judge_readings((reading,), ANCHOR_SPECS[field])


def _judge_missing(field: str) -> AnchorVerification:
    """无任何源取到：low。value=None（诚实标注，不编造 0）。"""
    return AnchorVerification(
        field=field,
        value=None,
        confidence="low",
        sources=(),
        agreed=False,
        note="所有数据源均无该锚点数据", quality=AnchorQuality(status="missing", reason_codes=("missing",)),
    )


def _judge_unknown(field: str) -> AnchorVerification:
    """未知锚点规格：low，避免静默放过。value=None。"""
    return AnchorVerification(
        field=field,
        value=None,
        confidence="low",
        sources=(),
        agreed=False,
        note="未知锚点规格，未配置验证", quality=AnchorQuality(status="missing", reason_codes=("unknown_anchor",)),
    )


# ------------------------------------------------------------------
# CrossSourceValidator
# ------------------------------------------------------------------
class CrossSourceValidator:
    """跨源验证器（N 源可插拔）。

    用法::

        validator = CrossSourceValidator(sources=[mx_source, ifind_source])
        result = validator.verify("600519", "pe_ratio", period="2024年报")
        # result.confidence in {"high","medium","low"}
    """

    def __init__(
        self,
        sources: Sequence[SourceAdapter],
        specs: Optional[Dict[str, AnchorSpec]] = None,
        max_workers: int = 4,
    ) -> None:
        self._sources: Tuple[SourceAdapter, ...] = tuple(sources)
        self._specs: Dict[str, AnchorSpec] = (
            dict(specs) if specs else dict(ANCHOR_SPECS)
        )
        self._max_workers = max(1, int(max_workers))
        self._pool = ThreadPoolExecutor(max_workers=self._max_workers)
        self._slots = BoundedSemaphore(self._max_workers)

    def verify(
        self,
        code: str,
        field: str,
        period: Optional[str] = None,
        primary_reading: Optional[AnchorReading] = None,
        *, enabled: bool = True, deadline: Optional[float] = None, expected_currency: Optional[str] = None,
    ) -> AnchorVerification:
        """验证单个锚点；源异常隔离，保留失败证据。

        ``primary_reading``：可选，注入的主源读数（如行情类的 realtime_quote），
        置于读数列表首位作 primary；其余从 ``sources`` 收集作验证源。
        """
        spec = self._specs.get(field)
        if spec is None:
            return _judge_unknown(field)

        # 输入校验：code 经自然语言查询送外部 API，拒绝非法格式（防注入 + 防配额浪费）
        if not _is_valid_code(code):
            return _judge_missing(field)

        collected, errors = self._collect(code, field, period, deadline) if enabled else ((), ())
        readings = (
            (primary_reading, *collected) if primary_reading is not None else collected
        )

        if field in MONEY_ANCHORS and expected_currency is not None:
            readings = tuple(replace(reading, normalization_error="currency_mismatch") if reading.currency is not None and reading.currency != expected_currency else reading for reading in readings)

        valid = tuple(reading for reading in readings if math.isfinite(reading.value))
        if primary_reading is None:
            selected = next((reading for reading in valid if adopted_field_record(field, reading)["rule_eligible"]), None)
            if selected is not None:
                valid = (selected, *(reading for reading in valid if reading is not selected))
        errors += tuple((reading.source, "invalid_value") for reading in readings if not math.isfinite(reading.value))
        return _judge_readings(valid, spec, code=code, enabled=enabled, source_errors=errors)

    def _collect(
        self, code: str, field: str, period: Optional[str], deadline: Optional[float] = None,
    ) -> tuple[tuple[AnchorReading, ...], tuple[tuple[str, str], ...]]:
        """并行收集各源读数。任一源异常/返回 None 都被隔离（fail-open）。"""
        if not self._sources:
            return (), ()

        def _safe_read(source: SourceAdapter) -> tuple[Optional[AnchorReading], Optional[str]]:
            try:
                if deadline is not None and deadline <= monotonic():
                    return None, "timeout"
                kwargs = {"deadline": deadline} if deadline is not None and "deadline" in signature(source.read).parameters else {}
                reading = source.read(code, field, period, **kwargs)
                return reading, None if reading is not None else "timeout" if deadline is not None and deadline <= monotonic() else "missing"
            except Exception as exc:  # noqa: BLE001 — fail-open：源异常不影响其他源
                logger.debug(
                    "[CrossValidate] source %s read %s failed: %s",
                    source.name,
                    field,
                    exc,
                )
                return None, "timeout" if "timeout" in type(exc).__name__.lower() else "source_error"

        if deadline is not None and deadline <= monotonic():
            return (), tuple((source.name, "timeout") for source in self._sources)
        futures = []
        busy_errors = []
        for source in self._sources:
            if not self._slots.acquire(blocking=False):
                busy_errors.append((source.name, "source_busy"))
                continue
            future = self._pool.submit(copy_context().run, _safe_read, source)
            future.add_done_callback(lambda _: self._slots.release())
            futures.append((source, future))
        wait([future for _, future in futures], timeout=None if deadline is None else max(0.0, deadline - monotonic()))
        readings: list[AnchorReading] = []
        errors: list[tuple[str, str]] = list(busy_errors)
        for source, future in futures:
            if not future.done():
                future.cancel()
                errors.append((source.name, "timeout"))
                continue
            reading, error = future.result()
            if reading is not None:
                readings.append(reading)
            if error:
                errors.append((source.name, error))
        return tuple(readings), tuple(errors)
