# -*- coding: utf-8 -*-
"""维度产物缓存（需求 §8-4：TTL 分档，同票复用未过期维度省钱）。

- 可缓存维度与 TTL：情报 4h（盘中易变）、数据 1 交易日、长线维度
  （六维/情景/产业链）5 交易日；
- 派生维度（贝叶斯/结论/计划/信号）**不缓存**——它们依赖其他维度产物，
  缓存会把过期依赖固化成"新结论"；纯规则维度（阶段/历史）毫秒级，不缓存；
- 降级产物不入缓存（下次重算给修复机会）；
- force_refresh 由编排器调用方传入，整体跳过读取；
- 情报（intel）缓存额外核对情报契约版本与本次已采用行情身份（issue #68）：
  变价、跨观测时点、无行情与身份不足均 miss 重算，搜索资料缓存不受影响。
"""

from __future__ import annotations

import json
import logging
import math
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

# 维度 → TTL（小时）。issue #64：ownership 研究退役，不再缓存该维度。
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
    "us_china": 120.0,
    "business": 24.0,
}

# 契约结构变更（18 维研究员 payload）→ bump

# 契约 schema 版本：维度 payload 结构变更必须 bump（审计 C4），载入版本不符即 miss
# v15（issue #64）：18 维有效范围与 v2.2 评分口径；旧缓存（含旧机构/治理分、旧
# 退役缺口）重算，历史报告不改写。
SCHEMA_VERSION = 15
# 本次来源提示已变、且编排器会读入新报告的维度。缺版本即 miss，不改 payload 正文。
# 不 bump 全局 SCHEMA_VERSION / 基本面 mapping，context 维度本就不走这段缓存。
SOURCE_LABEL_VERSION = 1
_SOURCE_LABEL_DIMS = frozenset({
    "business", "intel", "supply_chain", "sentiment", "us_china",
})

# issue #68：情报契约收敛（行情依据 + 事件预案为研究核验动作）只影响 intel
# 维度；旧 intel 缓存按本版本失效重算，其他维度缓存与搜索资料缓存不受影响。
INTEL_CONTRACT_VERSION = 2
_INTEL_CONTRACT_DIMS = frozenset({"intel"})

CACHEABLE_DIMS = frozenset(DIM_TTL_HOURS)
FUNDAMENTAL_MAPPING_VERSION = 20  # issue #64：阶段0退役行业榜/机构侧后快照内容变化，旧快照重算
_FUNDAMENTAL_MAPPING_DIMS = frozenset({"fundamental", "six_dim", "scenarios"})


def _cache_path(stock_code: str, dim: str) -> str:
    from src.config import get_config
    contract = get_config().data_source_contract
    suffix = "" if contract == "legacy" else f"_{contract}"
    return os.path.join(_CACHE_DIR, f"{stock_code}{suffix}_{dim}.json")



def capital_block_within_date_boundary(block: Any, *, now: Optional[Any] = None) -> bool:
    """资金块日期采纳谓词（B2/R12 共享）：actual/expected 双日期保守判定。

    输入可为 CapitalDim 载荷或含 ``data.stock_flow`` 的 manager 块；缺证据/乱序/
    未来/跨收盘旧回退一律 False（正常 miss 刷新，规格允许）。不新增探测请求。
    """
    flow = block
    if isinstance(block, dict) and isinstance(block.get("data"), dict):
        flow = (block["data"].get("stock_flow") or {}) if "stock_flow" in block.get("data", {}) else block.get("data") or {}
    if not isinstance(flow, dict):
        return False
    # CapitalDim 序列化后实际截至日字段名随 Pydantic 契约；兼容历史已保存载荷。
    as_of = str(flow.get("as_of_date") or flow.get("as_of_date") or "")
    expected = str(flow.get("expected_date") or "")
    if not as_of or not expected:
        return False
    try:
        from datetime import date as _date, datetime as _datetime

        as_of_day = _date.fromisoformat(as_of[:10])
        expected_day = _date.fromisoformat(expected[:10])
    except (ValueError, TypeError):
        return False
    today = now if now is not None else _datetime.now().date()
    if as_of_day > expected_day or expected_day > today:
        return False
    # 保存时证据仍"新鲜"（T_save ∈ {今天, 昨天}）才可无出站采信：T_now 相对
    # T_save 最多前进一个收盘；跨收盘后（如 10/28 晚存的 10/27 证据在 10/29 读）
    # 无法证明当前边界 → False（正常 miss 刷新）。
    from datetime import timedelta as _timedelta

    if expected_day not in (today, today - _timedelta(days=1)):
        return False
    if as_of_day == expected_day:
        return True
    # 保存时 T-1 回退采用：仅当证据日 == 今天（T_now 仍 == T_save，as_of 仍合法）
    return expected_day == today


def valid_quote_price(price: Any) -> bool:
    """已采用现价标量的有效性边界（issue #68）：有限正数值，bool 不算数值。

    情报消息、经理市场摘要、阶段 0 行情采纳与缓存身份共用同一边界——
    0/负/NaN/Inf/bool/非数值一律不构成本报告已采用现价，消费方走
    「无有效实时行情」披露分支，不注入权威现价。
    """
    return (
        not isinstance(price, bool)
        and isinstance(price, (int, float))
        and math.isfinite(price)
        and price > 0
    )


def quote_market_identity(quote: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """本报告已采用行情的缓存复用身份（issue #68）。

    返回值三态：
    - dict：有效身份，price+source+observed_at+unit+currency+资格齐备；
    - ``{"absent": True}``：本报告明确无有效行情（缺价或标量无效——
      0/负/NaN/Inf/bool 等经 ``valid_quote_price`` 拒绝；两次无行情会话状态
      等价，可复用）；
    - None：有有效价但身份不足——调用方不得写入缓存，读取时也无法与任何
      记录相等（保守 miss），不伪造时点。

    元数据解码复用既有 ``reading_from_field_record``：scalar 与 meta.value 不符
    （借用旧来源/时点）、元数据无效一律身份不足；解码后 source=unknown 或缺
    观测时点同样身份不足。只记录行情观测语义，不含报告装配时钟
    （fetched_at/as_of）：同一有效身份不因报告生成时间不同而失去复用。
    rule_eligible/input_reasons/quality_status 保留真实三态记录，未知不冒充 False。
    """
    if not isinstance(quote, dict) or not valid_quote_price(quote.get("price")):
        return {"absent": True}
    price = quote["price"]
    from data_provider.cross_source_validator import reading_from_field_record

    meta = (quote.get("field_meta") or {}).get("current_price") or {}
    reading = reading_from_field_record(price, meta)
    if reading is None or reading.normalization_error:
        return None
    if reading.source in ("", "unknown") or not reading.observed_at:
        return None
    return {
        "price": float(reading.value),
        "source": str(reading.source),
        "observed_at": str(reading.observed_at),
        "unit": str(reading.unit or ""),
        "currency": str(reading.currency or ""),
        "rule_eligible": meta.get("rule_eligible"),
        "input_reasons": [str(reason) for reason in (meta.get("input_reasons") or [])],
        "quality_status": str(((meta.get("quality") or {}).get("status")) or ""),
    }


def load_cached_dim(
    stock_code: str, dim: str, quote_identity: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    """命中且未过期返回维度 payload dict；否则 None（含一切异常）。

    issue #68：intel 维度额外校验情报契约版本与已采用行情身份——版本不符或
    身份缺失/不一致（变价、跨观测时点、无行情混用）一律 miss 重算；身份不足
    （quote_identity=None）不匹配任何记录。
    """
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
    if dim in _SOURCE_LABEL_DIMS and record.get("source_label_version") != SOURCE_LABEL_VERSION:
        return None  # 旧来源等级契约可能把 F10/社区标签带回新报告
    if dim in _INTEL_CONTRACT_DIMS and record.get("intel_contract_version") != INTEL_CONTRACT_VERSION:
        return None  # 旧情报契约（无行情依据/独立交易预案）重算
    if dim in _INTEL_CONTRACT_DIMS:
        record_identity = record.get("quote_identity")
        # 双缺身份（记录缺字段或调用方身份不足）不得视为相等——不可证明相同
        # 即保守 miss，避免无身份复用混入跨行情结论（issue #68 root 反例）。
        if (
            not isinstance(record_identity, dict)
            or not isinstance(quote_identity, dict)
            or record_identity != quote_identity
        ):
            return None
    if dim in _FUNDAMENTAL_MAPPING_DIMS and record.get("mapping_version") != FUNDAMENTAL_MAPPING_VERSION:
        return None  # 基本面及依赖维度的旧载荷可能含已丢失的零值/市值
    if dim in _FUNDAMENTAL_MAPPING_DIMS:
        from src.config import get_config
        if get_config().deep_research_cross_validate or (get_config().data_source_profile == "choice" and get_config().enable_ifind_mcp):
            return None  # 本次核验依赖当前读数，不能复用以前的high或派生评分
        if record.get("cross_validation_enabled") != bool(get_config().deep_research_cross_validate):
            return None
    saved_at = str(record.get("saved_at") or "")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    if dim == "business":
        from src.deep_research.researchers.business import parse
        try:
            business, _ = parse(payload, 0)
        except (ValueError, TypeError):
            return None
        if business.status != "ok":
            return None
    if dim == "capital":
        # issue #64/R12/B2：资金缓存采用走共享日期采纳谓词（保存证据保守判定，
        # 不用自然日常数替代交易日资格）；跨收盘旧回退等无法证明当前边界的情形
        # 一律 miss，走既有刷新路径（规格允许必要刷新）。
        if not capital_block_within_date_boundary(payload):
            return None
    try:
        saved_time = datetime.fromisoformat(saved_at)
    except ValueError:
        return None
    if datetime.now() - saved_time > timedelta(hours=ttl):
        return None
    return payload


def save_cached_dim(
    stock_code: str, dim: str, payload: Dict[str, Any],
    quote_identity: Optional[Dict[str, Any]] = None,
) -> None:
    """写缓存（降级产物请勿调用）。失败只记日志。

    intel 维度必须携带 ``quote_market_identity`` 的结果；身份不足（None）的
    情报不落盘（下次无法核验复用身份，宁重算不伪造）。
    """
    if dim not in CACHEABLE_DIMS:
        return
    if dim in _INTEL_CONTRACT_DIMS and quote_identity is None:
        logger.warning("[DimCache] 情报缓存跳过：行情身份不足，无法核验复用身份 %s", stock_code)
        return
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        stored = payload
        if dim in _SOURCE_LABEL_DIMS:
            from src.report_language import adopt_structured_source_grades
            stored = adopt_structured_source_grades(payload)
        record = {
            "schema_version": SCHEMA_VERSION,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "stock_code": stock_code,
            "dim": dim,
            "payload": stored,
        }
        if dim in _SOURCE_LABEL_DIMS:
            record["source_label_version"] = SOURCE_LABEL_VERSION
        if dim in _INTEL_CONTRACT_DIMS:
            record["intel_contract_version"] = INTEL_CONTRACT_VERSION
            record["quote_identity"] = quote_identity
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
