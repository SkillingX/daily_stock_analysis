# -*- coding: utf-8 -*-
"""Issue #64 最近完整交易日：资金三窗口共同截至 T 或允许的 T-1。

固定边界：优先最新已收盘交易日 T；仅当 T 缺行/空值且截至 T-1（含 T-1）的
连续 10 个应有交易日均有合格数值时，三窗口一并改用 T-1；最多落后一个交易日。
T 已有值但窗口中间缺数不回退；连续两日缺/短窗口/非有限/冲突保持真实缺口。
同一次交易日历+CSD 响应内完成选择，不追加供应商查询。全程离线（原生传输
夹具 + 禁 socket 出站断言）。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import pytest

from tests.test_choice_integration import choice_transport
from tests.test_issue64_no_target_mode import (
    _NO_TARGET_RESEARCH_DIMS,
    _assert_single_done_terminal,
    _no_target_vertical_env,
    _sse_events,
)

# 12 个连续应有交易日（倒序展示；T=2026-10-29，T-1=2026-10-28）
TRADING_DAYS: List[str] = [
    "2026-10-29", "2026-10-28", "2026-10-27", "2026-10-26", "2026-10-23",
    "2026-10-22", "2026-10-21", "2026-10-20", "2026-10-19", "2026-10-16",
    "2026-10-15", "2026-10-14",
]


class SocketBlocked(AssertionError):
    pass


@pytest.fixture()
def choice_capital(monkeypatch: pytest.MonkeyPatch):
    """原生 Choice 传输夹具：tradedates 固定日历，csd NETINFLOW 由用例给值。

    返回 (set_series, calls)；``set_series`` 接受 {iso_date: value-or-None}，
    未列出的交易日视为缺行。socket.socket 被替换为直接失败，真实出站零容忍。
    """
    import socket

    import socket as socket_module

    from data_provider.base import DataFetcherManager
    from data_provider.choice_adapter import ChoiceAdapter
    from src.config import Config
    from src.agent.tools import data_tools

    config = Config(data_source_profile="choice", deep_research_cross_validate=False)
    monkeypatch.setattr(Config, "_instance", config)

    def no_socket(*args: Any, **kwargs: Any) -> Any:
        raise SocketBlocked("real outbound socket is forbidden in this test")

    monkeypatch.setattr(socket_module, "socket", no_socket)

    calls: List[str] = []
    series: Dict[str, Optional[float]] = {}
    ordered = list(reversed(TRADING_DAYS))  # 供应商按升序返回

    def request(method: str, args: Any, options: Any, deadline: Any) -> Dict[str, Any]:
        calls.append(method)
        if method == "tradedates":
            return {"data": list(reversed(TRADING_DAYS))}
        assert method == "csd" and args[1] == "NETINFLOW"
        ordered = list(reversed(TRADING_DAYS))  # 供应商按升序返回
        dates: List[str] = []
        values: List[Optional[float]] = []
        for index, day in enumerate(ordered):
            if day in series and series[day] is None:
                dates.append(day)
                values.append(None)  # 空值行
                continue
            if day in series:
                dates.append(day)
                values.append(series[day])
                continue
            # 缺行：跳过该日期列
        return {
            "codes": [args[0]],
            "indicators": ["NETINFLOW"],
            "dates": dates,
            "data": {args[0]: [values]},
        }

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(request))
    manager = DataFetcherManager()
    monkeypatch.setattr(data_tools, "_fetcher_manager_singleton", manager)

    def set_series(values: Dict[str, Optional[float]]) -> None:
        series.clear()
        series.update(values)

    return set_series, calls, ordered


def _flow() -> Dict[str, Any]:
    from src.agent.tools.data_tools import _handle_get_capital_flow

    # 深研路径语义：零行业榜出站（issue #64 退役边界）
    return _handle_get_capital_flow("300398", include_sector_flows=False)


def test_complete_t_preferred_with_as_of_t(choice_capital) -> None:
    set_series, calls, ordered = choice_capital
    set_series({day: float(index + 1) for index, day in enumerate(ordered)})
    flow = _flow()
    assert calls.count("csd") == 1, "同次交易日历+CSD 响应内完成，不追加查询"
    assert flow["status"] == "ok"
    assert flow["expected_date"] == "2026-10-29"
    assert flow["as_of_date"] == "2026-10-29"
    # 升序日历：T=10-29 值 12.0；5日=12+11+10+9+8=50；10日=12+11+…+3=75（Decimal 合计，单位元）
    assert flow["main_net_inflow"] == 12.0
    assert flow["inflow_5d"] == 50.0
    assert flow["inflow_10d"] == 75.0
    assert flow["coverage"]["1"]["complete"] and flow["coverage"]["10"]["complete"]
    assert flow["coverage"]["10"]["expected_dates"][0] == "2026-10-29"
    record = flow["field_meta"]["main_inflow"]
    assert record["observed_at"] == "2026-10-29"


def test_t_absent_prior_ten_complete_adopts_t_minus_one(choice_capital) -> None:
    set_series, calls, ordered = choice_capital
    values = {day: float(index + 1) for index, day in enumerate(ordered)}
    values["2026-10-29"] = None  # T 空值
    set_series(values)
    flow = _flow()
    assert calls.count("csd") == 1
    assert flow["status"] == "ok", "允许范围内的前一交易日完整数据属于有效输入，不记未完整"
    assert not flow["errors"]
    assert flow["expected_date"] == "2026-10-29", "预期最新已收盘日保留"
    assert flow["as_of_date"] == "2026-10-28"
    assert flow["latest_date"] == "2026-10-28"
    # 单日=9.0；5日=9+8+7+6+5=35；10日=9+8+…+0（10-15 的值 index=11 → 12.0? 无：
    # ordered 升序，index: 10-14→0 … 10-28→10；T-1=10-28 值=11.0；10 日=11+10+…+2=65
    assert flow["main_net_inflow"] == 11.0
    assert flow["inflow_5d"] == 11 + 10 + 9 + 8 + 7
    assert flow["inflow_10d"] == sum(range(2, 12))
    coverage = flow["coverage"]
    assert coverage["10"]["expected_dates"] == [
        "2026-10-28", "2026-10-27", "2026-10-26", "2026-10-23", "2026-10-22",
        "2026-10-21", "2026-10-20", "2026-10-19", "2026-10-16", "2026-10-15",
    ]
    assert coverage["10"]["complete"]
    assert coverage["pending_day"] == {"expected_date": "2026-10-29", "observed": False}
    assert flow["field_meta"]["main_inflow"]["observed_at"] == "2026-10-28"
    note = str(flow.get("note") or "")
    assert "2026-10-28" in note and "2026-10-29" in note, "实际截至日与未就绪日都须披露"
    assert "当日" not in note.replace("单日", ""), "不得把前日写成报告当天"


def test_t_and_prior_both_missing_keeps_real_gap(choice_capital) -> None:
    set_series, calls, ordered = choice_capital
    values = {day: float(index + 1) for index, day in enumerate(ordered)}
    values["2026-10-29"] = None
    values["2026-10-28"] = None  # T-1 也缺 → 不得回退到更早
    set_series(values)
    flow = _flow()
    assert flow["as_of_date"] == "2026-10-29"
    assert flow["main_net_inflow"] is None
    assert not flow["coverage"]["1"]["complete"]
    assert flow["errors"], "连续两日缺失按真实缺口处理"


def test_t_value_present_with_window_hole_does_not_fall_back(choice_capital) -> None:
    set_series, calls, ordered = choice_capital
    values = {day: float(index + 1) for index, day in enumerate(ordered)}
    values["2026-10-20"] = None  # 10 日窗口中间缺数；T 有值
    set_series(values)
    flow = _flow()
    assert flow["as_of_date"] == "2026-10-29"
    assert flow["main_net_inflow"] == 12.0  # 单日窗口完整
    assert flow["inflow_10d"] is None  # 中间洞 → 不用 T-1 掩盖
    assert not flow["coverage"]["10"]["complete"]
    assert flow["coverage"]["10"]["missing_dates"] == ["2026-10-20"]


def test_short_history_blocks_prior_day_adoption(choice_capital) -> None:
    set_series, calls, ordered = choice_capital
    # 只有 T-1 结束的 6 个交易日有值（不足连续 10 日）→ 不允许回退
    values = {day: float(index + 1) for index, day in enumerate(ordered)}
    for day in ordered[8:]:
        values.pop(day, None)
    values["2026-10-29"] = None
    set_series(values)
    flow = _flow()
    assert flow["as_of_date"] == "2026-10-29"
    assert flow["main_net_inflow"] is None
    assert any("window_1_incomplete" in error for error in flow["errors"])


def test_nonfinite_value_is_rejected_not_adopted(choice_capital) -> None:
    set_series, calls, ordered = choice_capital
    values = {day: float(index + 1) for index, day in enumerate(ordered)}
    values["2026-10-20"] = float("inf")  # 非有限数：传输层响应校验直接拒绝
    set_series(values)
    flow = _flow()
    assert flow["status"] == "partial"
    assert flow["inflow_10d"] is None
    assert any("invalid_response" in error for error in flow["errors"]), "非有限数按真实错误处理"


def test_conflicting_duplicate_values_rejected(choice_capital) -> None:
    set_series, calls, ordered = choice_capital
    values = {day: float(index + 1) for index, day in enumerate(ordered)}
    values["2026-10-20"] = 99.0
    set_series(values)
    # 重复冲突由 compute_cumulative 的同日不同值拒绝路径覆盖；直接断言合计层
    from data_provider.capital_flow_provider import compute_cumulative

    series = [(day, values.get(day)) for day in reversed(ordered)]
    series.append(("2026-10-20", 42.0))  # 同日第二值且不同
    assert compute_cumulative(series, 10, trading_dates=ordered, source_unit="CNY") is None


def test_zero_and_negative_values_are_legal(choice_capital) -> None:
    set_series, calls, ordered = choice_capital
    set_series({day: (0.0 if index % 2 else -float(index + 1)) for index, day in enumerate(ordered)})
    flow = _flow()
    assert flow["status"] == "ok"
    assert flow["main_net_inflow"] == 0.0
    # 10 日窗口 = 升序 ordered[2..11]：-3,0,-5,0,-7,0,-9,0,-11,0 → -35.0
    assert flow["inflow_10d"] == -35.0


# ---------------------------------------------------------------------------
# T-1 合法采用：真实 Choice 响应（T 空值、前 10 应有交易日完整）经
# manager/阶段0复用 → 资金单轮分析 → 评分/结论 → 落盘/API/MD/PDF/SSE 一致
# （PR #65 评审缺口1：此前该正例止步于 parser→tool 投影，未走完整垂直链）
# ---------------------------------------------------------------------------


def test_t_minus_one_vertical_report_api_md_pdf_sse_consistency(
    tmp_path, monkeypatch: pytest.MonkeyPatch, choice_transport: Any,
) -> None:
    """规格 §2.1 T-1 垂直验收（受控原生传输，零网络零真实模型）。

    T 空值且截至 T-1（含）连续 10 个应有交易日均有合格数值 → 三窗口合法采用
    T-1。走真实 API SSE 新鲜交付（自然 done/单终态/EOF），断言：同一日历+CSD
    响应内完成选择且阶段0复用使资金研究零追加供应商查询；expected_date=T 与
    as_of_date=T-1/observed_at 一致；三窗口共同截至 T-1 的金额；代码固定的
    未就绪日说明；前日金额不得写成报告当天；落盘历史/API detail/dims/MD/PDF
    同一口径；缓存 SSE 载荷保持 T-1 口径；资金分进入六维评分，非估值三项
    保持完成。
    """
    from data_provider.choice_adapter import ChoiceAdapter
    from fastapi.testclient import TestClient

    service, database, app, no_target_transport = _no_target_vertical_env(
        tmp_path, monkeypatch, choice_transport,
    )
    try:
        # 冻结收盘边界为最近已收盘交易日（收盘后语义）：T 与日历断言与运行时刻
        # 无关地确定。周六/周日运行时先回退到周五——受控日历只含交易日，
        # expected_t 必须有日历行，否则 T 无空值行、资金直接选周五为 T 且不回退，
        # 与 expected_date/T-1 采用断言矛盾（PR #65 周末边界反例）。
        today = date.today()
        while today.weekday() >= 5:  # 周六=5，周日=6
            today -= timedelta(days=1)
        monkeypatch.setattr(ChoiceAdapter, "completed_day", staticmethod(lambda: today))
        expected_t = today.isoformat()
        # 升序 12 个应有交易日（跳过周末的真实交易日形态）：T 结尾，T-1 起连续 10 日完整。
        trading_desc: List[str] = []
        cursor = today
        while len(trading_desc) < 12:
            if cursor.weekday() < 5:  # 周一..周五
                trading_desc.append(cursor.isoformat())
            cursor -= timedelta(days=1)
        calendar_asc = list(reversed(trading_desc))
        as_of_t1 = calendar_asc[-2]

        # method+args 归属：同一报告内估值用 14 日日历（choice_adapter 既有行为）、
        # 资金用 60 日日历+一次 NETINFLOW CSD——资金选择不得重复出站。
        calls: List[Any] = []

        def t_minus_one_transport(method: str, args: Any, options: Any, deadline: Any) -> Dict[str, Any]:
            calls.append((method, tuple(str(a) for a in args)))
            if method == "tradedates":
                # 供应商按请求窗口返回：估值 14 日窗与资金 60 日窗各取交集。
                start, end = str(args[0]), str(args[1])
                return {
                    "codes": [],
                    "indicators": [],
                    "dates": [],
                    "data": [day for day in calendar_asc if start <= day <= end],
                }
            if method == "csd":
                assert args[1] == "NETINFLOW"
                dates: List[str] = []
                values: List[Optional[float]] = []
                for index, day in enumerate(calendar_asc):
                    dates.append(day)
                    # T 空值行；T-1 起（含）千元级合格数值（升序 1000→11000）。
                    values.append(None if day == expected_t else float((index + 1) * 1000.0))
                return {
                    "codes": [args[0]],
                    "indicators": ["NETINFLOW"],
                    "dates": dates,
                    "data": {args[0]: [values]},
                }
            return no_target_transport(method, args, options, deadline)

        monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(t_minus_one_transport))

        with TestClient(app) as client:
            stream = client.post(
                "/generate/stream",
                json={
                    "stock_code": "300398", "stock_name": "飞凯材料",
                    "dims": _NO_TARGET_RESEARCH_DIMS, "force_refresh": True,
                },
            )
            assert stream.status_code == 200, stream.text
            events = _sse_events(stream)
            done = _assert_single_done_terminal(events)
            rid = done["report_id"]
            assert rid and done["status"] == "success", (done["status"], done.get("error"))
            assert "capital" not in done["missing_layers"]
            assert not {"scenarios", "conclusion", "signal"} & set(done["missing_layers"])

            # 同一次交易日历+CSD 响应内完成 T/T-1 选择；资金选择一次出站且阶段0
            # 复用后零追加资金查询；估值 14 日日历为合法的另一需求（保留不变）。
            tradedates_calls = [(args[0], args[1]) for method, args in calls if method == "tradedates"]
            csd_calls = [(args[0], args[1]) for method, args in calls if method == "csd"]
            calendar_windows = sorted(
                (date.fromisoformat(end) - date.fromisoformat(start)).days
                for start, end in tradedates_calls
            )
            assert csd_calls and all(indicator == "NETINFLOW" for _, indicator in csd_calls)
            assert len(csd_calls) == 1, (
                f"资金 NETINFLOW CSD 不得重复出站: calls={[(m, a) for m, a in calls]}"
            )
            assert len(tradedates_calls) == 2 and calendar_windows == [14, 60], (
                f"日历调用须按需求归属（估值14日/资金60日）: windows={calendar_windows} "
                f"calls={[(m, a) for m, a in calls]}"
            )

            dims = done["dimensions"]
            capital = dims["capital"]
            assert capital["status"] == "ok", capital["degraded_reason"]
            assert capital["as_of_date"] == as_of_t1, "三窗口实际共同截至 T-1"
            assert capital["expected_date"] == expected_t, "预期最新已收盘日保留 T"
            assert capital["score"] == 70.0, "资金总分 = 合格 flow_score"
            assert capital["flow_summary"] == (
                f"Choice 主力净流入，截至{as_of_t1}已收盘交易日：单日 1.10 万元；"
                f"近5交易日 4.50 万元；近10交易日 6.50 万元；{expected_t}资金窗口尚未完整。"
                "单源数据，未独立核验。"
            ), capital["flow_summary"]
            # 留存事实 period 来自 field_meta.observed_at（T-1），不拿序列最新有效日充数
            flow_facts = [f for f in capital["retained_facts"] if f["tool"] == "get_capital_flow"]
            assert flow_facts and {f["period"] for f in flow_facts} == {as_of_t1}, flow_facts

            # 评分/结论链：资金面 70 进入六维框架，非估值三项保持完成
            framework = dims["six_dim"]["framework"]["dimensions"]
            assert next(d for d in framework if d["dimension"] == "资金面")["score"] == 70.0
            assert dims["conclusion"]["status"] == "ok", dims["conclusion"]["degraded_reason"]
            assert dims["conclusion"]["conclusion"]["action"] == "观察"
            assert dims["signal"]["status"] == "ok" and dims["signal"]["rating"] == "中性"

            markdown = done["markdown"]
            assert f"截至{as_of_t1}已收盘交易日" in markdown, "实际截至日贯穿 MD"
            assert f"{expected_t}资金窗口尚未完整" in markdown, "明确的未就绪日说明"
            assert f"资金数据截至{expected_t}" not in markdown, "不得把 T 冒充资金数据截至日"
            assert "单日 1.10 万元" in markdown and "近10交易日 6.50 万元" in markdown

            # 落盘历史/API detail/dims 与 done 载荷同一口径
            detail = client.get(f"/reports/{rid}").json()["data"]
            assert detail["markdown"] == markdown
            assert detail["dimensions"]["capital"]["as_of_date"] == as_of_t1
            assert detail["dimensions"]["capital"]["expected_date"] == expected_t
            dims_payload = client.get(f"/reports/{rid}/dims").json()["data"]
            assert dims_payload["dimensions"]["capital"]["as_of_date"] == as_of_t1

            # 缓存 SSE：报告日缓存采用边界用真实时钟（B2/R12 保守契约）——仅当
            # expected_t == 真实今天（工作日收盘后语义）时命中同一报告；真实周末
            # 跨收盘时旧 T-1 缓存被保守拒绝并按既有刷新路径生成新报告（非缺陷）。
            # 两种结果都要求资金实际/预期日期与金额语义和首次交付完全一致，
            # 单终态/自然完成契约不放宽。
            expected_hit = expected_t == date.today().isoformat()
            cached = client.post(
                "/generate/stream",
                json={
                    "stock_code": "300398", "stock_name": "飞凯材料",
                    "dims": _NO_TARGET_RESEARCH_DIMS, "force_refresh": False,
                },
            )
            assert cached.status_code == 200, cached.text
            cached_done = _assert_single_done_terminal(_sse_events(cached))
            if expected_hit:
                assert cached_done["cache_hit"] is True, "工作日同日同子集经报告日缓存复用"
                assert cached_done["report_id"] == rid
            else:
                assert cached_done.get("cache_hit") is not True, \
                    "周末跨收盘：旧 T-1 缓存被保守拒绝（正常刷新），不得伪装命中"
                assert cached_done["report_id"] and cached_done["report_id"] != rid, \
                    "缓存 miss 经既有刷新路径生成新报告"
                assert cached_done["status"] == "success", (cached_done["status"], cached_done.get("error"))
                assert not {"scenarios", "conclusion", "signal"} & set(cached_done["missing_layers"])
            cached_capital = cached_done["dimensions"]["capital"]
            assert cached_capital["as_of_date"] == as_of_t1, "资金实际截至日与首次交付一致"
            assert cached_capital["expected_date"] == expected_t, "资金预期交易日与首次交付一致"
            assert cached_capital["score"] == capital["score"] and cached_capital["flow_summary"] == capital["flow_summary"], \
                "实际金额语义与首次交付完全一致"

            # PDF：真实渲染路径，前日与未就绪说明随导出保留
            pdf = client.get(f"/reports/{rid}/pdf")
            assert pdf.status_code == 200, pdf.text
            assert pdf.content[:5] == b"%PDF-", "真实渲染字节"
            import io

            from pypdf import PdfReader

            pdf_text = "".join(
                page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf.content)).pages
            )
            # PDF 提取文本可能插入排版空白：本地真实导出中 flow_summary 已观察到
            # 短语在行尾被断行拆开。提取层先去空白再比对；标签+金额+单位仍整段
            # 精确校验，not-in 同样收紧，不留排版空白造成的假阴性。
            pdf_compact = "".join(pdf_text.split())
            assert f"截至{as_of_t1}已收盘交易日" in pdf_compact
            assert f"{expected_t}资金窗口尚未完整" in pdf_compact
            assert f"资金数据截至{expected_t}" not in pdf_compact, "PDF 同样不得用当日冒充前日"
            assert "单日1.10万元" in pdf_compact and "近10交易日6.50万元" in pdf_compact
    finally:
        database._engine.dispose()
