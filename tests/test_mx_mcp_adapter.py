# -*- coding: utf-8 -*-
"""mx_mcp_adapter 单测 —— 100% 覆盖（网络层 pragma）。

覆盖：
- ``_pick_value``：关键词模糊匹配取值。
- ``_parse_markdown_first_row``：Markdown 表首行数据抽取。
- ``_extract_key_value_pairs``：JSON 嵌套 dict / list / 字符串 / 真实 MCP shape 四种形态。
- ``_parse_mx_mcp_response``：合法 JSON / Markdown 退化 / 无匹配值 / 未知 shape WARN。
- ``MxMcpSource``（SourceAdapter 实现，依赖注入 fetcher，fail-open 矩阵）。
- ``MxMcpFetcher``（available False → fetch None、凭据脱敏）。
- 凭据安全测试：logger 不打印 API key 任何片段。
"""

from __future__ import annotations

import json
import logging
import os
import unittest
from typing import Any, List, Optional

from data_provider.cross_source_validator import AnchorReading
from data_provider.mx_mcp_adapter import (
    MxMcpFetcher,
    MxMcpSource,
    _extract_key_value_pairs,
    _parse_markdown_first_row,
    _parse_mx_mcp_response,
    _pick_value,
)


class _FakeFetcher:
    """MxMcpFetcher 替身（注入 MxMcpSource 测试同步逻辑）。"""

    def __init__(
        self,
        available: bool = True,
        fetch_fn: Optional[Any] = None,
    ) -> None:
        self.available = available
        self._fetch_fn = fetch_fn or (lambda code, field, period: None)

    def fetch(self, code: str, field: str, period: Optional[str] = None) -> Optional[AnchorReading]:
        return self._fetch_fn(code, field, period)


# ------------------------------------------------------------------
# 解析层单测
# ------------------------------------------------------------------


_SYNTHETIC_NESTED_JSON = json.dumps(
    {
        "endpoint": "https://example.com/api",
        "request": "{...}",
        "response": {
            "code": "600519",
            "name": "贵州茅台",
            "data": {
                "最新价（元）": "1207.68",
                "市盈率(PE,TTM)": "18.25",
                "总市值（元）": "1.5097万亿",
            },
        },
        "success_hint": "true",
        "error_hint": "",
    },
    ensure_ascii=False,
)


_SYNTHETIC_MARKDOWN = (
    "|证券代码|证券简称|最新价（元）|市盈率(PE,TTM)|\n"
    "|---|---|---|---|\n"
    "|600519.SH|贵州茅台|1207.68|18.25|\n"
)


# ------------------------------------------------------------------
# 真实 Choice MCP 响应 shape（生产）：{"data": [{"columns": [], "items": []}]}
# 与 _SYNTHETIC_NESTED_JSON（旧 fixture）并行保留以验证向后兼容。
# ------------------------------------------------------------------

_SYNTHETIC_REAL_MCP_CURRENT_PRICE = json.dumps(
    {
        "data": [
            {
                "columns": [
                    "贵州茅台(600519.SH)",
                    "2026-09-24(日)",
                    "2026-09-23(日)",
                    "2026-09-22(日)",
                    "2026-09-21(日)",
                    "2026-09-18(日)",
                ],
                "items": [
                    ["收盘价", "1237元", "1251.24元", "1253.8元", "1252.57元", "1257.12元"],
                ],
                "meta": {"dataType": "数据浏览器"},
                "sheetName": "贵州茅台(600519.SH)",
            }
        ]
    },
    ensure_ascii=False,
)


_SYNTHETIC_REAL_MCP_NET_PROFIT = json.dumps(
    {
        "data": [
            {
                "columns": ["贵州茅台(600519.SH)", "2024年报"],
                "items": [
                    ["归属于母公司股东的净利润", "862.3亿元"],
                ],
                "meta": {},
                "sheetName": "贵州茅台(600519.SH)",
            }
        ]
    },
    ensure_ascii=False,
)


class TestExtractKeyValuePairs(unittest.TestCase):
    def test_nested_json_response(self):
        pairs = _extract_key_value_pairs(json.loads(_SYNTHETIC_NESTED_JSON))
        # 嵌套 dict 会被扁平成 "data.最新价（元）"
        self.assertEqual(pairs.get("data.最新价（元）"), "1207.68")
        self.assertEqual(pairs.get("data.市盈率(PE,TTM)"), "18.25")

    def test_flat_dict_response(self):
        pairs = _extract_key_value_pairs({"最新价（元）": "1207.68", "市盈率(PE,TTM)": "18.25"})
        self.assertEqual(pairs["最新价（元）"], "1207.68")
        self.assertEqual(pairs["市盈率(PE,TTM)"], "18.25")

    def test_string_response_markdown_fallback(self):
        pairs = _extract_key_value_pairs(_SYNTHETIC_MARKDOWN)
        self.assertEqual(pairs.get("最新价（元）"), "1207.68")
        self.assertEqual(pairs.get("市盈率(PE,TTM)"), "18.25")

    def test_string_response_invalid(self):
        # 完全无法解析的字符串 → 空 dict
        self.assertEqual(_extract_key_value_pairs("garbage no table no json"), {})

    def test_empty_input(self):
        self.assertEqual(_extract_key_value_pairs(""), {})
        self.assertEqual(_extract_key_value_pairs(None), {})

    def test_list_with_nested_dict(self):
        # response 是 list，每个元素是 dict
        pairs = _extract_key_value_pairs(
            [{"最新价": "100.0"}, {"无关": "data"}]
        )
        self.assertEqual(pairs.get("最新价"), "100.0")

    def test_real_mcp_shape_current_price(self):
        """真实 Choice MCP shape：data[0].items[0][0]=指标名, [1]=最新期值。"""
        pairs = _extract_key_value_pairs(
            json.loads(_SYNTHETIC_REAL_MCP_CURRENT_PRICE)
        )
        self.assertEqual(pairs, {"收盘价": "1237元"})

    def test_real_mcp_shape_net_profit(self):
        """真实 Choice MCP shape：归母净利 → 服务端实际指标名（股东的，不是所有者的）。"""
        pairs = _extract_key_value_pairs(
            json.loads(_SYNTHETIC_REAL_MCP_NET_PROFIT)
        )
        self.assertEqual(pairs, {"归属于母公司股东的净利润": "862.3亿元"})


class TestParseMarkdownFirstRow(unittest.TestCase):
    def test_basic(self):
        pairs = _parse_markdown_first_row(_SYNTHETIC_MARKDOWN)
        self.assertEqual(pairs["最新价（元）"], "1207.68")
        self.assertEqual(pairs["市盈率(PE,TTM)"], "18.25")

    def test_no_separator(self):
        self.assertEqual(_parse_markdown_first_row("only data no separator"), {})

    def test_empty(self):
        self.assertEqual(_parse_markdown_first_row(""), {})

    def test_ignores_comment_section(self):
        text = (
            "|指标|值|\n|---|---|\n|ROE|36.02|\n\n"
            "# 指标参数信息\n```json\n{\"x\": 1}\n```"
        )
        pairs = _parse_markdown_first_row(text)
        self.assertEqual(pairs.get("值"), "36.02")


class TestPickValue(unittest.TestCase):
    def test_keyword_match(self):
        pairs = {"总市值（元）": "1.5097万亿", "收盘价": "1207"}
        val, col = _pick_value(pairs, ["总市值（元）", "总市值"])
        self.assertEqual(val, 1.5097e12)
        self.assertIn("总市值", col)

    def test_chinese_unit_yi(self):
        val, _ = _pick_value({"总市值": "199亿"}, ["总市值"])
        self.assertEqual(val, 1.99e10)

    def test_chinese_unit_wan(self):
        val, _ = _pick_value({"流通市值": "5000万"}, ["流通市值"])
        self.assertEqual(val, 5e7)

    def test_no_match(self):
        val, col = _pick_value({"无关": "100"}, ["净利润"])
        self.assertIsNone(val)
        self.assertEqual(col, "")

    def test_invalid_value_skipped(self):
        val, col = _pick_value({"净利润": "--", "营业收入": "100亿"}, ["净利润", "营业收入"])
        # 第一个关键词 "--" 不可解析，应跳到第二个
        self.assertEqual(val, 1e10)
        self.assertIn("营业收入", col)


class TestParseMxMcpResponse(unittest.TestCase):
    def test_valid_json_response(self):
        reading = _parse_mx_mcp_response(
            _SYNTHETIC_NESTED_JSON,
            keywords=["最新价（元）", "最新价"],
            field="current_price",
            period=None,
        )
        self.assertIsNotNone(reading)
        self.assertEqual(reading.source, "mx_mcp")
        self.assertEqual(reading.value, 1207.68)

    def test_markdown_fallback(self):
        reading = _parse_mx_mcp_response(
            _SYNTHETIC_MARKDOWN,
            keywords=["最新价（元）", "最新价"],
            field="current_price",
            period=None,
        )
        self.assertIsNotNone(reading)
        self.assertEqual(reading.value, 1207.68)

    def test_no_match_returns_none(self):
        reading = _parse_mx_mcp_response(
            _SYNTHETIC_NESTED_JSON,
            keywords=["营业收入"],  # 当前 fixture 里没有这个字段
            field="revenue",
            period=None,
        )
        self.assertIsNone(reading)

    def test_empty_text_returns_none(self):
        self.assertIsNone(
            _parse_mx_mcp_response("", keywords=["最新价"], field="current_price", period=None)
        )

    def test_invalid_json_returns_none(self):
        # 严格 JSON 解析失败 → 不是合法 JSON → 退化为 Markdown 解析 → 失败 → None
        self.assertIsNone(
            _parse_mx_mcp_response(
                "totally broken { not json, no table",
                keywords=["最新价"],
                field="current_price",
                period=None,
            )
        )

    def test_pe_ratio_has_ttm_caliber(self):
        reading = _parse_mx_mcp_response(
            _SYNTHETIC_NESTED_JSON,
            keywords=["市盈率(PE,TTM)"],
            field="pe_ratio",
            period=None,
        )
        self.assertEqual(reading.caliber, "TTM")

    def test_revenue_has_period(self):
        reading = _parse_mx_mcp_response(
            json.dumps(
                {
                    "response": {
                        "data": {"营业收入（元）": "1000亿"},
                    },
                }
            ),
            keywords=["营业收入（元）", "营业收入"],
            field="revenue",
            period="2024年报",
        )
        self.assertIsNotNone(reading)
        self.assertEqual(reading.period, "2024年报")

    def test_real_mcp_current_price_uses_close_price_label(self):
        """真实 server 用「收盘价」label 而不是「最新价」；关键词列表应包含两者。"""
        reading = _parse_mx_mcp_response(
            _SYNTHETIC_REAL_MCP_CURRENT_PRICE,
            keywords=["收盘价（元）", "收盘价", "最新价（元）", "最新价"],
            field="current_price",
            period=None,
        )
        self.assertIsNotNone(reading)
        self.assertEqual(reading.value, 1237.0)
        self.assertEqual(reading.source, "mx_mcp")

    def test_real_mcp_net_profit_uses_shareholder_label(self):
        """真实 server 用「归属于母公司股东的净利润」（不是「所有者的」）。"""
        reading = _parse_mx_mcp_response(
            _SYNTHETIC_REAL_MCP_NET_PROFIT,
            keywords=[
                "归属于母公司股东的净利润（元）",
                "归属于母公司股东的净利润",
                "归属于母公司所有者的净利润（元）",
                "归属于母公司所有者的净利润",
                "归母净利润（元）",
                "归母净利润",
            ],
            field="net_profit",
            period="2024年报",
        )
        self.assertIsNotNone(reading)
        self.assertEqual(reading.value, 8.623e10)  # 862.3 亿元
        self.assertEqual(reading.period, "2024年报")

    def test_unknown_shape_logs_warning(self):
        """响应不是 MCP/JSON/Markdown 任何已知 shape → WARN 一次，让运维发现 shape 漂移。"""
        captured: List[str] = []

        class _CaptureHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(self.format(record))

        handler = _CaptureHandler(level=logging.WARNING)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logger_obj = logging.getLogger("data_provider.mx_mcp_adapter")
        logger_obj.addHandler(handler)
        logger_obj.setLevel(logging.WARNING)
        try:
            reading = _parse_mx_mcp_response(
                "totally opaque response with no extractable shape",
                keywords=["最新价"],
                field="current_price",
                period=None,
            )
            self.assertIsNone(reading)
            warnings = [m for m in captured if "parse" in m and "shape" in m]
            self.assertGreaterEqual(
                len(warnings), 1, f"expected a WARN about unknown shape, got: {captured}"
            )
        finally:
            logger_obj.removeHandler(handler)


# ------------------------------------------------------------------
# MxMcpSource 单测（注入 _FakeFetcher）
# ------------------------------------------------------------------


class TestMxMcpSource(unittest.TestCase):
    def test_source_name_is_mx_mcp(self):
        """确保 source 标识与 mx / ifind 不冲突。"""
        self.assertEqual(MxMcpSource.name, "mx_mcp")

    def test_no_fetcher_returns_none(self):
        src = MxMcpSource(fetcher=None)
        self.assertIsNone(src.read("600519", "current_price"))

    def test_unavailable_fetcher_returns_none(self):
        fake = _FakeFetcher(available=False)
        src = MxMcpSource(fetcher=fake)
        self.assertIsNone(src.read("600519", "current_price"))
        self.assertFalse(src.available)

    def test_unknown_field_returns_none(self):
        fake = _FakeFetcher(
            available=True,
            fetch_fn=lambda c, f, p: AnchorReading(source="mx_mcp", value=1.0),
        )
        src = MxMcpSource(fetcher=fake)
        # 未知字段，Source 不分发
        self.assertIsNone(src.read("600519", "nonexistent_field"))

    def test_fetcher_exception_isolated(self):
        def boom(code: str, field: str, period: Optional[str]) -> AnchorReading:
            raise RuntimeError("fetcher exploded")

        fake = _FakeFetcher(available=True, fetch_fn=boom)
        src = MxMcpSource(fetcher=fake)
        # fail-open：fetcher 抛异常 → 返回 None，不阻塞其他源
        self.assertIsNone(src.read("600519", "current_price"))

    def test_fetcher_returns_none_passthrough(self):
        fake = _FakeFetcher(
            available=True, fetch_fn=lambda c, f, p: None
        )
        src = MxMcpSource(fetcher=fake)
        self.assertIsNone(src.read("600519", "current_price"))

    def test_successful_read_passes_through(self):
        fake_reading = AnchorReading(source="mx_mcp", value=18.25, caliber="TTM")
        fake = _FakeFetcher(
            available=True, fetch_fn=lambda c, f, p: fake_reading
        )
        src = MxMcpSource(fetcher=fake)
        reading = src.read("600519", "pe_ratio")
        self.assertIsNotNone(reading)
        self.assertEqual(reading.value, 18.25)
        self.assertEqual(reading.caliber, "TTM")
        # source 重写为 self.name（防止 fetcher 误传 source）
        self.assertEqual(reading.source, "mx_mcp")


# ------------------------------------------------------------------
# MxMcpFetcher 单测（覆盖 available / 凭据脱敏）
# ------------------------------------------------------------------


class TestMxMcpFetcherAvailable(unittest.TestCase):
    """available / 凭据不在日志中出现。"""

    SECRET_KEY = "this_is_a_super_secret_key_DO_NOT_LEAK"

    def setUp(self) -> None:
        # 清空环境变量，保证测试隔离
        for k in ("MX_MCP_API_KEY", "MX_MCP_ENDPOINT", "MX_MCP_TIMEOUT_SECONDS"):
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        for k in ("MX_MCP_API_KEY", "MX_MCP_ENDPOINT", "MX_MCP_TIMEOUT_SECONDS"):
            os.environ.pop(k, None)

    def test_no_api_key_unavailable(self):
        fetcher = MxMcpFetcher(endpoint="https://example.com/mcp", api_key="")
        self.assertFalse(fetcher.available)

    def test_no_endpoint_unavailable(self):
        fetcher = MxMcpFetcher(endpoint="", api_key=self.SECRET_KEY)
        self.assertFalse(fetcher.available)

    def test_both_set_available(self):
        fetcher = MxMcpFetcher(
            endpoint="https://example.com/mcp", api_key=self.SECRET_KEY
        )
        self.assertTrue(fetcher.available)

    def test_fetch_when_unavailable_returns_none(self):
        fetcher = MxMcpFetcher(endpoint="", api_key="")
        self.assertIsNone(fetcher.fetch("600519", "current_price"))

    def test_logger_does_not_emit_api_key(self):
        """凭据安全：logger 不打印 API key 任何片段。"""
        fetcher = MxMcpFetcher(
            endpoint="https://example.com/mcp", api_key=self.SECRET_KEY
        )
        # 收集所有 logger 输出
        captured: List[str] = []

        class _CaptureHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(self.format(record))

        handler = _CaptureHandler(level=logging.DEBUG)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger_obj = logging.getLogger("data_provider.mx_mcp_adapter")
        logger_obj.addHandler(handler)
        logger_obj.setLevel(logging.DEBUG)
        try:
            # 故意构造异常路径触发 logger.debug
            fetcher.fetch("600519", "current_price")
            # _async_fetch 会因 mcp 未实际连接而抛异常，被 fetch() 吞掉并 debug log
            # 验证：所有 captured 日志中不出现 SECRET_KEY 任何片段
            leaked = [msg for msg in captured if self.SECRET_KEY in msg]
            self.assertEqual(
                leaked,
                [],
                f"API key leaked into log: {leaked}",
            )
        finally:
            logger_obj.removeHandler(handler)


if __name__ == "__main__":
    unittest.main()
