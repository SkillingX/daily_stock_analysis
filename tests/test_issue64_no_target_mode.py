# -*- coding: utf-8 -*-
"""Issue #64 非估值模式：目标价成功空覆盖 + 合格非估值证据 → 情景/结论/信号完成。

规格 2.2：Choice 目标价查询成功、契约有效但窗口无可追溯目标价，且所用非估值
核心输入/研究/守卫正常时，三项 ok 并从 missing_layers 移除；valuation_basis=none，
目标价/EV/收益空间/价值区间为空；无概率时结构化 scenarios=[]，不伪造概率/EV；
查询错误、非法/过期读数、证据不足、模型失败仍真实降级；有效目标价路径回归。

测试走真实构建 → 编排证据包 → 结论/信号 → guardrail → 经理终稿 → 渲染入口；
只替换外部传输（socket/Choice 原生），配置经 empty.env 隔离，缓存落 tmp。
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Dict, List, Optional, cast

import pytest

from src.agent.llm_adapter import LLMResponse
from src.deep_research_dims.context import SharedContext
from src.deep_research_dims.scenarios_dim import build_scenarios_dim
from src.schemas.deep_research_dims import ScenariosDim
from tests.test_choice_integration import choice_transport


@pytest.fixture(autouse=True)
def _offline_isolation(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """确定性隔离（R6 统一要求）：socket 与 Choice 原生传输 fail-on-outbound、缓存落 tmp。"""
    import socket

    monkeypatch.setenv("LITELLM_TELEMETRY", "False")
    # litellm import 时默认拉远端 model cost map；本地副本零出站。
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    # 无凭据确定性配置（协调者实测口径）：fresh Config(choice) + 禁 iFinD。
    from src.config import Config

    config = Config(data_source_profile="choice", enable_ifind_mcp=False, deep_research_cross_validate=False)
    monkeypatch.setattr(Config, "_instance", config)
    from data_provider.choice_adapter import ChoiceAdapter
    from src.deep_research_dims import dim_cache

    def no_connect(self: Any, address: Any) -> None:
        raise AssertionError(f"unexpected outbound socket connect: {address!r}")

    def no_connect_ex(self: Any, address: Any) -> Any:
        raise AssertionError(f"unexpected outbound socket connect_ex: {address!r}")

    def no_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"unexpected outbound create_connection: {address!r}")

    monkeypatch.setattr(socket.socket, "connect", no_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", no_connect_ex)
    monkeypatch.setattr(socket, "create_connection", no_create_connection)

    def no_native(method: str, args: Any, options: Any, deadline: Any) -> Dict[str, Any]:
        raise AssertionError(f"unexpected Choice native request: {method}")

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(no_native))
    monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path / "dim_cache"))


def _ctx(research: Optional[Dict[str, Any]] = None) -> SharedContext:
    ctx = SharedContext(stock_code="300398", stock_name="飞凯材料", as_of="2026-10-29T15:00:00")
    ctx.quote = {"price": 20.0}
    ctx.fundamental = {
        "pe_ttm": 40.0,
        "pb": 3.0,
        "research": research if research is not None else {
            "source": "choice",
            "as_of": "2026-10-29",
            "target_window_days": 180,
            "targets": {},
            "errors": [],
        },
    }
    return ctx


def _evidence_pack(**overrides: Any) -> Dict[str, Any]:
    pack: Dict[str, Any] = {
        "industry_space": "盘面根因：材料订单回暖；机制：下游补库；量级：正面利好",
        "competitive_evolution": "公司在中端市场地位稳固，成本曲线位于行业左侧。",
        "catalysts": ["年报披露（2027-04）", "新产品验证（日期未知）"],
        "risks": ["上游原料价格波动", "客户集中度较高"],
    }
    pack.update(overrides)
    return {k: v for k, v in pack.items() if v}


def _choice_target_research(
    targets: Optional[Dict[str, Any]] = None,
    errors: Optional[list] = None,
    as_of: str = "2026-10-29",
    window_days: Any = 180,
) -> Dict[str, Any]:
    """Choice 目标价 research 块（默认：查询成功、窗口无覆盖）。"""
    return {
        "source": "choice",
        "as_of": as_of,
        "target_window_days": window_days,
        "targets": targets if targets is not None else {},
        "errors": errors or [],
    }


# ---------------------------------------------------------------------------
# scenarios：合法非估值分支 / 真实错误 / 证据不足
# ---------------------------------------------------------------------------


def test_scenarios_ok_on_qualitative_branch_without_target() -> None:
    dim = build_scenarios_dim(_ctx(), 20.0, _evidence_pack(), None)
    assert dim.status == "ok", dim.degraded_reason
    assert dim.valuation_basis == "none"
    assert dim.target_price is None
    assert dim.expected_value is None
    assert dim.analyst_target is None
    assert dim.scenarios is not None and dim.scenarios.scenarios == [], "无概率不创建结构化情景"
    assert dim.probability_sum == 0.0
    assert "非估值" in dim.narrative
    assert "2026-10-29" in dim.narrative, "范围说明带实际查询时点"
    # 定性内容承载在现有字段
    assert dim.scenarios.catalysts and dim.scenarios.risks
    assert dim.scenarios.industry_space


def test_scenarios_empty_or_blank_evidence_still_degraded() -> None:
    """整体定性内容为空/空白不算正常：维持降级；一条实际非空内容即合格。

    B5 统一谓词不数槽位、不设长度门槛：一份完成研究的一条实质风险合法；
    空包、空白字符串与仅状态字段的空壳不放行。
    """
    dim = build_scenarios_dim(_ctx(), 20.0, None, None)
    assert dim.status == "degraded"
    blank = build_scenarios_dim(_ctx(), 20.0, {"catalysts": ["", "  "], "risks": []}, None)
    assert blank.status == "degraded", "空白条目不构成实质内容"
    status_only = build_scenarios_dim(_ctx(), 20.0, {"industry_space": None, "risks": []}, None)
    assert status_only.status == "degraded", "仅状态/空字段的空壳不放行"
    thin = build_scenarios_dim(_ctx(), 20.0, {"catalysts": ["唯一一条实质风险"]}, None)
    assert thin.status == "ok", thin.degraded_reason


def test_scenarios_target_query_error_still_degraded() -> None:
    """查询错误（超时/额度）不得转换为"无覆盖"：维持降级与真实原因。"""
    research = _choice_target_research(errors=["choice:targets:timeout"])
    dim = build_scenarios_dim(_ctx(research), 20.0, _evidence_pack(), None)
    assert dim.status == "degraded"
    assert "尚未取得" in (dim.degraded_reason or "")


def test_scenarios_target_error_without_targets_key_keeps_real_cause() -> None:
    """生产适配器在查询失败时不写入 targets 键：真实原因不得被契约异常文案掩盖。"""
    research = _choice_target_research(errors=["choice:targets:quota_exhausted"])
    research.pop("targets")
    dim = build_scenarios_dim(_ctx(research), 20.0, _evidence_pack(), None)
    assert dim.status == "degraded"
    assert "额度" in (dim.degraded_reason or "")


def test_scenarios_stale_snapshot_without_valid_reference_degrades() -> None:
    """目标价读数过期（快照日超出允许窗）：不进入非估值 ok。"""
    research = _choice_target_research(targets={"RATINGTARGETPRICE": 25.0}, as_of="2026-10-20")
    dim = build_scenarios_dim(_ctx(research), 20.0, _evidence_pack(), None)
    assert dim.status == "degraded"
    assert "过期" in (dim.degraded_reason or "")


def test_scenarios_missing_as_of_or_window_marker_degrades() -> None:
    """快照无日期/缺 targets 标识的"成功空覆盖"不构成有效空覆盖。"""
    no_as_of = _choice_target_research(as_of="")
    dim = build_scenarios_dim(_ctx(no_as_of), 20.0, _evidence_pack(), None)
    assert dim.status == "degraded"
    research_no_targets_key = _choice_target_research()
    research_no_targets_key.pop("targets")
    dim = build_scenarios_dim(_ctx(research_no_targets_key), 20.0, _evidence_pack(), None)
    assert dim.status == "degraded", "缺 targets 标识不能当作已确认的无覆盖"
    no_window = _choice_target_research()
    no_window.pop("target_window_days")
    dim = build_scenarios_dim(_ctx(no_window), 20.0, _evidence_pack(), None)
    assert dim.status == "degraded", "缺窗口标识不能当作已确认的无覆盖"


def test_scenarios_stale_empty_coverage_degrades() -> None:
    """R7 反例：空覆盖但快照陈旧（6 个交易日前）→ 不是可信的当日空覆盖。"""
    research = _choice_target_research(targets={}, as_of="2026-10-20")
    dim = build_scenarios_dim(_ctx(research), 20.0, _evidence_pack(), None)
    assert dim.status == "degraded"


def test_scenarios_invalid_window_days_degrades() -> None:
    """R7 反例：target_window_days=0 的空覆盖——有效目标价契约是 180 日。"""
    research = _choice_target_research(targets={}, window_days=0)
    dim = build_scenarios_dim(_ctx(research), 20.0, _evidence_pack(), None)
    assert dim.status == "degraded"


def test_scenarios_partial_target_bounds_not_confirmed_empty_coverage() -> None:
    """部分/不一致读数（有边界无综合值）不是已确认无覆盖：降级并如实说明，不猜均值。"""
    research = _choice_target_research(
        targets={"RATINGTARGETPRICE": None, "RATINGTARGETPRICEMIN": 20.0, "RATINGTARGETPRICEMAX": 40.0},
    )
    dim = build_scenarios_dim(_ctx(research), 20.0, _evidence_pack(), None)
    assert dim.status == "degraded"
    assert "部分或不一致读数" in (dim.degraded_reason or "")
    research_min_only = _choice_target_research(targets={"RATINGTARGETPRICEMIN": 20.0})
    dim = build_scenarios_dim(_ctx(research_min_only), 20.0, _evidence_pack(), None)
    assert dim.status == "degraded"


def test_scenarios_valid_target_keeps_existing_ok_path() -> None:
    research = _choice_target_research(
        targets={"RATINGTARGETPRICE": 25.0, "RATINGTARGETPRICEMIN": 22.0, "RATINGTARGETPRICEMAX": 28.0},
    )
    dim = build_scenarios_dim(_ctx(research), 20.0, _evidence_pack(), None)
    assert dim.status == "ok"
    assert dim.valuation_basis == "analyst_consensus"
    assert dim.target_price == 25.0


# ---------------------------------------------------------------------------
# conclusion / signal：真实构建链（scenarios → conclusion → signal）
# ---------------------------------------------------------------------------


def _conclusion_signal_chain(research: Optional[Dict[str, Any]] = None, evidence: Optional[Dict[str, Any]] = None):
    """真实构建链：scenarios（含贝叶斯/六维真实结构）→ conclusion → signal。"""
    from src.deep_research_dims.bayesian_dim import build_bayesian_dim
    from src.deep_research_dims.conclusion_dim import build_conclusion_dim
    from src.deep_research_dims.signal_dim import build_signal_dim
    from src.deep_research_dims.six_dim import build_six_dim

    ctx = _ctx(research)
    ctx.history = [
        {"date": f"2026-09-{day:02d}", "open": 10, "high": 11, "low": 9, "close": 10.0 + day * 0.05, "volume": 1000}
        for day in range(1, 29)
    ]
    six_dim = build_six_dim(ctx, {}, None)
    bayesian = build_bayesian_dim(six_dim.model_dump(), [])
    scenarios = build_scenarios_dim(ctx, 20.0, evidence if evidence is not None else _evidence_pack(), None)
    conclusion = build_conclusion_dim(ctx, six_dim.model_dump(), bayesian.model_dump(), scenarios.model_dump())
    signal = build_signal_dim(ctx, conclusion.model_dump(), scenarios.model_dump())
    return scenarios, conclusion, signal


def test_chain_completes_on_qualitative_branch() -> None:
    scenarios, conclusion, signal = _conclusion_signal_chain()
    assert scenarios.status == "ok" and scenarios.valuation_basis == "none"
    assert conclusion.status == "ok", conclusion.degraded_reason
    assert conclusion.conclusion is not None
    assert conclusion.conclusion.action == "观察", "缺估值时行动保持观察/条件跟踪"
    assert conclusion.conclusion.value_range_1y is None and conclusion.conclusion.value_range_5y is None
    assert "非估值" in conclusion.narrative
    assert signal.status == "ok", signal.degraded_reason
    assert signal.rating == "中性" and signal.confidence == "低"
    assert signal.target_price is None and signal.expected_value is None
    assert "无目标价" in signal.one_sentence


def test_chain_still_degraded_when_scenarios_degraded() -> None:
    """目标价查询真实失败 → 情景降级 → 结论/信号连带降级（真实依赖契约）。"""
    research = _choice_target_research(errors=["choice:targets:quota_exhausted"])
    scenarios, conclusion, signal = _conclusion_signal_chain(research)
    assert scenarios.status == "degraded"
    assert conclusion.status == "degraded"
    assert conclusion.conclusion.action == "观察"
    assert signal.status == "degraded"


def test_chain_degraded_when_six_dim_has_no_usable_framework() -> None:
    """R7 反例：六维/贝叶斯空对象（无 dimension_total/后验结构）→ 结论/信号不得靠默认 50/0.5 自动成功。"""
    research = _choice_target_research()
    ctx = _ctx(research)
    from src.deep_research_dims.conclusion_dim import build_conclusion_dim
    from src.deep_research_dims.signal_dim import build_signal_dim

    scenarios = build_scenarios_dim(ctx, 20.0, _evidence_pack(), None)
    assert scenarios.status == "ok"
    conclusion = build_conclusion_dim(ctx, {}, {}, scenarios.model_dump())
    assert conclusion.status == "degraded", conclusion.degraded_reason
    signal = build_signal_dim(ctx, conclusion.model_dump(), scenarios.model_dump())
    assert signal.status == "degraded", "结论降级时信号不得单独 ok"


def test_chain_degraded_with_default_50_and_empty_dimensions() -> None:
    """R7 反例：显式 degraded 六维/贝叶斯、dimensions=[] 但默认 50/0.5 → 结论仍 degraded。"""
    from src.deep_research_dims.conclusion_dim import build_conclusion_dim
    from src.deep_research_dims.signal_dim import build_signal_dim

    scenarios = build_scenarios_dim(_ctx(), 20.0, _evidence_pack(), None)
    six_payload = {"framework": {"dimension_total": 50.0, "dimensions": []}, "status": "degraded"}
    bayes_payload = {"bayesian": {"prior_p": 0.5, "posterior_p": 0.5, "edge": 0.0}, "status": "degraded"}
    conclusion = build_conclusion_dim(_ctx(), six_payload, bayes_payload, scenarios.model_dump())
    assert conclusion.status == "degraded", conclusion.degraded_reason
    signal = build_signal_dim(_ctx(), conclusion.model_dump(), scenarios.model_dump())
    assert signal.status == "degraded"


def test_qualitative_content_required_for_conclusion() -> None:
    """仅 status/basis 两字段的裸 scenarios 不构成结论资格：需实质说明与条件/风险。"""
    from src.deep_research_dims.conclusion_dim import build_conclusion_dim

    bare = ScenariosDim(status="ok", valuation_basis="none").model_dump()
    conclusion = build_conclusion_dim(
        _ctx(),
        {"framework": {"dimension_total": 55.0, "dimensions": [{"dimension": "资金面"}]}},
        {"bayesian": {"posterior_p": 0.55}},
        bare,
    )
    assert conclusion.status == "degraded", conclusion.degraded_reason


# ---------------------------------------------------------------------------
# manager / 编排证据包（真实 Dim 模型）/ 渲染整链
# ---------------------------------------------------------------------------


def test_manager_section_keys_drop_ownership() -> None:
    from src.deep_research_dims.manager_writeup import DIM_SECTION_KEYS

    assert "ownership" not in DIM_SECTION_KEYS


def test_manager_evidence_complete_with_no_target_mode() -> None:
    """非估值三项 ok + 核心 ok 维度 → 经理终稿证据链完整（真实模型实例）。"""
    from src.deep_research_dims.manager_writeup import manager_evidence_complete
    from src.schemas.deep_research_dims import CapitalDim, FundamentalDim, SignalDim

    dims: Dict[str, Any] = {
        "fundamental": FundamentalDim(status="ok"),
        "capital": CapitalDim(status="ok", flow_summary="资金观察。"),
        "scenarios": ScenariosDim(status="ok", valuation_basis="none", narrative="非估值分析说明。"),
        "conclusion": _conclusion_dims_model("ok"),
        "signal": SignalDim(status="ok", one_sentence="观察。"),
    }
    assert manager_evidence_complete(dims)


def _conclusion_dims_model(status: str) -> Any:
    from src.deep_research_dims.conclusion_dim import build_conclusion_dim

    dimensions = [{"dimension": name, "score": 55.0, "indicators": [], "data_gaps": 0} for name in ("基本面", "情绪面", "资金面", "消息面", "技术面", "宏观")]
    return build_conclusion_dim(
        _ctx(),
        {"framework": {"dimension_total": 55.0, "dimensions": dimensions}},
        {"bayesian": {"prior_p": 0.5, "posterior_p": 0.55, "edge": 0.05}},
        ScenariosDim(
            status=status, valuation_basis="none", narrative="非估值分析说明。",
            scenarios={"catalysts": ["年报披露"], "risks": ["原料波动"]},
        ).model_dump() if status == "ok" else ScenariosDim(status=status, valuation_basis="none").model_dump(),
    )


def test_qualitative_evidence_pack_consumes_real_dim_models() -> None:
    """证据包消费真实 Dim 模型与 status：无可用研究时为空（scenarios 维持降级）。"""
    from src.agent.deep_research.orchestrator import _qualitative_evidence_pack
    from src.schemas.deep_research_dims import BusinessDim, IntelDim

    assert _qualitative_evidence_pack({}) == {}
    degraded_intel = IntelDim(status="degraded")
    assert _qualitative_evidence_pack({"intel": degraded_intel}) == {}
    # 核心 gate：fundamental/capital 未正常完成时非估值证据不充分
    from src.schemas.deep_research_dims import CapitalDim, FundamentalDim

    ok_intel = IntelDim(
        status="ok",
        root_cause={"event": "订单回暖", "mechanism": "下游补库", "magnitude": "正面利好", "persistence": "中期"},
    )
    assert _qualitative_evidence_pack({"intel": ok_intel}) == {}, "缺核心研究时证据包为空"
    core = {
        "fundamental": FundamentalDim(status="ok"),
        "capital": CapitalDim(status="ok", flow_summary="资金观察。"),
    }
    assert _qualitative_evidence_pack({**core, "intel": degraded_intel}) == {}
    assert _qualitative_evidence_pack({**core, "intel": ok_intel}).get("industry_space")
    # R7 反例：degraded intel 的残留根因载荷不得作为合格证据采纳
    degraded_with_payload = IntelDim(
        status="degraded",
        root_cause={"event": "旧根因", "mechanism": "旧机制", "magnitude": "正面利好", "persistence": "中期"},
    )
    assert _qualitative_evidence_pack({"intel": degraded_with_payload}) == {}
    intel = IntelDim(
        status="ok",
        root_cause={"event": "订单回暖", "mechanism": "下游补库", "magnitude": "正面利好", "persistence": "中期"},
    )
    business = BusinessDim(status="ok", narrative="成本曲线位于行业左侧。")
    pack = _qualitative_evidence_pack({**core, "intel": intel, "business": business})
    assert pack.get("industry_space") and pack.get("competitive_evolution")


class _DeterministicResearchAdapter:
    """既有 LLM 适配缝（call_with_tools/call_text → LLMResponse）上的确定性替身。

    零网络零付费：按真实提示词内容分派固定完成数据（业务/情报/资金研究 JSON、
    维度叙述、补充评分、终读结论、经理三波）；构建器/解析/评分/护栏/状态位全部
    走真实代码路径；未预期的 LLM 消费直接失败，防止静默旁路。
    """

    @staticmethod
    def _usage() -> Dict[str, int]:
        return {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}

    def _explore_content(self, user: str) -> str:
        if "业务画像" in user:
            return json.dumps(_BUSINESS_RESEARCH, ensure_ascii=False)
        if "情报收集" in user:
            return json.dumps(_INTEL_RESEARCH, ensure_ascii=False)
        if "资金" in user:
            return json.dumps(_CAPITAL_RESEARCH, ensure_ascii=False)
        raise AssertionError(f"未预期的探索研究消费: {user[:120]!r}")

    def _text_content(self, system: str) -> str:
        if "主笔基金经理" in system:
            if '"title_angle"' in system:
                return json.dumps(_MANAGER_WAVE_A, ensure_ascii=False)
            if '"thesis_points"' in system:
                return json.dumps(_MANAGER_WAVE_B, ensure_ascii=False)
            return json.dumps(_MANAGER_WAVE_C, ensure_ascii=False)
        if "投研评分员" in system:
            ids = re.findall(r"'([a-z_]+)'", system[system.rfind("["):])
            return json.dumps(
                {"indicators": [
                    {"id": ind, "score": 60, "basis_sentence": "依据输入事实给出的保守观察判断"}
                    for ind in ids
                ]},
                ensure_ascii=False,
            )
        if "分区叙述作者" in system:
            return "近期价格重心缓慢上移，波动区间收敛，成交温和，趋势口径以已披露日线数据为准。"
        if "中文投资结论" in system:
            return (
                "综合结论：行动保持观察与条件跟踪，不以高分或资金流入推升行动等级。"
                "基本面组件完整与主力资金近端流入是最关键论据，最大风险是上游原料成本上行。"
                "下一步关注年报披露与订单落地数据。"
            )
        raise AssertionError(f"未预期的文本消费: {system[:120]!r}")

    def call_with_tools(
        self, messages: List[Dict[str, Any]], tool_decls: List[Dict[str, Any]],
        timeout: Optional[float] = None,
    ) -> LLMResponse:
        user = "\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "user")
        return LLMResponse(
            content=self._explore_content(user), provider="fixture", model="fixture",
            finish_reason="stop", usage=self._usage(),
        )

    def call_text(
        self, messages: List[Dict[str, Any]], *, provider: Optional[str] = None,
        temperature: Optional[float] = None, max_tokens: Optional[float] = None,
        timeout: Optional[float] = None,
    ) -> LLMResponse:
        system = str(next((m.get("content") or "") for m in messages if m.get("role") == "system"))
        return LLMResponse(
            content=self._text_content(system), provider="fixture", model="fixture",
            finish_reason="stop", usage=self._usage(),
        )


# 确定性研究完成数据（LLM 适配缝返回的固定完成内容；不含未取得的数字）。
_BUSINESS_RESEARCH = {
    "business_model": "深耕光通信涂覆材料的配方与工艺一体化生产商",
    "main_products": "光纤涂覆材料与显影剂等电子化学品，覆盖光通信与半导体两条线",
    "competitive_position": "国内光纤涂覆材料主要供应商之一，与进口品牌正面竞争",
    "vs_market_leader": "中端市场具备成本与服务响应优势（inferred，依据为公开行业资料）",
    "data_gaps": ["分产品收入结构未披露"],
    "narrative": "公司在中端市场地位稳固，成本曲线位于行业左侧，产品线向半导体材料延伸。",
}
_INTEL_RESEARCH = {
    "sentiment_summary": "公司公告与行业新闻中性偏正面",
    "earnings_outlook": "订单回暖但成本压力仍在",
    "risk_alerts": ["上游原料价格波动", "客户集中度较高"],
    "positive_catalysts": ["下游补库周期", "新产品验证进展"],
    "latest_news": "公司披露订单回暖公告",
    "unverified_count": 1,
    "evidence_items": [
        {"evidence": "订单回暖公告（news）", "strength": "weak_positive", "lr": 1.3, "date": "2026-10-20"}
    ],
    "root_cause": {
        "event": "材料订单回暖", "mechanism": "下游补库带动出货",
        "magnitude": "正面利好", "persistence": "中期",
    },
    "event_calendar": [
        {
            "event": "年报披露", "date": "2027-04",
            "outcomes": ["超预期", "符合预期"],
            "plans": ["跟踪披露的订单与出货数据", "对照原料披露价格复核毛利"],
        }
    ],
}
_CAPITAL_RESEARCH = {
    "flow_score": 70,
    "narrative": "主力资金近端持续净流入，与消息面订单回暖的根因一致；单源数据未独立核验。",
}
# 经理三波固定完成内容（非估值口径：无胜率猜值、无目标价、无交易动作词）。
_MANAGER_WAVE_A = {
    "title_angle": "无目标价下的资金观察",
    "executive_summary": (
        "本研究在未取得可追溯目标价的口径下完成：基本面组件完整，主力资金近端持续流入，"
        "消息面根因为订单回暖。结论为观察与条件跟踪，不以高分或资金流入推升行动等级，"
        "估值区间全部留空。后续验证条件见各维度与作战计划。"
    ),
    "section_titles": {
        "investment": "行动保持观察的依据",
        "market": "订单回暖驱动的盘面结构",
        "worldview": "补库周期的三种走向",
        "valuation": "估值锚从哪里来",
        "business": "业务画像与竞争地位",
        "intel": "消息面详析",
        "capital": "资金面分析",
        "technical": "技术面分析",
        "sentiment": "情绪面分析",
        "us_china": "中美竞争分析",
    },
    "dim_verdicts": {
        "基本面": "财务组件完整，健康度中等",
        "消息面": "订单回暖且风险可列举",
        "资金面": "主力资金近端持续流入",
        "情绪面": "机构观点未取得，如实披露",
        "技术面": "未纳入本轮研究",
        "宏观": "未纳入本轮研究",
    },
}
_MANAGER_WAVE_B = {
    "section_narratives": {
        "investment": "研究在无目标价口径下完成，行动保持观察与条件跟踪。六维与贝叶斯结果仅作观察条件引用，不推升行动等级。后续以订单落地与资金连续性作为主要验证事实。",
        "market": "盘面结构由订单回暖驱动，机制为下游补库带动出货。资金近端流入与消息面根因一致。量级与持续性以已披露口径为准。",
        "worldview": "第一种走向是订单持续落地带动出货上行；第二种是补库结束后回归平稳；第三种是原料成本上行压缩利润。三种走向均以已披露数据验证。",
        "valuation": "本报告未取得可追溯目标价，采用非估值分析：不给目标价、预期收益或估值驱动结论；估值相关区间全部留空。",
    },
    "thesis_points": [
        {"header": "无目标价的观察研究", "body": "本研究在未取得可追溯目标价的口径下完成，估值区间留空，行动保持观察与条件跟踪，不以高分推升行动等级，验证条件见作战计划。"},
        {"header": "资金近端持续流入", "body": "资金面显示主力资金近端持续流入，与消息面订单回暖的根因一致；连续性以已披露口径为准，单源数据未独立核验。"},
        {"header": "验证条件已经明确", "body": "后续以订单落地、成本走势与资金连续性为验证事实；任何走向切换都以已披露数据为准，不依赖假设推演。"},
    ],
    "risk_matrix": [
        {"risk": "上游原料价格波动", "impact": "成本上行压缩毛利", "trigger": "原料披露价格高于已披露区间"},
        {"risk": "客户集中度较高", "impact": "单一大客户波动放大业绩弹性", "trigger": "披露的客户结构出现变化"},
    ],
    "money_paths": [
        {"name": "财报验证驱动", "win_rate": "不提供", "playbook": "以披露的订单落地与成本数据验证判断，操作条件只引用规则作战计划。"}
    ],
    "scenario_triggers": {
        "optimistic": "订单持续落地且原料成本稳定",
        "neutral": "补库平稳、出货平稳",
        "pessimistic": "原料成本上行压缩利润",
    },
}
_MANAGER_WAVE_C = {
    "section_narratives": {
        "business": "公司产品线覆盖光通信与半导体两条线，中端市场地位稳固。与海外品牌相比具备成本与响应优势，分产品收入结构未披露，待定期报告补齐。",
        "intel": "消息面根因为订单回暖，机制是下游补库；风险集中在原料价格与客户集中度。年报披露为下一验证节点。",
        "capital": "主力资金近端持续净流入，窗口截至已披露交易日。单源数据未独立核验，连续性以后续披露为准。",
    },
}

# 垂直链维度子集（含核心 fundamental/capital 与真实业务/情报研究；其余当前维度
# 按 skipped 语义占位。完整 18 项付费验收归 root 后续，本测试只覆盖 B5 契约）。
_NO_TARGET_RESEARCH_DIMS = sorted({
    "phase", "history", "data", "fundamental", "capital", "intel", "business",
    "sector", "six_dim", "bayesian", "scenarios", "conclusion", "plan", "signal",
})


def test_no_target_real_orchestration_service_vertical(
    tmp_path, monkeypatch: pytest.MonkeyPatch, choice_transport: Any,
) -> None:
    """B5 验收证据（真实垂直链，仅外部边界受控，零网络零真实模型）。

    正例：Choice 目标价查询成功、契约有效且返回无覆盖（三个 RATINGTARGETPRICE
    字段为 None），业务/情报研究经真实 agent 循环+Pydantic 解析完成，基本面/资金
    经真实 manager 资格链完成，情景/结论/信号经真实构建+护栏+经理三波+渲染完成；
    服务落盘 markdown/子报告/dims.json/SQLite，API detail/download/dims 一致。
    断言 null 目标价/EV/价值区间、实际定性内容、醒目范围说明、观察/中性/低、
    三项不再仅因无覆盖进入 missing_layers、无胜率猜值。
    负例：目标价查询真实失败（quota_exhausted）时三项 degraded，并在持久化
    结果中保持可见，不伪造完成。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.config import Config
    from src.storage import DatabaseManager
    from src.services import deep_research_service as service_module
    from api.v1.endpoints.deep_research import router

    cfg = choice_transport
    cfg.deep_research_engine = "dual_track"
    cfg.deep_research_timeout_seconds = 60
    cfg.enable_ifind_mcp = False
    monkeypatch.setattr(Config, "_instance", cfg)
    monkeypatch.setattr(service_module, "_get_dual_track_adapter", lambda: _DeterministicResearchAdapter())
    monkeypatch.setattr(service_module, "_DEEP_RESEARCH_DIR", tmp_path)
    monkeypatch.setattr(DatabaseManager, "_instance", None)
    database = DatabaseManager(f"sqlite:///{tmp_path / 'reports.sqlite'}")
    monkeypatch.setattr(service_module, "get_db", lambda: database)

    from src.agent.deep_research import orchestrator
    from src.deep_research_dims.context import _safe_fundamental

    today = date.today().isoformat()

    def _build_context(*args: Any, **kwargs: Any) -> SharedContext:
        # 真实阶段0：基本面/资金/目标价经真实 manager 与 Choice 传输缝取得
        # （force_refresh 令 24h 财务快照每次真实重取）；行情/日线用确定性夹具
        # （与既有执行测试同一缝）。
        ctx = SharedContext("300398", "飞凯材料", f"{today}T15:00:00", quote={"price": 20.0})
        ctx.history = [
            {"date": f"2026-09-{day:02d}", "open": 10, "high": 11, "low": 9, "close": 10.0 + day * 0.05, "volume": 1000}
            for day in range(1, 29)
        ]
        ctx.fundamental = _safe_fundamental("300398", ctx)
        return ctx

    monkeypatch.setattr(orchestrator, "build_shared_context", _build_context)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")

    # 公共历史传输缝（six_dim 周线过滤的既有有界补取，DB 优先同签名）提供
    # 确定性日线，替代 efinance 回退出站。
    import pandas as pd
    from src.services import history_loader

    def _deterministic_history(
        stock_code: str, days: int = 60, target_date: Any = None, min_records: Optional[int] = None,
    ) -> Any:
        rows = 1200
        idx = pd.bdate_range(end=today, periods=rows)
        return pd.DataFrame({
            "date": idx.strftime("%Y-%m-%d"),
            "open": [10.0] * rows,
            "high": [11.0] * rows,
            "low": [9.0] * rows,
            "close": [10.0 + i * 0.01 for i in range(rows)],
            "volume": [1000] * rows,
        }), "fixture_db"

    monkeypatch.setattr(history_loader, "load_history_df", _deterministic_history)

    # Choice 目标价缝（正例）：查询成功、契约有效但窗口无覆盖——仅把三个
    # RATINGTARGETPRICE 原生值置 None，其余合格财务/资金原生值原样保留。
    from data_provider.choice_adapter import ChoiceAdapter

    base_transport = ChoiceAdapter._native_request

    def no_target_transport(method: str, args: Any, options: Any, deadline: Any) -> Dict[str, Any]:
        raw = cast(Dict[str, Any], base_transport(method, args, options, deadline))
        if method == "css" and isinstance(raw.get("data"), dict):
            indicators = raw.get("indicators") or []
            for field in ("RATINGTARGETPRICE", "RATINGTARGETPRICEMIN", "RATINGTARGETPRICEMAX"):
                if field in indicators:
                    for values in raw["data"].values():
                        values[indicators.index(field)] = None
        return raw

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(no_target_transport))
    service = service_module.deep_research_service
    try:
        # —— 正例：成功空覆盖 + 真实完成的同报告研究 ——
        result = service.generate_report("300398", "飞凯材料", dims=_NO_TARGET_RESEARCH_DIMS, force_refresh=True)
        rid = result["report_id"]
        assert rid
        assert result["status"] == "success", (result["status"], result["error"])
        assert not {"scenarios", "conclusion", "signal"} & set(result["missing_layers"]), result["missing_layers"]
        dims = result["dimensions"]
        scenarios = dims["scenarios"]
        assert scenarios["status"] == "ok", scenarios["degraded_reason"]
        assert scenarios["valuation_basis"] == "none"
        assert scenarios["target_price"] is None and scenarios["expected_value"] is None
        assert scenarios["analyst_target"] is None
        assert scenarios["scenarios"]["scenarios"] == [], "无概率不创建结构化情景"
        assert scenarios["scenarios"]["horizons"] == {"horizon_1y": None, "horizon_3y": None, "horizon_5y": None}
        assert scenarios["scenarios"]["industry_space"], "证据包须来自真实完成的情报研究"
        assert scenarios["scenarios"]["competitive_evolution"]
        assert scenarios["scenarios"]["catalysts"] and scenarios["scenarios"]["risks"]
        conclusion = dims["conclusion"]
        assert conclusion["status"] == "ok", conclusion["degraded_reason"]
        assert conclusion["conclusion"]["action"] == "观察", "缺估值时行动保持观察/条件跟踪"
        assert conclusion["conclusion"]["value_range_1y"] is None
        assert conclusion["conclusion"]["value_range_5y"] is None
        signal = dims["signal"]
        assert signal["status"] == "ok", signal["degraded_reason"]
        assert signal["rating"] == "中性" and signal["confidence"] == "低"
        assert signal["signal_type"] == "非估值观察"
        assert signal["target_price"] is None and signal["expected_value"] is None
        assert dims["six_dim"]["framework"]["dimensions"], "评分真实执行并产生维度明细"
        bad_events = [e for e in result["guardrail_events"] if e["dim"] in {"scenarios", "conclusion", "signal"}]
        assert not bad_events, "非估值模式不得触发概率/锚点类护栏误报"

        markdown = result["markdown"]
        assert "非估值分析" in markdown and "未取得可追溯目标价" in markdown, "范围说明醒目"
        assert "股权架构与高管" not in markdown, "退役维度不得出现在新报告"
        assert "无目标价下的资金观察" in markdown, "经理波A标题真实渲染"
        assert "以订单落地与资金连续性作为主要验证事实" in markdown, "经理波B主干叙述不得隐藏"
        assert "胜率 不提供" in markdown, "不请求/不输出胜率猜值"
        assert "约50%" not in markdown
        assert "最关键论据" in markdown, "终读结论真实渲染"
        assert result["available_dim_reports"], "实际标记章节应落盘子报告"
        for dim in result["available_dim_reports"]:
            assert (tmp_path / f"{rid}_dim_{dim}.md").exists()

        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as client:
            detail = client.get(f"/reports/{rid}")
            assert detail.status_code == 200
            data = detail.json()["data"]
            assert data["markdown"] == markdown
            assert data["dimensions"]["scenarios"]["status"] == "ok"
            assert data["dimensions"]["signal"]["status"] == "ok"
            assert data["available_dim_reports"] == result["available_dim_reports"]
            download = client.get(f"/reports/{rid}/markdown")
            assert download.status_code == 200
            assert download.text == markdown
            dims_api = client.get(f"/reports/{rid}/dims")
            assert dims_api.status_code == 200
            assert dims_api.json()["data"]["dimensions"]["conclusion"]["status"] == "ok"

        # —— 负例：目标价查询真实失败 → 三项 degraded 且持久化可见 ——
        from data_provider.cross_source_validator import SourceReadError

        def targets_denied(method: str, args: Any, options: Any, deadline: Any) -> Dict[str, Any]:
            if method == "css" and "RATINGTARGETPRICE" in str(args[1]):
                raise SourceReadError("choice", "quota_exhausted")
            return no_target_transport(method, args, options, deadline)

        monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(targets_denied))
        negative = service.generate_report("300398", "飞凯材料", dims=_NO_TARGET_RESEARCH_DIMS, force_refresh=True)
        rid2 = negative["report_id"]
        assert rid2 and rid2 != rid
        assert {"scenarios", "conclusion", "signal"} <= set(negative["missing_layers"])
        assert negative["status"] == "partial"
        neg_dims = negative["dimensions"]
        assert neg_dims["scenarios"]["status"] == "degraded"
        assert "额度" in (neg_dims["scenarios"]["degraded_reason"] or "")
        assert neg_dims["conclusion"]["status"] == "degraded"
        assert neg_dims["signal"]["status"] == "degraded"
        neg_md = negative["markdown"]
        assert "调用额度受限" in neg_md, "真实失败原因渲染可见"
        assert "财务估值或资金依据不完整" in neg_md, "证据链不完整说明可见"
        assert "无目标价下的资金观察" not in neg_md, "证据链不完整时经理终稿不伪造"
        with TestClient(app) as client:
            data2 = client.get(f"/reports/{rid2}").json()["data"]
            assert data2["dimensions"]["scenarios"]["status"] == "degraded"
            assert {"scenarios", "conclusion", "signal"} <= set(data2["missing_layers"])
            dims_api2 = client.get(f"/reports/{rid2}/dims")
            assert dims_api2.status_code == 200
            assert dims_api2.json()["data"]["dimensions"]["signal"]["status"] == "degraded"
    finally:
        database._engine.dispose()


def test_render_no_target_report_end_to_end() -> None:
    """渲染单测（非 B5 验收证据）：18 项契约 dims 夹具 + 非估值三项 ok →
    guardrail/经理证据链/渲染一致。真实编排+服务的验收证据见上方
    test_no_target_real_orchestration_service_vertical。"""
    from datetime import datetime

    from src.deep_research_dims.guardrail import apply_guardrails
    from src.deep_research_dims.manager_writeup import (
        INCOMPLETE_EVIDENCE_NOTE,
        manager_evidence_complete,
    )
    from src.deep_research_dims.render import build_view, render_markdown, validate_structure

    dims = _full_dims_without_ownership()
    payloads = {d: m.model_dump() for d, m in dims.items()}
    events, adjusted = apply_guardrails(payloads)
    # 非估值模式不触发概率/锚点类误报
    assert not [e for e in events if e.dim in {"scenarios", "conclusion", "signal"}]
    from src.schemas.deep_research_dims import parse_dim

    dims = {key: parse_dim(key, value) for key, value in adjusted.items()}
    complete = manager_evidence_complete(dims)
    assert complete, "非估值三项 ok 后经理证据链完整"
    view = build_view(
        "飞凯材料", "300398", datetime.now().isoformat(timespec="seconds"),
        [], dims, events, report_id="300398_202610291500",
        final_conclusion="终读结论可用。" if complete else INCOMPLETE_EVIDENCE_NOTE,
        manager={},
    )
    markdown = render_markdown(view)
    assert "股权架构与高管" not in markdown, "退役维度不得出现在新报告"
    assert "非估值" in markdown, "范围说明必须醒目"
    assert "终读结论可用" in markdown, "终稿不得因 target_price 为空隐藏"
    assert validate_structure(markdown) == []


def _full_dims_without_ownership() -> Dict[str, Any]:
    """渲染夹具（非验收证据）：全部 18 项契约维度，非估值三项 ok，
    其余为最小 ok 实例/规则装配；仅覆盖模板与结构校验，不证明研究真实完成。"""
    from src.deep_research_dims.bayesian_dim import build_bayesian_dim
    from src.deep_research_dims.conclusion_dim import build_conclusion_dim
    from src.deep_research_dims.data_dim import build_data_dim
    from src.deep_research_dims.history_dim import build_history_dim
    from src.deep_research_dims.phase_dim import build_phase_dim
    from src.deep_research_dims.plan_dim import build_plan_dim
    from src.deep_research_dims.sector_dim import build_sector_dim
    from src.deep_research_dims.signal_dim import build_signal_dim
    from src.deep_research_dims.six_dim import build_six_dim
    from src.schemas.deep_research_dims import (
        BusinessDim,
        CapitalDim,
        FinancialDataQuality,
        FundamentalDim,
        IntelDim,
        SentimentDim,
        SupplyChainDim,
        TechnicalDim,
        UsChinaDim,
    )

    ctx = SharedContext(stock_code="300398", stock_name="飞凯材料", as_of="2026-10-29T15:00:00")
    ctx.quote = {"price": 20.0}
    ctx.history = [
        {"date": f"2026-09-{day:02d}", "open": 10, "high": 11, "low": 9, "close": 10.0 + day * 0.05, "volume": 1000}
        for day in range(1, 29)
    ]
    ctx.fundamental = {"pe_ttm": 40.0, "pb": 3.0, "research": _choice_target_research()}
    dims: Dict[str, Any] = {}
    dims["phase"] = build_phase_dim(ctx)
    dims["history"] = build_history_dim(ctx)
    # 财务维度用显式 ok 契约实例（本测试聚焦非估值三项与渲染一致性；
    # build_fundamental_dim 的字段资格链由 test_deep_research_dims 覆盖）。
    dims["fundamental"] = FundamentalDim(
        status="ok",
        data_quality=FinancialDataQuality(state="complete", obtained_count=6, rule_usable_count=6, health_components=5),
        health_score=62.0,
        narrative="财务组件完整评估：评分 62（参与 5/5 组件）。",
    )
    dims["data"] = build_data_dim(ctx)
    dims["sector"] = build_sector_dim(ctx)
    dims["six_dim"] = build_six_dim(ctx, dims["data"].model_dump(), None)
    dims["bayesian"] = build_bayesian_dim(dims["six_dim"].model_dump(), [])
    dims["scenarios"] = build_scenarios_dim(ctx, 20.0, _evidence_pack(), None)
    dims["conclusion"] = build_conclusion_dim(ctx, dims["six_dim"].model_dump(), dims["bayesian"].model_dump(), dims["scenarios"].model_dump())
    dims["plan"] = build_plan_dim(ctx, dims["data"].model_dump(), "观察")
    dims["signal"] = build_signal_dim(ctx, dims["conclusion"].model_dump(), dims["scenarios"].model_dump())

    dims["intel"] = IntelDim(status="ok", narrative="消息面观察。")
    dims["supply_chain"] = SupplyChainDim(status="ok", narrative="产业链观察。")
    dims["capital"] = CapitalDim(status="ok", flow_summary="资金观察。", narrative="资金面观察。")
    dims["technical"] = TechnicalDim(status="ok", narrative="技术面观察。")
    dims["sentiment"] = SentimentDim(status="ok", narrative="情绪面观察。")
    dims["us_china"] = UsChinaDim(status="ok", narrative="中美链观察。")
    dims["business"] = BusinessDim(status="ok", narrative="业务画像观察。")
    expected = {
        "signal", "data", "intel", "plan", "phase", "history", "six_dim", "bayesian",
        "conclusion", "supply_chain", "scenarios", "fundamental", "sector", "technical",
        "capital", "sentiment", "us_china", "business",
    }
    assert set(dims) == expected and "ownership" not in dims
    return dims
