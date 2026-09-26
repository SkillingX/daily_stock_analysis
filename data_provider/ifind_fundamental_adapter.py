# -*- coding: utf-8 -*-
"""iFinD 适配器 —— 同花顺 iFinD MCP（验证数据源）。

通过同花顺 iFinD MCP server（streamablehttp）获取专业结构化数据，作为
MX Data（主源）的交叉验证源。

设计（KISS · 高内聚低耦合）：
- :class:`IfindSource` 实现 :class:`SourceAdapter`，通过依赖注入 fetcher 解耦。
  字段映射 / 口径 / 报告期逻辑是纯同步代码，**100% 可单测**（注入同步假 fetcher）。
- :class:`IfindFetcher` 封装真实 async MCP 调用（``fetch`` 同步包装 ``_async_fetch``）。
  进程级单例复用连接（避免 MCP 冷启动）。

Phase 0 探测结果（2026-06-25）：
- iFinD stock MCP 所有工具参数均为 ``query`` 自然语言字符串，非结构化字段。
- 返回格式：``{"code":1,"data":{"answer":"<markdown table>"}}``，需解析 Markdown 表格。
- 主力净流入返回值为「股」单位但量级异常（-37631 股 = -3.76 万元，不合理，
  实际可能是「万/十万」单位刻度），方向验证可参考但不建议严格量级比对。

配置：``IFIND_MCP_ENDPOINT`` / ``IFIND_MCP_TOKEN`` / ``IFIND_MCP_TIMEOUT_SECONDS``。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from .cross_source_validator import AnchorReading

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
# iFinD PE/PB 口径
_CALIBERS: Dict[str, Optional[str]] = {
    "pe_ratio": "TTM",
    "pb_ratio": "TTM",
}


# ------------------------------------------------------------------
# 解析（纯函数，100% 可单测）
# ------------------------------------------------------------------


def _parse_ifind_markdown_table(text: str) -> Dict[str, str]:
    """从 iFinD ``data.answer`` Markdown 表格中提取「表头: 值」映射。

    表格格式（Phase 0 实测）：``|表头1|表头2|...|\n|---|---|---|\n|值1|值2|...|``
    仅解析第一行数据（最新值），忽略参数信息段。
    """
    lines = text.strip().splitlines()
    if not lines:
        return {}
    # 找第一个分隔行（|---|）确定表头
    sep_idx = -1
    for i, line in enumerate(lines):
        if re.match(r"^\|[\s\-:|]+\|$", line) or re.match(r"^\|[\s\-]+", line):
            sep_idx = i
            break
    if sep_idx < 0:
        return {}
    headers = [h.strip() for h in lines[0].split("|") if h.strip()]
    result: Dict[str, str] = {}
    for row in lines[sep_idx + 1 :]:
        cells = [c.strip() for c in row.split("|") if c.strip()]
        if not cells or cells[0] in (
            "# 指标参数信息",
            "# 行情衍生指标日期提示",
            "注:",
            "",
        ):
            continue  # pragma: no cover — Phase 0 响应参数段在表格外，数据行先触发 break
        for i, cell in enumerate(cells):
            if i < len(headers):
                result[headers[i]] = cell
        break  # 只取第一行数据
    return result


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
    table: Dict[str, str], keywords: List[str]
) -> Tuple[Optional[float], str]:
    """从解析后的表中按关键词取数值。

    返回 (value, used_column)。找不到关键词 → (None, "")。
    中文金额单位（万亿/亿/万）由 ``_safe_float`` 统一换算。
    """
    for kw in keywords:
        for col, val in table.items():
            if kw in col:
                v = _safe_float(val)
                if v is not None:
                    return v, col
    return None, ""


def _parse_ifind_response(
    raw_text: str, keywords: List[str], field: str, period: Optional[str]
) -> Optional[AnchorReading]:
    """从 iFinD MCP ``call_tool`` 返回的原始文本解析出 AnchorReading（纯函数）。

    输入：``content[0].text``（JSON 字符串，内含 ``data.answer`` Markdown 表格）。
    解析失败/无匹配值 → None。
    """
    if not raw_text:
        return None
    try:
        resp_json = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    answer = (
        resp_json.get("data", {}).get("answer", "")
        if isinstance(resp_json, dict)
        else str(raw_text)
    )
    table = _parse_ifind_markdown_table(answer)
    if not table:
        return None
    value, _col = _extract_ifind_value(table, keywords)
    if value is None:
        return None
    return AnchorReading(
        source="ifind",
        value=value,
        caliber=_CALIBERS.get(field),
        period=period if field in _PERIOD_FIELDS else None,
    )


# ------------------------------------------------------------------
# IfindFetcher —— async MCP client（单例）
# ------------------------------------------------------------------


class IfindFetcher:
    """iFinD MCP 客户端（async，``fetch`` 同步包装 ``_async_fetch``）。

    Phase 1 改进：常驻 daemon 线程 + 独立事件循环，复用 ``ClientSession``。
    每次 ``fetch`` 通过 ``asyncio.run_coroutine_threadsafe`` 把协程投递到
    那个 loop 上，避免 ``asyncio.run`` 反复启停 loop + 重建 streamablehttp
    连接（实测可让"4 个 anchor 并发拉取"从 16s 缩到 4-6s，并消除
    "GET stream disconnected, reconnecting in 1s" 循环）。

    fail-open 行为保持不变：无 token / 协程异常 / 超时 → 返回 None。
    """

    _instance: Optional["IfindFetcher"] = None

    def __init__(
        self,
        endpoint: Optional[str] = None,
        token: Optional[str] = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._endpoint = (endpoint or os.getenv("IFIND_MCP_ENDPOINT") or "").strip()
        self._token = (token or os.getenv("IFIND_MCP_TOKEN") or "").strip()
        # iFinD MCP is slow under load (5-15s per anchor in practice);
        # we keep a generous default so a single retry round can complete.
        self._timeout = float(
            timeout_seconds
            if timeout_seconds
            else float(os.getenv("IFIND_MCP_TIMEOUT_SECONDS", "30") or 30)
        )
        # Daemon loop (lazy): ``_ensure_loop()`` starts it on first fetch.
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._loop_lock = threading.Lock()
        # Shared ``ClientSession``: opened once via the daemon loop and
        # reused by every subsequent ``call_tool``. The ``session_lock``
        # serialises session recreation / teardown across threads.
        self._session: Any = None
        self._session_context: Any = None
        self._session_ready: bool = False
        self._session_lock = threading.Lock()

    @property
    def available(self) -> bool:
        return bool(self._endpoint and self._token)

    @classmethod
    def get_instance(cls) -> "IfindFetcher":
        """进程级单例（复用连接，避免 MCP 冷启动）。"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _ensure_loop(self) -> Optional[asyncio.AbstractEventLoop]:
        """Start a daemon thread that hosts an event loop. Idempotent."""
        if not self.available:
            return None
        with self._loop_lock:
            if self._loop is not None and self._loop.is_running():
                return self._loop

            ready = threading.Event()

            def _run_loop() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._loop = loop
                ready.set()
                try:
                    loop.run_forever()
                finally:
                    loop.close()

            self._loop_thread = threading.Thread(
                target=_run_loop,
                name="ifind-mcp-loop",
                daemon=True,
            )
            self._loop_thread.start()
            ready.wait(timeout=2.0)
            return self._loop

    def _submit(self, coro: Any, timeout: Optional[float] = None) -> Any:
        """Run ``coro`` on the daemon loop and wait for the result.

        Returns the coroutine result, or ``None`` on any error / timeout.
        Never raises.
        """
        loop = self._ensure_loop()
        if loop is None:
            return None
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.debug("[IfindFetcher] submit failed: %s", exc)
            return None
        try:
            wait = timeout if timeout is not None else self._timeout + 2.0
            return future.result(timeout=wait)
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.debug("[IfindFetcher] future failed: %s", exc)
            return None

    def fetch(
        self, code: str, field: str, period: Optional[str] = None
    ) -> Optional[AnchorReading]:
        """同步获取（封装 async MCP）。无 token/失败 → None。

        Per-call ``asyncio.run`` so each fetch gets a clean event loop
        and the MCP receive-loop semantics work as designed. The
        fallback adapter fans out across anchors via
        ``ThreadPoolExecutor``; each worker thread is responsible for
        driving its own event loop here.
        """
        if not self.available:
            return None
        try:
            return asyncio.run(self._async_fetch(code, field, period))
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.debug("[IfindFetcher] fetch %s/%s failed: %s", code, field, exc)
            return None

    async def _async_fetch(  # pragma: no cover — 真实 MCP 调用，Phase 1 已验证连通性
        self, code: str, field: str, period: Optional[str]
    ) -> Optional[AnchorReading]:
        """Execute one iFinD MCP call.

        We open a fresh ``streamablehttp_client`` + ``ClientSession`` per
        call. Earlier we tried a daemon-hosted shared session
        (``_ensure_session``) but the MCP receive-loop semantics did
        not survive ``run_coroutine_threadsafe`` cleanly when four
        growth anchors were issued back-to-back: the second call would
        hang because the receive loop was still draining the first
        call's response. Per-call sessions are slower (1-2s handshake
        each) but reliable, and the fallback adapter already runs four
        anchors in parallel via ``ThreadPoolExecutor`` so the wall
        clock stays around 2-4s for a fresh batch.
        """
        if field not in _IFIND_ANCHOR_QUERIES:
            return None
        tool, query_tpl, keywords = _IFIND_ANCHOR_QUERIES[field]
        query = query_tpl.format(code=code, period=period or "")
        from mcp import ClientSession  # type: ignore
        from mcp.client.streamable_http import streamablehttp_client  # type: ignore

        headers = {"Authorization": self._token}
        last_error: Optional[str] = None
        for attempt in range(2):
            try:
                async with streamablehttp_client(
                    self._endpoint, headers=headers, timeout=self._timeout
                ) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool(tool, {"query": query})
                # Parsing happens after the context manager closes so
                # we are not holding a connection while doing regex work.
                content = getattr(result, "content", None) or []
                raw_text = next(
                    (
                        getattr(b, "text", "")
                        for b in content
                        if getattr(b, "text", None)
                    ),
                    "",
                )
                return _parse_ifind_response(raw_text, keywords, field, period)
            except Exception as exc:  # noqa: BLE001 — fail-open
                last_error = str(exc)
                logger.debug(
                    "[IfindFetcher] attempt %d failed for %s/%s: %s",
                    attempt + 1,
                    code,
                    field,
                    exc,
                )
                continue
        logger.debug("[IfindFetcher] giving up on %s/%s: %s", code, field, last_error)
        return None

    async def _ensure_session(self) -> Any:
        """Open the shared MCP session on first use; reuse thereafter.

        The session lives on the daemon loop, so it is only safe to
        touch from coroutines running on that loop. A second concurrent
        caller will await ``_session_ready`` and pick up the same
        session once the first caller has finished ``initialize()``.
        """
        with self._session_lock:
            if self._session is not None and self._session_ready:
                return self._session
            # ``streamablehttp_client`` returns an async context manager.
            # We hold it in ``self._session_context`` so the underlying
            # HTTP transport is only torn down on explicit close.
            from mcp import ClientSession  # type: ignore
            from mcp.client.streamable_http import streamablehttp_client  # type: ignore

            headers = {"Authorization": self._token}
            self._session_context = streamablehttp_client(
                self._endpoint,
                headers=headers,
                timeout=self._timeout,
            )
            read, write, _ = await self._session_context.__aenter__()
            self._session = ClientSession(read, write)
            await self._session.initialize()
            self._session_ready = True
            return self._session

    async def _close_session(self) -> None:
        """Tear down the shared session so the next call reconnects."""
        with self._session_lock:
            if self._session is not None:
                try:
                    await self._session.close()
                except Exception:  # noqa: BLE001
                    pass
                self._session = None
                self._session_ready = False
            if self._session_context is not None:
                try:
                    await self._session_context.__aexit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass
                self._session_context = None

    def fetch_main_inflow_series(
        self, code: str, days: int = 12
    ) -> Optional[List[Tuple[str, float]]]:
        """取近 ``days`` 日主力净流入额序列（最新在前）。无 token/失败 → None。

        iFinD ``get_stock_performance`` 对 ``"{code} 近{days}个交易日主力净流入额"`` 返回
        多行每日金额表（实测 688486 近10个交易日→11行；金额列单位在值内「万/元」由
        ``_safe_float`` 换算）。供 capital_flow_provider 算 5/10 日累计，作为 akshare
        ``push2his`` 不可达时的稳定多日源。注：「近{days}日」措辞会被折叠为最新单值，
        故用「个交易日」。
        """
        if not self.available:
            return None
        try:  # pragma: no cover — 真实 MCP 调用（已实测连通）
            return asyncio.run(self._async_fetch_series(code, days))
        except Exception as exc:  # pragma: no cover  # noqa: BLE001 — fail-open
            logger.debug("[IfindFetcher] series %s failed: %s", code, exc)
            return None

    async def _async_fetch_series(  # pragma: no cover — 真实 MCP 调用
        self, code: str, days: int
    ) -> Optional[List[Tuple[str, float]]]:
        """执行一次 iFinD MCP 调用，解析多行序列。"""
        from mcp import ClientSession  # type: ignore
        from mcp.client.streamable_http import streamablehttp_client  # type: ignore

        # Phase 0 实测：「近{days}日主力净流入额」会被 iFinD 折叠为「最新」单值；
        # 改用「近{days}个交易日」措辞才返回多行每日序列（实测近10个交易日→11行）。
        query = f"{code} 近{days}个交易日主力净流入额"
        headers = {"Authorization": self._token}
        async with streamablehttp_client(
            self._endpoint, headers=headers, timeout=self._timeout
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "get_stock_performance", {"query": query}
                )
        content = getattr(result, "content", None) or []
        raw_text = next(
            (getattr(b, "text", "") for b in content if getattr(b, "text", None)), ""
        )
        try:
            resp_json = json.loads(raw_text)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        answer = (
            resp_json.get("data", {}).get("answer", "")
            if isinstance(resp_json, dict)
            else ""
        )
        return _parse_ifind_markdown_series(answer, "主力净流入额")


# ------------------------------------------------------------------
# IfindSource —— SourceAdapter 实现
# ------------------------------------------------------------------


class IfindSource:
    """iFinD 数据源适配器（实现 :class:`SourceAdapter`）。

    依赖注入 fetcher：测试注入同步假 fetcher 覆盖全部映射逻辑；真实
    :class:`IfindFetcher` 通过 ``IfindFetcher.get_instance()`` 注入。
    """

    name = "ifind"

    def __init__(self, fetcher: Optional[IfindFetcher] = None) -> None:
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
        if field not in _IFIND_ANCHOR_QUERIES:
            return None
        try:
            reading = self._fetcher.fetch(code, field, period)
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.debug("[IfindSource] read %s/%s failed: %s", code, field, exc)
            return None
        if reading is None:
            return None
        return AnchorReading(
            source="ifind",
            value=reading.value,
            caliber=_CALIBERS.get(field, reading.caliber),
            period=period if field in _PERIOD_FIELDS else reading.period,
        )
