# -*- coding: utf-8 -*-
"""issue #86：IfindDailyFetcher 完整获取链（真实脱敏 MCP 回执 + CSV 导出 fixture）。

经真实 BaseFetcher 生命周期（原始表→标准化→清洗→指标）与真实解析/资格层；
MCP 出站与导出下载接到 fixture（零真实网络），断言「一次 MCP 业务问句 +
至多一次完整导出下载」的整窗去重边界与 IPO/停牌整窗拒绝。
"""
from __future__ import annotations

import threading
from datetime import date

import pytest

from data_provider.base import DataFetchError
from data_provider.ifind_daily_adapter import IfindDailyFetcher
from data_provider.ifind_fundamental_adapter import IfindFetcher
from src.core.research_run import ResearchRun, current_research
from tests.ifind_daily_testutil import (
    fake_ifind_transport,
    strict_calendar,
)


@pytest.fixture()
def fetcher(monkeypatch):
    strict_calendar(monkeypatch)
    return IfindDailyFetcher(IfindFetcher("https://fixture.invalid/mcp", "test-only"))


def test_long_window_requires_complete_csv_export(fetcher, monkeypatch):
    transport = fake_ifind_transport(monkeypatch, "long_sh", "long_sh")
    df = fetcher.get_daily_data("600519.SH", "2025-10-14", "2026-11-06")
    # 截断提示 → 必须取完整导出，不能把内联前 49 行当全量
    assert len(transport.queries) == 1 and len(transport.downloads) == 1
    assert len(df) == 260
    assert str(df.iloc[-1]["date"])[:10] == "2026-11-06"
    assert float(df.iloc[-1]["close"]) == pytest.approx(1453.0)
    assert float(df.iloc[-1]["volume"]) == pytest.approx(4284772.0)
    assert float(df.iloc[-1]["amount"]) == pytest.approx(6199528289.0)
    # 标准化列与技术指标齐备（真实 BaseFetcher 生命周期）
    for column in ("date", "open", "high", "low", "close", "volume", "amount", "pct_chg", "ma5"):
        assert column in df.columns


def test_sz_long_window_full_fetch(fetcher, monkeypatch):
    transport = fake_ifind_transport(monkeypatch, "sz_long", "sz_long")
    df = fetcher.get_daily_data("000001.SZ", "2025-10-14", "2026-11-06")
    assert len(transport.downloads) == 1
    assert len(df) == 260
    assert float(df.iloc[-1]["volume"]) == pytest.approx(164027639)
    assert float(df.iloc[-1]["amount"]) == pytest.approx(1927307590.53)


def test_short_window_uses_inline_table_without_export(fetcher, monkeypatch):
    transport = fake_ifind_transport(monkeypatch, "halt", None)
    df = fetcher.get_daily_data("600150.SH", "2025-08-19", "2025-08-20")
    assert len(transport.queries) == 1
    assert transport.downloads == []  # 未截断的内联完整表直接采纳
    assert len(df) == 2


def test_halt_window_whole_rejection_falls_back(fetcher, monkeypatch):
    transport = fake_ifind_transport(monkeypatch, "halt", None)
    with pytest.raises(DataFetchError, match="整窗不采纳"):
        fetcher.get_daily_data("600150.SH", "2025-08-11", "2025-08-20")
    assert transport.downloads == []


def test_ipo_metadata_rejected_before_any_download(fetcher, monkeypatch):
    transport = fake_ifind_transport(monkeypatch, "ipo_full", "ipo_full")
    with pytest.raises(DataFetchError, match="最低价"):
        fetcher.get_daily_data("688795.SH", "2025-12-05", "2026-11-06")
    # 资格在元数据层整窗拒绝：不发生完整导出下载
    assert transport.downloads == []


def test_identity_gate_inside_fetcher(fetcher, monkeypatch):
    fake_ifind_transport(monkeypatch, "long_sh", "long_sh")
    with pytest.raises(DataFetchError):
        fetcher.get_daily_data("000001", "2025-10-14", "2026-11-06")  # 缺交易所身份的裸码


def test_duplicate_concurrent_full_fetch_single_flight(fetcher, monkeypatch):
    """同 run 同窗口并发：一次 MCP 业务问句 + 一次 CSV 下载（完整结果单飞）。"""
    calls = {"query": 0, "download": 0}
    lock = threading.Lock()

    class SlowTransport:
        def __init__(self, payload_name, csv_name):
            from tests.ifind_daily_testutil import daily_csv_text, daily_fixture

            self.payload = daily_fixture(payload_name)
            self.csv_text = daily_csv_text(csv_name) if csv_name else None

        def query(self, tool, question, *, deadline=None):
            from data_provider.ifind_fundamental_adapter import IfindData

            with lock:
                calls["query"] += 1
            threading.Event().wait(0.2)  # 制造并发窗口
            return IfindData(
                answer=self.payload["answer"],
                indicators_params=self.payload["indicators_params"],
            )

        def download(self, url):
            with lock:
                calls["download"] += 1
            threading.Event().wait(0.1)
            return self.csv_text

    slow = SlowTransport("sz_long", "sz_long")
    monkeypatch.setattr(IfindFetcher, "query", slow.query)
    from data_provider import ifind_daily_adapter as daily_adapter

    monkeypatch.setattr(daily_adapter, "stream_export_csv", slow.download)

    run = ResearchRun(timeout_seconds=60)
    token = current_research.set(run)
    results: list = []
    try:

        def worker():
            results.append(fetcher.get_daily_data("000001.SZ", "2025-10-14", "2026-11-06"))

        import contextvars

        contexts = [contextvars.copy_context() for _ in range(3)]
        threads = [threading.Thread(target=ctx.run, args=(worker,)) for ctx in contexts]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
    finally:
        current_research.reset(token)
    assert len(results) == 3
    assert {len(df) for df in results} == {260}
    assert calls["query"] == 1 and calls["download"] == 1
