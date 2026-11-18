# -*- coding: utf-8 -*-
"""iFinD 深研基本面预算预留回归（真实 manager/stage0 编排 + 可控虚拟时钟）。

根因（``.claude/reviews/ifind-api-timeout-root-cause-20261118.md``）：60 秒
阶段预算被必要财务/估值/资金与可选主营串行耗尽后，公司资料 API 机会不足
1 秒、MCP 补缺只剩 0.323 秒且没有 dispatch。修复契约（方案第二部分）：

- 必要资料（财务/当前估值/正式行业/资金）优先不变；进入定向 PB 与主营
  等可选工作**之前**计算剩余时间 R 并预留 C=min(8s, 30%×R, 单次基本面
  预算)——定向 PB 实际查询与 single_fetch 等待、主营初查共享
  ``阶段 deadline - C``；公司资料与历史主营候选仍用原阶段 deadline。
- ``_ifind_research_omissions`` 的全局停止/返回检查用原阶段截止，不整段
  缩短；非深研消费者不套用预留。
- 公司 API 部分成功不可被 MCP 补名失败丢弃；不用证券简称/用户输入造
  公司全名。
- 上层 stage0（``build_shared_context``/``_safe_fundamental``）的 90 秒
  部署配置仍被更短外层 deadline/request_deadline 夹取，不绕过父截止。

时钟形态：``_Clock`` 虚拟单调钟由各供应商替身显式推进（只模拟耗时，不
sleep）；``data_provider.base.time``/资金与 HTTP 适配层的 monotonic 同步
替换。所有替身快速返回，线程 join 无真实等待。
"""
from __future__ import annotations

import json
import time as _real_time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from data_provider import ifind_http_daily_adapter as A
from data_provider.base import DataFetcherManager
from data_provider.ifind_fundamental_adapter import (
    CompanyFact,
    CompanyProfile,
    IfindFetcher,
)
from src.config import Config
from src.core.research_run import ResearchRun

from tests.test_ifind_http_financial_manager import (  # 复用既有真实回执构造
    _install_choice_native,
    _resolve_token_mock,
)
from tests.test_issue73_no_choice_deep_research import (
    DATED_INFO_ENV,
    FINANCIAL_ENV,
)

CODE = "688183"
IDENTITY = "688183.SH"
SH = ZoneInfo("Asia/Shanghai")
# T 判定参考日：与冻结日历 expected[0]=2026-10-30 同日且刚收盘（资金窗口
# 走当日防御形态——MCP 优先，无未定稿 API 出站浪费预算）。
_REF_NOW = datetime(2026, 10, 30, 15, 30, tzinfo=SH)
from datetime import date as _date  # noqa: E402  （日历替身形态）

_SESSIONS = [
    _date(2026, 10, 30), _date(2026, 10, 29), _date(2026, 10, 28),
    _date(2026, 10, 27), _date(2026, 10, 26), _date(2026, 10, 23),
    _date(2026, 10, 22), _date(2026, 10, 21), _date(2026, 10, 20),
    _date(2026, 10, 19), _date(2026, 10, 16),
]

# 供应商需求（虚拟秒）——按根因时间线量级设定：前序必要资料 ~48 秒后，
# 可选主营需求 30 秒不可达成（两版一致超时），公司 HTTP 需求 1.9 秒
# （root cause 实测 1.87~2.1 秒）、公司 MCP name 需求 2.5 秒。
NEED_FINANCE_HTTP = 12.0
NEED_FINANCE_MCP = 11.0
NEED_INFO_MCP = 15.0
NEED_FLOW_MCP = 10.0
NEED_PRODUCTS = 30.0
NEED_PB_DIRECTED = 4.0  # 定向 PB 补查需求（消耗可选前序截止）
NEED_COMPANY_HTTP = 1.9
NEED_COMPANY_MCP = 2.5

_Q3 = {
    "revenue": 575.1, "net_profit": 92.6, "roe": 9.0, "gross_margin": 30.0,
    "revenue_yoy": 15.0, "net_profit_yoy": 36.1, "assets": 100.0, "liabilities": 40.0,
}


@pytest.fixture()
def stage0_no_egress(monkeypatch):
    """仅 stage0 确定性用例启用的零外部出站断言（复用 ``_network_guard``
    工厂；非 autouse——本模块其余测试走真实 manager 编排，其内部公共
    数据源出站由各自替身/降级契约承载，不受此断言约束）。"""
    import socket as _socket

    from tests._network_guard import (
        guarded_connect_factory,
        guarded_create_connection_factory,
    )

    leaks: list[str] = []
    monkeypatch.setattr(_socket.socket, "connect", guarded_connect_factory(leaks))
    monkeypatch.setattr(_socket, "create_connection", guarded_create_connection_factory(leaks))
    return leaks


class _Clock:
    """虚拟单调钟：供应商替身显式推进；被测编排只读。初值取真实钟量级。"""

    def __init__(self) -> None:
        self.now = _real_time.monotonic()

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeTime:
    """base.time 替身：monotonic 走虚拟钟，其余属性转发真实 time 模块。"""

    def __init__(self, clock: _Clock) -> None:
        self._clock = clock

    def monotonic(self) -> float:
        return self._clock.monotonic()

    def __getattr__(self, name: str) -> Any:
        return getattr(_real_time, name)


def _finance_payload() -> bytes:
    from tests.test_ifind_http_financial_manager import _http_payload

    return _http_payload(_Q3, IDENTITY, "20261031")


def _company_payload() -> bytes:
    return json.dumps({"errorcode": 0, "errmsg": "", "tables": [{
        "thscode": IDENTITY, "table": {
            "ths_corp_profile_stock": ["测试公司主营概况"],
            "ths_actual_controller_stock": ["测试控制人"],
            "ths_actual_controller_type_stock": ["国资"],
            "ths_mo_product_name_stock": ["产品甲、产品乙"],
            "ths_main_businuess_stock": ["测试行业"]}}]}).encode()


class _TimedEnv:
    """配置 + 虚拟时钟 + 按需求耗时推进的供应商替身（HTTP curl / MCP）。

    ``http_enabled``/``http_token`` 供 HTTP 不参与形态（复审阻断 3）复用；
    ``pb_needs_supplement`` 触发定向 PB 补查编排（估值批次无合格 PB）。"""

    def __init__(self, monkeypatch, clock: _Clock, *, company_mcp_error: str | None = None,
                 http_enabled: bool = True, http_token: str | None = "test-refresh",
                 pb_needs_supplement: bool = False, products_need: float = NEED_PRODUCTS):
        config = Config(data_source_profile="choice", deep_research_cross_validate=False)
        config.enable_ifind_mcp = True
        config.ifind_mcp_endpoint = "https://fixture.invalid/mcp"
        config.ifind_mcp_token = "test-only"
        config.ifind_mcp_timeout_seconds = 30.0
        config.financial_primary_source = "ifind"
        config.enable_ifind_http_api = http_enabled
        config.ifind_http_refresh_token = http_token
        config.ifind_http_timeout_seconds = 30.0
        config.fundamental_fetch_timeout_seconds = 20.0
        monkeypatch.setattr(Config, "_instance", config)
        self.pb_needs_supplement = pb_needs_supplement
        self.pb_deadlines: list[float | None] = []
        self.clock = clock
        self.outbound: list[dict] = []
        self.company_api_shares: list[float] = []
        self.finance_shares: list[float] = []
        self.products_deadlines: list[float] = []
        self.company_mcp_budgets: list[float] = []

        from src.core import trading_calendar as _tc

        monkeypatch.setattr(_tc, "recent_completed_trading_sessions",
                            lambda market, count, current_time=None, **_: _SESSIONS[:max(0, min(count, len(_SESSIONS)))])
        monkeypatch.setattr(_tc, "get_market_now",
                            lambda market, current_time=None: _REF_NOW)
        _resolve_token_mock(monkeypatch)
        _install_choice_native(monkeypatch, IDENTITY)

        from src.patches.eastmoney_patch import request_deadline

        def curl(url, headers, body, *, label):
            dl = request_deadline.get()
            share = (dl - clock.monotonic()) if dl is not None else 999.0
            self.outbound.append({"channel": "api", "label": label, "share": round(share, 3)})
            if label == "company_profile":
                self.company_api_shares.append(share)
                if share < NEED_COMPANY_HTTP:
                    raise TimeoutError("curl deadline exhausted")
                clock.advance(NEED_COMPANY_HTTP)
                return 200, _company_payload()
            if label == "finance":
                self.finance_shares.append(share)
                if share <= 0:
                    raise TimeoutError("curl deadline exhausted")
                clock.advance(min(NEED_FINANCE_HTTP, share))
                return 200, _finance_payload()
            raise AssertionError(f"unexpected curl label {label}")

        monkeypatch.setattr(A, "_http_api_curl_post", curl)

        async def query(self_fetcher, tool, question, *, deadline=None):
            self.outbound.append({"channel": "mcp", "tool": tool})
            if tool == "get_stock_financials":
                clock.advance(NEED_FINANCE_MCP)
                return json.loads(json.dumps(FINANCIAL_ENV))
            if tool == "get_stock_info":
                clock.advance(NEED_INFO_MCP)
                return json.loads(json.dumps(DATED_INFO_ENV))
            raise AssertionError(f"unexpected mcp tool {tool}")

        monkeypatch.setattr(IfindFetcher, "_async_query", query, raising=False)

        def flow(self_fetcher, code, days=12, *, deadline=None):
            budget = (deadline - clock.monotonic()) if deadline is not None else 999.0
            if budget < NEED_FLOW_MCP:
                clock.advance(max(budget, 0.0))
                from data_provider.cross_source_validator import SourceReadError

                raise SourceReadError("ifind", "timeout")
            clock.advance(NEED_FLOW_MCP)
            return [(session.isoformat(), float(index) + 100.0)
                    for index, session in enumerate(_SESSIONS)]

        monkeypatch.setattr(IfindFetcher, "fetch_main_inflow_series", flow)

        def products(self_fetcher, code, period=None, *, deadline=None):
            self.products_deadlines.append(deadline)
            budget = (deadline - clock.monotonic()) if deadline is not None else 999.0
            from data_provider.cross_source_validator import SourceReadError
            from data_provider.ifind_fundamental_adapter import ProductSegment, ProductSegments

            if budget >= products_need:
                clock.advance(products_need)
                return ProductSegments(
                    items=[ProductSegment(name="测试业务", rank=1,
                                          report_date=(period or "2026-09-30")[:10])],
                    errors=[])
            clock.advance(max(budget, 0.0))
            raise SourceReadError("ifind", "timeout")

        monkeypatch.setattr(IfindFetcher, "product_segments", products)

        def company_mcp(self_fetcher, code, *, deadline=None):
            budget = (deadline - clock.monotonic()) if deadline is not None else 999.0
            self.company_mcp_budgets.append(round(budget, 3))
            from data_provider.cross_source_validator import SourceReadError

            if budget < NEED_COMPANY_MCP:
                clock.advance(max(budget, 0.0))
                raise SourceReadError("ifind", "timeout")
            clock.advance(NEED_COMPANY_MCP)
            if company_mcp_error is not None:
                raise SourceReadError("ifind", company_mcp_error)
            return CompanyProfile(
                facts={"name": CompanyFact(value="测试全称股份有限公司", observed_at="2026-10-30")},
                errors=[])

        monkeypatch.setattr(IfindFetcher, "company_profile", company_mcp)

        if pb_needs_supplement:
            # 复审阻断 3 编排反例：估值批次无合格 PB（真实拒绝原因）+
            # 定向补查消耗可选前序截止后仍无 PB。
            def info_snapshot(self_fetcher, code, *, deadline=None, current_time=None):
                self.outbound.append({"channel": "mcp", "tool": "get_stock_info"})
                clock.advance(NEED_INFO_MCP)
                return {"valuation": {}, "industry": {"name": "测试行业"},
                        "pb_rejection": ("no_qualified_reading",), "requested_date": None}

            monkeypatch.setattr(IfindFetcher, "current_info_snapshot", info_snapshot)

        def directed_pb(self_fetcher, code, *, deadline=None, current_time=None):
            self.pb_deadlines.append(deadline)
            budget = (deadline - clock.monotonic()) if deadline is not None else 999.0
            clock.advance(min(NEED_PB_DIRECTED, max(budget, 0.0)))
            return {"pb_ratio": None, "rejection": ("no_qualified_reading",),
                    "requested_date": None}

        monkeypatch.setattr(IfindFetcher, "directed_pb_snapshot", directed_pb)


@pytest.fixture()
def virtual_clock(monkeypatch):
    import data_provider.base as base_mod
    import data_provider.capital_flow_provider as cfp

    clock = _Clock()
    monkeypatch.setattr(base_mod, "time", _FakeTime(clock))
    monkeypatch.setattr(cfp, "monotonic", clock.monotonic)
    monkeypatch.setattr(A, "monotonic", clock.monotonic)
    return clock


# ===== 60 秒预算饥饿（根因场景）：红→绿 =====

def test_company_api_starved_at_60s_gets_reserved_budget(monkeypatch, virtual_clock):
    """60 秒阶段预算：必要资料 ~48 秒后，可选主营（30 秒需求）不得再把
    公司资料挤到不足 1 秒——预留 C 后主营初查提前超时（真实失败共享），
    公司 API 获得 ≥1.9 秒的真实机会并保留五项事实；MCP 补名失败不丢弃
    API 事实，真实补缺错误随 research 披露。"""
    clock = virtual_clock
    env = _TimedEnv(monkeypatch, clock, company_mcp_error="timeout")
    stage_deadline = clock.monotonic() + 60.0
    with ResearchRun(600).bind():
        result = DataFetcherManager().get_fundamental_context(
            CODE, budget_seconds=60, include_sector_flows=False)
    assert clock.monotonic() <= stage_deadline + 0.5, "总时长不超过该次配置派生的 deadline"
    company_outbound = [c for c in env.outbound if c.get("label") == "company_profile"]
    assert company_outbound, "公司 API 实际出站（不再被可选前序挤掉全部机会）"
    assert env.company_api_shares[0] >= NEED_COMPANY_HTTP, \
        f"公司 API 份额 {env.company_api_shares[0]:.3f}s 必须覆盖真实请求需求"
    research = result["research"]
    company_payload = research.get("company") or {}
    for indicator in ("COMPROFILE", "MAINBUSINESS", "MAINPRODUCT", "AHOLDER", "AHOLDERTYPE"):
        assert company_payload.get(indicator), f"API 五项事实保留：{indicator}"
    errors = result.get("errors") + research.get("errors", [])
    assert any("mcp:company:timeout" in item for item in errors), \
        f"MCP 补名失败的真实错误随披露（不被 API 部分成功掩盖）: {errors}"
    assert not company_payload.get("NAME"), \
        "API 无名称指标、MCP 补名失败：不得用简称/输入造公司全名"
    assert any("ifind:products:timeout" in item for item in errors), \
        "主营初查真实失败共享（阻断默认年报旁路）"
    # 主营初查收到缩短后的可选前序截止（原阶段 - C=3.6s）。
    assert env.products_deadlines, "主营初查发生"
    assert env.products_deadlines[0] == pytest.approx(stage_deadline - 3.6, abs=0.6), \
        "主营初查使用 阶段deadline - C（预留公司份额）"


# ===== 90 秒部署配置：分配关系与不退化 =====

def test_company_budget_at_90s_keeps_optional_work_and_reservation(monkeypatch, virtual_clock):
    """90 秒（本部署显式配置）：主营（30 秒需求）在预留后仍可完成；公司
    获得预留份额并完整落位（含 MCP name）；财务半预算关系保持
    （finance 尝试 deadline ≈ 阶段 deadline - budget/2）；总时长不超 90。"""
    clock = virtual_clock
    env = _TimedEnv(monkeypatch, clock)
    stage_deadline = clock.monotonic() + 90.0
    with ResearchRun(600).bind():
        result = DataFetcherManager().get_fundamental_context(
            CODE, budget_seconds=90, include_sector_flows=False)
    assert clock.monotonic() <= stage_deadline + 0.5
    assert env.finance_shares and 0 < env.finance_shares[0] < 45.0, \
        f"财务快照尝试受半预算+既有预留链约束（{env.finance_shares[0]:.1f}s），不因 90 秒给每个请求重开全额"
    errors = result.get("errors") + (result["research"].get("errors") or [])
    assert not any("ifind:products:" in item for item in errors), \
        "90 秒下主营（30 秒需求）不受预留挤压而失败"
    company_payload = result["research"].get("company") or {}
    assert company_payload.get("NAME") == "测试全称股份有限公司", "公司完整落位（含 name）"
    for indicator in ("COMPROFILE", "MAINBUSINESS", "MAINPRODUCT", "AHOLDER", "AHOLDERTYPE"):
        assert company_payload.get(indicator)
    assert env.products_deadlines[0] == pytest.approx(stage_deadline - 8.0, abs=0.6), \
        "C=min(8s, 30%×R, 20s)=8s：主营初查使用 阶段deadline - 8"


# ===== 零预算不出站 =====

def test_no_outbound_after_budget_exhausted(monkeypatch, virtual_clock):
    """必要阶段已耗满（预算 1 秒 < 财务需求 12 秒）：后续估值/主营/公司
    全部零出站（保持既有不出站语义），不因补查重置 deadline。"""
    clock = virtual_clock
    env = _TimedEnv(monkeypatch, clock)
    with ResearchRun(600).bind():
        DataFetcherManager().get_fundamental_context(
            CODE, budget_seconds=1, include_sector_flows=False)
    assert not env.products_deadlines, "预算耗尽后主营零出站"
    assert not env.company_api_shares, "预算耗尽后公司零出站"
    assert not [c for c in env.outbound if c.get("tool") == "get_stock_info"], \
        "预算耗尽后估值批次零出站"


# ===== 非深研消费者不套用预留 =====

def test_non_deep_consumer_products_keep_full_deadline(monkeypatch, virtual_clock):
    """非深研（无 run）false-scope 消费者：主营初查仍用原阶段 deadline
    （不缩短），预留语义不外溢。"""
    clock = virtual_clock
    env = _TimedEnv(monkeypatch, clock)
    stage_deadline = clock.monotonic() + 60.0
    result = DataFetcherManager().get_fundamental_context(
        CODE, budget_seconds=60, include_sector_flows=False)
    assert env.products_deadlines, "主营初查发生"
    assert env.products_deadlines[0] == pytest.approx(stage_deadline, abs=0.5), \
        "非深研：主营初查不缩短（无公司预留）"


# ===== stage0 上层截止夹取（90 秒配置不绕过父截止） =====

def test_stage0_shorter_outer_deadline_truncates_90s_config(monkeypatch, stage0_no_egress):
    """配置 90 秒 + 外层报告 deadline 只剩 12 秒：stage0（build_shared_
    context → _safe_fundamental）传给 manager 的预算必须 ≤12 秒——配置
    90 不绕过更短的上层截止（request_deadline 与 deadline 参数两路径）。

    本用例只验证阶段零→基本面的截止传递：旁路行情/历史/概念查询在用例内
    替换为无出站的固定轻量结果（真实链路的供应商行为由各自专项测试承载），
    并以 network_guard 显式断言零外部出站——未标 network 的确定性用例不依
    赖实时供应商。"""
    from src.config import Config as _Cfg
    import src.deep_research_dims.context as ctx_mod
    from src.deep_research_dims.context import build_shared_context
    from src.patches.eastmoney_patch import request_deadline

    config = _Cfg(data_source_profile="choice", deep_research_cross_validate=False)
    config.enable_ifind_mcp = True
    config.ifind_mcp_endpoint = "https://fixture.invalid/mcp"
    config.ifind_mcp_token = "test-only"
    config.financial_primary_source = "ifind"
    config.fundamental_stage_timeout_seconds = 90.0
    config.fundamental_fetch_timeout_seconds = 20.0
    monkeypatch.setattr(_Cfg, "_instance", config)
    # 旁路三路替换为无出站固定轻量结果（worker ctx 保持空默认即可完成编排）。
    monkeypatch.setattr(ctx_mod, "_safe_quote", lambda code, ctx: {})
    monkeypatch.setattr(ctx_mod, "_safe_history", lambda code, days, ctx: [])
    monkeypatch.setattr(ctx_mod, "_safe_concepts", lambda code, deadline, ctx: {})
    received: list[float] = []

    class _RecordingManager:
        def get_fundamental_context(self, code, budget_seconds=None, include_sector_flows=True):
            received.append(float(budget_seconds))
            return {"valuation": {"data": {}}, "growth": {"data": {}},
                    "boards": {"data": {}}, "research": {}, "capital_flow": {},
                    "errors": []}

    from src.agent.tools import data_tools

    monkeypatch.setattr(data_tools, "_get_fetcher_manager", lambda: _RecordingManager())

    # 路径一：deadline 参数更短（12 秒）。
    from time import monotonic

    ctx = build_shared_context(CODE, "测试标的", deadline=monotonic() + 12.0)
    assert received, "manager 被调用"
    assert received[-1] <= 12.1, f"90 秒配置被外层 deadline 夹取：{received[-1]:.2f}"
    assert ctx.fundamental is not None

    # 路径二：request_deadline contextvar 更短（12 秒）、无显式 deadline 参数。
    received.clear()
    token = request_deadline.set(monotonic() + 12.0)
    try:
        build_shared_context(CODE, "测试标的")
    finally:
        request_deadline.reset(token)
    assert received and received[-1] <= 12.1, \
        f"90 秒配置被 request_deadline 夹取：{received[-1]:.2f}"
    assert not stage0_no_egress, f"确定性 stage0 用例不得有外部出站：{stage0_no_egress}"


# ===== 复审阻断 3：公司预留仅限 HTTP 实际参与形态 =====

def test_http_disabled_deep_scope_products_keep_stage_deadline(monkeypatch, virtual_clock):
    """HTTP 关闭的 iFinD 主源深研：公司 HTTP 不参与（无预留受益方），主营
    初查仍用原阶段截止（不被 7.2 秒级的预留提前截断）。"""
    clock = virtual_clock
    env = _TimedEnv(monkeypatch, clock, http_enabled=False)
    stage_deadline = clock.monotonic() + 60.0
    with ResearchRun(600).bind():
        DataFetcherManager().get_fundamental_context(
            CODE, budget_seconds=60, include_sector_flows=False)
    assert env.products_deadlines, "主营初查发生"
    assert env.products_deadlines[0] == pytest.approx(stage_deadline, abs=0.5), \
        f"HTTP 关闭：主营初查不缩短（实测比阶段截止早 {stage_deadline - env.products_deadlines[0]:.2f}s）"
    assert env.company_api_shares == [], "HTTP 关闭：公司 API 零出站"


def test_http_credentials_missing_deep_scope_products_keep_stage_deadline(monkeypatch, virtual_clock):
    """HTTP 开启但凭据缺失：与关闭同形态——主营初查用原阶段截止、公司
    API 零出站。"""
    clock = virtual_clock
    env = _TimedEnv(monkeypatch, clock, http_token=None)
    stage_deadline = clock.monotonic() + 60.0
    with ResearchRun(600).bind():
        DataFetcherManager().get_fundamental_context(
            CODE, budget_seconds=60, include_sector_flows=False)
    assert env.products_deadlines and env.products_deadlines[0] == pytest.approx(stage_deadline, abs=0.5)
    assert env.company_api_shares == []


def test_pb_exhausts_optional_deadline_company_keeps_stage_deadline(monkeypatch, virtual_clock):
    """完整编排反例（90 秒）：HTTP 参与形态下定向 PB 缺失并消耗可选前序
    截止、主营初查超时（真实失败共享）后，公司资料仍按原全局截止出站并完
    整落位（含 name）。"""
    clock = virtual_clock
    env = _TimedEnv(monkeypatch, clock, pb_needs_supplement=True, products_need=60.0)
    stage_deadline = clock.monotonic() + 90.0
    with ResearchRun(600).bind():
        result = DataFetcherManager().get_fundamental_context(
            CODE, budget_seconds=90, include_sector_flows=False)
    assert env.pb_deadlines, "定向 PB 补查发生（估值批次无合格 PB）"
    assert env.pb_deadlines[0] == pytest.approx(stage_deadline - 8.0, abs=0.6), \
        "定向 PB 使用可选前序截止（阶段 - C=8s）"
    pb_meta = (result["valuation"].get("field_meta") or {}).get("pb_ratio") or {}
    pb_reasons = [item.get("reason_code") for item in (pb_meta.get("source_errors") or [])]
    assert any(str(r).startswith("directed_pb:") for r in pb_reasons), \
        f"PB 补查失败原因保留在字段证据：{pb_reasons}"
    errors = result.get("errors") + (result["research"].get("errors") or [])
    assert any("ifind:products:timeout" in item for item in errors), \
        f"主营真实失败仍随报告共享：{errors}"
    company_payload = result["research"].get("company") or {}
    assert company_payload.get("NAME") == "测试全称股份有限公司", \
        "公司按原全局截止出站并完整落位（含 name）"
    for indicator in ("COMPROFILE", "MAINBUSINESS", "MAINPRODUCT", "AHOLDER", "AHOLDERTYPE"):
        assert company_payload.get(indicator), f"公司五项事实保留：{indicator}"
