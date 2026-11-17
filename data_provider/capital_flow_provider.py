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
    from data_provider.ifind_http_daily_adapter import http_financial_participation_active
    if http_financial_participation_active():
        from data_provider.ifind_http_daily_adapter import ifind_http_main_inflow_series
        from data_provider.base import DataFetchError as _FlowDFE
        from data_provider.cross_source_validator import SourceReadError as _FlowSRE
        from src.core.research_run import ResearchStopped as _FlowRS
        from src.core.trading_calendar import get_market_now
        from src.config import get_config as _flow_get_config
        # deadline=None 时按既有取数超时兜底（与 MCP 分支无 deadline 语义对齐）。
        _flow_deadline = deadline if deadline is not None else monotonic() + _flow_get_config().fundamental_fetch_timeout_seconds

        def _mcp_window():
            """MCP 原路径整个窗口（同预算；取消照原语义传播）。"""
            return fetcher.fetch_main_inflow_series(
                code, max(days + 8, _IFIND_FLOW_FETCH_ROWS),
                **({"deadline": deadline} if deadline is not None else {}),
            )

        api_series = None
        try:
            api_series = ifind_http_main_inflow_series(
                code, deadline=_flow_deadline, start_iso=expected[-1], end_iso=expected[0],
            )
        except _FlowRS:
            raise  # 取消不是供应商失败：不回退、不消耗 MCP 出站
        except (_FlowSRE, _FlowDFE, TimeoutError) as exc:
            logger.warning(
                "capital flow API 不合格/失败（%s: %s），回退 MCP", code,
                getattr(exc, "reason_code", type(exc).__name__))
        else:
            # 第十二~十四轮审查（T 定稿防御 + 原鲜度优先 + 失败披露与有效
            # 性判定）：参考日与窗口日历**同一时区语义**（``get_market_now``）。
            # **有效性按 expected 交易日集合判定**（第十四轮）：API 序列在
            # expected 内零有效值（全 null、或区间内非交易日/窗口外行非空而
            # expected 全 null）视同不合格——回 MCP 原路径，不得跳过兜底；
            # 单个 expected 日 null 仍为缺值行（不补零，窗口不完整披露）。
            _expected_set = set(expected)
            if not any(value is not None for day, value in api_series if day in _expected_set):
                logger.warning(
                    "capital flow API（%s）expected 交易日零有效值，视同不合格回 MCP", code)
            else:
                # 当日 T（expected[0] ≥ 参考日）：``recent_completed_trading_
                # sessions`` 只证明交易所已收盘——收盘后供应商可能返回非空
                # 但尚会修订的 T 日值（官方手册无定稿时效说明、既有双源对照
                # 皆为历史日，收盘瞬间定稿不可证）——
                #   T 非空：弃用 API 值，先以同预算调 MCP 整窗（原鲜度）；
                #     MCP 失败/空序列按缺口披露（不采信未定稿值、不拼两源）。
                #   T null/缺：同样先 MCP（MCP 可能有 T 值）；MCP **真实失败**
                #     才保留 API 的 T-1 完整窗口——失败原因（``ifind:<code>``）
                #     随 result.errors 结构化保留 + pending_day 披露，不伪装
                #     正常；MCP 空序列不构成「MCP 成功」——同按未提供处理。
                # 历史日（expected[0] < 参考日）：四证券×20 交易日双源对照
                # 背书，API 非空照常采纳（不试 MCP）。
                _ref_day = get_market_now("cn", current_time=current_time).date().isoformat()
                _api_t_value = next((value for day, value in api_series if day == expected[0]), None)
                if expected[0] >= _ref_day:
                    if _api_t_value is not None:
                        logger.info(
                            "capital flow API %s 当日 T（%s）非空但定稿未证明，弃用 API 值",
                            code, expected[0])
                        api_series = None  # 未定稿值不可采信
                    # MCP 在本分支**至多调用一次**（第十五轮）：不依赖底层
                    # single_fetch 假定包装层零额外调用——所有结果路径在此
                    # 分流终结，绝不落入下方通用分支二次调用。
                    _mcp_failure: Optional[str] = None
                    _mcp_raw: Optional[list] = None
                    try:
                        _mcp_raw = _mcp_window()
                    except SourceReadError as exc:
                        _mcp_failure = str(exc)
                    if _mcp_failure is not None:
                        # MCP 真实异常：T null 保留 API T-1（真实原因结构化
                        # 随 errors，不伪装正常）；T 非空弃用值 → 缺口披露。
                        if api_series is not None:
                            logger.warning(
                                "capital flow MCP 失败（%s），保留 API T-1 完整窗口", code)
                            series = api_series
                            result["errors"].append(_mcp_failure)
                        else:
                            result["status"] = "failed"
                            result["errors"].append(_mcp_failure)
                            return result
                    elif not _mcp_raw:
                        # 空序列不构成 MCP 成功（第十四轮）：T null 保留
                        # API T-1（数据质量优先，不虚构失败原因——与既有
                        # T-1 回退 ok+pending 语义一致）；T 非空弃用 → 按
                        # 原契约窗口全缺 partial 披露。
                        logger.warning("capital flow MCP（%s）返回空序列", code)
                        series = api_series if api_series is not None else []
                    else:
                        series = _mcp_raw  # MCP 完整窗口（原鲜度优先）
                else:
                    series = api_series  # 历史日：API 采纳（不试 MCP）
    if series is None:
        try:
            series = fetcher.fetch_main_inflow_series(
                code, max(days + 8, _IFIND_FLOW_FETCH_ROWS),
                **({"deadline": deadline} if deadline is not None else {}),
            )
        except SourceReadError as exc:
            result["status"] = "failed"
            result["errors"].append(str(exc))
            return result
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
