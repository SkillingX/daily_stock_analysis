# -*- coding: utf-8 -*-
"""Issue #64 评分 v2.2：退役机构/治理指标，基本面 31.25/43.75/25，资金流 100%。

规格中的已发布算例是验收 oracle：
- 基本面三项 80/60/40 → 61.25；
- 资金流 80、旧机构 20 → 资金面 80（旧机构分不参与）；
- 旧治理 99 不影响结果；其余四维均 50 → 六维总分 57.88。
全部离线（LLM adapter 传 None，走纪律缺口）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest


def _indicator(dim_results: Dict[str, Optional[Dict[str, Any]]], ind_id: str, score: float) -> Dict[str, Any]:
    return {
        "id": ind_id,
        "score": score,
        "confidence": "medium",
        "basis": "rule",
        "data_gap": False,
        "summary": f"{ind_id}={score}（测试夹具）",
    }


def test_scoring_version_and_indicator_tables() -> None:
    from src.scoring.weights_v2 import INDICATORS_V2, SCORING_VERSION_V2

    assert SCORING_VERSION_V2 == "v2.2"
    basic = INDICATORS_V2["基本面"]
    assert [ind[0] for ind in basic] == ["sector_vs_leader", "business_financial", "valuation"]
    weights = {ind[0]: ind[2] for ind in basic}
    assert weights == {
        "sector_vs_leader": 0.3125,
        "business_financial": 0.4375,
        "valuation": 0.25,
    }
    capital = INDICATORS_V2["资金面"]
    assert [ind[0] for ind in capital] == ["capital_flow"]
    assert capital[0][2] == 1.0
    all_ids = {ind[0] for inds in INDICATORS_V2.values() for ind in inds}
    assert "equity_mgmt" not in all_ids
    assert "institution_change" not in all_ids
    # 六大维度总权重不变
    from src.scoring.weights_v2 import DIMENSION_WEIGHTS_V2

    assert DIMENSION_WEIGHTS_V2 == {
        "基本面": 0.30, "情绪面": 0.15, "资金面": 0.15,
        "消息面": 0.15, "技术面": 0.15, "宏观": 0.10,
    }


def _base_inputs() -> Dict[str, Dict[str, Dict[str, Any]]]:
    """规格算例：基本面 80/60/40、资金 80/旧机构 20、旧治理 99、其余四维 50。"""
    return {
        "基本面": {
            "sector_vs_leader": _indicator({}, "sector_vs_leader", 80),
            "business_financial": _indicator({}, "business_financial", 60),
            "valuation": _indicator({}, "valuation", 40),
            # 旧治理 99：不得进入任何维度或总分
            "equity_mgmt": _indicator({}, "equity_mgmt", 99),
        },
        "资金面": {
            "capital_flow": _indicator({}, "capital_flow", 80),
            # 旧机构 20：不得拉低资金面
            "institution_change": _indicator({}, "institution_change", 20),
        },
        "消息面": {"real_news": _indicator({}, "real_news", 50), "event_plan": _indicator({}, "event_plan", 50)},
        "情绪面": {"institute_view": _indicator({}, "institute_view", 50)},
        "技术面": {
            "chanlun_struct": _indicator({}, "chanlun_struct", 50),
            "compass_weekly": _indicator({}, "compass_weekly", 50),
            "support_indicator": _indicator({}, "support_indicator", 50),
        },
        "宏观": {"liquidity": _indicator({}, "liquidity", 50), "risk_appetite": _indicator({}, "risk_appetite", 50)},
    }


def test_worked_example_basic_6125_capital_80_total_5788() -> None:
    from src.scoring.indicators_v2 import aggregate_v2_dimensions, framework_total_v2
    from src.scoring.weights_v2 import validate_v2_weights

    validate_v2_weights()
    dimensions = aggregate_v2_dimensions(_base_inputs())
    by_dim = {d["dimension"]: d for d in dimensions}
    assert by_dim["基本面"]["score"] == 61.25
    assert by_dim["资金面"]["score"] == 80.0
    # 退役指标不再出现在任何维度明细里
    for dim in dimensions:
        assert all(ind["id"] not in {"equity_mgmt", "institution_change"} for ind in dim["indicators"])
    total = framework_total_v2(dimensions)
    assert total == 57.88
    # 金融契约：总分落在各维度得分范围内（icontract ensure 的行为面）
    scores = [d["score"] for d in dimensions]
    assert min(scores) - 0.01 <= total <= max(scores) + 0.01


def test_weighted_aggregation_is_exact_decimal() -> None:
    """量化权重路径用 Decimal：31.25/43.75/25 加权无二进制漂移。"""
    from decimal import Decimal

    from src.scoring.indicators_v2 import aggregate_v2_dimensions, framework_total_v2

    inputs = _base_inputs()
    # 基本面：80×0.3125 + 60×0.4375 + 40×0.25 必须精确等于 61.25
    dimensions = aggregate_v2_dimensions(inputs)
    basic = next(d for d in dimensions if d["dimension"] == "基本面")
    assert Decimal(str(basic["score"])) == Decimal("61.25")
    total = framework_total_v2(dimensions)
    assert Decimal(str(total)) == Decimal("57.88")


def test_missing_capital_flow_keeps_gap_detail_not_fake_conclusion() -> None:
    """资金流真实缺失时仍有唯一保留指标的 50/low/gap 明细，不算有效资金结论。"""
    from src.scoring.indicators_v2 import aggregate_v2_dimensions

    inputs = _base_inputs()
    inputs["资金面"] = {"capital_flow": None, "institution_change": None}
    dimensions = aggregate_v2_dimensions(inputs)
    capital = next(d for d in dimensions if d["dimension"] == "资金面")
    assert len(capital["indicators"]) == 1
    flow = capital["indicators"][0]
    assert flow["id"] == "capital_flow"
    assert flow["score"] == 50.0 and flow["confidence"] == "low" and flow["data_gap"]
    assert capital["score"] == 50.0 and capital["data_gaps"] == 1


def test_aggregate_contract_rejects_wrong_normalized_result() -> None:
    """PR #65 标准审查反例（Layer 2 缺口）：基本面 49（正确 61.25）、资金 80、
    其余 50 → 总分 54.20 仍在 [49,80] 内，framework_total_v2 的范围 ensure
    查不出错；挂载在 aggregate_v2_dimensions 上的分维归一加权后置条件必须
    真实抛 ViolationError 拒绝该错误结果。

    受控坏体函数复用生产函数注册的同一契约对象（__postconditions__[0] 的
    condition/description），反例走真实 icontract 检查器——不是只调私有谓词
    或只查元数据的自证测试。
    """
    import copy

    from icontract import ViolationError, ensure

    from src.scoring.indicators_v2 import aggregate_v2_dimensions, framework_total_v2

    registered = getattr(aggregate_v2_dimensions, "__postconditions__", None)
    assert registered, "aggregate_v2_dimensions 必须带分维聚合后置条件"
    contract = registered[0]

    genuine = aggregate_v2_dimensions(_base_inputs())
    assert next(d for d in genuine if d["dimension"] == "基本面")["score"] == 61.25

    def bad_aggregate_v2_dimensions(
        indicator_results: Dict[str, Dict[str, Optional[Dict[str, Any]]]],
    ) -> List[Dict[str, Any]]:
        # 受控坏体：与生产同一实现路径产出，再注入审查反例的错误归一结果
        # （明细 80/60/40/权重不变，基本面总分 49 ≠ 61.25）。
        dims = copy.deepcopy(getattr(aggregate_v2_dimensions, "__wrapped__")(indicator_results))
        next(d for d in dims if d["dimension"] == "基本面")["score"] = 49.0
        return dims

    guarded_bad = ensure(condition=contract.condition, description=contract.description)(
        bad_aggregate_v2_dimensions
    )
    # 总分 54.20 仍在维度范围内（既有总分 ensure 不会拒绝），分维契约必须拒绝
    tampered = copy.deepcopy(genuine)
    next(d for d in tampered if d["dimension"] == "基本面")["score"] = 49.0
    total = framework_total_v2(tampered)
    assert total == 54.2
    scores = [d["score"] for d in tampered]
    assert min(scores) - 0.01 <= total <= max(scores) + 0.01
    with pytest.raises(ViolationError) as violated:
        guarded_bad(_base_inputs())
    assert "归一" in str(violated.value), "被拒原因必须是分维归一不变式"

    # 全缺口维度被改离中性 50：同一注册契约同样拒绝
    def bad_all_gap_aggregate_v2_dimensions(
        indicator_results: Dict[str, Dict[str, Optional[Dict[str, Any]]]],
    ) -> List[Dict[str, Any]]:
        dims = copy.deepcopy(getattr(aggregate_v2_dimensions, "__wrapped__")(indicator_results))
        capital = next(d for d in dims if d["dimension"] == "资金面")
        for entry in capital["indicators"]:
            entry["data_gap"] = True
        capital["score"] = 61.25
        return dims

    guarded_gap = ensure(condition=contract.condition, description=contract.description)(
        bad_all_gap_aggregate_v2_dimensions
    )
    with pytest.raises(ViolationError):
        guarded_gap(_base_inputs())


def test_partial_gap_renormalization_matches_contract() -> None:
    """部分缺口按可用权重归一：估值缺数据时基本面 = (80×31.25 + 60×43.75)/75
    = 68.33，契约对真实部分归一输出通过（挂载契约在真实调用上执行）。"""
    from decimal import Decimal

    from src.scoring.indicators_v2 import aggregate_v2_dimensions

    inputs = _base_inputs()
    inputs["基本面"]["valuation"] = None
    dimensions = aggregate_v2_dimensions(inputs)
    basic = next(d for d in dimensions if d["dimension"] == "基本面")
    assert Decimal(str(basic["score"])) == Decimal("68.33")
    assert basic["data_gaps"] == 1


def test_all_gap_neutral_and_zero_score_through_contract() -> None:
    """全缺口维度中性 50（规格纪律）；合法 0 分参与归一不被当缺口。

    消息面两指标 0/100 等权 → 50.0；全部缺口 → 50.0 且 data_gaps 计数如实。
    契约对两类真实输出均通过（挂载契约在真实调用上执行）。
    """
    from src.scoring.indicators_v2 import aggregate_v2_dimensions

    zero_inputs = _base_inputs()
    zero_inputs["消息面"] = {
        "real_news": _indicator({}, "real_news", 0),
        "event_plan": _indicator({}, "event_plan", 100),
    }
    dimensions = aggregate_v2_dimensions(zero_inputs)
    news = next(d for d in dimensions if d["dimension"] == "消息面")
    assert news["score"] == 50.0 and news["data_gaps"] == 0

    gap_inputs = _base_inputs()
    gap_inputs["消息面"] = {"real_news": None, "event_plan": None}
    dimensions = aggregate_v2_dimensions(gap_inputs)
    news = next(d for d in dimensions if d["dimension"] == "消息面")
    assert news["score"] == 50.0 and news["data_gaps"] == 2
    assert all(entry["data_gap"] for entry in news["indicators"])


def test_contract_follows_existing_rounding_context() -> None:
    """契约与生产共用 round(·,2) 的当前 Decimal 上下文语义（不另定舍入模式）。

    HALF_UP 环境下合法半分输出（0.005→0.01）须正常通过挂载契约，不因固定
    HALF_EVEN 假设被误拒。"""
    from decimal import ROUND_HALF_UP, localcontext

    from src.scoring.indicators_v2 import aggregate_v2_dimensions

    inputs = _base_inputs()
    inputs["基本面"] = {
        "sector_vs_leader": _indicator({}, "sector_vs_leader", 0),
        "business_financial": _indicator({}, "business_financial", 0),
        "valuation": _indicator({}, "valuation", 0.02),
    }
    with localcontext() as ctx:
        ctx.rounding = ROUND_HALF_UP
        dimensions = aggregate_v2_dimensions(inputs)
    basic = next(d for d in dimensions if d["dimension"] == "基本面")
    assert basic["score"] == 0.01, "HALF_UP 上下文：0.005 合法取整为 0.01"


def test_build_six_dim_end_to_end_v22() -> None:
    """端到端：LLM adapter=None（纪律缺口），产出框架版本 v2.2 且无退役指标。"""
    from src.deep_research_dims.context import SharedContext
    from src.deep_research_dims.six_dim import build_six_dim

    ctx = SharedContext(stock_code="600519", stock_name="贵州茅台", as_of="2026-10-29T10:00:00")
    ctx.quote = {"price": 13.0}
    ctx.history = [
        {"date": f"2026-09-{day:02d}", "close": 10.0 + day * 0.1} for day in range(1, 29)
    ]
    ctx.fundamental = {"pe_ttm": 25.0, "pb": 5.0}
    dim = build_six_dim(ctx, {}, None)
    assert dim.scoring_version == "v2.2"
    framework = dim.framework
    assert framework is not None
    names = {d.dimension for d in framework.dimensions}
    assert names == {"基本面", "情绪面", "资金面", "消息面", "技术面", "宏观"}
    for dimension in framework.dimensions:
        assert all(ind.name != "股权架构与高管" and ind.name != "机构/大户持仓变动" for ind in dimension.indicators)
