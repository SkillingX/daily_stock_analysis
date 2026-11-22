# -*- coding: utf-8 -*-
"""申万成分与同业编排接线（issue #100）：市场链扩展、身份回填与载荷契约。

测试缝（与既有板块测试约定一致）：
- ``_sw_market_chain`` 公开市场链缝：成分查询（p03473 同日）、成员行情
  分批出站（real_time_quotation）、目标独立行情仅在未确认成分时出站、
  成分查询失败降缺口不影响核心行情资格与兜底门槛；
- ``_sw_fallback_reason``：成分边界差异本身不触发东财兜底（#98 决策 3）；
- ``_compose_sw_payload``：成分状态回填 identity.membership（含
  sw_membership 三态明细）、sector_peers 块、assessment.reasons 阶段
  标注更新；严格载荷校验（SectorReportPayload）真实执行；
- 存量 v2 载荷（无新字段）读回兼容。

iFinD 传输在模块缝替换（响应形状取自真实脱敏样本）；交易日历用真实
exchange-calendars（冻结时刻取值）；资格判定/渲染/校验不 mock。
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

import pytest

from src.schemas.sector_analysis import SectorReportPayload
from src.services import sector_analysis_service as svc
from src.services import sector_sw_market as sw
from src.services.sector_analysis_service import (
    ReportBudget,
    compose_report,
    _sw_fallback_reason,
    _sw_market_chain,
    _compose_sw_payload,
)

# 模块导入时捕获真实参考链解析（autouse 离线缝会替换模块属性；
# sequencing 用例经此回接真实解析 + 传输替身走实际代码路径）
_REAL_RESOLVE_REFERENCE_LEVELS = sw.resolve_sw_reference_levels

SESSION_DATE = "2026-11-18"
SESSION_OPEN = datetime(2026, 11, 18, 9, 30)
SESSION_CLOSE = datetime(2026, 11, 18, 15, 0)
RECEIVED_AT = datetime(2026, 11, 18, 23, 0, 0)
TREND_DATES = [
    "2026-10-22", "2026-10-23", "2026-10-26", "2026-10-27", "2026-10-28",
    "2026-10-29", "2026-10-30", "2026-11-02", "2026-11-03", "2026-11-04",
    "2026-11-05", "2026-11-06", "2026-11-09", "2026-11-10", "2026-11-11",
    "2026-11-12", "2026-11-13", "2026-11-16", "2026-11-17", "2026-11-18"]


def _budget(seconds: float = 120.0) -> ReportBudget:
    return ReportBudget(seconds, finish_margin=min(5.0, seconds / 2.0))


# ---------------------------------------------------------------------------
# 受控响应（真实 wire 形状）
# ---------------------------------------------------------------------------


def _industry_payload_by_level(code: str, name: str) -> Dict[str, Any]:
    return {"errorcode": 0, "errmsg": "", "tables": [
        {"thscode": code, "time": [],
         "table": {"ths_the_sw_industry_stock": [name]}},
    ]}


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


def _quote_row(thscode: str, change: float, amount: float = 5.0e8) -> Dict[str, Any]:
    latest = round(100.0 * (1 + change / 100.0), 3)
    return {
        "marketCategory": "1", "pricetype": 1, "thscode": thscode,
        "time": [f"{SESSION_DATE} 22:00:46"],
        "table": {
            "changeRatio": [change], "amount": [amount],
            "tradeTime": ["22:00:46"], "latest": [latest], "preClose": [100.0],
            "turnoverRatio": [2.0], "riseCount": [20.0], "fallCount": [10.0],
        },
    }


SW31_CODES = [c for c, _ in sw.SW_L1_DIRECTORY]

COAL_MEMBERS = ["000552.SZ", "000723.SZ", "600188.SH", "601225.SH", "601898.SH"]
BJ_TARGET = "920101"
BJ_TARGET_SEC = "920101.BJ"


class _SwStub:
    """按端点与 codes 内容路由的申万传输替身，记录全部请求。"""

    def __init__(self, *, industry_by_level: Dict[int, str],
                 target_secucode: str = "600188.SH",
                 member_codes: Optional[List[str]] = None,
                 members_payload: Optional[Dict[str, Any]] = None,
                 members_error: Optional[Dict[str, Any]] = None,
                 quotes_error: bool = False,
                 target_quote_change: Optional[float] = 1.5) -> None:
        self.industry_by_level = industry_by_level
        self.target_secucode = target_secucode
        self.member_codes = member_codes if member_codes is not None else COAL_MEMBERS
        self.members_payload = members_payload
        self.members_error = members_error
        self.quotes_error = quotes_error
        self.target_quote_change = target_quote_change
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

    def __call__(self, url: str, headers: Dict[str, str],
                 body: Optional[bytes], *, label: str) -> Tuple[int, bytes]:
        assert body is not None
        parsed = json.loads(body.decode())
        self.calls.append((url, parsed))
        if "basic_data_service" in url:
            level = int(parsed["indipara"][0]["indiparams"][0])
            code = parsed["codes"]
            payload = _industry_payload_by_level(
                code, self.industry_by_level[level])
            return 200, json.dumps(payload).encode()
        if "data_pool" in url:
            if self.members_error is not None:
                return 200, json.dumps(self.members_error).encode()
            if self.members_payload is not None:
                return 200, json.dumps(self.members_payload).encode()
            return 200, json.dumps(_members_payload(
                self.member_codes,
                ["甘肃能化", "美锦能源", "兖矿能源", "陕西煤业", "中国神华"],
            )).encode()
        if "real_time_quotation" in url:
            if self.quotes_error:
                return 200, json.dumps({"errorcode": -3, "errmsg": "timeout",
                                        "tables": []}).encode()
            codes = parsed["codes"].split(",")
            if codes == SW31_CODES:
                rows = [_quote_row(c, -0.87 if c == "801950.SL" else 0.5)
                        for c in SW31_CODES]
                return 200, json.dumps(
                    {"errorcode": 0, "errmsg": "", "tables": rows}).encode()
            if codes == [self.target_secucode]:
                row = _quote_row(self.target_secucode,
                                 self.target_quote_change or 0.0, 4.0e7)
                return 200, json.dumps(
                    {"errorcode": 0, "errmsg": "", "tables": [row]}).encode()
            rows = [_quote_row(c, 1.0) for c in codes]
            return 200, json.dumps(
                {"errorcode": 0, "errmsg": "", "tables": rows}).encode()
        if "cmd_history_quotation" in url:
            codes = parsed["codes"].split(",")
            tables = [{"thscode": c, "time": list(TREND_DATES),
                       "table": {"close": [2000.0 + i for i in range(20)]}}
                      for c in codes]
            return 200, json.dumps(
                {"errorcode": 0, "errmsg": "", "tables": tables}).encode()
        raise AssertionError(f"未预置的申万端点: {url}")

    @staticmethod
    def resolve_token(refresh_token: str, *, force_refresh: bool = False) -> str:
        return "stub-access-token-value"


_ACTIVE_STUB: Optional[_SwStub] = None


def _stub_post_default(url: str, headers: Dict[str, str],
                       body: Optional[bytes], *, label: str) -> Tuple[int, bytes]:
    assert _ACTIVE_STUB is not None
    return _ACTIVE_STUB(url, headers, body, label=label)


@pytest.fixture(autouse=True)
def _offline_reference_levels(monkeypatch):
    """单元编排测试离线缝（spec 复审拆缝）：二/三级参考链出站受控替身。

    保留申万路径的载荷组装会在必需研究后补齐参考链——单元编排测试统一
    走受控解析（不读本地凭据、不真实出站）；真实传输与发起时序由
    TestOptionalReferenceLevelSequencing（真实解析 + 传输替身）与 API
    闭环（v100/v101）覆盖。"""
    monkeypatch.setattr(sw, "sw_http_config",
                        lambda: ("sector-test-refresh-token", 10.0))
    monkeypatch.setattr(
        sw, "resolve_sw_reference_levels",
        lambda secucode, *, observation_date, refresh_token,
        timeout_seconds, deadline: {
            "l2_name": "煤炭开采", "l3_name": "动力煤", "gaps": []})
    # issue #103 多窗口趋势（新可选出站）受控替身：单元编排测试不真实出站
    monkeypatch.setattr(
        "src.services.sector_analysis_service._sw_trend_windows_pillar",
        lambda *a, **k: {"windows": [], "gaps": []})


@pytest.fixture()
def frozen_session(monkeypatch):
    """冻结会话上下文与接收钟（真实交易日历取已完成会话序列）。"""
    ctx = svc.SessionContext(session_date=date(2026, 11, 18),
                             session_open=SESSION_OPEN,
                             session_close=SESSION_CLOSE)
    monkeypatch.setattr(svc, "_session_context", lambda now=None: ctx)
    monkeypatch.setattr(svc, "_received_at_now", lambda: RECEIVED_AT)
    sessions = svc.recent_completed_trading_sessions(
        "cn", 20, current_time=RECEIVED_AT)
    assert sessions and sessions[0].isoformat() == SESSION_DATE
    return ctx


def _run_chain(monkeypatch, frozen_session, stub: _SwStub, code: str) -> Dict[str, Any]:
    global _ACTIVE_STUB
    _ACTIVE_STUB = stub
    try:
        monkeypatch.setattr(sw, "sw_http_config",
                            lambda: ("sector-test-refresh-token", 10.0))
        monkeypatch.setattr(sw, "_http_api_curl_post", _stub_post_default)
        monkeypatch.setattr(sw, "_resolve_access_token", _SwStub.resolve_token)
        # current_time 与夹具同一时钟源：趋势窗口按 RECEIVED_AT 的严格日历
        # 取值（生产缝不冻结；本地 TZ 与 CI UTC runner 下同值确定）
        return _sw_market_chain(code, budget=_budget(),
                                current_time=RECEIVED_AT)
    finally:
        _ACTIVE_STUB = None


def _enrich_chain(monkeypatch, frozen_session, stub: _SwStub,
                  code: str) -> Dict[str, Any]:
    """核心链 + 显式成分富化（编排层判定保留后才发起——单元级断言入口）。"""
    from src.services.sector_analysis_service import _fetch_constituents

    global _ACTIVE_STUB
    _ACTIVE_STUB = stub
    try:
        monkeypatch.setattr(sw, "sw_http_config",
                            lambda: ("sector-test-refresh-token", 10.0))
        monkeypatch.setattr(sw, "_http_api_curl_post", _stub_post_default)
        monkeypatch.setattr(sw, "_resolve_access_token", _SwStub.resolve_token)
        chain = _sw_market_chain(code, budget=_budget(),
                                 current_time=RECEIVED_AT)
        ident = chain["identity"]
        session = chain.get("session")
        assert session is not None, "核心链应携带 session 供保留分支富化"
        chain["constituents"] = _fetch_constituents(
            code, ident["index_code"], chain["snapshot"]["session_date"],
            session, name="测试标的",
            refresh_token="sector-test-refresh-token",
            timeout_seconds=10.0, budget=_budget())
        return chain
    finally:
        _ACTIVE_STUB = None


# ---------------------------------------------------------------------------
# 市场链：成分查询、成员行情、独立行情出站
# ---------------------------------------------------------------------------


class TestMarketChainConstituents:
    def test_included_path_member_quotes_batched_no_independent_outbound(
            self, monkeypatch, frozen_session):
        stub = _SwStub(industry_by_level={1: "煤炭", 2: "煤炭开采", 3: "动力煤"})
        out = _enrich_chain(monkeypatch, frozen_session, stub, "600188")
        cons = out["constituents"]
        assert cons["membership"]["status"] == "included"
        assert cons["membership"]["target_secucode"] == "600188.SH"
        assert "600188.SH" in cons["member_quotes"]
        assert cons["target_quote"] is None
        assert out["quote_target_ok"] is True and out["trend_ok"] is True
        # 出站：3 归属 + 1 快照 + 1 成分 + 1 成员批次（5<150）+ 1 日线；无独立行情
        pool_calls = [p for u, p in stub.calls if "data_pool" in u]
        assert len(pool_calls) == 1
        assert pool_calls[0]["functionpara"]["iv_date"] == "20261118"
        rt_calls = [p for u, p in stub.calls if "real_time_quotation" in u]
        assert len(rt_calls) == 2  # 31 指数快照 + 成员批次
        member_call = rt_calls[1]
        assert member_call["indicators"] == (
            "changeRatio,amount,tradeTime,latest,preClose")
        assert "600188.SH" in member_call["codes"]

    def test_unknown_path_fetches_target_independent_quote(
            self, monkeypatch, frozen_session):
        stub = _SwStub(industry_by_level={1: "煤炭", 2: "煤炭开采", 3: "动力煤"},
                       target_secucode=BJ_TARGET_SEC)
        out = _enrich_chain(monkeypatch, frozen_session, stub, BJ_TARGET)
        cons = out["constituents"]
        assert cons["membership"]["status"] == "unknown"
        tq = cons["target_quote"]
        assert tq is not None and tq["thscode"] == BJ_TARGET_SEC
        assert tq["trade_date"] == SESSION_DATE
        rt_calls = [p for u, p in stub.calls if "real_time_quotation" in u]
        assert len(rt_calls) == 3
        # 独立行情（单次小出站）优先于成员大批次（快照 → 独立行情 → 批次）
        assert rt_calls[1]["codes"] == BJ_TARGET_SEC
        assert rt_calls[2]["codes"].startswith("000552.SZ")

    def test_members_fetch_failure_degrades_membership_not_core(
            self, monkeypatch, frozen_session):
        stub = _SwStub(industry_by_level={1: "煤炭", 2: "煤炭开采", 3: "动力煤"},
                       members_error={"errorcode": -4001, "errmsg": "no data",
                                      "tables": []})
        out = _enrich_chain(monkeypatch, frozen_session, stub, "600188")
        cons = out["constituents"]
        assert cons["membership"]["status"] == "unknown"
        assert cons["membership"]["gaps"]
        assert out["quote_target_ok"] is True and out["trend_ok"] is True
        # 成分失败：成员集为空 → 无成员批次；目标未确认成分 → 独立行情仍尝试
        rt_calls = [p for u, p in stub.calls if "real_time_quotation" in u]
        assert len(rt_calls) == 2  # 31 指数快照 + 目标独立行情
        assert rt_calls[1]["codes"] == "600188.SH"
        assert cons["member_quotes"] == {}
        assert cons["target_quote"] is not None

    def test_constituent_boundary_alone_never_triggers_fallback(
            self, monkeypatch, frozen_session):
        """成分边界差异（unknown）本身不触发东财兜底（#98 决策 3）。"""
        stub = _SwStub(industry_by_level={1: "煤炭", 2: "煤炭开采", 3: "动力煤"},
                       target_secucode=BJ_TARGET_SEC)
        out = _run_chain(monkeypatch, frozen_session, stub, BJ_TARGET)
        assert _sw_fallback_reason(out) is None

    def test_members_empty_still_fetches_independent_bj_quote(
            self, monkeypatch, frozen_session):
        """成分未取得该日成员（全部异日）：membership 保持 unknown，目标
        独立行情仍独立尝试（预算允许），绝不进入成员集合。"""
        stub = _SwStub(industry_by_level={1: "煤炭", 2: "煤炭开采", 3: "动力煤"},
                       target_secucode=BJ_TARGET_SEC)
        stub.members_payload = _members_payload(
            codes=COAL_MEMBERS, dates=["2026-11-17"] * len(COAL_MEMBERS))
        out = _enrich_chain(monkeypatch, frozen_session, stub, BJ_TARGET)
        cons = out["constituents"]
        assert cons["membership"]["status"] == "unknown"
        assert cons["member_quotes"] == {}
        assert cons["target_quote"] is not None
        assert cons["target_quote"]["thscode"] == BJ_TARGET_SEC
        rt_calls = [p for u, p in stub.calls if "real_time_quotation" in u]
        # 快照在前；独立行情（单次小出站）在成员批次之前（成员集空、无批次）
        assert len(rt_calls) == 2
        assert rt_calls[1]["codes"] == BJ_TARGET_SEC

    def test_member_quote_failure_keeps_membership_and_gaps(
            self, monkeypatch, frozen_session):
        stub = _SwStub(industry_by_level={1: "煤炭", 2: "煤炭开采", 3: "动力煤"},
                       quotes_error=True)
        out = _enrich_chain(monkeypatch, frozen_session, stub, "600188")
        cons = out["constituents"]
        assert cons["membership"]["status"] == "included"  # 成分事实独立于行情
        assert cons["member_quotes"] == {}
        assert cons["member_quote_gaps"]

    def test_quote_batches_qualified_against_requested_identities(
            self, monkeypatch, frozen_session):
        """每批行情按请求的完整身份集资格筛选：越界行不采用、跨批不静默覆盖。"""
        stub = _SwStub(industry_by_level={1: "煤炭", 2: "煤炭开采", 3: "动力煤"})
        batch_size_patch = patch.object(svc, "_MEMBER_QUOTE_BATCH_SIZE", 2)
        codes = COAL_MEMBERS  # 6 只 → 3 批（每批 2 只）
        first_batch = codes[0:2]

        real_stub_call = stub.__call__

        def _poisoned_call(url, headers, body, *, label):
            parsed = json.loads(body.decode())
            if ("real_time_quotation" in url
                    and parsed["codes"].split(",") == codes[2:4]):
                # 第 2 批响应混入第 1 批成员（不同值）与一个未请求身份
                rows = [
                    _quote_row(codes[2], 1.0), _quote_row(codes[3], 1.0),
                    _quote_row(first_batch[0], 9.9),   # 跨批重复（不同值）
                    _quote_row("ABC123.SH", 5.0),      # 越界身份
                ]
                return 200, json.dumps(
                    {"errorcode": 0, "errmsg": "", "tables": rows}).encode()
            return real_stub_call(url, headers, body, label=label)

        with batch_size_patch, patch.object(
                sw, "_http_api_curl_post", _poisoned_call), patch.object(
                sw, "_resolve_access_token", _SwStub.resolve_token):
            monkeypatch.setattr(sw, "sw_http_config",
                                lambda: ("sector-test-refresh-token", 10.0))
            from src.services.sector_analysis_service import _fetch_constituents
            out = _sw_market_chain("600188", budget=_budget(),
                                   current_time=RECEIVED_AT)
            out["constituents"] = _fetch_constituents(
                "600188", out["identity"]["index_code"],
                out["snapshot"]["session_date"], out["session"],
                name="测试标的", refresh_token="sector-test-refresh-token",
                timeout_seconds=10.0, budget=_budget())
        cons = out["constituents"]
        quotes = cons["member_quotes"]
        # 先批成员值保留（不被后批不同值静默覆盖）
        assert quotes[first_batch[0]]["change_pct"] == 1.0
        assert codes[2] in quotes and codes[3] in quotes
        assert "ABC123" not in "".join(quotes.keys())
        joined = " ".join(cons["member_quote_gaps"])
        assert "非本批请求身份" in joined
        assert "ABC123" not in joined  # 畸形/越界原始值不回显
        assert "不覆盖" in joined  # 后批越界/跨批行不覆盖先批已采用行


# ---------------------------------------------------------------------------
# 载荷接线：身份回填、同业块、阶段标注
# ---------------------------------------------------------------------------


def _confirmed_chain(constituents: Dict[str, Any]) -> Dict[str, Any]:
    """受控申万链输出（市场块 #99 已验收形状 + constituents）。"""
    return {
        "identity": {
            "status": "confirmed",
            "classification_system": "申万 2021 一级行业",
            "classification_level": "L1",
            "classification_version": sw.SW_L1_DIRECTORY_VERSION,
            "index_code": "801950.SL", "index_name": "煤炭",
            "vendor_as_of": SESSION_DATE, "l2_name": "煤炭开采",
            "l3_name": "动力煤", "gaps": [],
        },
        "snapshot": {"session_date": SESSION_DATE,
                     "rows": {"801950.SL": {
                         "thscode": "801950.SL", "trade_date": SESSION_DATE,
                         "trade_time": "22:00:46", "change_pct": -0.87}},
                     "reference_date": None, "reference_rows": {},
                     "target_date_valid": 31, "dates_seen": [SESSION_DATE],
                     "gaps": []},
        "trend": {"eligible": True, "window_completed_sessions": 20,
                  "interval_count": 19, "start_date": TREND_DATES[0],
                  "end_date": TREND_DATES[-1], "expected_dates": [],
                  "qualified_sessions": 20, "observations": [],
                  "period_return_pct": -0.62, "benchmark": None,
                  "relative_return_pp": None, "source": "iFinD 标准HTTP指数日线",
                  "observed_at": "2026-11-18T23:00:00", "gaps": []},
        "quote_target_ok": True, "trend_ok": True, "gaps": [],
        "constituents": constituents,
    }


def _included_constituents() -> Dict[str, Any]:
    member_codes = COAL_MEMBERS
    quotes = {c: {"thscode": c, "trade_date": SESSION_DATE,
                  "trade_time": "22:00:46", "change_pct": 1.0,
                  "amount": 5.0e8} for c in member_codes}
    return {
        "membership": sw.classify_membership(
            "600188.SH",
            {"member_codes": member_codes,
             "member_names": {c: f"成员{c[:6]}" for c in member_codes},
             "dates_seen": [SESSION_DATE], "rows_total": len(member_codes),
             "completeness_evidence": None, "gaps": []},
            observation_date=SESSION_DATE),
        "members": {"member_codes": member_codes,
                    "member_names": {c: f"成员{c[:6]}" for c in member_codes},
                    "dates_seen": [SESSION_DATE],
                    "rows_total": len(member_codes),
                    "completeness_evidence": None, "gaps": []},
        "member_quotes": quotes, "member_quote_gaps": [],
        "target_quote": None, "target_quote_gaps": [],
        "target_name": "兖矿能源",
    }


class TestPayloadWiring:
    def test_membership_backfill_and_peers_block_included(self):
        payload = _compose_sw_payload("600188", "兖矿能源",
                                      _confirmed_chain(_included_constituents()),
                                      budget=_budget())
        membership = payload["identity"]["membership"]
        assert membership["verified"] is True
        assert membership["verified_source"] == "sw_index_members"
        assert membership["sw_membership"] is not None
        assert membership["sw_membership"]["status"] == "included"
        assert membership["sw_membership"]["observation_date"] == SESSION_DATE
        assert membership["sw_membership"]["index_code"] == "801950.SL"
        peers = payload["sector_peers"]
        assert peers is not None
        assert peers["eligible"] is True
        assert peers["industry_reference"] is False
        assert any(i["is_target"] for i in peers["items"])
        # 研究块随载荷透出（issue #101）：真实支柱在无搜索配置下降级为
        # 有界缺口块，reasons 汇报实际研究缺口（无「待接入」阶段标签）
        reasons = payload["assessment"]["reasons"]
        assert not any("同业比较与指数成分验证" in r for r in reasons)
        assert not any("行业催化与证据化综合" in r for r in reasons)
        assert any("行业催化未取得" in r for r in reasons)
        assert any("证据化综合未产出" in r for r in reasons)
        SectorReportPayload.model_validate(payload)

    def test_bj_unknown_boundary_partial_reasons_and_reference_peers(self):
        cons = _included_constituents()
        cons["membership"] = sw.classify_membership(
            BJ_TARGET_SEC,
            {"member_codes": COAL_MEMBERS, "member_names": {},
             "dates_seen": [SESSION_DATE], "rows_total": len(COAL_MEMBERS),
             "completeness_evidence": None, "gaps": []},
            observation_date=SESSION_DATE)
        cons["member_quotes"] = {c: {"thscode": c, "trade_date": SESSION_DATE,
                                     "trade_time": "22:00:46",
                                     "change_pct": 1.0, "amount": 5.0e8}
                                 for c in COAL_MEMBERS}
        cons["target_quote"] = {"thscode": BJ_TARGET_SEC,
                                "trade_date": SESSION_DATE,
                                "trade_time": "22:00:46", "change_pct": 1.5,
                                "amount": 4.0e7}
        cons["target_name"] = "锦波生物"
        payload = _compose_sw_payload(BJ_TARGET, "锦波生物",
                                      _confirmed_chain(cons),
                                      budget=_budget())
        membership = payload["identity"]["membership"]
        assert membership["verified"] is False
        assert membership["sw_membership"]["status"] == "unknown"
        peers = payload["sector_peers"]
        assert peers["industry_reference"] is True
        assert peers["target_independent_quote"] is not None
        assert peers["target_independent_quote"]["code"] == BJ_TARGET
        assert peers["target_independent_quote"]["as_of_date"] == SESSION_DATE
        assert peers["target_change_pct_rank"] is None
        # 北交所成分边界：报告如实标注部分原因
        reasons = payload["assessment"]["reasons"]
        assert any("成分" in r and ("未知" in r or "未纳入" in r) for r in reasons)
        SectorReportPayload.model_validate(payload)

    def test_constituent_fetch_failure_keeps_report_with_gaps(self):
        cons = _included_constituents()
        cons["membership"] = sw.classify_membership(
            "600188.SH",
            {"member_codes": [], "member_names": {},
             "dates_seen": [], "rows_total": 0,
             "completeness_evidence": None,
             "gaps": ["成分查询失败（errorcode=-4001）"]},
            observation_date=SESSION_DATE)
        cons["members"] = {"member_codes": [], "member_names": {},
                           "dates_seen": [], "rows_total": 0,
                           "completeness_evidence": None,
                           "gaps": ["成分查询失败（errorcode=-4001）"]}
        cons["member_quotes"] = {}
        cons["member_quote_gaps"] = ["成分集合未取得，成员行情未查询"]
        payload = _compose_sw_payload("600188", "兖矿能源",
                                      _confirmed_chain(cons),
                                      budget=_budget())
        assert payload["identity"]["membership"]["verified"] is False
        assert payload["identity"]["membership"]["sw_membership"][
            "status"] == "unknown"
        assert payload["sector_peers"] is not None
        assert payload["sector_peers"]["items"] == []
        assert payload["assessment"]["completeness"] == "partial"
        SectorReportPayload.model_validate(payload)

    def test_v2_payload_without_new_membership_fields_validates(self):
        """存量 v2 载荷（membership 无 sw_membership）严格校验读回兼容。"""
        legacy = {
            "payload_version": 2,
            "stock_code": "600188", "stock_name": "兖矿能源",
            "generated_at": "2026-01-01T10:00:00",
            "identity": {"subject_status": "confirmed",
                         "membership": {"verified": True, "member_count": 33,
                                        "verified_source": "cons_table"}},
            "background": {"policy": {"lean": None, "label": None, "gaps": []},
                           "base_rate": {"is_default": True,
                                         "table_version": "v1.0",
                                         "table_updated": "2026-09-29",
                                         "table_industry_count": 21},
                           "intraday": {"sector_found": True}},
            "concepts": {"items": []},
            "assessment": {"status": "unscored",
                           "completeness": "complete", "reasons": []},
            "identification": {},
            "policy": {},
            "base_rate": {"is_default": True, "table_version": "v1.0",
                          "table_updated": "2026-09-29",
                          "table_industry_count": 21},
            "prosperity": {"sector_found": True},
            "sector_score": None, "band": None, "beta_meaning": None,
            "weights": None, "prosperity_score": None, "gaps": [],
        }
        validated = SectorReportPayload.model_validate(legacy)
        assert validated.identity.membership.sw_membership is None
        assert validated.identity.membership.verified_source == "cons_table"


# ---------------------------------------------------------------------------
# 编排顺序反例（amend）：成分/同业出站只在决定保留申万后发起恰好一次
# ---------------------------------------------------------------------------


def _em_core_stub(subject_status: str = "confirmed",
                  quote_change: Optional[float] = 1.5,
                  trend_return: Optional[float] = None,
                  ident_gaps: Optional[List[str]] = None) -> Dict[str, Any]:
    """东财核心事实缝替身（形状对齐真实 core，取自 #99 编排测试）。"""
    locked_name = "白酒Ⅱ"
    locked_code = "BK1277"
    return {
        "ident": {
            "industry_em": "白酒Ⅱ", "industry_cninfo": "", "business_scope": "",
            "board_name": locked_name, "board_code": locked_code,
            "subject_status": subject_status,
            "membership": {"verified": subject_status == "confirmed",
                           "member_count": 20 if subject_status == "confirmed" else None,
                           "verified_source": ("cons_table" if subject_status == "confirmed"
                                               else None)},
            "observed_at": "2026-11-05T14:00:00", "vendor_as_of": None,
            "classification_system": "东财行业板块", "classification_level": None,
            "classification_version": None,
            "sw_chain": {"l1": "", "l2": "", "l3": "", "l3_code": "", "gaps": []},
            "sector_name": locked_name, "gaps": ident_gaps or [],
        },
        "board_snapshot": None, "cons_snapshot": None, "cons_observed_at": None,
        "cons_batch": None, "f10_members_source": False,
        "confirmed": subject_status == "confirmed",
        "trend": {"eligible": subject_status == "confirmed",
                  "window_completed_sessions": 20, "interval_count": 19,
                  "start_date": None, "end_date": None, "expected_dates": [],
                  "qualified_sessions": None, "observations": [],
                  "period_return_pct": trend_return, "benchmark": None,
                  "relative_return_pp": None, "source": None,
                  "observed_at": "2026-11-05T14:00:02", "gaps": []},
        "prosper": {"sector_found": quote_change is not None,
                    "momentum": None if quote_change is None else 50.0,
                    "activity": 50.0, "amount_activity": 50.0, "breadth": 90.0,
                    "rank_of": 1, "total_boards": 31,
                    "row": {"board_name": locked_name, "board_code": locked_code,
                            "change_pct": quote_change, "turnover_rate": 2.3,
                            "total_market_cap": None, "amount": 3.1e9,
                            "up_count": 18, "down_count": 2,
                            "leader": "", "leader_change_pct": None},
                    "snapshot_source": None, "gaps": []},
    }


class TestOrchestrationConstituentOrder:
    """成分/同业出站时序：切换零被弃出站、保留富化恰好一次、过期不出站。"""

    def _dual_failure_sw(self) -> Dict[str, Any]:
        return _confirmed_chain(_included_constituents()) | {
            "quote_target_ok": False, "trend_ok": False,
            "constituents": None,  # 保留分支由 _enrich_constituents 恰好一次填充
            "session": svc.SessionContext(
                session_date=date(2026, 11, 18),
                session_open=datetime(2026, 11, 18, 9, 30),
                session_close=datetime(2026, 11, 18, 15, 0)),
        }

    def _run(self, monkeypatch, em_core, *, budget=None,
             sw_core=None) -> Any:
        from src.services.sector_analysis_service import (
            _compose_eastmoney_core,
            _concepts_pillar,
            _fetch_constituents,
            _policy_pillar,
            _resolve_sw_chain,
        )

        sw_value = sw_core if sw_core is not None else self._dual_failure_sw()
        enrich_calls = []
        core_calls: List[str] = []

        def _recorder(code, index_code, session_date, session, *, name,
                      refresh_token, timeout_seconds, budget,
                      full_constituent_audit=False,
                      reuse_member_quotes=None):
            enrich_calls.append((code, index_code))
            return {"membership": None, "members": {}, "member_quotes": {},
                    "member_quote_gaps": [], "target_quote": None,
                    "target_quote_gaps": [], "target_name": name}

        def _core_recorder(code, *, budget):
            core_calls.append(code)
            return em_core

        _recorder.calls = enrich_calls
        patches = [
            patch("src.services.sector_analysis_service._sw_market_chain",
                  return_value=sw_value),
            patch("src.services.sector_analysis_service._compose_eastmoney_core",
                  _core_recorder),
            patch("src.services.sector_sw_market.sw_http_config",
                  lambda: ("sector-test-refresh-token", 10.0)),
            patch("src.services.sector_analysis_service._fetch_constituents",
                  _recorder),
            patch("src.services.sector_analysis_service._resolve_sw_chain",
                  lambda code, text, *, budget: {
                      "l1": "", "l2": "", "l3": "", "l3_code": "", "gaps": []}),
            patch("src.services.sector_analysis_service._concepts_pillar",
                  lambda code, *, budget: {
                      "items": [], "source": "ifind", "total_returned": 0,
                      "truncated": False, "gaps": ["受控：概念阶段不执行"]}),
        ]
        for p in patches:
            p.start()
        try:
            payload = compose_report("600188", "兖矿能源",
                                     budget=budget or _budget())
        finally:
            for p in patches:
                p.stop()
        return payload, _recorder

    def test_em_switch_sends_zero_constituent_outbound(
            self, monkeypatch):
        """东财切换成功：成分/成员/独立行情出站为零（零被弃 SW 同业出站），
        二/三级参考链出站同样零发起（可选参考链不进入东财切换路径）。"""

        def _reference_sentinel(*args: Any, **kwargs: Any) -> Dict[str, Any]:
            raise AssertionError("东财切换路径不应发起二/三级归属参考出站")

        with patch("src.services.sector_sw_market.resolve_sw_reference_levels",
                   _reference_sentinel):
            payload, recorder = self._run(monkeypatch, _em_core_stub())
        assert payload["research_outcome"] == "switched_fallback"
        assert recorder.calls == []

    def test_sw_kept_enriches_constituents_exactly_once(self, monkeypatch):
        """双失败保留申万：成分/同业富化按指数恰好一次（预算允许）。

        #100 原语义（一级成分链不出站两次）保留；#104 追加二级同业阶段
        对**二级指数**的成分链调用恰好一次（不同指数、独立预算门控）。"""
        payload, recorder = self._run(
            monkeypatch, _em_core_stub(subject_status="missing",
                                       quote_change=None))
        assert payload["research_outcome"] == "kept_sw_stage"
        l1_calls = [c for c in recorder.calls if c[1] == "801950.SL"]
        l2_calls = [c for c in recorder.calls if c[1] == "801951.SL"]
        assert len(l1_calls) == 1
        assert len(l2_calls) == 1

    def test_fetch_expired_keeps_sw_without_constituent_outbound(
            self, monkeypatch):
        """取数截止已过：保留申万但不出站富化（预算不允许）。"""
        budget = _budget(120.0)

        def _expired():
            return True

        budget.fetch_expired = _expired  # type: ignore[method-assign]
        payload, recorder = self._run(monkeypatch, _em_core_stub(),
                                      budget=budget)
        assert payload["research_outcome"] == "kept_sw_stage"
        assert recorder.calls == []
        assert payload["sector_peers"] is not None
        assert payload["sector_peers"]["eligible"] is False


class TestOptionalReferenceLevelSequencing:
    """spec 复审（issue #99）拆缝回归：二/三级参考链是必需研究之后的余量
    可选出站——慢/失败的可选参考不得饿死核心事实与研究，取数截止后不
    发起（走真实 resolve_sw_reference_levels + 传输替身）。"""

    def test_slow_optional_reference_never_starves_core_or_research(
            self, monkeypatch, frozen_session):
        events: List[Tuple[str, float]] = []
        research_times: List[Tuple[str, float]] = []
        base = time.monotonic()
        # 回接真实参考链解析 + 受控传输：核心端点走既有 _SwStub（活跃替身），
        # L2/L3 参考出站慢且失败（真实解析代码路径，仅传输叶子受控）
        monkeypatch.setattr(sw, "resolve_sw_reference_levels",
                            _REAL_RESOLVE_REFERENCE_LEVELS)
        stub = _SwStub(industry_by_level={1: "煤炭", 2: "煤炭开采",
                                          3: "动力煤"})

        def _transport(url: str, headers: Dict[str, str],
                       body: Optional[bytes], *, label: str) -> Tuple[int, bytes]:
            parsed = json.loads(body.decode()) if body else {}
            if "basic_data_service" in url:
                level = int(parsed["indipara"][0]["indiparams"][0])
                if level == 1:
                    return 200, json.dumps(_industry_payload_by_level(
                        parsed["codes"], "煤炭")).encode()
                events.append((f"basic_l{level}", time.monotonic() - base))
                time.sleep(0.15)  # 慢可选参考
                return 200, json.dumps(
                    {"errorcode": -3, "errmsg": "slow upstream",
                     "tables": []}).encode()
            return stub(url, headers, body, label=label)

        monkeypatch.setattr(sw, "_http_api_curl_post", _transport)
        monkeypatch.setattr(sw, "_resolve_access_token", _SwStub.resolve_token)
        real_cat = svc._catalysts_pillar
        real_syn = svc._synthesis_pillar

        def _cat(name, *, budget, subject_confirmed, stock_name=None):
            research_times.append(("catalysts", time.monotonic() - base))
            return real_cat(name, budget=budget,
                            subject_confirmed=subject_confirmed,
                            stock_name=stock_name)

        def _syn(name, code, ident, prosper, trend, peers, catalysts, *,
                 budget, subject_confirmed=False):
            research_times.append(("synthesis", time.monotonic() - base))
            return real_syn(name, code, ident, prosper, trend, peers,
                            catalysts, budget=budget,
                            subject_confirmed=subject_confirmed)

        monkeypatch.setattr(svc, "_catalysts_pillar", _cat)
        monkeypatch.setattr(svc, "_synthesis_pillar", _syn)
        from src.services.sector_analysis_service import _fetch_constituents

        chain = _sw_market_chain("600188", budget=_budget(),
                                 current_time=RECEIVED_AT)
        chain["constituents"] = _fetch_constituents(
            "600188", chain["identity"]["index_code"],
            chain["snapshot"]["session_date"], chain["session"],
            name="测试标的", refresh_token="sector-test-refresh-token",
            timeout_seconds=10.0, budget=_budget())
        payload = _compose_sw_payload("600188", "兖矿能源", chain,
                                      budget=_budget())
        # 核心事实与研究不受慢可选参考影响（不饿死）
        assert payload["background"]["intraday"]["sector_found"] is True
        assert payload["sector_trend"]["period_return_pct"] is not None
        assert payload["sector_peers"] is not None and payload["sector_peers"]["items"]
        assert payload["sector_catalysts"] is not None
        assert payload["sector_synthesis"] is not None
        # 可选参考失败如实缺口，l2/l3 未取得
        assert payload["identity"]["sw_chain"]["l2"] is None
        assert any("可选参考" in g and "失败" in g
                   for g in payload["identity"]["gaps"])
        # 时序：二/三级参考出站在必需研究（催化/综合）之后（余量出站）
        assert events and [lv for lv, _t in events] == ["basic_l2", "basic_l3"]
        last_research = max(t for _name, t in research_times)
        assert all(t > last_research for _lv, t in events)

    def test_reference_levels_not_started_after_fetch_deadline(
            self, monkeypatch, frozen_session):
        chain = _run_chain(monkeypatch, frozen_session,
                           _SwStub(industry_by_level={
                               1: "煤炭", 2: "煤炭开采", 3: "动力煤"}),
                           "600188")
        assert chain["identity"]["status"] == "confirmed"
        budget = _budget(120.0)
        monkeypatch.setattr(budget, "fetch_expired", lambda: True)
        outbound_urls: List[str] = []
        monkeypatch.setattr(sw, "resolve_sw_reference_levels",
                            _REAL_RESOLVE_REFERENCE_LEVELS)

        def _recording_transport(url, headers, body, *, label):
            outbound_urls.append(url)
            return 200, json.dumps({"errorcode": -1, "errmsg": "unused",
                                    "tables": []}).encode()

        monkeypatch.setattr(sw, "_http_api_curl_post", _recording_transport)
        monkeypatch.setattr(svc, "_concepts_pillar", lambda code, *, budget: {
            "items": [], "source": "ifind", "total_returned": 0,
            "truncated": False, "gaps": ["受控：概念阶段不执行"]})
        payload = _compose_sw_payload("600188", "兖矿能源", chain,
                                      budget=budget)
        # 截止后不发起参考出站（零 basic 出站）+ 可选缺口如实
        assert not [u for u in outbound_urls if "basic_data_service" in u]
        assert payload["identity"]["sw_chain"]["l2"] is None
        assert any("取数截止已过" in g and "参考" in g
                   for g in payload["identity"]["gaps"])
