# -*- coding: utf-8 -*-
"""
===================================
实时行情统一类型定义 & 熔断机制
===================================

设计目标：
1. 统一各数据源的实时行情返回结构
2. 实现熔断/冷却机制，避免连续失败时反复请求
3. 支持多数据源故障切换

使用方式：
- 所有 Fetcher 的 get_realtime_quote() 统一返回 UnifiedRealtimeQuote
- CircuitBreaker 管理各数据源的熔断状态
"""

import copy
import logging
import math
import time
from decimal import Decimal
from threading import RLock
from dataclasses import dataclass, field
from typing import Optional, Dict, Any
from enum import Enum

from icontract import require, ensure

logger = logging.getLogger(__name__)


# ============================================
# 通用类型转换工具函数
# ============================================
# 设计说明：
# 各数据源返回的原始数据类型不一致（str/float/int/NaN），
# 使用这些函数统一转换，避免在各 Fetcher 中重复定义。

@require(lambda default: default is None or isinstance(default, (int, float)),
         "default must be a numeric scalar or None")
@ensure(lambda result: result is None or isinstance(result, float),
        "result must be a float or None")
def safe_float(val: Any, default: Optional[float] = None) -> Optional[float]:
    """
    安全转换为浮点数

    处理场景：
    - None / 空字符串 → default
    - pandas NaN / numpy NaN → default
    - 数值字符串 → float
    - 已是数值 → float

    Args:
        val: 待转换的值
        default: 转换失败时的默认值

    Returns:
        转换后的浮点数，或默认值
    """
    try:
        if val is None:
            return default

        # 处理字符串
        if isinstance(val, str):
            val = val.strip()
            if val == "" or val == "-" or val == "--":
                return default

        # 处理 pandas/numpy NaN
        # 使用 math.isnan 而不是 pd.isna，避免强制依赖 pandas
        import math
        try:
            if math.isnan(float(val)):
                return default
        except (ValueError, TypeError):
            pass

        return float(val)
    except (ValueError, TypeError):
        return default


@require(lambda default: default is None or isinstance(default, int),
         "default must be an int or None")
@ensure(lambda result: result is None or isinstance(result, int),
        "result must be an int or None")
def safe_int(val: Any, default: Optional[int] = None) -> Optional[int]:
    """
    安全转换为整数

    先转换为 float，再取整，处理 "123.0" 这类情况

    Args:
        val: 待转换的值
        default: 转换失败时的默认值

    Returns:
        转换后的整数，或默认值
    """
    f_val = safe_float(val, default=None)
    if f_val is not None:
        return int(f_val)
    return default


class RealtimeSource(Enum):
    """实时行情数据源"""
    EFINANCE = "efinance"           # 东方财富（efinance库）
    AKSHARE_EM = "akshare_em"       # 东方财富（akshare库）
    AKSHARE_SINA = "akshare_sina"   # 新浪财经
    AKSHARE_QQ = "akshare_qq"       # 腾讯财经
    TUSHARE = "tushare"             # Tushare Pro
    TENCENT = "tencent"             # 腾讯直连
    SINA = "sina"                   # 新浪直连
    STOOQ = "stooq"                 # Stooq 美股兜底
    LONGBRIDGE = "longbridge"       # 长桥（美股/港股兜底）
    FALLBACK = "fallback"           # 降级兜底


@dataclass
class UnifiedRealtimeQuote:
    """
    统一实时行情数据结构
    
    设计原则：
    - 各数据源返回的字段可能不同，缺失字段用 None 表示
    - 主流程使用 getattr(quote, field, None) 获取，保证兼容性
    - source 字段标记数据来源，便于调试
    """
    code: str
    name: str = ""
    source: RealtimeSource = RealtimeSource.FALLBACK

    # === 数据质量元数据（由 DataFetcherManager 统一补齐）===
    fetched_at: Optional[str] = None             # 本系统获取时间（ISO 8601 datetime）
    provider_timestamp: Optional[str] = None     # Provider 真实行情时间（ISO 8601 datetime）
    is_stale: Optional[bool] = None              # provider_timestamp 超过最小 TTL 阈值时为 True
    stale_seconds: Optional[int] = None          # provider_timestamp 距 fetched_at 的秒数
    fallback_from: Optional[str] = None          # 整源 fallback 的失败首选源 token
    field_meta: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    currency: Optional[str] = None
    
    # === 核心价格数据（几乎所有源都有）===
    price: Optional[float] = None           # 最新价
    change_pct: Optional[float] = None      # 涨跌幅(%)
    change_amount: Optional[float] = None   # 涨跌额
    
    # === 量价指标（部分源可能缺失）===
    volume: Optional[int] = None            # 成交量（股，与历史日线口径一致）
    amount: Optional[float] = None          # 成交额（元）
    volume_ratio: Optional[float] = None    # 量比
    turnover_rate: Optional[float] = None   # 换手率(%)
    amplitude: Optional[float] = None       # 振幅(%)
    
    # === 价格区间 ===
    open_price: Optional[float] = None      # 开盘价
    high: Optional[float] = None            # 最高价
    low: Optional[float] = None             # 最低价
    pre_close: Optional[float] = None       # 昨收价
    
    # === 估值指标（仅东财等全量接口有）===
    pe_ratio: Optional[float] = None        # 市盈率(动态)
    pb_ratio: Optional[float] = None        # 市净率
    total_mv: Optional[float] = None        # 总市值(元)
    circ_mv: Optional[float] = None         # 流通市值(元)
    
    # === 其他指标 ===
    change_60d: Optional[float] = None      # 60日涨跌幅(%)
    high_52w: Optional[float] = None        # 52周最高
    low_52w: Optional[float] = None         # 52周最低
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典（过滤 None 值）"""
        result = {
            'code': self.code,
            'name': self.name,
            'source': self.source.value,
        }
        # 只添加非 None 的字段
        optional_fields = [
            'fetched_at', 'provider_timestamp', 'is_stale', 'stale_seconds',
            'fallback_from',
            'price', 'change_pct', 'change_amount', 'volume', 'amount',
            'volume_ratio', 'turnover_rate', 'amplitude',
            'open_price', 'high', 'low', 'pre_close',
            'pe_ratio', 'pb_ratio', 'total_mv', 'circ_mv',
            'change_60d', 'high_52w', 'low_52w'
        ]
        for f in optional_fields:
            val = getattr(self, f, None)
            if val is not None:
                result[f] = val
        if self.field_meta:
            result["field_meta"] = self.field_meta
        if self.currency is not None:
            result["currency"] = self.currency
        return result
    
    def has_basic_data(self) -> bool:
        """检查是否有基本的价格数据"""
        return self.price is not None and self.price > 0
    
    def has_volume_data(self) -> bool:
        """检查是否有量价数据"""
        return self.volume_ratio is not None or self.turnover_rate is not None


QUOTE_ANCHOR_READINGS: Dict[str, str] = {
    "current_price": "price",
    "pe_ratio": "pe_ratio",
    "pb_ratio": "pb_ratio",
    "total_mv": "total_mv",
    "circ_mv": "circ_mv",
}
QUOTE_ANCHOR_FIELDS = frozenset(QUOTE_ANCHOR_READINGS)

# UnifiedRealtimeQuote 实际暴露的全部数值标量（与 to_dict 可选字段一致）；
# manager 采纳边界的异常值清理只针对这些字段，不递归处理第三方元数据。
QUOTE_NUMERIC_FIELDS: tuple[str, ...] = (
    "price", "change_pct", "change_amount", "volume", "amount",
    "volume_ratio", "turnover_rate", "amplitude",
    "open_price", "high", "low", "pre_close",
    "pe_ratio", "pb_ratio", "total_mv", "circ_mv",
    "change_60d", "high_52w", "low_52w",
)

# 标量字段名 -> field_meta 记录键（锚点记录键与标量名不同，如 price/current_price）。
_QUOTE_SCALAR_RECORD_KEYS: Dict[str, str] = {
    scalar: key for key, scalar in QUOTE_ANCHOR_READINGS.items()
}


def is_non_finite_number(value: Any) -> bool:
    """True 仅当 value 可确认为 NaN/±inf。

    bool 与非数值标量返回 False；字符串不转换、不判定（"inf"/"nan" 等非
    数值元数据保持原样）；非数字容器（list/dict 等）不是数值，一律 False；
    Decimal 按自身 ``is_finite`` 判定（覆盖 NaN/sNaN/±Inf 与有限大值）；
    int 恒有限（含超出 float 范围的大整数）；float（含 np.float64 子类）
    按 isfinite 判定；其它数值类型（NumPy float32/longdouble 等）用原生
    比较判定——NaN 与自身不等、±Inf 与 float("inf") 精确相等——不经
    float64 饱和转换，比较运算不依赖算术副作用（numpy 全局 errstate 不
    影响），平台上表示范围更大的有限 longdouble 保留；判定失败按有限
    处理，不因转换错误拒绝数据源。
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, Decimal):
        return not value.is_finite()
    if isinstance(value, int):
        return False
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, (str, bytes)):
        return False
    try:
        if value != value:  # NaN 与自身不等（quiet NaN 比较无算术副作用）
            return True
        positive_infinity = float("inf")
        return bool(value == positive_infinity or value == -positive_infinity)
    except (TypeError, ValueError, ArithmeticError):
        return False


def sanitize_non_finite_quote_scalars(
    quote: UnifiedRealtimeQuote,
) -> UnifiedRealtimeQuote:
    """行情对象上可确认的 NaN/±inf 数值标量按缺失处理后返回。

    在 manager 采纳判断（``has_basic_data``）之前调用：``price=+inf`` 能通过
    现有 ``price > 0`` 判断，必须在边界先清理。无非法字段时原样返回同一对象
    （兼容既有行为，不复制）；确有非法字段时返回最小浅副本——fetcher 可能
    持有并复用返回对象，原引用不被修改——副本上的非法标量置为缺失，并同步
    移除对应 field_meta 记录（含 ``price`` 的 ``current_price`` 别名键与
    ``quote_supplement`` 记录）；保留记录做浅复制——下游 enrich 会原地改写
    ``record["is_stale"]``，不复制会把改动传染给 fetcher 持有的原对象嵌套
    metadata；合法 Decimal/NumPy 数值、0 与有限负值保持原样。
    """
    cleared = [
        f
        for f in QUOTE_NUMERIC_FIELDS
        if (value := getattr(quote, f, None)) is not None
        and is_non_finite_number(value)
    ]
    if not cleared:
        return quote
    cleaned = copy.copy(quote)
    for f in cleared:
        setattr(cleaned, f, None)
    meta = getattr(cleaned, "field_meta", None)
    if meta:
        drop = {_QUOTE_SCALAR_RECORD_KEYS.get(f, f) for f in cleared}
        cleaned.field_meta = {
            k: dict(rec) if isinstance(rec, dict) else rec
            for k, rec in meta.items()
            if k not in drop
        }
    return cleaned


# 非锚点补充字段及其量纲：主源投影与合并采纳共用同一契约（issue #93）。
QUOTE_SUPPLEMENT_FIELD_UNITS: Dict[str, str] = {
    "volume_ratio": "multiple",
    "turnover_rate": "percentage_point",
    "amplitude": "percentage_point",
}


def quote_field_records(quote: UnifiedRealtimeQuote) -> Dict[str, Dict[str, Any]]:
    """Keep field provenance when a unified quote is merged or projected to a tool.

    锚点字段经资格投影生成 ``rule_eligible`` 记录；量比/换手/振幅投影
    主源自身的来源与时间记录（复审反例 1），合并采纳的 ``quote_supplement``
    记录保留补充源身份不被覆盖；无效标量（bool/非数值/非有限值）不产生记录。
    """
    from data_provider.cross_source_validator import adopted_field_record, reading_from_field_record
    records = dict(getattr(quote, "field_meta", {}) or {})
    raw_source = getattr(quote, "source", "unknown")
    source = raw_source.value if hasattr(raw_source, "value") else str(raw_source)
    for anchor, attribute in QUOTE_ANCHOR_READINGS.items():
        value = getattr(quote, attribute, None)
        metadata = records.get(anchor) or {}
        reading = reading_from_field_record(value, {
            "source": source, "unit": "multiple" if anchor in {"pe_ratio", "pb_ratio"} else "currency_base",
            "currency": getattr(quote, "currency", None), "observed_at": getattr(quote, "provider_timestamp", None),
            "fetched_at": getattr(quote, "fetched_at", None), "is_stale": getattr(quote, "is_stale", None), **metadata,
        })
        if reading is None:
            continue
        records[anchor] = adopted_field_record(anchor, reading, previous_record=metadata, selection_reason=str(metadata.get("selection_reason") or "primary"))
    for supplement_field in QUOTE_SUPPLEMENT_FIELD_UNITS:
        existing = records.get(supplement_field)
        if existing is not None and existing.get("selection_reason") == "quote_supplement":
            continue  # 合并采纳时已写入补充源溯源，不得用 quote 级来源覆盖
        value = getattr(quote, supplement_field, None)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            continue
        records[supplement_field] = {
            "field": supplement_field,
            "value": value,
            "source": source,
            "unit": QUOTE_SUPPLEMENT_FIELD_UNITS[supplement_field],
            "observed_at": getattr(quote, "provider_timestamp", None),
            "fetched_at": getattr(quote, "fetched_at", None),
            "is_stale": getattr(quote, "is_stale", None),
            "selection_reason": "primary",
        }
    return records


class CircuitBreaker:
    """
    熔断器 - 管理数据源的熔断/冷却状态
    
    策略：
    - 连续失败 N 次后进入熔断状态
    - 熔断期间跳过该数据源
    - 冷却时间后自动恢复半开状态
    - 半开状态下单次成功则完全恢复，失败则继续熔断
    
    状态机：
    CLOSED（正常） --失败N次--> OPEN（熔断）--冷却时间到--> HALF_OPEN（半开）
    HALF_OPEN --成功--> CLOSED
    HALF_OPEN --失败--> OPEN
    """
    
    # 状态常量
    CLOSED = "closed"      # 正常状态
    OPEN = "open"          # 熔断状态（不可用）
    HALF_OPEN = "half_open"  # 半开状态（试探性请求）
    
    def __init__(
        self,
        failure_threshold: int = 3,       # 连续失败次数阈值
        cooldown_seconds: float = 300.0,  # 冷却时间（秒），默认5分钟
        half_open_max_calls: int = 1      # 半开状态最大尝试次数
    ):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.half_open_max_calls = half_open_max_calls
        
        # 各数据源状态 {source_name: {state, failures, last_failure_time, half_open_calls}}
        self._states: Dict[str, Dict[str, Any]] = {}
        self._lock = RLock()
    
    def _get_state_locked(self, source: str) -> Dict[str, Any]:
        """获取或初始化数据源状态（调用方需持有锁）。"""
        if source not in self._states:
            self._states[source] = {
                'state': self.CLOSED,
                'failures': 0,
                'last_failure_time': 0.0,
                'half_open_calls': 0
            }
        return self._states[source]
    
    def is_available(self, source: str) -> bool:
        """
        检查数据源是否可用
        
        返回 True 表示可以尝试请求
        返回 False 表示应跳过该数据源
        """
        with self._lock:
            state = self._get_state_locked(source)
            current_time = time.time()

            if state['state'] == self.CLOSED:
                return True

            if state['state'] == self.OPEN:
                # 检查冷却时间
                time_since_failure = current_time - state['last_failure_time']
                if time_since_failure >= self.cooldown_seconds:
                    # 冷却完成，进入半开状态（不预占名额，由 HALF_OPEN 分支统一管理）
                    state['state'] = self.HALF_OPEN
                    state['half_open_calls'] = 0
                    state['last_failure_time'] = current_time
                    logger.info(f"[熔断器] {source} 冷却完成，进入半开状态")
                    # Fall through to HALF_OPEN check below
                else:
                    remaining = self.cooldown_seconds - time_since_failure
                    logger.debug(f"[熔断器] {source} 处于熔断状态，剩余冷却时间: {remaining:.0f}s")
                    return False

            if state['state'] == self.HALF_OPEN:
                if state['half_open_calls'] < self.half_open_max_calls:
                    state['half_open_calls'] += 1
                    return True
                # 所有探测名额已用完；若冷却时间再次到期仍未收到
                # record_success/record_failure 回调，重置名额允许重新探测，
                # 避免永久卡在 HALF_OPEN。
                time_since_failure = current_time - state['last_failure_time']
                if time_since_failure >= self.cooldown_seconds:
                    state['half_open_calls'] = 1
                    state['last_failure_time'] = current_time
                    logger.info(f"[熔断器] {source} 半开状态探测超时，重新探测")
                    return True
                return False

            return True
    
    def record_inconclusive(self, source: str) -> None:
        """记录不确定的探测结果（如返回 None）。

        仅影响 HALF_OPEN 状态：将其转回 OPEN 以便冷却后重新探测。
        CLOSED 状态下为空操作，不影响失败计数。
        """
        with self._lock:
            state = self._get_state_locked(source)
            if state['state'] == self.HALF_OPEN:
                state['state'] = self.OPEN
                state['half_open_calls'] = 0
                state['last_failure_time'] = time.time()
                logger.info(f"[熔断器] {source} 半开探测结果不确定，重新进入冷却")

    def record_success(self, source: str) -> None:
        """记录成功请求"""
        with self._lock:
            state = self._get_state_locked(source)

            if state['state'] == self.HALF_OPEN:
                # 半开状态下成功，完全恢复
                logger.info(f"[熔断器] {source} 半开状态请求成功，恢复正常")

            # 重置状态
            state['state'] = self.CLOSED
            state['failures'] = 0
            state['half_open_calls'] = 0
    
    def record_failure(self, source: str, error: Optional[str] = None) -> None:
        """记录失败请求"""
        with self._lock:
            state = self._get_state_locked(source)
            current_time = time.time()

            state['failures'] += 1
            state['last_failure_time'] = current_time

            if state['state'] == self.HALF_OPEN:
                # 半开状态下失败，继续熔断
                state['state'] = self.OPEN
                state['half_open_calls'] = 0
                logger.warning(f"[熔断器] {source} 半开状态请求失败，继续熔断 {self.cooldown_seconds}s")
            elif state['failures'] >= self.failure_threshold:
                # 达到阈值，进入熔断
                state['state'] = self.OPEN
                logger.warning(f"[熔断器] {source} 连续失败 {state['failures']} 次，进入熔断状态 "
                              f"(冷却 {self.cooldown_seconds}s)")
                if error:
                    logger.warning(f"[熔断器] 最后错误: {error}")
    
    def get_status(self) -> Dict[str, str]:
        """获取所有数据源状态"""
        with self._lock:
            return {source: info['state'] for source, info in self._states.items()}
    
    def reset(self, source: Optional[str] = None) -> None:
        """重置熔断器状态"""
        with self._lock:
            if source:
                if source in self._states:
                    del self._states[source]
            else:
                self._states.clear()


# 全局熔断器实例（实时行情专用）
_realtime_circuit_breaker = CircuitBreaker(
    failure_threshold=3,      # 连续失败3次熔断
    cooldown_seconds=300.0,   # 冷却5分钟
    half_open_max_calls=1
)

# 筹码接口熔断器（更保守的策略，因为该接口更不稳定）


def get_realtime_circuit_breaker() -> CircuitBreaker:
    """获取实时行情熔断器"""
    return _realtime_circuit_breaker


