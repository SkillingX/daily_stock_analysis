"""Compute capital amounts only from complete, dated, single-source windows.
Choice A-share CSD NETINFLOW is in yuan (see choice-quantapi-integration.md).
Issue #71：Choice 部署且 ``financial_primary_source=ifind`` 时以 iFinD 为主源
（同口径单源逐日窗口），Choice 仅为有界回退；两源窗口口径不同，禁止跨源拼接。

Issue #64 最近完整交易日：三窗口共同截至最新已收盘交易日 T；仅当 T 缺行/空值
且截至 T-1（含 T-1）的连续 10 个应有交易日均有合格数值时，一并改用 T-1。
实际采用截至日随 ``as_of_date`` 贯穿 coverage 与字段资格记录。
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import logging
from time import monotonic
from typing import Any, Literal, Optional, Sequence

from icontract import ensure, require

logger = logging.getLogger(__name__)

_WINDOW_5D = 5
_WINDOW_10D = 10
_DEFAULT_FETCH_DAYS = 12


@require(lambda n: n > 0)
@ensure(lambda result, trading_dates, n, source_unit: result is None or (source_unit is not None and trading_dates is not None and len(set(trading_dates)) >= n))
def compute_cumulative(
    series: Sequence[tuple[str, float | None]] | None, n: int, *,
    trading_dates: Sequence[str] | None = None,
    source_unit: Literal["CNY", "CNY_10K"] | None = None,
) -> Decimal | None:
    """Return yuan only when every expected trading date has one finite value."""
    if not trading_dates or source_unit is None:
        return None
    expected = sorted({date.fromisoformat(d).isoformat() for d in trading_dates}, reverse=True)[:n]
    if len(expected) < n:
        return None
    values: dict[str, Decimal | None] = {}
    for day, raw in series or ():
        day = date.fromisoformat(day).isoformat()
        value = Decimal(str(raw)) if raw is not None else None
        if value is not None and not value.is_finite():
            value = None
        if day in values and values[day] != value:
            return None
        values[day] = value
    if any(values.get(day) is None for day in expected):
        return None
    total = sum((value for day in expected if (value := values[day]) is not None), Decimal(0))
    return total * (10000 if source_unit == "CNY_10K" else 1)


def get_main_inflow_cumulative(
    code: str, days: int = _DEFAULT_FETCH_DAYS, *, deadline: float | None = None,
    current_time: Any = None, allow_choice_fallback: bool = True,
) -> dict[str, Any]:
    """``allow_choice_fallback=False``（issue #73 iFinD 主源深研 scope）：iFinD
    供应商失败按缺口如实披露，不登录 Choice 补窗；其余消费者默认 True 保持
    既有有界回退。"""
    from src.config import get_config
    if get_config().data_source_profile == "choice":
        if get_config().financial_primary_source == "ifind":
            result = _ifind_main_inflow_cumulative(code, days, deadline=deadline, current_time=current_time)
            # 本地交易日历不可用不是供应商失败：不回退 Choice 登录取日历，
            # 按缺口如实披露；其余 iFinD 供应商失败时 Choice 仍是有界补充
            # （10001009 后由同报告止损约束）。窗口不完整（partial）不算失败。
            if result.get("status") != "failed" or "ifind:calendar_unavailable" in result.get("errors", []):
                return result
            if not allow_choice_fallback:
                # issue #73：深研 scope 零 Choice 出站——失败原因已随 result 保留。
                return result
            fallback = _choice_main_inflow_cumulative(code, days, deadline=deadline)
            # 回退不静默：iFinD 失败原因随结果保留，来源标签仍如实
            fallback["errors"] = [*result.get("errors", []), *fallback.get("errors", [])]
            return fallback
        return _choice_main_inflow_cumulative(code, days, deadline=deadline)

    from data_provider.ifind_fundamental_adapter import IfindFetcher
    from data_provider.cross_source_validator import AnchorReading, adopted_field_record
    fetcher = IfindFetcher.get_instance()
    if not fetcher.available or (deadline is not None and deadline <= monotonic()):
        return {}
    from data_provider.cross_source_validator import SourceReadError
    try:
        series = fetcher.fetch_main_inflow_series(code, days, **({"deadline": deadline} if deadline is not None else {}))
    except SourceReadError as exc:
        logger.warning("iFinD capital flow unavailable: %s", exc)
        return {}
    if not series:
        return {}
    reading = AnchorReading("ifind", series[0][1], observed_at=series[0][0])
    return {"main_net_inflow": series[0][1], "inflow_5d": None, "inflow_10d": None,
            "daily_series": [{"date": d, "value": v} for d, v in series], "source": "ifind",
            "errors": ["ifind:window_calendar_and_unit_unconfirmed"],
            "field_meta": {"main_inflow": adopted_field_record("main_inflow", reading, selection_reason="fallback")}}


def _choice_main_inflow_cumulative(
    code: str, days: int, *, deadline: float | None,
) -> dict[str, Any]:
    """Choice 单源主力净流入 1/5/10 日窗口（CSD NETINFLOW，人民币元）。

    期望交易日只来自 Choice 交易日历；窗口按既有 T/T-1 语义归集
    （``_select_window_end`` + ``compute_cumulative``），任一应有交易日缺合格
    数值即如实记不完整，不补零、不前向填充。
    """
    from data_provider.choice_adapter import ChoiceAdapter
    from data_provider.cross_source_validator import AnchorQuality, AnchorReading, SourceReadError, adopted_field_record
    adapter = ChoiceAdapter()
    end = adapter.completed_day()
    start = end - timedelta(days=max(60, days))
    from src.config import get_config
    deadline = deadline if deadline is not None else monotonic() + get_config().fundamental_fetch_timeout_seconds
    result: dict[str, Any] = {"status": "partial", "source": "choice", "currency": "CNY", "source_unit": "CNY",
                              "main_net_inflow": None, "inflow_5d": None, "inflow_10d": None,
                              "errors": [], "daily_series": [], "coverage": {}}
    try:
        expected = sorted(adapter.trading_dates(start, end, deadline=deadline), reverse=True)
        series = adapter.capital_series(code, start, end, deadline=deadline)
        result["daily_series"] = [{"date": d, "value": v} for d, v in series]
        result["expected_date"] = expected[0] if expected else None
        result["latest_date"] = max((d for d, v in series if v is not None), default=None)
        by_date = dict(series)
        as_of, offset = _select_window_end(expected, series, by_date)
        result["as_of_date"] = as_of
        for n, field in ((1, "main_net_inflow"), (5, "inflow_5d"), (10, "inflow_10d")):
            window = expected[offset:offset + n]
            missing = [d for d in window if by_date.get(d) is None]
            amount = compute_cumulative(series, n, trading_dates=expected[offset:], source_unit="CNY")
            complete = amount is not None
            result[field] = float(amount) if amount is not None else None
            result["coverage"][str(n)] = {"expected_dates": window, "missing_dates": missing, "complete": complete}
            if not complete:
                result["errors"].append(f"choice:window_{n}_incomplete")
        if offset:
            # T 未就绪而采用 T-1：保留 T 未就绪证据并在说明中披露（issue #64）。
            result["coverage"]["pending_day"] = {"expected_date": expected[0], "observed": False}
            result["note"] = (
                f"资金数据截至{as_of}（最近完整交易日）；{expected[0]}资金窗口尚未完整，"
                f"单日及近5/10交易日均截至{as_of}。"
            )
        if result["main_net_inflow"] is not None:
            reading = AnchorReading("choice", result["main_net_inflow"], observed_at=as_of,
                                    unit="currency_base", currency="CNY", caliber="daily_main_net_inflow", raw_label="CSD NETINFLOW")
            result["field_meta"] = {"main_inflow": adopted_field_record("main_inflow", reading, AnchorQuality(status="single_source", reason_codes=("single_family",)))}
        else:
            result["field_meta"] = {"main_inflow": {"value": None, "rule_eligible": False, "quality": {"status": "missing", "reason_codes": ["window_incomplete"]}}}
        if not expected:
            result["errors"].append("choice:calendar_unavailable")
        result["status"] = "ok" if not result["errors"] else "partial"
    except SourceReadError as exc:
        result["status"] = "failed"
        result["errors"].append(str(exc))
    return result


_IFIND_FLOW_FETCH_ROWS = 20


def _ifind_main_inflow_cumulative(
    code: str, days: int, *, deadline: float | None, current_time: Any = None,
) -> dict[str, Any]:
    """iFinD 主源个股资金窗口：单源逐日金额 + 本地交易日历严格归集（issue #71）。

    期望交易日只来自严格交易日历；日历不可用/越界按 calendar_unavailable 披露，
    绝不以自然日或工作日兜底放行窗口。供应商周末空行不补零、不前向填充；
    窗口任一应有交易日缺合格数值即如实标记不完整。与 Choice 同名指标口径不同，
    本函数结果 source=ifind，禁止与 Choice 值跨源比较或拼接。
    """
    from src.core.trading_calendar import recent_completed_trading_sessions
    from data_provider.cross_source_validator import AnchorQuality, AnchorReading, SourceReadError, adopted_field_record, normalize_report_period
    from data_provider.ifind_fundamental_adapter import IfindFetcher

    result: dict[str, Any] = {"status": "partial", "source": "ifind", "currency": "CNY", "source_unit": "CNY",
                              "main_net_inflow": None, "inflow_5d": None, "inflow_10d": None,
                              "errors": [], "daily_series": [], "coverage": {}}
    fetcher = IfindFetcher.get_instance()
    if not fetcher.available:
        result["status"] = "failed"
        result["errors"].append("ifind:invalid_configuration")
        return result
    expected_sessions = recent_completed_trading_sessions("cn", _WINDOW_10D + 1, current_time=current_time)
    if expected_sessions is None:
        result["status"] = "failed"
        result["errors"].append("ifind:calendar_unavailable")
        return result
    expected = [session.isoformat() for session in expected_sessions]
    result["expected_date"] = expected[0]
    # 第十一轮：标准 HTTP 资金流候选仅「开关+个股深研执行上下文+出站资格」
    # 同时成立时参与（与财务/公司快照同一 ``http_financial_participation_
    # active`` 判定）——API 优先，失败/不可靠解析回 MCP 原路径；窗口起止由
    # **本地完成交易日历**锚定（enddate=expected[0]，盘中未定稿日不进请求
    # 且解析层丢弃更晚行——不可仅依据非空认定当日已收盘）；非深研/HTTP
    # 关闭形态保持 MCP 原路径零变化（预算：API 阶段为 MCP 兜底预留份额，
    # 取消照 curl 边界穿透，与 MCP 同语义）。
    series = None
    _adopted_channel: Optional[str] = None
    # 通用 MCP 后备的窗口：HTTP 参与时与 API 同用路由建立的 ``_flow_deadline``
    # （同窗合同——API 失败后后备不得按 ``deadline=None`` 重开自身 timeout 的
    # 更长窗口）；HTTP 不参与时保持调用方原 ``deadline``/None 语义。
    _mcp_fallback_deadline = deadline
    from data_provider.ifind_http_daily_adapter import http_financial_participation_active
    if http_financial_participation_active():
        from data_provider.ifind_http_daily_adapter import _finance_api_deadline, ifind_http_main_inflow_series
        from data_provider.base import DataFetchError as _FlowDFE
        from data_provider.cross_source_validator import SourceReadError as _FlowSRE
        from src.core.research_run import ResearchStopped as _FlowRS
        from src.core.research_run import current_research as _flow_research
        from src.core.trading_calendar import get_market_now
        from src.config import get_config as _flow_get_config
        # deadline=None 时按既有取数超时兜底（与 MCP 分支无 deadline 语义对齐）。
        _flow_deadline = deadline if deadline is not None else monotonic() + _flow_get_config().fundamental_fetch_timeout_seconds
        _mcp_fallback_deadline = _flow_deadline
        _flow_run = _flow_research.get()

        def _stopped_or_exhausted() -> bool:
            """停止或总预算耗尽：不再发任何后备出站（不重置 deadline）。"""
            return _flow_deadline <= monotonic() or bool(
                _flow_run and (_flow_run.stop_reason or _flow_run.finalized))

        # 参考日与窗口日历同一时区语义（``get_market_now``），**先于任何出
        # 站**比较 expected 最新日期与参考日决定出站次序（反转：T 日窗口
        # MCP 优先，消除「先 API 后必弃用」的必丢候选请求；历史窗口仍 API
        # 优先）。预算只在路由层分配一次：首尝试沿用既有有界比例思路（30%，
        # 最多 8 秒）为末位候选留正剩余、不得耗尽全部窗口——**MCP 首试的
        # 份额只按实际剩余 R 计算**（MCP 是独立渠道，其窗口不受 HTTP cap
        # 影响；MCP 自身 timeout 在 query 边界仍生效）；末位 API 只消费传入
        # 的本次尝试 deadline，不再内部预留不存在的第三次请求（适配入口
        # 仍夹取父截止与 HTTP 请求上限），两渠道同一外层绝对截止。
        _ref_day = get_market_now("cn", current_time=current_time).date().isoformat()
        if expected[0] >= _ref_day:
            # --- 当日 T：MCP 优先（至多一次）---
            # ``recent_completed_trading_sessions`` 只证明交易所已收盘——
            # 收盘后供应商可能返回尚会修订的 T 日值（官方手册无定稿时效说
            # 明、既有双源对照皆为历史日，收盘瞬间定稿不可证），故原鲜度
            # 优先取 MCP 整窗。
            _remaining_flow = _flow_deadline - monotonic()
            _first_reserve = min(8.0, _remaining_flow * 0.3) if _remaining_flow > 0 else 0.0
            _mcp_first_deadline = _flow_deadline - _first_reserve
            _mcp_failure: Optional[str] = None
            _mcp_raw: Optional[list] = None
            try:
                _mcp_raw = fetcher.fetch_main_inflow_series(
                    code, max(days + 8, _IFIND_FLOW_FETCH_ROWS),
                    deadline=_mcp_first_deadline,
                )
            except _FlowRS:
                raise  # 取消不是供应商失败：不回退、不消耗 API 出站
            except _FlowSRE as exc:
                _mcp_failure = str(exc)
            if _mcp_raw:
                # 非空序列（含全 null、日期不合格、缺交易日等不完整形态）
                # 一律交既有归一/窗口聚合规则——零 API 补查，不把「非空返
                # 回」当「窗口合格」，不放宽完整性/定稿校验、不换源拼接。
                series = _mcp_raw
                _adopted_channel = "mcp"
            elif not _stopped_or_exhausted():
                # MCP 真实失败或空结果（不构成成功）才走 API 既有补充路径。
                api_series = None
                try:
                    api_series = ifind_http_main_inflow_series(
                        code, deadline=_flow_deadline, start_iso=expected[-1], end_iso=expected[0],
                    )
                except _FlowRS:
                    raise
                except (_FlowSRE, _FlowDFE, TimeoutError) as exc:
                    logger.warning(
                        "capital flow API 补充不合格/失败（%s: %s）", code,
                        getattr(exc, "reason_code", type(exc).__name__))
                if api_series is not None:
                    _api_t_value = next(
                        (value for day, value in api_series if day == expected[0]), None)
                    if _api_t_value is not None:
                        # API T 非空（定稿未证明）：整批拒绝，不删 T 行后
                        # 拼装成合格历史结果。
                        logger.warning(
                            "capital flow API %s 当日 T（%s）非空但定稿未证明，整批拒绝",
                            code, expected[0])
                        api_series = None
                if api_series is not None:
                    series = api_series
                    _adopted_channel = "api"
                    if _mcp_failure is not None:
                        # T null：按既有数据质量语义保留 API T-1 完整窗口，
                        # 真实 MCP 失败原因结构化随 errors（不伪装正常）。
                        logger.warning(
                            "capital flow MCP 失败（%s），保留 API T-1 完整窗口", code)
                        result["errors"].append(_mcp_failure)
                elif _mcp_failure is not None:
                    # 双渠道失败：按失败披露（MCP 真实原因为主；预算耗尽/
                    # 供应商失败在诊断日志分别承载，不互相误述）。
                    result["status"] = "failed"
                    result["errors"].append(_mcp_failure)
                    return result
                else:
                    # MCP 空序列 + API 补充失败/拒绝：窗口全缺 partial（空
                    # 序列非供应商失败，不虚构错误码——与既有披露语义一致）。
                    series = []
            elif _mcp_failure is not None:
                # MCP 失败且停止/预算耗尽：不再发后备 API，失败如实披露。
                result["status"] = "failed"
                result["errors"].append(_mcp_failure)
                return result
            else:
                series = []  # MCP 空序列后停止/耗尽：窗口全缺 partial
        else:
            # --- 历史窗口：API 优先（四证券×20 交易日双源对照背书）---
            api_series = None
            try:
                api_series = ifind_http_main_inflow_series(
                    code, deadline=_finance_api_deadline(_flow_deadline),
                    start_iso=expected[-1], end_iso=expected[0],
                )
            except _FlowRS:
                raise  # 取消不是供应商失败：不回退、不消耗 MCP 出站
            except (_FlowSRE, _FlowDFE, TimeoutError) as exc:
                logger.warning(
                    "capital flow API 不合格/失败（%s: %s），回退 MCP", code,
                    getattr(exc, "reason_code", type(exc).__name__))
            else:
                # 有效性按 expected 交易日集合判定：API 序列在 expected 内
                # 零有效值（全 null、或区间内非交易日/窗口外行非空而
                # expected 全 null）视同不合格——回 MCP 原路径，不得跳过
                # 兜底；单个 expected 日 null 仍为缺值行（不补零）。
                _expected_set = set(expected)
                if not any(value is not None for day, value in api_series if day in _expected_set):
                    logger.warning(
                        "capital flow API（%s）expected 交易日零有效值，视同不合格回 MCP", code)
                else:
                    series = api_series
                    _adopted_channel = "api"
            # API 失败/不合格 → 落入下方通用 MCP 分支（历史路径兜底，一次）。
    if series is None:
        try:
            series = fetcher.fetch_main_inflow_series(
                code, max(days + 8, _IFIND_FLOW_FETCH_ROWS),
                **({"deadline": _mcp_fallback_deadline} if _mcp_fallback_deadline is not None else {}),
            )
        except SourceReadError as exc:
            result["status"] = "failed"
            result["errors"].append(str(exc))
            return result
    if _adopted_channel is not None and series:
        # 实际渠道采纳由消费层在此确认记录（底层日志只承载「取得候选」，
        # 候选随后被拒绝时不得宣称已采用）。
        logger.info(
            "capital flow %s 窗口实际采用渠道 %s（%d 行，null %d）", code,
            _adopted_channel, len(series),
            sum(1 for _d, v in series if v is None))
    # 归一供应商日期并丢弃无法证明实际日期的行；空白/无法解析的金额不补零。
    normalized: list[tuple[str, float | None]] = []
    for raw_day, raw_value in series or ():
        day, _basis = normalize_report_period(raw_day)
        if day is None:
            result["errors"].append("ifind:row_date_unknown")
            continue
        normalized.append((day, raw_value))
    normalized.sort(key=lambda item: item[0], reverse=True)
    result["daily_series"] = [{"date": day, "value": value} for day, value in normalized]
    result["latest_date"] = max((day for day, value in normalized if value is not None), default=None)
    by_date = dict(normalized)
    as_of, offset = _select_window_end(expected, normalized, by_date)
    result["as_of_date"] = as_of
    for n, field in ((1, "main_net_inflow"), (5, "inflow_5d"), (10, "inflow_10d")):
        window = expected[offset:offset + n]
        missing = [day for day in window if by_date.get(day) is None]
        amount = compute_cumulative(normalized, n, trading_dates=expected[offset:], source_unit="CNY")
        complete = amount is not None
        result[field] = float(amount) if amount is not None else None
        result["coverage"][str(n)] = {"expected_dates": window, "missing_dates": missing, "complete": complete}
        if not complete:
            result["errors"].append(f"ifind:window_{n}_incomplete")
    if offset:
        result["coverage"]["pending_day"] = {"expected_date": expected[0], "observed": False}
        result["note"] = (
            f"资金数据截至{as_of}（最近完整交易日）；{expected[0]}资金窗口尚未完整，"
            f"单日及近5/10交易日均截至{as_of}。"
        )
    if result["main_net_inflow"] is not None:
        reading = AnchorReading("ifind", result["main_net_inflow"], observed_at=as_of,
                                unit="currency_base", currency="CNY", caliber="daily_main_net_inflow", raw_label="主力净流入额")
        result["field_meta"] = {"main_inflow": adopted_field_record("main_inflow", reading, AnchorQuality(status="single_source", reason_codes=("single_family",)))}
    else:
        result["field_meta"] = {"main_inflow": {"value": None, "rule_eligible": False, "quality": {"status": "missing", "reason_codes": ["window_incomplete"]}}}
    result["status"] = "ok" if not result["errors"] else "partial"
    return result


def _select_window_end(
    expected: Sequence[str], series: Sequence[tuple[str, float | None]], by_date: dict[str, Any]
) -> tuple[str | None, int]:
    """在同一次交易日历+CSD 响应内选择共同截至日（issue #64，零追加查询）。

    返回 (as_of_date, offset)：优先 T=expected[0]；仅当 T 缺行/空值且截至 T-1
    （含 T-1）的连续 10 个应有交易日共同完整（复用既有 ``compute_cumulative``
    合计层判断：全日期有有限值、无同日冲突）时改用 T-1（offset=1）。
    T 已有值但窗口中间缺数不通过回退掩盖；日历不足由合计层拒绝。非法数值
    到不了这里——Choice 响应解析层已按无效响应拒绝。
    """
    if not expected:
        return None, 0
    if by_date.get(expected[0]) is not None:
        return expected[0], 0
    if compute_cumulative(series, 10, trading_dates=expected[1:], source_unit="CNY") is not None:
        return expected[1], 1
    return expected[0], 0
