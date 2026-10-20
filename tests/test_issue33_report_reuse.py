"""Issue #33: session cache, shareholder period, and status false positives."""
from __future__ import annotations

import json
import time
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from data_provider.base import DataFetcherManager, daily_attempt_budget
from data_provider.ifind_fundamental_adapter import IfindFetcher, confirmed_disclosed_period
from src.core.research_run import ResearchRun
from src.deep_research.researchers.business import parse as parse_business
from src.deep_research.researchers.ownership import parse as parse_ownership
from src.deep_research_dims.conclusion_dim import build_conclusion_dim
from src.deep_research_dims.context import SharedContext
from src.deep_research_dims.data_dim import build_data_dim
from src.deep_research_dims.guardrail import apply_guardrails, rule4_probability_sum
from src.deep_research_dims.plan_dim import build_plan_dim
from src.deep_research_dims.scenarios_dim import VALUATION_CASCADE, build_scenarios_dim
from src.deep_research_dims.signal_dim import build_signal_dim
from src.patches.eastmoney_patch import request_deadline
from src.services.history_loader import cache_session_date, load_history_df
from tests.test_choice_integration import choice_transport
from tests.test_ifind_choice_integration import ifind_transport


SHANGHAI = ZoneInfo("Asia/Shanghai")
FRIDAY = date(2026, 10, 16)
SATURDAY = date(2026, 10, 17)


def _bars(latest: date, count: int = 30) -> list[MagicMock]:
    bars = []
    for offset in range(count):
        bar = MagicMock()
        bar.date = latest
        bar.to_dict.return_value = {
            "date": latest.isoformat(),
            "open": 10 + offset,
            "high": 11,
            "low": 9,
            "close": 10.5,
            "volume": 100,
        }
        bars.append(bar)
    return bars


def test_weekend_query_reuses_completed_friday_cache(monkeypatch):
    db = MagicMock()
    db.get_data_range.return_value = _bars(FRIDAY)
    monkeypatch.setattr("src.storage.get_db", lambda: db)
    frame, source = load_history_df("300209", days=60, target_date=SATURDAY)
    assert source == "db_cache"
    assert frame is not None
    assert str(frame["date"].iloc[-1])[:10] == FRIDAY.isoformat()
    assert db.get_data_range.call_args[0][2] == FRIDAY


def test_stale_or_future_cache_is_not_reused(monkeypatch):
    db = MagicMock()
    db.get_data_range.return_value = _bars(date(2026, 10, 15))
    monkeypatch.setattr("src.storage.get_db", lambda: db)
    called = []
    monkeypatch.setattr(
        "src.services.history_loader._get_fetcher_manager",
        lambda: MagicMock(get_daily_data=lambda *args, **kwargs: called.append(True) or (None, "none")),
    )
    frame, source = load_history_df("300209", days=30, target_date=SATURDAY)
    assert called and frame is None and source == "none"


def test_future_bars_are_not_a_completed_session(monkeypatch):
    db = MagicMock()
    db.get_data_range.return_value = _bars(date(2026, 10, 19))
    monkeypatch.setattr("src.storage.get_db", lambda: db)
    called = []
    monkeypatch.setattr(
        "src.services.history_loader._get_fetcher_manager",
        lambda: MagicMock(get_daily_data=lambda *args, **kwargs: called.append(True) or (None, "none")),
    )
    frame, source = load_history_df("300209", days=30, target_date=SATURDAY)
    assert called and frame is None and source == "none"


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 10, 17, 22, 30, tzinfo=SHANGHAI), FRIDAY),
        (datetime(2026, 10, 16, 10, 0, tzinfo=SHANGHAI), date(2026, 10, 15)),
        (datetime(2026, 10, 16, 16, 0, tzinfo=SHANGHAI), FRIDAY),
        (datetime(2026, 10, 1, 12, 0, tzinfo=SHANGHAI), date(2026, 9, 30)),
    ],
)
def test_cache_session_follows_completed_trading_day(moment, expected):
    assert cache_session_date("300209", now=moment) == expected


def test_slow_primary_leaves_budget_for_fallback():
    seen = {}

    class Slow:
        name = "SlowFetcher"
        priority = 0

        def get_daily_data(self, **kwargs):
            seen["deadline"] = request_deadline.get()
            raise TimeoutError("capped")

    class Fast:
        name = "FastFetcher"
        priority = 1

        def get_daily_data(self, **kwargs):
            return pd.DataFrame({"date": [FRIDAY.isoformat()], "close": [10.0]})

    manager = DataFetcherManager(fetchers=[Slow(), Fast()])
    outer = time.monotonic() + 2.0
    token = request_deadline.set(outer)
    try:
        frame, source = manager.get_daily_data("300209", days=30)
    finally:
        request_deadline.reset(token)
    assert source == "FastFetcher"
    assert len(frame) == 1
    # The reserve must actually bite: the slow primary saw a shortened
    # deadline, and the outer deadline itself was never extended.
    assert seen["deadline"] is not None and outer - seen["deadline"] >= 0.3
    assert time.monotonic() < outer


def test_cancelled_daily_fetch_stays_bounded():
    from src.core.research_run import ResearchStopped

    class Stopped:
        name = "StoppedFetcher"
        priority = 0

        def get_daily_data(self, **kwargs):
            raise ResearchStopped("stopped")

    manager = DataFetcherManager(fetchers=[Stopped()])
    with pytest.raises(ResearchStopped):
        manager.get_daily_data("300209", days=30)


def test_published_history_is_adopted_only_inside_the_same_open_run():
    from src.services.history_loader import adopt_published_history

    records = [
        {"date": FRIDAY.isoformat(), "close": float(index), "high": 11, "low": 9}
        for index in range(30)
    ]
    run = ResearchRun(30)
    other = ResearchRun(30)
    ctx = SharedContext("300209", "行云科技", "2026-10-17T22:52:34", fundamental={"revenue": 12})
    with run.bind():
        run.publish_daily_history(FRIDAY.isoformat(), records, "baostock")
        assert ctx.history == []
        assert adopt_published_history(ctx)
        assert len(ctx.history) == 30
        assert ctx.fundamental["revenue"] == 12
        data = build_data_dim(ctx)
        plan = build_plan_dim(ctx, data.model_dump(), "观察")
        assert data.status == "ok"
        assert plan.status == "ok"
        run.finish()
        late = SharedContext("300209", "行云科技", "2026-10-17T22:52:34")
        assert not adopt_published_history(late)
    with other.bind():
        isolated = SharedContext("300260", "新莱应材", "2026-10-17T22:52:34")
        assert not adopt_published_history(isolated)


def test_adoption_clears_only_history_notes():
    from src.agent.deep_research.orchestrator import _reconcile_shared_history

    records = [{"date": FRIDAY.isoformat(), "close": 10.0} for _ in range(30)]
    run = ResearchRun(30)
    ctx = SharedContext("300209", "行云科技", "2026-10-17T22:52:34")
    ctx.limitations[:] = [
        "quote 装配超时，本次未采用",
        "history 装配超时，本次未采用",
        "chip 装配超时，本次未采用",
        "日线历史数据缺失",
        "日线历史数据装配失败: fixture",
    ]
    with run.bind():
        run.publish_daily_history(FRIDAY.isoformat(), records, "tencent")
        dims = {"data": build_data_dim(SharedContext("300209", "行云科技", "2026-10-17T22:52:34"))}
        dims["data"] = dims["data"].model_copy(update={"status": "degraded", "degraded_reason": "日线历史不足 20 条，无法计算均线与点位"})
        _reconcile_shared_history(ctx, dims)
        assert ctx.history and dims["data"].status == "ok"
        assert ctx.limitations == [
            "quote 装配超时，本次未采用",
            "chip 装配超时，本次未采用",
        ]


def test_daily_attempt_budget_does_not_extend_the_outer_deadline():
    import time
    outer = time.monotonic() + 2
    token = request_deadline.set(outer)
    try:
        with daily_attempt_budget(1):
            assert request_deadline.get() < outer
        assert request_deadline.get() == outer
    finally:
        request_deadline.reset(token)


def test_latest_label_is_not_filled_from_the_request(ifind_transport):
    response = ifind_transport[1]
    response.clear()
    response.update(json.loads((Path("tests/fixtures/ifind_shareholders_latest_label.json")).read_text()))
    result = IfindFetcher("https://fixture.invalid", "test-only").shareholders("300260", "2026-06-30")
    assert result.items == []
    assert result.coverage == "partial"
    assert "period_unknown" in result.errors
    assert "2026-06-30" not in json.dumps(result.model_dump())


def test_explicit_period_reaches_ownership_chapter(ifind_transport):
    response = ifind_transport[1]
    response.clear()
    response.update(json.loads((Path("tests/fixtures/ifind_shareholders.json")).read_text()))
    result = IfindFetcher("https://fixture.invalid", "test-only").shareholders("300260", "2025-12-31")
    assert result.coverage == "complete"
    assert len(result.items) == 10
    assert "报告期2025-12-31" in ifind_transport[2][-1][1]
    dim, _ = parse_ownership({"shareholder_details": result.model_dump(mode="json"), "controller": "实控人甲"}, 1)
    assert len(dim.shareholder_details.items) == 10
    assert dim.shareholder_details.items[0].report_date == "2025-12-31"


def test_ownership_query_uses_confirmed_disclosed_period(ifind_transport, monkeypatch):
    from src.agent.deep_research import explore_agents

    ifind_transport[1].clear()
    ifind_transport[1].update(json.loads((Path("tests/fixtures/ifind_shareholders.json")).read_text()))
    monkeypatch.setattr(explore_agents, "_run_explore", lambda *args, **kwargs: {"ok": True, "data": {"controller": "实控人甲"}})
    explore_agents.run_ownership_agent(
        "300260",
        "新莱应材",
        None,
        research={"as_of": "2026-10-17", "holders": {"period": "2025-12-31", "disclosed_at": "2026-04-20"}},
    )
    assert any("报告期2025-12-31" in call[1] for call in ifind_transport[2])
    assert not any("最新已披露财期" in call[1] for call in ifind_transport[2])
    assert confirmed_disclosed_period({"as_of": "2026-10-17"}, as_of="2026-10-17") is None
    assert confirmed_disclosed_period(
        {"holders": {"period": "2026-06-30"}}, as_of="2026-10-17"
    ) == "2026-06-30"
    assert confirmed_disclosed_period(
        {"holders": {"period": "2026-06-30", "disclosed_at": "2026-08-20"}},
        as_of="2026-10-17",
    ) == "2026-06-30"
    assert confirmed_disclosed_period(
        {"field_meta": {"revenue": {"period": "2026-10-16", "disclosed_at": "2026-10-16"}}},
        as_of="2026-10-17",
    ) is None
    assert confirmed_disclosed_period(
        {"field_meta": {"revenue": {"period": "2026-09-30", "disclosed_at": "2026-10-25"}}},
        as_of="2026-10-17",
    ) is None


def test_holder_edges_keep_identity_units_and_totals(ifind_transport):
    from data_provider.cross_source_validator import SourceReadError

    response = ifind_transport[1]
    response.clear()
    response.update(json.loads((Path("tests/fixtures/ifind_shareholders.json")).read_text()))
    answer = response["data"]["answer"]
    response["data"]["answer"] = answer.replace("300260.SZ", "600519.SH")
    with pytest.raises(SourceReadError):
        IfindFetcher("https://fixture.invalid", "test-only").shareholders("300260", "2025-12-31")
    response["data"]["answer"] = answer.replace("25.06", "125.06", 1)
    overrun = IfindFetcher("https://fixture.invalid", "test-only").shareholders("300260", "2025-12-31")
    assert overrun.coverage == "partial"
    assert overrun.items[0].ratio is not None and overrun.items[0].ratio.normalization_error
    assert overrun.items[0].ratio.value != 57.68


def _holder_reply(names: str, shares: str, ratio: str, *, period: str = "20251231", extra_table: str | None = None) -> dict:
    params = {
        "股东名称__十大股东排名:第1名__": {"大股东排名": "第1名", "截止日期": period},
        "股东持股数量__十大股东排名:第1名__": {"大股东排名": "第1名", "截止日期": period, "单位": "股"},
        "股东持股比例__十大股东排名:第1名__": {"大股东排名": "第1名", "截止日期": period, "单位": "%"},
    }
    tables = [
        f"|证券代码|证券简称|股东名称（十大股东排名：第1名）|\n|---|---|---|\n|300260.SZ|样本|{names}|\n",
        f"|证券代码|证券简称|日期|股东持股数量（十大股东排名：第1名；单位：股）|股东持股比例（十大股东排名：第1名；单位：%）|\n|---|---|---|---|---|\n|300260.SZ|样本|{period}|{shares}|{ratio}|\n",
    ]
    if extra_table:
        tables.append(extra_table)
    return {"code": 1, "msg": "success", "data": {"answer": "\n".join(tables), "indicators_params": params}}


def test_partial_holder_list_stays_partial_and_zero_is_kept(ifind_transport):
    response = ifind_transport[1]
    response.clear()
    response.update(_holder_reply("股东甲", "0", "0"))
    result = IfindFetcher("https://fixture.invalid", "test-only").shareholders("300260", "2025-12-31")
    assert result.coverage == "partial"
    assert len(result.items) == 1
    assert result.items[0].shares is not None and result.items[0].shares.value == 0
    assert result.items[0].ratio is not None and result.items[0].ratio.value == 0
    assert not (result.items[0].shares.normalization_error or result.items[0].ratio.normalization_error)


def test_empty_holder_quantity_is_disclosed_not_filled(ifind_transport):
    response = ifind_transport[1]
    response.clear()
    response.update(_holder_reply("股东甲", "", "25.06"))
    result = IfindFetcher("https://fixture.invalid", "test-only").shareholders("300260", "2025-12-31")
    assert result.coverage == "partial"
    assert result.items[0].shares is None
    assert result.items[0].ratio is not None


def test_conflicting_duplicate_rank_is_rejected(ifind_transport):
    conflict_table = (
        "|证券代码|证券简称|日期|股东持股数量（十大股东排名：第1名；单位：股）|\n|---|---|---|---|\n"
        "|300260.SZ|样本|20251231|1亿|\n|300260.SZ|样本|20251231|2亿|\n"
    )
    response = ifind_transport[1]
    response.clear()
    response.update(_holder_reply("股东甲", "1亿", "25.06", extra_table=conflict_table))
    result = IfindFetcher("https://fixture.invalid", "test-only").shareholders("300260", "2025-12-31")
    assert result.coverage == "partial"
    assert "duplicate_rank_conflict" in result.errors


def test_total_ratio_column_is_never_a_holder_value(ifind_transport):
    totals = (
        "|证券代码|证券简称|日期|前十大股东持股比例合计（单位：%）|\n|---|---|---|---|\n"
        "|300260.SZ|样本|20251231|57.68|\n"
    )
    response = ifind_transport[1]
    response.clear()
    response.update(_holder_reply("股东甲", "1.022亿", "25.06", extra_table=totals))
    result = IfindFetcher("https://fixture.invalid", "test-only").shareholders("300260", "2025-12-31")
    assert all(item.ratio is None or item.ratio.value != 57.68 for item in result.items)
    assert len(result.items) == 1


def test_wrong_actual_period_is_disclosed_not_adopted(ifind_transport):
    response = ifind_transport[1]
    response.clear()
    response.update(json.loads((Path("tests/fixtures/ifind_shareholders.json")).read_text()))
    result = IfindFetcher("https://fixture.invalid", "test-only").shareholders("300260", "2025-09-30")
    assert result.coverage == "partial"
    assert "period_mismatch" in result.errors
    assert all(item.report_date == "2025-12-31" for item in result.items)


def test_network_history_trims_future_bars_before_use(monkeypatch, caplog):
    from src.services.history_loader import load_history_df
    from src.core.research_run import ResearchRun

    rows = [
        {"date": "2026-10-15", "close": 10.0},
        {"date": "2026-10-16", "close": 11.0},
        {"date": "2026-10-19", "close": 12.0},  # Monday bar beyond the Saturday query point
    ]
    frame = pd.DataFrame(rows)
    manager = MagicMock()
    manager.get_daily_data.return_value = (frame, "tencent")
    db = MagicMock()
    db.get_data_range.return_value = []
    monkeypatch.setattr("src.storage.get_db", lambda: db)
    monkeypatch.setattr("src.services.history_loader._get_fetcher_manager", lambda: manager)

    run = ResearchRun(30)
    with run.bind():
        trimmed, source = load_history_df("300209", days=30, target_date=SATURDAY)
    assert source == "tencent"
    assert list(trimmed["date"]) == ["2026-10-15", "2026-10-16"]
    assert run.daily_history is not None and run.daily_history["as_of"] == FRIDAY.isoformat()

    manager.get_daily_data.return_value = (pd.DataFrame([{"date": "2026-10-19", "close": 12.0}]), "tencent")
    with caplog.at_level("WARNING", logger="src.services.history_loader"):
        dropped, source = load_history_df("300209", days=30, target_date=SATURDAY)
    assert dropped is None and source == "none"
    assert any("均晚于已完成交易日" in record.message for record in caplog.records)


def test_declared_core_gap_degrades_but_incidental_mention_does_not():
    declared, _ = parse_business(
        {"business_model": {"value": "制造设备"}, "data_gaps": ["收入结构未公开披露"]}, 1
    )
    assert declared.status == "degraded"
    incidental, _ = parse_business(
        {
            "business_model": {"value": "制造设备"},
            "main_products": {"value": "真空系统，收入占比 62%"},
            "data_gaps": ["收入结构披露口径存在差异，详见正文"],
        },
        1,
    )
    assert incidental.status == "ok"


def test_adoption_refuses_populated_context_and_foreign_session():
    from src.services.history_loader import adopt_published_history

    records = [{"date": FRIDAY.isoformat(), "close": 10.0} for _ in range(30)]
    run = ResearchRun(30)
    with run.bind():
        run.publish_daily_history(FRIDAY.isoformat(), records, "tencent")
        populated = SharedContext("300209", "行云科技", "2026-10-17T22:52:34", history=[{"date": FRIDAY.isoformat()}])
        assert not adopt_published_history(populated)
        assert len(populated.history) == 1
        foreign = SharedContext("300209", "行云科技", "2026-10-15T18:00:00")
        assert not adopt_published_history(foreign)
        assert foreign.history == []


def test_wan_unit_quantity_is_converted_but_flagged_not_clean(ifind_transport):
    reply = _holder_reply("股东甲", "1.2亿", "25.06")
    reply["data"]["indicators_params"]["股东持股数量__十大股东排名:第1名__"]["单位"] = "万股"
    response = ifind_transport[1]
    response.clear()
    response.update(reply)
    result = IfindFetcher("https://fixture.invalid", "test-only").shareholders("300260", "2025-12-31")
    assert result.coverage == "partial"
    assert result.items[0].shares is not None
    assert result.items[0].shares.value == 120000000.0
    assert result.items[0].shares.normalization_error == "unit_conflict"


def test_unranked_columns_never_become_holder_values(ifind_transport):
    reply = _holder_reply("股东甲", "1亿", "25.06")
    unranked = (
        "|证券代码|证券简称|日期|股东持股数量（单位：股）|股东持股比例（单位：%）|\n|---|---|---|---|---|\n"
        "|300260.SZ|样本|20251231|9亿|99.99|\n"
    )
    reply["data"]["answer"] += unranked
    response = ifind_transport[1]
    response.clear()
    response.update(reply)
    result = IfindFetcher("https://fixture.invalid", "test-only").shareholders("300260", "2025-12-31")
    assert len(result.items) == 1
    assert result.items[0].shares is not None and result.items[0].shares.value == 100000000.0
    assert all(item.ratio is None or item.ratio.value != 99.99 for item in result.items)


def test_abnormal_probabilities_are_normalized_without_false_guardrail():
    ctx = SharedContext("300209", "行云科技", "2026-10-17T22:55:20", fundamental={})
    dim = build_scenarios_dim(
        ctx,
        18.5,
        {"scenarios": [
            {"type": "optimistic", "probability": 0.7, "description": "需求上行"},
            {"type": "pessimistic", "probability": 0.1, "description": "需求下行"},
        ]},
    )
    assert dim.probability_sum == 1.0
    assert abs(sum(s.probability for s in dim.scenarios.scenarios) - 1.0) < 1e-9
    events, adjusted = apply_guardrails({"scenarios": dim.model_dump(), "signal": {}, "conclusion": {}, "intel": {}, "six_dim": {}})
    assert not [e for e in events if e.rule_id == "GR4_probability_sum"]


def test_local_business_gap_does_not_fail_the_chapter():
    payload = {
        "business_model": {"value": "半导体设备制造"},
        "main_products": {"value": "真空系统，收入占比 62%"},
        "competitive_position": {"value": "国内洁净应用设备供应商"},
        "vs_market_leader": {"value": "与龙头相比规模较小"},
        "data_gaps": ["市场份额未公开披露"],
        "score": 70,
    }
    dim, _ = parse_business(payload, 1)
    assert dim.status == "ok"
    assert dim.data_gaps == ["市场份额未公开披露"]
    assert dim.score == 70


def test_missing_or_placeholder_core_business_stays_limited():
    core_only, _ = parse_business({"business_model": {"value": "制造设备"}, "data_gaps": []}, 1)
    assert core_only.status == "ok"
    no_core, _ = parse_business({"competitive_position": {"value": "国内领先"}, "data_gaps": []}, 1)
    assert no_core.status == "degraded"
    placeholder, _ = parse_business(
        {"business_model": {"value": "未知"}, "main_products": {"value": "待补充"}}, 1
    )
    assert placeholder.status == "degraded"
    unfinished, _ = parse_business(
        {
            "status": "degraded",
            "degraded_reason": "研究模型未完成；保留已取得业务资料",
            "business_model": {"value": "制造设备"},
            "main_products": {"value": "真空系统"},
            "data_gaps": ["竞争地位与同行对比未完成"],
        },
        1,
    )
    assert unfinished.status == "degraded"
    assert unfinished.degraded_reason and "研究模型未完成" in unfinished.degraded_reason


def test_empty_scenarios_do_not_claim_normalization():
    payload = {
        "valuation_basis": "none",
        "probability_sum": 0,
        "scenarios": {"scenarios": []},
    }
    assert rule4_probability_sum(payload) == []
    consensus = {**payload, "valuation_basis": "analyst_consensus"}
    assert rule4_probability_sum(consensus) == []
    abnormal = {
        "valuation_basis": "none",
        "probability_sum": 0.4,
        "scenarios": {"scenarios": [{"type": "neutral", "probability": 0.4}]},
    }
    events = rule4_probability_sum(abnormal)
    assert events and events[0].rule_id == "GR4_probability_sum"
    legal = {
        "valuation_basis": "none",
        "probability_sum": 1,
        "scenarios": {"scenarios": [{"type": "neutral", "probability": 1}]},
    }
    assert rule4_probability_sum(legal) == []
    _, adjusted = apply_guardrails({"scenarios": payload, "signal": {}, "conclusion": {}, "intel": {}, "six_dim": {}})
    assert adjusted["scenarios"]["probability_sum"] == 0


def test_missing_valuation_explains_cascade_without_clearing_limits():
    ctx = SharedContext("300209", "行云科技", "2026-10-17T22:55:20", fundamental={})
    scenarios = build_scenarios_dim(ctx, 18.5)
    conclusion = build_conclusion_dim(
        ctx, {"framework": {"dimension_total": 80}}, {"bayesian": {"posterior_p": 0.8, "edge": 0.3}}, scenarios.model_dump()
    )
    signal = build_signal_dim(ctx, conclusion.model_dump(), scenarios.model_dump())
    assert scenarios.status == conclusion.status == signal.status == "degraded"
    assert scenarios.target_price is None and signal.target_price is None
    for reason in (scenarios.degraded_reason, conclusion.degraded_reason, signal.degraded_reason):
        assert reason and VALUATION_CASCADE in reason
    from src.deep_research_dims.render import build_view, render_markdown
    from tests.test_deep_research_dims import _all_default_dims

    dims = _all_default_dims()
    dims["scenarios"] = scenarios
    dims["conclusion"] = conclusion
    dims["signal"] = signal
    events, _ = apply_guardrails({key: value.model_dump() for key, value in dims.items()})
    markdown = render_markdown(build_view("行云科技", "300209", ctx.as_of, [], dims, events))
    assert VALUATION_CASCADE in markdown
    assert "已触发归一化" not in markdown
    assert "未完整完成" in markdown
