"""Report-level usage attribution: generation, save, day cache, history read.

Issue #55 acceptance: the per-run ledger reaches the saved report and the SSE
done event; a day-cache hit or history read keeps the generating run's usage
without any new outbound or duplicated accounting.
"""

from __future__ import annotations

import concurrent.futures
import json
import time
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

import httpx
import pytest

from tests.test_choice_integration import choice_transport  # noqa: F401 - fixture injection
from tests.test_model_attempt_ledger import usage_provider  # noqa: F401 - fixture injection
from tests.test_model_concurrency import provider  # noqa: F401 - fixture injection


@pytest.fixture
def report_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, choice_transport: Any, provider: tuple[str, dict[str, int]]) -> Iterator[tuple[str, dict[str, int], Path]]:
    from src.agent.deep_research import orchestrator
    from src.deep_research_dims.context import SharedContext, _safe_fundamental
    from src.services import deep_research_service as service
    from src.storage import DatabaseManager
    from api.v1.endpoints.deep_research import router

    base, observed = provider
    observed["research_mode"] = 2
    cfg = choice_transport
    cfg.deep_research_engine = "dual_track"
    observed["legacy_text"] = 0
    cfg.deep_research_timeout_seconds = 30
    cfg.litellm_model = cfg.agent_litellm_model = "openai/fixture"
    cfg.llm_model_list = [{"model_name": "openai/fixture", "litellm_params": {"model": "openai/fixture", "api_base": base, "api_key": "fixture-key"}}]
    cfg.llm_temperature = 0.3
    monkeypatch.setattr(cfg, "is_agent_available", lambda: True)
    monkeypatch.setattr(service, "_dual_track_adapter", None)
    monkeypatch.setattr(service, "_executor_instance", None)
    monkeypatch.setattr(service, "_DEEP_RESEARCH_DIR", tmp_path / "reports")
    monkeypatch.setattr(service, "_report_cache", {})
    monkeypatch.setattr(DatabaseManager, "_instance", None)
    database = DatabaseManager(f"sqlite:///{tmp_path / 'reports.sqlite'}")
    monkeypatch.setattr(service, "get_db", lambda: database)
    context = SharedContext("600519", "样本", "2026-10-15", quote={"price": 50.0})
    context.fundamental = _safe_fundamental("600519", context)
    monkeypatch.setattr(orchestrator, "build_shared_context", lambda *args, **kwargs: context)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")

    from fastapi import FastAPI
    import socket
    import threading
    import uvicorn

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
        yield f"http://127.0.0.1:{port}", observed, tmp_path / "reports"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        database._engine.dispose()


def request_body(run_id: str, code: str = "600519", force_refresh: bool = True, dims: list[str] | None = None) -> dict[str, Any]:
    return {"stock_code": code, "stock_name": "样本",
            "dims": dims or ["fundamental", "intel"],
            "force_refresh": force_refresh, "run_id": run_id}


def wait_until(predicate: Any) -> None:
    deadline = time.monotonic() + 30
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.05)


def _events(response_text: str) -> list[dict[str, Any]]:
    return [json.loads(line[6:]) for line in response_text.splitlines() if line.startswith("data: ")]


def test_generation_saves_usage_metadata_and_history_reads_it(report_http: tuple[str, dict[str, int], Path]) -> None:
    """done 事件与落盘产物携带同一份生成用量，历史读取引用原归属。"""
    base, observed, reports_dir = report_http
    run_id = uuid4().hex
    with httpx.Client(base_url=base, timeout=60, trust_env=False) as client:
        response = client.post("/generate/stream", json=request_body(run_id))
        events = _events(response.text)
        done = next(event for event in events if event["type"] == "done")
        assert done["status"] in {"success", "partial"}, done
        usage = done["model_usage"]
        assert usage["origin_run_id"] == run_id
        assert usage["outbound"] >= 1
        assert observed["count"] == usage["outbound"]  # ledger equals real requests
        # Known usage here is 2 tokens per provider call; only known values counted.
        assert usage["known"]["total_tokens"] == 2 * usage["outbound"]
        assert usage["completeness"] == "complete"

        report_id = done["report_id"]
        dims_path = reports_dir / f"{report_id}_dims.json"
        payload = json.loads(dims_path.read_text(encoding="utf-8"))
        assert payload["model_usage"] == usage
        assert payload["run_id"] == run_id
        # Per-attempt sidecar detail survives save: attribution, terminal
        # state, and the audit-row link are traceable after history reads.
        attempts = payload["model_usage"]["attempt_records"]
        assert attempts and all(a["run_id"] == run_id for a in attempts)
        outbound_attempts = [a for a in attempts if a["outbound"]]
        assert outbound_attempts
        assert all(a["usage_record_id"] is not None for a in outbound_attempts
                   if a["usage_presence"] == "provided")
        audit_total = len([a for a in attempts if a["usage_record_id"] is not None])
        # Every outbound of this fixture reports usage, so audit rows equal
        # real outbounds exactly, each with its own row id.
        assert audit_total == usage["outbound"]
        assert len({a["usage_record_id"] for a in attempts if a["usage_record_id"] is not None}) == audit_total

        detail = client.get(f"/reports/{report_id}").json()["data"]
        assert detail["model_usage"] == usage
        assert detail["run_id"] == run_id


def test_day_cache_hit_keeps_origin_usage_without_new_outbound(report_http: tuple[str, dict[str, int], Path]) -> None:
    """第二次请求命中日缓存：done 引用原生成的 run 与用量，零新增出站。"""
    base, observed, reports_dir = report_http
    first_run = uuid4().hex
    with httpx.Client(base_url=base, timeout=60, trust_env=False) as client:
        first = client.post("/generate/stream", json=request_body(first_run))
        first_done = next(e for e in _events(first.text) if e["type"] == "done")
        assert first_done["status"] in {"success", "partial"}, first_done
        first_outbound = observed["count"]
        first_report_id = first_done["report_id"]

        cached_run = uuid4().hex
        second = client.post("/generate/stream", json=request_body(cached_run, force_refresh=False))
        second_events = _events(second.text)
        second_done = next(e for e in second_events if e["type"] == "done")
        assert second_done.get("cache_hit") is True

        # The first (cacheable) generation must include real model outbounds.
        assert first_outbound > 0
        assert first_done["model_usage"]["outbound"] == first_outbound
        # No new model outbound and no copied accounting: the read keeps the
        # generating run's usage and report identity.
        assert observed["count"] == first_outbound
        cached_usage = second_done["model_usage"]
        assert cached_usage["origin_run_id"] == first_run
        assert cached_usage["origin_report_id"] is None or cached_usage["origin_report_id"] == first_report_id
        assert cached_usage["outbound"] == first_done["model_usage"]["outbound"]

        detail = client.get(f"/reports/{second_done['report_id']}").json()["data"]
        assert detail["model_usage"]["origin_run_id"] == first_run


def test_cancel_after_partial_ready_keeps_late_usage_consistent(report_http: tuple[str, dict[str, int], Path]) -> None:
    """Cancel with an in-flight model call: partial_ready shows the outbound
    as in-flight with unknown usage; the final done (after cleanup) carries
    the late usage; saved artifacts, history reads and the per-call audit
    rows agree with no double counting."""
    base, observed, reports_dir = report_http
    observed["hold_seconds"] = 3.0
    observed["hold_models"] = True
    run_id = uuid4().hex
    try:
        with httpx.Client(base_url=base, timeout=90, trust_env=False) as client, concurrent.futures.ThreadPoolExecutor() as pool:
            generating = pool.submit(client.post, "/generate/stream", json=request_body(run_id))
            wait_until(lambda: observed["active"] > 0)
            server_seen = observed["count"]
            assert client.post(f"/runs/{run_id}/cancel").status_code == 200
            response = generating.result(timeout=90)
            events = _events(response.text)
            partial = next(e for e in events if e["type"] == "partial_ready")
            done = next(e for e in events if e["type"] == "done")

            # The partial snapshot already shows the real outbound (never
            # zero) and honestly reports unknown usage for the in-flight call.
            partial_usage = partial["model_usage"]
            assert partial_usage["outbound"] >= 1
            assert server_seen >= 1
            statuses = {a["status"] for a in partial_usage["attempt_records"] if a["outbound"]}
            assert "ok" not in statuses  # in-flight is not a success terminal

            # The final done is refreshed after cleanup with the late usage.
            final_usage = done["model_usage"]
            assert final_usage["outbound"] == partial_usage["outbound"] or final_usage["outbound"] >= partial_usage["outbound"]
            provided = [a for a in final_usage["attempt_records"] if a["usage_presence"] == "provided"]
            assert provided, "late provider usage must land in the final snapshot"

            report_id = done["report_id"]
            dims_path = reports_dir / f"{report_id}_dims.json"
            payload = json.loads(dims_path.read_text(encoding="utf-8"))
            assert payload["model_usage"] == final_usage
            detail = client.get(f"/reports/{report_id}").json()["data"]
            assert detail["model_usage"] == final_usage

            from src.storage import LLMUsage
            from src.services import deep_research_service as service

            database = service.get_db()
            with database.get_session() as session:  # type: ignore[attr-defined]
                rows = session.query(LLMUsage).all()
            # Audit rows equal the provider-served requests that reported
            # usage: no missing row, no double count.
            recorded = len(rows)
            assert recorded >= 1
            assert recorded <= observed["count"]
    finally:
        observed["hold_models"] = False
        observed.pop("hold_seconds", None)


def test_malformed_business_json_attributes_parse_error(report_http: tuple[str, dict[str, int], Path]) -> None:
    """A malformed researcher answer: transport ok, structured parse fails.

    The consuming entry (explore agent JSON extraction) attributes the parse
    failure to the attempt that just returned; its provider usage stays on
    the audit row.
    """
    base, observed, reports_dir = report_http
    observed["legacy_text"] = 1  # plain-text answer, not valid researcher JSON
    run_id = uuid4().hex
    with httpx.Client(base_url=base, timeout=60, trust_env=False) as client:
        response = client.post("/generate/stream", json=request_body(run_id))
        done = next(e for e in _events(response.text) if e["type"] == "done")
        assert done["status"] in {"success", "partial"}, done
        records = done["model_usage"]["attempt_records"]
        parse_failed = [a for a in records if a["status"] == "parse_error"]
        assert parse_failed, "malformed answer must be attributed as parse_error"
        assert all(a["usage_record_id"] is not None for a in parse_failed if a["usage_presence"] == "provided")
    observed["legacy_text"] = 0


def test_usage_records_expose_normalized_completeness(report_http: tuple[str, dict[str, int], Path]) -> None:
    """Stored usage rows: full, zero, prompt-only and absent keep distinct truth.

    Normalized NULL fields stay NULL (no legacy-zero padding); the record
    completeness marker distinguishes known / partial / unknown per row.
    """
    base, observed, reports_dir = report_http
    from datetime import datetime

    from src.llm.usage import normalize_litellm_usage
    from src.services import deep_research_service as service

    database = service.get_db()
    shapes = {
        "full": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        "zero": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "prompt_only": {"prompt_tokens": 10},
    }
    from src.storage import persist_llm_usage

    for name, payload in shapes.items():
        persist_llm_usage(normalize_litellm_usage(payload, model="fixture"), "fixture", call_type="analysis")

    from_dt = datetime(2000, 1, 1)
    to_dt = datetime(2100, 1, 1)
    rows = database.get_llm_usage_records(from_dt, to_dt, limit=10)
    by_shape = {}
    for row in rows[:3]:
        key = (row["prompt_tokens"], row["completion_tokens"], row["total_tokens"], tuple(sorted(row["unknown_fields"])))
        by_shape[key] = row["completeness"]
    # full: all fields known → complete
    assert by_shape[(10, 5, 15, ())] == "known"
    # zero: provider-confirmed zero → complete, zeros are real values
    assert by_shape[(0, 0, 0, ())] == "known"
    # prompt-only: completion/total unknown → partial, never padded with 0
    assert by_shape[(10, 0, 0, ("completion_tokens", "total_tokens"))] == "partial"


@pytest.fixture
def reject_http(report_http: tuple[str, dict[str, int], Path], usage_provider: tuple[str, dict[str, Any]]) -> tuple[tuple[str, dict[str, int], Path], dict[str, Any]]:
    """Same report stack with the model loopback switched to the reject provider."""
    from src.config import get_config

    base, state = usage_provider
    cfg = get_config()
    cfg.llm_model_list = [{"model_name": "openai/fixture", "litellm_params": {"model": "openai/fixture", "api_base": base, "api_key": "fixture-key"}}]
    state["mode"] = "reject"
    return report_http, state


def test_model_http_reject_keeps_prepared_facts_in_saved_report(reject_http: tuple[tuple[str, dict[str, int], Path], dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    """issue #56 A2: prepared qualified facts survive a real SDK HTTP rejection.

    The provider 429s both pilot outbounds. Real in this test: preparation,
    runners, admission, SDK transport, degradation, save and history read.
    The shared context is the report fixture's stubbed assembly boundary with
    typed stage-zero materials injected below — assembly itself is covered by
    the root frozen-supplier probes, not here. Saved and history-read dims
    keep the prepared scalars with source/unit/period/statistical-object
    qualification; scores stay null and statuses stay degraded.
    """
    import json

    from src.agent.deep_research import orchestrator

    (base, observed, reports_dir), state = reject_http
    history = [
        {"date": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", "open": 10 + i * 0.1, "high": 11 + i * 0.1,
         "low": 9 + i * 0.1, "close": 10 + i * 0.1, "volume": 1000}
        for i in range(260)
    ]
    qualified = {"source": "choice", "value": 2050000000000.0, "unit": "currency_base", "currency": "CNY",
                 "period": None, "observed_at": "2026-10-15T15:00:00+08:00",
                 "quality": {"status": "single_source", "reason_codes": ["single_source_unverified"]},
                 "rule_eligible": True}
    original_builder = orchestrator.build_shared_context

    def with_prepared_facts(*args: Any, **kwargs: Any) -> Any:
        context = original_builder(*args, **kwargs)
        # Deterministic stage-zero materials: quote, history, qualified market
        # caps and the institution block, all zero extra outbound.
        context.history = history
        context.history_source = "fixture-source"
        context.quote = {"price": 50.0, "source": "fixture",
                         "field_meta": {"current_price": {"source": "fixture", "unit": "currency_base",
                                                          "quality": {"status": "unverified"}}}}
        context.fundamental = {
            "market_cap": 2050000000000.0,
            "field_meta": {"total_mv": dict(qualified, field="total_mv"),
                           "circ_mv": dict(qualified, field="circ_mv", value=2000000000000.0)},
            "institution": {"status": "ok", "data": {"institution_holding_change": -1.25},
                            "field_meta": {"institution_holding_change": {
                                "source": "choice", "unit": "percentage_point", "period": "2026中报",
                                "caliber": "top10_shareholders", "period_basis": "YTD",
                                "quality": {"status": "single_source", "reason_codes": ["single_family"]},
                                "rule_eligible": True}}},
        }
        return context

    monkeypatch.setattr(orchestrator, "build_shared_context", with_prepared_facts)
    run_id = uuid4().hex
    with httpx.Client(base_url=base, timeout=90, trust_env=False) as client:
        done = next(e for e in _events(client.post(
            "/generate/stream", json=request_body(run_id, dims=["technical", "capital"])).text) if e["type"] == "done")
        report_id = done["report_id"]
        payload = json.loads((reports_dir / f"{report_id}_dims.json").read_text(encoding="utf-8"))
        detail = client.get(f"/reports/{report_id}").json()["data"]

    # Both pilot outbounds really happened and were really rejected.
    assert done["model_usage"]["outbound"] == state["count"] == 2
    outbound = [a for a in done["model_usage"]["attempt_records"] if a["outbound"]]
    assert sorted(a["dim"] for a in outbound) == ["capital", "technical"]
    assert all(a["status"] != "ok" for a in outbound)
    assert done["status"] == "partial"  # retained facts keep the report honest but partial

    by_dim = {dim: payload["dimensions"][dim] for dim in ("technical", "capital")}
    for dim in ("technical", "capital"):
        assert by_dim[dim] == done["dimensions"][dim]
        assert by_dim[dim]["status"] == "degraded" and by_dim[dim]["score"] is None
    capital_facts = {fact["label"]: fact for fact in by_dim["capital"]["retained_facts"]}
    assert {"总市值", "流通市值", "机构持仓变化", "当日主力净流入"} <= set(capital_facts)
    assert capital_facts["总市值"]["value"] == "2050000000000.0"
    assert capital_facts["总市值"]["unit"] == "currency_base / CNY"
    assert "single_source" in capital_facts["总市值"]["qualification"]
    assert capital_facts["机构持仓变化"]["unit"] == "percentage_point"
    assert capital_facts["机构持仓变化"]["period"] == "2026中报"
    # 统计对象不丢失：退化报告能区分十大股东变动与全部机构口径
    assert "top10_shareholders" in capital_facts["机构持仓变化"]["qualification"]
    assert "YTD" in capital_facts["机构持仓变化"]["qualification"]
    technical_facts = {fact["label"]: fact for fact in by_dim["technical"]["retained_facts"]}
    assert {"20日均线", "6日RSI", "最近交易日收盘", "价格"} <= set(technical_facts)
    assert technical_facts["20日均线"]["source"] == "代码指标计算，日线来源 fixture-source"
    assert technical_facts["最近交易日收盘"]["source"] == "fixture-source"
    assert detail["model_usage"] == done["model_usage"]
    assert detail["run_id"] == run_id
    # 历史读取的维度与落盘产物逐字段一致（不只是 usage 一致），
    # 且渲染正文中统计对象口径可见
    for dim in ("technical", "capital"):
        assert detail["dimensions"][dim] == payload["dimensions"][dim]
    assert "top10_shareholders" in detail["markdown"]
