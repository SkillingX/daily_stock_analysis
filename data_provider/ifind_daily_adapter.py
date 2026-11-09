# -*- coding: utf-8 -*-
"""iFinD 现金分红前复权日线适配器（issue #86）。

只服务「当前最新已完成交易日」截止的沪深 A 股普通股日线窗口：复用既有
``IfindFetcher.query`` MCP 边界（预算/单飞/停止语义不变），在通用清洗之前
完成证券身份、逐价格字段现金分红复权元数据、量/额单位、冲突重复、截断导出
与严格交易日历全窗口资格校验；不合格整窗按数据源错误拒绝并交还既有候选。

一次日线尝试（MCP + CSV GET + 解码 + 解析 + 最终采纳）共享同一个有限绝对
deadline：无外层 deadline 时按「MCP 超时 + 导出阶段余量」自建，并预留既有
fallback 份额；导出 URL 只允许精确域 ``o.thsi.cn`` 的已验证结果缓存路径
（唯一允许 HTTP→HTTPS 协议升级，绝不先访问 HTTP），使用禁用环境凭据发现
的隔离一次性传输（无 netrc/代理/Authorization/cookie），无重定向、字节/
行数/逐字节流/逐行解析/最终采纳全链停止与剩余时间检查。
"""

from __future__ import annotations

import csv
import io
import logging
import re
from datetime import date as _date, datetime
from decimal import Decimal, DecimalException
from time import monotonic
from typing import Annotated, Dict, List, Optional, Tuple

import pandas as pd
from icontract import require
from pydantic import BaseModel, ConfigDict, Field

from .base import BaseFetcher, DataFetchError
from .ifind_fundamental_adapter import IfindFetcher, _ifind_tables, _same_security

logger = logging.getLogger(__name__)

# 四个价格字段的复权方式必须逐字段明确为「前复权(现金分红)」（全/半角括号
# 等价）；泛「前复权」「前复权(分红再投)」等一律不构成返回资格。
_CASH_QFQ_MODE = "前复权(现金分红)"
_PRICE_FIELDS: Tuple[str, str, str, str] = ("开盘价", "最高价", "最低价", "收盘价")
_VOLUME_FIELD = "成交量"
_AMOUNT_FIELD = "成交额"
_DATE_FIELD = "日期"
_SECURITY_FIELD = "证券代码"
_VOLUME_UNIT = "股"
_AMOUNT_UNIT = "元"

# 导出链接唯一允许的精确域与已验证结果缓存路径；查询串/片段/端口/用户信息
# 属于未验证变体，一律拒绝。
_EXPORT_HOST = "o.thsi.cn"
_EXPORT_PATH_RE = re.compile(r"^/ifind-python-aime-tools-service\.result-cache/query_result_\d+\.csv$")
_CSV_URL_RE = re.compile(r"https?://[^\s|，。；）)]+?\.csv(?:[?#][^\s|，。；）)]*)?", re.IGNORECASE)
_TRUNCATION_MARKER = "数据截断提示"

# 有界导出下载：字节/行数上限按已验证样本（778 行双股、389 行单股）与既有
# 请求窗口（最长 365 交易日 ≈ 730 自然日）取安全余量。
_MAX_EXPORT_BYTES = 1_000_000
_MAX_EXPORT_ROWS = 20_000
_EXPORT_READ_TIMEOUT = 15.0
# 一次完整日线尝试的导出阶段（GET + 解码 + 解析）余量；与 MCP 配置超时共同
# 构成无外层 deadline 时的自建绝对上限。
_EXPORT_STAGE_SECONDS = 20.0
_MAX_HEADER_BYTES = 65_536

_CN_SCALE_UNITS: Tuple[Tuple[str, int], ...] = (
    ("万亿", 10**12), ("千亿", 10**11), ("百亿", 10**10), ("十亿", 10**9),
    ("亿", 10**8), ("万", 10**4), ("千", 10**3),
)
_EMPTY_CELLS = {"", "\t", "-", "--", "nan", "None", "null", "N/A"}

_IDENTITY_RE = re.compile(r"^(\d{6})\.(SH|SZ)$")
# 沪深普通股白名单（仅已确认代码段）：沪市 6 开头（主板 600/601/603/605、
# 科创板 688/689），深市主板/中小板 000/001/002/003 与创业板 300/301。
# 399xxx 为深市指数、302+ 未获证据，ETF/债/指数/B 股/北交所均不在其内。
_SZ_ORDINARY_PREFIXES = ("000", "001", "002", "003", "300", "301")


class IfindDailyBar(BaseModel):
    """单日合格六字段行（Decimal 资格换算后的冻结值，单位：元/股/元）。"""

    model_config = ConfigDict(strict=True, frozen=True, validate_assignment=True, allow_inf_nan=False)
    date: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
    open: Annotated[float, Field(gt=0)]
    high: Annotated[float, Field(gt=0)]
    low: Annotated[float, Field(gt=0)]
    close: Annotated[float, Field(gt=0)]
    volume: Annotated[float, Field(gt=0)]
    amount: Annotated[float, Field(gt=0)]


@require(
    lambda o, h, l, c: l <= min(o, c) and max(o, c) <= h and l > 0,
    "cash-qfq daily bar must satisfy low <= min(open, close) <= max(open, close) <= high with positive low",
)
@require(
    lambda o, h, l, c, v, a: v > 0 and a > 0,
    "daily volume and amount must be positive on an adopted session",
)
def _daily_bar(
    session: str, o: Decimal, h: Decimal, l: Decimal, c: Decimal, v: Decimal, a: Decimal
) -> IfindDailyBar:
    """跨字段不变式（icontract 层）：价格钳制关系与正量额合格后才成行。"""
    return IfindDailyBar(
        date=session, open=float(o), high=float(h), low=float(l), close=float(c),
        volume=float(v), amount=float(a),
    )


def qualified_daily_identity(raw_code: str) -> Optional[str]:
    """沪深 A 股普通股资格身份（规范化之前保留交易所显式后缀）。

    返回 ``600519.SH`` 形式的合格身份；ETF/指数（含 000001.SH 上证指数与
    399001.SZ/399006.SZ 深市指数）/B 股/北交所/港美股/格式错误返回 None
    （继续原路由，不向 iFinD 出站、不计供应商故障）。显式交易所身份优先：
    ``000001.SH`` 是上证指数，不得归为平安银行 ``000001.SZ``。
    """
    from .base import _is_us_market, _is_hk_market

    raw = (raw_code or "").strip()
    if not raw:
        return None
    upper = raw.upper()
    if _is_us_market(raw) or _is_hk_market(raw):
        return None
    exchange: Optional[str] = None
    code = upper
    if "." in upper:
        base, suffix = upper.rsplit(".", 1)
        if suffix == "SS":
            suffix = "SH"
        if base == "SS":
            base = "SH"
        if suffix in ("SH", "SZ") and base.isdigit() and len(base) == 6:
            exchange, code = suffix, base
        elif base in ("SH", "SZ") and suffix.isdigit() and len(suffix) == 6:
            exchange, code = base, suffix
        else:
            return None
    else:
        for prefix in ("SH", "SZ"):
            if not upper.startswith(prefix):
                continue
            body = upper[len(prefix):]
            body = body[1:] if body.startswith(".") else body
            if body.isdigit() and len(body) == 6:
                exchange, code = prefix, body
                break
    if not code.isdigit() or len(code) != 6:
        return None
    if exchange is None:
        if code.startswith("6"):
            exchange = "SH"
        elif code.startswith(_SZ_ORDINARY_PREFIXES):
            exchange = "SZ"
        else:
            return None
    else:
        # 显式交易所与代码段必须自洽：沪市普通股 6 开头；深市普通股仅
        # 000/001/002/003/300/301。000001.SH 是指数、399001.SZ 是深市指数、
        # 600519.SZ 是矛盾身份，均不出站。
        if exchange == "SH" and not code.startswith("6"):
            return None
        if exchange == "SZ" and not code.startswith(_SZ_ORDINARY_PREFIXES):
            return None
    # ETF 前缀（沪 51/52/56/58、深 15/16/18）与 B 股（沪 900、深 200）不参与。
    if code.startswith(("51", "52", "56", "58", "15", "16", "18", "900", "200")):
        return None
    return f"{code}.{exchange}"


def ifind_daily_enabled() -> bool:
    """既有 Choice profile + ENABLE_IFIND_MCP + 有效配置才启用本日线能力。"""
    import math

    from src.config import get_config

    config = get_config()
    if config.data_source_profile != "choice" or not config.enable_ifind_mcp:
        return False
    endpoint = (getattr(config, "ifind_mcp_endpoint", None) or "").strip()
    token = (getattr(config, "ifind_mcp_token", None) or "").strip()
    timeout = float(getattr(config, "ifind_mcp_timeout_seconds", 0) or 0)
    return bool(endpoint and token) and math.isfinite(timeout) and timeout > 0


def ifind_daily_read_scope(raw_code: str) -> bool:
    """启用且证券合格的读取范围：持久 DB 命中不作为分析依据。"""
    return ifind_daily_enabled() and qualified_daily_identity(raw_code) is not None


def daily_routing(raw_code: str, end_date: Optional[str]) -> Optional[Tuple[str, str]]:
    """管理器级路由决策：返回 ``(合格身份, 约束后的结束日)`` 或 None（走原链）。

    任何 iFinD 出站之前完成：证券资格、日历可用性与时间资格。显式历史目标
    （结束日早于当前最新已完成交易日）直接跳过 iFinD；未来/当日结束日约束到
    最新已完成交易日。日历不可用时明确跳过（不退成 weekday 日历）。
    """
    if not ifind_daily_enabled():
        return None
    identity = qualified_daily_identity(raw_code)
    if identity is None:
        return None
    from src.core.trading_calendar import recent_completed_trading_sessions

    sessions = recent_completed_trading_sessions("cn", 1)
    if not sessions:
        logger.info("[ifind-daily] %s 严格交易日历不可用，本次跳过 iFinD 日线", raw_code)
        return None
    latest = sessions[0]
    effective = _parse_date(end_date) or _date.today()
    if effective < latest:
        logger.info(
            "[ifind-daily] %s 目标结束日 %s 早于最新已完成交易日 %s，跳过 iFinD 走原源链",
            raw_code, effective.isoformat(), latest.isoformat(),
        )
        return None
    return identity, latest.isoformat()


def _parse_date(value: Optional[str]) -> Optional[_date]:
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def check_attempt_alive() -> None:
    """一次日线尝试的停止/剩余时间检查（MCP、导出、解析与采纳共用）。

    用户取消/报告预算终止以 ``ResearchStopped`` 传播；绝对 deadline 耗尽以
    ``TimeoutError`` 传播。不依赖任何进程级全局 socket 补丁。
    """
    from src.core.research_run import current_research
    from src.patches.eastmoney_patch import request_deadline

    run = current_research.get()
    if run is not None:
        run.check()
    deadline = request_deadline.get()
    if deadline is not None and deadline <= monotonic():
        raise TimeoutError("daily attempt deadline exhausted")


def attempt_deadline(now: Optional[float] = None) -> Optional[float]:
    """构造一次完整日线尝试的有限绝对 deadline。

    取外层 request_deadline 与「MCP 配置超时 + 导出阶段余量」自建上限（再为
    既有后续源预留 fallback 份额）的最小值；外层已存在时沿用管理器已做过的
    fallback 预留，只被自建上限进一步收紧。
    """
    from src.config import get_config
    from src.patches.eastmoney_patch import request_deadline

    start = monotonic() if now is None else now
    cap = float(getattr(get_config(), "ifind_mcp_timeout_seconds", 30.0) or 0.0)
    if cap <= 0:
        return request_deadline.get()
    budget = cap + _EXPORT_STAGE_SECONDS
    reserve = min(8.0, budget * 0.3)
    self_deadline = start + max(budget - reserve, 1.0)
    outer = request_deadline.get()
    return min(self_deadline, outer) if outer is not None else self_deadline


def _normalize_cash_mode(value: object) -> str:
    """复权方式回显归一化：全角括号/空白归一后比较，不放宽语义。"""
    text = str(value or "").strip()
    return text.replace("（", "(").replace("）", ")").replace(" ", "")


def _field_params(indicators_params: Dict[str, Dict[str, object]], field: str) -> Dict[str, object]:
    params = indicators_params.get(field)
    if isinstance(params, dict):
        return params
    for key, candidate in indicators_params.items():
        if isinstance(candidate, dict) and re.sub(r"[（(][^()（）]*[）)]", "", str(key)).strip() == field:
            return candidate
    return {}


def _decimal_cell(raw: object, *, allow_scale: bool) -> Optional[Decimal]:
    """严格数值解析：拒绝空占位/公式样式/非有限值；中文倍率仅量/额可用。"""
    text = str(raw if raw is not None else "").strip().replace(",", "")
    if not text or text in _EMPTY_CELLS:
        return None
    scale = Decimal(1)
    if allow_scale:
        for unit, factor in _CN_SCALE_UNITS:
            if text.endswith(unit):
                text = text[: -len(unit)].strip()
                scale = Decimal(factor)
                break
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", text):
        return None
    try:
        value = Decimal(text) * scale
    except DecimalException:
        return None
    return value if value.is_finite() else None


_FIELD_HEADER_RE = re.compile(
    r"^(开盘价|最高价|最低价|收盘价|成交量|成交额|证券代码|证券简称|日期)"
    r"(?:[（(]\s*单位\s*[：:]\s*([^）)]+?)\s*[）)])?$"
)


def _split_header(header: str) -> tuple[str, Optional[str]]:
    """列名 → (规范字段, 列头实际单位或 None)。无单位后缀的纯列名单位为 None。"""
    match = _FIELD_HEADER_RE.match(str(header or "").strip())
    if match is None:
        return str(header or "").strip(), None
    unit = match.group(2).strip() if match.group(2) else None
    return match.group(1), unit


def _assert_actual_units(units: Dict[str, Optional[str]], identity: str) -> None:
    """列头实际单位与已合格元数据矛盾则整窗拒绝；纯列名沿用元数据，不猜测换算。"""
    expected = {_VOLUME_FIELD: _VOLUME_UNIT, _AMOUNT_FIELD: _AMOUNT_UNIT}
    for field, required in expected.items():
        actual = units.get(field)
        if actual is None:
            continue
        if actual != required:
            raise DataFetchError(
                f"ifind {identity} {field} 列单位 {actual} 与元数据 {required} 矛盾，整窗不采纳"
            )


def _parse_row_date(raw: object) -> Optional[_date]:
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) != 8:
        return None
    try:
        return datetime.strptime(digits, "%Y%m%d").date()
    except ValueError:
        return None


def _canonical_row(row: Dict[str, str], identity: str) -> tuple[Dict[str, str], Dict[str, Optional[str]]]:
    """把带单位后缀的列名收成规范字段，并收集列头实际单位。重复字段即拒绝。"""
    values: Dict[str, str] = {}
    units: Dict[str, Optional[str]] = {}
    for key, value in row.items():
        field, unit = _split_header(str(key))
        if field not in {_SECURITY_FIELD, _DATE_FIELD, *_PRICE_FIELDS, _VOLUME_FIELD, _AMOUNT_FIELD}:
            continue
        if field in values:
            raise DataFetchError(f"ifind {identity} duplicate column {field}")
        values[field] = str(value or "")
        units[field] = unit
    return values, units


_REQUIRED_FIELDS = (_SECURITY_FIELD, _DATE_FIELD, *_PRICE_FIELDS, _VOLUME_FIELD, _AMOUNT_FIELD)


def _require_unique_canonical_headers(names: List[str], identity: str) -> None:
    """在字典折叠之前拒绝重复的规范列身份。"""
    seen: set[str] = set()
    for name in names:
        field, _unit = _split_header(str(name).strip())
        if field not in _REQUIRED_FIELDS:
            continue
        if field in seen:
            raise DataFetchError(f"ifind {identity} duplicate column {field}")
        seen.add(field)


def _reject_markdown_duplicate_headers(answer: str, identity: str) -> None:
    """日线路径自己检查原始表头顺序，不改财务解析器的字典折叠。"""
    from data_provider.ifind_fundamental_adapter import _IFIND_TABLE_SEPARATOR

    lines = answer.splitlines()
    for index, line in enumerate(lines):
        if index == 0 or not _IFIND_TABLE_SEPARATOR.fullmatch(line):
            continue
        headers = [cell.strip() for cell in lines[index - 1].strip().strip("|").split("|")]
        _require_unique_canonical_headers(headers, identity)


def _collect_answer_rows(answer: str, identity: str) -> Dict[_date, Dict[str, str]]:
    """内联 markdown 表 → {日期: 六字段原文}；同日冲突、列单位与元数据矛盾即拒绝。"""
    rows: Dict[_date, Dict[str, str]] = {}
    matched = False
    _reject_markdown_duplicate_headers(answer, identity)
    for table in _ifind_tables(answer):
        for row in table:
            values, units = _canonical_row(row, identity)
            if not _same_security(values.get(_SECURITY_FIELD), identity):
                continue
            matched = True
            _assert_actual_units(units, identity)
            row_date = _parse_row_date(values.get(_DATE_FIELD))
            if row_date is None:
                continue
            needed = {field: values.get(field, "") for field in (*_PRICE_FIELDS, _VOLUME_FIELD, _AMOUNT_FIELD)}
            existing = rows.get(row_date)
            if existing is None:
                rows[row_date] = needed
            elif any(existing[k].strip() != needed[k].strip() for k in needed):
                raise DataFetchError(f"ifind conflicting duplicate date rows for {identity} on {row_date.isoformat()}")
    if not matched:
        raise DataFetchError(f"ifind answer carries no rows for {identity}")
    return rows


def _parse_csv_rows(text: str, identity: str) -> Dict[_date, Dict[str, str]]:
    """完整导出 CSV → {日期: 六字段原文}；非法结构/单位矛盾/冲突拒绝，逐行停止检查。"""
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise DataFetchError("ifind export csv has no header")
    _require_unique_canonical_headers(list(reader.fieldnames), identity)
    header_row = {name: "" for name in reader.fieldnames}
    _values, units = _canonical_row(header_row, identity)
    for field in (_SECURITY_FIELD, _DATE_FIELD, *_PRICE_FIELDS, _VOLUME_FIELD, _AMOUNT_FIELD):
        if field not in units:
            raise DataFetchError(f"ifind export csv missing required column {field}")
    _assert_actual_units(units, identity)
    mapping = {}
    for name in reader.fieldnames:
        field, _unit = _split_header(str(name))
        if field in {_SECURITY_FIELD, _DATE_FIELD, *_PRICE_FIELDS, _VOLUME_FIELD, _AMOUNT_FIELD}:
            mapping[field] = name
    rows: Dict[_date, Dict[str, str]] = {}
    matched = False
    for raw_row in reader:
        if None in raw_row or any(value is None for value in raw_row.values()):
            raise DataFetchError("ifind export csv row structure invalid")
        if not _same_security(str(raw_row.get(mapping[_SECURITY_FIELD], "")), identity):
            continue
        matched = True
        row_date = _parse_row_date(raw_row.get(mapping[_DATE_FIELD]))
        if row_date is None:
            raise DataFetchError("ifind export csv row date invalid")
        values = {field: str(raw_row.get(mapping[field]) or "") for field in (*_PRICE_FIELDS, _VOLUME_FIELD, _AMOUNT_FIELD)}
        existing = rows.get(row_date)
        if existing is None:
            rows[row_date] = values
        elif any(existing[k].strip() != values[k].strip() for k in values):
            raise DataFetchError(f"ifind conflicting duplicate date rows for {identity} on {row_date.isoformat()}")
        if len(rows) > _MAX_EXPORT_ROWS:
            raise DataFetchError("ifind export csv row limit exceeded")
        if len(rows) % 256 == 0:
            check_attempt_alive()
    if not matched:
        raise DataFetchError(f"ifind export csv carries no rows for {identity}")
    return rows


def export_download_url(answer: str) -> Optional[str]:
    """从回执中提取唯一获准的导出链接；无效变体直接拒绝。

    只允许精确域 ``o.thsi.cn`` 的已验证结果缓存路径；唯一允许的变体是
    HTTP→HTTPS 协议升级（仅改协议，不先访问 HTTP）。查询串、片段、用户信息、
    显式端口、相似域与其他路径/协议一律拒绝。
    """
    urls = set(match.group(0) for match in _CSV_URL_RE.finditer(answer))
    if not urls:
        return None
    if len(urls) > 1:
        raise DataFetchError("ifind answer carries more than one export csv url")
    return _validated_export_url(next(iter(urls)))


def _validated_export_url(url: str) -> str:
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url.strip())
    if parts.scheme == "http":
        scheme = "https"
    elif parts.scheme == "https":
        scheme = parts.scheme
    else:
        raise DataFetchError("ifind export url scheme rejected")
    host = (parts.hostname or "").lower()
    if host != _EXPORT_HOST:
        raise DataFetchError("ifind export url host rejected")
    if parts.port is not None or parts.username is not None or parts.password is not None:
        raise DataFetchError("ifind export url carries port or userinfo")
    if parts.query or parts.fragment:
        raise DataFetchError("ifind export url carries query or fragment")
    if not _EXPORT_PATH_RE.match(parts.path or ""):
        raise DataFetchError("ifind export url path rejected")
    return urlunsplit((scheme, parts.netloc, parts.path, "", ""))


def answer_truncated(answer: str) -> bool:
    return _TRUNCATION_MARKER in answer


def _curl_child_env() -> dict[str, str]:
    """最小子进程环境：只要能启动 curl 并使用系统 TLS 信任库。

    不继承代理、curlrc、netrc、cookie、MCP 凭据或 CA 覆盖变量。
    """
    import os

    return {"PATH": os.environ.get("PATH", os.defpath)}


def _ascii_content_length(raw: str) -> int:
    """HTTP Content-Length 只接受非空 ASCII 十进制，拒绝 +4 / 0_4 这类 int() 能吃的 token。"""
    token = raw.strip()
    if not re.fullmatch(r"[0-9]+", token):
        raise DataFetchError("ifind export content-length invalid")
    value = int(token)
    if value < 0 or value > _MAX_EXPORT_BYTES:
        raise DataFetchError("ifind export content-length exceeds bound")
    return value


def _transfer_tokens(values: list[str]) -> list[str]:
    """保留每一次 Transfer-Encoding 及其 token 顺序，折叠前再资格判定。"""
    tokens: list[str] = []
    for raw in values:
        for part in raw.split(","):
            token = part.strip().lower()
            if token:
                tokens.append(token)
    return tokens


def _single_critical_headers(multi: dict[str, list[str]]) -> dict[str, str]:
    """关键头保留全部出现；冲突或畸形在折叠成单值之前拒绝。"""
    parsed = [_ascii_content_length(raw) for raw in multi.get("content-length", [])]
    if len(set(parsed)) > 1:
        raise DataFetchError("ifind export content-length conflict")
    encodings = [item.strip().lower() for item in multi.get("content-encoding", [])]
    if len(set(encodings)) > 1:
        raise DataFetchError("ifind export content-encoding conflict")
    medias = [item.split(";", 1)[0].strip().lower() for item in multi.get("content-type", [])]
    if len(set(medias)) > 1:
        raise DataFetchError("ifind export content-type conflict")
    transfers = _transfer_tokens(multi.get("transfer-encoding", []))
    if parsed and transfers:
        raise DataFetchError("ifind export framing ambiguous")
    if transfers not in ([], ["chunked"]):
        raise DataFetchError("ifind export transfer-encoding rejected")
    return {key: values[-1] for key, values in multi.items()}


def _split_included_http(raw: bytes) -> tuple[int, dict[str, str], bytes]:
    """拆 curl --include 的最终响应。100 Continue 等中间块跳过。"""
    while True:
        head, sep, rest = raw.partition(b"\r\n\r\n")
        if not sep:
            raise DataFetchError("ifind export headers truncated")
        if len(head) > _MAX_HEADER_BYTES:
            raise DataFetchError("ifind export header bound exceeded")
        lines = head.decode("iso-8859-1", errors="replace").split("\r\n")
        parts = lines[0].split(" ", 2)
        if len(parts) < 2 or not parts[1].isdigit():
            raise DataFetchError("ifind export status line invalid")
        status = int(parts[1])
        multi: dict[str, list[str]] = {}
        for line in lines[1:]:
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            multi.setdefault(key.strip().lower(), []).append(value.strip())
        if status == 100:
            raw = rest
            continue
        return status, _single_critical_headers(multi), rest


def _qualified_export_body(status: int, headers: dict[str, str], body: bytes) -> bytes:
    if status != 200:
        raise DataFetchError(f"ifind export http status {status}")
    encoding = headers.get("content-encoding", "").strip().lower()
    if encoding not in {"", "identity"}:
        raise DataFetchError(f"ifind export content-encoding {encoding or 'unknown'} rejected")
    media = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media not in {"text/csv", "application/csv"}:
        raise DataFetchError(f"ifind export content-type {media or 'missing'} rejected")
    declared = headers.get("content-length")
    if declared is not None:
        try:
            expected = int(declared)
        except ValueError as exc:
            raise DataFetchError("ifind export content-length invalid") from exc
        if expected < 0 or expected > _MAX_EXPORT_BYTES:
            raise DataFetchError("ifind export content-length exceeds bound")
        if len(body) != expected:
            raise DataFetchError("ifind export body truncated")
    if len(body) > _MAX_EXPORT_BYTES:
        raise DataFetchError("ifind export byte bound exceeded")
    return body


def _ready_export_parts(raw: bytes) -> tuple[int, dict[str, str], bytes] | None:
    """最终响应头到齐后返回拆分结果；头还在到达时返回 None。"""
    if b"\r\n\r\n" not in raw:
        return None
    try:
        return _split_included_http(raw)
    except DataFetchError as exc:
        if "truncated" in str(exc):
            return None
        raise


def _reject_running_bounds(raw: bytes) -> None:
    """父进程自己数头/体字节。声明超限立即拒绝，不等 curl 把正文下完。"""
    ready = _ready_export_parts(raw)
    if ready is None:
        if len(raw) > _MAX_HEADER_BYTES:
            raise DataFetchError("ifind export header bound exceeded")
        return
    _status, headers, body = ready
    declared = headers.get("content-length")
    if declared is not None:
        try:
            expected = int(declared)
        except ValueError as exc:
            raise DataFetchError("ifind export content-length invalid") from exc
        if expected < 0 or expected > _MAX_EXPORT_BYTES:
            raise DataFetchError("ifind export content-length exceeds bound")
    if len(body) > _MAX_EXPORT_BYTES:
        raise DataFetchError("ifind export byte bound exceeded")


def stream_export_csv(url: str) -> str:
    """用本机 curl 子进程下载已验证导出链接，父进程持有绝对 deadline。

    ``-q`` 必须是第一个参数。不继承代理/curlrc/netrc/cookie/MCP 凭据。
    正常 TLS，不跟随重定向，不重试。Windows 管道无法用 selector 有界排空，
    该可选源在 Windows 上明确失败并走既有兜底，不声称已验证。
    """
    import os
    import selectors
    import shutil
    import subprocess
    from urllib.parse import urlsplit

    from src.core.research_run import ResearchStopped
    from src.patches.eastmoney_patch import request_deadline

    if os.name == "nt":
        raise DataFetchError("ifind export curl transport unsupported on this platform")
    curl = shutil.which("curl")
    if not curl:
        raise DataFetchError("ifind export curl unavailable")
    parts = urlsplit(url)
    if parts.scheme == "https":
        proto = "=https"
    elif parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost", "::1"}:
        proto = "=http"
    else:
        raise DataFetchError("ifind export url scheme rejected")
    check_attempt_alive()
    deadline = request_deadline.get()
    remaining = None if deadline is None else max(0.001, deadline - monotonic())
    max_time = f"{(remaining if remaining is not None else _EXPORT_READ_TIMEOUT):.3f}"
    argv = [
        curl, "-q", "--silent", "--show-error", "--globoff",
        "--proxy", "", "--noproxy", "*", "--proto", proto,
        "--http1.1", "--no-buffer", "--include",
        "--max-time", max_time,
        # 补充上限高于父进程正文上限：curl 8.4 前的 --max-filesize 不截断
        # 未知长度正文；父进程必须先看到超限字节再自己拒绝。
        "--max-filesize", str(_MAX_EXPORT_BYTES + _MAX_HEADER_BYTES),
        "-H", "Accept: text/csv,*/*",
        "-H", "Accept-Encoding: identity",
        "--url", url,
    ]
    proc = subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=_curl_child_env(),
    )
    stdout = bytearray()
    stderr = bytearray()
    try:
        assert proc.stdout is not None and proc.stderr is not None
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            selector.register(proc.stderr, selectors.EVENT_READ)
            while selector.get_map():
                check_attempt_alive()
                for key, _event in selector.select(0.02):
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if key.fileobj is proc.stderr:
                        if len(stderr) < 8192:
                            stderr.extend(chunk[: 8192 - len(stderr)])
                        continue
                    stdout.extend(chunk)
                    _reject_running_bounds(bytes(stdout))
        check_attempt_alive()
    except ResearchStopped:
        raise
    except TimeoutError:
        raise
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()
    if proc.returncode not in {0, None} and proc.returncode < 0:
        raise DataFetchError(f"ifind export transport failed: signal {-proc.returncode}")
    try:
        status, headers, body = _split_included_http(bytes(stdout))
        qualified = _qualified_export_body(status, headers, body)
    except DataFetchError as exc:
        # 已解析出的资格失败优先。没有任何响应字节时，curl 退出才是传输失败。
        if not stdout and proc.returncode not in {0, None}:
            raise DataFetchError(
                f"ifind export transport failed: curl exit {proc.returncode}"
            ) from exc
        raise
    if proc.returncode == 63:
        raise DataFetchError("ifind export byte bound exceeded")
    if proc.returncode not in {0, None}:
        raise DataFetchError(f"ifind export transport failed: curl exit {proc.returncode}")
    try:
        return qualified.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise DataFetchError("ifind export csv is not valid utf-8") from exc


def _qualify_metadata(indicators_params: Dict[str, Dict[str, object]], identity: str) -> None:
    """逐价格字段独立校验现金分红前复权；量/额单位必须为股/元。"""
    for field in _PRICE_FIELDS:
        params = _field_params(indicators_params, field)
        mode = _normalize_cash_mode(params.get("复权方式"))
        if mode != _CASH_QFQ_MODE:
            raise DataFetchError(
                f"ifind {identity} {field} 复权方式 {mode or '未回显'} 不满足现金分红前复权资格"
            )
    volume_unit = str(_field_params(indicators_params, _VOLUME_FIELD).get("单位") or "").strip()
    amount_unit = str(_field_params(indicators_params, _AMOUNT_FIELD).get("单位") or "").strip()
    if volume_unit != _VOLUME_UNIT:
        raise DataFetchError(f"ifind {identity} 成交量单位 {volume_unit or '未回显'} 不满足股单位资格")
    if amount_unit != _AMOUNT_UNIT:
        raise DataFetchError(f"ifind {identity} 成交额单位 {amount_unit or '未回显'} 不满足元单位资格")


def _qualify_window(
    rows: Dict[_date, Dict[str, str]], sessions: List[_date], identity: str
) -> pd.DataFrame:
    """严格交易日历全窗口资格：任一期望会话缺合格六字段即整窗拒绝。"""
    records: List[Dict[str, object]] = []
    for session in sessions:
        raw = rows.get(session)
        if raw is None:
            raise DataFetchError(
                f"ifind {identity} 完整窗口缺少交易日 {session.isoformat()}，整窗不采纳"
            )
        o = _require_decimal(raw, "开盘价", session, identity, allow_scale=False)
        h = _require_decimal(raw, "最高价", session, identity, allow_scale=False)
        l = _require_decimal(raw, "最低价", session, identity, allow_scale=False)
        c = _require_decimal(raw, "收盘价", session, identity, allow_scale=False)
        v = _require_decimal(raw, _VOLUME_FIELD, session, identity, allow_scale=True)
        a = _require_decimal(raw, _AMOUNT_FIELD, session, identity, allow_scale=True)
        bar = _daily_bar(session.isoformat(), o, h, l, c, v, a)
        records.append({
            "日期": bar.date, "开盘价": bar.open, "最高价": bar.high, "最低价": bar.low,
            "收盘价": bar.close, "成交量": bar.volume, "成交额": bar.amount,
        })
        if len(records) % 256 == 0:
            check_attempt_alive()
    return pd.DataFrame(records)


def _require_decimal(
    raw: Dict[str, str], field: str, session: _date, identity: str, *, allow_scale: bool
) -> Decimal:
    value = _decimal_cell(raw.get(field), allow_scale=allow_scale)
    if value is None or value <= 0:
        raise DataFetchError(
            f"ifind {identity} 交易日 {session.isoformat()} {field} 数值缺失或非法，整窗不采纳"
        )
    return value


class IfindDailyFetcher(BaseFetcher):
    """iFinD 现金分红前复权日线 Fetcher（issue #86）。

    不是转发包装：承载资格身份、明确 CASH-qfq 问句、完整导出、单位/身份/
    全窗口校验与 DataFrame 标准化；MCP 传输、预算与单飞复用既有
    ``IfindFetcher.query``。一次尝试以有限绝对 deadline 贯穿 MCP+CSV+解析+
    采纳（无外层 deadline 时自建并预留 fallback 份额）；深研 run 内整体经
    ``single_fetch`` 单飞（键含窗口/模式/契约/刷新身份），停止后不再把迟到
    成功写入可复用 future。
    """

    name = "IfindDailyFetcher"
    priority = 0

    def __init__(self, fetcher: Optional[IfindFetcher] = None) -> None:
        self._fetcher = fetcher if fetcher is not None else IfindFetcher.get_instance()

    @property
    def available(self) -> bool:
        return bool(getattr(self._fetcher, "available", False))

    def is_available_for_request(self, capability: str = "") -> bool:
        return self.available

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        identity_match = _IDENTITY_RE.match((stock_code or "").strip().upper())
        if identity_match is None:
            raise DataFetchError(f"IfindDailyFetcher unsupported stock identity: {stock_code}")
        identity = f"{identity_match.group(1)}.{identity_match.group(2)}"
        from src.core.research_run import current_research, supplier_need_key
        from src.patches.eastmoney_patch import request_deadline

        # 一次完整尝试的有限绝对 deadline：MCP、导出、解码、解析与最终采纳
        # 共享；无外层时自建（含 fallback 预留），有外层时只被进一步收紧。
        token = request_deadline.set(attempt_deadline())
        try:
            run = current_research.get()
            if run is not None:
                from data_provider.mx_data_adapter import financial_refresh
                from src.config import get_config

                marker = financial_refresh.get()
                refresh = marker[1] if marker else ""
                key = supplier_need_key(
                    "ifind-daily", get_config().data_source_contract, identity,
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
            raise DataFetchError(f"IfindDailyFetcher invalid window {start_date}~{end_date}")
        question = (
            f"{identity}，{start.strftime('%Y%m%d')}-{end.strftime('%Y%m%d')}，每日开盘价、最高价、"
            "最低价、收盘价、成交量（股）、成交额（元）；全部价格明确选择前复权（现金分红），"
            "不是前复权（分红再投）。请在指标参数中回显实际使用的复权方式，"
            "返回完整时间序列或全量CSV导出。"
        )
        data = self._query_daily_mcp(question)
        check_attempt_alive()  # MCP 返回后、出站导出前
        _qualify_metadata(data.indicators_params, identity)
        sessions = trading_sessions_between("cn", start, end)
        if not sessions:
            raise DataFetchError("ifind daily strict calendar unavailable")
        inline_rows = _collect_answer_rows(data.answer, identity)
        truncated = answer_truncated(data.answer)
        export_url = export_download_url(data.answer)
        if truncated:
            if export_url is None:
                raise DataFetchError("ifind answer truncated without a valid export url")
            rows = _parse_csv_rows(stream_export_csv(export_url), identity)
            frame = _qualify_window(rows, sessions, identity)
        else:
            try:
                frame = _qualify_window(inline_rows, sessions, identity)
            except DataFetchError:
                if export_url is None:
                    raise
                logger.info(
                    "[ifind-daily] %s 内联表未覆盖完整窗口，改取完整导出 %s",
                    identity, _sanitize_export_url(export_url),
                )
                rows = _parse_csv_rows(stream_export_csv(export_url), identity)
                frame = _qualify_window(rows, sessions, identity)
        check_attempt_alive()  # 最终采纳前：停止后不采纳迟到窗口
        return frame

    def _query_daily_mcp(self, question: str):
        """日线 MCP 查询。意外的 SDK CancelledError 在控制状态仍健康时才是源失败。"""
        import asyncio

        from src.core.research_run import current_research

        try:
            return self._fetcher.query("get_stock_performance", question)
        except asyncio.CancelledError as exc:
            run = current_research.get()
            if run is not None:
                run.check()
            raise DataFetchError("unexpected MCP transport cancellation") from exc

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        normalized = df.copy()
        normalized = normalized.rename(columns={
            "日期": "date", "开盘价": "open", "最高价": "high", "最低价": "low",
            "收盘价": "close", "成交量": "volume", "成交额": "amount",
        })
        normalized["pct_chg"] = normalized["close"].pct_change().fillna(0.0) * 100
        columns = ["date", "open", "high", "low", "close", "volume", "amount", "pct_chg"]
        return normalized.reindex(columns=columns)


def _sanitize_export_url(url: str) -> str:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname or ''}{parts.path}"


def is_ifind_daily_source(source: object) -> bool:
    """来源标签是否属于本票 iFinD 日线（含持久化 data_source 行）。"""
    return "ifind" in str(source or "").lower()
