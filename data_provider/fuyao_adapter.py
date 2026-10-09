# -*- coding: utf-8 -*-
"""同花顺 fuyao (aicubes) REST 适配器 —— fundamental 数据第 4 cross-validation 源。

通过 fuyao REST 端点（默认 ``https://fuyao.aicubes.cn``）拉取估值/行情/财务字段，
作为 MX 主源 + iFinD 验证源 + Choice MCP 验证源之后的**第 4 cross-validation 源**。
由 :mod:`src.agent.tools.cross_validation_helpers` 在 ``enable_fuyao=true`` 时装配。

设计（与 :class:`MxMcpSource` 同范式）：
- :class:`FuyaoSource` 实现 :class:`SourceAdapter`，依赖注入 fetcher 解耦，
  字段映射 / 解析是纯同步代码，**100% 可单测**。
- :class:`FuyaoFetcher` 封装真实 HTTP 调用（``requests.Session`` + 显式预算重试），
  同步阻塞；fuyao 是 REST + 同步范式（与 iFinD/MCP 的 async 范式不同）。
- fail-open：无 key / 抓取异常 / 超时 / 业务错误 → ``None``，不阻塞其他源。

凭据安全（硬约束）：
- API key 运行时从 ``FUYAO_API_KEY`` 环境变量取，**禁止**入库 / 打印。
- 日志中只记 endpoint host + 耗时 + 异常类型，**绝不打印** key 任何片段。

标的符号：fuyao 使用 ``thscode`` 格式（带 ``.SH``/``.SZ``/``.BJ``/``.HK``/``.US`` 后缀），
与项目内部 ticker (``600519`` / ``00700`` / ``AAPL``) 不同；由 :func:`_to_thscode` 转换。
"""

from __future__ import annotations

import logging
import time
import os
import re
from dataclasses import dataclass, replace
from typing import Any, Dict, Optional, Tuple

import requests
from pydantic import BaseModel, ConfigDict, Field

from .cross_source_validator import AnchorReading, caliber_from_label, normalize_anchor_value, observation_time_from_fields, report_period_from_fields, select_report_period

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Pydantic schemas（Layer 3 防御：strict + frozen；畸形输入解析期 422）
# ------------------------------------------------------------------


class FuyaoItem(BaseModel):
    """单条 fuyao 数据点。``fields`` 承载指标值（Any 以容忍 string/int/float/null）；

    缺失字段让 AnchorReading 走 None；数值按返回单位规范化。
    """

    model_config = ConfigDict(strict=True, frozen=True, validate_assignment=True, extra="ignore")

    thscode: str = Field(..., description="带交易所后缀的标的符号")
    report: Optional[str] = Field(default=None, description="fuyao YYYY-N 期间编码")
    fields: Dict[str, Any] = Field(default_factory=dict)


class FuyaoResponse(BaseModel):
    """fuyao REST 响应包络（``{"code": 0, "message": "success", "data": {...}}``）。

    strict + frozen 保证下游解析不会因意外修改污染上游契约；``extra="ignore"``
    对 API 后续新增字段（如 ``timestamp``）保持容忍。
    """

    model_config = ConfigDict(strict=True, frozen=True, validate_assignment=True, extra="ignore")

    code: int = Field(..., description="业务状态码；0 = success")
    message: str = Field(default="")
    request_id: Optional[str] = Field(default=None)
    data: Optional[Dict[str, Any]] = Field(default=None)


# ------------------------------------------------------------------
# 字段 → endpoint + 响应 item key 映射
# ------------------------------------------------------------------


@dataclass(frozen=True)
class _EndpointSpec:
    """字段到 fuyao REST 端点的映射规范。"""

    path: str           # e.g. "/v1/valuations/snapshot"
    item_key: str       # data.items[*].fields 中的指标名
    needs_period: bool  # 是否需要 report 参数


# A 股 6 位数字按交易所分桶：
# 60/688/9 → 上交所（SH）；00/30/20 → 深交所（SZ）；8/4 → 北交所（BJ）。
_EXCHANGE_BY_PREFIX: Dict[str, str] = {
    "60": "SH", "688": "SH", "9": "SH",
    "00": "SZ", "30": "SZ", "20": "SZ",
    "8": "BJ", "4": "BJ",
}

_ENDPOINT_BY_FIELD: Dict[str, _EndpointSpec] = {
    "current_price": _EndpointSpec("/v1/quotation/snapshot", "price", False),
    "total_mv": _EndpointSpec("/v1/quotation/snapshot", "total_mv", False),
    "circ_mv": _EndpointSpec("/v1/quotation/snapshot", "circ_mv", False),
    "pe_ratio": _EndpointSpec("/v1/valuations/snapshot", "pe_ttm", False),
    "pb_ratio": _EndpointSpec("/v1/valuations/snapshot", "pb_mrq", False),
    "revenue": _EndpointSpec("/v1/financial/income", "revenue", True),
    "net_profit": _EndpointSpec("/v1/financial/income", "net_profit", True),
    "gross_margin": _EndpointSpec("/v1/financial/income", "gross_margin", True),
    "roe": _EndpointSpec("/v1/financial/balance", "roe", True),
    "revenue_yoy": _EndpointSpec("/v1/financial/growth", "revenue_yoy", True),
    "net_profit_yoy": _EndpointSpec("/v1/financial/growth", "net_profit_yoy", True),
}

_CALIBERS: Dict[str, Optional[str]] = {
    "pe_ratio": "TTM",
    "pb_ratio": "MRQ",
}


# ------------------------------------------------------------------
# 转换（纯函数，100% 可单测）
# ------------------------------------------------------------------


def _to_thscode(code: str) -> str:
    """项目内部 ticker → fuyao ``thscode``（带 ``.SH`` / ``.SZ`` / ``.BJ`` / ``.HK`` / ``.US`` 后缀）。

    支持形态：
    - A 股 6 位数字: ``600519`` → ``600519.SH``；``000001`` → ``000001.SZ``
    - 港股 5 位数字: ``00700`` → ``00700.HK``
    - 美股 1-6 位字母: ``AAPL`` → ``AAPL.US``；大小写不敏感，统一转大写
    - 已带后缀: ``600519.SH`` / ``00700.HK`` → 透传并归一化大小写

    不可识别格式 → ``ValueError``（让 Source 走 fail-open None）。
    """
    s = (code or "").strip().upper()
    if not s:
        raise ValueError("empty code")
    if "." in s:
        head, _, tail = s.partition(".")
        if tail in {"SH", "SZ", "BJ", "HK", "US"} and head:
            return f"{head}.{tail}"
        raise ValueError(f"unsupported suffix: {tail!r}")
    if s.isdigit() and len(s) == 5:
        return f"{s}.HK"
    if s.isdigit() and len(s) == 6:
        for prefix_len in (2, 3, 1):
            prefix = s[:prefix_len]
            ex = _EXCHANGE_BY_PREFIX.get(prefix)
            if ex:
                return f"{s}.{ex}"
        raise ValueError(f"unknown A-share prefix: {s}")
    if s.isalpha() and 1 <= len(s) <= 6:
        return f"{s}.US"
    raise ValueError(f"unsupported code format: {code!r}")


# 用户可见 period 字符串 → fuyao report 编码（YYYY-N；N=1/2/3/4 对应 Q1/中报/Q3/年报）。
# 关键：``2024年年度报告`` 含两个「年」（年+年度），所以年报模式必须允许可选前缀「年」。
_PERIOD_PATTERNS: Tuple[Tuple[str, str], ...] = (
    # 年度：2024年报 / 2024年度报告 / 2024年年度报告 / 2024Q4
    (r"^(\d{4})\s*(?:Q4|(?:年)?(?:年度报告|年报))$", r"\1-4"),
    # Q3：2024Q3 / 2024三季度 / 2024三季度报告 / 2024第三季度报告
    (r"^(\d{4})\s*(?:Q3|第?三季(?:度)?(?:报(?:告)?)?)$", r"\1-3"),
    # Q2 / 半年：2024Q2 / 2024半年报 / 2024半年度报告 / 2024中期 / 2024中报
    (r"^(\d{4})\s*(?:Q2|半(?:年|年度?)?(?:报(?:告)?)?|中(?:期|报))$", r"\1-2"),
    # Q1：2024Q1 / 2024一季度 / 2024一季度报告 / 2024第一季度报告
    (r"^(\d{4})\s*(?:Q1|第?一季(?:度)?(?:报(?:告)?)?)$", r"\1-1"),
)


def _map_period(period: Optional[str]) -> Optional[str]:
    """用户可见 period 字符串 → fuyao ``YYYY-N`` 编码。

    不支持的形态（None、空、未知格式、未来期间）→ ``None``；财务字段由
    :class:`FuyaoFetcher` 看到 None 时直接返回 None（避免错锚）。
    """
    if not period:
        return None
    s = period.strip()
    for pattern, repl in _PERIOD_PATTERNS:
        m = re.match(pattern, s)
        if m:
            return re.sub(pattern, repl, s)
    return None


def _endpoint_for(field: str) -> Optional[_EndpointSpec]:
    """查表：field → endpoint spec。未知字段 → None（fail-open）。"""
    return _ENDPOINT_BY_FIELD.get(field)


# ------------------------------------------------------------------
# 解析（纯函数，100% 可单测）
# ------------------------------------------------------------------


def _parse_fuyao_response(
    raw_text: str,
    field: str,
    period: Optional[str],
) -> Optional[AnchorReading]:
    """从 fuyao REST 响应的 JSON 文本解析出 AnchorReading（纯函数）。

    解析失败 / 业务错误 / 缺字段 → None。
    """
    if not raw_text:
        return None
    try:
        resp = FuyaoResponse.model_validate_json(raw_text)
    except Exception as exc:  # noqa: BLE001 — Pydantic 校验失败 → fail-open
        logger.debug("[fuyao] parse: invalid response shape: %s", exc)
        return None
    if resp.code != 0:
        logger.debug(
            "[fuyao] parse: business error code=%s msg=%s",
            resp.code,
            resp.message,
        )
        return None
    if resp.data is None:
        return None
    items_raw = resp.data.get("items")
    if not isinstance(items_raw, list) or not items_raw:
        return None
    spec = _endpoint_for(field)
    if spec is None:
        return None
    readings: list[AnchorReading] = []
    for raw in items_raw:
        try:
            item = FuyaoItem.model_validate(raw)
        except Exception:  # noqa: BLE001 — 跳过畸形 item
            continue
        value = normalize_anchor_value(field, item.fields.get(spec.item_key), spec.item_key)[0]
        if value is None:
            continue
        period_fields = dict(item.fields)
        if item.report is not None:
            encoded = re.fullmatch(r"(\d{4})-([1-4])", item.report)
            report_label = f"{encoded[1]}{('一季报', '中报', '三季报', '年报')[int(encoded[2]) - 1]}" if encoded else item.report
            period_fields.setdefault("report_period", report_label)
        actual, basis = report_period_from_fields(period_fields, spec.item_key)
        value, unit, currency, error = normalize_anchor_value(field, item.fields[spec.item_key], spec.item_key, unit=item.fields.get(f"{spec.item_key}_unit", item.fields.get("unit")), currency=item.fields.get("currency"))
        if value is None:
            continue
        readings.append(AnchorReading(
            source="fuyao",
            value=value,
            caliber=_CALIBERS.get(field) or caliber_from_label(field, spec.item_key),
            period=actual if spec.needs_period else None,
            period_basis=basis if spec.needs_period else "unknown",
            requested_period=period if spec.needs_period else None,
            observed_at=observation_time_from_fields(item.fields), fetched_at=item.fields.get("fetched_at"),
            unit=unit, currency=currency, raw_value=str(item.fields[spec.item_key]), raw_label=spec.item_key, normalization_error=error,
            raw_unit=item.fields.get(f"{spec.item_key}_unit", item.fields.get("unit")), raw_currency=item.fields.get("currency"),
        ))
    selected = select_report_period([(reading.period, reading.period_basis) for reading in readings], period)
    return readings[selected if selected is not None else 0] if readings else None


# ------------------------------------------------------------------
# FuyaoFetcher —— 同步 HTTP 客户端（requests.Session + retry adapter）
# ------------------------------------------------------------------


class FuyaoFetcher:
    """fuyao REST 同步客户端。

    fail-open 行为：无 endpoint/api_key / HTTP 异常 / 超时 / 业务错误 → 返回 None。
    构造由 :func:`src.agent.tools.cross_validation_helpers._build_sources`
    在每次 reset / 配置变更时新生成实例；不维护进程级单例（避免 key 跨
    实例泄漏 + 测试实例串扰）。
    """

    def __init__(
        self,
        endpoint: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        # 区分 None（用环境变量/默认回落）与 ""（显式禁用 → available=False）。
        # env 空串（FUYAO_ENDPOINT=）走 None 路径回落默认：占位符场景下避免静默失败。
        if endpoint is None:
            env_value = os.getenv("FUYAO_ENDPOINT")
            endpoint = env_value if env_value else "https://fuyao.aicubes.cn"
        self._endpoint = endpoint.strip().rstrip("/") if endpoint else ""
        self._api_key = (api_key or os.getenv("FUYAO_API_KEY") or "").strip()
        self._timeout = float(timeout_seconds)
        self._session = self._build_session()

    @staticmethod
    def _build_session() -> requests.Session:
        """构造无隐藏重试的会话；fetch 按剩余预算显式重试。"""
        return requests.Session()

    @property
    def available(self) -> bool:
        return bool(self._endpoint and self._api_key)

    def fetch(
        self, code: str, field: str, period: Optional[str] = None, *, deadline: Optional[float] = None
    ) -> Optional[AnchorReading]:
        """同步获取。无 token / 失败 → None。"""
        if not self.available:
            return None
        spec = _endpoint_for(field)
        if spec is None:
            return None
        try:
            thscode = _to_thscode(code)
        except ValueError:
            return None
        payload: Dict[str, Any] = {"thscode": thscode}
        if spec.needs_period:
            mapped_period = _map_period(period)
            if mapped_period is None:
                # 财务字段缺 period：无法定锚，宁可不抓而非错锚。
                return None
            payload["report"] = mapped_period
        url = f"{self._endpoint}{spec.path}"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        for attempt in range(3):
            timeout = self._timeout if deadline is None else min(self._timeout, deadline - time.monotonic())
            if timeout <= 0:
                return None
            try:
                resp = self._session.post(url, json=payload, headers=headers, timeout=timeout)
            except requests.RequestException as exc:
                logger.debug("[FuyaoFetcher] POST %s failed: %s", spec.path, type(exc).__name__)
                return None
            if resp.status_code == 200:
                return _parse_fuyao_response(resp.text, field, period)
            if resp.status_code not in (429, 500, 502, 503, 504) or attempt == 2:
                logger.debug("[FuyaoFetcher] non-200 for %s/%s: status=%s", code, field, resp.status_code)
                return None
            pause = 0.3 * (2 ** attempt)
            if deadline is not None and deadline - time.monotonic() <= pause:
                return None
            time.sleep(pause)
        return None


# ------------------------------------------------------------------
# FuyaoSource —— SourceAdapter 实现
# ------------------------------------------------------------------


class FuyaoSource:
    """fuyao 数据源适配器（实现 :class:`SourceAdapter`）。

    依赖注入 fetcher：测试注入同步假 fetcher 覆盖全部映射逻辑；
    真实 :class:`FuyaoFetcher` 由 :mod:`src.agent.tools.cross_validation_helpers`
    直接 ``FuyaoFetcher(endpoint=, api_key=, ...)`` 构造并注入。
    """

    name = "fuyao"

    def __init__(self, fetcher: Optional[FuyaoFetcher] = None) -> None:
        self._fetcher = fetcher

    @property
    def available(self) -> bool:
        return bool(self._fetcher and getattr(self._fetcher, "available", False))

    def read(
        self, code: str, field: str, period: Optional[str] = None, *, deadline: Optional[float] = None
    ) -> Optional[AnchorReading]:
        """同步读取。无 fetcher / 未知字段 / 失败 → None（fail-open）。"""
        if self._fetcher is None:
            return None
        try:
            reading = self._fetcher.fetch(code, field, period, **({"deadline": deadline} if deadline is not None else {}))
        except Exception as exc:  # noqa: BLE001 — fail-open：单源异常不影响其他源
            logger.debug("[FuyaoSource] read %s/%s failed: %s", code, field, exc)
            return None
        if reading is None:
            return None
        return replace(reading, source=self.name, requested_period=reading.requested_period if reading.requested_period is not None else period if field in _ENDPOINT_BY_FIELD and _ENDPOINT_BY_FIELD[field].needs_period else None)
