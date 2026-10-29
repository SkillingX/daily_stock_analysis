# -*- coding: utf-8 -*-
"""
AkShare fundamental adapter (fail-open).

This adapter intentionally uses capability probing against multiple AkShare
endpoint candidates. It should never raise to caller; partial data is allowed.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from typing import cast  # added by mypy_codemod
from .cross_source_validator import AnchorReading, adopted_field_record, caliber_from_label, normalize_anchor_value, normalize_report_period, reading_input_reasons, report_period_from_fields, select_report_period

logger = logging.getLogger(__name__)

_DIVIDEND_KEYWORD_MAP: Dict[str, List[str]] = {
    "per_share": [
        "每股派息",
        "每股现金红利",
        "每股分红",
        "每股派现",
        "派现(元/股)",
        "派息(元/股)",
        "税前派息(元/股)",
        "现金分红(税前)",
    ],
    "plan_text": [
        "分配方案",
        "分红方案",
        "实施方案",
        "派息方案",
        "方案",
        "预案",
        "方案说明",
    ],
    "ex_dividend_date": ["除权除息日", "除息日", "除权日", "除权除息", "除息日期"],
    "record_date": ["股权登记日", "登记日"],
    "announce_date": ["公告日期", "公告日", "实施公告日", "预案公告日"],
    "report_date": ["报告期", "报告日期", "截止日期", "统计截止日期"],
}


def _safe_float(value: Any) -> Optional[float]:
    """Best-effort float conversion."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            number = float(value)
            return number if math.isfinite(number) else None
        except (TypeError, ValueError):
            return None
    s = str(value).strip().replace(",", "").replace("%", "")
    if not s:
        return None
    try:
        number = float(s)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _safe_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    try:
        parsed = pd.to_datetime(value)
    except Exception:
        return None
    if pd.isna(parsed):
        return None
    try:
        return cast(Optional[datetime], parsed.to_pydatetime())
    except Exception:
        return None


def _normalize_code(raw: Any) -> str:
    s = _safe_str(raw).upper()
    if "." in s:
        s = s.split(".", 1)[0]
    s = re.sub(r"^(SH|SZ|BJ)", "", s)
    return s


def _market_suffix(stock_code: str) -> Optional[str]:
    """Exchange suffix required by symbol-scoped endpoints (sh/sz/bj)."""
    code = _normalize_code(stock_code)
    if code.startswith(("60", "68", "90")):
        return "sh"
    if code.startswith(("00", "30")):
        return "sz"
    if code.startswith(("43", "83", "87", "92")):
        return "bj"
    return None


def _pick_by_keywords(row: pd.Series, keywords: List[str], exclude: Optional[set[str]] = None) -> Optional[Any]:
    """
    Return first non-empty row value whose column name contains any keyword.
    """
    for col in row.index:
        col_s = str(col)
        if exclude is not None and col_s in exclude:
            continue
        if any(k in col_s for k in keywords):
            val = row.get(col)
            if val is not None and str(val).strip() not in ("", "-", "nan", "None"):
                return val
    return None


def _pick_financial_value(row: pd.Series, keywords: List[str], errors: List[str], *, field: Optional[str] = None, source: str = "unknown", period: Optional[str] = None, field_meta: Optional[Dict[str, Any]] = None) -> Optional[float]:
    """Read a metric from the already selected period, preferring exact labels."""
    labels = {
        col: re.sub(r"[（(](?:单位[:：]\s*)?(?:%|元|万元|亿元|人民币|倍)[）)]", "", str(col)).strip()
        for col in row.index
    }
    is_amount = not any(term in keyword for keyword in keywords for term in ("率", "ROE", "同比", "增长"))
    fallback: Optional[float] = None
    fallback_reading: Optional[AnchorReading] = None
    for exact in (True, False):
        if not exact and is_amount:
            break  # 金额只接受确证身份，不能借利润率、扣非或每股指标
        for keyword in keywords:
            for label in dict.fromkeys(labels.values()):
                if not ((label == keyword if exact else keyword in label) or (
                    not exact and field in {"revenue_yoy", "net_profit_yoy"} and caliber_from_label(field, label) is not None
                )):
                    continue
                values: set[float] = set()
                candidates: list[AnchorReading] = []
                for col, normalized in labels.items():
                    if normalized != label:
                        continue
                    raw = row.get(col)
                    entries = raw.tolist() if isinstance(raw, pd.Series) else [raw]
                    for value in entries:
                        if field is not None:
                            # This endpoint strips the Sina percentage display metadata.
                            # Keep the mapping exact; unknown interfaces/labels remain unknown.
                            sina_labels = {
                                "净资产收益率(ROE)": "weighted_roe",
                                "毛利率": "gross_margin",
                                "营业总收入增长率": "total_operating_revenue_yoy",
                                "归属母公司净利润增长率": "parent_net_profit_yoy",
                            }
                            sina_metric = sina_labels.get(str(col)) if source == "akshare:stock_financial_abstract" else None
                            number, unit, currency, error = normalize_anchor_value(
                                field, value, str(col), unit="percentage_point" if sina_metric else None,
                            )
                            actual, basis = report_period_from_fields({"report_date": period}, str(col))
                            if sina_metric and actual and basis == "unknown":
                                if actual.endswith("-12-31"):
                                    basis = "annual"
                                elif actual.endswith(("-03-31", "-06-30", "-09-30")):
                                    basis = "YTD"
                            if number is not None:
                                candidates.append(AnchorReading(source, number, caliber=sina_metric or caliber_from_label(field, str(col)), period=actual, period_basis=basis, unit=unit, currency=currency, raw_value=str(value), raw_label=str(col), normalization_error=error))
                        else:
                            number = _safe_float(value)
                        if number is not None:
                            values.add(number)
                if len(values) > 1:
                    errors.append(f"financial_table:conflicting_duplicate_metric:{label}")
                    return None
                if values:
                    if field is not None and candidates and reading_input_reasons(candidates[0], field):
                        if fallback is None:
                            fallback, fallback_reading = next(iter(values)), candidates[0]
                        continue
                    if field_meta is not None and field is not None and candidates:
                        field_meta[field] = adopted_field_record(field, candidates[0])
                    return next(iter(values))
    if field_meta is not None and field is not None and fallback_reading is not None:
        field_meta[field] = adopted_field_record(field, fallback_reading)
    return fallback


def _select_financial_row(df: pd.DataFrame, stock_code: str) -> Tuple[Optional[pd.Series], Optional[str]]:
    """Select one confirmed latest period for either supported financial table shape."""
    if "指标" in df.columns:
        periods = {col: _normalize_report_date(col) for col in df.columns if col not in ("指标", "选项")}
        position = select_report_period([normalize_report_period(col) for col in periods])
        if position is not None:
            column = list(periods)[position]
            actual_period = periods[column]
        elif len(periods) == 1:
            column, actual_period = next(iter(periods)), None
        else:
            return None, None
        row = pd.Series(df[column].to_numpy(), index=df["指标"].astype(str))
        row.attrs["report_period"] = str(column)
        return row, actual_period

    rows = _filter_rows_by_code(df, stock_code)
    if rows.empty:
        return None, None
    date_cols = [col for col in rows.columns if str(col) in (*_DIVIDEND_KEYWORD_MAP["report_date"], "日期")]
    if date_cols:
        position = select_report_period([normalize_report_period(value) for value in rows[date_cols[0]].tolist()])
        if position is not None:
            row = rows.iloc[position]
            actual, basis = report_period_from_fields({"report_date": row[date_cols[0]], "period_basis": row.get("period_basis")})
            row.attrs["report_period"] = f"{actual} {basis}" if actual and basis != "unknown" else str(row[date_cols[0]])
            return row, actual
    return (rows.iloc[0], None) if len(rows) == 1 else (None, None)


def _parse_dividend_plan_to_per_share(plan_text: str) -> Optional[float]:
    """Parse per-share cash dividend from Chinese plan text."""
    text = _safe_str(plan_text)
    if not text:
        return None

    for pattern in (
        r"(?:每)?\s*10\s*股?\s*派(?:发)?\s*([0-9]+(?:\.[0-9]+)?)\s*元",
        r"10\s*派\s*([0-9]+(?:\.[0-9]+)?)\s*元",
    ):
        match = re.search(pattern, text)
        if match:
            parsed = _safe_float(match.group(1))
            if parsed is not None and parsed > 0:
                return parsed / 10.0

    match_per_share = re.search(
        r"每\s*股\s*派(?:发)?\s*([0-9]+(?:\.[0-9]+)?)\s*元", text
    )
    if match_per_share:
        parsed = _safe_float(match_per_share.group(1))
        if parsed is not None and parsed > 0:
            return parsed
    return None


def _extract_cash_dividend_per_share(row: pd.Series) -> Optional[float]:
    """Extract pre-tax cash dividend per share from a row."""
    plan_text = _safe_str(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["plan_text"]))
    # Keep pre-tax semantics; skip explicit after-tax plans unless pre-tax marker exists.
    if "税后" in plan_text and "税前" not in plan_text and "含税" not in plan_text:
        return None

    direct = _safe_float(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["per_share"]))
    if direct is not None and direct > 0:
        return direct
    return _parse_dividend_plan_to_per_share(plan_text)


def _filter_rows_by_code(df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    code_cols = [
        c
        for c in df.columns
        if any(
            k in str(c) for k in ("代码", "股票代码", "证券代码", "symbol", "ts_code")
        )
    ]
    if not code_cols:
        return df

    target = _normalize_code(stock_code)
    for col in code_cols:
        try:
            series = df[col].astype(str).map(_normalize_code)
            filtered = df[series == target]
            if not filtered.empty:
                return filtered
        except Exception:
            continue
    return pd.DataFrame()


def _normalize_report_date(value: Any) -> Optional[str]:
    return normalize_report_period(str(value) if isinstance(value, int) else value)[0]


def _build_dividend_payload(
    dividend_df: pd.DataFrame,
    stock_code: str,
    max_events: int = 5,
) -> Dict[str, Any]:
    work_df = _filter_rows_by_code(dividend_df, stock_code)
    if work_df.empty:
        return {}

    now_date = datetime.now().date()
    ttm_start_date = now_date - timedelta(days=365)
    dedupe_keys = set()
    events: List[Dict[str, Any]] = []

    for _, row in work_df.iterrows():
        if not isinstance(row, pd.Series):
            continue
        ex_dt = _safe_datetime(
            _pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["ex_dividend_date"])
        )
        record_dt = _safe_datetime(
            _pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["record_date"])
        )
        announce_dt = _safe_datetime(
            _pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["announce_date"])
        )
        event_dt = ex_dt or record_dt or announce_dt
        if event_dt is None:
            continue
        event_date = event_dt.date()
        if event_date > now_date:
            continue

        per_share = _extract_cash_dividend_per_share(row)
        if per_share is None or per_share <= 0:
            continue

        dedupe_key = (event_date.isoformat(), round(per_share, 6))
        if dedupe_key in dedupe_keys:
            continue
        dedupe_keys.add(dedupe_key)

        events.append(
            {
                "event_date": event_date.isoformat(),
                "ex_dividend_date": ex_dt.date().isoformat() if ex_dt else None,
                "record_date": record_dt.date().isoformat() if record_dt else None,
                "announcement_date": announce_dt.date().isoformat()
                if announce_dt
                else None,
                "cash_dividend_per_share": round(per_share, 6),
                "is_pre_tax": True,
            }
        )

    if not events:
        return {}

    events.sort(key=lambda item: item.get("event_date") or "", reverse=True)
    ttm_events: List[Dict[str, Any]] = []
    for item in events:
        event_dt = _safe_datetime(item.get("event_date"))
        if event_dt is None:
            continue
        event_date = event_dt.date()
        if ttm_start_date <= event_date <= now_date:
            ttm_events.append(item)

    return {
        "events": events[: max(1, max_events)],
        "ttm_event_count": len(ttm_events),
        "ttm_cash_dividend_per_share": (
            round(
                sum(
                    float(item.get("cash_dividend_per_share") or 0.0)
                    for item in ttm_events
                ),
                6,
            )
            if ttm_events
            else None
        ),
        "coverage": "cash_dividend_pre_tax",
        "as_of": now_date.isoformat(),
    }


# 指标业务时点列（报告期/统计期/交易日）优先于披露时间列；拿公告日替代统计期同样错误。
_BUSINESS_PERIOD_KEYWORDS: Tuple[str, ...] = ("报告期", "预告期", "统计截止", "截止日期", "交易日", "日期", "时间")
_DISCLOSURE_TIME_KEYWORDS: Tuple[str, ...] = ("公告", "披露", "发布")
_SECURITY_CODE_COLUMN_KEYWORDS: Tuple[str, ...] = ("代码", "股票代码", "证券代码", "ts_code", "symbol")


def _record_business_period(row: pd.Series, period_cols: List[Any]) -> Optional[str]:
    """One record's own business period; conflicting date fields invalidate it."""
    actuals: set[str] = set()
    for col in period_cols:
        raw = row.get(col)
        entries = raw.tolist() if isinstance(raw, pd.Series) else [raw]
        for entry in entries:
            actual = normalize_report_period(entry)[0]
            if actual is not None:
                actuals.add(actual)
    return next(iter(actuals)) if len(actuals) == 1 else None


def _time_columns(row: pd.Series) -> set[str]:
    """Date/time columns never carry narrative text; summaries must not pick them."""
    return {
        str(col) for col in row.index
        if any(k in str(col) for k in (*_BUSINESS_PERIOD_KEYWORDS, *_DISCLOSURE_TIME_KEYWORDS))
    }


def _period_columns(df: pd.DataFrame) -> List[Any]:
    """Business-period columns only; a publication date never qualifies as one."""
    return [
        col for col in df.columns
        if any(k in str(col) for k in _BUSINESS_PERIOD_KEYWORDS)
        and not any(k in str(col) for k in _DISCLOSURE_TIME_KEYWORDS)
    ]


def _extract_latest_row(df: pd.DataFrame, stock_code: str, *, allow_future_target: bool = False) -> Optional[pd.Series]:
    """Select the applicable record by row-level security identity and business period.

    Row order never decides recency: the adopted record is the one whose own
    date fields carry the metric's latest business period. Records without a
    parseable period are unknown evidence, not "latest"; a record whose date
    fields conflict with each other, or several different records sharing the
    latest period, stay ineligible (no invented recency, no gap papering).

    A record must also carry a row-level code column: without it the adapter
    cannot prove the row belongs to the requested security, because unscoped
    provider defaults may silently return a different stock.

    ``allow_future_target=True`` marks forecast-target semantics: a future
    forecast period is a legitimate record of its own. Realized-data
    semantics (trading days, published quick reports) keep the default and
    exclude periods that have not happened yet. Within one tied period,
    records are disambiguated by the latest disclosure time when the table
    provides one; otherwise the tie stays an explicit gap.
    """
    if df is None or df.empty:
        return None

    code_cols = [
        c
        for c in df.columns
        if any(k in str(c) for k in _SECURITY_CODE_COLUMN_KEYWORDS)
    ]
    if not code_cols:
        return None  # 无行级证券身份证据：unscoped 默认返回可能属于别的股票
    target = _normalize_code(stock_code)
    combined: Optional[pd.Series] = None
    for col in code_cols:
        try:
            mask = df[col].astype(str).map(_normalize_code) == target
        except Exception:
            continue
        combined = mask if combined is None else (combined & mask)
    if combined is None:
        return None
    matched = df[combined]
    if matched.empty:
        return None

    period_cols = _period_columns(matched)
    if not period_cols:
        return None  # 无行级期间证据：不编造"最新"

    dated = matched.assign(_business_period=matched.apply(lambda row: _record_business_period(row, period_cols), axis=1))
    dated = dated[dated["_business_period"].notna()]
    if dated.empty:
        return None
    if not allow_future_target:
        today = datetime.now().date().isoformat()
        dated = dated[dated["_business_period"] <= today]
        if dated.empty:
            return None
    tied = dated[dated["_business_period"] == dated["_business_period"].max()].drop(columns=["_business_period"]).drop_duplicates()
    if len(tied) > 1:
        # 同期多份不同记录：有披露时间列时按最新修订选择（统计期不变），否则值歧义。
        disclosure_cols = [col for col in tied.columns if any(k in str(col) for k in _DISCLOSURE_TIME_KEYWORDS)]
        if not disclosure_cols:
            return None
        tied = tied.assign(_disclosed_at=tied.apply(lambda row: _record_business_period(row, disclosure_cols), axis=1))
        tied = tied[tied["_disclosed_at"].notna()]
        if tied.empty:
            return None
        tied = tied[tied["_disclosed_at"] == tied["_disclosed_at"].max()].drop(columns=["_disclosed_at"]).drop_duplicates()
    if len(tied) != 1:
        return None  # 同期同披露时间仍多份不同记录：值歧义，保留明确缺口
    return cast(pd.Series, tied.iloc[0])


class AkshareFundamentalAdapter:
    """AkShare adapter for fundamentals, capital flow and dragon-tiger signals."""

    def _call_df_candidates(
        self,
        candidates: List[Tuple[str, Dict[str, Any]]],
    ) -> Tuple[Optional[pd.DataFrame], Optional[str], List[str]]:
        errors: List[str] = []
        try:
            import akshare as ak
        except Exception as exc:
            return None, None, [f"import_akshare:{type(exc).__name__}"]

        for func_name, kwargs in candidates:
            fn = getattr(ak, func_name, None)
            if fn is None:
                continue
            try:
                df = fn(**kwargs)
                if isinstance(df, pd.Series):
                    df = df.to_frame().T
                if isinstance(df, pd.DataFrame) and not df.empty:
                    return df, func_name, errors
            except Exception as exc:
                errors.append(f"{func_name}:{type(exc).__name__}")
                continue
        return None, None, errors

    def get_financial_indicators(self, stock_code: str) -> Dict[str, Any]:
        """
        Return normalized fundamental blocks from AkShare with partial tolerance.
        """
        result: Dict[str, Any] = {
            "status": "not_supported",
            "growth": {},
            "earnings": {},
            "institution": {},
            "source_chain": [],
            "errors": [],
        }

        # Financial indicators
        fin_df, fin_source, fin_errors = self._call_df_candidates(
            [
                ("stock_financial_abstract", {"symbol": stock_code}),
                ("stock_financial_analysis_indicator", {"symbol": stock_code}),
                ("stock_financial_analysis_indicator", {}),
            ]
        )
        result["errors"].extend(fin_errors)
        if fin_df is not None:
            row, report_date = _select_financial_row(fin_df, stock_code)
            if row is None:
                result["errors"].append("financial_table:unconfirmed_or_ambiguous_period")
            else:
                growth_meta: Dict[str, Any] = {}
                report_meta: Dict[str, Any] = {}
                origin = f"akshare:{fin_source}"
                period_evidence = row.attrs.get("report_period", report_date)
                result["growth"] = {
                    "revenue_yoy": _pick_financial_value(row, [
                        "营业总收入增长率", "主营业务收入增长率", "营业收入增长率",
                        "营收增长率", "收入增长率", "营业收入同比", "营收同比", "收入同比",
                    ], result["errors"], field="revenue_yoy", source=origin, period=period_evidence, field_meta=growth_meta),
                    "net_profit_yoy": _pick_financial_value(row, [
                        "归属母公司净利润增长率", "归母净利润增长率", "归母净利润同比",
                        "净利润增长率", "净利润同比", "净利同比",
                    ], result["errors"], field="net_profit_yoy", source=origin, period=period_evidence, field_meta=growth_meta),
                    "roe": _pick_financial_value(row, ["净资产收益率", "ROE"], result["errors"], field="roe", source=origin, period=period_evidence, field_meta=growth_meta),
                    "gross_margin": _pick_financial_value(row, ["销售毛利率", "毛利率"], result["errors"], field="gross_margin", source=origin, period=period_evidence, field_meta=growth_meta),
                    "report_date": report_date,
                }
                financial_report_payload = {
                    "report_date": report_date,
                    "revenue": _pick_financial_value(row, ["营业总收入", "营业收入", "营收"], result["errors"], field="revenue", source=origin, period=period_evidence, field_meta=report_meta),
                    "net_profit_parent": _pick_financial_value(row, [
                        "归母净利润", "归属母公司股东的净利润", "归属于母公司股东的净利润",
                        "归属于母公司所有者的净利润", "母公司股东净利润",
                    ], result["errors"], field="net_profit", source=origin, period=period_evidence, field_meta=report_meta),
                    "operating_cash_flow": _pick_financial_value(row, [
                        "经营活动产生的现金流量净额", "经营现金流", "经营活动现金流",
                    ], result["errors"]),
                    "roe": result["growth"]["roe"],
                }
                if "roe" in growth_meta:
                    report_meta["roe"] = growth_meta["roe"]
                if any(value is not None for key, value in financial_report_payload.items() if key != "report_date"):
                    financial_report_payload["field_meta"] = report_meta
                    result["earnings"]["financial_report"] = financial_report_payload
                if not any(value is not None for key, value in result["growth"].items() if key != "report_date") and not result["earnings"]:
                    result["errors"].append("financial_table:no_usable_metrics")
                result["growth"]["field_meta"] = growth_meta
                result["source_chain"].append(f"growth:{fin_source}")

        result["status"] = "partial" if any(value is not None for key, value in result["growth"].items() if key not in {"report_date", "field_meta"}) or result["earnings"] else "not_supported"
        return result

    def get_fundamental_bundle(self, stock_code: str, *, financials: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Append optional details without mutating already acquired financials."""
        from copy import deepcopy
        result = deepcopy(financials) if financials is not None else self.get_financial_indicators(stock_code)

        # These AkShare endpoints accept a quarter-end date, never a stock symbol.
        today = datetime.now().date()
        quarters = sorted(f"{year}{month:02d}{day:02d}" for year in (today.year - 1, today.year) for month, day in ((3, 31), (6, 30), (9, 30), (12, 31)) if f"{year}{month:02d}{day:02d}" <= today.strftime("%Y%m%d"))
        report_date = quarters[-1]

        # Earnings forecast
        forecast_df, forecast_source, forecast_errors = self._call_df_candidates(
            [
                ("stock_yjyg_em", {"date": report_date}),
            ]
        )
        result["errors"].extend(forecast_errors)
        if forecast_df is not None:
            # 预告的目标报告期可以是未来季度：那是预告自身的合法业务时点。
            row = _extract_latest_row(forecast_df, stock_code, allow_future_target=True)
            if row is not None:
                result["earnings"]["forecast_summary"] = _safe_str(
                    _pick_by_keywords(row, ["预告", "业绩变动", "内容", "摘要", "公告"], exclude=_time_columns(row))
                )[:200]
                result["source_chain"].append(f"earnings_forecast:{forecast_source}")

        # Earnings quick report
        quick_df, quick_source, quick_errors = self._call_df_candidates(
            [
                ("stock_yjkb_em", {"date": report_date}),
            ]
        )
        result["errors"].extend(quick_errors)
        if quick_df is not None:
            row = _extract_latest_row(quick_df, stock_code)
            if row is not None:
                result["earnings"]["quick_report_summary"] = _safe_str(
                    _pick_by_keywords(row, ["快报", "摘要", "公告", "说明"], exclude=_time_columns(row))
                )[:200]
                result["source_chain"].append(f"earnings_quick:{quick_source}")

        # Dividend details (cash dividend, pre-tax)
        dividend_df, dividend_source, dividend_errors = self._call_df_candidates(
            [
                ("stock_fhps_detail_em", {"symbol": stock_code}),
                (
                    "stock_history_dividend_detail",
                    {"symbol": stock_code, "indicator": "分红", "date": ""},
                ),
                ("stock_dividend_cninfo", {"symbol": stock_code}),
            ]
        )
        result["errors"].extend(dividend_errors)
        if dividend_df is not None:
            dividend_payload = _build_dividend_payload(
                dividend_df, stock_code, max_events=5
            )
            if dividend_payload:
                result["earnings"]["dividend"] = dividend_payload
                result["source_chain"].append(f"dividend:{dividend_source}")

        # 机构/十大股东持仓：公开接口均无合格证据，不再错误映射（issue #60）。
        # - stock_institute_hold 的 symbol 参数是季度筛选（如默认 "20051"=2005 年
        #   一季度），无个股筛选参数：不传参时隐式返回 20051 季度全市场表，且表格
        #   无行级期间列，无法证明所选行属于当前证券或任何已确认期间；其"机构数
        #   变化"是机构家数变化、"持股比例增幅"单位口径未证实，均不是持仓数量/
        #   比例变化。
        # - stock_zh_a_gdhs_detail_em 是股东户数表，"股东户数-增减"不是持股变化。
        # 两者都不具备把值写成 institution_holding_change / top10_holder_change 的
        # 统计对象、财期与单位证据；合格机构持仓接入需另行按真实能力验证，本处
        # 保留真实缺口（institution 块空 → 触发既有 iFinD fallback 资格门槛）。

        has_content = bool(
            any(value is not None for key, value in result["growth"].items() if key not in {"report_date", "field_meta"})
            or result["earnings"] or result["institution"]
        )
        result["status"] = "partial" if has_content else "not_supported"
        return result

    def get_capital_flow(self, stock_code: str, top_n: int = 5, *, include_stock: bool = True) -> Dict[str, Any]:
        """
        Return stock + sector capital flow.
        """
        result: Dict[str, Any] = {
            "status": "not_supported",
            "stock_flow": {},
            "sector_rankings": {"top": [], "bottom": []},
            "source_chain": [],
            "errors": [],
        }

        # stock_individual_fund_flow 按 stock+market 逐股请求；无参调用会返回默认股
        # 600094 而非请求证券，因此只做显式 scoped 候选。scoped 逐股响应自身不带
        # 代码列，其证券身份由请求参数保证，注入请求代码后进入同一选择契约；
        # 全市场排名表（带代码列）仅作 scoped 失败时的备选。
        stock_df: Optional[pd.DataFrame] = None
        stock_source: Optional[str] = None
        stock_errors: List[str] = []
        market = _market_suffix(stock_code)
        if include_stock and market is not None:
            stock_df, stock_source, scoped_errors = self._call_df_candidates(
                [
                    ("stock_individual_fund_flow", {"stock": stock_code, "market": market}),
                ]
            )
            stock_errors.extend(scoped_errors)
            if stock_df is not None and not any(
                keyword in str(col) for col in stock_df.columns for keyword in _SECURITY_CODE_COLUMN_KEYWORDS
            ):
                # 仅当 scoped 响应不带行级代码列时传播请求身份；已有代码列按行级证据过滤。
                stock_df = stock_df.assign(**{"股票代码": _normalize_code(stock_code)})
        if stock_df is None and include_stock:
            fallback_df, fallback_source, fallback_errors = self._call_df_candidates(
                [
                    ("stock_main_fund_flow", {}),
                ]
            )
            stock_errors.extend(fallback_errors)
            stock_df, stock_source = fallback_df, fallback_source
        result["errors"].extend(stock_errors)
        if stock_df is not None:
            # 资金流是交易日观察语义：正序历史表中"最新行"由日期决定，未来观察日期不是已发生数据。
            row = _extract_latest_row(stock_df, stock_code)
            if row is not None:
                flow_meta: Dict[str, Any] = {}
                net_inflow = _pick_financial_value(row, ["主力净流入-净额", "主力净流入额", "主力净流入", "净流入", "净额"], result["errors"], field="main_inflow", source=f"akshare:{stock_source}", field_meta=flow_meta)
                inflow_5d = _safe_float(_pick_by_keywords(row, ["5日", "五日"]))
                inflow_10d = _safe_float(_pick_by_keywords(row, ["10日", "十日"]))
                result["stock_flow"] = {
                    "main_net_inflow": net_inflow,
                    "inflow_5d": inflow_5d,
                    "inflow_10d": inflow_10d,
                }
                from data_provider.cross_source_validator import observation_time_from_fields
                for record in flow_meta.values():
                    record["observed_at"] = observation_time_from_fields(row.to_dict())
                result["stock_flow"]["field_meta"] = flow_meta
                result["source_chain"].append(f"capital_stock:{stock_source}")

        sector_df, sector_source, sector_errors = self._call_df_candidates(
            [
                ("stock_sector_fund_flow_rank", {}),
            ]
        )
        result["errors"].extend(sector_errors)
        if sector_df is not None:
            name_col = next(
                (
                    c
                    for c in sector_df.columns
                    if any(k in str(c) for k in ("板块", "行业", "名称", "name"))
                ),
                None,
            )
            flow_col = next(
                (
                    c
                    for c in sector_df.columns
                    if any(k in str(c) for k in ("净流入", "主力", "flow", "净额"))
                ),
                None,
            )
            if name_col and flow_col:
                work_df = sector_df[[name_col, flow_col]].copy()
                work_df[flow_col] = pd.to_numeric(work_df[flow_col], errors="coerce")
                work_df = work_df.dropna(subset=[flow_col])
                top_df = work_df.nlargest(top_n, flow_col)
                bottom_df = work_df.nsmallest(top_n, flow_col)
                result["sector_rankings"] = {
                    "top": [
                        {
                            "name": _safe_str(r[name_col]),
                            "net_inflow": float(r[flow_col]),
                        }
                        for _, r in top_df.iterrows()
                    ],
                    "bottom": [
                        {
                            "name": _safe_str(r[name_col]),
                            "net_inflow": float(r[flow_col]),
                        }
                        for _, r in bottom_df.iterrows()
                    ],
                }
                result["source_chain"].append(f"capital_sector:{sector_source}")

        has_content = bool(
            any(result["stock_flow"].get(key) is not None for key in ("main_net_inflow", "inflow_5d", "inflow_10d"))
            or result["sector_rankings"]["top"]
            or result["sector_rankings"]["bottom"]
        )
        result["status"] = "partial" if has_content else "not_supported"
        return result

    def get_dragon_tiger_flag(
        self, stock_code: str, lookback_days: int = 20
    ) -> Dict[str, Any]:
        """
        Return dragon-tiger signal in lookback window.
        """
        result: Dict[str, Any] = {
            "status": "not_supported",
            "is_on_list": False,
            "recent_count": 0,
            "latest_date": None,
            "source_chain": [],
            "errors": [],
        }

        df, source, errors = self._call_df_candidates(
            [
                ("stock_lhb_stock_statistic_em", {}),
                ("stock_lhb_detail_em", {}),
                ("stock_lhb_jgmmtj_em", {}),
            ]
        )
        result["errors"].extend(errors)
        if df is None:
            return result

        # Try code filter
        code_cols = [
            c
            for c in df.columns
            if any(k in str(c) for k in ("代码", "股票代码", "证券代码"))
        ]
        target = _normalize_code(stock_code)
        matched = pd.DataFrame()
        for col in code_cols:
            try:
                series = df[col].astype(str).map(_normalize_code)
                cur = df[series == target]
                if not cur.empty:
                    matched = cur
                    break
            except Exception:
                continue
        if matched.empty:
            result["source_chain"].append(f"dragon_tiger:{source}")
            result["status"] = "ok" if code_cols else "partial"
            return result

        date_col = next(
            (
                c
                for c in matched.columns
                if any(k in str(c) for k in ("日期", "上榜", "交易日", "time"))
            ),
            None,
        )
        parsed_dates: List[datetime] = []
        if date_col is not None:
            for val in matched[date_col].astype(str).tolist():
                try:
                    parsed_dates.append(pd.to_datetime(val).to_pydatetime())
                except Exception:
                    continue
        now = datetime.now()
        start = now - timedelta(days=max(1, lookback_days))
        recent_dates = [d for d in parsed_dates if start <= d <= now]

        result["is_on_list"] = bool(recent_dates)
        result["recent_count"] = (
            len(recent_dates) if recent_dates else int(len(matched))
        )
        result["latest_date"] = (
            max(recent_dates).date().isoformat()
            if recent_dates
            else (max(parsed_dates).date().isoformat() if parsed_dates else None)
        )
        result["status"] = "ok"
        result["source_chain"].append(f"dragon_tiger:{source}")
        return result
