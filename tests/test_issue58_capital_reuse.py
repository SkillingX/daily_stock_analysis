# -*- coding: utf-8 -*-
"""Issue #58 资金复用回归：同报告合格块零新增出站、缓存不携带资金、资格不放宽。

走真实装配链（``_safe_fundamental`` → ``SharedContext`` →
``_capital_flow_material``）与真实 Choice 传输夹具（真实 manager、真实盘上
快照缓存、真实资格层），缺口在传输夹具上构造、由真实资格链产出；启用交叉
验证时复用块走既有验证路径（主块零重取）。不 mock 预算/准入/资格路径，
无网络。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_choice_integration import choice_transport  # noqa: F401 - fixture 注入


@pytest.fixture()
def stage0_env(choice_transport, monkeypatch, tmp_path: Path):
    """真实 manager + 真实 _safe_fundamental，观察每次 Choice 原生出站。

    ``transport`` 是挂在类上的计数包装；后续用例若再叠加变换包装，必须链到
    它而不是绕过计数。
    """
    from data_provider.base import DataFetcherManager
    from data_provider.choice_adapter import ChoiceAdapter
    from src.agent.tools import data_tools

    monkeypatch.setattr(data_tools, "_fetcher_manager_singleton", DataFetcherManager())
    native = ChoiceAdapter._native_request
    calls: list[dict[str, object]] = []

    def transport(method, args, options, deadline):
        calls.append({"method": method, "indicator": args[1] if method in {"css", "csd"} else ""})
        return native(method, args, options, deadline)

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(transport))
    return {"calls": calls, "native": native, "transport": transport, "monkeypatch": monkeypatch, "tmp_path": tmp_path}


def _csd_count(calls: list[dict[str, object]]) -> int:
    return sum(1 for call in calls if call["method"] == "csd")


def test_valid_stage_zero_capital_reused_without_extra_outbound(stage0_env):
    """阶段 0 已取得合格 10 日资金：资金研究员零新增 NETINFLOW 出站。"""
    from src.agent.deep_research.explore_agents import _capital_flow_material
    from src.core.research_run import ResearchRun
    from src.deep_research_dims.context import SharedContext, _safe_fundamental

    calls = stage0_env["calls"]
    run = ResearchRun(60)
    ctx = SharedContext("300260", "样本", "2026-10-28")
    run.context = ctx
    with run.bind():
        ctx.fundamental = _safe_fundamental("300260", ctx)
        assert "capital_flow" in ctx.fundamental
        flow = ctx.fundamental["capital_flow"]["data"]["stock_flow"]
        assert flow["coverage"]["10"]["complete"]
        before = _csd_count(calls)
        pilot = _capital_flow_material("300260")
        after = _csd_count(calls)
    assert pilot is not None
    assert after == before, "qualified stage-zero capital must not re-fetch NETINFLOW"
    assert pilot["main_net_inflow"] == flow["main_net_inflow"]
    assert pilot["field_meta"]["main_inflow"]["rule_eligible"]
    assert pilot["expected_date"] == flow["expected_date"]
    assert pilot["source"] == "choice"


def test_financial_snapshot_cache_excludes_capital_block(stage0_env):
    """24h 财务快照不含资金块；命中缓存的新报告不继承前次资金，按自身有界补查取得。"""
    from src.agent.deep_research.explore_agents import _capital_flow_material
    from src.core.research_run import ResearchRun
    from src.deep_research_dims.context import SharedContext, _safe_fundamental

    calls = stage0_env["calls"]
    tmp_path: Path = stage0_env["tmp_path"]
    first = SharedContext("300260", "样本", "2026-10-28")
    with ResearchRun(60).bind():
        first.fundamental = _safe_fundamental("300260", first)
    assert first.fundamental.get("capital_flow")
    snapshots = list((tmp_path / "cache").glob("snap_stage0_fund_*.json"))
    assert snapshots, "fixture must exercise a real persisted financial snapshot"
    saved = json.loads(snapshots[0].read_text())["payload"]
    assert "capital_flow" not in saved, "dynamic capital must not enter the financial snapshot"

    before = _csd_count(calls)
    second = SharedContext("300260", "样本", "2026-10-29")
    with ResearchRun(60).bind():
        second.fundamental = _safe_fundamental("300260", second)
    # 命中财务快照的早返回路径不携带（也不复活）前次资金块。
    assert not second.fundamental.get("capital_flow"), "prior-run capital must not ride the financial cache"
    run = ResearchRun(60)
    run.context = second
    with run.bind():
        pilot = _capital_flow_material("300260")
    assert pilot is not None
    assert _csd_count(calls) > before, "capital needs must fetch on their own bounded path after a cache hit"
    assert pilot["main_net_inflow"] is not None


def test_incomplete_window_keeps_qualification_and_reuses_valid_fields(stage0_env):
    """真实传输夹具缺口（最旧一个交易日 NETINFLOW 缺失）：有效日/5日复用，
    10 日金额与窗口由真实资格层产出为缺失，零新增 NETINFLOW。

    变换包装链到计数包装（不替换它），并先断言阶段 0 真实发生至少一次
    初始 CSD，再比较复用前后计数。"""
    from data_provider.choice_adapter import ChoiceAdapter
    from src.agent.deep_research.explore_agents import _capital_flow_material
    from src.core.research_run import ResearchRun
    from src.deep_research_dims.context import SharedContext, _safe_fundamental

    calls = stage0_env["calls"]
    counting_transport = stage0_env["transport"]

    def drop_oldest_day(method, args, options, deadline):
        reply = counting_transport(method, args, options, deadline)
        # csd 夹具的日期升序（最旧在前）：把最旧一天的 NETINFLOW 置为缺失，
        # 由真实资格链产出 coverage/missing/errors，不在测试里手工标注资格。
        if method == "csd" and isinstance(reply, dict):
            code = reply["codes"][0]
            reply["data"][code][0][0] = None
        return reply

    stage0_env["monkeypatch"].setattr(ChoiceAdapter, "_native_request", staticmethod(drop_oldest_day))
    run = ResearchRun(60)
    ctx = SharedContext("300260", "样本", "2026-10-28")
    run.context = ctx
    with run.bind():
        ctx.fundamental = _safe_fundamental("300260", ctx)
        stock_flow = ctx.fundamental["capital_flow"]["data"]["stock_flow"]
        assert stock_flow["main_net_inflow"] is not None, "当日窗口应保持有效"
        assert stock_flow["inflow_5d"] is not None, "5 日窗口应保持有效"
        assert stock_flow["inflow_10d"] is None, "10 日窗口缺口应由真实资格层产出"
        coverage_10 = stock_flow["coverage"]["10"]
        assert not coverage_10["complete"] and coverage_10["missing_dates"]
        assert "choice:window_10_incomplete" in stock_flow["errors"]
        before = _csd_count(calls)
        assert before >= 1, "stage zero must have issued the initial CSD through the counting transport"
        pilot = _capital_flow_material("300260")
        after = _csd_count(calls)
    assert pilot is not None
    assert after == before, "partial-window reuse must not reissue NETINFLOW"
    assert pilot["main_net_inflow"] == stock_flow["main_net_inflow"]
    assert pilot["inflow_10d"] is None
    assert pilot["coverage"]["10"]["complete"] is False and pilot["coverage"]["10"]["missing_dates"]
    assert "choice:window_10_incomplete" in pilot["errors"]
    assert pilot["field_meta"]["main_inflow"]["rule_eligible"], "有效日金额的资格记录原样保留"


def test_invalid_stage_zero_capital_falls_back_to_bounded_refetch(stage0_env):
    """阶段 0 无有效资金（缺金额/量纲未确认）：不冒充复用，走既有有界补查。"""
    from src.agent.deep_research.explore_agents import _capital_flow_material
    from src.core.research_run import ResearchRun
    from src.deep_research_dims.context import SharedContext, _safe_fundamental

    calls = stage0_env["calls"]
    run = ResearchRun(60)
    ctx = SharedContext("300260", "样本", "2026-10-28")
    run.context = ctx
    with run.bind():
        ctx.fundamental = _safe_fundamental("300260", ctx)
        stock_flow = ctx.fundamental["capital_flow"]["data"]["stock_flow"]
        stock_flow["main_net_inflow"] = None
        stock_flow["field_meta"] = {
            "main_inflow": {"value": None, "rule_eligible": False, "quality": {"status": "missing", "reason_codes": ["window_incomplete"]}}
        }
        before = _csd_count(calls)
        pilot = _capital_flow_material("300260")
        after = _csd_count(calls)
    assert pilot is not None
    assert after > before, "unqualified stage-zero capital must fall back to the bounded refetch"
    assert pilot["main_net_inflow"] is not None


def test_capital_identity_guard_rejects_mismatched_context(stage0_env):
    """股票身份不一致的上下文不构成复用依据（身份/时点随块校验）。"""
    from src.agent.deep_research.explore_agents import _stage_zero_capital_flow
    from src.core.research_run import ResearchRun
    from src.deep_research_dims.context import SharedContext

    run = ResearchRun(60)
    ctx = SharedContext("600519", "样本", "2026-10-28")
    ctx.fundamental = {"capital_flow": {"status": "ok", "data": {"stock_flow": {"main_net_inflow": 1.0}}, "field_meta": {}}}
    run.context = ctx
    with run.bind():
        assert _stage_zero_capital_flow("300260", run) is None


def test_enabled_cross_validation_qualifies_reused_capital(choice_transport, monkeypatch):
    """opt-in 交叉验证开启时，复用块走既有验证路径：冲突保持不合格，不冒充已核验。"""
    from datetime import datetime, timezone

    from src.agent.deep_research.explore_agents import _capital_flow_material
    from src.agent.tools import cross_validation_helpers, data_tools
    from src.config import Config
    from src.core.research_run import ResearchRun
    from src.deep_research_dims.context import SharedContext
    from data_provider.cross_source_validator import (
        AnchorReading,
        CrossSourceValidator,
        SnapshotSource,
        adopted_field_record,
    )

    cfg = Config(deep_research_cross_validate=True)
    cfg.data_source_profile = "legacy"
    monkeypatch.setattr(Config, "_instance", cfg)
    now = datetime.now(timezone.utc).isoformat()
    primary = AnchorReading("mx", 100.0, unit="currency_base", currency="CNY", observed_at=now)
    other = AnchorReading("ifind", -100.0, unit="currency_base", currency="CNY", observed_at=now)
    block = {"status": "ok", "data": {"stock_flow": {"main_net_inflow": 100.0}},
             "field_meta": {"main_inflow": adopted_field_record("main_inflow", primary)}, "errors": []}
    monkeypatch.setattr(data_tools, "_get_fetcher_manager", lambda: __import__("types").SimpleNamespace(get_capital_flow_context=lambda code: block))
    monkeypatch.setattr(data_tools, "get_main_inflow_cumulative", lambda *args, **kwargs: {})
    monkeypatch.setattr(cross_validation_helpers, "_validator_instance", CrossSourceValidator([SnapshotSource("ifind", {"main_inflow": other})]))
    run = ResearchRun(10)
    run.context = SharedContext("300260", "fixture", now)
    run.context.fundamental = {"capital_flow": block}
    with run.bind():
        result = _capital_flow_material("300260")
    assert result is not None
    record = result["field_meta"]["main_inflow"]
    assert result.get("cross_validation"), "reuse must run the existing validation path"
    assert record["quality"]["status"] == "conflict"
    assert not record["rule_eligible"], "conflicting evidence must stay unqualified"
