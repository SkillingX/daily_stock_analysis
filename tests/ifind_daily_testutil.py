# -*- coding: utf-8 -*-
"""issue #86 iFinD 日线测试共享工具：启用配置、确定性严格日历与真实 fixture。

全部离线：不读 .env、不发真实供应商/LLM 请求；严格日历按固定参考时间
monkeypatch，窗口资格仍走真实 ``trading_sessions_between`` 实现。
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "ifind_daily"
NOW = datetime(2026, 11, 8, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
LATEST_SESSION = date(2026, 11, 6)
SESSIONS_NEWEST_FIRST = [LATEST_SESSION, date(2026, 11, 5), date(2026, 11, 4)]


def daily_fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def daily_csv_text(name: str) -> str:
    return (FIXTURES / f"{name}.csv").read_text()


def enabled_config(monkeypatch: pytest.MonkeyPatch, *, enabled: bool = True, profile: str = "choice"):
    """choice + ENABLE_IFIND_MCP + 有效配置（或显式关闭的对照配置）。"""
    from src.config import Config

    config = Config(enable_fundamental_pipeline=True, deep_research_cross_validate=False)
    config.data_source_profile = profile
    config.enable_ifind_mcp = enabled
    config.financial_primary_source = "choice"
    config.ifind_mcp_endpoint = "https://fixture.invalid/mcp" if enabled else None
    config.ifind_mcp_token = "test-only" if enabled else None
    config.ifind_mcp_timeout_seconds = 30.0
    monkeypatch.setattr(Config, "_instance", config)
    return config


def strict_calendar(monkeypatch: pytest.MonkeyPatch):
    """确定性严格日历：会话枚举走真实实现，参考时间固定为 NOW。

    只 patch tc 模块属性（三个入口），返回 tc 模块本身（既有契约，调用者
    如 test_ifind_daily_adapter 以 ``tc = strict_calendar(...)`` 取模块）。
    消费 pipeline 时间的 offline fixture（test_ifind_daily_cache）在
    teardown 用 ``tc.get_effective_trading_date`` 的当前值识别本作用域
    写入的实现，对 pipeline 的 from-import 别名做条件恢复。
    """
    from src.core import trading_calendar as tc

    real_between = tc.trading_sessions_between
    real_effective = tc.get_effective_trading_date

    def _sessions(market, start, end, *, current_time=None):
        return real_between(market, start, end, current_time=current_time or NOW)

    def _effective(market, current_time=None):
        # 显式 target（含冻结目标）委托真实日历；无参考时间时冻结为 NOW 语义。
        if current_time is not None:
            return real_effective(market, current_time=current_time)
        return LATEST_SESSION

    monkeypatch.setattr(
        tc, "recent_completed_trading_sessions",
        lambda market, count, current_time=None, **_: SESSIONS_NEWEST_FIRST[: max(0, count)],
    )
    monkeypatch.setattr(tc, "get_effective_trading_date", _effective)
    monkeypatch.setattr(tc, "trading_sessions_between", _sessions)
    return tc


def restore_pipeline_alias_if_scoped(scope_effective, entered_effective) -> None:
    """仅当已导入 pipeline 的日历别名仍指向 *scope_effective* 时恢复为进入值。

    pipeline 经 from-import 在导入时快照 tc 属性：若首次导入发生在某
    strict_calendar 作用域内，别名绑定该作用域实现，monkeypatch.undo 恢复
    tc 后别名仍留在旧引用上。此恢复只精确处理这一种情况——别名已被其它
    逻辑（外层 fixture、后续作用域）改写时不无条件覆盖，外层 sentinel 得以
    保留；无导入时为 no-op。
    """
    import sys

    pipeline_module = sys.modules.get("src.core.pipeline")
    if (
        pipeline_module is not None
        and pipeline_module.get_effective_trading_date is scope_effective
    ):
        pipeline_module.get_effective_trading_date = entered_effective


class RecordingIfindStub:
    """manager 注入用的 IfindDailyFetcher 桩：记录调用并可编排结果/异常。"""

    name = "IfindDailyFetcher"
    priority = 0
    allow_empty_daily_data = False

    def __init__(self, df=None, error: Exception | None = None) -> None:
        self.df = df
        self.error = error
        self.calls: list[dict] = []
        self._available = True

    @property
    def available(self) -> bool:
        return self._available

    def is_available_for_request(self, capability: str = "") -> bool:
        return self._available

    def get_daily_data(self, stock_code, start_date=None, end_date=None, days=30):
        self.calls.append({
            "stock_code": stock_code, "start_date": start_date,
            "end_date": end_date, "days": days,
        })
        if self.error is not None:
            raise self.error
        return self.df


def std_frame(rows: int = 5, last: str = "2026-11-06", close: float = 10.0):
    import pandas as pd

    dates = pd.bdate_range(end=last, periods=rows).strftime("%Y-%m-%d")
    return pd.DataFrame({
        "date": dates,
        "open": [close] * rows, "high": [close + 1] * rows, "low": [close - 1] * rows,
        "close": [close] * rows, "volume": [1000.0] * rows, "amount": [10000.0] * rows,
        "pct_chg": [0.0] * rows, "ma5": [close] * rows, "ma10": [close] * rows,
        "ma20": [close] * rows, "volume_ratio": [1.0] * rows,
    })


def fake_ifind_transport(monkeypatch: pytest.MonkeyPatch, payload_name: str, csv_name: str | None = None):
    """把 IfindFetcher.query 与导出下载接到真实 fixture（零真实网络）。"""
    from data_provider import ifind_daily_adapter as daily_adapter
    from data_provider.ifind_fundamental_adapter import IfindData, IfindFetcher

    payload = daily_fixture(payload_name)
    csv_text = daily_csv_text(csv_name) if csv_name else None
    queries: list[str] = []
    downloads: list[str] = []

    def fake_query(self, tool, question, *, deadline=None):
        queries.append((tool, question))
        return IfindData(answer=payload["answer"], indicators_params=payload["indicators_params"])

    def fake_stream(url, *, fetch=None):
        downloads.append(url)
        return csv_text

    monkeypatch.setattr(IfindFetcher, "query", fake_query)
    monkeypatch.setattr(daily_adapter, "stream_export_csv", fake_stream)
    return SimpleNamespace(queries=queries, downloads=downloads)
