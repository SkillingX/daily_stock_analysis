# -*- coding: utf-8 -*-
"""东财 Choice MCP（妙想 MCP）数据源适配器（KISS · 高内聚低耦合）。

通过 Choice MCP 标准 streamable-http 端点获取专业结构化财务/估值数据，
作为 fundamental 数据的**第三 cross-validation 源**（与 MX 主源 + iFinD 验证源并列）。
由 :mod:`src.agent.tools.cross_validation_helpers` 在 ``enable_mx_mcp=true`` 时装配。

设计（与 :class:`IfindFetcher` 同范式）：
- :class:`MxMcpSource` 实现 :class:`SourceAdapter`，通过依赖注入 fetcher 解耦。
  字段映射 / 解析是纯同步代码，**100% 可单测**。
- :class:`MxMcpFetcher` 封装真实 async MCP 调用（``fetch`` 同步包装 ``_async_fetch``），
  per-call ``asyncio.run``（仿 iFinD Phase 1：稳定 + 简单）。
- fail-open：无 key / 抓取异常 / 超时 → ``None``，不阻塞其他数据源。

凭据安全（硬约束）：
- API key 运行时从 ``MX_MCP_API_KEY`` 环境变量取，**禁止**入库 / 打印。
- 日志中只记 endpoint 主机名 + 耗时 + 异常类型，**绝不打印** key 任何片段。
- ``endpoint`` 默认官方 streamable-http：``https://mxapi.eastmoney.com/mxds/mcp``。
- 鉴权 Header：``em_api_key``（不是 ``emcp_api_key``，不是 ``apikey``）。

注意：本模块**只读**，不集成 ``mx_stock_simulator_*`` 等模拟交易类工具。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from .cross_source_validator import AnchorReading
from .ifind_fundamental_adapter import _safe_float

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# 锚点 → (tool, 自然语言 query 模板, 关键词列表)
# ------------------------------------------------------------------
#
# field 名对齐 :data:`data_provider.cross_source_validator.ANCHOR_SPECS`；
# 工具统一用 ``mx_ashare_finance_data``（自然语言 query）。
# 关键词用于解析返回结果中的"指标名: 值"映射（容错关键词列表）。
#
# 量级（T（万亿）/千亿/百亿/十亿/亿/万）由 ``_safe_float``（借自 ifind）统一换算。
_MX_MCP_ANCHOR_QUERIES: Dict[str, Tuple[str, str, List[str]]] = {
    "current_price": (
        "mx_ashare_finance_data",
        "{code} 最新价",
        # 真实 server 对「最新价」查询实际返回指标名「收盘价」，加首选以正确匹配。
        ["收盘价（元）", "收盘价", "最新价（元）", "最新价"],
    ),
    "pe_ratio": (
        "mx_ashare_finance_data",
        "{code} 市盈率PE TTM",
        ["市盈率(PE,TTM)", "市盈率PE(TTM)", "市盈率（TTM）"],
    ),
    "pb_ratio": (
        "mx_ashare_finance_data",
        "{code} 市净率PB",
        # 真实 server 返回指标名「市净率PB」（旧 kw 反而是更长的字符串，substring 匹配 kw-in-col 失败）。
        ["市净率PB", "市净率(PB,最新)", "市净率PB(最新)", "市净率（最新）"],
    ),
    "total_mv": (
        "mx_ashare_finance_data",
        "{code} 总市值",
        ["总市值（元）", "总市值"],
    ),
    "circ_mv": (
        "mx_ashare_finance_data",
        "{code} 流通市值",
        ["流通市值（元）", "流通市值"],
    ),
    "revenue": (
        "mx_ashare_finance_data",
        "{code} {period} 营业收入",
        ["营业收入（元）", "营业收入"],
    ),
    "net_profit": (
        "mx_ashare_finance_data",
        "{code} {period} 归属于母公司所有者的净利润",
        # 真实 server 返回指标名「归属于母公司股东的净利润」（不是「所有者的」）。
        [
            "归属于母公司股东的净利润（元）",
            "归属于母公司股东的净利润",
            "归属于母公司所有者的净利润（元）",
            "归属于母公司所有者的净利润",
            "归母净利润（元）",
            "归母净利润",
        ],
    ),
    "roe": (
        "mx_ashare_finance_data",
        "{code} {period} 净资产收益率ROE",
        ["净资产收益率ROE（%）", "净资产收益率ROE(%)", "净资产收益率ROE"],
    ),
    "gross_margin": (
        "mx_ashare_finance_data",
        "{code} {period} 销售毛利率",
        ["销售毛利率（%）", "销售毛利率(%)", "销售毛利率", "毛利率"],
    ),
    "revenue_yoy": (
        "mx_ashare_finance_data",
        "{code} {period} 营业收入同比增长率",
        ["营业收入同比增长率（%）", "营业收入同比增长率(%)", "营业收入同比增长率"],
    ),
    "net_profit_yoy": (
        "mx_ashare_finance_data",
        "{code} {period} 净利润同比",
        [
            "归属母公司股东的净利润(同比增长率)（%）",
            "归属母公司股东的净利润(同比增长率)(%)",
            "净利润同比增长率（%）",
            "净利润同比增长率(%)",
        ],
    ),
}

# 财务字段（需要 period）；其余字段 period 不透传
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


def _extract_key_value_pairs(payload: Any) -> Dict[str, str]:
    """从 Choice MCP 响应任意层级抽取 ``{指标名: 字符串值}`` 映射。

    支持形态（按发现顺序）：
    1. **Choice MCP 真实 shape**（生产）：
       ``{"data": [{"columns": [...], "items": [["指标名", 值, 值, ...]], ...}]}``
       —— ``items[0][0]`` 是指标名、``items[0][1]`` 是最新期值（columns[1] 通常是最近日期 / 期间）。
    2. 顶层 ``dict`` 含 ``response`` 键 → 递归（旧 fixture 兼容）。
    3. ``response``/顶层 ``dict`` 是 ``dict`` → 找形如 ``{key: value}`` 的所有键值对。
    4. ``response``/顶层是 ``str``（JSON 或 Markdown 表）→ 进一步 ``_parse_response_str``。

    兼容 List / 嵌套 dict；只保留标量值（``str / int / float``）。
    """
    if isinstance(payload, dict):
        # 1) Choice MCP 真实 shape: {"data": [{"columns": [], "items": [[指标, 值, ...]]}, ...]}
        data_list = payload.get("data")
        if isinstance(data_list, list) and data_list:
            for elem in data_list:
                if isinstance(elem, dict):
                    items = elem.get("items")
                    if isinstance(items, list) and items:
                        first_row = items[0]
                        if isinstance(first_row, list) and len(first_row) >= 2:
                            metric = str(first_row[0]).strip()
                            latest_value = str(first_row[1]).strip()
                            if metric:
                                return {metric: latest_value}
        # 2) {"response": ...} 包装（递归；iFind / 旧 fixture 兼容）
        if "response" in payload:
            return _extract_key_value_pairs(payload["response"])
        # 3) 扁平 dict（含一层嵌套 → 父键.子键 前缀）
        out: Dict[str, str] = {}
        for k, v in payload.items():
            if isinstance(v, (str, int, float)) and not isinstance(v, bool):
                out[str(k)] = str(v)
            elif isinstance(v, dict):
                for kk, vv in v.items():
                    if isinstance(vv, (str, int, float)) and not isinstance(vv, bool):
                        out[f"{k}.{kk}"] = str(vv)
        return out
    if isinstance(payload, list):
        for item in payload:
            found = _extract_key_value_pairs(item)
            if found:
                return found
        return {}
    if isinstance(payload, str):
        return _parse_response_str(payload)
    return {}


def _parse_response_str(text: str) -> Dict[str, str]:
    """解析 Choice MCP ``response`` 字符串字段。

    - 若 ``text`` 是合法 JSON → 走 ``_extract_key_value_pairs`` 递归抽取
    - 否则按 Markdown 表格（``|col|val|`` / 分隔行 ``|---|``）解析首行数据
    - 都失败 → ``{}``
    """
    text = (text or "").strip()
    if not text:
        return {}
    # 优先尝试 JSON
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    if parsed is not None:
        return _extract_key_value_pairs(parsed)
    # 退化：Markdown 表（与 iFinD 形态类似，宽松匹配首行数据）
    return _parse_markdown_first_row(text)


def _parse_markdown_first_row(text: str) -> Dict[str, str]:
    """Markdown 表首行数据 → ``{header: value}``。

    仅在响应字段不是 JSON 时退化使用；找第一个 ``|---|`` 分隔行确定表头，
    取下一行数据。无分隔行 → ``{}``。
    """
    lines = text.splitlines()
    sep_idx = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|") and "-" in stripped:
            sep_idx = i
            break
    if sep_idx < 0:
        return {}
    headers = [h.strip() for h in lines[0].split("|") if h.strip()]
    result: Dict[str, str] = {}
    for row in lines[sep_idx + 1 :]:
        cells = [c.strip() for c in row.split("|") if c.strip()]
        if not cells or cells[0].startswith("#"):
            continue
        for i, cell in enumerate(cells):
            if i < len(headers):
                result[headers[i]] = cell
        break  # 只取首行数据
    return result


def _pick_value(pairs: Dict[str, str], keywords: List[str]) -> Tuple[Optional[float], str]:
    """从 ``pairs`` 中按关键词模糊匹配取值。

    返回 ``(value, used_column)``。找不到 → ``(None, "")``。
    """
    for kw in keywords:
        for col, val in pairs.items():
            if kw in col:
                v = _safe_float(val)
                if v is not None:
                    return v, col
    return None, ""


def _parse_mx_mcp_response(
    raw_text: str, keywords: List[str], field: str, period: Optional[str]
) -> Optional[AnchorReading]:
    """从 Choice MCP ``call_tool`` 返回的原始文本解析出 AnchorReading（纯函数）。

    输入：``content[0].text``（JSON 字符串，含 ``endpoint / request / response / success_hint / error_hint``）。
    解析失败/无匹配值 → None。

    解析路径走完仍空（响应不是任何已知 shape：MCP data/list/JSON/Markdown），
    会打一条 WARN，便于运维在生产发现 shape 漂移或 server 异常。
    """
    if not raw_text:
        return None
    pairs = _extract_key_value_pairs(json.loads(raw_text) if raw_text.strip().startswith(("{", "[")) else raw_text)
    if not pairs:
        # 高信噪比预警：响应不是 MCP / JSON / Markdown 任何已知 shape。
        # 这是上游协议变更或 server 异常的强信号，而不是单字段缺失。
        logger.warning(
            "[mx_mcp] parse: response was not in any known shape (MCP/JSON/Markdown); "
            "first 80 chars: %s",
            (raw_text[:80] + "...") if len(raw_text) > 80 else raw_text,
        )
        return None
    value, _col = _pick_value(pairs, keywords)
    if value is None:
        return None
    return AnchorReading(
        source="mx_mcp",
        value=value,
        caliber="TTM" if field == "pe_ratio" else None,
        period=period if field in _PERIOD_FIELDS else None,
    )


# ------------------------------------------------------------------
# MxMcpFetcher —— async MCP client（per-call asyncio.run）
# ------------------------------------------------------------------


class MxMcpFetcher:
    """Choice MCP（妙想 MCP）async 客户端。

    Per-call ``asyncio.run()`` 同步包装 ``_async_fetch``（仿 iFinD Phase 1
    范式：避开 daemon-thread 共享 session 的 receive-loop 问题；实测 1-2s
    握手 + 并发 4 锚点 4-6s 总耗时）。

    fail-open 行为：无 endpoint/api_key / 协程异常 / 超时 → 返回 None。

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
        # 否则空字符串会被 `or` 链回落默认，破坏显式禁用的契约。
        if endpoint is None:
            endpoint = (
                os.getenv("MX_MCP_ENDPOINT")
                or "https://mxapi.eastmoney.com/mxds/mcp"
            )
        self._endpoint = endpoint.strip() if endpoint else ""
        self._api_key = (api_key or os.getenv("MX_MCP_API_KEY") or "").strip()
        self._timeout = float(timeout_seconds)

    @property
    def available(self) -> bool:
        return bool(self._endpoint and self._api_key)

    def fetch(
        self, code: str, field: str, period: Optional[str] = None
    ) -> Optional[AnchorReading]:
        """同步获取（封装 async MCP）。无 token/失败 → None。"""
        if not self.available:
            return None
        if field not in _MX_MCP_ANCHOR_QUERIES:
            return None
        try:
            return asyncio.run(self._async_fetch(code, field, period))
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.debug(
                "[MxMcpFetcher] fetch %s/%s failed: %s", code, field, exc
            )
            return None

    async def _async_fetch(  # pragma: no cover — 真实 MCP 调用，CI 不覆盖
        self, code: str, field: str, period: Optional[str]
    ) -> Optional[AnchorReading]:
        """执行一次 Choice MCP 调用。"""
        import inspect
        from mcp import ClientSession  # type: ignore
        # mcp 2.x renamed ``streamablehttp_client`` → ``streamable_http_client`` AND changed
        # the signature from ``(url, headers=..., timeout=...)`` to ``(url, *, http_client=...)``.
        # Detect at runtime; pass headers + timeout via ``httpx.AsyncClient`` for 2.x, pass
        # kwargs directly for 1.x legacy.
        try:  # pragma: no cover — real MCP call path, CI not covered
            from mcp.client.streamable_http import streamable_http_client  # type: ignore
        except ImportError:  # pragma: no cover — 1.x legacy
            from mcp.client.streamable_http import (  # type: ignore
                streamablehttp_client,  # type: ignore[attr-defined]
            )
            streamable_http_client = streamablehttp_client  # type: ignore[assignment]

        sig_params = inspect.signature(streamable_http_client).parameters
        uses_http_client_kwarg = "http_client" in sig_params

        tool, query_tpl, keywords = _MX_MCP_ANCHOR_QUERIES[field]
        query = query_tpl.format(code=code, period=period or "")
        headers = {"em_api_key": self._api_key}
        last_error: Optional[str] = None
        for attempt in range(2):
            try:
                if uses_http_client_kwarg:  # mcp >= 2.0
                    import httpx as _httpx  # type: ignore

                    async with streamable_http_client(
                        self._endpoint,
                        http_client=_httpx.AsyncClient(
                            headers=headers, timeout=self._timeout
                        ),  # type: ignore[arg-type] — httpx vs httpx2 namespace mismatch
                    ) as streams:
                        # mcp 2.x yields 2-tuple; 1.x legacy yielded 3-tuple.
                        if len(streams) == 3:  # pragma: no cover — 1.x legacy
                            read, write, _ = streams  # type: ignore[misc]
                        else:
                            read, write = streams  # type: ignore[misc]
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            result = await session.call_tool(tool, {"query": query})
                else:  # mcp < 2.0 (legacy)
                    async with streamable_http_client(
                        self._endpoint,
                        headers=headers,  # type: ignore[call-arg] — 1.x legacy kwarg
                        timeout=self._timeout,  # type: ignore[call-arg]
                    ) as streams:
                        if len(streams) == 3:  # pragma: no cover — 1.x legacy
                            read, write, _ = streams  # type: ignore[misc]
                        else:
                            read, write = streams  # type: ignore[misc]
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            result = await session.call_tool(tool, {"query": query})
                # 解析在上下文管理器关闭后做，避免持连接做正则
                content = getattr(result, "content", None) or []
                raw_text = next(
                    (
                        getattr(b, "text", "")
                        for b in content
                        if getattr(b, "text", None)
                    ),
                    "",
                )
                return _parse_mx_mcp_response(raw_text, keywords, field, period)
            except Exception as exc:  # noqa: BLE001 — fail-open
                last_error = str(exc)
                logger.debug(
                    "[MxMcpFetcher] attempt %d failed for %s/%s: %s",
                    attempt + 1,
                    code,
                    field,
                    exc,
                )
                continue
        logger.debug(
            "[MxMcpFetcher] giving up on %s/%s: %s", code, field, last_error
        )
        return None


# ------------------------------------------------------------------
# MxMcpSource —— SourceAdapter 实现
# ------------------------------------------------------------------


class MxMcpSource:
    """Choice MCP 数据源适配器（实现 :class:`SourceAdapter`）。

    依赖注入 fetcher：测试注入同步假 fetcher 覆盖全部映射逻辑；
    真实 :class:`MxMcpFetcher` 由 :mod:`src.agent.tools.cross_validation_helpers`
    直接 ``MxMcpFetcher(endpoint=, api_key=, ...)`` 构造并注入。
    """

    name = "mx_mcp"

    def __init__(self, fetcher: Optional[MxMcpFetcher] = None) -> None:
        self._fetcher = fetcher

    @property
    def available(self) -> bool:
        return bool(self._fetcher and getattr(self._fetcher, "available", False))

    def read(
        self, code: str, field: str, period: Optional[str] = None
    ) -> Optional[AnchorReading]:
        """同步读取。无 fetcher / 未知字段 / 失败 → None（fail-open）。"""
        if self._fetcher is None:
            return None
        if field not in _MX_MCP_ANCHOR_QUERIES:
            return None
        try:
            reading = self._fetcher.fetch(code, field, period)
        except Exception as exc:  # noqa: BLE001 — fail-open：MX 异常不影响其他源
            logger.debug("[MxMcpSource] read %s/%s failed: %s", code, field, exc)
            return None
        if reading is None:
            return None
        return AnchorReading(
            source=self.name,
            value=reading.value,
            caliber=reading.caliber,
            period=reading.period,
        )
