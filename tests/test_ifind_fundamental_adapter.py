from data_provider.cross_source_validator import SourceReadError
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
    IfindData,
    IfindFetcher,
    IfindSource,
    _extract_ifind_value,
    _parse_ifind_markdown_table,
    _parse_ifind_response,
    _parse_main_inflow_series,
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
        self.assertIsNone(r.period)
        self.assertEqual(r.requested_period, "2024年报")

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
        with self.assertRaises(SourceReadError):
            src.read("600519", "pe_ratio")

    def test_read_fetcher_returns_none(self):
        src = IfindSource(fetcher=_FakeFetcher(available=True, fetch_fn=lambda c, f, p: None))
        self.assertIsNone(src.read("600519", "pe_ratio"))

    def test_read_financial_carries_period(self):
        fake = _FakeFetcher(available=True, fetch_fn=lambda c, f, p: AnchorReading(
            source="ifind", value=1e10, period=None))
        src = IfindSource(fetcher=fake)
        r = src.read("600519", "revenue", period="2024年报")
        self.assertIsNone(r.period)
        self.assertEqual(r.requested_period, "2024年报")
        self.assertEqual(r.value, 1e10)

    def test_read_does_not_infer_ttm_from_field_request(self):
        # 缺少响应字段的口径证据，不能通过请求锚点强行补成 TTM。
        fake = _FakeFetcher(available=True, fetch_fn=lambda c, f, p: AnchorReading(
            source="ifind", value=30.0, caliber=None, period=None))
        src = IfindSource(fetcher=fake)
        r = src.read("600519", "pe_ratio")
        self.assertIsNone(r.caliber)


class TestIfindFetcher(unittest.TestCase):
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
        self.assertIsNone(r.period)
        self.assertEqual(r.requested_period, "2024年报")

    def test_read_revenue_yoy_carries_period(self):
        fake = _FakeFetcher(available=True, fetch_fn=lambda c, f, p: AnchorReading(
            source="ifind", value=18.5, period=None))
        src = IfindSource(fetcher=fake)
        r = src.read("688486", "revenue_yoy", period="2024年报")
        self.assertAlmostEqual(r.value, 18.5)
        self.assertIsNone(r.period)
        self.assertEqual(r.requested_period, "2024年报")

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
        self.assertIsNone(r.period)
        self.assertEqual(r.requested_period, "2024年报")


class TestParseMainInflowSeries(unittest.TestCase):
    """_parse_main_inflow_series：证券身份/金额单位/日期冲突严格校验（issue #71）。

    资金窗口合格性依赖序列边界本身可证明：返回证券须匹配请求证券，金额列须
    带「单位：元」证据（量/股列拒绝），同日金额冲突整批拒绝，周末空行不成行。
    """

    def _env(self, answer: str, params=None) -> IfindData:
        return IfindData(answer=answer, indicators_params=params if params is not None else {"主力净流入额": {"单位": " 元", "交易日期": "最新"}})

    _TABLE = (
        "|证券代码|证券简称|日期|主力净流入额（单位：元）|\n"
        "|---|---|---|---|\n"
        "|688486.SH|龙迅股份|20260625|245.4733万|\n"
        "|688486.SH|龙迅股份|20260624|-8481311.6|\n"
    )

    def test_parses_rows_with_units_scaled_once(self):
        series = _parse_main_inflow_series(self._env(self._TABLE), "688486")
        self.assertEqual([d for d, _v in series], ["2026-06-25", "2026-06-24"])
        self.assertAlmostEqual(dict(series)["2026-06-25"], 245.4733e4)  # 万只换算一次
        self.assertAlmostEqual(dict(series)["2026-06-24"], -8481311.6)

    def test_wrong_security_rejected_not_adopted(self):
        text = self._TABLE.replace("688486.SH", "600519.SH")
        with self.assertRaises(SourceReadError):
            _parse_main_inflow_series(self._env(text), "688486")

    def test_missing_unit_evidence_rejected(self):
        text = self._TABLE.replace("主力净流入额（单位：元）", "主力净流入额")
        with self.assertRaises(SourceReadError):
            _parse_main_inflow_series(self._env(text, params={}), "688486")

    def test_shares_column_rejected(self):
        text = self._TABLE.replace("主力净流入额（单位：元）", "主力净流入量（单位：股）")
        with self.assertRaises(SourceReadError):
            _parse_main_inflow_series(self._env(text), "688486")

    def test_foreign_currency_conflict_rejected(self):
        params = {"主力净流入额": {"单位": " 元", "币种": "USD"}}
        with self.assertRaises(SourceReadError):
            _parse_main_inflow_series(self._env(self._TABLE, params), "688486")

    def test_conflicting_duplicate_date_rejected(self):
        text = self._TABLE + "|688486.SH|龙迅股份|20260624|-1.0|\n"
        with self.assertRaises(SourceReadError):
            _parse_main_inflow_series(self._env(text), "688486")

    def test_weekend_blank_rows_produce_no_rows(self):
        text = self._TABLE + "|688486.SH|龙迅股份|20260621|\t|\n"
        series = _parse_main_inflow_series(self._env(text), "688486")
        self.assertEqual(len(series), 2)

    def test_avoids_quantity_shares_column(self):
        text = (
            "|证券代码|日期|主力净流入额（单位：元）|主力净流入量（单位：股）|\n|---|---|---|---|\n"
            "|688486.SH|20260625|245.4733万|37631|\n"
        )
        series = _parse_main_inflow_series(self._env(text), "688486")
        self.assertEqual(dict(series)["2026-06-25"], 245.4733e4)  # 额，非股数

    def test_empty_answer_rejected(self):
        with self.assertRaises(SourceReadError):
            _parse_main_inflow_series(self._env(""), "688486")


class TestIfindFetcherSeries(unittest.TestCase):
    """fetch_main_inflow_series：无 token → None（真实 MCP 路径 # pragma: no cover）。"""

    def test_unavailable_returns_none(self):
        f = IfindFetcher(endpoint="", token="")
        self.assertFalse(f.available)
        self.assertIsNone(f.fetch_main_inflow_series("688486"))


if __name__ == "__main__":
    unittest.main()


@pytest.mark.parametrize("returned,accepted", [("000001.SZ", True), ("000001", True), ("000001.SH", False), ("000001.BJ", False), ("300260.SZ", False)])
@pytest.mark.parametrize("value", ["0", "-123"])
def test_financial_identity_rejects_explicit_exchange_conflicts(returned, accepted, value):
    import json
    raw = {"code": 1, "data": {
        "answer": f"|证券代码|报告期|营业收入（单位：元）|\n|---|---|---|\n|{returned}|20260630|{value}|",
        "indicators_params": {"营业收入": {"单位": "元", "币种": "CNY", "报表类型": "合并报表(调整)", "报告期": "20260630"}},
    }}
    reading = _parse_ifind_response(json.dumps(raw), ["营业收入"], "revenue", None, code="000001", a_share_contract=True)
    assert (reading is not None) == accepted
    if reading is not None:
        assert reading.value == float(value)
        assert reading.period == "2026-06-30"


def test_disclosure_identity_does_not_cross_explicit_exchange_or_period():
    import json
    answer = "|证券代码|报告期|营业收入（单位：元）|\n|---|---|---|\n|000001.SZ|20260630|100|\n\n"
    answer += "|证券代码|报告期|定期报告实际披露日期|\n|---|---|---|\n|000001.SH|20260630|20260801|\n|000001.SZ|20251231|20260401|\n|000001|20260630|20260829|"
    raw = {"code": 1, "data": {"answer": answer, "indicators_params": {
        "营业收入": {"单位": "元", "币种": "CNY", "报表类型": "合并报表(调整)", "报告期": "20260630"},
        "定期报告实际披露日期": {"报告期": "20260630"},
    }}}
    reading = _parse_ifind_response(json.dumps(raw), ["营业收入"], "revenue", None, code="000001", a_share_contract=True)
    assert reading is not None
    assert reading.disclosed_at == "2026-08-29"


def test_ifind_query_preserves_research_stop_instead_of_supplier_error(monkeypatch):
    from src.core.research_run import ResearchRun, ResearchStopped
    run = ResearchRun(5)

    async def cancelled(*args, **kwargs):
        run.stop("cancelled")
        run.check()

    monkeypatch.setattr(IfindFetcher, "_async_query", cancelled)
    with run.bind(), pytest.raises(ResearchStopped):
        IfindFetcher("https://fixture.invalid/mcp", "test-only").query("get_stock_financials", "fixture")


def test_ifind_does_not_dispatch_after_cancellation(monkeypatch):
    """取消后仍进入共享边界记录零分派证据，但不得创建任何真实 MCP 客户端/会话。"""
    import mcp
    from mcp.client import streamable_http
    from mcp.shared import _httpx_utils
    from src.core.research_run import ResearchRun, ResearchStopped
    from src.services import mcp_client
    from tests.test_mcp_evidence import _payloads, _records

    def forbidden(*args, **kwargs):
        pytest.fail("Cancelled research must not create a real MCP client/session/transport")

    monkeypatch.setattr(_httpx_utils, "create_mcp_http_client", forbidden)
    monkeypatch.setattr(streamable_http, "streamable_http_client", forbidden, raising=False)
    monkeypatch.setattr(streamable_http, "streamablehttp_client", forbidden, raising=False)
    monkeypatch.setattr(mcp, "ClientSession", forbidden)

    run = ResearchRun(5)
    run.stop("cancelled")
    lines, handler = _records()
    try:
        with run.bind(), pytest.raises(ResearchStopped):
            IfindFetcher("https://fixture.invalid/mcp", "test-only").query("get_stock_financials", "fixture")
    finally:
        mcp_client.logger.removeHandler(handler)
    payloads = _payloads(lines)
    assert len(payloads) == 2
    assert {item["phase"] for item in payloads} == {"start", "end"}
    assert len({item["call_id"] for item in payloads}) == 1
    end = next(item for item in payloads if item["phase"] == "end")
    assert end["dispatched"] is False and end["status"] == "error"
    assert end["error_kind"] == "ResearchStopped"


@pytest.mark.parametrize("requested,expected", [(None, "2026-06-30"), ("2025-12-31", "2025-12-31")])
def test_financial_batch_requests_dated_periods_and_selects_real_latest(monkeypatch, requested, expected):
    """Public iFinD reply: the later empty row is not an eligible report period."""
    import json
    from datetime import date
    from pathlib import Path
    from time import monotonic
    from data_provider.ifind_fundamental_adapter import IfindData
    raw = json.loads((Path(__file__).parent / "fixtures/ifind_financial_periods.json").read_text())
    questions = []

    def query(self, tool, question, **kwargs):
        questions.append(question)
        return IfindData.model_validate(raw["data"])

    monkeypatch.setattr(IfindFetcher, "query", query)
    readings = IfindFetcher("https://fixture.invalid/mcp", "test-only").financial_snapshot("300260", requested, deadline=monotonic() + 5)
    assert {reading.period for reading in readings.values()} == {expected}
    assert readings["revenue"].value == (1545840000 if requested is None else 2997710000)
    assert readings["net_profit_yoy"].value < 0
    assert readings["roe"].adjustment is None  # Do not infer adjustment metadata from the request.
    if requested is None:
        today = date.today()
        assert date(today.year - 1, today.month, 1).isoformat() in questions[0]
        assert today.isoformat() in questions[0]
        assert "每个" in questions[0]
    else:
        assert requested in questions[0]
    assert len(questions) == 1


def test_requesting_dates_does_not_fabricate_missing_response_period(monkeypatch):
    from time import monotonic
    from data_provider.ifind_fundamental_adapter import IfindData
    data = IfindData(answer="|证券代码|营业收入（单位：元）|\n|---|---|\n|300260.SZ|0|", indicators_params={"营业收入": {"报告期": "最新一期(MRQ)", "单位": "元", "报表类型": "合并报表(调整)"}})
    monkeypatch.setattr(IfindFetcher, "query", lambda *a, **k: data)
    readings = IfindFetcher("https://fixture.invalid/mcp", "test-only").financial_snapshot("300260", None, deadline=monotonic() + 5)
    assert readings["revenue"].period is None
    assert readings["revenue"].value == 0
