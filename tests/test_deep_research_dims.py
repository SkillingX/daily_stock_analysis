# -*- coding: utf-8 -*-
"""双轨引擎单元测试：契约 / LR 标定 / 概率归一化 / 护栏规则 / 渲染结构。

全部离线（无网络、无 LLM），覆盖需求文档 §5.3 护栏表与 §5.4 标定表的核心路径。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from icontract import ViolationError

from src.deep_research_dims.bayesian_dim import build_bayesian_dim, calibrate_lr
from src.deep_research_dims.context import SharedContext
from src.deep_research_dims.data_dim import build_data_dim
from src.deep_research_dims.guardrail import (
    apply_guardrails,
    rule1_capital_outflow,
    rule2_negative_edge,
    rule3_weak_six_dim,
    rule7_signal_conclusion_consistency,
    rule9_evidence_freshness,
)
from src.deep_research_dims.plan_dim import compute_atr14
from src.deep_research_dims.render import build_view, render_markdown, validate_structure
from src.deep_research_dims.scenarios_dim import (
    build_scenarios_dim,
    expected_value,
    normalize_probabilities,
)
from src.schemas.bayesian_framework import EvidenceItem
from src.schemas.deep_research_dims import (
    BayesianDim,
    ConclusionDim,
    DataDim,
    FundamentalDim,
    SectorDim,
    TechnicalDim,
    CapitalDim,
    SentimentDim,
    OwnershipDim,
    UsChinaDim,
    BusinessDim,
    DIM_IDS,
    HistoryDim,
    IntelDim,
    parse_dim,
    PhaseDim,
    PlanDim,
    ScenariosDim,
    SignalDim,
    SixDimDim,
    SupplyChainDim,
)
from src.schemas.value_scenarios import Scenario


def _fake_history(n: int = 30, base: float = 10.0) -> list[dict]:
    rows = []
    for i in range(n):
        close = base + i * 0.1
        rows.append(
            {
                "date": str(date(2026, 8, 1) + timedelta(days=i)),
                "open": close - 0.05,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
                "volume": 10000 + i * 100,
            }
        )
    return rows


def _ctx(**overrides) -> SharedContext:
    ctx = SharedContext(stock_code="600519", stock_name="贵州茅台", as_of="2026-09-28T10:00:00")
    ctx.history = _fake_history()
    ctx.quote = {"price": 13.0}
    ctx.fundamental = {"pe_ttm": 25.0, "pb": 5.0}
    for k, v in overrides.items():
        setattr(ctx, k, v)
    return ctx


# ---------------------------------------------------------------------------
# LR 标定表（§5.4-2）
# ---------------------------------------------------------------------------


class TestLRCalibration:
    def test_in_range_passthrough(self):
        lr, clamped = calibrate_lr("strong_positive", 3.0)
        assert lr == 3.0 and not clamped

    def test_out_of_range_clamps_to_midpoint(self):
        lr, clamped = calibrate_lr("weak_positive", 9.9)
        assert clamped and 1.2 <= lr < 2.0

    def test_none_uses_midpoint(self):
        lr, clamped = calibrate_lr("neutral", None)
        assert clamped and 0.9 <= lr <= 1.1

    def test_invalid_strength_rejected(self):
        with pytest.raises(ViolationError):
            calibrate_lr("super_positive", 2.0)


# ---------------------------------------------------------------------------
# 概率归一化与 EV（护栏规则 4 底层）
# ---------------------------------------------------------------------------


class TestScenarioMath:
    def test_normalize_sum_exactly_one(self):
        scenarios = [
            Scenario(type="optimistic", probability=0.333),
            Scenario(type="neutral", probability=0.333),
            Scenario(type="pessimistic", probability=0.334),
        ]
        normalized = normalize_probabilities(scenarios)
        assert sum(s.probability for s in normalized) == pytest.approx(1.0, abs=1e-9)

    def test_expected_value_weighted(self):
        scenarios = [
            Scenario(type="optimistic", probability=0.5, value_anchor=20.0),
            Scenario(type="pessimistic", probability=0.5, value_anchor=10.0),
        ]
        assert expected_value(scenarios) == 15.0

    def test_expected_value_ignores_missing_anchor(self):
        scenarios = [Scenario(type="neutral", probability=1.0, value_anchor=None)]
        assert expected_value(scenarios) is None


# ---------------------------------------------------------------------------
# 维度后台代码
# ---------------------------------------------------------------------------


class TestDataDim:
    def test_build_with_history(self):
        dim = build_data_dim(_ctx())
        assert dim.status == "ok"
        p = dim.perspective
        assert p is not None
        assert p.price_position.current_price == 13.0
        assert p.trend_status.ma_alignment in ("多头排列", "纠缠", "空头排列")
        assert dim.ma250_deviation_pct is None  # 30 条数据不足 250 日均线

    def test_degraded_without_history(self):
        dim = build_data_dim(_ctx(history=[]))
        assert dim.status == "degraded"


class TestAtr:
    def test_atr_positive(self):
        atr = compute_atr14(_fake_history(30))
        assert atr > 0

    def test_atr_requires_15(self):
        with pytest.raises(ViolationError):
            compute_atr14(_fake_history(5))


class TestValuationBasis:
    def test_loss_maker_switches_to_pb(self):
        ctx = _ctx()
        ctx.fundamental = {"pe_ttm": -3.0}
        dim = build_scenarios_dim(ctx, current_price=13.0)
        assert dim.valuation_basis == "PB"

    def test_profit_maker_uses_pe(self):
        dim = build_scenarios_dim(_ctx(), current_price=13.0)
        assert dim.valuation_basis == "PE_TTM"
        assert dim.probability_sum == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 护栏规则（§5.3）
# ---------------------------------------------------------------------------


def _bayesian_payload(edge: float = 0.1, evidence_dates: list[str] | None = None) -> dict:
    evidence = [
        EvidenceItem(
            evidence=f"e{i}",
            strength="weak_positive",
            lr=1.5,
            posterior_p=0.5,
            date=d,
        )
        for i, d in enumerate(evidence_dates or [str(date.today())] * 3)
    ]
    from src.schemas.bayesian_framework import BayesianFramework

    return BayesianDim(
        bayesian=BayesianFramework(
            prior_p=0.6,
            market_implied_p=0.5,
            edge=edge,
            posterior_p=0.65,
            position_suggestion="3-5%",
            evidence_log=evidence,
        )
    ).model_dump()


class TestGuardrailRules:
    def test_rule2_downgrades_negative_edge(self):
        conclusion = ConclusionDim(
            conclusion={
                "prior_p": 0.6,
                "market_implied_p": 0.5,
                "edge": -0.1,
                "position": "建仓",
                "action": "建仓",
            }
        ).model_dump()
        events = rule2_negative_edge(conclusion)
        assert len(events) == 1 and events[0].rule_id == "GR2_negative_edge"

    def test_rule3_downgrades_weak_six_dim(self):
        conclusion = ConclusionDim(
            conclusion={
                "prior_p": 0.6,
                "market_implied_p": 0.5,
                "edge": 0.2,
                "position": "加仓",
                "action": "加仓",
            }
        ).model_dump()
        six = SixDimDim(
            framework={
                "dimension_total": 40.0,
                "dimensions": [],
                "scoring_version": "v1",
            }
        ).model_dump()
        events = rule3_weak_six_dim(conclusion, six)
        assert len(events) == 1

    def test_rule9_caps_stale_evidence(self):
        stale = _bayesian_payload(evidence_dates=["2026-01-01"] * 3)
        events = rule9_evidence_freshness(stale)
        assert any(e.rule_id == "GR9_evidence_freshness" for e in events)

    def test_rule9_passes_with_fresh_evidence(self):
        events = rule9_evidence_freshness(_bayesian_payload())
        assert events == []

    def test_rule7_flags_conflict(self):
        signal = SignalDim(rating="买入").model_dump()
        conclusion = ConclusionDim(
            conclusion={
                "prior_p": 0.6,
                "market_implied_p": 0.5,
                "edge": 0.2,
                "position": "减仓",
                "action": "减仓",
            }
        ).model_dump()
        events = rule7_signal_conclusion_consistency(signal, conclusion)
        assert len(events) == 1

    def test_apply_guardrails_override_conclusion(self):
        payloads = {
            "conclusion": ConclusionDim(
                conclusion={
                    "prior_p": 0.6,
                    "market_implied_p": 0.7,
                    "edge": -0.1,
                    "position": "建仓",
                    "action": "建仓",
                }
            ).model_dump(),
            "signal": SignalDim(rating="中性").model_dump(),
            "six_dim": SixDimDim(
                framework={"dimension_total": 80.0, "dimensions": [], "scoring_version": "v1"}
            ).model_dump(),
            "intel": IntelDim().model_dump(),
            "bayesian": _bayesian_payload(edge=-0.1),
            "scenarios": ScenariosDim(probability_sum=1.0).model_dump(),
        }
        events, adjusted = apply_guardrails(payloads)
        assert any(e.rule_id == "GR2_negative_edge" for e in events)
        assert adjusted["conclusion"]["conclusion"]["action"] == "观察"

    def test_rule1_single_authority_no_double_downgrade(self):
        """A1 回归：信号构建不预降级，GR1 只处罚一次（买入 → 增持，而非 → 中性）。"""
        from src.deep_research_dims.signal_dim import build_signal_dim

        ctx = _ctx()
        conclusion = ConclusionDim(
            conclusion={
                "prior_p": 0.7,
                "market_implied_p": 0.5,
                "edge": 0.2,
                "position": "建仓",
                "action": "建仓",
            }
        ).model_dump()
        scenarios = ScenariosDim(
            probability_sum=1.0, expected_value=20.0
        ).model_dump()
        signal = build_signal_dim(ctx, conclusion, scenarios)
        assert signal.rating == "买入"  # 构建期不降级（上行 20/13-1>20%）

        intel = IntelDim(
            intelligence={
                "risk_alerts": ["主力资金连续 5 日净流出（news）"],
            }
        ).model_dump()
        events = rule1_capital_outflow(signal.model_dump(), intel)
        assert len(events) == 1
        payloads = {
            "signal": signal.model_dump(),
            "intel": intel,
            "conclusion": conclusion,
            "scenarios": scenarios,
            "six_dim": SixDimDim(
                framework={"dimension_total": 80.0, "dimensions": [], "scoring_version": "v1"}
            ).model_dump(),
            "bayesian": _bayesian_payload(),
        }
        _events, adjusted = apply_guardrails(payloads)
        assert adjusted["signal"]["rating"] == "增持"  # 一次降级到位

    def test_rule7_caps_to_action_rating_cap(self):
        """A2 回归：买入 vs 行动「观察」→ 评级收敛到「中性」（上限映射），非只降一级。"""
        signal = SignalDim(rating="买入").model_dump()
        conclusion = ConclusionDim(
            conclusion={
                "prior_p": 0.6,
                "market_implied_p": 0.5,
                "edge": 0.2,
                "position": "观察",
                "action": "观察",
            }
        ).model_dump()
        events = rule7_signal_conclusion_consistency(signal, conclusion)
        assert len(events) == 1
        payloads = {
            "signal": signal,
            "conclusion": conclusion,
            "intel": IntelDim().model_dump(),
            "six_dim": SixDimDim(
                framework={"dimension_total": 80.0, "dimensions": [], "scoring_version": "v1"}
            ).model_dump(),
            "bayesian": _bayesian_payload(),
            "scenarios": ScenariosDim(probability_sum=1.0).model_dump(),
        }
        _events, adjusted = apply_guardrails(payloads)
        assert adjusted["signal"]["rating"] == "中性"

    def test_append_override_note(self):
        from src.deep_research_dims.guardrail import append_override_note

        payload = {"rationale": "原论证建仓。", "narrative": ""}
        updated = append_override_note(payload, "行动", "建仓", "观察")
        assert "由「建仓」修订为「观察」" in updated["rationale"]
        unchanged = append_override_note(payload, "行动", "建仓", "建仓")
        assert unchanged["rationale"] == "原论证建仓。"


# ---------------------------------------------------------------------------
# 行业基率表（§5.4-3：market_implied_p 机器锚点）
# ---------------------------------------------------------------------------


class TestDimSubset:
    def test_expand_closure_full_when_none(self):
        from src.agent.deep_research.orchestrator import expand_dim_selection

        assert expand_dim_selection(None) == set(DIM_IDS)
        assert expand_dim_selection(set()) == set(DIM_IDS)

    def test_expand_adds_dependencies(self):
        from src.agent.deep_research.orchestrator import expand_dim_selection

        # 只选信号 → 自动补 结论/情景/贝叶斯/六维（依赖闭包）
        selected = expand_dim_selection({"signal"})
        assert {"signal", "conclusion", "scenarios", "bayesian", "six_dim"} <= selected

    def test_expand_plan_pulls_data_and_bayesian(self):
        from src.agent.deep_research.orchestrator import expand_dim_selection

        selected = expand_dim_selection({"plan"})
        assert {"plan", "data", "bayesian", "six_dim"} <= selected

    def test_run_dual_track_subset_offline(self, monkeypatch, tmp_path):
        """离线冒烟：只选 2 个纯规则维度，其余 9 个 skipped。"""
        from datetime import date as _date
        import datetime as _dt
        from src.agent.deep_research import orchestrator as orch
        from src.deep_research_dims import dim_cache
        from src.deep_research_dims.context import SharedContext

        monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path))
        ctx = SharedContext(stock_code="600519", stock_name="贵州茅台", as_of="2026-09-29T10:00:00")
        ctx.quote = {"price": 13.0}
        ctx.history = [
            {"date": str(_date(2026, 8, 1) + _dt.timedelta(days=i)),
             "open": 10 + i * 0.1, "high": 10.3 + i * 0.1, "low": 9.7 + i * 0.1,
             "close": 10 + i * 0.1, "volume": 10000}
            for i in range(40)
        ]
        monkeypatch.setattr(orch, "build_shared_context", lambda code, name: ctx)

        result = orch.run_dual_track(
            "600519", "贵州茅台", llm_adapter=None, dims_filter={"phase", "history"}
        )
        assert result.status == "success"
        assert result.dims["phase"].status == "ok"
        assert result.dims["history"].status == "ok"
        skipped = [d for d, m in result.dims.items() if m.status == "skipped"]
        assert set(skipped) == set(DIM_IDS) - {"phase", "history"}
        assert result.quality_score == 100  # 只统计执行维度
        assert "未选择生成" in result.markdown


class TestIndustryBaseRate:
    def test_keyword_hit(self):
        from src.deep_research_dims.industry_base_rate import lookup_base_rate

        rate, basis = lookup_base_rate("中游酱香型白酒酿造商（核心环节）")
        assert rate == 0.55 and basis == "industry_base_rate:白酒"

    def test_miss_falls_back_neutral(self):
        from src.deep_research_dims.industry_base_rate import lookup_base_rate

        rate, basis = lookup_base_rate("完全未知的行业描述 xyz")
        assert rate == 0.5 and basis == "neutral_default"

    def test_empty_text_neutral(self):
        from src.deep_research_dims.industry_base_rate import lookup_base_rate

        rate, _ = lookup_base_rate("")
        assert rate == 0.5

    def test_l2_uses_injected_market_implied(self):
        """L2 采用编排器注入的行业基率，edge 与 basis 如实反映。"""
        six = SixDimDim(
            framework={"dimension_total": 82.0, "dimensions": [], "scoring_version": "v1"}
        ).model_dump()
        dim = build_bayesian_dim(
            six, [], market_implied_p=0.4, market_implied_basis="industry_base_rate:白酒"
        )
        assert dim.bayesian is not None
        assert dim.bayesian.market_implied_p == 0.4
        assert dim.bayesian.edge > 0  # 先验(82分→~0.73) > 0.4 → 正认知差
        assert dim.market_implied_basis == "industry_base_rate:白酒"


# ---------------------------------------------------------------------------
# 契约解析 + 渲染结构
# ---------------------------------------------------------------------------


class TestReportDayCache:
    def test_date_key_dims_fingerprint(self):
        from src.services.deep_research_service import _date_key

        full = _date_key("600519")
        subset = _date_key("600519", ["conclusion", "six_dim", "bayesian"])
        subset_reordered = _date_key("600519", ["bayesian", "conclusion", "six_dim"])
        assert full != subset
        assert subset == subset_reordered  # 顺序无关

    def test_cache_hit_emits_done_event(self, monkeypatch):
        """远程日缓存命中路径必须推送 done 事件，否则 SSE 端挂起等 90s 看门狗。"""
        from src.services import deep_research_service as svc

        fake_result = {
            "report_id": "600519_202609290900",
            "stock_code": "600519",
            "stock_name": "贵州茅台",
            "status": "success",
            "quality_score": 100,
            "missing_layers": [],
            "markdown": "# 缓存报告",
            "engine": "dual_track",
            "dimensions": {"signal": {"status": "ok"}},
            "guardrail_events": [],
            "error": None,
        }
        monkeypatch.setattr(
            svc, "_report_cache", {_date_key_of("600519"): ("600519_202609290900", 0.0, fake_result)}
        )
        events: list = []
        out = svc.deep_research_service.generate_report(
            raw_code="600519",
            raw_name="贵州茅台",
            progress_callback=lambda e: events.append(e),
        )
        assert out.get("cache_hit") is True
        done = [e for e in events if e.get("type") == "done"]
        assert len(done) == 1, f"缓存命中必须恰好推送 1 个 done，实际: {[e.get('type') for e in events]}"
        assert done[0]["cache_hit"] is True
        assert done[0]["markdown"] == "# 缓存报告"
        assert done[0]["engine"] == "dual_track"
        assert done[0]["report_id"] == "600519_202609290900"


def _date_key_of(code: str) -> str:
    from src.services.deep_research_service import _date_key

    return _date_key(code)


class TestDimCache:
    def test_save_load_roundtrip(self, tmp_path, monkeypatch):
        from src.deep_research_dims import dim_cache

        monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path))
        payload = {"dim": "data", "status": "ok", "perspective": None}
        dim_cache.save_cached_dim("600519", "data", payload)
        loaded = dim_cache.load_cached_dim("600519", "data")
        assert loaded == payload

    def test_ttl_expiry(self, tmp_path, monkeypatch):
        from src.deep_research_dims import dim_cache

        monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path))
        dim_cache.save_cached_dim("600519", "data", {"dim": "data", "status": "ok"})
        # 把 saved_at 拨到 25 小时前（data TTL=24h）
        import json
        import os
        from datetime import datetime, timedelta

        path = os.path.join(str(tmp_path), "600519_data.json")
        record = json.loads(open(path, encoding="utf-8").read())
        record["saved_at"] = (datetime.now() - timedelta(hours=25)).isoformat()
        open(path, "w", encoding="utf-8").write(json.dumps(record))
        assert dim_cache.load_cached_dim("600519", "data") is None

    def test_non_cacheable_dim_always_miss(self, tmp_path, monkeypatch):
        from src.deep_research_dims import dim_cache

        monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path))
        # 派生维度 bayesian 不可缓存：写了也读不到
        dim_cache.save_cached_dim("600519", "bayesian", {"dim": "bayesian"})
        assert dim_cache.load_cached_dim("600519", "bayesian") is None

    def test_clear_cache_scoped(self, tmp_path, monkeypatch):
        from src.deep_research_dims import dim_cache

        monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path))
        dim_cache.save_cached_dim("600519", "data", {"a": 1})
        dim_cache.save_cached_dim("000001", "data", {"a": 2})
        assert dim_cache.clear_cache("600519") == 1
        assert dim_cache.load_cached_dim("000001", "data") == {"a": 2}


class TestSnapshotCache:
    """阶段 0 快照缓存（基本面 24h）：命中不重抓，TTL 过期失效，空不缓存。"""

    def test_roundtrip_and_ttl(self, tmp_path, monkeypatch):
        from src.deep_research_dims import dim_cache

        monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path))
        payload = {"pe_ttm": 19.09, "roe": 16.75}
        dim_cache.save_snapshot("stage0_fund_600519", payload)
        assert dim_cache.load_snapshot("stage0_fund_600519", ttl_hours=24) == payload
        assert dim_cache.load_snapshot("stage0_fund_600519", ttl_hours=0) is None

    def test_empty_payload_not_cached(self, tmp_path, monkeypatch):
        from src.deep_research_dims import dim_cache

        monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path))
        dim_cache.save_snapshot("stage0_fund_600519", {})
        assert dim_cache.load_snapshot("stage0_fund_600519", ttl_hours=24) is None

    def test_all_none_fundamental_not_cached(self, monkeypatch, tmp_path):
        """E2E 抓的缓存污染 bug：失败抓取返回全 None dict，曾被固化 24h。"""
        from src.agent.tools import data_tools
        from src.deep_research_dims import context as ctx_mod
        from src.deep_research_dims import dim_cache

        monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path))
        # 关掉 fuyao 兜底：本机 .env 有 key 时会真实 HTTP 请求并成功补值，
        # 使"全 None"前提不成立（测试依赖网络状态的 flaky 根因）。
        monkeypatch.delenv("FUYAO_API_KEY", raising=False)

        class _Fund:
            def get_fundamental_context(self, code):
                return {"valuation": {"data": {}}, "financial": {"data": {}}}

        monkeypatch.setattr(
            data_tools, "_get_fetcher_manager", lambda: _Fund()
        )
        ctx = ctx_mod.SharedContext(stock_code="600519", stock_name="贵州茅台", as_of="x")
        result = ctx_mod._safe_fundamental("600519", ctx)
        assert result["pe_ttm"] is None
        # 全 None 不得写缓存
        import os

        assert not os.listdir(str(tmp_path)), "全 None 结果不应产生缓存文件"

    def test_fundamental_cache_hit_skips_fetch(self, monkeypatch, tmp_path):
        """命中快照时不得触碰 fetcher（monkeypatch 成抛异常即证）。"""
        from src.agent.tools import data_tools
        from src.deep_research_dims import context as ctx_mod
        from src.deep_research_dims import dim_cache

        monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path))
        dim_cache.save_snapshot("stage0_fund_v2_600519", {"pe_ttm": 19.09})

        def _boom(*a, **k):
            raise AssertionError("命中快照不应调用 fetcher")

        # _safe_fundamental 在函数内 from-import，需 patch 源模块属性
        monkeypatch.setattr(data_tools, "_get_fetcher_manager", _boom)
        ctx = ctx_mod.SharedContext(stock_code="600519", stock_name="贵州茅台", as_of="x")
        result = ctx_mod._safe_fundamental("600519", ctx)
        assert result["pe_ttm"] == 19.09
        assert not ctx.limitations


class TestNarrateHardening:
    def test_think_block_stripped(self):
        """实测缺陷回归：推理模型的 <think> 思考块不得泄漏进报告叙述。"""
        from src.deep_research_dims.narrate import narrate

        class _ThinkAdapter:
            def call_text(self, messages, **kwargs):
                class _R:
                    content = (
                        "<think>Let me analyze the data and think about the "
                        "ma alignment carefully step by step...</think>\n"
                        "均线空头排列显示短期承压，量能平稳暗示观望情绪，"
                        "当前位置宜等待回踩支撑再考虑介入。"
                    )

                return _R()

        out = narrate(
            _ThinkAdapter(), "data", {"a": 1}, "默认句", timeout=1
        )
        assert "<think>" not in out
        assert out.startswith("均线空头排列")

    def test_thinking_variant_stripped(self):
        from src.deep_research_dims.narrate import narrate

        class _T2:
            def call_text(self, messages, **kwargs):
                class _R:
                    content = "<thinking>长思考过程</thinking>结论句：当前阶段处于盘后时段，建议按报告计划挂单等待触发价，不要追高。"

                return _R()

        out = narrate(_T2(), "phase", {}, "默认句", timeout=1)
        assert "thinking" not in out.lower()
        assert out.startswith("结论句")

    def test_empty_after_strip_falls_back(self):
        from src.deep_research_dims.narrate import narrate

        class _Empty:
            def call_text(self, messages, **kwargs):
                class _R:
                    content = "<think>只有思考没有结论</think>"

                return _R()

        assert narrate(_Empty(), "history", {}, "默认句", timeout=1) == "默认句"


class TestDimSubReports:
    def test_split_all_dim_sections(self):
        from src.deep_research_dims.render import (
            DIM_LABELS,
            VIEW_DIM_ORDER,
            split_dim_sections,
        )

        dims = _all_default_dims()
        view = build_view("贵州茅台", "600519", "2026-09-29T10:00:00", [], dims, [])
        markdown = render_markdown(view)
        sections = split_dim_sections(markdown)
        assert set(sections) == set(VIEW_DIM_ORDER)
        # 每个章节非空且以「### 中文节名」标题开头
        for dim_id in VIEW_DIM_ORDER:
            assert sections[dim_id].startswith(f"### {DIM_LABELS[dim_id]}"), dim_id

    def test_split_legacy_anchor_markdown_still_works(self):
        """v1 模板历史报告（无 dim 标记）回退锚点切分。"""
        from src.deep_research_dims.render import (
            DIM_SECTION_ANCHORS,
            split_dim_sections,
        )

        dims = _all_default_dims()
        view = build_view("贵州茅台", "600519", "2026-09-29T10:00:00", [], dims, [])
        markdown = render_markdown(view, template_name="deep_research_dual_track.j2")
        sections = split_dim_sections(markdown)
        assert set(sections) == set(DIM_SECTION_ANCHORS)
        for dim_id, anchor in DIM_SECTION_ANCHORS.items():
            assert sections[dim_id].startswith(anchor), dim_id

    def test_skipped_section_carries_note(self):
        from src.deep_research_dims.render import split_dim_sections

        dims = _all_default_dims()
        dims["capital"] = dims["capital"].model_copy(
            update={"status": "skipped", "degraded_reason": "省钱模式未选择该维度"}
        )
        view = build_view("贵州茅台", "600519", "2026-09-29T10:00:00", [], dims, [])
        sections = split_dim_sections(render_markdown(view))
        assert "未选择生成" in sections["capital"]

    def test_build_dim_report_header_footer(self):
        from src.deep_research_dims.render import build_dim_report

        md = build_dim_report("intel", "#### 消息面详析\n\n内容", "贵州茅台", "600519", "2026-09-29T10:00:00")
        assert md.startswith("# 贵州茅台（600519）· 消息面详析（子报告）")
        assert "数据截至：2026-09-29T10:00:00" in md
        assert "不构成投资建议" in md
        assert "内容" in md

    def test_six_dim_ttl_not_exceeds_dependencies(self):
        """审计 C8：six_dim 缓存 TTL 不得超过其数据依赖（data/F1/F2=24h）。"""
        from src.deep_research_dims.dim_cache import DIM_TTL_HOURS

        assert DIM_TTL_HOURS["six_dim"] <= DIM_TTL_HOURS["data"]
        assert DIM_TTL_HOURS["six_dim"] <= DIM_TTL_HOURS["fundamental"]
        assert DIM_TTL_HOURS["six_dim"] <= DIM_TTL_HOURS["sector"]


class TestGuardrailEventPlans:
    def test_gr11_flags_incomplete_plans(self):
        from src.deep_research_dims.guardrail import rule11_event_plan_completeness

        intel = {
            "event_calendar": [
                {"event": "三季报", "outcomes": ["超预期", "平", "低于预期"], "plans": ["持有"]},
                {"event": "解禁", "outcomes": ["平稳"], "plans": ["观望", "减仓"]},
            ]
        }
        events = rule11_event_plan_completeness(intel)
        assert len(events) == 1 and events[0].rule_id == "GR11_event_plan_completeness"

    def test_gr11_passes_full_coverage(self):
        from src.deep_research_dims.guardrail import rule11_event_plan_completeness

        intel = {"event_calendar": [{"event": "e", "outcomes": ["a"], "plans": ["p1"]}]}
        assert rule11_event_plan_completeness(intel) == []


class TestChanlunFactsInjection:
    def test_engine_facts_empty_on_failure(self, monkeypatch):
        """引擎/数据失败时返回空串，研究员退回工具取数（不阻断）。"""
        from src.agent.deep_research import explore_agents
        from src.services import history_loader

        monkeypatch.setattr(history_loader, "load_history_df", lambda *a, **k: (None, "none"))
        assert explore_agents._chanlun_engine_facts("000000") == ""  # 无数据 → 空串兜底


class TestSupplyChainReuse:
    def test_existing_report_maps_to_dim(self):
        """供应链专项报告复用：载荷 → SupplyChainDim（链接/日期/深潜结构）。"""
        from src.deep_research.researchers.supply_chain import parse

        dim, steps = parse(
            {
                "existing_report": {
                    "report_id": "sc_202610011200",
                    "topic": "白酒产业链",
                    "created_at": "2026-10-01",
                    "link": "/api/v1/supply-chain/reports/sc_202610011200",
                    "deep_dive": {"chokepoints": [{"type": "geo", "description": "产区稀缺"}]},
                }
            },
            0,
        )
        assert dim.status == "ok"
        assert "白酒产业链" in dim.supply_chain.company_position
        assert dim.verification_status == "existing_report:sc_202610011200"
        assert "supply-chain/reports/sc_202610011200" in dim.narrative
        assert dim.supply_chain.chokepoints[0].type == "geo"


class TestPhaseFallback:
    def test_phase_never_degrades_on_calendar_failure(self, monkeypatch):
        """日历数据源抛异常时回退本地兜底，维度不降级（E2E 抓到的真实问题）。"""
        from src.deep_research_dims import phase_dim

        def _boom(*_a, **_k):
            raise RuntimeError("trade_cal permission denied")

        # phase_dim 用的是 from-import 名，直接 patch 模块内引用
        monkeypatch.setattr(phase_dim, "is_market_open", _boom)
        dim = phase_dim.build_phase_dim(_ctx())
        assert dim.status == "ok"
        assert dim.phase is not None
        assert any("兜底" in x for x in dim.phase.data_limitations)


def _all_default_dims() -> dict:
    dims = {
        "signal": SignalDim(),
        "data": DataDim(),
        "intel": IntelDim(),
        "plan": PlanDim(),
        "phase": PhaseDim(),
        "history": HistoryDim(),
        "six_dim": SixDimDim(),
        "bayesian": BayesianDim(),
        "conclusion": ConclusionDim(),
        "supply_chain": SupplyChainDim(),
        "scenarios": ScenariosDim(probability_sum=1.0),
        "fundamental": FundamentalDim(),
        "sector": SectorDim(),
        "technical": TechnicalDim(),
        "capital": CapitalDim(),
        "sentiment": SentimentDim(),
        "ownership": OwnershipDim(),
        "us_china": UsChinaDim(),
        "business": BusinessDim(),
    }
    assert set(dims) == set(DIM_IDS)
    return dims


class TestContractsAndRender:
    def test_parse_dim_roundtrip(self):
        for dim_id, model in _all_default_dims().items():
            parsed = parse_dim(dim_id, model.model_dump())
            assert parsed.dim == dim_id

    def test_parse_dim_unknown_rejected(self):
        with pytest.raises(ValueError):
            parse_dim("nope", {})

    def test_render_structure_complete(self):
        dims = _all_default_dims()
        view = build_view(
            "贵州茅台", "600519", "2026-09-28T10:00:00", ["测试局限"], dims, []
        )
        markdown = render_markdown(view)
        assert validate_structure(markdown) == []
        assert "不构成投资建议" in markdown
        assert "测试局限" in markdown

    def test_render_includes_guardrail_table(self):
        from src.schemas.deep_research_dims import GuardrailEvent

        dims = _all_default_dims()
        events = [
            GuardrailEvent(
                rule_id="GR2_negative_edge",
                dim="conclusion",
                action="override",
                reason="Edge ≤ 0",
            )
        ]
        view = build_view("贵州茅台", "600519", "2026-09-28T10:00:00", [], dims, events)
        markdown = render_markdown(view)
        assert "GR2_negative_edge" in markdown
        assert "Edge ≤ 0" in markdown
