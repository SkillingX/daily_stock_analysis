# -*- coding: utf-8 -*-
"""westock（腾讯自选股）CLI 数据源适配层。

数据通路（调研结论 2026-10-04）：
- 本机官方 CLI `~/.local/bin/westock`（Go 二进制，签名层：财务/筹码/两融/股东户数）；
- CLI 输出为 Markdown 表格，本模块解析为标准 dict；
- 行情快照与日/周/月 K 线另有免鉴权公开端点（qt.gtimg.cn / web.ifzq.gtimg.cn），
  见 `get_quote_public` / `get_kline_public`，作为 CLI 不可用时的无凭证 fallback。

约定：所有 get_* 失败（CLI 缺失/超时/无数据）返回 None，不抛异常——
对齐 data_provider 多源 fallback 纪律：单一数据源失败不拖垮主流程。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_CLI_PATH_CANDIDATES = (
    "~/.local/bin/westock",
    "/usr/local/bin/westock",
)
_CLI_TIMEOUT = 25.0


def _cli_path() -> Optional[str]:
    """定位 westock CLI：PATH 优先，候选路径兜底。"""
    found = shutil.which("westock")
    if found:
        return found
    for cand in _CLI_PATH_CANDIDATES:
        path = os.path.expanduser(cand)
        if os.path.exists(path):
            return path
    return None


def to_market_code(code: str) -> str:
    """6 位 A 股代码 → westock/腾讯系市场前缀（6/9→sh，0/2/3→sz，4/8→bj）。

    已是带前缀代码（sh/sz/bj/hk/us 开头）原样返回（前缀小写化）；
    非 6 位数字代码（美股字母码等）原样返回，不做大小写改动。
    """
    c = (code or "").strip()
    low = c.lower()
    if re.match(r"^(sh|sz|bj|hk|us)", low):
        return low
    if not re.fullmatch(r"\d{6}", c):
        return c
    head = c[0]
    if head in ("6", "9"):
        return f"sh{c}"
    if head in ("0", "2", "3"):
        return f"sz{c}"
    if head in ("4", "8"):
        return f"bj{c}"
    return c


def _parse_md_tables(text: str) -> Dict[str, List[Dict[str, Any]]]:
    """Markdown 文本 → {段落标题: [行dict]}。

    标准表状态机：表头行 → 分隔行（跳过）→ 数据行×N；
    遇到非表行（空行/标题）后状态复位。段落标题取最近的 ``**标题**`` 行。
    """
    sections: Dict[str, List[Dict[str, Any]]] = {}
    current_title = "_default"
    header: Optional[List[str]] = None
    in_table = False
    for line in text.splitlines():
        stripped = line.strip()
        m = re.match(r"^\*\*(.+?)\*\*\s*$", stripped)
        if m:
            current_title = m.group(1).strip()
            header, in_table = None, False
            continue
        is_row = stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2
        if not is_row:
            header, in_table = None, False
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            in_table = True  # 分隔行：下一行起是数据
            continue
        if not in_table or header is None:
            header = cells  # 表头行
            continue
        row = {key: _to_number(raw) for key, raw in zip(header, cells)}
        sections.setdefault(current_title, []).append(row)
    return sections


def _to_number(raw: str) -> Any:
    if raw == "" or raw == "--":
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def _run_cli(args: List[str]) -> Optional[str]:
    """调 westock CLI，返回 stdout；任何失败返回 None。"""
    path = _cli_path()
    if not path:
        logger.warning("[westock] CLI 不存在（%s）", _CLI_PATH_CANDIDATES[0])
        return None
    try:
        proc = subprocess.run(
            [path, *args],
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("[westock] CLI 调用失败 %s: %s", args, exc)
        return None
    if proc.returncode != 0:
        logger.warning("[westock] CLI 报错 %s: %s", args, (proc.stderr or "")[:200])
        return None
    return proc.stdout or None


def get_quote(code: str) -> Optional[Dict[str, Any]]:
    """行情快照：price/prev_close/pe/pb/52周高低/换手/量比/市值/涨跌。"""
    mc = to_market_code(code)
    out = _run_cli(["quote", mc])
    if not out:
        return None
    rows = _parse_md_tables(out).get("_default") or []
    return rows[0] if rows else None


def get_chip(code: str) -> Optional[Dict[str, Any]]:
    """筹码分布：平均成本/获利盘比例/集中度 70/90（仅 A 股）。"""
    mc = to_market_code(code)
    out = _run_cli(["chip", mc])
    if not out:
        return None
    rows = _parse_md_tables(out).get("_default") or []
    return rows[0] if rows else None


def get_margin(code: str) -> Optional[Dict[str, Any]]:
    """融资融券：融资余额/融券余额/当日买入偿还（仅沪深）。"""
    mc = to_market_code(code)
    out = _run_cli(["fund", "margin", mc])
    if not out:
        return None
    rows = _parse_md_tables(out).get("_default") or []
    return rows[0] if rows else None


def get_shareholder(code: str) -> Optional[Dict[str, Any]]:
    """股东研究：十大股东/十大流通股东/股东户数序列（最新一期在前）。"""
    mc = to_market_code(code)
    out = _run_cli(["shareholder", mc])
    if not out:
        return None
    sections = _parse_md_tables(out)
    return {
        "top_holders": sections.get("十大股东") or [],
        "top_float_holders": sections.get("十大流通股东") or [],
        "holder_counts": sections.get("股东户数统计") or [],
    }


def get_finance(code: str) -> Optional[Dict[str, Any]]:
    """三大表（最近一期）：利润表/资产负债表/现金流量表，全字段。"""
    mc = to_market_code(code)
    out = _run_cli(["finance", mc])
    if not out:
        return None
    sections = _parse_md_tables(out)
    result: Dict[str, Any] = {}
    for title, key in (("利润表", "income"), ("资产负债表", "balance"), ("现金流量表", "cashflow")):
        rows = sections.get(title) or []
        if rows:
            result[key] = rows[0]
    return result or None


# ---------------------------------------------------------------------------
# 公开免鉴权 fallback（qt.gtimg.cn 快照 + ifzq K 线；CLI 失效时保底）
# ---------------------------------------------------------------------------

_QT_URL = "https://qt.gtimg.cn/q={}"
_FQKLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


def get_quote_public(code: str) -> Optional[Dict[str, Any]]:
    """免鉴权行情快照（GBK 解码，v_ 位置字段）。CLI 不可用时的保底。"""
    import urllib.request

    mc = to_market_code(code)
    try:
        with urllib.request.urlopen(_QT_URL.format(mc), timeout=10) as resp:
            text = resp.read().decode("gb18030", errors="replace")
    except Exception as exc:  # noqa: BLE001 - 保底路径静默降级
        logger.warning("[westock] 公开快照失败 %s: %s", mc, exc)
        return None
    m = re.search(r'v_{}="([^"]*)"'.format(re.escape(mc)), text)
    if not m:
        return None
    f = m.group(1).split("~")
    if len(f) < 47 or not f[1]:
        return None
    try:
        return {
            "code": mc,
            "name": f[1],
            "price": float(f[3] or 0) or None,
            "prev_close": float(f[4] or 0) or None,
            "open": float(f[5] or 0) or None,
            "volume": float(f[6] or 0) or None,
            "time": f[30],
            "change_percent": float(f[32] or 0) or None,
            "high": float(f[33] or 0) or None,
            "low": float(f[34] or 0) or None,
            "turnover_rate": float(f[38] or 0) or None,
            "pe_ratio": float(f[39] or 0) or None,
            "total_market_cap": float(f[45] or 0) or None,
            "pb_ratio": float(f[46] or 0) or None,
        }
    except (ValueError, IndexError):
        return None


def get_kline_public(code: str, period: str = "day", count: int = 60) -> Optional[List[Dict[str, Any]]]:
    """免鉴权 K 线：[{date,open,close,high,low,volume},...]（前复权）。"""
    import urllib.request

    mc = to_market_code(code)
    period_map = {"day": "day", "week": "week", "month": "month"}
    p = period_map.get(period, "day")
    url = f"{_FQKLINE_URL}?param={mc},{p},,,{count},qfq"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[westock] 公开 K 线失败 %s: %s", mc, exc)
        return None
    try:
        node = body["data"][mc]
        rows = node.get(f"qfq{p}") or node.get(p) or []
        return [
            {
                "date": r[0],
                "open": float(r[1]),
                "close": float(r[2]),
                "high": float(r[3]),
                "low": float(r[4]),
                "volume": float(r[5]) if len(r) > 5 else None,
            }
            for r in rows
            if len(r) >= 5
        ]
    except (KeyError, TypeError, ValueError):
        return None
