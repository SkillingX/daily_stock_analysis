# -*- coding: utf-8 -*-
"""Issue #73：iFinD 主源深研零 Choice 出站 + 非估值分析完善。

规格（远端 issue #73 为准）：
- 现有 ``DATA_SOURCE_PROFILE=choice`` + ``FINANCIAL_PRIMARY_SOURCE=ifind`` +
  ``ENABLE_IFIND_MCP=true`` 的深研运行，Choice 完全不可用/无凭据时，聚合/header/
  资产负债/目标价/公司、独立核验（validators）、研究工具补查、取消与缓存路径
  的 Choice 实际出站均为 0；iFinD 资金失败不再回退 Choice。
- 聚合当前资料批次对 PB 发精确指标请求（市净率(PB,最新)，基准日=报表截止日期）；
  中证/% 变体等错误口径仍被生产解析拒绝，不别名成 MRQ。
- 非估值分支：iFinD 主源深研不查询目标价（未查询语义）与 Choice 无覆盖一样可在
  充分证据下完成定性情景/结论/观察信号；F1 整维 gate 的豁免仅限「四个非估值
  财务评分输入全部 rule_eligible 且 F1 不完整只来自 PE/PB 估值输入」，资金未
  完成、核心字段缺失或证据空壳仍不放行；F1 自身保持 degraded 并列明真实字段缺口
  （缺 PB 时 data_gaps 不再 0 项假象）。
- 停止 Choice 独立核验不清空已采纳单源事实：``financial_snapshot`` 不再依赖
  ``cross_validation`` 块存在，六项财务事实及其来源/资格随保存载荷保留，且不
  合成 verified 核验记录。
- Choice 主源行为（含 Choice 主源深研 scope）与普通消费者（include_sector_flows=
  True）保持兼容；独立年报入口不在本票范围。

测试只替换外部传输（Choice 原生/iFinD query），全部断言经生产解析/资格/编排/
保存层，供应商回执为 2026-11-02 真实脱敏样本（600176 中国巨石）与既有 688183
fixtures；零网络、零模型、零付费调用。
"""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, List
from zoneinfo import ZoneInfo

import pytest

from tests.test_choice_integration import choice_transport
from tests.test_ifind_choice_integration import ifind_transport  # noqa: F401  （成对回归的 Choice 健康传输）
from data_provider.base import DataFetcherManager
from data_provider.capital_flow_provider import get_main_inflow_cumulative
from data_provider.choice_adapter import ChoiceAdapter
from data_provider.cross_source_validator import reading_input_reasons
from data_provider.ifind_fundamental_adapter import IfindFetcher

FIXTURES = Path(__file__).parent / "fixtures"

# 2026-11-02 真实脱敏回执（600176 中国巨石）：
# 原聚合同题回执把 PB 映射为「市净率(中证发布)（单位：%）」→ 生产解析不得采纳；
# 定向精确问句回执「市净率(PB,最新)」PB=3.3605、observed 20261030 → 合格 MRQ。
ZHONGZHENG_ENV = json.loads((FIXTURES / "ifind_current_info_600176_zhongzheng.json").read_text())
DIRECTED_PB_ENV = json.loads((FIXTURES / "ifind_current_info_600176_pb.json").read_text())
FINANCIAL_ENV = json.loads((FIXTURES / "ifind_financials_688183_probe.json").read_text())
DATED_INFO_ENV = json.loads((FIXTURES / "ifind_current_info_688183_dated.json").read_text())
FLOW_ENV = json.loads((FIXTURES / "ifind_flow_688183_probe.json").read_text())

_NOW = datetime(2026, 11, 2, 12, 59, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def offline_env(choice_transport, monkeypatch: pytest.MonkeyPatch) -> Dict[str, list]:
    """choice profile + iFinD 主源；iFinD 按 tool 路由真实回执，Choice 原生计数。

    Choice 默认回 10001009（真实线上故障），但计数器让测试断言「零出站」而不只
    断言「失败」——登录尝试本身也是出站。
    """
    choice_transport.enable_ifind_mcp = True
    choice_transport.ifind_mcp_endpoint = "https://fixture.invalid/mcp"
    choice_transport.ifind_mcp_token = "test-only"
    choice_transport.financial_primary_source = "ifind"
    payloads: Dict[str, Any] = {
        "get_stock_financials": FINANCIAL_ENV,
        "get_stock_info": DATED_INFO_ENV,
        "get_stock_performance": FLOW_ENV,
    }
    from src.core import trading_calendar as _tc
    _sessions = [date(2026, 10, 30), date(2026, 10, 29), date(2026, 10, 28), date(2026, 10, 27),
                 date(2026, 10, 26), date(2026, 10, 23), date(2026, 10, 22), date(2026, 10, 21),
                 date(2026, 10, 20), date(2026, 10, 19), date(2026, 10, 16)]
    monkeypatch.setattr(_tc, "recent_completed_trading_sessions",
                        lambda market, count, current_time=None, **_: _sessions[:max(0, min(count, len(_sessions)))])
    calls: Dict[str, list] = {"ifind": [], "choice": []}

    async def query(self, tool, question, *, deadline=None):
        calls["ifind"].append((tool, question))
        return deepcopy(payloads[tool])

    monkeypatch.setattr(IfindFetcher, "_async_query", query, raising=False)
    monkeypatch.setattr(
        IfindFetcher, "get_instance",
        classmethod(lambda cls: IfindFetcher("https://fixture.invalid/mcp", "test-only")),
    )

    def native(method, args, options, deadline):
        calls["choice"].append((method, args[1] if len(args) > 1 else args[0]))
        return {
            "error": "authentication_failed", "native_code": 10001009,
            "error_text": "login count up to limit", "stage": "login",
            "codes": [], "indicators": [], "dates": [], "data": {},
        }

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(native))
    return calls


# ---------------------------------------------------------------- adapter：PB 精确请求

def _run_current_info(env: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, *, sessions: Any = None) -> tuple[Dict[str, Any], str]:
    from src.core import trading_calendar as _tc
    _sessions = sessions if sessions is not None else [date(2026, 10, 30), date(2026, 10, 29)]
    monkeypatch.setattr(_tc, "recent_completed_trading_sessions",
                        lambda market, count, current_time=None, **_: _sessions[:max(0, min(count, len(_sessions)))])
    captured: Dict[str, str] = {}

    async def query(self, tool, question, *, deadline=None):
        captured["question"] = question
        return deepcopy(env)

    monkeypatch.setattr(IfindFetcher, "_async_query", query, raising=False)
    fetcher = IfindFetcher("https://fixture.invalid/mcp", "test-only")
    out = fetcher.current_info_snapshot("600176", current_time=_NOW)
    return out, captured["question"]


def test_pb_question_requests_exact_indicator_not_ambiguous(offline_env, monkeypatch) -> None:
    """聚合当前资料批次必须按真实验证过的写法精确请求 PB，不给供应商模糊映射空间。"""
    _, question = _run_current_info(ZHONGZHENG_ENV, monkeypatch)
    assert "市净率(PB,最新)" in question
    assert "基准日=报表截止日期" in question
    assert "单位为倍" in question
    assert "不使用市净率(中证发布)" in question
    assert "不将百分比当倍数" in question


def test_zhongzheng_pb_variant_is_not_adopted_as_mrq(offline_env, monkeypatch) -> None:
    """真实聚合回执（中证/% 变体）不得被别名成合格 MRQ PB——生产解析原样拒绝。"""
    out, _ = _run_current_info(ZHONGZHENG_ENV, monkeypatch)
    pb = (out["valuation"] or {}).get("pb_ratio")
    assert pb is None, "市净率(中证发布)（单位：%）不是合格的最新 PB 读数"
    pe = out["valuation"].get("pe_ratio")
    assert pe is not None and pe.value == pytest.approx(18.2758)


def test_directed_pb_reply_adopted_with_full_qualification(offline_env, monkeypatch) -> None:
    """真实定向回执经生产解析：PB 3.3605、MRQ、multiple、实际日期 20261030。"""
    out, _ = _run_current_info(DIRECTED_PB_ENV, monkeypatch)
    pb = out["valuation"]["pb_ratio"]
    assert pb.value == pytest.approx(3.3605)
    assert pb.caliber == "MRQ" and pb.unit == "multiple"
    assert pb.observed_at == "20261030" and pb.raw_label == "市净率(PB,最新)"
    assert reading_input_reasons(pb, "pb_ratio") == ()
    # 观测日期落在严格交易日历窗口内，无 time_unknown/stale/future 标注
    assert pb.normalization_error is None


def test_pb_without_report_date_basis_stays_unknown_caliber(offline_env, monkeypatch) -> None:
    """表头「最新」但列参数缺基准日=报表截止日期：不得认定 MRQ（root 批准的 P2 范围）。"""
    env = deepcopy(DIRECTED_PB_ENV)
    env["data"]["indicators_params"]["市净率(PB,最新)"] = {"交易日期": "最新"}
    out, _ = _run_current_info(env, monkeypatch)
    pb = (out["valuation"] or {}).get("pb_ratio")
    assert pb is not None and pb.caliber is None
    assert "caliber_unknown" in reading_input_reasons(pb, "pb_ratio")


def test_pb_future_observation_rejected_not_backfilled(offline_env, monkeypatch) -> None:
    """观测日期晚于严格日历截至窗口 → time_in_future，实际日期保留、资格拒绝。"""
    env = deepcopy(DIRECTED_PB_ENV)
    env["data"]["answer"] = env["data"]["answer"].replace("20261030", "20261103")
    out, _ = _run_current_info(env, monkeypatch)
    pb = (out["valuation"] or {}).get("pb_ratio")
    assert pb is not None and pb.observed_at == "20261103"
    assert pb.normalization_error == "time_in_future"
    assert reading_input_reasons(pb, "pb_ratio") != []


def test_pb_wrong_security_row_rejected(offline_env, monkeypatch) -> None:
    """回执行归属其他证券 → 拒绝，不采纳跨证券读数。

    该批次只剩无证券匹配行：估值与行业全空时按既有整体 invalid_response 契约
    拒绝整批（不采纳任何跨证券读数）。"""
    from data_provider.cross_source_validator import SourceReadError

    env = deepcopy(DIRECTED_PB_ENV)
    env["data"]["answer"] = env["data"]["answer"].replace("600176.SH|中国巨石", "000001.SZ|平安银行")
    with pytest.raises(SourceReadError, match="invalid_response"):
        _run_current_info(env, monkeypatch)


# ---------------------------------------------------------------- manager：深研 scope 零 Choice

def test_deep_scope_prefer_ifind_has_zero_choice_outbound(offline_env) -> None:
    """iFinD 主源深研身份（深研 run + include_sector_flows=False）零 Choice 出站。

    估值/六字段/行业/可选主营/资金全部由既有 iFinD 路径落位；research 块按真实
    来源标注 ifind 且无 Choice 目标价键；errors 无 choice: 前缀。"""
    from src.core.research_run import ResearchRun

    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    assert offline_env["choice"] == [], f"深研 scope 不得有任何 Choice 原生出站: {offline_env['choice']}"
    valuation = result["valuation"]["data"]
    assert valuation["pe_ratio"] == pytest.approx(24.7573)
    assert valuation["pb_ratio"] == pytest.approx(5.7123)
    assert valuation["total_mv"] == pytest.approx(56200860000.0)
    growth = result["growth"]["data"]
    assert growth["roe"] == pytest.approx(29.34)
    assert growth["revenue_yoy"] is not None
    research = result["research"]
    assert research.get("source") == "ifind"
    assert "targets" not in research, "深研 scope 不发起目标价出站，也不得伪造目标价键"
    assert not [e for e in result["errors"] if e.startswith("choice:")], result["errors"]
    stock_flow = result["capital_flow"]["data"]["stock_flow"]
    assert stock_flow["source"] == "ifind" and stock_flow["main_net_inflow"] is not None


def test_deep_scope_ifind_capital_failure_does_not_fall_back_to_choice(offline_env, monkeypatch) -> None:
    """iFinD 资金失败：深研 scope 如实披露失败，不登录 Choice 补窗。"""
    from data_provider.cross_source_validator import SourceReadError

    async def failing_flow(self, tool, question, *, deadline=None):
        offline_env["ifind"].append((tool, question))
        if tool == "get_stock_performance":
            raise SourceReadError("ifind", "timeout")
        return deepcopy(FINANCIAL_ENV if tool == "get_stock_financials" else DATED_INFO_ENV)

    monkeypatch.setattr(IfindFetcher, "_async_query", failing_flow, raising=False)
    flow = get_main_inflow_cumulative("688183", deadline=None, current_time=_NOW, allow_choice_fallback=False)
    assert flow["status"] == "failed"
    assert flow["source"] == "ifind"
    assert any("timeout" in error for error in flow["errors"]), flow["errors"]
    assert offline_env["choice"] == [], "资金失败不得触发 Choice 登录/日历出站"


def test_deep_scope_capital_context_failure_displays_without_choice(offline_env, monkeypatch) -> None:
    """经 get_capital_flow_context 的深研资金失败路径同样零 Choice 出站。"""
    from data_provider.cross_source_validator import SourceReadError
    from src.core.research_run import ResearchRun

    async def failing_flow(self, tool, question, *, deadline=None):
        offline_env["ifind"].append((tool, question))
        if tool == "get_stock_performance":
            raise SourceReadError("ifind", "timeout")
        return deepcopy(FINANCIAL_ENV if tool == "get_stock_financials" else DATED_INFO_ENV)

    monkeypatch.setattr(IfindFetcher, "_async_query", failing_flow, raising=False)
    with ResearchRun(120).bind():
        block = DataFetcherManager().get_capital_flow_context("688183", budget_seconds=30, include_sector_flows=False)
    assert block["status"] in ("failed", "partial")
    assert not [e for e in block.get("errors") or [] if e.startswith("choice:")], block["errors"]
    assert offline_env["choice"] == []
    assert offline_env["choice"] == []


def test_choice_primary_deep_scope_keeps_choice_snapshot(choice_transport, monkeypatch) -> None:
    """Choice 主源（financial_primary_source=choice）深研 scope 行为保持原状。"""
    manager = DataFetcherManager()
    result = manager.get_fundamental_context("300260", budget_seconds=20, include_sector_flows=False)
    # Choice snapshot（header/六字段）照旧执行：growth 主值来自 Choice 真实回执
    assert result["growth"]["data"]["roe"] == 0.0
    research = result["research"]
    assert research.get("source") == "choice"


def test_plain_consumer_with_ifind_primary_keeps_choice_snapshot(offline_env) -> None:
    """普通消费者（include_sector_flows=True）保持既有 Choice 补充/目标价契约。"""
    result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60)
    assert len(offline_env["choice"]) > 0, "非深研 scope 的既有 Choice 出站不得被收敛误伤"
    assert result["research"].get("source") == "choice"
    assert any(e.startswith("choice:") for e in result["errors"]), "10001009 故障照旧如实记录"


def test_cancel_during_deep_scope_keeps_facts_zero_choice(offline_env, monkeypatch) -> None:
    """深研 scope 取消（可选主营批次）：已取得事实照常落位，且全程零 Choice 出站。"""
    from src.core.research_run import ResearchRun, ResearchStopped

    run = ResearchRun(120)

    def stopping_products(self, code, period=None, *, deadline=None):
        run.stop("cancelled")
        raise ResearchStopped(run.reason_text)

    monkeypatch.setattr(IfindFetcher, "product_segments", stopping_products)
    with run.bind():
        result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    assert result["valuation"]["data"]["pe_ratio"] == pytest.approx(24.7573)
    assert result["growth"]["data"]["roe"] == pytest.approx(29.34)
    stock_flow = result["capital_flow"]["data"]["stock_flow"]
    assert stock_flow["source"] == "ifind"
    assert offline_env["choice"] == []
    flow_calls = [tool for tool, _ in offline_env["ifind"] if tool == "get_stock_performance"]
    assert flow_calls == ["get_stock_performance"], "停止后不得重新出站补取资金"


def test_deep_scope_repeated_and_cached_calls_issue_zero_choice(offline_env, monkeypatch) -> None:
    """同报告重复聚合/工具补查命中 manager 缓存：不新增任何 Choice 出站。"""
    from src.core.research_run import ResearchRun

    manager = DataFetcherManager()
    with ResearchRun(120).bind():
        first = manager.get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
        assert offline_env["choice"] == []
        second = manager.get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    assert second["valuation"]["data"]["pe_ratio"] == first["valuation"]["data"]["pe_ratio"]
    assert offline_env["choice"] == []


# ---------------------------------------------------------------- fundamental_dim：投影与缺口

def _fund(fund: Dict[str, Any]):
    from src.deep_research_dims.context import SharedContext
    ctx = SharedContext(stock_code="600176", stock_name="中国巨石", as_of="2026-11-02T12:59:00")
    ctx.fundamental = fund
    return ctx


def _qualified_meta(field: str, value: float, *, source: str = "ifind") -> Dict[str, Any]:
    period = "2026-09-30"
    calibers = {"roe": "weighted_roe", "gross_margin": "gross_margin", "revenue_yoy": "operating_revenue_yoy",
                "net_profit_yoy": "parent_net_profit_yoy", "revenue": "operating_revenue", "net_profit": "parent_net_profit"}
    base = {"value": value, "source": source, "period": period, "period_basis": "YTD",
            "unit": "percentage_point", "statement_scope": "consolidated", "adjustment": "restated",
            "caliber": calibers.get(field),
            "selection_reason": "primary", "input_reasons": (), "rule_eligible": True,
            "quality": {"status": "single_source", "reason_codes": ("single_family",)}}
    if field in ("pe_ratio", "pb_ratio"):
        base.update(period=None, period_basis="unknown", unit="multiple", caliber="TTM" if field == "pe_ratio" else "MRQ",
                    observed_at="20261030", statement_scope=None, adjustment=None)
    return base


def test_financial_snapshot_projects_without_cross_validation() -> None:
    """无 cross_validation 块（深研零 Choice 核验）时六项已采纳事实照常投影。

    单源资格如实保留（不伪装 verified），来源/期间/单位元数据随投影走。"""
    from src.deep_research_dims.fundamental_dim import build_fundamental_dim

    fund: Dict[str, Any] = {
        "pe_ttm": 18.2758, "pb": None, "roe": 16.68, "gross_margin": 21.78,
        "revenue_growth": -2.31, "net_profit_yoy": -14.4,
        "field_meta": {field: _qualified_meta(field, value) for field, value in (
            ("pe_ratio", 18.2758), ("roe", 16.68), ("gross_margin", 21.78),
            ("revenue_yoy", -2.31), ("net_profit_yoy", -14.4))},
        "errors": [],
    }
    dim = build_fundamental_dim(_fund(fund))
    snapshot = dim.financial_snapshot
    assert set(snapshot) == {"revenue", "net_profit", "roe", "gross_margin", "revenue_yoy", "net_profit_yoy"}
    roe = snapshot["roe"]
    assert roe.reading is not None and roe.reading.value == pytest.approx(16.68)
    assert roe.reading.source == "ifind"
    assert roe.rule_eligible is True
    assert roe.quality.status != "verified", "单源事实不得伪装成已核验"
    # 无核验块时拒绝证据不凭空出现
    assert snapshot["revenue"].reading is None
    # 保存载荷同链路可序列化（API/历史消费契约）
    payload = json.dumps(dim.model_dump(), ensure_ascii=False)
    assert '"reading"' in payload and "ifind" in payload


def test_data_gaps_name_missing_pb_field_not_zero() -> None:
    """PE 有效而 PB 缺失：data_gaps 必须列明市净率字段缺口（不再 0 项假象）。

    完整门槛不降低（complete 仍要求六字段 rule_eligible），PE 估值分照常参与。"""
    from src.deep_research_dims.fundamental_dim import build_fundamental_dim

    fund: Dict[str, Any] = {
        "pe_ttm": 18.2758, "pb": None, "roe": 16.68, "gross_margin": 21.78,
        "revenue_growth": -2.31, "net_profit_yoy": -14.4,
        "field_meta": {field: _qualified_meta(field, value) for field, value in (
            ("pe_ratio", 18.2758), ("roe", 16.68), ("gross_margin", 21.78),
            ("revenue_yoy", -2.31), ("net_profit_yoy", -14.4))},
        "errors": [],
    }
    dim = build_fundamental_dim(_fund(fund))
    assert dim.status == "degraded"
    assert "市净率PB" in dim.data_gaps, dim.data_gaps
    assert "估值" not in dim.data_gaps, "PE 有效时估值组件有分，不构成组件级缺口"
    assert dim.data_quality.rule_usable_count == 5
    assert dim.health_score is not None and len(dim.data_quality.warnings) >= 1


def test_nonvaluation_financial_gate_predicate() -> None:
    """F1 整维 gate 豁免谓词（reader 澄清口径）的成对反例。"""
    from src.deep_research_dims.fundamental_dim import build_fundamental_dim, nonvaluation_financial_inputs_ok

    def _fund_with(pe: Any, pb: Any, roe: Any = 16.68) -> Dict[str, Any]:
        meta: Dict[str, Any] = {
            "gross_margin": _qualified_meta("gross_margin", 21.78),
            "revenue_yoy": _qualified_meta("revenue_yoy", -2.31),
            "net_profit_yoy": _qualified_meta("net_profit_yoy", -14.4),
        }
        if pe is not None:
            meta["pe_ratio"] = _qualified_meta("pe_ratio", pe)
        if pb is not None:
            meta["pb_ratio"] = _qualified_meta("pb_ratio", pb)
        if roe is not None:
            meta["roe"] = _qualified_meta("roe", roe)
        return {
            "pe_ttm": pe, "pb": pb, "roe": roe, "gross_margin": 21.78,
            "revenue_growth": -2.31, "net_profit_yoy": -14.4,
            "field_meta": meta, "errors": [],
            "balance_sheet": {"assets": 100.0, "liabilities": 40.0, "period": "2026-09-30",
                              "source": "ifind", "currency": "CNY", "unit": "currency_base",
                              "statement_scope": "consolidated", "adjustment": "restated"},
        }

    # F1 ok → 直接放行
    complete = build_fundamental_dim(_fund(_fund_with(18.2758, 3.3605)))
    assert complete.status == "ok" and nonvaluation_financial_inputs_ok(complete) is True
    # 仅缺 PB（四核心齐、资产负债齐）→ 豁免成立
    degraded_pb = build_fundamental_dim(_fund(_fund_with(18.2758, None)))
    assert degraded_pb.status == "degraded" and nonvaluation_financial_inputs_ok(degraded_pb) is True
    # 核心字段缺失（ROE 缺）→ 不放行
    missing_roe = build_fundamental_dim(_fund(_fund_with(18.2758, 3.3605, roe=None)))
    assert missing_roe.status == "degraded" and nonvaluation_financial_inputs_ok(missing_roe) is False
    # 资产负债结构缺口（非估值缺口）→ 不放行
    no_balance = _fund_with(18.2758, None)
    no_balance.pop("balance_sheet")
    degraded_balance = build_fundamental_dim(_fund(no_balance))
    assert degraded_balance.status == "degraded"
    assert "资产负债结构" in degraded_balance.data_gaps
    assert nonvaluation_financial_inputs_ok(degraded_balance) is False


# ---------------------------------------------------------------- scenarios：iFinD 未查询分支

def _ifind_research() -> Dict[str, Any]:
    return {"source": "ifind", "errors": [], "product_segments": {"items": [{"name": "粗纱"}]}}


def _ctx_for(research: Dict[str, Any]):
    from src.deep_research_dims.context import SharedContext
    ctx = SharedContext(stock_code="600176", stock_name="中国巨石", as_of="2026-11-02T12:59:00")
    ctx.quote = {"price": 12.5}
    ctx.fundamental = {"pe_ttm": 18.2758, "pb": 3.3605, "research": research}
    return ctx


def _pack() -> Dict[str, Any]:
    return {
        "industry_space": "盘面根因：粗纱提价落地；机制：供需收紧；量级：正面利好",
        "competitive_evolution": "公司成本曲线位于行业左侧，份额稳定。",
        "catalysts": ["年报披露（2027-03）"],
        "risks": ["海外需求波动", "天然气成本上行"],
    }


def test_scenarios_nonvaluation_ok_when_ifind_target_unqueried() -> None:
    """iFinD 主源深研未查询目标价：充分定性证据 → 情景 ok、无目标价/EV/估值口径。"""
    from src.deep_research_dims.scenarios_dim import build_scenarios_dim

    dim = build_scenarios_dim(_ctx_for(_ifind_research()), 12.5, _pack(), None)
    assert dim.status == "ok", dim.degraded_reason
    assert dim.valuation_basis == "none"
    assert dim.target_price is None and dim.expected_value is None and dim.analyst_target is None
    assert "目标价" in dim.narrative and "iFinD" in dim.narrative
    dumped = dim.model_dump()
    assert dumped["target_price"] is None and dumped["analyst_target"] is None


def test_scenarios_degraded_when_unqueried_without_content() -> None:
    """未查询但证据空壳：仍真实降级，不伪装完成。"""
    from src.deep_research_dims.scenarios_dim import build_scenarios_dim

    dim = build_scenarios_dim(_ctx_for(_ifind_research()), 12.5, {}, None)
    assert dim.status == "degraded"
    assert dim.valuation_basis == "none" and dim.target_price is None


def test_scenarios_unknown_research_shape_stays_degraded() -> None:
    """research 块形态未知（无 source）：保守降级，不得进入非估值分支。"""
    from src.deep_research_dims.scenarios_dim import build_scenarios_dim

    dim = build_scenarios_dim(_ctx_for({"errors": []}), 12.5, _pack(), None)
    assert dim.status == "degraded"


# ---------------------------------------------------------------- orchestrator：证据包豁免

def _core_dims(fundamental_ok: bool = True, capital_ok: bool = True, *, degraded_valuation_only: bool = False):
    from data_provider.cross_source_validator import AnchorReading
    from src.schemas.deep_research_dims import CapitalDim, FinancialDataQuality, FinancialFieldQuality, FundamentalDim

    def _core_field(value: float, caliber: str) -> FinancialFieldQuality:
        reading = AnchorReading("ifind", value, period="2026-09-30", period_basis="YTD", unit="percentage_point",
                                statement_scope="consolidated", adjustment="restated", caliber=caliber)
        return FinancialFieldQuality(reading=reading, quality={"status": "single_source", "reason_codes": ("single_family",)},
                                     rule_eligible=True, selection_reason="primary")

    if fundamental_ok:
        fundamental = FundamentalDim(status="ok")
    elif degraded_valuation_only:
        # 四个非估值评分输入真实合格（有 reading、rule_eligible），仅 PB 缺失——
        # 与豁免谓词语义一致的 mock 维度。
        calibers = {"roe": "weighted_roe", "gross_margin": "gross_margin", "revenue_yoy": "operating_revenue_yoy", "net_profit_yoy": "parent_net_profit_yoy"}
        fields = {field: _core_field(value, calibers[field]) for field, value in
                  (("roe", 16.68), ("gross_margin", 21.78), ("revenue_yoy", -2.31), ("net_profit_yoy", -14.4))}
        fields["pb_ratio"] = FinancialFieldQuality(reading=None, quality={"status": "missing", "reason_codes": ("no_eligible_current_value",)})
        fundamental = FundamentalDim(
            status="degraded",
            data_quality=FinancialDataQuality(state="partial", obtained_count=5, rule_usable_count=5, health_components=4, fields=fields),
            data_gaps=["市净率PB"],
            financial_safety={"leverage": 40.0, "period": "2026-09-30", "source": "ifind"},
        )
    else:
        fields = {field: FinancialFieldQuality(reading=None, quality={"status": "missing", "reason_codes": ("missing",)}) for field in ("roe", "gross_margin")}
        fundamental = FundamentalDim(
            status="degraded",
            data_quality=FinancialDataQuality(state="partial", obtained_count=3, rule_usable_count=3, health_components=3, fields=fields),
            data_gaps=["ROE", "毛利率"],
        )
    capital = CapitalDim(status="ok", flow_summary="资金观察。") if capital_ok else CapitalDim(status="degraded", degraded_reason="资金窗口不完整")
    return fundamental, capital


def _intel_business() -> Dict[str, Any]:
    from src.schemas.deep_research_dims import BusinessDim, IntelDim
    return {
        "intel": IntelDim(status="ok", root_cause={"event": "粗纱提价", "mechanism": "供需收紧", "magnitude": "正面利好", "persistence": "中期"}),
        "business": BusinessDim(status="ok", narrative="公司为玻纤龙头，成本曲线位于行业左侧。"),
    }


@pytest.fixture
def ifind_deep_policy(monkeypatch: pytest.MonkeyPatch):
    """获准的 iFinD 主源深研策略（豁免唯一适用的 policy 边界）。"""
    from src.config import Config
    monkeypatch.setattr(Config, "_instance", Config(data_source_profile="choice", enable_ifind_mcp=True, financial_primary_source="ifind",
                                                    ifind_mcp_endpoint="https://fixture.invalid/mcp", ifind_mcp_token="test-only",
                                                    deep_research_cross_validate=False))


def test_evidence_pack_survives_valuation_only_f1_gap(ifind_deep_policy) -> None:
    """iFinD 主源深研策略：F1 degraded 但缺口仅估值、资金完成且研究实质完成 → 证据包照常装配。"""
    from src.agent.deep_research.orchestrator import _qualitative_evidence_pack
    fundamental, capital = _core_dims(fundamental_ok=False, degraded_valuation_only=True)
    pack = _qualitative_evidence_pack({"fundamental": fundamental, "capital": capital, **_intel_business()})
    assert pack.get("industry_space") and pack.get("competitive_evolution")


def test_evidence_pack_choice_primary_keeps_baseline_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """root 反例成对回归：Choice 主源（ifind 关闭）同输入维持基线整维 gate——
    豁免不因「不想查行业资金」而激活，scenarios 保持真实降级。"""
    from src.config import Config
    from src.agent.deep_research.orchestrator import _qualitative_evidence_pack
    from src.deep_research_dims.scenarios_dim import build_scenarios_dim
    monkeypatch.setattr(Config, "_instance", Config(data_source_profile="choice", enable_ifind_mcp=False, financial_primary_source="choice", deep_research_cross_validate=False))
    fundamental, capital = _core_dims(fundamental_ok=False, degraded_valuation_only=True)
    # Choice 主源反例的字段来源是 choice（同 root repro 语义）
    from data_provider.cross_source_validator import AnchorReading
    for field, evidence in fundamental.data_quality.fields.items():
        if evidence.reading is not None:
            object.__setattr__(evidence.reading, "source", "choice")
    assert fundamental.data_quality.fields["roe"].reading.source == "choice"
    pack = _qualitative_evidence_pack({"fundamental": fundamental, "capital": capital, **_intel_business()})
    assert pack == {}, "Choice 主源不适用豁免：证据包保持空"
    ctx = _ctx_for({"source": "choice", "as_of": "2026-11-02", "target_window_days": 180, "targets": {}, "errors": []})
    dim = build_scenarios_dim(ctx, 12.5, pack, None)
    assert dim.status == "degraded", "基线整维 gate 下情景保持降级"


def test_evidence_pack_blocked_by_core_financial_gap(ifind_deep_policy) -> None:
    """核心财务字段缺失（ROE/毛利率缺）：豁免不成立，证据包为空。"""
    from src.agent.deep_research.orchestrator import _qualitative_evidence_pack
    fundamental, capital = _core_dims(fundamental_ok=False)
    assert _qualitative_evidence_pack({"fundamental": fundamental, "capital": capital, **_intel_business()}) == {}


def test_evidence_pack_blocked_by_non_ifind_core_fields(ifind_deep_policy) -> None:
    """核心字段非 iFinD 来源（Choice 主值）：豁免不成立（root 反例的来源边界）。"""
    from src.agent.deep_research.orchestrator import _qualitative_evidence_pack
    fundamental, capital = _core_dims(fundamental_ok=False, degraded_valuation_only=True)
    for evidence in fundamental.data_quality.fields.values():
        if evidence.reading is not None:
            object.__setattr__(evidence.reading, "source", "choice")
    assert _qualitative_evidence_pack({"fundamental": fundamental, "capital": capital, **_intel_business()}) == {}


def test_evidence_pack_blocked_by_capital_not_ok(ifind_deep_policy) -> None:
    """资金未完成：核心 gate 不变，证据包为空（估值豁免不外溢到资金）。"""
    from src.agent.deep_research.orchestrator import _qualitative_evidence_pack
    fundamental, capital = _core_dims(fundamental_ok=False, capital_ok=False, degraded_valuation_only=True)
    assert _qualitative_evidence_pack({"fundamental": fundamental, "capital": capital, **_intel_business()}) == {}


def test_evidence_pack_blocked_by_empty_intel_shell(ifind_deep_policy) -> None:
    """intel 降级空壳：既有实质内容谓词不变，证据包为空。"""
    from src.agent.deep_research.orchestrator import _qualitative_evidence_pack
    from src.schemas.deep_research_dims import IntelDim
    fundamental, capital = _core_dims(fundamental_ok=False, degraded_valuation_only=True)
    pack = _qualitative_evidence_pack({
        "fundamental": fundamental, "capital": capital,
        "intel": IntelDim(status="degraded", root_cause={"event": "旧根因", "mechanism": "旧机制", "magnitude": "正面利好", "persistence": "中期"}),
    })
    assert pack == {}


# ---------------------------------------------------------------- 纵向：情景→结论→信号

def test_chain_nonvaluation_completion_from_valuation_only_gap() -> None:
    """仅估值缺口的 F1 + 充分研究 → 情景/结论/信号完成，全程无估值驱动输出。"""
    from src.deep_research_dims.context import SharedContext
    from src.deep_research_dims.scenarios_dim import build_scenarios_dim
    from src.deep_research_dims.conclusion_dim import build_conclusion_dim
    from src.deep_research_dims.signal_dim import build_signal_dim

    ctx = SharedContext(stock_code="600176", stock_name="中国巨石", as_of="2026-11-02T12:59:00")
    ctx.quote = {"price": 12.5}
    ctx.fundamental = {"pe_ttm": 18.2758, "pb": None, "research": _ifind_research()}
    scenarios = build_scenarios_dim(ctx, 12.5, _pack(), None)
    assert scenarios.status == "ok"
    dimensions = [{"dimension": name, "score": 55.0, "indicators": [], "data_gaps": 0}
                  for name in ("基本面", "情绪面", "资金面", "消息面", "技术面", "宏观")]
    conclusion = build_conclusion_dim(
        ctx,
        {"framework": {"dimension_total": 55.0, "dimensions": dimensions}},
        {"bayesian": {"prior_p": 0.5, "posterior_p": 0.55, "edge": 0.05}},
        scenarios.model_dump(),
    )
    assert conclusion.status == "ok", conclusion.degraded_reason
    assert conclusion.conclusion.action == "观察"
    assert conclusion.conclusion.value_range_1y is None and conclusion.conclusion.value_range_3y is None
    signal = build_signal_dim(ctx, conclusion.model_dump(), scenarios.model_dump())
    assert signal.status == "ok", signal.degraded_reason
    assert signal.signal_type == "非估值观察"
    assert signal.rating == "中性" and signal.confidence == "低"
    assert signal.target_price is None and signal.expected_value is None
    # 保存/API 载荷一致：非估值链路无目标价、无估值驱动买卖评级
    blob = json.dumps({"scenarios": scenarios.model_dump(), "conclusion": conclusion.model_dump(), "signal": signal.model_dump()}, ensure_ascii=False)
    assert '"target_price": null' in blob and '"analyst_target": null' in blob
    for banned in ("买入", "增持"):
        assert blob.count(banned) == 0, f"非估值输出不得出现{banned}评级"


# ---------------------------------------------------------------- adapter：公司资料与定向 PB（真实回执）

COMPANY_ENV = json.loads((FIXTURES / "ifind_company_600176.json").read_text())


def _run_directed_pb(env: Dict[str, Any], monkeypatch: pytest.MonkeyPatch):
    from src.core import trading_calendar as _tc
    monkeypatch.setattr(_tc, "recent_completed_trading_sessions",
                        lambda market, count, current_time=None, **_: [date(2026, 10, 30), date(2026, 10, 29)][:max(0, min(count, 2))])

    async def query(self, tool, question, *, deadline=None):
        return deepcopy(env)

    monkeypatch.setattr(IfindFetcher, "_async_query", query, raising=False)
    return IfindFetcher("https://fixture.invalid/mcp", "test-only").directed_pb_snapshot("600176", current_time=_NOW)


def test_directed_pb_snapshot_parses_real_reply_with_full_qualification(offline_env, monkeypatch) -> None:
    """定向补查走同一生产解析/资格：真实 pb 回执 → PB 合格 MRQ/倍/实际日期。"""
    out = _run_directed_pb(DIRECTED_PB_ENV, monkeypatch)
    pb = out["pb_ratio"]
    assert pb is not None and pb.value == pytest.approx(3.3605)
    assert pb.caliber == "MRQ" and pb.unit == "multiple" and pb.observed_at == "20261030"
    assert out["rejection"] == () and reading_input_reasons(pb, "pb_ratio") == ()
    assert out["requested_date"] == "2026-10-30"


def test_directed_pb_snapshot_rejects_zhongzheng_variant_with_reason(offline_env, monkeypatch) -> None:
    """定向回执仍含中证/% 变体 → 无读数 + wrong_indicator_variant（不补造）。"""
    out = _run_directed_pb(ZHONGZHENG_ENV, monkeypatch)
    assert out["pb_ratio"] is None
    assert out["rejection"] == ("wrong_indicator_variant",)


def test_company_profile_parses_real_reply_with_own_dates(offline_env, monkeypatch) -> None:
    """真实公司回执经生产解析：字段原文值 + 各列自身截止日期（缺日期保持 None）。"""
    from src.core import trading_calendar as _tc
    monkeypatch.setattr(_tc, "recent_completed_trading_sessions",
                        lambda market, count, current_time=None, **_: [date(2026, 11, 2)][:max(0, min(count, 1))])

    async def query(self, tool, question, *, deadline=None):
        assert "实际控制人" in question and "公司简介" in question, "公司资料用已验证的定向问句"
        return deepcopy(COMPANY_ENV)

    monkeypatch.setattr(IfindFetcher, "_async_query", query, raising=False)
    profile = IfindFetcher("https://fixture.invalid/mcp", "test-only").company_profile("600176")
    assert profile.source == "ifind"
    assert profile.facts["name"].value == "中国巨石股份有限公司"
    assert "玻璃纤维" in profile.facts["main_business"].value
    assert "电子布" in profile.facts["main_products"].value
    assert profile.facts["controller"].value == "国务院国有资产监督管理委员会"
    assert profile.facts["controller_type"].value == "国资委"
    assert profile.facts["holding_ratio"].value == "12.9574"
    # 供应商按列提供自身截止日期：控制人/主营/比例 20261102；公司简介/名称/产品无日期不补造
    assert profile.facts["controller"].observed_at == "20261102"
    assert profile.facts["main_business"].observed_at == "20261102"
    assert profile.facts["profile"].observed_at is None
    assert profile.facts["name"].observed_at is None
    assert profile.facts["name"].raw_column == "公司中文名称"


def test_company_profile_wrong_security_rejected(offline_env, monkeypatch) -> None:
    """回执行归属其他证券 → 整批 invalid_response（不采纳跨证券公司事实）。"""
    from data_provider.cross_source_validator import SourceReadError
    from src.core import trading_calendar as _tc
    monkeypatch.setattr(_tc, "recent_completed_trading_sessions",
                        lambda market, count, current_time=None, **_: [date(2026, 11, 2)][:max(0, min(count, 1))])

    env = deepcopy(COMPANY_ENV)
    env["data"]["answer"] = env["data"]["answer"].replace("600176.SH|中国巨石", "000001.SZ|平安银行")

    async def query(self, tool, question, *, deadline=None):
        return deepcopy(env)

    monkeypatch.setattr(IfindFetcher, "_async_query", query, raising=False)
    with pytest.raises(SourceReadError, match="invalid_response"):
        IfindFetcher("https://fixture.invalid/mcp", "test-only").company_profile("600176")


# ---------------------------------------------------------------- 聚合：公司资料/来源归因/PB 定向补查（深研身份）

def test_deep_scope_company_facts_name_and_dates_flow_to_research(offline_env, monkeypatch) -> None:
    """深研身份内公司资料由 iFinD 回执承载：research.company 含原文字段/
    自身日期/NAME，stock_name 取真实公司中文名称；Choice 公司补查不发生。

    回执证券对齐 688183 聚合 fixtures（仅替换单元格证券/简称值，列结构与
    日期元数据保持真实 company.json 形态；字段语义由 600176 真实回执的
    adapter 级测试锁定）。"""
    from src.core.research_run import ResearchRun
    from src.core import trading_calendar as _tc
    monkeypatch.setattr(_tc, "recent_completed_trading_sessions",
                        lambda market, count, current_time=None, **_: [date(2026, 11, 2), date(2026, 10, 30), date(2026, 10, 29), date(2026, 10, 28),
                                                                       date(2026, 10, 26), date(2026, 10, 23), date(2026, 10, 22), date(2026, 10, 21),
                                                                       date(2026, 10, 20), date(2026, 10, 19), date(2026, 10, 16)][:max(0, min(count, 11))])
    company_env = deepcopy(COMPANY_ENV)
    company_env["data"]["answer"] = company_env["data"]["answer"].replace("600176.SH|中国巨石", "688183.SH|生益电子")

    async def query(self, tool, question, *, deadline=None):
        offline_env["ifind"].append((tool, question))
        if "实际控制人" in question and "公司简介" in question:
            return deepcopy(company_env)
        return deepcopy(FINANCIAL_ENV if tool == "get_stock_financials" else DATED_INFO_ENV if tool == "get_stock_info" else FLOW_ENV)

    monkeypatch.setattr(IfindFetcher, "_async_query", query, raising=False)
    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    research = result["research"]
    company = research.get("company") or {}
    assert company.get("MAINBUSINESS") and company.get("AHOLDER"), company
    assert company.get("NAME") == "中国巨石股份有限公司"
    dates = company.get("field_dates") or {}
    assert dates.get("AHOLDER") and dates.get("COMPROFILE") is None, "控制人有自身日期，公司简介无日期不补造"
    assert result.get("stock_name") == "中国巨石股份有限公司"
    assert offline_env["choice"] == []
    company_calls = [q for _t, q in offline_env["ifind"] if "实际控制人" in q]
    assert len(company_calls) == 1, "公司资料恰一次有界出站"
    holder_queries = [q for _t, q in offline_env["ifind"] if "股东" in q]
    assert holder_queries == [], "深研 scope 不重引入股东查询"


def test_company_failure_keeps_reason_and_rest_of_report(offline_env, monkeypatch) -> None:
    """公司批次失败如实记录 ifind:company 原因，其余事实照常落位（不拖垮聚合）。"""
    from data_provider.cross_source_validator import SourceReadError
    from src.core.research_run import ResearchRun

    async def query(self, tool, question, *, deadline=None):
        offline_env["ifind"].append((tool, question))
        if "实际控制人" in question:
            raise SourceReadError("ifind", "timeout")
        return deepcopy(FINANCIAL_ENV if tool == "get_stock_financials" else DATED_INFO_ENV if tool == "get_stock_info" else FLOW_ENV)

    monkeypatch.setattr(IfindFetcher, "_async_query", query, raising=False)
    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    assert any("ifind:company:timeout" in e for e in result["errors"]), result["errors"]
    assert result["growth"]["data"]["roe"] == pytest.approx(29.34)
    assert result.get("stock_name") in (None, ""), "公司名称未取得时保持空（不补造）"
    assert offline_env["choice"] == []


def test_ifind_info_failure_attributes_fields_to_ifind_never_choice(offline_env, monkeypatch) -> None:
    """root 反例回归：当前资料批次超时且零 Choice → 来源链/字段证据标实际尝试源
    iFinD（含真实失败原因），stock_name 保持空，不把未调用的 Choice 填为来源。"""
    from data_provider.cross_source_validator import SourceReadError
    from src.core.research_run import ResearchRun

    async def failing_info(self, tool, question, *, deadline=None):
        offline_env["ifind"].append((tool, question))
        if tool == "get_stock_info":
            raise SourceReadError("ifind", "timeout")
        return deepcopy(FINANCIAL_ENV if tool == "get_stock_financials" else FLOW_ENV)

    monkeypatch.setattr(IfindFetcher, "_async_query", failing_info, raising=False)
    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    assert offline_env["choice"] == []
    providers = [item["provider"] for item in result["valuation"]["source_chain"]]
    assert providers == ["ifind"], result["valuation"]["source_chain"]
    meta = result["valuation"]["field_meta"]
    for field in ("pe_ratio", "pb_ratio", "total_mv"):
        errors = (meta.get(field) or {}).get("source_errors") or []
        assert any(item.get("source") == "ifind" and item.get("reason_code") == "timeout" for item in errors), (field, meta.get(field))
    assert any("ifind:current_info:timeout" in e for e in result["errors"])
    assert result.get("stock_name") in (None, "")


def test_pb_directed_supplement_runs_once_and_adopted(offline_env, monkeypatch) -> None:
    """聚合批次缺合格 PB（中证变体）→ 定向补查恰一次且合格值采纳（生产解析）。

    解析产物取自真实回执回放（只 mock 传输，不 mock 解析/资格层）；补查后
    同报告重复聚合经缓存不再发起。"""
    from data_provider.cross_source_validator import AnchorReading, AnchorQuality, adopted_field_record
    from src.core.research_run import ResearchRun
    from src.core import trading_calendar as _tc
    monkeypatch.setattr(_tc, "recent_completed_trading_sessions",
                        lambda market, count, current_time=None, **_: [date(2026, 10, 30), date(2026, 10, 29), date(2026, 10, 28), date(2026, 10, 27),
                                                                       date(2026, 10, 26), date(2026, 10, 23), date(2026, 10, 22), date(2026, 10, 21),
                                                                       date(2026, 10, 20), date(2026, 10, 19), date(2026, 10, 16)][:max(0, min(count, 11))])
    # 聚合批次缺合格 PB 的模拟：真实回执形态（中证/% 变体列 + PE/市值/行业齐）
    # 先经生产解析取得实际产物——解析/资格层不 mock，只控制传输内容。回执证券
    # 对齐 688183 聚合 fixtures（仅替换单元格证券/简称值，600176 真实语义由
    # adapter 级测试锁定）。
    zhongzheng_env = deepcopy(ZHONGZHENG_ENV)
    zhongzheng_env["data"]["answer"] = zhongzheng_env["data"]["answer"].replace("600176.SH|中国巨石", "688183.SH|生益电子")
    async def zhongzheng_query(self, tool, question, *, deadline=None):
        return deepcopy(zhongzheng_env)

    monkeypatch.setattr(IfindFetcher, "_async_query", zhongzheng_query, raising=False)
    with ResearchRun(120).bind():
        full = IfindFetcher("https://fixture.invalid/mcp", "test-only").current_info_snapshot("688183", current_time=_NOW)
    assert full["valuation"].get("pb_ratio") is None
    assert full["pb_rejection"] == ("wrong_indicator_variant",)
    pb_without_pb = full

    directed_calls = {"count": 0}
    # 定向回执证券对齐 688183 聚合 fixtures（仅替换单元格证券/简称值；列结构/
    # 基准日元数据保持真实 pb.json 形态，值一致性断言仍走生产解析）。
    directed_env = deepcopy(DIRECTED_PB_ENV)
    directed_env["data"]["answer"] = directed_env["data"]["answer"].replace("600176.SH|中国巨石", "688183.SH|生益电子")

    async def route(self, tool, question, *, deadline=None):
        offline_env["ifind"].append((tool, question))
        if "所属申万行业" in question:
            return deepcopy(zhongzheng_env)
        if "不使用市净率(中证发布)" in question:
            directed_calls["count"] += 1
            return deepcopy(directed_env)
        return deepcopy(FINANCIAL_ENV if tool == "get_stock_financials" else FLOW_ENV)

    monkeypatch.setattr(IfindFetcher, "_async_query", route, raising=False)
    manager = DataFetcherManager()
    # 「每报告至多一次」的边界是 ResearchRun（single_fetch 按报告共享已知补查
    # 结果，成功与失败同样共享；新报告允许重新补查，不构成跨报告重试框架）。
    with ResearchRun(120).bind():
        result = manager.get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
        assert directed_calls["count"] == 1, "定向补查每报告至多一次"
        again = manager.get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
        assert again["valuation"]["data"]["pb_ratio"] == pytest.approx(3.3605)
        assert directed_calls["count"] == 1, "同报告（run）内重复聚合共享补查结果，不重试"
    valuation = result["valuation"]["data"]
    assert valuation["pb_ratio"] == pytest.approx(3.3605)
    record = result["valuation"]["field_meta"].get("pb_ratio") or {}
    assert record.get("source") == "ifind" and record.get("rule_eligible") is True, record
    errors = record.get("source_errors") or []
    assert any(item.get("reason_code") == "wrong_indicator_variant" for item in errors), f"聚合批次的中证变体拒绝原因保留: {errors}"
    assert offline_env["choice"] == []


def test_pb_directed_supplement_failure_stays_missing_with_reason(offline_env, monkeypatch) -> None:
    """定向补查失败 → PB 保持缺失并保留 directed_pb 失败原因，不重试不伪造。"""
    from data_provider.cross_source_validator import SourceReadError
    from src.core.research_run import ResearchRun
    from src.core import trading_calendar as _tc
    monkeypatch.setattr(_tc, "recent_completed_trading_sessions",
                        lambda market, count, current_time=None, **_: [date(2026, 10, 30), date(2026, 10, 29), date(2026, 10, 28), date(2026, 10, 27),
                                                                       date(2026, 10, 26), date(2026, 10, 23), date(2026, 10, 22), date(2026, 10, 21),
                                                                       date(2026, 10, 20), date(2026, 10, 19), date(2026, 10, 16)][:max(0, min(count, 11))])
    zhongzheng_env = deepcopy(ZHONGZHENG_ENV)
    zhongzheng_env["data"]["answer"] = zhongzheng_env["data"]["answer"].replace("600176.SH|中国巨石", "688183.SH|生益电子")

    async def zhongzheng_query(self, tool, question, *, deadline=None):
        return deepcopy(zhongzheng_env)

    monkeypatch.setattr(IfindFetcher, "_async_query", zhongzheng_query, raising=False)
    with ResearchRun(120).bind():
        full = IfindFetcher("https://fixture.invalid/mcp", "test-only").current_info_snapshot("688183", current_time=_NOW)
    assert full["valuation"].get("pb_ratio") is None
    pb_without_pb = full

    directed_calls = {"count": 0}

    async def route(self, tool, question, *, deadline=None):
        offline_env["ifind"].append((tool, question))
        if "所属申万行业" in question:
            return deepcopy(zhongzheng_env)
        if "不使用市净率(中证发布)" in question:
            directed_calls["count"] += 1
            from data_provider.cross_source_validator import SourceReadError as _SRE
            raise _SRE("ifind", "timeout")
        return deepcopy(FINANCIAL_ENV if tool == "get_stock_financials" else FLOW_ENV)

    monkeypatch.setattr(IfindFetcher, "_async_query", route, raising=False)
    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    assert directed_calls["count"] == 1, "失败后不重试（同报告 single_fetch 共享已知失败）"
    valuation = result["valuation"]["data"]
    assert valuation["pb_ratio"] is None
    record = result["valuation"]["field_meta"].get("pb_ratio") or {}
    errors = record.get("source_errors") or []
    assert any(item.get("reason_code") == "directed_pb:timeout" for item in errors), (record, errors)
    assert offline_env["choice"] == []


def test_non_deep_false_scope_keeps_choice_supplement_and_side_effects(ifind_transport, monkeypatch) -> None:
    """root 成对回归：非深研 false-scope（无深研 run）+ iFinD 不可用 + Choice
    健康 → 保持基线完整聚合（Choice 六字段补充/目标价 research 块），且保留原
    false-scope 副作用（无行业资金榜、无 iFinD 股东隐式出站）。"""
    config, response, calls = ifind_transport
    config.financial_primary_source = "ifind"
    monkeypatch.setattr(IfindFetcher, "get_instance", classmethod(lambda cls: IfindFetcher("https://fixture.invalid/mcp", "test-only")))

    async def failing_ifind(self, tool, question, *, deadline=None):
        from data_provider.cross_source_validator import SourceReadError as _SRE
        calls.append(("ifind", tool))
        raise _SRE("ifind", "timeout")

    monkeypatch.setattr(IfindFetcher, "_async_query", failing_ifind, raising=False)
    holders_before = sum(1 for _src, tool in calls if tool == "get_stock_shareholders")
    result = DataFetcherManager().get_fundamental_context("300260", budget_seconds=30, include_sector_flows=False)
    growth = result["growth"]["data"]
    assert growth["revenue"] == 2997710000, "Choice 健康时六字段补充照常（基线行为）"
    research = result["research"]
    assert research.get("source") == "choice"
    assert "targets" in research, "非深研 false-scope 保留 Choice 目标价/公司补查"
    assert "sector_rankings" not in (result.get("capital_flow") or {}).get("data", {}), "原 false-scope 副作用保留：无行业资金榜"
    holders_after = sum(1 for _src, tool in calls if tool == "get_stock_shareholders")
    assert holders_after == holders_before, "不新增 iFinD 股东隐式出站（原副作用）"


# ---------------------------------------------------------------- 受控纵向：编排→保存→API/SSE→下载（真实装配链）

_VERTICAL_DIMS = sorted({
    "phase", "history", "data", "fundamental", "capital", "intel", "business",
    "sector", "six_dim", "bayesian", "scenarios", "conclusion", "plan", "signal",
})

_VERTICAL_IFIND_SESSIONS = [date(2026, 10, 30), date(2026, 10, 29), date(2026, 10, 28), date(2026, 10, 27),
                            date(2026, 10, 26), date(2026, 10, 23), date(2026, 10, 22), date(2026, 10, 21),
                            date(2026, 10, 20), date(2026, 10, 19), date(2026, 10, 16)]


def _vertical_env(choice_transport, monkeypatch: pytest.MonkeyPatch, tmp_path, *, roe_missing: bool = False, products_mode: str = "reject"):
    """受控纵向环境：iFinD 主源深研零 Choice + 确定性研究替身 + SQLite/目录隔离。

    仅外部传输受控（iFinD 回执为真实脱敏 fixtures、Choice 原生 fail-on-call）；
    阶段0/编排/资格/保存/API/SSE 全走真实代码路径。
    """
    from fastapi import FastAPI
    from src.config import Config
    from src.storage import DatabaseManager
    from src.services import deep_research_service as service_module
    from api.v1.endpoints.deep_research import router
    from src.core import trading_calendar as _tc

    choice_transport.deep_research_engine = "dual_track"
    choice_transport.deep_research_timeout_seconds = 90
    choice_transport.enable_ifind_mcp = True
    choice_transport.ifind_mcp_endpoint = "https://fixture.invalid/mcp"
    choice_transport.ifind_mcp_token = "test-only"
    choice_transport.financial_primary_source = "ifind"
    monkeypatch.setattr(Config, "_instance", choice_transport)
    monkeypatch.setattr(Config, "is_agent_available", lambda self: True)

    monkeypatch.setattr(_tc, "recent_completed_trading_sessions",
                        lambda market, count, current_time=None, **_: _VERTICAL_IFIND_SESSIONS[:max(0, min(count, len(_VERTICAL_IFIND_SESSIONS)))])

    financials = deepcopy(FINANCIAL_ENV)
    if roe_missing:
        # 负例：六字段批次真实回执去掉 ROE 列（真实值删除，非伪造读数）——
        # 核心财务字段缺失 → F1 豁免不成立 → 非估值分支不放行（服务级反例）。
        answer = financials["data"]["answer"]
        lines = answer.splitlines()
        financials["data"]["answer"] = "\n".join(
            line for line in lines if "净资产收益率ROE" not in line
        )

    financials_env = deepcopy(financials)
    company_env = deepcopy(COMPANY_ENV)
    company_env["data"]["answer"] = company_env["data"]["answer"].replace("600176.SH|中国巨石", "688183.SH|生益电子")
    choice_calls: list = []

    def counting_native(method, args, options, deadline):
        choice_calls.append((method, args[1] if len(args) > 1 else args[0]))
        raise AssertionError(f"深研报告不得有任何 Choice 原生出站: {method}")

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(counting_native))

    products_q3 = _adapt_600176_env_to_688183(PRODUCTS_Q3_EMPTY_ENV)
    products_h1 = _adapt_600176_env_to_688183(PRODUCTS_H1_ITEMS_ENV)
    products_initial_future = _adapt_600176_env_to_688183(PRODUCTS_H1_ITEMS_ENV)
    products_initial_future["data"]["answer"] = products_initial_future["data"]["answer"].replace("20260630", "20271231")
    for _params in products_initial_future["data"]["indicators_params"].values():
        if _params.get("报告期") == "20260630":
            _params["报告期"] = "20271231"
    products_h1_empty = _adapt_600176_env_to_688183(PRODUCTS_Q3_EMPTY_ENV)
    products_h1_empty["data"]["answer"] = products_h1_empty["data"]["answer"].replace("20260930", "20260630")
    for _params in products_h1_empty["data"]["indicators_params"].values():
        if _params.get("报告期") == "20260930":
            _params["报告期"] = "20260630"

    async def ifind_route(self, tool, question, *, deadline=None):
        if tool == "get_stock_financials" and "主营构成" in question:
            if products_mode == "reject":
                # 该 fixture 不含主营构成表：按真实语义整批拒绝并保留原因。
                from data_provider.cross_source_validator import SourceReadError as _SRE
                raise _SRE("ifind", "invalid_response")
            # v5 纵向：本期（Q3）真实空覆盖信封；候选（H1）按模式给成功/仍空。
            if "2026-06-30" in question:
                return deepcopy(products_h1 if products_mode == "backfill_success" else products_h1_empty)
            if products_mode == "initial_earlier":
                return deepcopy(products_h1)  # 初查直接回较早财期真实明细
            if products_mode == "initial_future":
                return deepcopy(products_initial_future)
            if products_mode not in {"backfill_success", "both_empty"}:
                raise AssertionError(f"未预期的产品模式: {products_mode}")
            return deepcopy(products_q3)
        if tool == "get_stock_financials":
            return deepcopy(financials_env)
        if "实际控制人" in question and "公司简介" in question:
            return deepcopy(company_env)
        if tool == "get_stock_info":
            return deepcopy(DATED_INFO_ENV)
        if tool == "get_stock_performance":
            return deepcopy(FLOW_ENV)
        raise AssertionError(f"未预期的 iFinD 出站: {tool} {question[:40]}")

    monkeypatch.setattr(IfindFetcher, "_async_query", ifind_route, raising=False)
    monkeypatch.setattr(
        IfindFetcher, "get_instance",
        classmethod(lambda cls: IfindFetcher("https://fixture.invalid/mcp", "test-only")),
    )
    from tests.test_issue64_no_target_mode import _DeterministicResearchAdapter

    monkeypatch.setattr(service_module, "_get_dual_track_adapter", lambda: _DeterministicResearchAdapter())
    monkeypatch.setattr(service_module, "_DEEP_RESEARCH_DIR", tmp_path)
    monkeypatch.setattr(DatabaseManager, "_instance", None)
    database = DatabaseManager(f"sqlite:///{tmp_path / 'reports.sqlite'}")
    monkeypatch.setattr(service_module, "get_db", lambda: database)

    from src.agent.deep_research import orchestrator
    from src.deep_research_dims.context import SharedContext, _safe_fundamental

    today = date.today().isoformat()

    def _build_context(*args: Any, **kwargs: Any) -> SharedContext:
        ctx = SharedContext("688183", "生益电子", f"{today}T15:00:00", quote={"price": 20.0})
        ctx.history = [
            {"date": f"2026-09-{day:02d}", "open": 10, "high": 11, "low": 9, "close": 10.0 + day * 0.05, "volume": 1000}
            for day in range(1, 29)
        ]
        ctx.fundamental = _safe_fundamental("688183", ctx)
        return ctx

    monkeypatch.setattr(orchestrator, "build_shared_context", _build_context)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")

    import pandas as pd
    from src.services import history_loader

    def _deterministic_history(stock_code, days=60, target_date=None, min_records=None):
        rows = 1200
        idx = pd.bdate_range(end=today, periods=rows)
        return pd.DataFrame({
            "date": idx.strftime("%Y-%m-%d"), "open": [10.0] * rows, "high": [11.0] * rows,
            "low": [9.0] * rows, "close": [10.0 + i * 0.01 for i in range(rows)], "volume": [1000] * rows,
        }), "fixture_db"

    monkeypatch.setattr(history_loader, "load_history_df", _deterministic_history)

    app = FastAPI()
    app.include_router(router)
    return service_module.deep_research_service, database, app, choice_calls


def _sse_events(response: Any) -> List[Dict[str, Any]]:
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def test_service_vertical_nonvaluation_success_zero_choice(choice_transport, monkeypatch, tmp_path) -> None:
    """受控纵向正例：iFinD 主源深研（零 Choice）非估值完成贯穿真实编排→SQLite
    保存→API detail/download→SSE 唯一 done；财务快照六事实随保存载荷保留。"""
    from fastapi.testclient import TestClient
    from tests.test_issue64_no_target_mode import _DeterministicResearchAdapter, _assert_single_done_terminal
    from src.storage import DatabaseManager

    service, database, app, choice_calls = _vertical_env(choice_transport, monkeypatch, tmp_path)
    try:
        with TestClient(app) as client:
            stream = client.post(
                "/generate/stream",
                json={"stock_code": "688183", "stock_name": "生益电子", "dims": _VERTICAL_DIMS, "force_refresh": True},
            )
            assert stream.status_code == 200, stream.text
            assert stream.headers["content-type"].startswith("text/event-stream")
            events = _sse_events(stream)
            done = _assert_single_done_terminal(events)
            rid = done["report_id"]
            assert done["status"] == "success", (done["status"], done.get("error"))
            assert choice_calls == [], f"全链路零 Choice 原生出站: {choice_calls}"
            dims = done["dimensions"]
            scenarios = dims["scenarios"]
            assert scenarios["status"] == "ok", scenarios["degraded_reason"]
            assert scenarios["valuation_basis"] == "none"
            assert scenarios["target_price"] is None and scenarios["analyst_target"] is None
            assert "iFinD" in (scenarios.get("narrative") or ""), "未查询目标价语义如实进入叙述"
            assert dims["conclusion"]["status"] == "ok" and dims["conclusion"]["conclusion"]["action"] == "观察"
            assert dims["signal"]["status"] == "ok" and dims["signal"]["signal_type"] == "非估值观察"
            fundamental = dims["fundamental"]
            assert fundamental["financial_snapshot"], "零核验分支下已采纳六事实随保存载荷保留"
            assert {k: v for k, v in fundamental["financial_snapshot"].items() if (v.get("reading") or {}).get("source") == "ifind"}
            assert all(f.get("quality", {}).get("status") != "verified" for f in fundamental["financial_snapshot"].values()), "单源不伪装核验"
            markdown = done["markdown"]
            # 未查询目标价语义在 dims 载荷断言（scenarios.narrative 含 iFinD）；
            # 渲染层经理波可能重排叙述，范围说明（非估值分析）必须在正文醒目。
            assert "非估值分析" in markdown
            # 保存/API 同链路重读一致
            detail = client.get(f"/reports/{rid}")
            assert detail.status_code == 200, detail.text
            saved = detail.json()["data"]
            assert saved["dimensions"]["scenarios"] == scenarios
            assert saved["dimensions"]["fundamental"]["financial_snapshot"] == fundamental["financial_snapshot"]
            download = client.get(f"/reports/{rid}/markdown?download=1")
            assert download.status_code == 200 and download.text == done["markdown"]
            assert database.get_deep_research_report(rid) is not None
    finally:
        database._engine.dispose()


def test_service_vertical_core_gap_stays_degraded_end_to_end(choice_transport, monkeypatch, tmp_path) -> None:
    """受控纵向负例：核心财务字段缺失（ROE 无回执）→ 豁免不成立，情景/结论/
    信号保持 degraded 并随保存/API 载荷可见（不伪造完成）。"""
    from fastapi.testclient import TestClient
    from src.storage import DatabaseManager
    from tests.test_issue64_no_target_mode import _assert_single_done_terminal

    service, database, app, choice_calls = _vertical_env(choice_transport, monkeypatch, tmp_path, roe_missing=True)
    try:
        with TestClient(app) as client:
            stream = client.post(
                "/generate/stream",
                json={"stock_code": "688183", "stock_name": "生益电子", "dims": _VERTICAL_DIMS, "force_refresh": True},
            )
            assert stream.status_code == 200, stream.text
            events = _sse_events(stream)
            done = _assert_single_done_terminal(events)
            assert choice_calls == [], f"零 Choice 契约在负例同样成立: {choice_calls}"
            dims = done["dimensions"]
            assert dims["fundamental"]["status"] == "degraded"
            assert "ROE" in (dims["fundamental"].get("data_gaps") or []), dims["fundamental"].get("data_gaps")
            scenarios = dims["scenarios"]
            assert scenarios["status"] == "degraded", scenarios
            assert dims["signal"]["status"] == "degraded"
            rid = done["report_id"]
            saved = client.get(f"/reports/{rid}").json()["data"]
            assert saved["dimensions"]["scenarios"]["status"] == "degraded"
            assert saved["dimensions"]["fundamental"]["data_gaps"] == dims["fundamental"]["data_gaps"]
            # 降级链路无估值驱动评级/目标价（独立技术计划的观察性措辞不在评级字段）
            for dim_id in ("signal", "conclusion", "scenarios"):
                payload = json.dumps(saved["dimensions"].get(dim_id) or {}, ensure_ascii=False)
                for banned in ('"rating": "买入"', '"rating": "增持"', '"action": "建仓"', '"action": "加仓"'):
                    assert banned not in payload, (dim_id, banned)
                if dim_id in ("signal", "scenarios"):
                    assert '"target_price": null' in payload, (dim_id, "降级链路目标价保持空")
                else:
                    assert '"value_range_1y": null' in payload and '"action": "观察"' in payload, (dim_id,)
    finally:
        database._engine.dispose()


# ---------------------------------------------------------------- v3 返修：manager 缓存身份 / 公司解析语义 / PB 补查取消与不合格原因

def test_manager_cache_identity_two_way_deep_vs_plain(choice_transport, monkeypatch, tmp_path) -> None:
    """root 反例成对回归（真实 manager 冷写/热读双向，不 seed/inject 缓存层）：
    非深研 false-scope 冷写（Choice 形状）→ 深研热读零 Choice 真实重聚合；
    深研冷写 → 非深研热读恢复 Choice 形状；同身份热读命中自身缓存零新增出站；
    两种身份各占独立缓存条目。聚合全程零错误（供应商回执齐备）以使缓存生效
    （基线语义：含错误的聚合不写缓存）。"""
    from data_provider.base import DataFetcherManager
    from src.core.research_run import ResearchRun
    from src.core import trading_calendar as _tc
    monkeypatch.setattr(_tc, "recent_completed_trading_sessions",
                        lambda market, count, current_time=None, **_: _VERTICAL_IFIND_SESSIONS[:max(0, min(count, len(_VERTICAL_IFIND_SESSIONS)))])

    choice_transport.enable_ifind_mcp = True
    choice_transport.ifind_mcp_endpoint = "https://fixture.invalid/mcp"
    choice_transport.ifind_mcp_token = "test-only"
    choice_transport.financial_primary_source = "ifind"

    company_env = deepcopy(COMPANY_ENV)
    company_env["data"]["answer"] = company_env["data"]["answer"].replace("600176.SH|中国巨石", "688183.SH|生益电子")

    async def ifind_route(self, tool, question, *, deadline=None):
        if tool == "get_stock_financials" and "主营构成" in question:
            from data_provider.cross_source_validator import SourceReadError as _SRE
            raise _SRE("ifind", "invalid_response")
        if "实际控制人" in question and "公司简介" in question:
            return deepcopy(company_env)
        if tool == "get_stock_financials":
            return deepcopy(FINANCIAL_ENV)
        if tool == "get_stock_info":
            return deepcopy(DATED_INFO_ENV)
        return deepcopy(FLOW_ENV)

    monkeypatch.setattr(IfindFetcher, "_async_query", ifind_route, raising=False)
    monkeypatch.setattr(
        IfindFetcher, "get_instance",
        classmethod(lambda cls: IfindFetcher("https://fixture.invalid/mcp", "test-only")),
    )
    # 主营构成批次为既有解析语义的独立数据需求（非本票风险层），测试替身提供
    # 合格成功使聚合无错误、缓存语义可被观察。
    from data_provider.ifind_fundamental_adapter import ProductSegment, ProductSegments

    def successful_products(self, code, period=None, *, deadline=None):
        return ProductSegments(items=[ProductSegment(name="覆铜板", rank=1, report_date="2026-09-30",
                                                     revenue=None, cost=None, gross_profit=None, gross_margin=None)],
                               errors=[])

    monkeypatch.setattr(IfindFetcher, "product_segments", successful_products)
    # Choice 健康回执（非深研完整聚合需要）：真实 SDK 本地传输 + 关键值。
    from data_provider.choice_adapter import ChoiceAdapter
    from tests.test_choice_integration import ChoiceSDK
    base_native = ChoiceAdapter._native_request
    sdk = ChoiceSDK()

    def healthy_native(method, args, options, deadline):
        raw = base_native(method, args, options, deadline)
        if method == "css":
            values = {"LATESTREPORTNEW": "2025/12/31", "ACTUALISSUINGDATE": "2026/04/25",
                      "OPERATEREVENEW": 2997710000, "PARENTNETPROFITNEW": 175060000,
                      "ROEWA": 8.65, "GPMARGIN": 21.0, "YOYOR": 5.2194, "YOYPNI": -22.656,
                      "SUMASSETNEW": 100.0, "SUMLIABNEW": 40.0,
                      "MAINBUSINESS": "覆铜板生产", "MAINPRODUCT": "覆铜板",
                      "COMPROFILE": "生产覆铜板的公司", "AHOLDER": "国资", "AHOLDERTYPE": "国企",
                      "NAME": "生益电子", "PETTM": 20.0, "PB": 2.0, "MV": 100.0}
            if isinstance(raw.get("data"), dict):
                indicators = raw.get("indicators") or []
                raw["data"][args[0]] = [values.get(k, v) for k, v in zip(indicators, raw["data"][args[0]])]
        return raw

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(healthy_native))

    manager = DataFetcherManager()
    # —— 方向一：plain（无 run）冷写 → deep 热读 ——
    plain_cold = manager.get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    assert plain_cold["research"].get("source") == "choice", "非深研 false-scope 冷写走完整聚合（Choice 形状）"
    assert len(manager._fundamental_cache) == 1, "plain 冷写占 d0 条目"
    with ResearchRun(120).bind():
        deep_hot = manager.get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    assert deep_hot["research"].get("source") == "ifind" and "targets" not in deep_hot["research"], \
        "深研热读不得命中非深研缓存（d1 键真实重聚合）"
    assert len(manager._fundamental_cache) == 2, "两种身份各占独立缓存条目"
    with ResearchRun(120).bind():
        deep_hot2 = manager.get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    assert deep_hot2["research"].get("source") == "ifind"
    assert len(manager._fundamental_cache) == 2, "同身份热读命中自身缓存（不新增条目）"

    # —— 方向二：deep 冷写（新 manager）→ plain 热读 ——
    manager2 = DataFetcherManager()
    with ResearchRun(120).bind():
        deep_cold = manager2.get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    assert deep_cold["research"].get("source") == "ifind" and "targets" not in deep_cold["research"]
    plain_hot = manager2.get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    assert plain_hot["research"].get("source") == "choice", "非深研热读不得命中深研缓存"
    assert "targets" in plain_hot["research"], "非深研热读恢复完整聚合的 Choice research 形状"


def test_company_controller_absent_type_only_stays_separate(offline_env, monkeypatch) -> None:
    """root 反例：控制人单元格为空、类型=国资委在 → controller 保持缺失，
    controller_type 如实采纳；raw_column 是实际命中列名，不把类型列标成控制人。"""
    from src.core import trading_calendar as _tc
    monkeypatch.setattr(_tc, "recent_completed_trading_sessions",
                        lambda market, count, current_time=None, **_: [date(2026, 11, 2)][:max(0, min(count, 1))])
    env = deepcopy(COMPANY_ENV)
    env["data"]["answer"] = env["data"]["answer"].replace("国务院国有资产监督管理委员会|", "\t|", 1)

    async def query(self, tool, question, *, deadline=None):
        return deepcopy(env)

    monkeypatch.setattr(IfindFetcher, "_async_query", query, raising=False)
    profile = IfindFetcher("https://fixture.invalid/mcp", "test-only").company_profile("600176")
    assert "controller" not in profile.facts, "控制人缺失保持缺失，不以类型列补位"
    assert profile.facts["controller_type"].value == "国资委"
    assert profile.facts["controller_type"].raw_column == "实际控制人类型"
    assert profile.facts["holding_ratio"].raw_column == "实际控制人持股比例（单位：%）", "认可单位后缀列按实际列名记录"


def test_company_embedded_foreign_security_rejected(offline_env, monkeypatch) -> None:
    """root 反例：证券列为 000001.SZ、叙述文本提及 600176 → 不得凭子串巧合
    采纳外证券行；证券归属只认证券代码列（规范化后精确匹配）。"""
    from data_provider.cross_source_validator import SourceReadError
    from src.core import trading_calendar as _tc
    monkeypatch.setattr(_tc, "recent_completed_trading_sessions",
                        lambda market, count, current_time=None, **_: [date(2026, 11, 2)][:max(0, min(count, 1))])
    env = deepcopy(COMPANY_ENV)
    env["data"]["answer"] = env["data"]["answer"].replace("|600176.SH|", "|000001.SZ|")
    env["data"]["answer"] = env["data"]["answer"].replace("中国巨石股份有限公司的主营业务", "为600176提供配套服务的公司主营业务")

    async def query(self, tool, question, *, deadline=None):
        return deepcopy(env)

    monkeypatch.setattr(IfindFetcher, "_async_query", query, raising=False)
    with pytest.raises(SourceReadError, match="invalid_response"):
        IfindFetcher("https://fixture.invalid/mcp", "test-only").company_profile("600176")


def test_company_reordered_columns_parse_identically(offline_env, monkeypatch) -> None:
    """列顺序重排（列名不变）→ 解析结果与原表一致（列身份按名，不按位置）。"""
    from src.core import trading_calendar as _tc
    monkeypatch.setattr(_tc, "recent_completed_trading_sessions",
                        lambda market, count, current_time=None, **_: [date(2026, 11, 2)][:max(0, min(count, 1))])
    env = deepcopy(COMPANY_ENV)
    lines = env["data"]["answer"].splitlines()
    header = lines[0]
    sep = lines[1]
    body = lines[2]
    cells_h = header.strip("|").split("|")
    cells_b = body.strip("|").split("|")
    order = sorted(range(len(cells_h)), key=lambda i: cells_h[i])
    lines[0] = "|" + "|".join(cells_h[i] for i in order) + "|"
    lines[2] = "|" + "|".join(cells_b[i] for i in order) + "|"
    env["data"]["answer"] = "\n".join([lines[0], sep, lines[2]] + lines[3:])

    async def query(self, tool, question, *, deadline=None):
        return deepcopy(env)

    monkeypatch.setattr(IfindFetcher, "_async_query", query, raising=False)
    profile = IfindFetcher("https://fixture.invalid/mcp", "test-only").company_profile("600176")
    assert profile.facts["name"].value == "中国巨石股份有限公司"
    assert profile.facts["controller"].value == "国务院国有资产监督管理委员会"
    assert profile.facts["controller_type"].value == "国资委"
    assert profile.facts["holding_ratio"].value == "12.9574"
    assert profile.facts["controller"].observed_at == "20261102"
    assert profile.facts["profile"].observed_at is None


def test_pb_directed_supplement_cancelled_keeps_completed_facts(offline_env, monkeypatch) -> None:
    """root 反例（v3）：定向补查 seam 处取消 → 聚合照常返回并保留已完成的
    财务/估值/行业/资金/research 事实；PB 缺失且无 directed_pb 拒绝原因
    （取消不是供应商失败）；停止后不再发起新出站。"""
    from src.core.research_run import ResearchRun, ResearchStopped
    from src.core import trading_calendar as _tc
    monkeypatch.setattr(_tc, "recent_completed_trading_sessions",
                        lambda market, count, current_time=None, **_: [date(2026, 10, 30), date(2026, 10, 29), date(2026, 10, 28), date(2026, 10, 27),
                                                                       date(2026, 10, 26), date(2026, 10, 23), date(2026, 10, 22), date(2026, 10, 21),
                                                                       date(2026, 10, 20), date(2026, 10, 19), date(2026, 10, 16)][:max(0, min(count, 11))])
    zhongzheng_env = deepcopy(ZHONGZHENG_ENV)
    zhongzheng_env["data"]["answer"] = zhongzheng_env["data"]["answer"].replace("600176.SH|中国巨石", "688183.SH|生益电子")

    run = ResearchRun(120)

    def stopping_pb(self, code, *, deadline=None, current_time=None):
        run.stop("cancelled")
        raise ResearchStopped(run.reason_text)

    async def route(self, tool, question, *, deadline=None):
        offline_env["ifind"].append((tool, question))
        if "所属申万行业" in question:
            return deepcopy(zhongzheng_env)
        return deepcopy(FINANCIAL_ENV if tool == "get_stock_financials" else FLOW_ENV)

    monkeypatch.setattr(IfindFetcher, "_async_query", route, raising=False)
    monkeypatch.setattr(IfindFetcher, "directed_pb_snapshot", stopping_pb)
    monkeypatch.setattr(IfindFetcher, "product_segments", lambda *a, **k: (_ for _ in ()).throw(AssertionError("停止后不得再发起可选出站")))
    with run.bind():
        result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    valuation = result["valuation"]["data"]
    assert valuation["pe_ratio"] is not None, "已完成估值事实保留"
    assert valuation["pb_ratio"] is None
    record = result["valuation"]["field_meta"].get("pb_ratio") or {}
    reasons = [item.get("reason_code") for item in (record.get("source_errors") or [])]
    assert "wrong_indicator_variant" in reasons, "聚合批次变体拒绝原因保留"
    assert not any(str(r).startswith("directed_pb:") for r in reasons), "取消不标供应商失败"
    growth = result["growth"]["data"]
    assert growth["roe"] == pytest.approx(29.34), "已完成财务事实保留"
    assert result["research"].get("source") == "ifind", "research 块保留"
    stock_flow = result["capital_flow"]["data"]["stock_flow"]
    assert stock_flow["source"] == "ifind", "资金事实保留"
    assert not any("company" in e for e in result["errors"]), "停止后公司批次不再出站"
    assert offline_env["choice"] == []


def test_pb_directed_supplement_unqualified_reading_keeps_reason(offline_env, monkeypatch) -> None:
    """root 反例（v3）：定向补查返回 stale 读数 → 读数不采纳且 directed_pb:stale
    随字段 source_errors 保留（future/口径/单位同类）。"""
    from src.core.research_run import ResearchRun
    from src.core import trading_calendar as _tc
    monkeypatch.setattr(_tc, "recent_completed_trading_sessions",
                        lambda market, count, current_time=None, **_: [date(2026, 10, 30), date(2026, 10, 29), date(2026, 10, 28), date(2026, 10, 27),
                                                                       date(2026, 10, 26), date(2026, 10, 23), date(2026, 10, 22), date(2026, 10, 21),
                                                                       date(2026, 10, 20), date(2026, 10, 19), date(2026, 10, 16)][:max(0, min(count, 11))])
    zhongzheng_env = deepcopy(ZHONGZHENG_ENV)
    zhongzheng_env["data"]["answer"] = zhongzheng_env["data"]["answer"].replace("600176.SH|中国巨石", "688183.SH|生益电子")
    stale_env = deepcopy(DIRECTED_PB_ENV)
    stale_env["data"]["answer"] = stale_env["data"]["answer"].replace("600176.SH|中国巨石", "688183.SH|生益电子")
    stale_env["data"]["answer"] = stale_env["data"]["answer"].replace("20261030", "20261001")

    async def route(self, tool, question, *, deadline=None):
        offline_env["ifind"].append((tool, question))
        if "所属申万行业" in question:
            return deepcopy(zhongzheng_env)
        if "不使用市净率(中证发布)" in question:
            return deepcopy(stale_env)
        return deepcopy(FINANCIAL_ENV if tool == "get_stock_financials" else FLOW_ENV)

    monkeypatch.setattr(IfindFetcher, "_async_query", route, raising=False)
    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    valuation = result["valuation"]["data"]
    assert valuation["pb_ratio"] is None, "stale 读数不采纳"
    record = result["valuation"]["field_meta"].get("pb_ratio") or {}
    reasons = {item.get("reason_code") for item in (record.get("source_errors") or [])}
    assert "wrong_indicator_variant" in reasons and "directed_pb:stale" in reasons, record
    assert offline_env["choice"] == []


# ---------------------------------------------------------------- v4 收尾：公共 helper 超时的中性阶段名（2026-11-04 唯一实报发现）

def _public_details_timeout_env(offline_env, monkeypatch: pytest.MonkeyPatch, *, ifind_primary: bool):
    """离线真实 manager 环境：公共详情 helper 真实走 join 超时路径。

    复用 2026-11-04 实报语义：iFinD 批次成功（真实回执）、公司资料超时
    （SourceReadError timeout → ifind:company:timeout）、公共详情 helper 在
    fetch 预算内未完成（真实 `worker.join` 超时，不 mock `_run_with_timeout`）。
    """
    import time as _time
    from data_provider.cross_source_validator import SourceReadError
    from src.config import get_config

    cfg = get_config()
    cfg.financial_primary_source = "ifind" if ifind_primary else "choice"
    monkeypatch.setattr(cfg, "fundamental_fetch_timeout_seconds", 0.5)
    monkeypatch.setattr(IfindFetcher, "company_profile",
                        lambda self, code, *, deadline=None: (_ for _ in ()).throw(SourceReadError("ifind", "timeout")))
    from data_provider.ifind_fundamental_adapter import ProductSegments
    monkeypatch.setattr(IfindFetcher, "product_segments",
                        lambda self, code, period=None, *, deadline=None: ProductSegments(items=[], errors=[]))

    manager = DataFetcherManager()

    def slow_public_bundle(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        _time.sleep(3.0)
        return {}

    monkeypatch.setattr(manager._fundamental_adapter, "get_fundamental_bundle", slow_public_bundle)
    return manager


def test_public_details_timeout_neutral_label_zero_choice_report(offline_env, monkeypatch) -> None:
    """实报反例回归：零 Choice 深研路径公共详情超时标签中性（public_details），
    无任何假 choice_ 标签；ifind 公司失败证据保留；已成功事实照常落位并进入
    F1 保存投影（warnings）。"""
    from src.core.research_run import ResearchRun
    from src.deep_research_dims.fundamental_dim import build_fundamental_dim

    manager = _public_details_timeout_env(offline_env, monkeypatch, ifind_primary=True)
    with ResearchRun(120).bind():
        result = manager.get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    errors = list(result["errors"])
    assert offline_env["choice"] == [], "深研路径零 Choice 原生出站"
    assert "public_details timeout" in errors, errors
    assert not any("choice" in str(e).lower() for e in errors), f"零 Choice 报告不得残留 choice 前缀标签: {errors}"
    assert "ifind:company:timeout" in errors, "公司资料真实失败证据保留"
    # 已成功事实仍保存：估值/财务/行业/资金 + research 来源
    assert result["valuation"]["data"]["pe_ratio"] == pytest.approx(24.7573)
    assert result["valuation"]["data"]["pb_ratio"] == pytest.approx(5.7123)
    assert result["growth"]["data"]["roe"] == pytest.approx(29.34)
    assert result["research"].get("source") == "ifind"
    assert result["capital_flow"]["data"]["stock_flow"]["source"] == "ifind"
    # 保存投影（与实报 MD 225/397 同源字段）：warnings 含中性标签与真实公司失败
    ctx = _fund({"pe_ttm": result["valuation"]["data"]["pe_ratio"], "pb": result["valuation"]["data"]["pb_ratio"],
                 "roe": result["growth"]["data"]["roe"], "errors": errors,
                 "field_meta": {**result["valuation"]["field_meta"], **result["growth"]["field_meta"]}})
    dim = build_fundamental_dim(ctx)
    warnings = list(dim.data_quality.warnings)
    assert any("public_details timeout" in w for w in warnings), warnings
    assert any("ifind:company:timeout" in w for w in warnings), warnings
    assert not any("choice" in str(w).lower() for w in warnings), warnings


def test_public_details_timeout_choice_primary_keeps_real_choice_errors(offline_env, monkeypatch) -> None:
    """成对回归：Choice 主源同超时也用中性阶段名（公共 helper 非 Choice 原生），
    且真实 Choice 失败仍保留 choice: 前缀——中性化不吞噬/不冒充 Choice 证据。"""
    manager = _public_details_timeout_env(offline_env, monkeypatch, ifind_primary=False)
    result = manager.get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    errors = list(result["errors"])
    assert "public_details timeout" in errors, errors
    choice_errors = [e for e in errors if str(e).startswith("choice:")]
    assert choice_errors, "Choice 主源真实失败（本环境 10001009）照常带 choice: 前缀保留"
    assert not any(str(e).startswith("choice_public") for e in errors), errors


# ---------------------------------------------------------------- v5：主营分产品财期与正常空覆盖（真实 Q3 空/H1 成功回执）

PRODUCTS_Q3_EMPTY_ENV = json.loads((FIXTURES / "ifind_products_600176_q3_empty.json").read_text())
PRODUCTS_H1_ITEMS_ENV = json.loads((FIXTURES / "ifind_products_600176_h1_items.json").read_text())


def _env_reply(env_provider: Callable[[], Dict[str, Any]]):
    """真实 async 传输缝：每次调用深拷贝同一真实信封（lambda 不能 async）。"""
    async def reply(self, tool, question, *, deadline=None):
        return deepcopy(env_provider())
    return reply


def _adapt_600176_env_to_688183(env: Dict[str, Any]) -> Dict[str, Any]:
    """真实 600176 回执对齐 688183 聚合 fixtures（仅替换单元格证券/简称值，
    列结构/日期/参数元数据保持真实形态；600176 原始语义由 adapter 级测试锁定）。"""
    out = deepcopy(env)
    out["data"]["answer"] = out["data"]["answer"].replace("600176.SH|中国巨石", "688183.SH|生益电子")
    return out


def _products_ifind_route(offline_env, monkeypatch: pytest.MonkeyPatch, *, current_env: Dict[str, Any], prior_env: Dict[str, Any] | None, current_period: str = "2026-09-30", prior_period: str = "2026-06-30") -> None:
    """按问句财期路由产品回执：本期/候选各一份真实信封（证券对齐 688183，
    其余批次沿用 offline_env；公司批次同样使用证券对齐的真实回执）。"""
    current = _adapt_600176_env_to_688183(current_env)
    prior = _adapt_600176_env_to_688183(prior_env) if prior_env is not None else None
    company = _adapt_600176_env_to_688183(COMPANY_ENV)

    async def route(self, tool, question, *, deadline=None):
        offline_env["ifind"].append((tool, question))
        if tool == "get_stock_financials" and "主营构成" in question:
            if prior_period in question:
                if prior is None:
                    raise AssertionError(f"不应发起历史候选回查: {question[:50]}")
                return deepcopy(prior)
            if current_period in question:
                return deepcopy(current)
            raise AssertionError(f"未预期的产品财期问句: {question[:60]}")
        if tool == "get_stock_financials":
            return deepcopy(FINANCIAL_ENV)
        if "实际控制人" in question and "公司简介" in question:
            return deepcopy(company)
        if tool == "get_stock_info":
            return deepcopy(DATED_INFO_ENV)
        return deepcopy(FLOW_ENV)

    monkeypatch.setattr(IfindFetcher, "_async_query", route, raising=False)


def test_products_q3_empty_classifies_normal_no_data(offline_env, monkeypatch) -> None:
    """真实 Q3 空回执（2026-11-04 采集）经生产解析：正常无覆盖而非 invalid_response。

    供应商信封成功、证券/分产品结构/财期证据齐备、产品单元格确为空；同表
    整体毛利率/毛利值不充当产品值（不影响分类）。"""
    monkeypatch.setattr(IfindFetcher, "_async_query", _env_reply(lambda: PRODUCTS_Q3_EMPTY_ENV), raising=False)
    result = IfindFetcher("https://fixture.invalid/mcp", "test-only").product_segments("600176", "2026-09-30")
    assert result.items == [] and result.errors == []
    assert result.no_data_periods == ("2026-09-30",)
    assert result.earlier_period_reference is False


def test_products_h1_real_reply_parses_three_items(offline_env, monkeypatch) -> None:
    """真实 H1 成功回执（同日采集）：3 项、实际财期 2026-06-30（回归锁定）。"""
    monkeypatch.setattr(IfindFetcher, "_async_query", _env_reply(lambda: PRODUCTS_H1_ITEMS_ENV), raising=False)
    result = IfindFetcher("https://fixture.invalid/mcp", "test-only").product_segments("600176", "2026-06-30")
    assert len(result.items) == 3
    assert {item.report_date for item in result.items} == {"2026-06-30"}
    assert result.errors == [] and result.no_data_periods == ()


def _products_negatives() -> Dict[str, Callable[[], Dict[str, Any]]]:
    """分类负例（真实回执最小变体）：错证券/缺证券/缺结构/期冲突/未来期/未知期。"""
    def wrong_security() -> Dict[str, Any]:
        env = deepcopy(PRODUCTS_Q3_EMPTY_ENV)
        env["data"]["answer"] = env["data"]["answer"].replace("600176.SH", "000001.SZ")
        return env

    def missing_security() -> Dict[str, Any]:
        env = deepcopy(PRODUCTS_Q3_EMPTY_ENV)
        env["data"]["answer"] = env["data"]["answer"].replace("|600176.SH|中国巨石|", "|\t|中国巨石|")
        return env

    def no_structure() -> Dict[str, Any]:
        env = deepcopy(PRODUCTS_Q3_EMPTY_ENV)
        env["data"]["answer"] = env["data"]["answer"].replace("主营构成-项目", "其他构成-项目")
        return env

    def period_conflict() -> Dict[str, Any]:
        env = deepcopy(PRODUCTS_Q3_EMPTY_ENV)
        for key, params in env["data"]["indicators_params"].items():
            if key.startswith("主营构成-项目名称(按产品)") and params.get("报告期") == "20260930":
                params["报告期"] = "20260630"
        return env

    def future_period() -> Dict[str, Any]:
        env = deepcopy(PRODUCTS_Q3_EMPTY_ENV)
        env["data"]["answer"] = env["data"]["answer"].replace("20260930", "20271231")
        for params in env["data"]["indicators_params"].values():
            if params.get("报告期") == "20260930":
                params["报告期"] = "20271231"
        return env

    def unknown_period() -> Dict[str, Any]:
        env = deepcopy(PRODUCTS_Q3_EMPTY_ENV)
        env["data"]["answer"] = env["data"]["answer"].replace("|20260930|", "|\t|")
        for params in env["data"]["indicators_params"].values():
            if params.get("报告期") == "20260930":
                params["报告期"] = ""
        return env

    return {"wrong_security": wrong_security, "missing_security": missing_security,
            "no_structure": no_structure, "period_conflict": period_conflict,
            "future_period": future_period, "unknown_period": unknown_period}


@pytest.mark.parametrize("case", sorted(_products_negatives()))
def test_products_no_data_negative_cases_stay_rejected(offline_env, monkeypatch, case) -> None:
    """错证券/缺证券/缺结构/期冲突/未来期/未知期不吞为 no_data：维持 invalid_response。"""
    from data_provider.cross_source_validator import SourceReadError

    env_factory = _products_negatives()[case]
    monkeypatch.setattr(IfindFetcher, "_async_query", _env_reply(env_factory), raising=False)
    with pytest.raises(SourceReadError, match="invalid_response"):
        IfindFetcher("https://fixture.invalid/mcp", "test-only").product_segments("600176", "2026-09-30")


def test_products_nonempty_malformed_is_qualification_error_not_no_data(offline_env, monkeypatch) -> None:
    """非空畸形（有名称无金额）：走既有资格错误（product_values_missing），非 no_data。"""
    env = deepcopy(PRODUCTS_Q3_EMPTY_ENV)
    env["data"]["answer"] = env["data"]["answer"].replace(
        "|600176.SH|中国巨石|\t|\t|\t|\t|\t|", "|600176.SH|中国巨石|玻纤及制品|\t|\t|\t|\t|", 1)
    monkeypatch.setattr(IfindFetcher, "_async_query", _env_reply(lambda: env), raising=False)
    result = IfindFetcher("https://fixture.invalid/mcp", "test-only").product_segments("600176", "2026-09-30")
    assert result.items == []
    assert "product_values_missing" in result.errors
    assert result.no_data_periods == ()


def test_prior_product_period_candidate_boundaries() -> None:
    """候选映射：Q3→同年H1、Q1→上年年报、H1→上年年报、年报→同年H1；未知不猜。"""
    from data_provider.base import _prior_product_period_candidate
    assert _prior_product_period_candidate("2026-09-30") == "2026-06-30"
    assert _prior_product_period_candidate("2026-03-31") == "2025-12-31"
    assert _prior_product_period_candidate("2026-06-30") == "2025-12-31"
    assert _prior_product_period_candidate("2025-12-31") == "2025-06-30"
    assert _prior_product_period_candidate("20260930") == "2026-06-30"
    assert _prior_product_period_candidate("去年年报") is None
    assert _prior_product_period_candidate("2026-04-30") is None
    assert _prior_product_period_candidate("") is None


def test_deep_scope_q3_empty_backfills_h1_core_stays_q3(offline_env, monkeypatch) -> None:
    """深研身份：Q3 正常无覆盖 → 公司之后恰一次 H1 候选回查成功采用（真实回执）；
    核心财务保持 Q3；同 run 重复聚合经缓存共享同一候选（零 Choice）。"""
    from src.core.research_run import ResearchRun

    _products_ifind_route(offline_env, monkeypatch, current_env=PRODUCTS_Q3_EMPTY_ENV, prior_env=PRODUCTS_H1_ITEMS_ENV)
    manager = DataFetcherManager()
    with ResearchRun(120).bind():
        result = manager.get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
        second = manager.get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    products = result["research"]["product_segments"]
    assert len(products["items"]) == 3 and {item["report_date"] for item in products["items"]} == {"2026-06-30"}
    assert products["no_data_periods"] == ["2026-09-30"]
    assert products["earlier_period_reference"] is True
    # 核心财务仍是本期 Q3，不被 H1 替换
    growth_meta = result["growth"]["field_meta"]
    assert growth_meta["roe"]["period"] == "2026-09-30"
    assert result["growth"]["data"]["roe"] == pytest.approx(29.34)
    # 公司资料仍取得（回查在公司之后，不挤占）
    assert result["research"].get("company", {}).get("NAME")
    # 恰一次候选出站；同 run 第二次聚合经缓存零新增
    prior_queries = [q for _t, q in offline_env["ifind"] if "主营构成" in q and "2026-06-30" in q]
    assert len(prior_queries) == 1, prior_queries
    current_queries = [q for _t, q in offline_env["ifind"] if "主营构成" in q and "2026-09-30" in q]
    assert len(current_queries) == 1, current_queries
    assert second["research"]["product_segments"] == products, "同 run 缓存命中共享同一产品结果"
    assert offline_env["choice"] == []


def test_deep_scope_q3_empty_prior_also_empty_discloses_both(offline_env, monkeypatch) -> None:
    """两期均空：本期与候选财期都保留为正常无覆盖，不包装成完整明细，其余事实不清空。"""
    from src.core.research_run import ResearchRun

    prior_empty = deepcopy(PRODUCTS_Q3_EMPTY_ENV)
    prior_empty["data"]["answer"] = prior_empty["data"]["answer"].replace("20260930", "20260630")
    for params in prior_empty["data"]["indicators_params"].values():
        if params.get("报告期") == "20260930":
            params["报告期"] = "20260630"
    _products_ifind_route(offline_env, monkeypatch, current_env=PRODUCTS_Q3_EMPTY_ENV, prior_env=prior_empty)
    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    products = result["research"]["product_segments"]
    assert products["items"] == [] and products["errors"] == []
    assert products["no_data_periods"] == ["2026-09-30", "2026-06-30"]
    assert products["earlier_period_reference"] is False
    assert result["growth"]["data"]["roe"] == pytest.approx(29.34), "核心财务保留"
    assert result["research"].get("company", {}).get("NAME"), "公司资料保留"
    assert result["capital_flow"]["data"]["stock_flow"]["source"] == "ifind", "资金保留"
    prior_queries = [q for _t, q in offline_env["ifind"] if "主营构成" in q and "2026-06-30" in q]
    assert len(prior_queries) == 1, "两期均空也只尝试一次候选，不循环多年"


def test_deep_scope_current_items_no_backfill(offline_env, monkeypatch) -> None:
    """本期有合格明细：零历史回查（唯一一次产品问句即本期）。"""
    from src.core.research_run import ResearchRun

    current_items = deepcopy(PRODUCTS_H1_ITEMS_ENV)
    current_items["data"]["answer"] = current_items["data"]["answer"].replace("20260630", "20260930")
    for _params in current_items["data"]["indicators_params"].values():
        if _params.get("报告期") == "20260630":
            _params["报告期"] = "20260930"
    _products_ifind_route(offline_env, monkeypatch, current_env=current_items, prior_env=None)
    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    products = result["research"]["product_segments"]
    assert len(products["items"]) == 3 and products["no_data_periods"] == []
    product_queries = [q for _t, q in offline_env["ifind"] if "主营构成" in q]
    assert len(product_queries) == 1, product_queries


def test_deep_scope_products_timeout_no_backfill(offline_env, monkeypatch) -> None:
    """timeout 不是正常无覆盖：不回查、错误如实记录（权限/额度/认证同类）。"""
    from data_provider.cross_source_validator import SourceReadError
    from src.core.research_run import ResearchRun

    async def route(self, tool, question, *, deadline=None):
        offline_env["ifind"].append((tool, question))
        if tool == "get_stock_financials" and "主营构成" in question:
            raise SourceReadError("ifind", "timeout")
        if tool == "get_stock_financials":
            return deepcopy(FINANCIAL_ENV)
        if "实际控制人" in question and "公司简介" in question:
            return deepcopy(COMPANY_ENV)
        if tool == "get_stock_info":
            return deepcopy(DATED_INFO_ENV)
        return deepcopy(FLOW_ENV)

    monkeypatch.setattr(IfindFetcher, "_async_query", route, raising=False)
    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    assert "ifind:products:timeout" in result["errors"]
    # v6：失败状态随 research 共享（键存在+真实原因），business 不因缺键旁路
    # latest_annual；报告内复用（错误聚合不缓存），不把失败改无覆盖/成功。
    products = result["research"]["product_segments"]
    assert products["items"] == [] and products["errors"] == ["timeout"]
    assert products["no_data_periods"] == [] and products["earlier_period_reference"] is False
    assert all("2026-06-30" not in q for _t, q in offline_env["ifind"]), "超时不触发历史回查"
    assert result["growth"]["data"]["roe"] == pytest.approx(29.34), "其余事实保留"


def test_deep_scope_backfill_cancelled_keeps_completed_facts(offline_env, monkeypatch) -> None:
    """候选回查 seam 取消：已完成财务/估值/公司/资金保留，无 products_prior 错误，
    本期无覆盖事实保留，停止后不再发起新出站。"""
    from src.core.research_run import ResearchRun, ResearchStopped
    from data_provider.ifind_fundamental_adapter import ProductSegments

    run = ResearchRun(120)

    def stopping_prior(self, code, period=None, *, deadline=None):
        if period == "2026-06-30":
            run.stop("cancelled")
            raise ResearchStopped(run.reason_text)
        return ProductSegments(items=[], errors=[], no_data_periods=("2026-09-30",))

    _products_ifind_route(offline_env, monkeypatch, current_env=PRODUCTS_Q3_EMPTY_ENV, prior_env=None)
    monkeypatch.setattr(IfindFetcher, "product_segments", stopping_prior)
    with run.bind():
        result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    assert result["valuation"]["data"]["pe_ratio"] == pytest.approx(24.7573), "已完成估值保留"
    assert result["growth"]["data"]["roe"] == pytest.approx(29.34), "已完成财务保留"
    products = result["research"]["product_segments"]
    assert products["no_data_periods"] == ["2026-09-30"], "本期无覆盖事实保留"
    assert not any(str(e).startswith("ifind:products_prior") for e in result["errors"]), "取消不标供应商失败"
    assert result["research"].get("company", {}).get("NAME"), "公司资料保留（回查在公司之后）"
    assert offline_env["choice"] == []


def test_deep_scope_zero_budget_no_backfill_dispatch(offline_env, monkeypatch) -> None:
    """预算耗尽：本期无覆盖后不派发候选出站（保留无覆盖事实）。"""
    from src.core.research_run import ResearchRun

    _products_ifind_route(offline_env, monkeypatch, current_env=PRODUCTS_Q3_EMPTY_ENV, prior_env=None)
    run = ResearchRun(120)
    with run.bind():
        # 直接以过期 deadline 调用 omission 助手：真实停止语义下不派发
        from data_provider.base import _ifind_research_omissions
        from src.config import get_config
        from src.core.research_run import current_research
        payload = _ifind_research_omissions(
            "688183", {}, __import__("time").monotonic() - 1, get_config(),
            include_holders=False, include_company=False, deep_identity=True)
    assert payload.get("omit") == ()
    prior_queries = [q for _t, q in offline_env["ifind"] if "主营构成" in q and "2026-06-30" in q]
    assert prior_queries == [], "零预算不派发候选"


def test_non_deep_ifind_primary_no_backfill_choice_company_kept(offline_env, monkeypatch) -> None:
    """非深研 iFinD 主源（无 run）：Q3 正常无覆盖分类可见但零自动回查；
    Choice 公司补查照常（基线行为），显式财期不被自动改变。"""
    _products_ifind_route(offline_env, monkeypatch, current_env=PRODUCTS_Q3_EMPTY_ENV, prior_env=None)
    result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    products = result["research"]["product_segments"]
    assert products["items"] == [] and products["no_data_periods"] == ["2026-09-30"]
    assert products["earlier_period_reference"] is False
    assert all("2026-06-30" not in q for _t, q in offline_env["ifind"]), "非深研不自动回查"
    assert result["research"].get("source") == "choice", "Choice 公司/目标价补查照常"


def test_explicit_period_research_context_no_auto_backfill(offline_env, monkeypatch) -> None:
    """显式财期专项入口（get_ifind_research_context）：返回无覆盖分类但不自动回查。"""
    async def recording_reply(self, tool, question, *, deadline=None):
        offline_env["ifind"].append((tool, question))
        return deepcopy(PRODUCTS_Q3_EMPTY_ENV)

    monkeypatch.setattr(IfindFetcher, "_async_query", recording_reply, raising=False)
    dataset = DataFetcherManager().get_ifind_research_context("600176", "products", period="2026-09-30")
    assert dataset["items"] == [] and dataset["no_data_periods"] == ["2026-09-30"]
    questions = [q for _t, q in offline_env["ifind"] if "主营构成" in q]
    assert len(questions) == 1, "显式入口只查目标财期，不自动改变目标财期"


def test_vertical_products_backfill_success_report_consistency(choice_transport, monkeypatch, tmp_path) -> None:
    """受控纵向（v5 正例）：Q3 正常无覆盖→H1 候选成功。真实编排→SQLite 保存→
    API detail→SSE done 全链零 Choice；business 维度携带较早财期参考（真实
    2026-06-30 明细+本期无覆盖事实），核心财务保持 Q3，MD 如实标注。"""
    from fastapi.testclient import TestClient
    from tests.test_issue64_no_target_mode import _DeterministicResearchAdapter, _assert_single_done_terminal
    from src.storage import DatabaseManager

    service, database, app, choice_calls = _vertical_env(choice_transport, monkeypatch, tmp_path, products_mode="backfill_success")
    try:
        with TestClient(app) as client:
            stream = client.post(
                "/generate/stream",
                json={"stock_code": "688183", "stock_name": "生益电子", "dims": _VERTICAL_DIMS, "force_refresh": True},
            )
            assert stream.status_code == 200, stream.text
            events = _sse_events(stream)
            done = _assert_single_done_terminal(events)
            assert done["status"] == "success", (done["status"], done.get("error"))
            assert choice_calls == [], "全链路零 Choice 原生出站"
            dims = done["dimensions"]
            products = (dims["business"] or {}).get("product_segments") or {}
            assert len(products.get("items") or []) == 3, products
            assert {item["report_date"] for item in products["items"]} == {"2026-06-30"}
            assert products["no_data_periods"] == ["2026-09-30"], "本期无覆盖事实保留"
            assert products["earlier_period_reference"] is True
            # 核心财务仍是本期 Q3，不被 H1 替换/混算
            roe = ((dims["fundamental"].get("financial_snapshot") or {}).get("roe") or {}).get("reading") or {}
            assert roe.get("period") == "2026-09-30" and roe.get("source") == "ifind"
            markdown = done["markdown"]
            assert "较早财期" in markdown and "2026-06-30" in markdown, "MD 明确较早财期参考"
            assert "无分产品明细覆盖" in markdown and "2026-09-30" in markdown, "MD 保留本期无覆盖事实"
            rid = done["report_id"]
            saved = client.get(f"/reports/{rid}").json()["data"]
            assert (saved["dimensions"]["business"] or {}).get("product_segments") == products
            assert saved["dimensions"]["fundamental"]["financial_snapshot"]["roe"]["reading"]["period"] == "2026-09-30"
    finally:
        database._engine.dispose()


def test_vertical_products_both_periods_empty_still_degrades_honestly(choice_transport, monkeypatch, tmp_path) -> None:
    """受控纵向（v5 负例）：本期与候选财期均正常无覆盖——两期如实披露、不包装
    完整明细；核心财务/公司/资金事实保留；报告完成状态不受可选产品缺口绑架。"""
    from fastapi.testclient import TestClient
    from tests.test_issue64_no_target_mode import _DeterministicResearchAdapter, _assert_single_done_terminal
    from src.storage import DatabaseManager

    service, database, app, choice_calls = _vertical_env(choice_transport, monkeypatch, tmp_path, products_mode="both_empty")
    try:
        with TestClient(app) as client:
            stream = client.post(
                "/generate/stream",
                json={"stock_code": "688183", "stock_name": "生益电子", "dims": _VERTICAL_DIMS, "force_refresh": True},
            )
            assert stream.status_code == 200, stream.text
            events = _sse_events(stream)
            done = _assert_single_done_terminal(events)
            assert choice_calls == []
            dims = done["dimensions"]
            products = (dims["business"] or {}).get("product_segments") or {}
            assert products.get("items") == [] and products.get("errors") == []
            assert products["no_data_periods"] == ["2026-09-30", "2026-06-30"], products
            assert products["earlier_period_reference"] is False
            roe = ((dims["fundamental"].get("financial_snapshot") or {}).get("roe") or {}).get("reading") or {}
            assert roe.get("period") == "2026-09-30", "核心财务保持本期"
            markdown = done["markdown"]
            assert "正常无覆盖" in markdown and "2026-09-30、2026-06-30" in markdown
            assert "较早财期" not in markdown, "两期均空不渲染较早财期参考行"
            rid = done["report_id"]
            saved = client.get(f"/reports/{rid}").json()["data"]
            assert (saved["dimensions"]["business"] or {}).get("product_segments", {}).get("no_data_periods") == ["2026-09-30", "2026-06-30"]
    finally:
        database._engine.dispose()


def test_products_old_payload_parses_with_new_field_defaults() -> None:
    """旧产品载荷（无 v5 字段）可读：新字段取默认（兼容），不因缺键拒绝。"""
    from data_provider.ifind_fundamental_adapter import ProductSegments
    legacy = {"source": "ifind", "coverage": "partial", "items": [], "errors": [], "disclosed_at": None, "original_url": None}
    parsed = ProductSegments.model_validate_json(json.dumps(legacy))
    assert parsed.no_data_periods == () and parsed.earlier_period_reference is False


# ---------------------------------------------------------------- v6 返修：候选实际期资格 / 失败共享 / 结构资格

def _prior_env_with_actual_period(actual: str) -> Dict[str, Any]:
    """H1 真实成功回执的全部实际期改为指定财期（root 反例构造法）。"""
    prior = deepcopy(PRODUCTS_H1_ITEMS_ENV)
    prior["data"]["answer"] = prior["data"]["answer"].replace("20260630", actual)
    for params in prior["data"]["indicators_params"].values():
        if params.get("报告期") == "20260630":
            params["报告期"] = actual
    return prior


@pytest.mark.parametrize("actual,reason", [("20260930", "candidate_period_not_earlier"), ("20270630", "candidate_period_future")])
def test_prior_candidate_actual_period_disqualified(offline_env, monkeypatch, actual, reason) -> None:
    """R1 反例：候选回执实际期=本期或未来 → 不采纳为历史参考；保留本期无覆盖、
    公司/财务/资金与准确原因；无伪历史流入 research（保存投影同源字段）。"""
    from src.core.research_run import ResearchRun

    _products_ifind_route(offline_env, monkeypatch, current_env=PRODUCTS_Q3_EMPTY_ENV, prior_env=_prior_env_with_actual_period(actual))
    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    products = result["research"]["product_segments"]
    assert products["items"] == [], "同本期/未来明细不得流入"
    assert products["no_data_periods"] == ["2026-09-30"], "保留本期真实无覆盖"
    assert products["earlier_period_reference"] is False
    assert f"ifind:products_prior:{reason}" in result["errors"], result["errors"]
    assert result["research"].get("company", {}).get("NAME"), "公司资料保留"
    assert result["growth"]["data"]["roe"] == pytest.approx(29.34), "核心财务保留（仍是 Q3）"
    assert result["valuation"]["data"]["pe_ratio"] == pytest.approx(24.7573)
    assert offline_env["choice"] == []


def test_prior_candidate_mixed_periods_disqualified(offline_env, monkeypatch) -> None:
    """R1 反例：候选回执跨两个实际期（H1+Q3 各有 items）→ candidate_period_mixed。"""
    from src.core.research_run import ResearchRun

    # 真实构造跨两期 items：名称表补行日期、清名称列参数期（避免期冲突），
    # 两期各给名称行+数值行 → join 产出 H1 与 Q3 两组 items（生产解析路径）。
    prior = _prior_env_with_actual_period("20260630")
    lines = prior["data"]["answer"].splitlines()
    name_header_idx = 0
    name_row_idx = 2
    lines[name_header_idx] = lines[name_header_idx].replace("|证券代码|证券简称|", "|证券代码|证券简称|日期|", 1)
    lines[1] = lines[1].replace("|---|---|", "|---|---|---|", 1) if lines[1].startswith("|---|---|") else lines[1]
    lines[name_row_idx] = lines[name_row_idx].replace("|600176.SH|中国巨石|", "|600176.SH|中国巨石|20260630|", 1)
    extra_name_row = lines[name_row_idx].replace("20260630", "20260930")
    lines.insert(name_row_idx + 1, extra_name_row)
    value_row_idx = next(i for i, line in enumerate(lines) if "20260630" in line and "10860287088" in line)
    extra_value_row = lines[value_row_idx].replace("20260630", "20260930").replace("10860287088.6800", "999999.0")
    lines.insert(value_row_idx + 1, extra_value_row)
    prior["data"]["answer"] = "\n".join(lines)
    for key, params in prior["data"]["indicators_params"].items():
        if "项目名称" in key and params.get("报告期") == "20260630":
            params["报告期"] = ""
    _products_ifind_route(offline_env, monkeypatch, current_env=PRODUCTS_Q3_EMPTY_ENV, prior_env=prior)
    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    products = result["research"]["product_segments"]
    assert products["items"] == [] and products["no_data_periods"] == ["2026-09-30"]
    assert "ifind:products_prior:candidate_period_mixed" in result["errors"], result["errors"]


def test_current_no_data_evidenced_period_mismatch_not_coverage(offline_env, monkeypatch) -> None:
    """R1 反例：本期请求 Q3、回执空覆盖证据却是 H1 → 本期覆盖状态未知：
    period_mismatch 资格失败共享，不认定无覆盖、不回查。"""
    from src.core.research_run import ResearchRun

    mismatch_env = deepcopy(PRODUCTS_Q3_EMPTY_ENV)
    mismatch_env["data"]["answer"] = mismatch_env["data"]["answer"].replace("20260930", "20260630")
    for params in mismatch_env["data"]["indicators_params"].values():
        if params.get("报告期") == "20260930":
            params["报告期"] = "20260630"
    _products_ifind_route(offline_env, monkeypatch, current_env=mismatch_env, prior_env=None)
    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    products = result["research"]["product_segments"]
    assert products["items"] == [] and products["errors"] == ["period_mismatch"]
    assert products["no_data_periods"] == [], "回显期≠研究期不得记为本期无覆盖"
    assert "ifind:products:period_mismatch" in result["errors"]
    assert all("2026-06-30" not in q or "主营构成" not in q for _t, q in offline_env["ifind"]), "不触发回查"
    assert result["growth"]["data"]["roe"] == pytest.approx(29.34)


def test_business_reuses_shared_products_states_no_bypass(offline_env, monkeypatch) -> None:
    """R2：真实 manager→真实 business 入口——失败/无覆盖/成功三类共享状态下
    business 均零额外产品出站（不旁路 latest_annual）。"""
    from src.core.research_run import ResearchRun
    from src.agent.deep_research import explore_agents
    from src.agent.tools import data_tools

    manager = DataFetcherManager()
    monkeypatch.setattr(data_tools, "_fetcher_manager_singleton", manager)
    monkeypatch.setattr(explore_agents, "_run_explore", lambda *a, **k: {"ok": True, "data": {"narrative": "fixture", "data_gaps": []}})

    async def route(self, tool, question, *, deadline=None):
        offline_env["ifind"].append((tool, question))
        if tool == "get_stock_financials" and "主营构成" in question:
            from data_provider.cross_source_validator import SourceReadError as _SRE
            raise _SRE("ifind", "timeout")
        if tool == "get_stock_financials":
            return deepcopy(FINANCIAL_ENV)
        if "实际控制人" in question and "公司简介" in question:
            return deepcopy(COMPANY_ENV)
        if tool == "get_stock_info":
            return deepcopy(DATED_INFO_ENV)
        return deepcopy(FLOW_ENV)

    monkeypatch.setattr(IfindFetcher, "_async_query", route, raising=False)
    with ResearchRun(120).bind():
        raw = manager.get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
        before = len(offline_env["ifind"])
        output = explore_agents.run_business_agent("688183", "生益电子", None, research=raw["research"])
        extra = len(offline_env["ifind"]) - before
    assert extra == 0, f"business 在共享失败状态下不得另查产品: {extra}"
    shared = output["data"].get("product_segments") or {}
    assert shared.get("errors") == ["timeout"] and shared.get("items") == [], "失败状态随 business 载荷如实保留"

    # 正常无覆盖与成功状态同样去重（不另查默认年报）
    _products_ifind_route(offline_env, monkeypatch, current_env=PRODUCTS_Q3_EMPTY_ENV, prior_env=None)
    manager2 = DataFetcherManager()
    monkeypatch.setattr(data_tools, "_fetcher_manager_singleton", manager2)
    with ResearchRun(120).bind():
        raw2 = manager2.get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
        before2 = len(offline_env["ifind"])
        explore_agents.run_business_agent("688183", "生益电子", None, research=raw2["research"])
        assert len(offline_env["ifind"]) == before2, "无覆盖共享状态下 business 零额外产品出站"

    _products_ifind_route(offline_env, monkeypatch, current_env=_adapted_current_items(), prior_env=None)
    manager3 = DataFetcherManager()
    monkeypatch.setattr(data_tools, "_fetcher_manager_singleton", manager3)
    with ResearchRun(120).bind():
        raw3 = manager3.get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
        before3 = len(offline_env["ifind"])
        explore_agents.run_business_agent("688183", "生益电子", None, research=raw3["research"])
        assert len(offline_env["ifind"]) == before3, "成功共享状态下 business 零额外产品出站"


def _adapted_current_items() -> Dict[str, Any]:
    current_items = deepcopy(PRODUCTS_H1_ITEMS_ENV)
    current_items["data"]["answer"] = current_items["data"]["answer"].replace("20260630", "20260930")
    for params in current_items["data"]["indicators_params"].values():
        if params.get("报告期") == "20260630":
            params["报告期"] = "20260930"
    return current_items


def test_products_failure_not_negatively_cached_new_run_recovers(offline_env, monkeypatch) -> None:
    """R2：失败不写跨报告负缓存——新 run 恢复重试并可用（强刷走既有刷新标记）。"""
    from src.core.research_run import ResearchRun

    manager = DataFetcherManager()
    state = {"fail": True}

    async def route(self, tool, question, *, deadline=None):
        offline_env["ifind"].append((tool, question))
        if tool == "get_stock_financials" and "主营构成" in question:
            if state["fail"]:
                from data_provider.cross_source_validator import SourceReadError as _SRE
                raise _SRE("ifind", "timeout")
            if "2026-06-30" in question:
                return _adapt_600176_env_to_688183(PRODUCTS_H1_ITEMS_ENV)
            return _adapt_600176_env_to_688183(PRODUCTS_Q3_EMPTY_ENV)
        if tool == "get_stock_financials":
            return deepcopy(FINANCIAL_ENV)
        if "实际控制人" in question and "公司简介" in question:
            return _adapt_600176_env_to_688183(COMPANY_ENV)
        if tool == "get_stock_info":
            return deepcopy(DATED_INFO_ENV)
        return deepcopy(FLOW_ENV)

    monkeypatch.setattr(IfindFetcher, "_async_query", route, raising=False)
    with ResearchRun(120).bind():
        failed = manager.get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    assert failed["research"]["product_segments"]["errors"] == ["timeout"]
    assert not manager._fundamental_cache, "含错误的聚合不写缓存（无跨报告负缓存）"
    state["fail"] = False
    with ResearchRun(120).bind():
        recovered = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    products = recovered["research"]["product_segments"]
    assert len(products["items"]) == 3 and products["earlier_period_reference"] is True, "新 run 恢复：Q3 空→H1 参考正常取得"


def test_products_no_data_rank_zero_and_name_only_structure_rejected(offline_env, monkeypatch) -> None:
    """R3 反例（standards 红例）：第0名空结构 / 仅名称空列缺全部数值明细列
    → 结构/资格失败（invalid_response），不分类、不触发回查。"""
    from data_provider.cross_source_validator import SourceReadError

    rank_zero = deepcopy(PRODUCTS_Q3_EMPTY_ENV)
    rank_zero["data"]["answer"] = rank_zero["data"]["answer"].replace("第1名", "第0名")
    name_only = deepcopy(PRODUCTS_Q3_EMPTY_ENV)
    for col in ("主营构成-项目收入", "主营构成-项目成本", "主营构成-项目毛利", "主营构成-项目毛利率"):
        name_only["data"]["answer"] = name_only["data"]["answer"].replace(col, f"无关列-{col[5:]}")
        name_only["data"]["indicators_params"] = {k: v for k, v in name_only["data"]["indicators_params"].items() if col not in k}

    for env in (rank_zero, name_only):
        monkeypatch.setattr(IfindFetcher, "_async_query", _env_reply(lambda: env), raising=False)
        with pytest.raises(SourceReadError, match="invalid_response"):
            IfindFetcher("https://fixture.invalid/mcp", "test-only").product_segments("600176", "2026-09-30")

    # manager 级：结构失败不触发历史回查（走失败共享路径）
    from src.core.research_run import ResearchRun

    monkeypatch.setattr(IfindFetcher, "_async_query", _env_reply(lambda: rank_zero), raising=False)
    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    assert result["research"]["product_segments"]["errors"] == ["invalid_response"]
    assert all("2026-06-30" not in q or "主营构成" not in q for _t, q in offline_env["ifind"]), "结构失败不回查"


def test_products_no_data_structure_row_without_period_rejected(offline_env, monkeypatch) -> None:
    """R3：结构行缺财期证据（无行日期且列参数无报告期）→ 时间不明不分类。"""
    env = deepcopy(PRODUCTS_Q3_EMPTY_ENV)
    env["data"]["answer"] = env["data"]["answer"].replace("|20260930|", "|\t|")
    for key, params in list(env["data"]["indicators_params"].items()):
        if "项目名称" in key:
            params["报告期"] = ""
    monkeypatch.setattr(IfindFetcher, "_async_query", _env_reply(lambda: env), raising=False)
    with pytest.raises(Exception) as excinfo:
        IfindFetcher("https://fixture.invalid/mcp", "test-only").product_segments("600176", "2026-09-30")
    assert "invalid_response" in str(excinfo.value) or "period" in str(excinfo.value)


# ---------------------------------------------------------------- v7：初查四出口 / non-deep 收窄 / 同 rank 结构关联

def test_initial_items_earlier_period_zero_extra_adopted(offline_env, monkeypatch) -> None:
    """R1 正例（spec repro initial_h1 语义，真实聚合层）：初查回 H1 真实明细
    （实际期 2026-06-30 < 研究期 Q3）→ 零额外出站直接作较早财期参考
    （earlier=True、真实期保留、不伪造本期无覆盖），核心财务保持 Q3。"""
    from src.core.research_run import ResearchRun

    _products_ifind_route(offline_env, monkeypatch, current_env=PRODUCTS_H1_ITEMS_ENV, prior_env=None)
    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    products = result["research"]["product_segments"]
    assert len(products["items"]) == 3 and {i["report_date"] for i in products["items"]} == {"2026-06-30"}
    assert products["earlier_period_reference"] is True
    assert products["no_data_periods"] == [], "不声称本期无覆盖"
    product_queries = [q for _t, q in offline_env["ifind"] if "主营构成" in q]
    assert len(product_queries) == 1, "零额外候选出站"
    assert result["growth"]["field_meta"]["roe"]["period"] == "2026-09-30", "核心财务仍是本期"
    assert offline_env["choice"] == []


@pytest.mark.parametrize("actual,reason", [("20271231", "initial_period_future")])
def test_initial_items_future_rejected(offline_env, monkeypatch, actual, reason) -> None:
    """R1 反例：初查回未来期明细 → 不采用、共享准确原因、不回查、事实保留。"""
    from src.core.research_run import ResearchRun

    env = deepcopy(PRODUCTS_H1_ITEMS_ENV)
    env["data"]["answer"] = env["data"]["answer"].replace("20260630", actual)
    for params in env["data"]["indicators_params"].values():
        if params.get("报告期") == "20260630":
            params["报告期"] = actual
    _products_ifind_route(offline_env, monkeypatch, current_env=env, prior_env=None)
    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    products = result["research"]["product_segments"]
    assert products["items"] == [] and products["errors"] == [reason]
    assert products["no_data_periods"] == [] and products["earlier_period_reference"] is False
    assert f"ifind:products:{reason}" in result["errors"], result["errors"]
    assert all("2026-06-30" not in q or "主营构成" not in q for _t, q in offline_env["ifind"]), "拒绝后不回查"
    assert result["growth"]["data"]["roe"] == pytest.approx(29.34), "核心财务保留"
    assert result["research"].get("company", {}).get("NAME"), "公司保留"


def test_initial_items_mixed_rejected(offline_env, monkeypatch) -> None:
    """R1 反例：初查跨两期明细 → initial_period_mixed，不采用、不回查。"""
    from src.core.research_run import ResearchRun

    env = deepcopy(PRODUCTS_H1_ITEMS_ENV)
    lines = env["data"]["answer"].splitlines()
    lines[0] = lines[0].replace("|证券代码|证券简称|", "|证券代码|证券简称|日期|", 1)
    lines[2] = lines[2].replace("|600176.SH|中国巨石|", "|600176.SH|中国巨石|20260630|", 1)
    lines.insert(3, lines[2].replace("20260630", "20260930"))
    value_row_idx = next(i for i, line in enumerate(lines) if "20260630" in line and "10860287088" in line)
    lines.insert(value_row_idx + 1, lines[value_row_idx].replace("20260630", "20260930").replace("10860287088.6800", "888888.0"))
    env["data"]["answer"] = "\n".join(lines)
    for key, params in env["data"]["indicators_params"].items():
        if "项目名称" in key and params.get("报告期") == "20260630":
            params["报告期"] = ""
    _products_ifind_route(offline_env, monkeypatch, current_env=env, prior_env=None)
    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    products = result["research"]["product_segments"]
    assert products["items"] == [] and products["errors"] == ["initial_period_mixed"]
    assert "ifind:products:initial_period_mixed" in result["errors"]


def test_initial_selection_unit_edges() -> None:
    """R1 边界（助手单元）：晚于研究期拒绝；研究期缺失按真实期原样采用不猜关系。"""
    from data_provider.base import _initial_products_selection
    from data_provider.ifind_fundamental_adapter import IfindData, IfindFetcher
    from unittest.mock import patch

    h1 = json.loads((FIXTURES / "ifind_products_600176_h1_items.json").read_text())
    with patch.object(IfindFetcher, "query", return_value=IfindData.model_validate(h1["data"])):
        q3_items = IfindFetcher("https://fixture.invalid/mcp", "test-only").product_segments("600176", "2026-06-30")
        assert {i.report_date for i in q3_items.items} == {"2026-06-30"}
    adopted, reason = _initial_products_selection(q3_items, "2026-09-30")
    assert adopted is not None and reason is None and adopted.earlier_period_reference is True
    adopted2, reason2 = _initial_products_selection(q3_items, "2026-03-31")
    assert adopted2 is None and reason2 == "initial_period_after_research"
    adopted3, reason3 = _initial_products_selection(q3_items, None)
    assert adopted3 is not None and reason3 is None and adopted3.earlier_period_reference is False, "研究期缺失不猜关系"


def test_prior_candidate_no_data_period_mismatch(offline_env, monkeypatch) -> None:
    """R1 第四出口反例（spec repro prior_empty_same_current 语义）：Q3 空→请求
    H1 候选，回执却证实 Q3 空 → 不得静默合并冒充 H1 已验证；保留原 Q3 空事实
    与准确 candidate period mismatch，不发下一候选。"""
    from src.core.research_run import ResearchRun

    _products_ifind_route(offline_env, monkeypatch, current_env=PRODUCTS_Q3_EMPTY_ENV, prior_env=PRODUCTS_Q3_EMPTY_ENV)
    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    products = result["research"]["product_segments"]
    assert products["items"] == []
    assert products["no_data_periods"] == ["2026-09-30"], "原 Q3 空事实保留，不合并候选期"
    assert "ifind:products_prior:period_mismatch" in result["errors"], result["errors"]
    prior_queries = [q for _t, q in offline_env["ifind"] if "主营构成" in q and "2026-06-30" in q]
    assert len(prior_queries) == 1, "候选仍恰一次（请求已发出），无下一候选"


def test_nondeep_failure_no_key_business_keeps_original_query(offline_env, monkeypatch) -> None:
    """R2（spec nondeep repro 反转）：无深研 run 的独立 iFinD 主源聚合失败不落
    键——business 维持既有 latest_annual 查询资格（原行为），普通显式/独立
    服务重试语义不变；deep 失败共享行为由既有 deep 测试锁定。"""
    from data_provider.cross_source_validator import SourceReadError
    from src.agent.deep_research import explore_agents
    from src.agent.tools import data_tools

    async def route(self, tool, question, *, deadline=None):
        offline_env["ifind"].append((tool, question))
        if tool == "get_stock_financials" and "主营构成" in question:
            raise SourceReadError("ifind", "timeout")
        if tool == "get_stock_financials":
            return deepcopy(FINANCIAL_ENV)
        if "实际控制人" in question and "公司简介" in question:
            return deepcopy(COMPANY_ENV)
        if tool == "get_stock_info":
            return deepcopy(DATED_INFO_ENV)
        return deepcopy(FLOW_ENV)

    monkeypatch.setattr(IfindFetcher, "_async_query", route, raising=False)
    manager = DataFetcherManager()
    monkeypatch.setattr(data_tools, "_fetcher_manager_singleton", manager)
    monkeypatch.setattr(explore_agents, "_run_explore", lambda *a, **k: {"ok": True, "data": {"narrative": "fixture", "data_gaps": []}})
    raw = manager.get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    assert "product_segments" not in raw["research"], "非深研失败不落键（原语义）"
    before = len(offline_env["ifind"])
    explore_agents.run_business_agent("688183", "生益电子", None, research=raw["research"])
    extra = len(offline_env["ifind"]) - before
    assert extra == 1, f"business 维持既有默认年报查询资格: {extra}"
    assert any("2025-12-31" in q for _t, q in offline_env["ifind"][before:]), "查询为 latest_annual 目标期"


@pytest.mark.parametrize("headers,cells", [
    (["证券代码", "日期", "主营构成-项目名称(按产品)（排名：第1名）", "主营构成-项目收入(按产品)（排名：第2名）"],
     ["600176.SH", "20260930", "\t", "\t"]),  # disjoint ranks
    (["证券代码", "日期", "主营构成-项目名称(按产品)（排名：第1名）", "主营构成-项目收入(按产品)（排名：第1名）", "主营构成-项目收入(按产品)（排名：第-1名）"],
     ["600176.SH", "20260930", "\t", "\t", "99"]),  # negative rank non-empty
    (["证券代码", "日期", "主营构成-项目收入(按产品)（排名：第1名）"],
     ["600176.SH", "20260930", "\t"]),  # value-only（缺名称结构）
])
def test_products_unjoinable_structure_not_no_data(offline_env, monkeypatch, headers, cells) -> None:
    """R3（standards 红例+对称边界）：不相交 rank/非法排名非空列/缺名称结构
    均为结构资格失败（invalid_response），不分类、不回查。"""
    from data_provider.cross_source_validator import SourceReadError

    raw = deepcopy(PRODUCTS_Q3_EMPTY_ENV)
    raw["data"]["answer"] = "|" + "|".join(headers) + "|\n|" + "|".join("---" for _ in headers) + "|\n|" + "|".join(cells) + "|"
    raw["data"]["indicators_params"] = {}
    monkeypatch.setattr(IfindFetcher, "_async_query", _env_reply(lambda: raw), raising=False)
    with pytest.raises(SourceReadError, match="invalid_response"):
        IfindFetcher("https://fixture.invalid/mcp", "test-only").product_segments("600176", "2026-09-30")


def test_vertical_products_initial_earlier_reference(choice_transport, monkeypatch, tmp_path) -> None:
    """受控纵向（v7 正例）：初查直接回较早财期真实明细——earlier=True、真实期
    2026-06-30 随保存/API 一致、核心财务 Q3、MD 用通用较早参考说明且不出现
    伪造的本期无覆盖声明。"""
    from fastapi.testclient import TestClient
    from tests.test_issue64_no_target_mode import _assert_single_done_terminal
    from src.storage import DatabaseManager

    service, database, app, choice_calls = _vertical_env(choice_transport, monkeypatch, tmp_path, products_mode="initial_earlier")
    try:
        with TestClient(app) as client:
            stream = client.post("/generate/stream", json={"stock_code": "688183", "stock_name": "生益电子", "dims": _VERTICAL_DIMS, "force_refresh": True})
            assert stream.status_code == 200, stream.text
            done = _assert_single_done_terminal(_sse_events(stream))
            assert done["status"] == "success"
            assert choice_calls == []
            products = (done["dimensions"]["business"] or {}).get("product_segments") or {}
            assert len(products["items"]) == 3 and {i["report_date"] for i in products["items"]} == {"2026-06-30"}
            assert products["earlier_period_reference"] is True and products["no_data_periods"] == []
            roe = ((done["dimensions"]["fundamental"].get("financial_snapshot") or {}).get("roe") or {}).get("reading") or {}
            assert roe.get("period") == "2026-09-30", "核心财务仍是本期"
            markdown = done["markdown"]
            assert "较早财期" in markdown and "2026-06-30" in markdown
            assert "经供应商证实无分产品明细覆盖" not in markdown, "不伪造本期无覆盖"
            assert "（）" not in markdown, "不出现空括号"
    finally:
        database._engine.dispose()


def test_vertical_products_initial_future_rejected(choice_transport, monkeypatch, tmp_path) -> None:
    """受控纵向（v7 负例）：初查回未来期明细 → 拒绝并共享准确原因，核心 Q3
    财务保持，MD 如实披露缺口，不回查、不采用。"""
    from fastapi.testclient import TestClient
    from tests.test_issue64_no_target_mode import _assert_single_done_terminal
    from src.storage import DatabaseManager

    service, database, app, choice_calls = _vertical_env(choice_transport, monkeypatch, tmp_path, products_mode="initial_future")
    try:
        with TestClient(app) as client:
            stream = client.post("/generate/stream", json={"stock_code": "688183", "stock_name": "生益电子", "dims": _VERTICAL_DIMS, "force_refresh": True})
            assert stream.status_code == 200, stream.text
            done = _assert_single_done_terminal(_sse_events(stream))
            assert choice_calls == []
            products = (done["dimensions"]["business"] or {}).get("product_segments") or {}
            assert products["items"] == [] and products["errors"] == ["initial_period_future"]
            roe = ((done["dimensions"]["fundamental"].get("financial_snapshot") or {}).get("roe") or {}).get("reading") or {}
            assert roe.get("period") == "2026-09-30", "核心财务仍是本期"
            markdown = done["markdown"]
            assert "初查返回未来财期" in markdown, "MD 如实披露拒绝原因"
    finally:
        database._engine.dispose()


# ---------------------------------------------------------------- v8：产品失败不跨报告固化（business 维度缓存，已批准方案）

def _v8_business_env(offline_env, monkeypatch, tmp_path, *, products_route):
    """真实 manager→business→parse→dim_cache 环境（仅供应商/LLM 外部替身）。"""
    from src.agent.deep_research import explore_agents
    from src.agent.tools import data_tools
    from src.deep_research_dims import dim_cache

    monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path / "dims"))
    products_route(offline_env, monkeypatch)
    manager = DataFetcherManager()
    monkeypatch.setattr(data_tools, "_fetcher_manager_singleton", manager)
    monkeypatch.setattr(explore_agents, "_run_explore", lambda *a, **k: {
        "ok": True, "data": {"business_model": {"value": "玻纤产品生产销售"},
                             "main_products": {"value": "玻纤及电子布产品"},
                             "score": 60.0, "narrative": "已取得公开经营材料。", "data_gaps": []}})
    from src.deep_research.researchers.business import parse as business_parse
    return manager, business_parse, dim_cache


def _run_business_once(manager, business_parse, dim_cache, stock="688183"):
    """真实 run 内聚合→business→parse→按 orchestrator 同门槛写缓存。"""
    from src.core.research_run import ResearchRun
    from src.agent.deep_research import explore_agents

    with ResearchRun(120).bind():
        raw = manager.get_fundamental_context(stock, budget_seconds=60, include_sector_flows=False)
        out = explore_agents.run_business_agent(stock, "生益电子", None, research=raw["research"])
        business, _ = business_parse(out["data"], 0)
        if business.status == "ok":
            dim_cache.save_cached_dim(stock, "business", business.model_dump())
    return raw, business


def test_business_cache_write_skip_on_initial_product_failure(offline_env, monkeypatch, tmp_path) -> None:
    """验收1：初查 timeout——business 仍 ok、失败/公司事实保留、write skip、
    新 run 不命中旧失败（真实 manager→business→parse→缓存链）。"""
    from data_provider.cross_source_validator import SourceReadError

    async def route(self, tool, question, *, deadline=None):
        offline_env["ifind"].append((tool, question))
        if tool == "get_stock_financials" and "主营构成" in question:
            raise SourceReadError("ifind", "timeout")
        if tool == "get_stock_financials":
            return deepcopy(FINANCIAL_ENV)
        if "实际控制人" in question and "公司简介" in question:
            return _adapt_600176_env_to_688183(COMPANY_ENV)
        if tool == "get_stock_info":
            return deepcopy(DATED_INFO_ENV)
        return deepcopy(FLOW_ENV)

    manager, business_parse, dim_cache = _v8_business_env(offline_env, monkeypatch, tmp_path, products_route=lambda e, m: m.setattr(IfindFetcher, "_async_query", route, raising=False))
    raw, business = _run_business_once(manager, business_parse, dim_cache)
    assert business.status == "ok", "公司主营文本足以令业务正常完成（不为 cache 降级）"
    assert business.product_segments is not None and list(business.product_segments.errors) == ["timeout"]
    assert business.business_model.get("value"), "公司事实保留"
    assert dim_cache.load_cached_dim("688183", "business") is None, "write skip：含失败结果不落缓存"
    from src.core.research_run import ResearchRun
    with ResearchRun(120).bind():
        assert dim_cache.load_cached_dim("688183", "business") is None, "新 run 不命中（无旧坏记录可命中）"


def test_business_cache_legacy_error_record_read_miss(offline_env, monkeypatch, tmp_path) -> None:
    """验收2：注入合法版本/TTL/metadata 的旧 business 错误缓存记录 → read miss
    （不是因版本不符或写不进而假通过）。"""
    import datetime as _dt
    from src.deep_research_dims.dim_cache import FUNDAMENTAL_MAPPING_VERSION, SCHEMA_VERSION, _cache_path

    monkeypatch.setattr("src.deep_research_dims.dim_cache._CACHE_DIR", str(tmp_path / "dims"))
    import os as _os
    _os.makedirs(str(tmp_path / "dims"), exist_ok=True)
    payload = {
        "dim": "business", "status": "ok",
        "business_model": {"value": "玻纤产品生产销售"}, "main_products": {"value": "玻纤及电子布"},
        "product_segments": {"source": "ifind", "coverage": "partial", "items": [], "errors": ["timeout"]},
        "score": 60.0, "data_gaps": [], "narrative": "旧记录",
    }
    record = {"schema_version": SCHEMA_VERSION, "saved_at": _dt.datetime.now().isoformat(timespec="seconds"),
              "stock_code": "688183", "dim": "business", "payload": payload,
              "source_label_version": __import__("src.deep_research_dims.dim_cache", fromlist=["SOURCE_LABEL_VERSION"]).SOURCE_LABEL_VERSION,
              "mapping_version": FUNDAMENTAL_MAPPING_VERSION}
    import json as _json
    Path(_cache_path("688183", "business")).write_text(_json.dumps(record, ensure_ascii=False), encoding="utf-8")
    from src.deep_research_dims import dim_cache
    assert dim_cache.load_cached_dim("688183", "business") is None, "同版本旧坏记录 read guard 真实拒绝"

    # 同版本健康记录照常命中（不误杀）
    healthy = {**payload, "product_segments": {"source": "ifind", "coverage": "partial", "items": [], "errors": []}}
    record["payload"] = healthy
    Path(_cache_path("688183", "business")).write_text(_json.dumps(record, ensure_ascii=False), encoding="utf-8")
    loaded = dim_cache.load_cached_dim("688183", "business")
    assert loaded is not None and loaded["product_segments"]["errors"] == []


@pytest.mark.parametrize("prior_kind", ["timeout", "disqualified", "period_mismatch"])
def test_business_cache_candidate_failure_not_frozen(offline_env, monkeypatch, tmp_path, prior_kind) -> None:
    """验收3：Q3 正常空＋候选失败三态成对——初查 no_data 仍在、prior:<原因>
    结构化进入 business 产品记录、不可 cache；下一 run 产品恢复可正常研究。"""
    from data_provider.cross_source_validator import SourceReadError
    from src.core.research_run import ResearchRun

    state = {"fail": True}

    async def route(self, tool, question, *, deadline=None):
        offline_env["ifind"].append((tool, question))
        if tool == "get_stock_financials" and "主营构成" in question:
            if "2026-09-30" in question:
                return _adapt_600176_env_to_688183(PRODUCTS_Q3_EMPTY_ENV)
            if state["fail"]:
                if prior_kind == "period_mismatch":
                    return _adapt_600176_env_to_688183(PRODUCTS_Q3_EMPTY_ENV)  # 候选请求 H1 却回 Q3 空
                if prior_kind == "disqualified":
                    return _adapt_600176_env_to_688183(_prior_env_with_actual_period("20260930"))  # 同本期
                raise SourceReadError("ifind", "timeout")
            return _adapt_600176_env_to_688183(PRODUCTS_H1_ITEMS_ENV)
        if tool == "get_stock_financials":
            return deepcopy(FINANCIAL_ENV)
        if "实际控制人" in question and "公司简介" in question:
            return _adapt_600176_env_to_688183(COMPANY_ENV)
        if tool == "get_stock_info":
            return deepcopy(DATED_INFO_ENV)
        return deepcopy(FLOW_ENV)

    manager, business_parse, dim_cache = _v8_business_env(offline_env, monkeypatch, tmp_path, products_route=lambda e, m: m.setattr(IfindFetcher, "_async_query", route, raising=False))
    raw, business = _run_business_once(manager, business_parse, dim_cache)
    products = business.product_segments
    assert products is not None and products.no_data_periods == ("2026-09-30",), "本期合法 no_data 保留"
    prior_errors = [e for e in products.errors if e.startswith("prior:")]
    assert prior_errors, f"候选失败结构化进入产品记录: {products.errors}"
    expected = {"timeout": "prior:timeout", "disqualified": "prior:candidate_period_not_earlier", "period_mismatch": "prior:period_mismatch"}[prior_kind]
    assert expected in products.errors
    assert any(e.startswith("ifind:products_prior:") for e in raw["errors"]), "候选真实原因保留在 research errors"
    assert dim_cache.load_cached_dim("688183", "business") is None, "含候选失败不可 cache"

    state["fail"] = False  # 下一 run 候选恢复
    raw2, business2 = _run_business_once(manager, business_parse, dim_cache)
    products2 = business2.product_segments
    assert len(products2.items) == 3 and products2.earlier_period_reference is True, "新 run 恢复历史参考"
    assert products2.errors == [] and dim_cache.load_cached_dim("688183", "business") is not None, "健康结果可写可读"


def test_business_cache_healthy_states_cache_roundtrip(offline_env, monkeypatch, tmp_path) -> None:
    """验收5a：健康当前/较早参考/正常空覆盖照常缓存命中（不误杀）。"""
    current_items = deepcopy(PRODUCTS_H1_ITEMS_ENV)
    current_items["data"]["answer"] = current_items["data"]["answer"].replace("20260630", "20260930")
    for params in current_items["data"]["indicators_params"].values():
        if params.get("报告期") == "20260630":
            params["报告期"] = "20260930"
    h1_empty = _adapt_600176_env_to_688183(PRODUCTS_Q3_EMPTY_ENV)
    h1_empty["data"]["answer"] = h1_empty["data"]["answer"].replace("20260930", "20260630")
    for params in h1_empty["data"]["indicators_params"].values():
        if params.get("报告期") == "20260930":
            params["报告期"] = "20260630"
    modes = {
        "current": lambda q, question: deepcopy(_adapt_600176_env_to_688183(current_items)),
        "earlier": lambda q, question: deepcopy(_adapt_600176_env_to_688183(PRODUCTS_H1_ITEMS_ENV)),
        # v9：no_data 为真实 Q3 空 → H1 候选仍空（回执期随请求改为 2026-06-30），
        # 不再借 H1 明细成功路径重复 earlier 模式。
        "no_data": lambda q, question: deepcopy(
            h1_empty if "2026-06-30" in question else _adapt_600176_env_to_688183(PRODUCTS_Q3_EMPTY_ENV)),
    }
    for mode, responder in modes.items():
        async def route(self, tool, question, *, deadline=None, _r=responder):
            offline_env["ifind"].append((tool, question))
            if tool == "get_stock_financials" and "主营构成" in question:
                return _r(tool, question)
            if tool == "get_stock_financials":
                return deepcopy(FINANCIAL_ENV)
            if "实际控制人" in question and "公司简介" in question:
                return _adapt_600176_env_to_688183(COMPANY_ENV)
            if tool == "get_stock_info":
                return deepcopy(DATED_INFO_ENV)
            return deepcopy(FLOW_ENV)

        sub = tmp_path / mode
        manager, business_parse, dim_cache = _v8_business_env(offline_env, monkeypatch, sub, products_route=lambda e, m, _r=route: m.setattr(IfindFetcher, "_async_query", _r, raising=False))
        stock = "688183"  # 回执证券固定 688183；各模式用独立缓存目录隔离
        raw, business = _run_business_once(manager, business_parse, dim_cache, stock=stock)
        loaded = dim_cache.load_cached_dim(stock, "business")
        assert business.status == "ok", f"{mode} business 应正常完成: {business.degraded_reason}"
        assert loaded is not None, f"{mode} 健康状态应命中缓存: products={business.product_segments}"
        assert not loaded["product_segments"].get("errors"), mode
        if mode == "no_data":
            # v9：初查与候选均为有证据的正常空覆盖——两期如实记录、无失败、往返命中。
            segments = loaded["product_segments"]
            assert segments["items"] == [], f"no_data 两期均空: {segments}"
            assert segments["no_data_periods"] == ["2026-09-30", "2026-06-30"], segments
            assert segments["errors"] == [], segments
            assert segments == business.product_segments.model_dump(mode="json"), "缓存载荷与本次解析一致"


def test_business_cache_shared_entry_fundamentals_service_same_guard(offline_env, monkeypatch, tmp_path) -> None:
    """验收5b：独立 fundamentals_service 入口（真实 _research_dim 消费路径）与
    深研共用同一缓存资格——错误记录对两入口同样 miss，健康记录两入口可读。"""
    import datetime as _dt
    import os as _os
    import json as _json
    from src.deep_research_dims import dim_cache
    from src.deep_research_dims.dim_cache import FUNDAMENTAL_MAPPING_VERSION, SCHEMA_VERSION, _cache_path

    monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path / "dims"))
    _os.makedirs(str(tmp_path / "dims"), exist_ok=True)

    def inject(errors):
        payload = {"dim": "business", "status": "ok",
                   "business_model": {"value": "x"}, "main_products": {"value": "y"},
                   "product_segments": {"source": "ifind", "coverage": "partial", "items": [], "errors": errors},
                   "score": 60.0, "data_gaps": [], "narrative": "n"}
        record = {"schema_version": SCHEMA_VERSION, "saved_at": _dt.datetime.now().isoformat(timespec="seconds"),
                  "stock_code": "688183", "dim": "business", "payload": payload,
                  "source_label_version": __import__("src.deep_research_dims.dim_cache", fromlist=["SOURCE_LABEL_VERSION"]).SOURCE_LABEL_VERSION,
                  "mapping_version": FUNDAMENTAL_MAPPING_VERSION}
        Path(_cache_path("688183", "business")).write_text(_json.dumps(record, ensure_ascii=False), encoding="utf-8")

    # 深研入口（load_cached_dim 即 orchestrator try_cache 的读取点）
    inject(["timeout"])
    assert dim_cache.load_cached_dim("688183", "business") is None
    # 独立专项入口：真实 _research_dim 读取路径同样 miss（共享同一资格）
    from src.services import fundamentals_service as svc
    from src.agent.deep_research import explore_agents
    monkeypatch.setattr(explore_agents, "_run_explore", lambda *a, **k: {
        "ok": True, "data": {"business_model": {"value": "玻纤"}, "narrative": "n", "data_gaps": []}})
    monkeypatch.setattr("src.agent.tools.data_tools._fetcher_manager_singleton", DataFetcherManager())
    dim = svc._research_dim("business", "688183", "生益电子", force_refresh=False,
                            research={"company": {"MAINBUSINESS": "玻纤"}})
    assert (dim.get("product_segments") or {}).get("errors") != ["timeout"], "专项入口不命中旧失败缓存"

    # 健康记录两入口均可读
    inject([])
    assert dim_cache.load_cached_dim("688183", "business") is not None


def test_business_cache_cancel_not_supplier_failure(offline_env, monkeypatch, tmp_path) -> None:
    """验收6：候选 seam 取消不写 prior 失败资格（不与供应商失败混记）。"""
    from src.core.research_run import ResearchRun, ResearchStopped
    from data_provider.ifind_fundamental_adapter import ProductSegments

    run = ResearchRun(120)

    def stopping_prior(self, code, period=None, *, deadline=None):
        if period == "2026-06-30":
            run.stop("cancelled")
            raise ResearchStopped(run.reason_text)
        return ProductSegments(items=[], errors=[], no_data_periods=("2026-09-30",))

    _products_ifind_route(offline_env, monkeypatch, current_env=PRODUCTS_Q3_EMPTY_ENV, prior_env=None)
    monkeypatch.setattr(IfindFetcher, "product_segments", stopping_prior)
    manager, business_parse, dim_cache = _v8_business_env(offline_env, monkeypatch, tmp_path, products_route=lambda e, m: None)
    with run.bind():
        raw = manager.get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    products = (raw["research"].get("product_segments") or {})
    assert products.get("errors", []) == [], "取消不写成供应商失败"
    assert products.get("no_data_periods") == ["2026-09-30"], "本期无覆盖事实保留"


def test_business_cache_choice_policy_guard_inactive(offline_env, monkeypatch, tmp_path) -> None:
    """验收5c：Choice 主源/未启用 ifind 策略下守卫不生效（既有语义不变）。"""
    import datetime as _dt
    import os as _os
    import json as _json
    from src.config import get_config
    from src.deep_research_dims import dim_cache
    from src.deep_research_dims.dim_cache import SCHEMA_VERSION, _cache_path

    cfg = get_config()
    monkeypatch.setattr(cfg, "financial_primary_source", "choice")
    monkeypatch.setattr(cfg, "enable_ifind_mcp", False)
    monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path / "dims"))
    _os.makedirs(str(tmp_path / "dims"), exist_ok=True)
    payload = {"dim": "business", "status": "ok", "business_model": {"value": "x"},
               "product_segments": {"source": "ifind", "coverage": "partial", "items": [], "errors": ["timeout"]},
               "data_gaps": [], "narrative": "n"}
    record = {"schema_version": SCHEMA_VERSION, "saved_at": _dt.datetime.now().isoformat(timespec="seconds"),
              "stock_code": "688183", "dim": "business", "payload": payload,
              "source_label_version": __import__("src.deep_research_dims.dim_cache", fromlist=["SOURCE_LABEL_VERSION"]).SOURCE_LABEL_VERSION}
    Path(_cache_path("688183", "business")).write_text(_json.dumps(record, ensure_ascii=False), encoding="utf-8")
    # Choice 策略下不含 mapping_version 也不受本守卫影响（守卫不激活）
    loaded = dim_cache.load_cached_dim("688183", "business")
    assert loaded is not None, "Choice/legacy 策略既有读取语义不变"
