# -*- coding: utf-8 -*-
"""个股板块分析服务（v2，issue #81）：主行业身份 → 背景支柱 → 概念列示 → 报告。

数据来源（全部已有数据源，不新增外部依赖）：
- 主行业身份：东财个股行业名 → 东财行业板块全表精确名称+板块代码锁定 →
  同板块成分表验证目标股；申万一/二/三级链仅作归属参考单列（不与东财混算）
- 政策倾向：行业 DNA 库（industry_dna_loader，本地静态规则背景）
- 行业基率：industry_base_rates.json（启发式先验背景，报告透出表版本）
- 当日行情：东财行业板块全表（价格强弱/活跃度/广度——不是产业景气、
  成交额活跃度不是资金净流入）
- 相关概念：iFinD 已适配入口仅列示（可选阶段，失败/超时记缺口不拖垮主报告）

v2 不产出综合总分、分档或行动结论（旧 30/30/40 权重与 fuyao 兜底已退役）；
「未评分」（评分政策）与「资料不足」/「完整性 partial」（资料状态）语义分开。
同一报告总预算钳制取数/重试/等待（取数截止提前收尾余量），缺数据记缺口不编造
（共享宪法第 2 条），预算耗尽不存档、保存失败不留孤儿文件。
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple, cast
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from icontract import ensure, require
from pydantic import ValidationError

from src.core.trading_calendar import (
    MARKET_TIMEZONE,
    get_market_now,
    recent_completed_trading_sessions,
)
from src.schemas.sector_analysis import SectorReportPayload
from src.schemas.sector_vendor import (
    BoardQuote,
    F10BoardResponse,
    F10BoardRow,
    MemberQuote,
    TencentMemberRow,
    Trends2Response,
)

if TYPE_CHECKING:  # 跨模块字符串注解（避免循环 import；见三层防御 Layer 1）
    from src.services.sector_research import ArticleDateEvidence, EvidenceRegistry

logger = logging.getLogger(__name__)

_ID_PATTERN = "sa_{ts:%Y%m%d%H%M}"
_REPORT_DIR = Path(__file__).parent.parent.parent / "reports" / "sector_analysis"

# 政策倾向标签（对齐 F2 sector_dim 口径）。首版新报告不再产出综合总分，
# 政策只保留方向标签作为静态规则背景。
_POLICY_LABEL = {"supportive": "支持", "neutral": "中性", "restrictive": "限制", None: "未知"}

# 概念展示上限（schema ConceptsBlock.items max_length 同值一致）：
# 供应商合法返回超过上限时截取展示并标注总量，不当作供应商故障。
_CONCEPT_DISPLAY_CAP = 50

# 收尾余量（秒）：从总预算划出给渲染/严格校验/持久化的固定预留上限。
# 实际余量 = min(本值, 总预算/2)——渲染+校验+保存为毫秒级本地操作，300s 预算
# 留 5s 足够；1—5s 合法小预算下缩放为一半，避免取数时间在起点即被吃光。
# 不作为配置项暴露。
_FINISH_MARGIN_SECONDS = 5.0


# ---------------------------------------------------------------------------
# 报告总预算（issue #79 实施默认：独立板块总预算默认 300s、合法 1—300；
# 同一预算入口供后续趋势/催化票复用；取数/重试/等待全部钳制剩余预算）
# ---------------------------------------------------------------------------

class BudgetExhausted(Exception):
    """报告总预算已耗尽：终结为超时，不采纳晚到 IO、不保存伪成功。"""


class SectorInputError(ValueError):
    """板块专项**用户输入错误**（仅无效 A 股代码等请求侧校验）。

    本 API 对该类型返回 400；供应商/程序侧 ``ValueError`` **不属于**本类型，
    走既有通用 500——新 F10/报价传输层的程序缺陷不冒充用户请求错误
    （root entry-closure 反例：程序 ValueError 曾被通用 catch 误报 400 并
    泄漏缺陷原文）。不新增通用异常框架，仅本入口窄化输入错误归属。
    """


class ReportBudget:
    """单次报告生成的总预算（monotonic 绝对截止时刻）。

    ``deadline`` 是绝对单调时刻（可直接传给支持 deadline 的适配器入口，
    如 iFinD 适配器的 ``deadline`` 参数）；``remaining()``/``expired()``/
    ``require()`` 用于渲染/校验/持久化等收尾边界的预算检查。

    ``finish_margin`` 从总预算中划出收尾余量：所有取数出站按
    ``fetch_deadline = deadline - finish_margin`` 钳制，保证渲染/严格校验/
    持久化在主截止内始终有剩余时间（可选阶段耗尽取数预算不会丢掉收尾余量）。
    """

    def __init__(self, total_seconds: float, *, finish_margin: float = 0.0) -> None:
        if not total_seconds or total_seconds <= 0:
            raise ValueError(f"报告总预算必须为正秒数：{total_seconds}")
        self.total_seconds = float(total_seconds)
        self.finish_margin = max(float(finish_margin), 0.0)
        self.deadline = time.monotonic() + self.total_seconds

    @property
    def fetch_deadline(self) -> float:
        """取数截止：主截止提前收尾余量。所有新出站/重试/退避检查同一截止。"""
        return self.deadline - self.finish_margin

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def expired(self) -> bool:
        return self.remaining() <= 0

    def require(self) -> None:
        if self.expired():
            raise BudgetExhausted(
                f"报告总预算（{self.total_seconds:.0f} 秒）已耗尽，本次不采纳结果、不保存报告"
            )

    def fetch_remaining(self) -> float:
        return self.fetch_deadline - time.monotonic()

    def fetch_expired(self) -> bool:
        return self.fetch_remaining() <= 0

    def require_fetch(self) -> None:
        if self.fetch_expired():
            raise BudgetExhausted(
                f"取数预算已耗尽（总预算 {self.total_seconds:.0f} 秒，"
                f"预留 {self.finish_margin:.0f} 秒收尾余量）：不再发起新出站"
            )


# 有界取数专用线程池：进程级共享、不在请求路径上 shutdown/join。
# 超时只是放弃等待（供应商在途工作可能继续，如实披露），fut 的结果被丢弃，
# 不会进入任何已终结请求的存档 —— 这不是硬截止，也不冒充硬截止。
_FETCH_EXECUTOR = ThreadPoolExecutor(max_workers=6, thread_name_prefix="sector-fetch")


def bounded_call(fn: Callable[..., Any], *args: Any, budget: ReportBudget,
                 **kwargs: Any) -> Any:
    """在剩余取数预算内等待一次取数调用。

    - 取数预算已耗尽：直接拒绝，不派发新出站。
    - 等待超过剩余取数预算：放弃等待并抛 BudgetExhausted（线程内调用继续，
      结果被丢弃；供应商在途工作是否结束不由本进程等待语义决定）。
    - 被执行函数自身抛出的 ``TimeoutError``（Py3.11 与 ``futures.TimeoutError``
      同类）按局部来源错误原样传播，走调用方的缺口路径——不谎报预算耗尽。
    """
    budget.require_fetch()
    fut: Future = _FETCH_EXECUTOR.submit(fn, *args, **kwargs)
    try:
        result = fut.result(timeout=max(budget.fetch_remaining(), 0.001))
    except FutureTimeoutError:
        # 区分等待超时与 fn 自身异常：future 已因 fn 抛异常完成时，
        # fut.result 抛出的 TimeoutError 其实是 fn 的供应商超时 re-raise。
        if fut.done():
            fn_exc = fut.exception()
            if fn_exc is not None:
                raise fn_exc
        fut.cancel()
        raise BudgetExhausted(
            f"取数等待超过剩余取数预算（剩余 {budget.fetch_remaining():.1f} 秒）："
            "本次放弃等待；供应商侧调用可能仍在途，其结果不会被采纳或存档"
        ) from None
    budget.require()  # 竞态守卫：只在主截止前采纳结果
    return result


def _ak_with_retry(call: Callable[..., Any], *args: Any,
                   retries: int = 1, delay: float = 0.5,
                   retry_budget: ReportBudget,
                   **kwargs: Any) -> Any:
    """akshare 接口包一层瞬时网络重试：默认 1 次重试 + 0.5s 退避。

    场景：东财 push2 接口在网络波动时常抛 ``RemoteDisconnected`` /
    ``Max retries exceeded``，多一次重试即可恢复，避免直接 fallback 丢精度。
    非网络错误（如参数错）会原样抛出，不浪费时间。

    预算纪律（``retry_budget`` 由 :func:`_bounded_ak` 注入）：每次新出站前
    检查同一取数截止（含从队列晚获得执行权的任务——首次 require_fetch 即
    失败，不再发新调用）；退避时长钳制剩余取数预算。

    root-decision 线程内边界（PR84 复审 P2）：真正执行 SDK 的本工作线程
    内幂等安装 ``eastmoney_patch(budget_only=True)`` 并 bind 同一
    ``fetch_deadline`` 到 ``request_deadline``——AkShare 内部分页/重试
    （如 ``stock_board_industry_name_em`` → ``fetch_paginated_data``）在
    调用方放弃后发出的新 ``Session.request`` 同样服从取数截止（截止后
    派发被拒绝，不产生新 wire）。成功/供应商失败/截止异常三类退出都在
    finally reset；无 deadline 的无关 requests 不受影响，同线程后续任务
    不残留上下文。
    """
    from src.patches.eastmoney_patch import eastmoney_patch, request_deadline

    last_exc: Optional[BaseException] = None
    token: Optional["object"] = None
    eastmoney_patch(budget_only=True)
    token = request_deadline.set(retry_budget.fetch_deadline)
    try:
        for i in range(retries + 1):
            retry_budget.require_fetch()
            try:
                return call(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - 保留原 except 兼容
                last_exc = exc
                if i < retries:
                    time.sleep(min(delay, max(retry_budget.fetch_remaining(), 0.0)))
        assert last_exc is not None  # 至少跑了一次循环
        raise last_exc
    finally:
        request_deadline.reset(token)


def _bounded_ak(fn: Callable[..., Any], *args: Any, budget: ReportBudget,
                retries: int = 1, delay: float = 0.5,
                **kwargs: Any) -> Any:
    """东财/申万 akshare 出站统一入口：等待、每次出站与退避钳制同一取数截止。

    ``budget`` 供 :func:`bounded_call` 等待钳制，同一对象以 ``retry_budget``
    注入线程内重试器——取数线程晚于截止获得执行权时首个出站即被拒绝。
    """
    return bounded_call(_ak_with_retry, fn, *args, retries=retries, delay=delay,
                        budget=budget, retry_budget=budget, **kwargs)


# ---------------------------------------------------------------------------
# 东财替代接入（2026-11-07 收敛轮）：datacenter F10 身份/成员 + push2 单标的
# 行情 + trends2 分钟累计 backup。取数路径经生产只读探针验证；wire 契约见
# src/schemas/sector_vendor.py（严格消费字段 + extra=allow 兼容合法元数据）。
# ---------------------------------------------------------------------------

_EM_DATACENTER_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
_EM_STOCK_GET_URL = "https://push2.eastmoney.com/api/qt/stock/get"
_EM_TRENDS2_URL = "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
_BOARD_QUOTE_FIELDS = "f48,f57,f58,f86,f116,f168,f170"
_MEMBER_QUOTE_FIELDS = "f48,f57,f58,f86,f170"
_F10_PAGE_SIZE = 100
_F10_MAX_PAGES = 40
_HTTP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 衍生口径标记（provenance 单一 owner：生成与判定同用本常量，不散落字符串）
_DERIVED_MARKER = "供应商分钟累计衍生"

_CONS_SNAPSHOT_SOURCE = "东财行业板块成分（身份验证同一快照）"
_F10_CONS_SNAPSHOT_SOURCE = "东财 F10 板块完整成员（身份验证同一集合）"
_F10_AMOUNT_PROVENANCE_PREFIX = (
    "成交额单位为元：成分集合来自东财 F10 板块完整成员（datacenter "
    "RPT_F10_CORETHEME_BOARDTYPE）"
)


@dataclass(frozen=True)
class QuoteSnapshot:
    """单标的当日快照：已校验报价 + 来源/时间元数据（provenance 贯穿载荷）。

    ``derived_amount=True`` 表示 ``f48`` 是 trends2 分钟额求和的**衍生值**
    （与 stock/get f48 同元级口径，非原样字段）；``vendor_as_of`` 是供应商
    数据截至（trends2=最后一根分钟时间，stock/get=f86 时间戳的市场时区
    iso）——采集时间不冒充供应商时间。
    """

    quote: Any
    source: str
    derived_amount: bool
    vendor_as_of: Optional[str] = None
    vendor_time_epoch: Optional[int] = None


def _secucode(code: str) -> str:
    """A 股 6 位代码 → 东财 SECUCODE（交易所后缀与 `_stock_secid` 同一识别源）。

    复用 :func:`data_provider.base.is_bse_code` 权威北交所识别（43/83/87/88/92
    段，含 920 新段；900 沪 B 不误判），与成员行情 secid 一致——同一代码全链
    一个交易所口径。
    """
    from data_provider.base import is_bse_code

    if code.startswith("6") or (code.startswith("9") and not is_bse_code(code)):
        return f"{code}.SH"
    if is_bse_code(code):
        return f"{code}.BJ"
    return f"{code}.SZ"


def _stock_secid(code: str) -> str:
    """A 股 6 位代码 → stock/get secid 市场前缀（沪 1，深/北 0）。

    与 `_secucode` 同一 ``is_bse_code`` 识别源：920 北交段归 0 市场，不按 9
    前缀误判沪市。
    """
    from data_provider.base import is_bse_code

    if code.startswith("6") or (code.startswith("9") and not is_bse_code(code)):
        return f"1.{code}"
    return f"0.{code}"


def _f10_row_identity_ok(row: "F10BoardRow") -> bool:
    """F10 行自身双股票键一致：SECUCODE 的 6 位代码段与交易所后缀都必须与
    SECURITY_CODE 及其权威交易所识别一致。

    请 600519 返回 000001.SZ、或 600519/000001.SZ 单列漂移、或 920 段错标
    .SZ，都按源错误整体拒绝（不丢行缩集合冒充完整）。
    """
    from data_provider.base import is_bse_code

    code_part, _, suffix = row.SECUCODE.partition(".")
    return (code_part == row.SECURITY_CODE
            and suffix == _secucode(row.SECURITY_CODE).partition(".")[2]
            and suffix in {"SH", "SZ", "BJ"}
            and bool(is_bse_code(row.SECURITY_CODE)) == (suffix == "BJ"))


def _http_get_json(url: str, params: Dict[str, Any], *,
                   budget: ReportBudget, timeout: float = 6.0,
                   model: Optional[type] = None,
                   retries: int = 1) -> Optional[Any]:
    """受同一预算与执行器约束的东财 JSON GET（替代接入统一出站入口）。

    - 复用 :func:`bounded_call`：线程等待上界=剩余取数预算，等待超时抛
      BudgetExhausted（迟到结果丢弃）；竞态守卫只在主截止前采纳。
    - worker 内幂等安装 ``eastmoney_patch(budget_only=True)`` 并绑定同一线程级
      fetch deadline，finally 恢复——与 AkShare 出站同一 deadline/调度停止纪律。
    - 异常分层：requests 网络/HTTP 状态异常与供应商 JSON/模型校验错误 → None
      （调用方记缺口）；程序错误（TypeError/ValueError/AssertionError）原样
      传播，不吞异常、不伪装成供应商故障。
    - ``retries``：间歇拒绝（生产实测 stock/get 部分请求被拒后相邻请求成功）
      单次重试，退避钳制剩余预算——与 ``_ak_with_retry`` 同语义；程序错误不重试。
    - ``model``：严格供应商 I/O 模型（src/schemas/sector_vendor.py），形状/类型
      不符按源错误返回 None，不 str() 合法化。
    """
    from src.patches.eastmoney_patch import eastmoney_patch, request_deadline

    def _call() -> Any:
        eastmoney_patch(budget_only=True)
        token = request_deadline.set(budget.fetch_deadline)
        try:
            with requests.Session() as session:
                resp = session.get(url, params=params, timeout=timeout,
                                   headers={"User-Agent": _HTTP_UA})
                resp.raise_for_status()
                return resp.json()
        finally:
            request_deadline.reset(token)

    host = url.split("/")[2] if "//" in url else url
    data: Optional[Any] = None
    last_error: Optional[str] = None
    for attempt in range(retries + 1):
        try:
            data = bounded_call(_call, budget=budget)
            last_error = None
            break
        except BudgetExhausted:
            raise
        except requests.exceptions.JSONDecodeError as exc:
            # 仅 JSON 解码边界：resp.json() 失败（502 HTML 等）。注意它继承
            # RequestException，必须先于网络异常分支捕获才能区分错误文案。
            # 程序层的 ValueError/TypeError/AssertionError 不在此捕获，原样传播。
            last_error = f"response not JSON: {str(exc)[:60]}"
        except requests.RequestException as exc:  # noqa: BLE001 - 网络/HTTP 状态按源错误
            last_error = str(exc)[:100]
        if attempt < retries:
            remaining = budget.fetch_remaining()
            if remaining <= 0:
                break
            time.sleep(min(0.3, remaining))
    if last_error is not None or data is None:
        logger.warning("[SectorAnalysis] 替代接入 GET 失败 host=%s: %s",
                       host, last_error or "empty payload")
        return None
    if model is None:
        return data
    try:
        return model.model_validate(data)
    except ValidationError as exc:  # 供应商字段/形状不符按源错误
        logger.warning("[SectorAnalysis] 供应商响应模型校验失败 host=%s: %s",
                       host, str(exc)[:140])
        return None


def _f10_fetch_all_pages(filter_expr: str, *, budget: ReportBudget,
                         label: str) -> Optional[List[F10BoardRow]]:
    """F10 datacenter 报表完整分页取数（声明列表与反向成员共用同一实现）。

    完整性纪律（root 反例：正向声明 pages=2/count=2 仅 1 行曾被采纳）：
    逐页遍历到 pages 尾（不只取第一页）；跨页 count/pages 一致（漂移=源
    错误，服务端翻页间集合变化不得静默采为完整集合，预算内一次判定）；
    每页行数契约（非末页满 pageSize、末页恰余量）；总行数与 count 核齐；
    每行 `_f10_row_identity_ok` 双键校验。**SECURITY_CODE 去重不在此层**：
    个股声明列表同一股票合法多行（行业+概念各一行）；成员集合语义的去重
    由 `_fetch_board_members` 施加。
    返回 None=源不可用/形状异常/完整性不成立（调用方记缺口，不据此采用）。
    """
    all_rows: List[F10BoardRow] = []
    expected_count: Optional[int] = None
    pages = 1
    for page in range(1, _F10_MAX_PAGES + 1):
        resp = cast(
            Optional[F10BoardResponse],
            _http_get_json(_EM_DATACENTER_URL, {
                "reportName": "RPT_F10_CORETHEME_BOARDTYPE",
                # ALL：行业名/类型层级判定需要 BOARD_TYPE/BOARD_LEVEL
                "columns": "ALL",
                "filter": filter_expr,
                "pageNumber": str(page), "pageSize": str(_F10_PAGE_SIZE),
                "source": "WEB", "client": "WEB",
            }, budget=budget, model=F10BoardResponse),
        )
        if resp is None:
            return None
        if expected_count is None:
            expected_count = resp.result.count
            pages = resp.result.pages
            if pages > _F10_MAX_PAGES:
                logger.warning(
                    "[SectorAnalysis] F10 %s 页数 %s 超防御上限 %s，按源错误拒绝",
                    label, pages, _F10_MAX_PAGES)
                return None
        elif (resp.result.count != expected_count
                or resp.result.pages != pages):
            logger.warning(
                "[SectorAnalysis] F10 %s 跨页元数据漂移：page1 count=%s/pages=%s，"
                "page%s count=%s/pages=%s，不采用", label, expected_count,
                pages, page, resp.result.count, resp.result.pages)
            return None
        # 每页行数契约：非末页必须满 pageSize，末页恰为余量（缺行/多行=源错误）
        expected_page_rows = (_F10_PAGE_SIZE if page < pages
                              else expected_count - _F10_PAGE_SIZE * (pages - 1))
        if len(resp.result.data) != expected_page_rows:
            logger.warning(
                "[SectorAnalysis] F10 %s 页 %s 行数不符：期望 %s 实际 %s，"
                "分页不完整，不采用", label, page, expected_page_rows,
                len(resp.result.data))
            return None
        for row in resp.result.data:
            if not _f10_row_identity_ok(row):
                logger.warning(
                    "[SectorAnalysis] F10 %s 行双键不符：SECUCODE=%s "
                    "SECURITY_CODE=%s，整体不采用", label, row.SECUCODE,
                    row.SECURITY_CODE)
                return None
            all_rows.append(row)
        if page >= pages:
            break
    if expected_count is None or len(all_rows) != expected_count:
        logger.warning(
            "[SectorAnalysis] F10 %s 总数不符：count=%s 实际收齐 %s 行，不采用",
            label, expected_count, len(all_rows))
        return None
    return all_rows


def _fetch_board_claims(code: str, *, budget: ReportBudget,
                        ) -> Optional[F10BoardResponse]:
    """东财 F10 所属板块声明列表（SECUCODE filter，个股侧结构化归属）。

    完整分页核齐（与反向成员同一 `_f10_fetch_all_pages` 契约）+ 响应同键
    校验：全部行 SECUCODE 必须等于请求的 ``_secucode(code)``（请 600519
    返回 000001.SZ 属源错误，整体拒绝）。供应商失败/形状异常/同键/完整性
    不符 → None（调用方记缺口，不据此锁定）。成功时聚合为单一
    F10BoardResponse（pages=1/count=实际行数），接口形状与反向成员一致。
    """
    rows = _f10_fetch_all_pages(f'(SECUCODE="{_secucode(code)}")',
                                budget=budget, label="归属声明")
    if rows is None:
        return None
    expected = _secucode(code)
    for row in rows:
        if row.SECUCODE != expected:
            logger.warning(
                "[SectorAnalysis] F10 声明行同键不符：请求 %s，"
                "实际 SECUCODE=%s SECURITY_CODE=%s，整体不采用",
                expected, row.SECUCODE, row.SECURITY_CODE)
            return None
    return F10BoardResponse.model_validate({
        "success": True, "code": 0,
        "result": {"pages": 1, "count": len(rows),
                   "data": [row.model_dump() for row in rows]},
    })


def _fetch_board_members(board_code: str, board_name: str, code: str, *,
                         budget: ReportBudget) -> Optional[Dict[str, Any]]:
    """东财 F10 板块反向完整成员（NEW_BOARD_CODE filter，分页核完）。

    返回 None=源不可用/形状异常/同键或类型不符（源错误）；成功返回
    ``{"rows": [...], "target_in": bool, "member_count": int}``——
    ``target_in`` 表达目标股是否在成员集合（归属验证语义，非源错误）。

    分页完整性由 `_f10_fetch_all_pages` 共用契约保证；本入口追加成员语义
    校验：每行 NEW_BOARD_CODE/BOARD_NAME 同键（漂移=源错误）、BOARD_TYPE
    必须「行业」、BOARD_LEVEL ∈ {"1","2","3"}（概念/衍生同名板块拒绝）、
    SECURITY_CODE 跨页去重（成员集合内同一股票重复=源错误，不静默取其一）。
    """
    all_rows = _f10_fetch_all_pages(f'(NEW_BOARD_CODE="{board_code}")',
                                    budget=budget,
                                    label=f"板块成员（{board_code}）")
    if all_rows is None:
        return None
    seen_codes: set = set()
    for row in all_rows:
        if row.NEW_BOARD_CODE != board_code or row.BOARD_NAME != board_name:
            logger.warning(
                "[SectorAnalysis] F10 成员行同键不符：请求 %s/%s 实际 %s/%s，"
                "整体不采用", board_code, board_name,
                row.NEW_BOARD_CODE, row.BOARD_NAME)
            return None
        if row.BOARD_TYPE != "行业" or row.BOARD_LEVEL not in ("1", "2", "3"):
            logger.warning(
                "[SectorAnalysis] F10 成员行类型/层级异常：%s/%s（%s），"
                "整体不采用", row.BOARD_TYPE, row.BOARD_LEVEL,
                row.SECURITY_CODE)
            return None
        if row.SECURITY_CODE in seen_codes:
            logger.warning(
                "[SectorAnalysis] F10 成员集合重复代码 %s，按源错误拒绝",
                row.SECURITY_CODE)
            return None
        seen_codes.add(row.SECURITY_CODE)
    return {"rows": all_rows, "target_in": code in seen_codes,
            "member_count": len(all_rows)}


def _members_snapshot_df(rows: List[F10BoardRow]) -> pd.DataFrame:
    """F10 反向成员 → 同业支柱成分快照形状（代码/名称；行情列由逐股采集填充）。"""
    return pd.DataFrame({
        "代码": [r.SECURITY_CODE for r in rows],
        "名称": [r.SECURITY_NAME_ABBR for r in rows],
    })


@dataclass(frozen=True)
class SessionContext:
    """市场会话上下文：**只拥有日历事实**（XSHG 会话日与该日开/收盘时刻）。

    会话上下文的存在不暗示冻结测试世界，也绝不冒充响应接收钟——「会话
    是否已完结」与一切供应商时点上界都在取数处按**该响应实际接收时刻**
    （``_received_at_now``，HTTP 完成后取得）判定。
    """

    session_date: date
    session_open: datetime
    session_close: datetime


def _calendar_naive_local(value: Any) -> Optional[datetime]:
    """日历时间戳 → 市场本地 naive datetime（会话日/开收盘锚定用）。"""
    tz = ZoneInfo(MARKET_TIMEZONE["cn"])
    if hasattr(value, "tz_convert"):
        converted = cast(datetime, value.tz_convert(tz).to_pydatetime())
        return converted.replace(tzinfo=None)
    dt = cast(datetime,
              value.to_pydatetime() if hasattr(value, "to_pydatetime") else value)
    if dt.tzinfo is not None:
        return dt.astimezone(tz).replace(tzinfo=None)
    return dt.replace(tzinfo=None)


def _session_context(now: Optional[datetime] = None) -> Optional[SessionContext]:
    """当前报价归属会话上下文（会话感知，单一 now 注入点，测试冻结用）。

    会话感知规则（root 反例：盘中不得用「最近已完成交易日」拒绝合法当日分钟；
    ``now`` 只锚定会话日归属，不是接收钟——接收钟在每次 HTTP 完成后由
    :func:`_received_at_now` 取得）：
    - 交易日当天开盘（含）后：当天会话（盘中前缀覆盖与盘后全天同属当日）；
    - 非交易日 / 开盘前：最近已完成会话（date_to_session previous）；
    - 交易日历不可用：None（调用方拒绝采用，不按自然日猜测）。
    """
    market_now = get_market_now("cn", current_time=now)
    try:
        import exchange_calendars as xcals

        cal = xcals.get_calendar("XSHG")
    except Exception as exc:  # noqa: BLE001 - 日历不可用按明确未知处理
        logger.warning("[SectorAnalysis] 交易日历不可用，无法判定会话: %s", exc)
        return None
    try:
        local_date = market_now.date()
        session = cal.date_to_session(local_date, direction="previous")
        if cal.is_session(local_date):
            open_local = _calendar_naive_local(cal.session_open(session))
            if open_local is not None and market_now.replace(tzinfo=None) >= open_local:
                return SessionContext(
                    session_date=local_date,
                    session_open=open_local,
                    session_close=_calendar_naive_local(cal.session_close(session)),
                )
            session = cal.previous_session(session)
        session_open = _calendar_naive_local(cal.session_open(session))
        session_close = _calendar_naive_local(cal.session_close(session))
        if session_open is None or session_close is None:
            return None
        return SessionContext(
            session_date=session.date(), session_open=session_open,
            session_close=session_close,
        )
    except Exception as exc:  # noqa: BLE001 - 日历越界/异常按明确未知处理
        logger.warning("[SectorAnalysis] 会话解析失败: %s", exc)
        return None


def _received_at_now() -> datetime:
    """当前响应实际接收时刻（市场本地 naive；每次响应各自取钟，不用批次
    开始时刻冒充后续响应的接收时刻）。"""
    return get_market_now("cn").replace(tzinfo=None)


def _http_get_text(url: str, params: Dict[str, Any], *,
                   budget: ReportBudget, encoding: str = "utf-8",
                   timeout: float = 6.0,
                   ) -> Optional[str]:
    """受同一预算与执行器约束的公开行情文本 GET（腾讯批量行情专用）。

    与 :func:`_http_get_json` 同一预算/接收钟/异常分层纪律：worker 内
    ``eastmoney_patch(budget_only=True)`` + request_deadline 绑定（finally
    恢复）；requests 网络/HTTP 状态异常 → None（调用方记缺口）；程序异常
    原样传播。腾讯响应为 GBK JavaScript 赋值文本（非 JSON），故独立返回
    解码后文本。**单次尝试零重试**：每块仅一次终端请求，失败交既有
    选择性补缺——不新增请求消耗。
    """
    from src.patches.eastmoney_patch import eastmoney_patch, request_deadline

    def _call() -> Any:
        eastmoney_patch(budget_only=True)
        token = request_deadline.set(budget.fetch_deadline)
        try:
            with requests.Session() as session:
                resp = session.get(url, params=params, timeout=timeout,
                                   headers={"User-Agent": _HTTP_UA,
                                            "Referer": "http://finance.qq.com"})
                resp.raise_for_status()
                resp.encoding = encoding
                return resp.text
        finally:
            request_deadline.reset(token)

    host = url.split("/")[2] if "//" in url else url
    try:
        data = cast(str, bounded_call(_call, budget=budget))
    except BudgetExhausted:
        raise
    except requests.RequestException as exc:  # noqa: BLE001 - 网络/HTTP 状态按源错误
        logger.warning("[SectorAnalysis] 公开行情文本 GET 失败 host=%s: %s",
                       host, str(exc)[:100])
        return None
    if data is None:
        logger.warning("[SectorAnalysis] 公开行情文本 GET 失败 host=%s: %s",
                       host, "empty payload")
        return None
    return data


def _vendor_epoch_naive_local(epoch: int) -> Optional[datetime]:
    """供应商 epoch 秒 → 市场本地 naive datetime；畸形值（溢出/负/超界）返回
    None（调用方按源错误拒绝），不泄 OverflowError/ValueError/OSError。"""
    try:
        return datetime.fromtimestamp(
            epoch, tz=ZoneInfo(MARKET_TIMEZONE["cn"])).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


_TRENDS_MIN_TIME = dt_time(9, 30)
_TRENDS_MORNING_END = dt_time(11, 30)
_TRENDS_AFTERNOON_OPEN = dt_time(13, 1)
_TRENDS_AFTERNOON_END = dt_time(15, 0)
_TRENDS_LUNCH_GAP_MIN = 91  # 11:30 → 13:01（真实 wire：午后首根分钟为 13:01）


@require(lambda last_close: isinstance(last_close, Decimal)
         and last_close.is_finite() and last_close > 0,
         "last_close must be a finite positive Decimal")
@require(lambda pre_close: isinstance(pre_close, Decimal)
         and pre_close.is_finite() and pre_close > 0,
         "pre_close must be a finite positive Decimal")
@ensure(lambda result: isinstance(result, Decimal) and result.is_finite(),
        "trends2 change pct must be a finite Decimal")
def _trends_change_pct(last_close: Decimal, pre_close: Decimal) -> Decimal:
    """trends2 涨跌幅（百分点，2 位小数）：(末分钟现价 − preClose)/preClose×100。

    Layer 2 金融公式契约：现价列（close）对昨收的 Decimal 派生；现价/昨收
    有限正是 require 前置（Pydantic 已管 wire 字段范围，此处不重复字段搬运
    校验，只约束公式输入域）；preClose=10、close=11 → 10.00（高列 15 不得
    参与公式——真实 wire board 末行 close≠high 直接判别列选择）。
    """
    return ((last_close - pre_close) / pre_close
            * Decimal("100")).quantize(Decimal("0.01"))


@require(lambda amounts: len(amounts) > 0 and all(
    isinstance(a, Decimal) and a.is_finite() and a >= 0 for a in amounts),
    "amounts must be non-empty finite non-negative Decimals")
@ensure(lambda result: isinstance(result, Decimal) and result.is_finite()
        and result >= 0,
        "cumulative amount must be a finite non-negative Decimal")
def _sum_minute_amounts(amounts: List[Decimal]) -> Decimal:
    """trends2 当日累计额：供应商分钟额列 Decimal 求和（衍生口径）。

    Layer 2 金融公式契约：输入必须是已通过行级资格的有限非负分钟额
    （零额分钟合法）；不完整/缺段行不得进入（调用方整体拒绝后才求和），
    部分行部分和不冒充当日累计。
    """
    total = Decimal("0")
    for a in amounts:
        total += a
    return total


@ensure(
    lambda result: result is None or (
        isinstance(result, QuoteSnapshot)
        and result.quote is not None
        and result.quote.f48 is not None and result.quote.f48 >= 0
        and result.quote.f170 is not None
        and math.isfinite(float(result.quote.f170))
    ),
    "accepted trends2 snapshot must carry non-negative cumulative amount "
    "(known-zero is a legal observation) and finite change pct "
    "(None = rejected, not partial)")
def _fetch_trends2_snapshot(secid: str, expected_code: str, expected_name: str,
                            *, budget: ReportBudget,
                            quote_model: type,
                            now: Optional[datetime] = None,
                            session_ctx: Optional[SessionContext] = None,
                            received_at: Optional[datetime] = None,
                            ) -> Optional[QuoteSnapshot]:
    """trends2 当日累计口径快照（stock/get 不可用时的同口径 backup）。

    严格契约（真实 wire fixture 全链回放验证）：
    - wire 经 :class:`Trends2Response` 严格模型（消费字段 code/name/market/
      preClose/time/trendsTotal/trends 显式校验；无关合法元数据兼容放行）；
    - ``market`` 回显请求 secid 市场前缀、code/name 同键（身份漂移拒绝）；
    - ``len(trends) == trendsTotal``（供应商声明行数与实际核齐，完整性依据）；
    - **接收钟（root entry-closure 反例驱动）**：``received`` 在本 source 的
      ``_http_get_json`` 完成后立即取得（``received_at`` 仅供测试显式注入；
      session_ctx 只锚日历会话，绝不冒充冻结接收钟，生产调用链不提前取钟）。
      分钟未来上界、``time`` 上界与「会话是否已完结」（完整日 15:00 资格）
      全部按该接收时刻相对同一 open/close 判定；首源失败后的 fallback 独立
      取得自己的接收钟。
    - 供应商时点 ``time``：解码畸形（溢出等）按源错误拒绝；必须属于会话日、
      不早于末根分钟且不晚于接收时刻。
    - 分钟行逐行：8 列具名解析（time,open,close,high,low,volume,amount,avg）、
      单一会话日（=会话上下文判定）、**严格递增**、交易时段窗口
      （09:30-11:30 / 13:01-15:00）内、覆盖自 09:30 起、段内连续（仅午休
      11:30→13:01 跳变合法）、分钟不晚于当前时刻（未来拒绝）；
    - **覆盖资格（root 反例：盘后截尾曾采为累计）**：会话已完结
      （now >= session_close，含盘后/过去会话/周末盘前）→ 末分钟必须为该
      会话收盘 15:00（结合首分钟 09:30 与连续性即完整日）；盘中（now <
      session_close）允许截至供应商已完成分钟的真实连续前缀，as_of=最后一根
      已完成分钟如实透出；
    - 价格列（开/收/高/低/均价）有限正、量额有限非负（零量零额是合法观测；
      任一行参与字段缺失/畸形/负值 → 整体不可信返回 None，不跳坏行部分累计）；
    - 行内 OHLC 一致（low <= min(open, close) <= max <= high）；
    - **现价取 close 列**；累计/涨跌幅经 `_sum_minute_amounts` /
      `_trends_change_pct` typed 金融契约（Layer 2）；
    - ``preClose`` 模型层强制有限正（"Infinity"/"-"/0/负按源错误拒绝）。
    累计额是**衍生分钟累计**：调用方 provenance 必须标注衍生来源，不写 f48 原样。
    """
    raw = _http_get_json(_EM_TRENDS2_URL, {
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
        "secid": secid, "iscr": "0",
    }, budget=budget)
    if not isinstance(raw, dict):
        return None
    inner = raw.get("data")
    if not isinstance(inner, dict):
        return None
    try:
        data = Trends2Response.model_validate(inner)
    except ValidationError as exc:
        logger.warning("[SectorAnalysis] trends2 wire 模型校验失败 secid=%s: %s",
                       secid, str(exc)[:120])
        return None
    secid_market = secid.partition(".")[0]
    if not secid_market.isdigit() or data.market != int(secid_market):
        logger.warning(
            "[SectorAnalysis] trends2 market 回显不符：secid=%s 实际 market=%s，"
            "不采用", secid, data.market)
        return None
    if data.code != expected_code or data.name != expected_name:
        logger.warning(
            "[SectorAnalysis] trends2 快照同键不符：期望 %s/%s 实际 %s/%s，不采用",
            expected_code, expected_name, data.code, data.name)
        return None
    ctx = session_ctx if session_ctx is not None else _session_context(now)
    if ctx is None:
        logger.warning(
            "[SectorAnalysis] trends2 快照：交易日历不可用，无法判定分钟归属"
            "交易日，不采用")
        return None
    # 接收钟：本响应 HTTP 完成后立即取得（显式注入仅供测试；不提前取钟）
    received = received_at if received_at is not None else _received_at_now()
    # 会话阶段按接收时刻判定（14:59:58 请求、15:00:02 接收 → 完结会话）
    session_finished = received >= ctx.session_close
    if len(data.trends) != data.trendsTotal or not data.trends:
        logger.warning(
            "[SectorAnalysis] trends2 行数与声明不符或为空：trendsTotal=%s 实际=%s，"
            "完整性依据缺失，不采用", data.trendsTotal, len(data.trends))
        return None
    expected_date = ctx.session_date.isoformat()
    prev_minute: Optional[datetime] = None
    minute_amounts: List[Decimal] = []
    last_minute: Optional[str] = None
    last_minute_dt: Optional[datetime] = None
    last_close: Optional[Decimal] = None
    for raw_row in data.trends:
        cols = raw_row.split(",")
        if len(cols) != 8:
            logger.warning(
                "[SectorAnalysis] trends2 分钟行列数 %s ≠ 8（%s），整体不采用",
                len(cols), raw_row[:40])
            return None
        try:
            minute_dt = datetime.strptime(cols[0], "%Y-%m-%d %H:%M")
        except ValueError:
            logger.warning(
                "[SectorAnalysis] trends2 分钟时间不可解析 %r，整体不采用",
                cols[0][:24])
            return None
        if minute_dt.date().isoformat() != expected_date:
            logger.warning(
                "[SectorAnalysis] trends2 分钟日期 %s ≠ 会话日 %s，整体不采用"
                "（不跨日累计）", minute_dt.date().isoformat(), expected_date)
            return None
        t = minute_dt.time()
        morning = _TRENDS_MIN_TIME <= t <= _TRENDS_MORNING_END
        afternoon = _TRENDS_AFTERNOON_OPEN <= t <= _TRENDS_AFTERNOON_END
        if not (morning or afternoon):
            logger.warning(
                "[SectorAnalysis] trends2 分钟 %s 不在交易时段窗口内，整体不采用",
                cols[0])
            return None
        if minute_dt > received:
            logger.warning(
                "[SectorAnalysis] trends2 分钟 %s 晚于响应接收时刻 %s（未来数据），"
                "整体不采用", cols[0], received.isoformat())
            return None
        if prev_minute is not None:
            gap_min = (minute_dt - prev_minute).total_seconds() / 60.0
            is_lunch = (prev_minute.time() == _TRENDS_MORNING_END
                        and gap_min == _TRENDS_LUNCH_GAP_MIN)
            if not is_lunch and gap_min != 1.0:
                logger.warning(
                    "[SectorAnalysis] trends2 分钟序列断档/乱序：%s → %s（%s 分钟），"
                    "整体不采用（不把有限行部分求和冒充累计）",
                    prev_minute.strftime("%H:%M"), minute_dt.strftime("%H:%M"),
                    gap_min)
                return None
        else:
            if t != _TRENDS_MIN_TIME:
                logger.warning(
                    "[SectorAnalysis] trends2 首分钟 %s 非 09:30（覆盖缺段），"
                    "整体不采用", cols[0])
                return None
        prev_minute = minute_dt
        named: Dict[str, Decimal] = {}
        for idx, key in ((1, "open"), (2, "close"), (3, "high"), (4, "low"),
                         (5, "volume"), (6, "amount"), (7, "avg")):
            cell = cols[idx]
            if cell in ("", "-", None):
                logger.warning(
                    "[SectorAnalysis] trends2 分钟 %s 字段 %s 缺失，当日累计"
                    "不完整，整体不采用", cols[0], key)
                return None
            try:
                value = Decimal(cell)
            except (ArithmeticError, ValueError):
                logger.warning(
                    "[SectorAnalysis] trends2 分钟 %s 字段 %s 非数值 %r，"
                    "整体不采用", cols[0], key, cell[:12])
                return None
            if not value.is_finite():
                logger.warning(
                    "[SectorAnalysis] trends2 分钟 %s 字段 %s 非有限 %s，"
                    "整体不采用", cols[0], key, value)
                return None
            if key in ("volume", "amount"):
                if value < 0:
                    logger.warning(
                        "[SectorAnalysis] trends2 分钟 %s 字段 %s 为负 %s，"
                        "整体不采用", cols[0], key, value)
                    return None
            elif value <= 0:
                logger.warning(
                    "[SectorAnalysis] trends2 分钟 %s 价格字段 %s 非正 %s，"
                    "整体不采用", cols[0], key, value)
                return None
            named[key] = value
        if not (named["low"] <= min(named["open"], named["close"])
                <= max(named["open"], named["close"]) <= named["high"]):
            logger.warning(
                "[SectorAnalysis] trends2 分钟 %s OHLC 行内不一致（o=%s c=%s "
                "h=%s l=%s），整体不采用", cols[0], named["open"], named["close"],
                named["high"], named["low"])
            return None
        minute_amounts.append(named["amount"])
        last_minute = cols[0]
        last_minute_dt = minute_dt
        last_close = named["close"]
    if session_finished and (last_minute_dt is None
                             or last_minute_dt.time() != _TRENDS_AFTERNOON_END):
        # 接收时该会话已完结：截尾前缀 ≠ 全天，不得充当日累计
        # （结合首分钟 09:30 + 段内连续 + 末分钟 15:00 = 完整交易时段）
        logger.warning(
            "[SectorAnalysis] trends2 接收时刻 %s 已过收盘但末分钟 %s 非 15:00"
            "（盘后截尾不冒充全天累计），整体不采用",
            received.isoformat(), last_minute)
        return None
    if not minute_amounts or last_close is None or last_close <= 0:
        logger.warning(
            "[SectorAnalysis] trends2 累计额/现价无效（rows=%s close=%s），"
            "不采用", len(minute_amounts), last_close)
        return None
    # time 与实际分钟序列/会话/接收上界一致（final 反例：完整分钟 + time=2030
    # 曾被采纳）：属于会话日、不早于末根分钟、不晚于实际接收时刻
    vendor_time = _vendor_epoch_naive_local(data.time)
    if (vendor_time is None
            or vendor_time.date() != ctx.session_date
            or last_minute_dt is None
            or not (last_minute_dt <= vendor_time <= received)):
        logger.warning(
            "[SectorAnalysis] trends2 供应商时点 %s 与会话 %s/分钟序列末根 %s/"
            "接收时刻 %s 不一致（畸形/旧日/未来），按源错误整体不采用",
            vendor_time.isoformat() if vendor_time else "畸形",
            ctx.session_date.isoformat(),
            last_minute_dt.isoformat(timespec="minutes") if last_minute_dt else "无",
            received.isoformat())
        return None
    total_amount = _sum_minute_amounts(minute_amounts)
    pre_close = Decimal(str(data.preClose))
    change_pct = float(_trends_change_pct(last_close, pre_close))
    payload = {
        "f57": data.code, "f58": data.name,
        "f48": float(total_amount), "f170": change_pct,
    }
    try:
        quote = quote_model.model_validate(payload)
    except ValidationError as exc:
        logger.warning("[SectorAnalysis] trends2 快照模型校验失败 secid=%s: %s",
                       secid, str(exc)[:120])
        return None
    logger.info(
        "[SectorAnalysis] trends2 backup 采纳：%s/%s 当日累计额 %s、涨跌幅 %s"
        "（供应商分钟截至 %s；衍生口径，非 f48 原样字段）",
        data.code, data.name, quote.f48, quote.f170, last_minute)
    return QuoteSnapshot(
        quote=quote, source="trends2当日累计", derived_amount=True,
        vendor_as_of=last_minute, vendor_time_epoch=data.time,
    )


def _fetch_stock_quote(secid: str, expected_code: str, expected_name: str,
                       *, budget: ReportBudget,
                       quote_model: type,
                       session_ctx: Optional[SessionContext] = None,
                       received_at: Optional[datetime] = None,
                       ) -> Optional[QuoteSnapshot]:
    """stock/get 单标的快照（板块或成员），f57/f58 与预期同键完整校验。

    代码或名称任一不符即源错误整体不采用（错名板块/错股票行情不得采用）。
    **交易时点资格（root entry-closure 反例驱动）**：供应商时点 f86 必须
    存在、解码不畸形、属于当前会话日，且**不晚于该响应实际接收时刻**。
    接收钟在本 source 的 ``_http_get_json`` 完成后立即取得（``received_at``
    仅供测试显式注入；session_ctx 只锚日历会话，绝不自动当冻结接收钟，
    生产调用链不提前取钟）；「会话是否已完结」同样按接收时刻判定——
    完结会话要求 f86 ≥ 收盘（盘后收到盘中旧快照拒），盘中要求 ≥ 开盘
    （同日未来拒），日期 ≠ 会话日拒（旧日/跨会话未来）。无时效阈值。
    缺时点 = 无可比资格，拒绝。as_of = f86 供应商时间戳（市场时区 iso）。
    """
    params = {"fltt": "2", "invt": "2", "fields": _BOARD_QUOTE_FIELDS,
              "secid": secid}
    if quote_model is MemberQuote:
        params["fields"] = _MEMBER_QUOTE_FIELDS
    data = _http_get_json(_EM_STOCK_GET_URL, params, budget=budget)
    inner = data.get("data") if isinstance(data, dict) else None
    if not isinstance(inner, dict):
        return None
    try:
        quote = quote_model.model_validate(inner)
    except ValidationError as exc:
        logger.warning("[SectorAnalysis] 行情快照模型校验失败 secid=%s: %s",
                       secid, str(exc)[:120])
        return None
    if quote.f57 != expected_code or quote.f58 != expected_name:
        logger.warning(
            "[SectorAnalysis] 行情快照同键不符：期望 %s/%s 实际 %s/%s，不采用",
            expected_code, expected_name, quote.f57, quote.f58)
        return None
    if quote.f86 is None or quote.f86 <= 0:
        logger.warning(
            "[SectorAnalysis] 行情快照无供应商时点 f86（secid=%s），"
            "无可比当日资格，不采用", secid)
        return None
    f86_local = _vendor_epoch_naive_local(int(quote.f86))
    ctx = session_ctx if session_ctx is not None else _session_context()
    # 接收钟：本响应 HTTP 完成后立即取得（显式注入仅供测试；不提前取钟）
    received = (received_at if received_at is not None
                else _received_at_now())
    if f86_local is None or ctx is None:
        logger.warning(
            "[SectorAnalysis] 行情快照时点/会话不可判定（secid=%s），不采用",
            secid)
        return None
    # 会话阶段按接收时刻判定（14:59:58 请求、15:00:02 接收 → 完结会话）
    lower = (ctx.session_close if received >= ctx.session_close
             else ctx.session_open)
    if (f86_local.date() != ctx.session_date
            or not (lower <= f86_local <= received)):
        logger.warning(
            "[SectorAnalysis] 行情快照供应商时点 %s 不具备会话 %s 当日可比资格"
            "（窗口 [%s, %s]；旧日/未来/盘后旧盘中快照/缺资格），不采用",
            f86_local.isoformat(), ctx.session_date.isoformat(),
            lower.isoformat(), received.isoformat())
        return None
    as_of = f86_local.replace(tzinfo=ZoneInfo(MARKET_TIMEZONE["cn"])).isoformat(
        timespec="seconds")
    return QuoteSnapshot(quote=quote, source="stock/get", derived_amount=False,
                         vendor_as_of=as_of,
                         vendor_time_epoch=int(quote.f86))


def _fetch_board_quote(board_code: str, board_name: str, *, budget: ReportBudget,
                       now: Optional[datetime] = None,
                       session_ctx: Optional[SessionContext] = None,
                       received_at: Optional[datetime] = None,
                       ) -> Optional[QuoteSnapshot]:
    """东财板块单标的当日快照：stock/get 优先，不可用时 trends2 当日累计口径
    backup（锁定名称+代码同键；换手率/总市值在 backup 无来源为 None+缺口）。
    会话上下文只锚日历；接收钟由每个 source 在其 HTTP 完成后自行取得。"""
    snap = _fetch_stock_quote(f"90.{board_code}", board_code, board_name,
                              budget=budget, quote_model=BoardQuote,
                              session_ctx=session_ctx, received_at=received_at)
    if snap is not None:
        return snap
    return _fetch_trends2_snapshot(f"90.{board_code}", board_code, board_name,
                                   budget=budget, quote_model=BoardQuote,
                                   now=now, session_ctx=session_ctx,
                                   received_at=received_at)


def _fetch_member_quote(member_code: str, member_name: str, *,
                        budget: ReportBudget,
                        now: Optional[datetime] = None,
                        session_ctx: Optional[SessionContext] = None,
                        received_at: Optional[datetime] = None,
                        ) -> Optional[QuoteSnapshot]:
    """成员个股行情快照：stock/get 优先，不可用时 trends2 当日累计口径 backup。

    backup 口径（2026-11-07 生产/真实 wire 对照）：trends2 分钟额求和=当日
    累计成交额（元，元级与 f48 同口径）、末分钟 close 对 preClose 的 Decimal
    涨跌幅；来源/as_of/衍生标记随 QuoteSnapshot 透出（provenance 贯穿载荷，
    不写 f48 原样）。两路共用同一会话时点资格（调用方单次锚定传入）。
    """
    snap = _fetch_stock_quote(_stock_secid(member_code), member_code,
                              member_name, budget=budget, quote_model=MemberQuote,
                              session_ctx=session_ctx, received_at=received_at)
    if snap is not None:
        return snap
    return _fetch_trends2_snapshot(_stock_secid(member_code), member_code,
                                   member_name, budget=budget,
                                   quote_model=MemberQuote, now=now,
                                   session_ctx=session_ctx,
                                   received_at=received_at)


def get_report_dir() -> Path:
    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    return _REPORT_DIR


def _claim_report_file(base: str) -> Optional[Tuple[str, Path]]:
    """原子独占创建报告文件（O_CREAT|O_EXCL），返回 (报告 id, 路径)。

    创建成功者独占该 ID 与文件：同分钟并发请求不会共享同一文件
    （先 exists 后 write 的竞态会让失败方 unlink 掉成功方的存档）。
    分钟级冲突自动追加字母后缀；全部后缀被占用时返回 None。
    只清理本函数返回路径的文件——失败方 unlink 的是自己独占创建的文件。
    """
    dir_ = get_report_dir()
    for candidate in [base] + [f"{base}{s}" for s in "abcdefgh"]:
        path = dir_ / f"{candidate}.md"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
            return candidate, path
        except FileExistsError:
            continue
    return None


# ---------------------------------------------------------------------------
# P0b 申万行业链反查：证监会/东财行业名 → 三级候选 → 成分股验证 → 一/二/三级链
# ---------------------------------------------------------------------------

_SW_TABLES_CACHE: Optional[Dict[str, Any]] = None
_SW_CHAIN_CACHE: Dict[str, Dict[str, Any]] = {}


def _sw_tables(budget: ReportBudget) -> Optional[Dict[str, Any]]:
    """申万一/二/三级指数表（进程内缓存，日级快照由缓存键日期保证）。"""
    global _SW_TABLES_CACHE
    if _SW_TABLES_CACHE is not None:
        return _SW_TABLES_CACHE
    try:
        import akshare as ak

        third = _bounded_ak(ak.sw_index_third_info, budget=budget)
        second = _bounded_ak(ak.sw_index_second_info, budget=budget)
        if third is None or second is None or third.empty or second.empty:
            return None
        _SW_TABLES_CACHE = {"third": third, "second": second}
        return _SW_TABLES_CACHE
    except Exception as exc:  # noqa: BLE001
        logger.warning("[SectorAnalysis] 申万指数表获取失败: %s", str(exc)[:80])
        return None


# 证监会行业名里的通用词（匹配时剔除，提高申万三级名命中率）
_SW_STOPWORDS = ("制造业", "服务业", "生产", "供应", "和", "其他", "业", "的")

# 单字行业词白名单（≥2 过滤会丢掉「酒/药/车」等关键单字词根）
_SW_SINGLE_CHAR = frozenset("酒药茶车军银钢煤纸油气电医食纺服")


def _sw_keywords(industry_text: str) -> List[str]:
    """行业/主营文本 → 关键词集合。

    中文连续文本无空格分词（「半导体湿法清洗设备」是一个整词），直接整词匹配
    申万三级名必然 miss——对 ≥4 字的单元做 2-4 字滑窗补充（半导体/湿法/清洗/设备
    等词根进入集合）；单字单元仅白名单词根保留（酒/药/车等）。

    Ⅱ/Ⅲ 级后缀归一（issue #81 局部命名修复，真实反例 600519「白酒Ⅱ」）：
    带级位的单元同时加入去级位基词（白酒Ⅱ → 白酒），否则 3 字单元不滑窗、
    与申万三级名「白酒Ⅲ」整词不匹配，反查恒空。匹配仍以成分验证收口。
    """
    text = industry_text or ""
    for w in _SW_STOPWORDS:
        text = text.replace(w, " ")
    units = [t for t in re.split(r"[、，,\s/：:；;（）()]+", text) if t]
    kws: List[str] = []
    for u in dict.fromkeys(units):  # 保序去重
        base = _SW_LEVEL_SUFFIX_RE.sub("", u)
        if len(u) < 2:
            if u in _SW_SINGLE_CHAR and u not in kws:
                kws.append(u)
            continue
        if u not in kws:
            kws.append(u)
        if base != u and len(base) >= 2 and base not in kws:
            kws.append(base)
        effective = base if base != u else u
        if len(effective) >= 4:  # 滑窗补充 4/3/2 字词根
            for n in (4, 3, 2):
                for i in range(len(effective) - n + 1):
                    gram = effective[i : i + n]
                    if gram not in kws:
                        kws.append(gram)
    return kws


_SW_LEVEL_SUFFIX_RE = re.compile(r"[ⅠⅡⅢⅣⅤ]+$")


def _sw_hit_count(kws: List[str], name: Any) -> int:
    """关键词对申万行业名的命中数（Ⅱ/Ⅲ 归一基词参与匹配）。"""
    return sum(1 for k in kws if k in str(name))


def _resolve_sw_chain(code: str, industry_text: str, *, budget: "ReportBudget") -> Dict[str, Any]:
    """个股 → 申万一/二/三级链（参考口径，单列披露，不与东财成分混算）。

    路径：行业文本拆词 → 三级名候选（按命中词数排序，最多 8 个送成分验证）
    → index_component_sw 验证个股在成分中 → 三级行「上级行业」爬二级 → 一级。
    全链失败返回空链 + 缺口（如实，不编造）。结果按 自然日+代码 缓存。

    可选阶段隔离：取数预算耗尽记缺口返回，不向上传播 BudgetExhausted——
    申万参考链不影响东财主身份与主体报告存档。
    """
    chain: Dict[str, Any] = {"l1": "", "l2": "", "l3": "", "l3_code": "", "gaps": []}
    cache_key = f"{code}:{datetime.now().strftime('%Y%m%d')}"
    if cache_key in _SW_CHAIN_CACHE:
        return _SW_CHAIN_CACHE[cache_key]

    try:
        tables = bounded_call(_sw_tables, budget, budget=budget)
    except BudgetExhausted:
        chain["gaps"].append("申万链：取数预算不足，参考链未取得（参考口径，不影响主报告）")
        return chain
    if tables is None:
        chain["gaps"].append("申万链：申万指数表不可用")
        return chain
    third, second = tables["third"], tables["second"]

    kws = _sw_keywords(industry_text)
    if not kws:
        chain["gaps"].append(f"申万链：行业文本「{industry_text}」拆词后无有效关键词")
        return chain

    third = third.copy()
    third["_hits"] = third["行业名称"].map(lambda n: _sw_hit_count(kws, n))
    third["_nlen"] = third["行业名称"].map(lambda n: len(str(n)))
    # 同 hits 按名称长度升序：更精确的行业名（白酒）优先于边角长尾（其他农产品加工）
    candidates = (
        third[third["_hits"] > 0]
        .sort_values(["_hits", "_nlen"], ascending=[False, True])
        .head(8)
    )
    if candidates.empty:
        chain["gaps"].append(f"申万链：三级表无命中「{industry_text}」的候选板块")
        return chain

    locked = None
    for _, row in candidates.iterrows():
        if budget.expired():
            chain["gaps"].append("申万链：剩余预算不足，候选成分验证中止（如实披露）")
            break
        code_col = str(row.get("行业代码") or "").replace(".SI", "")
        try:
            import akshare as ak

            # 候选循环里 1-8 个候选要快速失败切换，不走重试（retries=0）——但
            # 必须经 _bounded_ak：bounded_call 的 require_fetch 只在 submit 前
            # 主线程检查，直接传 vendor 会让工作线程晚获得执行权时绕过 fetch
            # 截止（反例实测 vendor 在截止后 +0.16s 才启动）。线程内首出站
            # 检查同一取数截止，retries=0 保持快速切换语义。
            cons = _bounded_ak(ak.index_component_sw, symbol=code_col,
                               budget=budget, retries=0)
        except BudgetExhausted:
            chain["gaps"].append("申万链：剩余预算不足，候选成分验证中止（如实披露）")
            break
        except Exception:  # noqa: BLE001 - 单个候选失败继续下一个
            continue
        if cons is None or cons.empty or "证券代码" not in cons.columns:
            continue
        if cons["证券代码"].astype(str).str.contains(code).any():
            locked = row
            break
    if locked is None:
        chain["gaps"].append(f"申万链：{len(candidates)} 个候选三级板块成分验证均未命中 {code}")
        return chain

    chain["l3"] = str(locked.get("行业名称") or "")
    chain["l3_code"] = str(locked.get("行业代码") or "")
    l2_name = str(locked.get("上级行业") or "")
    chain["l2"] = l2_name
    if l2_name and not second.empty:
        l2_row = second[second["行业名称"] == l2_name]
        if not l2_row.empty:
            chain["l1"] = str(l2_row.iloc[0].get("上级行业") or "")
    _SW_CHAIN_CACHE[cache_key] = chain
    return chain


# ---------------------------------------------------------------------------
# P1 主行业身份（issue #81）：东财个股行业 → 东财行业板块锁定（精确名称+板块代码）
# → 同板块成分验证目标股 → 申万参考链（单列，不与东财成分混算）
# ---------------------------------------------------------------------------

def _resolve_em_board(industry_name: str, board_table: pd.DataFrame) -> Dict[str, Any]:
    """东财行业板块全表 → 精确名称锁定（板块名称+板块代码双确认）。

    不做包含/前缀近似匹配：无精确命中时不取首行，近似名如实透出供人工核对；
    多行精确命中按多候选处理，不静默确证；缺板块代码列时同样不可锁定。
    """
    out: Dict[str, Any] = {
        "locked": False, "board_name": "", "board_code": "",
        "near_names": [], "gaps": [],
    }
    if board_table is None or board_table.empty or "板块名称" not in board_table.columns:
        out["gaps"].append("东财行业板块表为空或缺名称列，板块身份未锁定")
        return out
    exact = board_table[board_table["板块名称"].astype(str) == industry_name]
    if len(exact) > 1:
        codes = sorted({str(c) for c in exact.get("板块代码", pd.Series(dtype=str)).tolist()})
        out["gaps"].append(
            f"板块表「{industry_name}」精确命中 {len(exact)} 行（代码 {codes[:5]}），无法唯一锁定"
        )
        return out
    if len(exact) == 1:
        board_code = str(exact.iloc[0].get("板块代码") or "").strip()
        if not board_code or board_code.lower() == "nan":
            out["gaps"].append(f"板块表「{industry_name}」命中行缺少板块代码，名称+代码双确认不成立")
            return out
        out["locked"] = True
        out["board_name"] = industry_name
        out["board_code"] = board_code
        return out
    # 无精确命中：披露近似名（不做包含/截短匹配，不取首行）。
    # 近似 = 板块名与行业名互为子串（白酒 ⊂ 白酒Ⅱ / 电池 ⊂ 电池薄膜），仅透出核对。
    near = board_table[
        board_table["板块名称"].map(
            lambda n: bool(str(n)) and (str(n) in industry_name or industry_name in str(n))
        )
    ]
    near_names = sorted({str(n) for n in near["板块名称"].tolist()})[:5]
    out["near_names"] = near_names
    hint = f"；存在近似名 {near_names}（不近似取首行）" if near_names else ""
    out["gaps"].append(f"板块表无「{industry_name}」精确命中，板块身份未锁定{hint}")
    return out


def _fallback_f10_verify(code: str, out: Dict[str, Any], *,
                         budget: ReportBudget) -> None:
    """路径 A（clist 全表锁定成功）成分验证失败后的 F10 兜底：反向成员验证。

    clist 成分端点不可用/为空时：F10 反向完整成员（同 clist 成分语义，
    f10_members）验证归属。目标不在集合或成员查询不可用时保持 unverified
    （root 收敛轮契约：**声明只用于精确板块锁定，不作为归属确认证据**——
    完整成员不可得即归属未确认，不创建更弱确认口径）。仅在板块已锁定且
    尚未 verified 时调用；程序异常原样传播（调用方不在 broad except 内调用）。
    ``claims_resp`` 由调用方传入已取得的声明列表（避免同预算内重复出站）。
    """
    if not out.get("board_code") or out["membership"].get("verified"):
        return
    members = _fetch_board_members(out["board_code"], out["board_name"],
                                   code, budget=budget)
    if members is None:
        out["gaps"].append(
            "F10 板块完整成员查询不可用，归属未确认（声明仅用于板块锁定，"
            "不作为确认证据）")
        return
    if not members["target_in"]:
        out["gaps"].append(
            f"F10 板块成员集合 {members['member_count']} 只不含目标股 {code}，"
            "归属未确认")
        return
    out["membership"]["verified"] = True
    out["membership"]["verified_source"] = "f10_members"
    out["membership"]["member_count"] = members["member_count"]
    df = _members_snapshot_df(members["rows"])
    batch = _collect_member_quotes(df, budget=budget)
    out["_cons_snapshot"] = batch.df
    out["_cons_observed_at"] = datetime.now().isoformat(timespec="seconds")
    out["_cons_quote_batch"] = batch
    out["gaps"].append(
        "归属确认来源：东财 F10 板块完整成员集合（成分来源 f10_members）")
    out["gaps"].extend(batch.gaps)


def _lock_board_via_claim(code: str, industry_name: str, out: Dict[str, Any], *,
                          budget: ReportBudget,
                          claims_resp: Optional[F10BoardResponse] = None) -> None:
    """clist 全表不可用时的 F10 声明锁定 + 板块反向完整成员成分验证。

    锁定：个股声明列表中**恰一行** BOARD_TYPE=「行业」AND BOARD_LEVEL=「2」
    的精确同名行业板块 → 名称+代码（概念/衍生同名行不参与锁定；多行/无
    命中不取首行、不近似锁定）。归属验证：板块反向完整成员（分页核齐、
    类型层级校验、目标在集合）→ verified_source="f10_members" + member_count
    + 同业成分快照（成员行情逐股采集，预算内受同一取数截止）；**成员查询
    不可用 → 保持 unverified**（声明只用于锁定，不升 confirmed）。
    observed_at 在锁定成功时统一设置。
    """
    claims = (claims_resp if claims_resp is not None
              else _fetch_board_claims(code, budget=budget))
    if claims is None:
        out["gaps"].append(
            "东财行业板块表与 F10 归属声明均不可用，板块身份未锁定")
        return
    industry_hits = [
        r for r in claims.result.data
        if r.BOARD_NAME == industry_name
        and r.BOARD_TYPE == "行业" and r.BOARD_LEVEL == "2"
    ]
    if len(industry_hits) > 1:
        codes = sorted({r.NEW_BOARD_CODE for r in industry_hits})
        out["gaps"].append(
            f"F10 归属声明「{industry_name}」行业 L2 板块命中 {len(industry_hits)} 行"
            f"（{codes[:5]}），无法唯一锁定，不取首行")
        return
    if not industry_hits:
        out["gaps"].append(
            f"F10 归属声明列表无精确同名行业 L2 板块「{industry_name}」，"
            "不按近似名/概念同名锁定")
        return
    claim = industry_hits[0]
    out["board_name"] = claim.BOARD_NAME
    out["board_code"] = claim.NEW_BOARD_CODE
    out["observed_at"] = datetime.now().isoformat(timespec="seconds")

    members = _fetch_board_members(claim.NEW_BOARD_CODE, claim.BOARD_NAME,
                                   code, budget=budget)
    if members is None:
        out["gaps"].append(
            "归属验证未执行：东财 F10 板块完整成员查询不可用（声明仅用于"
            "板块锁定，不作为确认证据），归属未确认")
        return
    if not members["target_in"]:
        out["gaps"].append(
            f"东财板块「{industry_name}」（{claim.NEW_BOARD_CODE}）F10 成员集合 "
            f"{members['member_count']} 只不含目标股 {code}，归属未确认")
        return
    out["membership"]["verified"] = True
    out["membership"]["verified_source"] = "f10_members"
    out["membership"]["member_count"] = members["member_count"]
    df = _members_snapshot_df(members["rows"])
    batch = _collect_member_quotes(df, budget=budget)
    out["_cons_snapshot"] = batch.df
    out["_cons_observed_at"] = datetime.now().isoformat(timespec="seconds")
    out["_cons_quote_batch"] = batch
    out["gaps"].append(
        "归属确认来源：东财 F10 板块完整成员集合（成分来源 f10_members）")
    out["gaps"].extend(batch.gaps)


def _identify_sector(code: str, *, budget: ReportBudget) -> Dict[str, Any]:
    """识别个股主行业身份（同一东财行业板块口径，issue #78/#81）。

    subject_status 语义（贯穿载荷/正文/页面，不静默取近似名）：
    - confirmed：东财板块锁定（精确名称+板块代码）且成分表含目标股
    - unverified：板块已锁定但成分验证失败或不含目标股（归属未确认）
    - ambiguous：行业名存在但板块身份无法唯一锁定（无精确命中/多候选/表不可用）
    - missing：无行业数据（识别链完全失败）

    供应商 as_of 未回显时 ``vendor_as_of`` 保持 None；``observed_at`` 是本系统
    采集时点，两者不混用。巨潮仅作行业名参考兜底。旧 fuyao 兜底（伪造 _Ctx
    调用 + 经深研聚合的隐式大出站）已移除：实现错误不再被包装成供应商失败。
    """
    out: Dict[str, Any] = {
        "industry_em": "",        # 东财个股信息行业名（无代码/层级/日期元数据）
        "industry_cninfo": "",    # 巨潮证监会行业（兜底参考）
        "business_scope": "",     # 巨潮主营业务文本（申万反查/政策/基率的关键词补充）
        "board_name": "",         # 东财行业板块精确名称
        "board_code": "",         # 东财板块代码（BK****）
        "subject_status": "missing",
        "membership": {"verified": False, "member_count": None,
                       "verified_source": None},
        "observed_at": None,      # 本系统采集时点（与供应商 as_of 区分）
        "vendor_as_of": None,     # 供应商未回显 as_of，如实为未知
        "classification_system": None,   # 分类体系（锁定口径）；无行业数据时 None
        "classification_level": None,    # 供应商未返回层级，如实未知不推算
        "classification_version": None,  # 供应商未返回版本，如实未知不推算
        "sw_chain": {},           # 申万一/二/三级参考链（成分验证；口径单列）
        "sector_name": "",        # 当日行情查询键（锁定的板块名）
        "gaps": [],
        "_board_table": None,     # 非载荷键：身份阶段板块全表快照（行情支柱复用）
        "_cons_snapshot": None,   # 非载荷键：成分验证同一快照（同业支柱复用，issue #82）
        "_cons_observed_at": None,  # 非载荷键：成分快照采集时点（与板块表时点分开）
    }
    # 1) 东财个股信息（行业名 f127；字段仅名称，无层级/代码/日期元数据，如实未知；
    #    stock/get 间歇不可用时由 F10 声明 L2 行业板块补位，来源如实标注）
    f127_name = ""
    try:
        import akshare as ak

        df = _bounded_ak(ak.stock_individual_info_em, symbol=code, budget=budget)
        if df is not None and not df.empty:
            kv = dict(zip(df["item"], df["value"]))
            f127_name = str(kv.get("行业") or "").strip()
            out["industry_em"] = f127_name
    except BudgetExhausted:
        raise
    except Exception as exc:  # noqa: BLE001
        out["gaps"].append(f"东财个股行业查询失败: {str(exc)[:50]}")

    # 2) 巨潮资讯（证监会行业 + 主营业务文本，独立链路兜底参考；不同分类体系，
    #    仅作行业名参考，不参与东财锁定与成分/行情拼接）
    if not out["industry_em"]:
        try:
            import akshare as ak

            df = _bounded_ak(ak.stock_profile_cninfo, symbol=code, budget=budget)
            if df is not None and not df.empty:
                row0 = df.iloc[0]
                industry = str(row0.get("所属行业") or "").strip()
                if industry:
                    out["industry_cninfo"] = industry
                # 主营业务文本：证监会行业名常不含赛道词（至纯=专用设备 vs 申万=半导体设备），
                # 主营业务补足赛道关键词，提高申万链/政策/基率命中率
                biz = str(row0.get("主营业务") or "").strip()
                if biz and biz != "nan":
                    out["business_scope"] = biz[:200]
        except BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001
            out["gaps"].append(f"巨潮行业查询失败: {str(exc)[:50]}")

    # 3) 东财行业锁定：行业名优先取 F10 声明列表的 L2 行业板块（BOARD_TYPE=
    #    「行业」AND BOARD_LEVEL=「2」，与 stock/get f127 同义同源——2026-11-07
    #    生产/本地双源比对一致；datacenter 集群稳定，规避 stock/get 间歇不可用）。
    #    f127 可用时与声明名交叉校验（不一致=源歧义不锁定）；clist 全表可用时
    #    原路径优先（全功能），否则声明+反向完整成员。
    claims_resp = _fetch_board_claims(code, budget=budget)
    declared_name: Optional[str] = None
    if claims_resp is not None:
        l2_rows = [r for r in claims_resp.result.data
                   if r.BOARD_TYPE == "行业" and r.BOARD_LEVEL == "2"]
        if len(l2_rows) == 1:
            declared_name = l2_rows[0].BOARD_NAME
        elif len(l2_rows) > 1:
            out["gaps"].append(
                f"F10 声明列表 L2 行业板块命中 {len(l2_rows)} 行，无法唯一锁定")
    if f127_name and declared_name and f127_name != declared_name:
        out["gaps"].append(
            f"行业名源不一致：stock/get「{f127_name}」vs F10 声明「{declared_name}」，"
            "不锁定（源歧义如实）")
        declared_name = None
        f127_name = ""
    industry_name = declared_name or f127_name
    if industry_name and not out["industry_em"]:
        out["industry_em"] = industry_name
        out["gaps"].append(
            "个股行业名来源：东财 F10 声明列表 L2 行业板块（stock/get 不可用）")
    lookup_industry = industry_name  # 仅东财口径名参与锁定；巨潮名不参与
    board_table: Optional[pd.DataFrame] = None
    if lookup_industry:
        try:
            import akshare as ak

            board_table = _bounded_ak(ak.stock_board_industry_name_em, budget=budget)
            out["observed_at"] = datetime.now().isoformat(timespec="seconds")
            out["_board_table"] = board_table  # 非载荷键：行情支柱复用同一快照
        except BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001
            out["gaps"].append(
                f"东财行业板块表查询失败: {str(exc)[:50]}（转 F10 归属声明路径）")
            board_table = None
        if board_table is not None and board_table.empty:
            # 空表等同不可用：身份走声明路径，行情支柱不再复用空快照
            board_table = None
            out["_board_table"] = None

    if lookup_industry and board_table is not None and not board_table.empty:
        resolved = _resolve_em_board(lookup_industry, board_table)
        out["gaps"].extend(resolved["gaps"])
        if resolved["locked"]:
            out["board_name"] = resolved["board_name"]
            out["board_code"] = resolved["board_code"]
            # 4) 同板块成分验证目标股（归属确认；申万成分不替代东财成分）。
            #    异常所有权（root 收敛轮）：AkShare 供应商边界的 broad except
            #    只生成缺口文本；成分不可用的统一处理（含 F10 反向成员 fallback）
            #    在 try/except **之外**——程序 TypeError/ValueError/AssertionError
            #    原样传播、fallback 仅派发一次，不被吞成缺口、不被当供应商故障
            #    重试（budget 截止仍按原语义向上传播走 504 路径）。
            cons: Optional[pd.DataFrame] = None
            cons_error: Optional[str] = None
            try:
                import akshare as ak

                cons_df = _bounded_ak(
                    ak.stock_board_industry_cons_em,
                    symbol=out["board_code"], budget=budget,
                )
                if isinstance(cons_df, pd.DataFrame):
                    cons = cons_df
            except BudgetExhausted:
                raise
            except Exception as exc:  # noqa: BLE001 - AkShare 供应商边界错误
                cons_error = f"东财板块成分查询失败: {str(exc)[:50]}"
                cons = None
            if cons is None or cons.empty or "代码" not in cons.columns:
                if cons_error is not None:
                    out["gaps"].append(cons_error)
                else:
                    out["gaps"].append("东财板块成分表为空或缺代码列，归属未确认")
                # clist 成分不可用：F10 反向完整成员兜底（同 clist 成分语义）
                _fallback_f10_verify(code, out, budget=budget)
            else:
                cons_codes = cons["代码"].astype(str)
                out["membership"]["member_count"] = int(len(cons_codes))
                if (cons_codes == code).any():
                    out["membership"]["verified"] = True
                    out["membership"]["verified_source"] = "cons_table"
                else:
                    out["gaps"].append(
                        f"东财板块「{out['board_name']}」（{out['board_code']}）成分 "
                        f"{len(cons_codes)} 只不含目标股 {code}，归属未确认"
                    )
                # issue #82：同一已验证快照保留给同业支柱（避免重复请求与
                # 盘中漂移）；非载荷键，compose_report pop。
                out["_cons_snapshot"] = cons
                out["_cons_observed_at"] = datetime.now().isoformat(timespec="seconds")
    elif lookup_industry and claims_resp is not None:
        # clist 全表不可用：F10 声明锁定 + 板块反向完整成员成分验证（同业恢复）
        _lock_board_via_claim(code, lookup_industry, out, budget=budget,
                              claims_resp=claims_resp)
    elif lookup_industry:
        out["gaps"].append(
            "东财行业板块表与 F10 归属声明均不可用，板块身份未锁定")

    # 5) 申万一/二/三级参考链移至 compose_report（issue #82）：趋势/同业等
    # 必需新块必须先于申万参考等可选长链出站，不能让可选链吃完取数预算；
    # 申万链在此处为空 dict，由编排层回填同一结构。

    # 6) subject_status 判定（行业数据缺失 = 东财 f127/F10 声明与巨潮均无）
    if not lookup_industry and not out["industry_cninfo"]:
        out["subject_status"] = "missing"
        out["gaps"].append("板块识别失败：无行业数据")
    elif out["board_code"]:
        out["subject_status"] = "confirmed" if out["membership"]["verified"] else "unverified"
    else:
        out["subject_status"] = "ambiguous"
    out["sector_name"] = out["board_name"] or lookup_industry
    # 分类元数据：体系名=本报告锁定的东财行业板块口径；层级/版本供应商未返回
    # 恒为 None（正文如实标未知，不推算、不用查询时间顶替）。
    if lookup_industry:
        out["classification_system"] = "东财行业板块"
    return out


# ---------------------------------------------------------------------------
# P2 政策倾向支柱（行业 DNA 库）
# ---------------------------------------------------------------------------

def _policy_pillar(industry_text: str) -> Dict[str, Any]:
    lean: Optional[str] = None
    gaps: List[str] = []
    try:
        from src.services.supply_chain.industry_dna_loader import (
            get_all_dna,
            lookup_industry_policy_lean,
        )

        # 先走官方短词查询（语义对齐 F2 维度）
        lean = lookup_industry_policy_lean(industry_text)
        if lean is None:
            # loader 是反向匹配（候选词须完整出现在 DNA 关键词里），长文本必 miss——
            # 反查：遍历 DNA 关键词，找出现在文本中的词，取首个命中 DNA 的政策倾向
            text = (industry_text or "").lower()
            for dna in get_all_dna().values():
                if any(str(k).lower() in text for k in dna.keywords):
                    lean = getattr(dna, "extra", {}).get("policy_lean") if isinstance(
                        getattr(dna, "extra", None), dict
                    ) else None
                    if lean is None:
                        lean = getattr(dna, "policy_lean", None)
                    if lean in ("supportive", "neutral", "restrictive"):
                        break
                    lean = None
    except Exception as exc:  # noqa: BLE001
        gaps.append(f"政策倾向查询失败: {str(exc)[:60]}")
    # v2 载荷不再携带政策档位分：政策 DNA 是静态规则背景，方向标签（支持/中性/
    # 限制）足够表达，分数容易与综合评分混淆
    return {
        "lean": lean,
        "label": _POLICY_LABEL.get(lean, "未知"),
        "gaps": gaps,
    }


# ---------------------------------------------------------------------------
# P3 行业基率支柱（启发式默认表，可迭代，版本透出）
# ---------------------------------------------------------------------------

def _base_rate_pillar(lookup_text: str) -> Dict[str, Any]:
    from src.deep_research_dims.industry_base_rate import (
        all_rates,
        lookup_base_rate,
        rate_percentile,
        table_meta,
    )

    rate, basis = lookup_base_rate(lookup_text)
    meta = table_meta()
    return {
        "base_rate": rate,
        "basis": basis,
        "hit_keyword": basis.split(":", 1)[1] if ":" in basis else "",
        "is_default": basis == "neutral_default",
        "table_version": meta["version"],
        "table_updated": meta["updated"],
        "table_industry_count": meta["industry_count"],
        "percentile": rate_percentile(rate),
        "all_rates": all_rates(),
    }


# ---------------------------------------------------------------------------
# P4 当日行情支柱（东财行业板块全表：动量/换手活跃度/成交额活跃度/广度）
#
# 口径纪律（issue #78/#81）：同一东财板块（名称+板块代码）精确命中，不做
# 名称近似/截短匹配，不回退新浪证监会口径板块表（不同分类不混算）；
# 「成交额活跃度」是成交额相对水平，不是资金净流入；当日价格强弱不是产业景气。
# ---------------------------------------------------------------------------

def _median(values: List[Optional[float]]) -> Optional[float]:
    """中位数计算。接受 ``Optional[float]``，自动过滤 None。"""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2


@require(lambda sector_name: isinstance(sector_name, str),
         "sector_name must be a string")
@require(lambda budget: isinstance(budget, ReportBudget) and not budget.expired(),
         "budget must be a live ReportBudget")
@require(lambda board_table: board_table is None or isinstance(board_table, pd.DataFrame),
         "board_table snapshot must be None or a DataFrame")
@ensure(
    lambda result: (
        isinstance(result, dict)
        and "sector_found" in result
        and "gaps" in result
    ),
    "intraday pillar must return dict with sector_found/gaps keys",
)
@ensure(
    lambda result: (
        result.get("rank_of") is None
        or result.get("total_boards") is None
        or 1 <= result["rank_of"] <= result["total_boards"]
    ),
    "rank_of must be in [1, total_boards] when both are set",
)
@ensure(
    lambda result: all(
        result.get(dim) is None or 0.0 <= result[dim] <= 100.0
        for dim in ("momentum", "activity", "amount_activity", "breadth")
    ),
    "intraday dims must be None or in [0, 100]",
)
def _prosperity_pillar(sector_name: str, *, budget: ReportBudget,
                       board_code: Optional[str] = None,
                       board_table: Optional[pd.DataFrame] = None,
                       now: Optional[datetime] = None,
                       received_at: Optional[datetime] = None) -> Dict[str, Any]:
    """东财行业板块全表 → 目标板块行 + 四维当日行情分（0-100）。

    四维：动量（涨跌幅有效样本分位 = 严格低于目标的有效样本数 ÷
    (有效样本数−1) × 100，并列不计入「低于」数，并列最高不恒为 100、
    全相等为 0）/ 换手活跃度（换手率 vs 全表中位）/成交额活跃度（成交额
    vs 全表中位，非资金净流入）/广度（上涨家数占比）。全表排名只在涨跌幅
    全部有效时输出（名次 = 严格高于目标的个数 + 1，并列最高同为 1）；有效
    样本不足 2 个 → 比较样本不足，动量与全表排名保守 N/A；2 ≤ 有效样本 <
    观测总量（同行涨幅缺失/非有限）时有效集合 ≠ 观测全表，不输出无依据的
    全表名次，动量仍按有效样本计算并在缺口注明资格（rank_of=None）。
    ``total_boards`` 保持观测板块总量语义。

    命中纪律（issue #81 第二轮：同键唯一，绝不 iloc[0] 裁决）：已知板块代码
    （身份锁定同键）时命中行必须「板块代码与锁定代码一致**且**板块名称与
    锁定名称一致」且唯一——同键恰 1 行才命中；同键多行（重复同键）、代码
    命中但无同名行（错名/快照漂移）、代码列缺失（同键不成立）都记缺口，
    不取首行、不回退名称或近似名（同名不同代码会绑错板块）。无代码时按
    唯一精确名称（多行命中不可锁定、不取首行）；包含/前缀截短近似不命中。
    东财表不可用时不回退新浪口径板块表。``board_table`` 传入身份阶段的
    同一快照时直接复用，不再二次出站（身份与行情同一口径快照）。

    全表不可用（clist 被拒，2026-11-07 生产窗口）且已锁定板块代码时：板块
    单标的快照（stock/get 优先，trends2 供应商分钟累计口径 backup）恢复 row
    ——动量/换手活跃度/成交额活跃度/广度/全表排名是**全表口径**，单标的
    快照无全表聚合来源，如实 N/A + 缺口（不按单板块值冒充分位）；上涨/
    下跌家数与领涨股同为全表字段保持 None；快照来源/as_of 经
    ``snapshot_source`` 载荷字段与缺口贯穿（衍生口径如实标注，不写 f48 原样）。
    """
    out: Dict[str, Any] = {
        "sector_found": False,
        "momentum": None, "activity": None, "amount_activity": None, "breadth": None,
        "rank_of": None, "total_boards": None,
        "row": {},
        "snapshot_source": None,
        "gaps": [],
    }
    if not sector_name:
        out["gaps"].append("当日行情：板块名为空")
        return out
    df: Optional[pd.DataFrame] = None
    ctx = _session_context(now)
    if board_table is not None:
        df = board_table
    else:
        try:
            import akshare as ak

            df = _bounded_ak(ak.stock_board_industry_name_em, budget=budget)
        except BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001
            out["gaps"].append(
                f"当日行情：东财行业板块表查询失败 {str(exc)[:50]}"
                "（转板块单标的快照路径）")
            df = None
    if not isinstance(df, pd.DataFrame) or df.empty:
        df = None
        if not board_code:
            out["gaps"].append(
                "当日行情：东财行业板块全表为空或不可用，且无板块代码可查单标的快照")
            return out
        # 全表不可用（clist 被拒，2026-11-07 生产窗口）：板块单标的快照恢复 row
        # （锁定名称+代码同键校验，错名板块不得采用）。动量/换手活跃度/
        # 成交额活跃度/广度/全表排名是**全表口径**——单标的快照无全表聚合
        # 来源，如实 N/A + 缺口，不按单板块值冒充分位；上涨/下跌家数与
        # 领涨股同为全表字段，快照无来源保持 None。
        snap = _fetch_board_quote(board_code, sector_name, budget=budget,
                                  now=now, session_ctx=ctx,
                                  received_at=received_at)
        if snap is None:
            out["gaps"].append(
                "当日行情：东财行业板块全表与板块单标的快照均不可用")
            return out
        quote = snap.quote
        if snap.source != "stock/get":
            out["snapshot_source"] = (
                f"东财 trends2 {_DERIVED_MARKER}（f48=分钟额求和、非原样字段，"
                f"元级与 f48 同口径；涨跌幅=末分钟现价对 preClose Decimal 计算；"
                f"供应商分钟截至 {snap.vendor_as_of or '未知'}）")
            out["gaps"].append(
                f"当日行情：板块快照经 trends2 {_DERIVED_MARKER}补采"
                f"（供应商分钟截至 {snap.vendor_as_of or '未知'}，非 f48 原样；"
                "换手率/总市值无该来源字段保持 None）")
        else:
            out["snapshot_source"] = (
                f"东财 stock/get 单标的快照（f48/f170 原样映射；供应商时间戳 "
                f"{snap.vendor_as_of or '未提供'}）")
        out["sector_found"] = True
        out["row"] = {
            "board_name": quote.f58,
            "board_code": quote.f57,
            "change_pct": quote.f170,
            "turnover_rate": quote.f168,
            "total_market_cap": quote.f116,
            "amount": quote.f48,
            "up_count": None,
            "down_count": None,
            "leader": "",
            "leader_change_pct": None,
        }
        out["gaps"].append(
            "当日行情：东财行业板块全表不可用（clist 被拒），仅恢复单板块"
            "快照行；动量/换手活跃度/成交额活跃度/广度/全表排名为全表口径，"
            "如实 N/A（不按单板块值冒充分位）")
        return out
    if "板块名称" not in df.columns:
        out["gaps"].append("当日行情：板块表缺少名称列")
        return out

    df = df.copy()
    total = len(df)
    out["total_boards"] = total

    # 命中行：已知板块代码 → 名称+代码同键唯一命中（同键冲突/错名/缺列记缺口，
    # 绝不 iloc[0] 静默裁决）；无代码 → 唯一精确名称
    hit: Optional[pd.DataFrame] = None
    if board_code:
        if "板块代码" not in df.columns:
            out["gaps"].append(
                f"当日行情：行情表缺板块代码列，无法与锁定代码 {board_code} 同键命中，"
                "不按名称回退取行"
            )
            return out
        by_code = df.loc[df["板块代码"].astype(str) == str(board_code)]
        if by_code.empty:
            out["gaps"].append(
                f"当日行情：身份锁定板块代码 {board_code} 未出现在行情全表，"
                "不按名称回退取行（同名板块可能不同代码）"
            )
            return out
        same_key = by_code.loc[by_code["板块名称"].astype(str) == str(sector_name)]
        if len(same_key) == 1:
            hit = same_key
        elif len(same_key) > 1:
            out["gaps"].append(
                f"当日行情：板块「{sector_name}」（{board_code}）同键命中 {len(same_key)} 行，"
                "无法唯一锁定，不取首行"
            )
            return out
        else:
            wrong = sorted({str(n) for n in by_code["板块名称"].tolist()})[:5]
            out["gaps"].append(
                f"当日行情：板块代码 {board_code} 在行情表对应名称 {wrong}，"
                f"与锁定名称「{sector_name}」不一致（同代码可能绑多板块/快照漂移），"
                "不取首行"
            )
            return out
    if hit is None or hit.empty:
        hit = df.loc[df["板块名称"].astype(str) == sector_name]
        if len(hit) > 1:
            out["gaps"].append(
                f"当日行情：名称「{sector_name}」命中 {len(hit)} 行，无法唯一锁定，不取首行"
            )
            return out
    if hit.empty:
        out["gaps"].append(
            f"当日行情：全表 {total} 个板块未精确命中「{sector_name}」"
            + (f"（板块代码 {board_code}）" if board_code else "")
            + "，不按近似名取行"
        )
        return out

    row = hit.iloc[0]
    out["sector_found"] = True
    out["row"] = {
        "board_name": str(row.get("板块名称") or ""),
        "board_code": str(row.get("板块代码") or "") if "板块代码" in df.columns else "",
        "change_pct": _num(row.get("涨跌幅")),
        "turnover_rate": _num(row.get("换手率")),
        "total_market_cap": _num(row.get("总市值")),
        "amount": _num(row.get("成交额")),
        "up_count": _num(row.get("上涨家数")),
        "down_count": _num(row.get("下跌家数")),
        "leader": str(row.get("领涨股票") or ""),
        "leader_change_pct": _num(row.get("领涨股票-涨跌幅")),
    }

    # 动量：涨跌幅有效样本分位 = 严格低于目标的有效样本数 ÷ (有效样本数−1) × 100
    # （并列值不计入「低于」数，故并列最高不恒为 100 分、全相等为 0 分）。
    # NaN 由 coerce/dropna 剔除，±inf 同样不是有效行情。排名/动量资格说明
    # 统一在此给出，不依赖目标涨跌幅是否有效、不依赖能否计算动量：
    # 有效样本 < 2 → 比较样本不足，不计算动量分、不提供全表排名（保守 N/A）；
    # 2 ≤ 有效样本 < 观测总量 → 无法确认缺失板块名次，不提供全表排名
    # （动量仍按有效样本计算）；全表全部有效时名次 = 严格高于目标的个数 + 1
    # （最高=1，并列最高同为 1）
    if "涨跌幅" in df.columns:
        df["涨跌幅"] = pd.to_numeric(df["涨跌幅"], errors="coerce")
        numeric = df["涨跌幅"].dropna()
        valid = numeric[numeric.map(math.isfinite)]
        target = out["row"]["change_pct"]
        if len(valid) < 2:
            out["gaps"].append(
                f"当日行情-动量与全表排名：观测 {total} 个板块中涨跌幅有效样本仅 "
                f"{len(valid)} 个（缺失/非有限行不参与比较），比较样本不足，"
                "不计算动量分、不提供全表排名"
            )
        elif len(valid) < total:
            out["gaps"].append(
                f"当日行情-全表排名：观测 {total} 个板块中涨跌幅有效样本仅 "
                f"{len(valid)} 个（缺失/非有限行不参与名次比较），"
                "无法确认缺失板块名次，不提供全表排名"
            )
        if target is not None and len(valid) > 1:
            below = int((valid < target).sum())
            if len(valid) == total:
                out["rank_of"] = 1 + int((valid > target).sum())
            out["momentum"] = round(below / (len(valid) - 1) * 100, 1)

    # 换手活跃度：换手率 / 全表中位
    if "换手率" in df.columns:
        med = _median([_num(v) for v in df["换手率"].tolist()])
        t = out["row"]["turnover_rate"]
        if med and t is not None and med > 0:
            out["activity"] = round(min(100.0, max(0.0, t / med * 50.0)), 1)
    else:
        out["gaps"].append("当日行情-换手活跃度：板块表缺换手率列")

    # 成交额活跃度：成交额 / 全表中位（相对水平，非资金净流入；
    # 当日全表缺成交额列时如实记缺口——历史日线成交额不能冒充全表分位）
    if "成交额" in df.columns:
        med = _median([_num(v) for v in df["成交额"].tolist()])
        a = out["row"]["amount"]
        if med and a is not None and med > 0:
            out["amount_activity"] = round(min(100.0, max(0.0, a / med * 50.0)), 1)
    else:
        out["gaps"].append("当日行情-成交额活跃度：当日全表缺成交额列（非净流入口径）")

    # 广度：上涨家数占比
    if "上涨家数" in df.columns and "下跌家数" in df.columns:
        up, down = out["row"]["up_count"], out["row"]["down_count"]
        if up is not None and down is not None and (up + down) > 0:
            out["breadth"] = round(up / (up + down) * 100, 1)
    else:
        out["gaps"].append("当日行情-广度：板块表缺上涨/下跌家数列")

    for dim, label in (("momentum", "动量"), ("activity", "换手活跃度"),
                       ("amount_activity", "成交额活跃度"), ("breadth", "广度")):
        if out[dim] is None and not any(label in g for g in out["gaps"]):
            out["gaps"].append(f"当日行情-{label}：板块表缺列或数据无效")
    return out


def _num(v: Any) -> Optional[float]:
    """数值适配入口：非有限数（NaN/±inf，含字符串形式）不是有效行情，归一为缺失。"""
    try:
        if v is None or v == "" or v == "-":
            return None
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


# ---------------------------------------------------------------------------
# P4b 近期板块趋势与同业比较（issue #82）：
# - 趋势：严格日历最近 20 个已完成交易日；板块日线（东财 BK 直查）与沪深300
#   指数日线各自对齐同一期望窗口，20/20 合格才各自有区间收益（19 间隔复合，
#   Decimal 计算）；相对差（百分点）仅两源对同一窗口均合格时输出。
# - 同业：复用身份阶段已验证的同一成分快照，按成交额前五 + 目标股去重选
#   「成交活跃代表」（非市值/基本面龙头）；目标位置按快照当日涨跌幅。
# 口径纪律（issue #78）：不用「20日收益」冒充 19 间隔；不补自然日、不取交集
# 凑 20；快照当日口径与 20 日趋势窗口是不同时间口径，正文分开标注。
# ---------------------------------------------------------------------------

_DEC_ONE = Decimal("1")
_DEC_HUNDRED = Decimal("100")


def _decimal_close(v: Any) -> Optional[Decimal]:
    """收盘价适配：可解析为有限正 Decimal 才有效（NaN/±inf/0/负/字符串非数 → None）。"""
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return None
    try:
        d = Decimal(str(v).strip())
    except (ArithmeticError, ValueError):
        return None
    if not d.is_finite() or d <= 0:
        return None
    return d


@require(lambda closes: len(closes) >= 2 and all(
    isinstance(c, Decimal) and c.is_finite() and c > 0 for c in closes),
    "closes must be >=2 finite positive Decimals")
@ensure(lambda result: isinstance(result, Decimal) and result.is_finite(),
        "period return must be a finite Decimal")
def _period_return_pct(closes: List[Decimal]) -> Decimal:
    """区间收益（百分点）：首尾收盘复合，19 个间隔不冒充「20日收益」。"""
    return (closes[-1] / closes[0] - _DEC_ONE) * _DEC_HUNDRED


def _qualify_daily_closes(df: Optional[pd.DataFrame], date_col: str,
                          close_col: str, expected: List[Any],
                          source_label: str,
                          ) -> Tuple[Dict[Any, Decimal], bool, List[str]]:
    """日线按期望交易日集对齐取收盘：返回（合格观测映射, 是否 20/20 合格, 缺口）。

    观测映射是**唯一资格真源**（display/收益共用，不重复判定）：键=期望窗口
    内日期、值=有限正 Decimal 收盘；部分合格时保留已取得部分（无发明值）。
    ``qualified`` 仅当每个期望日期恰一行且收盘有效（len==20；重复冲突日不进
    映射因此自动不合格）。窗口外行忽略（多给的旧日不能凑 20）；日期解析失败
    的行忽略并注明；重复日期冲突该日不取其一（整源不给区间收益）、其余无冲突
    日期的合格观测保留展示；无效收盘日期留缺口不进观测。
    供应商畸形列形状（重复列名等）与日期列整体解析失败是**源错误**：按缺口
    降块返回，不逃出边界误报为用户请求错误。重复日、收盘无效日、真缺失日三
    类缺陷独立收集、独立有界输出（不因重复日短路其他缺口）；日期一律
    isoformat 紧凑展示 + 上限 5 个 + 总数，单项长度可守 schema 上限。
    """
    gaps: List[str] = []
    if not isinstance(df, pd.DataFrame) or df.empty:
        return {}, False, [f"{source_label}：返回为空或形状异常"]
    if date_col not in df.columns or close_col not in df.columns:
        return {}, False, [f"{source_label}：缺日期列或收盘列"]
    date_series = df[date_col]
    close_series = df[close_col]
    # 重复列名等畸形形状下 df[col] 是 DataFrame 而非 Series：源错误按缺口降块
    if not isinstance(date_series, pd.Series) or not isinstance(close_series, pd.Series):
        return {}, False, [
            f"{source_label}：日期/收盘列形状异常（重复列名等，源错误），本源本窗口不采用"
        ]
    expected_set = set(expected)
    # format="mixed"：供应商日期列混合 str/Timestamp 且可能含不可解析行，
    # 逐元素解析 + coerce（解析失败 NaT 计入缺口），行为与默认一致但免格式推断警告；
    # 整列不可解析等供应商格式异常按源错误降块，不逃出边界（不误报请求 400）
    try:
        parsed = pd.to_datetime(date_series, errors="coerce", format="mixed")
    except (ValueError, TypeError) as exc:
        return {}, False, [
            f"{source_label}：日期列整体解析失败（{str(exc)[:60]}，源错误），本源本窗口不采用"
        ]
    unparseable = int(parsed.isna().sum())
    if unparseable:
        gaps.append(f"{source_label}：{unparseable} 行日期无法解析，已忽略")
    dup_dates: set = set()
    seen: set = set()
    by_date: Dict[Any, Decimal] = {}
    invalid_close_dates: List[Any] = []
    for d_raw, c_raw in zip(parsed, close_series):
        if pd.isna(d_raw):
            continue
        d = d_raw.date() if hasattr(d_raw, "date") else d_raw
        if d not in expected_set:
            continue  # 窗口外行（含多给旧日）不参与，也不缺口——缺天由期望集对齐判定
        if d in seen:
            dup_dates.add(d)
            by_date.pop(d, None)  # 冲突日两个值都不取其一，其余合格观测保留展示
            continue
        seen.add(d)
        close = _decimal_close(c_raw)
        if close is None:
            invalid_close_dates.append(d)
            continue
        by_date[d] = close
    if dup_dates:
        shown = ", ".join(d.isoformat() for d in sorted(dup_dates)[:5])
        more = f" 等 {len(dup_dates)} 天" if len(dup_dates) > 5 else ""
        gaps.append(
            f"{source_label}：期望窗口内 {len(dup_dates)} 天重复日期冲突"
            f"（{shown}{more}；冲突不静默取其一），数据不合格，区间收益不计算"
        )
    if invalid_close_dates:
        shown = ", ".join(d.isoformat() for d in sorted(invalid_close_dates)[:5])
        more = f" 等 {len(invalid_close_dates)} 天" if len(invalid_close_dates) > 5 else ""
        gaps.append(
            f"{source_label}：{len(invalid_close_dates)} 天收盘无效或非正"
            f"（{shown}{more}），对应日期无合格观测"
        )
    # 真缺失=期望窗口内既无合格观测、也未被重复/无效缺口单独报告的日期（不重复计数）
    reported = set(dup_dates) | set(invalid_close_dates)
    missing = [d for d in expected if d not in by_date and d not in reported]
    if missing:
        shown = ", ".join(d.isoformat() for d in missing[:5])
        more = f" 等 {len(missing)} 天" if len(missing) > 5 else ""
        got = len(by_date)
        gaps.append(
            f"{source_label}：期望 {len(expected)} 个已完成交易日中取得 {got} 天合格收盘，"
            f"缺失 {len(missing)} 天（{shown}{more}），"
            "不取交集/旧日凑数，区间收益不计算"
        )
    return by_date, len(by_date) == len(expected) and not dup_dates, gaps


def _observation_rows(obs: Dict[Any, Decimal]) -> List[Dict[str, Any]]:
    """合格观测映射 → 升序边界序列（date ISO str + float close；同一映射，无发明值）。"""
    return [
        {"date": d.isoformat(), "close": float(obs[d])}
        for d in sorted(obs.keys())
        if hasattr(d, "isoformat")
    ]


@ensure(
    lambda result: (
        result.get("relative_return_pp") is None
        or (
            result.get("period_return_pct") is not None
            and isinstance(result.get("benchmark"), dict)
            and result["benchmark"].get("period_return_pct") is not None
        )
    ),
    "relative_return_pp requires both board and benchmark qualified period returns",
)
def _sector_trend_pillar(board_code: str, board_name: str, *, budget: ReportBudget,
                         subject_confirmed: bool = False,
                         current_time: Optional[datetime] = None) -> Dict[str, Any]:
    """板块近期趋势（issue #82）：confirmed 资格在前，板块与基准各自有界。

    资格：主行业 confirmed 才发趋势/基准出站（不变量1；unverified 时板块代码
    虽已锁定但归属未确认，同样不查询）；否则 eligible=False 仅记录未执行原因。
    严格日历不可用 → 明确缺口且不出站（不自然日 fallback）。
    板块与基准各自 20/20 合格才各有区间收益；**实际合格观测（observations）与
    覆盖数（qualified_sessions）独立于区间收益资格保留**：部分合格时保留已取
    得部分（无发明值），板块侧与基准侧独立展示（合格基准不因板块残缺而隐藏）。
    供应商失败/超时/取数预算耗尽只降对应块（已取得内容保留），不向上传播
    异常——总截止收尾由提交边界把关；返回形状异常（如 dict）按源错误记缺口，
    不逃出边界。
    """
    out: Dict[str, Any] = {
        "eligible": False,
        "window_completed_sessions": 20,
        "interval_count": 19,
        "start_date": None, "end_date": None,
        "expected_dates": [],
        "qualified_sessions": None,
        "observations": [],
        "period_return_pct": None,
        "benchmark": None,
        "relative_return_pp": None,
        "source": None,
        "observed_at": None,
        "gaps": [],
    }
    observed_at = datetime.now().isoformat(timespec="seconds")
    out["observed_at"] = observed_at
    if not subject_confirmed or not board_code:
        out["gaps"].append("板块趋势：主行业身份未确认（confirmed 前置不满足），不执行板块趋势查询")
        return out
    out["eligible"] = True
    sessions = recent_completed_trading_sessions("cn", 20, current_time=current_time)
    if not sessions:
        out["gaps"].append(
            "板块趋势：严格交易日历不可用，无法确定最近 20 个已完成交易日，"
            "本次不查询板块/基准日线（不按自然日补齐）"
        )
        return out
    expected = sorted(sessions)  # 升序（newest-first 输入 → 升序窗口）
    out["expected_dates"] = [d.isoformat() for d in expected]
    out["start_date"] = expected[0].isoformat()
    out["end_date"] = expected[-1].isoformat()
    start_yyyymmdd = expected[0].strftime("%Y%m%d")
    end_yyyymmdd = expected[-1].strftime("%Y%m%d")

    # 板块日线（东财 BK 代码直查，避免内部按名称再映射）
    import akshare as ak

    board_obs: Dict[Any, Decimal] = {}
    board_qualified = False
    try:
        board_df = _bounded_ak(
            ak.stock_board_industry_hist_em, symbol=board_code,
            start_date=start_yyyymmdd, end_date=end_yyyymmdd,
            period="日k", adjust="", budget=budget,
        )
        if not isinstance(board_df, pd.DataFrame):
            # 错误形状（如 dict/str/None）按供应商源错误降块，不逃出边界成 500
            shape = type(board_df).__name__
            board_df = None
            out["gaps"].append(
                f"板块趋势：板块日线返回形状异常（{shape}，源错误），本次不采用"
            )
    except BudgetExhausted:
        out["gaps"].append("板块趋势：取数预算不足，板块日线未取得（已取得内容保留）")
        board_df = None
    except Exception as exc:  # noqa: BLE001 - 供应商失败降块（含 TimeoutError）
        out["gaps"].append(
            f"板块趋势：板块日线查询失败: {str(exc)[:60]}（供应商局部失败，只降趋势块）"
        )
        board_df = None
    if isinstance(board_df, pd.DataFrame):
        board_obs, board_qualified, board_gaps = _qualify_daily_closes(
            board_df, "日期", "收盘", expected,
            f"板块日线（{board_name} {board_code}）",
        )
        out["gaps"].extend(board_gaps)
    out["observations"] = _observation_rows(board_obs)
    out["qualified_sessions"] = len(board_obs)
    if board_qualified:
        out["source"] = "东财行业板块日线"
        board_closes = [board_obs[d] for d in expected]
        ret = _period_return_pct(board_closes)
        out["period_return_pct"] = float(round(ret, 4))

    # 基准日线（沪深300，同一期望窗口；失败/不合格单独降基准块）。
    # 东财指数日线不可用时切换腾讯同一指数（sh000300）日线：同一指数标的、
    # 同一收盘点位口径（指数无复权，切换经生产/本地双源逐日比对验证），
    # 实际来源在基准块 source 如实标注；东财失败缺口保留（不因 fallback 成功清空）。
    bench_gaps: List[str] = []
    bench_source = "东财指数日线"
    bench_note: Optional[str] = None
    try:
        bench_df = _bounded_ak(
            ak.stock_zh_index_daily_em, symbol="sh000300",
            start_date=start_yyyymmdd, end_date=end_yyyymmdd, budget=budget,
        )
        if not isinstance(bench_df, pd.DataFrame):
            shape = type(bench_df).__name__
            bench_df = None
            bench_gaps = [f"基准日线（沪深300）：返回形状异常（{shape}，源错误），本次不采用"]
            bench_note = f"基准（沪深300）东财日线返回形状异常（{shape}），尝试腾讯同一指数"
    except BudgetExhausted:
        out["gaps"].append("板块趋势：取数预算不足，基准日线未取得（板块已取得部分保留）")
        bench_df = None
        bench_gaps = ["基准日线未取得：取数预算不足"]
    except Exception as exc:  # noqa: BLE001
        bench_df = None
        bench_gaps = [f"基准日线查询失败: {str(exc)[:60]}"]
        bench_note = f"基准（沪深300）东财日线查询失败: {str(exc)[:60]}，尝试腾讯同一指数"
    if not isinstance(bench_df, pd.DataFrame) or bench_df.empty:
        try:
            bench_df = _bounded_ak(
                ak.stock_zh_index_daily_tx, symbol="sh000300",
                start_date=start_yyyymmdd, end_date=end_yyyymmdd, budget=budget,
            )
            if isinstance(bench_df, pd.DataFrame) and not bench_df.empty:
                bench_source = "腾讯指数日线"
                bench_note = None
                out["gaps"].append(
                    "板块趋势：基准（沪深300）切换腾讯同一指数日线（东财源不可用，"
                    "同一指数收盘点位口径，来源已在基准块标注）"
                )
        except BudgetExhausted:
            bench_df = None
            bench_gaps = bench_gaps or ["基准日线未取得：取数预算不足"]
        except Exception as exc:  # noqa: BLE001
            bench_df = None
            bench_gaps = bench_gaps + [f"腾讯指数日线查询失败: {str(exc)[:60]}"]
    if bench_note:
        out["gaps"].append(f"板块趋势：{bench_note}（只降基准块）")
    bench_obs: Dict[Any, Decimal] = {}
    bench_qualified = False
    # 东财源失败/形状异常缺口在 fallback 资格化前快照保留——资格化 gaps 只
    # 描述最终采用源，东财源错误不因腾讯 fallback 成功被覆盖清空
    em_bench_gaps = list(bench_gaps)
    if isinstance(bench_df, pd.DataFrame):
        bench_obs, bench_qualified, bench_gaps = _qualify_daily_closes(
            bench_df, "date", "close", expected, "基准日线（沪深300）"
        )
    bench_block: Dict[str, Any] = {
        "name": "沪深300", "symbol": "sh000300", "source": bench_source,
        "start_date": out["start_date"], "end_date": out["end_date"],
        "qualified_sessions": len(bench_obs),
        "observations": _observation_rows(bench_obs),
        "period_return_pct": None,
        "gaps": em_bench_gaps + bench_gaps,
    }
    if bench_qualified:
        bench_closes = [bench_obs[d] for d in expected]
        bench_block["period_return_pct"] = float(round(_period_return_pct(bench_closes), 4))
    else:
        bench_block["gaps"] = bench_block["gaps"] or ["基准日线未取得或不合格"]
    out["benchmark"] = bench_block
    if not bench_qualified:
        out["gaps"].extend(bench_gaps or ["板块趋势：基准日线未取得或不合格，相对表现不计算"])
    # 相对表现：两源对同一期望窗口均 20/20 合格才计算；单位=百分点（区间收益差）
    if board_qualified and bench_qualified:
        board_closes = [board_obs[d] for d in expected]
        bench_closes = [bench_obs[d] for d in expected]
        relative = _period_return_pct(board_closes) - _period_return_pct(bench_closes)
        out["relative_return_pp"] = float(round(relative, 4))
    # 缺口有界化（单项长度 + 总条数 ≤ schema 上限）：复合源失败不炸最终 strict 校验
    out["gaps"] = _bound_gaps(out["gaps"], 10)
    return out


def _decimal_amount(v: Any) -> Tuple[Optional[Decimal], str]:
    """成交额适配：原始值直接解析 Decimal（金融排序不经 float 中转）。

    返回 (Decimal 或 None, 原因)：``ok``=有限**正**（活跃代表排名资格）；
    ``zero``=**已知零**（合法零额观测——公开载荷/显示保留 0，不参与活跃
    前五，全体报价已知时目标 0 可判「不在前五」）；``missing``=缺失/空/
    NaN；``invalid``=负值/无法解析/±inf（源边界无效，保持 None=未知语义）。
    分类计数用于过滤前缺口（零额与无效分开披露，不因剩余足够而吞事实）。
    """
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return None, "missing"
    s = str(v).strip()
    if s in ("", "-", "nan", "NaN", "None"):
        return None, "missing"
    try:
        d = Decimal(s)
    except (ArithmeticError, ValueError):
        return None, "invalid"
    if not d.is_finite():
        return None, "invalid"
    if d < 0:
        return None, "invalid"
    if d == 0:
        return Decimal("0"), "zero"
    return d, "ok"


def _adapt_peer_name(raw: Any, code: str) -> Tuple[str, bool]:
    """供应商名称边界适配：空白/缺失/nan → 用代码显示（成员资格保留）。

    返回 (显示名, 是否适配)。适配行计缺口（供应商字段问题按源错误处理，
    不让整篇有效报告 500，也不静默）。
    """
    s = str(raw).strip() if raw is not None else ""
    if not s or s in ("nan", "None"):
        return code, True
    return s[:64], False


_GAP_LEN_CAP = 200  # schema _GapList 单项上限：复合源失败下缺口逐条有界，不炸 strict 校验


def _bound_gaps(gaps: List[str], cap: int) -> List[str]:
    """缺口有界化：单项长度与总条数双重钳制（诊断事实保留，超限截断加省略号）。"""
    clamped = [
        g if len(g) <= _GAP_LEN_CAP else g[: _GAP_LEN_CAP - 1] + "…"
        for g in gaps
    ]
    if len(clamped) <= cap:
        return clamped
    omitted = len(clamped) - (cap - 1)
    return clamped[: cap - 1] + [f"（另有 {omitted} 条缺口略，均为同类来源限制）"]


@dataclass(frozen=True)
class MemberQuoteBatch:
    """成员行情逐股采集批次（F10 成员集合无 clist 成交额列时填充）。

    ``amounts_known``：全体成员均有**已知**成交额（有报价且 f48 非 None）——
    这是全体 top5 资格可判定的唯一依据；任一成员缺报价/缺额/币种未证实
    （B 股成员保留但金额未知）→ False，``target_in_amount_top5`` 必须为
    None（缺报价成员资格未知，零与 None 分开）。
    """

    df: pd.DataFrame
    gaps: List[str]
    ts_min: Optional[float] = None
    ts_max: Optional[float] = None
    stockget_count: int = 0
    trends2_count: int = 0
    tencent_count: int = 0
    tencent_exact_count: int = 0
    tencent_wan_count: int = 0
    failed_count: int = 0
    amounts_known: bool = False
    trends2_as_of: Optional[str] = None
    tencent_as_of: Optional[str] = None
    currency_unknown_count: int = 0


# ---------------------------------------------------------------------------
# 腾讯公开行情批量补采（F10 成员行情专用；共享 akshare_fetcher 的既有
# 端点/symbol/金额字段约定原样复用，不改共享模块/路由/熔断/默认）。
# 生产实证（2026-11-07 04:37+08 生产出口，root 只读控制）：BK1277 精确 20
# 符号 200/20 行 0.11s、BK0739 精确 35 符号（含 7 个 bj920）200/35 行 0.054s
# ——分块上限按实测 35 收敛，不凭猜测扩大。
# ---------------------------------------------------------------------------

_TENCENT_REALTIME_QUOTE_URL = "http://qt.gtimg.cn/q"
_TENCENT_BATCH_CHUNK = 35  # 生产实测单批上限（BK0739 35 符号 200 全行）
_TENCENT_MEMBER_FIELDS_MIN = 38  # 覆盖 f30 时点 / f32 涨跌幅 / f35 金额 / f37 兜底
_TENCENT_PROVIDER_TIME_FMT = "%Y%m%d%H%M%S"
_TENCENT_AMOUNT_WAN = Decimal("10000")
_TENCENT_SYMBOL_RE = re.compile(r'v_([a-z]{2}\d{6})="([^"]*)"')
_UNCERTAIN_CURRENCY_PREFIXES = ("200", "900")  # 深/沪 B 股：币种可比性未证实


def _is_uncertain_currency_code(code: str) -> bool:
    """B 股成员（深 200xxx/沪 900xxx）：**币种可比性未证实**。

    成员资格保留、金额未知——不并入 CNY 活跃排名、不据此判定全体完整，
    也不让后续逐股 fallback 重引入未证币种金额（生产实证 BK1277 含
    200596.SZ 古井贡Ｂ）。
    """
    return code.startswith(_UNCERTAIN_CURRENCY_PREFIXES)


def _parse_tencent_amount_decimal(
    fields: List[str],
) -> Tuple[Optional[Decimal], bool, Optional[str]]:
    """腾讯行金额解析归一边界（Decimal 算术 + 外投射统一错误边界）。

    返回 (金额, 是否已知, 来源)：``exact35``=字段35 三元组第三段**精确
    金额（元）**优先；``rounded37``=字段37 万元口 ×10000（仅精确金额
    **缺值**时的兜底口径，Decimal 换算）。仅捕**供应商数值解析/Decimal
    算术/换算**错误（InvalidOperation/Overflow/Underflow 类）与外投射
    有限性失败——非零金额不可向外表示（含正下溢为 0）= 金额未知；
    **真实来源 0 保留为已知 0**；字段 35 存在但畸形/负值属真实坏值——
    不回退字段37、不补 0 冒充。无关 TypeError/AssertionError 原样传播，
    不做宽程序捕获。
    """
    exact = fields[35] if len(fields) > 35 else ""
    if exact:
        parts = exact.split("/")
        if len(parts) >= 3 and parts[2]:
            try:
                value = Decimal(parts[2])
                if value.is_finite() and value >= 0:
                    outward = float(value)
                    # 外投射三查：有限、非零 Decimal 不下溢为 0.0
                    if (math.isfinite(outward)
                            and not (outward == 0.0 and value > 0)):
                        return value, True, "exact35"
                # 非负但不可外投射（溢出/正下溢为 0）= 金额未知
                return None, False, None
            except (ArithmeticError, ValueError):
                return None, False, None
            # 不可达：仅算术/值错误归本边界
        return None, False, None  # 存在但非三元组：真实畸形，不回退兜底
    fallback_wan = fields[37] if len(fields) > 37 else ""
    if fallback_wan:
        # 原始 wan 先资格限定（finite/非负/零与正分开），**再**算术：
        # Decimal 乘法对极小 ±wan 会算术下溢为 ±0E-x（如 ±1e-1000100
        # ×10000 → ±0E-1000026）、对极大 wan 会溢出——仅捕供应商数值
        # 类（ArithmeticError/ValueError），金额未知；真实 raw 0 保留为
        # 已知 0；非零 wan 换算后必须仍 > 0 且可外表示为非零有限 float
        try:
            wan = Decimal(fallback_wan)
        except (ArithmeticError, ValueError):
            return None, False, None
        if not wan.is_finite():
            return None, False, None
        if wan < 0:
            return None, False, None  # 负值：真实坏值（含负下溢 ±0E-x 前拒绝）
        if wan == 0:
            return Decimal("0"), True, "rounded37"  # 真实 raw 0 = 已知 0
        try:
            converted = wan * _TENCENT_AMOUNT_WAN
        except (ArithmeticError, ValueError):
            return None, False, None
        if converted.is_finite() and converted > 0:
            outward = float(converted)
            if math.isfinite(outward) and outward > 0:
                return converted, True, "rounded37"
        # 溢出不可表示 / 算术下溢为 0 = 金额未知
        return None, False, None
    return None, False, None  # 两者皆缺值：金额未知（缺失语义）


def _member_display_name_equal(expected: str, actual: str) -> bool:
    """成员名称跨源 display 等价：剥除全部空白后完全相同且非空即同名。

    覆盖供应商固定宽度显示 padding（如「柳    工」）；不做模糊/近似、
    ST/*ST 剥离、大小写与全半角 Unicode 归一、前缀别名，剥后为空即
    不等。代码/符号同键精确校验不受本函数影响。
    """
    stripped_expected = "".join(expected.split())
    return (bool(stripped_expected)
            and stripped_expected == "".join(actual.split()))


def _parse_tencent_member_body(
    text: str, chunk_allowlist: Dict[str, Tuple[str, str]],
) -> Tuple[List["TencentMemberRow"], List[str], List[str]]:
    """腾讯 GBK 批量响应体解析（严格 typed wire 边界）。

    ``chunk_allowlist`` 是**本分块实际请求的符号集**（非全量成员表）：
    未请求符号不得暗示本块覆盖。行归一经 :class:`TencentMemberRow`
    strict/frozen 模型（消费字段边界 + 可表示性：如 f32=``1e999`` Decimal
    有限但 float 溢出 → 源错误行不采用）；重复 symbol 同键交给上层按
    code 冲突组统一失效（本层不采信首行）。供应商供应时点/金额来源
    （exact35/rounded37）随记录携带。名称比较经
    :func:`_member_display_name_equal`（空白 padding 等价；原始名不同的
    等价接受如实披露，行名称保留请求规范名）。程序异常原样传播。
    """
    from src.schemas.sector_vendor import TencentMemberRow

    rows: List[TencentMemberRow] = []
    anomalies: List[str] = []
    unsolicited_symbols: List[str] = []
    raw_symbol_amounts: Dict[str, List[str]] = {}
    raw_symbol_counts: Dict[str, int] = {}
    raw_duplicated: set = set()
    for match in _TENCENT_SYMBOL_RE.finditer(text or ""):
        symbol, body = match.group(1), match.group(2)
        fields = body.split("~")
        expected = chunk_allowlist.get(symbol)
        if expected is None:
            unsolicited_symbols.append(symbol)
            continue
        # raw 重复计数在资格过滤**之前**（第二行即便资格不过也不能证明
        # 首行无歧义——冲突组由上层统一失效）
        if symbol in raw_symbol_counts:
            raw_duplicated.add(symbol)
        raw_symbol_counts[symbol] = raw_symbol_counts.get(symbol, 0) + 1
        raw_symbol_amounts.setdefault(symbol, [])
        try:
            raw_symbol_amounts[symbol].append(
                fields[35].split("/")[-1] if fields[35] else f"wan:{fields[37]}")
        except (IndexError, AttributeError):
            pass
        code, name = expected
        if len(fields) < _TENCENT_MEMBER_FIELDS_MIN:
            anomalies.append(
                f"{symbol} 字段数 {len(fields)} 不足，行不采用")
            continue
        if fields[2] != code:
            anomalies.append(
                f"{symbol} 代码不符：请求 {code} 实际 {fields[2]}，行不采用")
            continue
        if not _member_display_name_equal(name, fields[1]):
            anomalies.append(
                f"{symbol} 名称不符：请求「{name}」实际「{fields[1]}」，行不采用")
            continue
        if fields[1] != name:
            # 空白 padding 等价接受：行名称保留 F10 规范名，原始供应商名
            # 如实披露（不静默归一，保留跨源 raw 来源可见性）
            anomalies.append(
                f"{symbol} 名称空白等价接受：请求「{name}」实际「{fields[1]}」"
                "（显示 padding，非不同证券；行名称保留请求规范名）")
        try:
            provider_time = datetime.strptime(
                fields[30], _TENCENT_PROVIDER_TIME_FMT)
        except ValueError:
            anomalies.append(
                f"{symbol} 供应商时点畸形（{fields[30][:16]}），行不采用")
            continue
        change_raw = fields[32] if len(fields) > 32 else ""
        change: Optional[float] = None
        if change_raw:
            try:
                change_value = Decimal(change_raw)
            except (ArithmeticError, ValueError):
                change_value = None
            if (change_value is not None and change_value.is_finite()
                    and -float("inf") < float(change_value) < float("inf")):
                change = float(change_value)
            else:
                anomalies.append(
                    f"{symbol} 涨跌幅不可表示（{change_raw[:16]}），涨跌幅未知")
        amount, amount_known, amount_source = (
            _parse_tencent_amount_decimal(fields))
        try:
            rows.append(TencentMemberRow.model_validate({
                "symbol": symbol, "code": code, "name": name,
                "provider_time": provider_time, "change_pct": change,
                "amount": amount, "amount_known": amount_known,
                "amount_source": amount_source,
            }))
        except ValidationError as exc:
            anomalies.append(
                f"{symbol} 行消费字段校验失败（源错误）：{str(exc)[:80]}")
    # raw 重复符号在资格过滤**之前**记录（含各行金额披露）：同 symbol
    # 第二行（即便名称/时点/金额资格不过）不能证明首行无歧义——冲突组
    # 由上层统一失效
    for sym, n in raw_symbol_counts.items():
        if n > 1 and sym in chunk_allowlist:
            amounts = "、".join(raw_symbol_amounts.get(sym, [])[:4])
            anomalies.append(
                f"{sym} raw 重复 {n} 行（金额 {amounts}）：整组失效，"
                "不采信任一行")
    if unsolicited_symbols:
        sample = ", ".join(unsolicited_symbols[:3])
        more = f" 等{len(unsolicited_symbols)}个" if len(unsolicited_symbols) > 3 else ""
        anomalies.append(
            f"返回未请求符号 {sample}{more}（忽略，不暗示本块覆盖）")
    return rows, anomalies, sorted(raw_duplicated)


def _fetch_tencent_member_quotes(
    df: pd.DataFrame, *, budget: ReportBudget, session_ctx: SessionContext,
) -> Dict[str, Any]:
    """腾讯公开行情批量补采（精确 F10 成员集合，分块 ≤35 符号，同一预算）。

    整组契约（root tencent 复审收敛）：
    - **每块仅尝试一次**（无重试）；接收钟在本块 HTTP 完成后取得；
    - **逐块 allowlist**：每块响应按该块实际请求符号集校验——其它块的
      成员即使返回也不暗示本块覆盖；
    - **部分存活**：某块 HTTP 失败/空响应只隔离该块（该块成员记为未
      取得），已成功块的合格行保留；预算中断保留已成功行并如实记录
      未尝试成员（collect 据此不再出站、不迟到采纳）；
    - **冲突组失效**：同 code（含跨块/重复符号）多行 → 该组全部不采纳
      （不采信任一行），成员保留并走既有逐股 fallback（仅实际缺口）；
    - 行级会话时点资格与既有来源同一规则（会话日一致、不早于开盘/
      已完结时不早于收盘、不晚于接收）。
    程序异常原样传播；不伪造完整覆盖。
    """
    from data_provider.akshare_fetcher import _to_sina_tx_symbol

    requested: Dict[str, Tuple[str, str]] = {}
    order: List[str] = []
    for i in range(len(df)):
        row = df.iloc[i]
        code = str(row["代码"])
        name = str(row["名称"])
        symbol = _to_sina_tx_symbol(code)
        if symbol not in requested:
            requested[symbol] = (code, name)
            order.append(symbol)
    rows_by_code: Dict[str, List[TencentMemberRow]] = {}
    anomalies: List[str] = []
    received_max: Optional[datetime] = None
    budget_interrupted = False
    untried_symbols: List[str] = []
    failed_symbols: List[str] = []
    raw_duplicated_symbols: set[str] = set()
    for chunk_start in range(0, len(order), _TENCENT_BATCH_CHUNK):
        chunk = order[chunk_start:chunk_start + _TENCENT_BATCH_CHUNK]
        chunk_allowlist = {s: requested[s] for s in chunk}
        try:
            text = _http_get_text(_TENCENT_REALTIME_QUOTE_URL,
                                  {"q": ",".join(chunk)}, budget=budget,
                                  encoding="gbk")
        except BudgetExhausted:
            budget_interrupted = True
            untried_symbols.extend(chunk)
            break  # 截止后零出站；已成功块保留
        if text is None or not text.strip():
            failed_symbols.extend(chunk)
            anomalies.append(
                f"分块 HTTP 失败/空响应（{len(chunk)} 符号未取得，"
                "交由既有逐股 fallback 或如实未知）")
            continue  # 块失败隔离：已成功块保留，后续块照常
        received = _received_at_now()  # 本块 HTTP 完成后取接收钟
        received_max = (received if received_max is None
                        else max(received_max, received))
        rows, chunk_anomalies, raw_dup_symbols = (
            _parse_tencent_member_body(text, chunk_allowlist))
        anomalies.extend(chunk_anomalies)
        raw_duplicated_symbols.update(raw_dup_symbols)
        session_finished = received >= session_ctx.session_close
        lower = (session_ctx.session_close if session_finished
                 else session_ctx.session_open)
        for row in rows:
            provider_time = row.provider_time
            if (provider_time.date() != session_ctx.session_date
                    or not (lower <= provider_time <= received)):
                anomalies.append(
                    f"{row.symbol} 供应商时点 {provider_time.isoformat()} "
                    f"不具会话 {session_ctx.session_date.isoformat()} 当日可比资格"
                    f"（窗口 [{lower.isoformat()}, {received.isoformat()}]），行不采用")
                continue
            rows_by_code.setdefault(row.code, []).append(row)
    # 冲突组失效：同 code 多行（跨块/重复符号）或 raw 同 symbol 重复
    # （第二行即便资格不过也不能证明首行无歧义）→ 全组不采信任一行
    conflicted = {row.code for code, group in rows_by_code.items()
                  if len(group) > 1 for row in group}
    for sym in raw_duplicated_symbols:
        if sym in requested:
            conflicted.add(requested[sym][0])
    accepted: Dict[str, TencentMemberRow] = {}
    conflict_codes: List[str] = []
    for code in sorted({row.code for group in rows_by_code.values()
                        for row in group}):
        if code in conflicted:
            anomalies.append(
                f"{code} 冲突组失效（raw 重复/同键多值）：不采信任一行，"
                "成员保留、金额/涨跌幅未知")
            conflict_codes.append(code)
            continue
        group = rows_by_code[code]
        accepted[code] = group[0]
    return {"accepted": accepted, "anomalies": anomalies,
            "received_max": received_max,
            "budget_interrupted": budget_interrupted,
            "untried_symbols": untried_symbols,
            "failed_symbols": failed_symbols,
            "conflict_codes": conflict_codes}


def _f10_quote_provenance(batch: MemberQuoteBatch) -> str:
    """F10 成员集合行情来源口径（紧凑、真实来源/计数，≤240 严格 schema）。"""
    parts: List[str] = ["成交额单位为元："]
    segments: List[str] = []
    if batch.tencent_count:
        segments.append(
            f"腾讯批量{batch.tencent_count}只(字段35精确{batch.tencent_exact_count}"
            f"/字段37换算{batch.tencent_wan_count}只，非f48)")
    if batch.stockget_count:
        segments.append(f"stock/get {batch.stockget_count}只f48原样")
    if batch.trends2_count:
        segments.append(f"trends2 {batch.trends2_count}只{_DERIVED_MARKER}(非f48)")
    if not segments:
        parts.append("成员行情未取得（成交额均缺失，不产金额排名）")
    else:
        parts.append("；".join(segments))
    if batch.currency_unknown_count:
        parts.append(
            f"；B股{batch.currency_unknown_count}只币种未证金额未知(不并CNY排名)")
    text = "".join(parts)
    return text if len(text) <= 240 else text[:240]


def _quote_as_of_note(batch: MemberQuoteBatch) -> Optional[str]:
    """成员行情供应商时点说明（紧凑、≤240 严格 schema；不含值级诊断）。"""
    segs: List[str] = []
    if batch.tencent_as_of:
        segs.append(
            f"腾讯批量{batch.tencent_count}只供应时点{batch.tencent_as_of}")
    if batch.trends2_as_of:
        segs.append(
            f"trends2 {batch.trends2_count}只截至{batch.trends2_as_of}")
    if batch.ts_min is not None and batch.ts_max is not None:
        if batch.ts_min == batch.ts_max:
            segs.append(f"stock/get {batch.stockget_count}只@{int(batch.ts_min)}")
        else:
            segs.append(
                f"stock/get {batch.stockget_count}只@{int(batch.ts_min)}~"
                f"{int(batch.ts_max)}(逐股)")
    text = "；".join(segs)
    return text[:240] or None


def _collect_member_quotes(df: pd.DataFrame, *, budget: ReportBudget,
                           now: Optional[datetime] = None,
                           session_ctx: Optional[SessionContext] = None,
                           received_at: Optional[datetime] = None,
                           ) -> MemberQuoteBatch:
    """成员逐股行情快照（同一取数预算，顺序有界；不缩分母语义）。

    **形状契约**：按原始行位置物化全部已成功行（腾讯预填/逐股补采），
    预算终态**单一所有权**——耗尽后一次性把全部剩余 pending/未获取位置
    落为未知并只发一份终局汇总（不重入逐股 fallback、零出站、不发明
    dispatch），df 行数恒等于成员行数；已采成功/采集失败/预算未获取三类
    计数来自最终事实并全部进缺口。会话上下文单次锚定，两路来源共用同一
    会话时点资格；资格被拒/失败的行成员资格保留、金额 None（不冒充全体
    成交额分布）。程序异常原样传播。
    """
    ctx = session_ctx if session_ctx is not None else _session_context(now)
    gaps: List[str] = []
    ts_min: Optional[float] = None
    ts_max: Optional[float] = None
    trends2_as_ofs: List[str] = []
    tencent_times: List[datetime] = []
    fetched = 0
    total = len(df)
    stockget_count = 0
    trends2_count = 0
    tencent_count = 0
    tencent_exact_count = 0
    tencent_wan_count = 0
    failed_count = 0
    currency_unknown_count = 0
    interrupted = False
    # 腾讯公开行情批量预填（F10 成员集合路径；clist 快照路径不带成交额列
    # 不会进入本函数）：在对每个成员连续重试两个不可用东财端点之前，先对
    # 精确成员集合做一次有界批量补采（同一预算；分块 ≤ 实测 35 符号；接收
    # 钟每分块 HTTP 完成后取得；行级会话时点资格与既有来源同一规则）。
    # 腾讯公开行情批量预填（F10 成员集合路径；clist 快照路径带成交额列，
    # 不进入本函数）：逐股重试不可用东财端点之前先做有界批量补采。整组
    # 契约：每块一次、逐块 allowlist、部分存活（块失败隔离/预算中断保留
    # 已成功）、raw 重复/同键冲突组失效、来源（exact35/rounded37）如实贯穿、
    # **预算终态单一所有权**（见下 fallback 终局汇总）。
    tencent_prefilled: Dict[int, TencentMemberRow] = {}  # 按 df 行位置预定位
    tencent_anomalies: List[str] = []
    budget_unacquired: set = set()  # 预算终态未出站/未取得成员（位置集）
    batch_budget_interrupted = False
    if ctx is not None and "成交额" not in df.columns:
        try:
            tencent = _fetch_tencent_member_quotes(df, budget=budget,
                                                   session_ctx=ctx)
        except BudgetExhausted:
            # 整批阶段即耗尽：全成员未出站（如实未知，不发明 dispatch）
            batch_budget_interrupted = True
            tencent = {"accepted": {}, "anomalies": [],
                       "budget_interrupted": True,
                       "untried_symbols": [], "failed_symbols": [],
                       "conflict_codes": []}
        if tencent is None:
            tencent = {"accepted": {}, "anomalies": [
                "腾讯批量源不可用（网络/HTTP/空响应），走既有逐股补采"],
                "budget_interrupted": False,
                "untried_symbols": [], "failed_symbols": [],
                "conflict_codes": []}
        tencent_anomalies = list(tencent["anomalies"])
        from data_provider.akshare_fetcher import _to_sina_tx_symbol as _tx
        symbol_to_pos: Dict[str, int] = {}
        for i in range(total):
            symbol_to_pos[_tx(str(df.iloc[i]["代码"]))] = i
        if tencent["budget_interrupted"]:
            batch_budget_interrupted = True
            for sym in tencent["untried_symbols"]:
                if sym in symbol_to_pos:
                    budget_unacquired.add(symbol_to_pos[sym])
        # failed_symbols（块失败，预算未耗尽）：不进 budget_unacquired——
        # 这些成员走既有逐股 fallback 在同一剩余预算内补实际缺口
        for code, tencent_row in tencent["accepted"].items():
            # 仅采纳金额已知的合格**非 B 股**行；金额畸形/缺失/冲突行留给
            # 既有逐股 fallback（仅实际缺口，预算内）
            if (tencent_row.amount_known
                    and not _is_uncertain_currency_code(code)):
                pos = next(i for i in range(total)
                           if str(df.iloc[i]["代码"]) == code)
                tencent_prefilled[pos] = tencent_row
        if tencent_anomalies:
            gaps.extend(_bound_gaps(
                [f"腾讯批量：{a}" for a in tencent_anomalies], 6))
    # 预填按**原始行位置**预定位：任何位置的已成功行不因更早缺口的
    # fallback 预算中断而被抹掉（root 反例：反向顺序 36th 先成功后被抹）
    final_amount: List[Optional[float]] = [None] * total
    final_change: List[Optional[float]] = [None] * total
    pos_state: List[str] = ["pending"] * total  # prefilled/b/unacquired/fallback
    for i in range(total):
        code = str(df.iloc[i]["代码"])
        if i in tencent_prefilled:
            pos_state[i] = "prefilled"
            tencent_count += 1
            tencent_row = tencent_prefilled[i]
            final_amount[i] = float(tencent_row.amount)
            final_change[i] = tencent_row.change_pct
            if tencent_row.amount_source == "exact35":
                tencent_exact_count += 1
            elif tencent_row.amount_source == "rounded37":
                tencent_wan_count += 1
            tencent_times.append(tencent_row.provider_time)
        elif _is_uncertain_currency_code(code):
            pos_state[i] = "b"
            currency_unknown_count += 1
            if currency_unknown_count == 1:
                gaps.append(
                    f"同业行情：B 股成员（{code} {str(df.iloc[i]['名称'])}）"
                    "币种可比性未证实，金额未知保留（成员资格保留，不并入 "
                    "CNY 活跃排名，不另行逐股补采）")
        elif i in budget_unacquired:
            pos_state[i] = "unacquired"
        elif batch_budget_interrupted:
            # 批量预算终态：全部剩余 pending 位置一并记为预算未获取，
            # **整体跳过逐股 fallback**（契约：已知批量中断后零重入/
            # 零出站）——成功位已在上方按原始位置物化，不受影响
            pos_state[i] = "unacquired"
    fallback_budget_broken = False
    if not batch_budget_interrupted:
        for i in range(total):
            if pos_state[i] != "pending":
                continue
            row = df.iloc[i]
            member_code = str(row["代码"])
            member_name = str(row["名称"])
            try:
                snap = _fetch_member_quote(member_code, member_name,
                                           budget=budget, now=now,
                                           session_ctx=ctx,
                                           received_at=received_at)
            except BudgetExhausted:
                # 逐股阶段预算终态：剩余 pending 一并预算未获取，不再重入
                fallback_budget_broken = True
                break
            pos_state[i] = "attempted"
            if snap is None:
                failed_count += 1
                gaps.append(
                    f"同业行情：{member_code}（{member_name}）快照失败或同键/会话"
                    "资格不符，金额/涨跌幅缺失（成员资格保留）")
                continue
            fetched += 1
            quote = snap.quote
            final_amount[i] = quote.f48
            final_change[i] = quote.f170
            if quote.f48 is None:
                gaps.append(
                    f"同业行情：{member_code}（{member_name}）成交额缺值，资格未知"
                    "（零与缺失分开，不补 0）")
            if snap.source == "stock/get":
                stockget_count += 1
                if quote.f86 is not None:
                    ts_min = (quote.f86 if ts_min is None
                              else min(ts_min, quote.f86))
                    ts_max = (quote.f86 if ts_max is None
                              else max(ts_max, quote.f86))
            else:
                trends2_count += 1
                if snap.vendor_as_of:
                    trends2_as_ofs.append(snap.vendor_as_of)
    # 预算终局：把所有 pending/unacquired 位置落为未知（一次、全覆盖、
    # 不重入 fallback、不出站、不发明 dispatch；两阶段合并为单一终态）
    budget_unacquired_total = sum(
        1 for s in pos_state if s in ("pending", "unacquired"))
    if batch_budget_interrupted or fallback_budget_broken:
        # 角色计数来自最终事实（已采成功=实际非未知行；采集失败=已尝试
        # 失败；预算未获取=未出站/未取得）；三者相加=分母
        successes = sum(v is not None for v in final_amount)
        gaps.append(
            "同业行情：取数预算耗尽，已采成功 "
            f"{successes} 只、采集失败 "
            f"{failed_count} 只、预算未获取 {budget_unacquired_total} 只"
            "（成员资格保留，缺失金额不参与成交额排名，不冒充全体分布；"
            "截止后零出站）")
    assert len(final_amount) == total and len(final_change) == total, (
        "成员行情批次列数必须等于成员行数（含失败与未尝试行）")
    df = df.copy()
    df["成交额"] = final_amount
    df["涨跌幅"] = final_change
    as_of_text: Optional[str] = None
    if trends2_as_ofs:
        uniq = sorted(set(trends2_as_ofs))
        as_of_text = uniq[0] if len(uniq) == 1 else f"{uniq[0]}~{uniq[-1]}"
    if trends2_count:
        gaps.append(
            f"同业行情：{trends2_count} 只经 trends2 {_DERIVED_MARKER}补采"
            f"（供应商分钟截至 {as_of_text}，元级与 f48 同口径；该来源无 f86 "
            "单标的时间戳，批次时间窗仅覆盖 stock/get 来源行）")
    if tencent_count:
        gaps.append(
            f"同业行情：{tencent_count} 只来自腾讯公开行情批量补采"
            f"（字段35精确 {tencent_exact_count} 只/字段37换算 "
            f"{tencent_wan_count} 只，单位元；非东财 f48 原样字段）")
    amounts_known = (not interrupted and failed_count == 0
                     and currency_unknown_count == 0
                     and all(v is not None for v in final_amount))
    tencent_as_of_text: Optional[str] = None
    if tencent_times:
        uniq_times = sorted(tencent_times)
        tencent_as_of_text = (
            uniq_times[0].strftime("%Y-%m-%d %H:%M:%S")
            if uniq_times[0] == uniq_times[-1]
            else f"{uniq_times[0].strftime('%Y-%m-%d %H:%M:%S')}~"
                 f"{uniq_times[-1].strftime('%Y-%m-%d %H:%M:%S')}")
    return MemberQuoteBatch(
        df=df, gaps=gaps, ts_min=ts_min, ts_max=ts_max,
        stockget_count=stockget_count, trends2_count=trends2_count,
        tencent_count=tencent_count,
        tencent_exact_count=tencent_exact_count,
        tencent_wan_count=tencent_wan_count,
        failed_count=failed_count, amounts_known=amounts_known,
        trends2_as_of=(as_of_text if trends2_as_ofs else None),
        tencent_as_of=tencent_as_of_text,
        currency_unknown_count=currency_unknown_count,
    )


_AMOUNT_PROVENANCE = (
    "成交额单位为元：已装 AkShare 成分接口与全市场快照接口同为东财 clist "
    "f6 字段且 fltt=2、原样映射「成交额」；官方行情文档标注全市场快照成交额"
    "为元，板块接口文档未标注单位，按同字段同接口参数推断"
)


@ensure(
    lambda result: (
        result.get("target_change_pct_rank") is None
        or (
            isinstance(result.get("sample_size"), int)
            and 1 <= result["target_change_pct_rank"] <= max(result["sample_size"], 1)
        )
    ),
    "target_change_pct_rank must be within the actual sample range",
)
def _peers_pillar(code: str, cons_snapshot: Optional[pd.DataFrame], *,
                  budget: ReportBudget,
                  snapshot_observed_at: Optional[str] = None,
                  subject_confirmed: bool = False,
                  amount_provenance: Optional[str] = None,
                  snapshot_source: Optional[str] = None,
                  quotes_as_of: Optional[str] = None,
                  quotes_complete: bool = True) -> Dict[str, Any]:
    """同业比较（issue #82）：身份验证同一成分快照按成交额选「成交活跃代表」。

    ``amount_provenance``/``snapshot_source``/``quotes_as_of``：成分集合来源为
    F10 反向完整成员（成员行情逐股采集）时注入对应口径与时点说明（缺省为
    clist 快照口径）。``quotes_complete=False`` 表示存在缺报价/缺额成员——
    ``target_in_amount_top5`` 必须为 None（无法证明全体资格返回未知），选取
    规则动态标注有效报价子集范围（缺报价成员资格未知，非全体板块排名）。

    规则（issue #78）：同一快照按成交额前五 + 目标股去重（目标在前五则 5 家，
    否则 6 家）；成交额原始值经 Decimal 判定后降序、并列按代码升序（稳定可
    解释）。**成员资格与成交额可排名资格分开**：无效成交额行（缺失/0/负/非
    有限）不参与排名但目标股成员仍保留（不误报「非成员」）；无效行计数在
    过滤**前**统计并暴露观测/可排名范围，剩余有效行足够也不吞限制；重复代码
    冲突整组剔除记缺口；样本不足 5 家如实标注实际范围。目标位置按快照当日
    涨跌幅降序（名次 = 严格更高数 + 1，并列同名次）；样本内存在涨跌幅缺失
    同行时**不发全样本名次**（withhold + 有效比较范围缺口），不把缺失同行
    默默排后。供应商空白/无效名称在边界适配为代码显示 + 缺口（其余有效块
    存活，不整篇 500）。不逐股补市值、不混用其他分类权重、不称基本面/市值
    龙头。资格：主行业 confirmed 才选取；资格满足而快照未取得时样本为空 +
    缺口（数据未取得，与资格不足分开表达）。
    """
    out: Dict[str, Any] = {
        "eligible": False,
        "selection_rule": "同一成分快照按成交额前五 + 目标股去重（成交活跃代表，非市值/基本面龙头）",
        "items": [],
        "target_in_amount_top5": None,
        "target_change_pct_rank": None,
        "amount_unit": "元",
        "amount_provenance": amount_provenance or _AMOUNT_PROVENANCE,
        "snapshot_rows": 0,
        "rankable_rows": 0,
        "sample_size": 0,
        "snapshot_observed_at": snapshot_observed_at,
        "source": snapshot_source,
        "quotes_as_of": quotes_as_of,
        "gaps": [],
    }
    if not subject_confirmed:
        out["gaps"].append("同业比较：主行业身份未确认（confirmed 前置不满足），不执行同业选取")
        return out
    out["eligible"] = True
    if not isinstance(cons_snapshot, pd.DataFrame) or cons_snapshot.empty:
        out["gaps"].append(
            "同业比较：成分快照未取得或形状异常，样本为空（不伪排名）"
        )
        return out
    out["source"] = snapshot_source or _CONS_SNAPSHOT_SOURCE
    df = cons_snapshot
    out["snapshot_rows"] = int(len(df))
    if "代码" not in df.columns:
        out["gaps"].append("同业比较：成分快照缺代码列，无法选取样本")
        return out

    # 行资格：6 位数字代码；重复代码冲突整组剔除
    code_series = df["代码"].astype(str).str.strip()
    valid_mask = code_series.str.fullmatch(r"\d{6}").fillna(False)
    bad_codes = int((~valid_mask).sum())
    if bad_codes:
        out["gaps"].append(f"同业比较：成分快照中 {bad_codes} 行代码非法（非 6 位数字），已剔除")
    dup_mask = code_series.duplicated(keep=False) & valid_mask
    dup_codes = sorted(set(code_series[dup_mask].tolist()))
    if dup_codes:
        shown = ", ".join(dup_codes[:5])
        more = f" 等 {len(dup_codes)} 个" if len(dup_codes) > 5 else ""
        out["gaps"].append(
            f"同业比较：成分快照存在重复代码 {shown}{more}（冲突不静默取其一），"
            "相关行全部剔除"
        )
    name_raw = df["名称"] if "名称" in df.columns else None
    amount_raw = df["成交额"] if "成交额" in df.columns else None
    change_raw = df["涨跌幅"] if "涨跌幅" in df.columns else None

    # 逐行适配（成员资格=代码有效+无重复冲突；成交额/涨跌幅/名称独立判定）。
    # 成交额整列缺失与逐行金额缺失同语义：成员保留、金额 None、不产金额排名
    # （不把未知金额成员列成伪 top5，也不为缺列丢弃已确认目标股）。
    blank_names = 0
    # 零额=已知零观测（公开保留 0、不参与活跃前五）；负/非数=无效（未知）
    amount_reasons = {"missing": 0, "zero": 0, "invalid": 0}
    members: List[Dict[str, Any]] = []
    member_index = valid_mask.to_numpy() & ~dup_mask.to_numpy()
    for i in range(len(df)):
        if not member_index[i]:
            continue
        member_code = str(code_series.iloc[i])
        name, adapted = _adapt_peer_name(
            name_raw.iloc[i] if name_raw is not None else None, member_code,
        )
        if adapted:
            blank_names += 1
        if amount_raw is not None:
            amount, reason = _decimal_amount(amount_raw.iloc[i])
            if reason == "zero":
                amount_reasons["zero"] += 1
            elif reason != "ok":
                amount_reasons[reason] += 1
        else:
            amount, reason = None, "missing"
        change = _num(change_raw.iloc[i]) if change_raw is not None else None
        members.append({
            "code": member_code, "name": name, "amount": amount,
            "change_pct": change, "is_target": member_code == code,
        })
    if blank_names:
        out["gaps"].append(
            f"同业比较：{blank_names} 行供应商名称空白/缺失，按代码显示（成员保留，源数据限制）"
        )
    # 全体报价完整性从**最终实际成员行**推导（root entry-closure 反例：clist
    # 缺额曾被外传 flag 宣称「全体成员报价完整」）：无效代码行/重复冲突行/
    # 任何金额未知（None）成员都使集合不完整。外传 ``quotes_complete``
    # 只能**降低**资格、不能提升——F10 批次与 clist 快照同一规则。
    derived_complete = (
        bad_codes == 0 and not dup_codes
        and all(m["amount"] is not None for m in members))
    quotes_effective = quotes_complete and derived_complete
    if not quotes_complete:
        out["gaps"].append(
            "同业比较：成员行情批次存在失败/未尝试成员（批次完整性标记否定全体资格）")
    elif not derived_complete:
        out["gaps"].append(
            f"同业比较：成分快照存在金额未知/资格剔除成员（原始观测 "
            f"{out['snapshot_rows']} 行、身份有效 {len(members)} 行，真实成员"
            "总数未知；由最终实际成员行推导，全体报价完整性不成立）")
    if amount_raw is None:
        out["gaps"].append(
            "同业比较：成分快照无成交额列，成交额均无数据（缺列不逐行伪计），"
            "无法按成交活跃选取代表，不输出成交额前五（成员与实际字段保留）"
        )
        out["rankable_rows"] = 0
    else:
        # 可排名行 = 有限**正**成交额（活跃代表资格）；合法零额是已知值，
        # 公开载荷保留 0 且不参与活跃前五；负/非数仍是无效（未知语义）
        out["rankable_rows"] = sum(
            1 for m in members
            if m["amount"] is not None and m["amount"] > 0)
        zero_total = amount_reasons["zero"]
        invalid_amount_total = (len(members) - out["rankable_rows"] - zero_total)
        if zero_total:
            out["gaps"].append(
                f"同业比较：{zero_total} 行成交额为合法零额（已知零观测，"
                "公开载荷保留 0；不参与成交额活跃前五，全体报价已知时其"
                "「不在前五」资格可判定）")
        if invalid_amount_total:
            parts = []
            if amount_reasons["missing"]:
                parts.append(f"缺失 {amount_reasons['missing']}")
            if amount_reasons["invalid"]:
                parts.append(f"负值/非数值 {amount_reasons['invalid']}")
            out["gaps"].append(
                f"同业比较：成分快照 {invalid_amount_total} 行成交额无效（{'、'.join(parts)}，"
                f"未知语义），不参与成交额排名；原始观测 {out['snapshot_rows']} 行、"
                f"身份有效 {len(members)} 行、正额可排名 {out['rankable_rows']} 行"
                "（真实成员总数未知，不以过滤后计数冒充）"
            )
    if not quotes_effective:
        # 缺报价/缺额/身份剔除成员资格未知：选取规则按真实角色标注有效子集
        # 范围（原始观测/身份有效/正额可排名；真实成员总数未知不称已知总数）
        # ——top5 次序只在有效报价子集内有语义，不冒充全体板块排名
        out["selection_rule"] = (
            "同一成分快照按成交额前五 + 目标股去重（成交活跃代表，非市值/基本面龙头）；"
            f"有效报价 {out['rankable_rows']} 行内次序（原始观测 "
            f"{out['snapshot_rows']} 行、身份有效 {len(members)} 行；"
            "缺报价/身份剔除成员资格未知，非全体板块排名）")

    # 成交额降序（Decimal 排序）、并列代码升序（稳定可解释）；无效额与
    # 合法零额都不进活跃排名（活跃代表只按正成交额选取，不把 0 塞进前五）
    rankable = sorted(
        (m for m in members
         if m["amount"] is not None and m["amount"] > 0),
        key=lambda m: (-m["amount"], m["code"]),
    )
    ranked_codes = [m["code"] for m in rankable]
    member_by_code = {m["code"]: m for m in members}

    target_member = member_by_code.get(code)
    if target_member is None:
        out["gaps"].append(
            f"同业比较：目标股 {code} 不在有效成分行中（与成分验证资格不符或数据异常），"
            "目标未加入样本、不输出相对位置"
        )
        out["target_in_amount_top5"] = None
    elif target_member["amount"] is None:
        out["gaps"].append(
            f"同业比较：目标股 {code} 快照成交额缺失或无效（未知语义），不参与成交额排名"
            "（成员保留，top5 资格无法判定）"
        )
        out["target_in_amount_top5"] = None  # 无法判定，不误报 False
    elif target_member["amount"] == 0:
        # 已知零额目标：全体报价已知时不属于正额活跃前五——可判 False
        # （与缺报价的「未知」分开；缺报价仍走 quotes_complete=False → None）
        if quotes_effective:
            out["gaps"].append(
                f"同业比较：目标股 {code} 成交额为已知零（合法零额观测），"
                "不属于正额活跃前五，target_in_amount_top5 判定为 False")
            out["target_in_amount_top5"] = False
        else:
            out["gaps"].append(
                "同业比较：存在缺报价/缺额或被剔除成员（全体资格未知），"
                "target_in_amount_top5 不输出 True/False（不冒充全体排名）")
            out["target_in_amount_top5"] = None
    elif not quotes_effective:
        out["gaps"].append(
            "同业比较：存在缺报价/缺额或被剔除成员（全体资格未知），"
            "target_in_amount_top5 不输出 True/False（不冒充全体排名）")
        out["target_in_amount_top5"] = None
    else:
        out["target_in_amount_top5"] = code in ranked_codes[:5]

    picked = list(ranked_codes[:5])
    if target_member is not None and code not in picked:
        picked.append(code)

    items: List[Dict[str, Any]] = []
    for c in picked:
        m = member_by_code[c]
        items.append({
            "code": c,
            "name": m["name"],
            "amount": float(m["amount"]) if m["amount"] is not None else None,
            "change_pct": m["change_pct"],
            "is_target": c == code,
        })
    out["items"] = items
    out["sample_size"] = len(items)
    if out["rankable_rows"] == 0 and items:
        out["gaps"].append(
            f"同业比较：无成交额可排名同行，样本仅保留目标股 {code}"
            "（无同业可比代表，相对位置不输出，不产出 1/1 伪名次）"
        )
    elif len(items) < 5:
        out["gaps"].append(
            f"同业比较：成交额可排名样本仅 {len(items)} 家（不足 5 家），"
            "按实际范围列示，不伪完整排名"
        )

    # 目标位置：样本内按当日涨跌幅降序，名次 = 严格更高的个数 + 1（并列同名次）；
    # 样本内存在涨跌幅缺失同行时不发全样本名次（withhold + 覆盖缺口），
    # 不把缺失同行默默排到目标之后；样本仅目标股自身（无可比同行）同样不发名次。
    if target_member is not None:
        target_change = target_member["change_pct"]
        if target_change is None:
            out["gaps"].append(
                f"同业比较：目标股 {code} 快照当日涨跌幅缺失，相对位置不输出（不按其他指标顶替）"
            )
        elif len(picked) <= 1:
            out["gaps"].append(
                "同业比较：样本内无可比同行（仅目标股自身），相对位置不输出"
                "（单成员样本不构成比较）"
            )
        else:
            valid_changes = [
                m["change_pct"] for m in (member_by_code[c] for c in picked)
                if m["change_pct"] is not None
            ]
            missing_changes = len(picked) - len(valid_changes)
            if missing_changes:
                out["gaps"].append(
                    f"同业比较：样本内 {missing_changes} 家当日涨跌幅缺失，"
                    f"有效比较范围 {len(valid_changes)}/{len(picked)} 家，"
                    "不输出全样本名次（不伪完整排名）"
                )
            else:
                out["target_change_pct_rank"] = (
                    1 + sum(1 for ch in valid_changes if ch > target_change)
                )
    out["gaps"] = _bound_gaps(out["gaps"], 14)
    return out


# ---------------------------------------------------------------------------
# P4c 行业催化（issue #83）：统一准入 + 有界文章日期证据（必需研究，
# 先于申万/概念可选长链出站）。判定逻辑在 sector_research（纯函数）；
# 本段只做编排：供应商 fallback、预算钳制、文章探查与升级重入。
# ---------------------------------------------------------------------------

def _probe_no_evidence(url: str) -> "ArticleDateEvidence":
    """探查失败路径的统一无证据对象（URL 声明日期仅作冲突判定记录）。"""
    from src.services import sector_research

    return sector_research.ArticleDateEvidence(
        None, sector_research.probe_url_declared_date(url), None,
    )


def _probe_article_date_evidence(url: str, budget: ReportBudget
                                 ) -> Tuple["ArticleDateEvidence", Optional[str],
                                            Optional[str]]:
    """有界文章日期证据探查（显式错误分类；同一线程同一取数截止）。

    同一有界 I/O 契约（与来源准入共用 ``validate_public_http_url``）：
    - 出站前 URL 公共 HTTP(S) 结构校验（注册时已验，重入防御）
    - requests ``stream=True`` 逐块读取，per-response 字节上限
      ``CATALYST_ARTICLE_MAX_BYTES`` **读取期间强制**，超限立即停止并关闭
      响应（已装 newspaper 0.2.8 ``Article.download`` 是非 stream 全量读，
      探查只需要出版元数据，不走它）；解析只在有界内容上复用已装
      ``Parser.fromstring`` + ``get_publishing_date``（不全文提取）
    - redirects 手动逐跳（``allow_redirects=False`` + Location 前置校验），
      每一跳在**连接前**过同一边界契约，跳数有界；自动跟随会让 requests
      先连出目标再检查，逃逸已经发生
    - ``request_deadline`` 线程内 bind/finally reset + 出站前显式幂等安装
      ``eastmoney_patch(budget_only=True)``（fresh process 中 ContextVar
      只有装了 patch 才真正钳制 requests 出站；不启用 auth/共享默认不变）

    返回 ``(evidence, failure_reason, final_url)``：
    - ``failure_reason`` None → evidence 有效（可能「无日期证据」）；
      ``final_url`` 是实际取得响应的**终态 URL**（重定向后的实际文章地址；
      日期证据冲突判定与升级登记的溯源域名一律以它为准，原 URL 保留引用
      身份由 caller 维护）。无重定向时 final_url == 原 URL。
    - 显式预期失败 → ``(无证据对象, 类别, 终态URL|None)``：``transport``
      （requests RequestException）、``probe_timeout``、``article_oversize``、
      ``article_parse_failed``（newspaper fromstring 显式 None 契约）、
      ``redirect_escaped:*``、``source_invalid:*``（出站前结构校验失败）。
      **已到达终态的失败**（终态非 2xx、2xx 后读取超限/超时/解析失败）
      仍返回该终态 URL——安全失败背景同样保留实际文章地址与 provenance，
      不丢弃；终态未到达（出站前拒绝/redirect 边界/连接失败）为 None。
    - ``BudgetExhausted`` 原样上抛（caller 局部终止探查，用收尾余量出
      partial 报告）；TypeError/AssertionError 等程序/契约错误**原样传播**
      （诚实失败，不伪装成「无日期」数据缺口）
    诊断文本只含静态类别名（``type(exc).__name__`` 经 sanitize），不带
    原始 URL/响应内容。
    """
    from src.patches.eastmoney_patch import eastmoney_patch, request_deadline
    from src.services import sector_research
    from src.services.run_diagnostics import sanitize_diagnostic_text

    ok, reason = sector_research.validate_public_http_url(url)
    if not ok:
        return _probe_no_evidence(url), f"source_invalid:{reason}", None

    budget.require_fetch()
    # 幂等安装既有 requests 预算边界（ContextVar 只有 patch 在位才真正
    # 钳制 requests 出站；budget_only 不启用 auth，不改共享默认行为）
    eastmoney_patch(budget_only=True)
    token = request_deadline.set(budget.fetch_deadline)
    current_url = url
    reached_terminal: Optional[str] = None  # 实际到达的终态（provenance）
    hops = 0
    try:
        # redirects 手动逐跳（allow_redirects=False）：**先校验后连接**——
        # 每一跳的 Location 在出站前过同一公共 HTTP(S) 结构契约，重定向
        # 不得实际连出边界（自动跟随会让 requests 先连到目标再检查，
        # 逃逸已经发生）；每跳同一次取数截止，跳数有界防循环
        response: Optional[requests.Response] = None
        while True:
            budget.require_fetch()
            response = requests.get(
                current_url,
                stream=True,
                timeout=max(min(5.0, budget.fetch_remaining()), 0.1),
                allow_redirects=False,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36"
                    ),
                },
            )
            if response.status_code in (301, 302, 303, 307, 308):
                location = str(response.headers.get("Location", "") or "")
                response.close()
                hops += 1
                if hops > 5:
                    return _probe_no_evidence(url), (
                        "redirect_escaped:too_many_hops"), None
                if not location:
                    return _probe_no_evidence(url), (
                        "redirect_escaped:missing_location"), None
                from urllib.parse import urljoin

                current_url = urljoin(response.url, location)
                hop_ok, hop_reason = sector_research.validate_public_http_url(
                    current_url,
                )
                if not hop_ok:
                    return _probe_no_evidence(url), (
                        f"redirect_escaped:{hop_reason}"), None
                continue
            # 重定向终态已到达：读/解析前校验**最终响应成功性**——非 2xx
            # （403/404/500/304 等）是本地文章来源失败，错误页携带的出版
            # meta 不得作日期证据（零日期升级）；安全类别只含状态码，不读
            # body/header，零查询串/内容泄漏。来源失败 ≠ 真「无新闻」，
            # 调用方按背景降级保留受限条目与已取得事实。终态地址已实际
            # 到达，随失败返回（背景保留实际文章 provenance，不丢弃）。
            if not (200 <= response.status_code < 300):
                status_code = response.status_code
                response.close()
                return _probe_no_evidence(url), (
                    f"article_http_error:{status_code}"), current_url
            reached_terminal = current_url
            break
        chunks: List[bytes] = []
        total = 0
        with response:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if budget.fetch_expired():
                    return _probe_no_evidence(url), "probe_timeout", reached_terminal
                if chunk:
                    total += len(chunk)
                    if total > sector_research.CATALYST_ARTICLE_MAX_BYTES:
                        return _probe_no_evidence(url), "article_oversize", (
                            reached_terminal)
                    chunks.append(chunk)
        if not total:
            html = ""
        else:
            # stream 已消费，response.text 不可再访问；在**有界字节**上用
            # 响应头 charset 解码（无声明时保守 utf-8 + replace——乱码页面
            # 提取不到元数据日期即保守无日期，不误报）
            charset = (response.encoding or "utf-8").strip() or "utf-8"
            try:
                html = b"".join(chunks).decode(charset, errors="replace")
            except (LookupError, TypeError):
                html = b"".join(chunks).decode("utf-8", errors="replace")
        # 日期证据以**实际终态 URL** 为准：URL 声明日期（冲突判定）与实际
        # 来源域名都取重定向后的真实文章地址——重定向不改变 provenance 事实
        # （original.html→302→2020 终态→200 metadata 2026 = URL/meta 冲突，
        # 不得借原始无日期 URL 绕过冲突合同）；原 URL 引用身份由 caller 保留
        evidence = sector_research.extract_article_date_evidence(
            html, current_url,
        )
        return evidence, None, current_url
    except BudgetExhausted:
        raise
    except sector_research.ArticleParseFailure as exc:
        # newspaper fromstring 显式 None 契约：预期解析数据错误，降级背景；
        # 解析发生在终态内容上——终态地址已知，随失败返回（不丢 provenance）
        safe = sanitize_diagnostic_text(str(exc), max_length=120)
        logger.info("article date probe parse failure: %s", safe)
        return _probe_no_evidence(url), "article_parse_failed", reached_terminal
    except requests.RequestException as exc:  # noqa: PERF203 - 显式边界
        # 已识别传输错误（连接/超时等）：预期网络失败类别，降级背景。
        # 终态已到达后的读取传输失败同样保留终态地址；连接/跳转阶段失败
        # 终态未到达（reached_terminal=None），不猜终态
        safe = sanitize_diagnostic_text(type(exc).__name__, max_length=40)
        logger.info("article date probe transport failure: %s", safe)
        return _probe_no_evidence(url), "transport", reached_terminal
    finally:
        request_deadline.reset(token)


@ensure(
    lambda result: (
        isinstance(result, dict) and "eligible" in result and "items" in result
        and "gaps" in result
    ),
    "catalysts pillar must return dict with eligible/items/gaps keys",
)
@ensure(
    lambda result: all(
        c.get("published_date") for c in result.get("items", [])
    ),
    "confirmed catalyst items must carry a qualified published_date",
)
def _catalysts_pillar(board_name: str, *, budget: ReportBudget,
                      subject_confirmed: bool = False,
                      stock_name: str = "") -> Dict[str, Any]:
    """行业催化（issue #83）：通用搜索边界 + **统一准入路径**。

    资格：主行业 confirmed 才发搜索出站（unverified 归属未确认同样不查）；
    否则 eligible=False 仅记录未执行原因。查询不走 ``search_stock_news``
    （个股新闻语义）；直接用既有 provider 链的通用 ``search`` 边界。

    统一准入（sector_research；provider-dated 与 article-dated 只在「日期
    获得」一步不同，事件资格/去重登记完全共享，背景不存在绕过事件资格的
    升级捷径）：
    1. source 验证（公共 HTTP(S) URL 结构契约：scheme/host/userinfo/端口/
       私有 literal IP/类型与长度；无效供应商字段本地拒绝计数，不 str(...)
       伪合法、不截断伪造；溯源域名一律取实际 URL host——provider source
       标签不是域名，类型非法时忽略不 500）
    2. 行业相关性（行业名/基词命中标题或摘要；个股活动词合同剔除）
    3. 日期获得：供应商发布字段（共享归一化语义）→ 合格日期或无日期候选
       → 无日期候选有界文章探查（requests 有界 stream 读取 + 字节上限 +
       每跳 redirect 边界 + 真实 newspaper 元数据提取、禁 URL 启发式、
       raw meta 依据；显式错误分类降背景，程序错误传播；
       ``BudgetExhausted`` 局部终止探查，已取得内容保留——文章探查
       超时是部分报告，不是整报 504）
    4. **同一**事件资格（窗口内不含未来日；标题声明主题时点早于窗口起始
       的历史回顾降级背景，无标题豁免词表，正文引用旧年份不参与）+
       **同一**去重登记（URL 归一化 / 同域同题 / 跨域同题+明确转载标记
       合并同一事件，升级重入完整资格——跨站新华社通稿是一个事件，不产生
       第二个 ID）
    发布日期与事件日期始终分离；事件日期未知不发明。

    预算纪律：搜索等待、每次出站、线程内首出站、文章探查全部钳制同一取数
    截止；出站前显式幂等安装 ``eastmoney_patch(budget_only=True)``（绝对
    requests 预算真正装到 wire，不只在 ContextVar）；worker 只写自己的局部
    缺口，等待成功后由 caller 合并——迟到 worker 不触碰已返回结果。供应商
    失败响应 ≠「无新闻」（sanitized 诊断保留供应商名与错误类别）；已识别
    传输错误降本块，程序错误原样可见。
    """
    from src.patches.eastmoney_patch import eastmoney_patch, request_deadline
    from src.search_service import SearchService
    from src.services import sector_research
    from src.services.run_diagnostics import sanitize_diagnostic_text

    # 幂等安装既有 requests 预算边界：本 pillar 与其线程 worker 的全部
    # requests 出站（供应商搜索、文章探查）在 fetch_deadline 后不得出 wire
    # （budget_only 不启用 auth，不改共享默认/全局 provider 行为）
    eastmoney_patch(budget_only=True)

    out: Dict[str, Any] = {
        "eligible": False,
        "query": None,
        "window_days": None, "window_start": None, "window_end": None,
        "observed_at": None, "provider": None,
        "items": [], "background_items": [],
        "dropped_duplicate_url": 0, "dropped_syndicated": 0,
        "dropped_offwindow": 0, "dropped_offtopic": 0,
        "dropped_invalid_source": 0, "dropped_invalid_field": 0,
        "source": None,
        "gaps": [],
    }
    observed_at = datetime.now().isoformat(timespec="seconds")
    out["observed_at"] = observed_at
    base_term = sector_research.catalyst_query_term(board_name)
    if not subject_confirmed or not base_term:
        out["gaps"].append(
            "行业催化：主行业身份未确认（confirmed 前置不满足），不执行行业新闻搜索"
        )
        return out
    out["eligible"] = True

    from src.search_service import get_search_service

    svc = get_search_service()
    providers = [p for p in svc._providers if p.is_available]
    if not providers:
        out["gaps"].append("行业催化：搜索入口未配置（套餐搜索不可用），本次未搜索")
        return out

    search_days = int(svc.news_window_days)
    query = f"{base_term} 行业 政策 新闻"
    out["query"] = query
    out["window_days"] = search_days
    today = datetime.now().date()
    earliest = today - timedelta(days=max(0, search_days - 1))
    # 板块窗口不含未来日（共享全局过滤的 +1 未来容忍不照搬到 board 口径）
    out["window_start"] = earliest.isoformat()
    out["window_end"] = today.isoformat()
    out["source"] = "套餐搜索（通用行业查询）"

    def _thread_search() -> Tuple[str, Any, List[str], int, int]:
        """provider fallback 搜索：返回 (provider 名, 响应或 None, 局部缺口,
        失败供应商数, 供应商侧畸形行累计)。

        线程内首出站/每次出站前检查同一取数截止；deadline bind + finally
        reset（对齐 ResearchRun.bind 语义）。失败 SearchResponse ≠ 无新闻：
        保留 sanitized 供应商名与错误类别。供应商 raw 行类型畸形计数
        （``SearchResponse.malformed_rows``，结构化字段）对**检查过的每个
        响应**累计——包括失败尝试与 fallback（那些行是真实被拒的供应商
        数据），不解析 error_message 文本，也不与本地准入拒绝双重计数
        （供应商侧拒绝的行根本不会进入本地准入）。
        """
        budget.require_fetch()
        token = request_deadline.set(budget.fetch_deadline)
        worker_gaps: List[str] = []
        failed_providers = 0
        malformed_total = 0
        last: Any = None
        try:
            for provider in providers:
                budget.require_fetch()
                provider_name = str(getattr(provider, "name", "?"))
                try:
                    resp = provider.search(
                        query, sector_research.CATALYST_SEARCH_LIMIT,
                        days=search_days,
                    )
                except BudgetExhausted:
                    raise
                except requests.RequestException as exc:
                    # 已识别传输错误：记录供应商失败，尝试下一 provider
                    safe = sanitize_diagnostic_text(
                        f"{provider_name} {type(exc).__name__}: {exc}", max_length=120,
                    ) or f"{provider_name} 传输失败"
                    worker_gaps.append(f"行业催化：搜索供应商 {safe} 传输失败")
                    failed_providers += 1
                    last = None
                    continue
                malformed_total += int(getattr(resp, "malformed_rows", 0) or 0)
                if getattr(resp, "success", False) and getattr(resp, "results", None):
                    return provider_name, resp, worker_gaps, failed_providers, (
                        malformed_total)
                if resp is not None and not getattr(resp, "success", False):
                    # 显式失败响应 ≠「无新闻」：sanitized 错误类别保留
                    safe = sanitize_diagnostic_text(
                        str(getattr(resp, "error_message", "") or "未知原因"),
                        max_length=120,
                    ) or "未知原因"
                    worker_gaps.append(
                        f"行业催化：搜索供应商 {provider_name} 返回失败（{safe}）"
                    )
                    failed_providers += 1
                if last is None or getattr(resp, "success", False):
                    last = resp
            return "", last, worker_gaps, failed_providers, malformed_total
        finally:
            request_deadline.reset(token)

    try:
        provider_name, resp, worker_gaps, failed_providers, malformed_total = (
            bounded_call(_thread_search, budget=budget,)
        )
    except BudgetExhausted:
        out["gaps"].append("行业催化：取数预算不足，行业新闻搜索未完成（已取得内容保留）")
        return out
    # 供应商 raw 畸形行计数并入 invalid_field 总数（诚实总数 = 供应商侧
    # 拒绝 + 本地准入拒绝；两者作用对象不相交，无双重计数）
    if malformed_total:
        out["dropped_invalid_field"] += malformed_total
    # 迟到 worker 只写自己的 worker_gaps；本 out 已被 caller 持有
    out["gaps"].extend(worker_gaps)
    if not provider_name or resp is None:
        if failed_providers:
            out["gaps"].append(
                "行业催化：搜索供应商均返回失败（见上方分类），不冒充「窗口内无新闻」"
            )
        elif not out["gaps"]:
            out["gaps"].append(
                "行业催化：搜索未返回结果（窗口内无可用新闻或供应商覆盖限制）"
            )
        out["gaps"] = _bound_gaps(out["gaps"], 14)
        return out
    out["provider"] = str(provider_name)

    board_name_s = str(board_name)
    stock_name_s = str(stock_name or "").strip()

    # ---- 统一登记闭包：事件资格 + 去重合并（两个日期路径共用同一步） ----
    seen_urls: set = set()
    seen_syndication: set = set()
    seen_headline: Dict[str, str] = {}
    items: List[Dict[str, Any]] = []
    background: List[Dict[str, Any]] = []
    seq = [0]
    bg_seq = [0]
    undated_count = [0]
    _title_to_snippet: Dict[str, str] = {}

    def url_key_of(url: str) -> str:
        return sector_research.normalize_catalyst_url(url)

    def _dedup_register(url_key: str, domain: str, title_key: str) -> str:
        """URL/同域同题去重登记。返回 ok/duplicate_url/syndicated。"""
        if not url_key or url_key in seen_urls:
            return "duplicate_url"
        synd_key = (domain.lower(), title_key)
        if synd_key in seen_syndication:
            return "syndicated"
        seen_urls.add(url_key)
        seen_syndication.add(synd_key)
        return "ok"

    def _merge_same_event(title_key: str, url: str, snippet: str) -> bool:
        """跨域同题 + 明确转载标记 → 同一事件（补充链接，不产生第二个 ID）。"""
        prior_id = seen_headline.get(title_key)
        if prior_id is None:
            return False
        first_snippet = _title_to_snippet.get(title_key, "")
        if not (sector_research.SYNDICATION_SOURCE_RE.search(snippet)
                or sector_research.SYNDICATION_SOURCE_RE.search(first_snippet)
                or sector_research.SYNDICATION_SOURCE_RE.search(title_key)):
            return False
        for entry in items:
            if entry["evidence_id"] == prior_id and len(
                entry["same_event_urls"]
            ) < sector_research.SAME_EVENT_URLS_MAX:
                entry["same_event_urls"].append(url[:sector_research.CATALYST_URL_MAX])
                break
        out["dropped_syndicated"] += 1
        return True

    def _clear_candidate_registration(url_key: str, domain: str,
                                      title_key: str) -> None:
        """摘除**本候选**在背景登记时的去重键（升级重入同一资格，不算重复）。"""
        seen_urls.discard(url_key)
        seen_syndication.discard((domain.lower(), title_key))

    def _qualify_and_register(published: date, date_basis: str,
                              date_basis_detail: Optional[str],
                              title: str, url: str, domain: str,
                              snippet: Optional[str],
                              final_url: Optional[str] = None) -> str:
        """统一事件资格：窗口 → 历史回顾 → 去重登记 → 已证实条目。

        返回 "ok" / "offwindow" / "retrospective" / "duplicate_url" /
        "syndicated"；provider-dated 与 article-dated 路径共用（升级重入
        完整资格）。历史回顾降级由 caller 落背景（带发布日期与降级原因）。

        ``final_url``（article_metadata 口径传入）：实际取得响应的终态
        URL——去重键与同域判定以**实际文章**为准（两个 redirect alias 是
        同一篇文章，不是独立确认），条目 ``url`` 保留原引用身份，
        ``final_url`` 如实区分。
        """
        title_key = re.sub(r"\s+", "", title)
        evidence_url = final_url or url
        # 1) 窗口资格（发布日期只作资格依据；事件日期未知不发明）
        reject = sector_research.qualifies_event(published, earliest, today)
        if reject is not None:
            out["dropped_offwindow"] += 1
            return "offwindow"
        # 2) 历史回顾判定（标题主题时点口径；两个日期路径一致）——只判资格
        #    与去重登记，背景落库由 caller 负责（provider 循环 / probe 升级
        #    各自携带自己的降级原因，同一候选不会双份落背景）
        if sector_research.is_retrospective_title(title, earliest):
            dup_kind = _dedup_register(url_key_of(evidence_url), domain,
                                       title_key)
            if dup_kind != "ok":
                out[f"dropped_{dup_kind}"] += 1
                return dup_kind
            return "retrospective"
        # 3) 去重登记（URL/同域同题；键取实际文章 URL）
        dup_kind = _dedup_register(url_key_of(evidence_url), domain, title_key)
        if dup_kind != "ok":
            out[f"dropped_{dup_kind}"] += 1
            return dup_kind
        # 4) 跨域同源转载合并（同题 + 明确转载标记 = 同一事件）
        if title_key in seen_headline:
            if _merge_same_event(title_key, url, snippet or ""):
                return "syndicated"
            # 跨域同题但无转载标记：保守不合并，按独立条目继续（display cap 内）
        if len(items) < sector_research.CATALYST_DISPLAY_CAP:
            seq[0] += 1
            entry: Dict[str, Any] = {
                "evidence_id": f"cat-{seq[0]}",
                "title": title[:sector_research.CATALYST_TITLE_MAX],
                "url": url[:sector_research.CATALYST_URL_MAX],
                "source_domain": domain,
                "snippet": (snippet or "")[:sector_research.CATALYST_SNIPPET_MAX] or None,
                "published_date": published.isoformat(),
                "date_basis": date_basis,
                "date_basis_detail": date_basis_detail,
                "same_event_urls": [],
                "final_url": final_url,
            }
            items.append(entry)
            seen_headline[title_key] = entry["evidence_id"]
            _title_to_snippet[title_key] = snippet or ""
            return "ok"
        return "ok"

    for raw_item in resp.results:
        # ---- 候选文本类型契约（单一准入入口；两个日期分支共享）----
        # 供应商字段先查**实际类型**再做语义处理：title/url 是必需的非空
        # str，snippet 可选（None=缺失，合法）但出现即必须是 str。畸形行
        # （dict/bytes/数字等集合/非文本类型）本地拒绝计数，不 str(...) 把
        # 结构/字节/数字伪合法成"标题/摘要"（真实 MiniMax JSON title=dict
        # 曾被字符串化为假标题登记 cat-1）；剩余有效行/事实全部保留，缺失
        # 可选文本只是缺字段，不是错误、不 500。
        raw_title = getattr(raw_item, "title", None)
        raw_url = getattr(raw_item, "url", None)
        raw_snippet = getattr(raw_item, "snippet", None)
        if not isinstance(raw_title, str) or not raw_title.strip():
            out["dropped_invalid_field"] += 1
            continue
        if not isinstance(raw_url, str) or not raw_url.strip():
            out["dropped_invalid_field"] += 1
            continue
        if raw_snippet is not None and not isinstance(raw_snippet, str):
            out["dropped_invalid_field"] += 1
            continue
        title = raw_title.strip()
        url = raw_url.strip()
        snippet = raw_snippet.strip() if isinstance(raw_snippet, str) else None
        # 1) source 验证先于一切分支（已证实与背景同一准入）：公共 HTTP(S)
        #    结构契约；无效字段类型/超长本地拒绝计数，不字符串化/截断伪合法
        url_ok, url_reason = sector_research.validate_public_http_url(url)
        if not url_ok:
            out["dropped_invalid_source"] += 1
            logger.info(
                "catalyst source rejected (%s): %s",
                url_reason, sanitize_diagnostic_text(url, max_length=120),
            )
            continue
        url_key = url_key_of(url)
        if not url_key:
            out["dropped_invalid_source"] += 1
            continue
        # 2) 行业相关性（同一准入；个股活动合同剔除）
        text = f"{title} {snippet or ''}"
        if board_name_s not in text and base_term not in text:
            out["dropped_offtopic"] += 1
            continue
        if stock_name_s and stock_name_s in title and sector_research.STOCK_ONLY_EVENT_RE.search(title):
            out["dropped_offtopic"] += 1
            continue
        # 溯源域名 = 实际 URL 的**完整**解析 host（结构校验已保证非空且
        # ≤253，不截断——截断段不是来源身份）；provider source 标签不是
        # 域名替身，类型/内容非法时忽略即可，不影响准入、不 500
        host = sector_research.urlsplit_host(url)
        domain = host
        # 3) 日期获得：供应商发布字段（共享归一化语义）
        published = SearchService._normalize_news_publish_date(
            getattr(raw_item, "published_date", None)
        )
        if published is not None:
            # provider-dated：直接进入统一事件资格（与 article-dated 同一步）
            verdict = _qualify_and_register(
                published, "provider_published", None,
                title, url, domain, snippet,
            )
            if verdict == "retrospective":
                # 历史回顾：统一资格判定通过（URL 已登记），落明确限制背景
                if len(background) < sector_research.CATALYST_BACKGROUND_CAP:
                    bg_seq[0] += 1
                    background.append({
                        "evidence_id": f"bg-{bg_seq[0]}",
                        "title": title[:sector_research.CATALYST_TITLE_MAX],
                        "url": url[:sector_research.CATALYST_URL_MAX],
                        "source_domain": domain,
                        "snippet": (snippet or "")[:sector_research.CATALYST_SNIPPET_MAX] or None,
                        "published_date": published.isoformat(),
                        "downgrade_reason": (
                            f"标题声明主题时点早于窗口起始（{earliest.isoformat()}），"
                            "历史回顾属性，仅背景（发布日期非事件日期）"
                        ),
                        "final_url": None,
                    })
            continue
        # 无日期候选：背景登记（同一准入序列；不做事件资格——尚无日期）
        title_key = re.sub(r"\s+", "", title)
        dup_kind = _dedup_register(url_key, domain, title_key)
        if dup_kind != "ok":
            out[f"dropped_{dup_kind}"] += 1
            continue
        undated_count[0] += 1
        if len(background) < sector_research.CATALYST_BACKGROUND_CAP:
            bg_seq[0] += 1
            background.append({
                "evidence_id": f"bg-{bg_seq[0]}",
                "title": title[:sector_research.CATALYST_TITLE_MAX],
                "url": url[:sector_research.CATALYST_URL_MAX],
                "source_domain": domain,
                "snippet": (snippet or "")[:sector_research.CATALYST_SNIPPET_MAX] or None,
                "published_date": None,
                "downgrade_reason": None,
                "final_url": None,
            })

    # 4) 无日期背景条目：有界文章日期探查（显式错误分类；升级走同一资格）
    probe_used = 0
    upgraded = 0
    kept_background: List[Dict[str, Any]] = []
    for bg_item in background:
        needs_probe = not bg_item.get("published_date")
        if needs_probe and (
            probe_used >= sector_research.CATALYST_ARTICLE_PROBE_CAP
            or len(items) >= sector_research.CATALYST_DISPLAY_CAP
            or budget.fetch_expired()
        ):
            kept_background.append(bg_item)
            continue
        if needs_probe:
            probe_used += 1
            try:
                evidence, probe_failure, final_url = bounded_call(
                    _probe_article_date_evidence, bg_item["url"],
                    budget, budget=budget,
                )
            except BudgetExhausted:
                # 文章探查耗尽取数预算：结束探查（部分报告），已取得内容保留，
                # 收尾用主截止内的既有收尾余量——不传播整报超时
                out["gaps"].append(
                    "行业催化：文章日期探查因取数预算不足提前结束"
                    "（已取得催化与背景内容保留）"
                )
                kept_background.append(bg_item)
                kept_background.extend(background[background.index(bg_item) + 1:])
                break
            # 终态已知（探查成功，或已到达终态的安全失败：非 2xx/读取超限/
            # 超时/解析失败）→ 资格判定**前**统一把本候选重键到**实际文章**
            # 身份：先按原登记键摘除（不先改域名再摘，避免摘错键），再按
            # 终态键重注册——去重身份与展示 provenance 一致；背景保留原引用
            # url + 终态 final_url + 实际完整 hostname（冲突/回顾/重复别名/
            # 安全失败背景同样不丢实际文章溯源）。同终态已被其他条目登记
            # （同文 alias 先行升级/先到背景）→ 按重复计数并保留背景+终态。
            final_host = sector_research.urlsplit_host(final_url or "")
            bg_title_key = re.sub(r"\s+", "", bg_item["title"])
            if final_url and final_host:
                _clear_candidate_registration(
                    url_key_of(bg_item["url"]), bg_item["source_domain"],
                    bg_title_key,
                )
                bg_item["final_url"] = final_url
                bg_item["source_domain"] = final_host
                rekey = _dedup_register(url_key_of(final_url), final_host,
                                        bg_title_key)
                if rekey != "ok":
                    out[f"dropped_{rekey}"] += 1
                    bg_item["downgrade_reason"] = {
                        "duplicate_url": (
                            "实际文章（终态地址）与已取得条目重复，仅保留先到条目"
                        ),
                        "syndicated": (
                            "与已取得条目为同一事件（同源转载），不重复计入"
                        ),
                    }.get(rekey, "实际文章与已取得条目重复，仅背景")
                    kept_background.append(bg_item)
                    continue
            if probe_failure is not None:
                # 显式预期失败（传输/超限/解析数据/redirect 边界）→ 背景保留
                # + 安全原因；程序/契约错误已原样传播（bounded_call 不吞）。
                # 终态已到达的失败上方已重键并携带终态 provenance。
                bg_item["downgrade_reason"] = {
                    "transport": "文章日期探查传输失败（已识别网络错误类别），仅背景",
                    "probe_timeout": "文章日期探查超过取数截止，仅背景",
                    "article_oversize": (
                        f"文章响应超过有界读取上限"
                        f"（{sector_research.CATALYST_ARTICLE_MAX_BYTES} 字节），仅背景"
                    ),
                    "article_parse_failed": (
                        "文章内容无法被解析器识别（解析数据错误），仅背景"
                    ),
                }.get(
                    probe_failure.split(":", 1)[0],
                    f"文章探查未确证（{probe_failure.split(':', 1)[0]}），仅背景",
                )[:200]
                kept_background.append(bg_item)
                continue
            qualified, reason = sector_research.qualifies_article_declared_date(
                evidence, earliest, today,
            )
            if qualified is not None:
                # 升级走**同一**事件资格（窗口/回顾/去重登记不绕过）。本候选
                # 已重键到终态身份：摘除自身终态键（重入非重复）后按终态
                # 注册——溯源域名与去重键取**实际文章**（终态 host 完整保留，
                # redirect alias 不算独立确认）；原 URL 保留引用身份
                # （条目 url 字段），final_url 如实区分。
                _clear_candidate_registration(
                    url_key_of(bg_item.get("final_url") or bg_item["url"]),
                    bg_item["source_domain"], bg_title_key,
                )
                verdict = _qualify_and_register(
                    qualified, "article_metadata",
                    (evidence.raw_basis or "")[:sector_research.CATALYST_SNIPPET_MAX],
                    bg_item["title"], bg_item["url"],
                    bg_item["source_domain"], bg_item.get("snippet"),
                    final_url=final_url,
                )
                if verdict == "ok":
                    upgraded += 1
                    continue
                # 升级被资格/去重拒绝：按原背景保留 + 具体说明（同一候选不双份）
                bg_item["downgrade_reason"] = {
                    "retrospective": (
                        f"文章元数据日期确证后经统一资格判定为历史回顾"
                        f"（标题声明主题时点早于 {earliest.isoformat()}），仅背景"
                    ),
                    "offwindow": "文章元数据日期在窗口外，仅背景",
                    "duplicate_url": (
                        "实际文章（终态地址）与已取得条目重复，仅保留先到条目"
                    ),
                    "syndicated": "与已取得条目为同一事件（同源转载），不重复计入",
                }.get(verdict, "文章元数据日期未通过统一资格判定，仅背景")
                kept_background.append(bg_item)
                continue
            # 未能确证：如实降级说明（URL-only/无依据/冲突/窗口外）；
            # 冲突/窗口外等背景的终态 provenance 已在上方重键时保留
            reason_text = {
                "url_only": "URL 含日期但无文章元数据依据（URL 日期不单独作证据）",
                "no_date_evidence": "文章无可用发布日期证据",
                "metadata_date_without_raw_basis": "文章元数据日期无法定位原始依据（保守拒绝）",
                "url_meta_date_conflict": "URL 日期与文章元数据日期冲突（provenance 冲突）",
                "article_date_offwindow": "文章元数据日期在窗口外",
            }.get(reason, f"文章日期证据不足（{reason}）")
            bg_item["downgrade_reason"] = reason_text[:200]
        kept_background.append(bg_item)
    background = kept_background

    out["items"] = items
    out["background_items"] = background
    if not items:
        if failed_providers:
            out["gaps"].append(
                "行业催化：搜索供应商失败，已证实近期催化未取得（不冒充无新闻）"
            )
        elif undated_count[0] and not out["dropped_offwindow"]:
            out["gaps"].append(
                f"行业催化：搜索返回 {undated_count[0]} 条相关结果均无可用发布日期，"
                "无法确证近期催化（URL 日期不单独作证据；文章探查未能确证）；"
                "无日期条目仅列示为背景线索，不作为已证实近期催化"
            )
        elif undated_count[0]:
            out["gaps"].append(
                "行业催化：窗口外或无日期结果均不构成已证实近期催化（见上方计数）"
            )
        else:
            out["gaps"].append(
                "行业催化：去重与资格过滤后无已证实近期催化（真实无窗口内行业新闻"
                "或供应商覆盖限制，不补造）"
            )
    else:
        if upgraded:
            out["gaps"].append(
                f"其中 {upgraded} 条经文章元数据日期确证"
                "（date_basis=article_metadata，含可保存的 meta 原始依据；"
                "URL 启发式已禁用）"
            )
        if background:
            out["gaps"].append(
                f"另有 {len(background)} 条无日期或降级结果仅列示为背景线索"
                "（bg-*，不作为已证实近期催化，不单独支撑重要结论）"
            )
    out["gaps"] = _bound_gaps(out["gaps"], 14)
    return out


# ---------------------------------------------------------------------------
# P4d 证据化综合研判（issue #83）：既有模型路由 + 登记表校验（必需研究，
# 有合格催化才调用模型；先于申万/概念可选长链）。
# ---------------------------------------------------------------------------

def _build_evidence_registry(stock_name: str, code: str, ident: Dict[str, Any],
                             prosper: Dict[str, Any], trend: Dict[str, Any],
                             peers: Dict[str, Any],
                             catalysts: Dict[str, Any]) -> "EvidenceRegistry":
    """实际取得的合格事实 → 证据登记表（缺失块不注册 → 模型不可引用）。

    只注册 immutable 事实：静态先验（政策 DNA/基率）不注册（仅报告背景，
    不能独立支撑结论）；身份注册但不含行情数值（身份事实不能佐证 fabricated
    metrics）；bg-* 注册为 background（只能附加引用）。材料与 payload 载荷
    同源（cat 条目字段一致）。root-decision 所有权：市场类证据
    （intraday/trend/peers/target）的 raw magnitudes 不进模型投影（由
    ``market_observation_relations`` 定性观察关系替代），非市场事实原样
    投影；模型 claims 不含明确业务量值（有限明确量值契约，无 numeric token 锚定）。
    """
    from src.services import sector_research

    registry = sector_research.EvidenceRegistry()
    registry.register(sector_research.EvidenceEntry.build(
        "identity", "identity", "主行业身份",
        {
            "stock_name": stock_name,
            "stock_code": code,
            "industry_em": ident.get("industry_em") or None,
            "board_name": ident.get("board_name") or None,
            "board_code": ident.get("board_code") or None,
            "member_count": (ident.get("membership") or {}).get("member_count"),
        },
    ))
    row = prosper.get("row") or {}
    if prosper.get("sector_found"):
        snapshot_source = prosper.get("snapshot_source")
        registry.register(sector_research.EvidenceEntry.build(
            "intraday", "fact", "当日板块行情（当前快照，单期观测）",
            {
                "change_pct": row.get("change_pct"),
                "rank_of": prosper.get("rank_of"),
                "total_boards": prosper.get("total_boards"),
                "momentum": prosper.get("momentum"),
                "breadth": prosper.get("breadth"),
                # 快照来源/时点贯穿登记表（root 纪律：as_of 不止 logger）；
                # 衍生标记与 provenance 文本同源（_DERIVED_MARKER 单一 owner）
                "snapshot_source": snapshot_source,
                "snapshot_derived": bool(
                    snapshot_source and _DERIVED_MARKER in snapshot_source),
                "metric_meaning": {
                    "change_pct": "板块指数当日涨跌幅（百分比，负值为下跌）",
                    "rank_of/total_boards": "按当日涨跌幅在全部行业板块中的"
                                            "排名（当前快照，无历史动量观测）",
                    "momentum": "有界 0-100 横截面涨跌幅分位（有效板块中严格"
                                "低于目标的占比折算），正值不表示收益为正或"
                                "涨势延续",
                    "breadth": "当日上涨家数占比（百分比，当前快照）",
                },
            },
        ))
    if trend.get("eligible") and trend.get("period_return_pct") is not None:
        registry.register(sector_research.EvidenceEntry.build(
            "trend", "fact", "板块近期趋势（最近 20 个已完成交易日）",
            {
                "window": [trend.get("start_date"), trend.get("end_date")],
                "qualified_sessions": trend.get("qualified_sessions"),
                "period_return_pct": trend.get("period_return_pct"),
                "benchmark_period_return_pct": (trend.get("benchmark") or {}).get(
                    "period_return_pct"),
                "relative_return_pp": trend.get("relative_return_pp"),
            },
        ))
    peer_items = [
        {"name": p.get("name"), "is_target": p.get("is_target"),
         "change_pct": p.get("change_pct")}
        for p in (peers.get("items") or [])
    ]
    if peer_items:
        registry.register(sector_research.EvidenceEntry.build(
            "peers", "fact", "同业成交活跃代表（快照当日口径）",
            {
                # 选取规则是权威口径元数据（同一快照成交额前五 + 目标股
                # 去重；活跃代表，非市值/基本面龙头）——投影给模型输入，
                # 防止把活跃代表升格为「龙头」；缺报价时动态标注有效子集范围
                "selection_rule": peers.get("selection_rule"),
                "items": peer_items,
                # 成交额来源口径与供应商时点贯穿登记表（root 纪律：
                # 衍生来源/as_of 不止 logger，模型投影同样不得越过真实口径）
                "amount_provenance": peers.get("amount_provenance"),
                "amount_derived": bool(
                    _DERIVED_MARKER in (peers.get("amount_provenance") or "")),
                "quotes_as_of": peers.get("quotes_as_of"),
            },
        ))
    target_change = next(
        (p.get("change_pct") for p in peer_items if p.get("is_target")), None,
    )
    if target_change is not None:
        registry.register(sector_research.EvidenceEntry.build(
            "target", "fact", "目标股快照当日涨跌幅",
            {"stock_name": stock_name, "snapshot_change_pct": target_change},
        ))
    for c in (catalysts.get("items") or []):
        registry.register(sector_research.EvidenceEntry.build(
            c["evidence_id"], "catalyst", c["title"],
            {
                "title": c["title"],
                "url": c["url"],
                "source_domain": c["source_domain"],
                "snippet": c.get("snippet"),
                "published_date": c["published_date"],
                "date_basis": c.get("date_basis") or "provider_published",
                "same_event_urls": c.get("same_event_urls") or [],
                "final_url": c.get("final_url"),
            },
        ))
    for c in (catalysts.get("background_items") or []):
        payload: Dict[str, Any] = {
            "title": c["title"],
            "url": c["url"],
            "source_domain": c["source_domain"],
            "snippet": c.get("snippet"),
        }
        if c.get("final_url"):
            # 终态已知（冲突/回顾/重复别名/到达终态的安全失败背景同样
            # 保留实际文章地址——模型与公开载荷同源，不丢 provenance）
            payload["final_url"] = c["final_url"]
        if c.get("published_date"):
            payload["published_date"] = c["published_date"]
        if c.get("downgrade_reason"):
            payload["limitation"] = c["downgrade_reason"]
        registry.register(sector_research.EvidenceEntry.build(
            c["evidence_id"], "background", c["title"], payload,
        ))
    return registry


@ensure(
    lambda result: (
        isinstance(result, dict) and "eligible" in result and "status" in result
        and "gaps" in result
    ),
    "synthesis pillar must return dict with eligible/status/gaps keys",
)
@ensure(
    lambda result: (
        result.get("status") != "ok"
        or (
            isinstance(result.get("claims"), list) and result["claims"]
            and all(c.get("evidence_refs") for c in result["claims"])
        )
    ),
    "ok synthesis must carry at least one claim with non-empty evidence_refs",
)
def _synthesis_pillar(stock_name: str, code: str, ident: Dict[str, Any],
                      prosper: Dict[str, Any], trend: Dict[str, Any],
                      peers: Dict[str, Any], catalysts: Dict[str, Any], *,
                      budget: ReportBudget,
                      subject_confirmed: bool = False) -> Dict[str, Any]:
    """证据化综合研判（issue #83）：登记表引用 + 有限明确量值输出契约 + 绝对预算。

    资格：confirmed 才执行；无已证实催化不调用模型（``no_qualified_facts``，
    先验/背景不产生空泛完整叙事）。模型未配置/失败/畸形是不同 ``status``；
    **程序契约错误（TypeError/AssertionError）经 adapter opt-in 上抛原样
    可见**（root 批准的唯一共享边界变更，``propagate_program_errors=True``），
    不伪装成模型失败或数据缺失；已识别传输/超时错误降本块（sanitized）。

    模型 claims 不含明确业务量值：明确量值/倍数陈述整条拒绝并记 sanitized
    原因（普通中文含数量形汉字不因字形拒绝，issue #85 有限合同），事实
    全部保留；市场数值关系的
    摘要/观察限制由服务端程序化渲染（``summary_origin="server_facts"``，
    含精确市场数值/日期——数值只来自服务端所有权，不来自模型文本）。

    绝对预算到真实 wire：模型出站线程内绑定 ``ResearchRun``（deadline =
    ReportBudget.fetch_deadline，同一单调时刻域）+ ``request_deadline``，
    finally reset（不向共享池下一个请求泄漏）；出站前显式幂等安装
    ``eastmoney_patch(budget_only=True)``（ContextVar 只有 patch 在位才
    真正钳制 requests/HTTPX 出站）；``limited_acompletion._deadline``
    取 run.deadline 绝对值 → 队列等待/首字节/流/每次 HTTP 重试同一绝对
    截止，不在路由选择后重置相对时钟（timeout 传进入时剩余值，fallback
    循环内递减消耗同一预算）。迟到 worker 只写自己诊断，不 mutate 已返回
    报告。
    """
    from src.core.research_run import ResearchRun, current_research
    from src.patches.eastmoney_patch import eastmoney_patch, request_deadline
    from src.services import sector_research
    from src.services.run_diagnostics import sanitize_diagnostic_text

    # 幂等安装既有 requests 预算边界（模型 SDK 传输与重试同受钳制；
    # budget_only 不启用 auth，不改共享默认/全局 provider 行为）
    eastmoney_patch(budget_only=True)

    out: Dict[str, Any] = {
        "eligible": False,
        "status": "skipped_identity",
        "summary": None, "claims": [], "uncertainty_note": None,
        "rejected_count": 0,
        "provider": None, "model": None,
        "observed_at": None,
        "gaps": [],
    }
    observed_at = datetime.now().isoformat(timespec="seconds")
    out["observed_at"] = observed_at
    if not subject_confirmed:
        out["gaps"].append(
            "证据化综合：主行业身份未确认（confirmed 前置不满足），不执行模型综合"
        )
        return out
    out["eligible"] = True
    if not (catalysts.get("items") or []):
        out["status"] = "no_qualified_facts"
        out["gaps"].append(
            "证据化综合：无已证实近期催化事实，不执行模型综合"
            "（背景/先验不产生完整叙事；趋势/同业事实保留在对应块）"
        )
        return out

    from src.agent.llm_adapter import LLMToolAdapter, _resolve_litellm_exception

    adapter = LLMToolAdapter()
    # 真实契约：bool property（显式断言可用性，不静默跳过）
    if not isinstance(adapter.is_available, bool) or not adapter.is_available:
        out["status"] = "model_unavailable"
        out["gaps"].append("证据化综合：模型未配置（LLM 通道不可用），本次未综合")
        return out

    registry = _build_evidence_registry(
        stock_name, code, ident, prosper, trend, peers, catalysts,
    )
    messages = [
        {"role": "system", "content": sector_research.SYNTH_SYSTEM_PROMPT},
        {"role": "user", "content": sector_research.build_synthesis_user_text(
            registry,
        )},
    ]

    # 已识别模型传输/服务错误类（stub 环境返回 fallback 类，行为一致）
    llm_transport_excs = tuple(
        _resolve_litellm_exception(name)
        for name in ("RateLimitError", "APIError", "AuthenticationError",
                     "BadRequestError", "ServiceUnavailableError",
                     "ContextWindowExceededError")
    )

    def _call_model() -> Any:
        """线程内绝对预算绑定 + opt-in 程序错误上抛（见 docstring）。"""
        budget.require_fetch()
        run = ResearchRun(timeout_seconds=max(budget.fetch_remaining(), 0.001))
        # 绝对修正：ResearchRun.deadline 与 ReportBudget.fetch_deadline 是
        # 同一 monotonic 时刻（构造 + 少量偏移已校正）；queue/fallback/stream
        # /每次重试经 limited_acompletion._deadline 取此绝对值。
        run.deadline = budget.fetch_deadline
        token_run = current_research.set(run)
        token_deadline = request_deadline.set(budget.fetch_deadline)
        try:
            return adapter.call_text(
                messages,
                timeout=max(budget.fetch_remaining(), 0.001),
                max_tokens=sector_research.SYNTHESIS_MAX_TOKENS,
                # root 批准的唯一共享边界变更：程序契约错误保留原样身份
                # 上抛（默认 False 完全保持既有消费者行为）
                propagate_program_errors=True,
            )
        finally:
            request_deadline.reset(token_deadline)
            current_research.reset(token_run)

    try:
        resp = bounded_call(_call_model, budget=budget)
    except BudgetExhausted:
        out["status"] = "model_error"
        out["gaps"].append("证据化综合：取数预算不足，模型综合未完成（合格事实保留）")
        return out
    except TimeoutError as exc:
        # 已识别超时（队列等待/调用超时/预算停止）：模型侧缺口，非预算终态
        safe = sanitize_diagnostic_text(
            f"{type(exc).__name__}: {exc}", max_length=120,
        ) or type(exc).__name__
        out["status"] = "model_error"
        out["gaps"].append(
            f"证据化综合：模型队列或调用超时（{safe}），合格事实保留，不产出综合"
        )
        return out
    except llm_transport_excs as exc:  # noqa: BLE001 - 已识别传输/服务错误降块
        safe = sanitize_diagnostic_text(
            f"{type(exc).__name__}: {exc}", max_length=120,
        ) or type(exc).__name__
        out["status"] = "model_error"
        out["gaps"].append(
            f"证据化综合：模型服务错误（{safe}），合格事实保留，不产出综合"
        )
        return out
    # propagate_program_errors=True 时 TypeError/AssertionError 已从 adapter
    # 原样上抛（不在本函数捕获）——程序契约错误经 compose/generate 可见，
    # API 层按 500 报告，不伪装成模型失败。

    if getattr(resp, "provider", "") == "error" or not getattr(resp, "content", None):
        out["status"] = "model_error"
        safe = sanitize_diagnostic_text(
            str(getattr(resp, "content", "") or "空响应"), max_length=120,
        ) or "空响应"
        out["gaps"].append(f"证据化综合：模型路由失败（{safe}），合格事实保留")
        return out

    parsed = sector_research.parse_synthesis_json(resp.content)
    if parsed is None:
        out["status"] = "malformed_output"
        out["gaps"].append(
            "证据化综合：模型输出不是合法 JSON（已拒绝，不采纳部分文本），"
            "合格事实保留"
        )
        return out
    claims, rejected_reasons = sector_research.validate_synthesis_output(
        parsed, registry,
    )
    if not claims:
        out["status"] = "malformed_output"
        detail = rejected_reasons[0] if rejected_reasons else "unknown"
        safe = sanitize_diagnostic_text(detail, max_length=120) or detail
        out["gaps"].append(
            f"证据化综合：模型结论均未通过结论/引用/结构与量值边界校验"
            f"（首个原因：{safe}），已整体拒绝，合格事实保留"
        )
        return out
    out["status"] = "ok"
    # root-decision 所有权边界：summary/uncertainty 由服务端从已采纳事实
    # 程序化生成，绝不消费模型 claims 文本——模型错误句不可能进入两个核心
    # 字段（identity/新闻的 raw 值与出处照常进 wire；市场 magnitudes 有意
    # 排除出 wire，以定性观察关系呈现）。
    out["summary"] = sector_research.program_fact_summary(registry)
    out["summary_origin"] = "server_facts"
    out["claims"] = claims
    out["uncertainty_note"] = sector_research.program_fact_limitations(registry)
    out["provider"] = str(getattr(resp, "provider", "") or "")[:64]
    out["model"] = str(getattr(resp, "model", "") or "")[:120]
    if rejected_reasons:
        # 混合输出（部分结论合格 + 部分被拒）：合格结论与全部服务端事实保留，
        # rejected_count 显式携带拒绝规模 → compose 完整性=partial（不呈现
        # 全部合格研究）；报告口径与用户可见状态一致（issue #83 边界终态）。
        out["rejected_count"] = len(rejected_reasons)
        safe = sanitize_diagnostic_text(
            f"{len(rejected_reasons)} 条（{rejected_reasons[0]}）", max_length=120,
        )
        out["gaps"].append(
            f"证据化综合：模型返回 {safe} 未通过校验的结论已整条拒绝"
            "（明确量值陈述 / 未知证据 ID / 结构畸形 / 行动词 / 超限），未采纳"
        )
    out["gaps"] = _bound_gaps(out["gaps"], 14)
    return out


# ---------------------------------------------------------------------------
# P5 相关概念（仅列示）+ v2 编排（不产出综合总分，issue #78 决策 5 / #79）
# ---------------------------------------------------------------------------

def _concepts_pillar(code: str, *, budget: ReportBudget) -> Dict[str, Any]:
    """个股相关概念，仅列示（iFinD 已适配入口，有界读取）。

    - 概念不替代行业、不展开独立研究、不扫描全部概念板块成分。
    - 入口未启用、供应商失败或取数预算不足记缺口，不拖垮主行业报告
      （可选阶段：BudgetExhausted 不向上传播，已取得事实按部分报告保留）。
    - 边界处理：超过展示上限（50 条）截取并如实标注总量与截取缺口
      （展示截取，非供应商故障）；非法条目逐条丢弃记缺口；结构性程序
      错误不吞。等待与适配有界均按同一取数截止。
    """
    out: Dict[str, Any] = {
        "items": [], "source": "ifind", "total_returned": 0, "truncated": False,
        "gaps": [],
    }
    from src.config import get_config

    config = get_config()
    if not config.enable_ifind_mcp:
        out["gaps"].append("相关概念：iFinD 概念入口未启用，本次未列示")
        return out
    try:
        from data_provider.ifind_fundamental_adapter import IfindFetcher

        fetcher = IfindFetcher(
            config.ifind_mcp_endpoint, config.ifind_mcp_token,
            config.ifind_mcp_timeout_seconds,
        )
        membership = bounded_call(
            fetcher.concept_membership, code, budget=budget,
            deadline=budget.fetch_deadline,
        )
        raw_names = list(membership.concepts)
        out["total_returned"] = len(raw_names)
        items: List[Dict[str, Any]] = []
        dropped = 0
        for n in raw_names:
            name = str(n).strip()
            if not name or len(name) > 64:
                dropped += 1
                continue
            items.append(
                {"name": name, "source": "ifind",
                 "as_of_nature": str(membership.as_of_nature)}
            )
        if len(items) > _CONCEPT_DISPLAY_CAP:
            out["truncated"] = True
            out["items"] = items[:_CONCEPT_DISPLAY_CAP]
            out["gaps"].append(
                f"相关概念：供应商共返回 {out['total_returned']} 条，"
                f"展示上限 {_CONCEPT_DISPLAY_CAP} 条，其余未列示"
                "（展示截取，非供应商故障）"
            )
        else:
            out["items"] = items
        if dropped:
            out["gaps"].append(
                f"相关概念：供应商返回 {dropped} 条非法条目已丢弃（不作为行业依据）"
            )
        if membership.errors:
            out["gaps"].append(f"相关概念：供应商返回异常 {membership.errors[0]}")
    except BudgetExhausted:
        out["gaps"].append("相关概念：取数预算不足，本次未读取（可选阶段，不影响主报告）")
    except Exception as exc:  # noqa: BLE001
        out["gaps"].append(f"相关概念查询失败: {str(exc)[:60]}（不影响主行业报告）")
    return out


def _bounded_reason(text: str, cap: int = 200) -> str:
    """assessment.reasons 有界汇总：单项 ≤ cap（strict schema 上限）。

    详细诊断保留在对应块（trend/peers/prosper）的 gaps 里；此处只做有界短
    汇总，复合源失败下长缺口不再把整份报告炸成 500/零存档。
    """
    if len(text) <= cap:
        return text
    return text[: cap - 1] + "…"


def _compose_status(ident: Dict[str, Any]) -> Tuple[str, List[str]]:
    """资料状态判定（正常「未评分」与「资料不足」语义分开）：

    - unscored：有可研究主体（板块锁定，成分验证可缺）——首版不产出综合评分
    - insufficient_data：主行业身份缺失或无法唯一锁定——核心问题无法回答
    """
    subject = ident.get("subject_status") or "missing"
    if subject in ("missing", "ambiguous"):
        if subject == "missing":
            reasons = ["主行业身份未取得：识别链未返回行业数据"]
        else:
            reasons = ["主行业身份无法唯一锁定：东财板块表无精确命中或多候选（不近似取首行）"]
        return "insufficient_data", reasons
    return "unscored", []


@require(lambda code: isinstance(code, str) and len(code) == 6 and code.isdigit(),
         "code must be a 6-digit string")
@require(lambda budget: isinstance(budget, ReportBudget) and not budget.expired(),
         "budget must be a live ReportBudget")
@ensure(
    lambda result: (
        isinstance(result, dict)
        and {"payload_version", "stock_code", "stock_name", "generated_at",
             "identity", "background", "concepts", "assessment", "gaps",
             "sector_trend", "sector_peers",
             "sector_catalysts", "sector_synthesis"}
        .issubset(result.keys())
    ),
    "compose_report must return the v2 payload contract",
)
@ensure(
    lambda result: (
        isinstance(result, dict)
        and result.get("sector_trend", {}).get("eligible")
        == (result.get("identity", {}).get("subject_status") == "confirmed")
        and result.get("sector_peers", {}).get("eligible")
        == (result.get("identity", {}).get("subject_status") == "confirmed")
        and result.get("sector_catalysts", {}).get("eligible")
        == (result.get("identity", {}).get("subject_status") == "confirmed")
        and result.get("sector_synthesis", {}).get("eligible")
        == (result.get("identity", {}).get("subject_status") == "confirmed")
    ),
    "trend/peers/catalysts/synthesis eligibility must follow confirmed subject identity "
    "(issues #82/#83)",
)
@ensure(
    lambda result: (
        result["assessment"]["status"] == "unscored"
        and result["identity"]["subject_status"] in ("confirmed", "unverified")
    ) or (
        result["assessment"]["status"] == "insufficient_data"
        and result["identity"]["subject_status"] in ("missing", "ambiguous")
    ),
    "assessment.status must agree with identity.subject_status",
)
@ensure(
    lambda result: (
        result.get("sector_score") is None and result.get("band") is None
        and result.get("beta_meaning") is None and result.get("weights") is None
        and result.get("prosperity_score") is None
    ),
    "legacy composite score/band fields must stay explicitly None (retired)",
)
def compose_report(code: str, name: str, *, budget: ReportBudget) -> Dict[str, Any]:
    """v2 编排：主行业身份 → 趋势/同业（必需新块）→ 可选长链 → 背景支柱 → 概念列示。

    首版新报告不沿用旧综合总分及其分档结论（assessment.score 显式 None）；
    缺数据记缺口不编造；默认基率 0.5 只是先验背景，不单独形成任何「中性」结论。
    编排顺序（issue #82 预算纪律）：趋势/同业必需块先于申万参考/概念等可选
    长链出站，可选阶段耗尽取数预算不会让必需块根本不尝试；趋势/同业自身
    失败/超时只降对应块，已取得内容保留走总截止收尾。

    资料完整性（completeness）与评分政策（status）分开：未评分是评分政策
    （首版不产分），completeness 反映动态事实的实际取得情况——confirmed
    但当日行情全缺、趋势核心未取得或同业样本为空时首屏可见 partial +
    具体原因，不与正常报告无异。

    v1 兼容投影（追加，非整体换形）：``identification``/``policy``/
    ``base_rate``/``prosperity`` 旧事实路径保留可读；评分字段恒为 None
    （显式退役），不恢复旧总分/分档/行动结论。
    """
    ident = _identify_sector(code, budget=budget)
    # 身份阶段快照：板块全表（当日行情复用）+ 成分表（同业复用，同一已验证
    # 快照，避免重复请求与盘中漂移）。均为非载荷键。
    board_snapshot = ident.pop("_board_table", None)
    cons_snapshot = ident.pop("_cons_snapshot", None)
    cons_observed = ident.pop("_cons_observed_at", None)
    cons_batch = ident.pop("_cons_quote_batch", None)
    verified_source = (ident.get("membership") or {}).get("verified_source")
    f10_members_source = verified_source == "f10_members"

    # 必需新块（issue #82）：confirmed 才发出站（支柱内部判定资格），
    # 先于申万参考链等可选长链。
    confirmed = ident["subject_status"] == "confirmed"
    trend = _sector_trend_pillar(
        ident["board_code"] or "", ident["board_name"] or "", budget=budget,
        subject_confirmed=confirmed,
    )
    peers = _peers_pillar(code, cons_snapshot if isinstance(cons_snapshot, pd.DataFrame) else None,
                          budget=budget, snapshot_observed_at=cons_observed,
                          subject_confirmed=confirmed,
                          amount_provenance=(_f10_quote_provenance(cons_batch)
                                             if f10_members_source and cons_batch else None),
                          snapshot_source=(_F10_CONS_SNAPSHOT_SOURCE
                                           if f10_members_source else None),
                          quotes_as_of=(_quote_as_of_note(cons_batch)
                                        if f10_members_source and cons_batch else None),
                          quotes_complete=(cons_batch.amounts_known
                                           if f10_members_source and cons_batch else True))
    # 当日行情先于催化/综合：来自身份阶段同一板块全表快照（本地计算，无新
    # 出站），综合登记表需要 intraday 事实完整。
    prosper = _prosperity_pillar(
        ident["sector_name"], budget=budget, board_code=ident["board_code"] or None,
        board_table=board_snapshot if isinstance(board_snapshot, pd.DataFrame) else None,
    )

    # 必需研究（issue #83）：催化搜索与证据化综合先于申万参考链/概念等可选
    # 长链出站——必需研究不被可选阶段吃掉取数预算；模型综合依赖催化结果。
    catalysts = _catalysts_pillar(
        ident["board_name"] or "", budget=budget, subject_confirmed=confirmed,
        stock_name=name,
    )
    synthesis = _synthesis_pillar(
        name, code, ident, prosper, trend, peers, catalysts,
        budget=budget, subject_confirmed=confirmed,
    )

    # 申万一/二/三级参考链（可选长链后置；失败如实披露，不影响东财主身份）
    lookup_industry = ident["industry_em"] or ident["industry_cninfo"]
    sw: Dict[str, Any] = {"l1": "", "l2": "", "l3": "", "l3_code": "", "gaps": []}
    if lookup_industry:
        sw = _resolve_sw_chain(
            code, f"{lookup_industry} {ident.get('business_scope') or ''}", budget=budget
        )
        ident["gaps"].extend(sw.get("gaps") or [])
    ident["sw_chain"] = sw
    sw_text = " ".join(x for x in (sw.get("l1"), sw.get("l2"), sw.get("l3")) if x)
    lookup_text = " ".join(
        x for x in (ident["industry_em"], ident["industry_cninfo"], sw_text,
                    ident.get("business_scope") or "", ident["sector_name"], name) if x
    )
    policy = _policy_pillar(
        " ".join(x for x in (ident["industry_em"], ident["industry_cninfo"],
                             ident.get("business_scope") or "", sw_text) if x)
    )
    base = _base_rate_pillar(lookup_text)
    # prosper 已在催化/综合之前取得（身份快照本地计算，见上方）；此处不再重复计算
    concepts = _concepts_pillar(code, budget=budget)

    status, reasons = _compose_status(ident)
    if status == "unscored" and ident["subject_status"] == "unverified":
        reasons = ["主行业归属未确认（成分验证未通过）：结论仅基于板块锁定，归属存疑"]

    # 资料完整性依据**有效动态事实**，独立于评分政策（issue #81 第二轮；
    # issue #82 起缺口原因**累积可见**而非短路——行情动量缺失不掩盖趋势/同业
    # 核心缺口）：行存在 ≠ 数据取得——confirmed 但行情字段全缺、归属未确认、
    # 趋势核心未取得或同业样本为空都如实 partial；部分可选维度缺口（如东财
    # 正常表无成交额列、同业样本 4 家、基准失败但板块区间收益已取得）由
    # gaps 如实标注，不机械降级整份报告。政策/基率是静态背景，其缺失由
    # gaps/is_default 标注，不参与完整性判定。
    completeness = "complete"
    if status == "insufficient_data":
        completeness = "partial"  # reasons 已含身份缺失/歧义原因
    if not prosper.get("sector_found"):
        completeness = "partial"
        reason = prosper["gaps"][0] if prosper.get("gaps") else "未知原因"
        reasons = list(reasons) + [_bounded_reason(f"当日板块行情未取得：{reason}")]
    elif prosper.get("momentum") is None:
        completeness = "partial"
        reason = next(
            (g for g in prosper["gaps"] if "动量" in g),
            prosper["gaps"][0] if prosper.get("gaps") else "未知原因",
        )
        reasons = list(reasons) + [
            _bounded_reason(f"当日行情行存在但动态字段未取得：{reason}")
        ]
    if ident["subject_status"] == "unverified":
        # 归属未确认：reasons 已含具体原因（compose 开头设置）
        completeness = "partial"
    if trend.get("eligible") and trend.get("period_return_pct") is None:
        # 趋势核心（板块侧区间收益）未取得（日历不可用/供应商失败/预算不足/
        # 数据不合格）：不全篇伪 complete，已取得内容保留在块内；
        # 短汇总有界（≤schema 单项上限），详细源限制保留 trend.gaps
        completeness = "partial"
        reason = trend["gaps"][0] if trend.get("gaps") else "未知原因"
        reasons = list(reasons) + [_bounded_reason(f"板块近期趋势未取得：{reason}")]
    if peers.get("eligible") and not peers.get("items"):
        completeness = "partial"
        reason = peers["gaps"][0] if peers.get("gaps") else "未知原因"
        reasons = list(reasons) + [_bounded_reason(f"同业样本未取得：{reason}")]
    if catalysts.get("eligible") and not catalysts.get("items"):
        # issue #83：无已证实近期催化（含供应商失败/无日期/无窗口内新闻）如实
        # partial——缺口原因见 catalysts.gaps，背景线索保留但不冒充催化
        completeness = "partial"
        reason = catalysts["gaps"][0] if catalysts.get("gaps") else "未知原因"
        reasons = list(reasons) + [_bounded_reason(f"行业催化未取得：{reason}")]
    if synthesis.get("eligible") and synthesis.get("status") != "ok":
        completeness = "partial"
        reason = synthesis["gaps"][0] if synthesis.get("gaps") else "未知原因"
        reasons = list(reasons) + [_bounded_reason(f"证据化综合未产出：{reason}")]
    if (synthesis.get("eligible") and synthesis.get("status") == "ok"
            and synthesis.get("rejected_count")):
        # issue #83 边界终态：混合输出（部分结论合格 + 部分被拒）不呈现
        # 「全部合格研究」——合格结论与全部服务端事实保留，完整性如实
        # partial 并带显式 rejected-output 原因（计数来自 schema 字段，
        # 不解析 gap 文本；正常全合格输出 rejected_count=0 不受影响）。
        completeness = "partial"
        reasons = list(reasons) + [_bounded_reason(
            f"模型综合存在已拒绝的结论输出（{synthesis['rejected_count']} 条），"
            "完整性不为完整"
        )]

    gaps: List[str] = list(ident["gaps"])
    for block in (policy, prosper, concepts, catalysts, synthesis):
        gaps.extend(block.get("gaps") or [])
    if base["is_default"]:
        gaps.append("行业基率未命中，使用板块中性 0.5（启发式默认先验，仅背景依据，不据此评分）")

    return {
        "payload_version": 2,
        "stock_name": name,
        "stock_code": code,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "identity": ident,
        "background": {
            "policy": policy,       # 静态规则背景（行业 DNA 库）
            "base_rate": base,      # 启发式先验背景（非实测胜率）
            "intraday": prosper,    # 当日行情（价格强弱/活跃度，非产业景气、非净流入）
        },
        "concepts": concepts,       # 仅列示
        "sector_trend": trend,      # 近期趋势（issue #82；confirmed 资格前置）
        "sector_peers": peers,      # 同业比较（issue #82；同一已验证成分快照）
        "sector_catalysts": catalysts,  # 行业催化（issue #83；统一准入）
        "sector_synthesis": synthesis,  # 证据化综合（issue #83；登记表校验）
        "assessment": {
            "status": status,
            "completeness": completeness,  # 资料完整性（与评分政策分开）
            "score": None,          # 首版不产出综合评分（显式未评分）
            "reasons": reasons,
        },
        # ---- v1 兼容投影：旧非评分事实路径保留可读 ----
        "identification": {
            "industry_em": ident["industry_em"],
            "industry_cninfo": ident["industry_cninfo"],
            "business_scope": ident.get("business_scope") or "",
            "sector_name": ident["sector_name"],
            "sw_chain": sw,
            "gaps": ident["gaps"],
        },
        "policy": {
            "lean": policy["lean"], "label": policy["label"], "gaps": policy["gaps"],
            "score": None,  # 退役恒 None：政策 DNA 只保留方向标签，分数易与综合评分混淆
        },
        "base_rate": base,
        "prosperity": {
            "sector_found": prosper["sector_found"],
            "momentum": prosper["momentum"], "activity": prosper["activity"],
            "capital": None,  # 退役：旧「资金」维度实为成交额分位，v2 更名 amount_activity
            "breadth": prosper["breadth"],
            "rank_of": prosper["rank_of"], "total_boards": prosper["total_boards"],
            "row": prosper["row"],
            "gaps": prosper["gaps"],
        },
        # ---- 评分退役：显式 None 语义（不恢复旧总分/分档/行动结论）----
        "sector_score": None,
        "band": None,
        "beta_meaning": None,
        "weights": None,
        "prosperity_score": None,
        "gaps": gaps,
    }


# ---------------------------------------------------------------------------
# 报告生成入口
# ---------------------------------------------------------------------------

def generate_report(raw_code: str, raw_name: Optional[str] = None) -> Dict[str, Any]:
    """生成个股板块分析专项报告（v2 载荷，issue #81）。

    终态三分（页面等待超时、请求断开与处理终态不混用）：
    - success：报告已渲染并实际存档（部分报告同样存档，缺口随载荷透出）
    - timeout：报告总预算耗尽 —— 不存档、不留文件（含渲染后/写入后/保存期间
      耗尽）；已放弃等待的供应商在途调用可能仍在进行，其结果不会被采纳
      （等待钳制不伪装硬截止）
    - failed：报告已生成但落库失败 —— 清理本请求独占创建的文件，不留孤儿、
      不返回成功

    提交边界：严格载荷校验先于任何存档动作（写文件/写库都不允许带病提交）；
    渲染前、渲染后、写入后、DB 提交边界分别检查/钳制同一主截止（写入与
    SQLite 锁等待/提交共用 ``budget.deadline``），过期即终止不保存伪成功。
    报告 ID/文件经 ``_claim_report_file`` 原子独占，失败只清理本请求拥有的
    新文件，不动他人存档。

    代码/市场资格错误抛 :class:`SectorInputError`（API 层 400），不落入以上
    终态；其它 ``ValueError``（含新 source 传输层程序缺陷）不属于输入错误，
    原样传播走通用 500。
    """
    from src.config import get_config
    from src.services.stock_code_utils import normalize_code

    raw = str(raw_code or "").strip()
    code = normalize_code(raw) if raw else ""
    if not code or not code.isdigit() or len(code) != 6:
        raise SectorInputError(f"仅支持 A 股 6 位代码：{raw_code}")
    name = (raw_name or "").strip()
    if name and code in name:
        name = name.replace(code, "").replace(".SZ", "").replace(".SH", "").strip()
    budget_seconds = float(get_config().sector_analysis_report_timeout_seconds)
    # 收尾余量随合法预算缩放（issue #81 第二轮）：固定 5s 会把 1—5s 的合法
    # 小预算取数时间直接吃光（fetch 截止在起点即过期）；小预算下留一半给取数。
    budget = ReportBudget(
        budget_seconds,
        finish_margin=min(_FINISH_MARGIN_SECONDS, budget_seconds / 2.0),
    )
    if not name:
        # 无显式名称：名称如实用代码，不为补名打开共享 manager——其内部多源
        # fallback 不感知预算（反例实测第二源在取数截止后 +0.10s 启动新出站）；
        # 页面与常规调用均显式携带 stock_name。
        name = code

    try:
        payload = compose_report(code, name, budget=budget)
    except BudgetExhausted as exc:
        logger.warning("[SectorAnalysis] 生成超时（%s）: %s", code, str(exc)[:80])
        return {"status": "timeout", "report_id": None, "stock_code": code,
                "stock_name": name, "markdown": None, "payload": None,
                "message": "生成超时：板块报告总预算（"
                           f"{budget.total_seconds:.0f} 秒）已耗尽，本次未保存报告；"
                           "可稍后重试（供应商在途调用可能仍在进行，其结果不会被采纳）"}

    # 提交边界 1：严格载荷校验先于任何存档动作（写文件/写库都不带病提交）。
    # 校验失败属于程序错误：不存档、不留文件，API 层按 500 报告。
    try:
        SectorReportPayload.model_validate(payload)
    except ValidationError as exc:
        logger.error("[SectorAnalysis] 载荷契约校验失败（%s）: %s", code, str(exc)[:300])
        return {"status": "failed", "report_id": None, "stock_code": code,
                "stock_name": name, "markdown": None, "payload": None,
                "message": "报告载荷契约校验失败，未存档（程序错误，请反馈）"}

    if budget.expired():  # 提交边界 2：只在主截止内渲染并保存，不采纳晚到结果
        return {"status": "timeout", "report_id": None, "stock_code": code,
                "stock_name": name, "markdown": None, "payload": None,
                "message": "生成超时：板块报告总预算（"
                           f"{budget.total_seconds:.0f} 秒）在存档前耗尽，本次未保存报告；"
                           "可稍后重试"}

    claimed = _claim_report_file(_ID_PATTERN.format(ts=datetime.now()))
    if claimed is None:
        return {"status": "failed", "report_id": None, "stock_code": code,
                "stock_name": name, "markdown": None, "payload": None,
                "message": "无法分配报告文件（同分钟存档已满），未存档；请重试"}
    rid, md_path = claimed
    saved_over_deadline = False
    try:
        md = _render(name, code, payload["generated_at"], rid, payload)
        if budget.expired():  # 提交边界 3：渲染可能耗时，保存前再查主截止
            md_path.unlink(missing_ok=True)
            logger.warning("[SectorAnalysis] 渲染后预算耗尽（%s），不保存", code)
            return {"status": "timeout", "report_id": None, "stock_code": code,
                    "stock_name": name, "markdown": None, "payload": None,
                    "message": "生成超时：板块报告总预算（"
                               f"{budget.total_seconds:.0f} 秒）在存档前耗尽，本次未保存报告；"
                               "可稍后重试"}
        try:
            md_path.write_text(md, encoding="utf-8")
        except OSError as exc:
            # 写入失败：只清理本请求独占创建的文件，不留孤儿、不返回成功
            logger.warning("[SectorAnalysis] 报告文件写入失败（%s）: %s", rid, exc)
            md_path.unlink(missing_ok=True)
            return {"status": "failed", "report_id": None, "stock_code": code,
                    "stock_name": name, "markdown": None, "payload": None,
                    "message": "报告已生成但保存失败，未存档；请重试"}
        # 提交边界 4（issue #81 第二轮）：写入本身可能迟到（慢盘/负载），
        # 写后复核主截止——过期即清理本请求独占文件，不开始 DB 保存。
        if budget.expired():
            md_path.unlink(missing_ok=True)
            logger.warning("[SectorAnalysis] 文件写入后预算耗尽（%s），不开始 DB 保存", code)
            return {"status": "timeout", "report_id": None, "stock_code": code,
                    "stock_name": name, "markdown": None, "payload": None,
                    "message": "生成超时：板块报告总预算（"
                               f"{budget.total_seconds:.0f} 秒）在存档期间耗尽，"
                               "本次未保存报告；可稍后重试"}
        record = {
            "id": rid, "stock_code": code, "stock_name": name,
            "md_path": str(md_path),
            "sector_score": None,  # v2 不产出综合评分；旧列保留给存量记录
            "analysis_json": json.dumps(payload, ensure_ascii=False, default=str),
        }
        from src.storage import get_db

        # 提交边界 5：DB 保存与同一主截止同界——锁等待钳制剩余时间、提交前到期
        # rollback（有界同步提交能力边界见 save_sector_analysis_report 文档）。
        if get_db().save_sector_analysis_report(record, deadline=budget.deadline):
            return {"status": "success", "report_id": rid, "stock_code": code,
                    "stock_name": name, "markdown": md, "payload": payload,
                    "message": ""}
        saved_over_deadline = budget.expired()
    except Exception:  # noqa: BLE001 - 渲染/编排程序错误：清理本请求文件后向上传播
        md_path.unlink(missing_ok=True)
        raise
    # 保存失败：清理本请求独占创建的文件，不留孤儿、不返回成功。
    # 总截止在保存期间耗尽（锁等待/提交前到期）→ timeout；其余 → 保存故障，
    # 两者在响应消息中如实区分（供应商超时/取数截止/总截止/保存故障分层）。
    md_path.unlink(missing_ok=True)
    if saved_over_deadline:
        logger.warning("[SectorAnalysis] 保存期间总预算耗尽（%s），未提交", code)
        return {"status": "timeout", "report_id": None, "stock_code": code,
                "stock_name": name, "markdown": None, "payload": None,
                "message": "生成超时：板块报告总预算（"
                           f"{budget.total_seconds:.0f} 秒）在保存期间耗尽，"
                           "本次未提交存档；可稍后重试"}
    return {"status": "failed", "report_id": None, "stock_code": code,
            "stock_name": name, "markdown": None, "payload": None,
            "message": "报告已生成但保存失败，未存档；请重试"}


def _md_link_text(title: str) -> str:
    """供应商标题投影到 GFM 表格内链接文本的字面转义（渲染层，不改数据）。

    真实标题可含字面 ``|``（如「去库存| 财经日日评」）：GFM 表格把它当列
    定界符，日期/口径/来源列整体左移、末列来源被丢弃。只在 Markdown 投影
    时转义；payload/存档 JSON 中标题原文保持不动：

    - ``\\`` 先转义：标题已有反斜杠时，后续插入的 ``\\|``/``\\[``/``\\]``
      不会与已有反斜杠重组出新的定界符或终止符（``政策\\]解读`` 若只加倍
      反斜杠，``\\`` 渲染为字面反斜杠后紧随的 ``]`` 会提前终止链接文本，
      adopted URL 的 anchor 由 1 变 0——先转义即消除该奇偶交互）；
    - ``|`` → ``\\|``：单元格内渲染为字面竖线，永不成为列边界；
    - ``[``/``]`` → ``\\[``/``\\]``：方括号是链接文本定界符，字面化后标题
      完整保留在单一原始 href anchor 内（``政策\\[解读| 资讯`` 的前缀不再
      脱落到链接外），既有可访问引用投影不回退；
    - CR/LF 折叠为空格：表格行是单行投影，原始换行会把行拆断。
    """
    return (
        title.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def _render(name: str, code: str, as_of: str, rid: str, a: Dict[str, Any]) -> str:
    from jinja2 import Environment, FileSystemLoader

    env = Environment(
        loader=FileSystemLoader(Path(__file__).parent.parent.parent / "templates"),
        autoescape=False, trim_blocks=True, lstrip_blocks=True,
    )
    env.filters["md_link_text"] = _md_link_text
    md = env.get_template("sector_analysis_report.j2").render(
        stock_name=name, stock_code=code, as_of=as_of, report_id=rid, a=a,
    )
    # 排版后处理：盘古空格（中英间），保留标准 Markdown。
    md = re.sub(r"(?<=[\u4e00-\u9fff])(?=[A-Za-z0-9])", " ", md)
    md = re.sub(r"(?<=[A-Za-z0-9%])(?=[\u4e00-\u9fff])", " ", md)
    return md
