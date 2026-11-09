# -*- coding: utf-8 -*-
"""issue #86：iFinD 日线适配器资格边界（离线，真实脱敏 fixture）。

覆盖：证券身份（规范化前保留交易所显式后缀）、逐价格字段现金分红复权
元数据、量/额单位、严格数值解析（中文倍率/科学计数法/公式样式拒绝）、
冲突重复、截断导出 URL 门槛与整窗资格（IPO 低价泛复权、停牌量额缺失均
整窗拒绝后按既有候选兜底）。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from data_provider import ifind_daily_adapter as A
from data_provider.base import DataFetchError
from tests.ifind_daily_testutil import (
    NOW,
    daily_csv_text,
    daily_fixture,
    enabled_config,
    strict_calendar,
)


# ---------------------------------------------------------------------------
# 证券身份：资格判定发生在 normalize_stock_code 之前
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("600519", "600519.SH"),
        ("600519.SH", "600519.SH"),
        ("sh600519", "600519.SH"),
        ("SH.600519", "600519.SH"),
        ("600519.SS", "600519.SH"),
        ("688795.SH", "688795.SH"),
        ("000001", "000001.SZ"),
        ("000001.SZ", "000001.SZ"),
        ("sz000001", "000001.SZ"),
        ("300750", "300750.SZ"),
        ("301236.SZ", "301236.SZ"),
    ],
)
def test_qualified_identity_accepts_ordinary_sh_sz(raw, expected):
    assert A.qualified_daily_identity(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "000001.SH",      # 上证指数：显式沪市 + 000 段不是普通股
        "sh000001",       # 同上前缀形态
        "600519.SZ",      # 矛盾身份：沪市代码配深市后缀
        "510300",         # 沪市 ETF
        "510300.SH",
        "159915",         # 深市 ETF
        "900901",         # 沪市 B 股
        "200002",         # 深市 B 股
        "920748",         # 北交所
        "920748.BJ",
        "BJ920748",
        "833171",         # 北交所历史段
        "HK00700",        # 港股
        "0700.HK",
        "AAPL",           # 美股
        "60051",          # 位数错误
        "600519.XSHG",    # 未支持后缀
        "",
        "贵州茅台",
    ],
)
def test_qualified_identity_rejects_out_of_scope(raw):
    assert A.qualified_daily_identity(raw) is None


def test_index_identity_never_becomes_bank(monkeypatch):
    """000001.SH 不得在规范化后变成 000001（平安银行）出站。"""
    from data_provider.base import normalize_stock_code

    enabled_config(monkeypatch)
    assert normalize_stock_code("000001.SH") == "000001"  # 既有规范化事实
    assert A.qualified_daily_identity("000001.SH") is None  # 资格判定先于规范化


# ---------------------------------------------------------------------------
# 管理器级路由：启用边界 + 时间资格 + 日历可用性
# ---------------------------------------------------------------------------
def test_daily_routing_requires_enabled_config(monkeypatch):
    strict_calendar(monkeypatch)
    enabled_config(monkeypatch, enabled=False)
    assert A.daily_routing("600519", None) is None


def test_daily_routing_clamps_to_latest_completed_session(monkeypatch):
    strict_calendar(monkeypatch)
    enabled_config(monkeypatch)
    identity, clamped = A.daily_routing("600519.SH", None)
    assert identity == "600519.SH"
    assert clamped == "2026-11-06"
    identity2, clamped2 = A.daily_routing("600519", "2026-11-10")
    assert clamped2 == "2026-11-06"


def test_daily_routing_skips_historical_target_before_dispatch(monkeypatch):
    strict_calendar(monkeypatch)
    enabled_config(monkeypatch)
    assert A.daily_routing("600519", "2026-11-05") is None
    assert A.daily_routing("600519", "2025-08-01") is None


def test_daily_routing_skips_unsupported_or_calendar_unavailable(monkeypatch):
    from src.core import trading_calendar as tc

    enabled_config(monkeypatch)
    assert A.daily_routing("510300", None) is None
    assert A.daily_routing("000001.SH", None) is None
    monkeypatch.setattr(tc, "recent_completed_trading_sessions", lambda *a, **k: None)
    assert A.daily_routing("600519", None) is None


# ---------------------------------------------------------------------------
# 严格数值解析
# ---------------------------------------------------------------------------
def test_decimal_cell_plain_and_scientific():
    assert A._decimal_cell("1195.67", allow_scale=False) == Decimal("1195.67")
    assert A._decimal_cell("9.526411E+7", allow_scale=False) == Decimal("9.526411E+7")
    assert A._decimal_cell("1,234.5", allow_scale=False) == Decimal("1234.5")


def test_decimal_cell_cn_scale_only_for_volume_amount():
    assert A._decimal_cell("5630.1365万", allow_scale=True) == Decimal("56301365")
    assert A._decimal_cell("17.3192亿", allow_scale=True) == Decimal("1731920000")
    # 价格字段不允许量级后缀：带万的价格不是价格，必须拒绝而不是换算。
    assert A._decimal_cell("1436万", allow_scale=False) is None


@pytest.mark.parametrize(
    "raw",
    ["", "\t", "-", "--", "nan", "None", "null", "=SUM(A1:A2)", "1+2", "abc", "1.2.3", "--5"],
)
def test_decimal_cell_rejects_placeholder_and_formula_text(raw):
    assert A._decimal_cell(raw, allow_scale=True) is None


# ---------------------------------------------------------------------------
# 元数据资格：四个价格字段独立校验 + 量/额单位
# ---------------------------------------------------------------------------
def test_metadata_accepts_real_long_cash_payload(monkeypatch):
    strict_calendar(monkeypatch)
    payload = daily_fixture("long_sh")
    A._qualify_metadata(payload["indicators_params"], "600519.SH")


def test_metadata_rejects_ipo_low_generic_qfq(monkeypatch):
    """真实 IPO 全量回执：最低价只回显泛「前复权」——任一字段不合格即拒绝。"""
    payload = daily_fixture("ipo_full")
    with pytest.raises(DataFetchError, match="最低价"):
        A._qualify_metadata(payload["indicators_params"], "688795.SH")


def test_metadata_rejects_missing_volume_unit(monkeypatch):
    """真实短窗探针（只请求价格）：成交量单位未回显，不构成资格。"""
    payload = daily_fixture("short_sh")
    with pytest.raises(DataFetchError, match="成交量单位"):
        A._qualify_metadata(payload["indicators_params"], "600519.SH")


def test_metadata_rejects_generic_and_reinvest_qfq(monkeypatch):
    base = daily_fixture("long_sh")["indicators_params"]
    for mode in ("前复权", "前复权(分红再投)", "后复权", "前复权（现金分红、再投）"):
        params = {k: dict(v) for k, v in base.items()}
        params["收盘价"] = {**params["收盘价"], "复权方式": mode}
        with pytest.raises(DataFetchError):
            A._qualify_metadata(params, "600519.SH")


def test_metadata_accepts_full_width_paren_variant(monkeypatch):
    base = daily_fixture("long_sh")["indicators_params"]
    params = {k: dict(v) for k, v in base.items()}
    params["收盘价"] = {**params["收盘价"], "复权方式": "前复权（现金分红）"}
    A._qualify_metadata(params, "600519.SH")


def test_metadata_rejects_wrong_volume_or_amount_unit(monkeypatch):
    base = daily_fixture("long_sh")["indicators_params"]
    params = {k: dict(v) for k, v in base.items()}
    params["成交量"] = {**params["成交量"], "单位": "手"}
    with pytest.raises(DataFetchError, match="成交量单位"):
        A._qualify_metadata(params, "600519.SH")
    params = {k: dict(v) for k, v in base.items()}
    params["成交额"] = {**params["成交额"], "单位": "万元"}
    with pytest.raises(DataFetchError, match="成交额单位"):
        A._qualify_metadata(params, "600519.SH")


# ---------------------------------------------------------------------------
# 内联表收集与整窗资格（真实 CSV/回执）
# ---------------------------------------------------------------------------
def test_sz_full_csv_qualifies_strict_sessions(monkeypatch):
    tc = strict_calendar(monkeypatch)
    rows = A._parse_csv_rows(daily_csv_text("sz_long"), "000001.SZ")
    sessions = tc.trading_sessions_between("cn", date(2025, 10, 14), date(2026, 11, 6))
    assert sessions is not None and len(sessions) == 260
    df = A._qualify_window(rows, sessions, "000001.SZ")
    assert len(df) == 260
    # 首行（2025-10-14）与末行（2026-11-06）取自真实导出
    assert str(df.iloc[0]["日期"]) == "2025-10-14"
    assert float(df.iloc[-1]["收盘价"]) == pytest.approx(11.81)
    assert float(df.iloc[-1]["成交量"]) == pytest.approx(164027639)


def test_inline_short_window_qualifies_without_export(monkeypatch):
    """真实短窗回执（未截断、无导出）：内联完整表可直接采纳。"""
    tc = strict_calendar(monkeypatch)
    payload = daily_fixture("halt")
    rows = A._collect_answer_rows(payload["answer"], "600150.SH")
    sessions = tc.trading_sessions_between("cn", date(2025, 8, 19), date(2025, 8, 20))
    df = A._qualify_window(rows, sessions, "600150.SH")
    assert len(df) == 2  # 复牌后两个交易日完整


def test_halt_window_whole_reject_missing_volume_amount(monkeypatch):
    """真实停牌窗口：价格前填而量/额缺失的交易日使整窗拒绝（不丢行称完整）。"""
    tc = strict_calendar(monkeypatch)
    payload = daily_fixture("halt")
    rows = A._collect_answer_rows(payload["answer"], "600150.SH")
    sessions = tc.trading_sessions_between("cn", date(2025, 8, 11), date(2025, 8, 20))
    assert sessions is not None
    with pytest.raises(DataFetchError, match="整窗不采纳"):
        A._qualify_window(rows, sessions, "600150.SH")


def test_ipo_full_window_rejects_on_pre_listing_sessions(monkeypatch):
    tc = strict_calendar(monkeypatch)
    rows = A._parse_csv_rows(daily_csv_text("ipo_full"), "688795.SH")
    sessions = tc.trading_sessions_between("cn", date(2025, 10, 14), date(2026, 11, 6))
    with pytest.raises(DataFetchError, match="整窗不采纳"):
        A._qualify_window(rows, sessions, "688795.SH")
    # 上市后的较短窗口按实际日期完整性判断，可以采纳
    post = tc.trading_sessions_between("cn", date(2025, 12, 5), date(2026, 11, 6))
    df = A._qualify_window(rows, post, "688795.SH")
    assert len(df) == 222


def test_nontrading_placeholders_excluded_by_strict_calendar(monkeypatch):
    """非交易日占位（周末前填）按严格日历剔除，不进入窗口也不触发拒绝。"""
    tc = strict_calendar(monkeypatch)
    rows = A._parse_csv_rows(daily_csv_text("sz_long"), "000001.SZ")
    sessions = tc.trading_sessions_between("cn", date(2026, 10, 26), date(2026, 10, 30))
    df = A._qualify_window(rows, sessions, "000001.SZ")
    assert [str(d) for d in df["日期"]] == [
        "2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30",
    ]


def test_conflicting_duplicate_date_rejected(monkeypatch):
    payload = daily_fixture("halt")
    original = "|600150.SH|中国船舶|20250819|37.55|2.4743亿|37.55|40.15|40.15|97.5905亿|"
    conflict = "|600150.SH|中国船舶|20250819|37.55|2.4743亿|37.55|41.15|40.15|97.5905亿|"
    tampered = payload["answer"].replace(original, original + "\n" + conflict, 1)
    with pytest.raises(DataFetchError, match="conflicting duplicate"):
        A._collect_answer_rows(tampered, "600150.SH")


def test_identical_duplicate_date_deduplicated(monkeypatch):
    payload = daily_fixture("halt")
    original = "|600150.SH|中国船舶|20250819|37.55|2.4743亿|37.55|40.15|40.15|97.5905亿|"
    tampered = payload["answer"].replace(original, original + "\n" + original, 1)
    rows = A._collect_answer_rows(tampered, "600150.SH")
    assert any(str(d) == "2025-08-19" for d in rows)


def test_wrong_security_rows_not_adopted(monkeypatch):
    payload = daily_fixture("halt")
    with pytest.raises(DataFetchError):
        A._collect_answer_rows(payload["answer"], "600519.SH")


def test_malformed_csv_structure_rejected(monkeypatch):
    good = daily_csv_text("sz_long")
    lines = good.splitlines()
    ragged = "\n".join([lines[0], lines[1] + ",extra"]) + "\n"
    with pytest.raises(DataFetchError, match="row structure"):
        A._parse_csv_rows(ragged, "000001.SZ")
    missing_col = "\n".join([lines[0].replace(",成交量", ""), lines[1].rsplit(",", 1)[0]]) + "\n"
    with pytest.raises(DataFetchError, match="missing required column"):
        A._parse_csv_rows(missing_col, "000001.SZ")


def test_formula_like_cell_rejects_window(monkeypatch):
    tc = strict_calendar(monkeypatch)
    rows = A._parse_csv_rows(daily_csv_text("sz_long"), "000001.SZ")
    target = rows[date(2026, 10, 30)]
    target["成交量"] = "=SUM(B2:B100)"
    sessions = tc.trading_sessions_between("cn", date(2026, 10, 26), date(2026, 10, 30))
    with pytest.raises(DataFetchError, match="成交量"):
        A._qualify_window(rows, sessions, "000001.SZ")


# ---------------------------------------------------------------------------
# 导出 URL 门槛：唯一允许的精确域 + 已验证路径 + 仅协议升级
# ---------------------------------------------------------------------------
def test_export_url_scheme_upgrade_http_to_https():
    answer = "下载URL为： http://o.thsi.cn/ifind-python-aime-tools-service.result-cache/query_result_1794140574667.csv"
    assert A.export_download_url(answer) == (
        "https://o.thsi.cn/ifind-python-aime-tools-service.result-cache/query_result_1794140574667.csv"
    )


def test_export_url_accepts_native_https():
    answer = "url: https://o.thsi.cn/ifind-python-aime-tools-service.result-cache/query_result_1.csv tail"
    assert A.export_download_url(answer).startswith("https://o.thsi.cn/")


@pytest.mark.parametrize(
    "url",
    [
        "http://o-thsi.cn/ifind-python-aime-tools-service.result-cache/query_result_1.csv",
        "http://evil.com/ifind-python-aime-tools-service.result-cache/query_result_1.csv",
        "http://sub.o.thsi.cn/ifind-python-aime-tools-service.result-cache/query_result_1.csv",
        "http://o.thsi.cn:8443/ifind-python-aime-tools-service.result-cache/query_result_1.csv",
        "http://user:pw@o.thsi.cn/ifind-python-aime-tools-service.result-cache/query_result_1.csv",
        "http://o.thsi.cn/ifind-python-aime-tools-service.result-cache/query_result_1.csv?x=1",
        "http://o.thsi.cn/ifind-python-aime-tools-service.result-cache/query_result_1.csv#frag",
        "http://o.thsi.cn/other-path/query_result_1.csv",
        "http://o.thsi.cn/ifind-python-aime-tools-service.result-cache/query_result_abc.csv",
        "http://o.thsi.cn/ifind-python-aime-tools-service.result-cache/../secret/query_result_1.csv",
    ],
)
def test_export_url_rejects_unverified_variants(url, monkeypatch):
    def _no_download(*_args, **_kwargs):
        raise AssertionError("URL gate must reject before any network request")

    monkeypatch.setattr(A, "stream_export_csv", _no_download)
    with pytest.raises(DataFetchError):
        A.export_download_url(f"链接 {url} 结束")


def test_export_url_ignores_non_http_schemes():
    # ftp 等协议不匹配导出链接形态：不采纳也绝不出站。
    assert A.export_download_url("链接 ftp://o.thsi.cn/a.csv 结束") is None


def test_export_url_rejects_multiple_distinct_links():
    answer = (
        "http://o.thsi.cn/ifind-python-aime-tools-service.result-cache/query_result_1.csv "
        "http://o.thsi.cn/ifind-python-aime-tools-service.result-cache/query_result_2.csv"
    )
    with pytest.raises(DataFetchError):
        A.export_download_url(answer)


def test_export_url_none_when_absent():
    assert A.export_download_url("无链接的回执") is None


def test_truncated_answer_marker():
    payload = daily_fixture("long_sh")
    assert A.answer_truncated(payload["answer"]) is True
    assert A.answer_truncated(daily_fixture("halt")["answer"]) is False


# ---------------------------------------------------------------------------
# 修正组5：深市指数命名空间（399xxx 不出站）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw",
    [
        "399001.SZ",   # 深证成指
        "399006.SZ",   # 创业板指
        "399001",      # 裸码推断同样拒绝
        "sz399001",
        "399300.SZ",
        "302123",      # 未获证据代码段
        "302123.SZ",
    ],
)
def test_sz_index_and_unverified_prefixes_bypass(raw):
    assert A.qualified_daily_identity(raw) is None


@pytest.mark.parametrize("raw", ["300750", "300750.SZ", "301236", "301236.SZ"])
def test_chinext_ordinary_stocks_still_qualified(raw):
    assert A.qualified_daily_identity(raw) in {"300750.SZ", "301236.SZ"}


def test_routing_skips_sz_index_without_dispatch(monkeypatch):
    strict_calendar(monkeypatch)
    enabled_config(monkeypatch)
    assert A.daily_routing("399001.SZ", None) is None
    assert A.daily_routing("399006.SZ", None) is None


def test_contradictory_inline_units_rejected_by_real_fetcher(monkeypatch):
    from types import SimpleNamespace

    from data_provider.ifind_daily_adapter import IfindDailyFetcher

    strict_calendar(monkeypatch)
    enabled_config(monkeypatch)
    answer = (
        "|证券代码|日期|开盘价|最高价|最低价|收盘价|成交量（单位：手）|成交额（单位：万元）|\n"
        "|---|---|---|---|---|---|---|---|\n"
        "|600519.SH|20261106|10|11|9|10|100|20|"
    )
    data = SimpleNamespace(answer=answer, indicators_params=daily_fixture("long_sh")["indicators_params"])
    fetcher = IfindDailyFetcher(SimpleNamespace(available=True, query=lambda *a, **kw: data))
    with pytest.raises(DataFetchError, match="列单位"):
        fetcher.get_daily_data("600519.SH", start_date="2026-11-06", end_date="2026-11-06")


def test_matching_inline_units_still_qualify(monkeypatch):
    from types import SimpleNamespace

    from data_provider.ifind_daily_adapter import IfindDailyFetcher

    strict_calendar(monkeypatch)
    enabled_config(monkeypatch)
    answer = (
        "|证券代码|日期|开盘价|最高价|最低价|收盘价|成交量（单位：股）|成交额（单位：元）|\n"
        "|---|---|---|---|---|---|---|---|\n"
        "|600519.SH|20261106|10|11|9|10|100|20|"
    )
    data = SimpleNamespace(answer=answer, indicators_params=daily_fixture("long_sh")["indicators_params"])
    fetcher = IfindDailyFetcher(SimpleNamespace(available=True, query=lambda *a, **kw: data))
    df = fetcher.get_daily_data("600519.SH", start_date="2026-11-06", end_date="2026-11-06")
    assert float(df.iloc[-1]["volume"]) == pytest.approx(100)
    assert float(df.iloc[-1]["amount"]) == pytest.approx(20)


def test_later_table_units_rejected_even_if_first_table_has_none(monkeypatch):
    """第一张匹配表只是非交易日占位；第二张表的手/万元仍整窗拒绝。"""
    from types import SimpleNamespace

    from data_provider.ifind_daily_adapter import IfindDailyFetcher

    strict_calendar(monkeypatch)
    enabled_config(monkeypatch)
    answer = (
        "|证券代码|日期|\n|---|---|\n|600519.SH|20261107|\n\n"
        "|证券代码|日期|开盘价|最高价|最低价|收盘价|成交量（单位：手）|成交额（单位：万元）|\n"
        "|---|---|---|---|---|---|---|---|\n"
        "|600519.SH|20261106|10|11|9|10|100|20|"
    )
    data = SimpleNamespace(answer=answer, indicators_params=daily_fixture("long_sh")["indicators_params"])
    fetcher = IfindDailyFetcher(SimpleNamespace(available=True, query=lambda *a, **kw: data))
    with pytest.raises(DataFetchError, match="列单位"):
        fetcher.get_daily_data("600519.SH", start_date="2026-11-06", end_date="2026-11-06")


def test_fragmented_tables_qualify_in_either_order(monkeypatch):
    from types import SimpleNamespace

    from data_provider.ifind_daily_adapter import IfindDailyFetcher

    strict_calendar(monkeypatch)
    enabled_config(monkeypatch)
    metadata = daily_fixture("long_sh")["indicators_params"]

    def table(day: str, explicit: bool) -> str:
        volume = "成交量（单位：股）" if explicit else "成交量"
        amount = "成交额（单位：元）" if explicit else "成交额"
        return (
            f"|证券代码|日期|开盘价|最高价|最低价|收盘价|{volume}|{amount}|\n"
            "|---|---|---|---|---|---|---|---|\n"
            f"|600519.SH|{day}|10|11|9|10|100|20|"
        )

    for explicit_first in (False, True):
        answer = table("20261105", explicit_first) + "\n\n" + table("20261106", not explicit_first)
        data = SimpleNamespace(answer=answer, indicators_params=metadata)
        fetcher = IfindDailyFetcher(SimpleNamespace(available=True, query=lambda *a, **kw: data))
        df = fetcher.get_daily_data("600519.SH", start_date="2026-11-05", end_date="2026-11-06")
        assert len(df) == 2
        assert float(df.iloc[-1]["volume"]) == pytest.approx(100)


def test_duplicate_csv_volume_rejected_by_real_fetcher(monkeypatch):
    from types import SimpleNamespace

    from data_provider import ifind_daily_adapter as adapter
    from data_provider.ifind_daily_adapter import IfindDailyFetcher

    strict_calendar(monkeypatch)
    enabled_config(monkeypatch)
    metadata = daily_fixture("long_sh")["indicators_params"]
    answer = (
        "|证券代码|日期|\n|---|---|\n|600519.SH|20261107|\n"
        "数据截断提示 https://o.thsi.cn/ifind-python-aime-tools-service.result-cache/query_result_123.csv"
    )

    def run(duplicate: bool):
        csv_text = (
            "证券代码,日期,开盘价,最高价,最低价,收盘价,成交量,成交额"
            + (",成交量" if duplicate else "")
            + "\n600519.SH,20261106,10,11,9,10,100,20"
            + (",999" if duplicate else "")
            + "\n"
        )
        monkeypatch.setattr(adapter, "stream_export_csv", lambda url, text=csv_text: text)
        data = SimpleNamespace(answer=answer, indicators_params=metadata)
        fetcher = IfindDailyFetcher(SimpleNamespace(available=True, query=lambda *a, **kw: data))
        return fetcher.get_daily_data("600519.SH", start_date="2026-11-06", end_date="2026-11-06")

    plain = run(False)
    assert float(plain.iloc[-1]["volume"]) == pytest.approx(100)
    with pytest.raises(DataFetchError, match="duplicate column"):
        run(True)
