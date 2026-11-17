# -*- coding: utf-8 -*-
"""issue #91：iFinD 标准 HTTP API 日线候选资格边界（离线，合成 fixture）。

全部离线：不读 .env、不发真实供应商请求；传输以受控替换边界（monkeypatch
``requests.post``）回放合成官方结构响应，生产解析器、单位资格、窗口资格
与管理器路由走真实代码路径。**合成样本不构成真实调用验收**；真实结构/单
位证据由协调者官方授权会话实测提供（600519.SH/600031.SH 各 250 日）。

覆盖：启用判定与配置校验（与 MCP 凭据互不混用）、路由、官方响应解析
（errorcode/单表/证券归属/ISO 日期/列长/null）、逐窗口双 CPS 单位资格
（量额不随复权变动 + 未复权 VWAP ∈ 当日未复权区间，越界/漂移/窗口不一致
整窗拒绝）、严格交易日历全窗口资格、传输边界（超时/字节上限/非 200）、
管理器预置次序与 issue #94 同源约束语义。
"""
from __future__ import annotations

import json
import time as _time_module
from datetime import date as _d
from decimal import Decimal
from typing import Dict, List, Optional

import pytest

from data_provider import ifind_http_daily_adapter as H
from data_provider.base import DataFetchError
from tests.ifind_daily_testutil import (
    LATEST_SESSION,
    enabled_config,
    http_enabled_config,
    strict_calendar,
)

# 严格日历的三个参考会话（tests.ifind_daily_testutil）。
SESSIONS = ["2026-11-04", "2026-11-05", "2026-11-06"]
IDENTITY = "600519.SH"

# 未复权（CPS=1 对照口径）与现金分红前复权（CPS=6 取数口径）的合成价格；
# 复权价 ≈ 未复权价 × 0.9（窗口内含除权事件的形态）。量额两口径共享同一
# 数值（单位资格第 2 条：量额不随复权变动），未复权 VWAP 落在当日区间。
_RAW_ROWS = {
    "2026-11-04": {"open": "1695", "high": "1720", "low": "1690", "close": "1710"},
    "2026-11-05": {"open": "1712", "high": "1735", "low": "1705", "close": "1728"},
    "2026-11-06": {"open": "1730", "high": "1740", "low": "1715", "close": "1736"},
}
_ADJUSTED_ROWS = {
    "2026-11-04": {"open": "1525.5", "high": "1548", "low": "1521", "close": "1539"},
    "2026-11-05": {"open": "1540.8", "high": "1561.5", "low": "1534.5", "close": "1555.2"},
    "2026-11-06": {"open": "1557", "high": "1566", "low": "1543.5", "close": "1562.4"},
}
_VOLUME_AMOUNT = {
    "2026-11-04": ("30000000", "51000000000"),  # VWAP=1700 ∈ [1690, 1720]
    "2026-11-05": ("32000000", "54784000000"),  # VWAP=1712 ∈ [1705, 1735]
    "2026-11-06": ("28000000", "48440000000"),  # VWAP=1730 ∈ [1715, 1740]
}


def synthetic_payload(
    *,
    identity: str = IDENTITY,
    sessions: Optional[List[str]] = None,
    errorcode: int = 0,
    errmsg: str = "success",
    tables: Optional[list] = None,
    drop_column: Optional[str] = None,
    time_format: str = "iso",
    prices: str = "adjusted",
) -> dict:
    """合成官方 THS_HQ 响应结构；数值精确十进制。

    ``prices="adjusted"`` 回放 CPS=6 取数口径；``prices="raw"`` 回放 CPS=1
    不复权对照口径（同一量额）。
    """
    sessions = sessions if sessions is not None else SESSIONS
    rows = _ADJUSTED_ROWS if prices == "adjusted" else _RAW_ROWS
    times: List[str] = []
    columns: Dict[str, List[object]] = {f: [] for f in ("open", "high", "low", "close", "volume", "amount")}
    for session in sessions:
        if time_format == "compact":
            times.append(session.replace("-", ""))
        elif time_format == "timestamp":
            times.append(str(int(Decimal(session.replace("-", "")))))
        else:
            times.append(session)
        prices_row = rows[session]
        volume, amount = _VOLUME_AMOUNT[session]
        values = {**prices_row, "volume": volume, "amount": amount}
        for field in columns:
            if field == drop_column:
                continue
            columns[field].append(Decimal(values[field]))
    if tables is not None:
        return {"errorcode": errorcode, "errmsg": errmsg, "tables": tables}
    table = {"thscode": identity, "time": times, "table": columns}
    if drop_column:
        table["table"].pop(drop_column)
    return {"errorcode": errorcode, "errmsg": errmsg, "tables": [table]}


def json_dumps(payload: dict) -> str:
    """把 Decimal 序列化成 JSON 数值文本（保持精确十进制）。"""
    import re

    text = json.dumps(payload, default=str)
    return re.sub(r'"(\d+(?:\.\d+)?)"', r"\1", text)


def install_cps_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    calls: List[dict],
    *,
    adjusted_payload: Optional[dict] = None,
    raw_payload: Optional[dict] = None,
) -> None:
    """按 URL/CPS 分发 curl 响应：token 端点默认成功换取，数据端点按 CPS。

    fake `_http_api_curl_post`（curl 子进程传输的受控边界）；每次出站的
    url/headers/body 记录进 ``calls``，供契约断言使用。
    """
    adjusted_payload = adjusted_payload if adjusted_payload is not None else synthetic_payload()
    raw_payload = raw_payload if raw_payload is not None else synthetic_payload(prices="raw")
    token_counter = {"n": 0}

    def fake_curl_post(url, headers, body, *, label):
        calls.append({"url": url, "headers": dict(headers or {}), "body": body, "label": label})
        if url == H._TOKEN_ENDPOINT:
            token_counter["n"] += 1
            payload = token_payload(f"auto-token-{token_counter['n']}", int(_time_module.time()) + 86400)
            return 200, json_dumps(payload).encode()
        request = json.loads(body or b"{}")
        cps = request.get("functionpara", {}).get("CPS")
        payload = adjusted_payload if cps == "6" else raw_payload
        return 200, json_dumps(payload).encode()

    monkeypatch.setattr(H, "_http_api_curl_post", fake_curl_post)


# ---------------------------------------------------------------------------
# 启用判定与配置校验：标准 API 凭据与 MCP 凭据互不混用
# ---------------------------------------------------------------------------
def test_config_defaults_keep_http_off():
    """默认配置不含 HTTP 候选：开关 False、refresh_token None、超时 30。"""
    from src.config import Config

    config = Config(enable_fundamental_pipeline=True, deep_research_cross_validate=False)
    assert config.enable_ifind_http_api is False
    assert config.ifind_http_refresh_token is None
    assert config.ifind_http_timeout_seconds == 30.0


def test_http_disabled_by_default(monkeypatch):
    strict_calendar(monkeypatch)
    enabled_config(monkeypatch, enabled=False)
    assert H.ifind_http_daily_enabled() is False
    assert H.http_daily_routing("600519", None) is None


def test_http_enabled_requires_choice_profile_and_dedicated_token(monkeypatch):
    strict_calendar(monkeypatch)
    # 只配 MCP 凭据不启用 HTTP 候选：凭据不混用。
    config = enabled_config(monkeypatch)
    assert H.ifind_http_daily_enabled() is False
    # 标准 API 凭据（refresh_token）齐全且显式启用才打开。
    config.enable_ifind_http_api = True
    assert H.ifind_http_daily_enabled() is False  # 缺 refresh_token
    config.ifind_http_refresh_token = "long-lived-refresh-token"
    assert H.ifind_http_daily_enabled() is True
    config.ifind_http_timeout_seconds = 0.0
    assert H.ifind_http_daily_enabled() is False
    config.ifind_http_timeout_seconds = float("nan")
    assert H.ifind_http_daily_enabled() is False


def test_http_enabled_requires_choice_profile(monkeypatch):
    strict_calendar(monkeypatch)
    config = enabled_config(monkeypatch, enabled=False)
    config.data_source_profile = "legacy"
    config.enable_ifind_http_api = True
    config.ifind_http_refresh_token = "long-lived-refresh-token"
    assert H.ifind_http_daily_enabled() is False


def test_config_validation_requires_token_and_profile():
    from src.config import Config

    base = dict(enable_fundamental_pipeline=True, deep_research_cross_validate=False)
    with pytest.raises(ValueError, match="IFIND_HTTP_REFRESH_TOKEN"):
        Config(**base, data_source_profile="choice", enable_ifind_http_api=True)
    with pytest.raises(ValueError, match="DATA_SOURCE_PROFILE=choice"):
        Config(**base, data_source_profile="legacy", enable_ifind_http_api=True,
               ifind_http_refresh_token="long-lived-refresh-token")
    with pytest.raises(ValueError, match="IFIND_HTTP_TIMEOUT_SECONDS"):
        Config(**base, data_source_profile="choice", enable_ifind_http_api=True,
               ifind_http_refresh_token="long-lived-refresh-token", ifind_http_timeout_seconds=-1.0)


# ---------------------------------------------------------------------------
# 路由：复用既有资格边界，仅启用条件不同
# ---------------------------------------------------------------------------
def test_http_routing_clamps_to_latest_completed_session(monkeypatch):
    strict_calendar(monkeypatch)
    http_enabled_config(monkeypatch)
    routing = H.http_daily_routing("600519.SH", None)
    assert routing is not None
    identity, clamped = routing
    assert identity == IDENTITY
    assert clamped == LATEST_SESSION.isoformat()


def test_http_routing_skips_explicit_history_targets(monkeypatch):
    strict_calendar(monkeypatch)
    http_enabled_config(monkeypatch)
    assert H.http_daily_routing("600519.SH", "2026-01-30") is None


def test_http_routing_skips_unqualified_securities(monkeypatch):
    strict_calendar(monkeypatch)
    http_enabled_config(monkeypatch)
    assert H.http_daily_routing("000001.SH", None) is None  # 上证指数
    assert H.http_daily_routing("510300.SH", None) is None  # ETF


# ---------------------------------------------------------------------------
# 官方响应解析：结构/证券归属/时间/数值
# ---------------------------------------------------------------------------
def test_parse_rows_decimal_exact(monkeypatch):
    rows = H._parse_history_rows(synthetic_payload(), IDENTITY)
    assert set(rows) == {_d.fromisoformat(s) for s in SESSIONS}
    assert rows[_d(2026, 11, 6)]["收盘价"] == "1562.4"
    assert rows[_d(2026, 11, 6)]["成交量"] == "28000000"


def test_parse_rows_rejects_supplier_error():
    with pytest.raises(DataFetchError, match="supplier errorcode 7"):
        H._parse_history_rows(synthetic_payload(errorcode=7, errmsg="no permission"), IDENTITY)


def test_parse_rows_rejects_missing_errorcode():
    payload = synthetic_payload()
    payload.pop("errorcode")
    with pytest.raises(DataFetchError, match="errorcode missing"):
        H._parse_history_rows(payload, IDENTITY)


def test_parse_rows_rejects_multiple_tables():
    table = {"thscode": IDENTITY, "time": [], "table": {}}
    with pytest.raises(DataFetchError, match="exactly one table"):
        H._parse_history_rows(synthetic_payload(tables=[table, table]), IDENTITY)


def test_parse_rows_rejects_foreign_security():
    with pytest.raises(DataFetchError, match="!= requested"):
        H._parse_history_rows(synthetic_payload(identity="000001.SZ"), IDENTITY)


def test_parse_rows_rejects_missing_column():
    with pytest.raises(DataFetchError, match="missing column amount"):
        H._parse_history_rows(synthetic_payload(drop_column="amount"), IDENTITY)


@pytest.mark.parametrize("mode", ["compact", "timestamp"])
def test_parse_rows_rejects_non_iso_time(mode):
    with pytest.raises(DataFetchError, match="not an ISO calendar date"):
        H._parse_history_rows(synthetic_payload(time_format=mode), IDENTITY)


def test_parse_rows_rejects_duplicate_time():
    payload = synthetic_payload(sessions=SESSIONS + ["2026-11-06"])
    with pytest.raises(DataFetchError, match="duplicate time row"):
        H._parse_history_rows(payload, IDENTITY)


def test_parse_rows_rejects_empty_rows():
    with pytest.raises(DataFetchError, match="carries no rows"):
        H._parse_history_rows(synthetic_payload(sessions=[]), IDENTITY)


def test_parse_rows_rejects_column_length_mismatch():
    payload = synthetic_payload()
    payload["tables"][0]["table"]["close"] = payload["tables"][0]["table"]["close"][:-1]
    with pytest.raises(DataFetchError, match="length != time length"):
        H._parse_history_rows(payload, IDENTITY)


# ---------------------------------------------------------------------------
# 逐窗口双 CPS 单位资格：量额不随复权变动 + 未复权 VWAP ∈ 当日未复权区间
# ---------------------------------------------------------------------------
def test_unit_qualification_accepts_consistent_dual_cps_rows(monkeypatch):
    adjusted = H._parse_history_rows(synthetic_payload(), IDENTITY)
    raw = H._parse_history_rows(synthetic_payload(prices="raw"), IDENTITY)
    # 通过即无异常；VWAP=1700 越出复权区间 [1521, 1548] 恰证明比较只用未复权价格。
    H._qualify_volume_unit_consistency(adjusted, raw, IDENTITY)


def test_unit_qualification_rejects_window_mismatch():
    adjusted = H._parse_history_rows(synthetic_payload(), IDENTITY)
    raw = H._parse_history_rows(synthetic_payload(prices="raw", sessions=SESSIONS[:2]), IDENTITY)
    with pytest.raises(DataFetchError, match="windows differ"):
        H._qualify_volume_unit_consistency(adjusted, raw, IDENTITY)


def test_unit_qualification_rejects_volume_drift():
    adjusted = H._parse_history_rows(synthetic_payload(), IDENTITY)
    raw_payload = synthetic_payload(prices="raw")
    raw_payload["tables"][0]["table"]["volume"][1] = Decimal("32000001")
    raw = H._parse_history_rows(raw_payload, IDENTITY)
    with pytest.raises(DataFetchError, match="成交量随复权变动"):
        H._qualify_volume_unit_consistency(adjusted, raw, IDENTITY)


def test_unit_qualification_rejects_amount_drift():
    adjusted = H._parse_history_rows(synthetic_payload(), IDENTITY)
    raw_payload = synthetic_payload(prices="raw")
    raw_payload["tables"][0]["table"]["amount"][1] = Decimal("54784000001")
    raw = H._parse_history_rows(raw_payload, IDENTITY)
    with pytest.raises(DataFetchError, match="成交额随复权变动"):
        H._qualify_volume_unit_consistency(adjusted, raw, IDENTITY)


def test_unit_qualification_rejects_one_sided_invalid_cell():
    """对照响应单侧量额非法（复权侧合法）：两响应不一致，不交下游。"""
    adjusted = H._parse_history_rows(synthetic_payload(), IDENTITY)
    raw_payload = synthetic_payload(prices="raw")
    raw_payload["tables"][0]["table"]["volume"][1] = None
    raw = H._parse_history_rows(raw_payload, IDENTITY)
    with pytest.raises(DataFetchError, match="成交量在复权/不复权响应一侧非法"):
        H._qualify_volume_unit_consistency(adjusted, raw, IDENTITY)


@pytest.mark.parametrize("side", ["above", "below"])
def test_unit_qualification_rejects_vwap_outside_raw_range(side):
    """未复权 VWAP 越出当日未复权区间即单位资格不成立（量纲错位）。"""
    adjusted = H._parse_history_rows(synthetic_payload(), IDENTITY)
    raw_payload = synthetic_payload(prices="raw")
    index = 1
    if side == "above":  # VWAP=1712 > raw high
        raw_payload["tables"][0]["table"]["high"][index] = Decimal("1710")
    else:  # VWAP=1712 < raw low
        raw_payload["tables"][0]["table"]["low"][index] = Decimal("1720")
    raw = H._parse_history_rows(raw_payload, IDENTITY)
    with pytest.raises(DataFetchError, match="越出当日未复权区间"):
        H._qualify_volume_unit_consistency(adjusted, raw, IDENTITY)


def test_unit_qualification_accepts_boundary_vwap_on_flat_session():
    """一字板（o=h=l=c、VWAP=high=low）在闭区间上成立。"""
    adjusted_payload = synthetic_payload(sessions=SESSIONS[:1])
    adjusted_payload["tables"][0]["table"].update({
        "open": [Decimal("1530")], "high": [Decimal("1530")],
        "low": [Decimal("1530")], "close": [Decimal("1530")],
    })
    raw_payload = synthetic_payload(prices="raw", sessions=SESSIONS[:1])
    raw_payload["tables"][0]["table"].update({
        "open": [Decimal("1700")], "high": [Decimal("1700")],
        "low": [Decimal("1700")], "close": [Decimal("1700")],
    })
    adjusted = H._parse_history_rows(adjusted_payload, IDENTITY)
    raw = H._parse_history_rows(raw_payload, IDENTITY)
    H._qualify_volume_unit_consistency(adjusted, raw, IDENTITY)


def test_unit_qualification_cannot_detect_proportional_scaling():
    """已知能力边界：量额等比缩放（各 ×100，百股/百元量纲）VWAP 不变且
    两口径仍一致——双请求判据**检测不到**绝对量纲错位，只能证明比率自洽
    与未复权一致性。本测试锁定该边界：绝对单位（股/元）的证据是东财 250
    日对照（qualification 报告），不是双 CPS 请求；残余缺口随文档披露，
    不静默假定（issue #91 第四轮）。"""
    adjusted = H._parse_history_rows(synthetic_payload(), IDENTITY)
    raw = H._parse_history_rows(synthetic_payload(prices="raw"), IDENTITY)
    scaled_adjusted = {
        day: {**row, "成交量": str(Decimal(row["成交量"]) * 100),
              "成交额": str(Decimal(row["成交额"]) * 100)}
        for day, row in adjusted.items()
    }
    scaled_raw = {
        day: {**row, "成交量": str(Decimal(row["成交量"]) * 100),
              "成交额": str(Decimal(row["成交额"]) * 100)}
        for day, row in raw.items()
    }
    # 百股/百元量纲下判据照样通过——这正是它不能证明绝对单位的原因。
    H._qualify_volume_unit_consistency(scaled_adjusted, scaled_raw, IDENTITY)


def test_read_scope_covers_http_only(monkeypatch):
    """读取范围按 MCP/HTTP **各自证券资格与参与范围**判定（第二十二轮）。

    MCP 语义保持原样（启用即全普通股，不加调用方门限）；HTTP 贡献限定个股
    深研执行上下文（``http_preset_scope_active``）——非深研 HTTP-only 不由
    HTTP 贡献读取范围（否则 scope True 跳过旧 DB 而 HTTP 又不预置，退化），
    深研内段级行为与第十九轮一致；双源组合与资格外反例全矩阵锁定。
    """
    strict_calendar(monkeypatch)
    from data_provider.ifind_daily_adapter import ifind_daily_read_scope
    from src.core.research_run import current_research

    segments = ["600519", "600036", "600601", "603259", "688981",
                "000001.SZ", "000002", "001979", "002415", "003816",
                "300059", "301269"]
    # 双关：不进入。
    enabled_config(monkeypatch, enabled=False)
    assert ifind_daily_read_scope("600519") is False
    # 仅 MCP：全普通股范围进入（既有语义不变，非深研同样成立）。
    enabled_config(monkeypatch, enabled=True)
    assert ifind_daily_read_scope("600519") is True
    assert ifind_daily_read_scope("000002") is True
    # MCP+HTTP 双开非深研：True 由 MCP 支撑（HTTP 预置被门挡、MCP 原样）。
    http_enabled_config(monkeypatch, mcp_enabled=True)
    assert ifind_daily_read_scope("600519") is True
    # 仅 HTTP 非深研：HTTP 贡献被门挡，不进入（保持 DB 路径）。
    http_enabled_config(monkeypatch)
    for code in segments:
        assert ifind_daily_read_scope(code) is False, code
    # 仅 HTTP 深研上下文：段级资格后全部普通股进入（第十一轮白名单已删）。
    research_token = _bind_run_adapter()
    try:
        for code in segments:
            assert ifind_daily_read_scope(code) is True, code
    finally:
        current_research.reset(research_token)
    # 不合格证券（ETF/指数/B 股/北交所）任何组合都不进入。
    http_enabled_config(monkeypatch)
    research_token = _bind_run_adapter()
    try:
        assert ifind_daily_read_scope("510300") is False
        assert ifind_daily_read_scope("000001.SH") is False
        assert ifind_daily_read_scope("900901") is False
        assert ifind_daily_read_scope("920748") is False
    finally:
        current_research.reset(research_token)


def _bind_run_adapter():
    """adapter 文件内绑定个股深研上下文的辅助（语义同 cache._bind_run）。"""
    from src.core.research_run import ResearchRun, current_research

    return current_research.set(ResearchRun(timeout_seconds=30))


def test_http_qualified_identity_scope(monkeypatch):
    """http_qualified_identity：段级资格（第十九轮白名单删除）；显式交易所
    身份与非普通股边界语义不变。"""
    assert H.http_qualified_identity("600519.SH") == "600519.SH"
    assert H.http_qualified_identity("000001") == "000001.SZ"  # 平安银行（000 段）
    assert H.http_qualified_identity("600601.SH") == "600601.SH"  # 沪 6 开头段内
    assert H.http_qualified_identity("000002.SZ") == "000002.SZ"  # 深 000 段内
    assert H.http_qualified_identity("301236.SZ") == "301236.SZ"  # 创业板 301 段内
    assert H.http_qualified_identity("003816.SZ") == "003816.SZ"  # 深 003 段内
    # 非普通股：显式身份/ETF/B 股/北交所仍在既有资格层拒绝。
    assert H.http_qualified_identity("000001.SH") is None  # 上证指数
    assert H.http_qualified_identity("399001.SZ") is None  # 深市指数
    assert H.http_qualified_identity("510300.SH") is None  # 沪 ETF
    assert H.http_qualified_identity("159915.SZ") is None  # 深 ETF
    assert H.http_qualified_identity("900901.SH") is None  # 沪 B 股
    assert H.http_qualified_identity("200001.SZ") is None  # 深 B 股
    assert H.http_qualified_identity("920748") is None  # 北交所


def test_unit_qualification_zero_volume_defers_to_value_gate(monkeypatch):
    """两侧量额同为 0：单位资格不除零不误判，交既有数值资格拒绝。"""
    strict_calendar(monkeypatch)
    from src.core.trading_calendar import trading_sessions_between

    payload = synthetic_payload()
    payload["tables"][0]["table"]["volume"][1] = Decimal("0")
    payload["tables"][0]["table"]["amount"][1] = Decimal("0")
    rows = H._parse_history_rows(payload, IDENTITY)
    # 对照响应同样 0（同态缺失）→ 单位资格静默放行，下游数值资格拒绝。
    raw_payload = synthetic_payload(prices="raw")
    raw_payload["tables"][0]["table"]["volume"][1] = Decimal("0")
    raw_payload["tables"][0]["table"]["amount"][1] = Decimal("0")
    raw_rows = H._parse_history_rows(raw_payload, IDENTITY)
    H._qualify_volume_unit_consistency(rows, raw_rows, IDENTITY)
    sessions = trading_sessions_between("cn", _d(2026, 11, 4), _d(2026, 11, 6))
    with pytest.raises(DataFetchError, match="数值缺失或非法"):
        A_qualify(rows, sessions)


# ---------------------------------------------------------------------------
# 全窗口资格（复权侧价格走既有严格日历资格链）
# ---------------------------------------------------------------------------
def test_full_window_accepts_complete_sessions(monkeypatch):
    strict_calendar(monkeypatch)
    from src.core.trading_calendar import trading_sessions_between

    rows = H._parse_history_rows(synthetic_payload(), IDENTITY)
    sessions = trading_sessions_between("cn", _d(2026, 11, 4), _d(2026, 11, 6))
    frame = A_qualify(rows, sessions)
    assert len(frame) == 3
    assert list(frame.columns) == ["日期", "开盘价", "最高价", "最低价", "收盘价", "成交量", "成交额"]


def A_qualify(rows, sessions):
    from data_provider.ifind_daily_adapter import _qualify_window

    return _qualify_window(rows, sessions, IDENTITY)


def test_full_window_rejects_missing_session(monkeypatch):
    """任一期望交易日缺失即整窗拒绝；空值不自动判停牌。"""
    strict_calendar(monkeypatch)
    from src.core.trading_calendar import trading_sessions_between

    rows = H._parse_history_rows(synthetic_payload(sessions=SESSIONS[:2]), IDENTITY)
    sessions = trading_sessions_between("cn", _d(2026, 11, 4), _d(2026, 11, 6))
    with pytest.raises(DataFetchError, match="完整窗口缺少交易日"):
        A_qualify(rows, sessions)


def test_null_cells_rejected_not_suspension(monkeypatch):
    """null 单元格不自动判停牌：进入数值缺失拒绝路径。"""
    strict_calendar(monkeypatch)
    from src.core.trading_calendar import trading_sessions_between

    payload = synthetic_payload()
    payload["tables"][0]["table"]["volume"][1] = None
    rows = H._parse_history_rows(payload, IDENTITY)
    sessions = trading_sessions_between("cn", _d(2026, 11, 4), _d(2026, 11, 6))
    with pytest.raises(DataFetchError, match="数值缺失或非法"):
        A_qualify(rows, sessions)


def test_supplier_errmsg_redacts_local_token():
    """供应商裸回显凭据不落日志：errmsg 进入异常消息前按本机 token 脱敏。"""
    token = "secret-standard-token"
    payload = synthetic_payload(errorcode=7, errmsg=f"bad token {token} for 600519.SH")
    with pytest.raises(DataFetchError) as excinfo:
        H._parse_history_rows(payload, IDENTITY, access_token=token)
    message = str(excinfo.value)
    assert token not in message
    assert "***" in message


def test_redact_token_leaves_unrelated_text_intact():
    assert H._redact_token("no secret here", "tok") == "no secret here"
    assert H._redact_token("has tok inside", "tok") == "has *** inside"
    assert H._redact_token("empty token", "") == "empty token"


# ---------------------------------------------------------------------------
# 传输边界：受控替换 requests.post
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 凭据模式与 access_token 自动续用（issue #91 第六轮；官方示例 get_access_token）
# ---------------------------------------------------------------------------
@pytest.fixture()
def fresh_token_cache(monkeypatch):
    """每个用例独立的进程级 token 缓存（teardown 恢复原值）。"""
    monkeypatch.setattr(H, "_token_cache", {})


def refresh_only_config(monkeypatch: pytest.MonkeyPatch) -> object:
    """自动模式启用配置（唯一凭据 refresh_token；第八轮移除静态模式）。"""
    return http_enabled_config(monkeypatch)


def token_payload(token: str, expired_time: object) -> dict:
    return {
        "errorcode": 0,
        "errmsg": "success",
        "data": {"access_token": token, "expired_time": expired_time},
    }


def install_full_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    calls: List[dict],
    *,
    token_payloads: Optional[List[dict]] = None,
    data_responses: Optional[List[tuple]] = None,
):
    """按 URL 分发 curl 响应：token 端点回放 token_payloads（耗尽后默认成功）；
    数据端点回放 data_responses（按序 ``(status, body)`` 弹出，耗尽后默认 CPS
    分发合成响应）。"""
    token_payloads = list(token_payloads or [])
    data_responses = list(data_responses or [])
    far_future = int(_time_module.time()) + 86400
    token_counter = {"n": 0}

    def fake_curl_post(url, headers, body, *, label):
        calls.append({"url": url, "headers": dict(headers or {}), "body": body, "label": label})
        if url == H._TOKEN_ENDPOINT:
            token_counter["n"] += 1
            payload = token_payloads.pop(0) if token_payloads else token_payload(
                f"auto-token-{token_counter['n']}", far_future,
            )
            return 200, json_dumps(payload).encode()
        if data_responses:
            return data_responses.pop(0)
        request = json.loads(body or b"{}")
        cps = request.get("functionpara", {}).get("CPS")
        payload = adjusted_payload if cps == "6" else raw_payload
        return 200, json_dumps(payload).encode()

    adjusted_payload = synthetic_payload()
    raw_payload = synthetic_payload(prices="raw")
    monkeypatch.setattr(H, "_http_api_curl_post", fake_curl_post)


def test_refresh_only_config_enables_and_uses_token_endpoint(monkeypatch, fresh_token_cache):
    """仅配 refresh_token 即启用：先换取 access_token 再发取数请求。"""
    strict_calendar(monkeypatch)
    refresh_only_config(monkeypatch)
    assert H.ifind_http_daily_enabled() is True
    calls: List[dict] = []
    install_full_dispatch(monkeypatch, calls)
    fetcher = H.IfindHttpDailyFetcher()
    frame = fetcher._fetch_raw_data("600519.SH", "2026-11-04", "2026-11-06")
    assert len(frame) == 3
    token_calls = [c for c in calls if c["url"] == H._TOKEN_ENDPOINT]
    data_calls = [c for c in calls if c["url"] != H._TOKEN_ENDPOINT]
    assert len(token_calls) == 1
    assert token_calls[0]["headers"]["refresh_token"] == "long-lived-refresh-token"
    assert token_calls[0]["headers"]["Content-Type"] == "application/json"
    assert token_calls[0]["body"] is None  # 官方示例形态：无请求体
    assert len(data_calls) == 2
    assert all(c["headers"]["access_token"] == "auto-token-1" for c in data_calls)
    # 凭据只出现在 header：url 与 body 都不含任何 token 值。
    for call in calls:
        assert "auto-token-1" not in (call["url"] or "")
        assert "long-lived-refresh-token" not in (call["url"] or "")
        assert "auto-token-1" not in (call["body"] or b"").decode("utf-8", "ignore")


def test_transport_headers_carry_token_not_url_or_body(monkeypatch, fresh_token_cache):
    """curl 传输契约：凭据只进 header（0600 临时文件），url/body 不携带。"""
    strict_calendar(monkeypatch)
    refresh_only_config(monkeypatch)
    calls: List[dict] = []
    install_full_dispatch(monkeypatch, calls)
    fetcher = H.IfindHttpDailyFetcher()
    fetcher._fetch_raw_data("600519.SH", "2026-11-04", "2026-11-06")
    for call in calls:
        joined_url_body = (call["url"] or "") + (call["body"] or b"").decode("utf-8", "ignore")
        assert "auto-token-1" not in joined_url_body
        assert "long-lived-refresh-token" not in joined_url_body
    data_calls = [c for c in calls if c["url"] != H._TOKEN_ENDPOINT]
    assert all(c["headers"]["access_token"] == "auto-token-1" for c in data_calls)


def test_token_cached_across_window_attempts(monkeypatch, fresh_token_cache):
    """缓存命中：后续窗口尝试复用 access_token，token 端点零重复出站。"""
    strict_calendar(monkeypatch)
    refresh_only_config(monkeypatch)
    calls: List[dict] = []
    install_full_dispatch(monkeypatch, calls)
    fetcher = H.IfindHttpDailyFetcher()
    fetcher._fetch_raw_data("600519.SH", "2026-11-04", "2026-11-06")
    fetcher._fetch_raw_data("600519.SH", "2026-11-05", "2026-11-06")
    token_calls = [c for c in calls if c["url"] == H._TOKEN_ENDPOINT]
    assert len(token_calls) == 1


def test_expired_cache_triggers_refresh(monkeypatch, fresh_token_cache):
    """缓存过期（含 60 秒缓冲）后新尝试重新换取并更新缓存。"""
    strict_calendar(monkeypatch)
    refresh_only_config(monkeypatch)
    now = _time_module.time()
    calls: List[dict] = []
    install_full_dispatch(
        monkeypatch, calls,
        token_payloads=[token_payload("expired-token", now - 10)],
    )
    fetcher = H.IfindHttpDailyFetcher()
    fetcher._fetch_raw_data("600519.SH", "2026-11-04", "2026-11-06")
    fetcher._fetch_raw_data("600519.SH", "2026-11-05", "2026-11-06")
    token_calls = [c for c in calls if c["url"] == H._TOKEN_ENDPOINT]
    assert len(token_calls) == 2  # 首次换取的 token 已过期：第二次尝试重新换取
    data_calls = [c for c in calls if c["url"] != H._TOKEN_ENDPOINT]
    assert data_calls[0]["headers"]["access_token"] == "expired-token"
    assert data_calls[2]["headers"]["access_token"].startswith("auto-token-")


@pytest.mark.parametrize("kind", ["seconds", "milliseconds", "missing", "garbage"])
def test_parse_expired_time_formats(kind):
    """expired_time 防御解析矩阵；时间参数在执行时求值（不依赖收集时刻）。"""
    now = _time_module.time()
    raw = {
        "seconds": now + 3600,
        "milliseconds": (now + 3600) * 1000,
        "missing": None,
        "garbage": "garbage",
    }[kind]
    parsed = H._parse_expired_at(raw, now)
    if kind in ("seconds", "milliseconds"):
        assert now - 5 <= parsed - 3600 <= now + 5
    else:
        assert parsed == pytest.approx(now + H._TOKEN_FALLBACK_TTL_SECONDS, abs=5)


def test_parse_expired_time_iso_string():
    from datetime import datetime as dt

    now = _time_module.time()
    stamp = dt.fromtimestamp(now + 7200)
    parsed = H._parse_expired_at(stamp.strftime("%Y-%m-%d %H:%M:%S"), now)
    assert now + 7000 <= parsed <= now + 7400


def test_auth_401_forces_refresh_and_retries_once(monkeypatch, fresh_token_cache):
    """自动模式 401：强制续用一次并重试同一请求成功；总尝试有界。"""
    strict_calendar(monkeypatch)
    refresh_only_config(monkeypatch)
    calls: List[dict] = []
    install_full_dispatch(
        monkeypatch, calls,
        data_responses=[(401, b"{}")],
    )
    fetcher = H.IfindHttpDailyFetcher()
    frame = fetcher._fetch_raw_data("600519.SH", "2026-11-04", "2026-11-06")
    assert len(frame) == 3
    token_calls = [c for c in calls if c["url"] == H._TOKEN_ENDPOINT]
    assert len(token_calls) == 2  # 初次换取 + 401 后强制刷新
    data_calls = [c for c in calls if c["url"] != H._TOKEN_ENDPOINT]
    assert len(data_calls) == 3  # 401 一次 + 重试成功 + 对照请求
    assert data_calls[0]["headers"]["access_token"] == "auto-token-1"
    assert data_calls[1]["headers"]["access_token"] == "auto-token-2"
    assert data_calls[2]["headers"]["access_token"] == "auto-token-2"


def test_auth_401_after_refresh_fails_with_clear_message(monkeypatch, fresh_token_cache):
    """刷新重试后仍 401：明确指向 refresh_token 有效性/权限，且只重试一次。"""
    strict_calendar(monkeypatch)
    refresh_only_config(monkeypatch)
    calls: List[dict] = []
    install_full_dispatch(
        monkeypatch, calls,
        data_responses=[(401, b"{}"), (403, b"{}")],
    )
    fetcher = H.IfindHttpDailyFetcher()
    with pytest.raises(DataFetchError, match="after token refresh"):
        fetcher._fetch_raw_data("600519.SH", "2026-11-04", "2026-11-06")
    data_calls = [c for c in calls if c["url"] != H._TOKEN_ENDPOINT]
    assert len(data_calls) == 2  # 初次 + 单次重试，无循环


def test_first_fetch_failure_after_success_keeps_reason(monkeypatch, fresh_token_cache):
    """首次换取成功后的业务失败（500）原因保真：不被 token 层吞掉或改写。"""
    strict_calendar(monkeypatch)
    refresh_only_config(monkeypatch)
    calls: List[dict] = []
    install_full_dispatch(
        monkeypatch, calls,
        data_responses=[(500, b"{}")],
    )
    fetcher = H.IfindHttpDailyFetcher()
    with pytest.raises(DataFetchError, match="http status 500"):
        fetcher._fetch_raw_data("600519.SH", "2026-11-04", "2026-11-06")
    assert len([c for c in calls if c["url"] == H._TOKEN_ENDPOINT]) == 1
    assert len([c for c in calls if c["url"] != H._TOKEN_ENDPOINT]) == 1  # 无重试


def test_token_refresh_supplier_error_redacts(monkeypatch, fresh_token_cache):
    """token 端点 errorcode≠0：供应商错误拒绝且 errmsg 按本机 refresh_token 脱敏。"""
    strict_calendar(monkeypatch)
    refresh_only_config(monkeypatch)
    calls: List[dict] = []
    secret = "long-lived-refresh-token"
    install_full_dispatch(
        monkeypatch, calls,
        token_payloads=[{
            "errorcode": 7,
            "errmsg": f"invalid refresh token {secret} for account",
        }],
    )
    fetcher = H.IfindHttpDailyFetcher()
    with pytest.raises(DataFetchError, match="token refresh supplier errorcode 7") as excinfo:
        fetcher._fetch_raw_data("600519.SH", "2026-11-04", "2026-11-06")
    message = str(excinfo.value)
    assert secret not in message
    assert "***" in message


def test_token_refresh_transport_error_wrapped(monkeypatch, fresh_token_cache):
    """token 端点传输失败（curl 退出）：包装为供应商错误传播走兜底。"""
    strict_calendar(monkeypatch)
    refresh_only_config(monkeypatch)

    def boom(url, headers, body, *, label):
        raise DataFetchError(f"ifind http daily {label} transport failed: curl exit 7")

    monkeypatch.setattr(H, "_http_api_curl_post", boom)
    fetcher = H.IfindHttpDailyFetcher()
    with pytest.raises(DataFetchError, match="token refresh transport failed: curl exit 7"):
        fetcher._fetch_raw_data("600519.SH", "2026-11-04", "2026-11-06")


def test_concurrent_token_resolution_single_flight(monkeypatch, fresh_token_cache):
    """并发缓存 miss：多线程只产生一次 token 端点出站（锁内单飞）。"""
    import concurrent.futures

    strict_calendar(monkeypatch)
    refresh_only_config(monkeypatch)
    calls: List[dict] = []
    install_full_dispatch(monkeypatch, calls)
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures = [
            pool.submit(H._resolve_access_token, "long-lived-refresh-token")
            for _ in range(6)
        ]
        results = [f.result(timeout=10) for f in futures]
    assert all(r == results[0] for r in results)
    token_calls = [c for c in calls if c["url"] == H._TOKEN_ENDPOINT]
    assert len(token_calls) == 1


def test_config_validation_requires_refresh_token():
    from src.config import Config

    base = dict(enable_fundamental_pipeline=True, deep_research_cross_validate=False)
    with pytest.raises(ValueError, match="IFIND_HTTP_REFRESH_TOKEN"):
        Config(**base, data_source_profile="choice", enable_ifind_http_api=True)
    # refresh_token 是唯一凭据形态（第八轮移除静态 access_token 模式）。
    config = Config(
        **base, data_source_profile="choice", enable_ifind_http_api=True,
        ifind_http_refresh_token="long-lived-refresh-token",
    )
    assert config.ifind_http_timeout_seconds == 30.0


def test_data_source_contract_covers_http_toggle(monkeypatch):
    """阻断 1（第十轮）：缓存身份覆盖 MCP×HTTP 四象限，互不相同。

    HTTP 开启后普通非强刷不复用 HTTP 关闭期的旧成功报告/维度缓存（身份
    变化 → 重新获取让新源真实出站）；HTTP 关闭时与历史身份一致。
    """
    from src.config import Config

    base = dict(enable_fundamental_pipeline=True, deep_research_cross_validate=False)

    def contract(*, mcp: bool, http: bool) -> str:
        return Config(
            **base, data_source_profile="choice",
            enable_ifind_mcp=mcp,
            ifind_mcp_endpoint="https://fixture.invalid/mcp" if mcp else None,
            ifind_mcp_token="test-only" if mcp else None,
            enable_ifind_http_api=http,
            ifind_http_refresh_token="long-lived-refresh-token" if http else None,
        ).data_source_contract

    off_off = contract(mcp=False, http=False)
    on_off = contract(mcp=True, http=False)
    off_on = contract(mcp=False, http=True)
    on_on = contract(mcp=True, http=True)
    assert len({off_off, on_off, off_on, on_on}) == 4
    assert "-ifind-daily-cashqfq-v1" in on_off and "-ifind-daily-cashqfq-v1" not in off_off
    assert "-ifind-http-daily-v1" in off_on and "-ifind-http-daily-v1" in on_on
    assert "-ifind-http-daily-v1" not in off_off and "-ifind-http-daily-v1" not in on_off


# ---------------------------------------------------------------------------
# 第十九轮：段级资格路由（普通股资格段全覆盖；非资格证券保持原候选）
# ---------------------------------------------------------------------------
def test_http_routing_covers_ordinary_share_segments(monkeypatch):
    """HTTP 对全部沪深普通股资格段路由（第十一轮 8 股白名单已删）。

    证据边界如实声明：已采样段为 600/601/603/605/688 与深 000/001/002/003/
    300/301 的代表样本；沪市资格实现为「6 开头」，覆盖 689（CDR）等未采样
    前缀——路由按资格段判定，不宣称未采样段已验证。非普通股（指数/ETF/
    B 股/北交所）不出站、保持原有候选。
    """
    strict_calendar(monkeypatch)
    http_enabled_config(monkeypatch)
    # 资格段内普通股（含未在量纲对照样本中的标的）：正常路由。
    for code, identity in [
        ("600519.SH", "600519.SH"),
        ("600036", "600036.SH"),
        ("601398", "601398.SH"),
        ("603259.SH", "603259.SH"),
        ("605499", "605499.SH"),
        ("688981.SH", "688981.SH"),
        ("000001.SZ", "000001.SZ"),
        ("000002.SZ", "000002.SZ"),
        ("001979.SZ", "001979.SZ"),
        ("002594.SZ", "002594.SZ"),
        ("003816.SZ", "003816.SZ"),
        ("300750", "300750.SZ"),
        ("301236.SZ", "301236.SZ"),
    ]:
        routing = H.http_daily_routing(code, None)
        assert routing is not None and routing[0] == identity, code
    # 非普通股资格：不出站（既有资格层拒绝，与开关无关）。
    for code in ["000001.SH", "399001.SZ", "510300.SH", "159915.SZ",
                 "900901.SH", "200001.SZ", "920748"]:
        assert H.http_daily_routing(code, None) is None, code


def test_manager_preset_covers_segments_and_skips_non_qualifying(monkeypatch):
    """manager 预置块走 http_daily_routing：段内普通股 prepare（fetcher 建）、
    非资格证券不出站。"""
    from data_provider.base import DataFetcherManager

    strict_calendar(monkeypatch)
    http_enabled_config(monkeypatch)
    mgr = DataFetcherManager(fetchers=[])
    mgr._ifind_http_daily_fetcher = None
    # 非资格：prepare 即 None（不出站、不计供应商故障）。
    assert mgr._prepare_ifind_http_daily_attempt("000001.SH", None) is None
    assert mgr._prepare_ifind_http_daily_attempt("510300", None) is None
    assert mgr._prepare_ifind_http_daily_attempt("920748", None) is None
    # 资格段内（含此前白名单外反例标的）：正常 prepare。
    for code, identity in [
        ("600519", "600519.SH"),
        ("600601", "600601.SH"),
        ("000002", "000002.SZ"),
        ("301236", "301236.SZ"),
    ]:
        prepared = mgr._prepare_ifind_http_daily_attempt(code, None)
        assert prepared is not None and prepared[1] == identity, code


def test_deep_research_final_entries_change_identity_with_http_toggle(monkeypatch):
    """第十轮阻断 1 最终入口：真实深研服务报告日缓存键与维度缓存路径在
    同日 HTTP 切换后都不再命中旧身份（不复用旧报告、维度缓存重新获取）。
    """
    from src.deep_research_dims import dim_cache as D
    from src.services import deep_research_service as S

    enabled_config(monkeypatch, enabled=False)  # MCP off、HTTP off
    report_key_off = S._date_key("600519")
    dim_path_off = D._cache_path("600519", "capital")
    http_enabled_config(monkeypatch)  # MCP off、HTTP on
    report_key_on = S._date_key("600519")
    dim_path_on = D._cache_path("600519", "capital")
    assert report_key_off != report_key_on
    assert dim_path_off != dim_path_on
    assert "-ifind-http-daily-v1" in report_key_on
    assert "-ifind-http-daily-v1" in dim_path_on


def test_dim_cache_real_roundtrip_misses_after_http_toggle(monkeypatch, tmp_path):
    """第十三轮：维度缓存**真实写读流程**——HTTP off 写入并命中的维度缓存，
    HTTP on 后 load miss（路径含新身份段）→ 强制重新获取；不只比较键字符串。
    （technical 维度无日期/身份特判，纯验证身份段对命中路径的作用。）
    """
    from src.deep_research_dims import dim_cache as D

    monkeypatch.setattr(D, "_CACHE_DIR", str(tmp_path))
    enabled_config(monkeypatch, enabled=False)  # MCP off、HTTP off
    D.save_cached_dim("600519", "technical", {"technical_dim": {"verified": True}})
    assert D.load_cached_dim("600519", "technical") == {"technical_dim": {"verified": True}}
    http_enabled_config(monkeypatch)  # HTTP on：契约身份变化
    assert D.load_cached_dim("600519", "technical") is None  # 旧身份缓存不再命中


def test_report_cache_entry_not_reused_after_http_toggle(monkeypatch, fresh_token_cache):
    """第十三轮：报告级日缓存真实容器——HTTP off 写入的条目在 HTTP on 后按
    新键查找 miss（不复用旧报告）；条目形状与消费方一致。"""
    from src.services import deep_research_service as S

    monkeypatch.setattr(S, "_report_cache", {})
    enabled_config(monkeypatch, enabled=False)  # MCP off、HTTP off
    key_off = S._date_key("600519")
    entry = ("600519_202611160000", _time_module.time(), {"final": "partial"})
    S._report_cache[key_off] = entry
    assert S._report_cache.get(S._date_key("600519")) == entry  # 同身份命中
    http_enabled_config(monkeypatch)  # HTTP on：契约身份变化
    key_on = S._date_key("600519")
    assert S._report_cache.get(key_on) is None  # 新身份 miss：不复用旧报告


# ---------------------------------------------------------------------------
# 第七轮反例矩阵：凭据隔离/有界等待/并发去重/首次回显脱敏/真实失败原因/有界读取
# ---------------------------------------------------------------------------
def test_token_cache_isolated_by_credential(monkeypatch, fresh_token_cache):
    """缓存按凭据指纹分键：换账号（运行时配置变更）不误用旧账号 token。"""
    strict_calendar(monkeypatch)
    refresh_only_config(monkeypatch)
    calls: List[dict] = []
    install_full_dispatch(monkeypatch, calls)
    fetcher = H.IfindHttpDailyFetcher()
    fetcher._fetch_raw_data("600519.SH", "2026-11-04", "2026-11-06")
    first_token = H._resolve_access_token("long-lived-refresh-token")
    # 运行时换到另一个账号的 refresh_token：独立换取，不复用旧账号缓存。
    second_token = H._resolve_access_token("another-account-refresh-token")
    assert second_token != first_token
    # 回到原凭据：仍命中原账号缓存（不被他账号覆盖）。
    assert H._resolve_access_token("long-lived-refresh-token") == first_token
    token_calls = [c for c in calls if c["url"] == H._TOKEN_ENDPOINT]
    assert len(token_calls) == 2  # 每个凭据各换取一次


def test_force_refresh_concurrent_dedup(monkeypatch, fresh_token_cache):
    """并发 force_refresh：一个等待者完成后其余复用，不重复换取（fetched_at 去重）。"""
    import concurrent.futures
    import time as _t

    strict_calendar(monkeypatch)
    refresh_only_config(monkeypatch)
    calls: List[dict] = []
    token_counter = {"n": 0}

    def fake_curl_post(url, headers, body, *, label):
        calls.append({"url": url})
        if url == H._TOKEN_ENDPOINT:
            token_counter["n"] += 1
            _t.sleep(0.15)  # 保证另一线程在持锁期间已进入等待
            payload = token_payload(f"auto-token-{token_counter['n']}", int(_t.time()) + 86400)
            return 200, json_dumps(payload).encode()
        return 200, json_dumps(synthetic_payload()).encode()

    monkeypatch.setattr(H, "_http_api_curl_post", fake_curl_post)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(
                H._resolve_access_token, "long-lived-refresh-token",
                force_refresh=True,
            )
            for _ in range(3)
        ]
        results = [f.result(timeout=10) for f in futures]
    assert len(set(results)) == 1
    assert token_counter["n"] == 1  # 并发强制刷新只产生一次换取出站


def test_waiter_bounded_by_deadline(monkeypatch, fresh_token_cache):
    """锁等待分段可取消：他人持锁换取时等待者受 deadline 约束超时退出。"""
    import time as _t

    from src.patches.eastmoney_patch import request_deadline

    strict_calendar(monkeypatch)
    refresh_only_config(monkeypatch)
    lock = H._token_cache_lock
    assert lock.acquire()
    try:
        token_value = request_deadline.set(_t.monotonic() + 0.25)
        try:
            # 分段轮询中 check_attempt_alive 检出 deadline 耗尽（既有停止契约）。
            with pytest.raises(TimeoutError, match="deadline"):
                H._resolve_access_token("long-lived-refresh-token")
        finally:
            request_deadline.reset(token_value)
    finally:
        lock.release()
    # 等待期间零出站：锁被外部持有，换取从未发生。
    assert H._token_cache == {}


def test_waiter_cancelled_while_lock_held(monkeypatch, fresh_token_cache):
    """无外层 deadline 时锁等待可被用户停止取消（ResearchStopped 传播）。

    持锁期间等待者的分段轮询每次醒来都经 ``check_attempt_alive``：深研 run
    已取消时立即以停止信号退出，不无限等待（第八轮可取消语义）。
    """
    from src.core.research_run import ResearchRun, current_research
    from src.core.research_run import ResearchStopped

    strict_calendar(monkeypatch)
    refresh_only_config(monkeypatch)
    lock = H._token_cache_lock
    assert lock.acquire()
    run = ResearchRun(timeout_seconds=30)
    token_value = current_research.set(run)
    try:
        import unittest.mock as _mock

        with _mock.patch.object(ResearchRun, "check", side_effect=ResearchStopped("cancelled")):
            with pytest.raises(ResearchStopped):
                H._resolve_access_token("long-lived-refresh-token")
    finally:
        current_research.reset(token_value)
        lock.release()
    # 取消即退出：换取从未发生。
    assert H._token_cache == {}


def test_first_fetch_errmsg_redacts_newly_issued_token(monkeypatch, fresh_token_cache):
    """首次换取签发的新 access_token 被 supplier errmsg 回显时同样脱敏。"""
    strict_calendar(monkeypatch)
    refresh_only_config(monkeypatch)
    calls: List[dict] = []
    install_full_dispatch(
        monkeypatch, calls,
        data_responses=[
            (
                200,
                json_dumps(
                    synthetic_payload(errorcode=7, errmsg="invalid access_token auto-token-1")
                ).encode(),
            ),
        ],
    )
    fetcher = H.IfindHttpDailyFetcher()
    with pytest.raises(DataFetchError, match="supplier errorcode 7") as excinfo:
        fetcher._fetch_raw_data("600519.SH", "2026-11-04", "2026-11-06")
    message = str(excinfo.value)
    assert "auto-token-1" not in message  # 新签发 token 也在脱敏全集内（换取后取全集）
    assert "***" in message


def test_second_failure_after_refresh_keeps_real_reason(monkeypatch, fresh_token_cache):
    """刷新重试后的 500 保留真实原因，不伪报鉴权失败。"""
    strict_calendar(monkeypatch)
    refresh_only_config(monkeypatch)
    calls: List[dict] = []
    install_full_dispatch(
        monkeypatch, calls,
        data_responses=[(401, b"{}"), (500, b"{}")],
    )
    fetcher = H.IfindHttpDailyFetcher()
    with pytest.raises(DataFetchError, match="http status 500") as excinfo:
        fetcher._fetch_raw_data("600519.SH", "2026-11-04", "2026-11-06")
    assert "after token refresh" not in str(excinfo.value)



# ---------------------------------------------------------------------------
# 第九轮反例：解析路径零锁依赖 + token_used 精确脱敏
# ---------------------------------------------------------------------------
def test_parsing_survives_token_lock_held_elsewhere(monkeypatch, fresh_token_cache):
    """反例（第九轮）：另一任务在 _resolve_access_token 持锁执行换取网络时
    进入历史响应解析——解析与脱敏**零锁依赖**（原 _local_secrets 以
    ``with lock`` 遍历缓存，持锁换取期间解析段不可取消），取消/deadline 在
    解析段立即可判；脱敏仍覆盖 refresh_token 与本次实际使用的 access_token。
    """
    import threading as _threading

    # 另一任务持锁（模拟换取网络进行中，测试结束前不释放）。
    assert H._token_cache_lock.acquire()
    outcome: dict = {}

    def _parse_work():
        try:
            payload = synthetic_payload(errorcode=7, errmsg="echo used-now-token")
            secrets = H._redaction_secrets("long-lived-refresh-token", "used-now-token")
            H._parse_history_rows(
                payload, IDENTITY,
                access_token=secrets[0] if secrets else "",
                extra_secrets=secrets[1:],
            )
        except BaseException as exc:  # noqa: BLE001 — 反例捕获断言
            outcome["error"] = exc

    worker = _threading.Thread(target=_parse_work)
    worker.start()
    worker.join(timeout=2.0)
    assert not worker.is_alive(), "解析段在持锁期间挂起：解析路径仍等待 token 锁"
    assert isinstance(outcome.get("error"), DataFetchError)
    message = str(outcome["error"])
    assert "used-now-token" not in message and "***" in message
    assert "long-lived-refresh-token" not in message
    H._token_cache_lock.release()


def test_authorized_post_returns_used_token_after_refresh(monkeypatch, fresh_token_cache):
    """wrapper 返回本次实际使用的 token：刷新重试后为续用值（脱敏依据）。"""
    from datetime import date as _date_mod

    strict_calendar(monkeypatch)
    refresh_only_config(monkeypatch)
    calls: List[dict] = []
    install_full_dispatch(monkeypatch, calls, data_responses=[(401, b"{}")])
    payload, used = H._post_history_query_authorized(
        IDENTITY, _date_mod(2026, 11, 4), _date_mod(2026, 11, 6),
        cps="6", refresh_token="long-lived-refresh-token",
    )
    assert payload["errorcode"] == 0
    assert used == "auto-token-2"  # 401 后续用的新值（header 与脱敏一致）


def test_refreshed_token_echo_redacted_end_to_end(monkeypatch, fresh_token_cache):
    """端到端：刷新后的续用 token 被 supplier errmsg 回显时同样脱敏。"""
    strict_calendar(monkeypatch)
    refresh_only_config(monkeypatch)
    calls: List[dict] = []
    install_full_dispatch(
        monkeypatch, calls,
        data_responses=[
            (401, b"{}"),
            (
                200,
                json_dumps(synthetic_payload(errorcode=7, errmsg="invalid token auto-token-2")).encode(),
            ),
        ],
    )
    fetcher = H.IfindHttpDailyFetcher()
    with pytest.raises(DataFetchError, match="supplier errorcode 7") as excinfo:
        fetcher._fetch_raw_data("600519.SH", "2026-11-04", "2026-11-06")
    message = str(excinfo.value)
    assert "auto-token-2" not in message  # 续用值（token_used）在脱敏集合内
    assert "long-lived-refresh-token" not in message
    assert "***" in message


def test_transport_posts_official_contract(monkeypatch):
    """认证 header、官方端点与 CPS=6 参数体进入受控 curl 传输。"""
    captured: dict = {}

    def fake_curl_post(url, headers, body, *, label):
        captured.update({"url": url, "body": body, "headers": headers, "label": label})
        return 200, json_dumps(synthetic_payload()).encode()

    monkeypatch.setattr(H, "_http_api_curl_post", fake_curl_post)
    payload, token_used = H._post_history_query(
        IDENTITY, _d(2026, 11, 4), _d(2026, 11, 6),
        access_token="standard-token",
    )
    assert captured["url"] == "https://quantapi.51ifind.com/api/v1/cmd_history_quotation"
    assert captured["headers"]["access_token"] == "standard-token"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["label"] == "history"
    body = json.loads(captured["body"])
    assert body["codes"] == IDENTITY
    assert body["indicators"] == "open,high,low,close,volume,amount"
    assert body["functionpara"] == {"Interval": "D", "CPS": "6", "fill": "Omit"}
    assert payload["errorcode"] == 0
    assert token_used == "standard-token"


def test_transport_unadjusted_query_posts_official_cps_1(monkeypatch):
    """单位资格对照请求用官方取值表 CPS=1（不复权），同一端点与凭据。"""
    captured: dict = {}

    def fake_curl_post(url, headers, body, *, label):
        captured.update({"url": url, "body": body, "headers": headers})
        return 200, json_dumps(synthetic_payload(prices="raw")).encode()

    monkeypatch.setattr(H, "_http_api_curl_post", fake_curl_post)
    payload, _used = H._post_history_query(
        IDENTITY, _d(2026, 11, 4), _d(2026, 11, 6),
        access_token="standard-token", cps=H._UNADJUSTED_CPS,
    )
    assert captured["url"] == "https://quantapi.51ifind.com/api/v1/cmd_history_quotation"
    assert captured["headers"]["access_token"] == "standard-token"
    assert json.loads(captured["body"])["functionpara"]["CPS"] == "1"
    assert payload["errorcode"] == 0


def test_fetch_window_issues_dual_cps_requests(monkeypatch, fresh_token_cache):
    """一次窗口尝试两笔数据出站：先 CPS=6 取数、后 CPS=1 对照，同窗口同凭据。"""
    calls: List[dict] = []
    install_cps_dispatch(monkeypatch, calls)
    strict_calendar(monkeypatch)
    http_enabled_config(monkeypatch)
    fetcher = H.IfindHttpDailyFetcher()
    frame = fetcher._fetch_raw_data("600519.SH", "2026-11-04", "2026-11-06")
    assert len(frame) == 3
    token_calls = [c for c in calls if c["url"] == H._TOKEN_ENDPOINT]
    data_calls = [c for c in calls if c["url"] != H._TOKEN_ENDPOINT]
    assert len(token_calls) == 1  # 首次换取 access_token（凭据缓存在其后的窗口复用）
    assert len(data_calls) == 2
    first, second = data_calls
    assert json.loads(first["body"])["functionpara"]["CPS"] == "6"
    assert json.loads(second["body"])["functionpara"]["CPS"] == "1"
    for call in data_calls:
        request = json.loads(call["body"])
        assert request["codes"] == IDENTITY
        assert request["startdate"] == "2026-11-04"
        assert request["enddate"] == "2026-11-06"
        assert call["headers"]["access_token"] == "auto-token-1"


def test_fetch_window_skips_unadjusted_call_when_data_query_fails(monkeypatch, fresh_token_cache):
    """取数请求失败（供应商错误）即拒绝，不产生第二次出站。"""
    calls: List[dict] = []

    def fake_curl_post(url, headers, body, *, label):
        calls.append(url)
        return 200, json_dumps(synthetic_payload(errorcode=7, errmsg="no permission")).encode()

    monkeypatch.setattr(H, "_http_api_curl_post", fake_curl_post)
    strict_calendar(monkeypatch)
    http_enabled_config(monkeypatch)
    fetcher = H.IfindHttpDailyFetcher()
    with pytest.raises(DataFetchError, match="supplier errorcode 7"):
        fetcher._fetch_raw_data("600519.SH", "2026-11-04", "2026-11-06")
    assert len(calls) == 1


def test_transport_rejects_http_status(monkeypatch):
    monkeypatch.setattr(H, "_http_api_curl_post", lambda *a, **k: (500, b"server error"))
    with pytest.raises(DataFetchError, match="http status 500"):
        H._post_history_query(
            IDENTITY, _d(2026, 11, 4), _d(2026, 11, 6),
            access_token="standard-token",
        )


def test_transport_wraps_request_exception(monkeypatch):
    def boom(url, headers, body, *, label):
        raise DataFetchError(f"ifind http daily {label} transport failed: curl exit 7")

    monkeypatch.setattr(H, "_http_api_curl_post", boom)
    with pytest.raises(DataFetchError, match="transport failed: curl exit 7"):
        H._post_history_query(
            IDENTITY, _d(2026, 11, 4), _d(2026, 11, 6),
            access_token="standard-token",
        )


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_points_to_token_expiry(monkeypatch, status):
    """401/403 明确指向 token 过期（官方 7 天有效期），便于运维诊断。"""
    monkeypatch.setattr(H, "_http_api_curl_post", lambda *a, **k: (status, b"{}"))
    with pytest.raises(DataFetchError, match="access_token 可能已过期"):
        H._post_history_query(
            IDENTITY, _d(2026, 11, 4), _d(2026, 11, 6),
            access_token="standard-token",
        )


# ---------------------------------------------------------------------------
# Fetcher 端到端（受控传输 + 真实单位资格与窗口资格链）
# ---------------------------------------------------------------------------
def test_fetcher_returns_normalized_frame(monkeypatch):
    strict_calendar(monkeypatch)
    http_enabled_config(monkeypatch)
    calls: List[dict] = []
    install_cps_dispatch(monkeypatch, calls)
    fetcher = H.IfindHttpDailyFetcher()
    assert fetcher.available is True
    df, source = manager_style_call(fetcher)
    assert source == "IfindHttpDailyFetcher"
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume", "amount", "pct_chg"]
    assert df.iloc[-1]["date"] == "2026-11-06"


def manager_style_call(fetcher):
    """模拟管理器调用路径：_call_fetcher_method 的 _fetch_raw_data + _normalize_data。"""
    raw = fetcher._fetch_raw_data("600519.SH", "2026-11-04", "2026-11-06")
    return fetcher._normalize_data(raw, "600519.SH"), fetcher.name


def test_fetcher_unavailable_without_credentials(monkeypatch):
    strict_calendar(monkeypatch)
    enabled_config(monkeypatch, enabled=False)
    fetcher = H.IfindHttpDailyFetcher()
    assert fetcher.available is False
    assert fetcher.is_available_for_request("daily_data") is False


def test_fetcher_deadline_self_built(monkeypatch):
    """无外层 deadline 时自建有限绝对 deadline（含 fallback 预留）。"""
    from src.patches.eastmoney_patch import request_deadline

    http_enabled_config(monkeypatch)
    token = request_deadline.set(None)
    try:
        deadline = H.http_attempt_deadline(now=1000.0)
        assert deadline is not None
        # budget = 30 + 5，reserve = min(8, 35×0.3)=8 → self = 1000 + 27
        assert deadline == pytest.approx(1027.0)
    finally:
        request_deadline.reset(token)
