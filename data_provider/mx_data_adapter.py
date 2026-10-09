# -*- coding: utf-8 -*-
"""MX Data 适配器 —— 东方财富「妙想」Skills Hub（主数据源）。

移植自 ``~/.claude/skills/uzi-skill/deep-analysis/scripts/lib/mx_api.py``，改造点：
- 剥离对 uzi-skill ``lib.cache`` 的依赖，``MXClient`` 自带进程内 TTL 缓存（控配额）。
- 实现 :class:`SourceAdapter`（见 ``cross_source_validator``），暴露 :class:`MXSource`。
- fail-open 贯穿：无 ``MX_APIKEY`` / HTTP 错误 / 解析失败 → ``read`` 返回 None。

数据源特性：``POST /finskillshub/api/claw/query`` 是**自然语言查询**接口，
header 带 ``apikey``。返回嵌套 JSON，``_extract_first_table_row`` 把首个表格解析成
``{中文指标名: 值}``，``MXSource`` 再按字段关键词映射到标准锚点。

比爬 ``push2.eastmoney.com`` 稳定（2026 大陆网络下爬虫常被封），是官方认证源。
"""

from __future__ import annotations

from contextvars import ContextVar
import logging
import os
import time
from threading import Lock
from typing import Any, Dict, List, Optional

from .cross_source_validator import AnchorReading, caliber_from_label, normalize_anchor_value, normalize_report_period, observation_time_from_fields, reading_input_reasons, report_period_from_fields, select_report_period
from typing import cast  # added by mypy_codemod

logger = logging.getLogger(__name__)

BASE = "https://mkapi2.dfcfs.com/finskillshub/api/claw"
QUERY_URL = f"{BASE}/query"

_MX_TTL = 30 * 60  # 30 min —— MX 响应半实时，缓存以控配额
financial_refresh: ContextVar[Optional[tuple[str, str]]] = ContextVar("fundamentals_refresh", default=None)

# 标准锚点 → MX 返回的中文字段关键词（命中其一即可）。按字段类别分组。
_SNAPSHOT_FIELDS: Dict[str, List[str]] = {
    "current_price": ["最新价", "最新", "现价", "收盘价"],
    "pe_ratio": ["市盈率", "市盈(TTM)", "动态市盈"],
    "pb_ratio": ["市净率", "市净"],
    "total_mv": ["总市值"],
    "circ_mv": ["流通市值", "流通"],
}
_FINANCIAL_FIELDS: Dict[str, List[str]] = {
    # 关键词须避免命中「同比增长率」：用精确措辞，且 _pick_value 按 dict 顺序，
    # 排除含「增长/同比」的列（见 _pick_value）
    "revenue": ["营业收入", "营业总收入", "营收"],
    "net_profit": ["归属于母公司股东的净利润", "归母净利润", "归属于母公司", "净利润"],
    "roe": ["净资产收益率ROE(加权)", "净资产收益率", "ROE"],
    "gross_margin": ["毛利率", "销售毛利率"],
}
# 同比增长类字段：取「同比增长率」列（与 _FINANCIAL_FIELDS 的绝对值互补）。
# 关键词与绝对值锚点相同（如「营业收入」），由 picker（_pick_growth_value 要求
# 增长率标记）区分：同一列名「营业收入(同比增长率)」，_pick_value 跳过、
# _pick_growth_value 命中。
_GROWTH_FIELDS: Dict[str, List[str]] = {
    "revenue_yoy": ["营业收入", "营收"],
    "net_profit_yoy": ["归属母公司股东的净利润同比", "归母净利润同比"],
}
_CAPITAL_FIELDS: Dict[str, List[str]] = {
    "main_inflow": ["主力净流入", "主力资金净流入", "主力净流入额"],
    "margin_balance": ["融资余额"],
}
# 财务/增长锚点需要带报告期；其余（行情/估值/资金）period 不适用
_PERIOD_FIELDS = set(_FINANCIAL_FIELDS.keys()) | set(_GROWTH_FIELDS.keys())


# 中文金额单位 → 乘数（仅万亿/亿/万/百万/千为数值后缀；万元/亿元为复合单位）。
# 注意：只 strip 亿元/万亿元中的「亿/万」，「元」保留在原值中（不做单位换算）。
# 对 MX 返回的「xxx亿」「xxx万亿」「xxx万」（亿在前、元在后）均能正确解析。
_CN_UNIT_FACTORS: Dict[str, float] = {
    "万亿": 1e12,
    "千亿": 1e11,
    "百亿": 1e10,
    "十亿": 1e9,
    "亿": 1e8,
    "万": 1e4,
}


def _safe_float(value: Any) -> Optional[float]:
    """容错转 float（MX 返回值多为字符串/科学计数/中文金额单位）。

    支持 ``"1.516万亿"`` → ``1.516e12``、``"1709亿元"`` → ``1.709e11``、
    ``"199.4亿"`` → ``1.994e10``、``"-8.699亿"`` → ``-8.699e8``。
    先移除「元」（单位词，不影响数值），再剥离末尾的「亿/万/万亿」等量级单位。
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (
            TypeError,
            ValueError,
        ):  # pragma: no cover — int/float 转 float 不会失败，纯防御
            return None
    s = str(value).strip().replace(",", "").replace("%", "")
    if not s or s in ("-", "--", "nan", "None", "null"):
        return None
    s = s.replace("元", "")  # 移除单位词「元」（保留量级单位「亿/万」）
    factor = 1.0
    for unit, mult in _CN_UNIT_FACTORS.items():
        if s.endswith(unit):
            s = s[: -len(unit)].strip()
            factor = mult
            break
    try:
        return float(s) * factor
    except (TypeError, ValueError):
        return None


def _pick_value(bundle: Dict[str, Any], keywords: List[str], field: Optional[str] = None) -> Optional[float]:
    """从 ``{中文label: 值}`` 中按关键词模糊匹配取首个可解析数值。

    排除含「同比/增长率」的列——财务 query 同时返回绝对值与同比增长率，
    关键词「营业收入」会命中「营业收入(同比增长率)」，需跳过取绝对值列。
    """
    if not isinstance(bundle, dict) or not bundle:
        return None
    for label, raw in bundle.items():
        label_s = str(label)
        if any(kw in label_s for kw in keywords):
            # 跳过增长率/同比列（绝对值锚点优先）
            if any(skip in label_s for skip in ("同比增长", "增长率", "环比")):
                continue
            val = normalize_anchor_value(field, raw, label_s)[0] if field is not None else _safe_float(raw)
            if val is not None:
                return val
    return None


# 同比增长类列标记：_pick_value 跳过这些列取绝对值，_pick_growth_value 专门取这些列。
_GROWTH_MARKERS = ("同比", "yoy", "增长率", "环比")


def _pick_growth_value(bundle: Dict[str, Any], keywords: List[str], field: Optional[str] = None) -> Optional[float]:
    """取「同比增长率」类数值，与 :func:`_pick_value` 互补。

    已确认的同比身份复用集中口径判定；旧关键词候选仍保留原始证据。
    """
    if not isinstance(bundle, dict) or not bundle:
        return None
    for label, raw in bundle.items():
        label_s = str(label)
        if (field is not None and caliber_from_label(field, label_s) is not None) or (
            any(kw in label_s for kw in keywords) and any(m in label_s.lower() for m in _GROWTH_MARKERS)
        ):
            val = normalize_anchor_value(field, raw, label_s)[0] if field is not None else _safe_float(raw)
            if val is not None:
                return val
    return None


def _cn_period_to_yyyymmdd(period: Any) -> Optional[str]:
    """Compatibility format for actual response periods used by bundle adapters."""
    actual, _basis = normalize_report_period(period)
    return actual.replace("-", "") if actual is not None else None


# ------------------------------------------------------------------
# HTTP + 解析（移植自 mx_api.py，纯函数化便于单测）
# ------------------------------------------------------------------
def _post(
    url: str, body: Dict[str, Any], api_key: str, timeout: float = 30, attempts: int = 2, *, deadline: Optional[float] = None
) -> Dict[str, Any]:
    """POST + 小重试。返回解析 JSON 或 ``{"error": ...}``。"""
    try:
        import requests  # 延迟导入，未装时优雅降级
    except ImportError:
        return {"error": "requests library missing"}

    headers = {"Content-Type": "application/json", "apikey": api_key}
    last_err: Optional[str] = None
    for i in range(attempts):
        remaining = timeout if deadline is None else min(timeout, deadline - time.monotonic())
        if remaining <= 0:
            return {"error": "timeout"}
        try:
            r = requests.post(url, headers=headers, json=body, timeout=remaining)
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                if r.status_code in (401, 403):  # key 问题不重试
                    break
                if i + 1 < attempts:
                    pause = 1.0 * (i + 1)
                    if deadline is not None and deadline - time.monotonic() <= pause:
                        return {"error": "timeout"}
                    time.sleep(pause)
                continue
            return cast(Dict[str, Any], r.json())
        except Exception as exc:  # noqa: BLE001 — 网络/解析错误统一捕获
            last_err = f"{type(exc).__name__}: {str(exc)[:200]}"
            if i + 1 < attempts:
                pause = 1.0 * (i + 1)
                if deadline is not None and deadline - time.monotonic() <= pause:
                    return {"error": "timeout"}
                time.sleep(pause)
    return {"error": last_err or "unknown"}


def _extract_first_table_row(result: Any, period: Optional[str] = None, field: Optional[str] = None) -> Dict[str, Any]:
    """从首个 MX 表格读取同一实际期，指期优先、缺期回退已确认最新期。

    按 headName 的实际日期选列，不假设源列顺序；_mx_period 是所选响应列，
    无法辨认的日期保持未知。非期间型的单值表保留既有首观测读取行为。
    """
    if not isinstance(result, dict) or result.get("error"):
        return {}
    if result.get("status") not in (0, None):
        return {}
    data = result.get("data") or {}
    inner = data.get("data") or {}
    sr = inner.get("searchDataResultDTO") or {}
    dto_list = sr.get("dataTableDTOList") or []
    if not dto_list or not isinstance(dto_list[0], dict):
        return {}
    dto = dto_list[0]
    table = dto.get("table") or {}
    if not isinstance(table, dict):
        return {}
    name_map = dto.get("nameMap") or {}
    if isinstance(name_map, list):  # 退化成 index map
        name_map = {str(i): v for i, v in enumerate(name_map)}

    head_name = table.get("headName") or []
    head_first = head_name[0] if isinstance(head_name, list) and head_name else None
    position = 0
    if isinstance(head_name, list):
        periods = [normalize_report_period(label) for label in head_name]
        if field is not None:
            keywords = _FINANCIAL_FIELDS.get(field) or _GROWTH_FIELDS.get(field) or []
            picker = _pick_growth_value if field in _GROWTH_FIELDS else _pick_value
            for index in range(len(periods)):
                candidate = {str(name_map.get(key) or key): values[index] for key, values in table.items() if key != "headName" and isinstance(values, list) and len(values) > index}
                if picker(candidate, keywords, field=field) is None:
                    periods[index] = (None, "unknown")
        selected = select_report_period(periods, period)
        if selected is not None:
            position = selected
        head_first = head_name[position] if head_name else None

    out: Dict[str, Any] = {
        "_mx_entity": dto.get("entityName") or "",
        "_mx_period": head_first,
    }
    for key, values in table.items():
        if key == "headName":
            continue
        label = name_map.get(key) or name_map.get(str(key)) or str(key)
        if isinstance(values, list) and values:
            out[str(label)] = values[position] if position < len(values) else None
        else:
            out[str(label)] = values
    return out


# ------------------------------------------------------------------
# MXClient —— 封装 HTTP + TTL 缓存
# ------------------------------------------------------------------
class MXClient:
    """妙想 Skills Hub 客户端。无 key 可安全实例化（检查 ``.available``）。"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout: int = 8,
        ttl: int = _MX_TTL,
    ) -> None:
        self.api_key = (api_key or os.getenv("MX_APIKEY") or "").strip()
        self.available = bool(self.api_key)
        self.timeout = timeout
        self._ttl = ttl
        self._cache: Dict[str, List[Any]] = {}  # query -> [ts, result, this-refresh identity]
        self._cache_lock = (
            Lock()
        )  # CrossSourceValidator 在线程池并发调用 query，需保护缓存

    def query(self, tool_query: str, *, deadline: Optional[float] = None, refresh_id: Optional[str] = None) -> Dict[str, Any]:
        """自然语言查询，带 TTL 缓存。无 key 返回 error dict。

        线程安全：CrossSourceValidator 用 ThreadPoolExecutor 并发取多源，
        同一 query 可能被多线程同时 miss → 重复 HTTP（浪费配额）。用锁保证
        缓存读/写的原子性，HTTP 调用在锁外执行避免阻塞其他 query。
        """
        if not self.available:
            return {"error": "MX_APIKEY not set"}
        if deadline is not None and deadline <= time.monotonic():
            return {"error": "timeout"}
        now = time.time()
        with self._cache_lock:
            hit = self._cache.get(tool_query)
            if hit and now - hit[0] < self._ttl and (refresh_id is None or len(hit) > 2 and hit[2] == refresh_id):
                return cast(Dict[str, Any], hit[1])
        # 锁外执行 HTTP（不阻塞其他 query 的缓存读）
        result = _post(QUERY_URL, {"toolQuery": tool_query}, self.api_key, self.timeout, **({"deadline": deadline} if deadline is not None else {}))
        with self._cache_lock:
            self._cache[tool_query] = [now, result, refresh_id]
        return result

    def fetch_snapshot(self, code: str, *, deadline: Optional[float] = None) -> Dict[str, Any]:
        """行情+估值快照：最新价/总市值/流通市值/PE/PB。"""
        refresh = financial_refresh.get()
        return _extract_first_table_row(
            self.query(f"{code} 最新价 总市值 流通市值 市盈率 市净率", **({"refresh_id": refresh[1]} if refresh and refresh[0] == code else {}), **({"deadline": deadline} if deadline is not None else {}))
        )

    def query_financials(self, code: str, period: Optional[str], field: Optional[str] = None, *, deadline: Optional[float] = None) -> Dict[str, Any]:
        """财务指标：营收/归母净利润/ROE/毛利率/营收同比（带报告期）。"""
        suffix = f" {period}" if period else ""
        refresh = financial_refresh.get()
        return _extract_first_table_row(
            self.query(
                f"{code} 营业收入 归属于母公司净利润 净资产收益率 毛利率 营业收入同比增长率 归属于母公司净利润同比增长率{suffix}", **({"refresh_id": refresh[1]} if refresh and refresh[0] == code else {}), **({"deadline": deadline} if deadline is not None else {})
            ), period=period, field=field
        )

    def query_capital(self, code: str, *, deadline: Optional[float] = None) -> Dict[str, Any]:
        """资金类：主力净流入/融资余额。"""
        return _extract_first_table_row(self.query(f"{code} 主力净流入额 融资余额", **({"deadline": deadline} if deadline is not None else {})))


# ------------------------------------------------------------------
# MXSource —— 实现 SourceAdapter
# ------------------------------------------------------------------
class MXSource:
    """MX Data 数据源适配器（实现 :class:`SourceAdapter`）。

    按锚点类别分发到 snapshot/financial/capital 三类自然语言查询；
    ``MXClient`` 的 TTL 缓存保证同 code 同 query 30min 内只 HTTP 一次（控配额）。
    """

    name = "mx"

    def __init__(self, client: Optional[MXClient] = None) -> None:
        self._client = client if client is not None else MXClient()

    @property
    def available(self) -> bool:
        return self._client.available

    def read(
        self, code: str, field: str, period: Optional[str] = None, *, deadline: Optional[float] = None
    ) -> Optional[AnchorReading]:
        """读取单锚点。失败/无 key/无数据 → None（fail-open）。"""
        if not self._client.available or (deadline is not None and deadline <= time.monotonic()):
            return None
        try:
            budget = {"deadline": deadline} if deadline is not None else {}
            picker = _pick_value
            if field in _SNAPSHOT_FIELDS:
                bundle, keywords = self._client.fetch_snapshot(code, **budget), _SNAPSHOT_FIELDS[field]
            elif field in _PERIOD_FIELDS:
                keywords = _FINANCIAL_FIELDS.get(field) or _GROWTH_FIELDS[field]
                if field in _GROWTH_FIELDS:
                    picker = _pick_growth_value
                bundle = self._client.query_financials(code, period, field=field, **budget)
                if period and picker(bundle, keywords, field=field) is None:
                    bundle = self._client.query_financials(code, None, field=field, **budget)
            elif field in _CAPITAL_FIELDS:
                bundle, keywords = self._client.query_capital(code, **budget), _CAPITAL_FIELDS[field]
            else:
                return None
            return self.read_bundle(bundle, field, period)
        except Exception as exc:  # noqa: BLE001 — 单个源异常不影响其他源
            logger.debug("[MXSource] read %s/%s failed: %s", code, field, exc)
            return None

    def read_bundle(self, bundle: Dict[str, Any], field: str, period: Optional[str] = None) -> Optional[AnchorReading]:
        """Parse an acquired bundle without issuing another request."""
        keywords = _SNAPSHOT_FIELDS.get(field) or _FINANCIAL_FIELDS.get(field) or _GROWTH_FIELDS.get(field) or _CAPITAL_FIELDS.get(field)
        if not keywords:
            return None
        picker = _pick_growth_value if field in _GROWTH_FIELDS else _pick_value
        fallback: Optional[AnchorReading] = None
        for label, raw in bundle.items():
            value = picker({label: raw}, keywords, field=field)
            if value is None:
                continue
            actual, basis = report_period_from_fields({**bundle, "report_period": bundle.get("_mx_period")}, label) if field in _PERIOD_FIELDS else (None, "unknown")
            value, unit, currency, error = normalize_anchor_value(field, raw, label, unit=bundle.get("unit"), currency=bundle.get("currency"))
            if value is None:
                continue
            observed = observation_time_from_fields(bundle, bundle.get("_mx_period") if field not in _PERIOD_FIELDS else None)
            reading = AnchorReading(
                source=self.name, value=value,
                caliber=caliber_from_label(field, label),
                period=actual, period_basis=basis,
                requested_period=period if field in _PERIOD_FIELDS else None,
                observed_at=observed, fetched_at=bundle.get("fetched_at"),
                unit=unit, currency=currency, raw_value=str(raw), raw_label=label, normalization_error=error,
                raw_unit=bundle.get("unit"), raw_currency=bundle.get("currency"),
            )
            if not reading_input_reasons(reading, field):
                return reading
            if fallback is None:
                fallback = reading
        return fallback
