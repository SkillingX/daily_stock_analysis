# -*- coding: utf-8 -*-
"""跨数据源交叉验证层（KISS · 纯逻辑判定，无网络依赖）。

职责：对同一锚点（如 ``pe_ratio``），收集多个数据源的读数，按
「容差 + 口径 + 报告期」三重判定产出置信度；资金流类按「方向 + 量级」判定
（东财/同花顺主力净流入算法口径不同，不能盲目数值比对）。

设计要点（高内聚低耦合）：
- 本模块**只做判定**，不联网。所有 IO（取数）由调用方注入的 ``SourceAdapter`` 负责，
  通过 Protocol 解耦 —— 加第三源只需实现 ``read``，不改本模块。
- 判定逻辑全部是纯函数（``_judge_*``），无副作用，易做 100% 单测。
- ``CrossSourceValidator.verify`` 编排「收集 → 判定」，收集时并行 + 异常隔离（fail-open）。

置信度语义：
- ``high``   双源取到且通过判定（数值容差内 / 方向+量级一致）
- ``medium`` 仅单源，或口径/报告期不一致而未做数值比对，或方向同但量级差异大
- ``low``    数值超容差（冲突），或资金流方向相反（真异常）

对应方案 ``rippling-percolating-fairy.md`` 第三节锚点分级与第四节验证流程。
"""

from __future__ import annotations

import logging
import math
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol, Sequence, Tuple

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# 验证模式
# ------------------------------------------------------------------
MODE_NUMERIC = "numeric"  # 数值容差比对（PE/PB/市值/营收/净利/ROE/当前价/融资余额）
MODE_DIRECTION = "direction"  # 方向+量级比对（主力净流入；两源算法口径不同）


@dataclass(frozen=True)
class AnchorSpec:
    """单个锚点的验证规格。"""

    field: str  # 标准字段名，如 "pe_ratio"
    mode: str  # numeric | direction
    tolerance_pct: float  # numeric 模式的相对容差（百分比，10.0 = ±10%）
    caliber_aware: bool = (
        True  # 是否要求口径一致才做数值比对（财务/估值 True；行情/资金 False）
    )


# 锚点规格表（对齐方案第三节）。key = 标准字段名。
ANCHOR_SPECS: Dict[str, AnchorSpec] = {
    "current_price": AnchorSpec(
        "current_price", MODE_NUMERIC, 1.0, caliber_aware=False
    ),
    "pe_ratio": AnchorSpec("pe_ratio", MODE_NUMERIC, 10.0),
    "pb_ratio": AnchorSpec("pb_ratio", MODE_NUMERIC, 10.0),
    "total_mv": AnchorSpec("total_mv", MODE_NUMERIC, 5.0),
    "circ_mv": AnchorSpec("circ_mv", MODE_NUMERIC, 5.0),
    "revenue": AnchorSpec("revenue", MODE_NUMERIC, 3.0),
    "net_profit": AnchorSpec("net_profit", MODE_NUMERIC, 3.0),
    "roe": AnchorSpec("roe", MODE_NUMERIC, 3.0),
    # 毛利率 / 营收同比：派生指标，两源口径（毛利/销售毛利率；同比基准期）非标准化，
    # caliber_aware=False 避免口径判定误伤，容差放宽（毛利率 15%、增速 20%）
    "gross_margin": AnchorSpec("gross_margin", MODE_NUMERIC, 15.0, caliber_aware=False),
    "revenue_yoy": AnchorSpec("revenue_yoy", MODE_NUMERIC, 20.0, caliber_aware=False),
    # 净利润同比：派生指标，波动较大，容差放宽至 25%
    "net_profit_yoy": AnchorSpec(
        "net_profit_yoy", MODE_NUMERIC, 25.0, caliber_aware=False
    ),
    # 融资余额：交易所每日确定数据，应严格一致
    "margin_balance": AnchorSpec(
        "margin_balance", MODE_NUMERIC, 0.5, caliber_aware=False
    ),
    # 主力净流入：东财/同花顺算法口径不同，比对方向非数值
    "main_inflow": AnchorSpec("main_inflow", MODE_DIRECTION, 0.0, caliber_aware=False),
}


# ------------------------------------------------------------------
# 数据结构（不可变）
# ------------------------------------------------------------------
@dataclass(frozen=True)
class AnchorReading:
    """单个数据源对某锚点的一次读数。"""

    source: str  # "mx" | "ifind" | "akshare" | ...
    value: float
    caliber: Optional[str] = None  # 口径，如 "TTM"/"static"；None=未知/不适用
    period: Optional[str] = None  # 报告期，如 "2024年报"；None=不适用（行情/资金）


@dataclass(frozen=True)
class AnchorVerification:
    """某锚点的跨源验证结果（不可变，进 LLM 上下文前再 compact）。"""

    field: str
    value: Optional[float]  # 采纳值（主源值）；缺失/未知锚点为 None（不编造 0）
    confidence: str  # high | medium | low
    sources: Tuple[str, ...]
    agreed: bool  # 是否通过判定（容差内 / 方向一致）
    discrepancy_pct: Optional[float] = None
    caliber: Optional[str] = None
    period: Optional[str] = None
    note: str = ""

    def to_compact(self) -> Dict[str, Any]:
        """压缩为 LLM 友好的 dict（省 token）。"""
        payload: Dict[str, Any] = {
            "v": _round(self.value),
            "conf": self.confidence,
            "src": list(self.sources),
        }
        if self.discrepancy_pct is not None:
            payload["diff"] = round(self.discrepancy_pct, 2)
        if self.caliber:
            payload["caliber"] = self.caliber
        if self.period:
            payload["period"] = self.period
        if self.note:
            payload["note"] = self.note
        return payload


# ------------------------------------------------------------------
# SourceAdapter Protocol（N 源可插拔）
# ------------------------------------------------------------------
class SourceAdapter(Protocol):
    """数据源适配器协议。实现 ``read`` 即可被 validator 调用。"""

    name: str

    def read(
        self, code: str, field: str, period: Optional[str] = None
    ) -> Optional[AnchorReading]:
        """读取某股票某锚点。失败返回 None（fail-open），不抛异常。"""
        ...


# ------------------------------------------------------------------
# 判定纯函数
# ------------------------------------------------------------------

# 股票代码白名单（code 经自然语言查询送外部 API，拒绝非法格式防注入 + 防配额浪费）。
# A 股 6 位数字、港股 5 位数字、美股 1-6 位字母、可带 SH/SZ/HK 前缀或 .SH 后缀。
_CODE_RE = re.compile(
    r"^(?:SH|SZ|BJ|sh|sz|bj)?\d{5,6}$|^(?:HK|hk)?\d{5}$|^[A-Za-z]{1,6}$|^\d{5,6}\.(SH|SZ|BJ|HK)$"
)


def _is_valid_code(code: str) -> bool:
    """校验股票代码格式。非法/空 → False（验证层直接 fail-open missing）。"""
    if not isinstance(code, str) or not code.strip():
        return False
    # 去掉常见前缀后再校验纯代码部分
    return bool(_CODE_RE.match(code.strip()))


def _round(value: Optional[float]) -> Optional[float]:
    """压缩精度，避免 LLM 上下文里长小数。None 透传（缺失锚点）。"""
    if value is None:
        return None
    return round(float(value), 4)


def _discrepancy_pct(a: float, b: float) -> float:
    """相对差异百分比（以较大绝对值为基准）。"""
    base = max(abs(a), abs(b))
    if base == 0:
        return 0.0
    return abs(a - b) / base * 100.0


def _within_tolerance(a: float, b: float, tol_pct: float) -> bool:
    """相对容差判定。"""
    return _discrepancy_pct(a, b) <= tol_pct


def _magnitude_tier(value: float) -> int:
    """金额量级档位 = floor(log10(|value|))。亿=8、千万=7、万=4。

    相邻档（差 ≤1）视为同档，用于主力净流入「量级同档」判定。
    """
    if value == 0:
        return 0
    return int(math.floor(math.log10(abs(value))))


def _judge_numeric(
    primary: AnchorReading,
    secondary: AnchorReading,
    spec: AnchorSpec,
    tertiary: Optional[AnchorReading] = None,
) -> AnchorVerification:
    """数值模式判定：口径 → 报告期 → 容差。

    三源 majority vote（``tertiary`` 提供时）：
      - (primary, secondary) 一致 → high（tertiary 仅记录一致性，不提升 confidence）
      - (primary, secondary) 不一致 + tertiary 与任一方一致 → medium（少数派标记为 outlier）
      - 三源两两都不一致 → low（3-way 真冲突）
    """
    diff = _discrepancy_pct(primary.value, secondary.value)

    # 口径检查（仅 caliber_aware 且两源都带口径时启用）
    if (
        spec.caliber_aware
        and primary.caliber
        and secondary.caliber
        and primary.caliber != secondary.caliber
    ):
        return AnchorVerification(
            field=spec.field,
            value=primary.value,
            confidence="medium",
            sources=(primary.source, secondary.source),
            agreed=False,
            discrepancy_pct=diff,
            caliber=primary.caliber,
            period=primary.period,
            note=f"口径不一致（{primary.source}={primary.caliber}/"
            f"{secondary.source}={secondary.caliber}），未做数值比对",
        )

    # 报告期检查（两源都带期且不同）
    if primary.period and secondary.period and primary.period != secondary.period:
        return AnchorVerification(
            field=spec.field,
            value=primary.value,
            confidence="medium",
            sources=(primary.source, secondary.source),
            agreed=False,
            discrepancy_pct=diff,
            caliber=primary.caliber,
            period=primary.period,
            note=f"报告期不一致（{primary.period}/{secondary.period}），未做数值比对",
        )

    # 容差比对（primary vs secondary）
    agreed_ps = _within_tolerance(primary.value, secondary.value, spec.tolerance_pct)
    if agreed_ps:
        return AnchorVerification(
            field=spec.field,
            value=primary.value,
            confidence="high",
            sources=(
                (primary.source, secondary.source, tertiary.source)
                if tertiary is not None
                else (primary.source, secondary.source)
            ),
            agreed=True,
            discrepancy_pct=diff,
            caliber=primary.caliber,
            period=primary.period,
            note="" if tertiary is None else f"3源一致（含{tertiary.source}）",
        )

    # primary vs secondary 不一致：tertiary 提供时做 3-way majority
    if tertiary is not None:
        # tertiary 必带口径/期一致（来源同一查询）；不再做 caliber/period 二次判定
        pt_ok = _within_tolerance(
            primary.value, tertiary.value, spec.tolerance_pct
        )
        st_ok = _within_tolerance(
            secondary.value, tertiary.value, spec.tolerance_pct
        )
        # majority vote：(primary, tertiary) 赢 / (secondary, tertiary) 赢 / 全不一致
        if pt_ok and not st_ok:
            outlier = secondary
            agreed_value = primary.value
        elif st_ok and not pt_ok:
            outlier = primary
            agreed_value = primary.value  # primary 仍是 primary，但 verdict 反映 tertiary 校正
        else:
            # 两两都不一致 → 3-way 真冲突
            return AnchorVerification(
                field=spec.field,
                value=primary.value,
                confidence="low",
                sources=(primary.source, secondary.source, tertiary.source),
                agreed=False,
                discrepancy_pct=diff,
                caliber=primary.caliber,
                period=primary.period,
                note=(
                    f"3源不一致：{primary.source}={_round(primary.value)}/"
                    f"{secondary.source}={_round(secondary.value)}/"
                    f"{tertiary.source}={_round(tertiary.value)}，"
                    f"差异{diff:.1f}%"
                ),
            )
        return AnchorVerification(
            field=spec.field,
            value=agreed_value,
            confidence="medium",
            sources=(primary.source, secondary.source, tertiary.source),
            agreed=False,
            discrepancy_pct=diff,
            caliber=primary.caliber,
            period=primary.period,
            note=(
                f"{primary.source}+{tertiary.source} 一致，"
                f"{outlier.source}={_round(outlier.value)} 为 outlier"
            ),
        )

    # 2-source 不一致 → 旧行为不变
    return AnchorVerification(
        field=spec.field,
        value=primary.value,
        confidence="low",
        sources=(primary.source, secondary.source),
        agreed=False,
        discrepancy_pct=diff,
        caliber=primary.caliber,
        period=primary.period,
        note=f"数据冲突：{primary.source}={_round(primary.value)}/"
        f"{secondary.source}={_round(secondary.value)}，差异{diff:.1f}%",
    )


def _judge_direction(
    primary: AnchorReading,
    secondary: AnchorReading,
    field: str,
    tertiary: Optional[AnchorReading] = None,
) -> AnchorVerification:
    """方向+量级判定（主力净流入；两源算法口径不同）。

    3-source 时（``tertiary`` 提供）：majority vote —— 同方向 ≥2 → high；否则 medium。
    """
    diff = _discrepancy_pct(primary.value, secondary.value)
    sources_pair = (
        (primary.source, secondary.source, tertiary.source)
        if tertiary is not None
        else (primary.source, secondary.source)
    )
    # 零值视为「无数据/收盘」，方向不可靠 → medium（避免双零误判 high）
    if primary.value == 0 or secondary.value == 0:
        return AnchorVerification(
            field=field,
            value=primary.value,
            confidence="medium",
            sources=sources_pair,
            agreed=False,
            discrepancy_pct=diff,
            caliber="方向比对",
            note="含零值（可能收盘/数据缺失），方向不可靠",
        )
    # 显式三元（避免 bool 当索引：True==1/False==0 与方向词顺序错位）
    primary_word = "净流入" if primary.value > 0 else "净流出"
    secondary_word = "净流入" if secondary.value > 0 else "净流出"
    d_primary = primary.value > 0
    d_secondary = secondary.value > 0

    # 方向相反 = 真冲突
    if d_primary != d_secondary:
        # 3-source 时：tertiary 偏向任一方 → medium；否则 → low（保持旧行为）
        if tertiary is not None:
            tertiary_word = "净流入" if tertiary.value > 0 else "净流出"
            d_tertiary = tertiary.value > 0
            if d_tertiary == d_primary:
                return AnchorVerification(
                    field=field,
                    value=primary.value,
                    confidence="medium",
                    sources=sources_pair,
                    agreed=False,
                    discrepancy_pct=diff,
                    caliber="方向比对",
                    note=(
                        f"方向冲突：{primary.source}+{tertiary.source}={primary_word}/"
                        f"{secondary.source}={secondary_word}，secondary 为 outlier"
                    ),
                )
            if d_tertiary == d_secondary:
                return AnchorVerification(
                    field=field,
                    value=primary.value,
                    confidence="medium",
                    sources=sources_pair,
                    agreed=False,
                    discrepancy_pct=diff,
                    caliber="方向比对",
                    note=(
                        f"方向冲突：{primary.source}={primary_word}/"
                        f"{secondary.source}+{tertiary.source}={secondary_word}，primary 为 outlier"
                    ),
                )
        return AnchorVerification(
            field=field,
            value=primary.value,
            confidence="low",
            sources=sources_pair,
            agreed=False,
            discrepancy_pct=diff,
            caliber="方向比对",
            note=f"方向冲突：{primary.source}={primary_word}/"
            f"{secondary.source}={secondary_word}，需核对",
        )

    # 方向一致，查量级同档
    same_word = primary_word
    tier_diff = abs(_magnitude_tier(primary.value) - _magnitude_tier(secondary.value))
    if tier_diff <= 1:
        return AnchorVerification(
            field=field,
            value=primary.value,
            confidence="high",
            sources=(primary.source, secondary.source),
            agreed=True,
            discrepancy_pct=diff,
            caliber="方向比对",
            note=f"方向一致（{same_word}）+量级同档",
        )
    return AnchorVerification(
        field=field,
        value=primary.value,
        confidence="medium",
        sources=(primary.source, secondary.source),
        agreed=True,
        discrepancy_pct=diff,
        caliber="方向比对",
        note=f"方向一致（{same_word}）但量级差异大，两源算法口径不同",
    )


def _judge_single(reading: AnchorReading, field: str) -> AnchorVerification:
    """仅单源：medium，诚实标注未交叉验证。"""
    return AnchorVerification(
        field=field,
        value=reading.value,
        confidence="medium",
        sources=(reading.source,),
        agreed=False,
        discrepancy_pct=None,
        caliber=reading.caliber,
        period=reading.period,
        note=f"单源（{reading.source}），未交叉验证",
    )


def _judge_missing(field: str) -> AnchorVerification:
    """无任何源取到：low。value=None（诚实标注，不编造 0）。"""
    return AnchorVerification(
        field=field,
        value=None,
        confidence="low",
        sources=(),
        agreed=False,
        note="所有数据源均无该锚点数据",
    )


def _judge_unknown(field: str) -> AnchorVerification:
    """未知锚点规格：low，避免静默放过。value=None。"""
    return AnchorVerification(
        field=field,
        value=None,
        confidence="low",
        sources=(),
        agreed=False,
        note="未知锚点规格，未配置验证",
    )


# ------------------------------------------------------------------
# CrossSourceValidator
# ------------------------------------------------------------------
class CrossSourceValidator:
    """跨源验证器（N 源可插拔）。

    用法::

        validator = CrossSourceValidator(sources=[mx_source, ifind_source])
        result = validator.verify("600519", "pe_ratio", period="2024年报")
        # result.confidence in {"high","medium","low"}
    """

    def __init__(
        self,
        sources: Sequence[SourceAdapter],
        specs: Optional[Dict[str, AnchorSpec]] = None,
        max_workers: int = 4,
    ) -> None:
        self._sources: Tuple[SourceAdapter, ...] = tuple(sources)
        self._specs: Dict[str, AnchorSpec] = (
            dict(specs) if specs else dict(ANCHOR_SPECS)
        )
        self._max_workers = max(1, int(max_workers))

    def verify(
        self,
        code: str,
        field: str,
        period: Optional[str] = None,
        primary_reading: Optional[AnchorReading] = None,
    ) -> AnchorVerification:
        """验证单个锚点。永不抛异常（fail-open）。

        ``primary_reading``：可选，注入的主源读数（如行情类的 realtime_quote），
        置于读数列表首位作 primary；其余从 ``sources`` 收集作验证源。
        """
        spec = self._specs.get(field)
        if spec is None:
            return _judge_unknown(field)

        # 输入校验：code 经自然语言查询送外部 API，拒绝非法格式（防注入 + 防配额浪费）
        if not _is_valid_code(code):
            return _judge_missing(field)

        collected = self._collect(code, field, period)
        readings = (
            (primary_reading, *collected) if primary_reading is not None else collected
        )

        if not readings:
            return _judge_missing(field)
        if len(readings) == 1:
            return _judge_single(readings[0], spec.field)

        # 双源及以上：primary = readings[0]，secondary = readings[1]；
        # tertiary = readings[2]（当存在时进入 3-way majority vote，否则 2-source 行为不变）。
        primary, secondary = readings[0], readings[1]
        tertiary = readings[2] if len(readings) >= 3 else None
        if spec.mode == MODE_DIRECTION:
            return _judge_direction(primary, secondary, spec.field, tertiary=tertiary)
        return _judge_numeric(primary, secondary, spec, tertiary=tertiary)

    def _collect(
        self, code: str, field: str, period: Optional[str]
    ) -> Tuple[AnchorReading, ...]:
        """并行收集各源读数。任一源异常/返回 None 都被隔离（fail-open）。"""
        if not self._sources:
            return ()

        def _safe_read(source: SourceAdapter) -> Optional[AnchorReading]:
            try:
                return source.read(code, field, period)
            except Exception as exc:  # noqa: BLE001 — fail-open：源异常不影响其他源
                logger.debug(
                    "[CrossValidate] source %s read %s failed: %s",
                    source.name,
                    field,
                    exc,
                )
                return None

        if len(self._sources) == 1:
            reading = _safe_read(self._sources[0])
            return (reading,) if reading else ()

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            results = list(pool.map(_safe_read, self._sources))
        return tuple(r for r in results if r is not None)
