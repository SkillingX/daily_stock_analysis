# -*- coding: utf-8 -*-
"""旧一级 20 点趋势与参考口径的收盘边界取时回归。

复审反例：报告入口 2026-11-19 14:59:59（盘中），L1 快照 15:00:04 到达
且返回空（quote_target_ok=False），趋势日线给出 10/23–11/19 共 20 个
交易日。趋势按查询时实时钟取窗口 → 20/20 合格 → 不兜底、保留申万、
东财零出站；钉在入口钟则窗口 10/22–11/18、19/20 → 误触发东财兜底
（修复前本用例以东财哨兵 AssertionError 为红证据）。显式 current_time
直传时窗口按传入钟取值（旧代码语义）；入口钟仍贯穿会话归属与
``reference_now``（#103 多窗口沿用入口钟由
``test_sector_trend_windows_v103.test_reference_clock_passed_by_orchestration``
覆盖，不重复）。

真实 XSHG 日历（交易日历不 mock）；钟缝受推进钟替换；申万传输以函数缝
受控；资格谓词、趋势与兜底判定、compose 编排真实执行。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.core import trading_calendar
from src.schemas.sector_analysis import SectorReportPayload
from src.services import sector_analysis_service as svc
from src.services import sector_sw_market as sw
from src.services.sector_analysis_service import (
    _sw_market_chain,
    compose_report,
)
from src.services.sector_sw_market import SW_L1_DIRECTORY_VERSION
from src.services.sector_sw_market import SwSectorFetchError

from tests.test_sector_sw_compose_v99 import (
    _budget,
    _catalysts_stub_payload,
    _synthesis_stub_payload,
)

ENTRY = datetime(2026, 11, 19, 14, 59, 59)      # 报告入口：盘中
AFTER_CLOSE = datetime(2026, 11, 19, 15, 0, 4)  # 快照到达：已收盘
_TZ = ZoneInfo("Asia/Shanghai")
_COAL = "801950.SL"
_BENCH = "000300.SH"

_REAL_GET_MARKET_NOW = trading_calendar.get_market_now


def _window20(at: datetime) -> List[date]:
    sessions = trading_calendar.recent_completed_trading_sessions(
        "cn", 20, current_time=at)
    assert sessions is not None and len(sessions) == 20
    return sorted(sessions)


class _ArrivalClock:
    """市场钟替身：显式传入 current_time 透传真实实现，否则取推进钟。"""

    def __call__(self, market: Optional[str],
                 current_time: Optional[datetime] = None) -> datetime:
        if current_time is not None:
            return _REAL_GET_MARKET_NOW(market, current_time=current_time)
        return self.now.replace(tzinfo=_TZ)


@pytest.fixture()
def boundary(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """收盘边界场景：真实日历 + 推进钟 + 申万传输受控 + 东财兜底哨兵。"""
    h: Dict[str, Any] = {"clock": _ArrivalClock(), "windows": []}
    h["clock"].now = ENTRY
    h["trend_dates"] = _window20(at=AFTER_CLOSE)
    # 真实日历钉住复审反例的窗口事实（红绿分歧的前提）
    assert (h["trend_dates"][0], h["trend_dates"][-1]) == (
        date(2026, 10, 23), date(2026, 11, 19))
    entry_window = _window20(at=ENTRY)
    assert (entry_window[0], entry_window[-1]) == (
        date(2026, 10, 22), date(2026, 11, 18))

    def fake_quotes(codes: str, **kwargs: Any) -> List[Dict[str, Any]]:
        h["clock"].now = AFTER_CLOSE  # 快照响应到达：已收盘
        raise SwSectorFetchError("快照响应无有效行（tables 为空）")

    def fake_history(codes: str, start_date: str, end_date: str, **kwargs: Any):
        h["windows"].append((codes, start_date, end_date))
        frame = pd.DataFrame({
            "date": [d.isoformat() for d in h["trend_dates"]],
            "close": [100.0 + i for i in range(len(h["trend_dates"]))],
        })
        return {code: frame.copy() for code in codes.split(",")}

    def _confirmed_identity(observation_date: str) -> Dict[str, Any]:
        return {
            "status": "confirmed",
            "classification_system": "申万 2021 一级行业",
            "classification_level": "L1",
            "classification_version": SW_L1_DIRECTORY_VERSION,
            "index_code": _COAL, "index_name": "煤炭",
            "vendor_as_of": observation_date,
            "l2_name": None, "l3_name": None, "gaps": [],
        }

    def _empty_reference_levels(secucode: str, *, observation_date: str,
                                refresh_token: str, timeout_seconds: float,
                                deadline: Optional[float]) -> Dict[str, Any]:
        return {"l2_name": None, "l3_name": None, "gaps": []}

    monkeypatch.setattr(trading_calendar, "get_market_now", h["clock"])
    monkeypatch.setattr(svc, "get_market_now", h["clock"])
    monkeypatch.setattr(sw, "sw_http_config",
                        lambda: ("sector-test-refresh-token", 10.0))
    monkeypatch.setattr(sw, "resolve_sw_main_industry",
                        lambda secucode, *, observation_date, refresh_token,
                        timeout_seconds, deadline:
                        _confirmed_identity(observation_date))
    monkeypatch.setattr(sw, "fetch_index_quotes", fake_quotes)
    monkeypatch.setattr(sw, "fetch_index_history", fake_history)
    monkeypatch.setattr(sw, "resolve_sw_reference_levels",
                        _empty_reference_levels)
    # 可选/研究替身（v99 桩形状）：不在本反例关注面，零真实出站
    monkeypatch.setattr(svc, "_sw_trend_windows_pillar",
                        lambda *a, **k: {"windows": [], "gaps": []})
    monkeypatch.setattr(svc, "_fetch_constituents", lambda *a, **k: None)
    monkeypatch.setattr(svc, "_catalysts_pillar",
                        lambda *a, **k: _catalysts_stub_payload())
    monkeypatch.setattr(svc, "_synthesis_pillar",
                        lambda *a, **k: _synthesis_stub_payload())
    monkeypatch.setattr(svc, "_concepts_pillar",
                        lambda *a, **k: {"items": [], "source": "ifind",
                                         "total_returned": 0,
                                         "truncated": False, "gaps": []})
    # 东财兜底哨兵：保留申万路径必须零调用
    monkeypatch.setattr(
        svc, "_compose_eastmoney_core",
        Mock(side_effect=AssertionError("不应触发东财兜底")))
    return h


class TestCloseBoundaryTrendClock:
    """趋势在快照后跨收盘查询：实时钟纳入当日 → 保留申万、东财零出站。"""

    def test_trend_includes_session_day_keeps_sw(
            self, boundary: Dict[str, Any]):
        payload = compose_report("600188", "兖矿能源", budget=_budget())

        # 趋势窗口按查询时实时钟（已收盘）：单次出站、含当日 20 点
        assert boundary["windows"] == [
            (f"{_COAL},{_BENCH}", "2026-10-23", "2026-11-19")]
        trend = payload["sector_trend"]
        assert trend["qualified_sessions"] == 20
        assert (trend["start_date"], trend["end_date"]) == (
            "2026-10-23", "2026-11-19")
        assert trend["period_return_pct"] is not None
        # 有界兜底不触发：保留申万口径；会话归属取入口钟（11-19 会话日）
        svc._compose_eastmoney_core.assert_not_called()
        assert payload["research_basis"] == "sw2021_l1"
        assert payload["research_outcome"] == "kept_sw_stage"
        assert payload["sector_l2_identity"]["observation_date"] == "2026-11-19"
        SectorReportPayload.model_validate(payload)

    def test_empty_snapshot_first_screen_has_no_empty_change_label(
            self, boundary: Dict[str, Any]):
        """缺当日快照（快照响应空 → 渲染输入 ``row={}``）：真实 ``_render``
        的速览不渲染空的「当日涨跌幅」标签——修复前 Jinja ``Undefined is
        not none`` 恒 True，渲染出「 ｜ 当日涨跌幅 」空标签，与 Web 缺口
        语义不一致（issue #105 复审）。合法 0、参考日/正常行与既有涨跌幅
        排名、日期渲染不受影响（数字分支未改动，见 v103 首屏渲染用例）。"""
        payload = compose_report("600188", "兖矿能源", budget=_budget())
        # 空快照是本用例真实链事实：行情块 sector_found=false + row 空
        intraday = payload["background"]["intraday"]
        assert intraday["sector_found"] is False
        assert intraday["row"] == {}
        md = svc._render("兖矿能源", "600188",
                         payload["generated_at"], "sr-close-0001", payload)
        seg = md.split("## 报告速览（首屏）", 1)[1].split("\n##", 1)[0]
        fs_line = next(l for l in seg.splitlines() if l.startswith("- 一级 "))
        assert "涨跌幅" not in fs_line
        # 同行保留身份事实回退（row 无名称时用身份板块名）与快照缺口
        assert "**煤炭**" in fs_line
        # 快照缺失不阻断同报告的二级身份与趋势事实
        assert payload["sector_l2_identity"]["observation_date"] == "2026-11-19"
        assert payload["sector_trend"]["qualified_sessions"] == 20
        svc._compose_eastmoney_core.assert_not_called()
        SectorReportPayload.model_validate(payload)

    def test_explicit_current_time_pins_trend_window(
            self, boundary: Dict[str, Any]):
        """显式 current_time 直传旧趋势（旧代码语义）：窗口钉在传入钟
        （14:59:59 盘中 → 10/22–11/18，受控日线只 19/20 合格），不被
        偷偷换成实时钟；会话归属与 reference_now 同取传入钟。"""
        chain = _sw_market_chain("600188", budget=_budget(),
                                 current_time=ENTRY)

        assert boundary["windows"] == [
            (f"{_COAL},{_BENCH}", "2026-10-22", "2026-11-18")]
        trend = chain["trend"]
        assert (trend["start_date"], trend["end_date"]) == (
            "2026-10-22", "2026-11-18")
        assert trend["qualified_sessions"] == 19
        assert chain["quote_target_ok"] is False
        assert chain["trend_ok"] is False
        assert chain["snapshot"]["session_date"] == "2026-11-19"
        assert chain["reference_now"] == ENTRY
