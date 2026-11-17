# -*- coding: utf-8 -*-
"""第二十四轮共享表契约：HTTP 日线不落跨业务共享 StockDaily 表（真库序列）。

全部离线：真实 SQLite 数据库（tmp_path）+ 确定性严格日历 + stub manager，
不发真实供应商请求。锁定语义：
- 深研 HTTP 首取/复用/扩窗只在内存共享窗口，不写库、不覆盖旧源行；
- run 结束后普通链路读库命中完好旧源缓存（旧源不可用也可读取旧源）；
- 直接读表只见旧源行；
- HTTP 失败回退东财 → 东财行照常写库；全关行为不变；
- 存储层过滤精确匹配 HTTP 来源，MCP/旧源来源写入不受影响；
- 财务缓存身份（financial_cache_contract）不随 HTTP 开关翻转。
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from src.services import history_loader as H
from tests.ifind_daily_testutil import (
    LATEST_SESSION,
    http_enabled_config,
    strict_calendar,
)

CODE = "600519"
HTTP_SOURCE = "IfindHttpDailyFetcher"
MCP_SOURCE = "IfindDailyFetcher"
LEGACY_SOURCE = "EfinanceFetcher"


# ---------------------------------------------------------------------------
# 真实 SQLite 数据库与 stub 基建
# ---------------------------------------------------------------------------
@pytest.fixture()
def real_db(tmp_path, monkeypatch):
    """独立真实 SQLite 库：单例重置隔离，teardown 防单例泄漏到其他测试。"""
    from src.storage import DatabaseManager

    DatabaseManager.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{tmp_path}/shared_daily.db")
    monkeypatch.setattr("src.storage.get_db", lambda: db)
    try:
        yield db
    finally:
        monkeypatch.undo()
        DatabaseManager.reset_instance()


@pytest.fixture()
def offline(monkeypatch):
    from src.services import history_loader as loader

    strict_calendar(monkeypatch)
    monkeypatch.setattr(loader, "_fetcher_singleton", None)
    yield


class _StubManager:
    """按脚本出站的 manager stub：模拟单次调用内部的候选链重试。

    ``script`` 每个元素是一次 ``get_daily_data`` 调用的 attempt 列表；依次
    尝试（异常即尝试下一候选，模拟 manager 内部 HTTP 失败后走东财兜底），
    首个成功帧即返回，全部失败抛最后一个异常。每次尝试都记入 ``calls``。
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    def get_daily_data(self, stock_code, start_date=None, end_date=None, days=30, constraint_source=None):
        attempts = self.script.pop(0)
        last: Exception | None = None
        for entry in attempts:
            self.calls.append(
                {"stock_code": stock_code, "days": days, "constraint_source": constraint_source}
            )
            if isinstance(entry, Exception):
                last = entry
                continue
            return entry
        assert last is not None, "attempt 脚本为空"
        raise last


def _daily_df(close: float, sessions: int = 3, end: date = LATEST_SESSION) -> pd.DataFrame:
    rows = []
    for offset in range(sessions):
        day = end - timedelta(days=offset)
        rows.append(
            {
                "date": day,
                "open": close - 0.5,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": 1_000_000 + offset,
                "amount": close * 1_000_000,
                "pct_chg": 1.0,
            }
        )
    return pd.DataFrame(rows)


def _seed_legacy_rows(db, code: str = CODE, days: int = 40) -> pd.DataFrame:
    """预写旧源历史行（价格 10.0 基准，与 HTTP 帧 20.0 可区分）。"""
    df = _daily_df(10.0, sessions=days)
    saved = db.save_daily_data(df, code, LEGACY_SOURCE)
    assert saved > 0
    return df


def _bind_run():
    from src.core.research_run import ResearchRun, current_research

    run = ResearchRun(timeout_seconds=30)
    return current_research.set(run)


def _unbind_run(token) -> None:
    from src.core.research_run import current_research

    current_research.reset(token)


# ---------------------------------------------------------------------------
# 核心真库序列：旧源行 → 深研 HTTP 首取/复用/扩窗 → 普通读库 → 直接读表
# ---------------------------------------------------------------------------
def test_real_db_full_sequence_http_run_keeps_legacy_rows_intact(
    monkeypatch, offline, real_db
):
    http_enabled_config(monkeypatch, enabled=True, mcp_enabled=False)
    legacy_df = _seed_legacy_rows(real_db)

    http_frame = (_daily_df(20.0), HTTP_SOURCE)
    manager = _StubManager([[http_frame]])
    monkeypatch.setattr(H, "_get_fetcher_manager", lambda: manager)

    run_token = _bind_run()
    scope_token, _scope = H.open_current_stock_scope(CODE, target_date=LATEST_SESSION)
    try:
        # 深研首取：HTTP 帧进入内存共享窗口，DB 不被触碰
        df, source = H.load_history_df(CODE, days=30, target_date=LATEST_SESSION)
        assert source == HTTP_SOURCE
        assert float(df.iloc[0]["close"]) == 20.0
        assert len(manager.calls) == 1

        # run 内同源复用：零新出站、零写库
        df2, source2 = H.load_history_df(CODE, days=30, target_date=LATEST_SESSION)
        assert source2 == HTTP_SOURCE and len(manager.calls) == 1

        # 扩窗（更长需求）：同源 HTTP 再出站，仍不写库
        manager.script.append([(_daily_df(20.0, sessions=60), HTTP_SOURCE)])
        df3, source3 = H.load_history_df(CODE, days=60, target_date=LATEST_SESSION)
        assert source3 == HTTP_SOURCE and len(manager.calls) == 2

        # 存档动作（深研写库入口的存储层语义）：HTTP 来源被统一拒绝
        assert real_db.save_daily_data(df3, CODE, HTTP_SOURCE) == 0
    finally:
        H.close_current_stock_scope(scope_token)
        _unbind_run(run_token)

    # DB 完好：行数、价格、来源全部是旧源，无 HTTP 行
    rows = real_db.get_data_range(
        CODE, LATEST_SESSION - timedelta(days=60), LATEST_SESSION
    )
    assert rows, "旧源行必须保留"
    assert all(row.data_source == LEGACY_SOURCE for row in rows)
    assert all(abs(float(row.close) - 10.0) < 1e-9 for row in rows)
    assert not any("ifind" in str(row.data_source).lower() for row in rows)
    assert len(rows) >= len(legacy_df)

    # 普通读库（run 已结束）：旧源缓存命中，数据未被覆盖
    df_norm, source_norm = H.load_history_df(CODE, days=30, target_date=LATEST_SESSION)
    assert source_norm == "db_cache"
    assert float(df_norm.iloc[0]["close"]) == 10.0

    # 直接读表：只有旧源行
    latest = list(real_db.get_latest_data(CODE, days=2) or [])
    assert latest
    assert all(row.data_source == LEGACY_SOURCE for row in latest)


def test_normal_reader_hits_legacy_cache_when_legacy_source_down(
    monkeypatch, offline, real_db
):
    """旧源行在库、旧源网络故障：普通链路仍读取旧源缓存（不被 HTTP 行排斥）。"""
    http_enabled_config(monkeypatch, enabled=True, mcp_enabled=False)
    _seed_legacy_rows(real_db)
    manager = _StubManager([[RuntimeError("legacy source down")]])
    monkeypatch.setattr(H, "_get_fetcher_manager", lambda: manager)

    df, source = H.load_history_df(CODE, days=30, target_date=LATEST_SESSION)
    assert source == "db_cache"
    assert float(df.iloc[0]["close"]) == 10.0


def test_http_failure_fallback_legacy_rows_written_normally(
    monkeypatch, offline, real_db
):
    """深研 HTTP 失败回退东财：东财行照常写库（UPSERT 既有语义保留）。"""
    http_enabled_config(monkeypatch, enabled=True, mcp_enabled=False)
    _seed_legacy_rows(real_db, days=3)

    legacy_frame = (_daily_df(11.0), LEGACY_SOURCE)
    manager = _StubManager([[RuntimeError("http candidate failed"), legacy_frame]])
    monkeypatch.setattr(H, "_get_fetcher_manager", lambda: manager)

    run_token = _bind_run()
    scope_token, _scope = H.open_current_stock_scope(CODE, target_date=LATEST_SESSION)
    try:
        df, source = H.load_history_df(CODE, days=30, target_date=LATEST_SESSION)
        assert source == LEGACY_SOURCE
        assert len(manager.calls) == 2

        # 深研写库入口（data_tools/pipeline 同路径）：东财来源照常入库。
        # 同日期行为 UPSERT 覆盖（返回值只计新增，预置行同窗口时 saved==0），
        # 覆盖生效由下方行级断言验证。
        real_db.save_daily_data(df, CODE, source)
    finally:
        H.close_current_stock_scope(scope_token)
        _unbind_run(run_token)

    rows = real_db.get_data_range(
        CODE, LATEST_SESSION - timedelta(days=30), LATEST_SESSION
    )
    assert rows
    assert all(row.data_source == LEGACY_SOURCE for row in rows)
    by_date = {row.date if isinstance(row.date, date) else str(row.date)[:10]: row for row in rows}
    assert abs(float(by_date[LATEST_SESSION].close) - 11.0) < 1e-9


def test_storage_filter_http_source_rejected_mcp_and_legacy_accepted(
    offline, real_db
):
    """存储层过滤三分支：仅精确 HTTP 来源拒写，MCP/旧源照常入库。"""
    assert real_db.save_daily_data(_daily_df(20.0), CODE, HTTP_SOURCE) == 0
    assert not list(real_db.get_latest_data(CODE, days=5) or [])

    assert real_db.save_daily_data(_daily_df(12.0), CODE, MCP_SOURCE) > 0
    assert (
        real_db.save_daily_data(
            _daily_df(10.0, end=LATEST_SESSION - timedelta(days=3)), CODE, LEGACY_SOURCE
        )
        > 0
    )
    assert (
        real_db.save_daily_data(
            _daily_df(9.0, end=LATEST_SESSION - timedelta(days=10)), CODE, "Unknown"
        )
        > 0
    )

    rows = list(real_db.get_latest_data(CODE, days=10) or [])
    sources = {row.data_source for row in rows}
    assert sources == {MCP_SOURCE, LEGACY_SOURCE, "Unknown"}


def test_mcp_and_http_disabled_unchanged_write_behavior(
    monkeypatch, offline, real_db
):
    """MCP 与 HTTP 全关：普通链路取数与写库行为与既有完全一致。"""
    http_enabled_config(monkeypatch, enabled=False, mcp_enabled=False)
    manager = _StubManager([[(_daily_df(10.0), LEGACY_SOURCE)]])
    monkeypatch.setattr(H, "_get_fetcher_manager", lambda: manager)

    df, source = H.load_history_df(CODE, days=30, target_date=LATEST_SESSION)
    assert source == LEGACY_SOURCE

    # 既有语义：非启用范围取数后调用方照常写库（UPSERT 覆盖）
    saved = real_db.save_daily_data(df, CODE, source)
    assert saved > 0
    rows = list(real_db.get_latest_data(CODE, days=2) or [])
    assert rows and all(row.data_source == LEGACY_SOURCE for row in rows)


def test_legacy_concurrent_write_during_http_run_survives(
    monkeypatch, offline, real_db
):
    """HTTP run 活跃期间旧源并发写库：旧源行保留，不被 HTTP 窗口覆盖。"""
    http_enabled_config(monkeypatch, enabled=True, mcp_enabled=False)
    _seed_legacy_rows(real_db, days=3)

    manager = _StubManager([[(_daily_df(20.0), HTTP_SOURCE)]])
    monkeypatch.setattr(H, "_get_fetcher_manager", lambda: manager)

    run_token = _bind_run()
    scope_token, _scope = H.open_current_stock_scope(CODE, target_date=LATEST_SESSION)
    try:
        df, source = H.load_history_df(CODE, days=30, target_date=LATEST_SESSION)
        assert source == HTTP_SOURCE

        # 并发旧源写入（另一线程/任务的同股旧源落库）
        concurrent = _daily_df(12.5, sessions=3)
        assert real_db.save_daily_data(concurrent, CODE, LEGACY_SOURCE) >= 0
    finally:
        H.close_current_stock_scope(scope_token)
        _unbind_run(run_token)

    rows = real_db.get_data_range(
        CODE, LATEST_SESSION - timedelta(days=30), LATEST_SESSION
    )
    assert rows
    assert all(row.data_source == LEGACY_SOURCE for row in rows)
    assert all(abs(float(row.close) - 12.5) < 1e-9 for row in rows)


# ---------------------------------------------------------------------------
# 财务缓存身份隔离（financial_cache_contract）
# ---------------------------------------------------------------------------
def test_financial_cache_contract_ignores_http_toggle(monkeypatch):
    """本轮审查收敛（替代第三十四轮形态）：标准 HTTP 财务快照仅个股深研
    执行上下文出站——非深研普通基本面专项的财务缓存身份**不随开关翻转**
    （调用、预算、缓存三面均不被该开关改变）；完整 data_source_contract
    仍随开关变化（深研报告/维度缓存身份语义不变）；legacy 一致。
    """
    cfg_on = http_enabled_config(monkeypatch, enabled=True, mcp_enabled=False)
    contract_on = cfg_on.data_source_contract
    financial_on = cfg_on.financial_cache_contract
    assert contract_on.endswith("-ifind-http-daily-v1")
    assert financial_on == cfg_on._choice_contract_base  # 非深研财务来源与开关无关：身份不含 HTTP 段
    assert financial_on != contract_on

    cfg_off = http_enabled_config(monkeypatch, enabled=False, mcp_enabled=False)
    assert cfg_off.data_source_contract != contract_on  # 完整契约仍随开关变化（深研身份）
    assert cfg_off.financial_cache_contract == financial_on  # 财务身份稳定：旧缓存继续命中

    cfg_legacy = http_enabled_config(monkeypatch, enabled=False, mcp_enabled=False)
    cfg_legacy.data_source_profile = "legacy"
    assert cfg_legacy.financial_cache_contract == "legacy"
    assert cfg_legacy.data_source_contract == "legacy"


def test_financial_report_identity_stable_across_http_toggle(monkeypatch):
    """非深研身份稳定：HTTP on 期写入的财务报告身份在 off 后仍命中
    （普通业务缓存不因开关翻转失效——替代第三十四轮来源切换语义）。"""
    import json

    http_enabled_config(monkeypatch, enabled=True, mcp_enabled=False)
    from src.config import get_config

    written = {"_data_contract": get_config().financial_cache_contract}
    http_enabled_config(monkeypatch, enabled=False, mcp_enabled=False)
    # 开关翻转后身份不变：旧产物继续作为当前身份命中
    assert get_config().financial_cache_contract == written["_data_contract"]
    assert json.dumps(written)


# ---------------------------------------------------------------------------
# 第二十五轮：真实入口回归（Agent 日线工具 / 财务报告持久化与同日去重）
# ---------------------------------------------------------------------------
def test_agent_daily_history_tool_real_entry_http_run_keeps_shared_rows(
    monkeypatch, offline, real_db
):
    """深研 Agent 日线工具真实入口：HTTP 帧经 handler 取得且共享表旧源行不受影响。

    穿过 ``data_tools._handle_get_daily_history`` 真实入口（工具定义的
    handler 本体），工具内部 ``save_daily_data`` 走真实 SQLite 存储层过滤；
    覆盖深研首取、扩窗与 run 结束后普通读路径。
    """
    from src.agent.tools import data_tools

    http_enabled_config(monkeypatch, enabled=True, mcp_enabled=False)
    _seed_legacy_rows(real_db)
    monkeypatch.setattr(data_tools, "_get_db", lambda: real_db)

    manager = _StubManager([[(_daily_df(20.0), HTTP_SOURCE)]])
    monkeypatch.setattr(H, "_get_fetcher_manager", lambda: manager)

    run_token = _bind_run()
    scope_token, _scope = H.open_current_stock_scope(CODE, target_date=LATEST_SESSION)
    try:
        # 真实工具入口深研首取：HTTP 来源、零共享表写入
        response = data_tools._handle_get_daily_history(CODE, days=30)
        assert response["source"] == HTTP_SOURCE
        assert response["cache_hit"] is False
        assert len(manager.calls) == 1

        # 工具扩窗（更长窗口）：同源扩窗一次出站，共享表仍不被触碰
        manager.script.append([(_daily_df(20.0, sessions=60), HTTP_SOURCE)])
        response_wide = data_tools._handle_get_daily_history(CODE, days=60)
        assert response_wide["source"] == HTTP_SOURCE
        assert len(manager.calls) == 2
    finally:
        H.close_current_stock_scope(scope_token)
        _unbind_run(run_token)

    # 共享表：只有完好旧源行，无 HTTP 行（工具内部写库被存储层过滤）
    rows = real_db.get_data_range(
        CODE, LATEST_SESSION - timedelta(days=60), LATEST_SESSION
    )
    assert rows
    assert all(row.data_source == LEGACY_SOURCE for row in rows)
    assert all(abs(float(row.close) - 10.0) < 1e-9 for row in rows)
    assert not any("ifind" in str(row.data_source).lower() for row in rows)

    # run 结束后普通读路径：命中旧源缓存
    df_norm, source_norm = H.load_history_df(CODE, days=30, target_date=LATEST_SESSION)
    assert source_norm == "db_cache"
    assert float(df_norm.iloc[0]["close"]) == 10.0

    # 普通语境再走同一工具入口：DB 命中、零新出站
    response_norm = data_tools._handle_get_daily_history(CODE, days=30)
    assert response_norm["cache_hit"] is True
    assert response_norm["source"] == "db_cache"
    assert len(manager.calls) == 2


def test_fundamentals_report_persists_and_dedup_survives_http_toggle(
    monkeypatch, offline, real_db, tmp_path
):
    """财务报告真实生成落库 → HTTP 开关翻转 → 同日去重仍命中（零新出站）。

    穿过 ``generate_fundamentals_report`` 真实服务入口（身份写入、markdown
    渲染与落库全部真实执行；LLM 研究员与共享上下文为可控替身），再经
    ``_same_day_dedup_report`` 与第二次 ``generate`` 验证开关翻转后的同日
    去重读取行为。
    """
    from types import SimpleNamespace

    from src.services import fundamentals_service as fs

    http_enabled_config(monkeypatch, enabled=True, mcp_enabled=False)
    monkeypatch.setattr(fs, "get_fundamentals_dir", lambda: tmp_path)
    monkeypatch.setattr("src.storage.get_db", lambda: real_db)

    research_calls: list[str] = []

    def _fake_research(dim_id, code, name, **kwargs):
        research_calls.append(f"{dim_id}:{code}")
        return {"status": "ok", "dim": dim_id, "competitive_position": {"text": "stub"}}

    monkeypatch.setattr(fs, "_research_dim", _fake_research)
    monkeypatch.setattr(
        fs, "_rule_dim", lambda dim_id, code, name, ctx, position_text="": {
            "dim": dim_id, "status": "degraded", "degraded_reason": "stub",
        }
    )
    monkeypatch.setattr(
        "src.deep_research_dims.context.build_shared_context",
        lambda code, name, deadline=None: SimpleNamespace(
            quote={"name": name}, stock_name=name, fundamental={}
        ),
        raising=False,
    )

    first = fs.generate_fundamentals_report(CODE, "贵州茅台")
    # stub 场景 sector/financial 规则维度 degraded → 服务按真实语义判 partial；
    # 落库、身份写入与去重行为不受影响（business dim 为 ok）。
    assert first["status"] == "partial"
    assert first["report_id"]
    # 本轮审查收敛：非深研财务身份不含 HTTP 段（HTTP on 期写入亦然）
    assert not first["dims"]["_data_contract"].endswith("-ifind-http-daily-v1")
    assert research_calls == [f"business:{CODE}"]

    # 开关翻转：非深研财务身份稳定（缓存不因开关失效——本轮语义）
    http_enabled_config(monkeypatch, enabled=False, mcp_enabled=False)

    # 同日去重读取：旧产物身份仍命中（测试名本意：去重在开关翻转后存活）
    deduped = fs._same_day_dedup_report(CODE)
    assert deduped is not None, "开关翻转后同日去重仍命中（身份稳定）"
    assert (deduped.get("report_id") or deduped.get("id")) == first["report_id"]

    # 第二次 generate（非强刷）：同日去重命中（零新出站、零新报告）
    second = fs.generate_fundamentals_report(CODE, "贵州茅台")
    assert second["report_id"] == first["report_id"]
    assert not second["dims"]["_data_contract"].endswith("-ifind-http-daily-v1")
    assert research_calls == [f"business:{CODE}"]


def test_fundamentals_dedup_skips_degraded_business(monkeypatch, offline, real_db, tmp_path):
    """同日去重过滤链真实执行：business degraded 的当日记录不作为去重命中。"""
    import json
    from datetime import datetime

    from src.services import fundamentals_service as fs

    http_enabled_config(monkeypatch, enabled=True, mcp_enabled=False)
    monkeypatch.setattr(fs, "get_fundamentals_dir", lambda: tmp_path)
    monkeypatch.setattr("src.storage.get_db", lambda: real_db)

    contract = fs.get_config().financial_cache_contract if hasattr(fs, "get_config") else None
    from src.config import get_config

    contract = get_config().financial_cache_contract
    dims = {"business": {"status": "degraded", "degraded_reason": "stub"}}
    dims["_data_contract"] = contract
    rid = "fd_test_degraded_0001"
    ok = real_db.save_fundamentals_report(
        {
            "id": rid,
            "stock_code": CODE,
            "stock_name": CODE,
            "md_path": str(tmp_path / f"{rid}.md"),
            "dims_json": json.dumps(dims, ensure_ascii=False),
            "created_at": datetime.now(),
        }
    )
    assert ok
    assert fs._same_day_dedup_report(CODE) is None
