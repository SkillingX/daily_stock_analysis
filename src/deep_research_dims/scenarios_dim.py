# -*- coding: utf-8 -*-
"""L5 情景：概率和=100% 机器校验 + EV 强制计算 + 估值口径自动切换。叙述型 LLM。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import logging

from icontract import require

from src.deep_research_dims.context import SharedContext
from src.schemas.deep_research_dims import ScenariosDim
logger = logging.getLogger(__name__)

from src.schemas.value_scenarios import Scenario, ValueHorizons, ValueScenarios


def _valuation_basis(pe_ttm: Optional[float]) -> str:
    """亏损股（PE≤0/缺失）自动切换 PB/PS 口径。"""
    if pe_ttm is None or pe_ttm <= 0:
        return "PB"
    return "PE_TTM"


@require(lambda scenarios: len(scenarios) > 0, "至少一个情景")
def normalize_probabilities(scenarios: List[Scenario]) -> List[Scenario]:
    """概率和机器归一化到严格 100%（护栏规则 4 的底层实现）。"""
    total = sum(s.probability for s in scenarios)
    if total <= 0:
        return [s.model_copy(update={"probability": round(1.0 / len(scenarios), 4)}) for s in scenarios]
    normalized = [
        s.model_copy(update={"probability": round(s.probability / total, 4)})
        for s in scenarios
    ]
    # 四舍五入残差补到概率最大的情景上，保证和严格=1
    residual = round(1.0 - sum(s.probability for s in normalized), 4)
    if residual and normalized:
        top = max(normalized, key=lambda s: s.probability)
        idx = normalized.index(top)
        normalized[idx] = top.model_copy(
            update={"probability": round(top.probability + residual, 4)}
        )
    return normalized


def expected_value(scenarios: List[Scenario]) -> Optional[float]:
    """期望目标价 EV=Σ(概率×价值锚)。无价值锚情景不参与。"""
    anchors = [s for s in scenarios if s.value_anchor]
    if not anchors:
        return None
    return round(sum(s.probability * float(s.value_anchor) for s in anchors), 2)  # type: ignore[arg-type]


def _fetch_time_paths(
    llm_adapter: Any, stock_name: str, scenarios: List[Scenario]
) -> Dict[str, str]:
    """段三时间层级：1天/1周/1月/1季各一句「条件→目标位」（LLM 生成，机器不背书数字）。

    目标位必须挂条件（方案 C6）；失败/缺 LLM → 空 dict（报告该节显示"未生成"）。
    """
    if llm_adapter is None or not scenarios:
        return {}
    import json
    import re

    scen_desc = [
        {"type": s.type, "probability": s.probability, "anchor": s.value_anchor}
        for s in scenarios
    ]
    system = (
        "你是投研走势分析师。输入三情景（类型/概率/价值锚），为 1天/1周/1月/1季 各写一句"
        "中文路径判断，每句必须包含触发条件和目标位（如『放量站上X则看Y』），"
        "覆盖最大可能情景为主，另两句简述其他情景路径。只输出 JSON："
        '{"1d": "...", "1w": "...", "1m": "...", "1q": "..."}'
    )
    try:
        resp = llm_adapter.call_text(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(
                    {"stock": stock_name, "scenarios": scen_desc}, ensure_ascii=False
                )},
            ],
            temperature=0.4,
            timeout=90.0,
        )
        import re as _re

        text = (resp.content or "").strip()
        text = _re.sub(
            r"<think(?:ing)?>.*?</think(?:ing)?>", "", text, flags=_re.DOTALL | _re.IGNORECASE
        ).strip()
        text = _re.sub(r"```(?:json)?", "", text).strip()
        if not text.startswith("{"):
            match = _re.search(r"\{.*\}", text, flags=_re.DOTALL)
            text = match.group(0) if match else ""
        if not text:
            return {}
        parsed = json.loads(text)
        return {k: str(v).strip() for k, v in parsed.items() if str(v).strip() and k in ("1d", "1w", "1m", "1q")}
    except Exception as exc:  # noqa: BLE001 - 时间层级失败只降级为空
        logger.warning("[Scenarios] 时间层级生成失败: %s", exc)
        return {}


def build_scenarios_dim(
    ctx: SharedContext,
    current_price: Optional[float],
    llm_scenarios: Optional[Dict[str, Any]] = None,
    llm_adapter: Any = None,
) -> ScenariosDim:
    """L5：LLM 提供情景叙述（可选），概率/EV/口径全部机器计算；时间层级挂条件句。"""
    fund = ctx.fundamental
    pe_ttm = fund.get("pe_ttm")
    basis = _valuation_basis(pe_ttm if isinstance(pe_ttm, (int, float)) else None)

    raw_scenarios: List[Scenario] = []
    raw = (llm_scenarios or {}).get("scenarios") or []
    for item in raw:
        try:
            raw_scenarios.append(
                Scenario(
                    type=item.get("type") or "neutral",
                    probability=float(item.get("probability") or 0),
                    value_anchor=item.get("value_anchor"),
                    description=item.get("description"),
                )
            )
        except (ValueError, TypeError):
            continue

    if not raw_scenarios:
        # 无 LLM 情景时的规则默认：围绕现价对称三档
        base = current_price or ctx.quote.get("price")
        if base:
            raw_scenarios = [
                Scenario(type="optimistic", probability=0.25, value_anchor=round(base * 1.3, 2)),
                Scenario(type="neutral", probability=0.5, value_anchor=round(base, 2)),
                Scenario(
                    type="pessimistic",
                    probability=0.25,
                    value_anchor=round(base * 0.75, 2),
                ),
            ]
    normalized = normalize_probabilities(raw_scenarios)
    ev = expected_value(normalized)

    scenarios = ValueScenarios(
        industry_space=(llm_scenarios or {}).get("industry_space"),
        competitive_evolution=(llm_scenarios or {}).get("competitive_evolution"),
        scenarios=normalized,
        horizons=ValueHorizons(
            horizon_1y=(llm_scenarios or {}).get("horizon_1y"),
            horizon_3y=(llm_scenarios or {}).get("horizon_3y"),
            horizon_5y=(llm_scenarios or {}).get("horizon_5y"),
        ),
        catalysts=list((llm_scenarios or {}).get("catalysts") or []),
        risks=list((llm_scenarios or {}).get("risks") or []),
    )
    prob_sum = round(sum(s.probability for s in normalized), 4)
    time_paths = _fetch_time_paths(llm_adapter, ctx.stock_name, normalized)
    return ScenariosDim(
        scenarios=scenarios,
        probability_sum=prob_sum,
        expected_value=ev,
        valuation_basis=basis,  # type: ignore[arg-type]
        current_pe_ttm=pe_ttm if isinstance(pe_ttm, (int, float)) else None,
        time_paths=time_paths,
        narrative=(
            f"三情景概率和 {prob_sum * 100:.0f}%（机器归一化），期望价值 {ev}，"
            f"估值口径 {basis}（当前 PE(TTM)={pe_ttm}）。"
        ),
    )
