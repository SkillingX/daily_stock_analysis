"""Exercise specialist refresh through real caches, service, storage and HTTP routes.
Run: python -m pytest tests/test_fundamentals_refresh.py
Requires project dependencies; owned SQLite/temporary files and controlled provider IO.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any
import json
from copy import deepcopy

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_fundamentals_data_quality import financial_sources
from src.config import Config
from src.services import fundamentals_service as svc
from src.storage import get_db
from data_provider.mx_data_adapter import MXClient, MXSource, financial_refresh


@pytest.fixture
def refresh_inputs(financial_sources: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    from src.agent.tools import data_tools, cross_validation_helpers
    from src.deep_research_dims import dim_cache
    from data_provider.cross_source_validator import CrossSourceValidator
    import requests
    financial_sources["frame"] = pd.DataFrame()
    quote = financial_sources["quote"]
    quote.pe_ratio, quote.pb_ratio = 30.0, None
    quote.field_meta = {"pe_ratio": {"caliber": "TTM"}}
    cfg = Config(realtime_source_priority="efinance", fundamental_cache_ttl_seconds=3600)
    monkeypatch.setattr("src.config.get_config", lambda: cfg)
    manager = data_tools._get_fetcher_manager()
    client = MXClient(api_key="fixture", ttl=3600)
    source = MXSource(client)
    monkeypatch.setattr(manager, "_mx_source", source)
    monkeypatch.setattr(cross_validation_helpers, "_validator_instance", CrossSourceValidator([source]))
    monkeypatch.setattr(svc, "_REPORT_DIR", tmp_path / "reports")
    state: dict[str, Any] = {"calls": [], "roe": None, "error": False, "config": cfg, "client": client, "manager": manager}
    def post(url: str, **kwargs: Any):
        query = kwargs["json"]["toolQuery"]
        state["calls"].append(query)
        rows = {"市盈率TTM": 30.0} if "最新价" in query else {"净资产收益率ROE(加权)(%)": state["roe"]}
        table = {"headName": ["2025年报"], **{f"r{i}": [value] for i, value in enumerate(rows.values())}}
        names = {f"r{i}": name for i, name in enumerate(rows)}
        payload = {"error": "upstream unavailable"} if state["error"] else {"status": 0, "data": {"data": {"searchDataResultDTO": {"dataTableDTOList": [{"table": table, "nameMap": names}]}}}}
        return SimpleNamespace(status_code=200, json=lambda: payload)
    monkeypatch.setattr(requests, "post", post)
    for code in ("600519", "000001"):
        dim_cache.save_cached_dim(code, "business", {"dim": "business", "status": "ok", "narrative": "cached业务画像"})
    yield state
    rows, _ = get_db().list_fundamentals_reports(limit=200)
    for record in rows:
        full = get_db().get_fundamentals_report(record["id"])
        if full and Path(full["md_path"]).parent == tmp_path / "reports":
            get_db().delete_fundamentals_report(record["id"])


@pytest.mark.parametrize("initial_error,cross_validate", [(False, False), (True, False), (True, True)])
def test_real_source_manager_snapshot_and_same_day_caches_are_bypassed(refresh_inputs: dict[str, Any], initial_error: bool, cross_validate: bool) -> None:
    from src.deep_research_dims import dim_cache
    state = refresh_inputs
    state["error"] = initial_error
    state["config"].deep_research_cross_validate = cross_validate
    # Warm the actual raw-query cache, including an error/partial financial reply.
    state["client"].query_financials("600519", None)
    state["client"].query_financials("000001", None)
    state["manager"].get_fundamental_context("000001", budget_seconds=25)
    other_cache = {key: deepcopy(value) for key, value in state["manager"]._fundamental_cache.items() if key.startswith("000001|")}
    dim_cache.save_snapshot("stage0_unrelated_000001", {"sentinel": True})
    first = svc.generate_fundamentals_report("600519", "样本")
    assert first["status"] == "success" and first["dims"]["financial"]["profitability"]["roe"]["value"] is None
    assert state["manager"]._fundamental_cache
    before = len(state["calls"])
    business_file = Path(dim_cache._CACHE_DIR) / "600519_business.json"
    business_bytes = business_file.read_bytes()
    state.update(roe=18.0, error=False)
    ordinary = svc.generate_fundamentals_report("600519", "样本")
    assert ordinary["status"] == "already_exists" and ordinary["report_id"] == first["report_id"]
    assert len(state["calls"]) == before
    refreshed = svc.generate_fundamentals_report("600519", "样本", force_refresh=True)
    assert refreshed["status"] == "success" and refreshed["report_id"] != first["report_id"]
    assert refreshed["dims"]["financial"]["profitability"]["roe"]["value"] == 18
    assert refreshed["dims"]["financial"]["data_quality"]["fields"]["roe"]["reading"]["period"] == "2025-12-31"
    assert refreshed["dims"]["financial"]["data_quality"]["state"] == "partial"
    assert len(state["calls"]) > before
    assert all("600519" in query for query in state["calls"][before:])
    assert len([query for query in state["calls"][before:] if "最新价" not in query]) == 1
    assert {key: value for key, value in state["manager"]._fundamental_cache.items() if key.startswith("000001|")} == other_cache
    assert dim_cache.load_snapshot("stage0_unrelated_000001", ttl_hours=24) == {"sentinel": True}
    assert business_file.read_bytes() == business_bytes
    count = len(state["calls"])
    state["client"].query_financials("000001", None)
    assert len(state["calls"]) == count
    latest = svc.generate_fundamentals_report("600519", "样本")
    assert latest["report_id"] == refreshed["report_id"]
    assert get_db().get_fundamentals_report(first["report_id"])
    assert financial_refresh.get() is None


def test_failed_refresh_does_not_replace_prior_saved_report(refresh_inputs: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    first = svc.generate_fundamentals_report("600519", "样本")
    db = get_db()
    with monkeypatch.context() as patch:
        patch.setattr(db, "save_fundamentals_report", lambda record: False)
        failed = svc.generate_fundamentals_report("600519", "样本", force_refresh=True)
        assert failed["status"] == "failed" and failed["report_id"] is None
    assert svc.generate_fundamentals_report("600519", "样本")["report_id"] == first["report_id"]
    import src.deep_research_dims.context as context_module
    with monkeypatch.context() as patch:
        patch.setattr(context_module, "build_shared_context", lambda *args: (_ for _ in ()).throw(RuntimeError("fixture failure")))
        with pytest.raises(RuntimeError):
            svc.generate_fundamentals_report("600519", "样本", force_refresh=True)
    assert financial_refresh.get() is None
    assert svc.generate_fundamentals_report("600519", "样本")["report_id"] == first["report_id"]


def test_refresh_reaches_a_separate_validator_clients_stale_raw_cache(refresh_inputs: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from data_provider.cross_source_validator import CrossSourceValidator
    from src.agent.tools import cross_validation_helpers
    state = refresh_inputs
    first = svc.generate_fundamentals_report("600519", "样本")
    assert first["dims"]["financial"]["profitability"]["roe"]["value"] is None
    cv_client = MXClient(api_key="fixture", ttl=3600)
    state["roe"] = 300.0
    cv_client.query_financials("600519", None)
    monkeypatch.setattr(cross_validation_helpers, "_validator_instance", CrossSourceValidator([MXSource(cv_client)]))
    state["roe"] = 18.0
    state["config"].deep_research_cross_validate = True
    refreshed = svc.generate_fundamentals_report("600519", "样本", force_refresh=True)
    roe = refreshed["dims"]["financial"]["data_quality"]["fields"]["roe"]
    assert roe["reading"]["value"] == 18.0 and roe["quality"]["status"] == "single_source" and roe["rule_eligible"] is True
    assert cv_client.query_financials("600519", None, field="roe")["净资产收益率ROE(加权)(%)"] == 18.0


def test_two_concurrent_refreshes_keep_distinct_files_and_creation_order(refresh_inputs: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    # Delay A at the real save boundary; B is created later but saves first.
    db = get_db()
    save = db.save_fundamentals_report
    a_started, b_saved = Event(), Event()
    def ordered_save(record: dict[str, Any]) -> bool:
        if record["stock_name"] == "A":
            a_started.set()
            assert b_saved.wait(5)
            return save(record)
        result = save(record)
        b_saved.set()
        return result
    monkeypatch.setattr(db, "save_fundamentals_report", ordered_save)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(svc.generate_fundamentals_report, "600519", "A", True)
        assert a_started.wait(5)
        b = pool.submit(svc.generate_fundamentals_report, "600519", "B", True)
        a_out, b_out = a.result(timeout=5), b.result(timeout=5)
    assert a_out["status"] == b_out["status"] == "success"
    assert a_out["report_id"] != b_out["report_id"]
    for out in (a_out, b_out):
        record = db.get_fundamentals_report(out["report_id"])
        assert record["stock_name"] == out["stock_name"]
        assert Path(record["md_path"]).read_text(encoding="utf-8") == out["markdown"]
        assert json.loads(record["dims_json"]) == json.loads(json.dumps(out["dims"], default=str))
    rows, _ = db.list_fundamentals_reports("600519")
    assert rows[0]["id"] == b_out["report_id"]
    assert svc.generate_fundamentals_report("600519", "样本")["report_id"] == b_out["report_id"]


def test_same_creation_time_uses_the_same_id_order_in_list_and_reuse(refresh_inputs: dict[str, Any]) -> None:
    db = get_db()
    now = datetime.now()
    path = svc.get_fundamentals_dir() / "tie.md"
    path.write_text("tie")
    for suffix in (1, 2):
        assert db.save_fundamentals_report({"id": f"fd_{now:%Y%m%d%H%M}_{suffix}", "stock_code": "600519", "md_path": str(path), "dims_json": "{}", "created_at": now})
    rows, _ = db.list_fundamentals_reports("600519")
    assert rows[0]["id"].endswith("_2")
    assert svc.generate_fundamentals_report("600519", "样本")["report_id"] == rows[0]["id"]


def test_api_refresh_validation_and_version_compatibility(refresh_inputs: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from api.v1.endpoints.fundamentals import router
    app = FastAPI()
    app.include_router(router, prefix="/fundamentals")
    with TestClient(app) as client:
        params = {"stock_code": "600519", "stock_name": "样本"}
        first = client.post("/fundamentals/generate", params=params).json()
        assert first["status"] == "success"
        for invalid in ("1", "yes", "garbage", "", "null"):
            response = client.post("/fundamentals/generate", params={**params, "force_refresh": invalid})
            assert response.status_code == 422
        normal = client.post("/fundamentals/generate", params={**params, "force_refresh": "false"}).json()
        assert normal["report_id"] == first["report_id"]
        refreshed = client.post("/fundamentals/generate", params={**params, "force_refresh": "true"}).json()
        assert refreshed["status"] == "success" and refreshed["report_id"] != first["report_id"]
        for report_id in (first["report_id"], refreshed["report_id"]):
            assert client.get(f"/fundamentals/reports/{report_id}").status_code == 200
            assert client.get(f"/fundamentals/reports/{report_id}/markdown").status_code == 200
        import src.deep_research_dims.context as context_module
        monkeypatch.setattr(context_module, "build_shared_context", lambda *args: (_ for _ in ()).throw(RuntimeError("fixture failure")))
        failed = client.post("/fundamentals/generate", params={**params, "force_refresh": "true"})
        assert failed.status_code == 500
        assert client.get("/fundamentals/reports", params={"stock_code": "600519"}).json()["data"][0]["id"] == refreshed["report_id"]
