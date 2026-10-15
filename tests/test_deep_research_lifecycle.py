"""Research cancellation and disconnect through the actual HTTP/model boundary."""
from __future__ import annotations

import concurrent.futures
import json
import socket
import threading
import time
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from tests.test_choice_integration import choice_transport
from tests.test_model_concurrency import provider


@pytest.fixture
def research_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, choice_transport: Any, request: pytest.FixtureRequest,
                  provider: tuple[str, dict[str, int]]) -> Iterator[tuple[str, dict[str, int]]]:
    from src.agent.deep_research import orchestrator
    from src.deep_research_dims.context import SharedContext, _safe_fundamental
    from src.services import deep_research_service as service
    from src.storage import DatabaseManager
    from api.v1.endpoints.deep_research import router
    base, observed = provider
    observed["research_mode"] = 2
    cfg = choice_transport
    cfg.deep_research_engine = getattr(request, "param", "dual_track")
    observed["legacy_text"] = int(cfg.deep_research_engine == "legacy")
    cfg.deep_research_timeout_seconds = 30
    cfg.litellm_model = cfg.agent_litellm_model = "openai/fixture"
    cfg.llm_model_list = [{"model_name": "openai/fixture", "litellm_params": {"model": "openai/fixture", "api_base": base, "api_key": "fixture-key"}}]
    monkeypatch.setattr(cfg, "is_agent_available", lambda: True)
    monkeypatch.setattr(service, "_dual_track_adapter", None)
    monkeypatch.setattr(service, "_executor_instance", None)
    monkeypatch.setattr(service, "_DEEP_RESEARCH_DIR", tmp_path / "reports")
    monkeypatch.setattr(DatabaseManager, "_instance", None)
    database = DatabaseManager(f"sqlite:///{tmp_path / 'reports.sqlite'}")
    monkeypatch.setattr(service, "get_db", lambda: database)
    context = SharedContext("600519", "样本", "2026-10-15", quote={"price": 50.0})
    context.fundamental = _safe_fundamental("600519", context)
    monkeypatch.setattr(orchestrator, "build_shared_context", lambda *args, **kwargs: context)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    app = FastAPI()
    app.include_router(router)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}", observed
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        database._engine.dispose()


def request_body(run_id: str, code: str = "600519") -> dict[str, Any]:
    return {"stock_code": code, "stock_name": "样本", "dims": ["fundamental", "business"],
            "force_refresh": True, "run_id": run_id}


def wait_until(predicate: Any) -> None:
    deadline = time.monotonic() + 8
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_cancel_waits_for_real_cleanup_and_cannot_cancel_a_new_run(research_http: tuple[str, dict[str, int]]) -> None:
    base, observed = research_http
    run_id = uuid4().hex
    with httpx.Client(base_url=base, timeout=15, trust_env=False) as client, concurrent.futures.ThreadPoolExecutor() as pool:
        generating = pool.submit(client.post, "/generate/stream", json=request_body(run_id))
        wait_until(lambda: observed["active"] > 0)
        cancelled = client.post(f"/runs/{run_id}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "cancelling"
        duplicate = client.post("/generate/stream", json=request_body(uuid4().hex, "600519.SH"))
        assert duplicate.status_code == 409
        response = generating.result(timeout=10)
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        done = next(event for event in events if event["type"] == "done")
        assert done["end_reason"] == "cancelled" and done["status"] == "partial"
        assert done["report_id"] and "用户已取消" in done["markdown"]
        assert client.get(f"/reports/{done['report_id']}").json()["data"]["markdown"] == done["markdown"]
        assert client.post(f"/runs/{run_id}/cancel").json()["status"] == "finished"
        newer_id = uuid4().hex
        newer = pool.submit(client.post, "/generate/stream", json=request_body(newer_id))
        wait_until(lambda: observed["active"] > 0)
        assert client.post(f"/runs/{run_id}/cancel").json()["status"] == "finished"
        assert client.get(f"/runs/{newer_id}").json()["end_reason"] is None
        assert newer.result(timeout=10).status_code == 200


@pytest.mark.parametrize("research_http", ["dual_track", "legacy"], indirect=True)
def test_disconnect_keeps_original_run_and_final_history(research_http: tuple[str, dict[str, int]]) -> None:
    base, observed = research_http
    run_id = uuid4().hex
    with httpx.Client(base_url=base, timeout=15, trust_env=False) as client:
        with client.stream("POST", "/generate/stream", json=request_body(run_id)) as response:
            assert response.status_code == 200
            first = next(line for line in response.iter_lines() if line.startswith("data: "))
            assert json.loads(first[6:])["type"] == "accepted"
        wait_until(lambda: observed["active"] > 0)
        assert client.post("/generate/stream", json=request_body(uuid4().hex)).status_code == 409
        wait_until(lambda: client.get(f"/runs/{run_id}").json().get("status") == "finished")
        status = client.get(f"/runs/{run_id}").json()
        assert status["end_reason"] is None and status["report_id"]
        assert client.get(f"/reports/{status['report_id']}").status_code == 200
        assert observed["count"] == (2 if observed.get("legacy_text") else 1)


@pytest.mark.parametrize("research_http", ["legacy"], indirect=True)
def test_legacy_cancel_has_real_lifecycle_and_no_empty_success(research_http: tuple[str, dict[str, int]]) -> None:
    base, observed = research_http
    run_id = uuid4().hex
    with httpx.Client(base_url=base, timeout=15, trust_env=False) as client, concurrent.futures.ThreadPoolExecutor() as pool:
        generating = pool.submit(client.post, "/generate/stream", json=request_body(run_id))
        wait_until(lambda: observed["active"] > 0)
        cancelled = client.post(f"/runs/{run_id}/cancel")
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelling"
        assert client.post("/generate/stream", json=request_body(uuid4().hex)).status_code == 409
        response = generating.result(timeout=10)
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        done = next(event for event in events if event["type"] == "done")
        assert done["end_reason"] == "cancelled"
        assert done["report_id"] is None and not done["markdown"]
        assert client.post(f"/runs/{run_id}/cancel").json()["status"] == "finished"
        assert client.post("/runs/not-a-run/cancel").status_code == 422


def test_cancel_during_tool_keeps_dedupe_until_source_finishes(research_http: tuple[str, dict[str, int]], monkeypatch: pytest.MonkeyPatch) -> None:
    from src.search_service import SearchResponse, SearchResult, SearchService
    base, observed = research_http
    observed["research_mode"] = 1
    entered, release = threading.Event(), threading.Event()
    def source(self: Any, **kwargs: Any) -> dict[str, SearchResponse]:
        entered.set()
        assert release.wait(8)
        return {"business": SearchResponse(query="fixture", provider="fixture", results=[SearchResult("资料样本", "样本", "https://example.com", "fixture")])}
    monkeypatch.setattr(SearchService, "is_available", property(lambda self: True))
    monkeypatch.setattr(SearchService, "search_comprehensive_intel", source)
    run_id = uuid4().hex
    with httpx.Client(base_url=base, timeout=15, trust_env=False) as client, concurrent.futures.ThreadPoolExecutor() as pool:
        generating = pool.submit(client.post, "/generate/stream", json=request_body(run_id))
        try:
            assert entered.wait(8)
            assert client.post(f"/runs/{run_id}/cancel").json()["status"] == "cancelling"
            # Allow the parent to save its partial report while the real tool remains in flight.
            wait_until(lambda: client.get(f"/runs/{run_id}").json().get("report_id") is not None)
            assert client.post("/generate/stream", json=request_body(uuid4().hex)).status_code == 409
        finally:
            release.set()
        response = generating.result(timeout=10)
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        done = next(event for event in events if event["type"] == "done")
        assert done["end_reason"] == "cancelled" and done["report_id"]
        assert observed["count"] == 1  # No post-cancellation synthesis call.


def test_cancelling_a_queued_model_never_sends_it_later(research_http: tuple[str, dict[str, int]]) -> None:
    from src.services.system_config_service import SystemConfigService
    from src.config import get_config
    base, observed = research_http
    observed["hold_models"] = 1
    model_base = get_config().llm_model_list[0]["litellm_params"]["api_base"]
    def occupy() -> Any:
        return SystemConfigService().test_llm_channel(name="fixture", protocol="openai", base_url=model_base,
                                                     api_key="fixture-key", models=["fixture"], timeout_seconds=15)
    run_id = uuid4().hex
    with httpx.Client(base_url=base, timeout=15, trust_env=False) as client, concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        occupied = [pool.submit(occupy) for _ in range(3)]
        try:
            wait_until(lambda: observed["active"] == 3)
            generating = pool.submit(client.post, "/generate/stream", json=request_body(run_id))
            wait_until(lambda: client.get(f"/runs/{run_id}").status_code == 200)
            assert client.post(f"/runs/{run_id}/cancel").status_code == 200
            response = generating.result(timeout=5)
            assert '"end_reason": "cancelled"' in response.text
            assert observed["count"] == 3
        finally:
            observed["hold_models"] = 0
        assert all(future.result(timeout=10)["success"] for future in occupied)
        assert observed["count"] == 3
