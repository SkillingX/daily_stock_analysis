# -*- coding: utf-8 -*-
"""标准 HTTP 财务/公司快照的深研门回归（两主源真实 manager 入口）。

审查收敛锁定：用户仅授权个股深研使用标准 HTTP 财务/公司快照——

- **非深研（普通基本面专项，无 ResearchRun 绑定）完全不因
  ``ENABLE_IFIND_HTTP_API`` 改变调用、预算和缓存**：iFinD 主源财务走
  MCP 快照直调（HTTP 接入前旧行为）；Choice 主源交叉验证段保持旧行为
  （MCP 开 → MCP 快照直调；MCP 关 → 段不进零出站）；公司资料分支本就
  仅深研进入；交叉验证 HTTP 预算份额不虚占；``financial_cache_contract``
  不含 HTTP 段（开关翻转不失效非深研缓存）；
- **深研（ResearchRun 绑定）保持隔离与回退**：API 优先/失败回 MCP/
  缓存键携带 ``-http-fin-v1`` 段（开关翻转身份隔离）；
- 公司资料唯一生产调用 ``include_company=deep_scope`` 天然深研门，
  非深研零 company API 出站。

全部离线：供应商替身（curl stub / IfindFetcher 方法 stub），真实 manager
聚合入口（``DataFetcherManager.get_fundamental_context``）。
"""
from __future__ import annotations

import pytest

from data_provider import ifind_http_daily_adapter as A
from data_provider.base import DataFetcherManager
from tests.ifind_daily_testutil import http_enabled_config  # noqa: F401  （deep 环境基础）
from tests.test_choice_integration import choice_transport  # noqa: F401  （fixture：完整 Choice 传输；测试参数注入同名 fixture）
from tests.test_ifind_http_financial_manager import (
    _freeze_calendar,
    _http_payload,
    _install_choice_native,
    _install_config,
    _resolve_token_mock,
    _stub_capital_chain,
    _stub_mcp_methods,
)

CODE = "600519"
IDENTITY = "600519.SH"
Q3 = {"revenue": 575.1, "net_profit": 92.6, "roe": 9.0, "gross_margin": 30.0,
      "revenue_yoy": 15.0, "net_profit_yoy": 36.1, "assets": 100.0, "liabilities": 39.0}


def _finance_payload() -> bytes:
    return _http_payload(Q3, IDENTITY, "20261031")


class _CurlGuard:
    """拦截标准 HTTP API 出站（finance/moc/company 等全部 label）。"""

    def __init__(self):
        self.calls: list[str] = []

    def install(self, monkeypatch, *, allow_labels: tuple = (), reply: bytes | None = None):
        guard = self
        # 深研资金流 API（第十一轮）：date_sequence 形态的合法响应（11 个
        # 完成交易日全值），与 finance/company 的 basic_data_service 分流。
        import json as _json
        flow_reply = _json.dumps({"errorcode": 0, "errmsg": "", "tables": [{
            "thscode": "688183.SH",
            "time": ["2026-10-30", "2026-10-29", "2026-10-28", "2026-10-27", "2026-10-26",
                     "2026-10-23", "2026-10-22", "2026-10-21", "2026-10-20", "2026-10-19", "2026-10-16"],
            "table": {"ths_net_inflow_amt_stock": [100.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]}}]}).encode()

        def guarded(url, headers, body, *, label):
            if label not in allow_labels:
                raise AssertionError(f"非授权出站：label={label}")
            guard.calls.append(label)
            if label == "flow":
                return 200, flow_reply
            return 200, reply if reply is not None else _finance_payload()

        monkeypatch.setattr(A, "_http_api_curl_post", guarded)
        return guard


class _McpSnapshotStub:
    """IfindFetcher.financial_snapshot 方法级替身（记录调用，MCP 原路径证据）。"""

    def __init__(self, result=None):
        self.calls: list[tuple] = []
        self.result = result if result is not None else {}

    def install(self, monkeypatch):
        stub = self

        def fake(self_fetcher, code, period, *, deadline):
            stub.calls.append((code, period, deadline))
            return dict(stub.result)

        monkeypatch.setattr(
            "data_provider.ifind_fundamental_adapter.IfindFetcher.financial_snapshot", fake)
        return stub


@pytest.fixture()
def base_env(monkeypatch):
    _freeze_calendar(monkeypatch)
    _resolve_token_mock(monkeypatch)
    _stub_capital_chain(monkeypatch)


def test_ifind_primary_non_deep_zero_http_mcp_path(base_env, monkeypatch):
    """iFinD 主源非深研 + HTTP 开 + MCP 开：财务走 MCP 快照直调（旧行为），
    零标准 HTTP API 出站。"""
    _install_config(monkeypatch, mcp=True, http=True, primary="ifind")
    _stub_mcp_methods(monkeypatch)
    mcp_stub = _McpSnapshotStub().install(monkeypatch)
    guard = _CurlGuard().install(monkeypatch)
    _install_choice_native(monkeypatch, IDENTITY)
    DataFetcherManager().get_fundamental_context(CODE, budget_seconds=60)
    assert guard.calls == [], "非深研不得发起标准 HTTP API 出站"
    assert mcp_stub.calls and mcp_stub.calls[0][1] is None, "iFinD 主源非深研财务走 MCP 直调（主源多期语义）"


def test_ifind_primary_deep_http_participates(base_env, monkeypatch):
    """iFinD 主源深研 + HTTP 开：API 参与且采纳（隔离与回退语义保持）。"""
    _install_config(monkeypatch, mcp=True, http=True, primary="ifind")
    _stub_mcp_methods(monkeypatch)
    mcp_stub = _McpSnapshotStub().install(monkeypatch)
    # 深研身份公司资料分支（include_company=deep_scope）也经 API 出站（现行为）。
    guard = _CurlGuard().install(monkeypatch, allow_labels=("finance", "company_profile", "flow"))
    _install_choice_native(monkeypatch, IDENTITY)
    from src.core.research_run import ResearchRun
    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context(CODE, budget_seconds=60, include_sector_flows=False)
    assert "finance" in guard.calls, "深研身份 API 参与"
    assert mcp_stub.calls == [], "API 采纳时不出 MCP 兜底"
    growth = result["growth"]["data"]
    assert growth["revenue"] == 575.1, "API 快照进入财务主值"


def test_choice_primary_non_deep_mcp_on_zero_http(base_env, monkeypatch):
    """Choice 主源非深研 + MCP 开 + HTTP 开：交叉验证财务走 MCP 快照直调
    （旧行为），零标准 HTTP API 出站。"""
    _install_config(monkeypatch, mcp=True, http=True, primary="choice")
    _stub_mcp_methods(monkeypatch)
    mcp_stub = _McpSnapshotStub().install(monkeypatch)
    guard = _CurlGuard().install(monkeypatch)
    _install_choice_native(monkeypatch, IDENTITY)
    DataFetcherManager().get_fundamental_context(CODE, budget_seconds=60)
    assert guard.calls == [], "非深研不得发起标准 HTTP API 出站"
    assert mcp_stub.calls and mcp_stub.calls[0][1] is not None, "Choice 主源非深研交叉验证财务走 MCP 直调（具体期）"


def test_choice_primary_non_deep_mcp_off_zero_outbound(base_env, monkeypatch):
    """Choice 主源非深研 + MCP 关 + HTTP 开：旧行为（交叉验证段不进）——
    零 API 出站、零 MCP 快照出站，预算不含 HTTP 份额。"""
    _install_config(monkeypatch, mcp=False, http=True, primary="choice")
    _stub_mcp_methods(monkeypatch)
    mcp_stub = _McpSnapshotStub().install(monkeypatch)
    guard = _CurlGuard().install(monkeypatch)
    _install_choice_native(monkeypatch, IDENTITY)
    DataFetcherManager().get_fundamental_context(CODE, budget_seconds=60)
    assert guard.calls == [] and mcp_stub.calls == [], "MCP 关非深研零 iFinD 财务出站（旧行为）"


def test_choice_primary_deep_http_participates(base_env, monkeypatch):
    """Choice 主源深研 + MCP 关 + HTTP 开：fallback API 参与并采纳
    （MCP 未配置即跳过兜底——既有隔离与回退语义保持）。"""
    _install_config(monkeypatch, mcp=False, http=True, primary="choice")
    _stub_mcp_methods(monkeypatch)
    mcp_stub = _McpSnapshotStub().install(monkeypatch)
    guard = _CurlGuard().install(monkeypatch, allow_labels=("finance",))
    _install_choice_native(monkeypatch, IDENTITY)
    from src.core.research_run import ResearchRun
    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context(CODE, budget_seconds=60, include_sector_flows=False)
    assert "finance" in guard.calls and mcp_stub.calls == [], "深研 API 参与、MCP 未配置不兜底"
    field_meta = (result.get("growth") or {}).get("field_meta") or {}
    supplement = [record for record in field_meta.values()
                  if isinstance(record, dict) and record.get("source") == "ifind"]
    assert supplement, "API 快照按既有同期补充/核验契约进入（source=ifind）"


def test_non_deep_cache_identity_survives_http_toggle(choice_transport, monkeypatch):  # noqa: F811  参数注入 fixture
    """非深研缓存身份不含 HTTP 段（同一 manager 实例 + 回执齐备零错误真实
    冷写，two_way 已验证的可缓存构造）：HTTP 开冷写（iFinD MCP 主源路径、
    零标准 HTTP API 出站）→ 翻转关热读命中（iFinD 数据源零新增、结果一致）
    → 翻转回开仍命中（两方向翻转均不失效）。普通基本面专项不因该开关改变
    调用与缓存身份。"""
    from copy import deepcopy
    from src.config import get_config
    from src.core import trading_calendar as _tc
    from tests.test_issue73_no_choice_deep_research import (
        COMPANY_ENV,
        DATED_INFO_ENV,
        FINANCIAL_ENV,
        FLOW_ENV,
        _VERTICAL_IFIND_SESSIONS,
    )

    ifind_calls: list[str] = []
    monkeypatch.setattr(_tc, "recent_completed_trading_sessions",
                        lambda market, count, current_time=None, **_: _VERTICAL_IFIND_SESSIONS[:max(0, min(count, len(_VERTICAL_IFIND_SESSIONS)))] or None)
    choice_transport.enable_ifind_mcp = True
    choice_transport.ifind_mcp_endpoint = "https://fixture.invalid/mcp"
    choice_transport.ifind_mcp_token = "test-only"
    choice_transport.financial_primary_source = "ifind"
    choice_transport.enable_ifind_http_api = True
    choice_transport.ifind_http_refresh_token = "test-refresh"
    choice_transport.ifind_http_timeout_seconds = 30.0

    company_env = deepcopy(COMPANY_ENV)
    company_env["data"]["answer"] = company_env["data"]["answer"].replace("600176.SH|中国巨石", "688183.SH|生益电子")

    async def ifind_route(self, tool, question, *, deadline=None):
        ifind_calls.append(tool)
        if tool == "get_stock_financials":
            return deepcopy(FINANCIAL_ENV)
        if tool == "get_stock_info":
            if "实际控制人" in question and "公司简介" in question:
                return deepcopy(company_env)
            return deepcopy(DATED_INFO_ENV)
        return deepcopy(FLOW_ENV)

    monkeypatch.setattr(
        "data_provider.ifind_fundamental_adapter.IfindFetcher._async_query", ifind_route, raising=False)
    monkeypatch.setattr(
        "data_provider.ifind_fundamental_adapter.IfindFetcher.get_instance",
        classmethod(lambda cls: __import__("data_provider.ifind_fundamental_adapter", fromlist=["IfindFetcher"]).IfindFetcher("https://fixture.invalid/mcp", "test-only")))

    def successful_products(self, code, period=None, *, deadline=None):
        from data_provider.ifind_fundamental_adapter import ProductSegment, ProductSegments
        return ProductSegments(items=[ProductSegment(name="覆铜板", rank=1, report_date="2026-09-30")], errors=[])

    monkeypatch.setattr(
        "data_provider.ifind_fundamental_adapter.IfindFetcher.product_segments", successful_products)
    _resolve_token_mock(monkeypatch)
    from data_provider.choice_adapter import ChoiceAdapter
    base_native = ChoiceAdapter._native_request

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
            raw_data = raw.get("data")
            if isinstance(raw_data, dict):
                indicators = raw.get("indicators") or []
                raw_data[args[0]] = [values.get(k, v) for k, v in zip(indicators, raw_data[args[0]])]
        return raw

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(healthy_native))

    manager = DataFetcherManager()  # 复用同一实例：进程内缓存真实生效
    code = "688183"  # 回执 fixture 的同票证券（_same_security 校验要求一致）
    first = manager.get_fundamental_context(code, budget_seconds=60, include_sector_flows=False)
    assert first.get("errors") == [], f"回执齐备零错误（缓存写入前提）：{first.get('errors')}"
    assert len(manager._fundamental_cache) == 1, "冷写占单条目"
    assert len(ifind_calls) > 0, "财务经 iFinD MCP 主源路径"
    get_config().enable_ifind_http_api = False
    fin_calls_before = len([t for t in ifind_calls if t == "get_stock_financials"])
    second = manager.get_fundamental_context(code, budget_seconds=60, include_sector_flows=False)
    # 命中返回经既有资金跨收盘边界剥离（capital_flow 不入进程内缓存快照语义），
    # 除资金块外其余维度与首轮一致；数据源零新增调用是热命中的核心证据。

    def strip(r):
        return {k: v for k, v in r.items() if k != "capital_flow"}

    assert strip(second) == strip(first), "翻转关热读命中：结果为首轮写入"
    assert len([t for t in ifind_calls if t == "get_stock_financials"]) == fin_calls_before, \
        "翻转关热读：iFinD 财务数据源零新增调用"
    get_config().enable_ifind_http_api = True
    third = manager.get_fundamental_context(code, budget_seconds=60, include_sector_flows=False)
    assert strip(third) == strip(first) and len([t for t in ifind_calls if t == "get_stock_financials"]) == fin_calls_before, \
        "翻转回开仍命中（两方向翻转均不失效）"
    assert len(manager._fundamental_cache) == 1, "同身份命中不新增条目"


def test_deep_cache_identity_isolated_by_http_toggle(choice_transport, monkeypatch, tmp_path):  # noqa: F811  参数注入 fixture
    """深研缓存键携带 HTTP 段（同一 manager 实例 + 回执齐备零错误真实冷写，
    two_way 已验证的可缓存构造）：HTTP 开深研冷写（API 来源写入键 A，零
    MCP 兜底）→ 翻转关深研读（键 B 未命中 → MCP 兜底真实调用=隔离证据）
    → 翻转回开（键 A 热命中，关→开方向同样隔离）→ 再翻转关（键 B 热命中，
    同身份零新调用）。"""
    from copy import deepcopy
    from src.config import get_config
    from src.core import trading_calendar as _tc
    from src.core.research_run import ResearchRun
    from tests.test_issue73_no_choice_deep_research import (
        COMPANY_ENV,
        DATED_INFO_ENV,
        FINANCIAL_ENV,
        FLOW_ENV,
        _VERTICAL_IFIND_SESSIONS,
    )

    ifind_calls: list[str] = []
    monkeypatch.setattr(_tc, "recent_completed_trading_sessions",
                        lambda market, count, current_time=None, **_: _VERTICAL_IFIND_SESSIONS[:max(0, min(count, len(_VERTICAL_IFIND_SESSIONS)))] or None)
    choice_transport.enable_ifind_mcp = True
    choice_transport.ifind_mcp_endpoint = "https://fixture.invalid/mcp"
    choice_transport.ifind_mcp_token = "test-only"
    choice_transport.financial_primary_source = "ifind"
    choice_transport.enable_ifind_http_api = True
    choice_transport.ifind_http_refresh_token = "test-refresh"
    choice_transport.ifind_http_timeout_seconds = 30.0

    company_env = deepcopy(COMPANY_ENV)
    company_env["data"]["answer"] = company_env["data"]["answer"].replace("600176.SH|中国巨石", "688183.SH|生益电子")

    async def ifind_route(self, tool, question, *, deadline=None):
        ifind_calls.append(tool)
        if tool == "get_stock_financials":
            return deepcopy(FINANCIAL_ENV)
        if tool == "get_stock_info":
            if "实际控制人" in question and "公司简介" in question:
                return deepcopy(company_env)
            return deepcopy(DATED_INFO_ENV)
        return deepcopy(FLOW_ENV)

    monkeypatch.setattr(
        "data_provider.ifind_fundamental_adapter.IfindFetcher._async_query", ifind_route, raising=False)
    monkeypatch.setattr(
        "data_provider.ifind_fundamental_adapter.IfindFetcher.get_instance",
        classmethod(lambda cls: __import__("data_provider.ifind_fundamental_adapter", fromlist=["IfindFetcher"]).IfindFetcher("https://fixture.invalid/mcp", "test-only")))

    def successful_products(self, code, period=None, *, deadline=None):
        from data_provider.ifind_fundamental_adapter import ProductSegment, ProductSegments
        return ProductSegments(items=[ProductSegment(name="覆铜板", rank=1, report_date="2026-09-30")], errors=[])

    monkeypatch.setattr(
        "data_provider.ifind_fundamental_adapter.IfindFetcher.product_segments", successful_products)
    _resolve_token_mock(monkeypatch)
    from data_provider.choice_adapter import ChoiceAdapter
    base_native = ChoiceAdapter._native_request

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
            raw_data = raw.get("data")
            if isinstance(raw_data, dict):
                indicators = raw.get("indicators") or []
                raw_data[args[0]] = [values.get(k, v) for k, v in zip(indicators, raw_data[args[0]])]
        return raw

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(healthy_native))

    from src.deep_research_dims import dim_cache
    monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path / "cache"))
    # API 快照回执必须是请求同票（688183 回执 fixture；错股会被拒绝走 MCP）。
    guard = _CurlGuard().install(
        monkeypatch, allow_labels=("finance", "company_profile", "flow"),
        reply=_http_payload(Q3, "688183.SH", "20261031"))

    def full_mcp_readings(code, period=None, *, deadline=None):
        """MCP 兜底替身：完整 Q3 快照（B 形态产物合格可缓存，来源标注 MCP
        形态的 revenue 值与 API 形态可区分）。"""
        from data_provider.cross_source_validator import AnchorReading
        values = {"revenue": (222.0, "currency_base", "operating_revenue"),
                  "net_profit": (33.0, "currency_base", "parent_net_profit"),
                  "roe": (7.5, "percentage_point", "weighted_roe"),
                  "gross_margin": (25.0, "percentage_point", "gross_margin"),
                  "revenue_yoy": (9.0, "percentage_point", "operating_revenue_yoy"),
                  "net_profit_yoy": (18.0, "percentage_point", "parent_net_profit_yoy"),
                  "assets": (90.0, "currency_base", "balance_sheet_total"),
                  "liabilities": (35.0, "currency_base", "balance_sheet_total")}
        return {field: AnchorReading("ifind", value, period="2026-09-30", period_basis="YTD",
                                     unit=unit, currency="CNY", caliber=caliber,
                                     statement_scope="consolidated", adjustment="restated")
                for field, (value, unit, caliber) in values.items()}

    mcp_stub = _McpSnapshotStub(full_mcp_readings("688183")).install(monkeypatch)
    manager = DataFetcherManager()  # 复用同一实例：进程内缓存真实生效
    code = "688183"  # 回执 fixture 的同票证券
    # 阶段 A（HTTP 开深研）：API 来源写入键 A（d1-http-fin-v1），零 MCP 兜底。
    with ResearchRun(120).bind():
        manager.get_fundamental_context(code, budget_seconds=60, include_sector_flows=False)
    assert guard.calls.count("finance") == 1 and mcp_stub.calls == [], \
        "深研 API 来源冷写（键 A）"
    assert len(manager._fundamental_cache) == 1
    # 阶段 B（翻转关，新 run）：键 B（d1 无段）未命中 → MCP 兜底真实调用。
    get_config().enable_ifind_http_api = False
    with ResearchRun(120).bind():
        manager.get_fundamental_context(code, budget_seconds=60, include_sector_flows=False)
    assert mcp_stub.calls, "翻转关：MCP 兜底真实调用（键 A 未命中=隔离证据）"
    assert guard.calls.count("finance") == 1, "HTTP 关闭后不再发起 API 出站"
    assert len(manager._fundamental_cache) == 2, "两种形态各占独立缓存条目"
    b_mcp_calls = len(mcp_stub.calls)
    # 阶段 C（翻转回开，新 run）：键 A 热命中（同身份零新调用）。
    get_config().enable_ifind_http_api = True
    with ResearchRun(120).bind():
        manager.get_fundamental_context(code, budget_seconds=60, include_sector_flows=False)
    assert guard.calls.count("finance") == 1 and len(mcp_stub.calls) == b_mcp_calls, \
        "翻转回开：键 A 热命中（关→开方向同样隔离）"
    # 阶段 D（再翻转关，新 run）：键 B 热命中（同身份零新调用）。
    get_config().enable_ifind_http_api = False
    with ResearchRun(120).bind():
        manager.get_fundamental_context(code, budget_seconds=60, include_sector_flows=False)
    assert len(mcp_stub.calls) == b_mcp_calls, "再翻转关：键 B 热命中（同身份零新调用）"
    assert len(manager._fundamental_cache) == 2, "同身份命中不新增条目"


def test_financial_cache_contract_ignores_http_toggle(monkeypatch):
    """契约属性：financial_cache_contract 不随 enable_ifind_http_api 变化
    （非深研持久过滤身份稳定）；完整 data_source_contract 仍随开关变化
    （深研报告/维度缓存身份语义不变）。"""
    from src.config import Config
    config = Config(enable_fundamental_pipeline=True, deep_research_cross_validate=False)
    config.data_source_profile = "choice"
    config.enable_ifind_mcp = True
    config.enable_ifind_http_api = False
    before = config.financial_cache_contract
    config.enable_ifind_http_api = True
    assert config.financial_cache_contract == before, "财务缓存身份不含 HTTP 段"
    assert config.data_source_contract != before or "-ifind-http-daily-v1" in config.data_source_contract


def test_non_deep_company_branch_zero_http(base_env, monkeypatch):
    """公司资料分支仅深研进入（include_company=deep_scope）：非深研聚合
    零 company API 出站。"""
    _install_config(monkeypatch, mcp=True, http=True, primary="ifind")
    _stub_mcp_methods(monkeypatch)
    _McpSnapshotStub().install(monkeypatch)
    guard = _CurlGuard().install(monkeypatch)
    _install_choice_native(monkeypatch, IDENTITY)
    DataFetcherManager().get_fundamental_context(CODE, budget_seconds=60)
    assert "company_profile" not in guard.calls and "finance" not in guard.calls, \
        "非深研零 company/finance API 出站（分支门 + 快照门双保险）"

# ===== 阶段 0 持久快照 / 维度持久缓存的 HTTP 参与身份（真实持久层）=====


def _stage0_payload(tag: str) -> dict:
    """完整可缓存聚合载荷：财务七字段全非 None（保存条件满足），
    revenue 按 tag 区分来源形态（HTTP 参与 vs 不参与）。"""
    revenue = 111.0 if tag == "http" else 222.0
    return {
        "market": "cn", "status": "ok", "stock_name": "贵州茅台", "source": "choice",
        "valuation": {"status": "ok", "errors": [],
                      "data": {"pe_ratio": 22.0, "pb_ratio": 8.0, "total_mv": 5e10}, "field_meta": {}},
        "growth": {"status": "ok", "errors": [],
                   "data": {"revenue": revenue, "net_profit": 50.0, "roe": 9.0,
                            "gross_margin": 30.0, "revenue_yoy": 15.0, "net_profit_yoy": 36.1},
                   "field_meta": {}},
        "boards": {"status": "failed", "data": {}},
        "research": {}, "errors": [],
    }


class _FakeManager:
    """manager 替身：按当前 HTTP 参与形态返回来源可区分载荷（记录调用）。"""

    def __init__(self):
        self.calls: list[bool] = []

    def install(self, monkeypatch):
        fake = self

        def http_participating():
            from data_provider.ifind_http_daily_adapter import http_financial_participation_active
            return http_financial_participation_active()

        def get_fundamental_context(code, budget_seconds=None, include_sector_flows=False):
            participating = http_participating()
            fake.calls.append(participating)
            return _stage0_payload("http" if participating else "plain")

        fake.get_fundamental_context = get_fundamental_context
        monkeypatch.setattr(
            "src.agent.tools.data_tools._get_fetcher_manager", lambda: fake)
        return fake


def test_stage0_deep_write_not_reused_by_plain_and_vice_versa(choice_transport, monkeypatch, tmp_path):  # noqa: F811  参数注入 fixture
    """审查场景双向收敛（Choice 主源 + HTTP 开，真实持久层）：深研先写
    （HTTP 财务形态）不得被普通专项复用；普通先写（不参与形态）不得被
    深研复用；同身份二次读取命中零新取数。"""
    from src.core.research_run import ResearchRun
    from src.deep_research_dims.context import SharedContext, _safe_fundamental

    config = choice_transport
    config.enable_ifind_http_api = True
    config.ifind_http_refresh_token = "test-refresh"
    config.ifind_http_timeout_seconds = 30.0
    config.financial_primary_source = "choice"
    from src.deep_research_dims import dim_cache
    monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path / "cache"))
    fake = _FakeManager().install(monkeypatch)

    # 1) 深研先写（HTTP 参与形态）。
    with ResearchRun(120).bind():
        deep_first = _safe_fundamental(CODE, SharedContext(stock_code=CODE, stock_name="贵州茅台", as_of="2026-11-17T10:00:00"))
    assert fake.calls == [True] and deep_first["revenue"] == 111.0

    # 2) 普通专项读：不得复用深研 HTTP 快照（h1/h0 键隔离）→ 重新取数。
    plain_first = _safe_fundamental(CODE, SharedContext(stock_code=CODE, stock_name="贵州茅台", as_of="2026-11-17T10:01:00"))
    assert fake.calls == [True, False] and plain_first["revenue"] == 222.0, \
        "普通专项不得复用深研 HTTP 形态快照"

    # 3) 普通同身份二次读取：命中 h0 缓存零新取数。
    plain_second = _safe_fundamental(CODE, SharedContext(stock_code=CODE, stock_name="贵州茅台", as_of="2026-11-17T10:02:00"))
    assert fake.calls == [True, False] and plain_second["revenue"] == 222.0

    # 4) 深研同身份二次读取：命中 h1 缓存零新取数（反向不串）。
    with ResearchRun(120).bind():
        deep_second = _safe_fundamental(CODE, SharedContext(stock_code=CODE, stock_name="贵州茅台", as_of="2026-11-17T10:03:00"))
    assert fake.calls == [True, False] and deep_second["revenue"] == 111.0


def test_stage0_plain_write_not_reused_by_deep(choice_transport, monkeypatch, tmp_path):  # noqa: F811  参数注入 fixture
    """反向场景：普通专项先写（不参与形态）后，深研不得复用其快照。"""
    from src.core.research_run import ResearchRun
    from src.deep_research_dims import dim_cache
    from src.deep_research_dims.context import SharedContext, _safe_fundamental

    config = choice_transport
    config.enable_ifind_http_api = True
    config.ifind_http_refresh_token = "test-refresh"
    config.ifind_http_timeout_seconds = 30.0
    config.financial_primary_source = "choice"
    monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path / "cache"))
    fake = _FakeManager().install(monkeypatch)

    plain = _safe_fundamental(CODE, SharedContext(stock_code=CODE, stock_name="贵州茅台", as_of="2026-11-17T10:00:00"))
    assert fake.calls == [False] and plain["revenue"] == 222.0
    with ResearchRun(120).bind():
        deep = _safe_fundamental(CODE, SharedContext(stock_code=CODE, stock_name="贵州茅台", as_of="2026-11-17T10:01:00"))
    assert fake.calls == [False, True] and deep["revenue"] == 111.0, \
        "深研不得复用普通专项的不参与形态快照"


def test_stage0_http_toggle_rekeys(base_env, monkeypatch, tmp_path):
    """开关变化：HTTP 开深研写（h1）→ 翻转 HTTP 关 → 深研再读（h0）不命中
    旧快照、重新取数。"""
    from src.config import get_config
    from src.core.research_run import ResearchRun
    from src.deep_research_dims import dim_cache
    from src.deep_research_dims.context import SharedContext, _safe_fundamental

    _install_config(monkeypatch, mcp=True, http=True, primary="choice")
    monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path / "cache"))
    fake = _FakeManager().install(monkeypatch)
    with ResearchRun(120).bind():
        _safe_fundamental(CODE, SharedContext(stock_code=CODE, stock_name="贵州茅台", as_of="2026-11-17T10:00:00"))
    assert fake.calls == [True]
    get_config().enable_ifind_http_api = False
    with ResearchRun(120).bind():
        after = _safe_fundamental(CODE, SharedContext(stock_code=CODE, stock_name="贵州茅台", as_of="2026-11-17T10:01:00"))
    assert fake.calls == [True, False] and after["revenue"] == 222.0, \
        "开关翻转后深研不命中 HTTP 期快照（身份随参与形态变化）"


def test_dim_cache_path_carries_participation_segment(monkeypatch, tmp_path):
    """维度持久缓存键与阶段 0 快照/manager 键同判定：参与形态文件名带段、
    非参与与 HTTP 关闭形态不带（不因开关分裂非深研缓存）。"""
    from src.config import get_config
    from src.deep_research_dims import dim_cache
    from src.core.research_run import ResearchRun

    _install_config(monkeypatch, mcp=True, http=True, primary="ifind")
    monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path / "cache"))
    plain = dim_cache._cache_path(CODE, "growth")
    with ResearchRun(120).bind():
        on_deep = dim_cache._cache_path(CODE, "growth")
    # 同开关状态下：深研（HTTP 参与）与非深研键不同（_httpfin 段）。
    assert on_deep != plain and on_deep.endswith("_httpfin_growth.json")
    assert not plain.endswith("_httpfin_growth.json")
    get_config().enable_ifind_http_api = False
    # 开关关闭后：参与判定 False——深研 run 内键与普通键一致（contract 段随
    # 开关变化属完整契约既有语义，与本段无关）。
    with ResearchRun(120).bind():
        off_deep = dim_cache._cache_path(CODE, "growth")
    off_plain = dim_cache._cache_path(CODE, "growth")
    assert off_deep == off_plain and "_httpfin" not in off_deep, \
        "HTTP 关闭后深研/普通键一致（参与段不残留）"


# ===== 第八轮：资格收敛身份 + 旧缓存迁移隔离（真实持久层）=====

def test_dim_cache_legacy_files_isolated_for_both_forms(choice_transport, monkeypatch, tmp_path):  # noqa: F811  参数注入 fixture
    """阻断 1 迁移测试（真实持久层，只有旧文件）：旧版本 HTTP 开启期的
    旧形态文件（可能由深研 HTTP 财务产生——旧代码无深研门）存在时，新
    普通与新深研的真实 load 均 miss（不错误命中），随后各自新身份
    save/load 热命中。"""
    import os
    from src.deep_research_dims import dim_cache
    from src.core.research_run import ResearchRun

    config = choice_transport
    config.enable_ifind_http_api = True
    config.ifind_http_refresh_token = "test-refresh"
    config.ifind_http_timeout_seconds = 30.0
    config.financial_primary_source = "ifind"
    monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path / "cache"))
    code, dim = "600519", "fundamental"
    # 只放置旧版本形态文件（payload 为合法 save 产物复制，schema/TTL 全合规）：
    # 旧无段（HTTP 开期 contract，可能由深研 HTTP 财务产生）与假想旧 plain 段。
    plain_path = dim_cache._cache_path(code, dim)  # 当前不参与形态（无 run）→ _plain
    assert "_plain" in plain_path
    dim_cache.save_cached_dim(code, dim, {"status": "ok", "seed": True})
    legacy_names = []
    for tag in ("", "_plain"):
        legacy = plain_path.replace("_plain", tag)
        if legacy != plain_path:
            legacy_names.append(legacy)
    # 重放：用 save 产物复制出全部旧形态后删除新形态文件，确保目录里只有旧文件。
    dim_cache.save_cached_dim(code, dim, {"status": "ok", "seed": True})
    for legacy in legacy_names:
        import shutil
        shutil.copy(plain_path, legacy)
    os.remove(plain_path)
    assert not os.path.exists(plain_path) and all(os.path.exists(p) for p in legacy_names), \
        "目录中只有旧版本形态文件"

    # 新普通（不参与）真实 load：不得命中任何旧文件。
    assert dim_cache.load_cached_dim(code, dim) is None, "新普通不得命中旧文件"
    # 新普通 save/load 热命中。
    dim_cache.save_cached_dim(code, dim, {"status": "ok", "plain": True})
    loaded = dim_cache.load_cached_dim(code, dim)
    assert loaded is not None and loaded.get("payload", loaded).get("plain") is True, \
        "新普通 save/load 热命中"

    # 新深研（参与，run 绑定）真实 load：不得命中旧文件或 plain 文件。
    with ResearchRun(120).bind():
        httpfin_path = dim_cache._cache_path(code, dim)
        assert "_httpfin" in httpfin_path and httpfin_path != plain_path
        assert dim_cache.load_cached_dim(code, dim) is None, "新深研不得命中旧/plain 文件"
        # 新深研 save/load 热命中。
        dim_cache.save_cached_dim(code, dim, {"status": "ok", "http": True})
        loaded_deep = dim_cache.load_cached_dim(code, dim)
        assert loaded_deep is not None and loaded_deep.get("payload", loaded_deep).get("http") is True, \
            "新深研 save/load 热命中"
    # 回到不参与：plain 文件仍独立热命中（两形态各自隔离互不串用）。
    assert dim_cache.load_cached_dim(code, dim) is not None
    assert loaded_deep.get("payload", loaded_deep).get("http") is True or loaded_deep.get("http") is True


def test_stage0_invalid_credential_state_is_degraded_not_normal(choice_transport, monkeypatch, tmp_path):  # noqa: F811  参数注入 fixture
    """非法配置形态的失效行为（明确非正常模式）：运行中置空凭据且开关
    仍开属配置错误（重启会被 ``Config.__post_init__`` 拒绝）——运行时资格
    不满足 → 参与判定收敛为 False → 降级为不参与身份（重取不参与来源），
    不将非法状态宣传为正常模式。"""
    from src.config import get_config
    from src.core.research_run import ResearchRun
    from src.deep_research_dims import dim_cache
    from src.deep_research_dims.context import SharedContext, _safe_fundamental

    config = choice_transport
    config.enable_ifind_http_api = True
    config.ifind_http_refresh_token = "test-refresh"
    config.ifind_http_timeout_seconds = 30.0
    config.financial_primary_source = "choice"
    monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path / "cache"))
    fake = _FakeManager().install(monkeypatch)

    def read():
        return _safe_fundamental(CODE, SharedContext(stock_code=CODE, stock_name="贵州茅台", as_of="2026-11-17T10:00:00"))

    with ResearchRun(120).bind():
        deep = read()
    assert fake.calls == [True] and deep["revenue"] == 111.0
    # 非法状态（配置错误）：开关仍开但凭据置空——运行时资格不满足的失效行为。
    get_config().ifind_http_refresh_token = None
    from data_provider.ifind_http_daily_adapter import (
        http_financial_participation_active,
        ifind_http_daily_enabled,
    )
    assert not ifind_http_daily_enabled() and not http_financial_participation_active(), \
        "凭据缺失时资格判定收敛为 False（失效降级，不虚称参与）"
    degraded = read()
    assert fake.calls == [True, False] and degraded["revenue"] == 222.0, \
        "失效形态降级为不参与来源（不命中参与产物）"


def test_deep_manager_credential_toggle_rekeys(choice_transport, monkeypatch, tmp_path):  # noqa: F811  参数注入 fixture
    """阻断 2（manager 进程内键，同一 manager + 真实数据源调用计数）：
    合法参与配置冷写（键 A）→ 切换合法不参与配置（键 B miss → MCP 兜底
    真实调用）→ 切回参与配置（键 A 缓存返回：iFinD 数据源零新增调用且
    结果与首轮一致）——两方向身份隔离与热命中均有直接证据。"""
    from copy import deepcopy
    from src.config import get_config
    from src.core import trading_calendar as _tc
    from src.core.research_run import ResearchRun
    from tests.test_issue73_no_choice_deep_research import (
        COMPANY_ENV, DATED_INFO_ENV, FINANCIAL_ENV, FLOW_ENV, _VERTICAL_IFIND_SESSIONS,
    )

    # choice_transport 底座：完整 Choice 本地传输 + efinance/akshare 板块替身
    # （boards 真实出站在沙箱挂起超时，其块级 error 会汇入顶层 errors 阻断
    # 缓存写入——与 two_way「聚合零错误」前提一致）。
    config = choice_transport
    config.enable_ifind_mcp = True
    config.ifind_mcp_endpoint = "https://fixture.invalid/mcp"
    config.ifind_mcp_token = "test-only"
    config.ifind_mcp_timeout_seconds = 30.0
    config.financial_primary_source = "ifind"
    config.enable_ifind_http_api = True
    config.ifind_http_refresh_token = "test-refresh"
    config.ifind_http_timeout_seconds = 30.0
    monkeypatch.setattr(_tc, "recent_completed_trading_sessions",
                        lambda market, count, current_time=None, **_: _VERTICAL_IFIND_SESSIONS[:max(0, min(count, len(_VERTICAL_IFIND_SESSIONS)))] or None)

    company_env = deepcopy(COMPANY_ENV)
    company_env["data"]["answer"] = company_env["data"]["answer"].replace("600176.SH|中国巨石", "688183.SH|生益电子")

    ifind_calls: list[str] = []

    async def ifind_route(self, tool, question, *, deadline=None):
        ifind_calls.append(tool)
        if tool == "get_stock_financials":
            return deepcopy(FINANCIAL_ENV)
        if tool == "get_stock_info":
            if "实际控制人" in question and "公司简介" in question:
                return deepcopy(company_env)
            return deepcopy(DATED_INFO_ENV)
        return deepcopy(FLOW_ENV)

    monkeypatch.setattr(
        "data_provider.ifind_fundamental_adapter.IfindFetcher._async_query", ifind_route, raising=False)
    monkeypatch.setattr(
        "data_provider.ifind_fundamental_adapter.IfindFetcher.get_instance",
        classmethod(lambda cls: __import__("data_provider.ifind_fundamental_adapter", fromlist=["IfindFetcher"]).IfindFetcher("https://fixture.invalid/mcp", "test-only")))

    def successful_products(self, code, period=None, *, deadline=None):
        from data_provider.ifind_fundamental_adapter import ProductSegment, ProductSegments
        return ProductSegments(items=[ProductSegment(name="覆铜板", rank=1, report_date="2026-09-30")], errors=[])

    monkeypatch.setattr(
        "data_provider.ifind_fundamental_adapter.IfindFetcher.product_segments", successful_products)
    # 板块榜单本地替身（真实 efinance 出站在沙箱挂起超时，其块级 error 会
    # 汇入顶层 errors 阻断缓存写入——与 two_way「聚合零错误」前提一致）。
    monkeypatch.setattr(DataFetcherManager, "_get_sector_rankings_with_meta",
                        lambda self, n=5: ([{"name": "stub", "change_pct": 1.0}], [], [], ""))
    _resolve_token_mock(monkeypatch)
    from src.deep_research_dims import dim_cache
    monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path / "cache"))
    guard = _CurlGuard().install(
        monkeypatch, allow_labels=("finance", "company_profile", "flow"),
        reply=_http_payload(Q3, "688183.SH", "20261031"))
    mcp_stub = _McpSnapshotStub().install(monkeypatch)

    def full_mcp_readings(code, period=None, *, deadline=None):
        """MCP 兜底替身：完整 Q3 快照（不参与形态产物合格可缓存，revenue
        值与 API 形态可区分）。"""
        from data_provider.cross_source_validator import AnchorReading
        values = {"revenue": (222.0, "currency_base", "operating_revenue"),
                  "net_profit": (33.0, "currency_base", "parent_net_profit"),
                  "roe": (7.5, "percentage_point", "weighted_roe"),
                  "gross_margin": (25.0, "percentage_point", "gross_margin"),
                  "revenue_yoy": (9.0, "percentage_point", "operating_revenue_yoy"),
                  "net_profit_yoy": (18.0, "percentage_point", "parent_net_profit_yoy"),
                  "assets": (90.0, "currency_base", "balance_sheet_total"),
                  "liabilities": (35.0, "currency_base", "balance_sheet_total")}
        return {field: AnchorReading("ifind", value, period="2026-09-30", period_basis="YTD",
                                     unit=unit, currency="CNY", caliber=caliber,
                                     statement_scope="consolidated", adjustment="restated")
                for field, (value, unit, caliber) in values.items()}

    mcp_stub = _McpSnapshotStub(full_mcp_readings("688183")).install(monkeypatch)
    manager = DataFetcherManager()  # 复用同一实例：进程内缓存真实生效
    code = "688183"  # 回执 fixture 的同票证券

    def fin_source_calls():
        return len([t for t in ifind_calls if t == "get_stock_financials"])

    # 阶段 A（合法参与配置冷写）：键 A（d1-http-fin-v1），API 来源、零 MCP。
    with ResearchRun(120).bind():
        first = manager.get_fundamental_context(code, budget_seconds=60, include_sector_flows=False)
    keys_a = [k for k in manager._fundamental_cache.keys() if "http-fin-v1" in k]
    assert len(keys_a) == 1, "参与形态键 A 独立条目"
    assert guard.calls.count("finance") == 1 and mcp_stub.calls == [], "API 来源冷写"
    a_fin_calls = fin_source_calls()
    # 阶段 B（切换合法不参与配置，新 run）：键 B miss → MCP 兜底真实调用
    # （iFinD MCP 财务数据源真实调用发生=隔离直接证据）。
    get_config().enable_ifind_http_api = False
    with ResearchRun(120).bind():
        manager.get_fundamental_context(code, budget_seconds=60, include_sector_flows=False)
    assert mcp_stub.calls, "切换不参与配置：MCP 兜底真实调用（键 A 未命中=隔离证据）"
    assert guard.calls.count("finance") == 1, "不参与配置不再发起 API 出站"
    assert len(manager._fundamental_cache) == 2, "两形态各占独立缓存条目"
    assert len(mcp_stub.calls) == 1, "键 B 真实重取计数（MCP 兜底真实调用一次）"
    # 阶段 C（切回合法参与配置，新 run）：键 A 缓存返回——iFinD 数据源零
    # 新增调用、结果与首轮一致（热命中直接证据，不以缓存条目数代替）。
    get_config().enable_ifind_http_api = True
    with ResearchRun(120).bind():
        third = manager.get_fundamental_context(code, budget_seconds=60, include_sector_flows=False)

    def strip_capital(r):
        # 命中返回经既有资金跨收盘边界剥离（capital_flow 不入进程内缓存快照
        # 语义），比较时排除该块——与首轮其余维度一致即热命中。
        return {k: v for k, v in r.items() if k != "capital_flow"}

    assert strip_capital(third) == strip_capital(first), "键 A 缓存返回与首轮一致"
    assert fin_source_calls() == a_fin_calls, "切回参与配置热命中：iFinD 数据源零新增调用"
    assert guard.calls.count("finance") == 1 and len(mcp_stub.calls) == 1, \
        "API/MCP 调用次数不变（同身份热命中）"
    assert len(manager._fundamental_cache) == 2, "同身份命中不新增条目"
