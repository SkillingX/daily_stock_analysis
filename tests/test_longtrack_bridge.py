# -*- coding: utf-8 -*-
"""长线桥接器单元测试：映射契约 / 降级回退 / 非 A 股绕过 / skip_narrations 传播。

全部离线（monkeypatch run_dual_track / 内嵌回退），不触网不烧额度。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.schemas.deep_research_dims import DIM_IDS
from typing import Any, Dict, List


@dataclass
class _FakeResult:
    """AnalysisResult 最小替身（dataclass 允许动态挂五段式字段）。"""

    code: str = "600519"
    stock_name: str = "贵州茅台"
    extra: Dict[str, Any] = field(default_factory=dict)


def _fake_dims_payload() -> Dict[str, Dict[str, Any]]:
    return {
        "conclusion": {
            "dim": "conclusion",
            "status": "ok",
            "conclusion": {
                "prior_p": 0.73,
                "market_implied_p": 0.55,
                "edge": 0.18,
                "position": "持有",
                "action": "持有",
                "value_range_1y": "1200-1400",
                "value_range_3y": None,
                "value_range_5y": None,
                "rationale": "",
            },
            "rationale": "",
            "narrative": "",
        },
        "supply_chain": {
            "dim": "supply_chain",
            "status": "ok",
            "supply_chain": {
                "company_position": "白酒产业链中游",
                "chain_map": [],
                "chokepoints": [],
                "upstream": [],
                "downstream": [],
                "bargaining_power": None,
                "us_china_chain": None,
            },
            "verification_status": "partial",
            "narrative": "",
        },
        "scenarios": {
            "dim": "scenarios",
            "status": "ok",
            "scenarios": {
                "industry_space": None,
                "competitive_evolution": None,
                "scenarios": [
                    {"type": "optimistic", "probability": 0.25, "value_anchor": 1617.0},
                    {"type": "neutral", "probability": 0.5, "value_anchor": 1243.0},
                    {"type": "pessimistic", "probability": 0.25, "value_anchor": 932.0},
                ],
                "horizons": None,
                "catalysts": [],
                "risks": [],
            },
            "probability_sum": 1.0,
            "expected_value": 1264.0,
            "valuation_basis": "PE_TTM",
            "current_pe_ttm": 19.09,
            "narrative": "",
        },
        "bayesian": {
            "dim": "bayesian",
            "status": "ok",
            "bayesian": {
                "prior_p": 0.73,
                "market_implied_p": 0.55,
                "edge": 0.18,
                "posterior_p": 0.75,
                "position_suggestion": "3-5%",
                "confidence": "中",
                "evidence_log": [],
                "stop_conditions": {"should_stop": False},
            },
            "evidence_rejected": [],
            "market_implied_basis": "industry_base_rate:白酒",
            "narrative": "",
        },
        "six_dim": {
            "dim": "six_dim",
            "status": "ok",
            "framework": {
                "dimension_total": 76.5,
                "dimensions": [
                    {"dimension": "产业链定位", "weight": 0.25, "score": 80.0, "indicators": [], "warnings": []}
                ],
                "scoring_version": "v1",
                "warnings": [],
            },
            "scoring_version": "v1",
            "warnings": [],
            "narrative": "",
        },
    }


class _FakeDualTrackResult:
    def __init__(self, status: str, payload: Dict[str, Dict[str, Any]], quality: int = 100):
        self.status = status
        self.dims_payload = payload
        self.quality_score = quality
        self.final_conclusion = ""
        self.success = status != "failed"


class TestLongtrackBridge:
    def test_happy_path_maps_five_sections(self, monkeypatch):
        from src.services import longtrack_bridge

        captured: Dict[str, Any] = {}

        def _fake_run(stock_code, stock_name, llm_adapter, **kwargs):
            captured.update(kwargs)
            return _FakeDualTrackResult("success", _fake_dims_payload())

        monkeypatch.setattr(
            "src.agent.deep_research.orchestrator.run_dual_track", _fake_run
        )
        monkeypatch.setattr(
            "src.services.deep_research_service._get_dual_track_adapter",
            lambda: object(),
        )

        result = _FakeResult()
        out = longtrack_bridge.integrate_longtrack_dual(result, {})

        assert out.longtrack_source == "dual_track"
        # 五段式字段全部落位（下游模板读取的形状契约）
        assert out.investment_conclusion["action"] == "持有"
        assert out.investment_conclusion["prior_p"] == 0.73
        assert out.supply_chain["company_position"] == "白酒产业链中游"
        assert len(out.value_scenarios["scenarios"]) == 3
        assert out.bayesian_framework["posterior_p"] == 0.75
        assert out.bayesian_framework["market_implied_basis"] if "market_implied_basis" in out.bayesian_framework else True
        assert out.research_framework["dimension_total"] == 76.5
        # 审计 A2：批量场景必须跳过叙述调用
        assert captured.get("skip_narrations") is True
        assert captured.get("dims_filter") == longtrack_bridge.LONG_TRACK_SUBSET
        assert captured.get("progress_callback") is None
        # 审计 B2 修复：空叙述的情景维度不得写入缓存（探索/评分维度照写）
        assert captured.get("cache_exclude") == frozenset({"scenarios"})

    def test_non_a_share_falls_back(self, monkeypatch):
        from src.services import longtrack_bridge

        called = {"embedded": False}

        def _fake_rf(result, context, **kwargs):
            called["embedded"] = True
            result.embedded_marker = True
            return result

        monkeypatch.setattr(
            "src.services.research_framework_integration.integrate_research_framework",
            _fake_rf,
        )

        result = _FakeResult(code="AAPL", stock_name="Apple")
        out = longtrack_bridge.integrate_longtrack_dual(result, {})
        assert called["embedded"] is True
        assert getattr(out, "longtrack_source", None) in (None, "legacy_fallback")

    def test_failed_dual_result_falls_back(self, monkeypatch):
        from src.services import longtrack_bridge

        monkeypatch.setattr(
            "src.agent.deep_research.orchestrator.run_dual_track",
            lambda *a, **k: _FakeDualTrackResult("failed", {}),
        )
        monkeypatch.setattr(
            "src.services.deep_research_service._get_dual_track_adapter",
            lambda: object(),
        )

        def _fake_rf(result, context, **kwargs):
            result.embedded_marker = True
            return result

        monkeypatch.setattr(
            "src.services.research_framework_integration.integrate_research_framework",
            _fake_rf,
        )

        result = _FakeResult()
        out = longtrack_bridge.integrate_longtrack_dual(result, {})
        assert out.longtrack_source == "legacy_fallback"
        assert out.embedded_marker is True
        assert not hasattr(out, "investment_conclusion")

    def test_incomplete_mapping_falls_back(self, monkeypatch):
        """五段式缺一（如 scenarios 缺失）视为长线失败 → 回退。"""
        from src.services import longtrack_bridge

        payload = _fake_dims_payload()
        del payload["scenarios"]
        monkeypatch.setattr(
            "src.agent.deep_research.orchestrator.run_dual_track",
            lambda *a, **k: _FakeDualTrackResult("success", payload),
        )
        monkeypatch.setattr(
            "src.services.deep_research_service._get_dual_track_adapter",
            lambda: object(),
        )

        def _fake_rf(result, context, **kwargs):
            result.embedded_marker = True
            return result

        monkeypatch.setattr(
            "src.services.research_framework_integration.integrate_research_framework",
            _fake_rf,
        )

        result = _FakeResult()
        out = longtrack_bridge.integrate_longtrack_dual(result, {})
        assert out.longtrack_source == "legacy_fallback"
        assert out.embedded_marker is True

    def test_engine_exception_falls_back(self, monkeypatch):
        from src.services import longtrack_bridge

        def _boom(*a, **k):
            raise RuntimeError("engine down")

        monkeypatch.setattr(
            "src.agent.deep_research.orchestrator.run_dual_track", _boom
        )
        monkeypatch.setattr(
            "src.services.deep_research_service._get_dual_track_adapter",
            lambda: object(),
        )

        def _fake_rf(result, context, **kwargs):
            result.embedded_marker = True
            return result

        monkeypatch.setattr(
            "src.services.research_framework_integration.integrate_research_framework",
            _fake_rf,
        )

        result = _FakeResult()
        out = longtrack_bridge.integrate_longtrack_dual(result, {})
        assert out.longtrack_source == "legacy_fallback"
        assert out.embedded_marker is True


class TestCacheHygieneRegressions:
    """审计 B1/B2 回归：缓存命中的 emit/TTL 语义 + 批量空叙述缓存污染。"""

    def _seed_scenarios_cache(self, tmp_path, monkeypatch) -> str:
        """预置一个带 saved_at 的 scenarios 缓存，返回路径。"""
        import json
        import os
        from datetime import datetime, timedelta

        from src.deep_research_dims import dim_cache

        monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path))
        payload = {
            "dim": "scenarios",
            "status": "ok",
            "scenarios": {"scenarios": [], "catalysts": [], "risks": []},
            "probability_sum": 1.0,
            "narrative": "缓存里已有的叙述",
        }
        dim_cache.save_cached_dim("600519", "scenarios", payload)
        path = os.path.join(str(tmp_path), "600519_scenarios.json")
        record = json.loads(open(path, encoding="utf-8").read())
        record["saved_at"] = (datetime.now() - timedelta(hours=1)).isoformat()
        open(path, "w", encoding="utf-8").write(json.dumps(record))
        return path

    def _run_offline(self, monkeypatch, tmp_path, **run_kwargs):
        from datetime import date, timedelta

        from src.agent.deep_research import orchestrator as orch
        from src.deep_research_dims import dim_cache
        from src.deep_research_dims.context import SharedContext

        monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path))
        ctx = SharedContext(stock_code="600519", stock_name="贵州茅台", as_of="2026-10-01T12:00:00")
        ctx.quote = {"price": 13.0}
        ctx.fundamental = {"pe_ttm": 25.0}
        ctx.history = [
            {"date": str(date(2026, 8, 1) + timedelta(days=i)), "open": 10 + i * 0.1,
             "high": 10.3 + i * 0.1, "low": 9.7 + i * 0.1, "close": 10 + i * 0.1, "volume": 10000}
            for i in range(40)
        ]
        monkeypatch.setattr(orch, "build_shared_context", lambda code, name: ctx)
        events: List[Dict[str, Any]] = []
        orch.run_intel_agent = lambda *a, **k: {"ok": True, "data": {"sentiment_summary": "中", "risk_alerts": [], "positive_catalysts": [], "unverified_count": 0, "evidence_items": []}, "steps": 1}
        orch.run_supply_chain_agent = lambda *a, **k: {"ok": True, "data": {"company_position": "白酒"}, "steps": 1}
        result = orch.run_dual_track(
            "600519", "贵州茅台", llm_adapter=None,
            progress_callback=lambda e: events.append(e),
            **run_kwargs,
        )
        return result, events

    def test_scenarios_cache_hit_single_emit_and_no_ttl_refresh(self, monkeypatch, tmp_path):
        """B1：缓存命中的 scenarios 只 emit 一次，且 saved_at 不被刷新（TTL 不滑动）。"""
        import json

        path = self._seed_scenarios_cache(tmp_path, monkeypatch)
        before = json.loads(open(path, encoding="utf-8").read())["saved_at"]

        _result, events = self._run_offline(monkeypatch, tmp_path)

        scen_emits = [e for e in events if e.get("type") == "dim_done" and e.get("dim") == "scenarios"]
        assert len(scen_emits) == 1, f"缓存命中应恰好 1 次 emit，实际 {len(scen_emits)}"
        after = json.loads(open(path, encoding="utf-8").read())["saved_at"]
        assert after == before, "缓存命中不得刷新 saved_at（TTL 滑动语义）"

    def test_cache_exclude_skips_scenarios_but_writes_others(self, monkeypatch, tmp_path):
        """B2：cache_exclude=scenarios 时探索/评分维度照写、scenarios 不写。"""
        import json
        import os

        from src.deep_research_dims import dim_cache

        monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path))
        _result, _events = self._run_offline(
            monkeypatch, tmp_path, force_refresh=True, skip_narrations=True,
            cache_exclude=frozenset({"scenarios"}),
        )
        names = set(os.listdir(str(tmp_path)))
        assert "600519_scenarios.json" not in names, "被排除维度不得写缓存"
        assert "600519_supply_chain.json" in names, "探索维度照写（次日命中省探索 Agent）"
        assert "600519_six_dim.json" in names, "评分维度照写"
        # 写出的 supply_chain payload 叙述无关（skip_narrations 不污染）
        sc = json.loads(open(os.path.join(str(tmp_path), "600519_supply_chain.json"), encoding="utf-8").read())
        assert sc["payload"]["supply_chain"]["company_position"] == "白酒"


class TestFinalConclusion:
    def test_conclusion_generated_with_fake_adapter(self, monkeypatch, tmp_path):
        """终读结论：fake adapter 产出文本进报告段一与 result 字段。"""
        from datetime import date, timedelta

        from src.agent.deep_research import orchestrator as orch
        from src.deep_research_dims import dim_cache
        from src.deep_research_dims.context import SharedContext

        monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path))
        ctx = SharedContext(stock_code="600519", stock_name="贵州茅台", as_of="2026-10-02T15:00:00")
        ctx.quote = {"price": 13.0}
        ctx.fundamental = {"pe_ttm": 25.0}
        ctx.history = [
            {"date": str(date(2026, 8, 1) + timedelta(days=i)), "open": 10 + i * 0.1,
             "high": 10.3 + i * 0.1, "low": 9.7 + i * 0.1, "close": 10 + i * 0.1, "volume": 10000}
            for i in range(40)
        ]
        monkeypatch.setattr(orch, "build_shared_context", lambda code, name: ctx)
        orch.run_intel_agent = lambda *a, **k: {"ok": True, "data": {"sentiment_summary": "中", "risk_alerts": [], "positive_catalysts": [], "unverified_count": 0, "evidence_items": []}, "steps": 1}
        orch.run_supply_chain_agent = lambda *a, **k: {"ok": True, "data": {"company_position": "白酒"}, "steps": 1}

        captured: dict = {}

        class _Adapter:
            def call_text(self, messages, **kwargs):
                import json as _json

                facts = _json.loads(messages[1]["content"])
                captured.update(facts)

                class _R:
                    content = "总评分 50 分，建议观望。论据：估值合理。风险：需求疲弱。关注三季报。"
                return _R()

        result = orch.run_dual_track("600519", "贵州茅台", llm_adapter=_Adapter(), force_refresh=True)
        assert "总评分 50 分" in result.final_conclusion
        # v2：终读结论文本进「一、结论」（template v2 重构后 §1 = 结论）
        assert "总评分 50 分" in result.markdown
        assert "## 一、结论" in result.markdown
        # 数字注入式：事实面含评分与行动
        assert captured.get("total_score") is not None
        assert captured.get("action") in ("建仓", "加仓", "持有", "减仓", "止损", "观察")

    def test_conclusion_skipped_when_no_adapter(self, monkeypatch, tmp_path):
        from datetime import date, timedelta

        from src.agent.deep_research import orchestrator as orch
        from src.deep_research_dims import dim_cache
        from src.deep_research_dims.context import SharedContext

        monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path))
        ctx = SharedContext(stock_code="600519", stock_name="贵州茅台", as_of="x")
        ctx.quote = {"price": 13.0}
        ctx.fundamental = {"pe_ttm": 25.0}
        ctx.history = [
            {"date": str(date(2026, 8, 1) + timedelta(days=i)), "open": 10, "high": 11,
             "low": 9, "close": 10, "volume": 100}
            for i in range(40)
        ]
        monkeypatch.setattr(orch, "build_shared_context", lambda code, name: ctx)
        orch.run_intel_agent = lambda *a, **k: {"ok": True, "data": {"sentiment_summary": "中", "risk_alerts": [], "positive_catalysts": [], "unverified_count": 0, "evidence_items": []}, "steps": 1}
        orch.run_supply_chain_agent = lambda *a, **k: {"ok": True, "data": {"company_position": "白酒"}, "steps": 1}
        result = orch.run_dual_track("600519", "贵州茅台", llm_adapter=None, force_refresh=True)
        assert result.final_conclusion == ""
        # 模板回退：无 LLM 时第一节仍渲染（信号一句话兜底）
        assert "## 一、结论" in result.markdown


class TestResearcherParseCoercion:
    def test_nested_dict_fields_coerced(self):
        """LLM 把 summary 字段返回成 dict 时必须矫正而非校验失败。"""
        from src.agent.deep_research.orchestrator import _parse_researcher

        dim, _ = _parse_researcher(
            "capital",
            {
                "flow_summary": {"status": "data_unavailable", "summary": "流向数据不可用"},
                "institution_summary": {"note": "机构持仓无变动"},
                "chip_summary": {"text": "筹码集中度 12%"},
                "flow_score": {"score": 40.0},
                "institution_score": "45",
                "chip_score": None,
                "score": {"value": 42},
            },
            1,
        )
        assert dim.status == "ok"
        assert dim.flow_summary == "流向数据不可用"
        assert dim.institution_summary == "机构持仓无变动"
        assert dim.chip_summary == "筹码集中度 12%"
        assert dim.flow_score == 40.0
        assert dim.institution_score == 45.0
        assert dim.chip_score is None
        assert dim.score == 42.0

    def test_unknown_dict_becomes_compact_json(self):
        from src.agent.deep_research.orchestrator import _parse_researcher

        dim, _ = _parse_researcher(
            "us_china",
            {"export_control": {"list": [1, 2], "flag": True}},
            1,
        )
        assert isinstance(dim.export_control, str) and "list" in dim.export_control


class TestSkipNarrationsPropagation:
    def test_orchestrator_skip_narrations_offline(self, monkeypatch, tmp_path):
        """skip_narrations=True 时全部 7 个叙述点静默（输出=规则默认句）。"""
        from datetime import date, timedelta

        from src.agent.deep_research import orchestrator as orch
        from src.deep_research_dims import dim_cache
        from src.deep_research_dims.context import SharedContext

        monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path))
        ctx = SharedContext(stock_code="600519", stock_name="贵州茅台", as_of="2026-10-01T12:00:00")
        ctx.quote = {"price": 13.0}
        ctx.fundamental = {"pe_ttm": 25.0}
        ctx.history = [
            {"date": str(date(2026, 8, 1) + timedelta(days=i)), "open": 10 + i * 0.1,
             "high": 10.3 + i * 0.1, "low": 9.7 + i * 0.1, "close": 10 + i * 0.1, "volume": 10000}
            for i in range(40)
        ]
        monkeypatch.setattr(orch, "build_shared_context", lambda code, name: ctx)
        orch.run_intel_agent = lambda *a, **k: {"ok": True, "data": {"sentiment_summary": "中性", "risk_alerts": [], "positive_catalysts": [], "unverified_count": 0, "evidence_items": []}, "steps": 1}
        orch.run_supply_chain_agent = lambda *a, **k: {"ok": True, "data": {"company_position": "白酒"}, "steps": 1}
        for _name in (
            "run_technical_agent", "run_capital_agent", "run_sentiment_agent",
            "run_ownership_agent", "run_us_china_agent", "run_business_agent",
        ):
            setattr(
                orch, _name,
                lambda *a, **k: {"ok": True, "data": {"score": 55, "narrative": "测试"}, "steps": 1},
            )

        # narrate 被调用即失败（证明 skip 生效）
        def _forbidden_narrate(*a, **k):
            raise AssertionError("skip_narrations=True 时不应调用 narrate")

        monkeypatch.setattr(orch, "narrate", _forbidden_narrate)

        result = orch.run_dual_track(
            "600519", "贵州茅台", llm_adapter=None,
            force_refresh=True, skip_narrations=True,
        )
        assert result.status == "success"
        assert len(result.dims) == len(DIM_IDS) == 19
