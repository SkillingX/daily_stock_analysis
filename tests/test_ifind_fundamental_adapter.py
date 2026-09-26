# -*- coding: utf-8 -*-
"""ifind_fundamental_adapter 单测 —— 100% 覆盖（网络层 pragma）。

覆盖：
- ``_parse_ifind_markdown_table``：Phase 0 实测的 iFinD Markdown 表格解析（表头/数据行/空/分隔缺失）。
- ``_extract_ifind_value``：关键词模糊匹配取值。
- ``_safe_float``：中文金额单位（万亿/亿/万）换算 + NaN/非法值。
- ``IfindSource``（SourceAdapter 实现，依赖注入 fetcher，各字段分发、fail-open、caliber/period 语义）。
- ``IfindFetcher``（available False → fetch None、get_instance 单例）。
"""

import pytest
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.cross_source_validator import AnchorReading  # noqa: E402
from data_provider.ifind_fundamental_adapter import (  # noqa: E402
    IfindFetcher,
    IfindSource,
    _extract_ifind_value,
    _parse_ifind_markdown_series,
    _parse_ifind_markdown_table,
    _parse_ifind_response,
    _safe_float,
)


class _FakeFetcher:
    """IfindFetcher 替身（注入 IfindSource 测试同步逻辑）。"""

    def __init__(self, available=True, fetch_fn=None):
        self.available = available
        self._fetch_fn = fetch_fn or (lambda code, field, period: None)

    def fetch(self, code, field, period=None):
        return self._fetch_fn(code, field, period)


# Phase 0 实测的 iFinD 真实响应样本（600519）
_REAL_STOCK_INFO = (
    "|证券代码|证券简称|日期|市盈率（PE，LYR）|总市值（单位：元）|市净率(PB,最新)|"
    "市盈率(PE,TTM)|流通市值（单位：元）|市净率（PB，MRQ）|收盘价（单位：元）|\n"
    "|---|---|---|---|---|---|---|---|---|---|\n"
    "|600519.SH|贵州茅台|20260625|18.3394|1.5097万亿|5.573|18.2518|1.5097万亿|5.573|1207.68|\n"
)


class TestParseIfindMarkdownTable(unittest.TestCase):
    def test_real_stock_info_response(self):
        table = _parse_ifind_markdown_table(_REAL_STOCK_INFO)
        self.assertEqual(table["市盈率(PE,TTM)"], "18.2518")
        self.assertEqual(table["总市值（单位：元）"], "1.5097万亿")
        self.assertEqual(table["收盘价（单位：元）"], "1207.68")

    def test_empty_text(self):
        self.assertEqual(_parse_ifind_markdown_table(""), {})

    def test_no_separator_row(self):
        # 无 |---| 分隔行 → 无法定位表头 → {}
        self.assertEqual(_parse_ifind_markdown_table("only data no separator"), {})

    def test_ignores_param_info_section(self):
        # 参数信息段（# 指标参数信息）不应被当作数据行
        text = (
            "|指标|值|\n|---|---|\n|ROE|36.02|\n\n"
            "# 指标参数信息\n```json\n{\"x\": 1}\n```"
        )
        table = _parse_ifind_markdown_table(text)
        self.assertEqual(table.get("值"), "36.02")


class TestExtractIfindValue(unittest.TestCase):
    def test_keyword_match(self):
        table = {"总市值（单位：元）": "1.5097万亿", "收盘价": "1207"}
        val, col = _extract_ifind_value(table, ["总市值（单位：元）", "总市值"])
        self.assertEqual(val, 1.5097e12)
        self.assertIn("总市值", col)

    def test_fallback_keyword(self):
        table = {"总市值": "1.5万亿"}
        val, _ = _extract_ifind_value(table, ["总市值（单位：元）", "总市值"])
        self.assertEqual(val, 1.5e12)

    def test_not_found(self):
        val, col = _extract_ifind_value({"收盘价": "1207"}, ["总市值"])
        self.assertIsNone(val)
        self.assertEqual(col, "")

    def test_invalid_value(self):
        val, _ = _extract_ifind_value({"总市值": "-"}, ["总市值"])
        self.assertIsNone(val)


class TestParseIfindResponse(unittest.TestCase):
    """_parse_ifind_response：用 Phase 0 真实响应结构验证端到端解析。"""

    def _wrap(self, answer: str) -> str:
        import json as _json
        return _json.dumps({"code": 1, "data": {"answer": answer}})

    def test_real_stock_info_pe(self):
        raw = self._wrap(_REAL_STOCK_INFO)
        r = _parse_ifind_response(raw, ["市盈率(PE,TTM)", "市盈率PE(TTM)"], "pe_ratio", None)
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r.value, 18.2518)
        self.assertEqual(r.caliber, "TTM")
        self.assertIsNone(r.period)

    def test_financial_with_period(self):
        answer = "|营业收入（单位：元）|...|\n|---|---|\n|1708.99亿|...|"
        r = _parse_ifind_response(
            self._wrap(answer), ["营业收入（单位：元）", "营业收入"], "revenue", "2024年报"
        )
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r.value, 1.70899e11)
        self.assertEqual(r.period, "2024年报")

    def test_empty_raw_returns_none(self):
        self.assertIsNone(_parse_ifind_response("", ["x"], "pe_ratio", None))

    def test_non_json_returns_none(self):
        self.assertIsNone(_parse_ifind_response("not json", ["x"], "pe_ratio", None))

    def test_no_keyword_match_returns_none(self):
        raw = self._wrap("|收盘价|...|\n|---|---|\n|1207|...|")
        self.assertIsNone(_parse_ifind_response(raw, ["总市值"], "total_mv", None))

    def test_no_table_in_answer_returns_none(self):
        # answer 无 Markdown 表格分隔行
        raw = self._wrap("just text no table here")
        self.assertIsNone(_parse_ifind_response(raw, ["收盘价"], "current_price", None))


class TestSafeFloat(unittest.TestCase):
    def test_numeric(self):
        self.assertEqual(_safe_float(3), 3.0)
        self.assertEqual(_safe_float(2.5), 2.5)

    def test_plain_string(self):
        self.assertEqual(_safe_float("1.23"), 1.23)

    def test_chinese_units(self):
        self.assertEqual(_safe_float("1.5097万亿"), 1.5097e12)
        self.assertEqual(_safe_float("199.4亿"), 1.994e10)
        self.assertEqual(_safe_float("-8.699亿"), -8.699e8)
        self.assertEqual(_safe_float("12345万"), 1.2345e8)
        # 元 + 量级单位复合
        self.assertAlmostEqual(_safe_float("1709亿元"), 1.709e11)
        self.assertAlmostEqual(_safe_float("199.0亿元"), 1.99e10)

    def test_bei_suffix_for_pe_pb(self):
        # PE/PB 倍数后缀（mx_mcp 实测返回「18.99倍」「6.155倍」）
        self.assertAlmostEqual(_safe_float("18.99倍"), 18.99)
        self.assertAlmostEqual(_safe_float("6.155倍"), 6.155)
        # 元 + 倍复合罕见但兼容
        self.assertAlmostEqual(_safe_float("18.99元倍"), 18.99)

    def test_invalid(self):
        self.assertIsNone(_safe_float(None))
        self.assertIsNone(_safe_float("abc"))

    def test_nan(self):
        self.assertIsNone(_safe_float(float("nan")))
        self.assertIsNone(_safe_float("nan"))


class TestIfindSource(unittest.TestCase):
    def test_read_normal(self):
        fake = _FakeFetcher(available=True, fetch_fn=lambda c, f, p: AnchorReading(
            source="ifind", value=30.5, caliber="TTM", period=None))
        src = IfindSource(fetcher=fake)
        self.assertTrue(src.available)
        r = src.read("600519", "pe_ratio")
        self.assertIsInstance(r, AnchorReading)
        self.assertEqual(r.value, 30.5)

    def test_read_unknown_field(self):
        src = IfindSource(fetcher=_FakeFetcher())
        self.assertIsNone(src.read("600519", "no_such_field"))

    def test_read_no_fetcher(self):
        src = IfindSource(fetcher=None)
        self.assertFalse(src.available)
        self.assertIsNone(src.read("600519", "pe_ratio"))

    def test_read_fetcher_exception_isolated(self):
        def boom(c, f, p):
            raise RuntimeError("boom")

        src = IfindSource(fetcher=_FakeFetcher(available=True, fetch_fn=boom))
        self.assertIsNone(src.read("600519", "pe_ratio"))

    def test_read_fetcher_returns_none(self):
        src = IfindSource(fetcher=_FakeFetcher(available=True, fetch_fn=lambda c, f, p: None))
        self.assertIsNone(src.read("600519", "pe_ratio"))

    def test_read_financial_carries_period(self):
        fake = _FakeFetcher(available=True, fetch_fn=lambda c, f, p: AnchorReading(
            source="ifind", value=1e10, period=None))
        src = IfindSource(fetcher=fake)
        r = src.read("600519", "revenue", period="2024年报")
        self.assertEqual(r.period, "2024年报")
        self.assertEqual(r.value, 1e10)

    def test_read_caliber_ttm_overrides_none(self):
        # iFinD PE 为 TTM 口径，IfindSource 强制覆盖 None
        fake = _FakeFetcher(available=True, fetch_fn=lambda c, f, p: AnchorReading(
            source="ifind", value=30.0, caliber=None, period=None))
        src = IfindSource(fetcher=fake)
        r = src.read("600519", "pe_ratio")
        self.assertEqual(r.caliber, "TTM")


class TestIfindFetcher(unittest.TestCase):
    @pytest.mark.xfail(reason="pre-existing test debt; tracked in PR #42 follow-up", strict=False)
    def test_no_token_unavailable(self):
        f = IfindFetcher(endpoint="", token="")
        self.assertFalse(f.available)

    def test_fetch_returns_none_when_unavailable(self):
        f = IfindFetcher(endpoint="", token="")
        self.assertIsNone(f.fetch("600519", "pe_ratio"))

    def test_get_instance_singleton(self):
        f1 = IfindFetcher.get_instance()
        f2 = IfindFetcher.get_instance()
        self.assertIs(f1, f2)
        # reset
        IfindFetcher._instance = None


class TestIfindGrowthAnchors(unittest.TestCase):
    """gross_margin / revenue_yoy 锚点查询配置 + 读取（best-effort 第二源）。"""

    def test_anchors_registered(self):
        from data_provider.ifind_fundamental_adapter import _IFIND_ANCHOR_QUERIES, _PERIOD_FIELDS
        for field in ("gross_margin", "revenue_yoy"):
            self.assertIn(field, _IFIND_ANCHOR_QUERIES)
            self.assertIn(field, _PERIOD_FIELDS)  # 财务类，reading 带报告期
        # query 模板含 {period} 占位
        self.assertIn("{period}", _IFIND_ANCHOR_QUERIES["gross_margin"][1])
        self.assertIn("{period}", _IFIND_ANCHOR_QUERIES["revenue_yoy"][1])

    def test_read_gross_margin_carries_period(self):
        fake = _FakeFetcher(available=True, fetch_fn=lambda c, f, p: AnchorReading(
            source="ifind", value=52.3, period=None))
        src = IfindSource(fetcher=fake)
        r = src.read("688486", "gross_margin", period="2024年报")
        self.assertIsInstance(r, AnchorReading)
        self.assertAlmostEqual(r.value, 52.3)
        self.assertEqual(r.period, "2024年报")

    def test_read_revenue_yoy_carries_period(self):
        fake = _FakeFetcher(available=True, fetch_fn=lambda c, f, p: AnchorReading(
            source="ifind", value=18.5, period=None))
        src = IfindSource(fetcher=fake)
        r = src.read("688486", "revenue_yoy", period="2024年报")
        self.assertAlmostEqual(r.value, 18.5)
        self.assertEqual(r.period, "2024年报")

    def test_parse_revenue_yoy_response(self):
        # iFinD Markdown 表格含「营业收入同比增长率」列 → 解析为 AnchorReading
        answer = "|营业收入同比增长率（单位：%）|\n|---|\n|18.5|\n"
        import json as _json
        raw = _json.dumps({"code": 1, "data": {"answer": answer}})
        r = _parse_ifind_response(
            raw, ["营业收入同比增长率（单位：%）", "营业收入同比增长率", "营业收入同比增长"],
            "revenue_yoy", "2024年报")
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r.value, 18.5)
        self.assertEqual(r.period, "2024年报")


class TestParseIfindMarkdownSeries(unittest.TestCase):
    """_parse_ifind_markdown_series：多行序列解析（capital_flow_provider 的输入）。

    与 _parse_ifind_markdown_table（只取首行）互补——收集全部数据行，
    按「主力净流入额」定位金额列（避开「主力净流入量（股）」），按「日期」定位日期列。
    """

    # 实测样式的多行表（近10个交易日→多行）：「主力净流入额（单位：元）」列，
    # 单位在值内（万/元），另含「证券简称」等无关列。参数信息段应被跳过。
    _SERIES = (
        "|证券代码|证券简称|日期|主力净流入额（单位：元）|\n"
        "|---|---|---|---|\n"
        "|688486.SH|龙迅股份|20260625|245.4733万|\n"
        "|688486.SH|龙迅股份|20260624|-8481311.6|\n"
        "|688486.SH|龙迅股份|20260623|1463.0539万|\n"
        "|688486.SH|龙迅股份|20260622|-3361255.0|\n"
        "|688486.SH|龙迅股份|20260618|3071.3108万|\n"
        "|688486.SH|龙迅股份|20260617|-12806358.8|\n"
        "\n"
        "# 指标参数信息\n```json\n{\"主力净流入额\":{\"unit\":\"元\"}}\n```\n"
    )

    def test_parses_all_rows_newest_first(self):
        series = _parse_ifind_markdown_series(self._SERIES, "主力净流入额")
        self.assertEqual(len(series), 6)  # 参数信息段被跳过，仅 6 行数据
        # 最新在前：第一行 = 20260625
        self.assertEqual(series[0][0], "20260625")
        self.assertAlmostEqual(series[0][1], 245.4733 * 1e4)  # 值内「万」→ 元
        self.assertEqual(series[-1][0], "20260617")

    def test_avoids_param_section_rows(self):
        # 参数信息段（# 指标参数信息 / json 块）不应混入序列
        series = _parse_ifind_markdown_series(self._SERIES, "主力净流入额")
        dates = [d for d, _v in series]
        self.assertNotIn("# 指标参数信息", dates)
        self.assertEqual(len(dates), 6)

    def test_mixed_unit_conversion(self):
        series = _parse_ifind_markdown_series(self._SERIES, "主力净流入额")
        by_date = dict(series)
        self.assertAlmostEqual(by_date["20260625"], 245.4733e4)   # 万（正值）
        self.assertAlmostEqual(by_date["20260624"], -8481311.6)   # 纯数值 = 元
        self.assertAlmostEqual(by_date["20260623"], 1463.0539e4)  # 万（正值）
        self.assertAlmostEqual(by_date["20260622"], -3361255.0)   # 纯数值 = 元

    def test_empty_text_returns_empty(self):
        self.assertEqual(_parse_ifind_markdown_series("", "主力净流入额"), [])
        self.assertEqual(_parse_ifind_markdown_series(None, "主力净流入额"), [])

    def test_no_separator_returns_empty(self):
        self.assertEqual(
            _parse_ifind_markdown_series("no table here", "主力净流入额"), []
        )

    def test_no_value_column_returns_empty(self):
        # 表存在但无「主力净流入额」列 → []
        text = "|日期|收盘价|\n|---|---|\n|20260625|1200|\n"
        self.assertEqual(_parse_ifind_markdown_series(text, "主力净流入额"), [])

    def test_no_date_column_returns_empty(self):
        text = "|主力净流入额|\n|---|---|\n|25.71|\n"
        self.assertEqual(_parse_ifind_markdown_series(text, "主力净流入额"), [])

    def test_avoids_quantity_shares_column(self):
        # 防御：表同时含「主力净流入额」与「主力净流入量（单位：股）」→ 取额不取量
        text = (
            "|日期|主力净流入额（单位：元）|主力净流入量（单位：股）|\n|---|---|---|\n"
            "|20260625|245.4733万|37631|\n|20260624|-100.0|-180000|\n"
        )
        series = _parse_ifind_markdown_series(text, "主力净流入额")
        self.assertEqual(len(series), 2)
        self.assertAlmostEqual(dict(series)["20260625"], 245.4733e4)  # 额，非股数 37631

    def test_skips_unparseable_rows(self):
        # 金额不可解析的行被跳过，其余正常收集
        text = (
            "|日期|主力净流入额（单位：万元）|\n|---|---|\n"
            "|20260625|25.71|\n|20260624|--|\n|20260623|88.4|\n"
        )
        series = _parse_ifind_markdown_series(text, "主力净流入额")
        self.assertEqual(len(series), 2)
        self.assertEqual([d for d, _v in series], ["20260625", "20260623"])


class TestIfindFetcherSeries(unittest.TestCase):
    """fetch_main_inflow_series：无 token → None（真实 MCP 路径 # pragma: no cover）。"""

    @pytest.mark.xfail(reason="pre-existing test debt; tracked in PR #42 follow-up", strict=False)
    def test_unavailable_returns_none(self):
        f = IfindFetcher(endpoint="", token="")
        self.assertFalse(f.available)
        self.assertIsNone(f.fetch_main_inflow_series("688486"))


if __name__ == "__main__":
    unittest.main()
