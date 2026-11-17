# -*- coding: utf-8 -*-
"""深研个股资金流标准 API 纵向闭环回归（真实消费入口 ``get_main_inflow_cumulative``）。

第十一~十三轮接入与审查收敛锁定（证据见
``.claude/reviews/ifind-http-capital-flow-research-20261117.md``）：

- 深研资格 API 优先；请求起止由**本地完成交易日历**锚定，解析层严格请求
  窗口边界（晚于 enddate 的盘中未定稿行与早于 startdate 的窗口外旧日行
  均不采信）；null 保留为缺值行（停牌/T 缺 → 既有 T-1 回退 + 披露）；
- **当日 T 场景**（参考日与窗口日历同一时区语义 ``get_market_now``，跨时
  区 anchor 不误判历史日）：T 非空（定稿未证明）弃用 API 值、先以同预算
  调 MCP 整窗（原鲜度优先）；T null 同样先 MCP（MCP 可能有 T 值），MCP
  真实失败才保留 API T-1 完整窗（不拼两源、不把 T-1 伪装为 T）；历史日
  非空由四证券×20 交易日双源对照背书照常采纳；
- 不可解释形态（errorcode≠0/错股/长度不一致/bool/文本/非有限/同日冲突/
  空序列/全 null/窗口内全 null 而窗口外旧日非空）整体拒绝回 MCP；资格外
  出站即拒；取消穿透零 MCP；API 超时回退 MCP 正剩余预算；非深研与 HTTP
  关闭 MCP 原路径零变化；Choice 主源走既有路径。

时钟形态（真实日历，上海时区）：盘中（T=昨日，历史采纳）、刚收盘（T=当日，
防御）、次日（T=昨日，历史采纳）、跨时区 +14（上海当日，防御——复验
``get_market_now`` 与日历同时区）。
"""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

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
    """配置 + MCP 方法级替身（按动态窗口记录/返回）+ curl 拦截。"""

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

        def mcp(self_fetcher, code, days=12, *, deadline=None):
            self.mcp_calls.append((code, days, deadline))
            if mcp_error is not None:
                raise mcp_error
            series = mcp_series if mcp_series is not None else self._default_mcp
            return [(d, v * -2.0) for d, v in series]

        self._default_mcp: list[tuple[str, float]] = []
        monkeypatch.setattr(IfindFetcher, "fetch_main_inflow_series", mcp)
        monkeypatch.setattr(IfindFetcher, "get_instance",
                            classmethod(lambda cls: IfindFetcher("https://fixture.invalid/mcp", "test-only")))
        monkeypatch.setattr(A, "_resolve_access_token",
                            lambda refresh, *, force_refresh=False: "tok_test")
        self.curl_calls: list[dict] = []

        def curl(url, headers, body, *, label):
            self.curl_calls.append({"url": url, "body": json.loads(body or b"{}"), "label": label})
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

def test_deep_api_today_t_nonnull_discards_api_mcp_full_window(monkeypatch):
    """刚收盘（T=当日）API T 非空（未证定稿）：弃用 API 值，同预算 MCP 整窗
    ——窗口值全部来自 MCP（无拼窗）、含 T（原鲜度）。"""
    from src.core.research_run import ResearchRun

    expected = _expected(NOW_JUST_CLOSED)
    assert expected[0] == "2026-11-17"
    api_values = [555.0, *_anchor_for(expected)[1:]]  # T 非空（未定稿形态）
    env = _Env(monkeypatch, reply=_flow_payload(expected, api_values))
    _sync_mcp(env, expected)
    with ResearchRun(120).bind():
        result = get_main_inflow_cumulative(CODE, current_time=NOW_JUST_CLOSED)
    assert env.curl_calls, "API 先尝试（一次出站）"
    assert env.mcp_calls, "当日 T 非空必须整体走 MCP"
    assert len(env.mcp_calls) == 1, "MCP 恰好一次（不依赖 single_fetch 假定）"
    assert result["as_of_date"] == expected[0], "MCP 完整窗口含 T（原鲜度）"
    assert result["main_net_inflow"] == pytest.approx(-200.0), "T 值为 MCP 值（×-2 锚）"


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
    """当日 T=null 且 MCP T 有值（原鲜度回归反例）：先同预算 MCP 整窗——
    MCP 成功用 MCP（含 T），不因 API T=null 直接降级 T-1。"""
    from src.core.research_run import ResearchRun

    expected = _expected(NOW_JUST_CLOSED)
    api_values = [None, *_anchor_for(expected)[1:]]  # API T null、T-1 起完整
    env = _Env(monkeypatch, reply=_flow_payload(expected, api_values))
    _sync_mcp(env, expected)  # MCP：T 有值（×-2 锚）
    with ResearchRun(120).bind():
        result = get_main_inflow_cumulative(CODE, current_time=NOW_JUST_CLOSED)
    assert env.mcp_calls, "T null 也要先试 MCP（原鲜度优先）"
    assert len(env.mcp_calls) == 1, "MCP 恰好一次"
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
    """当日 T 非空（弃用 API）+ MCP 空序列：MCP **恰好一次**（不依赖底层
    single_fetch 假定包装层零额外调用）——空结果按原契约窗口全缺 partial
    披露，不二次调用、不伪装。"""
    from src.core.research_run import ResearchRun

    expected = _expected(NOW_JUST_CLOSED)
    api_values = [555.0, *_anchor_for(expected)[1:]]
    env = _Env(monkeypatch, reply=_flow_payload(expected, api_values),
               mcp_series=[])  # MCP 空序列
    with ResearchRun(120).bind():
        result = get_main_inflow_cumulative(CODE, current_time=NOW_JUST_CLOSED)
    assert len(env.mcp_calls) == 1, "MCP 恰好一次（空结果不触发通用分支二次调用）"
    assert result["status"] == "partial"
    assert result["main_net_inflow"] is None
    assert all(f"ifind:window_{n}_incomplete" in result["errors"] for n in (1, 5, 10))
