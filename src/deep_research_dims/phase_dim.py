# -*- coding: utf-8 -*-
"""S5 阶段决策：交易日历 + 盘中/盘后时段判定。纯规则，无 LLM。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from src.core.trading_calendar import (
    MarketPhase,
    get_effective_trading_date,
    infer_market_phase,
    is_market_open,
)
from src.deep_research_dims.context import SharedContext
from src.schemas.deep_research_dims import PhaseDim
from src.schemas.report_schema import PhaseDecision


def _fallback_session(today: Any) -> tuple[bool, str]:
    """日历数据源不可用时的本地兜底：工作日=交易日，时段按当前时刻粗分。"""
    weekday = today.weekday() < 5
    now = datetime.now().time()
    if now.hour < 9 or now.hour >= 15:
        phase = "closed"
    elif now.hour == 9 and now.minute < 30:
        phase = "auction"
    else:
        phase = "continuous"
    return weekday, phase


def build_phase_dim(ctx: SharedContext) -> PhaseDim:
    """S5：基于交易日历给出行动窗口与下次检查时点。

    日历数据源失败时回退本地工作日/时段兜底（纯规则维度不应因网络降级）。
    """
    today = datetime.now().date()
    limitations: list[str] = []
    try:
        market_open = is_market_open("cn", today)
        phase = infer_market_phase("cn", datetime.now())
        phase_label = phase.value if isinstance(phase, MarketPhase) else str(phase)
        next_trading = get_effective_trading_date("cn", today)
    except Exception as exc:  # noqa: BLE001 - 日历异常回退本地兜底
        market_open, phase_label = _fallback_session(today)
        now = datetime.now().time()
        start = today if (now.hour < 15 and market_open) else today + timedelta(days=1)
        next_trading = start
        while next_trading.weekday() >= 5:
            next_trading += timedelta(days=1)
        limitations.append(f"交易日历数据源不可用，已按本地日期兜底（{exc}）")
    # 盘中 = 连续竞价/午间休市/收盘集合竞价（均可下单，口径保守）
    in_session = market_open and phase_label in (
        MarketPhase.INTRADAY.value,
        MarketPhase.LUNCH_BREAK.value,
        MarketPhase.CLOSING_AUCTION.value,
    )

    if in_session:
        window = "盘中（交易时段，观点仅供收盘后复核）"
        action = "不追高，收盘后按报告计划执行"
        next_check = f"今日收盘后（{next_trading} 15:30 后复核）"
    elif market_open:
        window = "盘后（当日行情已收盘）"
        action = "可按作战计划挂单或等待触发价"
        next_check = f"下一交易日（{next_trading}）开盘前"
    else:
        window = "非交易日"
        action = "等待下一交易日执行，避免假日消息面扰动"
        next_check = f"下一交易日（{next_trading}）"

    decision = PhaseDecision(
        phase_context={"market_phase": phase_label, "trading_day": market_open},
        action_window=window,
        immediate_action=action,
        watch_conditions=[
            "触发价到达且量能配合（量比 ≥ 1.5）",
            "跌破止损位无条件执行",
            "出现报告外重大公告时暂停执行并重检",
        ],
        next_check_time=next_check,
        confidence_reason="交易日历与时段规则推导，无 LLM 参与",
        data_limitations=list(ctx.limitations) + limitations,
    )
    return PhaseDim(phase=decision, trading_day=market_open)
