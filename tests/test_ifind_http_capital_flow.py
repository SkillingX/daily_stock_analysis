# -*- coding: utf-8 -*-
"""深研个股资金流标准 API 纵向闭环回归（真实消费入口 ``get_main_inflow_cumulative``）。

第十一~十三轮接入与审查收敛锁定（证据见
``.claude/reviews/ifind-http-capital-flow-research-20261117.md``）：

- 深研资格 API 优先；请求起止由**本地完成交易日历**锚定，解析层严格请求
  窗口边界（晚于 enddate 的盘中未定稿行与早于 startdate 的窗口外旧日行
  均不采信）；null 保留为缺值行（停牌/T 缺 → 既有 T-1 回退 + 披露）；
- **当日 T 场景**（参考日与窗口日历同一时区语义 ``get_market_now``，跨时
  区 anchor 不误判历史日）：**T 日窗口 MCP 优先**（至多一次）——MCP 非空
  序列（含全 null/不完整形态）一律交原归一/窗口聚合披露缺口、零 API；MCP
  真实失败或空结果才走 API 既有补充路径（末位 API 用已分配剩余截止、不再
  内部预留）：T null 保留 API T-1 完整窗（pending + 真实错误），T 非空
  （定稿未证明）整批拒绝不删行拼装；预算只在路由层分配一次（首尝试沿用
  有界比例预留思路为末位候选留正剩余）；历史日 API 优先（四证券×20 交易
  日双源对照背书照常采纳，失败/不合格回 MCP）；
- 不可解释形态（errorcode≠0/错股/长度不一致/bool/文本/非有限/同日冲突/
  空序列/全 null/窗口内全 null 而窗口外旧日非空）整体拒绝回 MCP；资格外
  出站即拒；取消穿透零 MCP；API 超时回退 MCP 正剩余预算；停止/预算耗尽
  不再发后备出站；非深研与 HTTP 关闭 MCP 原路径零变化；Choice 主源走既
  有路径；底层"API 采纳"日志改"取得候选"，实际渠道由消费层确认记录。

时钟形态（真实日历，上海时区）：盘中（T=昨日，历史采纳）、刚收盘（T=当日，
防御）、次日（T=昨日，历史采纳）、跨时区 +14（上海当日，防御——复验
``get_market_now`` 与日历同时区）。
"""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import data_provider.capital_flow_provider as CF
from data_provider import ifind_http_daily_adapter as A
from data_provider.capital_flow_provider import get_main_inflow_cumulative
from data_provider.ifind_fundamental_adapter import IfindFetcher
from src.config import Config

CODE = "688183"
IDENTITY = "688183.SH"
SH = ZoneInfo("Asia/Shanghai")
# 四种真实时钟形态（上海时区 + 跨时区 anchor）。
NOW_INTRADAY = datetime(2026, 11, 17, 12, 0, tzinfo=SH)      # 盘中：T=11-16（历史）
NOW_JUST_CLOSED = datetime(2026, 11, 17, 15, 30, tzinfo=SH)  # 刚收盘：T=11-17（当日）
NOW_NEXT_DAY = datetime(2026, 11, 18, 10, 0, tzinfo=SH)      # 次日：T=11-17（历史）
NOW_TZ_UTC14 = datetime(2026, 11, 18, 0, 0, tzinfo=ZoneInfo("Etc/GMT-14"))  # 上海 11-17 18:00：当日


def _expected(current_time) -> list[str]:
    """真实完成交易日历的 11 日窗口（新→旧）；日历不可用则失败（不伪造）。"""
    from src.core.trading_calendar import recent_completed_trading_sessions
    sessions = recent_completed_trading_sessions("cn", 11, current_time=current_time)
    assert sessions, "真实交易日历可用是本测试前置（离线环境请检查 _XCALS_AVAILABLE）"
    return [d.isoformat() for d in sessions]


def _anchor_for(expected: list[str]) -> list[float]:
    """与窗口等长的锚值（新→旧）：main=100、5d=110、10d=145。"""
    return [100.0] + [float(i) for i in range(1, len(expected))]


def _flow_payload(time_list: list, values: list, *, code: str = IDENTITY, errorcode: int = 0) -> bytes:
    return json.dumps({"errorcode": errorcode, "errmsg": "",
                       "tables": [{"thscode": code, "time": time_list,
                                   "table": {A._FLOW_INDICATOR: values}}]}).encode("utf-8")


class _Env:
    """配置 + MCP 方法级替身（按动态窗口记录/返回）+ curl 拦截。

    ``outbound_order`` 按实际发生顺序记录 ``"mcp"``/``"api"`` 出站（T 日窗口
    次序与末位预算断言依赖它，不依赖各渠道分列列表的相对顺序）。
    """

    def __init__(self, monkeypatch, *, http: bool = True, reply=None, fail_curl=None,
                 mcp_series=None, mcp_error=None):
        config = Config(data_source_profile="choice", deep_research_cross_validate=False)
        config.enable_ifind_mcp = True
        config.ifind_mcp_endpoint = "https://fixture.invalid/mcp"
        config.ifind_mcp_token = "test-only"
        config.financial_primary_source = "ifind"
        config.enable_ifind_http_api = http
        config.ifind_http_refresh_token = "test-refresh" if http else None
        config.ifind_http_timeout_seconds = 30.0
        monkeypatch.setattr(Config, "_instance", config)
        self.mcp_calls: list[tuple] = []
        self.outbound_order: list[str] = []

        def mcp(self_fetcher, code, days=12, *, deadline=None):
            self.mcp_calls.append((code, days, deadline))
            self.outbound_order.append("mcp")
            if mcp_error is not None:
                raise mcp_error
            series = mcp_series if mcp_series is not None else self._default_mcp
            return [(d, (v * -2.0) if v is not None else None) for d, v in series]

        self._default_mcp: list[tuple[str, float | None]] = []
        monkeypatch.setattr(IfindFetcher, "fetch_main_inflow_series", mcp)
        monkeypatch.setattr(IfindFetcher, "get_instance",
                            classmethod(lambda cls: IfindFetcher("https://fixture.invalid/mcp", "test-only")))
        monkeypatch.setattr(A, "_resolve_access_token",
                            lambda refresh, *, force_refresh=False: "tok_test")
        self.curl_calls: list[dict] = []

        def curl(url, headers, body, *, label):
            self.curl_calls.append({"url": url, "body": json.loads(body or b"{}"), "label": label})
            self.outbound_order.append("api")
            if fail_curl is not None:
                raise fail_curl
            return 200, reply if reply is not None else _flow_payload([], [])

        monkeypatch.setattr(A, "_http_api_curl_post", curl)


def _api_reply_for(expected: list[str], values: list | None = None) -> bytes:
    anchor = _anchor_for(expected)
    return _flow_payload(expected, values if values is not None else anchor)


def _sync_mcp(env: _Env, expected: list[str]) -> None:
    """MCP 默认序列对齐窗口（×-2 由 _Env 统一施加）。"""
    env._default_mcp = list(zip(expected, _anchor_for(expected)))


# ===== 历史日（盘中/周日/次日）：API 采纳 =====

def test_deep_api_history_adopted_intraday_and_next_day(monkeypatch):
    """历史 T（盘中 T=昨日、次日 T=昨日）：API 采纳完整窗口，MCP 零调用。"""
    from src.core.research_run import ResearchRun

    for now in (NOW_INTRADAY, NOW_NEXT_DAY):
        expected = _expected(now)
        env = _Env(monkeypatch, reply=_api_reply_for(expected))
        with ResearchRun(120).bind():
            result = get_main_inflow_cumulative(CODE, current_time=now)
        assert env.curl_calls and env.mcp_calls == [], f"历史日 API 采纳：{now}"
        assert result["status"] == "ok" and result["source"] == "ifind"
        assert result["main_net_inflow"] == pytest.approx(100.0)
        assert result["as_of_date"] == expected[0]
        assert result["field_meta"]["main_inflow"]["unit"] == "currency_base"


def test_deep_api_request_contract_real_calendar(monkeypatch):
    """请求契约：官方空参数模板 + 起止锚定真实完成日历。"""
    from src.core.research_run import ResearchRun

    expected = _expected(NOW_INTRADAY)
    env = _Env(monkeypatch, reply=_api_reply_for(expected))
    with ResearchRun(120).bind():
        get_main_inflow_cumulative(CODE, current_time=NOW_INTRADAY)
    body = env.curl_calls[0]["body"]
    assert body["codes"] == IDENTITY
    assert body["startdate"] == expected[-1].replace("-", "")
    assert body["enddate"] == expected[0].replace("-", "")
    assert body["functionpara"] == {"Days": "Tradedays", "Fill": "Blank"}
    assert body["indipara"] == [{"indicator": A._FLOW_INDICATOR, "indiparams": []}]


# ===== 当日 T（刚收盘/跨时区）：防御与原鲜度优先 =====

def test_deep_api_today_t_nonnull_discards_api_mcp_full_window(monkeypatch, caplog):
    """刚收盘（T=当日）API T 非空（未证定稿）场景在 MCP 优先路由下的语义：
    T 日窗口 MCP 首尝试零 API 出站——不再先 API 再弃用（必丢候选请求已消除），
    窗口值全部来自 MCP（无拼窗）、含 T（原鲜度）。"""
    from src.core.research_run import ResearchRun

    expected = _expected(NOW_JUST_CLOSED)
    assert expected[0] == "2026-11-17"
    api_values = [555.0, *_anchor_for(expected)[1:]]  # T 非空（未定稿形态）
    env = _Env(monkeypatch, reply=_flow_payload(expected, api_values))
    _sync_mcp(env, expected)
    import logging

    with caplog.at_level(logging.INFO):
        with ResearchRun(120).bind():
            result = get_main_inflow_cumulative(CODE, current_time=NOW_JUST_CLOSED)
    assert env.curl_calls == [], "T 日 MCP 优先：MCP 非空零 API 出站（不再先 API 再弃用）"
    assert env.mcp_calls, "当日 T 非空必须整体走 MCP"
    assert len(env.mcp_calls) == 1, "MCP 恰好一次（不依赖 single_fetch 假定）"
    assert env.outbound_order == ["mcp"], "出站次序：MCP 在前且唯一"
    assert result["as_of_date"] == expected[0], "MCP 完整窗口含 T（原鲜度）"
    assert result["main_net_inflow"] == pytest.approx(-200.0), "T 值为 MCP 值（×-2 锚）"
    assert any("实际采用渠道 mcp" in r.getMessage() for r in caplog.records), \
        "消费层确认实际采用渠道（MCP 整窗）"


def test_deep_api_today_t_nonnull_mcp_fail_disclosed(monkeypatch):
    """当日 T 非空 + MCP 失败：不采信未定稿 API 值，按缺口披露（原失败语义）。"""
    from data_provider.cross_source_validator import SourceReadError
    from src.core.research_run import ResearchRun

    expected = _expected(NOW_JUST_CLOSED)
    api_values = [555.0, *_anchor_for(expected)[1:]]
    env = _Env(monkeypatch, reply=_flow_payload(expected, api_values),
               mcp_error=SourceReadError("ifind", "timeout"))
    with ResearchRun(120).bind():
        result = get_main_inflow_cumulative(CODE, current_time=NOW_JUST_CLOSED)
    assert env.mcp_calls and result["status"] == "failed"
    assert "ifind:timeout" in result["errors"] and result["main_net_inflow"] is None


def test_deep_api_today_t_null_mcp_has_t_prefers_mcp(monkeypatch):
    """当日 T=null 且 MCP T 有值（原鲜度回归反例）：MCP 优先整窗——
    MCP 成功用 MCP（含 T），零 API 出站，不因 API T=null 直接降级 T-1。"""
    from src.core.research_run import ResearchRun

    expected = _expected(NOW_JUST_CLOSED)
    api_values = [None, *_anchor_for(expected)[1:]]  # API T null、T-1 起完整
    env = _Env(monkeypatch, reply=_flow_payload(expected, api_values))
    _sync_mcp(env, expected)  # MCP：T 有值（×-2 锚）
    with ResearchRun(120).bind():
        result = get_main_inflow_cumulative(CODE, current_time=NOW_JUST_CLOSED)
    assert env.mcp_calls, "T null 也要先试 MCP（原鲜度优先）"
    assert len(env.mcp_calls) == 1, "MCP 恰好一次"
    assert env.curl_calls == [], "MCP 非空零 API（不再先 API 探值再弃用）"
    assert result["as_of_date"] == expected[0]
    assert result["main_net_inflow"] == pytest.approx(-200.0), "MCP 的 T 值"


def test_deep_api_today_t_null_mcp_fail_keeps_api_t1(monkeypatch):
    """当日 T=null + MCP 真实失败：按既有数据质量语义保留 API 的 T-1 完整
    窗口（pending_day 披露，不把 T-1 伪装为 T）。"""
    from data_provider.cross_source_validator import SourceReadError
    from src.core.research_run import ResearchRun

    expected = _expected(NOW_JUST_CLOSED)
    api_values = [None, *_anchor_for(expected)[1:]]
    env = _Env(monkeypatch, reply=_flow_payload(expected, api_values),
               mcp_error=SourceReadError("ifind", "timeout"))
    with ResearchRun(120).bind():
        result = get_main_inflow_cumulative(CODE, current_time=NOW_JUST_CLOSED)
    assert env.mcp_calls, "先 MCP"
    assert result["as_of_date"] == expected[1], "T-1（API 完整窗口）"
    assert result["main_net_inflow"] == pytest.approx(1.0)
    assert result["coverage"]["pending_day"] == {"expected_date": expected[0], "observed": False}
    assert "资金窗口尚未完整" in (result.get("note") or "")
    # 第十四轮：MCP 真实失败原因结构化随行 + 不伪装正常（partial 非 ok）。
    assert "ifind:timeout" in result["errors"], "真实 MCP 失败原因保留在 errors"
    assert result["status"] == "partial", "API 单源 T-1 窗口不伪装为完全正常"


def test_deep_api_today_tz_anchor_defense(monkeypatch):
    """跨时区 anchor（+14 时区 11-18 00:00 → 上海 11-17 18:00）：参考日与
    日历同源 ``get_market_now``——T=11-17 判为当日防御（旧实现按
    current_time.isoformat 取 11-18 会误判历史日直接采纳未定稿值）。"""
    from src.core.research_run import ResearchRun

    expected = _expected(NOW_TZ_UTC14)
    assert expected[0] == "2026-11-17"
    api_values = [555.0, *_anchor_for(expected)[1:]]
    env = _Env(monkeypatch, reply=_flow_payload(expected, api_values))
    _sync_mcp(env, expected)
    with ResearchRun(120).bind():
        result = get_main_inflow_cumulative(CODE, current_time=NOW_TZ_UTC14)
    assert env.mcp_calls, "跨时区 anchor 的当日 T 非空必须走 MCP（不被误判历史日）"
    assert result["main_net_inflow"] == pytest.approx(-200.0)


def test_deep_api_intraday_rows_dropped(monkeypatch):
    """盘中未定稿防御：响应混入晚于 enddate 的行（非空）→ 一律丢弃。"""
    from src.core.research_run import ResearchRun

    expected = _expected(NOW_INTRADAY)
    extra_days = ["2026-11-17"]  # 盘中日 > enddate（11-16）
    env = _Env(monkeypatch, reply=_flow_payload([*extra_days, *expected], [999.0, *_anchor_for(expected)]))
    with ResearchRun(120).bind():
        result = get_main_inflow_cumulative(CODE, current_time=NOW_INTRADAY)
    assert env.mcp_calls == []
    assert result["status"] == "ok"
    assert all(row["date"] <= expected[0] for row in result["daily_series"])


# ===== 缺失/坏数据与窗口边界 =====

def test_deep_api_suspend_null_window_incomplete(monkeypatch):
    """停牌/缺值（窗口中间日 null）→ window_n_incomplete 披露、不补零。"""
    from src.core.research_run import ResearchRun

    expected = _expected(NOW_INTRADAY)
    values = _anchor_for(expected)
    values[6] = None  # 第 7 日（10 日窗口内、5 日窗外）
    _Env(monkeypatch, reply=_flow_payload(expected, values))
    with ResearchRun(120).bind():
        result = get_main_inflow_cumulative(CODE, current_time=NOW_INTRADAY)
    assert result["inflow_10d"] is None
    assert result["coverage"]["10"]["missing_dates"] == [expected[6]]
    assert "ifind:window_10_incomplete" in result["errors"]
    assert result["inflow_5d"] == pytest.approx(110.0)
    assert result["status"] == "partial"


def test_deep_api_malformed_rejects_to_mcp(monkeypatch):
    """不可解释形态整体拒绝回 MCP 原路径（MCP 真实调用、窗口按 MCP 值）。"""
    from src.core.research_run import ResearchRun

    expected = _expected(NOW_INTRADAY)
    cases = [
        _flow_payload(expected, _anchor_for(expected), errorcode=-4001),
        _flow_payload(expected, _anchor_for(expected), code="000001.SZ"),
        _flow_payload(expected, _anchor_for(expected)[:9]),
        _flow_payload(expected, [True, *_anchor_for(expected)[1:]]),
        _flow_payload(expected, ["n/a", *_anchor_for(expected)[1:]]),
        _flow_payload([], []),
    ]
    for reply in cases:
        env = _Env(monkeypatch, reply=reply)
        _sync_mcp(env, expected)
        with ResearchRun(120).bind():
            result = get_main_inflow_cumulative(CODE, current_time=NOW_INTRADAY)
        assert env.mcp_calls, f"该形态必须回 MCP：{reply[:60]}"
        assert result["main_net_inflow"] == pytest.approx(-200.0)
        assert result["source"] == "ifind"


def test_deep_api_all_null_rejects_to_mcp_full_window(monkeypatch):
    """全 null（零有效数值）→ 整体不合格回 MCP 完整窗口。"""
    from src.core.research_run import ResearchRun

    expected = _expected(NOW_INTRADAY)
    env = _Env(monkeypatch, reply=_flow_payload(expected, [None] * len(expected)))
    _sync_mcp(env, expected)
    with ResearchRun(120).bind():
        result = get_main_inflow_cumulative(CODE, current_time=NOW_INTRADAY)
    assert env.mcp_calls and result["status"] == "ok"
    assert result["inflow_10d"] == pytest.approx(-290.0)


def test_deep_api_window_all_null_outside_stale_nonnull_rejects(monkeypatch):
    """窗口内全 null + 窗口外旧日非空（第十三轮补充复审反例）：窗口外行不
    采信，全 null 判定不受其干扰——整体回 MCP 完整窗口。"""
    from datetime import date as _date, timedelta as _td
    from src.core.research_run import ResearchRun

    expected = _expected(NOW_INTRADAY)
    # 请求窗口前的旧交易日（非空）——供应商多给的窗口外数据。
    stale_days = [(_date.fromisoformat(expected[-1]) - _td(days=7)).isoformat()]
    reply = _flow_payload([*stale_days, *expected], [12345.0] + [None] * len(expected))
    env = _Env(monkeypatch, reply=reply)
    _sync_mcp(env, expected)
    with ResearchRun(120).bind():
        result = get_main_inflow_cumulative(CODE, current_time=NOW_INTRADAY)
    assert env.mcp_calls, "窗口内全 null（窗口外旧日非空不算有效）必须回 MCP"
    assert result["main_net_inflow"] == pytest.approx(-200.0)


def test_deep_api_all_null_and_mcp_fail_disclosed(monkeypatch):
    """双失败语义：API 全 null + MCP 失败 → 深研 scope 缺口如实披露。"""
    from data_provider.cross_source_validator import SourceReadError
    from src.core.research_run import ResearchRun

    expected = _expected(NOW_INTRADAY)
    _Env(monkeypatch, reply=_flow_payload(expected, [None] * len(expected)),
         mcp_error=SourceReadError("ifind", "timeout"))
    with ResearchRun(120).bind():
        result = get_main_inflow_cumulative(CODE, current_time=NOW_INTRADAY)
    assert result["status"] == "failed" and "ifind:timeout" in result["errors"]
    assert result["main_net_inflow"] is None


def test_deep_api_duplicate_day_conflict_rejects(monkeypatch):
    from src.core.research_run import ResearchRun

    expected = _expected(NOW_INTRADAY)
    env = _Env(monkeypatch, reply=_flow_payload([*expected, expected[0]], [*_anchor_for(expected), 42.0]))
    _sync_mcp(env, expected)
    with ResearchRun(120).bind():
        result = get_main_inflow_cumulative(CODE, current_time=NOW_INTRADAY)
    assert env.mcp_calls and result["main_net_inflow"] == pytest.approx(-200.0)


# ===== 资格/取消/预算/非深研 =====

def test_deep_api_permission_out_of_scope_to_mcp(monkeypatch):
    from src.core.research_run import ResearchRun

    env = _Env(monkeypatch)
    with ResearchRun(120).bind():
        get_main_inflow_cumulative("510300", current_time=NOW_INTRADAY)  # ETF 资格外
    assert env.curl_calls == [] and env.mcp_calls and env.mcp_calls[0][0] == "510300"


def test_deep_api_cancel_propagates_zero_mcp(monkeypatch):
    from src.core.research_run import ResearchRun, ResearchStopped

    env = _Env(monkeypatch, fail_curl=ResearchStopped("user stop"))
    with ResearchRun(120).bind():
        with pytest.raises(ResearchStopped):
            get_main_inflow_cumulative(CODE, current_time=NOW_INTRADAY)
    assert env.mcp_calls == []


def test_deep_api_timeout_falls_back_mcp_positive_budget(monkeypatch):
    from src.core.research_run import ResearchRun

    expected = _expected(NOW_INTRADAY)
    env = _Env(monkeypatch, fail_curl=TimeoutError("curl deadline exhausted"))
    _sync_mcp(env, expected)
    deadline = A.monotonic() + 30.0
    with ResearchRun(120).bind():
        result = get_main_inflow_cumulative(CODE, deadline=deadline, current_time=NOW_INTRADAY)
    assert env.mcp_calls and env.mcp_calls[0][2] - A.monotonic() > 0
    assert result["main_net_inflow"] == pytest.approx(-200.0)


def test_non_deep_zero_api_mcp_path(monkeypatch):
    expected = _expected(NOW_INTRADAY)
    env = _Env(monkeypatch)
    _sync_mcp(env, expected)
    result = get_main_inflow_cumulative(CODE, current_time=NOW_INTRADAY)
    assert env.curl_calls == [] and env.mcp_calls
    assert result["main_net_inflow"] == pytest.approx(-200.0)


def test_http_disabled_mcp_path(monkeypatch):
    from src.core.research_run import ResearchRun

    expected = _expected(NOW_INTRADAY)
    env = _Env(monkeypatch, http=False)
    _sync_mcp(env, expected)
    with ResearchRun(120).bind():
        result = get_main_inflow_cumulative(CODE, current_time=NOW_INTRADAY)
    assert env.curl_calls == [] and env.mcp_calls
    assert result["main_net_inflow"] == pytest.approx(-200.0)


def test_deep_api_today_t_null_mcp_empty_keeps_api_no_fake_success(monkeypatch):
    """当日 T=null + MCP 返回空序列：空序列不构成「MCP 成功」——按未提供
    处理保留 API T-1 完整窗（pending 披露），errors 不进虚构原因、不伪装。"""
    from src.core.research_run import ResearchRun

    expected = _expected(NOW_JUST_CLOSED)
    api_values = [None, *_anchor_for(expected)[1:]]
    env = _Env(monkeypatch, reply=_flow_payload(expected, api_values),
               mcp_series=[])  # MCP 空序列
    with ResearchRun(120).bind():
        result = get_main_inflow_cumulative(CODE, current_time=NOW_JUST_CLOSED)
    assert env.mcp_calls, "先试 MCP"
    assert len(env.mcp_calls) == 1, "MCP 恰好一次"
    assert result["as_of_date"] == expected[1] and result["main_net_inflow"] == pytest.approx(1.0)
    assert result["coverage"]["pending_day"]["expected_date"] == expected[0]
    # 与既有 T-1 回退语义一致（_select_window_end：T-1 完整窗 + pending_day
    # + note 即正常形态——空序列非供应商失败，不虚构错误码）。
    assert result["status"] == "ok"
    assert not any(e.startswith("ifind:") for e in result["errors"]), \
        "空序列不伪造供应商失败原因（仅既有窗口披露语义）"


def test_deep_api_today_mcp_cancel_propagates(monkeypatch):
    """当日场景 MCP 段取消（ResearchStopped）穿透：不保留 API、不伪装。"""
    from src.core.research_run import ResearchRun, ResearchStopped

    expected = _expected(NOW_JUST_CLOSED)
    api_values = [None, *_anchor_for(expected)[1:]]
    env = _Env(monkeypatch, reply=_flow_payload(expected, api_values))

    def _mcp_stopped(self, code, days=12, *, deadline=None):
        env.mcp_calls.append((code, days, deadline))
        raise ResearchStopped("user stop")

    monkeypatch.setattr(IfindFetcher, "fetch_main_inflow_series", _mcp_stopped)
    with ResearchRun(120).bind():
        with pytest.raises(ResearchStopped):
            get_main_inflow_cumulative(CODE, current_time=NOW_JUST_CLOSED)


def test_deep_api_expected_all_null_with_weekend_nonnull_rejects(monkeypatch):
    """第十四轮 FAIL2 反例（真实消费入口）：请求区间内的**非交易日**（周末）
    行非空 + 全部 expected 交易日 null——有效性按 expected 集合判定：零有效
    值视同不合格回 MCP 完整窗口，不得因周末行非空跳过兜底。"""
    from datetime import date as _date, timedelta as _td
    from src.core.research_run import ResearchRun

    expected = _expected(NOW_INTRADAY)
    # 区间内的周末日（expected[0] 与其前一交易日之间的周六/周日）非空。
    weekend = []
    probe = _date.fromisoformat(expected[0])
    while len(weekend) < 2:
        probe -= _td(days=1)
        if probe.weekday() >= 5 and expected[-1] <= probe.isoformat() <= expected[0]:
            weekend.append(probe.isoformat())
    reply = _flow_payload([*weekend, *expected], [777.0, 888.0] + [None] * len(expected))
    env = _Env(monkeypatch, reply=reply)
    _sync_mcp(env, expected)
    with ResearchRun(120).bind():
        result = get_main_inflow_cumulative(CODE, current_time=NOW_INTRADAY)
    assert env.mcp_calls, "expected 交易日零有效值必须回 MCP（周末行非空不算有效）"
    assert result["status"] == "ok" and result["main_net_inflow"] == pytest.approx(-200.0)
    assert all(row["date"] in set(expected) for row in result["daily_series"]), \
        "窗口值全部来自 MCP（周末行不进窗口）"


def test_deep_api_today_t_nonnull_mcp_empty_exactly_once_partial(monkeypatch):
    """当日 T 非空 + MCP 空序列：MCP **恰好一次**（不依赖底层 single_fetch
    假定包装层零额外调用），MCP 空后走 API 补充路径；API T 非空（未定稿）
    整批拒绝——空结果按原契约窗口全缺 partial 披露，不二次调用、不伪装。"""
    from src.core.research_run import ResearchRun

    expected = _expected(NOW_JUST_CLOSED)
    api_values = [555.0, *_anchor_for(expected)[1:]]
    env = _Env(monkeypatch, reply=_flow_payload(expected, api_values),
               mcp_series=[])  # MCP 空序列
    with ResearchRun(120).bind():
        result = get_main_inflow_cumulative(CODE, current_time=NOW_JUST_CLOSED)
    assert len(env.mcp_calls) == 1, "MCP 恰好一次（空结果不触发通用分支二次调用）"
    assert env.outbound_order == ["mcp", "api"], "MCP 优先：空结果后才 API 补充"
    assert result["status"] == "partial"
    assert result["main_net_inflow"] is None
    assert all(f"ifind:window_{n}_incomplete" in result["errors"] for n in (1, 5, 10))


# ===== T 日窗口 MCP 优先：非空不完整形态零 API、原聚合披露（审查 R2 反例） =====

def test_deep_api_today_t_mcp_nonempty_all_null_zero_api_partial(monkeypatch):
    """MCP 非空全 null：交原归一/窗口聚合披露缺口——零 API 补查、不换窗口、
    不改数据资格、不把全 null 解释成 MCP 失败。"""
    from src.core.research_run import ResearchRun

    expected = _expected(NOW_JUST_CLOSED)
    env = _Env(monkeypatch, mcp_series=[(d, None) for d in expected])
    with ResearchRun(120).bind():
        result = get_main_inflow_cumulative(CODE, current_time=NOW_JUST_CLOSED)
    assert env.curl_calls == [], "MCP 非空（全 null）不是失败：零 API 补查"
    assert len(env.mcp_calls) == 1
    assert result["source"] == "ifind" and result["status"] == "partial"
    assert result["main_net_inflow"] is None
    assert all(f"ifind:window_{n}_incomplete" in result["errors"] for n in (1, 5, 10))


def test_deep_api_today_t_mcp_nonempty_t_missing_t1_complete_zero_api(monkeypatch):
    """MCP 非空但 T 缺值、T-1 起完整：沿用原窗口规则（_select_window_end）
    得 T-1 + pending_day，零 API——不因「T 缺」回 API 换源拼装。"""
    from src.core.research_run import ResearchRun

    expected = _expected(NOW_JUST_CLOSED)
    values = list(_anchor_for(expected))
    values[0] = None  # MCP T 缺、T-1 起完整
    env = _Env(monkeypatch, mcp_series=list(zip(expected, values)))
    with ResearchRun(120).bind():
        result = get_main_inflow_cumulative(CODE, current_time=NOW_JUST_CLOSED)
    assert env.curl_calls == [], "MCP 非空（T 缺）零 API：原窗口规则裁决"
    assert result["as_of_date"] == expected[1]
    assert result["main_net_inflow"] == pytest.approx(-2.0), "T-1 值来自 MCP（×-2 锚）"
    assert result["coverage"]["pending_day"]["expected_date"] == expected[0]


def test_deep_api_today_t_mcp_nonempty_middle_gap_no_window_shift_zero_api(monkeypatch):
    """MCP 非空 T 有值但中间缺日：不通过回退掩盖（T 有值时窗口不后移），
    中间缺日如实 window_incomplete，零 API。"""
    from src.core.research_run import ResearchRun

    expected = _expected(NOW_JUST_CLOSED)
    values = list(_anchor_for(expected))
    values[3] = None  # T 有值、中间缺日
    env = _Env(monkeypatch, mcp_series=list(zip(expected, values)))
    with ResearchRun(120).bind():
        result = get_main_inflow_cumulative(CODE, current_time=NOW_JUST_CLOSED)
    assert env.curl_calls == []
    assert result["as_of_date"] == expected[0], "T 有值不回退"
    assert result["main_net_inflow"] == pytest.approx(-200.0), "1 日窗完整"
    assert "ifind:window_10_incomplete" in result["errors"]
    assert result["status"] == "partial"


# ===== T 日窗口 MCP 优先：失败/空后 API 补充路径（真实失败原因与 pending 不丢） =====

def test_deep_api_today_t_mcp_fail_api_supplement_t_null_keeps_t1(monkeypatch):
    """T 日 MCP 真实失败 + API T null：API 补充路径保留 T-1 完整窗
    （pending + 真实 MCP 失败原因结构化随 errors，不伪装正常）。"""
    from data_provider.cross_source_validator import SourceReadError
    from src.core.research_run import ResearchRun

    expected = _expected(NOW_JUST_CLOSED)
    api_values = [None, *_anchor_for(expected)[1:]]
    env = _Env(monkeypatch, reply=_flow_payload(expected, api_values),
               mcp_error=SourceReadError("ifind", "timeout"))
    with ResearchRun(120).bind():
        result = get_main_inflow_cumulative(CODE, current_time=NOW_JUST_CLOSED)
    assert env.outbound_order == ["mcp", "api"], "MCP 优先：真实失败后才 API 补充"
    assert len(env.mcp_calls) == 1
    assert result["as_of_date"] == expected[1], "T-1（API 完整窗口）"
    assert result["main_net_inflow"] == pytest.approx(1.0)
    assert result["coverage"]["pending_day"] == {"expected_date": expected[0], "observed": False}
    assert "ifind:timeout" in result["errors"], "真实 MCP 失败原因保留"
    assert result["status"] == "partial"


def test_deep_api_today_t_mcp_fail_api_t_nonnull_whole_batch_rejected(monkeypatch):
    """T 日 MCP 失败 + API T 非空（未定稿）：整批拒绝——不删 T 行后拼装成
    合格历史结果，failed + 真实 MCP 失败原因。"""
    from data_provider.cross_source_validator import SourceReadError
    from src.core.research_run import ResearchRun

    expected = _expected(NOW_JUST_CLOSED)
    api_values = [555.0, *_anchor_for(expected)[1:]]
    env = _Env(monkeypatch, reply=_flow_payload(expected, api_values),
               mcp_error=SourceReadError("ifind", "timeout"))
    with ResearchRun(120).bind():
        result = get_main_inflow_cumulative(CODE, current_time=NOW_JUST_CLOSED)
    assert env.outbound_order == ["mcp", "api"]
    assert result["status"] == "failed"
    assert "ifind:timeout" in result["errors"]
    assert result["main_net_inflow"] is None
    assert result["daily_series"] == [], "不删 T 行拼装历史窗口"


def test_deep_api_today_t_mcp_empty_api_supplement_t_null_keeps_t1(monkeypatch):
    """T 日 MCP 空序列（不构成成功）→ API 补充：T null 保留 T-1 + pending，
    空序列不虚构失败原因。"""
    from src.core.research_run import ResearchRun

    expected = _expected(NOW_JUST_CLOSED)
    api_values = [None, *_anchor_for(expected)[1:]]
    env = _Env(monkeypatch, reply=_flow_payload(expected, api_values), mcp_series=[])
    with ResearchRun(120).bind():
        result = get_main_inflow_cumulative(CODE, current_time=NOW_JUST_CLOSED)
    assert env.outbound_order == ["mcp", "api"]
    assert result["as_of_date"] == expected[1] and result["main_net_inflow"] == pytest.approx(1.0)
    assert result["coverage"]["pending_day"]["expected_date"] == expected[0]
    assert result["status"] == "ok"
    assert not any(e.startswith("ifind:") for e in result["errors"])


# ===== 路由层唯一分配尝试预算（审查 R1 的 20/14/5 反例，可控虚拟时钟） =====

class _FakeClock:
    """虚拟单调时钟：供应商替身各自推进；被测编排/适配层只读不拨。

    初始值取真实 ``time.monotonic()``，与 ``ResearchRun`` 等未替换时钟保持
    同一量级，避免 contextvar/绝对 deadline 判定错乱。
    """

    def __init__(self) -> None:
        import time as _time

        self.now = _time.monotonic()

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_deep_api_today_t_mcp_14s_exhausted_last_api_completes_5s(monkeypatch):
    """20/14/5 反例（真实适配函数参与）：20 秒资金窗口内 MCP 首尝试预算 14
    秒且实际耗尽后，末位 API 用剩余 6 秒完成 5 秒请求——API 入口不得再内部
    预留（否则缩成 4.2 秒、5 秒请求必然失败）。只模拟传输耗时，不 mock 资金
    API 适配函数本身。"""
    from data_provider.cross_source_validator import SourceReadError
    from src.core.research_run import ResearchRun
    from src.patches.eastmoney_patch import request_deadline

    clock = _FakeClock()
    expected = _expected(NOW_JUST_CLOSED)
    api_values = [None, *_anchor_for(expected)[1:]]  # API T null → 成功后保留 T-1
    monkeypatch.setattr(A, "monotonic", clock.monotonic)
    monkeypatch.setattr(CF, "monotonic", clock.monotonic)
    env = _Env(monkeypatch)
    flow_deadline = clock.now + 20.0
    mcp_first_deadlines: list[float | None] = []

    def mcp_slow(self, code, days=12, *, deadline=None):
        env.mcp_calls.append((code, days, deadline))
        env.outbound_order.append("mcp")
        mcp_first_deadlines.append(deadline)
        budget = (deadline - clock.now) if deadline is not None else 999.0
        assert budget > 0, "MCP 首尝试预算必须为正"
        if budget < 15.0:
            clock.advance(budget)  # 真实语义：耗尽预算后超时
            raise SourceReadError("ifind", "timeout")
        clock.advance(15.0)
        return [(d, v * -2.0) for d, v in env._default_mcp]

    monkeypatch.setattr(IfindFetcher, "fetch_main_inflow_series", mcp_slow)

    def curl_slow(url, headers, body, *, label):
        assert label == "flow", f"资金窗口只应有 flow 出站，见 {label}"
        env.curl_calls.append({"url": url, "body": json.loads(body or b"{}"), "label": label})
        env.outbound_order.append("api")
        dl = request_deadline.get()
        if dl is not None and dl - clock.now < 5.0:
            raise TimeoutError("curl deadline exhausted")  # 真实语义：--max-time 硬截止
        clock.advance(5.0)
        return 200, _flow_payload(expected, api_values)

    monkeypatch.setattr(A, "_http_api_curl_post", curl_slow)
    with ResearchRun(600).bind():
        result = get_main_inflow_cumulative(
            CODE, deadline=flow_deadline, current_time=NOW_JUST_CLOSED)
    assert env.outbound_order == ["mcp", "api"], "T 日 MCP 优先，失败后 API 补充"
    assert mcp_first_deadlines[0] is not None
    assert mcp_first_deadlines[0] - flow_deadline + 20.0 == pytest.approx(14.0, abs=0.5), \
        "路由层为首尝试 MCP 分配 14 秒（为末位 API 预留 6 秒）"
    assert clock.now - flow_deadline + 20.0 == pytest.approx(19.0, abs=0.5), \
        "14+5 秒串行在 20 秒总窗内完成（未因二次预留缩成 4.2 秒）"
    assert result["as_of_date"] == expected[1], "末位 API T-1 完整窗口"
    assert result["main_net_inflow"] == pytest.approx(1.0)
    assert "ifind:timeout" in result["errors"], "真实 MCP 超时原因保留"
    assert result["coverage"]["pending_day"]["expected_date"] == expected[0]


def test_deep_api_history_api_priority_reserve_unchanged(monkeypatch):
    """历史窗口 API 优先：API 阶段仍为 MCP 兜底预留有界份额（路由层分配），
    API 失败后 MCP 用原 deadline 正剩余。"""
    from src.core.research_run import ResearchRun

    clock = _FakeClock()
    expected = _expected(NOW_INTRADAY)
    monkeypatch.setattr(A, "monotonic", clock.monotonic)
    monkeypatch.setattr(CF, "monotonic", clock.monotonic)
    env = _Env(monkeypatch, fail_curl=TimeoutError("curl deadline exhausted"))
    _sync_mcp(env, expected)
    flow_deadline = clock.now + 30.0
    with ResearchRun(600).bind():
        result = get_main_inflow_cumulative(
            CODE, deadline=flow_deadline, current_time=NOW_INTRADAY)
    assert env.outbound_order == ["api", "mcp"], "历史窗口 API 优先"
    assert env.mcp_calls and env.mcp_calls[0][2] > clock.now, "MCP 兜底用原 deadline 正剩余"
    assert result["main_net_inflow"] == pytest.approx(-200.0)


def test_deep_api_today_t_mcp_fail_zero_budget_no_api_outbound(monkeypatch):
    """MCP 失败且总预算已耗尽：不再发后备 API 请求（预算耗尽不出站、不重置
    deadline），真实失败原因随 failed 披露。"""
    from data_provider.cross_source_validator import SourceReadError
    from src.core.research_run import ResearchRun

    clock = _FakeClock()
    expected = _expected(NOW_JUST_CLOSED)
    monkeypatch.setattr(A, "monotonic", clock.monotonic)
    monkeypatch.setattr(CF, "monotonic", clock.monotonic)
    env = _Env(monkeypatch, reply=_flow_payload(expected, _anchor_for(expected)))
    flow_deadline = clock.now + 20.0

    def mcp_eat_all(self, code, days=12, *, deadline=None):
        env.mcp_calls.append((code, days, deadline))
        env.outbound_order.append("mcp")
        clock.advance(20.0)  # 耗尽全部窗口预算
        raise SourceReadError("ifind", "timeout")

    monkeypatch.setattr(IfindFetcher, "fetch_main_inflow_series", mcp_eat_all)
    with ResearchRun(600).bind():
        result = get_main_inflow_cumulative(
            CODE, deadline=flow_deadline, current_time=NOW_JUST_CLOSED)
    assert env.outbound_order == ["mcp"], "预算耗尽不出站"
    assert env.curl_calls == []
    assert result["status"] == "failed" and "ifind:timeout" in result["errors"]


def test_deep_api_today_t_stop_after_mcp_fail_no_api_outbound(monkeypatch):
    """run 停止（MCP 失败路径中）：停止后不再发后备 API 出站，不伪装成功。"""
    from data_provider.cross_source_validator import SourceReadError
    from src.core.research_run import ResearchRun

    expected = _expected(NOW_JUST_CLOSED)
    env = _Env(monkeypatch, reply=_flow_payload(expected, _anchor_for(expected)))
    run = ResearchRun(600)

    def mcp_stop(self, code, days=12, *, deadline=None):
        env.mcp_calls.append((code, days, deadline))
        env.outbound_order.append("mcp")
        run.stop("cancelled")
        raise SourceReadError("ifind", "timeout")

    monkeypatch.setattr(IfindFetcher, "fetch_main_inflow_series", mcp_stop)
    with run.bind():
        result = get_main_inflow_cumulative(
            CODE, deadline=A.monotonic() + 20.0, current_time=NOW_JUST_CLOSED,
            allow_choice_fallback=False)  # 深研 scope：失败不回退 Choice
    assert env.outbound_order == ["mcp"], "停止后不发后备出站"
    assert env.curl_calls == []
    assert result["status"] == "failed" and "ifind:timeout" in result["errors"]


# ===== 底层候选/采纳日志语义 =====

def test_flow_api_candidate_log_not_adopted_wording(monkeypatch, caplog):
    """底层"API 采纳"日志改"取得候选"：实际渠道采纳由消费层确认后记录——
    历史日 API 候选取得后由消费层确认采用 api；T 日 MCP 整窗场景底层零
    出站零候选日志，消费层确认 mcp。全程无"API 采纳"措辞。"""
    import logging

    from src.core.research_run import ResearchRun

    expected = _expected(NOW_INTRADAY)
    env = _Env(monkeypatch, reply=_api_reply_for(expected))
    with caplog.at_level(logging.INFO):
        with ResearchRun(120).bind():
            result = get_main_inflow_cumulative(CODE, current_time=NOW_INTRADAY)
    texts = [record.getMessage() for record in caplog.records]
    assert not any("API 采纳" in text for text in texts), \
        "底层不得再宣称 API 采纳（候选可能被消费层拒绝）"
    assert any("取得候选" in text for text in texts), "底层日志为候选取得语义"
    assert any("实际采用渠道 api" in text for text in texts), \
        "消费层确认实际采用渠道（历史日 API 采纳）"


def test_flow_api_consumer_confirms_api_channel_after_fallback(monkeypatch, caplog):
    """MCP 失败后 API 补充被消费层实际采用：消费层日志确认 api 渠道。"""
    import logging

    from data_provider.cross_source_validator import SourceReadError
    from src.core.research_run import ResearchRun

    expected = _expected(NOW_JUST_CLOSED)
    api_values = [None, *_anchor_for(expected)[1:]]
    env = _Env(monkeypatch, reply=_flow_payload(expected, api_values),
               mcp_error=SourceReadError("ifind", "timeout"))
    with caplog.at_level(logging.INFO):
        with ResearchRun(120).bind():
            result = get_main_inflow_cumulative(CODE, current_time=NOW_JUST_CLOSED)
    assert result["main_net_inflow"] == pytest.approx(1.0)
    texts = [record.getMessage() for record in caplog.records]
    assert any("实际采用渠道 api" in text for text in texts), texts


# ===== 复审阻断 1/2：末位 API 上限与父截止夹取、MCP 首试不借 HTTP cap =====

def _transport_deadline_curl(env, clock):
    """记录传输层（curl 边界）实际看到的 request_deadline 的 curl 替身。"""
    from src.patches.eastmoney_patch import request_deadline

    def curl(url, headers, body, *, label):
        env.curl_calls.append({"url": url, "label": label,
                               "deadline": request_deadline.get()})
        env.outbound_order.append("api")
        return 200, env._reply

    return curl


class _DeadlineEnv(_Env):
    """真实适配函数入口（``ifind_http_main_inflow_series``）+ 记录传输层截止。"""

    def __init__(self, monkeypatch, *, reply, http_timeout):
        config = Config(data_source_profile="choice", deep_research_cross_validate=False)
        config.enable_ifind_mcp = True
        config.ifind_mcp_endpoint = "https://fixture.invalid/mcp"
        config.ifind_mcp_token = "test-only"
        config.financial_primary_source = "ifind"
        config.enable_ifind_http_api = True
        config.ifind_http_refresh_token = "test-refresh"
        config.ifind_http_timeout_seconds = http_timeout
        monkeypatch.setattr(Config, "_instance", config)
        self._reply = reply
        self.curl_calls: list[dict] = []
        self.outbound_order: list[str] = []
        self.mcp_calls: list[tuple] = []
        monkeypatch.setattr(A, "_resolve_access_token",
                            lambda refresh, *, force_refresh=False: "tok_test")
        monkeypatch.setattr(A, "_http_api_curl_post",
                            _transport_deadline_curl(self, None))


def test_flow_api_small_http_cap_bounds_attempt_deadline(monkeypatch):
    """HTTP cap 较小（timeout=1）：末位 API 尝试截止仍受「cap + 解析余量」
    上限约束——不因路由层传入更长窗口而丢失请求级上限（复审阻断 1 反例：
    传入 30 秒、cap=1 时传输层不得看到 30）。"""
    from src.patches.eastmoney_patch import request_deadline
    from data_provider.ifind_http_daily_adapter import ifind_http_main_inflow_series

    clock = _FakeClock()
    monkeypatch.setattr(A, "monotonic", clock.monotonic)
    expected = _expected(NOW_JUST_CLOSED)
    env = _DeadlineEnv(monkeypatch, reply=_api_reply_for(expected), http_timeout=1.0)
    token = request_deadline.set(clock.now + 120.0)  # 父截止很长
    try:
        ifind_http_main_inflow_series(
            CODE, deadline=clock.now + 30.0,
            start_iso=expected[-1], end_iso=expected[0])
    finally:
        request_deadline.reset(token)
    assert env.curl_calls, "真实适配入口完成一次出站"
    seen = env.curl_calls[0]["deadline"]
    assert seen is not None and seen <= clock.now + 1.0 + A._HTTP_PARSE_STAGE_SECONDS + 0.01, \
        f"传输层截止须被 cap+解析余量夹取（实测 {seen - clock.now:.2f}s > 1+{A._HTTP_PARSE_STAGE_SECONDS}s）"


def test_flow_api_shorter_parent_deadline_bounds_attempt(monkeypatch):
    """父截止更短（3 秒 < 传入尝试截止 30 秒）：传输层不得看到长于父截止
    的窗口——更短外层截止优先（复审阻断 1 反例：实测 120）。"""
    from src.patches.eastmoney_patch import request_deadline
    from data_provider.ifind_http_daily_adapter import ifind_http_main_inflow_series

    clock = _FakeClock()
    monkeypatch.setattr(A, "monotonic", clock.monotonic)
    expected = _expected(NOW_JUST_CLOSED)
    env = _DeadlineEnv(monkeypatch, reply=_api_reply_for(expected), http_timeout=30.0)
    token = request_deadline.set(clock.now + 3.0)  # 父截止更短
    try:
        ifind_http_main_inflow_series(
            CODE, deadline=clock.now + 30.0,
            start_iso=expected[-1], end_iso=expected[0])
    finally:
        request_deadline.reset(token)
    seen = env.curl_calls[0]["deadline"]
    assert seen is not None and seen <= clock.now + 3.01, \
        f"传输层截止须被更短父截止夹取（实测 {seen - clock.now:.2f}s > 3s）"


def test_today_t_mcp_first_share_ignores_http_cap(monkeypatch):
    """MCP 首试份额只按实际 R 预留（30%，最多 8 秒），不借 HTTP cap（复审
    阻断 2 反例：R=20、HTTP cap=1 → MCP 首试应得 14 秒，旧实现 4.2 秒）。"""
    from data_provider.cross_source_validator import SourceReadError
    from src.core.research_run import ResearchRun

    clock = _FakeClock()
    monkeypatch.setattr(A, "monotonic", clock.monotonic)
    monkeypatch.setattr(CF, "monotonic", clock.monotonic)
    expected = _expected(NOW_JUST_CLOSED)
    env = _Env(monkeypatch, reply=_flow_payload(expected, [None, *_anchor_for(expected)[1:]]))
    env_config_http_timeout = 1.0
    from src.config import get_config
    get_config().ifind_http_timeout_seconds = env_config_http_timeout
    flow_deadline = clock.now + 20.0
    mcp_deadlines: list[float | None] = []

    def mcp(self, code, days=12, *, deadline=None):
        env.mcp_calls.append((code, days, deadline))
        env.outbound_order.append("mcp")
        mcp_deadlines.append(deadline)
        raise SourceReadError("ifind", "timeout")  # 首试即失败 → 后续 API 补充不适用本断言

    monkeypatch.setattr(IfindFetcher, "fetch_main_inflow_series", mcp)
    with ResearchRun(600).bind():
        get_main_inflow_cumulative(CODE, deadline=flow_deadline, current_time=NOW_JUST_CLOSED)
    assert mcp_deadlines and mcp_deadlines[0] is not None
    share = mcp_deadlines[0] - clock.now
    assert share == pytest.approx(14.0, abs=0.5), \
        f"MCP 首试份额应为 R×0.7=14 秒（不受 HTTP cap=1 影响），实测 {share:.2f}s"


# ===== 最终复审剩余项：HTTP 参与历史 fallback 的同窗合同 =====

def test_history_api_fail_mcp_fallback_uses_established_window(monkeypatch):
    """HTTP 参与 + 历史窗口 + 调用未显式传 deadline：路由建立
    ``_flow_deadline = now + fetch_timeout`` 供 API 使用，API 失败后的通用
    MCP 后备必须用同一窗口（不再按 deadline=None 重开自身 timeout 的更长
    窗口）——确定性探针：API 截止预留后耗尽、MCP 只剩窗口内剩余。"""
    from src.core.research_run import ResearchRun

    expected = _expected(NOW_INTRADAY)
    start = A.monotonic()
    env = _Env(monkeypatch, fail_curl=TimeoutError("curl deadline exhausted"))
    _sync_mcp(env, expected)
    with ResearchRun(120).bind():
        result = get_main_inflow_cumulative(CODE, current_time=NOW_INTRADAY)  # deadline=None
    assert env.curl_calls, "历史窗口 API 先尝试"
    assert env.mcp_calls, "API 失败回退 MCP"
    mcp_deadline = env.mcp_calls[0][2]
    assert mcp_deadline is not None, \
        "HTTP 参与时 MCP 后备必须收到已建立的同窗 deadline（不得为 None 重开窗口）"
    from src.config import get_config
    window = get_config().fundamental_fetch_timeout_seconds
    assert start < mcp_deadline <= start + window + 1.0, \
        f"MCP 后备窗口 = 路由建窗 [now, now+{window}s]，实测 {mcp_deadline - start:.2f}s"
    assert result["main_net_inflow"] == pytest.approx(-200.0)


def test_history_api_fail_mcp_fallback_explicit_deadline_unchanged(monkeypatch):
    """HTTP 参与 + 显式 deadline：MCP 后备沿用调用方 deadline（既有语义
    锁定，不被建窗逻辑改写）。"""
    from src.core.research_run import ResearchRun

    expected = _expected(NOW_INTRADAY)
    env = _Env(monkeypatch, fail_curl=TimeoutError("curl deadline exhausted"))
    _sync_mcp(env, expected)
    dl = A.monotonic() + 45.0
    with ResearchRun(120).bind():
        result = get_main_inflow_cumulative(CODE, deadline=dl, current_time=NOW_INTRADAY)
    assert env.mcp_calls and env.mcp_calls[0][2] == pytest.approx(dl, abs=0.5), \
        "显式 deadline 原样传递给 MCP 后备"
    assert result["main_net_inflow"] == pytest.approx(-200.0)


def test_http_off_mcp_path_keeps_none_deadline(monkeypatch):
    """HTTP 不参与（关闭）：通用 MCP 分支保持原 deadline/None 语义（无建
    窗逻辑介入）——锁定非 HTTP 消费者零变化。"""
    from src.core.research_run import ResearchRun

    expected = _expected(NOW_INTRADAY)
    env = _Env(monkeypatch, http=False)
    _sync_mcp(env, expected)
    with ResearchRun(120).bind():
        result = get_main_inflow_cumulative(CODE, current_time=NOW_INTRADAY)
    assert env.curl_calls == [] and env.mcp_calls
    assert env.mcp_calls[0][2] is None, "HTTP 关闭：MCP 原路径 deadline=None 语义不变"
    assert result["main_net_inflow"] == pytest.approx(-200.0)
