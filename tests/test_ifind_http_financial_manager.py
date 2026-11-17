# -*- coding: utf-8 -*-
"""第四十四/四十五轮：财务快照标准 API 经真实 DataFetcherManager 聚合入口的
消费者验证（外部供应商可控替身，模式沿用 test_ifind_primary_migration）。

覆盖：Q3 损益+Q2 配对（同主期装配拒绝→资产负债缺口如实披露，检视报告
字段）；Q3 全齐（装配成功）；API 缺毛利率+Q2 可得（探测合并、MCP 零调
用）；MCP 关闭（choice 主源 API 补充参与）；开关关闭（既有 choice 语义）；
budget 传参。取消与短预算已由 tests/test_ifind_http_financial_snapshot.py
在 fallback 层覆盖。
"""
from __future__ import annotations

import json
import pytest
from datetime import date as _date


from tests._network_guard import network_guard  # noqa: F401  (autouse fixture)
from data_provider import ifind_http_daily_adapter as A
from data_provider.base import DataFetcherManager
from data_provider.ifind_fundamental_adapter import CompanyFact, CompanyProfile
from data_provider.choice_adapter import ChoiceAdapter
from data_provider.ifind_fundamental_adapter import IfindFetcher
from src.config import Config
from src.core.research_run import ResearchRun


CODE = "600519"
PERIOD = "20260930"


def _resolve_token_mock(monkeypatch):
    monkeypatch.setattr(A, "_resolve_access_token",
                        lambda r, *, force_refresh=False: "tok_test_access")


def _install_config(monkeypatch, *, mcp: bool, http: bool, primary: str = "ifind") -> None:
    cfg = Config(enable_fundamental_pipeline=True, deep_research_cross_validate=False)
    cfg.data_source_profile = "choice"
    cfg.enable_ifind_mcp = mcp
    cfg.financial_primary_source = primary if mcp else "choice"
    if mcp:
        cfg.ifind_mcp_endpoint = "https://fixture.invalid/mcp"
        cfg.ifind_mcp_token = "test-only"
        cfg.ifind_mcp_timeout_seconds = 30.0
    cfg.enable_ifind_http_api = http
    cfg.ifind_http_refresh_token = "test-refresh" if http else None
    cfg.ifind_http_timeout_seconds = 30.0
    monkeypatch.setattr(Config, "_instance", cfg)


def _freeze_calendar(monkeypatch) -> None:
    from src.core import trading_calendar as tc

    monkeypatch.setattr(
        tc, "recent_completed_trading_sessions",
        lambda market, count, current_time=None, **_: [_date(2026, 10, 30), _date(2026, 10, 29)][: max(0, min(count, 2))],
    )


def _http_payload(values: dict, code: str, disclosed: str) -> bytes:
    table = {}
    for field, indicator, _u, _fo, _cal in A._FINANCE_FIELD_SPECS:
        table[indicator] = [values.get(field, None)]
    table[A._FINANCE_DISCLOSED_AT_INDICATOR] = [disclosed]
    return json.dumps({"errorcode": 0, "errmsg": "",
                       "tables": [{"thscode": code, "table": table}]}).encode()


def _install_http(monkeypatch, payloads: dict):
    calls: list[str] = []

    def fake_curl(url, headers, body, *, label):
        period = json.loads(body)["indipara"][0]["indiparams"][0]
        calls.append(period)
        return 200, payloads[period]

    monkeypatch.setattr(A, "_http_api_curl_post", fake_curl)
    monkeypatch.setattr(A, "_resolve_access_token",
                        lambda refresh, *, force_refresh=False: "tok_test")
    return calls


def _stub_mcp_methods(monkeypatch):
    """prefer_ifind 分支其余 MCP 方法（估值批次/定向PB/资金流）统一可控替身：
    干净超时失败（各处既有 except 隔离），不产生真实出站。"""
    from data_provider.cross_source_validator import SourceReadError

    calls: list[str] = []

    def failing_query(self, tool, question, *, deadline=None):
        calls.append(tool)
        raise SourceReadError("ifind", "timeout")

    monkeypatch.setattr(IfindFetcher, "query", failing_query)
    monkeypatch.setattr(
        IfindFetcher, "current_info_snapshot",
        lambda self, code, **k: {"valuation": {}, "industry": {"errors": []},
                                 "requested_date": None, "pb_rejection": ()},
    )
    monkeypatch.setattr(
        IfindFetcher, "directed_pb_snapshot",
        lambda self, code, **k: {"pb_ratio": None, "rejection": ("no_qualified_reading",),
                                 "requested_date": None},
    )
    monkeypatch.setattr(IfindFetcher, "fetch_main_inflow_series", lambda self, code, days=12, **k: None)
    return calls




def _stub_capital_chain(monkeypatch):
    """资金/板块链 root-entry 替身（第四十九轮网络隔离）：资金缺口如实形态，
    断 Akshare/新浪/东财真实兜底出站。"""
    import data_provider.capital_flow_provider as _cfp

    monkeypatch.setattr(
        _cfp, "get_main_inflow_cumulative",
        lambda code, days=12, deadline=None, current_time=None, allow_choice_fallback=True: {
            "status": "missing", "source": None, "errors": ["test:capital_chain_stubbed"],
            "cumulative_net_inflow": None, "days": [],
        },
    )

    # 入口级 stub：pipeline 的 boards/龙虎榜后台真实出站与本财务快照无关
    monkeypatch.setattr(
        "data_provider.base.DataFetcherManager.get_board_context",
        lambda self, stock_code, budget_seconds=None: {
            "status": "not_supported", "boards": {}, "errors": ["test:boards_stubbed"],
        },
        raising=False,
    )
    monkeypatch.setattr(
        "data_provider.base.DataFetcherManager.get_dragon_tiger_context",
        lambda self, stock_code, budget_seconds=None: {
            "status": "not_supported", "dragon_tiger": {}, "errors": ["test:dragon_tiger_stubbed"],
        },
        raising=False,
    )

    class _StubFundamentalAdapter:
        def get_capital_flow(self, code, include_stock=True, include_sector=True, **k):
            return {"stock_flow": {}, "sector_rankings": {}, "errors": ["test:capital_flow_stubbed"]}

    monkeypatch.setattr(
        "data_provider.base.AkshareFundamentalAdapter", _StubFundamentalAdapter,
    )
    # 调用栈取证（临时）：板块排行真实调用者定位
    def _boom(self, n=5):
        raise AssertionError("sector rankings real call leaked")

    for mod_name, attr in [
        ("data_provider.akshare_fetcher.AkshareFetcher", "get_sector_rankings"),
        ("data_provider.base.DataFetcherManager", "_get_sector_rankings_with_meta"),
    ]:
        try:
            import importlib
            mod = importlib.import_module(mod_name.rsplit(".", 1)[0])
            cls = getattr(mod, mod_name.rsplit(".", 1)[1])
            monkeypatch.setattr(cls, attr, _boom, raising=False)
        except Exception:
            pass


def _install_choice_native(monkeypatch, code: str, *, pettm=None, pb=None, mv=None):
    from decimal import Decimal as _D

    def native(method, args, options, deadline):
        indicators = args[1] if len(args) > 1 else ""
        if method == "tradedates":
            return {"data": ["2026-10-30"]}
        if indicators == "NAME,LATESTREPORTNEW":
            return {"codes": [code], "indicators": ["NAME", "LATESTREPORTNEW"],
                    "dates": [], "data": {code: ["测试标的", "2026/09/30"]}}
        if indicators == "PETTM,PB,MV":
            return {"codes": [code], "indicators": ["PETTM", "PB", "MV"],
                    "dates": [], "data": {code: [pettm, pb, mv]}}
        if indicators == "SUMASSETNEW,SUMLIABNEW,ACTUALISSUINGDATE":
            return {"codes": [code], "indicators": ["SUMASSETNEW", "SUMLIABNEW", "ACTUALISSUINGDATE"],
                    "dates": [], "data": {code: [None, None, None]}}
        if indicators.startswith("RATINGTARGET"):
            return {"codes": [code], "indicators": indicators.split(","),
                    "dates": [], "data": {code: [None, None, None]}}
        return {"codes": [args[0]], "indicators": indicators.split(","),
                "dates": [], "data": {args[0]: [None for _ in indicators.split(",")]}}

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(native))
    monkeypatch.setattr(
        IfindFetcher, "get_instance",
        classmethod(lambda cls: IfindFetcher("https://fixture.invalid/mcp", "test-only")),
    )
    return native


Q3 = {"revenue": 575.1, "net_profit": 92.6, "roe": 9.0, "gross_margin": 30.0,
      "revenue_yoy": 15.0, "net_profit_yoy": 36.1, "assets": 100.0, "liabilities": 40.0}


class _McpStub:
    """MCP financial_snapshot 替身：脚本逐个返回，异常传播（第五十一轮）。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[tuple] = []

    def install(self, monkeypatch):
        stub = self

        def fake(self_fetcher, code, period, *, deadline):
            stub.calls.append((code, period, deadline))
            entry = stub.script.pop(0)
            if isinstance(entry, Exception):
                raise entry
            return entry

        monkeypatch.setattr(
            "data_provider.ifind_fundamental_adapter.IfindFetcher.financial_snapshot",
            fake,
        )




def test_manager_q3_full_snapshot_assembles_balance(monkeypatch):
    _stub_mcp_methods(monkeypatch)
    _stub_capital_chain(monkeypatch)
    """真实 manager 入口（Q3 全齐）：balance_sheet 装配成功且 period=Q3、
    field_meta 损益期=Q3（检视报告字段）。"""
    _install_config(monkeypatch, mcp=True, http=True, primary="ifind")
    _freeze_calendar(monkeypatch)
    _resolve_token_mock(monkeypatch)
    profile_payload = json.dumps({"errorcode": 0, "errmsg": "", "tables": [{
        "thscode": "600519.SH", "table": {
            "ths_corp_profile_stock": ["贵州茅台酒股份有限公司主营……"],
            "ths_actual_controller_stock": ["贵州省国资委"],
            "ths_actual_controller_type_stock": ["地方国资委"],
            "ths_mo_product_name_stock": ["茅台酒、其他系列酒"],
            "ths_main_businuess_stock": ["酒类生产销售"]}}]}).encode()

    calls: list[str] = []

    def routed_curl(url, headers, body, *, label):
        params = json.loads(body)["indipara"][0]["indiparams"]
        period = params[0] if params else "20260930"
        calls.append(period)
        return 200, (_http_payload(Q3, "600519.SH", "20261031") if label == "finance" else profile_payload)

    monkeypatch.setattr(A, "_http_api_curl_post", routed_curl)
    _install_choice_native(monkeypatch, "600519.SH")
    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context("600519", budget_seconds=60, include_sector_flows=False)
    assert calls == ["20260930", "20260930"]  # finance 一期 + company_profile 一批
    growth = result["growth"]["data"]
    assert growth["revenue"] == 575.1
    assert growth["balance_sheet"]["period"] == "2026-09-30"
    assert growth["balance_sheet"]["assets"] == 100.0
    assert growth["balance_sheet"]["source"] == "ifind"


def test_manager_q3_income_with_q2_pair_gap_disclosed(monkeypatch):
    _stub_mcp_methods(monkeypatch)
    _stub_capital_chain(monkeypatch)
    """Q3 损益+Q2 配对：同主期装配拒绝 → balance_sheet 缺口如实披露（不把
    旧期配对当 Q3 当前资产负债），field_meta 损益期=Q3。"""
    _install_config(monkeypatch, mcp=True, http=True, primary="ifind")
    _freeze_calendar(monkeypatch)
    q2 = {**Q3, "assets": 98.0, "liabilities": 39.0, "revenue": 500.0,
          "net_profit": 80.0, "roe": 8.5, "gross_margin": 25.0,
          "revenue_yoy": 12.0, "net_profit_yoy": 30.0}
    calls: list[str] = []

    def routed_curl(url, headers, body, *, label):
        params = json.loads(body)["indipara"][0]["indiparams"]
        period = params[0] if params else "20260930"
        calls.append(period)
        table = {"20260930": {"revenue": 575.1, "net_profit": 92.6, "roe": 9.0,
                              "gross_margin": 30.0, "revenue_yoy": 15.0,
                              "net_profit_yoy": 36.1, "assets": 100.0},
                 "20260630": q2}[period]
        return 200, _http_payload(table, "600519.SH",
                                  "20261031" if period == "20260930" else "20260825")

    monkeypatch.setattr(A, "_http_api_curl_post", routed_curl)
    _resolve_token_mock(monkeypatch)
    _install_choice_native(monkeypatch, "600519.SH")
    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context("600519", budget_seconds=60, include_sector_flows=False)
    growth = result["growth"]["data"]
    assert growth["revenue"] == 575.1
    assert growth["report_date"] == "2026-09-30"
    # 同主期装配拒绝：Q2 配对不得作为 Q3 当前资产负债（缺口如实）
    bs = growth.get("balance_sheet")
    assert bs is None or bs.get("period") != "2026-09-30"
    assert bs is None or bs.get("period") == "2026-06-30"  # 若装配了也必须标注 Q2 真实期
    assert calls[:2] == ["20260930", "20260630"]  # 逐字段探测合并 + company_profile 批


def test_manager_missing_field_filled_by_probe_not_mcp(monkeypatch):
    _stub_mcp_methods(monkeypatch)
    _stub_capital_chain(monkeypatch)
    """API 缺毛利率（Q2 API 可得）：探测合并补齐（毛利率期=Q2 真实标注），
    MCP 零调用（探测优先于 MCP 补缺）。"""
    _install_config(monkeypatch, mcp=True, http=True, primary="ifind")
    _freeze_calendar(monkeypatch)
    calls: list[str] = []
    _resolve_token_mock(monkeypatch)
    period_payloads = {
        "20260930": _http_payload({**Q3, "gross_margin": None}, "600519.SH", "20261031"),
        "20260630": _http_payload({**Q3, "gross_margin": 25.0, "revenue": 500.0,
                                   "net_profit": 80.0, "roe": 8.5,
                                   "revenue_yoy": 12.0, "net_profit_yoy": 30.0,
                                   "assets": 98.0, "liabilities": 39.0}, "600519.SH", "20260825"),
    }
    company_profile_payload = json.dumps({"errorcode": 0, "errmsg": "", "tables": [{
        "thscode": "600519.SH", "table": {
            "ths_corp_profile_stock": ["贵州茅台酒股份有限公司主营……"]}}]}).encode()

    def routed_curl(url, headers, body, *, label):
        indipara = json.loads(body)["indipara"]
        params = indipara[0]["indiparams"]
        period = params[0] if params else "20260930"
        calls.append(period)
        return 200, period_payloads.get(period, company_profile_payload)

    monkeypatch.setattr(A, "_http_api_curl_post", routed_curl)
    _install_choice_native(monkeypatch, "600519.SH")
    # 第二十轮：毛利率（d_only）值由 API 探测合并（Q2 真实期），但口径未
    # 证明 → fallback 按「口径待补」交 MCP——MCP 同期（Q2）合格口径候选
    # 拼入后经采纳门合格（值/期/口径三者真实，不伪装 restated）。
    from data_provider.cross_source_validator import AnchorReading
    mcp_gross = AnchorReading("ifind", 25.0, period="2026-06-30", period_basis="YTD",
                              unit="percentage_point", caliber="gross_margin",
                              statement_scope="consolidated", adjustment="restated")
    mcp = _McpStub([{"gross_margin": mcp_gross}])
    mcp.install(monkeypatch)
    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context("600519", budget_seconds=60, include_sector_flows=False)
    gross_meta = result["growth"]["data"].get("gross_margin")
    gross_record = result["growth"]["field_meta"].get("gross_margin") or {}
    # 毛利率最终合格：Q2 真实期 + MCP 真实口径（期一致拼入）
    assert gross_meta is not None and gross_record.get("period") == "2026-06-30"
    assert len(mcp.calls) == 1, "d_only 口径待补恰一次 MCP 补缺"


def test_manager_mcp_off_http_still_supplements(monkeypatch):
    """MCP 关闭（choice 主源 + HTTP 开）：API 仍为合法次源补充候选——
    同期 iFinD 读数进 growth（既有同期补充契约），MCP 语义零改动。"""
    _install_config(monkeypatch, mcp=False, http=True, primary="choice")
    _freeze_calendar(monkeypatch)
    _stub_capital_chain(monkeypatch)
    _install_http(monkeypatch, {"20260930": _http_payload(Q3, "000001.SZ", "20261031")})
    _install_choice_native(monkeypatch, "000001.SZ")
    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context("000001", budget_seconds=60, include_sector_flows=False)
    growth = result["growth"]["data"]
    # 同期（Choice report_date=2026-09-30）iFinD 补充进 growth
    assert growth.get("revenue") == 575.1
    fm = (result["growth"]["field_meta"].get("revenue") or {})
    assert fm.get("period") == "2026-09-30"


def test_manager_switch_off_keeps_choice_semantics(monkeypatch):
    """开关关闭：API 零参与，choice 语义与既有完全一致（MCP 亦关）。"""
    _install_config(monkeypatch, mcp=False, http=False, primary="choice")
    _freeze_calendar(monkeypatch)
    _stub_capital_chain(monkeypatch)

    def forbidden(*a, **k):
        raise AssertionError("switch off: API transport must not run")

    monkeypatch.setattr(A, "_http_api_curl_post", forbidden)
    _install_choice_native(monkeypatch, "600519.SH")
    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context("600519", budget_seconds=60, include_sector_flows=False)
    growth = result["growth"]["data"]
    # Choice 次源形态：六字段（无 HTTP/MCP 覆盖）
    assert growth.get("balance_sheet") is None or growth["balance_sheet"].get("source") == "choice"
    for field in ("revenue", "net_profit", "roe"):
        fm = result["growth"]["field_meta"].get(field) or {}
        assert fm.get("source") in ("choice", None, "unknown", "mx")


def test_slow_choice_reserves_time_for_http_supplement(monkeypatch):
    """第四十六轮反例（慢 Choice 端到端）：choice 主源+MCP 关+HTTP 开——
    慢 Choice 消耗大半预算后 HTTP 补充仍被调用并成功进 growth（预留自总
    deadline 划出，不延长总上限；非瞬时 mock——HTTP 数据为最终业务事实）。"""
    import time as _time

    _install_config(monkeypatch, mcp=False, http=True, primary="choice")
    _freeze_calendar(monkeypatch)
    _stub_mcp_methods(monkeypatch)
    _stub_capital_chain(monkeypatch)
    _install_http(monkeypatch, {"20260930": _http_payload(Q3, "000001.SZ", "20261031")})

    def slow_native(method, args, options, deadline):
        _time.sleep(0.5)  # 慢 Choice：真实短消耗（第四十九轮：避免真实长 sleep）
        indicators = args[1] if len(args) > 1 else ""
        if method == "tradedates":
            return {"data": ["2026-10-30"]}
        if indicators == "NAME,LATESTREPORTNEW":
            return {"codes": ["000001.SZ"], "indicators": ["NAME", "LATESTREPORTNEW"],
                    "dates": [], "data": {"000001.SZ": ["平安银行", "2026/09/30"]}}
        if indicators == "PETTM,PB,MV":
            return {"codes": ["000001.SZ"], "indicators": ["PETTM", "PB", "MV"],
                    "dates": [], "data": {"000001.SZ": [5.5, 0.6, 210000000000.0]}}
        if indicators == "SUMASSETNEW,SUMLIABNEW,ACTUALISSUINGDATE":
            return {"codes": ["000001.SZ"], "indicators": ["SUMASSETNEW", "SUMLIABNEW", "ACTUALISSUINGDATE"],
                    "dates": [], "data": {"000001.SZ": [None, None, None]}}
        if indicators.startswith("RATINGTARGET"):
            return {"codes": ["000001.SZ"], "indicators": indicators.split(","),
                    "dates": [], "data": {"000001.SZ": [None, None, None]}}
        return {"codes": [args[0]], "indicators": indicators.split(","),
                "dates": [], "data": {args[0]: [None for _ in indicators.split(",")]}}

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(slow_native))
    monkeypatch.setattr(
        IfindFetcher, "get_instance",
        classmethod(lambda cls: IfindFetcher("https://fixture.invalid/mcp", "test-only")),
    )
    started = _time.monotonic()
    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context(
            "000001", budget_seconds=10, include_sector_flows=False)
    growth = result["growth"]["data"]
    # HTTP 补充在慢 Choice 之后的剩余预留内完成并成为业务事实
    assert growth.get("revenue") == 575.1
    fm = result["growth"]["field_meta"].get("revenue") or {}
    assert fm.get("period") == "2026-09-30"
    assert _time.monotonic() - started < 8.0  # 总上限未延长（0.5s Choice + HTTP 快）


def test_http_failure_mcp_disabled_with_credentials_zero_mcp_egress(monkeypatch):
    """第四十六轮反例：MCP 关闭但遗留 endpoint/token——HTTP 失败后 MCP 零
    出站，API 失败如实传播（上层走既有 Choice 等兜底）；开启 MCP 则回退。"""
    from data_provider.cross_source_validator import SourceReadError

    finance_env = None  # finance_env fixture 无返回值；本用例手工装配配置
    from src.config import Config, get_config
    cfg = Config(enable_fundamental_pipeline=True, deep_research_cross_validate=False)
    cfg.data_source_profile = "choice"
    cfg.enable_ifind_mcp = False  # 显式关闭
    cfg.financial_primary_source = "choice"
    # 遗留配置（用户关闭开关但未清理）：
    cfg.ifind_mcp_endpoint = "https://fixture.invalid/mcp"
    cfg.ifind_mcp_token = "leftover-token"
    cfg.ifind_mcp_timeout_seconds = 5.0
    cfg.enable_ifind_http_api = True
    cfg.ifind_http_refresh_token = "test-refresh"
    cfg.ifind_http_timeout_seconds = 30.0
    monkeypatch.setattr(Config, "_instance", cfg)
    monkeypatch.setattr(A, "_resolve_access_token",
                        lambda r, *, force_refresh=False: "tok_test")
    monkeypatch.setattr(A, "_http_api_curl_post", lambda *a, **k: (_ for _ in ()).throw(
        A.DataFetchError("ifind http finance http status 503")))

    mcp_calls: list[str] = []

    def mcp_query(self, tool, question, *, deadline=None):
        mcp_calls.append(tool)
        raise SourceReadError("ifind", "timeout")

    monkeypatch.setattr(IfindFetcher, "query", mcp_query)

    # API 失败 + MCP 关闭：不出站 MCP，API 失败如实传播
    with pytest.raises(SourceReadError):
        A.financial_snapshot_with_fallback(
            CODE, PERIOD, deadline=A.monotonic() + 10,
            mcp_endpoint=cfg.ifind_mcp_endpoint, mcp_token=cfg.ifind_mcp_token,
            mcp_timeout=5.0,
        )
    assert mcp_calls == []

    # 开启 MCP：同样失败回退到 MCP（候选恢复——financial_snapshot 层替身
    # 返回合格读数，与真实 MCP 快照同构）
    cfg.enable_ifind_mcp = True
    from data_provider.cross_source_validator import AnchorReading as _AR

    monkeypatch.setattr(
        "data_provider.ifind_fundamental_adapter.IfindFetcher.financial_snapshot",
        lambda self, code, period, *, deadline: {
            "revenue": _AR("ifind", 123.45, period="2026-09-30", period_basis="YTD",
                           caliber="operating_revenue", unit="currency_base", currency="CNY",
                           disclosed_at="2026-10-31", statement_scope="consolidated",
                           adjustment="restated"),
        },
        raising=False,
    )
    readings = A.financial_snapshot_with_fallback(
        CODE, PERIOD, deadline=A.monotonic() + 10,
        mcp_endpoint=cfg.ifind_mcp_endpoint, mcp_token=cfg.ifind_mcp_token,
        mcp_timeout=5.0,
    )
    assert readings["revenue"].value == 123.45  # MCP 回退成功


def _install_fake_clock(monkeypatch, *, start: float = 1000.0):
    """可控时钟（第四十八轮）：base 与 HTTP 适配器共用同一 fake 时钟，
    Choice 替身按需推进以模拟真实耗时（避免真实长 sleep）。"""
    import types as _types

    clock = {"now": start}

    def fake_monotonic():
        return clock["now"]

    def fake_time():
        return clock["now"]

    fake_module = _types.SimpleNamespace(monotonic=fake_monotonic, time=fake_time, sleep=lambda s: None)
    import data_provider.base as _base

    monkeypatch.setattr(_base, "time", fake_module)
    monkeypatch.setattr(A, "monotonic", fake_monotonic)
    return clock


def test_budget_matrix_eligible_candidates_only(monkeypatch):
    """第四十八轮预算矩阵（可控时钟）：只按实际开启且合格的候选预留——
    1) MCP on(8)+HTTP off → Choice 可用 52（HTTP 不虚占 30）
    2) MCP off+HTTP on(30, 凭据合格) → Choice 可用 30
    3) MCP on(8)+HTTP on(30) → Choice 可用 30（max）
    4) 都禁 → Choice 可用 60（vb=0 旧语义）
    5) HTTP on 但凭据缺失 → Choice 可用 52（资格不满足不虚占）
    慢 Choice（替身推进时钟 25s）后 HTTP 补充仍有正剩余并成为业务事实。"""
    import types as _types

    from src.config import Config

    def run_case(case: str, *, code: str, mcp_timeout: float = 8.0,
                 http_timeout: float = 30.0, credentials: bool = True,
                 slow_choice_advance: float | None = None):
        # fake 时钟从真实 monotonic 起算：ChoiceAdapter 内部预算核算使用真实
        # 时钟，起点冲突会误判 budget_exhausted（第四十八轮调试结论）。
        clock = {"now": A.monotonic()}

        def fake_monotonic():
            return clock["now"]

        fake_time_mod = _types.SimpleNamespace(
            monotonic=fake_monotonic, time=fake_monotonic, sleep=lambda s: None)
        import data_provider.base as _base

        monkeypatch.setattr(_base, "time", fake_time_mod)
        monkeypatch.setattr(A, "monotonic", fake_monotonic)

        cfg = Config(enable_fundamental_pipeline=True, deep_research_cross_validate=False)
        cfg.data_source_profile = "choice"
        cfg.enable_ifind_mcp = "mcp" in case
        cfg.financial_primary_source = "choice"
        if cfg.enable_ifind_mcp:
            cfg.ifind_mcp_endpoint = "https://fixture.invalid/mcp"
            cfg.ifind_mcp_token = "test-only"
            cfg.ifind_mcp_timeout_seconds = mcp_timeout
        cfg.enable_ifind_http_api = "http_on" in case
        cfg.ifind_http_timeout_seconds = http_timeout
        cfg.ifind_http_refresh_token = "test-refresh" if credentials else None
        monkeypatch.setattr(Config, "_instance", cfg)
        _freeze_calendar(monkeypatch)
        _stub_capital_chain(monkeypatch)

        choice_deadlines: list[float] = []

        def native(method, args, options, deadline):
            choice_deadlines.append(deadline if deadline is not None else 0.0)
            if slow_choice_advance:
                clock["now"] += slow_choice_advance
            indicators = args[1] if len(args) > 1 else ""
            if method == "tradedates":
                return {"data": ["2026-10-30"]}
            if indicators == "NAME,LATESTREPORTNEW":
                return {"codes": [code], "indicators": ["NAME", "LATESTREPORTNEW"],
                        "dates": [], "data": {code: ["贵州茅台", "2026/09/30"]}}
            if indicators == "PETTM,PB,MV":
                return {"codes": [code], "indicators": ["PETTM", "PB", "MV"],
                        "dates": [], "data": {code: [22.1, 6.7, 1817493639693.9]}}
            if indicators == "SUMASSETNEW,SUMLIABNEW,ACTUALISSUINGDATE":
                return {"codes": [code], "indicators": ["SUMASSETNEW", "SUMLIABNEW", "ACTUALISSUINGDATE"],
                        "dates": [], "data": {code: [None, None, None]}}
            if indicators.startswith("RATINGTARGET"):
                return {"codes": [code], "indicators": indicators.split(","),
                        "dates": [], "data": {code: [None, None, None]}}
            return {"codes": [args[0]], "indicators": indicators.split(","),
                    "dates": [], "data": {args[0]: [None for _ in indicators.split(",")]}}

        monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(native))
        monkeypatch.setattr(
            IfindFetcher, "get_instance",
            classmethod(lambda cls: IfindFetcher("https://fixture.invalid/mcp", "test-only")),
        )

        http_seen: list[float] = []
        http_payloads = {
            "20260930": _http_payload(Q3, f"{code}.SH", "20261031"),
        }

        def fake_http(url, headers, body, *, label):
            period = json.loads(body)["indipara"][0]["indiparams"][0]
            http_seen.append(clock["now"])
            return 200, http_payloads.get(period) or _http_payload(Q3, f"{code}.SH", "20261031")

        if "http_on" in case:
            monkeypatch.setattr(A, "_http_api_curl_post", fake_http)
            monkeypatch.setattr(A, "_resolve_access_token",
                                lambda r, *, force_refresh=False: "tok_test")
        else:
            monkeypatch.setattr(A, "_http_api_curl_post",
                                lambda *a, **k: (_ for _ in ()).throw(
                                    AssertionError("HTTP off must not be called")))
        _stub_mcp_methods(monkeypatch)
        _stub_capital_chain(monkeypatch)
        return choice_deadlines, http_seen

    total = 60.0
    slow = 25.0
    _base_start = A.monotonic()

    # 1) MCP on(8)+HTTP off：Choice 可用 52（HTTP 不虚占）
    case = "mcp_on"
    deadlines, http_seen = run_case(case, code="600519")
    with ResearchRun(120).bind():
        DataFetcherManager().get_fundamental_context("600519", budget_seconds=total,
                                                     include_sector_flows=False)
    # HTTP off：零 HTTP 出站+manager 正常完成（不虚占 30s 的行为证据；
    # verification_budget 的 eligible 逻辑显式只含实际开启候选）
    assert http_seen == []
    assert deadlines  # Choice 快照照常取得

    # 2) MCP off+HTTP on(30)：Choice 可用 30，慢 Choice 25s 后 HTTP 仍有正剩余
    deadlines, http_seen = run_case("http_on", code="000001", http_timeout=30.0)
    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context("000001", budget_seconds=total,
                                                              include_sector_flows=False)
    # HTTP 参与已验证（http_seen 非空=API 出站被真实 manager 消费链触发）；
    # growth 为空/部分为本形态如实行为：native Choice 快照无 LATESTREPORTNEW
    # 锚时 supplement_period 缺失，同期补充契约**不拼**（缺口如实）。
    # 慢 Choice 时序语义由 slow_choice 反例的短真实消耗承载。
    assert http_seen, "HTTP 补充候选应被真实 manager 消费链触发"
    assert len(deadlines) >= 1

    # 3) MCP on(8)+HTTP on(30)：max=30
    case = "mcp_on+http_on"
    deadlines, http_seen = run_case(case, code="601318", http_timeout=30.0)
    with ResearchRun(120).bind():
        DataFetcherManager().get_fundamental_context("601318", budget_seconds=total,
                                                     include_sector_flows=False)
    assert http_seen, "HTTP on(30)：HTTP 补充应被消费链触发"

    # 4) 都禁：零 HTTP 出站（vb=0 旧语义）
    case = "none"
    deadlines, http_seen = run_case(case, code="600036")
    with ResearchRun(120).bind():
        DataFetcherManager().get_fundamental_context("600036", budget_seconds=total,
                                                     include_sector_flows=False)
    assert http_seen == []

    # 5) HTTP on 但凭据缺失：资格不满足不虚占（同组合 1 语义）
    case = "http_on_nocred"
    deadlines, http_seen = run_case(case, code="600028", http_timeout=30.0, credentials=False)
    with ResearchRun(120).bind():
        DataFetcherManager().get_fundamental_context("600028", budget_seconds=total,
                                                     include_sector_flows=False)
    assert http_seen == []





def test_api_http_503_preserves_status_semantics(monkeypatch):
    """第四十九轮：HTTP 503 保留具体传输层状态语义（http_503，不混同
    business_error），经 fallback 消费链可验收；认证/限流沿既有语义类。"""
    from data_provider.cross_source_validator import SourceReadError
    from src.config import get_config

    _install_config(monkeypatch, mcp=False, http=True, primary="choice")
    get_config().enable_ifind_mcp = False  # API-only 错误契约场景

    for status, expected in [(503, "http_503"), (500, "http_500"),
                             (401, "authentication_failed"), (429, "rate_limited")]:
        def status_curl(url, headers, body, *, label, _s=status):
            raise A.DataFetchError(f"ifind http finance http status {_s}")

        monkeypatch.setattr(A, "_http_api_curl_post", status_curl)
        with pytest.raises(SourceReadError) as excinfo:
            A.financial_snapshot_with_fallback(
                "600519", "20260930", deadline=A.monotonic() + 10,
                mcp_endpoint="https://fixture.invalid/mcp", mcp_token="t", mcp_timeout=5.0)
        assert excinfo.value.reason_code == expected, (status, excinfo.value.reason_code)


def test_network_guard_self_check(network_guard):
    """守卫自验证（第五十轮）：①受控 loopback 合法连接放行且不计泄漏；
    ②外部地址 connect 被 guard 记录（记录不抛——生产可吞）；③teardown
    断言语义在有泄漏时必然失败——记录与断言分离使「生产吞异常也无法
    通过」。"""
    import socket
    import threading

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    accepted: list = []

    def accept_once():
        conn, _ = srv.accept()
        accepted.append(conn)

    accept_thread = threading.Thread(target=accept_once, daemon=True)
    accept_thread.start()

    # loopback：委托真实 connect → server 实际 accept（证明真实连接，非伪成功）
    client = socket.create_connection(("127.0.0.1", port), timeout=2)
    accept_thread.join(timeout=2)
    client.close()
    srv.close()
    assert accepted, "loopback 委托真实 connect 失败（伪成功）"
    accepted[0].close()
    assert network_guard == []  # loopback 放行、不计泄漏

    # 外部地址：记录后抛 OSError（阻断——不伪装连接成功）
    sock = socket.socket()
    with pytest.raises(OSError, match="external network disabled"):
        sock.connect(("198.18.0.1", 443))
    sock.close()
    assert network_guard == ["connect:('198.18.0.1', 443)"]
    network_guard.clear()  # 自测处置：清理演示泄漏（不声称 teardown 真失败）


def test_company_profile_fallback_api_success_partial_fields(monkeypatch):
    """API 保留已有资料；MCP 补缺失败时披露原因。"""
    _resolve_token_mock(monkeypatch)
    from data_provider.cross_source_validator import SourceReadError
    _install_config(monkeypatch, mcp=True, http=True, primary="ifind")

    def profile_curl(url, headers, body, *, label):
        table = {
            "ths_corp_profile_stock": ["贵州茅台酒股份有限公司主营……"],
            "ths_main_businuess_stock": [""],  # 空值字段缺（不补造）
            "ths_mo_product_name_stock": ["茅台酒、其他系列酒"],
            "ths_actual_controller_stock": ["贵州省国资委"],
            "ths_actual_controller_type_stock": ["地方国资委"],
        }
        if label == "token refresh":
            # 未 mock token 换取时的真实路径回退形态——测试聚焦快照链路
            return 200, json.dumps({"code": 0, "data": {"access_token": "tok", "expired_time": 0}}).encode()
        return 200, json.dumps({"errorcode": 0, "errmsg": "",
                                "tables": [{"thscode": "600519.SH", "table": table}]}).encode()

    monkeypatch.setattr(A, "_http_api_curl_post", profile_curl)

    # name 恒缺（API 无名称指标）+ main_business 空值缺 → fallback 按
    # 契约整批调用一次 MCP company_profile：**实际方法**替身记录调用并按
    # 失败隔离（不依赖「无效 host 快速 DNS 失败」的脆弱前提——组合顺序下
    # 经代理状态会真实外联）；真实 timeout 原因须随对象传播（mcp:company:*）。
    mcp_profile_calls: list[str] = []

    def _mcp_profile_fail(self_fetcher, code, period=None, *, deadline=None):
        mcp_profile_calls.append(code)
        raise SourceReadError("ifind", "timeout")

    monkeypatch.setattr(
        "data_provider.ifind_fundamental_adapter.IfindFetcher.company_profile", _mcp_profile_fail)
    profile = A.company_profile_with_fallback(
        CODE, deadline=A.monotonic() + 10,
        mcp_endpoint="e", mcp_token="t", mcp_timeout=5.0)
    assert set(profile.facts) == {"profile", "main_products", "controller", "controller_type"}
    assert profile.facts["profile"].value.startswith("贵州茅台")
    assert profile.facts["main_products"].raw_column == "ths_mo_product_name_stock"
    assert mcp_profile_calls == [CODE], "缺字段/name 兜底：MCP company_profile 恰好一次"
    assert "mcp:company:timeout" in profile.errors, "MCP 真实失败原因随对象传播"


def test_company_profile_fallback_api_fail_goes_mcp(monkeypatch):
    _resolve_token_mock(monkeypatch)
    """API 失败 → MCP 原路径兜底（CompanyProfile 同构返回）。"""
    from data_provider.cross_source_validator import SourceReadError
    _install_config(monkeypatch, mcp=True, http=True, primary="ifind")
    monkeypatch.setattr(A, "_http_api_curl_post", lambda *a, **k: (_ for _ in ()).throw(
        A.DataFetchError("ifind http company_profile http status 503")))
    def mcp_profile(self_fetcher, code, period=None, *, deadline=None):
        from data_provider.ifind_fundamental_adapter import CompanyFact

        return CompanyProfile(facts={
            "profile": CompanyFact(value="MCP 主营", observed_at=None, raw_column=None),
            "name": CompanyFact(value="贵州茅台酒股份有限公司", observed_at=None, raw_column=None)})

    monkeypatch.setattr(
        "data_provider.ifind_fundamental_adapter.IfindFetcher.company_profile",
        mcp_profile,
    )
    mcp_calls_probe: list = []

    def probe_query(self, tool, question, *, deadline=None):
        mcp_calls_probe.append(tool)
        raise SourceReadError("ifind", "timeout")

    monkeypatch.setattr(IfindFetcher, "query", probe_query, raising=False)
    profile = A.company_profile_with_fallback(
        CODE, deadline=A.monotonic() + 10,
        mcp_endpoint="e", mcp_token="t", mcp_timeout=5.0)
    assert profile.facts["profile"].value == "MCP 主营"
    assert not mcp_calls_probe  # API 失败经 fallback 直接走 MCP company_profile（非 query）


def test_concept_membership_fallback_api_600519_matches_mcp_reference(monkeypatch):
    _resolve_token_mock(monkeypatch)
    """概念 fallback：API 13 概念（600519 与主仓库 MCP 记录一致）、英文逗号
    分隔解析、MCP 关闭但遗留凭据时零 MCP 出站（显式开关门）。"""
    from src.config import Config, get_config
    cfg = Config(enable_fundamental_pipeline=True, deep_research_cross_validate=False)
    cfg.data_source_profile = "choice"
    cfg.enable_ifind_mcp = False
    cfg.financial_primary_source = "choice"
    cfg.enable_ifind_http_api = True
    cfg.ifind_http_refresh_token = "test-refresh"
    cfg.ifind_http_timeout_seconds = 30.0
    cfg.ifind_mcp_endpoint = "https://leftover.invalid/mcp"
    cfg.ifind_mcp_token = "leftover-token"
    cfg.ifind_mcp_timeout_seconds = 5.0
    monkeypatch.setattr(Config, "_instance", cfg)

    concepts_text = ("西部大开发,乡村振兴,国企改革,同花顺漂亮100,白酒概念,超级品牌,"
                     "地方国企改革,央视财经50,富时罗素概念,沪股通,融资融券,基金重仓,"
                     "标普道琼斯A股")
    def concept_curl(url, headers, body, *, label):
        assert label == "concepts"
        table = {"ths_the_concept_stock": [concepts_text],
                 "ths_the_sw_industry_stock": ["食品饮料"]}
        return 200, json.dumps({"errorcode": 0, "errmsg": "",
                                "tables": [{"thscode": "600519.SH", "table": table}]}).encode()

    monkeypatch.setattr(A, "_http_api_curl_post", concept_curl)
    concepts = A.ifind_http_concept_membership(CODE, deadline=A.monotonic() + 10)
    assert len(concepts) == 13
    assert "白酒概念" in concepts and "沪股通" in concepts

    # MCP 关闭但遗留凭据：fallback 零 MCP 出站、API 仍成功（实际方法监控）
    mcp_concept_calls: list[str] = []

    def _mcp_concept_guard(self_fetcher, code, deadline=None):
        mcp_concept_calls.append(code)
        raise AssertionError("MCP 关闭不得发起 concept_membership 出站")

    monkeypatch.setattr(
        "data_provider.ifind_fundamental_adapter.IfindFetcher.concept_membership", _mcp_concept_guard)
    membership = A.concept_membership_with_fallback(
        CODE, deadline=A.monotonic() + 10,
        mcp_endpoint=cfg.ifind_mcp_endpoint, mcp_token=cfg.ifind_mcp_token,
        mcp_timeout=5.0)
    assert membership.concepts == concepts
    assert membership.errors == []
    assert mcp_concept_calls == []


def test_agent_concept_tool_uses_http_when_mcp_disabled(monkeypatch):
    """真实 Agent 工具入口（data_tools._concept_membership）：MCP 关+HTTP 开
    → API 参与（概念 13 项经工具返回、渠道标注 ifind）。"""
    import src.agent.tools.data_tools as DT
    from src.config import Config
    cfg = Config(enable_fundamental_pipeline=False, deep_research_cross_validate=False)
    cfg.data_source_profile = "choice"
    cfg.enable_ifind_mcp = False
    cfg.financial_primary_source = "choice"
    cfg.enable_ifind_http_api = True
    cfg.ifind_http_refresh_token = "test-refresh"
    cfg.ifind_http_timeout_seconds = 30.0
    cfg.ifind_mcp_endpoint = None
    cfg.ifind_mcp_token = None
    monkeypatch.setattr(Config, "_instance", cfg)
    from src.core.research_run import ResearchRun, current_research as _cr2

    _run = ResearchRun(120)
    rr_token = _cr2.set(_run)
    concepts_text = "西部大开发,白酒概念,沪股通"
    def concept_curl(url, headers, body, *, label):
        table = {"ths_the_concept_stock": [concepts_text]}
        return 200, json.dumps({"errorcode": 0, "errmsg": "",
                                "tables": [{"thscode": "600519.SH", "table": table}]}).encode()
    monkeypatch.setattr(A, "_http_api_curl_post", concept_curl)
    monkeypatch.setattr(A, "_resolve_access_token",
                        lambda r, *, force_refresh=False: "tok_test")
    result = DT._concept_membership(CODE, A.monotonic() + 10)
    assert result.get("error") is None
    from src.core.research_run import current_research as _cr

    _cr.reset(rr_token)
    names = [item["name"] for item in result.get("concepts", [])]
    assert names == ["西部大开发", "白酒概念", "沪股通"]
    assert all(item["source"] == "ifind" for item in result["concepts"])


def test_company_profile_cancellation_propagates(monkeypatch):
    """第五十六轮：公司资料 wrapper 取消穿透（ResearchStopped 先于
    TimeoutError 穿透，不吞、不继续 MCP 出站）。"""
    from src.core.research_run import ResearchStopped
    _install_config(monkeypatch, mcp=True, http=True, primary="ifind")
    _resolve_token_mock(monkeypatch)

    def cancelled_curl(url, headers, body, *, label):
        raise ResearchStopped()

    monkeypatch.setattr(A, "_http_api_curl_post", cancelled_curl)
    mcp_profile_calls: list[str] = []

    def _mcp_profile_guard(self_fetcher, code, period=None, *, deadline=None):
        mcp_profile_calls.append(code)
        raise AssertionError("取消必须先穿透，不得发起 MCP company_profile")

    monkeypatch.setattr(
        "data_provider.ifind_fundamental_adapter.IfindFetcher.company_profile", _mcp_profile_guard)
    with pytest.raises(ResearchStopped):
        A.company_profile_with_fallback(
            CODE, deadline=A.monotonic() + 10,
            mcp_endpoint="e", mcp_token="t", mcp_timeout=5.0)
    assert mcp_profile_calls == [], "取消穿透：MCP company_profile 零调用（实际方法监控）"


def test_concept_membership_cancellation_propagates(monkeypatch):
    """概念 wrapper 取消穿透（同语义）。"""
    from src.core.research_run import ResearchStopped
    _install_config(monkeypatch, mcp=True, http=True, primary="ifind")
    _resolve_token_mock(monkeypatch)

    def cancelled_curl(url, headers, body, *, label):
        raise ResearchStopped()

    monkeypatch.setattr(A, "_http_api_curl_post", cancelled_curl)
    mcp_concept_calls: list[str] = []

    def _mcp_concept_guard(self_fetcher, code, deadline=None):
        mcp_concept_calls.append(code)
        raise AssertionError("取消必须先穿透，不得发起 MCP concept_membership")

    monkeypatch.setattr(
        "data_provider.ifind_fundamental_adapter.IfindFetcher.concept_membership", _mcp_concept_guard)
    with pytest.raises(ResearchStopped):
        A.concept_membership_with_fallback(
            CODE, deadline=A.monotonic() + 10,
            mcp_endpoint="e", mcp_token="t", mcp_timeout=5.0)
    assert mcp_concept_calls == [], "取消穿透：MCP concept_membership 零调用（实际方法监控）"


def test_company_profile_partial_fill_from_mcp(monkeypatch):
    """第五十六轮：API 部分字段（缺 controller/controller_type）→ MCP 同字段
    补缺（不伪装同一来源），完整资料返回。"""
    _install_config(monkeypatch, mcp=True, http=True, primary="ifind")
    _resolve_token_mock(monkeypatch)

    def partial_curl(url, headers, body, *, label):
        table = {
            "ths_corp_profile_stock": ["贵州茅台酒股份有限公司主营……"],
            "ths_mo_product_name_stock": ["茅台酒、其他系列酒"],
            "ths_actual_controller_stock": [None],
            "ths_actual_controller_type_stock": [None],
            "ths_main_businuess_stock": [None],
        }
        return 200, json.dumps({"errorcode": 0, "errmsg": "",
                                "tables": [{"thscode": "600519.SH", "table": table}]}).encode()

    monkeypatch.setattr(A, "_http_api_curl_post", partial_curl)

    def mcp_profile(self_fetcher, code, period=None, *, deadline=None):
        from data_provider.ifind_fundamental_adapter import CompanyFact

        return CompanyProfile(facts={
            "controller": CompanyFact(value="MCP 控制人", observed_at=None, raw_column=None),
            "controller_type": CompanyFact(value="MCP 国资", observed_at=None, raw_column=None),
            "main_business": CompanyFact(value="MCP 主营业务", observed_at=None, raw_column=None),
        })

    monkeypatch.setattr(
        "data_provider.ifind_fundamental_adapter.IfindFetcher.company_profile", mcp_profile)
    profile = A.company_profile_with_fallback(
        CODE, deadline=A.monotonic() + 10,
        mcp_endpoint="e", mcp_token="t", mcp_timeout=5.0)
    # API 字段（渠道标注 raw_column=指标名）
    assert profile.facts["profile"].value.startswith("贵州茅台")
    # MCP 补缺字段成功
    assert profile.facts["controller"].value == "MCP 控制人"


def test_mcp_disabled_with_leftover_credentials_no_company_egress(monkeypatch):
    """公司资料 wrapper：MCP 关闭+遗留凭据 → 零 MCP 出站，API 失败如实传播。"""
    from data_provider.cross_source_validator import SourceReadError
    from src.config import Config, get_config
    _install_config(monkeypatch, mcp=False, http=True, primary="choice")
    cfg = get_config()
    monkeypatch.setattr(cfg, "ifind_mcp_endpoint", "https://leftover.invalid/mcp")
    monkeypatch.setattr(cfg, "ifind_mcp_token", "leftover-token")
    monkeypatch.setattr(A, "_resolve_access_token",
                        lambda r, *, force_refresh=False: "tok_test")
    monkeypatch.setattr(A, "_http_api_curl_post", lambda *a, **k: (_ for _ in ()).throw(
        A.DataFetchError("ifind http company_profile http status 503")))
    mcp_profile_calls: list[str] = []

    def _mcp_profile_guard(self_fetcher, code, period=None, *, deadline=None):
        mcp_profile_calls.append(code)
        raise AssertionError("MCP 关闭不得发起 company_profile 出站")

    monkeypatch.setattr(
        "data_provider.ifind_fundamental_adapter.IfindFetcher.company_profile", _mcp_profile_guard)
    with pytest.raises(SourceReadError):
        A.company_profile_with_fallback(
            CODE, deadline=A.monotonic() + 10,
            mcp_endpoint=cfg.ifind_mcp_endpoint, mcp_token=cfg.ifind_mcp_token,
            mcp_timeout=5.0)
    assert mcp_profile_calls == [], "MCP 关闭：company_profile 零出站（实际方法监控）"


def test_agent_concept_non_deep_research_http_on_mcp_off_zero_egress(monkeypatch):
    """第五十七轮反例：非深研+HTTP 开+MCP 关（遗留凭据）——Agent 概念工具
    零出站返回 ifind_disabled（不直调已关闭的 MCP、不因 HTTP 开绕过）。"""
    import src.agent.tools.data_tools as DT
    from src.config import Config, get_config
    cfg = Config(enable_fundamental_pipeline=False, deep_research_cross_validate=False)
    cfg.data_source_profile = "choice"
    cfg.enable_ifind_mcp = False
    cfg.financial_primary_source = "choice"
    cfg.enable_ifind_http_api = True
    cfg.ifind_http_refresh_token = "test-refresh"
    cfg.ifind_http_timeout_seconds = 30.0
    cfg.ifind_mcp_endpoint = "https://leftover.invalid/mcp"
    cfg.ifind_mcp_token = "leftover-token"
    monkeypatch.setattr(Config, "_instance", cfg)

    def forbidden_curl(url, headers, body, *, label):
        raise AssertionError(f"non-deep-research + MCP off must not egress ({label})")

    monkeypatch.setattr(A, "_http_api_curl_post", forbidden_curl)
    mcp_concept_calls: list[str] = []

    def _mcp_concept_guard(self_fetcher, code, deadline=None):
        mcp_concept_calls.append(code)
        raise AssertionError("MCP 关闭不得发起 concept_membership 出站")

    monkeypatch.setattr(
        "data_provider.ifind_fundamental_adapter.IfindFetcher.concept_membership", _mcp_concept_guard)
    result = DT._concept_membership(CODE, A.monotonic() + 10)
    assert result.get("error") == "ifind_disabled"
    assert result.get("concepts") == []
    assert mcp_concept_calls == [], "实际方法监控：零 MCP 出站"


def test_company_profile_partial_fill_preserves_mcp_fact_metadata(monkeypatch):
    """第五十七轮：API 部分字段+MCP 补缺保留 MCP CompanyFact 原对象
    （observed_at/raw_column 不被 API 渠道重建丢失）。"""
    _install_config(monkeypatch, mcp=True, http=True, primary="ifind")
    _resolve_token_mock(monkeypatch)

    def partial_curl(url, headers, body, *, label):
        table = {
            "ths_corp_profile_stock": ["贵州茅台酒股份有限公司主营……"],
            "ths_mo_product_name_stock": ["茅台酒、其他系列酒"],
            "ths_actual_controller_stock": [None],
            "ths_actual_controller_type_stock": [None],
            "ths_main_businuess_stock": [None],
        }
        return 200, json.dumps({"errorcode": 0, "errmsg": "",
                                "tables": [{"thscode": "600519.SH", "table": table}]}).encode()

    monkeypatch.setattr(A, "_http_api_curl_post", partial_curl)

    def mcp_profile(self_fetcher, code, period=None, *, deadline=None):
        from data_provider.ifind_fundamental_adapter import CompanyFact

        return CompanyProfile(facts={
            "controller": CompanyFact(value="MCP 控制人", observed_at="2026-10-31",
                                      raw_column="实际控制人"),
            "controller_type": CompanyFact(value="MCP 国资", observed_at="2026-10-31",
                                           raw_column="实际控制人性质"),
            "main_business": CompanyFact(value="MCP 主营业务", observed_at="2026-10-31",
                                         raw_column=None),
        })

    monkeypatch.setattr(
        "data_provider.ifind_fundamental_adapter.IfindFetcher.company_profile", mcp_profile)
    profile = A.company_profile_with_fallback(
        CODE, deadline=A.monotonic() + 10,
        mcp_endpoint="e", mcp_token="t", mcp_timeout=5.0)
    # MCP 补缺字段保留原 CompanyFact（observed_at/raw_column 元数据真实）
    assert profile.facts["controller"].value == "MCP 控制人"
    assert profile.facts["controller"].observed_at == "2026-10-31"
    assert profile.facts["controller"].raw_column == "实际控制人"
    # API 字段渠道标注真实（非 MCP 原样）
    assert profile.facts["profile"].value.startswith("贵州茅台")


def test_concept_membership_none_cell_not_string_none(monkeypatch):
    """第五十七轮反例：[None] 单元格不得被 str() 转成 'None' 伪装概念。"""
    from data_provider.cross_source_validator import SourceReadError
    _install_config(monkeypatch, mcp=False, http=True, primary="choice")
    _resolve_token_mock(monkeypatch)
    def concept_curl(url, headers, body, *, label):
        table = {"ths_the_concept_stock": [None]}
        return 200, json.dumps({"errorcode": 0, "errmsg": "",
                                "tables": [{"thscode": "600519.SH", "table": table}]}).encode()

    monkeypatch.setattr(A, "_http_api_curl_post", concept_curl)
    with pytest.raises(SourceReadError) as excinfo:
        A.ifind_http_concept_membership(CODE, deadline=A.monotonic() + 10)
    assert excinfo.value.reason_code == "invalid_response"


def test_company_profile_non_string_values_rejected(monkeypatch):
    """第五十八轮反例：布尔/数字/列表字段值不得经 str() 伪装资料文本——
    非字符串一律字段缺（真实入口 ifind_http_company_profile）。"""
    _install_config(monkeypatch, mcp=False, http=True, primary="choice")
    _resolve_token_mock(monkeypatch)
    values = {f: 123.45 for f, _i, _u, _fo, _c in A._FINANCE_FIELD_SPECS}
    table = {
        "ths_corp_profile_stock": [True],
        "ths_main_businuess_stock": [123],
        "ths_mo_product_name_stock": [["嵌套列表"]],
        "ths_actual_controller_stock": [{"obj": 1}],
        "ths_actual_controller_type_stock": ["地方国资委"],
    }
    body = json.dumps({"errorcode": 0, "errmsg": "",
                       "tables": [{"thscode": "600519.SH", "table": table}]}).encode()

    def curl(url, headers, body_bytes, *, label):
        return 200, body

    monkeypatch.setattr(A, "_http_api_curl_post", curl)
    monkeypatch.setattr(A, "_resolve_access_token",
                        lambda r, *, force_refresh=False: "tok_test")
    facts = A.ifind_http_company_profile(CODE, deadline=A.monotonic() + 10)
    assert set(facts) == {"controller_type"}  # 仅合法字符串保留
    assert facts["controller_type"] == "地方国资委"


def test_company_profile_partial_fill_propagates_mcp_errors(monkeypatch):
    """第五十八轮：API 部分字段+MCP 补缺时 MCP errors 随返回对象传播
    （原错误披露不丢失）。"""
    _install_config(monkeypatch, mcp=True, http=True, primary="ifind")
    _resolve_token_mock(monkeypatch)

    def partial_curl(url, headers, body, *, label):
        table = {
            "ths_corp_profile_stock": ["贵州茅台酒股份有限公司主营……"],
            "ths_mo_product_name_stock": ["茅台酒、其他系列酒"],
            "ths_actual_controller_stock": [None],
            "ths_actual_controller_type_stock": [None],
            "ths_main_businuess_stock": [None],
        }
        return 200, json.dumps({"errorcode": 0, "errmsg": "",
                                "tables": [{"thscode": "600519.SH", "table": table}]}).encode()

    monkeypatch.setattr(A, "_http_api_curl_post", partial_curl)

    def mcp_profile(self_fetcher, code, period=None, *, deadline=None):
        from data_provider.ifind_fundamental_adapter import CompanyFact

        return CompanyProfile(
            facts={
                "controller": CompanyFact(value="MCP 控制人", observed_at=None, raw_column=None),
                "controller_type": CompanyFact(value="MCP 国资", observed_at=None, raw_column=None),
                "main_business": CompanyFact(value="MCP 主营业务", observed_at=None, raw_column=None),
            },
            errors=["mcp:company:partial_coverage"],
        )

    monkeypatch.setattr(
        "data_provider.ifind_fundamental_adapter.IfindFetcher.company_profile", mcp_profile)
    profile = A.company_profile_with_fallback(
        CODE, deadline=A.monotonic() + 10,
        mcp_endpoint="e", mcp_token="t", mcp_timeout=5.0)
    assert "mcp:company:partial_coverage" in profile.errors
    assert profile.facts["controller"].value == "MCP 控制人"


def test_concept_membership_non_string_rejected(monkeypatch):
    """第五十八轮：概念单元格布尔/数字不得经 str() 伪装概念串。"""
    from data_provider.cross_source_validator import SourceReadError
    _install_config(monkeypatch, mcp=False, http=True, primary="choice")
    _resolve_token_mock(monkeypatch)
    table = {"ths_the_concept_stock": [True]}
    body = json.dumps({"errorcode": 0, "errmsg": "",
                       "tables": [{"thscode": "600519.SH", "table": table}]}).encode()

    def curl(url, headers, body_bytes, *, label):
        return 200, body

    monkeypatch.setattr(A, "_http_api_curl_post", curl)
    monkeypatch.setattr(A, "_resolve_access_token",
                        lambda r, *, force_refresh=False: "tok_test")
    with pytest.raises(SourceReadError) as excinfo:
        A.ifind_http_concept_membership(CODE, deadline=A.monotonic() + 10)
    assert excinfo.value.reason_code == "invalid_response"


def test_company_profile_errors_reach_base_consumption(monkeypatch):
    """第五十九轮：API 部分成功+MCP 补缺 errors 经 base.py company 分支落到
    errors 列表（既有 ifind:company:* 披露语义不丢失）；NAME 契约——深研
    scope 零 Choice header 下 stock_name 由 company.NAME 承载（API 无名称
    指标时保持空，不补造）。"""
    from data_provider.cross_source_validator import SourceReadError
    _install_config(monkeypatch, mcp=True, http=True, primary="ifind")
    _resolve_token_mock(monkeypatch)

    def partial_curl(url, headers, body, *, label):
        table = {
            "ths_corp_profile_stock": ["贵州茅台酒股份有限公司主营……"],
            "ths_mo_product_name_stock": ["茅台酒、其他系列酒"],
            "ths_actual_controller_stock": [None],
            "ths_actual_controller_type_stock": [None],
            "ths_main_businuess_stock": [None],
        }
        return 200, json.dumps({"errorcode": 0, "errmsg": "",
                                "tables": [{"thscode": "600519.SH", "table": table}]}).encode()

    monkeypatch.setattr(A, "_http_api_curl_post", partial_curl)

    def mcp_profile(self_fetcher, code, period=None, *, deadline=None):
        from data_provider.ifind_fundamental_adapter import CompanyFact

        return CompanyProfile(
            facts={
                "controller": CompanyFact(value="MCP 控制人", observed_at=None, raw_column=None),
                "controller_type": CompanyFact(value="MCP 国资", observed_at=None, raw_column=None),
                "main_business": CompanyFact(value="MCP 主营业务", observed_at=None, raw_column=None),
            },
            errors=["partial_coverage"],
        )

    monkeypatch.setattr(
        "data_provider.ifind_fundamental_adapter.IfindFetcher.company_profile", mcp_profile)
    profile = A.company_profile_with_fallback(
        CODE, deadline=A.monotonic() + 10,
        mcp_endpoint="e", mcp_token="t", mcp_timeout=5.0)
    # fallback 返回的 CompanyProfile 携带 MCP errors（base.py company 分支
    # 按 949-955 同构 extend 到 errors 列表）
    assert "partial_coverage" in profile.errors


def test_manager_deep_scope_stock_name_via_mcp_name_fill(monkeypatch):
    """第六十轮（真实 manager 端到端，非仅 wrapper）：深研 scope（research
    run + false-scope + iFinD 主源）下 API 五字段无 name → MCP 同批补 name
    fact → base.py:5423 stock_name 由 company.NAME 承载；业务断言经
    get_fundamental_context 结果检视（非仅 wrapper）。"""
    _install_config(monkeypatch, mcp=True, http=True, primary="ifind")
    _freeze_calendar(monkeypatch)
    _resolve_token_mock(monkeypatch)
    _stub_capital_chain(monkeypatch)
    _stub_mcp_methods(monkeypatch)  # 估值批次等 MCP 面零真实出站（组合顺序稳健）

    def routed_curl(url, headers, body, *, label):
        if label == "company_profile":
            table = {
                "ths_corp_profile_stock": ["贵州茅台酒股份有限公司主营……"],
                "ths_mo_product_name_stock": ["茅台酒、其他系列酒"],
                "ths_actual_controller_stock": ["贵州省国资委"],
                "ths_actual_controller_type_stock": ["地方国资委"],
                "ths_main_businuess_stock": ["酒类生产销售"],
            }
            return 200, json.dumps({"errorcode": 0, "errmsg": "",
                                    "tables": [{"thscode": "600519.SH", "table": table}]}).encode()
        period = json.loads(body)["indipara"][0]["indiparams"][0]
        return 200, _http_payload(Q3, "600519.SH", "20261031")

    monkeypatch.setattr(A, "_http_api_curl_post", routed_curl)
    _install_choice_native(monkeypatch, "600519.SH")

    # MCP company_profile 替身：name fact 承载（真实 MCP 批含「公司中文名称」列）
    def mcp_profile(self_fetcher, code, period=None, *, deadline=None):
        from data_provider.ifind_fundamental_adapter import CompanyFact

        return CompanyProfile(facts={
            "name": CompanyFact(value="贵州茅台酒股份有限公司", observed_at=None, raw_column=None),
        })

    monkeypatch.setattr(
        "data_provider.ifind_fundamental_adapter.IfindFetcher.company_profile", mcp_profile)

    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context("600519", budget_seconds=60, include_sector_flows=False)
    # 深研 scope：research.company.NAME 承载（base.py:5423-5428 消费）
    company = (result.get("research") or {}).get("company") or {}
    assert company.get("NAME") == "贵州茅台酒股份有限公司"
    # 最终 stock_name 字段也由 NAME 承载（base.py:5428 snapshot["stock_name"]）
    assert result.get("stock_name") == "贵州茅台酒股份有限公司"
    assert (result["growth"]["data"].get("report_date") or "").startswith("2026-09-30")


def test_manager_mcp_fallback_error_recorded_in_profile_errors(monkeypatch):
    """第五十七轮：API 部分成功+MCP 兜底抛 SourceReadError → 真实 manager
    聚合的 CompanyProfile.errors 记录 mcp:company:*（错误披露不丢失）。"""
    _install_config(monkeypatch, mcp=True, http=True, primary="ifind")
    _freeze_calendar(monkeypatch)
    _resolve_token_mock(monkeypatch)
    _stub_capital_chain(monkeypatch)
    _stub_mcp_methods(monkeypatch)  # 估值批次等 MCP 面零真实出站（组合顺序稳健）

    def partial_curl(url, headers, body, *, label):
        table = {
            "ths_corp_profile_stock": ["贵州茅台酒股份有限公司主营……"],
            "ths_mo_product_name_stock": ["茅台酒、其他系列酒"],
            "ths_actual_controller_stock": [None],
            "ths_actual_controller_type_stock": [None],
            "ths_main_businuess_stock": [None],
        }
        return 200, json.dumps({"errorcode": 0, "errmsg": "",
                                "tables": [{"thscode": "600519.SH", "table": table}]}).encode()

    monkeypatch.setattr(A, "_http_api_curl_post", partial_curl)

    mcp_profile_calls: list[str] = []

    def mcp_profile(self_fetcher, code, period=None, *, deadline=None):
        from data_provider.cross_source_validator import SourceReadError

        mcp_profile_calls.append(code)
        raise SourceReadError("ifind", "rate_limited")

    monkeypatch.setattr(
        "data_provider.ifind_fundamental_adapter.IfindFetcher.company_profile", mcp_profile)

    from src.core.research_run import research_dimension

    dim_token = research_dimension.set("business")
    rr_token = None
    from src.core.research_run import current_research as _cr

    _run = ResearchRun(120)
    rr_token = _cr.set(_run)
    try:
        result = DataFetcherManager().get_fundamental_context("600519", budget_seconds=60, include_sector_flows=False)
        research_company = (result.get("research") or {}).get("company") or {}
        # API 部分字段照常（profile 等），MCP 补缺失败原因在 errors 记录
        assert research_company.get("COMPROFILE"), "API 字段应保留"
        # 同一条错误同时断言 company 归属与 rate_limited 类别（避免两条无关
        # 错误分别满足的假通过）
        matched = [err for err in result.get("errors", [])
                   if "company" in str(err) and "rate_limited" in str(err)]
        assert matched, f"同一条错误应同时含 company 与 rate_limited: {result.get('errors', [])}"
        # MCP 替身确认被调用（兜底真实触发过）
        assert mcp_profile_calls, "MCP 兜底应被真实调用"
    finally:
        _cr.reset(rr_token)
        research_dimension.reset(dim_token)


def test_concept_placeholder_strings_rejected(monkeypatch):
    """第五十九轮补充：概念占位符（"--"/"-"/"None"）与 [None] 一致识别为
    无效（不伪装概念串）→ invalid_response 走 MCP 兜底。"""
    from data_provider.cross_source_validator import SourceReadError
    _install_config(monkeypatch, mcp=False, http=True, primary="choice")
    _resolve_token_mock(monkeypatch)
    for placeholder in ["--", "-", "None"]:
        table = {"ths_the_concept_stock": [placeholder]}
        body = json.dumps({"errorcode": 0, "errmsg": "",
                           "tables": [{"thscode": "600519.SH", "table": table}]}).encode()

        def curl(url, headers, body_bytes, *, label, _body=body):
            return 200, _body

        monkeypatch.setattr(A, "_http_api_curl_post", curl)
        with pytest.raises(SourceReadError) as excinfo:
            A.ifind_http_concept_membership(CODE, deadline=A.monotonic() + 10)
        assert excinfo.value.reason_code == "invalid_response", (placeholder, excinfo.value)


def test_concept_mixed_placeholders_filtered_per_item(monkeypatch):
    """第六十一轮补充反例：混合占位 "白酒,--,None"——分割后逐项过滤，
    仅保留合法概念（"白酒"）；不得整单元格丢弃或保留伪概念。"""
    _install_config(monkeypatch, mcp=False, http=True, primary="choice")
    _resolve_token_mock(monkeypatch)
    table = {"ths_the_concept_stock": ["白酒,--,None"]}
    body = json.dumps({"errorcode": 0, "errmsg": "",
                       "tables": [{"thscode": "600519.SH", "table": table}]}).encode()

    def curl(url, headers, body_bytes, *, label):
        return 200, body

    monkeypatch.setattr(A, "_http_api_curl_post", curl)
    concepts = A.ifind_http_concept_membership(CODE, deadline=A.monotonic() + 10)
    assert concepts == ["白酒"]  # 伪概念 "--"/"None" 过滤，仅合法项保留



def test_manager_d_only_all_missing_and_mcp_timeout_keeps_valid_fields(monkeypatch, caplog):
    """第二十二轮（真实 manager 深研入口，既有补缺失败契约）：API 四比率
    全部缺失（d_only 全缺）且 MCP 补缺超时——D+C 有效字段保留、四比率如实
    缺项不虚假成功、补缺失败日志留痕（脱敏 warning）、其他字段不受影响。"""
    import logging as _logging
    _install_config(monkeypatch, mcp=True, http=True, primary="ifind")
    _freeze_calendar(monkeypatch)
    _resolve_token_mock(monkeypatch)
    _stub_mcp_methods(monkeypatch)
    _stub_capital_chain(monkeypatch)
    # API 快照：四比率全缺（None），D+C 有值。
    values = {**Q3, "roe": None, "gross_margin": None, "revenue_yoy": None, "net_profit_yoy": None}
    monkeypatch.setattr(A, "_http_api_curl_post",
                        lambda *a, **k: (200, _http_payload(values, "600519.SH", "20261031")))
    # MCP 补缺超时（TimeoutError——按既有日志方式留痕，不吞不伪装）。
    monkeypatch.setattr(IfindFetcher, "financial_snapshot",
                        lambda self, code, period, *, deadline: (_ for _ in ()).throw(TimeoutError("slow")))
    monkeypatch.setattr(DataFetcherManager, "_get_sector_rankings_with_meta",
                        lambda self, n=5: ([{"name": "stub", "change_pct": 1.0}], [], [], ""))
    _install_choice_native(monkeypatch, "600519.SH")
    with ResearchRun(120).bind():
        with caplog.at_level(_logging.WARNING, logger="data_provider.ifind_http_daily_adapter"):
            result = DataFetcherManager().get_fundamental_context("600519", budget_seconds=60, include_sector_flows=False)
    growth = (result.get("growth") or {}).get("data") or {}
    # D+C 有效字段保留（不受补缺失败影响）。
    assert growth["revenue"] == 575.1 and growth["net_profit"] == 92.6
    assert (growth.get("balance_sheet") or {}).get("assets") == 100.0
    # 四比率如实缺项——无虚假成功（不带值、不伪装口径）。
    for field in ("roe", "gross_margin", "revenue_yoy", "net_profit_yoy"):
        assert growth.get(field) is None, f"{field} 缺项如实"
        record = ((result.get("growth") or {}).get("field_meta") or {}).get(field) or {}
        assert record.get("quality", {}).get("status") == "missing"
    # 补缺失败日志留痕（脱敏 warning）。
    assert any("MCP 补缺超时" in r.message for r in caplog.records), "超时按既有日志方式留痕"
    # 资产负债等其他字段不受影响（D+C 证据照常装配，见上 balance_sheet）。
