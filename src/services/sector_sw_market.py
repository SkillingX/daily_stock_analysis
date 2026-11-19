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
import math
import re
import time
from datetime import date, datetime
from decimal import Decimal, DecimalException
from fractions import Fraction
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import pandas as pd
from icontract import ensure

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
# latest=100.5、preClose=100、changeRatio=0.495 的确切差值是 0.005pp，
# 落在容差边界，拒绝。
_CHANGE_RATIO_TOLERANCE_PP = Fraction("0.005")


def _parse_quote_number(text: str) -> Optional[Decimal]:
    """保留实时行情的十进制字面，超出 Decimal 表示范围时仅该字段缺失。"""
    try:
        return Decimal(text)
    except DecimalException:
        return None


def _change_ratio_within_tolerance(row: Dict[str, Any]) -> bool:
    """qualified 行出口契约谓词（容差判定唯一入口）：带涨跌幅且带复核基准
    （latest/preClose>0）的行必须落在既有容差内；无涨跌幅（成分金额口径行，
    ``require_change=False`` 路径，含交叉复核不合格被空白化的行）或无复核
    基准时不对缺失发明断言。资格循环与出口契约共用本谓词，不重复实现。
    先限制非零输入可由有限浮点值承载，再用精确比值比较容差。"""
    change = row.get("change_pct")
    if change is None:
        return True
    try:
        projected_change = float(change)
        if not math.isfinite(projected_change) or (change != 0 and projected_change == 0):
            return False
        latest = row.get("latest")
        pre_close = row.get("pre_close")
        if latest is None or pre_close is None or pre_close <= 0:
            return True
        projected_latest, projected_pre_close = float(latest), float(pre_close)
        if (not math.isfinite(projected_latest) or not math.isfinite(projected_pre_close)
                or (latest != 0 and projected_latest == 0) or projected_pre_close == 0):
            return False
        # 将 Decimal 字面转换为精确分数，避免上下文舍入改变阈值两侧的判断。
        exact_latest = Fraction(Decimal(str(latest)))
        exact_pre_close = Fraction(Decimal(str(pre_close)))
        implied = (exact_latest / exact_pre_close - 1) * 100
        diff = abs(implied - Fraction(Decimal(str(change))))
    except (DecimalException, OverflowError):
        return False
    return diff < _CHANGE_RATIO_TOLERANCE_PP


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
                      deadline: Optional[float],
                      parse_float: Optional[Callable[[str], Optional[Decimal]]] = None,
                      ) -> Dict[str, Any]:
    """板块共用出站：认证 → 有界 curl POST → envelope/errorcode/tables 边界。

    认证缓存沿用共享键/刷新/锁语义；本次出站截止为
    ``min(deadline, now + timeout_seconds)``。失败统一抛
    :class:`SwSectorFetchError`；全部消息**先凭据脱敏后截断**（refresh token
    与本次实际使用的 access token 一律不出现，跨截断边界的长回显同样被
    脱敏——复用既有适配器 ``_redact_token`` 安全语义）；errorcode 非标量时
    按类型分类不入消息内容（防恶意长回显）。
    ``parse_float``（可选）仅作用于**响应** JSON 的浮点字面解析（如实时行情
    传 ``Decimal`` 保留入站字面精度供金融交叉复核）；不传时响应解码与既有
    行为逐位一致（归属/成分/日线等其他端点不受影响）。
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
            payload = (json.loads(raw, parse_float=parse_float)
                       if parse_float is not None else json.loads(raw))
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
    """个股申万主行业归属解析（决策 2：单一主行业口径；L1 核心单一出站）。

    一级归属（核心）：与核定目录唯一名称匹配（不近似择首、不拼同名东财
    板块）。**本函数不查询二/三级参考**——可选参考出站延迟到核心事实
    （快照/趋势）、口径选择与必需研究完成之后，由
    :func:`resolve_sw_reference_levels` 在真正剩余取数预算内有界执行
    （spec 复审：一级很快、二级等待耗尽截止时不得让快照/趋势/兜底失去
    机会；东财切换路径不发起参考出站）。``l2_name``/``l3_name`` 恒为
    None，由调用方在参考补齐后回填。
    供应商不回显归属日期，``vendor_as_of`` 为本次请求的归属观察日（查询
    口径如实标注）。失败/未命中 → ``status="unavailable"`` + 可诊断缺口
    （供兜底门槛判定）。
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
    out.update({
        "status": "confirmed",
        "classification_system": "申万 2021 一级行业",
        "classification_level": "L1",
        "classification_version": SW_L1_DIRECTORY_VERSION,
        "index_code": index_code,
        "index_name": l1_name,
        "vendor_as_of": observation_date,
        "l2_name": None,
        "l3_name": None,
        "gaps": gaps,
    })
    return out


def resolve_sw_reference_levels(secucode: str, *, observation_date: str,
                                refresh_token: str, timeout_seconds: float,
                                deadline: Optional[float]) -> Dict[str, Any]:
    """申万二/三级归属参考（可选链；L1 确认后的余量出站）。

    层级单独查询（同批重复指标会覆盖结果）与既有失败隔离语义不变：单级
    失败仅记该级可选缺口，不影响另一级与主行业状态。调用方负责预算门控
    （取数截止后不发起；东财切换路径不发起）与结果回填
    （``l2_name``/``l3_name`` 回填进主行业归属事实，缺口随身份缺口透出）。
    返回 ``{"l2_name", "l3_name", "gaps"}``；未取得的层级为 None。
    """
    gaps: List[str] = []
    l2_name: Optional[str] = None
    l3_name: Optional[str] = None
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
    return {"l2_name": l2_name, "l3_name": l3_name, "gaps": gaps}


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
    ``trade_time`` 自**已解析合格的完整供应商时间戳**归一（``HH:MM:SS``）：
    wire 的辅助 ``tradeTime`` 单元格是任意文本，不作为行事实或下游来源
    文字（畸形/超长辅助值既不进快照行，也不进 ``snapshot_source`` 等边界
    字段）。
    缺值（None）原样保留，不在 wire 层补造；跨行资格（同日、时间/新鲜度、
    交叉复核、同码重复冲突）由 :func:`qualify_quote_rows` 判定。
    畸形 wire 形状（非 dict 行/缺键/不可解析时间戳）逐行保守跳过，不让
    供应商形状异常逃出边界（整体无有效行按源错误抛 :class:`SwSectorFetchError`，
    由调用方降缺口，不产生未处理 500）。
    ``indicators`` 默认为 31 指数快照口径（issue #99）；成分成员行情
    （issue #100）传成员口径子集（与 2026-11-18 实测一致），其余行为不变。
    ``raw_amount=True``（成分成员行情路径）：``amount`` 原样保留首个有限
    整数或 Decimal（不经 float cast，供成交额排序保原始精度）；默认路径
    （31 指数快照与既有调用方）行为不变。

    金融精度（实时行情端点响应按 ``Decimal`` 解析浮点字面）：``change_pct``/
    ``latest``/``pre_close`` 在行内**原样保留精确数值**（int/float/Decimal，
    不经 float cast）贯穿 :func:`qualify_quote_rows` 交叉复核——字面
    ``0.495000000000000001`` 与 ``0.495`` 在资格判定下可区分（前者对
    100.5/100 推算 0.5pp 差 0.00499… 在容差内，后者恰 0.005pp 拒绝）；
    公开展示投影（float 字段）由消费方 ``_num``/``float`` 边界转换（JSON
    安全）。其余数值位（换手/涨跌家数与非 raw 金额）维持 float 口径。
    """
    body = {
        "codes": codes,
        "indicators": indicators,
    }
    payload = _sector_http_post(
        _REALTIME_ENDPOINT, body, label="sector_sw_quotes",
        refresh_token=refresh_token, timeout_seconds=timeout_seconds,
        deadline=deadline, parse_float=_parse_quote_number,
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
            # trade_time 归一自已解析的完整供应商时间戳（不取辅助 tradeTime）
            stamp = _parse_vendor_stamp(trade_stamp)
            rows.append({
                "thscode": thscode,
                "trade_date": trade_date,
                "trade_time": stamp.strftime("%H:%M:%S") if stamp else "",
                "stamp": stamp,
                # 交叉复核输入保留精确字面（Decimal 语义见 docstring）
                "change_pct": _first_finite_raw(cells.get("changeRatio")),
                "latest": _first_finite_raw(cells.get("latest")),
                "pre_close": _first_finite_raw(cells.get("preClose")),
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


def _first_finite_raw(values: object) -> Optional[Union[int, float, Decimal]]:
    """wire 数值位**原样**首个有限数值（共享数值位解析唯一入口：不做
    cast，Decimal 字面/大整数保留原始精度供金融复核与排序）。输入如实
    类型化为 ``object``（wire 单元格是未知 JSON 值），输出收窄为
    ``int | float | Decimal | None``。畸形输入本地守卫，不抛出、不让单行
    字段异常拖垮同批健康行：
    - 非 list 容器（dict/str/标量等 malformed wire）→ 缺值（不取 ``[0]``
      顶替、不按容器真值猜测）；
    - 布尔永不作数值；非 int/float/Decimal 元素 → 缺值；
    - 浮点 NaN/±inf 与非有限 Decimal → 缺值；原始大整数/大 Decimal
      **保留**（float 口径由 :func:`_first_finite` 另行守卫溢出与有限性）。
    """
    if not isinstance(values, list) or not values:
        return None
    v = values[0]
    if isinstance(v, bool) or not isinstance(v, (int, float, Decimal)):
        return None
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, Decimal) and not v.is_finite():
        return None
    return v


def _first_finite(values: object) -> Optional[float]:
    """wire 数值位 float 口径（公开展示投影/31 指数快照非精确字段）：委托
    共享解析器后转 float；原始值超出 float 可表示范围或转换结果非有限
    （NaN/±inf，含大 Decimal 转 inf）按缺值处理（异常本地消化，不逃出
    边界拖垮同批健康行）。"""
    v = _first_finite_raw(values)
    if v is None:
        return None
    try:
        finite = float(v)
    except (OverflowError, ValueError, TypeError):
        return None
    return finite if math.isfinite(finite) else None


@ensure(
    lambda result: all(_change_ratio_within_tolerance(row)
                       for row in result[0].values()),
    "qualified rows carrying change_pct must agree with the exact implied "
    "latest/preClose change within the 0.005pp tolerance (financial "
    "cross-field invariant at the quote qualification boundary)",
)
def qualify_quote_rows(rows: List[Dict[str, Any]], *, session_date: str,
                       received_at: Optional[datetime] = None,
                       session_open: Optional[datetime] = None,
                       session_close: Optional[datetime] = None,
                       require_change: bool = True,
                       ) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """快照行资格筛选：身份/会话/时间戳纪律 + 字段资格 + 重复保守拒绝。

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
    - **字段资格**（身份/会话/时间戳之外，按口径拆分）：``change_pct``
      与 ``latest/preClose`` 的精确推算值差异须在容差内（缺
      latest/preClose 时不发明复核基准；Decimal 入站保留字面
      精度，容差边界反例 100.5/100 + 0.495 恰差 0.005pp 拒绝、
      0.495000000000000001 在容差内采纳）。``require_change=True``
      （31 指数快照等既有口径，默认）：涨跌幅缺失**或交叉复核不合格**
      的行整体不合格——目标日行情资格与 ``quote_target_ok`` 依赖有效
      涨跌幅。``require_change=False``（成分成员/目标独立行情口径）：
      涨跌幅缺失或复核不合格**不剔除整行**——身份/会话/时间戳合格的行
      保留有效金额用于选样（amount-only 成员仍按成交额参与前五）；
      复核不合格的涨跌幅在**副本上置 None**（不污染调用方原始行），涨
      跌幅名次由同业支柱对缺失同行 withhold 并给覆盖说明。
    - 返回 ``({thscode: 行}, 保守拒绝说明)``：不合格行剔除（调用方按全集
      对齐缺口，剔除事实不丢失）；说明仅记录重复冲突等需显式诊断的拒绝，
      调用方负责有界化。
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
        if require_change and change is None:
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
        if change is not None and not _change_ratio_within_tolerance(row):
            # 涨跌幅与 latest/preClose 交叉复核不合格（唯一容差谓词）：
            # 严格口径（31 指数快照）整行拒绝；成分口径只作废涨跌幅本身
            # ——副本上置 None（不污染调用方复用的原始行，如参考日二筛），
            # 有效金额保留参与选样，涨跌幅名次由同业支柱 withhold
            if require_change:
                continue
            row = dict(row)
            row["change_pct"] = None
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


def _history_close_key(value: Any) -> Tuple[str, Any]:
    """收盘等价键（与既有资格层收盘语义一致）：可解析为有限正 Decimal →
    ``("valid", Decimal)``——数值等价（wire ``10`` 与 ``"10.00"`` 同键）；
    否则 ``("invalid", None)``——None/NaN/布尔/非数值/非正一律不作数值
    比较（布尔永不作数值，缺值语义同类）。"""
    if value is None or (isinstance(value, float)
                         and not math.isfinite(value)):
        return ("invalid", None)
    try:
        d = Decimal(str(value).strip())
    except (ArithmeticError, ValueError):
        return ("invalid", None)
    if not d.is_finite() or d <= 0:
        return ("invalid", None)
    return ("valid", d)


def fetch_index_history(codes: str, start_date: str, end_date: str, *,
                        refresh_token: str, timeout_seconds: float,
                        deadline: Optional[float]) -> Dict[str, pd.DataFrame]:
    """指数日线收盘 wire 解析：``{thscode: DataFrame(date, close)}``。

    请求固定 ``Interval=D / CPS=1（不复权，指数口径）/ Fill=Omit（无交易
    日期剔除——严格交易日历对齐由调用方完成）``。

    同一 thscode 可能拆多表返回：同码观测**逐行合并**（缺失段互补，不取
    末表覆盖前表）。合并前先做行级归一（与既有严格窗口资格语义一致）：
    - **日期归一**：``pd.to_datetime``（mixed + coerce）解析后按自然日
      ``date()`` 作键——同一交易日的不同 wire 形式（``"2026-11-03"`` 与
      ``"2026-11-03 00:00:00"``）同键，不产生伪重复；
    - **请求窗口过滤**：归一日期在 ``[start_date, end_date]`` 之外的行为
      窗口外行，与资格层语义一致忽略——窗口外矛盾不淘汰窗口内有效行；
    - **收盘等价**：按有限正 Decimal 数值比较（``10`` ≡ ``"10.00"``；
      布尔/非数/非正同类缺值）。同日等值去重保留一行，同日不等价 → 该
      代码整体保守拒绝（不取其一；不影响其他代码的有效行——调用方按缺行
      降缺口）。
    不可解析日期行原样透传（资格层忽略并计数，行级诊断不丢）。"""
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
        # 请求窗口成对表示（两端同取同弃的显式不变式）：解析失败整体为
        # None——退化为不按窗口过滤（资格层仍按期望交易日集对齐，不放大
        # 采用面），使用点无需再对两端分别收窄
        window: Optional[Tuple[date, date]] = None
        try:
            window = (date.fromisoformat(start_date),
                      date.fromisoformat(end_date))
        except ValueError:
            window = None
        rows_by_code: Dict[str, Dict[date, Tuple[Tuple[str, Any], Any]]] = {}
        passthrough_by_code: Dict[str, List[Tuple[str, Any]]] = {}
        conflicting: set = set()
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
            if thscode in conflicting:
                continue
            parsed = pd.to_datetime(dates, errors="coerce", format="mixed")
            merged = rows_by_code.setdefault(thscode, {})
            passthrough = passthrough_by_code.setdefault(thscode, [])
            for d_ts, raw_date, c_raw in zip(parsed, dates, closes):
                if pd.isna(d_ts):
                    # 不可解析日期行：原样透传（资格层忽略并注明，诊断不丢）
                    passthrough.append((raw_date, c_raw))
                    continue
                d = d_ts.date()
                if window is not None and not (window[0] <= d <= window[1]):
                    continue  # 窗口外行：资格层忽略，不参与冲突判定
                key = _history_close_key(c_raw)
                prior = merged.get(d)
                if prior is not None and prior[0] != key:
                    # 同码同日收盘矛盾（数值不等或有效性不同类）：供应商对
                    # 同一身份给出冲突事实，该代码整体不采用（保守，不取
                    # 其一；已合并观测一并放弃）
                    conflicting.add(thscode)
                    rows_by_code.pop(thscode, None)
                    passthrough_by_code.pop(thscode, None)
                    break
                if prior is None:
                    merged[d] = (key, c_raw)
        frames: Dict[str, pd.DataFrame] = {}
        for thscode, merged in rows_by_code.items():
            rows = [(d.isoformat(), merged[d][1]) for d in sorted(merged)]
            rows.extend(passthrough_by_code.get(thscode) or [])
            frames[thscode] = pd.DataFrame(
                {"date": [r[0] for r in rows],
                 "close": [r[1] for r in rows]},
            )
    except SwSectorFetchError:
        raise
    except Exception as exc:  # noqa: BLE001 - wire 形状异常按源错误降级，不逃出边界
        raise SwSectorFetchError(f"日线响应形状异常（{type(exc).__name__}）") from None
    if not frames:
        raise SwSectorFetchError(
            "日线响应无有效行（tables 为空或同码矛盾整码拒绝）")
    return frames
