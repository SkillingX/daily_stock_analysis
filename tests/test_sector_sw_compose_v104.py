# -*- coding: utf-8 -*-
"""二级同业与全成分成交额名次编排接线（issue #104）：阶段门控、门槛降级、
失败隔离与载荷契约。

测试缝（与既有板块测试约定一致）：iFinD 传输在模块缝替换（按端点/指数
路由，响应形状取自真实脱敏样本）；交易日历用真实 exchange-calendars；
支柱/核验/严格载荷校验（SectorReportPayload）真实执行。L1 成分链用已
验收受控事实构造（#100 覆盖其编排），L2 阶段走真实代码路径。

覆盖：
- 干净链（名单 + 计数一致 + 报价全集）→ ``sector_l2_peers`` 发布
  ``full_constituents`` 名次与双接口核验证据；
- 脏行同表观计数（5 合格 + 1 空日期、计数 5）→ 降级样本范围名次 + 具体
  拒绝缺口（验收 3 反例）；
- 计数查询失败 → 降级样本范围 + 缺口，一级块与完整性不受影响；
- 一只成员缺涨跌幅 → 涨跌幅名次 withhold，全成分成交额名次照常（验收 4）；
- 取数预算耗尽 → 零出站 + 缺口（可选出站隔离）；
- L2 成分查询失败 → 缺口披露、无东财出站、一级事实与完整性不变；
- 存量载荷（无 ``sector_l2_peers``）严格校验读回兼容。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple, Union

import pytest

from src.schemas.sector_analysis import SectorReportPayload
from src.services import sector_analysis_service as svc
from src.services import sector_sw_market as sw
from src.services.sector_analysis_service import (
    ReportBudget,
    _compose_sw_payload,
)

SESSION_DATE = "2026-11-18"
SESSION_OPEN = datetime(2026, 11, 18, 9, 30)
SESSION_CLOSE = datetime(2026, 11, 18, 15, 0)
RECEIVED_AT = datetime(2026, 11, 18, 23, 0, 0)
TREND_DATES = [
    "2026-10-22", "2026-10-23", "2026-10-26", "2026-10-27", "2026-10-28",
    "2026-10-29", "2026-10-30", "2026-11-02", "2026-11-03", "2026-11-04",
    "2026-11-05", "2026-11-06", "2026-11-09", "2026-11-10", "2026-11-11",
    "2026-11-12", "2026-11-13", "2026-11-16", "2026-11-17", "2026-11-18"]

TARGET = "600188"
TARGET_SEC = "600188.SH"
L1_INDEX = "801950.SL"     # 煤炭（一级）
L2_INDEX = "801951.SL"     # 煤炭开采（二级，核定目录映射）
SW31_CODES = [c for c, _ in sw.SW_L1_DIRECTORY]
COAL_MEMBERS = ["000552.SZ", "000723.SZ", "600188.SH", "601225.SH", "601898.SH"]
COAL_NAMES = ["甘肃能化", "美锦能源", "兖矿能源", "陕西煤业", "中国神华"]
# L2 成员行情（600188 兖矿能源 9.0e8 居首）
L2_AMOUNTS = {"000552.SZ": 3.0e8, "000723.SZ": 5.0e8, "600188.SH": 9.0e8,
              "601225.SH": 7.0e8, "601898.SH": 1.0e9}


def _budget(seconds: float = 120.0) -> ReportBudget:
    return ReportBudget(seconds, finish_margin=min(5.0, seconds / 2.0))


def _expired_budget() -> ReportBudget:
    """取数预算已耗尽（deadline 与收尾余量抵消）的报告预算。"""
    return ReportBudget(10.0, finish_margin=10.0)


# ---------------------------------------------------------------------------
# 受控传输替身（按端点与指数路由）
# ---------------------------------------------------------------------------


def _members_payload(codes: List[str], names: Optional[List[str]] = None,
                     dates: Optional[List[str]] = None) -> Dict[str, Any]:
    if names is None:
        names = [f"成员{c[:6]}" for c in codes]
    if dates is None:
        dates = [SESSION_DATE] * len(codes)
    return {"errorcode": 0, "errmsg": "", "tables": [{
        "table": {"p03473_f001": dates, "p03473_f002": codes,
                  "p03473_f003": names},
    }]}


def _count_response(codes: str, count: int, ymd: str) -> Dict[str, Any]:
    return {
        "errorcode": 0, "errmsg": "",
        "tables": [{"thscode": codes,
                    "table": {"ths_constituent_num_index": [count]}}],
        "datatype": [{"itemid": "ths_constituent_num_index",
                      "type": "DT_INTEGER"}],
        "inputParams": {"jsonrpc": True, "params": [{
            "function": "ths_constituent_num_index", "id": "652793",
            "params": [
                {"name": "THSCODE", "system": "false", "value": ""},
                {"name": "FDATE", "system": "false", "value": ymd},
                {"name": "FDIR", "system": "false", "value": ""},
            ],
        }]},
        "dataVol": 1,
    }


def _quote_row(thscode: str, change: Optional[float],
               amount: Optional[Union[float, str]]) -> Dict[str, Any]:
    """amount 允许测试序列化占位 str（l2_quote_literal 字面注入用）。"""
    latest = round(100.0 * (1 + (change or 0.0) / 100.0), 3)
    cells: Dict[str, List[Any]] = {
        "amount": [amount], "latest": [latest], "preClose": [100.0],
        "tradeTime": ["22:00:46"], "turnoverRatio": [2.0],
        "riseCount": [20.0], "fallCount": [10.0],
    }
    cells["changeRatio"] = [change]  # None 原样进 wire（缺失语义）
    return {
        "marketCategory": "1", "pricetype": 1, "thscode": thscode,
        "time": [f"{SESSION_DATE} 22:00:46"], "table": cells,
    }


class _Sw104Stub:
    """按端点与指数路由的申万传输替身（#104：p03473 按指数、计数端点）。"""

    def __init__(self, *, l2_members_payload: Optional[Dict[str, Any]] = None,
                 l2_members_error: Optional[Dict[str, Any]] = None,
                 l2_count: Optional[int] = 5,
                 l2_count_error: Optional[Dict[str, Any]] = None,
                 l2_count_raw: Optional[bytes] = None,
                 l2_quote_literal: Optional[Tuple[str, str]] = None,
                 l2_quote_changes: Optional[Dict[str, Optional[float]]] = None,
                 l2_quote_drop: Optional[List[str]] = None,
                 l2_quote_extra_codes: Optional[List[str]] = None) -> None:
        self.l2_members_payload = l2_members_payload
        self.l2_members_error = l2_members_error
        self.l2_count = l2_count
        self.l2_count_error = l2_count_error
        # 原始响应字节（issue #104 复审）：模拟 json.dumps 会失真的计数
        # JSON 字面（如 5.0000000000000001）
        self.l2_count_raw = l2_count_raw
        # 成员金额的原始 JSON 字面（issue #104 五轮）：(成员码, 字面)——
        # json.dumps 会把 1e-400 失真为 0.0，以占位符+文本替换注入
        self.l2_quote_literal = l2_quote_literal
        self.l2_quote_changes = l2_quote_changes or {}
        self.l2_quote_drop = l2_quote_drop or []
        # 名单外干扰行（issue #105 复审 P2 闭环）：供应商响应多带的非本批
        # 请求身份行（资格过滤后隔离，不采用）
        self.l2_quote_extra_codes = l2_quote_extra_codes or []
        self.l2_data_pool_calls = 0
        self.l2_count_calls = 0
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

    def __call__(self, url: str, headers: Dict[str, str],
                 body: Optional[bytes], *, label: str) -> Tuple[int, bytes]:
        assert body is not None
        parsed = json.loads(body.decode())
        self.calls.append((url, parsed))
        if "basic_data_service" in url:
            indicator = parsed["indipara"][0]["indicator"]
            assert indicator == "ths_constituent_num_index"
            assert parsed["codes"] == L2_INDEX, "计数请求必须单指数绑定"
            assert parsed["indipara"][0]["indiparams"] == ["20261118"]
            self.l2_count_calls += 1
            if self.l2_count_error is not None:
                return 200, json.dumps(self.l2_count_error).encode()
            if self.l2_count_raw is not None:
                return 200, self.l2_count_raw
            return 200, json.dumps(_count_response(
                L2_INDEX, self.l2_count or 0, "20261118")).encode()
        if "data_pool" in url:
            iv_zsdm = parsed["functionpara"]["iv_zsdm"]
            if iv_zsdm == L2_INDEX:
                self.l2_data_pool_calls += 1
                if self.l2_members_error is not None:
                    return 200, json.dumps(self.l2_members_error).encode()
                if self.l2_members_payload is not None:
                    return 200, json.dumps(self.l2_members_payload).encode()
                return 200, json.dumps(_members_payload(
                    COAL_MEMBERS, COAL_NAMES)).encode()
            # L1 成分（煤炭 801950.SL）：已验收受控形状
            return 200, json.dumps(_members_payload(
                COAL_MEMBERS, COAL_NAMES)).encode()
        if "real_time_quotation" in url:
            codes = parsed["codes"].split(",")
            if codes == SW31_CODES:
                rows = [_quote_row(c, -0.87 if c == L1_INDEX else 0.5, 5.0e8)
                        for c in SW31_CODES]
                return 200, json.dumps(
                    {"errorcode": 0, "errmsg": "", "tables": rows}).encode()
            rows = []
            for c in codes:
                if c in self.l2_quote_drop:
                    continue
                amt = ("__AMT_LITERAL__"
                       if (self.l2_quote_literal and c == self.l2_quote_literal[0])
                       else L2_AMOUNTS.get(c, 5.0e8))
                rows.append(_quote_row(
                    c, self.l2_quote_changes.get(c, 1.0), amt))
            rows.extend(_quote_row(c, 0.4, 6.0e8)
                        for c in self.l2_quote_extra_codes)
            body = {"errorcode": 0, "errmsg": "", "tables": rows}
            if self.l2_quote_literal:
                text = json.dumps(body).replace(
                    '"__AMT_LITERAL__"', self.l2_quote_literal[1])
                return 200, text.encode()
            return 200, json.dumps(body).encode()
        if "cmd_history_quotation" in url:
            rcodes = parsed["codes"].split(",")
            tables = [{"thscode": c, "time": list(TREND_DATES),
                       "table": {"close": [2000.0 + i for i in range(20)]}}
                      for c in rcodes]
            return 200, json.dumps(
                {"errorcode": 0, "errmsg": "", "tables": tables}).encode()
        raise AssertionError(f"未预置的申万端点: {url}")

    @staticmethod
    def resolve_token(refresh_token: str, *, force_refresh: bool = False) -> str:
        return "stub-access-token-value"


_ACTIVE_STUB: Optional[_Sw104Stub] = None


def _stub_post(url: str, headers: Dict[str, str],
               body: Optional[bytes], *, label: str) -> Tuple[int, bytes]:
    assert _ACTIVE_STUB is not None
    return _ACTIVE_STUB(url, headers, body, label=label)


@pytest.fixture(autouse=True)
def _offline_seams(monkeypatch):
    """单元编排离线缝：参考链/多窗口/会话钟受控，不读本地凭据、不出站。"""
    monkeypatch.setattr(sw, "sw_http_config",
                        lambda: ("sector-test-refresh-token", 10.0))
    monkeypatch.setattr(
        sw, "resolve_sw_reference_levels",
        lambda secucode, *, observation_date, refresh_token,
        timeout_seconds, deadline: {
            "l2_name": "煤炭开采", "l3_name": "动力煤", "gaps": []})
    monkeypatch.setattr(
        "src.services.sector_analysis_service._sw_trend_windows_pillar",
        lambda *a, **k: {"windows": [], "gaps": []})
    ctx = svc.SessionContext(session_date=date(2026, 11, 18),
                             session_open=SESSION_OPEN,
                             session_close=SESSION_CLOSE)
    monkeypatch.setattr(svc, "_session_context", lambda now=None: ctx)
    monkeypatch.setattr(svc, "_received_at_now", lambda: RECEIVED_AT)


@pytest.fixture()
def transport(monkeypatch):
    def _install(stub: _Sw104Stub) -> _Sw104Stub:
        global _ACTIVE_STUB
        _ACTIVE_STUB = stub
        monkeypatch.setattr(sw, "_http_api_curl_post", _stub_post)
        monkeypatch.setattr(sw, "_resolve_access_token",
                            _Sw104Stub.resolve_token)
        return stub
    return _install


# ---------------------------------------------------------------------------
# 受控 L1 事实（#100 已验收形状；L2 阶段走真实代码路径）
# ---------------------------------------------------------------------------


def _l1_constituents() -> Dict[str, Any]:
    member_codes = COAL_MEMBERS
    members = {
        "member_codes": member_codes,
        "member_names": {c: n for c, n in zip(COAL_MEMBERS, COAL_NAMES)},
        "dates_seen": [SESSION_DATE], "rows_total": len(member_codes),
        "completeness_evidence": None, "gaps": [],
    }
    quotes = {c: {"thscode": c, "trade_date": SESSION_DATE,
                  "trade_time": "22:00:46", "stamp": None,
                  "change_pct": 1.0, "amount": 5.0e8} for c in member_codes}
    return {
        "membership": sw.classify_membership(
            TARGET_SEC, members, observation_date=SESSION_DATE),
        "members": members,
        "member_quotes": quotes, "member_quote_gaps": [],
        "target_quote": None, "target_quote_gaps": [],
        "target_name": "兖矿能源",
    }


def _confirmed_chain(with_session: bool = True) -> Dict[str, Any]:
    chain = {
        "identity": {
            "status": "confirmed",
            "classification_system": "申万 2021 一级行业",
            "classification_level": "L1",
            "classification_version": sw.SW_L1_DIRECTORY_VERSION,
            "index_code": L1_INDEX, "index_name": "煤炭",
            "vendor_as_of": SESSION_DATE, "l2_name": "煤炭开采",
            "l3_name": "动力煤", "gaps": [],
        },
        "snapshot": {"session_date": SESSION_DATE,
                     "rows": {L1_INDEX: {
                         "thscode": L1_INDEX, "trade_date": SESSION_DATE,
                         "trade_time": "22:00:46", "change_pct": -0.87}},
                     "reference_date": None, "reference_rows": {},
                     "target_date_valid": 31, "dates_seen": [SESSION_DATE],
                     "gaps": []},
        "trend": {"eligible": True, "window_completed_sessions": 20,
                  "interval_count": 19, "start_date": TREND_DATES[0],
                  "end_date": TREND_DATES[-1], "expected_dates": [],
                  "qualified_sessions": 20, "observations": [],
                  "period_return_pct": -0.62, "benchmark": None,
                  "relative_return_pp": None,
                  "source": "iFinD 标准HTTP指数日线",
                  "observed_at": "2026-11-18T23:00:00", "gaps": []},
        "quote_target_ok": True, "trend_ok": True, "gaps": [],
        "constituents": _l1_constituents(),
    }
    if with_session:
        chain["session"] = svc.SessionContext(
            session_date=date(2026, 11, 18), session_open=SESSION_OPEN,
            session_close=SESSION_CLOSE)
    return chain


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


class TestL2PeersWiring:
    def test_clean_chain_publishes_full_constituents_rank(
            self, transport) -> None:
        """干净链：L2 块发布全成分名次 + 双接口核验证据，载荷严格校验过。"""
        stub = transport(_Sw104Stub())
        payload = _compose_sw_payload(TARGET, "兖矿能源", _confirmed_chain(),
                                      budget=_budget())
        l2 = payload["sector_l2_peers"]
        assert l2 is not None
        assert l2["eligible"] is True
        assert l2["industry_reference"] is False
        assert l2["source"] is not None and L2_INDEX in json.dumps(
            stub.calls[0][1])  # p03473 请求绑定 L2 指数
        assert stub.l2_data_pool_calls == 1  # 单指数绑定：恰好一次成分请求
        assert stub.l2_count_calls == 1
        assert l2["target_amount_rank"] == 2  # 601898 1.0e9 居首、兖矿 9.0e8 次之
        assert l2["amount_rank_denominator"] == 5
        assert l2["amount_rank_scope"] == "full_constituents"
        assert "双接口" in (l2["amount_rank_evidence"] or "")
        codes = [i["code"] for i in l2["items"]]
        assert codes[0] == "601898" and TARGET in codes and len(codes) == 5
        # 一级既有块不受影响（无新字段值）
        l1_peers = payload["sector_peers"]
        assert l1_peers is not None and l1_peers["eligible"] is True
        assert l1_peers.get("target_amount_rank") is None
        SectorReportPayload.model_validate(payload)

    def test_dirty_extra_row_same_count_degrades_to_sample(
            self, transport) -> None:
        """验收 3 反例：5 条合格 + 1 条空日期、计数恰为 5——表观吻合不豁免
        脏行，名次降级样本范围，拒绝原因随载荷缺口透出。"""
        dates = [SESSION_DATE] * 5 + [""]
        stub = transport(_Sw104Stub(l2_members_payload=_members_payload(
            COAL_MEMBERS + ["601699.SH"],
            COAL_NAMES + ["潞安环能"], dates=dates)))
        payload = _compose_sw_payload(TARGET, "兖矿能源", _confirmed_chain(),
                                      budget=_budget())
        l2 = payload["sector_l2_peers"]
        assert l2 is not None
        assert l2["target_amount_rank"] is not None  # 样本名次照常
        assert l2["amount_rank_scope"] == "qualified_sample"
        assert l2["amount_rank_evidence"] is None
        assert any("空日期" in g for g in payload["gaps"])
        SectorReportPayload.model_validate(payload)

    def test_count_failure_degrades_to_sample_discloses_gap(
            self, transport) -> None:
        """计数查询失败：降级样本范围名次 + 缺口；一级块与完整性不变。"""
        stub = transport(_Sw104Stub(l2_count_error={
            "errorcode": -1, "errmsg": "mock 计数失败", "tables": []}))
        payload = _compose_sw_payload(TARGET, "兖矿能源", _confirmed_chain(),
                                      budget=_budget())
        l2 = payload["sector_l2_peers"]
        assert l2 is not None
        assert l2["amount_rank_scope"] == "qualified_sample"
        assert any("计数查询失败" in g or "计数" in g for g in payload["gaps"])
        # 一级事实与完整性不受 L2 核验失败影响（理由无 L2 字样）
        reasons = payload["assessment"]["reasons"]
        assert not any("二级同业" in r for r in reasons)
        assert payload["research_basis"] == "sw2021_l1"
        SectorReportPayload.model_validate(payload)

    def test_missing_change_member_keeps_full_amount_rank(
            self, transport) -> None:
        """验收 4：一只成员缺同日涨跌幅——涨跌幅名次 withhold + 有效覆盖
        缺口，已符合资格的全成分成交额名次照常发布。"""
        stub = transport(_Sw104Stub(l2_quote_changes={"000552.SZ": None}))
        payload = _compose_sw_payload(TARGET, "兖矿能源", _confirmed_chain(),
                                      budget=_budget())
        l2 = payload["sector_l2_peers"]
        assert l2 is not None
        assert l2["target_change_pct_rank"] is None
        assert any("涨跌幅缺失" in g for g in l2["gaps"])
        assert l2["target_amount_rank"] == 2
        assert l2["amount_rank_scope"] == "full_constituents"
        SectorReportPayload.model_validate(payload)

    def test_expired_budget_no_outbound(self, transport) -> None:
        """取数预算耗尽：零出站、块缺席、缺口随载荷透出。"""
        stub = transport(_Sw104Stub())
        payload = _compose_sw_payload(TARGET, "兖矿能源", _confirmed_chain(),
                                      budget=_expired_budget())
        assert payload["sector_l2_peers"] is None
        assert stub.l2_data_pool_calls == 0 and stub.l2_count_calls == 0
        assert any("二级同业" in g and "预算" in g for g in payload["gaps"])
        SectorReportPayload.model_validate(payload)

    def test_l2_members_failure_isolated_no_fallback(self, transport) -> None:
        """L2 成分查询失败：缺口披露、东财零出站（stub 无东财路由，误发即
        AssertionError）、一级事实与完整性判定不变。"""
        stub = transport(_Sw104Stub(l2_members_error={
            "errorcode": -3, "errmsg": "mock 成分失败", "tables": []}))
        payload = _compose_sw_payload(TARGET, "兖矿能源", _confirmed_chain(),
                                      budget=_budget())
        assert payload["research_basis"] == "sw2021_l1"
        assert payload["research_outcome"] == "kept_sw_stage"
        l2 = payload["sector_l2_peers"]
        assert l2 is not None and l2["items"] == []
        assert any("二级同业" in g or "成分" in g for g in payload["gaps"])
        reasons = payload["assessment"]["reasons"]
        assert not any("二级" in r for r in reasons)
        # 一级块（受控事实）照常
        assert payload["sector_peers"]["eligible"] is True
        SectorReportPayload.model_validate(payload)

    def test_missing_session_no_stage(self, transport) -> None:
        """会话上下文缺失（受控链）：stage 不发起，块缺席 + 缺口。"""
        stub = transport(_Sw104Stub())
        payload = _compose_sw_payload(TARGET, "兖矿能源",
                                      _confirmed_chain(with_session=False),
                                      budget=_budget())
        assert payload["sector_l2_peers"] is None
        assert stub.l2_data_pool_calls == 0
        assert any("二级同业" in g for g in payload["gaps"])

    def test_l2_identity_missing_keeps_l1_and_skips_stage(
            self, monkeypatch, transport) -> None:
        """二级身份未确认（参考链失败）：阶段零出站，一级事实照常。"""
        stub = transport(_Sw104Stub())
        monkeypatch.setattr(
            sw, "resolve_sw_reference_levels",
            lambda secucode, *, observation_date, refresh_token,
            timeout_seconds, deadline: {
                "l2_name": None, "l3_name": None,
                "gaps": ["参考查询失败（mock）"]})
        payload = _compose_sw_payload(TARGET, "兖矿能源", _confirmed_chain(),
                                      budget=_budget())
        assert payload["sector_l2_peers"] is None
        assert stub.l2_data_pool_calls == 0
        assert payload["sector_l2_identity"]["status"] == "missing"
        assert payload["research_basis"] == "sw2021_l1"
        SectorReportPayload.model_validate(payload)


class TestLegacyReadback:
    def test_payload_without_l2_peers_validates(self) -> None:
        """存量 #103 形状载荷（无 sector_l2_peers）严格校验读回兼容。"""
        payload = _compose_sw_payload(TARGET, "兖矿能源", _confirmed_chain(),
                                      budget=_expired_budget())
        payload.pop("sector_l2_peers")
        validated = SectorReportPayload.model_validate(payload)
        assert validated.sector_l2_peers is None


class TestL2PeersMarkdownRendering:
    """Markdown 渲染与存档同源：Web/Markdown 从同一载荷读（trace 口径）。"""

    def _render(self, payload: Dict[str, Any]) -> str:
        from src.services.sector_analysis_service import _render

        return _render("兖矿能源", TARGET, payload["generated_at"],
                       "sa_render_v104", payload)

    def test_clean_chain_renders_rank_scope_and_evidence(self, transport) -> None:
        """干净链：二级同业表 + 全成分名次 + 核验证据 + 覆盖统计渲染。"""
        transport(_Sw104Stub())
        payload = _compose_sw_payload(TARGET, "兖矿能源", _confirmed_chain(),
                                      budget=_budget())
        md = self._render(payload)
        assert "二级同业对照" in md
        assert "煤炭开采" in md and L2_INDEX in md
        # 覆盖统计（观察成分 / 有效报价含已知零 / 正额可排名 / 样本 /
        # 涨跌幅覆盖——issue #104 复审：覆盖与分母分开表达）
        assert "观察成分 5 行" in md
        assert ("有效成交额报价 5 行（含已知零额 0，正额可排名 5 行）" in md)
        assert "有效涨跌幅覆盖 5/5" in md
        assert "正额名次分母" in md or "分母只取正额可排名行" in md
        # 已验证成分数（核验事实独立于名次发布）
        assert "已验证成分数：5" in md and "双接口核验一致" in md
        # 名次行：范围 + 分母 + 名次
        assert "第 **2** / 5" in md
        assert "全成分正额范围" in md
        # 双接口核验证据
        assert "全成分核验" in md and "双接口" in md
        # 成员行情时点（供应商时间戳口径）
        assert "2026-11-18" in md

    def test_full_universe_survives_out_of_batch_noise_row(
            self, transport) -> None:
        """全额 + 供应商多带 1 行名单外身份（issue #105 复审 P2）：行被
        资格过滤隔离、不采用，全量资格不因干扰行降级——双接口证据照发，
        隔离诊断随缺口可见。"""
        stub = transport(_Sw104Stub(l2_quote_extra_codes=["999999.SZ"]))
        payload = _compose_sw_payload(TARGET, "兖矿能源", _confirmed_chain(),
                                      budget=_budget())
        l2 = payload["sector_l2_peers"]
        assert l2 is not None
        assert l2["amount_rank_scope"] == "full_constituents"
        assert l2["amount_rank_evidence"] is not None
        assert l2["target_amount_rank"] is not None
        assert any("非本批请求身份" in g for g in l2["gaps"])
        SectorReportPayload.model_validate(payload)
        assert stub.l2_data_pool_calls >= 1

    def test_verified_constituent_num_survives_quote_gate_failure(
            self, transport) -> None:
        """复审：行情失败（门槛 4 拒）——名次降级样本范围，已验证成分数
        与适用缺口照常渲染（已核实名单事实独立于报价状态）。"""
        transport(_Sw104Stub(l2_quote_drop=["000552.SZ"]))
        payload = _compose_sw_payload(TARGET, "兖矿能源", _confirmed_chain(),
                                      budget=_budget())
        l2 = payload["sector_l2_peers"]
        assert l2 is not None
        assert l2["amount_rank_scope"] == "qualified_sample"
        assert l2["verified_constituent_num"] == 5
        md = self._render(payload)
        assert "已验证成分数：5" in md
        assert "有效正额样本范围（全成分门槛未满足）" in md
        assert any("不可判定" in g for g in l2["gaps"])
        SectorReportPayload.model_validate(payload)

    def test_degraded_sample_scope_rendered_as_sample(self, transport) -> None:
        """核验拒绝（脏行）：名次行标注样本范围 + 拒绝缺口渲染；门槛 3
        未过时无已验证成分数行（名单完整性不可证）。"""
        dates = [SESSION_DATE] * 5 + [""]
        transport(_Sw104Stub(l2_members_payload=_members_payload(
            COAL_MEMBERS + ["601699.SH"], COAL_NAMES + ["潞安环能"],
            dates=dates)))
        payload = _compose_sw_payload(TARGET, "兖矿能源", _confirmed_chain(),
                                      budget=_budget())
        md = self._render(payload)
        assert "有效正额样本范围（全成分门槛未满足）" in md
        assert "空日期" in md
        assert "已验证成分数" not in md

    def test_stage_gaps_rendered_when_chain_not_started(self, transport) -> None:
        """整链未发起（预算耗尽）：整链缺口行渲染。"""
        transport(_Sw104Stub())
        payload = _compose_sw_payload(TARGET, "兖矿能源", _confirmed_chain(),
                                      budget=_expired_budget())
        md = self._render(payload)
        assert "二级同业未取得" in md
        assert "预算" in md

    def test_legacy_payload_renders_without_l2_section(self, transport) -> None:
        """旧存档（无 sector_l2_peers 键）：不渲染二级子段标题。"""
        transport(_Sw104Stub())
        payload = _compose_sw_payload(TARGET, "兖矿能源", _confirmed_chain(),
                                      budget=_expired_budget())
        payload.pop("sector_l2_peers", None)
        payload.pop("sector_l2_peers_gaps", None)
        md = self._render(payload)
        assert "二级同业对照" not in md


class TestCountPrecisionWiring:
    """issue #104 复审：计数 JSON 字面在二进制浮点解码中的损失不可在
    decode 后补救——真实字节含 5.0000000000000001 时必须拒绝全量资格，
    五成分不得发布 full_constituents 名次（降级样本范围 + 精确缺口）。"""

    def test_imprecise_count_literal_never_publishes_full_rank(
            self, transport) -> None:
        raw = json.dumps(_count_response(L2_INDEX, 5, "20261118")).replace(
            "[5]", "[5.0000000000000001]").encode()
        assert b"5.0000000000000001" in raw  # 字面未失真
        transport(_Sw104Stub(l2_count_raw=raw))
        payload = _compose_sw_payload(TARGET, "兖矿能源", _confirmed_chain(),
                                      budget=_budget())
        l2 = payload["sector_l2_peers"]
        assert l2 is not None
        assert l2["amount_rank_scope"] == "qualified_sample"
        assert l2["amount_rank_evidence"] is None
        assert l2["verified_constituent_num"] is None  # 门槛 3 拒绝
        assert any("含小数" in g or "计数" in g
                   for g in payload["gaps"])
        SectorReportPayload.model_validate(payload)


class TestAmountUnderflowBoundary:
    """issue #104 五轮：wire 字面 ``1e-400`` 经 ``_sw_public_amount`` 下溢为
    ``0.0`` 曾被认成正额（全量 5/5、进入前五），公开转换却发布 0——正
    Decimal 下溢为 0.0 判 invalid；真 0 仍合格计覆盖但不排行。"""

    def test_underflow_literal_never_ranks_or_grants_full(
            self, transport) -> None:
        """目标金额字面 1e-400：不发正额名次、不发错误全量资格；核实成分
        数、正额同行、合理缺口与其他报告块保留。"""
        raw = json.dumps(_count_response(L2_INDEX, 5, "20261118")).encode()
        transport(_Sw104Stub(
            l2_quote_literal=(TARGET_SEC, "1e-400"),
            l2_quote_changes={TARGET_SEC: -0.73}))
        payload = _compose_sw_payload(TARGET, "兖矿能源", _confirmed_chain(),
                                      budget=_budget())
        l2 = payload["sector_l2_peers"]
        assert l2 is not None
        # 目标金额无效：正额名次字段组不发布（不冒充全量 5/5）
        assert l2["target_amount_rank"] is None
        assert l2["amount_rank_denominator"] is None
        assert l2["amount_rank_scope"] is None
        # 全量资格拒（门槛 4：金额状态不可判定），但名单/计数已核实
        assert l2["verified_constituent_num"] == 5
        assert l2["amount_rank_evidence"] is None
        assert l2["industry_reference"] is False
        # 正额同行保留；目标行金额 None（不发布 0 假象）
        target_row = next(i for i in l2["items"] if i["is_target"])
        assert target_row["amount"] is None
        assert any(i["amount"] is not None and not i["is_target"]
                   for i in l2["items"])
        assert any("不可判定" in g for g in l2["gaps"])
        # 其他报告块不受影响（一级事实与完整性判定不变）
        assert payload["research_basis"] == "sw2021_l1"
        assert payload["sector_peers"]["eligible"] is True
        SectorReportPayload.model_validate(payload)

    def test_true_zero_literal_keeps_coverage_and_full_rank(
            self, transport) -> None:
        """真 0（字面 0.0）：合法零观测——全量资格照常（零额计入报价覆盖）、
        名次不涉及该成员、其他成员照常。"""
        zero_member = "000552.SZ"
        transport(_Sw104Stub(l2_quote_literal=(zero_member, "0.0")))
        payload = _compose_sw_payload(TARGET, "兖矿能源", _confirmed_chain(),
                                      budget=_budget())
        l2 = payload["sector_l2_peers"]
        assert l2 is not None
        # 全量资格照常（真 0 可判定，计覆盖）：5 份有效报价 = 4 正额 + 1 零
        assert l2["amount_rank_scope"] == "full_constituents"
        assert l2["verified_constituent_num"] == 5
        assert l2["valid_quote_rows"] == 5
        assert l2["rankable_rows"] == 4
        assert l2["target_amount_rank"] == 2
        assert l2["amount_rank_denominator"] == 4
        # 非目标零额成员不进活跃样本（零额不参与正额前五）
        codes = [i["code"] for i in l2["items"]]
        assert zero_member.split(".")[0] not in codes
        assert any("零" in g for g in l2["gaps"])
        SectorReportPayload.model_validate(payload)
