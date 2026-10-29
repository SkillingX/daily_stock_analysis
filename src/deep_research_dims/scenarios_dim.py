# -*- coding: utf-8 -*-
"""L5 情景：概率和=100% 机器校验 + EV 强制计算 + 估值口径自动切换。叙述型 LLM。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
import json
from datetime import date
from pydantic import ValidationError

from icontract import require

from src.deep_research_dims.context import SharedContext
from src.deep_research_dims.fundamental_dim import eligible_financial_value
from src.schemas.deep_research_dims import ScenariosDim

from src.schemas.value_scenarios import AnalystTarget, Scenario, ValueHorizons, ValueScenarios

VALUATION_CASCADE = "主缺口及受影响章节：缺少可追溯目标价，情景、结论与信号因此受限"

# Choice 目标价统计窗口契约（与 AnalystTarget.window_days 同一口径）
TARGET_WINDOW_DAYS = 180

# 合法非估值分支的醒目范围说明（issue #64：2.2 用户已确认范围）
NON_VALUATION_SCOPE = (
    "未取得可追溯目标价，本报告采用非估值分析：仅基于已完成研究给出定性情景与"
    "观察结论，不提供目标价、预期收益空间或估值驱动买卖评级。"
)


def with_valuation_cascade(reason: str) -> str:
    return f"{reason}。{VALUATION_CASCADE}"


def has_substantive_qualitative_content(llm_scenarios: Optional[Dict[str, Any]]) -> bool:
    """非估值分支内容判定（B5 统一谓词）：实际非空的定性文本/条件/风险。

    只认内容本身，不数槽位、不设长度门槛：叙述文本（industry_space/
    competitive_evolution）或条件/风险条目（catalysts/risks）任一实际非空即算
    有实质内容——一份完成研究的一条实质风险合法，同一 risks 列表两条亦合法；
    空壳（无任何实际内容、空白字符串或仅有状态字段）不构成充分，维持真实降级。
    内容是否来自本报告已完成研究由装配层（_qualitative_evidence_pack）的完成
    门控保证；装配层与结论层共用本谓词，不建内容计数框架。
    """
    pack = llm_scenarios or {}
    for key in ("industry_space", "competitive_evolution"):
        if str(pack.get(key) or "").strip():
            return True
    for key in ("catalysts", "risks"):
        value = pack.get(key)
        if isinstance(value, list) and any(str(item or "").strip() for item in value):
            return True
    return False


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


def build_scenarios_dim(
    ctx: SharedContext,
    current_price: Optional[float],
    llm_scenarios: Optional[Dict[str, Any]] = None,
    llm_adapter: Any = None,
) -> ScenariosDim:
    """L5：LLM 提供情景叙述（可选），概率/EV/口径全部机器计算；时间层级挂条件句。"""
    fund = ctx.fundamental
    pe_ttm = eligible_financial_value(fund, "pe_ratio")

    raw_scenarios: List[Scenario] = []
    raw = (llm_scenarios or {}).get("scenarios") or []
    for item in raw:
        try:
            raw_scenarios.append(
                Scenario(
                    type=item.get("type") or "neutral",
                    probability=float(item.get("probability") or 0),
                    value_anchor=None,  # Model targets have no source/calculation evidence in this contract.
                    description=item.get("description"),
                )
            )
        except (ValueError, TypeError):
            continue

    normalized = normalize_probabilities(raw_scenarios) if raw_scenarios else []

    scenarios = ValueScenarios(
        industry_space=(llm_scenarios or {}).get("industry_space"),
        competitive_evolution=(llm_scenarios or {}).get("competitive_evolution"),
        scenarios=normalized,
        horizons=ValueHorizons(
            horizon_1y=None,
            horizon_3y=None,
            horizon_5y=None,
        ),
        catalysts=list((llm_scenarios or {}).get("catalysts") or []),
        risks=list((llm_scenarios or {}).get("risks") or []),
    )
    prob_sum = round(sum(s.probability for s in normalized), 4)
    research = fund.get("research") or {}
    targets = research.get("targets") or {}
    reference = None
    reason = "缺少可追溯的目标价与估值计算依据，仅保留定性情景"
    qualitative_no_coverage = False
    if research.get("source") == "choice":
        reason = "Choice 在截至日期的近180日窗口未提供机构目标价；不以现价倍数或模型猜测补齐"
        # 已知查询错误优先披露真实原因（超时/额度/未发出等）：供应商真实失败
        # 不得被泛化的契约异常文案掩盖，也不得转换为"无覆盖"。注意生产适配器
        # 在查询失败时不写入 targets 键，因此错误分支必须先于契约检查。
        target_errors = [error for error in research.get("errors") or [] if error.startswith("choice:targets:")]
        if target_errors:
            cause = {
                "authentication_failed": "Choice登录验证暂未通过",
                "permission_denied": "当前Choice权限未覆盖此项数据",
                "quota_exhausted": "Choice调用额度受限",
                "rate_limited": "Choice接口临时限流",
                "timeout": "Choice查询超时",
                "query_timeout": "Choice查询超时",
                "login_timeout": "Choice登录超时",
                "lock_wait_timeout": "Choice连接排队等待超时",
                "budget_exhausted": "本次取数预算已用尽，目标价查询未发出",
                "sdk_unavailable": "Choice数据连接尚未就绪",
            }.get(target_errors[0].rsplit(":", 1)[-1], "Choice查询未完成")
            suffix = "" if target_errors[0].rsplit(":", 1)[-1] in {"budget_exhausted", "lock_wait_timeout"} else "恢复数据连接后可重新生成。"
            reason = f"机构目标价尚未取得：{cause}，暂时无法提供估值参考。{suffix}".rstrip("。") + "。"
        else:
            # 契约有效性（issue #64 §2.2）：与有效目标价同一契约口径——快照日期距报告
            # 时点 0/1 天、统计窗口 180 日、targets 结果标识齐全。缺任一不构成"已确认
            # 的无覆盖"，保守按契约异常降级，不得进入合法非估值分支。
            try:
                snapshot_age = (
                    date.fromisoformat(ctx.as_of[:10]) - date.fromisoformat(str(research.get("as_of"))[:10])
                ).days
            except (ValueError, TypeError):
                snapshot_age = None
            contract_valid = (
                snapshot_age is not None
                and snapshot_age in (0, 1)
                and "targets" in research
                and research.get("target_window_days") == TARGET_WINDOW_DAYS
            )
            if not contract_valid:
                reason = "机构目标价响应缺少快照日期/窗口标识、结果标识或快照已过期，按契约异常处理，不能参与估值"
            elif targets.get("RATINGTARGETPRICE") is not None:
                try:
                    reference = AnalystTarget.model_validate_json(json.dumps({"source": research["source"], "as_of": research.get("as_of"), "window_days": research.get("target_window_days"), "price": targets["RATINGTARGETPRICE"], "low": targets.get("RATINGTARGETPRICEMIN"), "high": targets.get("RATINGTARGETPRICEMAX")}))
                    if (date.fromisoformat(ctx.as_of[:10]) - reference.as_of).days not in (0, 1):
                        reference = None
                        reason = "机构目标价快照已过期，需重新获取"
                except (ValidationError, ValueError):
                    reference = None
                    reason = "机构目标价数据契约异常，不能参与估值"
            elif targets.get("RATINGTARGETPRICEMIN") is not None or targets.get("RATINGTARGETPRICEMAX") is not None:
                # 部分/不一致读数（有边界无综合值）不是已确认的无覆盖：保持降级
                # 并如实说明，不以边界均值猜测补齐估值锚（issue #64 §2.2）。
                reason = "机构目标价响应缺少综合值但带非空边界，属部分或不一致读数，按契约异常处理，不能参与估值，也不以均值猜测补齐"
            else:
                # 目标价查询成功、契约有效但窗口无覆盖（issue #64 §2.2）：配合充分
                # 的同报告非估值证据可走合法非估值分支；证据不足仍按缺口降级。
                qualitative_no_coverage = has_substantive_qualitative_content(llm_scenarios)
                if qualitative_no_coverage:
                    reason = (
                        f"{NON_VALUATION_SCOPE}Choice 在截至 {research.get('as_of') or '查询日'} 的近180日"
                        "窗口无可追溯机构目标价；以下为基于已完成研究的定性情景与观察条件。"
                    )
    if reference is None and not qualitative_no_coverage:
        return ScenariosDim(
            status="degraded",
            degraded_reason=with_valuation_cascade(reason),
            scenarios=scenarios,
            probability_sum=prob_sum,
            expected_value=None,
            valuation_basis="none",
            analyst_target=None,
            target_price=None,
            current_pe_ttm=pe_ttm,
            narrative=reason,
        )
    if reference is not None:
        narrative = (
            f"机构目标价综合值 {reference.price:.2f} 元；Choice {reference.indicator}，"
            f"截至 {reference.as_of}，统计窗口180日。仅为机构研报参考，不是独立估值或"
            "三情景概率加权期望值，窗口不代表目标价期限。"
        )
    else:
        catalysts = scenarios.catalysts or []
        risks = scenarios.risks or []
        narrative = (
            f"{reason}"
            + (f" 后续观察/催化：{'；'.join(str(c) for c in catalysts[:3])}。" if catalysts else "")
            + (f" 主要风险/反证：{'；'.join(str(r) for r in risks[:3])}。" if risks else "")
        )
    return ScenariosDim(
        status="ok",
        degraded_reason=None,
        scenarios=scenarios,
        probability_sum=prob_sum,
        expected_value=None,
        valuation_basis="analyst_consensus" if reference is not None else "none",
        analyst_target=reference,
        target_price=float(reference.price) if reference is not None else None,
        current_pe_ttm=pe_ttm,
        narrative=narrative,
    )
