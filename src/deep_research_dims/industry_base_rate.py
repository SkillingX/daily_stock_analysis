# -*- coding: utf-8 -*-
"""行业基率表：market_implied_p 的行业锚点（需求 §5.4-3）。

数据文件：``data/deep_research/industry_base_rates.json``（keyword → base_rate），
启发式默认，可迭代。匹配失败回退板块中性 0.5 并标记 basis，绝不抛异常。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

_BASE_RATES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
    "deep_research",
    "industry_base_rates.json",
)

_cache: Dict[str, Any] | None = None


def _load_table() -> Dict[str, Any]:
    global _cache
    if _cache is None:
        try:
            with open(_BASE_RATES_PATH, encoding="utf-8") as fh:
                _cache = json.load(fh)
        except (OSError, ValueError) as exc:
            logger.warning("[BaseRate] 基率表读取失败 %s: %s", _BASE_RATES_PATH, exc)
            _cache = {"rates": [], "default": {"base_rate": 0.5}}
    # _cache 初始化为 None，但此处已被 if 分支保证赋值；断言避免 type ignore 噪音
    assert _cache is not None
    return _cache


def reload_table() -> None:
    """强制重载基率表（配置热迭代用）。"""
    global _cache
    _cache = None
    _load_table()


def _entries() -> List[Dict[str, Any]]:
    table = _load_table()
    rates = table.get("rates")
    return rates if isinstance(rates, list) else []


def lookup_base_rate(text: str | None) -> Tuple[float, str]:
    """按关键词在文本中匹配行业基率。

    Returns:
        (base_rate, basis)：basis 为 "industry_base_rate:<keyword>" 或
        "neutral_default"（未命中/无文本）。
    """
    default = float((_load_table().get("default") or {}).get("base_rate") or 0.5)
    if not text:
        return default, "neutral_default"
    for entry in _entries():
        keywords = entry.get("keywords") or []
        for kw in keywords:
            if isinstance(kw, str) and kw and kw in text:
                rate = entry.get("base_rate")
                if isinstance(rate, (int, float)) and 0.0 <= rate <= 1.0:
                    return float(rate), f"industry_base_rate:{kw}"
    return default, "neutral_default"
