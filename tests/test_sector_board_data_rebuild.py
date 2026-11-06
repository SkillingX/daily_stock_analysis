# -*- coding: utf-8 -*-
"""板块数据重建风险回归（真实东财 wire fixture 驱动，2026-11-07 收敛轮）。

``tests/fixtures/sector_eastmoney/`` 是 2026-11-06 经东财官方接口取得的
**真实完整响应**（sha256 与 root capture-manifest 逐一相等，未裁剪任何
导致旧实现误拒的元数据字段），仅作离线测试回放；本套件只隔离
``requests.Session.send`` 终端与注入冻结时钟，真实解析/资格/计算/预算链
全部执行。fixtures 不是生产取数恢复证据，不得复制到生产当动态数据。

root 金标准独立对照（本文件内从 fixture 重新核算，不引用实现内部值）：
- 600519: preClose=1452、末分钟 close=1453、分钟额 sum=6199528292、
  stock/get f48=6199528289（元级舍入差 3，衍生值不得冒充原样 f48）；
- BK1277: preClose=4642.21、末分钟 close=4543.15（high=4543.90，两列不同，
  可区分现价列选择）、sum=15030073967、f48=15030073973（差 6）。
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse, parse_qs

import pandas as pd
import pytest
import requests

from src.schemas.sector_vendor import BoardQuote, MemberQuote
from src.services.sector_analysis_service import (
    BudgetExhausted,
    ReportBudget,
    _collect_member_quotes,
    _fetch_board_members,
    _fetch_member_quote,
    _fetch_stock_quote,
    _fetch_trends2_snapshot,
    _http_get_json,
    _peers_pillar,
    _prosperity_pillar,
    _secucode,
    _session_context,
    _stock_secid,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "sector_eastmoney"

# 盘后（当日会话已收盘）：11-06 16:30 市场本地时间（naive=市场时区契约）。
# 必须晚于真实 fixture 的供应商时点 16:11:39（root 纪律：测试锚点与 fixture
# 自洽——不得在 16:00 声明 16:11 的未来时点合法）
_AFTER_CLOSE = datetime(2026, 11, 6, 16, 30)
# 盘中：11-06 10:00
_INTRA_DAY = datetime(2026, 11, 6, 10, 0)


def _load(name: str) -> Dict[str, Any]:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _golden_sum(name: str) -> Decimal:
    """从 fixture 分钟额列独立求和（root 金标准核算路径）。"""
    rows = _load(name)["data"]["trends"]
    return sum((Decimal(r.split(",")[6]) for r in rows), Decimal("0"))


def _wire_response(payload: Any) -> requests.Response:
    resp = requests.Response()
    resp.status_code = 200
    resp.encoding = "utf-8"
    resp._content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    resp.headers["Content-Type"] = "application/json"
    return resp


def _install_wire(monkeypatch, route: Callable[[str], Any]) -> None:
    """隔离 Session.send 终端：route(prepared_url) → payload dict 或异常。"""

    def fake_send(self, prepared, **kwargs):  # noqa: ANN001
        outcome = route(prepared.url)
        if isinstance(outcome, Exception):
            raise outcome
        return _wire_response(outcome)

    monkeypatch.setattr(requests.Session, "send", fake_send)


def _claims_rows_for_target() -> List[Dict[str, Any]]:
    """目标股 600519 声明列表：行业 L2 白酒Ⅱ BK1277 + 不同代码概念行。

    声明列表同一股票合法多行（SECUCODE 同键）；去重语义属成员集合。
    """
    rows = [_f10_row(*_REAL_BAIJIU_ROWS[3])]
    rows += [_f10_row("600519.SH", "600519", "贵州茅台", f"BK9{i:03d}",
                      f"白酒概念{i}", board_type="概念", board_level="1")
             for i in range(4)]
    return rows


def _frozen_session_ctx(now=None):  # noqa: ANN001 - 会话日历锚定（无挂钟）
    from src.services.sector_analysis_service import SessionContext

    # SessionContext 只锚日历事实；接收钟用显式 received_at 或 monkeypatch
    # _received_at_now 注入，不由 session_ctx 暗示
    return SessionContext(
        session_date=_AFTER_CLOSE.date(),
        session_open=datetime(2026, 11, 6, 9, 30),
        session_close=datetime(2026, 11, 6, 15, 0),
    )


def _route_by_secid(mapping: Dict[str, Any]) -> Callable[[str], Any]:
    """按 query 参数路由：trends2/stock/get 按 secid，datacenter 按 filter+页。"""

    def route(url: str) -> Any:
        parsed = parse_qs(urlparse(url).query)
        secid = (parsed.get("secid") or [""])[0]
        for key, payload in mapping.items():
            kind, _, target = key.partition(":")
            if kind == "trends2" and "trends2" in url and secid == target:
                return payload
            if kind == "stockget" and "stock/get" in url and secid == target:
                return payload
            if kind == "f10claims" and "datacenter" in url and "SECUCODE" in (
                    parsed.get("filter") or [""])[0]:
                return payload
            if kind.startswith("f10members") and "datacenter" in url:
                page = (parsed.get("pageNumber") or ["1"])[0]
                board = ((parsed.get("filter") or [""])[0]).split('"')[1]
                if board == target:
                    payload_pages = payload
                    return payload_pages.get(page, payload_pages.get("1"))
        return AssertionError(f"wire 回放未匹配请求: {url[:120]}")

    return route


# ---------------------------------------------------------------------------
# 金标准独立对照（与实现无关）
# ---------------------------------------------------------------------------

def test_golden_amount_sums_match_root_reference() -> None:
    """root 金标准：sum(分钟额) == 6199528292 / 15030073967。"""
    assert _golden_sum("stock-trends2.json") == Decimal("6199528292")
    assert _golden_sum("board-trends2.json") == Decimal("15030073967")


# ---------------------------------------------------------------------------
# P1 反例 1+2：真实完整 wire 全链（消费字段严格校验 + 现价列选择）
# ---------------------------------------------------------------------------

def test_stock_trends2_full_wire_accepted_derived_values(monkeypatch) -> None:
    """完整真实 wire（含 market 等 10 个元数据字段）走全链被采纳。"""
    fixture = _load("stock-trends2.json")
    budget = ReportBudget(30)
    _install_wire(monkeypatch, _route_by_secid({"trends2:1.600519": fixture}))
    snap = _fetch_trends2_snapshot(
        "1.600519", "600519", "贵州茅台", budget=budget, quote_model=MemberQuote,
        now=_AFTER_CLOSE)
    assert snap is not None
    assert snap.source == "trends2当日累计"
    assert snap.derived_amount is True
    assert snap.vendor_as_of == "2026-11-06 15:00"
    assert snap.vendor_time_epoch == fixture["data"]["time"]
    # 独立核算：分钟累计额（元级差 3 于 f48，不得冒充原样）
    assert snap.quote.f48 == float(_golden_sum("stock-trends2.json"))
    assert abs(snap.quote.f48 - 6199528289.0) <= 10
    # 现价列：末分钟 close=1453（high 同值，弱区分），Decimal 涨幅 → 0.07
    assert snap.quote.f170 == pytest.approx(0.07, abs=1e-9)


def test_board_trends2_full_wire_close_column_and_market_echo(monkeypatch) -> None:
    """板块 wire：market=90 回显校验；末行 close≠high 区分现价列选择。"""
    fixture = _load("board-trends2.json")
    budget = ReportBudget(30)
    _install_wire(monkeypatch, _route_by_secid({"trends2:90.BK1277": fixture}))
    snap = _fetch_trends2_snapshot(
        "90.BK1277", "BK1277", "白酒Ⅱ", budget=budget, quote_model=BoardQuote,
        now=_AFTER_CLOSE)
    assert snap is not None
    assert snap.quote.f48 == float(_golden_sum("board-trends2.json"))
    assert abs(snap.quote.f48 - 15030073973.0) <= 10
    # 若实现取 high 列：(4543.90-4642.21)/4642.21 = -2.1208 → 与 close 列不同
    assert snap.quote.f170 == pytest.approx(-2.13, abs=1e-9)
    assert snap.quote.f170 != pytest.approx(-2.1208, abs=1e-3)


def test_ohlc_distinct_shape_yields_ten_percent_not_fifty(monkeypatch) -> None:
    """root 反例回放：preClose=10、close=11、high=15 → 10%（非 parsed[1] 的 50%）。"""
    payload = _load("board-trends2.json")
    rows = payload["data"]["trends"]
    # 仅改末行价格形状与 preClose，金额列保持真实（累计求和不变）
    parts = rows[-1].split(",")
    parts[1], parts[2], parts[3], parts[4] = "10.90", "11", "15", "10.80"
    rows[-1] = ",".join(parts)
    payload["data"]["preClose"] = 10
    budget = ReportBudget(30)
    _install_wire(monkeypatch, _route_by_secid({"trends2:90.BK1277": payload}))
    snap = _fetch_trends2_snapshot(
        "90.BK1277", "BK1277", "白酒Ⅱ", budget=budget, quote_model=BoardQuote,
        now=_AFTER_CLOSE)
    assert snap is not None
    assert snap.quote.f170 == pytest.approx(10.0, abs=1e-9)


# ---------------------------------------------------------------------------
# P1 反例 2：wire 模型不拒绝合法元数据（旧 extra=forbid 反例）
# ---------------------------------------------------------------------------

def test_trends2_model_rejects_only_invalid_not_metadata() -> None:
    from src.schemas.sector_vendor import Trends2Response

    inner = _load("stock-trends2.json")["data"]
    # 完整真实 inner（14 字段）必须通过
    Trends2Response.model_validate(inner)
    # 消费字段类型漂移必须拒绝：market 字符串、preClose 缺失、trends 非数组
    for mutate in (
        lambda d: d.update(market="1"),
        lambda d: d.pop("preClose"),
        lambda d: d.update(trends="not-a-list"),
        lambda d: d.pop("trendsTotal"),
    ):
        broken = dict(inner)
        mutate(broken)
        with pytest.raises(Exception):
            Trends2Response.model_validate(broken)


# ---------------------------------------------------------------------------
# P1 反例 3+4：会话/完整性契约
# ---------------------------------------------------------------------------

def _trim_fixture_to(prefix_rows: int) -> Dict[str, Any]:
    payload = _load("stock-trends2.json")
    trends = payload["data"]["trends"][:prefix_rows]
    payload["data"]["trends"] = trends
    payload["data"]["trendsTotal"] = len(trends)
    # envelope 自洽：供应商 time 元数据同步为末根分钟时刻（盘中前缀响应的
    # time 不得晚于其分钟序列/接收时刻）
    from zoneinfo import ZoneInfo

    last_minute = trends[-1].split(",")[0]
    payload["data"]["time"] = int(datetime.strptime(
        last_minute, "%Y-%m-%d %H:%M").replace(
            tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
    return payload


def test_intraday_prefix_coverage_accepted_with_honest_as_of(monkeypatch) -> None:
    """盘中 Fri10:00：截至 10:00 的前缀分钟合法采纳（31 行 09:30..10:00）。"""
    payload = _trim_fixture_to(31)
    assert payload["data"]["trends"][-1].startswith("2026-11-06 10:00")
    budget = ReportBudget(30)
    _install_wire(monkeypatch, _route_by_secid({"trends2:1.600519": payload}))
    snap = _fetch_trends2_snapshot(
        "1.600519", "600519", "贵州茅台", budget=budget, quote_model=MemberQuote,
        now=_INTRA_DAY, received_at=_INTRA_DAY)
    assert snap is not None
    assert snap.vendor_as_of == "2026-11-06 10:00"
    expected = sum(
        Decimal(r.split(",")[6]) for r in payload["data"]["trends"])
    assert snap.quote.f48 == float(expected)


def test_completed_day_window_rejected_as_future_minutes_intraday(monkeypatch) -> None:
    """盘中 10:00 收到全天响应：分钟超过当前时刻=未来数据，整体拒绝。"""
    fixture = _load("stock-trends2.json")
    budget = ReportBudget(30)
    _install_wire(monkeypatch, _route_by_secid({"trends2:1.600519": fixture}))
    assert _fetch_trends2_snapshot(
        "1.600519", "600519", "贵州茅台", budget=budget, quote_model=MemberQuote,
        now=_INTRA_DAY, received_at=_INTRA_DAY) is None


def test_stale_old_day_rejected_on_next_trading_session(monkeypatch) -> None:
    """下一交易日盘中（11-09 周一 10:00）收到 11-06 分钟：旧日整体拒绝。"""
    fixture = _load("stock-trends2.json")
    budget = ReportBudget(30)
    _install_wire(monkeypatch, _route_by_secid({"trends2:1.600519": fixture}))
    snap = _fetch_trends2_snapshot(
        "1.600519", "600519", "贵州茅台", budget=budget, quote_model=object,
        now=datetime(2026, 11, 9, 10, 0))
    assert snap is None


def test_preopen_accepts_previous_completed_session(monkeypatch) -> None:
    """开盘前（11-09 08:00）：最近已完成会话（11-06）分钟合法采纳。"""
    fixture = _load("stock-trends2.json")
    budget = ReportBudget(30)
    _install_wire(monkeypatch, _route_by_secid({"trends2:1.600519": fixture}))
    snap = _fetch_trends2_snapshot(
        "1.600519", "600519", "贵州茅台", budget=budget, quote_model=MemberQuote,
        now=datetime(2026, 11, 9, 8, 0))
    assert snap is not None
    assert snap.vendor_as_of == "2026-11-06 15:00"


def test_reversed_minutes_rejected(monkeypatch) -> None:
    """乱序（倒序数组）整体拒绝——不把首行价格误当最新现价。"""
    payload = _load("stock-trends2.json")
    payload["data"]["trends"] = list(reversed(payload["data"]["trends"]))
    budget = ReportBudget(30)
    _install_wire(monkeypatch, _route_by_secid({"trends2:1.600519": payload}))
    assert _fetch_trends2_snapshot(
        "1.600519", "600519", "贵州茅台", budget=budget, quote_model=object,
        now=_AFTER_CLOSE) is None


def test_dropped_middle_minute_rejected(monkeypatch) -> None:
    """缺中间分钟（覆盖断档）整体拒绝——部分求和不得冒充累计。"""
    payload = _load("stock-trends2.json")
    trends = payload["data"]["trends"]
    payload["data"]["trends"] = trends[:100] + trends[101:]
    payload["data"]["trendsTotal"] = len(payload["data"]["trends"])
    budget = ReportBudget(30)
    _install_wire(monkeypatch, _route_by_secid({"trends2:1.600519": payload}))
    assert _fetch_trends2_snapshot(
        "1.600519", "600519", "贵州茅台", budget=budget, quote_model=object,
        now=_AFTER_CLOSE) is None


def test_duplicate_minute_rejected(monkeypatch) -> None:
    payload = _load("stock-trends2.json")
    trends = payload["data"]["trends"]
    payload["data"]["trends"] = trends[:100] + [trends[99]] + trends[100:]
    payload["data"]["trendsTotal"] = len(payload["data"]["trends"])
    budget = ReportBudget(30)
    _install_wire(monkeypatch, _route_by_secid({"trends2:1.600519": payload}))
    assert _fetch_trends2_snapshot(
        "1.600519", "600519", "贵州茅台", budget=budget, quote_model=object,
        now=_AFTER_CLOSE) is None


def test_trendstotal_mismatch_rejected(monkeypatch) -> None:
    """供应商声明行数与实际行数不一致：完整性依据缺失，拒绝。"""
    payload = _load("stock-trends2.json")
    payload["data"]["trendsTotal"] = 242
    budget = ReportBudget(30)
    _install_wire(monkeypatch, _route_by_secid({"trends2:1.600519": payload}))
    assert _fetch_trends2_snapshot(
        "1.600519", "600519", "贵州茅台", budget=budget, quote_model=object,
        now=_AFTER_CLOSE) is None


@pytest.mark.parametrize("preclose", ["Infinity", "-", 0, -1, "NaN"])
def test_invalid_preclose_rejected_as_source_error(preclose: Any,
                                                   monkeypatch) -> None:
    """preClose 必须有限正：Infinity/-/0/负/NaN 全部按源错误拒绝，不泄公式异常。"""
    payload = _load("stock-trends2.json")
    payload["data"]["preClose"] = preclose
    budget = ReportBudget(30)
    _install_wire(monkeypatch, _route_by_secid({"trends2:1.600519": payload}))
    snap = _fetch_trends2_snapshot(
        "1.600519", "600519", "贵州茅台", budget=budget, quote_model=MemberQuote,
        now=_AFTER_CLOSE)
    assert snap is None


def test_nonfinite_minute_amount_rejected(monkeypatch) -> None:
    payload = _load("stock-trends2.json")
    trends = payload["data"]["trends"]
    parts = trends[120].split(",")
    parts[6] = "NaN"
    trends[120] = ",".join(parts)
    budget = ReportBudget(30)
    _install_wire(monkeypatch, _route_by_secid({"trends2:1.600519": payload}))
    assert _fetch_trends2_snapshot(
        "1.600519", "600519", "贵州茅台", budget=budget, quote_model=object,
        now=_AFTER_CLOSE) is None


def test_missing_minute_amount_rejected(monkeypatch) -> None:
    payload = _load("stock-trends2.json")
    trends = payload["data"]["trends"]
    parts = trends[120].split(",")
    parts[6] = "-"
    trends[120] = ",".join(parts)
    budget = ReportBudget(30)
    _install_wire(monkeypatch, _route_by_secid({"trends2:1.600519": payload}))
    assert _fetch_trends2_snapshot(
        "1.600519", "600519", "贵州茅台", budget=budget, quote_model=object,
        now=_AFTER_CLOSE) is None


# ---------------------------------------------------------------------------
# P1 反例 5：F10 envelope / 双键 / 分页 / 920 识别
# ---------------------------------------------------------------------------

def _f10_row(secucode: str, code: str, name: str, board_code: str = "BK1277",
             board_name: str = "白酒Ⅱ", board_type: str = "行业",
             board_level: str = "2") -> Dict[str, Any]:
    return {
        "SECUCODE": secucode, "SECURITY_CODE": code,
        "SECURITY_NAME_ABBR": name, "NEW_BOARD_CODE": board_code,
        "BOARD_NAME": board_name, "BOARD_CODE": "1277",
        "BOARD_TYPE": board_type, "BOARD_LEVEL": board_level,
    }


_REAL_BAIJIU_ROWS = [
    ("000799.SZ", "000799", "酒鬼酒"),
    ("600197.SH", "600197", "伊力特"),
    ("600809.SH", "600809", "山西汾酒"),
    ("600519.SH", "600519", "贵州茅台"),
    ("600199.SH", "600199", "金种子酒"),
]


def _f10_ok(resp_rows: List[Dict[str, Any]], count: Optional[int] = None,
            pages: int = 1) -> Dict[str, Any]:
    return {
        "version": "367f9a4158cac6cd2ba458970b69aeb1",
        "result": {"pages": pages, "data": resp_rows,
                   "count": count if count is not None else len(resp_rows)},
        "success": True, "message": "success", "code": 0,
    }


def test_f10_success_false_rejected(monkeypatch) -> None:
    """success=false envelope 整体拒绝：残留 data 不产成员集合。"""
    body = _f10_ok([_f10_row(*_REAL_BAIJIU_ROWS[3])])
    body["success"] = False
    body["message"] = "错误"
    budget = ReportBudget(30)
    _install_wire(monkeypatch, _route_by_secid(
        {"f10members:BK1277": {"1": body}}))
    assert _fetch_board_members(
        "BK1277", "白酒Ⅱ", "600519", budget=budget) is None


def test_f10_row_dual_key_mismatch_rejected(monkeypatch) -> None:
    """SECUCODE 与 SECURITY_CODE 双键漂移（600519/000001.SZ）整体拒绝。"""
    rows = [_f10_row(*_REAL_BAIJIU_ROWS[3])]
    rows[0]["SECUCODE"] = "000001.SZ"
    budget = ReportBudget(30)
    _install_wire(monkeypatch, _route_by_secid(
        {"f10members:BK1277": {"1": _f10_ok(rows)}}))
    assert _fetch_board_members(
        "BK1277", "白酒Ⅱ", "600519", budget=budget) is None


def test_secucode_920_maps_bj_and_consistent_secid(monkeypatch) -> None:
    """920 北交新段：SECUCODE=.BJ、secid 前缀 0；沪 B 900 不误判。"""
    assert _secucode("920001") == "920001.BJ"
    assert _stock_secid("920001") == "0.920001"
    assert _secucode("900901") == "900901.SH"
    assert _stock_secid("600519") == "1.600519"
    assert _stock_secid("000001") == "0.000001"
    # 920 成员行后缀错标 SZ：双键校验拒绝
    rows = [_f10_row("920001.SZ", "920001", "北交样本")]
    budget = ReportBudget(30)
    _install_wire(monkeypatch, _route_by_secid(
        {"f10members:BK0739": {"1": _f10_ok(rows)}}))
    assert _fetch_board_members(
        "BK0739", "工程机械", "920001", budget=budget) is None


def test_f10_pagination_metadata_drift_rejected(monkeypatch) -> None:
    """跨页 count/pages 漂移：不静默采为完整集合。"""
    page1 = _f10_ok([_f10_row(*r) for r in _REAL_BAIJIU_ROWS], count=10, pages=2)
    page2 = _f10_ok([_f10_row("603919.SH", "603919", "金徽酒")],
                    count=11, pages=2)
    budget = ReportBudget(30)
    _install_wire(monkeypatch, _route_by_secid(
        {"f10members:BK1277": {"1": page1, "2": page2}}))
    assert _fetch_board_members(
        "BK1277", "白酒Ⅱ", "600519", budget=budget) is None


def test_f10_multi_page_full_collection_and_target(monkeypatch) -> None:
    """分页收齐：count=20、page1 满 100 行(pageSize=100 时单页)、target 命中。"""
    rows = [_f10_row(*r) for r in _REAL_BAIJIU_ROWS]
    # 扩充到 20 行（保持真实行形状；重复代码会在收齐后按重复拒绝，故换码）
    extra = [(f"6008{i:02d}.SH", f"6008{i:02d}", f"样本{i}") for i in range(15, 30)]
    rows += [_f10_row(*r) for r in extra]
    assert len(rows) == 20
    budget = ReportBudget(30)
    _install_wire(monkeypatch, _route_by_secid(
        {"f10members:BK1277": {"1": _f10_ok(rows, count=20, pages=1)}}))
    out = _fetch_board_members("BK1277", "白酒Ⅱ", "600519", budget=budget)
    assert out is not None
    assert out["member_count"] == 20
    assert out["target_in"] is True
    assert out["rows"][3].SECURITY_CODE == "600519"


def test_f10_cross_page_duplicate_codes_rejected(monkeypatch) -> None:
    """跨页重复代码=源错误，不静默取其一。"""
    page1 = _f10_ok([_f10_row(*r) for r in _REAL_BAIJIU_ROWS], count=6, pages=2)
    page2 = _f10_ok([_f10_row(*_REAL_BAIJIU_ROWS[0])], count=6, pages=2)
    budget = ReportBudget(30)
    _install_wire(monkeypatch, _route_by_secid(
        {"f10members:BK1277": {"1": page1, "2": page2}}))
    assert _fetch_board_members(
        "BK1277", "白酒Ⅱ", "600519", budget=budget) is None


# ---------------------------------------------------------------------------
# 反例 7：预算与异常分层
# ---------------------------------------------------------------------------

def test_slow_wire_exceeding_budget_raises_budget_exhausted(monkeypatch) -> None:
    """真实慢 Session.send：等待超预算即 BudgetExhausted（迟到无采纳）。"""

    def slow_send(self, prepared, **kwargs):  # noqa: ANN001
        import time as _t

        _t.sleep(1.2)
        return _wire_response(_load("stock-trends2.json"))

    monkeypatch.setattr(requests.Session, "send", slow_send)
    budget = ReportBudget(0.4)
    with pytest.raises(BudgetExhausted):
        _http_get_json("https://push2his.eastmoney.com/api/qt/stock/trends2/get",
                       {"secid": "1.600519"}, budget=budget)


def test_program_error_propagates_not_none(monkeypatch) -> None:
    """程序 TypeError 原样传播：不当 JSON/供应商故障降 None。"""

    def broken_send(self, prepared, **kwargs):  # noqa: ANN001
        raise TypeError("program bug in wire layer")

    monkeypatch.setattr(requests.Session, "send", broken_send)
    budget = ReportBudget(30)
    with pytest.raises(TypeError):
        _http_get_json("https://push2.eastmoney.com/api/qt/stock/get",
                       {"secid": "1.600519"}, budget=budget)


def test_non_json_response_downgrades_to_none(monkeypatch) -> None:
    """502 HTML：resp.json() 边界解码失败按源错误 None（不误报网络全断）。"""
    resp = requests.Response()
    resp.status_code = 200
    resp._content = b"<html>502 bad gateway</html>"
    resp.encoding = "utf-8"

    def html_send(self, prepared, **kwargs):  # noqa: ANN001
        return resp

    monkeypatch.setattr(requests.Session, "send", html_send)
    budget = ReportBudget(30)
    assert _http_get_json("https://push2.eastmoney.com/api/qt/stock/get",
                          {"secid": "1.600519"}, budget=budget) is None


def test_member_quote_falls_back_to_trends2_with_source(monkeypatch) -> None:
    """stock/get 不可用 → trends2 backup：来源/as_of/衍生标记齐备。"""
    budget = ReportBudget(60)
    _install_wire(monkeypatch, _route_by_secid({
        "stockget:1.600519": requests.ConnectionError("stock/get 拒绝窗口"),
        "trends2:1.600519": _load("stock-trends2.json"),
    }))
    snap = _fetch_member_quote("600519", "贵州茅台", budget=budget,
                               now=_AFTER_CLOSE)
    assert snap is not None
    assert snap.source == "trends2当日累计"
    assert snap.derived_amount is True
    assert snap.vendor_as_of == "2026-11-06 15:00"


# ---------------------------------------------------------------------------
# 反例 6：缺报价时 top5 未知（分母未知不冒充全体）
# ---------------------------------------------------------------------------

def _peers_df(rows: List[tuple]) -> pd.DataFrame:
    return pd.DataFrame({
        "代码": [r[0] for r in rows],
        "名称": [r[1] for r in rows],
        "成交额": [r[2] for r in rows],
        "涨跌幅": [r[3] for r in rows],
    })


def test_missing_member_quotes_withhold_top5() -> None:
    """6 成员仅 5 个已知成交额：target_in_amount_top5=None + 子集口径标注。"""
    rows = [
        ("600519", "贵州茅台", 1000.0, 1.0),
        ("000799", "酒鬼酒", 900.0, 0.5),
        ("600197", "伊力特", 800.0, -0.5),
        ("600809", "山西汾酒", 700.0, 2.0),
        ("600199", "金种子酒", 600.0, 0.0),
        ("603919", "金徽酒", None, 1.0),  # 缺报价成员（资格未知）
    ]
    out = _peers_pillar("600519", _peers_df(rows), budget=ReportBudget(30),
                        subject_confirmed=True, quotes_complete=False)
    assert out["eligible"] is True
    assert out["target_in_amount_top5"] is None
    assert "有效报价 5 行内次序（原始观测 6 行、身份有效 6 行" in out["selection_rule"]
    assert any("缺报价" in g or "未知" in g for g in out["gaps"])
    assert out["items"] and len(out["items"]) >= 5


def test_full_member_quotes_allow_top5_verdict() -> None:
    rows = [
        ("600519", "贵州茅台", 1000.0, 1.0),
        ("000799", "酒鬼酒", 900.0, 0.5),
        ("600197", "伊力特", 800.0, -0.5),
        ("600809", "山西汾酒", 700.0, 2.0),
        ("600199", "金种子酒", 600.0, 0.0),
        ("603919", "金徽酒", 500.0, 1.0),
    ]
    out = _peers_pillar("600519", _peers_df(rows), budget=ReportBudget(30),
                        subject_confirmed=True, quotes_complete=True)
    assert out["target_in_amount_top5"] is True
    assert "内次序" not in out["selection_rule"]


# ---------------------------------------------------------------------------
# 当日行情单标的恢复路径 + provenance/as_of 贯穿
# ---------------------------------------------------------------------------

def test_prosperity_snapshot_recovery_threading_provenance(monkeypatch) -> None:
    """全表不可用：板块单标的 trends2 快照恢复 row；来源/as_of 进载荷与缺口。"""
    import akshare as ak

    def _em_down():
        raise RuntimeError("clist 拒绝窗口")

    monkeypatch.setattr(ak, "stock_board_industry_name_em", _em_down)
    budget = ReportBudget(60)
    _install_wire(monkeypatch, _route_by_secid({
        "stockget:90.BK1277": requests.ConnectionError("拒绝窗口"),
        "trends2:90.BK1277": _load("board-trends2.json"),
    }))
    out = _prosperity_pillar("白酒Ⅱ", budget=budget, board_code="BK1277",
                             board_table=None, now=_AFTER_CLOSE)
    assert out["sector_found"] is True
    assert out["row"]["change_pct"] == pytest.approx(-2.13, abs=1e-9)
    assert out["row"]["amount"] == float(_golden_sum("board-trends2.json"))
    assert out.get("snapshot_source"), "载荷须携带快照来源说明"
    assert "trends2" in out["snapshot_source"]
    assert "15:00" in out["snapshot_source"]
    # 全表口径维度如实 N/A + 来源缺口
    assert out["momentum"] is None and out["rank_of"] is None
    assert any("全表" in g for g in out["gaps"])
    # payload 严格 schema 接受 snapshot_source 字段（追加兼容）
    from src.schemas.sector_analysis import IntradayPillar

    pillar = IntradayPillar.model_validate(out)
    assert pillar.snapshot_source and "trends2" in pillar.snapshot_source


# ---------------------------------------------------------------------------
# 身份链：F10 声明锁定 + 反向成员验证（clist 全表不可用路径）
# ---------------------------------------------------------------------------

def test_identity_f10_claim_lock_confirmed(monkeypatch) -> None:
    """akshare 全链不可用：F10 声明锁定 + 反向完整成员 → confirmed(f10_members)。"""
    from src.services import sector_analysis_service as svc

    def _ak_down(*a, **k):  # noqa: ANN002 - 透传替身
        raise RuntimeError("clist/stock-get 拒绝窗口")

    monkeypatch.setattr(svc, "_bounded_ak", _ak_down)
    monkeypatch.setattr(svc, "_session_context", _frozen_session_ctx)
    members_rows = [_f10_row(*_REAL_BAIJIU_ROWS[0]),  # 酒鬼酒
                    _f10_row(*_REAL_BAIJIU_ROWS[3])]  # 贵州茅台（目标股）
    budget = ReportBudget(60)
    _install_wire(monkeypatch, _route_by_secid({
        "f10claims:600519.SH": _f10_ok(
            [_f10_row(*_REAL_BAIJIU_ROWS[3]),
             _f10_row("600519.SH", "600519", "贵州茅台", "BK0001", "白酒概念",
                      board_type="概念", board_level="1")]),
        "f10members:BK1277": {"1": _f10_ok(members_rows, count=2, pages=1)},
        # 成员行情：目标股 stock/get 成功（真实 quote fixture），另一成员拒绝
        "stockget:1.600519": _load("stock-quote.json"),
        "stockget:0.000799": requests.ConnectionError("拒绝窗口"),
        "trends2:0.000799": requests.ConnectionError("拒绝窗口"),
    }))
    ident = svc._identify_sector("600519", budget=budget)
    assert ident["board_code"] == "BK1277"
    assert ident["board_name"] == "白酒Ⅱ"
    assert ident["subject_status"] == "confirmed"
    assert ident["membership"]["verified"] is True
    assert ident["membership"]["verified_source"] == "f10_members"
    assert ident["membership"]["member_count"] == 2
    assert ident["industry_em"] == "白酒Ⅱ"
    cons = ident["_cons_snapshot"]
    assert isinstance(cons, pd.DataFrame)
    # 目标股行情已采（f48 原样映射），失败成员保留成员资格、金额 None + 缺口
    target_row = cons[cons["代码"] == "600519"].iloc[0]
    assert target_row["成交额"] == 6199528289.0
    missing = cons[cons["代码"] == "000799"].iloc[0]
    assert missing["成交额"] is None or pd.isna(missing["成交额"])
    assert any("trends2" in g or "快照失败" in g for g in ident["gaps"])


def test_membership_schema_accepts_verified_source_literals() -> None:
    from src.schemas.sector_analysis import MembershipInfo

    for source in ("cons_table", "f10_members"):
        info = MembershipInfo.model_validate(
            {"verified": True, "member_count": 20, "verified_source": source})
        assert info.verified_source == source
    for rejected in ("unknown", "f10_board_claim"):
        with pytest.raises(Exception):
            MembershipInfo.model_validate(
                {"verified": True, "member_count": 20, "verified_source": rejected})


# ---------------------------------------------------------------------------
# provenance/as_of 贯穿事实摘要与观察限制
# ---------------------------------------------------------------------------

def test_fact_summary_and_limitations_thread_provenance() -> None:
    from src.services import sector_research as sr

    registry = sr.EvidenceRegistry()
    registry.register(sr.EvidenceEntry.build(
        "intraday", "fact", "当日板块行情",
        {"change_pct": -2.13,
         "snapshot_source": "东财 trends2 供应商分钟累计衍生（非原样字段；"
                            "供应商分钟截至 2026-11-06 15:00）",
         "snapshot_derived": True},
    ))
    registry.register(sr.EvidenceEntry.build(
        "peers", "fact", "同业成交活跃代表",
        {"selection_rule": "同一成分快照按成交额前五 + 目标股去重（成交活跃代表，"
                           "非市值/基本面龙头）；有效报价 5 行内次序（原始观测 "
                           "6 行、身份有效 6 行；缺报价/身份剔除成员资格未知，"
                           "非全体板块排名）",
         "items": [{"name": "贵州茅台", "is_target": True, "change_pct": 0.07}],
         "amount_provenance": "成交额单位为元：…行情 5 只 stock/get 原样、"
                              "1 只 trends2 供应商分钟累计衍生（非原样字段）",
         "amount_derived": True,
         "quotes_as_of": "trends2 补采 1 只供应商分钟截至 2026-11-06 15:00"},
    ))
    summary = sr.program_fact_summary(registry)
    assert "快照口径" in summary and "衍生" in summary
    assert "行情时点" in summary and "有效报价 5 行" in summary
    limitations = sr.program_fact_limitations(registry)
    assert limitations is not None
    assert "分钟累计衍生" in limitations
    assert "逐股实际采集" in limitations


# ---------------------------------------------------------------------------
# root 双轴审查反例回归（revision-contract 收敛轮）
# ---------------------------------------------------------------------------

def test_member_batch_partial_then_budget_keeps_shape(monkeypatch) -> None:
    """root 反例：第1股失败、第2股预算耗尽 → df 行数恒等成员数、三类计数清晰。

    旧实现 `fetched` 只数成功、按 total-fetched 补缺 → 3 值写 2 行
    ValueError，合法部分报告消失并被 API 误报 400。
    """
    from src.services import sector_analysis_service as svc

    frame = pd.DataFrame({
        "代码": ["600519", "600031"], "名称": ["贵州茅台", "三一重工"],
    })
    outcomes = [None, BudgetExhausted("probe exhausted")]

    def _fake_quote(*a, **k):  # noqa: ANN002
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(svc, "_fetch_member_quote", _fake_quote)
    batch = _collect_member_quotes(frame, budget=ReportBudget(10))
    assert len(batch.df) == 2
    assert batch.df["成交额"].tolist() == [None, None]
    assert batch.df["涨跌幅"].tolist() == [None, None]
    assert batch.amounts_known is False
    joined = "\n".join(batch.gaps)
    assert "采集失败 1 只" in joined
    assert "未尝试 1 只" in joined
    assert "已采成功 0 只" in joined


def test_stockget_stale_f86_rejected_after_close(monkeypatch) -> None:
    """root 反例：stock/get 旧日 f86（11-05 15:00）在 11-06 会话按源错误拒绝。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    raw = _load("board-quote.json")
    raw["data"]["f86"] = int(datetime(
        2026, 11, 5, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
    budget = ReportBudget(30)
    _install_wire(monkeypatch, _route_by_secid({"stockget:90.BK1277": raw}))
    assert _fetch_stock_quote("90.BK1277", "BK1277", "白酒Ⅱ", budget=budget,
                              quote_model=BoardQuote,
                              session_ctx=_session_context(_AFTER_CLOSE)) is None


def test_stockget_future_f86_rejected(monkeypatch) -> None:
    """root 反例：未来 f86（2030）不是真实观测，按源错误拒绝。"""
    from datetime import datetime, timezone

    raw = _load("board-quote.json")
    raw["data"]["f86"] = int(datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp())
    budget = ReportBudget(30)
    _install_wire(monkeypatch, _route_by_secid({"stockget:90.BK1277": raw}))
    assert _fetch_stock_quote("90.BK1277", "BK1277", "白酒Ⅱ", budget=budget,
                              quote_model=BoardQuote,
                              session_ctx=_session_context(_AFTER_CLOSE)) is None


def test_stockget_missing_f86_rejected(monkeypatch) -> None:
    """缺供应商时点 = 无可比当日资格，按源错误拒绝（不冒充当日行情）。"""
    raw = _load("board-quote.json")
    raw["data"]["f86"] = "-"
    budget = ReportBudget(30)
    _install_wire(monkeypatch, _route_by_secid({"stockget:90.BK1277": raw}))
    assert _fetch_stock_quote("90.BK1277", "BK1277", "白酒Ⅱ", budget=budget,
                              quote_model=BoardQuote,
                              session_ctx=_session_context(_AFTER_CLOSE)) is None


def test_negative_f48_rejected_zero_is_known(monkeypatch) -> None:
    """root 反例：f48=-1 在 I/O 边界拒绝（负值不进排名/不产 known）；0 是已知零观测。"""
    from src.schemas.sector_vendor import MemberQuote

    with pytest.raises(Exception):
        MemberQuote.model_validate({"f57": "600519", "f58": "贵州茅台", "f48": -1})
    zero = MemberQuote.model_validate({"f57": "600519", "f58": "贵州茅台", "f48": 0})
    assert zero.f48 == 0  # 0=已知零观测；None=缺失，语义分开


def test_board_truncated_prefix_after_close_rejected(monkeypatch) -> None:
    """root 反例：盘后（16:00）只到 10:00 的截尾前缀不冒充全天累计。"""
    payload = _trim_fixture_to(31)
    budget = ReportBudget(30)
    _install_wire(monkeypatch, _route_by_secid({"trends2:90.BK1277": payload}))
    assert _fetch_trends2_snapshot(
        "90.BK1277", "BK1277", "白酒Ⅱ", budget=budget, quote_model=BoardQuote,
        now=_AFTER_CLOSE) is None


def test_board_full_prefix_after_close_accepted(monkeypatch) -> None:
    """对照：盘后完整 241 分钟（末分钟 15:00）照常采纳（既有金标准路径）。"""
    budget = ReportBudget(30)
    _install_wire(monkeypatch, _route_by_secid(
        {"trends2:90.BK1277": _load("board-trends2.json")}))
    snap = _fetch_trends2_snapshot(
        "90.BK1277", "BK1277", "白酒Ⅱ", budget=budget, quote_model=BoardQuote,
        now=_AFTER_CLOSE)
    assert snap is not None
    assert snap.vendor_as_of == "2026-11-06 15:00"


def test_f10_business_code_rejected(monkeypatch) -> None:
    """root 反例：success=true + 业务 code=123（失败 envelope）不得产成员集合。"""
    body = _f10_ok([_f10_row(*_REAL_BAIJIU_ROWS[3])])
    body["code"] = 123
    budget = ReportBudget(30)
    _install_wire(monkeypatch, _route_by_secid(
        {"f10members:BK1277": {"1": body}}))
    assert _fetch_board_members(
        "BK1277", "白酒Ⅱ", "600519", budget=budget) is None


def test_claims_incomplete_pagination_rejected(monkeypatch) -> None:
    """root 反例：声明 pages=2/count=2 仅回 1 行 → 完整性不成立，整体不采用。"""
    from src.services import sector_analysis_service as svc

    body = _f10_ok([_f10_row(*_REAL_BAIJIU_ROWS[3])], count=2, pages=2)
    budget = ReportBudget(30)
    _install_wire(monkeypatch, _route_by_secid(
        {"f10claims:600519.SH": body}))
    assert svc._fetch_board_claims("600519", budget=budget) is None


def test_concept_same_name_claim_neither_locks_nor_confirms(monkeypatch) -> None:
    """root 反例：声明同名行标「概念/1」→ 不锁定不确认；成员不可用 → unverified。

    root 收敛轮契约：声明只用于精确板块锁定（行业 L2 唯一），概念同名不参与；
    完整反向成员不可得 → unverified（无 f10_board_claim 升 confirmed 分支）。
    """
    from src.services import sector_analysis_service as svc

    def _ak_down(*a, **k):  # noqa: ANN002
        raise RuntimeError("clist 拒绝窗口")

    monkeypatch.setattr(svc, "_bounded_ak", _ak_down)
    claims_body = _f10_ok([
        # 概念同名行（非行业 L2）不得参与锁定
        _f10_row(*_REAL_BAIJIU_ROWS[3], board_code="BK0001", board_name="白酒概念",
                 board_type="概念", board_level="1"),
    ])
    budget = ReportBudget(60)
    _install_wire(monkeypatch, _route_by_secid({
        "f10claims:600519.SH": claims_body,
    }))
    ident = svc._identify_sector("600519", budget=budget)
    assert ident["board_code"] == ""  # 概念同名不参与锁定
    assert ident["membership"]["verified"] is False
    # 声明无行业名（仅概念同名）且 f127/cninfo 不可用 → 无行业数据
    assert ident["subject_status"] == "missing"


def test_program_error_propagates_single_fallback_no_confirm(monkeypatch) -> None:
    """root 反例：clist 成分空 + F10 TypeError → 程序错误原样传播、仅一次派发。

    旧实现 fallback 在 broad except 内：TypeError 被吞 → 再次调用 → None →
    claim 升 confirmed(f10_board_claim)——程序 bug 被记成供应商故障。
    """
    from src.services import sector_analysis_service as svc

    def _ak_down(*a, **k):  # noqa: ANN002
        raise RuntimeError("clist 拒绝窗口")

    monkeypatch.setattr(svc, "_bounded_ak", _ak_down)
    calls: List[int] = []

    def _members(board_code, board_name, code, *, budget):  # noqa: ANN001
        calls.append(1)
        raise TypeError("program bug in F10 member path")

    monkeypatch.setattr(svc, "_fetch_board_members", _members)
    monkeypatch.setattr(svc, "_session_context", _frozen_session_ctx)
    claims_body = _f10_ok(_claims_rows_for_target())
    budget = ReportBudget(60)
    _install_wire(monkeypatch, _route_by_secid({
        "f10claims:600519.SH": claims_body,
    }))
    with pytest.raises(TypeError):
        svc._identify_sector("600519", budget=budget)
    assert len(calls) == 1  # 不重复派发


def test_identity_f10_members_unavailable_stays_unverified(monkeypatch) -> None:
    """声明锁定成功但完整成员不可用 → unverified（不产 f10_board_claim 确认）。"""
    from src.services import sector_analysis_service as svc

    def _ak_down(*a, **k):  # noqa: ANN002
        raise RuntimeError("clist 拒绝窗口")

    monkeypatch.setattr(svc, "_bounded_ak", _ak_down)
    monkeypatch.setattr(svc, "_session_context", _frozen_session_ctx)
    claims_body = _f10_ok(_claims_rows_for_target())
    budget = ReportBudget(60)
    _install_wire(monkeypatch, _route_by_secid({
        "f10claims:600519.SH": claims_body,
        "f10members:BK1277": {"1": requests.ConnectionError("成员拒绝窗口")},
    }))
    ident = svc._identify_sector("600519", budget=budget)
    assert ident["board_code"] == "BK1277"
    assert ident["board_name"] == "白酒Ⅱ"
    assert ident["subject_status"] == "unverified"
    assert ident["membership"]["verified"] is False
    assert ident["membership"]["verified_source"] is None


def test_model_projection_threads_provenance_via_real_entry(monkeypatch) -> None:
    """真实最终入口：_build_evidence_registry → build_synthesis_user_text。

    来源/供应商时点/子集范围进模型 wire（纯口径描述）；raw magnitudes
    （精确金额/涨跌幅数值）不进投影——基线市场字段排除纪律保持。
    """
    from src.services import sector_analysis_service as svc
    from src.services import sector_research as sr

    prosper = {
        "sector_found": True,
        "momentum": None, "activity": None, "amount_activity": None,
        "breadth": None, "rank_of": None, "total_boards": None,
        "row": {"board_name": "白酒Ⅱ", "board_code": "BK1277",
                "change_pct": -2.13, "amount": 15030073967.0},
        "snapshot_source": "东财 trends2 供应商分钟累计衍生（f48=分钟额求和、"
                           "非原样字段，元级与 f48 同口径；涨跌幅=末分钟现价对 "
                           "preClose Decimal 计算；供应商分钟截至 2026-11-06 15:00）",
        "gaps": [],
    }
    peers = {
        "eligible": True,
        "selection_rule": "同一成分快照按成交额前五 + 目标股去重（成交活跃代表，"
                          "非市值/基本面龙头）；有效报价 5 行内次序（原始观测 "
                          "6 行、身份有效 6 行；缺报价/身份剔除成员资格未知，"
                          "非全体板块排名）",
        "items": [{"code": "600519", "name": "贵州茅台", "amount": None,
                   "change_pct": 0.07, "is_target": True}],
        "target_in_amount_top5": None, "target_change_pct_rank": None,
        "amount_unit": "元",
        "amount_provenance": "成交额单位为元：…行情 5 只 stock/get 原样映射，"
                             "1 只 trends2 供应商分钟累计衍生（非原样字段，"
                             "元级与 f48 同口径）",
        "snapshot_rows": 6, "rankable_rows": 5, "sample_size": 6,
        "snapshot_observed_at": "2026-11-06T16:00:00",
        "source": "东财 F10 板块完整成员（身份验证同一集合）",
        "quotes_as_of": "trends2 补采 1 只供应商分钟截至 2026-11-06 15:00；"
                        "stock/get 5 只时间戳范围 1793952000~1793952600"
                        "（epoch 秒，逐股采集非同一瞬间）",
        "gaps": [],
    }
    ident = {"stock_name": "贵州茅台", "board_name": "白酒Ⅱ", "board_code": "BK1277",
             "industry_em": "白酒Ⅱ",
             "membership": {"verified": True, "member_count": 6,
                            "verified_source": "f10_members"}}
    trend = {"eligible": True, "start_date": None, "end_date": None,
             "qualified_sessions": None, "period_return_pct": None,
             "benchmark": None, "relative_return_pp": None}
    registry = svc._build_evidence_registry(
        "贵州茅台", "600519", ident, prosper, trend, peers, catalysts={})
    wire = sr.build_synthesis_user_text(registry)
    # 口径/asof 进模型 wire
    assert "供应商分钟累计衍生" in wire
    assert "2026-11-06 15:00" in wire
    assert "逐股实际采集" in wire or "逐股采集" in wire
    assert "有效报价 5 行" in wire
    # raw magnitudes 不进投影（基线市场字段排除纪律）
    assert "15030073967" not in wire
    assert "-2.13" not in wire
    # 观察限制同口径
    limitations = sr.program_fact_limitations(registry)
    assert limitations is not None
    assert "分钟累计衍生" in limitations
    assert "逐股实际采集" in limitations


# ---------------------------------------------------------------------------
# root final 反例回归（revision-source-final 收敛轮）
# ---------------------------------------------------------------------------

def _intraday_ctx() -> Any:
    from src.services.sector_analysis_service import SessionContext

    return SessionContext(
        session_date=_AFTER_CLOSE.date(),
        session_open=datetime(2026, 11, 6, 9, 30),
        session_close=datetime(2026, 11, 6, 15, 0),
    )


def test_stockget_same_day_future_rejected(monkeypatch) -> None:
    """root final 反例：盘中 10:00 接收，f86=同日 14:30 是未来时点 → 拒绝。"""
    from datetime import datetime as dtdt

    raw = _load("board-quote.json")
    raw["data"]["f86"] = int(dtdt(2026, 11, 6, 14, 30).timestamp())
    budget = ReportBudget(30)
    _install_wire(monkeypatch, _route_by_secid({"stockget:90.BK1277": raw}))
    assert _fetch_stock_quote("90.BK1277", "BK1277", "白酒Ⅱ", budget=budget,
                              quote_model=BoardQuote,
                              session_ctx=_intraday_ctx(),
                              received_at=datetime(2026, 11, 6, 10, 0)) is None


def test_stockget_postclose_same_day_late_rejected(monkeypatch) -> None:
    """root final 反例：盘后 16:00 接收，f86=同日 23:00 晚于接收 → 拒绝。"""
    raw = _load("board-quote.json")
    raw["data"]["f86"] = int(datetime(2026, 11, 6, 23, 0).timestamp())
    budget = ReportBudget(30)
    _install_wire(monkeypatch, _route_by_secid({"stockget:90.BK1277": raw}))
    assert _fetch_stock_quote("90.BK1277", "BK1277", "白酒Ⅱ", budget=budget,
                              quote_model=BoardQuote,
                              session_ctx=_session_context(_AFTER_CLOSE),
                              received_at=datetime(2026, 11, 6, 16, 0)) is None


def test_stockget_postclose_older_intraday_rejected(monkeypatch) -> None:
    """root final 反例：盘后 16:30 接收，f86=上午 10:00 盘中旧快照 → 拒绝。

    完整会话的可比快照时点必须不早于收盘（15:00）；10:00 的盘中时点不得
    冒充盘后可比行情进入全体已知排名。
    """
    raw = _load("board-quote.json")
    raw["data"]["f86"] = int(datetime(2026, 11, 6, 10, 0).timestamp())
    budget = ReportBudget(30)
    _install_wire(monkeypatch, _route_by_secid({"stockget:90.BK1277": raw}))
    assert _fetch_stock_quote("90.BK1277", "BK1277", "白酒Ⅱ", budget=budget,
                              quote_model=BoardQuote,
                              session_ctx=_session_context(_AFTER_CLOSE),
                              received_at=datetime(2026, 11, 6, 16, 30)) is None


def test_stockget_fixture_ts_within_receive_window_accepted(monkeypatch) -> None:
    """对照：真实 fixture f86=16:11:39，锚点/接收 16:30（自洽）→ 采纳。"""
    budget = ReportBudget(30)
    _install_wire(monkeypatch, _route_by_secid(
        {"stockget:90.BK1277": _load("board-quote.json")}))
    snap = _fetch_stock_quote("90.BK1277", "BK1277", "白酒Ⅱ", budget=budget,
                              quote_model=BoardQuote,
                              session_ctx=_session_context(_AFTER_CLOSE),
                              received_at=_AFTER_CLOSE)
    assert snap is not None
    assert snap.source == "stock/get"


def test_trends2_future_time_envelope_rejected(monkeypatch) -> None:
    """root final 反例：完整当日分钟 + time=2030 → 与接收时刻不一致 → 拒绝。"""
    from datetime import datetime as dtdt

    payload = _load("board-trends2.json")
    payload["data"]["time"] = int(dtdt(2030, 1, 1).timestamp())
    budget = ReportBudget(30)
    _install_wire(monkeypatch, _route_by_secid({"trends2:90.BK1277": payload}))
    assert _fetch_trends2_snapshot(
        "90.BK1277", "BK1277", "白酒Ⅱ", budget=budget, quote_model=BoardQuote,
        session_ctx=_session_context(_AFTER_CLOSE),
        received_at=_AFTER_CLOSE) is None


def test_trends2_time_before_last_minute_rejected(monkeypatch) -> None:
    """time 早于完整分钟序列末根（10:00 vs 15:00）= envelope 不一致 → 拒绝。"""
    payload = _load("board-trends2.json")
    payload["data"]["time"] = int(datetime(2026, 11, 6, 10, 0).timestamp())
    budget = ReportBudget(30)
    _install_wire(monkeypatch, _route_by_secid({"trends2:90.BK1277": payload}))
    assert _fetch_trends2_snapshot(
        "90.BK1277", "BK1277", "白酒Ⅱ", budget=budget, quote_model=BoardQuote,
        session_ctx=_session_context(_AFTER_CLOSE),
        received_at=_AFTER_CLOSE) is None


def test_expired_budget_is_budget_exhausted_not_contract_error(monkeypatch) -> None:
    """root final 反例：正常预算耗尽（含截止跨越）必须是 BudgetExhausted。

    旧 @require(not budget.expired()) 把真实
    _collect→stock/get None→trends2 的正常 deadline 跨越变成 ViolationError，
    绕过批次保留与 504 语义变 500。契约不拦正常时间流逝；预算语义仍由
    ReportBudget.require_fetch()/BudgetExhausted 承担。
    """
    import time as _t

    from src.services import sector_analysis_service as svc

    budget = svc.ReportBudget(0.6)
    state = {"n": 0}

    def slow_send(self, prepared, **kwargs):  # noqa: ANN001
        _t.sleep(0.5)  # 单次响应即越过剩余预算（截止在等待中跨越）
        return _wire_response({"data": None})

    monkeypatch.setattr(requests.Session, "send", slow_send)
    batch = _collect_member_quotes(
        pd.DataFrame({"代码": ["600519"], "名称": ["贵州茅台"]}),
        budget=budget)
    # 批次形状保留、缺口为预算语义（无 ViolationError 泄漏）
    assert len(batch.df) == 1
    assert batch.df["成交额"].tolist() == [None]
    joined = "\n".join(batch.gaps)
    assert "预算耗尽" in joined
    assert state["n"] == 0


def test_f10_envelope_type_drift_rejected(monkeypatch) -> None:
    """root final 反例：Pydantic Literal 等值语义接受 success=1/code=false。

    strict wire 类型必须精确：success 精确 bool 且为 True；code 精确 int
    （非 bool）且为 0；类型漂移按源错误拒绝，真实 wire 不受影响。
    """
    budget = ReportBudget(30)
    body_ok = _f10_ok([_f10_row(*_REAL_BAIJIU_ROWS[3])])
    drift = dict(body_ok)
    drift["success"] = 1
    drift["code"] = 0
    _install_wire(monkeypatch, _route_by_secid(
        {"f10members:BK1277": {"1": drift}}))
    assert _fetch_board_members(
        "BK1277", "白酒Ⅱ", "600519", budget=budget) is None
    body2 = dict(body_ok)
    body2["success"] = True
    body2["code"] = False
    _install_wire(monkeypatch, _route_by_secid(
        {"f10members:BK1277": {"1": body2}}))
    assert _fetch_board_members(
        "BK1277", "白酒Ⅱ", "600519", budget=budget) is None


def test_peers_only_projection_carries_actual_asof_and_source(monkeypatch) -> None:
    """root final 反例：peers-only 真实登记→build_synthesis_user_text。

    同业实际供应商时点/时间窗与来源必须进入模型 wire（此前只写「见公开载荷」
    ——模型没有载荷）；旧测试的日期断言被 intraday 块偶然满足，本反例用
    peers-only + 独一无二时刻 14:43 保证逐块独立证明；raw magnitudes 仍排除。
    """
    from src.services import sector_analysis_service as svc
    from src.services import sector_research as sr

    peers = {
        "eligible": True,
        "selection_rule": "同一成分快照按成交额前五 + 目标股去重（成交活跃代表，"
                          "非市值/基本面龙头）；有效报价 1 行内次序（原始观测 "
                          "2 行、身份有效 2 行；缺报价/身份剔除成员资格未知，"
                          "非全体板块排名）",
        "items": [{"code": "600519", "name": "贵州茅台", "amount": None,
                   "change_pct": 0.07, "is_target": True}],
        "target_in_amount_top5": None, "target_change_pct_rank": None,
        "amount_unit": "元",
        "amount_provenance": "成交额单位为元：成分集合来自东财 F10 板块完整成员"
                             "（datacenter RPT_F10_CORETHEME_BOARDTYPE）；行情 "
                             "1 只经 trends2 供应商分钟累计衍生（非原样字段）",
        "snapshot_rows": 2, "rankable_rows": 1, "sample_size": 2,
        "snapshot_observed_at": "2026-11-06T14:44:00",
        "source": "东财 F10 板块完整成员（身份验证同一集合）",
        "quotes_as_of": "trends2 补采 1 只供应商分钟截至 2026-11-06 14:43",
        "gaps": [],
    }
    ident = {"stock_name": "贵州茅台", "board_name": "白酒Ⅱ",
             "board_code": "BK1277", "industry_em": "白酒Ⅱ",
             "membership": {"verified": True, "member_count": 2,
                            "verified_source": "f10_members"}}
    registry = svc._build_evidence_registry(
        "贵州茅台", "600519", ident,
        prosper={"sector_found": False, "row": {}, "gaps": []},
        trend={"eligible": False}, peers=peers, catalysts={})
    wire = sr.build_synthesis_user_text(registry)
    assert "2026-11-06 14:43" in wire  # 实际供应商时点（非泛指）
    assert "trends2" in wire           # 实际来源
    assert "有效报价 1 行" in wire      # 子集范围（真实角色口径）
    assert "供应商分钟累计衍生" in wire
    # raw magnitudes 仍排除（价格/涨幅/金额/分位/收益）
    assert "0.07" not in wire
    limitations = sr.program_fact_limitations(registry)
    assert limitations is not None and "分钟累计衍生" in limitations


def test_zero_amount_target_known_false_vs_missing_unknown(monkeypatch) -> None:
    """root final 反例：合法零额目标曾被旧 _decimal_amount 重新变未知。

    明确口径：合法 0 在公开载荷保留 0；活跃前五仍只按正成交额选；全体报价
    已知时零额目标「不在前五」可判 False；缺报价仍是 None（未知）。
    """
    rows = [
        ("600519", "贵州茅台", 0.0, 1.0),   # 目标：已知零额
        ("000799", "酒鬼酒", 900.0, 0.5),
        ("600197", "伊力特", 800.0, -0.5),
        ("600809", "山西汾酒", 700.0, 2.0),
        ("600199", "金种子酒", 600.0, 0.0),  # 同行零额：合法保留
        ("603919", "金徽酒", 500.0, 1.0),
    ]
    out = _peers_pillar("600519", _peers_df(rows), budget=ReportBudget(30),
                        subject_confirmed=True, quotes_complete=True)
    assert out["target_in_amount_top5"] is False  # 已知零额：可判定不在前五
    target_item = next(i for i in out["items"] if i["is_target"])
    assert target_item["amount"] == 0.0  # 公开载荷保留 0
    assert out["rankable_rows"] == 5     # 只有正额参与活跃排名（目标 0 + 无其他零额）
    joined = "\n".join(out["gaps"])
    assert "合法零额" in joined
    assert "零或负" not in joined
    # 对照：缺报价成员 → 全体资格未知 → None（不冒充 False）
    rows_missing = list(rows)
    rows_missing[5] = ("603919", "金徽酒", None, 1.0)
    out2 = _peers_pillar("600519", _peers_df(rows_missing),
                         budget=ReportBudget(30), subject_confirmed=True,
                         quotes_complete=False)
    assert out2["target_in_amount_top5"] is None


def test_trends2_all_zero_amount_minutes_accepted(monkeypatch) -> None:
    """源分钟累计全为合法 0：不得被不必要的 >0 丢成未知（已知零额快照）。"""
    payload = _load("stock-trends2.json")
    trends = payload["data"]["trends"]
    mutated = []
    for r in trends:
        parts = r.split(",")
        parts[5] = "0"
        parts[6] = "0.00"
        mutated.append(",".join(parts))
    payload["data"]["trends"] = mutated
    budget = ReportBudget(30)
    _install_wire(monkeypatch, _route_by_secid({"trends2:1.600519": payload}))
    snap = _fetch_trends2_snapshot(
        "1.600519", "600519", "贵州茅台", budget=budget, quote_model=MemberQuote,
        session_ctx=_session_context(_AFTER_CLOSE), received_at=_AFTER_CLOSE)
    assert snap is not None
    assert snap.quote.f48 == 0.0  # 已知零额（不补 0 也不丢成未知）


# ---------------------------------------------------------------------------
# root entry-closure 反例回归（真实调用链，仅隔离 Session.send + 挂钟脚本）
# ---------------------------------------------------------------------------

def _scripted_clock(times: List[datetime]):
    """接收钟脚本（monkeypatch svc._received_at_now；按调用次序推进）。"""
    from src.services import sector_analysis_service as svc

    seq = list(times)

    def _clock() -> datetime:
        return seq.pop(0) if seq else seq[-1] if seq else times[-1]

    return svc, _clock


def test_collect_accepts_quote_arriving_after_request(monkeypatch) -> None:
    """root 反例：批次 10:00:00 请求，响应 10:00:02 到达，f86=10:00:01 → 采纳。

    接收钟必须在每个 source 的 HTTP 完成后取得——批次/请求开始时刻不冒充
    响应接收时刻（旧实现按 ctx 锚点判未来误拒，而直接单股入口采纳）。
    """
    from src.services import sector_analysis_service as svc

    raw = _load("stock-quote.json")
    raw["data"]["f86"] = int(datetime(2026, 11, 6, 10, 0, 1).timestamp())
    budget = ReportBudget(30)
    _install_wire(monkeypatch, _route_by_secid({"stockget:1.600519": raw}))
    monkeypatch.setattr(svc, "_received_at_now",
                        lambda: datetime(2026, 11, 6, 10, 0, 2))
    frame = pd.DataFrame({"代码": ["600519"], "名称": ["贵州茅台"]})
    batch = _collect_member_quotes(
        frame, budget=budget,
        session_ctx=_session_context(datetime(2026, 11, 6, 10, 0)))
    assert batch.amounts_known is True
    assert batch.df["成交额"].iloc[0] == 6199528289.0
    # 同一数据直接单股入口同判（两入口一致）
    snap = _fetch_stock_quote("1.600519", "600519", "贵州茅台", budget=budget,
                              quote_model=MemberQuote,
                              session_ctx=_session_context(
                                  datetime(2026, 11, 6, 10, 0)))
    assert snap is not None and "10:00:01" in snap.vendor_as_of


def test_trends_fallback_receive_after_batch_start_accepted(monkeypatch) -> None:
    """root 反例：stock/get 不可用，trends2 于 10:01:05 收到截至 10:01 的
    连续分钟（批次始 10:00:30）→ 采纳，不按批次旧分钟判未来。"""
    from src.services import sector_analysis_service as svc

    payload = _trim_fixture_to(32)  # 09:30..10:01 连续前缀（time 同步末根）
    assert payload["data"]["trends"][-1].startswith("2026-11-06 10:01")
    budget = ReportBudget(60)
    _install_wire(monkeypatch, _route_by_secid({
        "stockget:1.600519": requests.ConnectionError("stock/get 拒绝窗口"),
        "trends2:1.600519": payload,
    }))
    monkeypatch.setattr(svc, "_received_at_now",
                        lambda: datetime(2026, 11, 6, 10, 1, 5))
    batch = _collect_member_quotes(
        pd.DataFrame({"代码": ["600519"], "名称": ["贵州茅台"]}),
        budget=budget,
        session_ctx=_session_context(datetime(2026, 11, 6, 10, 0, 30)))
    assert batch.amounts_known is True
    target = batch.df[batch.df["代码"] == "600519"].iloc[0]
    # 独立核算截尾集涨跌幅（末分钟 close 对 preClose，Decimal 2 位舍入）
    from decimal import Decimal as _D

    last_close = _D(payload["data"]["trends"][-1].split(",")[2])
    expected = float(((_D("1452") - last_close) * 0 + (last_close - _D(str(payload["data"]["preClose"])))
                      / _D(str(payload["data"]["preClose"])) * 100).quantize(_D("0.01")))
    assert float(target["涨跌幅"]) == expected


def test_cross_close_intraday_snapshot_rejected(monkeypatch) -> None:
    """root 反例：14:59:58 请求、15:00:02 实际接收、f86=14:59:30 → 拒绝。

    「会话是否已完结」按接收时刻判定：接收已过收盘 → 盘中旧快照不冒充
    完整会话可比报价（旧实现按请求相位判盘中而误采，amounts_known=True）。
    """
    from src.services import sector_analysis_service as svc

    raw = _load("board-quote.json")
    raw["data"]["f86"] = int(datetime(2026, 11, 6, 14, 59, 30).timestamp())
    budget = ReportBudget(30)
    _install_wire(monkeypatch, _route_by_secid({"stockget:90.BK1277": raw}))
    monkeypatch.setattr(svc, "_received_at_now",
                        lambda: datetime(2026, 11, 6, 15, 0, 2))
    assert _fetch_stock_quote(
        "90.BK1277", "BK1277", "白酒Ⅱ", budget=budget, quote_model=BoardQuote,
        session_ctx=_session_context(datetime(2026, 11, 6, 14, 59, 58))) is None
    frame = pd.DataFrame({"代码": ["600519"], "名称": ["贵州茅台"]})
    _install_wire(monkeypatch, _route_by_secid({
        "stockget:1.600519": raw,
        "trends2:1.600519": requests.ConnectionError("拒绝窗口"),
    }))
    batch = _collect_member_quotes(
        frame, budget=budget,
        session_ctx=_session_context(datetime(2026, 11, 6, 14, 59, 58)))
    assert batch.amounts_known is False


def test_prosperity_real_chain_receive_clock(monkeypatch) -> None:
    """root 反例：生产 _prosperity（传 ctx）板块 fallback 不得把 ctx 当冻结
    接收钟——供应商在接收前更新 f86 → 采纳 sector_found=True。"""
    import akshare as ak
    from src.services import sector_analysis_service as svc

    def _em_down():
        raise RuntimeError("clist 拒绝窗口")

    monkeypatch.setattr(ak, "stock_board_industry_name_em", _em_down)
    raw = _load("board-quote.json")
    raw["data"]["f86"] = int(datetime(2026, 11, 6, 10, 0, 1).timestamp())
    budget = ReportBudget(60)
    _install_wire(monkeypatch, _route_by_secid({"stockget:90.BK1277": raw}))
    monkeypatch.setattr(svc, "_received_at_now",
                        lambda: datetime(2026, 11, 6, 10, 0, 2))
    out = _prosperity_pillar(
        "白酒Ⅱ", budget=budget, board_code="BK1277", board_table=None,
        now=datetime(2026, 11, 6, 10, 0))
    assert out["sector_found"] is True
    assert out["row"]["board_code"] == "BK1277"


def test_clist_incomplete_quotes_derive_incomplete_and_template_claim(monkeypatch):
    """root 反例：clist 缺额成员仍宣称「全体成员报价完整」+ top5=True。

    完整性从最终实际成员行推导（未知成员若是 1000，目标 100 实际不在前五
    ——反事实判 False）；外传 flag 只能降低；模板全体宣称仅在有资格判定时
    出现；selection_rule 携带有效子集范围。
    """
    from jinja2 import Environment, FileSystemLoader

    from src.services.sector_analysis_service import _render  # noqa: F401

    rows = [("600519", "贵州茅台", 100.0, 1.0),
            ("000799", "酒鬼酒", 500.0, 0.5),
            ("600197", "伊力特", 400.0, -0.5),
            ("600809", "山西汾酒", 300.0, 2.0),
            ("600199", "金种子酒", 200.0, 0.0),
            ("603919", "金徽酒", None, 1.0)]
    out = _peers_pillar("600519", _peers_df(rows), budget=ReportBudget(30),
                        subject_confirmed=True)  # clist 路径：未传 quotes_complete
    assert out["target_in_amount_top5"] is None  # 未知成员 → 资格未知
    assert "有效报价 5 行内次序（原始观测 6 行、身份有效 6 行" in out["selection_rule"]
    assert "原始观测 6 行" in "\n".join(out["gaps"])
    # 反事实：未知成员实际为 1000 → 目标不在前五（True 是错误断言）
    completed = _peers_df(rows)
    completed.loc[completed["代码"] == "603919", "成交额"] = 1000.0
    opposite = _peers_pillar("600519", completed, budget=ReportBudget(30),
                             subject_confirmed=True)
    assert opposite["target_in_amount_top5"] is False

    # 模板最终入口：target_in=None 时不得出现「全体成员报价完整」宣称
    env = Environment(loader=FileSystemLoader("templates"),
                      autoescape=False, trim_blocks=True, lstrip_blocks=True)
    env.filters["md_link_text"] = (
        __import__("src.services.sector_analysis_service", fromlist=["_md_link_text"])
        ._md_link_text)
    peers_block = dict(out)
    peers_block["items"] = [
        {"code": c, "name": n, "amount": a, "change_pct": ch,
         "is_target": c == "600519"}
        for c, n, a, ch in rows
    ]
    template_src = open("templates/sector_analysis_report.j2").read()
    start = template_src.index(
        "{% if peers.target_in_amount_top5 is false %}")
    end = template_src.index("- ⚠️ 口径说明", start)
    fragment = template_src[start:end]
    md = env.from_string(
        "{% set peer_rows = peers['items'] %}\n" + fragment
    ).render(peers=peers_block)
    assert "全体成员报价完整" not in md
    # 对照：全体完整（top5 判 False/True）时宣称保留
    peers_block2 = dict(peers_block)
    peers_block2["target_in_amount_top5"] = False
    md2 = env.from_string(
        "{% set peer_rows = peers['items'] %}\n" + fragment
    ).render(peers=peers_block2)
    assert "全体成员报价完整" in md2


def test_f10_program_valueerror_api_500_not_400(monkeypatch) -> None:
    """root 反例：新 F10 传输层程序 ValueError 经真实 generate→API 得 400。

    输入错误窄化：仅 SectorInputError/MarketDisabledError 400；程序
    ValueError/TypeError/AssertionError 走通用 500（缺陷原文不冒充请求错误）。
    合法/非法输入对照：非法代码 SectorInputError→400；合法代码（F10 TypeError
    /AssertionError）→500。
    """
    import asyncio
    from unittest.mock import patch

    from fastapi import HTTPException

    from api.v1.endpoints.sector_analysis import generate
    from src.services import sector_analysis_service as svc

    def ak_unavailable(*a, **k):  # noqa: ANN002
        raise requests.ConnectionError("legacy vendor unavailable")

    def f10_program_value_error(self, request, **kwargs):  # noqa: ANN001
        raise ValueError("synthetic program defect in new F10 transport")

    with patch.object(svc, "_bounded_ak", ak_unavailable), \
         patch.object(requests.Session, "send", f10_program_value_error):
        try:
            asyncio.run(generate("600519", "贵州茅台"))
            raise AssertionError("program error unexpectedly succeeded")
        except HTTPException as e:
            assert e.status_code == 500, f"程序 ValueError 必须 500，实际 {e.status_code}"
            assert "400" not in str(e.status_code)

    def f10_program_type_error(self, request, **kwargs):  # noqa: ANN001
        raise TypeError("synthetic program TypeError")

    with patch.object(svc, "_bounded_ak", ak_unavailable), \
         patch.object(requests.Session, "send", f10_program_type_error):
        try:
            asyncio.run(generate("600519", "贵州茅台"))
            raise AssertionError("program error unexpectedly succeeded")
        except HTTPException as e:
            assert e.status_code == 500

    # 非法输入对照：无效 A 股代码 → SectorInputError（ValueError 子类）→ 400
    def f10_never(self, request, **kwargs):  # noqa: ANN001
        raise AssertionError("无效代码不得出站")

    with patch.object(requests.Session, "send", f10_never):
        try:
            asyncio.run(generate("60051", "样本"))
            raise AssertionError("invalid code unexpectedly succeeded")
        except HTTPException as e:
            assert e.status_code == 400
        except svc.SectorInputError:
            pass  # 控制器外直接调用时按类型传播（API 层映射 400 已由上一分支证明）


# ---------------------------------------------------------------------------
# minor-review-closure 三窄问题回归（真实最终入口）
# ---------------------------------------------------------------------------

def test_peers_role_counts_three_counterfactuals_and_model_wire(monkeypatch):
    """root minor 反例：过滤后计数冒充观测分母/已知总成员。

    原始观测行数必须保留（不把 6 原始观测写成 5/5 或 4/4），selection_rule
    与同义 gaps 按真实角色（原始观测/身份有效/正额可排名）表述，真实成员
    总数未知时不称已知总数；同一 selection_rule 进真实模型 wire。
    """
    from src.services import sector_analysis_service as svc
    from src.services import sector_research as sr

    # 对照 1：非法代码行（非 6 位）剔除后，分母保留原始观测
    rows_bad = [("600519", "贵州茅台", 100.0, 1.0),
                ("000799", "酒鬼酒", 500.0, 0.5),
                ("BADCODE", "非法规行", 900.0, 2.0)]
    df_bad = pd.DataFrame({"代码": [r[0] for r in rows_bad],
                           "名称": [r[1] for r in rows_bad],
                           "成交额": [r[2] for r in rows_bad],
                           "涨跌幅": [r[3] for r in rows_bad]})
    out = _peers_pillar("600519", df_bad, budget=ReportBudget(30),
                        subject_confirmed=True)
    assert out["snapshot_rows"] == 3  # 原始观测保留
    assert out["rankable_rows"] == 2
    assert "有效报价 2 行内次序（原始观测 3 行、身份有效 2 行" in out["selection_rule"]
    assert "身份有效 2 行" in "\n".join(out["gaps"])

    # 对照 2：重复冲突行剔除（unique 真实成员总数未知 → 不称已知总成员）
    rows_dup = [("600519", "贵州茅台", 100.0, 1.0),
                ("600519", "贵州茅台", 999.0, 1.0),
                ("000799", "酒鬼酒", 500.0, 0.5)]
    df_dup = pd.DataFrame({"代码": [r[0] for r in rows_dup],
                           "名称": [r[1] for r in rows_dup],
                           "成交额": [r[2] for r in rows_dup],
                           "涨跌幅": [r[3] for r in rows_dup]})
    out2 = _peers_pillar("600519", df_dup, budget=ReportBudget(30),
                         subject_confirmed=True)
    assert out2["snapshot_rows"] == 3  # 原始观测保留
    assert out2["rankable_rows"] == 1  # 冲突组整组出局，仅 000799 正额可排名
    assert out2["target_in_amount_top5"] is None  # 目标组整组出局（成员不存在）
    assert "原始观测 3 行、身份有效 1 行" in out2["selection_rule"]
    assert any("重复代码" in g for g in out2["gaps"])

    # 对照 3：普通缺额（None）→ 未知成员 + 角色口径
    rows_missing = [("600519", "贵州茅台", 100.0, 1.0),
                    ("000799", "酒鬼酒", None, 0.5)]
    df_missing = pd.DataFrame({"代码": [r[0] for r in rows_missing],
                               "名称": [r[1] for r in rows_missing],
                               "成交额": [r[2] for r in rows_missing],
                               "涨跌幅": [r[3] for r in rows_missing]})
    out3 = _peers_pillar("600519", df_missing, budget=ReportBudget(30),
                         subject_confirmed=True)
    assert out3["snapshot_rows"] == 2
    assert out3["target_in_amount_top5"] is None
    assert "有效报价 1 行内次序（原始观测 2 行、身份有效 2 行" in out3["selection_rule"]

    # 真实模型 wire：同一 selection_rule（角色口径）进 build_synthesis_user_text
    ident = {"stock_name": "贵州茅台", "board_name": "白酒Ⅱ",
             "board_code": "BK1277", "industry_em": "白酒Ⅱ",
             "membership": {"verified": True, "member_count": 2,
                            "verified_source": "f10_members"}}
    peers_block = dict(out)
    peers_block["items"] = [
        {"code": c, "name": n, "amount": a, "change_pct": ch,
         "is_target": c == "600519"}
        for c, n, a, ch in rows_bad
    ]
    registry = svc._build_evidence_registry(
        "贵州茅台", "600519", ident,
        prosper={"sector_found": False, "row": {}, "gaps": []},
        trend={"eligible": False}, peers=peers_block, catalysts={})
    wire = sr.build_synthesis_user_text(registry)
    assert "有效报价 2 行内次序（原始观测 3 行、身份有效 2 行" in wire


def test_api_500_fixed_text_log_no_marker_and_annotation(monkeypatch):
    """root minor 反例：500 detail=f'{exc}' 泄漏异常原文，日志含值 traceback。

    500 固定安全文案；日志仅异常类型 + filename/lineno/function（无参数值/
    源码行/locals）；合成 marker 证明响应与日志都无原料；非法代码 400/无出站
    不变；公开 async generate 返回类型注解为 SectorAnalysisGenerateResponse。
    """
    import asyncio
    import inspect
    from unittest.mock import patch

    from fastapi import HTTPException

    from api.v1.endpoints import sector_analysis as endpoint_module
    from api.v1.endpoints.sector_analysis import generate
    from src.services import sector_analysis_service as svc

    # 公开返回注解
    sig = inspect.signature(generate)
    assert sig.return_annotation is not None
    annotation = sig.return_annotation
    annotation_name = (annotation if isinstance(annotation, str)
                       else getattr(annotation, "__name__", str(annotation)))
    assert "SectorAnalysisGenerateResponse" in annotation_name

    marker = "MARKER-SECRET-424242"

    def ak_unavailable(*a, **k):  # noqa: ANN002
        raise requests.ConnectionError("legacy vendor unavailable")

    def f10_marker_error(self, request, **kwargs):  # noqa: ANN001
        raise ValueError(f"synthetic program defect {marker}")

    with patch.object(svc, "_bounded_ak", ak_unavailable), \
         patch.object(requests.Session, "send", f10_marker_error), \
         patch.object(endpoint_module.logger, "error") as log_spy:
        try:
            asyncio.run(generate("600519", "贵州茅台"))
            raise AssertionError("program error unexpectedly succeeded")
        except HTTPException as e:
            assert e.status_code == 500
            # 固定安全文案：响应 detail 无异常原文/marker
            assert e.detail == "生成失败：内部错误（详见服务端日志）"
            assert marker not in str(e.detail)
        # 日志：无异常原文/参数值（marker 不出现）；保留类型与不含值位置
        logged = " ".join(str(call.args) for call in log_spy.call_args_list)
        assert marker not in logged
        assert "ValueError" in logged  # 异常类型保留
        assert ".py:" in logged.replace("\\", "/") or any(
            isinstance(a, str) and a.endswith(".py") or (isinstance(a, int))
            for call in log_spy.call_args_list for a in call.args)

    # 非法代码对照：400、无出站不变
    def f10_never(self, request, **kwargs):  # noqa: ANN001
        raise AssertionError("无效代码不得出站")

    with patch.object(requests.Session, "send", f10_never):
        try:
            asyncio.run(generate("60051", "样本"))
            raise AssertionError("invalid code unexpectedly succeeded")
        except HTTPException as e:
            assert e.status_code == 400
        except svc.SectorInputError:
            pass  # 控制器外直接调用按类型传播（API 层映射 400 已由上一分支证明）
