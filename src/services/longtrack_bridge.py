# -*- coding: utf-8 -*-
"""长线桥接器：每日个股分析的长线五段式字段改由双轨引擎产出（A 股收敛单实现）。

背景（方案审计 A5/A6）：每日分析的内嵌五段式（research_framework_integration）
与深度分析双轨引擎的长线五段式是两套平行实现，同一票可能给出矛盾结论。本模块
让 A 股自选股的长线段直调双轨引擎长线子集（不持久化 deep-research 产物、不污染
web 历史列表），失败时回退内嵌路径并标注来源。

关键决策：
- 只处理 A 股（`normalize_a_share` 判定，非 A 股直接走内嵌）；
- 直调编排器（不经 service 层）：批量场景不生成 deep-research 报告文件/DB；
- `skip_narrations=True`：叙述是给深度报告人读的，批量场景纯浪费（方案审计 A2）；
- dim_cache 红利自动生效（次日 supply_chain/six_dim/scenarios 命中，省探索 Agent）；
- 降级/异常回退内嵌路径，`longtrack_source` 标注 dual_track / legacy_fallback。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

# 长线子集（依赖闭包自动补 data；跳过快照类短线维度不为批量场景付费）
LONG_TRACK_SUBSET = frozenset(
    {"supply_chain", "intel", "six_dim", "bayesian", "scenarios", "conclusion"}
)


def _map_dims_to_five_sections(dims_payload: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """双轨维度 payload → AnalysisResult 五段式字段（下游模板读取的键契约见单测）。"""
    mapped: Dict[str, Any] = {}
    conclusion = (dims_payload.get("conclusion") or {}).get("conclusion")
    if conclusion:
        mapped["investment_conclusion"] = conclusion
    sc = (dims_payload.get("supply_chain") or {}).get("supply_chain")
    if sc:
        mapped["supply_chain"] = sc
    scenarios = (dims_payload.get("scenarios") or {}).get("scenarios")
    if scenarios:
        mapped["value_scenarios"] = scenarios
    bayesian = (dims_payload.get("bayesian") or {}).get("bayesian")
    if bayesian:
        mapped["bayesian_framework"] = bayesian
    framework = (dims_payload.get("six_dim") or {}).get("framework")
    if framework:
        mapped["research_framework"] = framework
    return mapped


# 五段式全量映射所需的最小键集（缺一即视为长线失败 → 回退）
_REQUIRED_KEYS: Dict[str, tuple] = {
    "investment_conclusion": ("action", "prior_p"),
    "supply_chain": ("company_position",),
    "value_scenarios": ("scenarios",),
    "bayesian_framework": ("posterior_p", "evidence_log"),
    "research_framework": ("dimension_total", "dimensions"),
}


def _mapping_complete(mapped: Dict[str, Any]) -> bool:
    for field, keys in _REQUIRED_KEYS.items():
        value = mapped.get(field)
        if not isinstance(value, dict):
            return False
        if any(k not in value for k in keys):
            return False
    return True


def _fallback_embedded(result: Any, context: Dict[str, Any]) -> Any:
    """回退：现有内嵌五段式路径（research_framework_integration）。"""
    from src.services.research_framework_integration import (
        integrate_research_framework as rf_integrate,
    )

    try:
        return rf_integrate(result, context, enable_research_framework=True)
    except Exception as exc:  # noqa: BLE001 - 回退路径也不得阻塞每日主流程
        logger.warning("[LongTrackBridge] 内嵌回退路径也失败: %s", exc)
        return result


def integrate_longtrack_dual(result: Any, context: Dict[str, Any]) -> Any:
    """A 股：双轨长线子集 → 五段式字段；非 A 股/失败/降级 → 内嵌回退。

    调用方（pipeline）在开关开启且为 A 股时调用本函数；本函数内部完成回退，
    调用方无需二次分支。
    """
    code = str(getattr(result, "code", "") or "").strip()
    if not code:
        return _fallback_embedded(result, context)
    try:
        from src.services.deep_research_service import (
            _get_dual_track_adapter,
            normalize_a_share,
        )

        a_code = normalize_a_share(code)  # 非 A 股抛 DeepResearchInputError
    except Exception:  # noqa: BLE001 - 非 A 股或归一化失败 → 内嵌
        return _fallback_embedded(result, context)

    stock_name = str(getattr(result, "stock_name", "") or "").strip() or a_code
    try:
        from src.agent.deep_research.orchestrator import run_dual_track

        dt_result = run_dual_track(
            stock_code=a_code,
            stock_name=stock_name,
            llm_adapter=_get_dual_track_adapter(),
            progress_callback=None,  # 批量场景无 SSE 消费者
            dims_filter=set(LONG_TRACK_SUBSET),
            skip_narrations=True,  # 审计 A2：叙述对批量场景纯浪费
            cache_exclude=frozenset({"scenarios"}),  # 审计 B2：空叙述不固化；探索/评分维度照写，次日命中省探索 Agent
        )
    except Exception as exc:  # noqa: BLE001 - 引擎异常不阻塞每日主流程
        logger.warning("[LongTrackBridge] 双轨长线异常，回退内嵌: %s", exc)
        result.longtrack_source = "legacy_fallback"
        return _fallback_embedded(result, context)

    mapped = _map_dims_to_five_sections(dt_result.dims_payload)
    if dt_result.status == "failed" or not _mapping_complete(mapped):
        logger.warning(
            "[LongTrackBridge] 双轨长线不完整（status=%s），回退内嵌",
            dt_result.status,
        )
        result.longtrack_source = "legacy_fallback"
        return _fallback_embedded(result, context)

    for field, value in mapped.items():
        setattr(result, field, value)
    result.longtrack_source = "dual_track"
    # 终读结论供通知/摘要卡消费（需求 3 + 决策 2）
    result.longtrack_conclusion = dt_result.final_conclusion or ""
    logger.info(
        "[LongTrackBridge] %s 长线段已由双轨产出（status=%s quality=%d）",
        a_code,
        dt_result.status,
        dt_result.quality_score,
    )
    return result
