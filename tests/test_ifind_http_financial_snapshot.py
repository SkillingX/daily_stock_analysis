# -*- coding: utf-8 -*-
"""第三十一/三十二轮：财务快照标准 API 纵向闭环回归（真实入口，供应商替身）。

第三十二轮完整收敛后锁定：
- 主源多期语义（period=None）API 真实候选（最新已披露期探测，非直落 MCP）；
- 每字段按官方模板出参（roe 仅 TD、毛利率/同比/披露日仅 D、四指标 D+C）；
- 报表口径按字段证据（第二十轮）：仅 D+C 指标（C='3' 合并报表(调整)枚举）
  标注 consolidated/restated；d_only 指标（roe/毛利率/两同比）口径未证明
  → None（真实未知，不伪装 restated）→ fallback 按「口径待补」交 MCP 补缺
  （MCP 可用则真实口径拼入；不可用则消费层 unknown 资格路径如实降级）；
  period_basis annual/YTD 不变；
- 采纳前披露时点校验（None/晚于今日拒绝，未来披露期回退上一期）；
- API 部分缺失由 MCP 同期补缺（逐字段来源标注忠实，期不一致不拼）；
- 401/403 鉴权强刷重试一次（与日线 authorized 一致）；取消/双失败/关开关/
  资格外/脱敏与既有 MCP、日线行为不回归。
"""
from __future__ import annotations

import json

import pytest

from data_provider import ifind_http_daily_adapter as A
from data_provider.base import _qualified_ifind_balance
from data_provider.cross_source_validator import (
    AnchorReading,
    SourceReadError,
    _eligible_current_financial,
    normalize_report_period,
)
from tests.ifind_daily_testutil import http_enabled_config

CODE = "600519"
IDENTITY = "600519.SH"
PERIOD = "20260930"
DISCLOSED = "20261031"


def _real_deadline(seconds: float) -> float:
    """真实边界 deadline（绝对 monotonic + 余量）——预算 helper 按实际可用
    计算（第三十八轮），测试 deadline 必须落在未来才符合真实调用形态。"""
    return A.monotonic() + seconds


def _payload(values: dict | None = None, *, code: str = IDENTITY, disclosed=DISCLOSED) -> bytes:
    table: dict = {}
    for field, indicator, _unit, _form, _cal in A._FINANCE_FIELD_SPECS:
        value = values.get(field, 123.45) if values else 123.45
        table[indicator] = [value]
    table[A._FINANCE_DISCLOSED_AT_INDICATOR] = [disclosed]
    return json.dumps(
        {"errorcode": 0, "errmsg": "", "tables": [{"thscode": code, "table": table}]}
    ).encode("utf-8")


@pytest.fixture()
def finance_env(monkeypatch):
    # 默认 MCP 开启：兜底场景（API 失败→MCP）的既有契约语义；MCP 关闭类
    # 反例在用例内显式关闭。
    http_enabled_config(monkeypatch, enabled=True, mcp_enabled=True)
    monkeypatch.setattr(
        A, "_resolve_access_token",
        lambda refresh, *, force_refresh=False: (
            "tok_refreshed" if force_refresh else "tok_test_access"
        ),
    )


class _McpStub:
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


def test_api_full_snapshot_and_request_contract(finance_env, monkeypatch):
    """API 成功：每字段按官方模板出参（roe/gross 单参、revenue 两参）。"""
    calls: list[dict] = []

    def fake_curl(url, headers, body, *, label):
        calls.append({"url": url, "body": json.loads(body or b"{}")})
        return 200, _payload()

    monkeypatch.setattr(A, "_http_api_curl_post", fake_curl)
    readings = A.ifind_http_financial_snapshot(CODE, PERIOD, deadline=_real_deadline(10))
    body = calls[0]["body"]
    assert body["codes"] == IDENTITY
    params = {p["indicator"]: p["indiparams"] for p in body["indipara"]}
    assert params["ths_revenue_stock"] == [PERIOD, "3"]          # D+C
    assert params["ths_roe_wgt_publish_stock"] == [PERIOD]        # 仅 TD
    assert params["ths_gross_selling_rate_stock"] == [PERIOD]     # 仅 D
    assert params[A._FINANCE_DISCLOSED_AT_INDICATOR] == [PERIOD]  # 仅 D
    # 报表口径按字段证据（第二十轮）：D+C 指标有 C='3' 枚举证据
    # （consolidated/restated，采纳门合格）；d_only 指标口径未证明
    # （None 真实未知，采纳门如实拒——不伪装 restated）。
    for field, reading in readings.items():
        assert reading.source == "ifind"
        # 第三十三轮：source_family 由 post_init 从 source 自动推导 "ths"
        # （与 MCP 同供应商家族，交叉验证独立性不被误判）
        assert reading.source_family == "ths"
        assert reading.disclosed_at == "2026-10-31"
        if field in ("revenue", "net_profit", "assets", "liabilities"):
            assert reading.statement_scope == "consolidated"
            assert reading.adjustment == "restated"
            assert _eligible_current_financial(reading, field) is True
        else:
            assert reading.statement_scope is None, f"{field} d_only 口径未证明"
            assert reading.adjustment is None
            assert _eligible_current_financial(reading, field) is False, "未知口径不得伪装合格（待 MCP 补缺/如实降级）"


def test_bank_natural_missing_then_mcp_same_period_fill(finance_env, monkeypatch):
    """API 缺毛利率（自然缺项）→ MCP 同期补缺：逐字段来源标注忠实。"""
    from src.config import get_config
    get_config().enable_ifind_mcp = True  # 补缺路径需要 MCP 参与判定
    monkeypatch.setattr(A, "_http_api_curl_post", lambda *a, **k: (
        200, _payload(values={"gross_margin": None})))
    mcp = _McpStub([{
        "gross_margin": AnchorReading(
            "ifind", 91.0, period="2026-09-30", period_basis="YTD",
            statement_scope="consolidated", adjustment="restated",
        ),
        "revenue": AnchorReading(
            "ifind", 999.0, period="2026-09-30", period_basis="YTD",
            statement_scope="consolidated", adjustment="restated",
        ),
    }])
    mcp.install(monkeypatch)
    readings = A.financial_snapshot_with_fallback(
        CODE, PERIOD, deadline=_real_deadline(10),
        mcp_endpoint="e", mcp_token="t", mcp_timeout=5.0,
    )
    # 补缺字段与 API 字段同供应商家族 ths（不误判独立来源），
    # 且各自 source/source_family 保持真实渠道语义不互相覆盖
    assert readings["gross_margin"].source == "ifind"
    assert readings["revenue"].source == "ifind"
    assert readings["revenue"].value == 123.45


def test_specific_period_fill_requires_same_normalized_period(finance_env, monkeypatch):
    """第四十轮反例：具体期契约——指定 2026-09-30 时 MCP 候选 2025-12-31
    经归一化比较不符**拒绝拼入**（fixture 此前 MCP 默认关闭致补缺未执行而
    虚绿，现已真实执行）。"""
    from src.config import get_config
    get_config().enable_ifind_mcp = True
    monkeypatch.setattr(A, "_http_api_curl_post", lambda *a, **k: (
        200, _payload(values={"gross_margin": None})))
    mcp = _McpStub([{
        "gross_margin": AnchorReading("ifind", 91.0, period="2025-12-31"),
    }])
    mcp.install(monkeypatch)
    readings = A.financial_snapshot_with_fallback(
        CODE, PERIOD, deadline=_real_deadline(10),
        mcp_endpoint="e", mcp_token="t", mcp_timeout=5.0,
    )
    assert "gross_margin" not in readings  # 任意期不得填入指定财期
    assert readings["revenue"].period == "2026-09-30"


def test_specific_period_fill_same_period_accepted(finance_env, monkeypatch):
    """具体期补缺正例：MCP 候选与请求期归一化相同 → 拼入（真实补缺执行）。"""
    from src.config import get_config
    get_config().enable_ifind_mcp = True
    monkeypatch.setattr(A, "_http_api_curl_post", lambda *a, **k: (
        200, _payload(values={"gross_margin": None})))
    mcp = _McpStub([{
        "gross_margin": AnchorReading("ifind", 91.0, period="2026-09-30"),
    }])
    mcp.install(monkeypatch)
    readings = A.financial_snapshot_with_fallback(
        CODE, PERIOD, deadline=_real_deadline(10),
        mcp_endpoint="e", mcp_token="t", mcp_timeout=5.0,
    )
    assert readings["gross_margin"].value == 91.0  # 同期（归一化相等）拼入


def test_period_none_fill_keeps_own_period_label(finance_env, monkeypatch):
    """period=None 主源多期的补缺：protected 语义——MCP 候选携带自身财期
    真实标注补入（不同财期不混称同一期），消费层按字段自身期处理。"""
    from src.config import get_config
    get_config().enable_ifind_mcp = True
    monkeypatch.setattr(A, "_http_api_curl_post", lambda *a, **k: (
        200, _payload(values={"gross_margin": None})))
    mcp = _McpStub([{
        "gross_margin": AnchorReading("ifind", 91.0, period="2025-12-31"),
    }])
    mcp.install(monkeypatch)
    readings = A.financial_snapshot_with_fallback(
        CODE, None, deadline=_real_deadline(30),
        mcp_endpoint="e", mcp_token="t", mcp_timeout=5.0,
    )
    assert readings["gross_margin"].period == "2025-12-31"  # 自身财期真实标注
    assert readings["revenue"].period == "2026-09-30"


def test_period_none_latest_disclosed_probe(finance_env, monkeypatch):
    """主源多期语义（period=None）：API 最新已披露期探测（首期命中一次出站）。"""
    calls: list[dict] = []

    def fake_curl(url, headers, body, *, label):
        calls.append(json.loads(body or b"{}"))
        return 200, _payload()

    monkeypatch.setattr(A, "_http_api_curl_post", fake_curl)
    from src.config import get_config
    get_config().enable_ifind_mcp = False  # 探测语义不掺补缺（第二十轮）
    readings = A.financial_snapshot_with_fallback(
        CODE, None, deadline=_real_deadline(60.0),
        mcp_endpoint="e", mcp_token="t", mcp_timeout=5.0,
    )
    assert readings and readings["revenue"].period == "2026-09-30"
    assert len(calls) == 1  # 首个候选（20260930）披露日已过即命中
    assert calls[0]["indipara"][0]["indiparams"][0] == "20260930"
    # d_only 口径未证明：真实 None（不伪装 restated），由补缺语义用例覆盖。
    assert readings["roe"].statement_scope is None


def test_future_disclosure_falls_back_to_earlier_period(finance_env, monkeypatch):
    """披露日反例：首期披露日在未来 → 拒绝并回退上一期；采纳前按分析时点拒绝。"""
    payloads = {
        "20260930": _payload(disclosed="20270430"),  # 错误/未来披露日
        "20260630": _payload(disclosed="20260815"),
    }

    def fake_curl(url, headers, body, *, label):
        period = json.loads(body)["indipara"][0]["indiparams"][0]
        return 200, payloads[period]

    monkeypatch.setattr(A, "_http_api_curl_post", fake_curl)
    from src.config import get_config
    get_config().enable_ifind_mcp = False  # 披露日反例专注 API 探测（第二十轮）
    readings = A.financial_snapshot_with_fallback(
        CODE, None, deadline=_real_deadline(60.0),
        mcp_endpoint="e", mcp_token="t", mcp_timeout=5.0,
    )
    assert readings["revenue"].period == "2026-06-30"
    assert readings["revenue"].disclosed_at == "2026-08-15"


def test_missing_disclosure_date_rejected(finance_env, monkeypatch):
    """披露日 None（未披露期）：该期拒绝（供应商回退值不可信）。

    reason_code 用既有白名单值 ``invalid_response``（细分原因进日志）。
    """
    monkeypatch.setattr(
        A, "_http_api_curl_post", lambda *a, **k: (200, _payload(disclosed=None)))
    with pytest.raises(SourceReadError) as excinfo:
        A.ifind_http_financial_snapshot(CODE, PERIOD, deadline=_real_deadline(10))
    assert excinfo.value.reason_code == "invalid_response"


def test_api_failure_falls_back_to_mcp(finance_env, monkeypatch):
    """API 失败 → MCP 快照兜底（快照级）。"""
    monkeypatch.setattr(A, "_http_api_curl_post", lambda *a, **k: (_ for _ in ()).throw(
        A.DataFetchError("ifind http finance http status 500")))
    mcp = _McpStub([{"revenue": AnchorReading("ifind", 2.0)}])
    mcp.install(monkeypatch)
    readings = A.financial_snapshot_with_fallback(
        CODE, PERIOD, deadline=_real_deadline(10),
        mcp_endpoint="e", mcp_token="t", mcp_timeout=5.0,
    )
    assert readings["revenue"].value == 2.0


def test_both_fail_propagates(finance_env, monkeypatch):
    """双失败：MCP SourceReadError 传播（上层走 Choice 等兜底）。"""
    monkeypatch.setattr(A, "_http_api_curl_post", lambda *a, **k: (_ for _ in ()).throw(
        A.DataFetchError("ifind http finance http status 503")))
    mcp = _McpStub([SourceReadError("ifind", "timeout")])
    mcp.install(monkeypatch)
    with pytest.raises(SourceReadError):
        A.financial_snapshot_with_fallback(
            CODE, PERIOD, deadline=_real_deadline(10),
            mcp_endpoint="e", mcp_token="t", mcp_timeout=5.0,
        )


def test_disabled_switch_zero_api_egress(monkeypatch):
    """开关关闭：API 零出站、直接 MCP 原路径（既有语义不变）。"""
    http_enabled_config(monkeypatch, enabled=True, mcp_enabled=True)
    from src.config import get_config
    get_config().enable_ifind_http_api = False

    monkeypatch.setattr(A, "_http_api_curl_post", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("switch off")))
    mcp = _McpStub([{"revenue": AnchorReading("ifind", 3.0)}])
    mcp.install(monkeypatch)
    readings = A.financial_snapshot_with_fallback(
        CODE, PERIOD, deadline=_real_deadline(10),
        mcp_endpoint="e", mcp_token="t", mcp_timeout=5.0,
    )
    assert readings["revenue"].value == 3.0


def test_ineligible_identity_goes_straight_to_mcp(finance_env, monkeypatch):
    """资格外（指数显式身份）：API 不参与、直接 MCP。"""
    monkeypatch.setattr(A, "_http_api_curl_post", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("ineligible identity")))
    mcp = _McpStub([{"revenue": AnchorReading("ifind", 4.0)}])
    mcp.install(monkeypatch)
    readings = A.financial_snapshot_with_fallback(
        "000001.SH", PERIOD, deadline=_real_deadline(10),
        mcp_endpoint="e", mcp_token="t", mcp_timeout=5.0,
    )
    assert readings["revenue"].value == 4.0


def test_auth_401_forces_refresh_and_retries_once(finance_env, monkeypatch):
    """401 鉴权：强制续用后重试一次（与日线 authorized 模式一致）。"""
    attempts: list[int] = []

    def fake_curl(url, headers, body, *, label):
        attempts.append(headers.get("access_token"))
        if len(attempts) == 1:
            return 401, b""
        return 200, _payload()

    monkeypatch.setattr(A, "_http_api_curl_post", fake_curl)
    readings = A.ifind_http_financial_snapshot(CODE, PERIOD, deadline=_real_deadline(10))
    assert attempts == ["tok_test_access", "tok_refreshed"]
    assert readings["revenue"].source_family == "ths"


def test_supplier_error_message_redacted(finance_env, monkeypatch):
    """供应商 errmsg 携带凭据形态时脱敏后才进异常消息。"""
    leak_body = json.dumps({
        "errorcode": -1,
        "errmsg": "invalid token tok_test_access rejected",
        "tables": [],
    }).encode("utf-8")
    monkeypatch.setattr(A, "_http_api_curl_post", lambda *a, **k: (200, leak_body))
    with pytest.raises(Exception) as excinfo:
        A.ifind_http_financial_snapshot(CODE, PERIOD, deadline=_real_deadline(10))
    assert "tok_test_access" not in str(excinfo.value)


def test_cancellation_propagates(finance_env, monkeypatch):
    """用户取消（ResearchStopped）：不吞、不继续消耗 MCP 出站。"""
    from src.core.research_run import ResearchStopped

    monkeypatch.setattr(A, "_http_api_curl_post", lambda *a, **k: (_ for _ in ()).throw(
        ResearchStopped()))
    mcp = _McpStub([])
    mcp.install(monkeypatch)
    with pytest.raises(ResearchStopped):
        A.financial_snapshot_with_fallback(
            CODE, PERIOD, deadline=_real_deadline(10),
            mcp_endpoint="e", mcp_token="t", mcp_timeout=5.0,
        )
    assert mcp.calls == []


def test_wrong_security_rejected(finance_env, monkeypatch):
    """响应证券与请求不符：整快照拒绝（不混拼）。"""
    monkeypatch.setattr(A, "_http_api_curl_post", lambda *a, **k: (200, _payload(code="000001.SZ")))
    with pytest.raises(A.DataFetchError):
        A.ifind_http_financial_snapshot(CODE, PERIOD, deadline=_real_deadline(10))


def test_period_normalization_key_consistent():
    """财期比较键：API 期与 MCP 期同标准形（补缺同期判定基础）。"""
    assert normalize_report_period("20260930")[0] == "2026-09-30"
    assert A._normalized_period_key("2026-09-30") == "2026-09-30"


def test_cross_source_family_not_independent(finance_env, monkeypatch):
    """第三十三轮反例：API 与 MCP 读数同供应商家族 ths——同源不被误判为
    独立来源交叉验证（审查阻断项 1 的业务使用层反例）。"""
    monkeypatch.setattr(A, "_http_api_curl_post", lambda *a, **k: (200, _payload()))
    api_readings = A.ifind_http_financial_snapshot(CODE, PERIOD, deadline=_real_deadline(10))
    mcp_reading = AnchorReading("ifind", 130970743649.04, period="2026-09-30")
    # 同源同家族：家族一致（独立性判定 readings[index].source_family !=
    # primary.source_family 不成立）
    assert api_readings["revenue"].source_family == mcp_reading.source_family == "ths"
    assert api_readings["revenue"].source_family != "http_api"


def test_curl_timeout_falls_back_to_mcp(finance_env, monkeypatch):
    """第三十三轮反例：curl deadline 耗尽（TimeoutError）按既有单源失败契约
    回退 MCP（此前绕过回退直接上抛）。"""
    monkeypatch.setattr(A, "_http_api_curl_post", lambda *a, **k: (_ for _ in ()).throw(
        TimeoutError("ifind http finance curl max-time reached")))
    mcp = _McpStub([{"revenue": AnchorReading("ifind", 6.0)}])
    mcp.install(monkeypatch)
    passed_deadline = _real_deadline(10)
    readings = A.financial_snapshot_with_fallback(
        CODE, PERIOD, deadline=passed_deadline,
        mcp_endpoint="e", mcp_token="t", mcp_timeout=5.0,
    )
    assert readings["revenue"].value == 6.0
    # MCP 收到入口 deadline 原值（总上限不被延长/替换）
    assert mcp.calls == [(CODE, PERIOD, passed_deadline)]


def test_timeout_both_fail_propagates(finance_env, monkeypatch):
    """API TimeoutError + MCP timeout：按既有超时契约传播。"""
    monkeypatch.setattr(A, "_http_api_curl_post", lambda *a, **k: (_ for _ in ()).throw(
        TimeoutError("ifind http finance curl max-time reached")))
    mcp = _McpStub([SourceReadError("ifind", "timeout")])
    mcp.install(monkeypatch)
    with pytest.raises(SourceReadError):
        A.financial_snapshot_with_fallback(
            CODE, PERIOD, deadline=_real_deadline(10),
            mcp_endpoint="e", mcp_token="t", mcp_timeout=5.0,
        )


def test_fill_timeout_keeps_api_snapshot(finance_env, monkeypatch):
    """补缺 MCP 超时：不阻断已取得的 API 快照（缺失如实保留、预算不延长）。"""
    from src.config import get_config
    get_config().enable_ifind_mcp = True
    monkeypatch.setattr(A, "_http_api_curl_post", lambda *a, **k: (
        200, _payload(values={"gross_margin": None})))
    mcp = _McpStub([TimeoutError("ifind http finance curl max-time reached")])
    mcp.install(monkeypatch)
    readings = A.financial_snapshot_with_fallback(
        CODE, PERIOD, deadline=_real_deadline(10),
        mcp_endpoint="e", mcp_token="t", mcp_timeout=5.0,
    )
    assert "gross_margin" not in readings  # 缺失如实保留
    assert "revenue" in readings  # 已取得的 API 字段不被丢弃


def test_fallback_logs_do_not_expose_credentials(finance_env, monkeypatch, caplog):
    """审查附带项：fallback 警告日志与异常消息不暴露凭据。"""
    import logging

    # 上游未脱敏消息形态的防御深度：fallback 日志经 refresh token 二次脱敏。
    token = "long-lived-refresh-token"
    leak_msg = f"supplier rejected {token}"

    def fake_curl(url, headers, body, *, label):
        raise A.DataFetchError(f"ifind http finance {leak_msg}")

    monkeypatch.setattr(A, "_http_api_curl_post", fake_curl)
    mcp = _McpStub([{"revenue": AnchorReading("ifind", 7.0)}])
    mcp.install(monkeypatch)
    with caplog.at_level(logging.WARNING):
        readings = A.financial_snapshot_with_fallback(
            CODE, PERIOD, deadline=_real_deadline(10),
            mcp_endpoint="e", mcp_token="t", mcp_timeout=5.0,
        )
    assert readings["revenue"].value == 7.0
    for record in caplog.records:
        assert token not in record.getMessage()


def test_choice_report_date_format_normalized_on_wire(finance_env, monkeypatch):
    """第三十四轮：Choice 形态 report_date（YYYY-MM-DD）出站规范化为官方
    枚举形态（YYYYMMDD），reading.period 保留消费方原格式。"""
    calls: list[dict] = []

    def fake_curl(url, headers, body, *, label):
        calls.append(json.loads(body or b"{}"))
        return 200, _payload()

    monkeypatch.setattr(A, "_http_api_curl_post", fake_curl)
    readings = A.ifind_http_financial_snapshot(CODE, "2026-09-30", deadline=_real_deadline(10))
    params = {p["indicator"]: p["indiparams"] for p in calls[0]["indipara"]}
    assert params["ths_revenue_stock"] == ["20260930", "3"]  # 出站无横线
    assert readings["revenue"].period == "2026-09-30"  # 消费方格式保留
    assert readings["revenue"].disclosed_at == "2026-10-31"


def test_nan_placeholder_blocked_fill_then_mcp_restores(finance_env, monkeypatch):
    """第三十五轮反例：API NaN/±Inf 占位不得伪装成功、不得阻止 MCP 同期补回。

    消费门层验证：补回后的字段经既有采纳门（_eligible_current_financial）
    判定合格；合法负值（未盈利期归母）按字段契约保留。
    """
    from data_provider.cross_source_validator import _eligible_current_financial
    from src.config import get_config

    get_config().enable_ifind_mcp = True  # 补缺路径需要 MCP 参与判定
    values = {f: 123.45 for f, _i, _u, _fo, _c in A._FINANCE_FIELD_SPECS}
    values.update({"gross_margin": "nan", "net_profit": float("inf"), "net_profit_yoy": -33.2})
    monkeypatch.setattr(A, "_http_api_curl_post", lambda *a, **k: (200, _payload(values=values)))
    mcp = _McpStub([{
        "gross_margin": AnchorReading("ifind", 91.0, period="2026-09-30", period_basis="YTD",
                                      caliber="gross_margin", unit="percentage_point", currency="CNY",
                                      statement_scope="consolidated", adjustment="restated"),
        "net_profit": AnchorReading("ifind", 966.45, period="2026-09-30", period_basis="YTD",
                                    caliber="parent_net_profit", unit="currency_base", currency="CNY",
                                    statement_scope="consolidated", adjustment="restated"),
    }])
    mcp.install(monkeypatch)
    readings = A.financial_snapshot_with_fallback(
        CODE, PERIOD, deadline=_real_deadline(10),
        mcp_endpoint="e", mcp_token="t", mcp_timeout=5.0,
    )
    assert readings["gross_margin"].value == 91.0      # NaN 占位被 MCP 合格值补回
    assert readings["net_profit"].value == 966.45      # Inf 占位被补回
    assert readings["net_profit_yoy"].value == -33.2   # 合法负值按契约保留
    # 消费门：补回字段经既有采纳门合格
    assert _eligible_current_financial(readings["gross_margin"], "gross_margin") is True
    assert _eligible_current_financial(readings["net_profit"], "net_profit") is True


def test_fill_cancellation_propagates_not_swallowed(finance_env, monkeypatch):
    """第三十五轮反例：部分 API → MCP 补缺实际触发取消——ResearchStopped
    （TimeoutError 子类）不得被补缺的 except TimeoutError 吞成成功返回。"""
    from src.core.research_run import ResearchStopped
    from src.config import get_config

    get_config().enable_ifind_mcp = True
    monkeypatch.setattr(A, "_http_api_curl_post", lambda *a, **k: (
        200, _payload(values={"gross_margin": None})))  # 部分 API：触发补缺
    mcp = _McpStub([ResearchStopped()])  # 补缺 MCP 阶段用户取消
    mcp.install(monkeypatch)
    with pytest.raises(ResearchStopped):
        A.financial_snapshot_with_fallback(
            CODE, PERIOD, deadline=_real_deadline(10),
            mcp_endpoint="e", mcp_token="t", mcp_timeout=5.0,
        )


def test_canonical_period_annual_with_dash(finance_env, monkeypatch):
    """第三十六轮反例：带横线年报期（2025-12-31）出站规范化且期间基数正确
    判为 annual（此前原形态 endswith('1231') False 误标 YTD）。"""
    calls: list[dict] = []

    def fake_curl(url, headers, body, *, label):
        calls.append(json.loads(body or b"{}"))
        return 200, _payload(disclosed="20260425")

    monkeypatch.setattr(A, "_http_api_curl_post", fake_curl)
    readings = A.ifind_http_financial_snapshot(CODE, "2025-12-31", deadline=_real_deadline(10))
    params = {p["indicator"]: p["indiparams"] for p in calls[0]["indipara"]}
    assert params["ths_revenue_stock"] == ["20251231", "3"]  # 出站 canonical
    assert readings["revenue"].period == "2025-12-31"
    assert readings["revenue"].period_basis == "annual"      # 反例修复点
    assert readings["revenue"].disclosed_at == "2026-04-25"


def test_canonical_period_equivalence_with_and_without_dash(finance_env, monkeypatch):
    """同一期间带/不带横线：出站、读数、期间基数完全等价。"""
    seen: list[dict] = []

    def fake_curl(url, headers, body, *, label):
        seen.append(json.loads(body or b"{}"))
        return 200, _payload(disclosed="20261031")

    monkeypatch.setattr(A, "_http_api_curl_post", fake_curl)
    dashed = A.ifind_http_financial_snapshot(CODE, "2026-09-30", deadline=_real_deadline(10))
    plain = A.ifind_http_financial_snapshot(CODE, "20260930", deadline=_real_deadline(10))
    assert seen[0]["indipara"][0]["indiparams"] == seen[1]["indipara"][0]["indiparams"]
    for field in dashed:
        assert dashed[field].period == plain[field].period
        assert dashed[field].period_basis == plain[field].period_basis
        assert dashed[field].disclosed_at == plain[field].disclosed_at
        assert dashed[field].value == plain[field].value


def test_illegal_periods_rejected_not_passed_through(finance_env, monkeypatch):
    """非法期（错误日历/非法形态）显式拒绝出站（invalid_configuration），
    fallback 语义下回退 MCP 原路径，不静默透传依赖服务端。"""
    monkeypatch.setattr(A, "_http_api_curl_post", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("illegal period must not reach transport")))
    for bad in ["2026-13-40", "abc", "202613", "2026-9-30"]:
        with pytest.raises(SourceReadError) as excinfo:
            A.ifind_http_financial_snapshot(CODE, bad, deadline=_real_deadline(10))
        assert excinfo.value.reason_code == "invalid_configuration"


def test_quarter_candidates_cross_year_boundary():
    """跨年边界：候选按「当前所在季度不计，已完成季度往回」回退。

    12-31（Q4 当天结束，实现从 Q3 起——Q4 年报不可能当日披露）；
    1 月起 Q4 已完成，候选从 Q4 起（未披露由采纳前披露校验自然回退）。
    """
    from datetime import date as _d
    assert A._quarter_end_candidates(_d(2026, 12, 31))[:3] == ["20260930", "20260630", "20260331"]
    assert A._quarter_end_candidates(_d(2027, 1, 15))[:3] == ["20261231", "20260930", "20260630"]
    assert A._quarter_end_candidates(_d(2027, 2, 1))[3] == "20260331"
    # 期形态统一 canonical（YYYYMMDD），跨年无横线形态混入
    assert all(len(c) == 8 and c.isdigit() for c in A._quarter_end_candidates(_d(2027, 2, 1)))


def test_disclosure_before_report_period_rejected(finance_env, monkeypatch):
    """第三十七轮反例：披露日早于/等于所请求报告期（供应商回退/错乱形态）
    按既有 Choice 契约（disclosure > report_date）拒绝，不按请求期冒充财期。"""
    for disclosed in ["20240430", "20251231"]:  # 早于 / 等于 20251231 报告期
        def fake_curl(*a, _d=disclosed, **k):
            return 200, _payload(disclosed=_d)
        monkeypatch.setattr(A, "_http_api_curl_post", fake_curl)
        with pytest.raises(SourceReadError) as excinfo:
            A.ifind_http_financial_snapshot(CODE, "20251231", deadline=_real_deadline(10))
        assert excinfo.value.reason_code == "invalid_response"


def test_probe_budget_reserve_leaves_mcp_positive_time(finance_env, monkeypatch):
    """第三十七轮反例（可控时钟）：多期探测在预算线停止，MCP 收到正剩余
    deadline 且真实切换（此前 mock 不检查时间无法证明兜底可用）。"""
    from src.config import get_config
    get_config().enable_ifind_mcp = True
    real_monotonic = A.monotonic
    clock = {"now": real_monotonic() + 1000.0}  # 可控时钟（绝对值远大于测试 deadline）

    def fake_monotonic():
        return clock["now"]

    monkeypatch.setattr(A, "monotonic", fake_monotonic)

    def fake_curl(url, headers, body, *, label):
        clock["now"] += 5.0  # 每期探测消耗 5 秒
        return 200, _payload(disclosed=None)  # 全部未披露 → 逐期拒绝

    monkeypatch.setattr(A, "_http_api_curl_post", fake_curl)

    deadline = clock["now"] + 20.0  # 总预算 20s：budget=30+解析余量，reserve=min(8, 30%·budget)
    mcp_deadlines: list[float] = []

    def fake_mcp(self_fetcher, code, period, *, deadline):
        mcp_deadlines.append(deadline)
        return {"revenue": AnchorReading("ifind", 8.0)}

    monkeypatch.setattr(
        "data_provider.ifind_fundamental_adapter.IfindFetcher.financial_snapshot", fake_mcp)

    readings = A.financial_snapshot_with_fallback(
        CODE, None, deadline=deadline,
        mcp_endpoint="e", mcp_token="t", mcp_timeout=5.0,
    )
    assert readings["revenue"].value == 8.0
    # 探测停止后 MCP 收到的 deadline 相对可控时钟仍有正剩余（兜底可用）
    assert mcp_deadlines and mcp_deadlines[0] > fake_monotonic()
    # 且探测消耗没有吃满总预算（预留份额生效）
    assert deadline - mcp_deadlines[0] < 20.0


def test_short_deadline_reserve_keeps_mcp_positive_time(finance_env, monkeypatch):
    """第三十八轮反例：外层仅余 10s（cap=30）——预算按实际可用计算，
    HTTP 慢耗尽 API 份额后 MCP 仍有正剩余（此前 reserve 按 cap 被截断、
    HTTP 用满后 MCP 剩 0）。"""
    real_monotonic = A.monotonic
    clock = {"now": real_monotonic() + 1000.0}

    def fake_monotonic():
        return clock["now"]

    monkeypatch.setattr(A, "monotonic", fake_monotonic)

    def slow_curl(url, headers, body, *, label):
        clock["now"] += 7.0  # 首期慢耗尽 API 份额（7s of 7s）
        return 200, _payload(disclosed=None)

    monkeypatch.setattr(A, "_http_api_curl_post", slow_curl)
    mcp_deadlines: list[float] = []

    def fake_mcp(self_fetcher, code, period, *, deadline):
        mcp_deadlines.append(deadline)
        return {"revenue": AnchorReading("ifind", 9.0)}

    monkeypatch.setattr(
        "data_provider.ifind_fundamental_adapter.IfindFetcher.financial_snapshot", fake_mcp)

    deadline = clock["now"] + 10.0  # 外层仅余 10 秒
    readings = A.financial_snapshot_with_fallback(
        CODE, PERIOD, deadline=deadline,
        mcp_endpoint="e", mcp_token="t", mcp_timeout=5.0,
    )
    assert readings["revenue"].value == 9.0
    remaining = mcp_deadlines[0] - fake_monotonic()
    assert remaining > 0                       # MCP 有正剩余（此前为 0）
    assert remaining >= 2.0                    # reserve = min(8, budget30%) ≥ 2
    assert mcp_deadlines[0] <= deadline        # 总 deadline 语义保持


def test_bool_value_rejected_and_mcp_fills(finance_env, monkeypatch):
    """第三十八轮反例：JSON true/false 不是财务数字（float(True)=1.0 误当
    合格并阻止补缺）——显式拒绝后同财期 MCP 补回，经消费门合格。"""
    from data_provider.cross_source_validator import _eligible_current_financial
    from src.config import get_config
    get_config().enable_ifind_mcp = True

    values = {f: 123.45 for f, _i, _u, _fo, _c in A._FINANCE_FIELD_SPECS}
    values.update({"gross_margin": True, "revenue_yoy": False})
    monkeypatch.setattr(A, "_http_api_curl_post", lambda *a, **k: (200, _payload(values=values)))
    mcp = _McpStub([{
        "gross_margin": AnchorReading("ifind", 30.5, period="2026-09-30", period_basis="YTD",
                                      caliber="gross_margin", unit="percentage_point", currency="CNY",
                                      statement_scope="consolidated", adjustment="restated"),
        "revenue_yoy": AnchorReading("ifind", 12.3, period="2026-09-30", period_basis="YTD",
                                     caliber="operating_revenue_yoy", unit="percentage_point", currency="CNY",
                                     statement_scope="consolidated", adjustment="restated"),
    }])
    mcp.install(monkeypatch)
    readings = A.financial_snapshot_with_fallback(
        CODE, PERIOD, deadline=_real_deadline(10),
        mcp_endpoint="e", mcp_token="t", mcp_timeout=5.0,
    )
    assert readings["gross_margin"].value == 30.5   # bool 占位被 MCP 合格值补回
    assert readings["revenue_yoy"].value == 12.3
    assert _eligible_current_financial(readings["gross_margin"], "gross_margin") is True
    assert _eligible_current_financial(readings["revenue_yoy"], "revenue_yoy") is True


def test_probe_transport_failure_keeps_merged_fields(finance_env, monkeypatch):
    """第四十轮反例：首期已合并合格字段、后续期传输失败——保留已累积事实
    返回（不整体丢弃），由调用方按剩余预算安全补缺/披露。"""
    calls: list[dict] = []

    def payload_for(values):
        table = {}
        for field, indicator, _u, _fo, _cal in A._FINANCE_FIELD_SPECS:
            table[indicator] = [values.get(field, None)]
        table[A._FINANCE_DISCLOSED_AT_INDICATOR] = ["20261031"]
        return json.dumps({"errorcode": 0, "errmsg": "",
                           "tables": [{"thscode": IDENTITY, "table": table}]}).encode()

    # 首期核心不齐（仅资产/负债）→ 探测继续；第二期传输失败 → 保留 merged
    payloads = [
        payload_for({"assets": 100.0, "liabilities": 40.0}),
        None,  # 第二期传输失败
    ]

    def fake_curl(url, headers, body, *, label):
        calls.append(json.loads(body or b"{}"))
        entry = payloads.pop(0)
        if entry is None:
            raise A.DataFetchError("ifind http finance http status 503")
        return 200, entry

    monkeypatch.setattr(A, "_http_api_curl_post", fake_curl)
    readings = A.ifind_http_financial_snapshot_latest(CODE, deadline=_real_deadline(60.0))
    # 首期已合并的合格字段保留（不因后续期传输失败整体丢弃）
    assert readings["assets"].period == "2026-09-30"
    assert "revenue" not in readings  # 后续期未取得（由调用方补缺/披露）
    assert len(calls) == 2  # 失败后停止探测


def test_probe_cancellation_still_propagates_with_partial_merge(finance_env, monkeypatch):
    """部分合并后的用户取消仍穿透（不吞、不伪装为部分成功返回）。"""
    from src.core.research_run import ResearchStopped

    payloads = [
        json.dumps({"errorcode": 0, "errmsg": "", "tables": [{"thscode": IDENTITY, "table": {
            A._FINANCE_FIELD_SPECS[0][1]: [123.45],
            A._FINANCE_DISCLOSED_AT_INDICATOR: ["20261031"]}}]}).encode(),
        None,
    ]

    def fake_curl(url, headers, body, *, label):
        entry = payloads.pop(0)
        if entry is None:
            raise ResearchStopped()  # 第二期用户取消
        return 200, entry

    monkeypatch.setattr(A, "_http_api_curl_post", fake_curl)
    with pytest.raises(ResearchStopped):
        A.ifind_http_financial_snapshot_latest(CODE, deadline=_real_deadline(60.0))


def test_latest_field_level_merge_single_side_real_period(finance_env, monkeypatch):
    """第四十五轮（逐字段最新承诺恢复）：新季损益齐+单侧 assets——探测继续
    合并 liabilities（Q2 真实期标注），下游装配按同主期契约裁决。"""
    def payload_for(values, disclosed):
        table = {}
        for field, indicator, _u, _fo, _cal in A._FINANCE_FIELD_SPECS:
            table[indicator] = [values.get(field, None)]
        table[A._FINANCE_DISCLOSED_AT_INDICATOR] = [disclosed]
        return json.dumps({"errorcode": 0, "errmsg": "",
                           "tables": [{"thscode": IDENTITY, "table": table}]}).encode()

    payloads = {
        "20260930": payload_for({"assets": 100.0, "revenue": 575.1, "net_profit": 92.6,
                                 "roe": 9.0, "gross_margin": None, "revenue_yoy": 15.0,
                                 "net_profit_yoy": 36.1}, "20261031"),
        "20260630": payload_for({"assets": 98.0, "liabilities": 39.0, "revenue": 500.0,
                                 "net_profit": 80.0, "roe": 8.5, "gross_margin": 25.0,
                                 "revenue_yoy": 12.0, "net_profit_yoy": 30.0}, "20260825"),
    }

    def fake_curl(url, headers, body, *, label):
        period = json.loads(body)["indipara"][0]["indiparams"][0]
        return 200, payloads[period]

    calls: list[str] = []

    def counting_curl(url, headers, body, *, label):
        calls.append(json.loads(body)["indipara"][0]["indiparams"][0])
        return fake_curl(url, headers, body, label=label)

    monkeypatch.setattr(A, "_http_api_curl_post", counting_curl)
    readings = A.ifind_http_financial_snapshot_latest(CODE, deadline=_real_deadline(60.0))
    # 逐字段合并：损益/assets=Q3、liabilities/gross_margin=Q2（各自有值最新期，
    # 期真实标注不混称同一期）
    assert readings["revenue"].period == "2026-09-30"
    assert readings["net_profit"].period == "2026-09-30"
    assert readings["assets"].period == "2026-09-30"
    assert readings["liabilities"].period == "2026-06-30"
    assert readings["gross_margin"].period == "2026-06-30"
    assert calls == ["20260930", "20260630"]  # 逐字段探测两期
    # 采纳门按字段口径证据（第二十轮）：D+C 指标合格；d_only 指标口径
    # 未证明（None）→ 如实拒（本入口为 API-only 探测，补缺语义由
    # fallback 用例覆盖）。
    for field, reading in readings.items():
        expected = field in ("revenue", "net_profit", "assets", "liabilities")
        assert _eligible_current_financial(reading, field) is expected


def test_balance_pair_rejected_by_manager_same_period_rule(finance_env, monkeypatch):
    """第四十四轮 manager/装配层反例：Q3 损益主期下，Q3 单侧 assets 无
    liabilities → ``_qualified_ifind_balance`` 拒绝（不得作为 Q3 当前资产
    负债），缺口如实（依赖既有 MCP/Choice 同主期兜底与披露）。"""
    def payload_for(values: dict, disclosed: str) -> bytes:
        table = {}
        for field, indicator, _u, _fo, _cal in A._FINANCE_FIELD_SPECS:
            table[indicator] = [values.get(field, None)]
        table[A._FINANCE_DISCLOSED_AT_INDICATOR] = [disclosed]
        return json.dumps({"errorcode": 0, "errmsg": "",
                           "tables": [{"thscode": IDENTITY, "table": table}]}).encode()

    payloads = {
        "20260930": payload_for({"assets": 100.0, "revenue": 575.1, "net_profit": 92.6,
                                 "roe": 9.0, "gross_margin": None, "revenue_yoy": 15.0,
                                 "net_profit_yoy": 36.1}, "20261031"),
        "20260630": payload_for({"assets": 98.0, "liabilities": 39.0, "revenue": 500.0,
                                 "net_profit": 80.0, "roe": 8.5, "gross_margin": 25.0,
                                 "revenue_yoy": 12.0, "net_profit_yoy": 30.0}, "20260825"),
    }

    def fake_curl(url, headers, body, *, label):
        period = json.loads(body)["indipara"][0]["indiparams"][0]
        return 200, payloads[period]

    monkeypatch.setattr(A, "_http_api_curl_post", fake_curl)
    readings = A.ifind_http_financial_snapshot_latest(CODE, deadline=_real_deadline(60.0))
    # 字段级合并：损益/assets=Q3、liabilities=Q2（各自真实期标注）
    assert readings["revenue"].period == "2026-09-30"
    assert readings["assets"].period == "2026-09-30"
    assert readings["liabilities"].period == "2026-06-30"
    balance_readings = {f: readings[f] for f in ("assets", "liabilities") if f in readings}
    # manager 主期（损益 Q3）下装配：Q2 liabilities 与主期不同 → 装配拒绝
    # （缺口如实披露，同主期兜底契约不变——读数保留真实期标注供诊断）
    assert _qualified_ifind_balance(balance_readings, "2026-09-30") is None
    # 旧期 Q2 配对同样不得作为 Q3 当前资产负债（非同主期）
    q2_pair = {
        "assets": AnchorReading("ifind", 98.0, period="2026-06-30", period_basis="YTD",
                                caliber="balance_sheet_total", unit="currency_base", currency="CNY",
                                disclosed_at="2026-08-25", statement_scope="consolidated",
                                adjustment="restated"),
        "liabilities": AnchorReading("ifind", 39.0, period="2026-06-30", period_basis="YTD",
                                     caliber="balance_sheet_total", unit="currency_base", currency="CNY",
                                     disclosed_at="2026-08-25", statement_scope="consolidated",
                                     adjustment="restated"),
    }
    assert _qualified_ifind_balance(q2_pair, "2026-09-30") is None
    # 正例：同主期完整配对 → 装配成功（issue #71 契约天然形态）
    q3_pair = {
        f: AnchorReading("ifind", v, period="2026-09-30", period_basis="YTD",
                         caliber="balance_sheet_total", unit="currency_base", currency="CNY",
                         disclosed_at="2026-10-31", statement_scope="consolidated",
                         adjustment="restated")
        for f, v in (("assets", 100.0), ("liabilities", 40.0))
    }
    assembled = _qualified_ifind_balance(q3_pair, "2026-09-30")
    assert assembled is not None and assembled["period"] == "2026-09-30"


def test_disabled_switch_keeps_mcp_and_choice_semantics(monkeypatch):
    """API 关闭：MCP/Choice 既有语义不变（manager 装配与快照选择零改动）。"""
    http_enabled_config(monkeypatch, enabled=True, mcp_enabled=True)
    from src.config import get_config
    get_config().enable_ifind_http_api = False
    monkeypatch.setattr(A, "_http_api_curl_post", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("switch off")))
    mcp = _McpStub([{"revenue": AnchorReading("ifind", 3.0)}])
    mcp.install(monkeypatch)
    readings = A.financial_snapshot_with_fallback(
        CODE, None, deadline=_real_deadline(30),
        mcp_endpoint="e", mcp_token="t", mcp_timeout=5.0,
    )
    assert readings["revenue"].value == 3.0
    # 装配层对关闭态语义不变：同期契约与来源判定与 API 无关
    assert _qualified_ifind_balance({}, "2026-09-30") is None


def test_mcp_unavailable_preserves_api_failure_reasons(finance_env, monkeypatch, caplog):
    """第四十七轮（合并收敛）：MCP 关闭时按 API 实际失败类别上抛白名单 code
    （超时→timeout / HTTP500→business_error / 401→authentication_failed /
    资格→permission_denied），诊断文本进 warning 且凭据脱敏，不伪装成功。"""
    import logging as _logging

    from src.config import get_config

    # MCP 不可用场景：显式关闭（fixture 默认开启，第三十八轮）
    cfg = get_config()
    monkeypatch.setattr(cfg, "enable_ifind_mcp", False)

    # 超时 → timeout
    monkeypatch.setattr(A, "_http_api_curl_post", lambda *a, **k: (_ for _ in ()).throw(
        TimeoutError("ifind http finance curl max-time reached")))
    with pytest.raises(SourceReadError) as excinfo:
        A.financial_snapshot_with_fallback(
            CODE, PERIOD, deadline=A.monotonic() + 10,
            mcp_endpoint="https://fixture.invalid/mcp", mcp_token="t", mcp_timeout=5.0)
    assert excinfo.value.reason_code == "timeout"

    # HTTP 500 → http_500（传输层具体状态语义，第四十九轮）
    monkeypatch.setattr(A, "_http_api_curl_post", lambda *a, **k: (_ for _ in ()).throw(
        A.DataFetchError("ifind http finance http status 500")))
    with pytest.raises(SourceReadError) as excinfo:
        A.financial_snapshot_with_fallback(
            CODE, PERIOD, deadline=A.monotonic() + 10,
            mcp_endpoint="https://fixture.invalid/mcp", mcp_token="t", mcp_timeout=5.0)
    assert excinfo.value.reason_code == "http_500"

    # 401 → authentication_failed
    monkeypatch.setattr(A, "_http_api_curl_post", lambda *a, **k: (_ for _ in ()).throw(
        A._HttpAuthError("ifind http finance authentication failed (http 401)")))
    with pytest.raises(SourceReadError) as excinfo:
        A.financial_snapshot_with_fallback(
            CODE, PERIOD, deadline=A.monotonic() + 10,
            mcp_endpoint="https://fixture.invalid/mcp", mcp_token="t", mcp_timeout=5.0)
    assert excinfo.value.reason_code == "authentication_failed"

    # 资格外 → permission_denied（语义等价映射），诊断细节在 warning 日志
    with pytest.raises(SourceReadError) as excinfo:
        A.financial_snapshot_with_fallback(
            "000001.SH", PERIOD, deadline=A.monotonic() + 10,
            mcp_endpoint="e", mcp_token="t", mcp_timeout=5.0)
    assert excinfo.value.reason_code == "permission_denied"

    # 凭据缺失 → invalid_configuration（语义等价映射）
    monkeypatch.setattr(cfg, "ifind_http_refresh_token", None)
    with pytest.raises(SourceReadError) as excinfo:
        A.financial_snapshot_with_fallback(
            CODE, PERIOD, deadline=A.monotonic() + 10,
            mcp_endpoint="e", mcp_token="t", mcp_timeout=5.0)
    assert excinfo.value.reason_code == "invalid_configuration"

    # 诊断 warning 不暴露凭据（refresh token 形态）
    monkeypatch.setattr(cfg, "ifind_http_refresh_token", "tok_test_access")
    caplog_records: list[str] = []

    def leaking_curl(url, headers, body, *, label):
        raise A.DataFetchError(f"supplier rejected tok_test_access")

    monkeypatch.setattr(A, "_http_api_curl_post", leaking_curl)
    monkeypatch.setattr(A, "_resolve_access_token",
                        lambda r, *, force_refresh=False: "tok_test_access")
    with caplog.at_level(_logging.WARNING):
        with pytest.raises(SourceReadError):
            A.financial_snapshot_with_fallback(
                CODE, PERIOD, deadline=A.monotonic() + 10,
                mcp_endpoint="e", mcp_token="t", mcp_timeout=5.0)
    for record in caplog.records:
        assert "tok_test_access" not in record.getMessage()


def test_d_only_unknown_caliber_filled_by_mcp_when_available(finance_env, monkeypatch):
    """第二十轮反例（MCP 可用）：d_only 四指标口径未证明（None）→ fallback
    按「口径待补」交 MCP——MCP 同期候选携带真实合格口径拼入（期一致），
    补入后字段口径为 MCP 真实值（不虚构）；D+C 指标保持 API 证据口径。"""
    from src.config import get_config
    get_config().enable_ifind_mcp = True
    monkeypatch.setattr(A, "_http_api_curl_post", lambda *a, **k: (200, _payload()))
    mcp_candidate = {}
    for field in ("roe", "gross_margin", "revenue_yoy", "net_profit_yoy"):
        mcp_candidate[field] = AnchorReading(
            "ifind", 77.0, period="2026-09-30", period_basis="YTD",
            unit="percentage_point", statement_scope="consolidated",
            adjustment="restated")
    mcp = _McpStub([mcp_candidate])
    mcp.install(monkeypatch)
    readings = A.financial_snapshot_with_fallback(
        CODE, PERIOD, deadline=_real_deadline(30.0),
        mcp_endpoint="e", mcp_token="t", mcp_timeout=5.0,
    )
    assert mcp.calls, "口径待补字段必须尝试 MCP 补缺"
    for field in ("roe", "gross_margin", "revenue_yoy", "net_profit_yoy"):
        assert readings[field].value == 77.0
        assert readings[field].statement_scope == "consolidated"
        assert readings[field].adjustment == "restated", "补入口径为 MCP 真实值"
    for field in ("revenue", "net_profit", "assets", "liabilities"):
        assert readings[field].value == 123.45, "D+C 指标保持 API 值（有证据口径不待补）"


def test_d_only_unknown_caliber_kept_unknown_when_mcp_unavailable(finance_env, monkeypatch):
    """第二十轮反例（MCP 不可用）：d_only 四指标口径未知且 MCP 关闭——
    API 值保留在快照、口径如实 None（不伪装 restated 不拦快照），采纳门
    如实拒（消费层既有 unknown 资格路径降级）。"""
    from src.config import get_config
    from data_provider.cross_source_validator import _eligible_current_financial
    get_config().enable_ifind_mcp = False
    monkeypatch.setattr(A, "_http_api_curl_post", lambda *a, **k: (200, _payload()))
    readings = A.financial_snapshot_with_fallback(
        CODE, PERIOD, deadline=_real_deadline(30.0),
        mcp_endpoint="e", mcp_token="t", mcp_timeout=5.0,
    )
    for field in ("roe", "gross_margin", "revenue_yoy", "net_profit_yoy"):
        assert readings[field].value == 123.45, "值保留在快照（不拦 fallback）"
        assert readings[field].statement_scope is None
        assert readings[field].adjustment is None
        assert _eligible_current_financial(readings[field], field) is False, "未知口径不得伪装合格（如实降级）"
    for field in ("revenue", "net_profit", "assets", "liabilities"):
        assert _eligible_current_financial(readings[field], field) is True


def test_ineligible_mcp_candidate_no_evidence_loss(finance_env, monkeypatch):
    """无资格 MCP 候选（口径同样未知）拼入不丢证据：值保留可追溯、期真实
    标注（多期形态自身期）、最终按既有资格拒——不伪装、不丢 API 证据链。"""
    from src.config import get_config
    from data_provider.cross_source_validator import AnchorReading, _eligible_current_financial
    from data_provider.ifind_fundamental_adapter import IfindFetcher
    get_config().enable_ifind_mcp = True
    monkeypatch.setattr(A, "_http_api_curl_post", lambda *a, **k: (200, _payload()))

    def mcp_ineligible(self, code, period, *, deadline):
        return {"roe": AnchorReading("ifind", 55.0, period="2025-12-31",  # 自身真实期
                                     unit="percentage_point", statement_scope=None, adjustment=None)}

    monkeypatch.setattr(IfindFetcher, "financial_snapshot", mcp_ineligible)
    readings = A.financial_snapshot_with_fallback(
        CODE, None, deadline=_real_deadline(30.0),  # 主源多期：候选带自身期补入
        mcp_endpoint="e", mcp_token="t", mcp_timeout=5.0,
    )
    assert readings["roe"].value == 55.0 and readings["roe"].period == "2025-12-31"
    assert readings["roe"].normalization_error is None, "拼入候选不带失败标注（补缺成功）"
    assert _eligible_current_financial(readings["roe"], "roe") is False, "无资格候选最终如实拒"
    for field in ("revenue", "net_profit", "assets", "liabilities"):
        assert readings[field].value == 123.45, "D+C API 证据不受影响"
