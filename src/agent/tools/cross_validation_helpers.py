# -*- coding: utf-8 -*-
"""深度投研关键锚点交叉验证注入辅助（KISS · opt-in · 零回归）。

受 ``DEEP_RESEARCH_CROSS_VALIDATE`` 开关控制：关闭时
:func:`build_cross_validation_block` 返回 None，data_tools 工具返回与改动前
完全一致（无 ``cross_validation`` 字段）—— 保证不影响现有功能。

职责（高内聚）：
- 构建/缓存 :class:`CrossSourceValidator`（MX 主源 + iFinD/Choice MCP/fuyao 验证源）。
- 对一组锚点逐个验证，压缩为 LLM 友好的 ``cross_validation`` 块。

data_tools 各 handler 只需一行调用，不感知验证细节（低耦合）。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from time import monotonic
from threading import Lock
from typing import Any, Dict, Iterable, List, Optional

from data_provider.cross_source_validator import (
    AnchorReading,
    AnchorQuality,
    adopted_field_record,
    reading_from_field_record,
    CrossSourceValidator,
    SourceAdapter,
)

logger = logging.getLogger(__name__)


def field_record_from_validation(field: str, anchor: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Use the actual selected reading, never reconstruct provenance from source counts."""
    readings = anchor.get("readings") or []
    reading = reading_from_field_record(anchor.get("v"), readings[0] if readings else {})
    if reading is None:
        return None
    main_reasons = (readings[0].get("reason_codes") or []) if readings else []
    if "stale" in main_reasons:
        reading = replace(reading, is_stale=True)
    elif "time_in_future" in main_reasons:
        reading = replace(reading, normalization_error="time_in_future")
    quality_data = anchor.get("quality") or {}
    quality = AnchorQuality(status=quality_data.get("status", "unverified"), reason_codes=tuple(quality_data.get("reason_codes") or ()))
    return adopted_field_record(field, reading, quality, selection_reason="validation_candidate")

_validator_instance: Optional[CrossSourceValidator] = None
_validator_lock = Lock()


def reset_validator() -> None:
    """重置缓存（config reload / 测试用）。"""
    global _validator_instance
    with _validator_lock:
        _validator_instance = None


def _build_sources(config: Any, primary_mx_source: Optional[SourceAdapter] = None) -> List[SourceAdapter]:
    """构建数据源列表：MX 主源 + iFinD/Choice MCP/fuyao 验证源（按需启用）。"""
    from data_provider.mx_data_adapter import MXSource

    sources: List[SourceAdapter] = [primary_mx_source if primary_mx_source is not None else MXSource()]
    # Reuse the acquired MX response, including this refresh's scoped cache identity.
    if getattr(config, "ifind_mcp_endpoint", None) and getattr(
        config, "ifind_mcp_token", None
    ):
        from data_provider.ifind_fundamental_adapter import IfindFetcher, IfindSource

        fetcher = IfindFetcher(
            endpoint=config.ifind_mcp_endpoint,
            token=config.ifind_mcp_token,
            timeout_seconds=float(getattr(config, "ifind_mcp_timeout_seconds", 8.0)),
        )
        sources.append(IfindSource(fetcher=fetcher))
    # 第三验证源：东财 Choice MCP（妙想 MCP），opt-in，默认关 → 零回归
    if getattr(config, "enable_mx_mcp", False) and getattr(
        config, "mx_mcp_api_key", None
    ):
        from data_provider.mx_mcp_adapter import MxMcpFetcher, MxMcpSource

        fetcher = MxMcpFetcher(
            endpoint=getattr(config, "mx_mcp_endpoint", None),
            api_key=getattr(config, "mx_mcp_api_key", None),
            timeout_seconds=float(getattr(config, "mx_mcp_timeout_seconds", 30.0)),
        )
        sources.append(MxMcpSource(fetcher=fetcher))
    # 第四验证源：同花顺 fuyao (aicubes REST)，opt-in，默认关 → 零回归
    if getattr(config, "enable_fuyao", False) and getattr(
        config, "fuyao_api_key", None
    ):
        from data_provider.fuyao_adapter import FuyaoFetcher, FuyaoSource

        fetcher = FuyaoFetcher(
            endpoint=getattr(config, "fuyao_endpoint", None),
            api_key=getattr(config, "fuyao_api_key", None),
            timeout_seconds=float(getattr(config, "fuyao_timeout_seconds", 30.0)),
        )
        sources.append(FuyaoSource(fetcher=fetcher))
    return sources


def _get_validator() -> Optional[CrossSourceValidator]:
    """懒加载 validator（进程级单例）。开关关 / 无源 → None。"""
    global _validator_instance
    from src.config import get_config

    config = get_config()
    if not getattr(config, "deep_research_cross_validate", False):
        return None
    if _validator_instance is not None:
        return _validator_instance
    with _validator_lock:
        if _validator_instance is None:
            from src.agent.tools.data_tools import _get_fetcher_manager
            sources = _build_sources(config, primary_mx_source=_get_fetcher_manager()._mx_source)
            if not sources:
                return None
            _validator_instance = CrossSourceValidator(sources=sources)
    return _validator_instance


def build_cross_validation_block(
    code: str,
    fields: Iterable[str],
    period: Optional[str] = None,
    primary_readings: Optional[Dict[str, AnchorReading]] = None,
    validator: Optional[CrossSourceValidator] = None,
    deadline: Optional[float] = None,
    expected_currencies: Optional[Dict[str, str]] = None,
) -> Optional[Dict[str, Any]]:
    """构建 ``cross_validation`` 块。

    - 开关关 / 无 validator / 全锚点失败 → None（零回归，不注入字段）。
    - ``primary_readings``：注入主源读数（如行情类的 realtime_quote），该源作 primary。
    - ``validator``：测试注入；默认从 config 开关懒加载。
    - 任一锚点验证异常被隔离（fail-open），不影响其余锚点。
    """
    validator = validator if validator is not None else _get_validator()
    if validator is None:
        return None
    if deadline is None and isinstance(validator, CrossSourceValidator):
        from src.config import get_config
        deadline = monotonic() + get_config().fundamental_stage_timeout_seconds
    primary_readings = primary_readings or {}
    anchors: Dict[str, Any] = {}
    agreed = 0
    total = 0
    for field in fields:
        total += 1
        try:
            verification = validator.verify(
                code,
                field,
                period=period,
                primary_reading=primary_readings.get(field),
                **({"deadline": deadline} if deadline is not None else {}),
                **({"expected_currency": expected_currencies[field]} if expected_currencies and field in expected_currencies else {}),
            )
        except Exception as exc:  # noqa: BLE001 — fail-open：单锚点失败不阻塞其余
            logger.debug("[CrossValidate] verify %s/%s failed: %s", code, field, exc)
            continue
        anchors[field] = verification.to_compact()
        if verification.confidence == "high":
            agreed += 1
    if not anchors:
        return None
    return {
        "enabled": True,
        "anchors": anchors,
        "summary": f"{agreed}/{total} 锚点取得独立来源佐证",
    }
