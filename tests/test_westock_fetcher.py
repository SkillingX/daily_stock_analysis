# -*- coding: utf-8 -*-
"""westock fetcher 测试：Markdown 表解析/代码映射离线单测 + 真实 CLI 冒烟（network 标记）。"""

from __future__ import annotations

import pytest

from data_provider.westock_fetcher import (
    _parse_md_tables,
    _to_number,
    get_chip,
    get_margin,
    get_quote,
    get_shareholder,
    to_market_code,
)

_QUOTE_SAMPLE = """\
| code | name | price | pe_ratio | pb_ratio | low_52week |
| --- | --- | --- | --- | --- | --- |
| sh603690 | 至纯科技 | 22.22 | -8.28 | 2.64 | 19.46 |
"""

_MULTI_SECTION_SAMPLE = """\
#### sh603690 至纯科技 (2026-06-30)

**十大股东**

| name | holdPct | holdShares |
| --- | --- | --- |
| 蒋渊 | 20.59 | 78863941.00 |

**股东户数统计**

| date | aSHNum |
| --- | --- |
| 2026-06-30 | 98087.00 |
| 2026-03-31 | 89760.00 |
"""


class TestMarketCode:
    def test_a_share_prefix(self):
        assert to_market_code("603690") == "sh603690"
        assert to_market_code("600519") == "sh600519"
        assert to_market_code("000001") == "sz000001"
        assert to_market_code("300623") == "sz300623"
        assert to_market_code("430047") == "bj430047"

    def test_already_prefixed(self):
        assert to_market_code("sh603690") == "sh603690"
        assert to_market_code("SZ000001") == "sz000001"

    def test_non_a_share_passthrough(self):
        assert to_market_code("hk00700") == "hk00700"
        assert to_market_code("AAPL") == "AAPL"
        assert to_market_code("") == ""


class TestParseMdTables:
    def test_single_table_default_section(self):
        sections = _parse_md_tables(_QUOTE_SAMPLE)
        rows = sections["_default"]
        assert len(rows) == 1
        row = rows[0]
        assert row["code"] == "sh603690"
        assert row["name"] == "至纯科技"
        assert row["price"] == 22.22
        assert row["pe_ratio"] == -8.28
        assert row["low_52week"] == 19.46

    def test_multi_section_tables(self):
        sections = _parse_md_tables(_MULTI_SECTION_SAMPLE)
        holders = sections["十大股东"]
        assert holders[0]["name"] == "蒋渊"
        assert holders[0]["holdPct"] == 20.59
        counts = sections["股东户数统计"]
        assert len(counts) == 2  # 同一张表的多数据行
        assert counts[0]["aSHNum"] == 98087.0
        assert counts[1]["aSHNum"] == 89760.0

    def test_no_semicolon_misalignment(self):
        """相邻两张表字段数不同，第二表数据不得用第一表头错位解析。"""
        text = (
            "| a | b |\n| --- | --- |\n| 1 | 2 |\n\n"
            "| c | d | e |\n| --- | --- | --- |\n| 3 | 4 | 5 |\n"
        )
        sections = _parse_md_tables(text)
        assert sections["_default"][0] == {"a": 1, "b": 2}
        assert sections["_default"][1] == {"c": 3, "d": 4, "e": 5}

    def test_to_number(self):
        assert _to_number("22.22") == 22.22
        assert _to_number("-8") == -8
        assert _to_number("至纯科技") == "至纯科技"
        assert _to_number("") is None
        assert _to_number("--") is None


@pytest.mark.network
class TestLiveCliSmoke:
    """真实 CLI 冒烟：本机 westock 可用时验证端到端数据通路（603690 为固定测试标的）。"""

    def test_quote(self):
        q = get_quote("603690")
        assert q is not None
        assert q["name"] == "至纯科技"
        assert q["price"] > 0
        assert q["low_52week"] > 0

    def test_chip(self):
        chip = get_chip("603690")
        assert chip is not None
        assert 0 <= chip["chipProfitRate"] <= 100
        assert chip["chipAvgCost"] > 0

    def test_margin(self):
        m = get_margin("603690")
        assert m is not None
        assert m["FinanceValue"] > 0

    def test_shareholder(self):
        sh = get_shareholder("603690")
        assert sh is not None
        assert sh["holder_counts"], "股东户数序列不应为空"
        latest = sh["holder_counts"][0]
        assert latest["totalSHNum"] > 0

    def test_cli_missing_degrades_gracefully(self, monkeypatch):
        monkeypatch.setattr("data_provider.westock_fetcher._cli_path", lambda: None)
        assert get_quote("603690") is None
        assert get_chip("603690") is None
