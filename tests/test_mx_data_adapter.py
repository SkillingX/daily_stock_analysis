# -*- coding: utf-8 -*-
"""mx_data_adapter 单测 —— mock HTTP/client，100% 覆盖。

覆盖：_safe_float/_pick_value 解析、_post HTTP（成功/错误/401/异常/无requests）、
_extract_first_table_row（多种 MX 返回结构）、MXClient（无key/cache TTL/高级查询）、
MXSource.read（三类字段分发、fail-open、period 透传、未知字段）。
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.mx_data_adapter import (  # noqa: E402
    MXClient,
    MXSource,
    _extract_first_table_row,
    _pick_growth_value,
    _pick_value,
    _post,
    _safe_float,
)
from data_provider.cross_source_validator import AnchorReading  # noqa: E402


def _mx_response(rows):
    """构造 MX dataTable 响应：rows = [(label, value), ...]。"""
    table = {"headName": ["指标", "数值"]}
    name_map = {}
    for i, (label, value) in enumerate(rows, 1):
        key = f"row{i}"
        table[key] = [value]
        name_map[key] = label
    return {
        "status": 0,
        "data": {
            "data": {
                "searchDataResultDTO": {
                    "dataTableDTOList": [
                        {
                            "entityName": "贵州茅台(600519.SH)",
                            "table": table,
                            "nameMap": name_map,
                        }
                    ]
                }
            }
        },
    }


class _FakeResp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class _FakeMXClient:
    """MXClient 替身：按预设 bundle 返回，记录调用。"""

    def __init__(self, available=True, snapshot=None, financials=None, capital=None):
        self.available = available
        self._snapshot = snapshot or {}
        self._financials = financials or {}
        self._capital = capital or {}
        self.calls = []

    def fetch_snapshot(self, code):
        self.calls.append(("snapshot", code))
        return self._snapshot

    def query_financials(self, code, period, field=None):
        self.calls.append(("financials", code, period))
        return self._financials

    def query_capital(self, code):
        self.calls.append(("capital", code))
        return self._capital


class TestSafeFloat(unittest.TestCase):
    def test_int_float(self):
        self.assertEqual(_safe_float(3), 3.0)
        self.assertEqual(_safe_float(2.5), 2.5)

    def test_string_numeric(self):
        self.assertEqual(_safe_float("1,234.5"), 1234.5)
        self.assertEqual(_safe_float("3%"), 3.0)
        self.assertEqual(_safe_float("2.1e12"), 2.1e12)

    def test_chinese_amount_units(self):
        # Phase 0 实测 MX 返回「万亿/亿/万」后缀，需换算成绝对值
        self.assertAlmostEqual(_safe_float("1.516万亿"), 1.516e12)
        self.assertAlmostEqual(_safe_float("199.4亿"), 1.994e10)
        self.assertAlmostEqual(_safe_float("-8.699亿"), -8.699e8)
        self.assertAlmostEqual(_safe_float("12345万"), 1.2345e8)
        self.assertAlmostEqual(_safe_float("862.3亿"), 8.623e10)

    def test_invalid(self):
        self.assertIsNone(_safe_float(None))
        self.assertIsNone(_safe_float("-"))
        self.assertIsNone(_safe_float("--"))
        self.assertIsNone(_safe_float(""))
        self.assertIsNone(_safe_float("abc"))


class TestPickValue(unittest.TestCase):
    def test_match_by_keyword(self):
        bundle = {"最新价": "1685.2", "总市值": "2.1e12"}
        self.assertEqual(_pick_value(bundle, ["最新价", "现价"]), 1685.2)
        self.assertEqual(_pick_value(bundle, ["总市值"]), 2.1e12)

    def test_no_match(self):
        self.assertIsNone(_pick_value({"营收": "100"}, ["PE"]))
        self.assertIsNone(_pick_value({}, ["PE"]))
        self.assertIsNone(_pick_value(None, ["PE"]))

    def test_skip_unparseable(self):
        # 命中关键词但值不可解析 → 跳过继续找，全不可解析返回 None
        self.assertIsNone(_pick_value({"市盈率": "-"}, ["市盈率"]))

    def test_skip_growth_rate_columns(self):
        # MX 财务 query 同时返回「营业收入」与「营业收入(同比增长率)」，
        # 关键词「营业收入」会命中两者，应跳过增长率列取绝对值
        bundle = {
            "营业收入(同比增长率)": "15.66",
            "营业收入": "1709亿",
        }
        self.assertAlmostEqual(_pick_value(bundle, ["营业收入", "营收"]), 1.709e11)

    def test_safe_float_chinese_unit_strip(self):
        # 「199.0243亿」→ 1.990243e10（亿 strip，元无影响）
        self.assertAlmostEqual(_safe_float("199.0243亿"), 1.990243e10)
        # 纯数值
        self.assertEqual(_safe_float("18.33"), 18.33)
        # 「862.3亿」→ 8.623e10
        self.assertAlmostEqual(_safe_float("862.3亿"), 8.623e10)
        # 「1709亿元」→ 元+亿 复合单位
        self.assertAlmostEqual(_safe_float("1709亿元"), 1.709e11)
        # 「199.0亿元」
        self.assertAlmostEqual(_safe_float("199.0亿元"), 1.99e10)


class TestPickGrowthValue(unittest.TestCase):
    """_pick_growth_value：与 _pick_value 互补，专门取「同比增长率」列。"""

    def test_match_growth_column(self):
        # 关键词「营业收入」+ 增长率标记 → 命中「营业收入(同比增长率)」列（带括号）
        bundle = {"营业收入": "1709亿", "营业收入(同比增长率)": "15.66"}
        self.assertAlmostEqual(_pick_growth_value(bundle, ["营业收入", "营收"]), 15.66)

    def test_no_growth_marker_returns_none(self):
        # 关键词命中绝对值列但无增长率标记 → None（_pick_value 才取这种列）
        self.assertIsNone(_pick_growth_value({"营业收入": "1709亿"}, ["营业收入"]))

    def test_skip_unparseable(self):
        # 命中增长率列但值不可解析 → 跳过，全不可解析返回 None
        self.assertIsNone(
            _pick_growth_value({"营业收入(同比增长率)": "-"}, ["营业收入"])
        )

    def test_empty_bundle(self):
        self.assertIsNone(_pick_growth_value({}, ["营业收入"]))
        self.assertIsNone(_pick_growth_value(None, ["营业收入"]))


class TestPost(unittest.TestCase):
    def test_success(self):
        with patch("requests.post", return_value=_FakeResp(200, {"ok": 1})):
            self.assertEqual(_post("u", {}, "k", attempts=1), {"ok": 1})

    def test_http_error_returns_error(self):
        with patch("requests.post", return_value=_FakeResp(500, text="boom")):
            r = _post("u", {}, "k", attempts=1)
        self.assertIn("error", r)  # noqa: S102

    def test_http_401_no_retry(self):
        with patch("requests.post", return_value=_FakeResp(401, text="bad key")) as m:
            _post("u", {}, "k", attempts=3)
        self.assertEqual(m.call_count, 1)  # 401/403 不重试

    def test_exception_returns_error(self):
        with patch("requests.post", side_effect=TimeoutError("slow")):
            r = _post("u", {}, "k", attempts=1)
        self.assertIn("error", r)  # noqa: S102

    def test_requests_missing(self):
        with patch.dict(sys.modules, {"requests": None}):
            r = _post("u", {}, "k", attempts=1)
        self.assertEqual(r, {"error": "requests library missing"})


class TestExtractFirstTableRow(unittest.TestCase):
    def test_normal(self):
        out = _extract_first_table_row(
            _mx_response([("最新价", "1685"), ("总市值", "2e12")])
        )
        self.assertEqual(out["最新价"], "1685")
        self.assertEqual(out["总市值"], "2e12")
        self.assertIn("_mx_entity", out)

    def test_error_dict_returns_empty(self):
        self.assertEqual(_extract_first_table_row({"error": "x"}), {})

    def test_bad_status_returns_empty(self):
        self.assertEqual(_extract_first_table_row({"status": 1, "data": {}}), {})

    def test_no_data_table_returns_empty(self):
        self.assertEqual(
            _extract_first_table_row({"status": 0, "data": {"data": {}}}), {}
        )

    def test_table_not_dict_returns_empty(self):
        r = {
            "status": 0,
            "data": {
                "data": {
                    "searchDataResultDTO": {"dataTableDTOList": [{"table": "oops"}]}
                }
            },
        }
        self.assertEqual(_extract_first_table_row(r), {})

    def test_name_map_list_fallback(self):
        # nameMap 退化为 list 时按 index 转 dict；row 类 key 匹配不上则回退原 key
        r = {
            "status": 0,
            "data": {
                "data": {
                    "searchDataResultDTO": {
                        "dataTableDTOList": [
                            {
                                "table": {"headName": ["a"], "row1": ["99"]},
                                "nameMap": ["PE"],
                            }
                        ]
                    }
                }
            },
        }
        out = _extract_first_table_row(r)
        self.assertEqual(out["_mx_entity"], "")
        self.assertEqual(out["row1"], "99")

    def test_non_list_value_branch(self):
        # values 非 list 时直接赋值（防御性 else 分支）
        r = {
            "status": 0,
            "data": {
                "data": {
                    "searchDataResultDTO": {
                        "dataTableDTOList": [
                            {
                                "table": {"headName": ["a"], "row1": "99"},
                                "nameMap": {"row1": "PE"},
                            }
                        ]
                    }
                }
            },
        }
        out = _extract_first_table_row(r)
        self.assertEqual(out["_mx_entity"], "")
        self.assertEqual(out["PE"], "99")
        self.assertIn("_mx_period", out)  # always emitted (may be None)

    def test_non_dict_returns_empty(self):
        self.assertEqual(_extract_first_table_row("not a dict"), {})
        self.assertEqual(_extract_first_table_row(None), {})


class TestMXClient(unittest.TestCase):
    def test_no_key_is_unavailable(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MX_APIKEY", None)
            c = MXClient()
        self.assertFalse(c.available)
        self.assertEqual(c.query("x"), {"error": "MX_APIKEY not set"})

    def test_query_caches(self):
        c = MXClient(api_key="k", ttl=60)
        with patch("data_provider.mx_data_adapter._post", return_value={"ok": 1}) as m:
            self.assertEqual(c.query("q1"), {"ok": 1})
            self.assertEqual(c.query("q1"), {"ok": 1})  # 命中缓存，不再 HTTP
        self.assertEqual(m.call_count, 1)

    def test_fetch_snapshot_uses_query(self):
        c = MXClient(api_key="k")
        with patch.object(c, "query", return_value=_mx_response([("最新价", "1685")])):
            out = c.fetch_snapshot("600519")
        self.assertEqual(out["最新价"], "1685")

    def test_query_financials_with_period(self):
        c = MXClient(api_key="k")
        captured = {}

        def fake_query(q):
            captured["q"] = q
            return _mx_response([("营业收入", "1e10")])

        with patch.object(c, "query", side_effect=fake_query):
            c.query_financials("600519", "2024年报")
        self.assertIn("2024年报", captured["q"])

    def test_query_financials_includes_growth_indicators(self):
        # 查询串须含「毛利率」「营业收入同比增长率」，让 MX 返回这两列
        c = MXClient(api_key="k")
        captured = {}

        def fake_query(q):
            captured["q"] = q
            return _mx_response([("毛利率", "50")])

        with patch.object(c, "query", side_effect=fake_query):
            c.query_financials("688486", None)
        self.assertIn("毛利率", captured["q"])
        self.assertIn("营业收入同比增长率", captured["q"])

    def test_query_capital(self):
        c = MXClient(api_key="k")
        with patch.object(
            c, "query", return_value=_mx_response([("主力净流入额", "2e8")])
        ):
            out = c.query_capital("600519")
        self.assertEqual(out["主力净流入额"], "2e8")


class TestMXSource(unittest.TestCase):
    def test_unavailable_returns_none(self):
        src = MXSource(client=_FakeMXClient(available=False))
        self.assertIsNone(src.read("600519", "pe_ratio"))

    def test_read_snapshot_field(self):
        src = MXSource(
            client=_FakeMXClient(
                available=True, snapshot={"市盈率": "30.5", "最新价": "1685"}
            )
        )
        r = src.read("600519", "pe_ratio")
        self.assertIsInstance(r, AnchorReading)
        self.assertEqual(r.value, 30.5)
        self.assertIsNone(r.period)
        self.assertEqual(r.source, "mx")

    def test_read_financial_field_carries_period(self):
        src = MXSource(
            client=_FakeMXClient(available=True, financials={"营业收入": "1e10", "_mx_period": "2024年报"})
        )
        r = src.read("600519", "revenue", period="2024年报")
        self.assertEqual(r.value, 1e10)
        self.assertEqual(r.period, "2024-12-31")

    def test_read_capital_field(self):
        src = MXSource(
            client=_FakeMXClient(available=True, capital={"主力净流入额": "2e8"})
        )
        r = src.read("600519", "main_inflow")
        self.assertEqual(r.value, 2e8)

    def test_read_missing_value_returns_none(self):
        src = MXSource(client=_FakeMXClient(available=True, snapshot={"营收": "1"}))
        self.assertIsNone(src.read("600519", "pe_ratio"))  # snapshot 里没有 PE

    def test_read_unknown_field_returns_none(self):
        src = MXSource(client=_FakeMXClient(available=True))
        self.assertIsNone(src.read("600519", "no_such_field"))

    def test_read_exception_isolated(self):
        client = _FakeMXClient(available=True)
        client.fetch_snapshot = lambda code: (_ for _ in ()).throw(RuntimeError("boom"))
        src = MXSource(client=client)
        self.assertIsNone(src.read("600519", "pe_ratio"))  # fail-open，不抛

    def test_default_client_built_when_none(self):
        # 不传 client 时自动构造 MXClient（验证不崩）
        with patch.dict(os.environ, {"MX_APIKEY": "k"}):
            src = MXSource()
        self.assertTrue(src.available)


class _PeriodFakeMXClient:
    """MXClient 替身：按 period 返回不同 financials bundle（测 period 回退用）。"""

    def __init__(self, available=True, financials_by_period=None):
        self.available = available
        self._by_period = financials_by_period or {}  # {period-or-None: bundle}
        self.calls = []

    def fetch_snapshot(self, code):
        return {}

    def query_financials(self, code, period, field=None):
        self.calls.append((code, period))
        return self._by_period.get(period, {})

    def query_capital(self, code):
        return {}


class TestMXSourceGrowthFields(unittest.TestCase):
    """MXSource 对 gross_margin / revenue_yoy 的读取（financials bundle 含对应列）。"""

    def test_read_gross_margin(self):
        src = MXSource(
            client=_FakeMXClient(
                available=True, financials={"毛利率": "52.3", "营业收入": "4.66亿"}
            )
        )
        r = src.read("688486", "gross_margin", period="2024年报")
        self.assertIsInstance(r, AnchorReading)
        self.assertAlmostEqual(r.value, 52.3)
        self.assertEqual(r.source, "mx")

    def test_read_revenue_yoy_from_growth_column(self):
        # financials 同时含绝对值与同比增长率列；revenue_yoy 取增长率列
        src = MXSource(
            client=_FakeMXClient(
                available=True,
                financials={"营业收入": "4.66亿", "营业收入(同比增长率)": "18.5"},
            )
        )
        r = src.read("688486", "revenue_yoy", period="2024年报")
        self.assertIsInstance(r, AnchorReading)
        self.assertAlmostEqual(r.value, 18.5)

    def test_read_revenue_yoy_missing_returns_none(self):
        # bundle 无同比增长率列 → None（不会误取绝对值）
        src = MXSource(
            client=_FakeMXClient(available=True, financials={"营业收入": "4.66亿"})
        )
        self.assertIsNone(src.read("688486", "revenue_yoy", period="2024年报"))

    def test_read_net_profit_yoy_from_growth_column(self):
        # financials 含净利润同比增长率列；net_profit_yoy 取增长率列
        src = MXSource(
            client=_FakeMXClient(
                available=True,
                financials={
                    "归属于母公司股东的净利润": "8.6亿",
                    "归属母公司股东的净利润同比增长率": "15.38",
                },
            )
        )
        r = src.read("600519", "net_profit_yoy", period="2024年报")
        self.assertIsInstance(r, AnchorReading)
        self.assertAlmostEqual(r.value, 15.38)
        self.assertEqual(r.source, "mx")
        self.assertIsNone(r.period)
        self.assertEqual(r.requested_period, "2024年报")

    def test_read_net_profit_yoy_missing_returns_none(self):
        # bundle 无净利润同比增长率列 → None
        src = MXSource(
            client=_FakeMXClient(
                available=True, financials={"归属于母公司股东的净利润": "8.6亿"}
            )
        )
        self.assertIsNone(src.read("600519", "net_profit_yoy", period="2024年报"))


class TestMXSourcePeriodFallback(unittest.TestCase):
    """_fetch_field period 回退：带期查不到 → 回退最新(period=None)。"""

    def test_period_hit_returns_period(self):
        # 指期 bundle 含该字段 → 返回 (val, period)
        client = _PeriodFakeMXClient(
            available=True,
            financials_by_period={
                "2024年报": {"营业收入": "1e10", "_mx_period": "2024年报"},
                None: {"营业收入": "9e9"},
            },
        )
        src = MXSource(client=client)
        r = src.read("600519", "revenue", period="2024年报")
        self.assertAlmostEqual(r.value, 1e10)
        self.assertEqual(r.period, "2024-12-31")

    def test_period_miss_falls_back_to_latest(self):
        # 指期 bundle 不含该字段 → 回退 None(latest)，period=None
        client = _PeriodFakeMXClient(
            available=True,
            financials_by_period={"2025年报": {}, None: {"营业收入": "9e9"}},
        )
        src = MXSource(client=client)
        r = src.read("600519", "revenue", period="2025年报")
        self.assertAlmostEqual(r.value, 9e9)
        self.assertIsNone(r.period)  # 回退后 period=None，validator 报告期检查会跳过
        # 两次查询：先指期，后 latest
        self.assertEqual([c[1] for c in client.calls], ["2025年报", None])

    def test_no_period_queries_latest_once(self):
        # period=None → 直接查 latest，只一次查询
        client = _PeriodFakeMXClient(
            available=True, financials_by_period={None: {"营业收入": "9e9"}}
        )
        src = MXSource(client=client)
        r = src.read("600519", "revenue", period=None)
        self.assertAlmostEqual(r.value, 9e9)
        self.assertIsNone(r.period)
        self.assertEqual(len(client.calls), 1)


if __name__ == "__main__":
    unittest.main()
