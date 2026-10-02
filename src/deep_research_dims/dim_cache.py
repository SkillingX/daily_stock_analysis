# -*- coding: utf-8 -*-
"""维度产物缓存（需求 §8-4：TTL 分档，同票复用未过期维度省钱）。

- 可缓存维度与 TTL：情报 4h（盘中易变）、数据 1 交易日、长线维度
  （六维/情景/产业链）5 交易日；
- 派生维度（贝叶斯/结论/计划/信号）**不缓存**——它们依赖其他维度产物，
  缓存会把过期依赖固化成"新结论"；纯规则维度（阶段/历史）毫秒级，不缓存；
- 降级产物不入缓存（下次重算给修复机会）；
- force_refresh 由编排器调用方传入，整体跳过读取。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
    "deep_research",
    ".dim_cache",
)

# 维度 → TTL（小时）
DIM_TTL_HOURS: Dict[str, float] = {
    "intel": 4.0,
    "data": 24.0,
    "six_dim": 120.0,
    "scenarios": 120.0,
    "supply_chain": 120.0,
}

CACHEABLE_DIMS = frozenset(DIM_TTL_HOURS)


def _cache_path(stock_code: str, dim: str) -> str:
    return os.path.join(_CACHE_DIR, f"{stock_code}_{dim}.json")


def load_cached_dim(stock_code: str, dim: str) -> Optional[Dict[str, Any]]:
    """命中且未过期返回维度 payload dict；否则 None（含一切异常）。"""
    ttl = DIM_TTL_HOURS.get(dim)
    if ttl is None:
        return None
    path = _cache_path(stock_code, dim)
    try:
        with open(path, encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, ValueError):
        return None
    saved_at = str(record.get("saved_at") or "")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    try:
        saved_time = datetime.fromisoformat(saved_at)
    except ValueError:
        return None
    if datetime.now() - saved_time > timedelta(hours=ttl):
        return None
    return payload


def save_cached_dim(stock_code: str, dim: str, payload: Dict[str, Any]) -> None:
    """写缓存（降级产物请勿调用）。失败只记日志。"""
    if dim not in CACHEABLE_DIMS:
        return
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        record = {
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "stock_code": stock_code,
            "dim": dim,
            "payload": payload,
        }
        with open(_cache_path(stock_code, dim), "w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False, default=str)
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("[DimCache] 写缓存失败 %s/%s: %s", stock_code, dim, exc)


def clear_cache(stock_code: Optional[str] = None) -> int:
    """清理缓存（stock_code=None 全清）。返回删除文件数。"""
    removed = 0
    try:
        names = os.listdir(_CACHE_DIR)
    except OSError:
        return 0
    for name in names:
        if not name.endswith(".json"):
            continue
        if stock_code and not name.startswith(f"{stock_code}_"):
            continue
        try:
            os.remove(os.path.join(_CACHE_DIR, name))
            removed += 1
        except OSError:
            pass
    return removed


# ---------------------------------------------------------------------------
# 阶段 0 快照缓存（基本面等日内不变的数据源，TTL 由调用方指定）
# ---------------------------------------------------------------------------


def load_snapshot(key: str, ttl_hours: float) -> Optional[Dict[str, Any]]:
    """通用快照缓存：命中且未过期返回 payload；key 自带命名空间。"""
    path = os.path.join(_CACHE_DIR, f"snap_{key}.json")
    try:
        with open(path, encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, ValueError):
        return None
    saved_at = str(record.get("saved_at") or "")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    try:
        saved_time = datetime.fromisoformat(saved_at)
    except ValueError:
        return None
    if datetime.now() - saved_time > timedelta(hours=ttl_hours):
        return None
    return payload


def save_snapshot(key: str, payload: Dict[str, Any]) -> None:
    """写快照缓存（失败只记日志）。空 payload 不缓存（保留重试机会）。"""
    if not payload:
        return
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        record = {
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "key": key,
            "payload": payload,
        }
        with open(os.path.join(_CACHE_DIR, f"snap_{key}.json"), "w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False, default=str)
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("[DimCache] 写快照缓存失败 %s: %s", key, exc)
