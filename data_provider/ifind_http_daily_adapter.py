# -*- coding: utf-8 -*-
"""iFinD 标准 HTTP API 现金分红前复权日线候选（issue #91 施工边界）。

在既有 MCP 日线（issue #86）不可用或失败时，于东财等既有候选链之前提供
标准 HTTP 同口径整窗候选，减少深研日线对东财公开接口的依赖。与 MCP 路径
复用同一套资格边界（证券身份、严格交易日历全窗口、Decimal 金融口径、
跨字段不变式、空值/停牌不放宽），差异仅在传输与元数据来源：

- 传输：``POST https://quantapi.51ifind.com/api/v1/cmd_history_quotation``，
  header ``access_token`` 携带经 refresh_token 自动换取的凭据（
  ``IFIND_HTTP_REFRESH_TOKEN``，与 MCP ``IFIND_MCP_TOKEN`` 独立，绝不混
  用）；一次尝试两笔出站（CPS=6 取数 + CPS=1 单位对照，同一有限 deadline
  与凭据），无重试。传输为本机 curl 子进程（复用 MCP 导出的既有可中断
  模式）：``--max-time`` 硬截止 + 父进程 selectors 读循环 20ms 逐轮停止/
  deadline 检查 + ``finally`` kill 回收——socket read 不可能跨越绝对
  deadline，用户取消即时生效（第十轮：requests 的 iter_content 每块返回
  后才检查，慢 header/慢 body/滴流实测超期 ~0.4s 且取消需等读取超时，已
  整体替换）。凭据只进 0600 临时 header 文件（finally 删除），不出现在
  argv/日志/stdin；子进程环境只继承 PATH，正常 TLS、不跟随重定向、不重试。
- 复权口径：请求 ``functionpara`` 恒为 ``CPS=6``（官方取值 6-前复权(现金
  分红)），与 MCP 日线同一现金分红前复权口径。MCP 靠供应商自然语言回显
  复权方式，HTTP 参数为结构化枚举；官方协议的响应结构（errorcode/tables/
  thscode/time/table）不含复权元数据回显，口径由请求参数与真实验收（双
  CPS 量额对照 + 东财对照）承载。
- 量额单位：官方文档对 volume/amount 单位零声明（手册/FAQ/权限页均无），
  且不存在静态常量可替代逐窗口证据。曾实现的「成交均价 amount/volume ∈
  [low×0.5, high×2]」比例校验已删除——它比较复权后 high/low 与未复权
  amount/volume，窗口跨多次除权后累计复权偏离使合法行的真实成交均价越出
  比例界（官方 FAQ：现金分红复权涉及加减与乘除迭代运算，无简单复权因子；
  合成反例：真实 VWAP=3.50、累计因子≈0.45 → 前复权 high≈1.62，比例界上
  界 3.24 不含合法值 3.50）。现行资格是**逐窗口双请求出证**：同窗口再取
  一份 CPS=1（不复权，官方取值表默认值）对照响应，要求两响应日期集合一
  致、量额逐日精确相等（量额不随复权变动）、且未复权成交均价
  amount/volume ∈ 当日**未复权** [low, high]（一字板闭区间成立）。价格
  比较只用未复权行——复权价与未复权 VWAP 不可比。任一环节不满足即整窗
  拒绝走兜底；量额缺失/非法由既有 ``_require_decimal`` 拒绝路径兜底。
  **判据的能力边界（勿夸大）**：双请求只能证明「量额互一致、未复权、
  amount/volume 是每股价格这一**比率**」——把 volume 与 amount 以同一倍
  率缩放（例如各 ×100，即 百股/百元 量纲）VWAP 不变且两口径仍一致，判据
  照样通过，**不能证明绝对单位是 股/元**（反例测试
  ``test_unit_qualification_cannot_detect_proportional_scaling`` 锁定）。
  绝对单位的现有证据是东财 250 日对照（qualification 报告，600519.SH/
  600031.SH 两股量额逐日一致——东财链路在本管线中量纲为 股/元）；该证
  据未外推为全市场结论，其他证券/新市场的绝对量纲仍是残余缺口，随文档
  与交付披露，不静默假定。真实验收证据（协调者于官方授权会话内实测）：
  600519.SH/600031.SH 各 2025-11-03~2026-11-12 共 250 日，CPS=6 与不复
  权量额逐日完全一致、未复权 VWAP 全部落在当日区间；实现侧离线 fixture
  同语义覆盖。

标准 API 凭据缺失或未显式启用时该候选不出站，既有候选链行为不变。凭据
契约（官方 FAQ + 官方示例页 example.html「HTTP接口应用案例」逐字依据，
第六轮更正：此前「公开资料无换取端点」结论有误——示例页明确给出
``POST https://quantapi.51ifind.com/api/v1/get_access_token``，header
``{"Content-Type": "application/json", "refresh_token": <长期令牌>}``、
无请求体，响应 ``data.access_token``（实测另含 ``expired_time``））：
access_token 有效期 7 天、失效后经该端点重新获取并顺延 7 天，
refresh_token 长期有效（网页版超级命令账号详情可获取）。唯一凭据模式
（第八轮按最小接入移除静态 access_token 双模式）：配
``IFIND_HTTP_REFRESH_TOKEN``——进程内缓存 access_token 并在过期/401 时
经官方端点续用（stream=True 流式有界读取、分段可取消等待、并发单飞、
真实异常脱敏；不调用 update access token、不改变 refresh_token）。
凭据只进 header、不落日志/仓库/报告；expired_time 官方无格式文档，解析
按防御回退（7 天规则留余量）。历史行情函数取值年限：试用/免费账号仅最
近 5 年（官方权限页），更长窗口按供应商拒绝走兜底。
"""

from __future__ import annotations

import json
import math
import re
import tempfile
import threading
import time as _time_module
from dataclasses import dataclass
from datetime import date as _date, datetime as _datetime
from decimal import Decimal
from time import monotonic
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from data_provider.ifind_fundamental_adapter import CompanyProfile, ConceptMembership

import pandas as pd

from .base import BaseFetcher, DataFetchError
from .ifind_daily_adapter import (
    _IDENTITY_RE,
    _MAX_EXPORT_BYTES,
    _MAX_HEADER_BYTES,
    _curl_child_env,
    _parse_date,
    _qualify_window,
    _ready_export_parts,
    _split_included_http,
    check_attempt_alive,
    daily_routing,
    qualified_daily_identity,
    standardize_daily_frame,
)

# 官方 THS_HQ 历史行情端点与认证 header；标准 API 凭据与 MCP 凭据互不通用。
_HTTP_ENDPOINT = "https://quantapi.51ifind.com/api/v1/cmd_history_quotation"
_HTTP_INDICATORS = "open,high,low,close,volume,amount"
# 官方 CPS 取值 6 = 前复权(现金分红)；与 MCP 日线复权口径一致。
_CASH_QFQ_CPS = "6"
# 官方 CPS 取值 1 = 不复权（取值表缺省值）；逐窗口量额单位资格的对照口径。
_UNADJUSTED_CPS = "1"
# 官方示例页「HTTP接口应用案例」的 token 换取端点与请求形态（第六轮更正：
# 此前「公开资料无换取端点」结论有误）：POST + header refresh_token、无请求
# 体，响应 data.access_token（实测另含 expired_time，官方无格式文档）。
_TOKEN_ENDPOINT = "https://quantapi.51ifind.com/api/v1/get_access_token"
# 过期提前量与 expired_time 缺失/不可解析时的保守回退 TTL（官方 7 天有效期
# 留 1 天余量）：宁可早刷新，不冒进使用可能过期的凭据。
_TOKEN_REFRESH_MARGIN_SECONDS = 60.0
_TOKEN_FALLBACK_TTL_SECONDS = 6 * 86400.0
# 锁等待分段轮询间隔：每段醒来经既有停止契约检查取消/超时（第八轮）。
_TOKEN_WAIT_POLL_SECONDS = 0.1
# 无外层 deadline 时 curl 子进程的 --max-time 上限（与配置超时同量级兜底）。
_HTTP_CURL_MAX_TIME_FALLBACK = 30.0
_HTTP_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 官方英文指标列 → 既有校验管线的中文规范字段（issue #86 同一套资格链）。
_COLUMN_TO_FIELD = {
    "open": "开盘价",
    "high": "最高价",
    "low": "最低价",
    "close": "收盘价",
    "volume": "成交量",
    "amount": "成交额",
}
# 官方 FAQ：单个函数 QPS 限 10、账号总限 20；本候选一次尝试两笔顺序出站
# 且无重试（单账号并发远低于限频），无需限频器。
# 无外层 deadline 时自建绝对上限的解析阶段余量（HTTP 无导出下载阶段）。
_HTTP_PARSE_STAGE_SECONDS = 5.0


@dataclass
class _AccessTokenEntry:
    """进程内 access_token 缓存条目（wall-clock 过期，凭据不落盘）。

    缓存按凭据身份分键（dict key = refresh_token 的截断 SHA-256 指纹，不
    可逆、不含凭据明文）：运行时配置变更/并发不同凭据不误用他账号 token；
    ``fetched_at`` 供并发 force_refresh 去重。
    """

    access_token: str
    expires_at: float
    fetched_at: float = 0.0


_token_cache_lock = threading.Lock()
_token_cache: Dict[str, _AccessTokenEntry] = {}


def _credential_fingerprint(refresh_token: str) -> str:
    """refresh_token → 截断 SHA-256 指纹（不输出、不持久化凭据明文）。"""
    import hashlib

    return hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()[:16]


def _parse_expired_at(raw: object, now: float) -> float:
    """官方 ``expired_time``（无格式文档）→ Unix 秒；不可解析按 7 天回退。

    兼容数值秒（1e8~1e11）、数值毫秒（>1e12）与 ISO 日期/日期时间字符串；
    其余（缺失/0/负值/乱码）一律回退保守 TTL，宁可早刷新不冒进。
    """
    try:
        if isinstance(raw, bool) or raw is None:
            raise ValueError("missing expired_time")
        if isinstance(raw, (int, float, Decimal)):
            value = float(raw)
        else:
            text = str(raw).strip()
            if not text:
                raise ValueError("empty expired_time")
            try:
                value = float(Decimal(text))
            except ArithmeticError:
                return _datetime.fromisoformat(text).timestamp()
        if value > 1e12:  # 毫秒时间戳
            value /= 1000.0
        if 1e8 <= value <= 1e11:  # 合理秒区间
            return value
        raise ValueError("expired_time out of plausible range")
    except (ValueError, ArithmeticError, OverflowError, TypeError):
        return now + _TOKEN_FALLBACK_TTL_SECONDS


def _fetch_access_token(refresh_token: str) -> _AccessTokenEntry:
    """一次出站换取 access_token（官方示例形态：header refresh_token、无体）。

    curl 可中断传输（硬截止、无重试）；errorcode≠0 按供应商错误拒绝并保留
    脱敏 errmsg（防裸回显 refresh_token 落日志）。
    """
    headers = {"Content-Type": "application/json", "refresh_token": refresh_token}
    status, body = _http_api_curl_post(_TOKEN_ENDPOINT, headers, None, label="token refresh")
    if status != 200:
        raise DataFetchError(
            f"ifind http daily token refresh http status {status}"
        )
    payload = _parse_json_payload(body, label="token refresh")
    _require_zero_errorcode(payload, "token refresh", access_token=refresh_token)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise DataFetchError("ifind http daily token refresh response carries no data object")
    token = str(data.get("access_token") or "").strip()
    if not token:
        raise DataFetchError("ifind http daily token refresh response carries no access_token")
    return _AccessTokenEntry(
        access_token=token,
        expires_at=_parse_expired_at(data.get("expired_time"), _time_module.time()),
    )


def _resolve_access_token(refresh_token: str, *, force_refresh: bool = False) -> str:
    """进程内缓存 + 并发单飞的 access_token 解析（自动模式）。

    - 缓存**按凭据指纹分键**：不同 refresh_token（运行时配置变更/并发不同
      账号）各自缓存命中，不误用他账号 token；
    - 命中未过期（含 60 秒缓冲）直接复用、零出站；过期/强制/无缓存时换取，
      换取失败按供应商错误传播（该次窗口尝试失败走兜底），不吞异常、不
      缓存失败结果；
    - 锁等待**分段可取消**（第八轮）：每 0.1 秒醒来经 ``check_attempt_alive``
      响应用户停止（ResearchStopped）与 deadline 耗尽（TimeoutError）——
      持锁网络请求期间等待者可被取消，无外层 deadline 时停止即可退出，
      不再无限等待；
    - 并发 force_refresh **去重**：进入时刻之后已有其他等待者完成换取
      （缓存 ``fetched_at`` 更新）则复用该结果，不重复出站。只调用官方
      get_access_token，不触碰 update access token、不改变 refresh_token。
    """
    from src.patches.eastmoney_patch import request_deadline

    fingerprint = _credential_fingerprint(refresh_token)
    entered_at = _time_module.time()
    deadline = request_deadline.get()
    if deadline is not None and deadline <= monotonic():
        raise TimeoutError("daily attempt deadline exhausted before token wait")
    # 分段可取消等待（第八轮）：每段短超时醒来，经 ``check_attempt_alive``
    # 响应用户停止（ResearchStopped）与 deadline 耗尽（TimeoutError）——
    # 持锁网络请求期间等待者可被取消；无外层 deadline 时不再无限等待
    # （停止即可退出），不再出现「一次 acquire 整段 deadline」的死等。
    while True:
        check_attempt_alive()
        if _token_cache_lock.acquire(timeout=_TOKEN_WAIT_POLL_SECONDS):
            break
    try:
        entry = _token_cache.get(fingerprint)
        if (
            entry is not None
            and entry.expires_at - _TOKEN_REFRESH_MARGIN_SECONDS > _time_module.time()
        ):
            if not force_refresh or entry.fetched_at >= entered_at:
                check_attempt_alive()
                return entry.access_token
        new_entry = _fetch_access_token(refresh_token)
        new_entry.fetched_at = _time_module.time()
        _token_cache[fingerprint] = new_entry
        check_attempt_alive()
        return new_entry.access_token
    finally:
        _token_cache_lock.release()


def _redaction_secrets(refresh_token: str, token_used: str) -> Tuple[str, ...]:
    """本次请求的脱敏集合：refresh_token + 实际使用的 access_token（无锁）。

    supplier errmsg 回显的是**本请求 header 携带**的凭据——只需覆盖本次
    实际使用的 access_token（``_post_history_query_authorized`` 返回的
    ``token_used``，刷新重试后为续用值）与本机 refresh_token，无需遍历共
    享缓存（第九轮：删除解析路径对 token 锁的依赖——原 ``_local_secrets``
    以 ``with lock`` 遍历缓存，另一任务持锁换取网络时解析段不可取消）。
    纯函数、零共享状态；请求链上的锁只存在于 ``_resolve_access_token``
    （分段可取消等待）一处。
    """
    return tuple(secret for secret in (refresh_token, token_used) if secret)


class _HttpAuthError(DataFetchError):
    """HTTP 401/403：凭据被供应商拒绝（触发单次刷新重试）。"""


def _post_history_query_authorized(
    identity: str,
    start: _date,
    end: _date,
    *,
    cps: str,
    refresh_token: str,
) -> Tuple[Dict[str, object], str]:
    """带凭据解析与 401/403 单次刷新重试的取数 POST（issue #91 第六轮）。

    返回 ``(payload, token_used)``——后者是本次请求**实际携带**的
    access_token（刷新重试后为续用值），供解析路径无需锁即可精确脱敏
    （第九轮）。先取缓存/换取的 access_token 出站；被 401/403 拒绝时强制
    续用一次并重试**同一请求一次**（不改变 refresh_token、不调用 update
    access token）；重试仍被拒绝则指向 refresh_token 有效性/账号权限，
    原始异常保留为诊断上下文；刷新后的 5xx/网络/超时保留真实原因原样传播
    （第七轮）。非鉴权失败（资格/供应商业务错误）不在此捕获，原因保真向
    上传播。
    """
    token = _resolve_access_token(refresh_token)
    try:
        return _post_history_query(identity, start, end, access_token=token, cps=cps)
    except _HttpAuthError:
        fresh = _resolve_access_token(refresh_token, force_refresh=True)
        try:
            return _post_history_query(identity, start, end, access_token=fresh, cps=cps)
        except _HttpAuthError as exc:
            raise DataFetchError(
                "ifind http daily authentication failed after token refresh; "
                "请检查 IFIND_HTTP_REFRESH_TOKEN 有效性与账号权限"
            ) from exc


def ifind_http_daily_enabled() -> bool:
    """既有 Choice profile + ``ENABLE_IFIND_HTTP_API`` + refresh_token 才启用。

    与 ``ifind_daily_enabled`` 同一启用结构：标准 HTTP 凭据独立于 MCP
    endpoint/token，两者互不充当对方的启用条件。量额单位不做静态放行——
    每个待采纳窗口在 ``_qualify_volume_unit_consistency`` 中现场出证。
    """
    from src.config import get_config

    config = get_config()
    if config.data_source_profile != "choice" or not config.enable_ifind_http_api:
        return False
    refresh = (getattr(config, "ifind_http_refresh_token", None) or "").strip()
    timeout = float(getattr(config, "ifind_http_timeout_seconds", 0) or 0)
    return bool(refresh) and math.isfinite(timeout) and timeout > 0


# 资格段与证据边界（第十九轮按协调者采纳的段级代表性方案扩大，删除第十一
# 轮 8 股白名单）：HTTP 候选资格 = ``qualified_daily_identity`` 的沪深普通
# 股资格段（沪 6 开头 + 深 000/001/002/003/300/301），ETF/指数（含
# 000001.SH 显式身份）/B 股/北交所/港美股/格式错误仍在既有资格层拒绝。
# 量纲绝对单位（股/元）证据为**代表样本级**，不是全市场或接口契约证明：
# 沪市交易所官方数据（query.sse.com.cn 概况接口）603259@2024-07-03 单股
# 单日精确锚 + 10 只×2 个交易日量纲锚，深市交易所快照 4 只 amount 粗量纲
# 锚（.claude/reviews/ifind-official-unit-crosscheck-20261116.md）。沪市
# 「6 开头」资格覆盖 689（CDR）等未采样前缀，深市各段同理——未采样标的
# 仅由运行时逐窗口门（双 CPS 量额一致 + 未复权 VWAP 区间）与供应商间一致
# 性支撑；量额**同倍率缩放**不可被运行时校验检测（既有盲区锁定测试）；
# 605499 除息前后前复权价与第三方存在未定源差异。任一窗口校验不满足即整
# 窗拒绝走既有兜底（东财兜底保留），差异验证不宣称精确等价。
def http_qualified_identity(raw_code: str) -> Optional[str]:
    """标准 HTTP 候选的证券资格：MCP 共享的沪深普通股资格段。

    段级代表性方案（第十九轮）：资格判定与 ``qualified_daily_identity``
    完全一致；逐股白名单已删除，量纲证据边界见上方模块注释。原始交易所
    显式身份语义不变（``000001.SH`` 上证指数在既有资格层即 None）。
    """
    return qualified_daily_identity(raw_code)


def http_daily_routing(raw_code: str, end_date: Optional[str]) -> Optional[Tuple[str, str]]:
    """HTTP 候选的路由决策（生产入口）：既有普通股资格段边界。"""
    if http_qualified_identity(raw_code) is None:
        return None
    return daily_routing(raw_code, end_date, enabled=ifind_http_daily_enabled)


IFIND_HTTP_DAILY_SOURCE_NAME = "IfindHttpDailyFetcher"


def is_ifind_http_daily_source(source: object) -> bool:
    """来源标签是否为标准 HTTP 日线（区别于 MCP 的 ``IfindDailyFetcher``）。

    第二十四轮共享表契约：HTTP 日线行不落跨业务共享 ``StockDaily`` 表
    （``save_daily_data`` 存储层统一过滤，单一判定来源即本函数）——旧源与
    MCP 来源行不受影响，深研 run 内同源复用/扩窗走内存共享窗口、不依赖
    该持久化。精确匹配 fetcher 名，不子串匹配，避免误伤同含 "ifind" 的
    MCP 来源标签。
    """
    return str(source or "").strip() == IFIND_HTTP_DAILY_SOURCE_NAME


def http_financial_participation_active() -> bool:
    """HTTP 财务参与形态判定（缓存身份段的单一判定源）。

    **与实际出站资格完全同判定**：``ifind_http_daily_enabled()``（choice
    profile + 开关 + refresh_token + 有效 timeout）**且**个股深研执行上下文
    （``http_preset_scope_active``）。第八轮审查：仅查开关+run 时，凭据补齐
    /撤去不改变判定——撤凭据后调用已回 MCP 而缓存身份段仍为「参与」，会
    命中错误身份旧快照；本判定随资格实时收敛，凭据切换两方向身份同步。
    所有承载「HTTP 财务参与来源差异」的缓存身份（manager 进程内键、阶段 0
    持久快照键、维度持久缓存键）必须用本函数分段：参与形态与不参与形态
    互不串用，非深研/HTTP 关闭/凭据缺失形态不因开关分裂。
    """
    return ifind_http_daily_enabled() and http_preset_scope_active()


def http_preset_scope_active() -> bool:
    """HTTP 候选（manager 预置与读取范围贡献）仅个股深研执行上下文参与。

    判定复用 ``current_research``（ResearchRun 上下文变量，第二十二轮）：
    个股深研编排经 ``run.bind()`` 设置并随线程/任务传播（扩窗换源约束同在
    该上下文内）。设置点核实清单——个股深研编排器（``orchestrator.bind``）
    与深研服务入口是个股深研语境；板块分析在 LLM 调用线程内 set
    （``sector_analysis_service``），与日线获取线程隔离，不把板块分析误判
    为个股深研；预警/回测/罗盘/定时任务等非深研链路无 ResearchRun。新增
    ResearchRun 设置点时须复审本判定。非深研调用返回 False：HTTP 预置不
    出站、读取范围不由 HTTP 贡献（MCP 语义独立、保持原样）。
    """
    from src.core.research_run import current_research

    return current_research.get() is not None


def http_attempt_deadline(now: Optional[float] = None) -> Optional[float]:
    """HTTP 一次完整尝试的有限绝对 deadline（镜像 ``attempt_deadline``）。

    取外层 request_deadline 与「标准 API 配置超时 + 解析余量」自建上限（再
    为既有后续源预留 fallback 份额）的最小值。
    """
    from src.config import get_config
    from src.patches.eastmoney_patch import request_deadline

    start = monotonic() if now is None else now
    cap = float(getattr(get_config(), "ifind_http_timeout_seconds", 30.0) or 0.0)
    if cap <= 0:
        return request_deadline.get()
    budget = cap + _HTTP_PARSE_STAGE_SECONDS
    reserve = min(8.0, budget * 0.3)
    self_deadline = start + max(budget - reserve, 1.0)
    outer = request_deadline.get()
    return min(self_deadline, outer) if outer is not None else self_deadline


def _reject_http_api_bounds(raw: bytes, label: str) -> None:
    """父进程数 header/body 字节：声明或实际超限立即拒绝，不等 curl 下完。"""
    ready = _ready_export_parts(raw)
    if ready is None:
        if len(raw) > _MAX_HEADER_BYTES:
            raise DataFetchError(f"ifind http daily {label} header bound exceeded")
        return
    _status, headers, body = ready
    declared = headers.get("content-length")
    if declared is not None:
        try:
            expected = int(declared)
        except ValueError as exc:
            raise DataFetchError(f"ifind http daily {label} content-length invalid") from exc
        if expected < 0 or expected > _MAX_EXPORT_BYTES:
            raise DataFetchError(f"ifind http daily {label} content-length exceeds bound")
    if len(body) > _MAX_EXPORT_BYTES:
        raise DataFetchError(f"ifind http daily {label} byte bound exceeded")


# curl write-out 传输诊断元数据：经 ``%{stderr}``（curl ≥7.75）重定向到
# 独立 stderr 通道，不占响应正文配额；正文边界检查只看 stdout（响应字节
# 语义不变）。字段白名单见 ``_parse_curl_meta``；原始 stderr 错误文本只排
# 空不记录（curl 退出码承载失败语义）。硬截止/取消杀进程时帧可能来不及输
# 出——缺项如实不可得，不为等统计数据延长请求。
_CURL_META_SENTINEL = "__IFIND_CURL_META__"
_CURL_WRITEOUT_FORMAT = (
    f"%{{stderr}}\n\n{_CURL_META_SENTINEL}%{{http_code}}|%{{remote_ip}}|%{{time_namelookup}}|"
    f"%{{time_connect}}|%{{time_appconnect}}|%{{time_starttransfer}}|%{{time_total}}|%{{exitcode}}"
)
# stderr 诊断通道有界上限：write-out 帧（~100B）+ curl 错误行绰绰有余；
# 超限丢弃后续（诊断降级，不影响传输语义）。
_CURL_META_STDERR_MAX = 8192
_CURL_META_FIELD_LIMIT = 64
_META_NUMERIC_RE = re.compile(r"\d+(?:\.\d+)?")
_META_STATUS_RE = re.compile(r"\d{3}")
_META_EXIT_RE = re.compile(r"-?\d+")
_REQUEST_ID_RE = re.compile(r"[^A-Za-z0-9._-]")


def _split_curl_meta(raw: Optional[bytes]) -> Optional[bytes]:
    """stderr 通道 write-out 帧提取（哨兵 rfind，前置错误文本不干扰）。

    帧缺失（硬截止/取消被杀、旧 curl 输出字面占位、stderr 超限截断）返回
    None——缺项如实不可得；哨兵撞车取最后一次出现（真帧在流末尾）。
    """
    if not raw:
        return None
    idx = raw.rfind(_CURL_META_SENTINEL.encode("utf-8"))
    if idx < 0:
        return None
    return raw[idx + len(_CURL_META_SENTINEL):]


def _curl_meta_ip_plausible(value: str) -> bool:
    import ipaddress

    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _parse_curl_meta(raw: Optional[bytes]) -> Dict[str, Optional[str]]:
    """write-out 元数据白名单解析：字段限长 + 格式校验。

    字段数不符（部分输出/被杀后残段）整段不可得；逐字段格式校验失败（旧
    curl 字面 ``%{exitcode}``、非法数值、非法 IP、超长回显）该字段不可得，
    不抛异常——诊断采集失败不得覆盖原异常或令失败变成功。
    """
    if not raw:
        return {}
    text = raw.decode("ascii", errors="replace").strip()
    keys = ("http_status", "remote_ip", "dns_seconds", "tcp_seconds", "tls_seconds",
            "ttfb_seconds", "total_seconds", "curl_exit")
    parts = text.split("|")
    if len(parts) != len(keys):
        return {}
    out: Dict[str, Optional[str]] = {}
    for key, value in zip(keys, parts):
        value = value.strip()[:_CURL_META_FIELD_LIMIT]
        if not value:
            out[key] = None
        elif key == "http_status":
            out[key] = value if _META_STATUS_RE.fullmatch(value) else None
        elif key == "remote_ip":
            out[key] = value if _curl_meta_ip_plausible(value) else None
        elif key == "curl_exit":
            out[key] = value if _META_EXIT_RE.fullmatch(value) else None
        else:
            out[key] = value if _META_NUMERIC_RE.fullmatch(value) else None
    return out


def _log_curl_transport_diagnostics(
    label: str, *, level: int, failure: str, curl_exit: Optional[int],
    resp_headers: Optional[Dict[str, str]], meta: Dict[str, Optional[str]],
    budget_at_start: Optional[float], elapsed: float,
    max_time: Optional[float] = None,
) -> None:
    """传输诊断单行（白名单字段，复用现有 logger 入口）。

    不输出 URL 参数/header/正文/原始 stderr/凭据；request ID 仅从响应
    header 提取并做白名单字符过滤 + 限长；run/调用关联取当前 ResearchRun
    id（可取得时）。失败 warning / 成功 debug（避免日志量显著增长）。
    ``budget_at_start`` 是出站时的真实剩余预算；``max_time`` 是实际下发给
    curl 的 ``--max-time``（finance 单请求上限收窄后可小于剩余预算——两个
    值并列保留，不互相冒充）。
    """
    import logging

    request_id: Optional[str] = None
    if resp_headers:
        for name, value in resp_headers.items():
            if str(name).lower() == "x-request-id":
                filtered = _REQUEST_ID_RE.sub("", str(value))[:128]
                request_id = filtered or None
                break
    run_id: Optional[str] = None
    try:
        from src.core.research_run import current_research

        run = current_research.get()
        run_id = run.run_id[:12] if run is not None else None
    except Exception:  # noqa: BLE001  诊断失败不覆盖原异常
        run_id = None
    logging.getLogger(__name__).log(
        level,
        "ifind http daily %s transport diagnostics: failure=%s curl_exit=%s "
        "http_status=%s remote_ip=%s dns=%s tcp=%s tls=%s ttfb=%s total=%s "
        "request_id=%s run=%s budget_at_start=%s max_time=%s elapsed=%.3fs",
        label, failure, curl_exit if curl_exit is not None else "unavailable",
        *(meta.get(key) or "unavailable" for key in
          ("http_status", "remote_ip", "dns_seconds", "tcp_seconds", "tls_seconds",
           "ttfb_seconds", "total_seconds")),
        request_id or "unavailable", run_id or "unavailable",
        f"{budget_at_start:.3f}s" if budget_at_start is not None else "unbounded",
        f"{max_time:.3f}s" if max_time is not None else "unset",
        elapsed,
    )


def _http_api_curl_post(
    url: str, headers: Dict[str, str], body: Optional[bytes], *, label: str
) -> Tuple[int, bytes]:
    """本机 curl 子进程 POST（复用 MCP 导出的既有可中断传输，第十轮）。

    requests 的 socket read 可跨越绝对 deadline（iter_content 每块返回后才
    检查；慢 header/慢 body/滴流实测超期 ~0.4s，取消也需等待读取超时）——
    本原语换用既有 curl 模式实现真实硬截止：

    - ``--max-time``：有外层 deadline 用其剩余，缺 deadline 才用兜底
      上限；finance label 另与该次配置超时 ``ifind_http_timeout_seconds``
      取小（低于毫秒格式化精度直接不派发并按本地超时报错）硬切传输；
      父进程 selectors 读循环每 20ms ``check_attempt_alive``——
      用户取消（ResearchStopped）与 deadline 耗尽（TimeoutError）即时生效，
      ``finally`` kill 回收子进程；
    - 字节边界由父进程数 header/body 字节（声明超限立即拒绝，不等正文下完）；
    - 凭据只进 0600 临时 header 文件（finally 删除），不出现在 argv/日志/stdin；
    - 子进程环境只继承 PATH（与 MCP 导出一致：无代理/curlrc/netrc/cookie），
      正常 TLS、不跟随重定向、不重试、Accept-Encoding identity（无解压语义）。

    返回 ``(status, body)``；Windows（管道无法 selector 有界排空）明确失败
    走既有兜底。
    """
    import logging
    import os
    import selectors
    import shutil
    import subprocess

    if os.name == "nt":
        raise DataFetchError(f"ifind http daily {label} curl transport unsupported on this platform")
    curl = shutil.which("curl")
    if not curl:
        raise DataFetchError(f"ifind http daily {label} curl unavailable")
    check_attempt_alive()
    proto = "=https" if url.startswith("https://") else "=http"
    from src.patches.eastmoney_patch import request_deadline

    deadline = request_deadline.get()
    remaining = None if deadline is None else max(0.001, deadline - monotonic())
    max_time_value = remaining if remaining is not None else _HTTP_CURL_MAX_TIME_FALLBACK
    # finance 单请求硬上限：多期探测各期共用入口一次预留的 API 阶段截止，
    # 剩余可大于该次配置的单次传输上限（cap=8 时 API 阶段=cap+余量-reserve
    # =9.1s）——本次 ``--max-time`` 再与配置上限取小；其余 label 与无外层
    # deadline 行为不变。budget_at_start 仍记真实剩余预算，实际收窄值单独
    # 进诊断（max_time），两者不互相冒充。
    if label == "finance":
        from src.config import get_config

        configured = float(getattr(get_config(), "ifind_http_timeout_seconds", 30.0) or 0.0)
        if configured > 0:
            max_time_value = min(max_time_value, configured)
        if max_time_value < 0.001:
            # 极小正数配置（如 0.0004）格式化成 ``--max-time 0.000``，curl
            # 语义为不超时——低于毫秒精度时直接不派发，按本地预算耗尽报
            # TimeoutError（不伪装成供应商超时/空回复，无出站无诊断帧）。
            import logging

            logging.getLogger(__name__).warning(
                "ifind http daily %s budget %.6fs below curl max-time precision, not dispatched",
                label, max_time_value,
            )
            raise TimeoutError(
                f"ifind http daily {label} budget below curl max-time precision"
            )
    max_time = f"{max_time_value:.3f}"
    start_monotonic = monotonic()
    budget_at_start = remaining
    header_fd, header_path = tempfile.mkstemp(prefix="ifind-http-hdr-")
    proc: Optional[subprocess.Popen] = None
    try:
        with os.fdopen(header_fd, "w", encoding="utf-8") as handle:
            for name, value in headers.items():
                handle.write(f"{name}: {value}\n")
        os.chmod(header_path, 0o600)
        argv = [
            curl, "-q", "--silent", "--show-error", "--globoff",
            "--proto", proto,
            "--http1.1", "--no-buffer", "--include",
            "--max-time", max_time,
            "--max-filesize", str(_MAX_EXPORT_BYTES + _MAX_HEADER_BYTES),
            "--request", "POST",
            "--header", f"@{header_path}",
            "--data-binary", "@-",
            # write-out 元数据经 %{stderr} 重定向到独立通道（不占正文配额）；
            # 解析与脱敏见 _split_curl_meta/_parse_curl_meta。旧 curl 对
            # %{stderr}/未知变量输出字面占位——解析层按白名单标不可得。
            "--write-out", _CURL_WRITEOUT_FORMAT,
            "--url", url,
        ]
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=_curl_child_env(),
        )
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        try:
            proc.stdin.write(body or b"")
            proc.stdin.close()
        except BrokenPipeError:
            proc.stdin.close()
        stdout = bytearray()
        stderr_buf = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            selector.register(proc.stderr, selectors.EVENT_READ)
            while selector.get_map():
                try:
                    check_attempt_alive()
                except BaseException as exc:
                    # 本地中止（硬截止/取消）：元数据可能来不及输出，缺项
                    # 如实；只报告本地已知时间/原因，不为统计延长请求。
                    logging.getLogger(__name__).warning(
                        "ifind http daily %s aborted locally: reason=%s elapsed=%.3fs budget_at_start=%s max_time=%s",
                        label, f"{type(exc).__name__}:{str(exc)[:80]}",
                        monotonic() - start_monotonic,
                        f"{budget_at_start:.3f}s" if budget_at_start is not None else "unbounded",
                        f"{max_time_value:.3f}s" if max_time_value is not None else "unset",
                    )
                    raise
                for key, _event in selector.select(0.02):
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if key.fileobj is proc.stderr:
                        # 错误文本不采纳（退出码承载失败语义）；仅 write-out
                        # 诊断帧有界累积（超限丢弃后续，诊断降级）。
                        if len(stderr_buf) < _CURL_META_STDERR_MAX:
                            stderr_buf.extend(
                                chunk[:_CURL_META_STDERR_MAX - len(stderr_buf)])
                        continue
                    stdout.extend(chunk)
                    _reject_http_api_bounds(bytes(stdout), label)
        check_attempt_alive()
    finally:
        if proc is not None:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
        try:
            os.unlink(header_path)
        except FileNotFoundError:
            pass
    if proc is None:
        raise DataFetchError(f"ifind http daily {label} transport failed: no process")
    # 独立 stderr 诊断通道：元数据只从 stderr 有界提取，stdout 是纯业务
    # 正文（边界检查已在读循环完成）——诊断解析在任何情况下都不读取、不
    # 改写 stdout（正文含哨兵文本亦原样保留）。curl ≥7.75（%{stderr} 支持）
    # 是环境前置条件；更旧 curl 帧缺失时诊断如实不可得，不做 stdout 兼容。
    meta_raw = _split_curl_meta(bytes(stderr_buf))
    meta = _parse_curl_meta(meta_raw)
    elapsed = monotonic() - start_monotonic
    try:
        status, resp_headers, body = _split_included_http(bytes(stdout))
    except DataFetchError as exc:
        if label == "finance" and proc.returncode == 28:
            # finance 新可达分支（单请求上限收窄后新增）：curl 在共享预算
            # 仍剩时被 ``--max-time`` 硬切且尚未完成响应头（慢 header 场景
            # 无任何可解析字节）——exit 28 的真实原因是本地超时，沿用
            # returncode 28 分支的 curl_max_time/TimeoutError 归类与诊断
            # （此形态无可得响应头，resp_headers 如实为 None），不得先经
            # parser 误判成空回复类供应商失败；其他 label 既有语义保持。
            _log_curl_transport_diagnostics(
                label, level=logging.WARNING, failure="curl_max_time",
                curl_exit=proc.returncode, resp_headers=None, meta=meta,
                budget_at_start=budget_at_start, elapsed=elapsed, max_time=max_time_value)
            raise TimeoutError(f"ifind http daily {label} curl max-time reached") from exc
        if not stdout and proc.returncode not in {0, None}:
            # curl 52 类空回复：正是诊断要定位的现场（退出码 + 可得节点
            # 元数据），失败语义不变。
            _log_curl_transport_diagnostics(
                label, level=logging.WARNING, failure="empty_reply",
                curl_exit=proc.returncode, resp_headers=None, meta=meta,
                budget_at_start=budget_at_start, elapsed=elapsed, max_time=max_time_value)
            raise DataFetchError(
                f"ifind http daily {label} transport failed: curl exit {proc.returncode}"
            ) from exc
        _log_curl_transport_diagnostics(
            label, level=logging.WARNING, failure="unparsable_response",
            curl_exit=proc.returncode, resp_headers=None, meta=meta,
            budget_at_start=budget_at_start, elapsed=elapsed, max_time=max_time_value)
        raise
    if proc.returncode == 28:
        _log_curl_transport_diagnostics(
            label, level=logging.WARNING, failure="curl_max_time",
            curl_exit=proc.returncode, resp_headers=resp_headers, meta=meta,
            budget_at_start=budget_at_start, elapsed=elapsed, max_time=max_time_value)
        raise TimeoutError(f"ifind http daily {label} curl max-time reached")
    if proc.returncode == 63:
        _log_curl_transport_diagnostics(
            label, level=logging.WARNING, failure="byte_bound",
            curl_exit=proc.returncode, resp_headers=resp_headers, meta=meta,
            budget_at_start=budget_at_start, elapsed=elapsed, max_time=max_time_value)
        raise DataFetchError(f"ifind http daily {label} byte bound exceeded")
    if proc.returncode not in {0, None}:
        _log_curl_transport_diagnostics(
            label, level=logging.WARNING, failure="transport_failed",
            curl_exit=proc.returncode, resp_headers=resp_headers, meta=meta,
            budget_at_start=budget_at_start, elapsed=elapsed, max_time=max_time_value)
        raise DataFetchError(
            f"ifind http daily {label} transport failed: curl exit {proc.returncode}"
        )
    if status != 200:
        # 非 200 是「传输完成、HTTP 层异常」：默认日志下以 warning 保留现场
        # （节点/时延/状态白名单证据）——不推断额度/限流原因（上层按状态
        # 分类：401/403 触发续用重试、其余按 DataFetchError 语义）。
        _log_curl_transport_diagnostics(
            label, level=logging.WARNING, failure="http_response",
            curl_exit=proc.returncode, resp_headers=resp_headers, meta=meta,
            budget_at_start=budget_at_start, elapsed=elapsed, max_time=max_time_value)
    else:
        # 200 成功诊断仅 debug 级（对照样本，不显著增加常规日志量）。
        _log_curl_transport_diagnostics(
            label, level=logging.DEBUG, failure="none",
            curl_exit=proc.returncode, resp_headers=resp_headers, meta=meta,
            budget_at_start=budget_at_start, elapsed=elapsed, max_time=max_time_value)
    return status, body


def _parse_json_payload(body: bytes, *, label: str) -> Dict[str, object]:
    """有界响应体（curl 子进程输出）→ 精确十进制 JSON 对象。"""
    try:
        payload = json.loads(body.decode("utf-8"), parse_float=Decimal, parse_int=Decimal)
    except (ValueError, UnicodeDecodeError) as exc:
        raise DataFetchError(f"ifind http daily {label} response invalid: {type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise DataFetchError(f"ifind http daily {label} response is not a json object")
    return payload


def _history_body(identity: str, start: _date, end: _date, *, cps: str = _CASH_QFQ_CPS) -> Dict[str, object]:
    """THS_HQ 请求体：官方结构化参数；``cps`` 取官方枚举（6=现金分红前复
    权为管线取数口径，1=不复权为逐窗口单位资格对照口径）。"""
    return {
        "codes": identity,
        "indicators": _HTTP_INDICATORS,
        "startdate": start.isoformat(),
        "enddate": end.isoformat(),
        "functionpara": {"Interval": "D", "CPS": cps, "fill": "Omit"},
    }


def _post_history_query(
    identity: str,
    start: _date,
    end: _date,
    *,
    access_token: str,
    cps: str = _CASH_QFQ_CPS,
) -> Tuple[Dict[str, object], str]:
    """一次出站的认证 POST（curl 可中断传输）：硬截止、不重试、凭据只进
    0600 临时 header 文件。返回 ``(payload, token_used)``（第九轮脱敏契约）。
    """
    headers = {
        "Content-Type": "application/json",
        "access_token": access_token,
        "ifindlang": "cn",
    }
    body = json.dumps(
        _history_body(identity, start, end, cps=cps),
        default=str,
    ).encode("utf-8")
    status, response_body = _http_api_curl_post(
        _HTTP_ENDPOINT, headers, body, label="history",
    )
    if status != 200:
        # 401/403 直接指向凭据问题（官方 FAQ：access_token 有效期 7 天）；
        # 上层 `_post_history_query_authorized` 会刷新续用并重试一次。不携带
        # 任何凭据内容。
        if status in (401, 403):
            raise _HttpAuthError(
                f"ifind http daily authentication failed (http {status}); "
                "access_token 可能已过期（官方有效期 7 天），请重新获取后重启进程"
            )
        raise DataFetchError(f"ifind http daily http status {status}")
    return _parse_json_payload(response_body, label="history"), access_token


def _require_zero_errorcode(
    payload: Dict[str, object],
    identity: str,
    *,
    access_token: str,
    extra_secrets: Tuple[str, ...] = (),
) -> None:
    """errorcode=0 才继续；非 0 按供应商错误拒绝并保留脱敏 errmsg。

    errmsg 是供应商文本，进入异常消息（随后的日志/诊断/报告路径）前按本机
    全部凭据值（access_token、refresh_token 与缓存 token）脱敏，防止供应商
    裸回显任一凭据落日志。
    """
    raw_code = payload.get("errorcode")
    numeric: Optional[int] = None
    if isinstance(raw_code, (int, str, Decimal)) and not isinstance(raw_code, bool):
        try:
            numeric = int(raw_code)
        except ValueError:
            numeric = None
    if numeric is None:
        raise DataFetchError(f"ifind http daily {identity} response errorcode missing")
    if numeric != 0:
        message = _redact_token(
            str(payload.get("errmsg") or "").strip(), access_token, *extra_secrets
        )
        raise DataFetchError(
            f"ifind http daily {identity} supplier errorcode {numeric}" + (f": {message}" if message else "")
        )


def _response_tables(payload: Dict[str, object], identity: str) -> Tuple[List[str], Dict[str, List[object]]]:
    """取唯一合格表：多表/空表/证券不符/缺列一律整窗拒绝，不混拼。"""
    tables = payload.get("tables")
    if not isinstance(tables, list) or len(tables) != 1 or not isinstance(tables[0], dict):
        raise DataFetchError(
            f"ifind http daily {identity} response must carry exactly one table"
        )
    table = tables[0]
    thscode = str(table.get("thscode") or "").strip().upper()
    if thscode != identity:
        raise DataFetchError(
            f"ifind http daily response security {thscode or 'unknown'} != requested {identity}"
        )
    times = table.get("time")
    columns = table.get("table")
    if not isinstance(times, list) or not isinstance(columns, dict):
        raise DataFetchError(f"ifind http daily {identity} response table structure invalid")
    for field in _COLUMN_TO_FIELD:
        if field not in columns or not isinstance(columns[field], list):
            raise DataFetchError(f"ifind http daily {identity} response missing column {field}")
        if len(columns[field]) != len(times):
            raise DataFetchError(
                f"ifind http daily {identity} column {field} length != time length"
            )
    return times, columns


def _parse_history_rows(
    payload: Dict[str, object],
    identity: str,
    *,
    access_token: str = "",
    extra_secrets: Tuple[str, ...] = (),
) -> Dict[_date, Dict[str, str]]:
    """官方响应 → {日期: 六字段原文}；时间严格 ISO 日期，其余整窗拒绝。

    JSON 数值经 ``parse_float/parse_int=Decimal`` 直接得到精确十进制，再以
    中文规范字段名的字符串原文进入既有 ``_qualify_window`` 校验管线；null/
    占位交给同一拒绝路径，不自动判停牌（issue #91：600176 案例不授权普遍
    放宽）。
    """
    _require_zero_errorcode(payload, identity, access_token=access_token, extra_secrets=extra_secrets)
    times, columns = _response_tables(payload, identity)
    rows: Dict[_date, Dict[str, str]] = {}
    for index, raw_time in enumerate(times):
        text = str(raw_time or "").strip()
        if not _HTTP_TIME_RE.match(text):
            raise DataFetchError(
                f"ifind http daily {identity} time {text or 'empty'} is not an ISO calendar date"
            )
        session = _date.fromisoformat(text)
        if session in rows:
            raise DataFetchError(
                f"ifind http daily {identity} duplicate time row {text}"
            )
        values: Dict[str, str] = {}
        for column, field in _COLUMN_TO_FIELD.items():
            cell = columns[column][index]
            values[field] = "" if isinstance(cell, bool) or cell is None else str(cell)
        rows[session] = values
    if not rows:
        raise DataFetchError(f"ifind http daily {identity} response carries no rows")
    return rows


def _redact_token(message: str, token: str, *extra: str) -> str:
    """供应商文本进入异常消息前按本机凭据值脱敏（防裸回显落日志）。"""
    for secret in (token, *extra):
        if secret and secret in message:
            message = message.replace(secret, "***")
    return message


def _decimal_or_none(text: str) -> Optional[Decimal]:
    """量额/价格原文 → 精确 Decimal；缺失或非法返回 None（交下游拒绝）。"""
    try:
        value = Decimal((text or "").strip())
    except ArithmeticError:  # InvalidOperation：占位/畸形原文
        return None
    return value if value.is_finite() else None


def _qualify_volume_unit_consistency(
    adjusted_rows: Dict[_date, Dict[str, str]],
    raw_rows: Dict[_date, Dict[str, str]],
    identity: str,
) -> None:
    """逐窗口量额资格（issue #91 复审澄清的运行时出证）。

    官方文档对 THS_HQ 量额单位零声明，且静态常量/比例校验都不构成单位证
    明——本资格在每次窗口采纳前对**同一窗口**现场出证：

    1. CPS=6（现金分红前复权）与 CPS=1（不复权，官方取值表缺省值）两响应
       日期集合必须一致；
    2. 逐日成交量/成交额精确相等——量额不随复权方式变动（即未复权口径）；
    3. 逐日成交均价 ``amount/volume ∈ 当日未复权 [low, high]``（闭区间：
       一字板 VWAP=high=low）——量额比率自洽，amount/volume 是每股价格。

    **能力边界（勿夸大为绝对单位证明）**：把 volume 与 amount 以同一倍率
    缩放（例如各 ×100，即 百股/百元 量纲）VWAP 不变且第 1/2 条仍成立，本
    资格**无法检测**——双请求证明的是比率与未复权一致性，不是绝对单位
    股/元（``test_unit_qualification_cannot_detect_proportional_scaling``
    锁定该边界）。绝对单位现有证据是东财 250 日对照（qualification 报告，
    600519.SH/600031.SH 两股量额逐日一致），未外推为全市场结论；残余缺口
    随文档与交付披露。

    价格比较**只用未复权行的 low/high**：复权价与未复权 VWAP 不可比（窗
    口跨除权后累计复权偏离使合法行越界，见模块 docstring 反例）。任一环
    节不满足即整窗拒绝走兜底，不产生部分采纳。量额缺失/非法/非正不在本
    资格判定（不除零、不误判停牌），由既有 ``_require_decimal`` 拒绝路径
    统一兜底。真实验收证据：600519.SH/600031.SH 各 250 日（协调者官方授
    权会话实测）量额跨 CPS 逐日一致、未复权 VWAP 全部在当日区间。
    """
    if set(adjusted_rows) != set(raw_rows):
        raise DataFetchError(
            f"ifind http daily {identity} adjusted/unadjusted windows differ "
            f"({len(adjusted_rows)} vs {len(raw_rows)} rows)，量额单位资格不成立，整窗不采纳"
        )
    for session in sorted(adjusted_rows):
        adj = adjusted_rows[session]
        raw = raw_rows[session]
        day = session.isoformat()
        adj_volume = _decimal_or_none(adj["成交量"])
        raw_volume = _decimal_or_none(raw["成交量"])
        adj_amount = _decimal_or_none(adj["成交额"])
        raw_amount = _decimal_or_none(raw["成交额"])
        # 任一侧量额缺失/非法而另一侧合法：两响应已不一致，直接拒绝（下游
        # 只校验复权侧，非法对照行不会经过任何其他资格路径）。
        if (adj_volume is None) != (raw_volume is None):
            raise DataFetchError(
                f"ifind http daily {identity} 交易日 {day} 成交量在复权/不复权响应一侧非法，"
                "量额单位资格不成立，整窗不采纳"
            )
        if (adj_amount is None) != (raw_amount is None):
            raise DataFetchError(
                f"ifind http daily {identity} 交易日 {day} 成交额在复权/不复权响应一侧非法，"
                "量额单位资格不成立，整窗不采纳"
            )
        if adj_volume is not None and adj_volume != raw_volume:
            raise DataFetchError(
                f"ifind http daily {identity} 交易日 {day} 成交量随复权变动 "
                f"({adj_volume} vs {raw_volume})，量额单位资格不成立，整窗不采纳"
            )
        if adj_amount is not None and adj_amount != raw_amount:
            raise DataFetchError(
                f"ifind http daily {identity} 交易日 {day} 成交额随复权变动 "
                f"({adj_amount} vs {raw_amount})，量额单位资格不成立，整窗不采纳"
            )
        # 量额缺失/非法（两侧同态）交由既有数值资格拒绝；不除零。
        if adj_volume is None or adj_amount is None or adj_volume <= 0:
            continue
        raw_low = _decimal_or_none(raw["最低价"])
        raw_high = _decimal_or_none(raw["最高价"])
        if raw_low is None or raw_high is None:
            raise DataFetchError(
                f"ifind http daily {identity} 交易日 {day} 未复权对照价格非法，"
                "量额单位资格不成立，整窗不采纳"
            )
        vwap = adj_amount / adj_volume
        if not (raw_low <= vwap <= raw_high):
            raise DataFetchError(
                f"ifind http daily {identity} 交易日 {day} 未复权成交均价 {vwap} "
                f"越出当日未复权区间 [{raw_low}, {raw_high}]，量额单位资格不成立，整窗不采纳"
            )


class IfindHttpDailyFetcher(BaseFetcher):
    """iFinD 标准 HTTP 现金分红前复权日线 Fetcher（issue #91 施工边界）。

    不注册进通用 fetcher 列表；由管理器在 MCP 日线预置尝试之后、既有候选
    链（东财优先）之前作为第二预置尝试调用。资格校验与来源披露完全复用
    MCP 日线的既有边界；名称独立，保证「HTTP 和 MCP 不混拼」的窗口同源
    约束与跨源整窗一致性保护按精确来源名生效。
    """

    name = "IfindHttpDailyFetcher"
    priority = 0

    @property
    def available(self) -> bool:
        return ifind_http_daily_enabled()

    def is_available_for_request(self, capability: str = "") -> bool:
        return self.available

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        identity_match = _IDENTITY_RE.match((stock_code or "").strip().upper())
        if identity_match is None:
            raise DataFetchError(f"IfindHttpDailyFetcher unsupported stock identity: {stock_code}")
        identity = f"{identity_match.group(1)}.{identity_match.group(2)}"
        from src.config import get_config
        from src.core.research_run import current_research, supplier_need_key
        from src.patches.eastmoney_patch import request_deadline

        token = request_deadline.set(http_attempt_deadline())
        try:
            run = current_research.get()
            if run is not None:
                from data_provider.mx_data_adapter import financial_refresh

                marker = financial_refresh.get()
                refresh = marker[1] if marker else ""
                key = supplier_need_key(
                    "ifind-http-daily", get_config().data_source_contract, identity,
                    f"{start_date}|{end_date}|cash-qfq", refresh,
                )
                return run.single_fetch(key, lambda: self._fetch_window_once(identity, start_date, end_date))
            return self._fetch_window_once(identity, start_date, end_date)
        finally:
            request_deadline.reset(token)

    def _fetch_window_once(self, identity: str, start_date: str, end_date: str) -> pd.DataFrame:
        from src.core.trading_calendar import trading_sessions_between

        check_attempt_alive()
        start = _parse_date(start_date)
        end = _parse_date(end_date)
        if start is None or end is None or start > end:
            raise DataFetchError(f"IfindHttpDailyFetcher invalid window {start_date}~{end_date}")
        sessions = trading_sessions_between("cn", start, end)
        if not sessions:
            raise DataFetchError("ifind http daily strict calendar unavailable")
        from src.config import get_config

        config = get_config()
        refresh_token = (config.ifind_http_refresh_token or "").strip()
        # 取数请求（CPS=6 现金分红前复权）：先解析合格再发对照请求，取数
        # 失败不产生第二次出站；401/403 时刷新重试一次。超时预算由
        # http_attempt_deadline（自建/外层）经 curl --max-time 硬性承载。
        payload, token_used = _post_history_query_authorized(
            identity, start, end,
            cps=_CASH_QFQ_CPS,
            refresh_token=refresh_token,
        )
        check_attempt_alive()  # 传输返回后、解析与采纳前
        # 脱敏只覆盖本次请求实际携带的凭据（refresh_token + token_used）：
        # 纯函数无锁——另一任务持锁换取网络时解析段不受阻塞、取消/deadline
        # 立即可判（第九轮；supplier errmsg 回显的是本请求 header 的凭据）。
        secrets = _redaction_secrets(refresh_token, token_used)
        adjusted_rows = _parse_history_rows(
            payload, identity,
            access_token=secrets[0] if secrets else "",
            extra_secrets=secrets[1:],
        )
        # 单位资格对照请求（CPS=1 不复权）：同窗口、同一凭据与超时预算。
        raw_payload, raw_token_used = _post_history_query_authorized(
            identity, start, end,
            cps=_UNADJUSTED_CPS,
            refresh_token=refresh_token,
        )
        check_attempt_alive()
        secrets = _redaction_secrets(refresh_token, raw_token_used)
        raw_rows = _parse_history_rows(
            raw_payload, identity,
            access_token=secrets[0] if secrets else "",
            extra_secrets=secrets[1:],
        )
        _qualify_volume_unit_consistency(adjusted_rows, raw_rows, identity)
        frame = _qualify_window(adjusted_rows, sessions, identity)
        check_attempt_alive()  # 最终采纳前：停止后不采纳迟到窗口
        return frame

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        return standardize_daily_frame(df)


# ---------------------------------------------------------------------------
# 财务快照八项（第三十一轮纵向闭环、第三十二轮完整收敛）：标准 API 快照级
# 优先、MCP 快照兜底。用户已授权验证可用的 MCP 能力迁移标准 API。
# 第三十二轮审查修正：每字段按官方模板出参（roe 仅 TD、毛利率/同比/披露日
# 仅 D、四指标 D+C——不给单参指标传多余参数）；statement_scope/
# adjustment 对齐既有合格契约（consolidated + restated，对齐 MCP 合并报表
# (调整) 解析）；披露时点在采纳前按分析时点校验（None/晚于今日拒绝，不凭
# 请求日期充当披露时间）；401/403 鉴权强刷重试一次（与日线 authorized 模
# 式一致）；主源多期语义（period=None）经「最新期往回探测首个已披露期」
# 实现；API 快照缺失字段由 MCP 同期补缺（逐字段来源标注忠实）。
# ---------------------------------------------------------------------------
_FINANCE_FIELD_SPECS: Tuple[Tuple[str, str, str, str, str], ...] = (
    # (field, indicator, unit, 出参形态, caliber)——出参形态按官方
    # get_indicator_params 逐字段核实（第三十二轮）：D+C=报告期+报表类型
    # 枚举；D=仅报告期；TD=报告期（该指标单参数变体）。多余参数不可依赖
    # 服务端忽略。caliber 对齐 MCP 解析规范值（caliber_from_label），
    # 消费方 reading_input_reasons 按此校验口径身份。
    # 报表口径证据（第二十轮审查收敛，scope 与 adjustment 各自独立证明）：
    # 仅 **D+C** 指标携带 C='3'（合并报表(调整)）枚举——解析标注
    # consolidated/restated 有请求参数证据；**d_only** 指标（roe/毛利率/
    # 两同比）请求不含报表类型参数，数值与 D+C 指标接近不构成调整口径
    # 证据——scope/adjustment 按真实未知（None）标注，不伪装 restated，
    # 由 ``financial_snapshot_with_fallback`` 按「口径待补」交给既有合格
    # MCP 补缺/消费层 unknown 资格路径如实处理。
    ("revenue", "ths_revenue_stock", "currency_base", "dc", "operating_revenue"),
    ("net_profit", "ths_np_atoopc_stock", "currency_base", "dc", "parent_net_profit"),
    ("roe", "ths_roe_wgt_publish_stock", "percentage_point", "d_only", "weighted_roe"),
    ("gross_margin", "ths_gross_selling_rate_stock", "percentage_point", "d_only", "gross_margin"),
    ("revenue_yoy", "ths_or_yoy_stock", "percentage_point", "d_only", "operating_revenue_yoy"),
    ("net_profit_yoy", "ths_np_atsopc_yoy_stock", "percentage_point", "d_only", "parent_net_profit_yoy"),
    ("assets", "ths_total_assets_stock", "currency_base", "dc", "balance_sheet_total"),
    ("liabilities", "ths_total_liab_stock", "currency_base", "dc", "balance_sheet_total"),
)
_FINANCE_DISCLOSED_AT_INDICATOR = "ths_regular_report_actual_dd_stock"  # 仅 D
_FINANCE_BASIC_ENDPOINT = "https://quantapi.51ifind.com/api/v1/basic_data_service"
_FINANCE_REPORT_TYPE_ENUM = "3"  # 合并报表(调整)：官方 C 参数 choice 值（非显示文本）


def _canonical_period(period: str) -> str:
    """期次统一规范化（第三十六轮）：整个请求、解析与业务资格的单一事实来源。

    接受消费方 ``YYYY-MM-DD``（Choice report_date 形态）与官方 ``YYYYMMDD``
    两形态，统一为 YYYYMMDD canonical；其余形态/非法日历日期显式拒绝
    （``invalid_configuration``，fallback 语义下回退 MCP 原路径），不静默
    透传依赖服务端。年度/期间基数判定（annual/YTD）与探测候选均以
    canonical 为准——带/不带横线的同一期间产出完全等价的读数与资格。
    """
    import logging

    from data_provider.cross_source_validator import SourceReadError

    logger = logging.getLogger(__name__)
    text = str(period or "").strip()
    digits = text.replace("-", "")
    if len(digits) != 8 or not digits.isdigit():
        logger.warning("ifind http finance 非法期次形态 %r（拒绝出站）", period)
        raise SourceReadError("ifind", "invalid_configuration")
    try:
        _datetime.strptime(digits, "%Y%m%d")
    except ValueError:
        logger.warning("ifind http finance 非法日历日期 %r（拒绝出站）", period)
        raise SourceReadError("ifind", "invalid_configuration")
    return digits


def _finance_indiparams(indicator: str, form: str, period: str) -> List[str]:
    """按该指标官方模板构造参数（枚举值非显示文本）。"""
    if indicator == _FINANCE_DISCLOSED_AT_INDICATOR or form == "d_only":
        return [period]
    if form == "dc":
        return [period, _FINANCE_REPORT_TYPE_ENUM]
    raise DataFetchError(f"ifind http finance unknown param form {form!r} for {indicator}")


def _finance_api_deadline(deadline: float) -> float:
    """财务 API 阶段的预算上限（第三十八轮，按实际可用计算预留）。

    预留公式沿用 ``http_attempt_deadline`` 既有语义（reserve=min(8, 30%)）
    但基数改为实际可用 ``min(cap + 解析余量, deadline - now)``——短预算下
    reserve 不被外层 deadline 截断，HTTP 用满 API 份额后 MCP 兜底仍有正
    剩余；``deadline`` 语义（总上限/取消）不变。
    """
    from src.config import get_config

    now = monotonic()
    available = deadline - now
    if available <= 1.0:
        return now + max(available, 0.001)  # 预算本身已近耗尽：不虚留
    cap = float(getattr(get_config(), "ifind_http_timeout_seconds", 30.0) or 0.0)
    budget = min(cap + _HTTP_PARSE_STAGE_SECONDS, available)
    if cap <= 0:
        return deadline  # 无自建上限语义：保持原 deadline（不虚留）
    reserve = min(8.0, budget * 0.3)
    return now + max(budget - reserve, 1.0)


def _finance_body(identity: str, period: str) -> Dict[str, object]:
    """basic_data_service 请求体：八字段（各自官方模板出参）+ 披露日指标。"""
    indipara = [
        {"indicator": indicator, "indiparams": _finance_indiparams(indicator, form, period)}
        for _field, indicator, _unit, form, _caliber in _FINANCE_FIELD_SPECS
    ]
    indipara.append({
        "indicator": _FINANCE_DISCLOSED_AT_INDICATOR,
        "indiparams": _finance_indiparams(_FINANCE_DISCLOSED_AT_INDICATOR, "d_only", period),
    })
    return {"codes": identity, "indipara": indipara}


def _post_finance_query_authorized(
    identity: str, period: str, *, access_token: str, refresh_token: str,
) -> Tuple[Dict[str, object], str]:
    """认证 POST + 401/403 强制续用重试一次（与日线 authorized 模式一致）。

    传输复用既有 curl 硬截止；凭据只进 0600 临时 header 文件；重试仅一次
    （供应商错误不重试），失败原因保真脱敏。
    """
    headers = {"Content-Type": "application/json", "access_token": access_token, "ifindlang": "cn"}
    body = json.dumps(_finance_body(identity, period), default=str).encode("utf-8")
    status, response_body = _http_api_curl_post(
        _FINANCE_BASIC_ENDPOINT, headers, body, label="finance",
    )
    used = access_token
    if status in (401, 403) and refresh_token:
        # 鉴权失败：强制刷新续用后重试同一请求一次（不重试供应商业务错误）。
        refreshed = _resolve_access_token(refresh_token, force_refresh=True)
        headers = {**headers, "access_token": refreshed}
        body = json.dumps(_finance_body(identity, period), default=str).encode("utf-8")
        status, response_body = _http_api_curl_post(
            _FINANCE_BASIC_ENDPOINT, headers, body, label="finance",
        )
        used = refreshed
    if status != 200:
        raise DataFetchError(f"ifind http finance http status {status}")
    return _parse_json_payload(response_body, label="finance"), used


def _same_security_identity(returned: str, requested: str) -> bool:
    """返回证券码与请求一致（点分后缀等价比较，大小写不敏感）。"""
    def norm(value: str) -> str:
        return str(value or "").strip().upper()
    return norm(returned) == norm(requested)


def _parse_finance_table(
    payload: Dict[str, object], identity: str, period: str, *,
    access_token: str, extra_secrets: Tuple[str, ...],
) -> Dict[str, Any]:
    """校验 errorcode/表形状/证券归属并解析八字段 + 披露日；采纳前校验披露时点。

    - 每字段按 indicator 取值：缺失（None/空串/'--'）不进快照（自然缺项
      如银行/非银毛利率、未披露期），不补零不推算；
    - 披露日只取披露日指标实际值；None 或晚于分析时点（本地今日，与既有
      ``confirmed_disclosed_period`` 锚一致）→ 整期拒绝（供应商可能回退
      返回未披露期数据，值本身不可信）——走 MCP 兜底而非采纳；
    - 报表口径按字段证据（第二十轮）：仅 **D+C 指标**（请求携带 C='3'
      合并报表(调整) 枚举）标注 ``statement_scope="consolidated"`` +
      ``adjustment="restated"``（对齐既有合格契约，P1/主源采纳判定按此
      比较）；**d_only 指标**（roe/毛利率/两同比，请求仅日期参数）口径
      未证明——None 真实未知，不伪装 restated，由 fallback 按「口径待补」
      交 MCP 补缺/消费层 unknown 资格路径处理。
    """
    import logging

    from data_provider.cross_source_validator import AnchorReading, SourceReadError
    from datetime import timezone as _tz

    logger = logging.getLogger(__name__)
    _require_zero_errorcode(payload, identity, access_token=access_token, extra_secrets=extra_secrets)
    tables = payload.get("tables")
    if not isinstance(tables, list) or len(tables) != 1 or not isinstance(tables[0], dict):
        raise DataFetchError(f"ifind http finance {identity} unexpected tables shape")
    entry = tables[0]
    returned_code = str(entry.get("thscode") or "")
    if not _same_security_identity(returned_code, identity):
        raise DataFetchError(f"ifind http finance {identity} returned {returned_code or 'unknown'}")
    table = entry.get("table")
    if not isinstance(table, dict):
        raise DataFetchError(f"ifind http finance {identity} missing table")

    disclosed_raw = table.get(_FINANCE_DISCLOSED_AT_INDICATOR)
    disclosed_cell = disclosed_raw[0] if isinstance(disclosed_raw, list) and disclosed_raw else None
    disclosed_text = str(disclosed_cell).strip() if disclosed_cell is not None and str(disclosed_cell).strip() else ""
    disclosed_at: Optional[str] = None
    if disclosed_text:
        digits = re.sub(r"\D", "", disclosed_text)[:8]
        if len(digits) == 8:
            disclosed_at = f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    # 采纳前披露时点校验：未披露（无披露日）或披露日晚于分析时点 → 该期
    # 数据不可采纳（供应商对未披露期可能回退最近可得值，值本身不可信）。
    if disclosed_at is None:
        logger.warning(
            "ifind http finance %s %s 披露日缺失（未披露期），该期拒绝采纳", identity, period,
        )
        raise SourceReadError("ifind", "invalid_response")
    try:
        disclosed_day = _datetime.strptime(disclosed_at, "%Y-%m-%d").date()
    except ValueError:
        logger.warning(
            "ifind http finance %s %s 披露日不可解析: %r", identity, period, disclosed_text,
        )
        raise SourceReadError("ifind", "invalid_response")
    if disclosed_day > _date.today():
        logger.warning(
            "ifind http finance %s %s 披露日 %s 晚于分析时点，该期拒绝采纳",
            identity, period, disclosed_at,
        )
        raise SourceReadError("ifind", "invalid_response")
    # 披露日必须晚于所请求报告期（对齐 Choice 既有契约
    # ``disclosure <= report_date → not_disclosed``）：披露日早于/等于报告期
    # 属供应商回退或错乱数据，按请求期冒充财期的值不可采纳（第三十七轮）。
    report_period_day = _datetime.strptime(period, "%Y%m%d").date()
    if disclosed_day <= report_period_day:
        logger.warning(
            "ifind http finance %s %s 披露日 %s 不晚于报告期，疑似回退响应，该期拒绝采纳",
            identity, period, disclosed_at,
        )
        raise SourceReadError("ifind", "invalid_response")

    # 期间基数（A 股合并报表累计口径）：年报=annual，一/三/中报=YTD——
    # period 参数为 canonical（YYYYMMDD，调用方经 _canonical_period 保证），
    # 带横线形态不会误判年度基数（第三十六轮）。
    # 消费方采纳门（has_current_financial_period/_eligible_current_financial）
    # 要求 basis ∈ {annual, YTD}，最终业务字段才合格。
    period_basis = "annual" if period.endswith("1231") else "YTD"
    readings: Dict[str, Any] = {}
    for field, indicator, unit, form, caliber in _FINANCE_FIELD_SPECS:
        cells = table.get(indicator)
        value = cells[0] if isinstance(cells, list) and cells else None
        if value is None or str(value).strip() in ("", "--"):
            continue
        if isinstance(value, bool):
            # bool（JSON true/false）不是财务数值：显式拒绝（float(True)=1.0
            # 会误当合格值并阻止 MCP 补缺，第三十八轮）；文本数字仍按供应商
            # 文本数字契约解析。
            logger.warning(
                "ifind http finance %s %s %s 布尔值不是财务数字，按缺失处理",
                identity, period, field,
            )
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(numeric):
            # NaN/±Inf（供应商占位或解析伪值）不得伪装成有效读数：与缺失
            # 同语义处理（不进快照），使 MCP 同期补缺可以补回合格值；
            # 合法 0/负数（未盈利期归母等）经 isfinite 通过按字段契约保留。
            logger.warning(
                "ifind http finance %s %s %s 非有限值 %r，按缺失处理", identity, period, field, value,
            )
            continue
        readings[field] = AnchorReading(
            "ifind",
            numeric,
            period=period,
            period_basis=period_basis,
            caliber=caliber,
            unit=unit,
            currency="CNY",
            disclosed_at=disclosed_at,
            # 报表口径按字段证据（第二十轮）：仅 D+C 指标（C='3' 合并报表
            # (调整) 枚举）标注 consolidated/restated；d_only 指标请求不含
            # 报表类型参数、口径未证明——None（真实未知），不伪装 restated，
            # 待 MCP 补缺/消费层 unknown 资格路径处理。
            statement_scope="consolidated" if form == "dc" else None,
            adjustment="restated" if form == "dc" else None,
            # source_family 不显式设置：AnchorReading post_init 从
            # source="ifind" 自动推导 "ths"（与 MCP 读数同供应商家族）——
            # 同花顺 HTTP 与 MCP 是同一来源家族，不得被交叉验证误判为
            # 独立来源（第三十三轮审查）；传输渠道区分仅存在于 fallback
            # 警告日志，不进入校验独立性语义。
            fetched_at=_datetime.now(_tz.utc).isoformat(),
        )
    if not readings:
        raise SourceReadError("ifind", "invalid_response")
    # 渠道可观测（第三十七轮）：字段级契约（AnchorReading）不承载渠道身份，
    # API/MCP 渠道区分的可验收证据在日志层（本条 + fallback 回退/补缺日志）。
    logger.info(
        "ifind http finance %s %s API 快照采纳（%d/%d 字段）",
        identity, period, len(readings), len(_FINANCE_FIELD_SPECS),
    )
    return readings


def _ifind_http_financial_snapshot_once(code: str, period: str, *, deadline: float) -> Dict[str, Any]:
    """单期快照的实际出站体：``deadline`` 已是绝对 API 截止，不再预留。

    公开单期入口与多期探测共用（第五十轮）：多期入口只在入口预留一次
    （MCP 兜底份额），各单期出站直接复用该绝对截止——若每期再套
    ``_finance_api_deadline`` 会重复预留（线上 600031 事件：90 秒阶段
    预算的财务半窗 45 秒被两层预留 45→27→约 19 秒）。"""
    from data_provider.cross_source_validator import SourceReadError
    from src.config import get_config
    from src.patches.eastmoney_patch import request_deadline

    identity = http_qualified_identity(code)
    if identity is None:
        raise SourceReadError("ifind", "permission_denied")
    config = get_config()
    refresh = (getattr(config, "ifind_http_refresh_token", None) or "").strip()
    if not refresh:
        raise SourceReadError("ifind", "invalid_configuration")

    canonical_period = _canonical_period(period)
    deadline_token = request_deadline.set(float(deadline))
    try:
        access_token = _resolve_access_token(refresh)
        payload, used = _post_finance_query_authorized(
            identity, canonical_period, access_token=access_token, refresh_token=refresh,
        )
        # canonical 期次作为请求、解析与期间基数（annual/YTD）的单一事实
        # 来源：带/不带横线的同一期间产出完全等价的读数与资格（第三十六轮）。
        return _parse_finance_table(
            payload, identity, canonical_period,
            access_token=used, extra_secrets=_redaction_secrets(refresh, used),
        )
    finally:
        request_deadline.reset(deadline_token)


def ifind_http_financial_snapshot(code: str, period: str, *, deadline: float) -> Dict[str, Any]:
    """标准 API 财务快照（快照级单期出站）：具体报告期入口。

    契约见 ``_parse_finance_table``；``deadline`` 落实 ``request_deadline``
    （curl 硬截止与用户取消语义与日线同一传输）；失败统一抛
    ``SourceReadError``/``DataFetchError`` 由调用方走 MCP 快照兜底。
    单期公开入口自行预留一次（多期探测路径不经本入口，避免重复预留）。
    """
    # 预算预留按**实际可用**计算（第三十八轮修正：此前按配置 cap 使短预算
    # 下 reserve 被外层 deadline 截断、HTTP 慢耗尽后 MCP 剩余 0）：
    # available = deadline - now；budget = min(cap + 解析余量, available)；
    # reserve = min(8, budget * 30%)；API 阶段 = now + max(budget - reserve, 1)。
    # 总 deadline 与取消语义保持（api_deadline ≤ deadline），MCP 兜底复用
    # 原 deadline 时保证正剩余。
    return _ifind_http_financial_snapshot_once(
        code, period, deadline=_finance_api_deadline(float(deadline)))


def _quarter_end_candidates(today: _date) -> List[str]:
    """主源多期探测的期候选（最新→更早，与 MCP「查询去年至今天每报告期」
    同语义的回退序）：最近已完成季度末往前共 8 期。"""
    candidates: List[str] = []
    year, quarter = today.year, (today.month - 1) // 3  # 当前未完成季度不计
    quarter_end_month_day = ("0331", "0630", "0930", "1231")
    for _ in range(8):
        quarter -= 1
        if quarter < 0:
            quarter = 3
            year -= 1
        candidates.append(f"{year}{quarter_end_month_day[quarter]}")
    return candidates


def ifind_http_financial_snapshot_latest(code: str, *, deadline: float) -> Dict[str, Any]:
    """主源多期语义（MCP ``period=None`` 等价）：字段级「有值的最新期」合并。

    第三十九轮按既有 ``select_current_financials`` 契约重做：MCP 多期解析
    每字段取「有该字段值的最新期」（快照可为字段级混合期，合格字段按自身
    财期保留——protected 语义），原「首期有任意字段即返回」会丢失上一已披
    露期完整核心财务。现从最近季度末往回逐期请求（每期九指标一批）：

    - 字段级合并：每字段在**最先遇到的有值期**定格（即该字段有值的最新
      期），后续期只填仍缺字段；各字段 ``period`` 真实标注自身财期，不把
      不同财期混称同一期；
    - 自然缺一项（银行毛利率）不再导致弃用 API 或丢弃已合并核心财务；
    - 每期披露时点校验不变（未披露/回退/乱序期该期整体拒绝跳过）；
    - 预算线（第三十八轮）与取消穿透不变；全字段合并齐即提前完成；
    - 预算只在入口预留一次（第五十轮，MCP 兜底份额）：各单期出站复用
      该绝对 API 截止，不再逐期重套 ``_finance_api_deadline`` 重复预留
      （后续财期不重开预算）；单次传输在 curl 边界另受该次配置单请求
      上限收窄（``min(剩余, IFIND_HTTP_TIMEOUT_SECONDS)``，默认 30 秒），
      共享父截止、5 秒处理余量与保留已取得读数语义不变。

    全部候选耗尽仍无任何字段 → ``SourceReadError`` 走 MCP 原路径。
    """
    import logging

    from data_provider.cross_source_validator import SourceReadError
    from src.core.research_run import ResearchStopped
    from src.patches.eastmoney_patch import request_deadline

    logger = logging.getLogger(__name__)
    identity = http_qualified_identity(code)
    if identity is None:
        raise SourceReadError("ifind", "permission_denied")
    errors: List[str] = []
    api_deadline = _finance_api_deadline(float(deadline))
    floor = api_deadline - _HTTP_PARSE_STAGE_SECONDS
    merged: Dict[str, Any] = {}
    deadline_token = request_deadline.set(api_deadline)
    try:
        for candidate in _quarter_end_candidates(_date.today()):
            if monotonic() >= floor:
                logger.warning(
                    "ifind http finance %s 探测预算线到达，剩余期次转由 MCP 兜底", code,
                )
                break
            try:
                # 复用入口一次预留的绝对 API 截止（第五十轮：不逐期重开预算）。
                readings = _ifind_http_financial_snapshot_once(code, candidate, deadline=api_deadline)
            except SourceReadError as exc:
                # 该期整体不合格（未披露/回退/乱序/无值）跳过；资格/凭据类失败传播。
                if exc.reason_code == "invalid_response":
                    errors.append(f"{candidate}:invalid_response")
                    continue
                raise
            except ResearchStopped:
                # 用户取消是 TimeoutError 子类：探测阶段的取消同样穿透，
                # 不被保留块吞掉（第四十轮）。
                raise
            except (DataFetchError, TimeoutError) as exc:
                # 非取消的网络/deadline 失败：已累积的合格字段事实不丢弃
                # （第四十轮——自然缺毛利率的银行尤其易在后续期传输失败），
                # 停止探测返回已合并快照，由调用方按剩余预算安全补缺/披露；
                # 无任何已累积事实时按失败传播（无事实可保，不伪装成功）。
                if merged:
                    logger.warning(
                        "ifind http finance %s 探测失败（%s），保留已合并 %d 字段转交补缺",
                        code, type(exc).__name__, len(merged),
                    )
                    break
                raise
            # 字段级合并：每字段取有值的最新期（与 MCP 多期解析同语义，
            # 期次选择最终由消费层 select_current_financials 负责——适配器
            # 只提供真实读数候选，不臆造财报事实）。资产/负债单侧读数同样
            # 按真实期入候选（下游 ``_qualified_ifind_balance`` 同主期装配
            # 契约不变：与损益主期不同期则装配拒绝、缺口如实披露——不为
            # 最终必被拒的旧期配对额外扫描，第四十四轮）。
            for field, reading in readings.items():
                if field not in merged:
                    merged[field] = reading
            if len(merged) == len(_FINANCE_FIELD_SPECS):
                break  # 全字段已齐（通常最新完整样本一期完成）
            # 其余缺失字段（自然缺项/供应商缺口）继续在候选期内探测「有值的
            # 最新期」（第四十五轮：与 latest 逐字段最新承诺一致——自然缺项
            # 如银行毛利率在更早期有值则合并、真实期标注；有界性由候选期
            # 上限与预算线承载，无谓无限扫描不存在；探测后仍缺字段走既有
            # MCP 补缺，再缺如实降级不假称等价）。
        if not merged:
            logger.warning(
                "ifind http finance %s 多期探测未命中任何字段: %s", code, "; ".join(errors) or "无候选",
            )
            raise SourceReadError("ifind", "invalid_response")
        return merged
    finally:
        request_deadline.reset(deadline_token)


def _company_profile_indiparams(today: str) -> List[Dict[str, object]]:
    """公司资料批次：各指标按官方模板出参（D=查询基准日；无参指标不传参）。"""
    spec = [
        ("ths_corp_profile_stock", []),
        ("ths_main_businuess_stock", [today]),
        ("ths_mo_product_name_stock", []),
        ("ths_actual_controller_stock", [today]),
        ("ths_actual_controller_type_stock", [today]),
    ]
    return [{"indicator": indicator, "indiparams": params} for indicator, params in spec]


def _concepts_indiparams(today: str) -> List[Dict[str, object]]:
    """概念/行业批次：概念 D=查询基准日；申万行业 LT(一级)+D。"""
    return [
        {"indicator": "ths_the_concept_stock", "indiparams": [today]},
        {"indicator": "ths_the_sw_industry_stock", "indiparams": ["1", today]},
    ]


def _post_authorized_generic(
    identity: str, indipara: List[Dict[str, object]], *, label: str,
    access_token: str, refresh_token: str, deadline: float,
) -> Tuple[Dict[str, object], str]:
    """公司资料/概念批次的认证 POST（401/403 强刷重试一次，与财务快照一致）。"""
    headers = {"Content-Type": "application/json", "access_token": access_token, "ifindlang": "cn"}
    body = json.dumps({"codes": identity, "indipara": indipara}, default=str).encode("utf-8")
    status, response_body = _http_api_curl_post(
        _FINANCE_BASIC_ENDPOINT, headers, body, label=label,
    )
    used = access_token
    if status in (401, 403) and refresh_token:
        refreshed = _resolve_access_token(refresh_token, force_refresh=True)
        headers = {**headers, "access_token": refreshed}
        status, response_body = _http_api_curl_post(
            _FINANCE_BASIC_ENDPOINT, headers, body, label=label,
        )
        used = refreshed
    if status != 200:
        raise DataFetchError(f"ifind http {label} http status {status}")
    return _parse_json_payload(response_body, label=label), used


def ifind_http_company_profile(code: str, *, deadline: float) -> Dict[str, str]:
    """标准 API 公司资料批次（第五十五轮）：五字段单值文本（空=该字段缺失）。

    字段→指标映射（官方模板逐指标核实）：corp_profile/profile 无参、
    main_businuess/main_products D=查询基准日（main_businuess 缺 D 返回空
    ——第五十五轮实测）、controller/controller_type D=查询基准日。
    证券归属精确匹配；空值字段缺（CompanyProfile 允许部分 facts，name 由
    消费层既有 stock_name 承载——API 无独立名称指标，缺口如实）。
    """
    from data_provider.cross_source_validator import SourceReadError
    from src.config import get_config
    from src.patches.eastmoney_patch import request_deadline

    identity = http_qualified_identity(code)
    if identity is None:
        raise SourceReadError("ifind", "permission_denied")
    config = get_config()
    refresh = (getattr(config, "ifind_http_refresh_token", None) or "").strip()
    if not refresh:
        raise SourceReadError("ifind", "invalid_configuration")
    today = _date.today().isoformat()
    deadline_token = request_deadline.set(float(deadline))
    try:
        access_token = _resolve_access_token(refresh)
        indipara = _company_profile_indiparams(today)
        payload, used = _post_authorized_generic(
            identity, indipara, label="company_profile",
            access_token=access_token, refresh_token=refresh, deadline=deadline,
        )
        _require_zero_errorcode(
            payload, identity, access_token=used, extra_secrets=_redaction_secrets(refresh, used),
        )
        tables = payload.get("tables")
        if not isinstance(tables, list) or len(tables) != 1 or not isinstance(tables[0], dict):
            raise DataFetchError(f"ifind http company_profile {identity} unexpected tables shape")
        entry = tables[0]
        returned = str(entry.get("thscode") or "")
        if returned.strip().upper() != identity.strip().upper():
            raise DataFetchError(
                f"ifind http company_profile {identity} returned {returned or 'unknown'}")
        table = entry.get("table")
        if not isinstance(table, dict):
            raise DataFetchError(f"ifind http company_profile {identity} missing table")
        facts: Dict[str, str] = {}
        for key, indicator, *_ in (
            ("profile", "ths_corp_profile_stock",),
            ("main_business", "ths_main_businuess_stock",),
            ("main_products", "ths_mo_product_name_stock",),
            ("controller", "ths_actual_controller_stock",),
            ("controller_type", "ths_actual_controller_type_stock",),
        ):
            cells = table.get(indicator)
            raw = (cells or [None])[0] if isinstance(cells, list) and cells else None
            # 仅接受非空字符串（第五十八轮：布尔/数字/对象/数组不得经 str()
            # 伪装成资料文本）；[None]/占位符如实缺失不补造。
            if not isinstance(raw, str):
                continue
            value = raw.strip()
            if not value or value in {"-", "--", "\t", "None"}:
                continue
            facts[key] = value
        if not facts:
            raise SourceReadError("ifind", "invalid_response")
        return facts
    finally:
        request_deadline.reset(deadline_token)


def ifind_http_concept_membership(code: str, *, deadline: float) -> List[str]:
    """标准 API 概念批次（第五十五轮）：查询时点最新归属（D=查询基准日）。

    概念单元格为**英文逗号分隔串**（与 MCP ``_parse_concepts`` 分隔符一致）；
    申万行业一级同批取得（不混入 concepts——消费层 ConceptMembership 只含
    概念）。空概念 → ``invalid_response`` 走 MCP 兜底。
    """
    from data_provider.cross_source_validator import SourceReadError
    from src.config import get_config
    from src.patches.eastmoney_patch import request_deadline

    identity = http_qualified_identity(code)
    if identity is None:
        raise SourceReadError("ifind", "permission_denied")
    config = get_config()
    refresh = (getattr(config, "ifind_http_refresh_token", None) or "").strip()
    if not refresh:
        raise SourceReadError("ifind", "invalid_configuration")
    today = _date.today().isoformat()
    deadline_token = request_deadline.set(float(deadline))
    try:
        access_token = _resolve_access_token(refresh)
        payload, used = _post_authorized_generic(
            identity, _concepts_indiparams(today), label="concepts",
            access_token=access_token, refresh_token=refresh, deadline=deadline,
        )
        _require_zero_errorcode(
            payload, identity, access_token=used, extra_secrets=_redaction_secrets(refresh, used),
        )
        tables = payload.get("tables")
        if not isinstance(tables, list) or len(tables) != 1 or not isinstance(tables[0], dict):
            raise DataFetchError(f"ifind http concepts {identity} unexpected tables shape")
        entry = tables[0]
        returned = str(entry.get("thscode") or "")
        if returned.strip().upper() != identity.strip().upper():
            raise DataFetchError(
                f"ifind http concepts {identity} returned {returned or 'unknown'}")
        table = entry.get("table")
        if not isinstance(table, dict):
            raise DataFetchError(f"ifind http concepts {identity} missing table")
        concepts_cell = table.get("ths_the_concept_stock")
        raw_cell = (concepts_cell or [None])[0] if isinstance(concepts_cell, list) and concepts_cell else None
        # 仅接受非空字符串（布尔/数字/对象/数组不得经 str() 伪装概念串）
        concepts_text = (
            raw_cell.strip()
            if isinstance(raw_cell, str) and raw_cell.strip() not in {"-", "--", "None"}
            else ""
        )
        concepts = [name.strip() for name in concepts_text.split(",") if name.strip()]
        # 分割后逐项过滤占位符（混合占位 "白酒,--,None" 不得保留伪概念）
        concepts = [name for name in concepts if name not in {"-", "--", "None"}]
        concepts = list(dict.fromkeys(concepts))
        if not concepts:
            raise SourceReadError("ifind", "invalid_response")
        return list(dict.fromkeys(concepts))
    finally:
        request_deadline.reset(deadline_token)


def _finance_failure_code(exc: Optional[BaseException]) -> str:
    """API 失败原因 → 既有 SourceReadError 白名单内最贴近的 code（第四十七轮）。

    保留真实失败类别（timeout/business_error/authentication_failed/
    invalid_response/…），不统一折叠成 invalid_configuration 误导为 MCP 配置
    问题；凭据/资格类映射到语义等价的既有 code。可安全输出的诊断文本由
    fallback 的 warning 日志承载（经凭据脱敏）。
    """
    from data_provider.cross_source_validator import SourceReadError

    if exc is None:
        # API 未参与（凭据缺失/开关门拦截）：配置语义（不虚称来源失败）。
        return "invalid_configuration"
    if isinstance(exc, SourceReadError):
        known = {
            "identity_not_eligible_for_http": "permission_denied",
            "http_credentials_missing": "invalid_configuration",
        }
        return known.get(exc.reason_code, exc.reason_code)
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, _HttpAuthError):
        return "authentication_failed"
    if isinstance(exc, DataFetchError):
        # 传输层状态保留具体 http_XXX 语义（既有
        # ``source_failure_reason``/SourceReadError 白名单支持 http_1xx-5xx）；
        # 认证与限流沿既有语义类，供应商业务码不作传输层状态混同。
        import re as _re

        text = str(exc)
        if "http status 401" in text or "http status 403" in text:
            return "authentication_failed"
        if "http status 429" in text:
            return "rate_limited"
        m = _re.search(r"http status (\d{3})", text)
        if m:
            return f"http_{m.group(1)}"
        return "source_error"
    return "source_error"


def company_profile_with_fallback(
    code: str, *, deadline: float,
    mcp_endpoint: Optional[str], mcp_token: Optional[str], mcp_timeout: float,
) -> "CompanyProfile":
    """公司资料统一入口：标准 API 优先，失败/不合格走 MCP 原路径。

    - 预算预留按实际可用计算（``_finance_api_deadline``：API 阶段为 MCP
      兜底留正剩余份额，总 deadline/取消语义不变）；
    - MCP 兜底/补缺参与按**显式开关+实际配置**同时判定（``mcp_ready``）；
    - **部分资料补缺**：API 部分字段非空但缺字段（含 name——API 无独立名称
      指标，深研 scope stock_name 由 company.NAME 承载）时，MCP 同字段补入
      并保留 MCP CompanyFact **原对象**（value/observed_at/raw_column 均为
      MCP 真实元数据，渠道标注不伪装同一来源）；
    - 取消（``ResearchStopped`` 是 ``TimeoutError`` 子类）在 ``TimeoutError``
      之前显式穿透——不吞、不继续 MCP 出站、不延长预算；
    - MCP 关闭或未配置：API 失败按实际类别上抛（``_finance_failure_code``），
      诊断文本在 warning（已脱敏），异常链保留。
    """
    import logging

    from data_provider.cross_source_validator import SourceReadError
    from data_provider.ifind_fundamental_adapter import CompanyFact, CompanyProfile, IfindFetcher
    from src.config import get_config
    from src.core.research_run import ResearchStopped, current_research
    from src.patches.eastmoney_patch import request_deadline

    logger = logging.getLogger(__name__)

    def _safe(msg: str) -> str:
        from src.config import get_config as _cfg
        return _redact_token(str(msg), (getattr(_cfg(), "ifind_http_refresh_token", None) or "").strip())

    config = get_config()
    mcp_ready = bool(config.enable_ifind_mcp) and bool(mcp_endpoint) and bool(mcp_token)
    api_readings: Dict[str, Any] = {}
    api_failure: Optional[BaseException] = None
    api_deadline = _finance_api_deadline(float(deadline))
    if ifind_http_daily_enabled():
        if api_deadline - monotonic() < 1.0:
            # 本地预算耗尽（份额不足最小可执行请求）≠ 供应商接口超时：仍按
            # 既有语义出站（curl 即刻硬截止），日志明确区分，不误述为远端故障。
            logger.warning(
                "company_profile API 本地预算耗尽（份额 %.3fs 不足最小可执行请求），"
                "出站将即刻硬截止（本地预算耗尽，非供应商超时）",
                api_deadline - monotonic(),
            )
        deadline_token = request_deadline.set(api_deadline)
        try:
            api_readings = ifind_http_company_profile(code, deadline=api_deadline)
        except SourceReadError as exc:
            api_failure = exc
            logger.warning(
                "company_profile API 不合格（%s: %s），回退 MCP", code, exc.reason_code)
        except DataFetchError as exc:
            api_failure = exc
            logger.warning("company_profile API 失败，回退 MCP：%s", _safe(exc))
        except ResearchStopped:
            # 用户取消是 TimeoutError 子类：先于 TimeoutError 穿透。
            raise
        except TimeoutError as exc:
            api_failure = exc
            logger.warning("company_profile API deadline 耗尽，回退 MCP：%s", _safe(exc))
        finally:
            request_deadline.reset(deadline_token)

    indicator_by_key = {
        "profile": "ths_corp_profile_stock",
        "main_business": "ths_main_businuess_stock",
        "main_products": "ths_mo_product_name_stock",
        "controller": "ths_actual_controller_stock",
        "controller_type": "ths_actual_controller_type_stock",
    }
    mcp_errors: List[str] = []
    mcp_profile: Optional[CompanyProfile] = None
    # MCP 兜底：API 整体失败 → MCP 快照整批；API 部分字段（含 name）→ MCP 同字段补缺。
    # name 缺失（API 无独立名称指标）也触发 MCP 兜底——深研 scope stock_name
    # 由 company.NAME 承载（base.py:5423-5428）。
    if (api_readings is None or missing_fields(api_readings, indicator_by_key)
            or "name" not in api_readings) and mcp_ready:
        if deadline - monotonic() <= 0:
            # 预算耗尽后未派发 ≠ 供应商超时：如实区分（不发起无预算出站）。
            logger.warning(
                "company_profile MCP 补缺本地预算耗尽（未派发，非供应商超时）")
            mcp_errors.append("mcp:company:timeout")
            mcp_profile = None
        else:
            try:
                mcp_profile = IfindFetcher(
                    mcp_endpoint, mcp_token, mcp_timeout,
                ).company_profile(code, deadline=deadline)
            except SourceReadError as exc:
                mcp_errors.append(f"mcp:company:{exc.reason_code}")
                logger.warning(
                    "company_profile MCP 兜底失败（%s: %s）", code, exc.reason_code)
                mcp_profile = None
            except ResearchStopped:
                raise
            except (DataFetchError, TimeoutError) as exc:
                mcp_errors.append(f"mcp:company:{_finance_failure_code(exc)}")
                logger.warning("company_profile MCP 兜底失败：%s", _safe(exc))
                mcp_profile = None
        if not api_readings:
            # API 未产出任何有效字段（HTTP 关/失败/资格拒绝且 MCP 关）→
            # 按实际类别上抛（不返回空 facts CompanyProfile 伪装成功）。
            if mcp_profile is not None:
                return mcp_profile  # MCP 快照整批兜底（含 MCP errors 如实传播）
            if mcp_errors:
                # API 未参与（http 关/资格外）且 MCP 兜底失败：传播 MCP 真实
                # 失败类别（``mcp:company:<code>`` 首项）——不折叠成
                # invalid_configuration 掩盖 timeout 等供应商失败（既有测试
                # 契约 ifind:company:timeout 语义）。
                prefix = "mcp:company:"
                reason = next((item[len(prefix):] for item in mcp_errors
                               if item.startswith(prefix) and item[len(prefix):]),
                              _finance_failure_code(api_failure))
                raise SourceReadError("ifind", reason) from api_failure
            raise SourceReadError("ifind", _finance_failure_code(api_failure))
        # 部分资料补缺：仅补 API 缺失字段，保留 MCP CompanyFact **原对象**；
        # MCP errors 随补缺合并进最终 CompanyProfile（原错误披露不丢失）。
        fill_targets = missing_fields(api_readings, indicator_by_key)
        if "name" not in api_readings and current_research.get() is not None:
            # name 契约（第六十轮）：API 无独立名称指标——深研 scope stock_name
            # 由 company.NAME 承载（base.py:5423-5428），name 缺失必然 → MCP
            # 同批补 name fact（需名称时利用原 MCP，不凭空造值）；非深研无该
            # 消费、不多出站。
            fill_targets = list(dict.fromkeys((*fill_targets, "name")))
        for key in fill_targets:
            fact = mcp_profile.facts.get(key) if mcp_profile is not None else None
            if fact is not None:
                api_readings[key] = fact  # 渠道标注保持 MCP 原样
        mcp_errors = list(dict.fromkeys(
            (*mcp_errors, *(mcp_profile.errors if mcp_profile is not None else ()))))

    if not api_readings:
        raise SourceReadError("ifind", _finance_failure_code(api_failure))
    facts_obj: Dict[str, CompanyFact] = {}
    for key, value in api_readings.items():
        if isinstance(value, CompanyFact):
            facts_obj[key] = value  # MCP CompanyFact 原对象（元数据真实）
        else:
            # API 字段：值/渠道（raw_column=API 指标名）按 API 渠道构造
            facts_obj[key] = CompanyFact(
                value=value, observed_at=None, raw_column=indicator_by_key.get(key))
    return CompanyProfile(facts=facts_obj, errors=list(dict.fromkeys(mcp_errors)))


def missing_fields(facts: Dict[str, Any], indicator_by_key: Dict[str, str]) -> List[str]:
    """API 快照缺字段清单（值可为 str（API）或 CompanyFact（MCP 补缺））."""
    return [k for k in indicator_by_key if k not in facts]



def concept_membership_with_fallback(
    code: str, *, deadline: float,
    mcp_endpoint: Optional[str], mcp_token: Optional[str], mcp_timeout: float,
) -> "ConceptMembership":
    """概念统一入口：标准 API 优先（查询时点最新归属），失败走 MCP 原路径。

    - 预算预留按实际可用计算（``_finance_api_deadline``，第四十八轮公式）；
    - MCP 兜底参与按**显式开关+实际配置**（``mcp_ready``，第四十六轮）；
    - 取消（``ResearchStopped`` 是 ``TimeoutError`` 子类）在 ``TimeoutError``
      之前显式穿透（第五十六轮顺序修正）；API 失败按实际类别上抛。
    """
    import logging

    from data_provider.cross_source_validator import SourceReadError
    from data_provider.ifind_fundamental_adapter import ConceptMembership, IfindFetcher
    from src.config import get_config
    from src.core.research_run import ResearchStopped
    from src.patches.eastmoney_patch import request_deadline

    logger = logging.getLogger(__name__)

    def _safe(msg: str) -> str:
        from src.config import get_config as _cfg
        return _redact_token(str(msg), (getattr(_cfg(), "ifind_http_refresh_token", None) or "").strip())

    config = get_config()
    mcp_ready = bool(config.enable_ifind_mcp) and bool(mcp_endpoint) and bool(mcp_token)
    api_readings: Optional[List[str]] = None
    api_failure: Optional[BaseException] = None
    api_deadline = _finance_api_deadline(float(deadline))
    if ifind_http_daily_enabled():
        deadline_token = request_deadline.set(api_deadline)
        try:
            concepts = ifind_http_concept_membership(code, deadline=api_deadline)
            api_readings = concepts
        except SourceReadError as exc:
            api_failure = exc
            logger.warning(
                "concept_membership API 不合格（%s: %s），回退 MCP", code, exc.reason_code)
        except DataFetchError as exc:
            api_failure = exc
            logger.warning("concept_membership API 失败，回退 MCP：%s", _safe(exc))
        except ResearchStopped:
            # 用户取消是 TimeoutError 子类：先于 TimeoutError 穿透。
            raise
        except TimeoutError as exc:
            api_failure = exc
            logger.warning("concept_membership API deadline 耗尽，回退 MCP：%s", _safe(exc))
        finally:
            request_deadline.reset(deadline_token)
    if api_readings is not None:
        from data_provider.ifind_fundamental_adapter import ConceptMembership

        return ConceptMembership(code=code, concepts=list(dict.fromkeys(api_readings)), errors=[])
    if not mcp_ready:
        code_ = _finance_failure_code(api_failure)
        logger.warning(
            "concept_membership %s MCP 不可用，按 API 实际失败原因上抛（%s）：%s",
            code, code_, _safe(api_failure),
        )
        raise SourceReadError("ifind", code_) from api_failure
    return IfindFetcher(mcp_endpoint, mcp_token, mcp_timeout).concept_membership(
        code, deadline=deadline,
    )


def financial_snapshot_with_fallback(
    code: str,
    period: Optional[str],
    *,
    deadline: float,
    mcp_endpoint: Optional[str],
    mcp_token: Optional[str],
    mcp_timeout: float,
) -> Dict[str, Any]:
    """财务快照统一入口：标准 API 优先，失败/不合格走 MCP 原路径（快照级）。

    - API 参与条件：``ifind_http_daily_enabled()``（开关关闭即完全不参与）；
      ``period`` 具体值走单期入口；``None`` 走主源多期探测
      （``ifind_http_financial_snapshot_latest``）——主源路径不再直落 MCP；
    - **逐字段补缺**：API 快照非空但缺字段（如银行毛利率自然缺项）时，MCP
      快照可用则按**同一财期**补缺（期不一致不拼——财期身份必须明确），
      每字段保留各自 ``source_family``（不伪装成同一来源）；
    - MCP 失败按既有 ``SourceReadError`` 传播（上层走 Choice 等既有兜底）；
      用户取消（``ResearchStopped``）不吞、不继续消耗 MCP 出站。
    """
    import logging

    from data_provider.cross_source_validator import SourceReadError
    from data_provider.ifind_fundamental_adapter import IfindFetcher
    from src.config import get_config
    from src.core.research_run import ResearchStopped

    logger = logging.getLogger(__name__)
    # 回退日志二次脱敏（防御深度）：异常消息在实现层已按本次凭据脱敏，
    # 此处再按本机 refresh_token 过滤一次，防上游未脱敏消息经日志泄漏。
    def _safe(msg: str) -> str:
        from src.config import get_config as _cfg
        return _redact_token(str(msg), (getattr(_cfg(), "ifind_http_refresh_token", None) or "").strip())

    api_readings: Optional[Dict[str, Any]] = None
    api_failure: Optional[BaseException] = None
    if ifind_http_daily_enabled():
        try:
            if period:
                api_readings = ifind_http_financial_snapshot(code, period, deadline=deadline)
            else:
                api_readings = ifind_http_financial_snapshot_latest(code, deadline=deadline)
        except SourceReadError as exc:
            api_failure = exc
            logger.warning(
                "financial_snapshot API 不合格（%s: %s），回退 MCP", code, _safe(exc.reason_code),
            )
        except DataFetchError as exc:
            api_failure = exc
            logger.warning("financial_snapshot API 失败，回退 MCP：%s", _safe(exc))
        except ResearchStopped:
            # 用户取消是 TimeoutError 子类，必须先于 TimeoutError 穿透：
            # 取消不是供应商失败，不回退、不消耗 MCP 出站、不延长预算。
            raise
        except TimeoutError as exc:
            api_failure = exc
            # curl/token deadline 耗尽（非取消）：按既有单源失败契约回退；
            # MCP 复用同一绝对 deadline，不延长原预算。
            logger.warning("financial_snapshot API deadline 耗尽，回退 MCP：%s", _safe(exc))
    # MCP 兜底/补缺的参与判定（第四十六轮）：显式开关 + 实际配置同时成立
    # ——用户关闭 MCP 但遗留 endpoint/token 时不得错误出站。
    mcp_ready = bool(get_config().enable_ifind_mcp) and bool(mcp_endpoint) and bool(mcp_token)
    if api_readings is None:
        if not mcp_ready:
            # MCP 关闭或未配置：保留 API 实际失败原因（第四十七轮——统一
            # invalid_configuration 会误导为 MCP 配置问题并丢失真实原因）：
            # 映射到既有白名单内最贴近的 code，诊断文本在 warning（已脱敏），
            # 异常链保留原始异常。
            code = _finance_failure_code(api_failure)
            logger.warning(
                "financial_snapshot %s MCP 不可用，按 API 实际失败原因上抛（%s）：%s",
                code, code, _safe(api_failure),
            )
            raise SourceReadError("ifind", code) from api_failure
        return IfindFetcher(mcp_endpoint, mcp_token, mcp_timeout).financial_snapshot(
            code, period, deadline=deadline,
        )

    config = get_config()
    # 缺失或无效（NaN/±Inf 占位）的字段都需要 MCP 补缺：无效值占位不得
    # 阻止合格 MCP 值补回（第三十五轮审查）。
    missing = [
        f for f, _i, _u, _form, _cal in _FINANCE_FIELD_SPECS
        if f not in api_readings or not math.isfinite(api_readings[f].value)
        # 口径待补（第二十轮）：值在但 statement_scope/adjustment 未证明
        # （d_only 指标无请求参数证据）的字段同样交 MCP 补缺——不把未知
        # 口径值伪装成合格快照阻塞 fallback；MCP 候选携带自身真实口径拼入
        # （期一致+有限既有条件不变），不可用则消费层既有 unknown 资格
        # 路径如实降级（不虚构 restated）。
        or api_readings[f].statement_scope is None or api_readings[f].adjustment is None
    ]
    if missing and mcp_ready:
        # 补缺财期契约按调用形态区分（第四十轮修正第三十九轮的过宽合并）：
        # - ``period`` 具体值（Choice 次源/单期契约）：MCP 候选经归一化比较
        #   **等于请求期才拼**——指定 2026-09-30 不得填入 2025-12-31；
        # - ``period=None``（主源多期）：protected 语义——MCP 候选携带自身
        #   财期真实标注补入（不把不同财期混称同一期，消费层按字段自身期
        #   处理）。无效候选（None/非有限）不占位；渠道标注保持 MCP 原样。
        mcp_readings: Optional[Dict[str, Any]] = None
        try:
            if mcp_endpoint and mcp_token:
                mcp_readings = IfindFetcher(mcp_endpoint, mcp_token, mcp_timeout).financial_snapshot(
                    code, period, deadline=deadline,
                )
            api_period_key = (
                _normalized_period_key(period) if period
                else _normalized_period_key(next(iter(api_readings.values())).period)
            )
            for field in missing:
                candidate = mcp_readings.get(field)
                if candidate is None or candidate.value is None:
                    continue
                if not math.isfinite(candidate.value):
                    continue  # MCP 候选同样无效则不占位
                if period:
                    if _normalized_period_key(candidate.period) != api_period_key:
                        logger.warning(
                            "financial_snapshot %s MCP 补缺候选财期 %s 与请求期 %s 不符，拒绝拼入",
                            code, candidate.period, period,
                        )
                        continue  # 具体期契约：归一化不等不拼
                logger.info(
                    "financial_snapshot %s 字段 %s 由 MCP 补缺（财期 %s）",
                    code, field, candidate.period,
                )
                api_readings[field] = candidate  # source/source_family/period 保持 MCP 值
        except SourceReadError as exc:
            # 补缺失败如实保留 API 缺失/无效字段，不伪装（日志保真；第二十
            # 二轮：不借 normalization_error 扩展诊断协议——该通道承载口径
            # 未知等原始拒因，供应商超时不覆盖之，缺失字段也不因此获因）。
            logger.warning("financial_snapshot %s MCP 补缺失败：%s", code, exc.reason_code)
        except DataFetchError as exc:
            logger.warning("financial_snapshot %s MCP 补缺传输失败：%s", code, _safe(exc))
        except ResearchStopped:
            # 用户取消是 TimeoutError 子类，必须先于 TimeoutError 穿透：
            # 补缺阶段的取消同样不吞、不伪装为补缺成功。
            raise
        except TimeoutError as exc:
            # 补缺超时不阻断已取得的 API 快照（预算不延长）；与其他补缺
            # 失败同按既有日志方式留痕（脱敏 warning）。
            logger.warning("financial_snapshot %s MCP 补缺超时：%s", code, _safe(exc))
    return api_readings


def _normalized_period_key(period: Optional[str]) -> Optional[str]:
    """财期比较键（YYYY-MM-DD 标准形），期不一致不拼接。"""
    if not period:
        return None
    from data_provider.cross_source_validator import normalize_report_period

    return normalize_report_period(period)[0]


# ===== 深研个股资金流（主力净流入逐日序列）标准 API（第十一轮接入）=====
# 证据边界（.claude/reviews/ifind-http-capital-flow-research-20261117.md +
# 协调者补证）：四证券（600519/600031/000001/301269）×20 交易日双源对照
# 80 日期重合、相对差均 <0.1%（最大 0.00238%），MCP 严格元口径——样本级
# 支持数值为元，非供应商书面保证；官方 get_indicator_params 对该指标的
# 日期序列模板为**空参数列表**（[] 与 ["","100"]/["","101"] 实测同值，
# 100 不构成单位枚举），实施按官方模板空参数、不虚构参数含义。停牌日
# Fill=Blank 返回 null 占位（600176 单次停牌样本，不外推全部停牌形态）；
# 盘中当日值最终性未证明——**不可仅依据非空认定当日已收盘**：请求
# enddate 由消费方以本地完成交易日历锚定，解析层强制丢弃晚于 enddate
# 的任何行（未定稿日混入也不得采用）。
_FLOW_ENDPOINT = "https://quantapi.51ifind.com/api/v1/date_sequence"
_FLOW_INDICATOR = "ths_net_inflow_amt_stock"


def _flow_body(identity: str, start_compact: str, end_compact: str) -> Dict[str, object]:
    """date_sequence 请求体：官方模板空参数 + 严格交易日 + Blank 空值。"""
    return {
        "codes": identity,
        "startdate": start_compact,
        "enddate": end_compact,
        "functionpara": {"Days": "Tradedays", "Fill": "Blank"},
        "indipara": [{"indicator": _FLOW_INDICATOR, "indiparams": []}],
    }


def _parse_flow_series(
    payload: Dict[str, object], identity: str, start_iso: str, end_iso: str, *,
    access_token: str, extra_secrets: Tuple[str, ...],
) -> List[Tuple[str, Optional[float]]]:
    """解析逐日主力净流入序列（元，null 保留为缺值行）。

    契约：errorcode=0、单表、thscode 归属（``_same_security_identity``）、
    time 回显与值行数一致；行日期归一 YYYY-MM-DD、**晚于 enddate 的行一律
    丢弃**（盘中未定稿日不得进入——数据新鲜度边界由本地完成日历锚定）；
    值仅接受 None（缺值，保留为 (day, None) 供消费层窗口不完整披露）或
    有限 float——bool/文本/非有限一律整体拒绝（不可解释形态回 MCP）；
    同日重复/冲突拒绝；空序列拒绝（与参数级失败不可区分）。
    """
    import math as _math
    from data_provider.cross_source_validator import SourceReadError, normalize_report_period

    _require_zero_errorcode(payload, identity, access_token=access_token, extra_secrets=extra_secrets)
    tables = payload.get("tables")
    if not isinstance(tables, list) or len(tables) != 1 or not isinstance(tables[0], dict):
        raise DataFetchError(f"ifind http flow {identity} unexpected tables shape")
    entry = tables[0]
    returned_code = str(entry.get("thscode") or "")
    if not _same_security_identity(returned_code, identity):
        raise DataFetchError(f"ifind http flow {identity} returned {returned_code or 'unknown'}")
    time_list = entry.get("time")
    table = entry.get("table")
    cells = table.get(_FLOW_INDICATOR) if isinstance(table, dict) else None
    if not isinstance(time_list, list) or not isinstance(cells, list) or len(time_list) != len(cells):
        raise SourceReadError("ifind", "invalid_response")

    series: List[Tuple[str, Optional[float]]] = []
    by_date: Dict[str, Optional[float]] = {}
    for raw_day, raw_value in zip(time_list, cells):
        day, _basis = normalize_report_period(raw_day)
        if day is None:
            raise SourceReadError("ifind", "invalid_response")  # 日期不可证明的行整体拒绝
        if day > end_iso:
            continue  # 晚于本地完成日历锚定日的行：盘中未定稿，一律丢弃不采用
        if day < start_iso:
            continue  # 早于请求窗口的行：供应商多给的窗口外旧数据不采信（与
            # 上界对称的严格窗口边界；防止窗口内全 null 时被窗口外旧日非空
            # 误判「有有效值」而跳过 MCP——第十三轮补充复审反例）
        value: Optional[float]
        if raw_value is None:
            value = None  # 停牌/未就绪占位：保留为缺值行（消费层不补零）
        elif isinstance(raw_value, bool) or not isinstance(raw_value, (int, float, Decimal)):
            raise SourceReadError("ifind", "invalid_response")  # bool/文本不可伪装数值
        else:
            # ``_parse_json_payload`` 的精确十进制契约给 Decimal：bool 已排除，
            # int/float/Decimal 均为合法数值形态（Decimal 有限性单独判定）。
            value = float(raw_value)
            if not _math.isfinite(value) or (isinstance(raw_value, Decimal) and not raw_value.is_finite()):
                raise SourceReadError("ifind", "invalid_response")
        if day in by_date and by_date[day] != value:
            raise SourceReadError("ifind", "invalid_response")  # 同日冲突不可解释
        by_date[day] = value
        series.append((day, value))
    if not series or not any(value is not None for _day, value in series):
        # 空序列或**全 null**（零有效数值）：无法与参数级失败/异常响应区分，
        # 不是可采信的「正常无数据」证明——整体拒绝，交还 MCP 原路径裁决
        # （个别日期 null 仍保留为缺值行，窗口不完整披露语义不变）。
        raise SourceReadError("ifind", "invalid_response")
    return series


def ifind_http_main_inflow_series(
    code: str, *, deadline: float, start_iso: str, end_iso: str,
) -> List[Tuple[str, Optional[float]]]:
    """标准 API 逐日主力净流入序列（深研资金窗 HTTP 候选，元口径）。

    ``start_iso``/``end_iso``（YYYY-MM-DD）由消费方以**本地完成交易日历**
    锚定——enddate 恒为最近已完成交易日，响应不含盘中日（解析层再防御性
    丢弃更晚行）。失败统一抛 ``SourceReadError``/``DataFetchError`` 由调用
    方回 MCP 原路径。资金专用尝试预留由唯一业务调用者（资金路由层
    ``capital_flow_provider``）一次分配——本入口**不再划拨后备份额**，但
    仍将本次尝试截止与「当前父截止」「HTTP 自建上限（配置 timeout + 解析
    余量）」夹取（认证/强刷/业务请求同窗），更短外层截止照旧优先。取消
    （``ResearchStopped``）照 curl 边界穿透。凭据/传输复用既有 authorized
    模式（401/403 强刷重试一次）。本函数日志只承载「取得候选」语义：实际
    渠道采纳由资金消费层确认后记录。
    """
    from data_provider.cross_source_validator import SourceReadError
    from src.config import get_config
    from src.patches.eastmoney_patch import request_deadline

    identity = http_qualified_identity(code)
    if identity is None:
        raise SourceReadError("ifind", "permission_denied")
    config = get_config()
    refresh = (getattr(config, "ifind_http_refresh_token", None) or "").strip()
    if not refresh:
        raise SourceReadError("ifind", "invalid_configuration")
    start_compact = start_iso.replace("-", "")
    end_compact = end_iso.replace("-", "")
    # 末位尝试截止：不预留，但保留请求级上限与更短父截止（min 三取一）。
    _limits = [float(deadline)]
    _outer = request_deadline.get()
    if _outer is not None:
        _limits.append(float(_outer))
    _cap = float(getattr(config, "ifind_http_timeout_seconds", 30.0) or 0.0)
    if _cap > 0:
        _limits.append(monotonic() + _cap + _HTTP_PARSE_STAGE_SECONDS)
    api_deadline = min(_limits)
    deadline_token = request_deadline.set(api_deadline)
    try:
        access_token = _resolve_access_token(refresh)
        headers = {"Content-Type": "application/json", "access_token": access_token, "ifindlang": "cn"}
        body = json.dumps(_flow_body(identity, start_compact, end_compact), default=str).encode("utf-8")
        status, response_body = _http_api_curl_post(_FLOW_ENDPOINT, headers, body, label="flow")
        used = access_token
        if status in (401, 403) and refresh:
            refreshed = _resolve_access_token(refresh, force_refresh=True)
            headers = {**headers, "access_token": refreshed}
            status, response_body = _http_api_curl_post(_FLOW_ENDPOINT, headers, body, label="flow")
            used = refreshed
        if status != 200:
            raise DataFetchError(f"ifind http flow http status {status}")
        payload = _parse_json_payload(response_body, label="flow")
        series = _parse_flow_series(
            payload, identity, start_iso, end_iso,
            access_token=used, extra_secrets=_redaction_secrets(refresh, used),
        )
        import logging
        logging.getLogger(__name__).info(
            "ifind http flow %s %s~%s 取得候选（%d 行，null %d）",
            identity, start_iso, end_iso, len(series),
            sum(1 for _d, v in series if v is None),
        )
        return series
    finally:
        request_deadline.reset(deadline_token)
