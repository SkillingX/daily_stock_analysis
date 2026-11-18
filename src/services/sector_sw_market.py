# -*- coding: utf-8 -*-
"""板块分析申万一级行业市场数据链——iFinD 标准 HTTP 取数与 wire 边界（issue #99）。

范围（本票，父规格 #98 决策 1/2/4）：申万 2021 一级目录（核定配置数据）、
主行业归属解析（正式归属按层级单独查询 + 目录唯一匹配）、31 个一级指数
实时快照 wire 解析与同日合格行筛选、行业/基准指数日线 wire 解析。
成分/同业（#100）与催化/综合（#101）不在本模块。

复用边界（父规格决策 1：仅行为等价复用）：
- 认证与传输按行为等价复用 ``data_provider.ifind_http_daily_adapter`` 的
  ``_resolve_access_token`` / ``_http_api_curl_post``（认证缓存键、刷新与
  锁语义、有界 curl 传输、凭据脱敏语义均不变）；
- 板块入口独立判断资格：只依赖 ``IFIND_HTTP_REFRESH_TOKEN`` 与
  ``IFIND_HTTP_TIMEOUT_SECONDS``（凭据 + 单次请求超时），不依赖个股日线
  开关（``ENABLE_IFIND_HTTP_API``）、数据源 profile 或深度投研配置；
- 板块业务不新增业务缓存（单次报告内取数，不跨报告复用）。

单次请求超时（决策 1）在共享传输的绝对截止上再取小：每次出站的
``request_deadline`` 取 ``min(调用方截止, now + IFIND_HTTP_TIMEOUT_SECONDS)``，
与既有传输的 ``--max-time`` 语义组合生效。
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from data_provider.ifind_http_daily_adapter import (
    _http_api_curl_post,
    _redact_token,
    _resolve_access_token,
)

_HTTP_ROOT = "https://quantapi.51ifind.com/api/v1"
_BASIC_DATA_ENDPOINT = f"{_HTTP_ROOT}/basic_data_service"
_REALTIME_ENDPOINT = f"{_HTTP_ROOT}/real_time_quotation"
_HISTORY_ENDPOINT = f"{_HTTP_ROOT}/cmd_history_quotation"
_DATA_POOL_ENDPOINT = f"{_HTTP_ROOT}/data_pool"

# 涨跌幅与最新价/昨收交叉复核容差（与 2026-11-18 实测复核口径一致：
# abs((latest/preClose − 1)×100 − changeRatio) < 0.005 个百分点）。
_CHANGE_RATIO_TOLERANCE_PP = 0.005


class SwSectorFetchError(RuntimeError):
    """iFinD 标准 HTTP 板块取数失败（认证/传输/HTTP 状态/errorcode/形状）。

    消息只携带供应商业务诊断（errorcode/errmsg 截断）与本地异常类型，
    不携带凭据或完整 URL（凭据脱敏由共享传输层保证）。
    """


# ---------------------------------------------------------------------------
# 申万 2021 一级目录（核定配置数据，父规格决策 2）
# ---------------------------------------------------------------------------

SW_L1_DIRECTORY_VERSION = "sw2021-l1-20261118"
"""核定目录版本：申万 2021 一级行业指数目录，2026-11-18 实测核定。"""

SW_L1_DIRECTORY_SOURCE = (
    "申万 2021 一级行业指数目录：iFinD basic_data_service ths_index_short_name_index "
    "2026-11-18 盘后实测核定（31/31 简称逐一核对），对照申万行业分类标准 2021 版"
    "（31 个一级、发布时口径）；供应商单次搜索结果不作为全集依据"
)

SW_L1_DIRECTORY: Tuple[Tuple[str, str], ...] = (
    ("801010.SL", "农林牧渔"),
    ("801030.SL", "基础化工"),
    ("801040.SL", "钢铁"),
    ("801050.SL", "有色金属"),
    ("801080.SL", "电子"),
    ("801110.SL", "家用电器"),
    ("801120.SL", "食品饮料"),
    ("801130.SL", "纺织服饰"),
    ("801140.SL", "轻工制造"),
    ("801150.SL", "医药生物"),
    ("801160.SL", "公用事业"),
    ("801170.SL", "交通运输"),
    ("801180.SL", "房地产"),
    ("801200.SL", "商贸零售"),
    ("801210.SL", "社会服务"),
    ("801230.SL", "综合"),
    ("801710.SL", "建筑材料"),
    ("801720.SL", "建筑装饰"),
    ("801730.SL", "电力设备"),
    ("801740.SL", "国防军工"),
    ("801750.SL", "计算机"),
    ("801760.SL", "传媒"),
    ("801770.SL", "通信"),
    ("801780.SL", "银行"),
    ("801790.SL", "非银金融"),
    ("801880.SL", "汽车"),
    ("801890.SL", "机械设备"),
    ("801950.SL", "煤炭"),
    ("801960.SL", "石油石化"),
    ("801970.SL", "环保"),
    ("801980.SL", "美容护理"),
)
"""申万 2021 一级行业指数目录（指数代码 → 一级行业简称，31 项）。

依据见 ``SW_L1_DIRECTORY_SOURCE`` 与
``.claude/reviews/ifind-sector-api-live-validation-20261118.md``；
目录变更须重核 31 个代码及名称并更新版本号。
"""

_DIRECTORY_BY_NAME: Dict[str, str] = {name: code for code, name in SW_L1_DIRECTORY}


# ---------------------------------------------------------------------------
# 配置门控（板块入口独立判断，不读日线开关/profile）
# ---------------------------------------------------------------------------


def sw_http_config() -> Optional[Tuple[str, float]]:
    """板块 iFinD 标准 HTTP 入口配置： ``(refresh_token, timeout_seconds)``。

    未配置凭据（缺失/空白）时返回 None——申万主行业按不可确认处理
    （父规格决策 1），不抛异常、不产出站。
    """
    from src.config import get_config

    config = get_config()
    token = (config.ifind_http_refresh_token or "").strip()
    if not token:
        return None
    timeout = float(getattr(config, "ifind_http_timeout_seconds", 30.0) or 0.0)
    if timeout <= 0:
        timeout = 30.0
    return token, timeout


# ---------------------------------------------------------------------------
# 共享传输出站（行为等价复用；单次超时取小见模块 docstring）
# ---------------------------------------------------------------------------


def _bounded_deadline(deadline: Optional[float], timeout_seconds: float) -> float:
    """本次出站的绝对截止：调用方截止与单次请求超时取小（monotonic）。"""
    single = time.monotonic() + max(timeout_seconds, 0.001)
    if deadline is None:
        return single
    return min(single, deadline)


def _sector_http_post(endpoint_url: str, body: Dict[str, Any], *, label: str,
                      refresh_token: str, timeout_seconds: float,
                      deadline: Optional[float]) -> Dict[str, Any]:
    """板块共用出站：认证 → 有界 curl POST → envelope/errorcode/tables 边界。

    认证缓存沿用共享键/刷新/锁语义；本次出站截止为
    ``min(deadline, now + timeout_seconds)``。失败统一抛
    :class:`SwSectorFetchError`；全部消息**先凭据脱敏后截断**（refresh token
    与本次实际使用的 access token 一律不出现，跨截断边界的长回显同样被
    脱敏——复用既有适配器 ``_redact_token`` 安全语义）；errorcode 非标量时
    按类型分类不入消息内容（防恶意长回显）。
    """
    from src.patches.eastmoney_patch import request_deadline

    _MESSAGE_CAP = 160

    def _safe(message: str, access_used: Optional[str]) -> str:
        # 凭据脱敏下界：真实凭据均为长串，过短占位值（<8）不做全文替换，
        # 避免病态短值把供应商业务文本（如 errmsg）替换成掩码；
        # 先脱敏后截断——跨截断边界（80/120 等）的长回显先被整体脱敏
        secrets: Tuple[str, ...] = tuple(
            s for s in ((refresh_token or "").strip(), (access_used or "").strip())
            if len(s) >= 8)
        redacted = _redact_token(message, *secrets) if secrets else message
        return redacted[:_MESSAGE_CAP]

    def _errorcode_repr(errorcode: object) -> str:
        # 标量 code 保留有界字面；非标量（dict/list 等）只暴露类型名，
        # 不把潜在恶意/超长内容回显进缺口文本
        if isinstance(errorcode, bool):
            return f"<bool:{errorcode}>"
        if isinstance(errorcode, (int, float)):
            return repr(errorcode)
        if isinstance(errorcode, str):
            return errorcode[:20]
        return f"<{type(errorcode).__name__}>"

    def _post_once(access_token: str) -> Tuple[int, bytes]:
        return _http_api_curl_post(
            endpoint_url,
            {"Content-Type": "application/json", "access_token": access_token,
             "ifindlang": "cn"},
            json.dumps(body).encode(), label=label,
        )

    effective = _bounded_deadline(deadline, timeout_seconds)
    guard = request_deadline.set(effective)
    access_used: Optional[str] = None
    try:
        access_used = _resolve_access_token(refresh_token)
        status, raw = _post_once(access_used)
        if status in (401, 403):
            # 凭据被供应商拒绝：与共享凭据路径
            # （``_post_history_query_authorized``）一致——强制续用一次并重试
            # **同一请求一次**（不改变 refresh_token）；同一 guard 内完成，
            # 刷新与重试同受原每请求剩余截止钳制（有界，不另起预算）
            access_used = _resolve_access_token(refresh_token, force_refresh=True)
            status, raw = _post_once(access_used)
        if status in (401, 403):
            raise SwSectorFetchError(_safe(
                "ifind sector authentication failed after token refresh; "
                "请检查 IFIND_HTTP_REFRESH_TOKEN 有效性与账号权限", access_used))
        if status != 200:
            raise SwSectorFetchError(_safe(f"HTTP {status}", access_used))
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise SwSectorFetchError(_safe("响应不是有效 JSON", access_used)) from None
        if not isinstance(payload, dict):
            raise SwSectorFetchError(_safe("响应顶层形状异常（非对象）", access_used))
        errorcode = payload.get("errorcode")
        if errorcode != 0:
            errmsg = payload.get("errmsg")
            errmsg_text = errmsg if isinstance(errmsg, str) else ""
            raise SwSectorFetchError(_safe(
                f"errorcode={_errorcode_repr(errorcode)} errmsg={errmsg_text}",
                access_used))
        if not isinstance(payload.get("tables"), list):
            raise SwSectorFetchError(_safe("响应缺 tables（形状异常）", access_used))
        return payload
    except SwSectorFetchError:
        raise
    except Exception as exc:  # noqa: BLE001 - 传输/认证边界统一降级
        raise SwSectorFetchError(_safe(
            f"{type(exc).__name__}: {str(exc)}", access_used)) from None
    finally:
        request_deadline.reset(guard)


# ---------------------------------------------------------------------------
# 主行业归属（basic_data_service，层级单独查询——同批重复指标会覆盖结果）
# ---------------------------------------------------------------------------

_SW_INDUSTRY_INDICATOR = "ths_the_sw_industry_stock"


def _fetch_sw_industry_name(secucode: str, level: int, observation_date: str, *,
                            refresh_token: str, timeout_seconds: float,
                            deadline: Optional[float]) -> str:
    """指定层级的正式申万行业归属名称（严格身份与类型边界）。

    - **精确身份**：只采用 ``thscode`` 与请求代码完全一致的响应行（错误
      代码的行不采用，不取首行）；
    - **类型边界**：值必须是**非空字符串且 ≤64 字符**（行业名边界随载荷
      schema）——非字符串/空白/超长值按源错误拒绝，不做 ``str()`` 合法化；
    - **同码冲突**：多行同码给出不同有效值 → 冲突，不取其一。
    无匹配行/值非法/冲突 → :class:`SwSectorFetchError`（调用方按层级降级：
    L1 主行业不可确认、L2/L3 可选参考缺口——永不产生 API 500）。
    """
    body = {
        "codes": secucode,
        "indipara": [{
            "indicator": _SW_INDUSTRY_INDICATOR,
            "indiparams": [str(level), observation_date.replace("-", "")],
        }],
    }
    payload = _sector_http_post(
        _BASIC_DATA_ENDPOINT, body, label="sector_sw_industry",
        refresh_token=refresh_token, timeout_seconds=timeout_seconds,
        deadline=deadline,
    )
    tables = payload.get("tables")
    if not isinstance(tables, list):
        raise SwSectorFetchError(
            f"申万归属响应形状异常（tables 非 list，L{level}）")
    valid_names: List[str] = []
    matched = False
    for table in tables:
        if not isinstance(table, dict):
            continue
        if str(table.get("thscode") or "").strip() != secucode:
            continue  # 非本请求代码的响应行：不采用（精确身份，不取首行）
        matched = True
        cells = table.get("table") if isinstance(table.get("table"), dict) else {}
        values = cells.get(_SW_INDUSTRY_INDICATOR)
        if not isinstance(values, list) or not values:
            continue
        # 同表多值同样歧义：逐值校验后取本表去重结果，多个不同行业名 → 冲突
        # （不取首个——同表重复指标覆盖/多值都是供应商形状异常）
        names: List[str] = []
        for raw in values:
            if not isinstance(raw, str):
                raise SwSectorFetchError(
                    f"申万归属响应值非法（L{level} {secucode}：非字符串值，"
                    "不 str() 合法化）")
            name = raw.strip()
            if not name or len(name) > 64:
                raise SwSectorFetchError(
                    f"申万归属响应值非法（L{level} {secucode}：空白或超 64 字符行业名）")
            if name not in names:
                names.append(name)
        if len(names) > 1:
            raise SwSectorFetchError(
                f"申万归属响应同表多值冲突（L{level} {secucode}："
                f"{len(names)} 个不同行业名），不取其一")
        if names and names[0] not in valid_names:
            valid_names.append(names[0])
    if not matched:
        raise SwSectorFetchError(
            f"申万归属响应无精确匹配行（L{level} {secucode}）")
    if len(valid_names) > 1:
        # 冲突消息只含计数，不含供应商返回值（防回显）
        raise SwSectorFetchError(
            f"申万归属响应同码多表冲突（L{level} {secucode}："
            f"{len(valid_names)} 个不同行业名），不取其一")
    if not valid_names:
        raise SwSectorFetchError(
            f"申万归属响应无有效值（L{level} {secucode}）")
    return valid_names[0]


def resolve_sw_main_industry(secucode: str, *, observation_date: str,
                             refresh_token: str, timeout_seconds: float,
                             deadline: Optional[float]) -> Dict[str, Any]:
    """个股申万主行业归属解析（决策 2：单一主行业口径）。

    一级归属（核心）先行：与核定目录唯一名称匹配（不近似择首、不拼同名
    东财板块）；核心未确认时**不发起**二/三级参考出站（可选参考不消耗
    核心预算、不间接影响兜底门槛）。确认后才以同一剩余截止作有界可选
    参考查询二、三级（层级单独查询——同批重复指标会覆盖结果）；参考失败
    仅记可选缺口，不影响主行业状态。供应商不回显归属日期，
    ``vendor_as_of`` 为本次请求的归属观察日（查询口径如实标注）。
    失败/未命中 → ``status="unavailable"`` + 可诊断缺口（供兜底门槛判定）。
    """
    out: Dict[str, Any] = {
        "status": "unavailable",
        "classification_system": None,
        "classification_level": None,
        "classification_version": None,
        "index_code": None,
        "index_name": None,
        "vendor_as_of": None,
        "l2_name": None,
        "l3_name": None,
        "gaps": [],
    }
    gaps: List[str] = []
    # 核心：一级归属（决定主行业口径与兜底门槛；失败即返回，无参考出站）
    try:
        l1_name = _fetch_sw_industry_name(
            secucode, 1, observation_date,
            refresh_token=refresh_token, timeout_seconds=timeout_seconds,
            deadline=deadline,
        )
    except SwSectorFetchError as exc:
        gaps.append(f"申万一级归属查询失败（{exc}），主行业不可确认")
        out["gaps"] = gaps
        return out
    if not l1_name:
        gaps.append("申万一级归属未取得（供应商无值），主行业不可确认")
        out["gaps"] = gaps
        return out
    index_code = _DIRECTORY_BY_NAME.get(l1_name)
    if index_code is None:
        gaps.append(
            f"申万一级归属「{l1_name}」未命中核定一级目录（不近似择首、"
            "不拼入同名东财板块），主行业不可确认")
        out["gaps"] = gaps
        return out

    # 可选参考：二、三级归属（仅在核心口径选定后有界查询；失败不降主行业）
    l2_name = ""
    l3_name = ""
    for level in (2, 3):
        label = "二级" if level == 2 else "三级"
        try:
            name = _fetch_sw_industry_name(
                secucode, level, observation_date,
                refresh_token=refresh_token, timeout_seconds=timeout_seconds,
                deadline=deadline,
            )
        except SwSectorFetchError as exc:
            gaps.append(
                f"申万{label}归属参考查询失败（可选参考，不影响主行业）：{exc}")
            continue
        if level == 2:
            l2_name = name
        else:
            l3_name = name
    out.update({
        "status": "confirmed",
        "classification_system": "申万 2021 一级行业",
        "classification_level": "L1",
        "classification_version": SW_L1_DIRECTORY_VERSION,
        "index_code": index_code,
        "index_name": l1_name,
        "vendor_as_of": observation_date,
        "l2_name": l2_name or None,
        "l3_name": l3_name or None,
        "gaps": gaps,
    })
    return out


# ---------------------------------------------------------------------------
# 一级指数快照（real_time_quotation）
# ---------------------------------------------------------------------------


def fetch_index_quotes(codes: str, *, refresh_token: str, timeout_seconds: float,
                       deadline: Optional[float],
                       indicators: str = ("changeRatio,amount,tradeTime,"
                                          "latest,preClose,turnoverRatio,"
                                          "riseCount,fallCount"),
                       raw_amount: bool = False,
                       ) -> List[Dict[str, Any]]:
    """指数/证券实时快照 wire 解析：每行一个 thscode 的归一事实 dict。

    行字段：``thscode``/``trade_date``/``trade_time``/``stamp``（完整供应
    商时间戳 datetime，新鲜度资格依据）/``change_pct``/``latest``/
    ``pre_close``/``amount``/``turnover``/``up_count``/``down_count``。
    缺值（None）原样保留，不在 wire 层补造；跨行资格（同日、时间/新鲜度、
    交叉复核、同码重复冲突）由 :func:`qualify_quote_rows` 判定。
    畸形 wire 形状（非 dict 行/缺键/不可解析时间戳）逐行保守跳过，不让
    供应商形状异常逃出边界（整体无有效行按源错误抛 :class:`SwSectorFetchError`，
    由调用方降缺口，不产生未处理 500）。
    ``indicators`` 默认为 31 指数快照口径（issue #99）；成分成员行情
    （issue #100）传成员口径子集（与 2026-11-18 实测一致），其余行为不变。
    ``raw_amount=True``（成分成员行情路径）：``amount`` 原样保留首个有限
    int/float（不经 float cast，供 Decimal 排序保原始精度）；默认路径
    （31 指数快照与既有调用方）行为不变。
    """
    body = {
        "codes": codes,
        "indicators": indicators,
    }
    payload = _sector_http_post(
        _REALTIME_ENDPOINT, body, label="sector_sw_quotes",
        refresh_token=refresh_token, timeout_seconds=timeout_seconds,
        deadline=deadline,
    )
    try:
        rows: List[Dict[str, Any]] = []
        for table in payload.get("tables") or []:
            if not isinstance(table, dict):
                continue
            thscode = str(table.get("thscode") or "").strip()
            if not thscode:
                continue
            raw_cells = table.get("table")
            cells = raw_cells if isinstance(raw_cells, dict) else {}
            raw_times = table.get("time")
            times = raw_times if isinstance(raw_times, list) else []
            trade_stamp = str(times[0]) if times else ""
            trade_date = trade_stamp.split(" ")[0] if trade_stamp else ""
            trade_time = ""
            raw_trade_times = cells.get("tradeTime")
            if isinstance(raw_trade_times, list) and raw_trade_times:
                trade_time = str(raw_trade_times[0] or "")
            rows.append({
                "thscode": thscode,
                "trade_date": trade_date,
                "trade_time": trade_time,
                "stamp": _parse_vendor_stamp(trade_stamp),
                "change_pct": _first_finite(cells.get("changeRatio")),
                "latest": _first_finite(cells.get("latest")),
                "pre_close": _first_finite(cells.get("preClose")),
                "amount": (_first_finite_raw(cells.get("amount"))
                           if raw_amount
                           else _first_finite(cells.get("amount"))),
                "turnover": _first_finite(cells.get("turnoverRatio")),
                "up_count": _first_finite(cells.get("riseCount")),
                "down_count": _first_finite(cells.get("fallCount")),
            })
    except SwSectorFetchError:
        raise
    except Exception as exc:  # noqa: BLE001 - wire 形状异常按源错误降级，不逃出边界
        raise SwSectorFetchError(f"快照响应形状异常（{type(exc).__name__}）") from None
    if not rows:
        raise SwSectorFetchError("快照响应无有效行（tables 为空）")
    return rows


def _parse_vendor_stamp(raw: str) -> Optional[datetime]:
    """完整供应商时间戳（``YYYY-MM-DD HH:MM:SS``）→ naive datetime。

    不可解析/缺失返回 None（该行时间资格无法验证，按保守拒绝处理，
    不猜测时区或用请求时刻顶替）。
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _first_finite(values: Optional[List[Any]]) -> Optional[float]:
    """wire 数值位首个有限数；缺值/None/非有限数原样缺失（不补造）。"""
    if not values:
        return None
    v = values[0]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    finite = float(v)
    if finite != finite or finite in (float("inf"), float("-inf")):
        return None
    return finite


def qualify_quote_rows(rows: List[Dict[str, Any]], *, session_date: str,
                       received_at: Optional[datetime] = None,
                       session_open: Optional[datetime] = None,
                       session_close: Optional[datetime] = None,
                       ) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """快照行资格筛选：同日 + 时间/新鲜度纪律 + 交叉复核 + 重复保守拒绝。

    资格规则（沿用既有报价采纳纪律，见板块文档「统一报价交易时点资格」）：
    - **同码重复保守拒绝**：同一 thscode 出现多行时，逐字段完全一致才保留
      一行；任何矛盾（不同日期/数值/时间）→ 该代码整体不采用（供应商对
      同一身份给出冲突事实，不可信），不取最后一行、不取首行。
    - ``trade_date == session_date``（其他日期行不进入该日截面）；
    - **时间/新鲜度**（``received_at`` 为该响应实际接收钟，市场本地 naive）：
      完整供应商时间戳必须可解析、**不晚于接收时刻**（未来行情拒绝）；
      该会话已完结（接收 ≥ ``session_close``）→ 时间戳须 ≥ 会话收盘
      （盘后推送如 22:00:46 合法；已完结会话的上午盘中旧快照拒绝）；
      盘中（接收 < ``session_close``）→ 时间戳须 ≥ 会话开盘。会话开收盘
      未提供时仅要求不晚于接收时刻（保守下界不省略上界验证）。
    - ``change_pct`` 有限，且与 ``latest/preClose`` 推算值差异在容差内
      （缺 latest/preClose 时仅要求 change_pct 有限，不发明复核基准）；
    - 返回 ``({thscode: 行}, 保守拒绝说明)``：不合格行剔除（调用方按 31
      目录全集对齐缺口，剔除事实不丢失）；说明仅记录重复冲突等需显式
      诊断的拒绝，调用方负责有界化。
    """
    # 同码重复保守拒绝：逐字段完全一致才去重，矛盾即整码不采用
    by_code: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("thscode") or "").strip()
        if not code:
            continue
        by_code.setdefault(code, []).append(row)
    notes: List[str] = []
    deduped: List[Dict[str, Any]] = []
    for code, code_rows in by_code.items():
        if len(code_rows) == 1:
            deduped.append(code_rows[0])
            continue
        keys = ("trade_date", "trade_time", "stamp", "change_pct", "latest",
                "pre_close", "amount", "turnover", "up_count", "down_count")
        first = code_rows[0]
        if all(all(r.get(k) == first.get(k) for k in keys) for r in code_rows[1:]):
            deduped.append(first)
        else:
            notes.append(
                f"快照同码重复行冲突（{code}，{len(code_rows)} 行事实不一致），"
                "该代码行情保守拒绝（不取首行/末行）")
    qualified: Dict[str, Dict[str, Any]] = {}
    for row in deduped:
        if row.get("trade_date") != session_date:
            continue
        change = row.get("change_pct")
        if change is None:
            continue
        stamp = row.get("stamp")
        if received_at is not None:
            if stamp is None:
                continue  # 时间戳不可解析：新鲜度无法验证，保守拒绝
            if stamp > received_at:
                continue  # 未来行情
            if session_close is not None and received_at >= session_close:
                if session_close > stamp:
                    continue  # 已完结会话的盘前/盘中旧快照（未过收盘）
            elif session_open is not None and stamp < session_open:
                continue  # 盘中：当日开盘前的时间戳
        latest = row.get("latest")
        pre_close = row.get("pre_close")
        if latest is not None and pre_close is not None and pre_close > 0:
            implied = (latest / pre_close - 1.0) * 100.0
            if abs(implied - change) >= _CHANGE_RATIO_TOLERANCE_PP:
                continue
        qualified[str(row.get("thscode") or "")] = row
    return qualified, notes


# ---------------------------------------------------------------------------
# 指数成分（data_pool 专题报表 p03473；issue #100，父规格 #98 决策 3）
# ---------------------------------------------------------------------------


_VALID_MEMBER_SECUCODE = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")
"""入站成分成员身份格式：6 位数字 + 沪/深/北交易所后缀（不猜交易所）。"""

_MAX_PUBLISHABLE_MEMBER_ROWS = 10000
"""成分成员集合的可发布行数上限（与载荷 schema 的 member_count/snapshot_rows
上界一致）：供应商响应超过该上限属异常形状，成员集合整体不发布（明确有界
源缺口，不发布全集覆盖、不产生载荷校验 500）。"""


def _first_finite_raw(values: Optional[List[Any]]) -> Any:
    """wire 数值位**原样**首个有限数（int/float 不做 cast，金融金额排序
    经 Decimal 时保留原始精度）；缺值/None/非有限数原样缺失（不补造）。"""
    if not values:
        return None
    v = values[0]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if isinstance(v, float) and (v != v or v in (float("inf"),
                                                 float("-inf"))):
        return None
    return v


def _is_valid_a_share_secucode(code_text: str) -> bool:
    """入站成员代码必须形如 ``NNNNNN.SH|.SZ|.BJ``（6 位数字，精确后缀）。"""
    return bool(_VALID_MEMBER_SECUCODE.fullmatch(code_text or ""))


def fetch_index_members(index_code: str, observation_date: str, *,
                        refresh_token: str, timeout_seconds: float,
                        deadline: Optional[float]) -> Dict[str, Any]:
    """申万指数成分 wire 解析（专题报表 p03473，``iv_date`` + ``iv_zsdm``）。

    返回：``member_codes``（与观察日**同日**的成员代码，保留供应商带回的
    **交易所后缀**——带交易所证券身份匹配的依据）、``member_names``、
    ``dates_seen``（响应中出现的全部行日期）、``rows_total``（原始行数）、
    ``completeness_evidence``（p03473 响应无总数/分页元数据，供应商单次
    响应不能证明全集——恒 None，完整性判定交给 :func:`classify_membership`）、
    ``rejected_identity_rows``（身份格式不合格被剔除的行数：非 6 位数字、缺
    后缀或后缀非沪/深/北；结构化计数，完整性证明的分母依据）、
    ``conflicting_code_members``（剥后缀后同 6 位码跨交易所矛盾被整组剔除的
    成员数）、``gaps``。行级同日筛选：行日期 ≠ 观察日的行不进入成员集合
    （异日成分不与目标日截面混用）；全部异日 = 未取得该日成分（非空成分，
    如实缺口）。身份校验：入站成员代码必须是 ``6 位数字 + 有效交易所后缀``
    （.SH/.SZ/.BJ），不合格行不采用且缺口**只含计数、不回显原始畸形标识**；
    剥后缀后同 6 位码出现多个交易所后缀 = 身份矛盾，整组剔除（保守，不取
    其一）。失败抛 :class:`SwSectorFetchError`，由调用方降缺口（成分查询
    失败 ≠ 非成员）。
    """
    body = {
        "reportname": "p03473",
        "functionpara": {
            "iv_date": observation_date.replace("-", ""),
            "iv_zsdm": index_code,
        },
        "outputpara": "p03473_f001,p03473_f002,p03473_f003",
    }
    payload = _sector_http_post(
        _DATA_POOL_ENDPOINT, body, label="sector_sw_members",
        refresh_token=refresh_token, timeout_seconds=timeout_seconds,
        deadline=deadline,
    )
    try:
        raw_tables = payload.get("tables")
        tables = raw_tables if isinstance(raw_tables, list) else []
        codes: List[str] = []
        names: Dict[str, str] = {}
        dates_seen: List[str] = []
        off_dates: List[str] = []
        rejected_identity_rows = 0
        stripped_seen: Dict[str, str] = {}
        conflicting_stripped: set = set()
        conflicting_members = 0
        shape_gaps: List[str] = []
        rows_total = 0
        for table in tables:
            if not isinstance(table, dict):
                continue
            raw_cells = table.get("table")
            cells = raw_cells if isinstance(raw_cells, dict) else {}
            row_dates = cells.get("p03473_f001")
            row_codes = cells.get("p03473_f002")
            row_names = cells.get("p03473_f003")
            if not isinstance(row_codes, list):
                shape_gaps.append("成分响应缺成员代码列（p03473_f002），该表不采用")
                continue
            if not (isinstance(row_dates, list) and isinstance(row_names, list)
                    and len(row_dates) == len(row_codes)
                    and len(row_names) == len(row_codes)):
                shape_gaps.append(
                    "成分响应列形状异常（p03473 三列不对齐），该表不采用（不按位置猜值）")
                continue
            rows_total += len(row_codes)
            for raw_date, raw_code, raw_name in zip(row_dates, row_codes,
                                                    row_names):
                date_text = str(raw_date or "").strip()
                if date_text and date_text not in dates_seen:
                    dates_seen.append(date_text)
                if date_text != observation_date:
                    if date_text and date_text not in off_dates:
                        off_dates.append(date_text)
                    continue
                code_text = str(raw_code or "").strip()
                if not _is_valid_a_share_secucode(code_text):
                    rejected_identity_rows += 1
                    continue
                stripped = code_text.split(".", 1)[0]
                prior = stripped_seen.get(stripped)
                if prior is not None and prior != code_text:
                    # 同 6 位码跨交易所矛盾：整组剔除（保守，不取其一）
                    if stripped not in conflicting_stripped:
                        conflicting_stripped.add(stripped)
                        conflicting_members += 1  # 先到成员随后回滚
                    conflicting_members += 1
                    continue
                stripped_seen.setdefault(stripped, code_text)
                if code_text not in codes:
                    codes.append(code_text)
                    if isinstance(raw_name, str) and raw_name.strip():
                        names[code_text] = raw_name.strip()
        # 冲突组整组回滚（先到成员先回退，保证分母如实）
        if conflicting_stripped:
            codes = [c for c in codes
                     if c.split(".", 1)[0] not in conflicting_stripped]
            names = {c: n for c, n in names.items()
                     if c.split(".", 1)[0] not in conflicting_stripped}
        gaps: List[str] = list(shape_gaps)
        if rejected_identity_rows:
            gaps.append(
                f"成分响应 {rejected_identity_rows} 行成员身份格式不合格"
                "（非 6 位数字或交易所后缀无效，计数披露、不回显原始值），"
                "不采用")
        if conflicting_stripped:
            shown = "、".join(sorted(conflicting_stripped)[:3])
            more = (f" 等 {len(conflicting_stripped)} 个" if len(
                conflicting_stripped) > 3 else "")
            gaps.append(
                f"成分响应 {len(conflicting_stripped)} 组剥后缀后同码跨交易所"
                f"矛盾（{shown}{more}），身份矛盾整组剔除（保守，不取其一）")
        if off_dates:
            shown_dates = "、".join(sorted(off_dates)[:3])
            more = f" 等 {len(off_dates)} 个日期" if len(off_dates) > 3 else ""
            gaps.append(
                f"成分响应存在非观察日 {observation_date} 的行"
                f"（{shown_dates}{more}，异日成分不与目标日截面混用），已剔除并记录")
        if rows_total and not codes and not rejected_identity_rows \
                and not conflicting_stripped:
            gaps.append(
                f"成分响应共 {rows_total} 行但无 {observation_date} 同日合格行，"
                "未取得该日成分（非空成分，如实缺口）")
    except SwSectorFetchError:
        raise
    except Exception as exc:  # noqa: BLE001 - wire 形状异常按源错误降级，不逃出边界
        raise SwSectorFetchError(f"成分响应形状异常（{type(exc).__name__}）") from None
    # 可发布上限：供应商响应超出载荷 schema 成员上界属异常形状——成员集合
    # 整体不发布（明确有界源缺口；不发布全集覆盖、不产生载荷校验 500）
    over_publish_limit = rows_total > _MAX_PUBLISHABLE_MEMBER_ROWS
    if over_publish_limit:
        codes = []
        names = {}
        gaps.append(
            f"成分响应 {rows_total} 行超出可发布成员上限 "
            f"{_MAX_PUBLISHABLE_MEMBER_ROWS}，成员集合不发布（完整性不可证、"
            "不发布全集覆盖）")
    if rows_total == 0 and not shape_gaps:
        # 响应结构整体无成员表/无成员行：按源错误处理（与既有快照/日线 wire
        # 同语义），调用方降缺口（成分未取得 ≠ 非成员）；表级形状问题
        # （缺列/不对齐）已在 shape_gaps 如实记录，返回空成员由判定层保守未知
        raise SwSectorFetchError(
            "成分响应无成员行（" + ("、".join(gaps) if gaps else "tables 为空") + "）")
    return {
        "member_codes": codes,
        "member_names": names,
        "dates_seen": sorted(dates_seen),
        "rows_total": rows_total,
        "completeness_evidence": None,
        "rejected_identity_rows": rejected_identity_rows,
        "conflicting_code_members": conflicting_members,
        "over_publish_limit": over_publish_limit,
        "gaps": gaps,
    }


def classify_membership(target_secucode: str, members: Dict[str, Any], *,
                        observation_date: str) -> Dict[str, Any]:
    """指数成分边界三态判定（#98 决策 3：已纳入 / 明确未纳入 / 未知）。

    - ``included``：目标带交易所身份在同日成分集合中（positive evidence，
      无需完整性证明——找到即找到）；
    - ``excluded``：目标未命中且完整性证据可证（完整集合或明确独立证据；
      现场 p03473 无完整性元数据，此态仅受控/证据升级路径可达）；
    - ``unknown``：目标未命中且完整性不可证（现场常态），或成分未取得
      （查询失败/未取得 ≠ 非成员，不冒充排除）。

    返回 ``{status, target_secucode, observation_date, member_count,
    completeness_evidence, gaps}``；``member_count`` 是观测行数（非全集计数）。
    """
    member_codes = [str(c) for c in (members.get("member_codes") or [])]
    evidence = members.get("completeness_evidence")
    gaps = [str(g) for g in (members.get("gaps") or [])]
    member_count = len(member_codes)
    obtained = bool(member_codes)
    if target_secucode in member_codes:
        status = "included"
    elif obtained and evidence:
        # 排除需要真实成员集 + 完整性证据；空成员集与「完整集合」证据矛盾
        # （申万一级指数均有成分），保守未知
        status = "excluded"
    else:
        status = "unknown"
        if not obtained:
            gaps = gaps + [
                f"目标股 {target_secucode} 未命中且成分集合不可用/完整性不可证，"
                "成分状态未知（查询失败或无完整性证据 ≠ 非成员）"]
    return {
        "status": status,
        "target_secucode": target_secucode,
        "observation_date": observation_date,
        "member_count": member_count if obtained else None,
        "completeness_evidence": evidence if status != "unknown" else None,
        "gaps": gaps,
    }


# ---------------------------------------------------------------------------
# 指数日线（cmd_history_quotation）
# ---------------------------------------------------------------------------


def fetch_index_history(codes: str, start_date: str, end_date: str, *,
                        refresh_token: str, timeout_seconds: float,
                        deadline: Optional[float]) -> Dict[str, pd.DataFrame]:
    """指数日线收盘 wire 解析：``{thscode: DataFrame(date, close)}``。

    请求固定 ``Interval=D / CPS=1（不复权，指数口径）/ Fill=Omit（无交易
    日期剔除——严格交易日历对齐由调用方完成）``。
    """
    body = {
        "codes": codes,
        "indicators": "close",
        "startdate": start_date,
        "enddate": end_date,
        "functionpara": {"Interval": "D", "CPS": "1", "Fill": "Omit"},
    }
    payload = _sector_http_post(
        _HISTORY_ENDPOINT, body, label="sector_sw_history",
        refresh_token=refresh_token, timeout_seconds=timeout_seconds,
        deadline=deadline,
    )
    try:
        raw_tables = payload.get("tables")
        tables = raw_tables if isinstance(raw_tables, list) else []
        frames: Dict[str, pd.DataFrame] = {}
        for table in tables:
            if not isinstance(table, dict):
                continue
            thscode = str(table.get("thscode") or "").strip()
            raw_times = table.get("time")
            time_list = raw_times if isinstance(raw_times, list) else []
            dates = [str(d) for d in time_list]
            raw_cells = table.get("table")
            cells = raw_cells if isinstance(raw_cells, dict) else {}
            raw_closes = cells.get("close")
            closes = raw_closes if isinstance(raw_closes, list) else []
            if not thscode or not dates or not closes:
                continue
            frames[thscode] = pd.DataFrame(
                {"date": dates[:len(closes)], "close": closes[:len(dates)]},
            )
    except SwSectorFetchError:
        raise
    except Exception as exc:  # noqa: BLE001 - wire 形状异常按源错误降级，不逃出边界
        raise SwSectorFetchError(f"日线响应形状异常（{type(exc).__name__}）") from None
    if not frames:
        raise SwSectorFetchError("日线响应无有效行（tables 为空）")
    return frames
