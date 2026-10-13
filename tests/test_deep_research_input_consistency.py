"""Report consumers must not turn missing valuation evidence into normal targets.
Run: python -m pytest tests/test_deep_research_input_consistency.py
Uses real scenario/conclusion functions; no provider or model requests.
"""
from src.deep_research_dims.context import SharedContext
from src.deep_research_dims.scenarios_dim import build_scenarios_dim
from src.deep_research_dims.conclusion_dim import build_conclusion_dim
from tests.test_fundamentals_data_quality import financial_sources


def test_empty_financials_do_not_generate_price_multiplier_valuation():
    ctx = SharedContext('300260', '样本', 'fixture', fundamental={})
    scenarios = build_scenarios_dim(ctx, 47.56)
    assert scenarios.status == 'degraded'
    assert scenarios.valuation_basis == 'none'
    assert scenarios.expected_value is None
    conclusion = build_conclusion_dim(ctx, {'framework': {'dimension_total': 85}}, {'bayesian': {'posterior_p': 0.8, 'edge': 0.3}}, scenarios.model_dump())
    assert conclusion.conclusion.action == '观察'
    assert conclusion.conclusion.value_range_1y is None


def test_model_target_without_evidence_cannot_become_a_valuation():
    ctx = SharedContext('300260', '样本', 'fixture', fundamental={})
    scenarios = build_scenarios_dim(ctx, 47.56, {'scenarios': [{'type': 'neutral', 'probability': 1.0, 'value_anchor': 100.0, 'description': '业务改善假设'}], 'horizon_1y': '100元'})
    assert scenarios.expected_value is None
    assert scenarios.status == 'degraded'
    assert scenarios.scenarios.scenarios[0].value_anchor is None
    assert scenarios.scenarios.scenarios[0].description == '业务改善假设'
    assert scenarios.scenarios.horizons.horizon_1y is None


def test_refresh_reaches_stage_zero_and_resets_request_context(monkeypatch, tmp_path):
    from data_provider.mx_data_adapter import financial_refresh
    from src.agent.deep_research import orchestrator
    from src.deep_research_dims import dim_cache
    from tests.test_deep_research_dims import _ctx
    seen = []
    def snapshot(code, name):
        seen.append(financial_refresh.get())
        return _ctx()
    monkeypatch.setattr(orchestrator, 'build_shared_context', snapshot)
    monkeypatch.setattr(dim_cache, '_CACHE_DIR', str(tmp_path))
    previous = financial_refresh.get()
    for code in ('600519', '300260'):
        orchestrator.run_dual_track(code, '样本', None, dims_filter={'phase', 'history'}, force_refresh=True, skip_narrations=True, write_cache=False)
        assert financial_refresh.get() == previous
    assert [value[0] for value in seen] == ['600519', '300260']
    assert seen[0][1] != seen[1][1]


def test_current_data_dimension_does_not_reuse_previous_quote(monkeypatch, tmp_path):
    from src.agent.deep_research import orchestrator
    from src.deep_research_dims import dim_cache
    from tests.test_deep_research_dims import _ctx
    ctx = _ctx()
    monkeypatch.setattr(orchestrator, 'build_shared_context', lambda *args: ctx)
    monkeypatch.setattr(dim_cache, '_CACHE_DIR', str(tmp_path))
    ctx.quote['price'] = 20.0
    first = orchestrator.run_dual_track('600519', '样本', None, dims_filter={'data'}, skip_narrations=True, write_cache=False)
    dim_cache.save_cached_dim('600519', 'data', first.dims['data'].model_dump())
    ctx.quote['price'] = 35.0
    second = orchestrator.run_dual_track('600519', '样本', None, dims_filter={'data'}, skip_narrations=True, write_cache=False)
    assert first.dims['data'].perspective.price_position.current_price == 20.0
    assert second.dims['data'].perspective.price_position.current_price == 35.0


def test_partial_financial_snapshot_retries_real_manager_and_provider(financial_sources, monkeypatch):
    from src.deep_research_dims import context, dim_cache
    from src.agent.tools import data_tools
    from src.config import get_config
    cfg = get_config()
    cfg.fundamental_cache_ttl_seconds = 3600
    financial_sources['ratio'] = None
    first = context.build_shared_context('600519', '样本')
    assert first.fundamental['roe'] is None
    # Seed a previously persisted partial snapshot, too: it must not prevent retry.
    key = f'stage0_fund_{cfg.data_source_contract}_v{dim_cache.FUNDAMENTAL_MAPPING_VERSION}_cv0_600519'
    dim_cache.save_snapshot(key, first.fundamental)
    load_snapshot = dim_cache.load_snapshot
    consulted = []
    def read_snapshot(name, *args, **kwargs):
        result = load_snapshot(name, *args, **kwargs)
        if name == key and result is not None:
            consulted.append(result)
        return result
    monkeypatch.setattr(dim_cache, 'load_snapshot', read_snapshot)
    financial_sources['ratio'] = 4.56
    second = context.build_shared_context('600519', '样本')
    assert consulted and consulted[0]['roe'] is None
    assert second.fundamental['roe'] == 4.56
    assert second.fundamental['revenue_growth'] == 4.56
    assert data_tools._get_fetcher_manager()._fundamental_cache


def test_orchestration_keeps_guarded_final_payload_for_missing_evidence(financial_sources):
    from src.agent.deep_research.orchestrator import run_dual_track
    from src.deep_research_dims.manager_writeup import INCOMPLETE_EVIDENCE_NOTE
    from tests.test_manager_writeup_render import _WaveLLM
    class ResearchLLM(_WaveLLM):
        def call_text(self, messages, **kwargs):
            assert "3-5 句的中文投资结论" not in messages[0]["content"]
            return super().call_text(messages, **kwargs)
    result = run_dual_track("600519", "样本", ResearchLLM(), dims_filter={"phase", "history"}, write_cache=False)
    assert result.final_conclusion == INCOMPLETE_EVIDENCE_NOTE
    assert result.manager_writeup["money_paths"] == []
    assert result.manager_writeup["decision_matrix"] == []
    assert INCOMPLETE_EVIDENCE_NOTE in result.markdown
