# -*- coding: utf-8 -*-
"""板块申万市场数据链（issue #99）模块缝测试。

覆盖 `src/services/sector_sw_market.py` 的公共缝：
- 申万 2021 一级目录完整性（31 代码+名称、唯一性、版本与来源可追溯）
- 主行业归属解析（iFinD 标准 HTTP 按层级单独查询、目录唯一匹配、
  不近似择首、失败/未配置降级为不可确认）
- 一级指数快照 wire 解析与同日合格行筛选（涨跌幅与最新价/昨收交叉复核）
- 指数日线 wire 解析（cmd_history_quotation 多代码）
- 共享传输复用边界（认证缓存/有界传输按既有语义；板块入口不依赖
  个股日线开关）

供应商边界（HTTP 传输与认证）全部 mock；目录匹配、日期合格性、
交叉复核等本地逻辑不 mock。响应形状取自 2026-11-18 真实脱敏样本
（见 .claude/reviews/ifind-sector-api-live-validation-20261118.md）。
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import pytest

from src.services import sector_sw_market as sw


# ---------------------------------------------------------------------------
# 申万 2021 一级目录（核定配置数据）
# ---------------------------------------------------------------------------


class TestSwL1Directory:
    def test_exactly_31_entries_with_unique_code_and_name(self):
        assert len(sw.SW_L1_DIRECTORY) == 31
        codes = [c for c, _ in sw.SW_L1_DIRECTORY]
        names = [n for _, n in sw.SW_L1_DIRECTORY]
        assert len(set(codes)) == 31
        assert len(set(names)) == 31

    def test_directory_codes_are_sl_index_identities(self):
        for code, _name in sw.SW_L1_DIRECTORY:
            assert code.endswith(".SL")
            assert code[:6].isdigit()

    def test_known_directory_samples_from_live_validation(self):
        """2026-11-18 实测核定的样本对照（live-validation 报告表）。"""
        directory = dict(sw.SW_L1_DIRECTORY)
        assert directory["801950.SL"] == "煤炭"
        assert directory["801120.SL"] == "食品饮料"
        assert directory["801890.SL"] == "机械设备"
        assert directory["801080.SL"] == "电子"
        assert directory["801010.SL"] == "农林牧渔"
        assert directory["801980.SL"] == "美容护理"

    def test_version_and_source_recorded(self):
        assert sw.SW_L1_DIRECTORY_VERSION
        assert "2021" in sw.SW_L1_DIRECTORY_VERSION
        assert sw.SW_L1_DIRECTORY_SOURCE
        assert "申万" in sw.SW_L1_DIRECTORY_SOURCE


# ---------------------------------------------------------------------------
# 配置门控（板块入口独立判断：只看凭据与单次超时，不看日线开关）
# ---------------------------------------------------------------------------


class TestSwHttpConfig:
    def test_missing_token_means_no_config(self, monkeypatch):
        from src.config import get_config

        monkeypatch.setattr(get_config(), "ifind_http_refresh_token", None)
        assert sw.sw_http_config() is None

    def test_blank_token_means_no_config(self, monkeypatch):
        from src.config import get_config

        monkeypatch.setattr(get_config(), "ifind_http_refresh_token", "   ")
        assert sw.sw_http_config() is None

    def test_configured_token_and_timeout(self, monkeypatch):
        from src.config import get_config

        monkeypatch.setattr(get_config(), "ifind_http_refresh_token", "secret-token")
        monkeypatch.setattr(get_config(), "ifind_http_timeout_seconds", 12.0)
        cfg = sw.sw_http_config()
        assert cfg is not None
        token, timeout = cfg
        assert token == "secret-token"
        assert timeout == 12.0


# ---------------------------------------------------------------------------
# 主行业归属解析（basic_data_service，层级单独查询 + 目录唯一匹配）
# ---------------------------------------------------------------------------


def _basic_data_payload(name: str) -> Dict[str, Any]:
    """basic_data_service 成功响应（真实形状：每 code 一个 table，列=指标名）。"""
    return {
        "errorcode": 0,
        "errmsg": "",
        "tables": [
            {"thscode": "600188.SH", "time": [],
             "table": {"ths_the_sw_industry_stock": [name]}}
        ],
    }


class _TransportRecorder:
    """替换 sw 模块的传输与认证缝，记录请求体并返回预置响应。"""

    def __init__(self, responses: Dict[str, Any]) -> None:
        self.responses = responses
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

    def post(self, url: str, headers: Dict[str, str],
             body: Optional[bytes], *, label: str) -> Tuple[int, bytes]:
        assert body is not None
        parsed = json.loads(body.decode())
        self.calls.append((url, parsed))
        for key, payload in self.responses.items():
            if key in url:
                return 200, json.dumps(payload).encode()
        raise AssertionError(f"未预置响应的端点: {url}")

    def resolve_token(self, refresh_token: str, *, force_refresh: bool = False) -> str:
        return "stub-access-token-value"


@pytest.fixture()
def recorder(monkeypatch):
    def _install(responses: Dict[str, Any]) -> _TransportRecorder:
        rec = _TransportRecorder(responses)
        monkeypatch.setattr(sw, "_http_api_curl_post", rec.post)
        monkeypatch.setattr(sw, "_resolve_access_token", rec.resolve_token)
        return rec
    return _install


class TestResolveSwMainIndustry:
    def test_l1_unique_directory_match_confirms_identity(self, recorder):
        rec = recorder({
            "basic_data_service": _basic_data_payload("煤炭"),
        })
        out = sw.resolve_sw_main_industry(
            "600188.SH", observation_date="2026-11-18",
            refresh_token="sector-test-refresh-token", timeout_seconds=10.0, deadline=None,
        )
        assert out["status"] == "confirmed"
        assert out["index_code"] == "801950.SL"
        assert out["index_name"] == "煤炭"
        assert out["classification_system"] == "申万 2021 一级行业"
        assert out["classification_level"] == "L1"
        assert out["classification_version"] == sw.SW_L1_DIRECTORY_VERSION
        assert out["vendor_as_of"] == "2026-11-18"
        # L1 核心单一出站（spec 复审拆缝）：二/三级参考出站延迟到
        # resolve_sw_reference_levels（核心事实/口径选择/必需研究之后）
        basic_calls = [p for u, p in rec.calls if "basic_data_service" in u]
        assert len(basic_calls) == 1
        indipara = basic_calls[0]["indipara"]
        assert len(indipara) == 1
        assert indipara[0]["indicator"] == "ths_the_sw_industry_stock"
        assert indipara[0]["indiparams"][0] == "1"
        assert out["l2_name"] is None and out["l3_name"] is None

    def test_reference_levels_resolved_per_level_in_leftover_call(self, recorder):
        """参考链独立入口：层级单独查询（L2/L3 各一次），结果随 dict 返回。"""
        rec = recorder({})
        queue = {2: _basic_data_payload("煤炭开采"),
                 3: _basic_data_payload("动力煤")}

        def post_by_order(url, headers, body, *, label):
            parsed = json.loads(body.decode())
            rec.calls.append((url, parsed))
            level = int(parsed["indipara"][0]["indiparams"][0])
            return 200, json.dumps(queue[level]).encode()

        rec.calls.clear()
        from unittest.mock import patch as _patch

        with _patch.object(sw, "_http_api_curl_post", post_by_order), \
                _patch.object(sw, "_resolve_access_token", rec.resolve_token):
            refs = sw.resolve_sw_reference_levels(
                "600188.SH", observation_date="2026-11-18",
                refresh_token="sector-test-refresh-token",
                timeout_seconds=10.0, deadline=None,
            )
        assert refs == {"l2_name": "煤炭开采", "l3_name": "动力煤", "gaps": []}
        levels = [p["indipara"][0]["indiparams"][0] for _u, p in rec.calls]
        assert levels == ["2", "3"]  # 层级单独查询（同批重复指标会覆盖结果）

    def test_reference_level_failure_isolates_levels_with_gap(self, recorder):
        """单级参考失败：该级 None + 可选缺口，另一级不受影响（不抛出）。"""
        rec = recorder({})

        def post_by_order(url, headers, body, *, label):
            parsed = json.loads(body.decode())
            rec.calls.append((url, parsed))
            level = int(parsed["indipara"][0]["indiparams"][0])
            if level == 2:
                return 200, json.dumps(
                    {"errorcode": -1, "errmsg": "busy", "tables": []}).encode()
            return 200, json.dumps(_basic_data_payload("动力煤")).encode()

        from unittest.mock import patch as _patch

        with _patch.object(sw, "_http_api_curl_post", post_by_order), \
                _patch.object(sw, "_resolve_access_token", rec.resolve_token):
            refs = sw.resolve_sw_reference_levels(
                "600188.SH", observation_date="2026-11-18",
                refresh_token="sector-test-refresh-token",
                timeout_seconds=10.0, deadline=None,
            )
        assert refs["l2_name"] is None
        assert refs["l3_name"] == "动力煤"
        assert len(refs["gaps"]) == 1
        assert "二级" in refs["gaps"][0] and "可选参考" in refs["gaps"][0]

    def test_l1_name_not_in_directory_is_unavailable_no_approximation(self, recorder):
        """三级行业名（如「动力煤」）不在一级目录：不近似择首、不拼同名板块。"""
        recorder({"basic_data_service": _basic_data_payload("动力煤")})
        out = sw.resolve_sw_main_industry(
            "600188.SH", observation_date="2026-11-18",
            refresh_token="sector-test-refresh-token", timeout_seconds=10.0, deadline=None,
        )
        assert out["status"] == "unavailable"
        assert out["index_code"] is None
        assert out.get("gaps") and "不近似择首" in out["gaps"][0]

    def test_transport_failure_is_unavailable_with_reason(self, recorder, monkeypatch):
        recorder({})
        monkeypatch.setattr(
            sw, "_http_api_curl_post",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("curl unavailable")),
        )
        out = sw.resolve_sw_main_industry(
            "600188.SH", observation_date="2026-11-18",
            refresh_token="sector-test-refresh-token", timeout_seconds=10.0, deadline=None,
        )
        assert out["status"] == "unavailable"
        assert out["gaps"] and "curl unavailable" in out["gaps"][0]

    def test_vendor_errorcode_is_unavailable(self, recorder):
        recorder({"basic_data_service": {
            "errorcode": -4001, "errmsg": "no data", "tables": [],
        }})
        out = sw.resolve_sw_main_industry(
            "600188.SH", observation_date="2026-11-18",
            refresh_token="sector-test-refresh-token", timeout_seconds=10.0, deadline=None,
        )
        assert out["status"] == "unavailable"
        assert any("no data" in g for g in out["gaps"])


# ---------------------------------------------------------------------------
# 一级指数快照（real_time_quotation）：wire 解析 + 同日合格行筛选
# ---------------------------------------------------------------------------


def _quote_row(thscode: str, date: str, time: str, change: float,
               latest: float, pre_close: float, *, amount: float = 1.0e9,
               turnover: float = 2.0, up: float = 20.0, down: float = 10.0,
               name: Optional[str] = None) -> Dict[str, Any]:
    table: Dict[str, List[Any]] = {
        "changeRatio": [change], "amount": [amount], "tradeTime": [time],
        "latest": [latest], "preClose": [pre_close], "turnoverRatio": [turnover],
        "riseCount": [up], "fallCount": [down],
    }
    if name is not None:
        table["ths_index_short_name_index"] = [name]
    return {"marketCategory": "1", "pricetype": 1, "thscode": thscode,
            "time": [f"{date} {time}"], "table": table}


class TestFetchIndexQuotes:
    def test_parses_rows_with_identity_and_facts(self, recorder):
        recorder({"real_time_quotation": {
            "errorcode": 0, "errmsg": "Success!",
            "tables": [
                _quote_row("801950.SL", "2026-11-18", "22:00:46",
                           -0.867, 2520.109, 2542.15),
                _quote_row("000300.SH", "2026-11-18", "22:00:46",
                           -0.159, 3253.993, 3258.83),
            ],
        }})
        rows = sw.fetch_index_quotes(
            "801950.SL,000300.SH", refresh_token="sector-test-refresh-token",
            timeout_seconds=10.0, deadline=None,
        )
        by_code = {r["thscode"]: r for r in rows}
        coal = by_code["801950.SL"]
        assert coal["trade_date"] == "2026-11-18"
        assert coal["trade_time"] == "22:00:46"
        # 交叉复核输入保留精确字面（Decimal）；公开投影经 float 边界
        assert isinstance(coal["change_pct"], Decimal)
        assert float(coal["change_pct"]) == pytest.approx(-0.867)
        assert float(coal["latest"]) == pytest.approx(2520.109)
        assert float(coal["pre_close"]) == pytest.approx(2542.15)
        assert coal["amount"] == pytest.approx(1.0e9)
        assert coal["up_count"] == 20.0 and coal["down_count"] == 10.0

    def test_qualify_filters_by_session_date_and_sanity(self, recorder):
        """同日 + 涨跌幅与最新价/昨收交叉复核；异日行与不一致行不合格。"""
        consistent = _quote_row("801950.SL", "2026-11-18", "22:00:46",
                                -0.867, 2520.109, 2542.15)
        stale = _quote_row("801120.SL", "2026-11-17", "22:00:01",
                           1.0, 3543.0, 3579.0)
        inconsistent = _quote_row("801890.SL", "2026-11-18", "22:00:46",
                                  5.0, 1940.623, 1931.23)  # 差异远超容差
        recorder({"real_time_quotation": {
            "errorcode": 0, "errmsg": "Success!",
            "tables": [consistent, stale, inconsistent],
        }})
        rows = sw.fetch_index_quotes(
            "801950.SL,801120.SL,801890.SL",
            refresh_token="sector-test-refresh-token",
            timeout_seconds=10.0, deadline=None,
        )
        qualified, notes = sw.qualify_quote_rows(rows, session_date="2026-11-18")
        assert "801950.SL" in qualified
        assert "801120.SL" not in qualified  # 异日
        assert "801890.SL" not in qualified  # 交叉复核不一致
        assert notes == []

    def test_qualify_allows_small_rounding_tolerance(self, recorder):
        row = _quote_row("801950.SL", "2026-11-18", "22:00:46",
                         -0.86702200, 2520.109, 2542.15)
        recorder({"real_time_quotation": {
            "errorcode": 0, "errmsg": "Success!", "tables": [row],
        }})
        rows = sw.fetch_index_quotes(
            "801950.SL", refresh_token="sector-test-refresh-token", timeout_seconds=10.0, deadline=None,
        )
        qualified, _notes = sw.qualify_quote_rows(rows, session_date="2026-11-18")
        assert "801950.SL" in qualified

    def test_missing_change_ratio_row_never_qualifies(self, recorder):
        row = _quote_row("801950.SL", "2026-11-18", "22:00:46",
                         -0.867, 2520.109, 2542.15)
        row["table"]["changeRatio"] = [None]
        recorder({"real_time_quotation": {
            "errorcode": 0, "errmsg": "Success!", "tables": [row],
        }})
        rows = sw.fetch_index_quotes(
            "801950.SL", refresh_token="sector-test-refresh-token", timeout_seconds=10.0, deadline=None,
        )
        qualified, _notes = sw.qualify_quote_rows(rows, session_date="2026-11-18")
        assert qualified == {}

    # ---- #102 复审：Decimal 交叉复核边界 + 畸形数值位不拖垮同批健康行 ----

    def test_tolerance_boundary_counterexample_rejected_by_decimal(self, recorder):
        """复核边界反例（#102 复审 Standards 1）：latest=100.5 / preClose=100 /
        changeRatio=0.495 的确切差值 0.005pp 按Decimal 计算**拒绝**（二进制
        浮点曾算成 0.00499… 并错误采纳）；容差内侧同样反例仍拒绝、一致行采纳。"""
        boundary = _quote_row("801950.SL", "2026-11-18", "22:00:46",
                              0.495, 100.5, 100.0)
        beyond = _quote_row("801120.SL", "2026-11-18", "22:00:46",
                            0.494, 100.5, 100.0)  # 差 0.006pp：边界外
        consistent = _quote_row("801890.SL", "2026-11-18", "22:00:46",
                                0.496, 100.5, 100.0)  # 差 0.004pp：容差内
        recorder({"real_time_quotation": {
            "errorcode": 0, "errmsg": "Success!",
            "tables": [boundary, beyond, consistent],
        }})
        rows = sw.fetch_index_quotes(
            "801950.SL,801120.SL,801890.SL",
            refresh_token="sector-test-refresh-token",
            timeout_seconds=10.0, deadline=None,
        )
        qualified, notes = sw.qualify_quote_rows(
            rows, session_date="2026-11-18", received_at=_dt(2026, 11, 19, 1, 35))
        assert set(qualified) == {"801890.SL"}
        assert notes == []

    def test_mixed_batch_malformed_cells_keep_healthy_rows(self, recorder):
        """混合批次（#102 复审 Standards 2）：第二行涨跌幅为非 list 畸形容器、
        第三行 amount 为超 float 范围的原始整数——畸形都只作用于本行字段
        （按缺值处理），健康首行完整存活，整批不再 KeyError/OverflowError。"""
        healthy = _quote_row("801950.SL", "2026-11-18", "22:00:46",
                             -0.867, 2520.109, 2542.15)
        bad_change = _quote_row("801120.SL", "2026-11-18", "22:00:46",
                                1.0, 3543.0, 3579.0)
        bad_change["table"]["changeRatio"] = {"unexpected": 1}  # 非 list
        overflow_amount = _quote_row("801890.SL", "2026-11-18", "22:00:46",
                                     0.486, 1940.623, 1931.23)
        overflow_amount["table"]["amount"] = [10 ** 400]  # float 口径溢出
        recorder({"real_time_quotation": {
            "errorcode": 0, "errmsg": "Success!",
            "tables": [healthy, bad_change, overflow_amount],
        }})
        rows = sw.fetch_index_quotes(
            "801950.SL,801120.SL,801890.SL",
            refresh_token="sector-test-refresh-token",
            timeout_seconds=10.0, deadline=None,
        )
        by_code = {r["thscode"]: r for r in rows}
        assert float(by_code["801950.SL"]["change_pct"]) == pytest.approx(-0.867)
        assert by_code["801120.SL"]["change_pct"] is None  # 畸形按缺值
        assert float(by_code["801890.SL"]["change_pct"]) == pytest.approx(0.486)
        assert by_code["801890.SL"]["amount"] is None  # float 口径溢出按缺值
        # 健康行照常合格；畸形行不冒充有效涨跌幅（31 指数口径剔除）
        qualified, _notes = sw.qualify_quote_rows(
            rows, session_date="2026-11-18", received_at=_dt(2026, 11, 19, 1, 35))
        assert set(qualified) == {"801950.SL", "801890.SL"}

    def test_ancillary_trade_time_never_becomes_source_text(self, recorder):
        """超长辅助 tradeTime（250 字符，#102 复审 Standards 3）：不进入行
        事实——trade_time 归一自已解析合格的完整供应商时间戳。"""
        row = _quote_row("801950.SL", "2026-11-18", "22:00:46",
                         -0.867, 2520.109, 2542.15)
        row["table"]["tradeTime"] = ["x" * 250]
        recorder({"real_time_quotation": {
            "errorcode": 0, "errmsg": "Success!", "tables": [row],
        }})
        rows = sw.fetch_index_quotes(
            "801950.SL", refresh_token="sector-test-refresh-token",
            timeout_seconds=10.0, deadline=None,
        )
        assert rows[0]["trade_time"] == "22:00:46"
        assert rows[0]["stamp"] == _dt(2026, 11, 18, 22, 0, 46)




def _raw_quotes_wire(change_literal: str) -> bytes:
    """真实 wire 原始字节（transport leaf）：changeRatio 保留**字面**精度。"""
    return (
        '{"errorcode":0,"errmsg":"Success!","tables":[{"thscode":"801950.SL",'
        '"time":["2026-11-18 22:00:46"],"table":{"changeRatio":[%s],'
        '"amount":[1000000000],"tradeTime":["22:00:46"],'
        '"latest":[100.5],"preClose":[100],"turnoverRatio":[2.0],'
        '"riseCount":[20.0],"fallCount":[10.0]}}]}' % change_literal
    ).encode()


class TestQuoteWireLiteralPrecision:
    """真实 wire 字面精度（#102 复审 #1）：json.loads 的 float 会把
    ``0.495000000000000001`` 舍入成 ``0.495``——实时行情响应按 Decimal 解析、
    交叉复核输入原样贯穿资格判定，两个字面可区分；公开展示投影仍是
    JSON 安全 float。测试在**传输叶子**喂原始响应字节（真实解码/解析全链）。"""

    def _install_raw_wire(self, monkeypatch, raw: bytes) -> None:
        monkeypatch.setattr(sw, "_resolve_access_token",
                            lambda rt, *, force_refresh=False: "stub-token")
        monkeypatch.setattr(
            sw, "_http_api_curl_post",
            lambda *a, **k: (200, raw))

    @pytest.mark.parametrize("literal", [
        "0.495000000000000001",
        "0.4950000000000000000000000000001",
    ])
    def test_literal_beyond_float_precision_passes_tolerance(self, monkeypatch, literal):
        """差值低于 0.005pp 时采纳，避免 float 或默认 Decimal 精度改变判定。"""
        self._install_raw_wire(monkeypatch, _raw_quotes_wire(literal))
        rows = sw.fetch_index_quotes(
            "801950.SL", refresh_token="sector-test-refresh-token",
            timeout_seconds=10.0, deadline=None,
        )
        # 精确字面贯穿资格判定（不被 float 舍入）
        assert rows[0]["change_pct"] == Decimal(literal)
        qualified, notes = sw.qualify_quote_rows(
            rows, session_date="2026-11-18",
            received_at=_dt(2026, 11, 19, 1, 35),
            session_close=_dt(2026, 11, 18, 15, 0, 0),
        )
        assert set(qualified) == {"801950.SL"}
        assert notes == []
        # 公开展示投影：JSON 安全 float（str/float 边界正常转换）
        assert float(qualified["801950.SL"]["change_pct"]) == pytest.approx(0.495)
        assert str(qualified["801950.SL"]["change_pct"]) == literal

    def test_exact_boundary_literal_rejected_strict_route(self, monkeypatch):
        """字面恰 0.495（latest=100.5/preClose=100 推算差恰 0.005pp）：
        严格指数口径（require_change 默认 True）整行拒绝。"""
        self._install_raw_wire(monkeypatch, _raw_quotes_wire("0.495"))
        rows = sw.fetch_index_quotes(
            "801950.SL", refresh_token="sector-test-refresh-token",
            timeout_seconds=10.0, deadline=None,
        )
        assert rows[0]["change_pct"] == Decimal("0.495")
        qualified, _notes = sw.qualify_quote_rows(
            rows, session_date="2026-11-18",
            received_at=_dt(2026, 11, 19, 1, 35),
            session_close=_dt(2026, 11, 18, 15, 0, 0),
        )
        assert qualified == {}

    def test_overflow_magnitude_rejects_row_not_batch(self, monkeypatch):
        """数值超出解码或报告范围时，保留同批健康行和合格的成分成交额。"""
        raw = (
            '{"errorcode":0,"errmsg":"Success!","tables":['
            '{"thscode":"801950.SL","time":["2026-11-18 22:00:46"],'
            '"table":{"changeRatio":[0.5],"amount":[1000000000],'
            '"tradeTime":["22:00:46"],"latest":[1e1000000],'
            '"preClose":[100],"turnoverRatio":[2.0],'
            '"riseCount":[20.0],"fallCount":[10.0]}},'
            '{"thscode":"801890.SL","time":["2026-11-18 22:00:46"],'
            '"table":{"changeRatio":[1e400],"amount":[1000000000]}},'
            '{"thscode":"801010.SL","time":["2026-11-18 22:00:46"],'
            '"table":{"changeRatio":[1e9999999999999999999],"amount":[1000000000]}},'
            '{"thscode":"801120.SL","time":["2026-11-18 22:00:46"],'
            '"table":{"changeRatio":[0.5],"amount":[1000000000],'
            '"tradeTime":["22:00:46"],"latest":[100.5],'
            '"preClose":[100],"turnoverRatio":[2.0],'
            '"riseCount":[20.0],"fallCount":[10.0]}}]}'
        ).encode()
        self._install_raw_wire(monkeypatch, raw)
        rows = sw.fetch_index_quotes(
            "801950.SL,801890.SL,801010.SL,801120.SL",
            refresh_token="sector-test-refresh-token",
            timeout_seconds=10.0, deadline=None,
        )
        # 严格口径：溢出行拒绝，健康行合格
        strict, _notes = sw.qualify_quote_rows(
            rows, session_date="2026-11-18",
            received_at=_dt(2026, 11, 19, 1, 35),
            session_close=_dt(2026, 11, 18, 15, 0, 0),
        )
        assert set(strict) == {"801120.SL"}
        # 成分口径：溢出行保留（涨跌幅空白化、金额保留）
        member, _notes2 = sw.qualify_quote_rows(
            rows, session_date="2026-11-18",
            received_at=_dt(2026, 11, 19, 1, 35),
            session_close=_dt(2026, 11, 18, 15, 0, 0),
            require_change=False,
        )
        invalid_change_codes = {"801950.SL", "801890.SL", "801010.SL"}
        assert set(member) == invalid_change_codes | {"801120.SL"}
        for code in invalid_change_codes:
            assert member[code]["change_pct"] is None
            assert member[code]["amount"] == pytest.approx(1.0e9)

    def test_history_decoding_stays_plain_float(self, monkeypatch):
        """日线/其他端点解码不受影响（不传 parse_float，历史行为逐位一致）。"""
        import pandas as pd

        raw = ('{"errorcode":0,"errmsg":"","tables":[{"thscode":"801950.SL",'
               '"time":["2026-11-03"],"table":{"close":[2500.5]}}]}').encode()
        monkeypatch.setattr(sw, "_resolve_access_token",
                            lambda rt, *, force_refresh=False: "stub-token")
        monkeypatch.setattr(sw, "_http_api_curl_post",
                            lambda *a, **k: (200, raw))
        frames = sw.fetch_index_history(
            "801950.SL", "2026-11-03", "2026-11-03",
            refresh_token="sector-test-refresh-token",
            timeout_seconds=10.0, deadline=None,
        )
        close = frames["801950.SL"].iloc[0]["close"]
        assert isinstance(close, float)  # 既有 float 解码语义不变


class TestMemberQuoteFieldSplitQualification:
    """成分成员/目标独立行情口径（require_change=False）：身份/会话/时间戳
    资格照常，涨跌幅缺失不剔除整行——amount-only 行保留有效金额。"""

    def _rows(self, *, change) -> List[Dict[str, Any]]:
        row = _quote_row("600188.SH", "2026-11-18", "22:00:46",
                         change if change is not None else -0.867,
                         9.4 if change is not None else 100.0, 100.0)
        if change is None:
            row["table"]["changeRatio"] = [None]
        return [row]

    def test_amount_only_member_stays_qualified_with_amount(self, recorder):
        """同会话 amount-only 成员（涨跌幅缺失）：行保留（金额参与选样）。"""
        recorder({"real_time_quotation": {
            "errorcode": 0, "errmsg": "Success!", "tables": self._rows(change=None),
        }})
        rows = sw.fetch_index_quotes(
            "600188.SH", refresh_token="sector-test-refresh-token",
            timeout_seconds=10.0, deadline=None,
        )
        qualified, notes = sw.qualify_quote_rows(
            rows, session_date="2026-11-18",
            received_at=_dt(2026, 11, 19, 1, 35),
            session_close=_dt(2026, 11, 18, 15, 0, 0),
            require_change=False,
        )
        assert set(qualified) == {"600188.SH"}
        assert qualified["600188.SH"]["amount"] == pytest.approx(1.0e9)
        assert qualified["600188.SH"]["change_pct"] is None
        assert notes == []

    def test_member_cross_check_still_applies_when_change_present(self, recorder):
        """amount-only 口径下涨跌幅存在时交叉复核照常：不一致（报告 5.0 但
        latest/preClose 推算不符）→ 成分口径只作废涨跌幅（行保留、金额
        保留）；严格指数口径（require_change=True）整行拒绝。"""
        bad = self._rows(change=5.0)  # 报告 5.0 但 latest/preClose 推算不符
        recorder({"real_time_quotation": {
            "errorcode": 0, "errmsg": "Success!", "tables": bad,
        }})
        rows = sw.fetch_index_quotes(
            "600188.SH", refresh_token="sector-test-refresh-token",
            timeout_seconds=10.0, deadline=None,
        )
        qualified, _notes = sw.qualify_quote_rows(
            rows, session_date="2026-11-18",
            received_at=_dt(2026, 11, 19, 1, 35),
            session_close=_dt(2026, 11, 18, 15, 0, 0),
            require_change=False,
        )
        assert set(qualified) == {"600188.SH"}  # 行保留（副本空白化）
        assert qualified["600188.SH"]["change_pct"] is None
        assert qualified["600188.SH"]["amount"] == pytest.approx(1.0e9)
        # 原始行未被污染（调用方复用同一 rows 二筛时不携带空白化结果）
        assert rows[0]["change_pct"] is not None
        strict, _notes2 = sw.qualify_quote_rows(
            rows, session_date="2026-11-18",
            received_at=_dt(2026, 11, 19, 1, 35),
            session_close=_dt(2026, 11, 18, 15, 0, 0),
        )
        assert strict == {}  # 严格指数口径：复核不合格整行拒绝

    def test_malformed_change_member_kept_amount_only(self, recorder):
        """畸形涨跌幅单元（非 list 容器）：按缺值处理，amount-only 行保留
        （不抛出、不丢行）。"""
        bad = self._rows(change=None)
        bad[0]["table"]["changeRatio"] = {"unexpected": 1}
        recorder({"real_time_quotation": {
            "errorcode": 0, "errmsg": "Success!", "tables": bad,
        }})
        rows = sw.fetch_index_quotes(
            "600188.SH", refresh_token="sector-test-refresh-token",
            timeout_seconds=10.0, deadline=None,
        )
        qualified, _notes = sw.qualify_quote_rows(
            rows, session_date="2026-11-18",
            received_at=_dt(2026, 11, 19, 1, 35),
            session_close=_dt(2026, 11, 18, 15, 0, 0),
            require_change=False,
        )
        assert set(qualified) == {"600188.SH"}
        assert qualified["600188.SH"]["change_pct"] is None


# ---------------------------------------------------------------------------
# 指数日线（cmd_history_quotation）：多代码 wire 解析
# ---------------------------------------------------------------------------


class TestFetchIndexHistory:
    def test_parses_per_code_frames(self, recorder):
        tables = []
        for code, base in (("801950.SL", 2500.0), ("000300.SH", 3200.0)):
            dates = [f"2026-11-{d:02d}" for d in range(3, 21) if d not in (7, 8, 14, 15)]
            tables.append({
                "thscode": code, "time": dates,
                "table": {"close": [base + i for i in range(len(dates))]},
            })
        recorder({"cmd_history_quotation": {
            "errorcode": 0, "errmsg": "", "tables": tables,
        }})
        frames = sw.fetch_index_history(
            "801950.SL,000300.SH", "2026-11-03", "2026-11-20",
            refresh_token="sector-test-refresh-token", timeout_seconds=10.0, deadline=None,
        )
        assert set(frames) == {"801950.SL", "000300.SH"}
        df = frames["801950.SL"]
        assert isinstance(df, pd.DataFrame)
        assert list(df.columns) == ["date", "close"]
        assert df.iloc[0]["date"] == "2026-11-03"
        assert df.iloc[0]["close"] == pytest.approx(2500.0)

    def test_request_carries_strict_window_and_unadjusted_cps(self, recorder):
        rec = recorder({"cmd_history_quotation": {
            "errorcode": 0, "errmsg": "", "tables": [],
        }})
        with pytest.raises(sw.SwSectorFetchError):
            sw.fetch_index_history(
                "801950.SL", "2026-10-22", "2026-11-18",
                refresh_token="sector-test-refresh-token", timeout_seconds=10.0, deadline=None,
            )
        hist_calls = [p for u, p in rec.calls if "cmd_history_quotation" in u]
        assert len(hist_calls) == 1
        body = hist_calls[0]
        assert body["codes"] == "801950.SL"
        assert body["indicators"] == "close"
        assert body["startdate"] == "2026-10-22"
        assert body["enddate"] == "2026-11-18"
        assert body["functionpara"] == {"Interval": "D", "CPS": "1", "Fill": "Omit"}

    def test_empty_tables_raise_fetch_error(self, recorder):
        recorder({"cmd_history_quotation": {
            "errorcode": 0, "errmsg": "", "tables": [],
        }})
        with pytest.raises(sw.SwSectorFetchError):
            sw.fetch_index_history(
                "801950.SL", "2026-10-22", "2026-11-18",
                refresh_token="sector-test-refresh-token", timeout_seconds=10.0, deadline=None,
            )

    # ---- #98 复审：同码多表不再 last-wins（合并对齐观测 / 矛盾整码拒绝）----

    def test_same_code_disjoint_tables_concatenate_not_last_wins(self, recorder):
        """同 thscode 拆两表（日期互斥）：合并全部观测，前表不被末表覆盖。"""
        early = {"thscode": "801950.SL",
                 "time": ["2026-11-03", "2026-11-04", "2026-11-05"],
                 "table": {"close": [2500.0, 2501.0, 2502.0]}}
        late = {"thscode": "801950.SL",
                "time": ["2026-11-06", "2026-11-07"],
                "table": {"close": [2503.0, 2504.0]}}
        recorder({"cmd_history_quotation": {
            "errorcode": 0, "errmsg": "", "tables": [early, late],
        }})
        frames = sw.fetch_index_history(
            "801950.SL", "2026-11-03", "2026-11-07",
            refresh_token="sector-test-refresh-token", timeout_seconds=10.0, deadline=None,
        )
        df = frames["801950.SL"]
        assert list(df["date"]) == ["2026-11-03", "2026-11-04", "2026-11-05",
                                    "2026-11-06", "2026-11-07"]
        assert list(df["close"]) == [2500.0, 2501.0, 2502.0, 2503.0, 2504.0]

    def test_same_code_identical_overlap_deduped(self, recorder):
        """同码两表重叠日期且值一致：按同日去重保留一行（不矛盾、不拒绝）。"""
        t1 = {"thscode": "801950.SL",
              "time": ["2026-11-03", "2026-11-04"],
              "table": {"close": [2500.0, 2501.0]}}
        t2 = {"thscode": "801950.SL",
              "time": ["2026-11-04", "2026-11-05"],
              "table": {"close": [2501.0, 2502.0]}}
        recorder({"cmd_history_quotation": {
            "errorcode": 0, "errmsg": "", "tables": [t1, t2],
        }})
        frames = sw.fetch_index_history(
            "801950.SL", "2026-11-03", "2026-11-05",
            refresh_token="sector-test-refresh-token", timeout_seconds=10.0, deadline=None,
        )
        df = frames["801950.SL"]
        assert list(df["date"]) == ["2026-11-03", "2026-11-04", "2026-11-05"]
        assert list(df["close"]) == [2500.0, 2501.0, 2502.0]

    def test_same_code_conflicting_duplicate_rejects_code_keeps_other(self, recorder):
        """同码同日收盘矛盾：该代码整体不采用（不取其一），
        其他指数/基准的有效行不受影响（不丢弃）。"""
        bad_a = {"thscode": "801950.SL",
                 "time": ["2026-11-03", "2026-11-04"],
                 "table": {"close": [2500.0, 2501.0]}}
        bad_b = {"thscode": "801950.SL",
                 "time": ["2026-11-04"],
                 "table": {"close": [9999.0]}}  # 同日矛盾值
        bench = {"thscode": "000300.SH",
                 "time": ["2026-11-03", "2026-11-04"],
                 "table": {"close": [3200.0, 3201.0]}}
        recorder({"cmd_history_quotation": {
            "errorcode": 0, "errmsg": "", "tables": [bad_a, bad_b, bench],
        }})
        frames = sw.fetch_index_history(
            "801950.SL,000300.SH", "2026-11-03", "2026-11-04",
            refresh_token="sector-test-refresh-token", timeout_seconds=10.0, deadline=None,
        )
        assert "801950.SL" not in frames  # 矛盾整码保守拒绝
        assert set(frames) == {"000300.SH"}  # 基准有效行保留
        assert list(frames["000300.SH"]["close"]) == [3200.0, 3201.0]

    def test_all_codes_conflicting_raises_fetch_error(self, recorder):
        """全部代码同码矛盾：无有效行 → 源错误（调用方降缺口，不静默通过）。"""
        a = {"thscode": "801950.SL",
             "time": ["2026-11-03"], "table": {"close": [2500.0]}}
        b = {"thscode": "801950.SL",
             "time": ["2026-11-03"], "table": {"close": [2501.0]}}
        recorder({"cmd_history_quotation": {
            "errorcode": 0, "errmsg": "", "tables": [a, b],
        }})
        with pytest.raises(sw.SwSectorFetchError):
            sw.fetch_index_history(
                "801950.SL", "2026-11-03", "2026-11-03",
                refresh_token="sector-test-refresh-token", timeout_seconds=10.0, deadline=None,
            )

    # ---- #98 复审二轮：同码合并的日期/数值归一与窗口边界 ----

    def test_same_day_wire_format_variants_dedupe_to_one_observation(
            self, recorder):
        """"2026-11-03" 与 "2026-11-03 00:00:00" 是同一交易日：同日等值
        合并去重为一行，不产生伪重复、不丢该日（既有严格窗口资格语义）。"""
        a = {"thscode": "801950.SL",
             "time": ["2026-11-03"], "table": {"close": [2500.0]}}
        b = {"thscode": "801950.SL",
             "time": ["2026-11-03 00:00:00"], "table": {"close": [2500.0]}}
        recorder({"cmd_history_quotation": {
            "errorcode": 0, "errmsg": "", "tables": [a, b],
        }})
        frames = sw.fetch_index_history(
            "801950.SL", "2026-11-03", "2026-11-03",
            refresh_token="sector-test-refresh-token", timeout_seconds=10.0, deadline=None,
        )
        df = frames["801950.SL"]
        assert list(df["date"]) == ["2026-11-03"]
        assert list(df["close"]) == [2500.0]

    def test_normalized_same_day_conflicting_closes_reject_code(self, recorder):
        """同日不同 wire 形式的收盘矛盾：归一后命中整码冲突守卫（不再因
        键不同而漏判），该代码不采用，其他代码保留。"""
        bad_a = {"thscode": "801950.SL",
                 "time": ["2026-11-03"], "table": {"close": [2500.0]}}
        bad_b = {"thscode": "801950.SL",
                 "time": ["2026-11-03 00:00:00"], "table": {"close": [9999.0]}}
        bench = {"thscode": "000300.SH",
                 "time": ["2026-11-03"], "table": {"close": [3200.0]}}
        recorder({"cmd_history_quotation": {
            "errorcode": 0, "errmsg": "", "tables": [bad_a, bad_b, bench],
        }})
        frames = sw.fetch_index_history(
            "801950.SL,000300.SH", "2026-11-03", "2026-11-03",
            refresh_token="sector-test-refresh-token", timeout_seconds=10.0, deadline=None,
        )
        assert "801950.SL" not in frames
        assert set(frames) == {"000300.SH"}

    def test_out_of_window_conflict_never_drops_in_window_rows(self, recorder):
        """窗口外多给日期的矛盾按既有资格语义忽略（多给的旧日不能淘汰
        窗口内有效行）：窗口内等值行全部保留、代码不拒绝。"""
        a = {"thscode": "801950.SL",
             "time": ["2026-11-03", "2026-11-04"],
             "table": {"close": [2500.0, 2501.0]}}
        b = {"thscode": "801950.SL",
             "time": ["2026-11-03", "2026-11-04", "2026-10-30"],
             "table": {"close": [2500.0, 2501.0, 9999.0]}}
        recorder({"cmd_history_quotation": {
            "errorcode": 0, "errmsg": "", "tables": [a, b],
        }})
        frames = sw.fetch_index_history(
            "801950.SL", "2026-11-03", "2026-11-04",
            refresh_token="sector-test-refresh-token", timeout_seconds=10.0, deadline=None,
        )
        df = frames["801950.SL"]
        assert list(df["date"]) == ["2026-11-03", "2026-11-04"]
        assert list(df["close"]) == [2500.0, 2501.0]

    def test_numeric_close_variants_of_same_day_dedupe(self, recorder):
        """wire ``10`` 与 ``"10.00"`` 是同一有效收盘（既有有限正 Decimal
        语义）：同日数值等价去重，不整码拒绝。"""
        a = {"thscode": "801950.SL",
             "time": ["2026-11-03"], "table": {"close": [10]}}
        b = {"thscode": "801950.SL",
             "time": ["2026-11-03"], "table": {"close": ["10.00"]}}
        recorder({"cmd_history_quotation": {
            "errorcode": 0, "errmsg": "", "tables": [a, b],
        }})
        frames = sw.fetch_index_history(
            "801950.SL", "2026-11-03", "2026-11-03",
            refresh_token="sector-test-refresh-token", timeout_seconds=10.0, deadline=None,
        )
        df = frames["801950.SL"]
        assert list(df["date"]) == ["2026-11-03"]
        assert list(df["close"]) == [10]

    def test_boolean_close_is_never_numeric(self, recorder):
        """布尔收盘永不作数值（True ≠ 1）：与同日有效值不同类 → 整码矛盾
        保守拒绝（不取其一）；单码全拒绝 → 源错误（调用方降缺口）。"""
        a = {"thscode": "801950.SL",
             "time": ["2026-11-03"], "table": {"close": [True]}}
        b = {"thscode": "801950.SL",
             "time": ["2026-11-03"], "table": {"close": [1.0]}}
        recorder({"cmd_history_quotation": {
            "errorcode": 0, "errmsg": "", "tables": [a, b],
        }})
        with pytest.raises(sw.SwSectorFetchError):
            sw.fetch_index_history(
                "801950.SL", "2026-11-03", "2026-11-03",
                refresh_token="sector-test-refresh-token", timeout_seconds=10.0, deadline=None,
            )

    def test_identical_missing_close_dedupes_preserving_row(self, recorder):
        """同日同为缺值（None）：同一「缺值事实」去重保留一行透传（资格层
        按无效收盘日记缺口，行级诊断不丢），不整码拒绝。"""
        a = {"thscode": "801950.SL",
             "time": ["2026-11-03"], "table": {"close": [None]}}
        b = {"thscode": "801950.SL",
             "time": ["2026-11-03"], "table": {"close": [None]}}
        recorder({"cmd_history_quotation": {
            "errorcode": 0, "errmsg": "", "tables": [a, b],
        }})
        frames = sw.fetch_index_history(
            "801950.SL", "2026-11-03", "2026-11-03",
            refresh_token="sector-test-refresh-token", timeout_seconds=10.0, deadline=None,
        )
        df = frames["801950.SL"]
        assert list(df["date"]) == ["2026-11-03"]
        assert list(df["close"]) == [None]


# ---------------------------------------------------------------------------
# 时间/新鲜度纪律（接收钟 + 会话开收盘；盘后供应商推送合法）
# ---------------------------------------------------------------------------

from datetime import datetime as _dt


class TestQuoteFreshnessDiscipline:
    def test_completed_session_requires_post_close_stamp(self):
        """已完结会话（接收≥收盘）：须 收盘≤供应商时间戳≤接收；盘后推送合法。"""
        rows = [{
            "thscode": "801950.SL", "trade_date": "2026-11-18",
            "trade_time": "22:00:46",
            "stamp": _dt(2026, 11, 18, 22, 0, 46),
            "change_pct": -0.867, "latest": 2520.109, "pre_close": 2542.15,
        }, {
            "thscode": "801120.SL", "trade_date": "2026-11-18",
            "trade_time": "10:30:00",
            "stamp": _dt(2026, 11, 18, 10, 30, 0),  # 已完结会话的盘中旧快照
            "change_pct": -1.006, "latest": 3543.0, "pre_close": 3579.0,
        }, {
            "thscode": "801890.SL", "trade_date": "2026-11-18",
            "trade_time": "15:10:00",
            "stamp": _dt(2026, 11, 18, 15, 10, 0),  # 盘后推送，合法
            "change_pct": 0.486, "latest": 1940.623, "pre_close": 1931.23,
        }]
        received = _dt(2026, 11, 19, 1, 35, 0)
        session_close = _dt(2026, 11, 18, 15, 0, 0)
        qualified, notes = sw.qualify_quote_rows(
            rows, session_date="2026-11-18",
            received_at=received, session_close=session_close,
        )
        assert set(qualified) == {"801950.SL", "801890.SL"}
        assert "801120.SL" not in qualified  # 未过收盘的旧快照拒绝
        assert notes == []

    def test_intraday_requires_stamp_within_open_to_received(self):
        """盘中（接收<收盘）：须 开盘≤供应商时间戳≤接收；未来时间戳拒绝。"""
        good = {"thscode": "801950.SL", "trade_date": "2026-11-19",
                "trade_time": "10:29:00",
                "stamp": _dt(2026, 11, 19, 10, 29, 0),
                "change_pct": 1.0, "latest": 101.0, "pre_close": 100.0}
        before_open = {"thscode": "801120.SL", "trade_date": "2026-11-19",
                       "trade_time": "09:00:00",
                       "stamp": _dt(2026, 11, 19, 9, 0, 0),
                       "change_pct": 0.5, "latest": 100.5, "pre_close": 100.0}
        future = {"thscode": "801890.SL", "trade_date": "2026-11-19",
                  "trade_time": "10:31:00",
                  "stamp": _dt(2026, 11, 19, 10, 31, 0),  # 晚于接收时刻
                  "change_pct": 0.2, "latest": 100.2, "pre_close": 100.0}
        received = _dt(2026, 11, 19, 10, 30, 0)
        qualified, notes = sw.qualify_quote_rows(
            [good, before_open, future], session_date="2026-11-19",
            received_at=received,
            session_open=_dt(2026, 11, 19, 9, 30, 0),
            session_close=_dt(2026, 11, 19, 15, 0, 0),
        )
        assert set(qualified) == {"801950.SL"}
        assert notes == []

    def test_unparseable_or_missing_stamp_rejected_conservatively(self):
        """时间戳缺失/不可解析：新鲜度无法验证，保守拒绝（不用请求时刻顶替）。"""
        rows = [{"thscode": "801950.SL", "trade_date": "2026-11-18",
                 "trade_time": "22:00:46", "stamp": None,
                 "change_pct": 1.0, "latest": 101.0, "pre_close": 100.0}]
        received = _dt(2026, 11, 19, 1, 35, 0)
        qualified, notes = sw.qualify_quote_rows(
            rows, session_date="2026-11-18", received_at=received)
        assert qualified == {}
        assert notes == []

    def test_wire_stamp_unparseable_row_dropped(self, recorder):
        """wire 时间戳畸形（非标准格式）：该行 stamp=None（不逃出边界）。"""
        bad = _quote_row("801950.SL", "2026-11-18", "22:00:46", -0.867,
                         2520.109, 2542.15)
        bad["time"] = ["not-a-timestamp"]
        recorder({"real_time_quotation": {
            "errorcode": 0, "errmsg": "Success!", "tables": [bad],
        }})
        rows = sw.fetch_index_quotes(
            "801950.SL", refresh_token="sector-test-refresh-token",
            timeout_seconds=10.0, deadline=None,
        )
        assert rows[0]["stamp"] is None
        qualified, _notes = sw.qualify_quote_rows(
            rows, session_date="2026-11-18", received_at=_dt(2026, 11, 19, 1, 35))
        assert qualified == {}


# ---------------------------------------------------------------------------
# 同码重复行保守拒绝
# ---------------------------------------------------------------------------


class TestDuplicateQuoteRows:
    def test_contradictory_date_duplicates_reject_code_with_note(self):
        """同码矛盾重复行（不同日期）：整码不采用，不取首行/末行，缺口注明。"""
        a = {"thscode": "801950.SL", "trade_date": "2026-11-18",
             "trade_time": "22:00:46", "stamp": _dt(2026, 11, 18, 22, 0, 46),
             "change_pct": -0.867, "latest": 2520.109, "pre_close": 2542.15}
        b = dict(a, trade_date="2026-11-17", change_pct=1.2,
                 latest=2530.0, stamp=_dt(2026, 11, 17, 22, 0, 46))  # 异日矛盾
        qualified, notes = sw.qualify_quote_rows(
            [a, b], session_date="2026-11-18",
            received_at=_dt(2026, 11, 19, 1, 35))
        assert qualified == {}
        assert notes and "801950.SL" in notes[0] and "保守拒绝" in notes[0]

    def test_contradictory_value_duplicates_reject_code(self):
        a = {"thscode": "801120.SL", "trade_date": "2026-11-18",
             "trade_time": "22:00:46", "stamp": _dt(2026, 11, 18, 22, 0, 46),
             "change_pct": -1.006, "latest": 3543.0, "pre_close": 3579.0}
        b = dict(a, change_pct=8.11)  # 同日不同涨跌幅
        qualified, notes = sw.qualify_quote_rows(
            [a, b], session_date="2026-11-18",
            received_at=_dt(2026, 11, 19, 1, 35))
        assert "801120.SL" not in qualified
        assert notes


# ---------------------------------------------------------------------------
# 归属查询顺序（L1 核心；L2/L3 可选参考仅在确认后）与凭据脱敏
# ---------------------------------------------------------------------------


class TestAttributionOrderAndRedaction:
    def test_l1_unmatched_stops_without_reference_outbound(self, recorder):
        """L1 未命中目录：主行业不可确认，且不发起 L2/L3 参考出站。"""
        rec = recorder({"basic_data_service": _basic_data_payload("动力煤")})
        out = sw.resolve_sw_main_industry(
            "600188.SH", observation_date="2026-11-18",
            refresh_token="sector-test-refresh-token",
            timeout_seconds=10.0, deadline=None,
        )
        assert out["status"] == "unavailable"
        assert len(rec.calls) == 1  # 仅 L1，无参考出站

    def test_l1_transport_failure_stops_without_reference_outbound(self, recorder):
        rec = recorder({})
        from unittest.mock import patch as _patch

        with _patch.object(sw, "_http_api_curl_post",
                           lambda *a, **k: (_ for _ in ()).throw(
                               RuntimeError("curl unavailable"))):
            out = sw.resolve_sw_main_industry(
                "600188.SH", observation_date="2026-11-18",
                refresh_token="sector-test-refresh-token",
                timeout_seconds=10.0, deadline=None,
            )
        assert out["status"] == "unavailable"
        assert any("curl unavailable" in g for g in out["gaps"])
        assert len(rec.calls) == 0

    def test_l1_confirm_initiates_no_reference_outbound(self, recorder):
        """核心确认后无参考出站：可选二/三级延迟到 resolve_sw_reference_levels
        （由编排层在必需研究后的余量预算内调用），主行业解析内也无虚构的
        可选参考缺口。"""
        rec = recorder({})
        rec.responses.clear()

        def post(url, headers, body, *, label):
            parsed = json.loads(body.decode())
            rec.calls.append((url, parsed))
            level = parsed["indipara"][0]["indiparams"][0]
            if level == "1":
                return 200, json.dumps(_basic_data_payload("煤炭")).encode()
            return 200, json.dumps({"errorcode": -1, "errmsg": "busy",
                                    "tables": []}).encode()

        from unittest.mock import patch as _patch

        with _patch.object(sw, "_http_api_curl_post", post), \
                _patch.object(sw, "_resolve_access_token", rec.resolve_token):
            out = sw.resolve_sw_main_industry(
                "600188.SH", observation_date="2026-11-18",
                refresh_token="sector-test-refresh-token",
                timeout_seconds=10.0, deadline=None,
            )
        assert out["status"] == "confirmed"
        assert out["index_code"] == "801950.SL"
        assert out["l2_name"] is None and out["l3_name"] is None
        assert len(rec.calls) == 1  # 仅 L1 出站
        assert out["gaps"] == []

    def test_error_message_redacts_tokens(self, monkeypatch):
        """异常消息中的凭据值一律脱敏（refresh 与本次 access token）。"""
        refresh = "super-secret-refresh-token-value"
        access = "super-secret-access-token-value"
        monkeypatch.setattr(sw, "_resolve_access_token",
                            lambda rt, *, force_refresh=False: access)
        monkeypatch.setattr(
            sw, "_http_api_curl_post",
            lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError(f"boom {access} {refresh}")),
        )
        with pytest.raises(sw.SwSectorFetchError) as excinfo:
            sw._sector_http_post(
                "https://quantapi.51ifind.com/api/v1/x", {}, label="t",
                refresh_token=refresh, timeout_seconds=10.0, deadline=None)
        message = str(excinfo.value)
        assert refresh not in message
        assert access not in message
        assert "boom" in message

    def test_vendor_errmsg_redacts_tokens(self, monkeypatch):
        """供应商 errmsg 复制进消息前同样脱敏。"""
        refresh = "super-secret-refresh-token-value"
        access = "super-secret-access-token-value"
        monkeypatch.setattr(sw, "_resolve_access_token",
                            lambda rt, *, force_refresh=False: access)
        monkeypatch.setattr(
            sw, "_http_api_curl_post",
            lambda *a, **k: (200, json.dumps({
                "errorcode": -1,
                "errmsg": f"bad request token={access} {refresh}",
                "tables": [],
            }).encode()),
        )
        with pytest.raises(sw.SwSectorFetchError) as excinfo:
            sw._sector_http_post(
                "https://quantapi.51ifind.com/api/v1/x", {}, label="t",
                refresh_token=refresh, timeout_seconds=10.0, deadline=None)
        message = str(excinfo.value)
        assert refresh not in message and access not in message
        assert "bad request" in message


# ---------------------------------------------------------------------------
# 畸形 wire 形状保守降级（不逃出边界成未处理 500）
# ---------------------------------------------------------------------------


class TestMalformedWireShapes:
    def test_non_dict_table_rows_skipped(self, recorder):
        recorder({"real_time_quotation": {
            "errorcode": 0, "errmsg": "", "tables": ["bad", 3, None],
        }})
        with pytest.raises(sw.SwSectorFetchError):
            sw.fetch_index_quotes(
                "801950.SL", refresh_token="sector-test-refresh-token",
                timeout_seconds=10.0, deadline=None,
            )

    def test_missing_table_key_rows_degrade_to_defaults(self, recorder):
        """缺 table/time 键的行保留身份但数值全缺（不在 wire 层崩溃）。"""
        recorder({"real_time_quotation": {
            "errorcode": 0, "errmsg": "",
            "tables": [{"thscode": "801950.SL"}],
        }})
        rows = sw.fetch_index_quotes(
            "801950.SL", refresh_token="sector-test-refresh-token",
            timeout_seconds=10.0, deadline=None,
        )
        assert rows[0]["thscode"] == "801950.SL"
        assert rows[0]["change_pct"] is None
        assert rows[0]["stamp"] is None

    def test_history_non_dict_tables_raise_fetch_error(self, recorder):
        recorder({"cmd_history_quotation": {
            "errorcode": 0, "errmsg": "", "tables": ["oops"],
        }})
        with pytest.raises(sw.SwSectorFetchError):
            sw.fetch_index_history(
                "801950.SL", "2026-10-22", "2026-11-18",
                refresh_token="sector-test-refresh-token",
                timeout_seconds=10.0, deadline=None,
            )

    def test_top_level_non_object_payload_rejected(self, monkeypatch):
        monkeypatch.setattr(sw, "_resolve_access_token",
                            lambda rt, *, force_refresh=False: "stub-access-token-value")
        monkeypatch.setattr(
            sw, "_http_api_curl_post",
            lambda *a, **k: (200, b'["not","an","object"]'),
        )
        with pytest.raises(sw.SwSectorFetchError):
            sw._sector_http_post(
                "https://quantapi.51ifind.com/api/v1/x", {}, label="t",
                refresh_token="sector-test-refresh-token",
                timeout_seconds=10.0, deadline=None)


# ---------------------------------------------------------------------------
# amend 三轮：401/403 有界强制刷新（对齐共享凭据路径）、先脱敏后截断、
# 非标量 errorcode 类型化、重复行等价包含完整时间戳
# ---------------------------------------------------------------------------


class _AuthSequenceStub:
    """按序返回响应的传输替身：记录 force_refresh 与 POST 次数。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.posts = 0
        self.refresh_forced = 0

    def post(self, url, headers, body, *, label):
        self.posts += 1
        outcome = self.responses[min(self.posts - 1, len(self.responses) - 1)]
        return outcome

    def resolve_token(self, refresh_token, *, force_refresh=False):
        if force_refresh:
            self.refresh_forced += 1
        return (f"access-token-{self.refresh_forced}-value-padding"[:40]
                or "stub-access-token-value")


class TestAuthRefreshAndRedaction:
    def test_401_forces_single_refresh_and_retry_succeeds(self, monkeypatch):
        """401 → 强制续用一次并重试同一请求一次 → 200 采纳（对齐共享凭据路径）。"""
        stub = _AuthSequenceStub([(401, b'{"errorcode":401}'), (200, json.dumps({
            "errorcode": 0, "errmsg": "", "tables": [],
        }).encode())])
        monkeypatch.setattr(sw, "_http_api_curl_post", stub.post)
        monkeypatch.setattr(sw, "_resolve_access_token", stub.resolve_token)
        payload = sw._sector_http_post(
            "https://quantapi.51ifind.com/api/v1/x", {}, label="t",
            refresh_token="sector-test-refresh-token",
            timeout_seconds=10.0, deadline=None)
        assert payload["errorcode"] == 0
        assert stub.posts == 2
        assert stub.refresh_forced == 1

    def test_403_rejected_after_refresh_is_typed_error(self, monkeypatch):
        """刷新重试仍 403：类型化认证失败消息，指向 refresh_token/权限。"""
        stub = _AuthSequenceStub([(403, b"{}"), (403, b"{}")])
        monkeypatch.setattr(sw, "_http_api_curl_post", stub.post)
        monkeypatch.setattr(sw, "_resolve_access_token", stub.resolve_token)
        with pytest.raises(sw.SwSectorFetchError) as excinfo:
            sw._sector_http_post(
                "https://quantapi.51ifind.com/api/v1/x", {}, label="t",
                refresh_token="sector-test-refresh-token",
                timeout_seconds=10.0, deadline=None)
        assert stub.posts == 2
        assert stub.refresh_forced == 1  # 有界：只强制刷新一次
        message = str(excinfo.value)
        assert "IFIND_HTTP_REFRESH_TOKEN" in message
        assert "access-token-" not in message  # 凭据不出现

    def test_redaction_happens_before_truncation_boundary(self, monkeypatch):
        """跨截断边界（120/80）的长回显先脱敏后截断：部分 token 也不泄漏。"""
        refresh = "super-secret-refresh-token-value"
        access = "super-secret-access-token-value"
        monkeypatch.setattr(sw, "_resolve_access_token",
                            lambda rt, *, force_refresh=False: access)
        # token 起点在 120 截断边界附近：旧实现（先 [:120] 后脱敏）会泄漏前缀
        leaky = "x" * 115 + access
        monkeypatch.setattr(
            sw, "_http_api_curl_post",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError(leaky)),
        )
        with pytest.raises(sw.SwSectorFetchError) as excinfo:
            sw._sector_http_post(
                "https://quantapi.51ifind.com/api/v1/x", {}, label="t",
                refresh_token=refresh, timeout_seconds=10.0, deadline=None)
        message = str(excinfo.value)
        assert access not in message
        assert access[:8] not in message  # 跨边界的部分回显同样不在
        assert refresh not in message

    def test_long_errmsg_echo_redacted_then_capped(self, monkeypatch):
        """errmsg 长回显（含 token，>80/160 边界）：先脱敏后有界截断。"""
        refresh = "super-secret-refresh-token-value"
        access = "super-secret-access-token-value"
        monkeypatch.setattr(sw, "_resolve_access_token",
                            lambda rt, *, force_refresh=False: access)
        long_errmsg = "a" * 100 + refresh + "b" * 100
        monkeypatch.setattr(
            sw, "_http_api_curl_post",
            lambda *a, **k: (200, json.dumps({
                "errorcode": -1, "errmsg": long_errmsg, "tables": [],
            }).encode()),
        )
        with pytest.raises(sw.SwSectorFetchError) as excinfo:
            sw._sector_http_post(
                "https://quantapi.51ifind.com/api/v1/x", {}, label="t",
                refresh_token=refresh, timeout_seconds=10.0, deadline=None)
        message = str(excinfo.value)
        assert refresh not in message and refresh[:8] not in message
        assert access not in message
        assert len(message) <= 200  # 有界（160 消息上限 + 前缀余量）

    def test_non_scalar_errorcode_typed_not_echoed(self, monkeypatch):
        """恶意非标量 errorcode（dict）：只暴露类型名，内容不入消息。"""
        access = "stub-access-token-value"
        monkeypatch.setattr(sw, "_resolve_access_token",
                            lambda rt, *, force_refresh=False: access)
        malicious = {"boom": "x" * 500, "tokens": access}
        monkeypatch.setattr(
            sw, "_http_api_curl_post",
            lambda *a, **k: (200, json.dumps({
                "errorcode": malicious, "errmsg": "", "tables": [],
            }).encode()),
        )
        with pytest.raises(sw.SwSectorFetchError) as excinfo:
            sw._sector_http_post(
                "https://quantapi.51ifind.com/api/v1/x", {}, label="t",
                refresh_token="sector-test-refresh-token",
                timeout_seconds=10.0, deadline=None)
        message = str(excinfo.value)
        assert "dict" in message
        assert "boom" not in message
        assert access not in message


class TestDuplicateStampEquality:
    def test_identical_rows_same_stamp_keep_one_both_orders(self):
        row = {"thscode": "801950.SL", "trade_date": "2026-11-18",
               "trade_time": "22:00:46", "stamp": _dt(2026, 11, 18, 22, 0, 46),
               "change_pct": -0.867, "latest": 2520.109, "pre_close": 2542.15}
        dup = dict(row)
        qualified_a, notes_a = sw.qualify_quote_rows(
            [row, dup], session_date="2026-11-18",
            received_at=_dt(2026, 11, 19, 1, 35))
        qualified_b, notes_b = sw.qualify_quote_rows(
            [dup, row], session_date="2026-11-18",
            received_at=_dt(2026, 11, 19, 1, 35))
        assert set(qualified_a) == {"801950.SL"} == set(qualified_b)
        assert notes_a == [] and notes_b == []

    def test_rows_differing_only_in_stamp_reject_both_orders(self):
        """仅完整时间戳不同的重复行：矛盾（首行序无关），整码保守拒绝。

        单行自身合格（涨跌幅与 latest/preClose 的 Decimal 推算一致），
        冲突只来自时间戳矛盾——拒绝理由不被字段不合格混淆。"""
        base = {"thscode": "801120.SL", "trade_date": "2026-11-18",
                "trade_time": "22:00:46",
                "change_pct": -1.006, "latest": 3543.0, "pre_close": 3579.0}
        early = dict(base, stamp=_dt(2026, 11, 18, 15, 1, 0))
        late = dict(base, stamp=_dt(2026, 11, 18, 22, 0, 46))
        single, _notes = sw.qualify_quote_rows(
            [dict(late)], session_date="2026-11-18",
            received_at=_dt(2026, 11, 19, 1, 35))
        assert set(single) == {"801120.SL"}  # 单独看每行都合格
        for pair in ([early, late], [late, early]):
            qualified, notes = sw.qualify_quote_rows(
                pair, session_date="2026-11-18",
                received_at=_dt(2026, 11, 19, 1, 35))
            assert qualified == {}
            assert notes and "保守拒绝" in notes[0]
