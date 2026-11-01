# -*- coding: utf-8 -*-
"""Issue #71-G：资金评分采纳边界证据（adoption_evidence）契约回归。

2026-10-31 深证年报报告资金资料完整、模型请求成功，但采纳的 flow_score 与
分析为空导致降级；原始模型回复未留存，根因无法回溯。本文件锁定修复后的
采纳边界行为契约（真实 run_capital_agent → capital parse → parse_dim 链路，
不经付费调用）：

- 证据为最小脱敏形状：字段存在性/声明类型/有限性/裁决枚举（outcome）；
  不携带原始回复文本，无动态顶层键转储（model_output_keys 等，模型可借
  输出键注入任意内容）；声明类型必须在归一覆写前捕获，否则记录的是适配
  后类型而非模型声明类型；
- 证据模型为 AGENTS 三层防御 L3 I/O 契约：strict + frozen +
  validate_assignment + extra=forbid——未知键、可矫正冒充类型（1/"yes"
  冒充布尔、越界枚举字符串）在边界被拒；CapitalDim 本体保持既有 lax
  归一契约不变（数字字符串评分等既有形态不受影响）；
- 采纳与拒绝两条路径都留证据；证据随 typed dim 保存/历史链路
  （generic_parse → CapitalDim → parse_dim 回写）真实持久化；
- 评分范围不重复校验：CapitalDim 契约是唯一范围权威，有效但越界的数字
  评分仍由契约硬拒绝（不清洗、不夹分、不加重复范围判断）；契约拒绝后经
  capital 解析窄边界保留白名单证据的降级维度（outcome=contract_rejected，
  不宣称 adopted、不含原始输入值），证据实际随维度保存；
- issue #71 收敛后的无效形状契约：布尔/非数字字符串/非有限数（含原始 NaN
  float）在采纳边界降级且原始值归 None——形状证据已在 adoption_evidence，
  原始值不进契约解析、不借 dim/save/API 或解析异常文本持久化；维度经真实
  parse → CapitalDim → parse_dim 链路可解析，证据与已取得资金事实不丢；
- 与既有 issue #64 锁定契约完全兼容：合法零分采纳；有效分 + 空分析降级时
  dim.flow_score 保留原值、score 为 None；固定 flow_summary 文案不得掩盖
  模型分析为空的事实；
- 分析正文只认真正的字符串：布尔/数字/容器的 narrative/flow_summary 不经
  str() 强转成「分析」（``str(True)`` 不得构成解读正文），evidence 的
  analysis_present/analysis_source 与实际采纳严格一致。

键名以收敛实现为准：本文件断言 flow_score_present / flow_score_declared_type /
flow_score_finite / analysis_present / analysis_source / outcome（裁决枚举：
adopted / score_missing / score_bool / score_invalid / score_nonfinite /
analysis_empty；范围越界不设专用枚举值，由 CapitalDim 契约权威处置）。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Callable, Dict, List

import pytest
from pydantic import ValidationError

from src.agent.deep_research import explore_agents
from src.agent.tools import data_tools
from src.schemas.deep_research_dims import (
    CapitalAdoptionEvidence,
    CapitalDim,
    parse_dim,
)
from src.deep_research.researchers.capital import parse

_EVIDENCE_ALLOWED_KEYS = frozenset(
    {
        "outcome",
        "gate",
        "flow_score_present",
        "flow_score_declared_type",
        "flow_score_finite",
        "flow_score_coerced",
        "analysis_present",
        "analysis_source",
        "narrative_kind",
        "model_flow_summary_kind",
    }
)
_EVIDENCE_REQUIRED_KEYS = frozenset(
    {
        "outcome",
        "flow_score_present",
        "flow_score_declared_type",
        "flow_score_finite",
        "analysis_present",
        "analysis_source",
    }
)
_DECLARED_TYPE_WHITELIST = frozenset(
    {"absent", "NoneType", "bool", "int", "float", "str", "dict", "list"}
)

_DATES: List[str] = [
    "2026-10-29", "2026-10-28", "2026-10-27", "2026-10-26", "2026-10-23",
    "2026-10-22", "2026-10-21", "2026-10-20", "2026-10-19", "2026-10-16",
    "2026-10-15", "2026-10-14",
]


@pytest.fixture
def flow_fixture(monkeypatch: pytest.MonkeyPatch) -> Dict[str, int]:
    """合格 Choice 资金三窗口（T 完整），与 tests/test_issue64_capital_research.py 同口径。"""
    from data_provider.base import DataFetcherManager
    from data_provider.choice_adapter import ChoiceAdapter
    from src.config import Config

    monkeypatch.setattr(Config, "_instance", Config(data_source_profile="choice", deep_research_cross_validate=False))
    ordered = list(reversed(_DATES))

    def request(method: str, args: Any, options: Any, deadline: Any) -> Dict[str, Any]:
        if method == "tradedates":
            return {"data": ordered}
        assert method == "csd" and args[1] == "NETINFLOW"
        values = [float(i + 1) for i in range(len(ordered))]
        return {"codes": [args[0]], "indicators": ["NETINFLOW"], "dates": ordered,
                "data": {args[0]: [values]}}

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(request))
    manager = DataFetcherManager()
    monkeypatch.setattr(data_tools, "_fetcher_manager_singleton", manager)
    return {}


def _scripted_model(content: str) -> Callable[..., Any]:
    def generate(**kwargs: Any) -> Any:
        return SimpleNamespace(success=True, content=content, total_steps=1, error=None)

    return generate


def _capital_result(monkeypatch: pytest.MonkeyPatch, content: str) -> Dict[str, Any]:
    monkeypatch.setattr(explore_agents, "run_agent_loop", _scripted_model(content))
    result = explore_agents.run_capital_agent("300398", "飞凯材料", object())
    assert result.get("ok"), f"资金研究员应成功返回：{result.get('error')}"
    return result


def _parse_dim_roundtrip(data: Dict[str, Any]) -> Any:
    """编排器同口径：capital parse → 契约 → parse_dim 回写（retained 附加路径）。"""
    dim, used = parse(data, 1)
    payload = dim.model_dump()
    payload["retained_evidence"] = []
    return parse_dim("capital", payload), used


def _evidence(dim: Any) -> Dict[str, Any]:
    evidence = dim.adoption_evidence
    assert evidence is not None, "该路径必须留采纳边界证据"
    dumped = evidence.model_dump() if hasattr(evidence, "model_dump") else dict(evidence)
    return dict(dumped)


def test_valid_zero_score_adopted_with_evidence(flow_fixture, monkeypatch) -> None:
    result = _capital_result(
        monkeypatch,
        json.dumps({"flow_score": 0, "narrative": "资金中性，无明显异动。", "flow_summary": "零分结论"}),
    )
    assert result["data"]["score"] == 0.0, "合法零分必须采纳，不得伪造或丢弃"
    dim, _ = _parse_dim_roundtrip(result["data"])
    assert dim.status == "ok"
    assert dim.score == 0.0
    evidence = _evidence(dim)
    assert evidence["outcome"] == "adopted"
    assert evidence["flow_score_present"] is True
    assert evidence["flow_score_declared_type"] == "int"
    assert evidence["flow_score_finite"] is True
    assert evidence["analysis_source"] == "narrative"


def test_missing_score_degrades_and_evidence_persists(flow_fixture, monkeypatch) -> None:
    result = _capital_result(
        monkeypatch,
        json.dumps({"narrative": "只有叙述", "flow_summary": "只有摘要"}),
    )
    assert result["data"]["status"] == "degraded"
    dim, _ = _parse_dim_roundtrip(result["data"])
    assert dim.status == "degraded"
    assert "未形成可用评分" in (dim.degraded_reason or ""), "人读降级原因必须保留，不得变成契约报错"
    evidence = _evidence(dim)
    assert evidence["outcome"] == "score_missing"
    assert evidence["flow_score_present"] is False
    assert "score" not in result["data"], "缺失评分不得派生资金总分"


def test_bool_score_degraded_truthful(flow_fixture, monkeypatch) -> None:
    result = _capital_result(
        monkeypatch,
        json.dumps({"flow_score": True, "narrative": "观察结论段落。"}),
    )
    assert result["data"]["status"] == "degraded"
    assert "score" not in result["data"], "布尔分不得被当作有效分采纳"
    dim, _ = _parse_dim_roundtrip(result["data"])
    assert dim.status == "degraded" and dim.score is None
    assert dim.flow_score is None, "无效形状的原始布尔值不得随 dim 持久化"
    evidence = _evidence(dim)
    assert evidence["outcome"] == "score_bool"
    assert evidence["flow_score_declared_type"] == "bool"


def test_invalid_score_shapes_keep_degraded_dim_with_evidence(flow_fixture, monkeypatch) -> None:
    """issue #71 收敛：无效评分形状在采纳边界降级，原始值不进契约解析。

    反例覆盖非数字字符串/非有限字符串/原始 NaN float：维度必须经真实
    parse → CapitalDim → parse_dim 链路完整可解析（不再在契约边界
    ValidationError 丢失证据），degraded 原因为人读固定文案（不含原始值），
    evidence 保留形状级结论，flow_score 不落任何原始无效值。
    """
    for content, outcome, declared, finite in (
        (json.dumps({"flow_score": "abc", "narrative": "观察。"}), "score_invalid", "str", None),
        (json.dumps({"flow_score": "NaN", "narrative": "观察。"}), "score_invalid", "str", None),
        ('{"flow_score": NaN, "narrative": "观察。"}', "score_nonfinite", "float", False),
        (json.dumps({"flow_score": True, "narrative": "观察。"}), "score_bool", "bool", None),
        (json.dumps({"narrative": "观察。"}), "score_missing", "absent", None),
    ):
        result = _capital_result(monkeypatch, content)
        assert result["data"]["status"] == "degraded"
        assert "score" not in result["data"], "无效评分不得派生资金总分"
        assert result["data"]["flow_score"] is None, "原始无效值不得留在载荷中"
        assert "abc" not in json.dumps(result["data"]["adoption_evidence"])
        dim, _ = _parse_dim_roundtrip(result["data"])
        assert dim.status == "degraded"
        assert dim.score is None and dim.flow_score is None
        assert "未形成可用评分" in (dim.degraded_reason or "")
        evidence = _evidence(dim)
        assert evidence["outcome"] == outcome
        assert evidence["flow_score_declared_type"] == declared
        assert evidence["flow_score_finite"] == finite


def test_empty_analysis_degraded_not_masked_by_fixed_summary(flow_fixture, monkeypatch) -> None:
    result = _capital_result(
        monkeypatch,
        json.dumps({"flow_score": 80}),
    )
    assert result["data"]["status"] == "degraded"
    dim, _ = _parse_dim_roundtrip(result["data"])
    assert dim.status == "degraded"
    assert dim.flow_score == 80, "既有锁定：模型原始评分保留为事实（R5 反例）"
    assert dim.score is None, "无分析正文不构成有效结论"
    assert dim.flow_summary.startswith(("Choice 主力净流入", "主力净流入")), "降级维度 flow_summary 为固定说明文案"
    evidence = _evidence(dim)
    assert evidence["outcome"] == "analysis_empty"
    assert evidence["analysis_present"] is False, "固定文案不得掩盖模型分析为空的事实"
    assert evidence["analysis_source"] is None
    assert evidence["flow_score_declared_type"] == "int"


def test_analysis_from_model_flow_summary_adopted(flow_fixture, monkeypatch) -> None:
    result = _capital_result(
        monkeypatch,
        json.dumps({"flow_score": 55, "narrative": "", "flow_summary": "模型摘要正文"}),
    )
    assert result["data"]["score"] == 55.0
    dim, _ = _parse_dim_roundtrip(result["data"])
    assert dim.status == "ok"
    evidence = _evidence(dim)
    assert evidence["outcome"] == "adopted"
    assert evidence["analysis_source"] == "flow_summary"


def test_summary_fallback_analysis_actually_saved_into_final_dim(flow_fixture, monkeypatch) -> None:
    """R4：采纳正文选中 flow_summary 时，真实模型分析必须实际落最终维度。

    后段固定资金事实会覆写 data.flow_summary；narrative 原为空或非字符串时，
    唯一真实模型分析经落位 narrative 保存——最终 CapitalDim 携带原分析文本
    （不只证明 gate 曾看见字符串），非字符串 narrative 原值不被转成正文，
    固定资金事实 flow_summary 照常保留。
    """
    for raw_narrative in (True, "", None):
        result = _capital_result(
            monkeypatch,
            json.dumps({"flow_score": 66, "narrative": raw_narrative, "flow_summary": "模型真实摘要分析"}),
        )
        assert result["data"]["score"] == 66.0
        assert result["data"]["narrative"] == "模型真实摘要分析"
        dim, _ = _parse_dim_roundtrip(result["data"])
        assert dim.status == "ok"
        assert dim.narrative == "模型真实摘要分析", "最终保存的必须是模型真实分析"
        assert dim.flow_summary.startswith(("Choice 主力净流入", "主力净流入")), "固定资金事实照常保留"
        evidence = _evidence(dim)
        assert evidence["outcome"] == "adopted"
        assert evidence["analysis_source"] == "flow_summary"


def test_non_string_analysis_not_str_coerced_into_adoption(flow_fixture, monkeypatch) -> None:
    """布尔/数字/容器/空白的分析正文不 str 强转成「已完成解读」（issue #71）。

    ``str(True)=="True"`` 曾伪装成非空分析并携评分采纳；现在必须降级，且
    evidence 的 analysis_present/analysis_source 与实际采纳一致；最终维度经
    真实 parse → CapitalDim 链路解析（非字符串 narrative 不炸契约解析）。
    """
    for content in (
        json.dumps({"flow_score": 55, "flow_summary": True}),
        json.dumps({"flow_score": 55, "narrative": True}),
        json.dumps({"flow_score": 55, "narrative": ["段落"]}),
        json.dumps({"flow_score": 55, "narrative": {"text": "段落"}}),
        json.dumps({"flow_score": 55, "narrative": 123}),
        json.dumps({"flow_score": 55, "narrative": "   "}),
    ):
        result = _capital_result(monkeypatch, content)
        assert result["data"]["status"] == "degraded", content
        assert "score" not in result["data"], content
        dim, _ = _parse_dim_roundtrip(result["data"])
        assert dim.status == "degraded" and dim.score is None, content
        evidence = _evidence(dim)
        assert evidence["outcome"] == "analysis_empty", content
        assert evidence["analysis_present"] is False, content
        assert evidence["analysis_source"] is None, content


def test_nested_dict_score_declared_type_recorded_before_adaptation(flow_fixture, monkeypatch) -> None:
    result = _capital_result(
        monkeypatch,
        json.dumps({"flow_score": {"score": 87}, "narrative": "分析正文"}),
    )
    assert result["data"]["score"] == 87.0
    dim, _ = _parse_dim_roundtrip(result["data"])
    evidence = _evidence(dim)
    assert evidence["flow_score_declared_type"] == "dict", (
        "声明类型必须在归一覆写前捕获——覆写后只能读到适配后的 float"
    )
    assert evidence["outcome"] == "adopted"


def test_evidence_survives_parse_dim_roundtrip_and_json(flow_fixture, monkeypatch) -> None:
    result = _capital_result(
        monkeypatch,
        json.dumps({"narrative": "只有叙述"}),
    )
    dim, _ = _parse_dim_roundtrip(result["data"])
    assert dim.adoption_evidence is not None
    assert "adoption_evidence" in dim.model_dump_json(), "证据随历史序列化可回放"


def test_evidence_shape_is_sanitized(flow_fixture, monkeypatch) -> None:
    result = _capital_result(
        monkeypatch,
        json.dumps({"flow_score": None, "weird_model_key": "x" * 500, "narrative": "正文"}),
    )
    dim, _ = _parse_dim_roundtrip(result["data"])
    evidence = _evidence(dim)
    assert _EVIDENCE_REQUIRED_KEYS <= set(evidence), f"缺少必要语义键：{_EVIDENCE_REQUIRED_KEYS - set(evidence)}"
    assert set(evidence) <= _EVIDENCE_ALLOWED_KEYS, f"证据出现契约外动态键：{set(evidence) - _EVIDENCE_ALLOWED_KEYS}"
    assert "model_output_keys" not in evidence, "禁止动态顶层键转储（模型可借输出键注入任意内容）"
    for key, value in evidence.items():
        if isinstance(value, str):
            assert len(value) <= 32, f"{key} 必须是短枚举值，不得携带模型内容"
            assert value in _DECLARED_TYPE_WHITELIST or key == "analysis_source" or key == "outcome" or key == "gate", (
                f"{key} 取值必须来自固定枚举：{value}"
            )


def test_evidence_contract_strict_closed_frozen() -> None:
    """AGENTS L3 I/O 契约：strict + frozen + validate_assignment + extra=forbid。

    未知键与可矫正冒充类型在边界被拒；CapitalDim 本体 lax 契约不受影响。
    """
    base: Dict[str, Any] = {
        "outcome": "adopted",
        "flow_score_present": True,
        "flow_score_declared_type": "int",
        "flow_score_finite": True,
        "analysis_present": True,
        "analysis_source": "narrative",
    }
    # 未知键被 extra=forbid 拒绝（model_output_keys 即真实注入反例）
    with pytest.raises(ValidationError):
        CapitalAdoptionEvidence(**{**base, "model_output_keys": ["任意注入键"]})
    # strict 拒绝可矫正冒充类型：int/字符串冒充布尔
    with pytest.raises(ValidationError):
        CapitalAdoptionEvidence(**{**base, "flow_score_present": 1})
    with pytest.raises(ValidationError):
        CapitalAdoptionEvidence(**{**base, "flow_score_finite": "true"})
    with pytest.raises(ValidationError):
        CapitalAdoptionEvidence(**{**base, "analysis_present": "yes"})
    # 枚举封闭：范围越界无专用枚举值，枚举外字符串被拒
    with pytest.raises(ValidationError):
        CapitalAdoptionEvidence(**{**base, "outcome": "score_out_of_range"})
    with pytest.raises(ValidationError):
        CapitalAdoptionEvidence(**{**base, "flow_score_declared_type": "float64"})

    evidence = CapitalAdoptionEvidence(**base)
    # frozen + validate_assignment：赋值改写被拒
    with pytest.raises(ValidationError):
        evidence.outcome = "score_missing"


def test_evidence_json_string_roundtrip_through_contract_and_registry(flow_fixture, monkeypatch) -> None:
    """合法 typed 证据以 JSON 字符串形态经真实契约与 registry 链路往返。

    generic_parse 的 coerce_value 会把 dict 证据 json.dumps 成字符串，
    CapitalDim before-validator 在契约边界解析回 strict 模型；CapitalDim
    本体 lax 归一契约（数字字符串评分）不受证据 strict 化影响。
    """
    evidence = {
        "outcome": "adopted",
        "flow_score_present": True,
        "flow_score_declared_type": "int",
        "flow_score_finite": True,
        "analysis_present": True,
        "analysis_source": "narrative",
    }
    dim = CapitalDim.model_validate(
        {
            "dim": "capital",
            "adoption_evidence": json.dumps(evidence, ensure_ascii=False),
            "flow_score": 87,
            "narrative": "分析正文",
        }
    )
    assert isinstance(dim.adoption_evidence, CapitalAdoptionEvidence)
    assert dim.adoption_evidence.outcome == "adopted"
    assert dim.adoption_evidence.flow_score_finite is True

    legacy = CapitalDim.model_validate({"dim": "capital", "flow_score": "82"})
    assert legacy.flow_score == 82.0, "既有 lax 数字字符串归一契约保持不变"
    assert legacy.adoption_evidence is None, "历史载荷缺证据字段解析为 None"

    result = _capital_result(monkeypatch, json.dumps({"flow_score": 87, "narrative": "分析正文"}))
    dim3, _ = _parse_dim_roundtrip(result["data"])
    assert isinstance(dim3.adoption_evidence, CapitalAdoptionEvidence)
    assert dim3.adoption_evidence.outcome == "adopted"


def test_out_of_range_score_keeps_existing_contract_rejection(flow_fixture, monkeypatch) -> None:
    """R5 收敛：范围权威仍在 CapitalDim 契约（越界值不进维度），采纳门既有行为
    不变；契约硬拒绝后，白名单采纳证据经现有 capital 解析窄边界实际保存进
    降级维度——最终保存载荷 evidence 非空、不宣称 adopted、score 为 None，
    降级原因为固定脱敏文案（不含原始 ValidationError 输入值）。
    """
    result = _capital_result(monkeypatch, json.dumps({"flow_score": 150, "narrative": "分析正文"}))
    assert result["data"]["flow_score"] == 150, "越界评分不被清洗为 None 或其他值"
    assert result["data"]["score"] == 150.0, "采纳门既有行为不变（不伪造成功也不额外降级）"
    assert result["data"].get("status") != "degraded"
    dim, _ = _parse_dim_roundtrip(result["data"])
    assert dim.status == "degraded" and dim.score is None
    assert dim.flow_score is None, "越界值不进维度（契约权威不变）"
    assert "150" not in (dim.degraded_reason or ""), "降级原因不得携带原始 ValidationError 输入值"
    evidence = _evidence(dim)
    assert evidence["outcome"] == "contract_rejected", "契约拒绝后证据不得仍宣称 adopted"
    assert evidence["flow_score_present"] is True
    assert evidence["flow_score_declared_type"] == "int"
    assert "adoption_evidence" in dim.model_dump_json(), "证据随保存/历史序列化实际持久化"
