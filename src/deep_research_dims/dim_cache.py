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
    # six_dim 依赖 data/F1/F2(24h) 与 intel(4h)：TTL 不得超过最小依赖的日级上限，
    # 否则会出现"旧评分快照 + 新情报正文"的一致性问题（审计 C8）
    "six_dim": 24.0,
    "scenarios": 120.0,
    "supply_chain": 120.0,
    "fundamental": 24.0,  # F1 基本面快照驱动（日内不变）
    "sector": 24.0,  # F2 政策倾向/基率日内稳定
    "technical": 24.0,
    "capital": 24.0,
    "sentiment": 24.0,
    "ownership": 24.0,  # 高管动态时效：审计 A4，不设 5d
    "us_china": 120.0,
    "business": 24.0,
}

# 契约结构变更（18 维研究员 payload）→ bump

# 契约 schema 版本：维度 payload 结构变更必须 bump（审计 C4），载入版本不符即 miss
SCHEMA_VERSION = 7  # BusinessDim 四字段为 Dict；F1结构仅由财务mapping版本隔离

CACHEABLE_DIMS = frozenset(DIM_TTL_HOURS)
FUNDAMENTAL_MAPPING_VERSION = 9  # F1逐字段品质及评分资格，旧无品质缓存拒读
_FUNDAMENTAL_MAPPING_DIMS = frozenset({"fundamental", "six_dim", "scenarios"})


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
    if record.get("schema_version") != SCHEMA_VERSION:
        return None  # 旧契约 payload 不兼容（C4）：视为 miss，重算并覆盖
    if dim in _FUNDAMENTAL_MAPPING_DIMS and record.get("mapping_version") != FUNDAMENTAL_MAPPING_VERSION:
        return None  # 基本面及依赖维度的旧载荷可能含已丢失的零值/市值
    if dim in _FUNDAMENTAL_MAPPING_DIMS:
        from src.config import get_config
        if get_config().deep_research_cross_validate:
            return None  # 本次核验依赖当前读数，不能复用以前的high或派生评分
        if record.get("cross_validation_enabled") != bool(get_config().deep_research_cross_validate):
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
            "schema_version": SCHEMA_VERSION,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "stock_code": stock_code,
            "dim": dim,
            "payload": payload,
        }
        if dim in _FUNDAMENTAL_MAPPING_DIMS:
            record["mapping_version"] = FUNDAMENTAL_MAPPING_VERSION
            from src.config import get_config
            record["cross_validation_enabled"] = bool(get_config().deep_research_cross_validate)
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
