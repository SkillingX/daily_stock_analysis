"""Execution and report-boundary regressions; no live provider requests."""

from pathlib import Path

import pytest

from src.agent.llm_adapter import LLMResponse
from src.deep_research_dims import narrate as narrative_module


@pytest.mark.parametrize("default", ["", "已有规则结论"])
def test_provider_error_cannot_become_research_prose(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, default: str) -> None:
    (tmp_path / "phase.md").write_text("解释已有事实")
    monkeypatch.setattr(narrative_module, "_AGENTS_DIR", str(tmp_path))

    class FailedProvider:
        def call_text(self, *args: object, **kwargs: object) -> LLMResponse:
            return LLMResponse(provider="error", content="All LLM models failed. Last error: model request timed out " * 3)

    assert narrative_module.narrate(FailedProvider(), "phase", {"status": "known"}, default) == default

from typing import Any
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.test_model_concurrency import provider
from tests.test_choice_integration import choice_transport


@pytest.mark.parametrize("mode", [1, 2])
def test_partial_research_preserves_evidence_and_matches_api_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    provider: tuple[str, dict[str, int]], choice_transport: Any, mode: int,
) -> None:
    import time
    from src.config import Config, get_config
    from src.storage import DatabaseManager
    from src.search_service import SearchService, SearchResult, SearchResponse
    from src.services import deep_research_service as service_module
    from api.v1.endpoints.deep_research import router
    base, observed = provider
    observed["research_mode"] = mode
    cfg = choice_transport
    cfg.deep_research_timeout_seconds = 30 if mode == 1 else 2.0
    cfg.deep_research_engine = "dual_track"
    cfg.litellm_model = "openai/fixture"
    cfg.agent_litellm_model = "openai/fixture"
    cfg.llm_model_list = [{"model_name": "openai/fixture", "litellm_params": {"model": "openai/fixture", "api_base": base, "api_key": "fixture-key"}}]
    monkeypatch.setattr(Config, "_instance", cfg)
    monkeypatch.setattr(service_module, "_dual_track_adapter", None)
    monkeypatch.setattr(service_module, "_DEEP_RESEARCH_DIR", tmp_path)
    monkeypatch.setattr(DatabaseManager, "_instance", None)
    database = DatabaseManager(f"sqlite:///{tmp_path / 'reports.sqlite'}")
    monkeypatch.setattr(service_module, "get_db", lambda: database)
    from src.agent.deep_research import orchestrator
    from src.deep_research_dims.context import SharedContext, _safe_fundamental
    context = SharedContext("600519", "样本", "2026-10-15", quote={"price": 50.0})
    context.fundamental = _safe_fundamental("600519", context)
    monkeypatch.setattr(orchestrator, "build_shared_context", lambda *args, **kwargs: context)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setattr(SearchService, "is_available", property(lambda self: True))
    monkeypatch.setattr(SearchService, "search_comprehensive_intel", lambda self, **kwargs: {
        "business": SearchResponse(query="fixture", provider="fixture", results=[SearchResult(title="公司公告样本", snippet="已取得的业务资料样本", url="https://example.com/disclosure", source="公司公告", published_date="2026-10-01")])
    })
    before = time.monotonic()
    try:
        service = service_module.deep_research_service
        result = service.generate_report("600519", "样本", dims=["business", "fundamental"], force_refresh=True)
        assert time.monotonic() - before < (10 if mode == 1 else 4)
        assert result["status"] == "partial", result
        assert result["report_id"]
        assert "All LLM models failed" not in result["markdown"]
        business = result["dimensions"]["business"]
        if mode == 1:
            assert business["retained_evidence"][0]["url"] == "https://example.com/disclosure"
            assert "公司公告样本" in result["markdown"]
            assert "研究时限耗尽" not in business["degraded_reason"]
        else:
            assert "超时" in business["degraded_reason"] or "预算" in business["degraded_reason"]
        assert not result.get("cache_hit")
        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as client:
            detail = client.get(f"/reports/{result['report_id']}")
            assert detail.status_code == 200
            assert detail.json()["data"]["markdown"] == result["markdown"]
            download = client.get(f"/reports/{result['report_id']}/markdown")
            assert download.status_code == 200
            assert download.text == result["markdown"]
        assert observed["count"] == (2 if mode == 1 else 1)
    finally:
        database._engine.dispose()
