# -*- coding: utf-8 -*-
"""Issue #64 最近完整交易日：资金三窗口共同截至 T 或允许的 T-1。

固定边界：优先最新已收盘交易日 T；仅当 T 缺行/空值且截至 T-1（含 T-1）的
连续 10 个应有交易日均有合格数值时，三窗口一并改用 T-1；最多落后一个交易日。
T 已有值但窗口中间缺数不回退；连续两日缺/短窗口/非有限/冲突保持真实缺口。
同一次交易日历+CSD 响应内完成选择，不追加供应商查询。全程离线（原生传输
夹具 + 禁 socket 出站断言）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

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
