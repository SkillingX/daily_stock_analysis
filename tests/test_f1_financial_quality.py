"""Verify F1 counts and eligibility reach downstream rules and actual report templates.
Run: python -m pytest tests/test_f1_financial_quality.py
Requires project dependencies; synthetic normalized facts, no provider requests.
"""
from pathlib import Path
from typing import Any
from datetime import datetime, timezone
from types import SimpleNamespace
import json

from tests.test_fundamentals_data_quality import financial_sources

import pytest
from jinja2 import Environment, FileSystemLoader

from data_provider.cross_source_validator import AnchorQuality, AnchorReading, adopted_field_record
from src.config import Config
from src.deep_research_dims.context import SharedContext
from src.deep_research_dims.fundamental_dim import build_fundamental_dim
from src.deep_research_dims.scenarios_dim import build_scenarios_dim
from src.deep_research_dims.six_dim import build_six_dim


def context(values: dict[str, float], conflicts: tuple[str, ...] = (), periods: dict[str, str] | None = None) -> SharedContext:
    aliases = {"pe_ratio": "pe_ttm", "pb_ratio": "pb", "revenue_yoy": "revenue_growth"}
    calibers = {"pe_ratio": "TTM", "pb_ratio": "MRQ", "roe": "weighted_roe", "gross_margin": "gross_margin", "revenue_yoy": "operating_revenue_yoy", "net_profit_yoy": "parent_net_profit_yoy"}
    records: dict[str, Any] = {}
    for field, value in values.items():
        reading = AnchorReading("mx", value, caliber=calibers[field], unit="multiple" if field in {"pe_ratio", "pb_ratio"} else "percentage_point", period=(periods or {}).get(field))
        records[field] = adopted_field_record(field, reading, AnchorQuality(status="conflict" if field in conflicts else "unverified"))
    return SharedContext("600519", "样本", "2026-10-09", fundamental={**{aliases.get(k, k): v for k, v in values.items()}, "field_meta": records})


def test_revenue_only_has_original_65_score_and_partial_counts() -> None:
    dim = build_fundamental_dim(context({"revenue_yoy": 10})).model_dump(mode="json")
    assert dim["health_score"] == 65 and dim["status"] == "degraded"
    quality = dim["data_quality"]
    assert (quality["obtained_count"], quality["rule_usable_count"], quality["health_components"]) == (1, 1, 1)
    assert quality["state"] == "partial"


def test_conflicting_pe_cannot_feed_f1_six_dim_or_scenarios() -> None:
    ctx = context({"pe_ratio": 999}, conflicts=("pe_ratio",))
    f1 = build_fundamental_dim(ctx).model_dump(mode="json")
    assert f1["valuation_detail"]["score"] is None and f1["health_score"] is None
    six = build_six_dim(ctx, f1_payload=f1).model_dump(mode="json")
    fundamental = next(d for d in six["framework"]["dimensions"] if d["dimension"] == "基本面")
    guarded = [item for item in fundamental["indicators"] if item["name"] in {"估值", "业务与财务健康"}]
    assert len(guarded) == 2
    for item in guarded:
        assert item["score"] == 50 and item["confidence"] == "low" and item["data_gap"]
    scenarios = build_scenarios_dim(ctx, 20)
    assert scenarios.current_pe_ttm is None


def test_ytd_and_single_quarter_do_not_form_growth_scissors() -> None:
    ctx = context({"revenue_yoy": 10, "net_profit_yoy": 30}, periods={"revenue_yoy": "2026-06-30 YTD", "net_profit_yoy": "2026-06-30 single_quarter"})
    f1 = build_fundamental_dim(ctx)
    assert f1.growth_quality["scissors"] is None
    assert f1.health_score == 72.5


def test_full_acquisition_is_distinct_from_usable_components_and_verification() -> None:
    f1 = build_fundamental_dim(context({"pe_ratio": 0, "pb_ratio": 2, "roe": 0, "gross_margin": 0, "revenue_yoy": 0, "net_profit_yoy": 0})).model_dump(mode="json")
    quality = f1["data_quality"]
    assert (quality["obtained_count"], quality["rule_usable_count"], quality["health_components"]) == (6, 6, 5)
    assert quality["state"] == "complete" and quality["warnings"]
    assert f1["profitability"]["roe"]["value"] == 0


def test_real_specialist_template_shows_counts_and_unverified_limitation() -> None:
    dim = build_fundamental_dim(context({"revenue_yoy": 10})).model_dump(mode="json")
    env = Environment(loader=FileSystemLoader(Path(__file__).parents[1] / "templates"))
    md = env.get_template("fundamentals_report.j2").render(stock_name="样本", stock_code="600519", financial=dim, business={}, sector={}, degraded_note=lambda value: "")
    assert "取得 1/6" in md and "规则可用 1/6" in md and "评分组件 1/5" in md
    assert "未核验" in md and "局部" in md


def test_actual_shared_context_binds_enabled_validation_to_its_adopted_pe(financial_sources: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from src.agent.tools import cross_validation_helpers
    from src.deep_research_dims import context as context_module
    from data_provider.cross_source_validator import CrossSourceValidator
    from src.config import Config
    quote = financial_sources["quote"]
    quote.pe_ratio = 999.0
    quote.currency = "CNY"
    quote.provider_timestamp = datetime.now(timezone.utc).isoformat()
    quote.field_meta = {"pe_ratio": {"caliber": "TTM"}, "pb_ratio": {"caliber": "MRQ"}}
    config = Config(realtime_source_priority="efinance", fundamental_cache_ttl_seconds=0, deep_research_cross_validate=True)
    monkeypatch.setattr("src.config.get_config", lambda: config)
    reads: list[tuple[str, str]] = []
    def read(name: str, value: float, code: str, field: str, period: str | None):
        reads.append((name, field))
        return AnchorReading(name, value, caliber="TTM", unit="multiple", observed_at=quote.provider_timestamp) if field == "pe_ratio" else None
    validator = CrossSourceValidator([SimpleNamespace(name=name, read=lambda code, field, period=None, name=name, value=value: read(name, value, code, field, period)) for name, value in [("mx", 30), ("ifind", 30.1)]])
    monkeypatch.setattr(cross_validation_helpers, "_get_validator", lambda: validator)
    ctx = context_module.build_shared_context("600519", "样本")
    assert ctx.fundamental["pe_ttm"] == 999
    f1 = build_fundamental_dim(ctx)
    assert f1.data_quality is not None
    assert f1.data_quality.fields["pe_ratio"].quality.status == "conflict"
    assert f1.valuation_detail["score"] is None
    assert reads.count(("mx", "pe_ratio")) == 1 and reads.count(("ifind", "pe_ratio")) == 1


def test_mismatched_metadata_cannot_attach_old_verified_to_a_new_scalar() -> None:
    ctx = context({"pe_ratio": 999})
    ctx.fundamental["field_meta"]["pe_ratio"]["quality"] = {"status": "verified"}
    ctx.fundamental["pe_ttm"] = 30
    evidence = build_fundamental_dim(ctx).data_quality.fields["pe_ratio"]
    assert evidence.quality.status == "unverified" and not evidence.rule_eligible


def test_shared_context_preserves_existing_conflict_when_validation_disabled(financial_sources: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from src.agent.tools import data_tools
    from src.deep_research_dims.context import _safe_fundamental
    ctx = context({"pe_ratio": 999}, conflicts=("pe_ratio",))
    record = ctx.fundamental["field_meta"]["pe_ratio"]
    raw = {"valuation": {"data": {"pe_ratio": 999}, "field_meta": {"pe_ratio": record}}}
    monkeypatch.setattr(data_tools._get_fetcher_manager(), "get_fundamental_context", lambda *args, **kwargs: raw)
    ctx.fundamental = _safe_fundamental("600519", ctx)
    assert build_fundamental_dim(ctx).data_quality.fields["pe_ratio"].quality.status == "conflict"
    assert build_fundamental_dim(ctx).valuation_detail["score"] is None


def test_malformed_fuyao_does_not_erase_acquired_primary_fields(financial_sources: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    import requests
    from src.agent.tools import data_tools
    from src.deep_research_dims.context import _safe_fundamental
    raw = {"growth": {"data": {"revenue_yoy": 10}, "field_meta": context({"revenue_yoy": 10}).fundamental["field_meta"]}}
    monkeypatch.setattr(data_tools._get_fetcher_manager(), "get_fundamental_context", lambda *args, **kwargs: raw)
    monkeypatch.setattr("src.config.get_config", lambda: Config(enable_fuyao=True, fuyao_endpoint="https://fixture.invalid", fuyao_api_key="fixture"))
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"code": 0, "data": [1]}))
    ctx = SharedContext("600519", "样本", "2026-10-09")
    ctx.fundamental = _safe_fundamental("600519", ctx)
    assert ctx.fundamental["revenue_growth"] == 10
    assert build_fundamental_dim(ctx).health_score == 65
    assert ctx.limitations


def test_validation_only_conflict_is_visible_evidence_but_not_a_rule_input(financial_sources: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from src.agent.tools import data_tools, cross_validation_helpers
    from src.deep_research_dims.context import _safe_fundamental
    from data_provider.cross_source_validator import CrossSourceValidator
    monkeypatch.setattr(data_tools._get_fetcher_manager(), "get_fundamental_context", lambda *args, **kwargs: {})
    monkeypatch.setattr("src.config.get_config", lambda: Config(deep_research_cross_validate=True))
    now = datetime.now(timezone.utc).isoformat()
    validator = CrossSourceValidator([SimpleNamespace(name=name, read=lambda code, field, period=None, name=name, value=value: AnchorReading(name, value, caliber="TTM", unit="multiple", observed_at=now) if field == "pe_ratio" else None) for name, value in (("mx", 30), ("ifind", 300))])
    monkeypatch.setattr(cross_validation_helpers, "_validator_instance", validator)
    ctx = SharedContext("600519", "样本", "2026-10-09")
    ctx.fundamental = _safe_fundamental("600519", ctx)
    dim = build_fundamental_dim(ctx)
    assert dim.data_quality.obtained_count == 1 and dim.data_quality.rule_usable_count == 0
    assert dim.data_quality.fields["pe_ratio"].quality.status == "conflict"
    assert dim.valuation_detail["score"] is None


def test_derived_percentage_rejects_zero_denominator() -> None:
    from decimal import Decimal
    from icontract import ViolationError
    from src.deep_research_dims.context import _financial_percentage
    assert _financial_percentage(Decimal(20), Decimal(100)) == Decimal(20)
    with pytest.raises(ViolationError):
        _financial_percentage(Decimal(20), Decimal(0))


def test_direct_fuyao_slow_body_keeps_shared_stage_deadline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from time import monotonic, sleep
    from data_provider.base import DataFetcherManager
    from src.agent.tools import data_tools
    from src.deep_research_dims import context as context_module, dim_cache

    payload = json.dumps({"code": 0, "data": {"item": [{"pe_ttm": 20.0, "pb_mrq": 2.0}]}}).encode()
    class SlowBody(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            for start in range(0, len(payload), 5):
                self.wfile.write(payload[start:start + 5])
                self.wfile.flush()
                sleep(0.025)
    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowBody)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cfg = Config(enable_fuyao=True, fuyao_endpoint=f"http://127.0.0.1:{server.server_port}", fuyao_api_key="fixture", fundamental_stage_timeout_seconds=0.1, fundamental_fetch_timeout_seconds=0.1)
    monkeypatch.setattr("src.config.get_config", lambda: cfg)
    monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path / "cache"))
    fund = context({"roe": 18.0, "gross_margin": 40.0, "revenue_yoy": 10.0, "net_profit_yoy": 10.0}).fundamental
    manager = DataFetcherManager(fetchers=[])
    monkeypatch.setattr(manager, "get_fundamental_context", lambda *args, **kwargs: {"growth": {"data": {"roe": 18.0, "gross_margin": 40.0, "revenue_yoy": 10.0, "net_profit_yoy": 10.0}, "field_meta": fund["field_meta"]}})
    monkeypatch.setattr(data_tools, "_fetcher_manager_singleton", manager)
    monkeypatch.setattr(context_module, "_safe_quote", lambda *args: {})
    monkeypatch.setattr(context_module, "_safe_history", lambda *args: [])
    monkeypatch.setattr(context_module, "_safe_chip", lambda *args: {})
    monkeypatch.setattr(context_module, "_safe_history_reports", lambda *args: [])
    try:
        start = monotonic()
        result = context_module.build_shared_context("600519", "样本")
        assert monotonic() - start < 0.22
        assert result.fundamental["revenue_growth"] == 10
        assert result.fundamental["pe_ttm"] is None
        assert any("Fuyao补值未完成" in warning for warning in result.limitations)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(1)


def test_quality_json_round_trip_and_financial_cache_isolation(financial_sources: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from src.deep_research_dims import dim_cache
    from src.schemas.deep_research_dims import parse_dim
    original = build_fundamental_dim(context({"revenue_yoy": 10}))
    dim_cache.save_cached_dim("600519", "fundamental", original.model_dump(mode="json"))
    cached = dim_cache.load_cached_dim("600519", "fundamental")
    assert parse_dim("fundamental", cached) == original
    dim_cache.save_cached_dim("600519", "business", {"narrative": "保留"})
    cfg = Config(deep_research_cross_validate=True)
    monkeypatch.setattr("src.config.get_config", lambda: cfg)
    assert dim_cache.load_cached_dim("600519", "fundamental") is None
    assert dim_cache.load_cached_dim("600519", "business") == {"narrative": "保留"}


def test_shared_context_warm_mode_switch_and_unknown_family_outlier(financial_sources: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from src.agent.tools import cross_validation_helpers
    from src.deep_research_dims.context import build_shared_context
    from data_provider.cross_source_validator import CrossSourceValidator
    quote = financial_sources["quote"]
    quote.pe_ratio = 30.0
    quote.provider_timestamp = datetime.now(timezone.utc).isoformat()
    quote.field_meta = {"pe_ratio": {"caliber": "TTM"}, "pb_ratio": {"caliber": "MRQ"}}
    config = Config(realtime_source_priority="efinance", fundamental_cache_ttl_seconds=0, deep_research_cross_validate=False)
    monkeypatch.setattr("src.config.get_config", lambda: config)
    reads: list[str] = []
    value = {"unknown": 30.0}
    def read(name: str, field: str):
        reads.append(field)
        return AnchorReading(name, value["unknown"] if name == "unknown" else 30.1, caliber="TTM", unit="multiple", observed_at=quote.provider_timestamp) if field == "pe_ratio" else None
    validator = CrossSourceValidator([SimpleNamespace(name=name, read=lambda code, field, period=None, name=name: read(name, field)) for name in ("ifind", "unknown")])
    monkeypatch.setattr(cross_validation_helpers, "_validator_instance", validator)
    off = build_shared_context("600519", "样本")
    assert not reads and build_fundamental_dim(off).data_quality.fields["pe_ratio"].quality.status == "unverified"
    config.deep_research_cross_validate = True
    on = build_shared_context("600519", "样本")
    assert build_fundamental_dim(on).data_quality.fields["pe_ratio"].quality.status == "verified"
    config.deep_research_cross_validate = False
    reads.clear()
    assert build_fundamental_dim(build_shared_context("600519", "样本")).data_quality.fields["pe_ratio"].quality.status == "unverified"
    assert not reads
    config.deep_research_cross_validate = True
    value["unknown"] = 300.0
    conflict = build_fundamental_dim(build_shared_context("600519", "样本"))
    assert conflict.data_quality.fields["pe_ratio"].quality.status == "conflict"
    assert conflict.valuation_detail["score"] is None


@pytest.mark.parametrize("currency,period,expected", [("CNY", "2025年报", 20), (None, "2025年报", None), ("CNY", "2024年报", None)])
def test_fuyao_derived_roe_is_period_bound_evidence_not_weighted_score(financial_sources: dict[str, Any], monkeypatch: pytest.MonkeyPatch, currency: str | None, period: str, expected: int | None) -> None:
    import requests
    from src.deep_research_dims.context import _safe_fundamental
    from src.agent.tools import data_tools
    manager = data_tools._get_fetcher_manager()
    monkeypatch.setattr(manager, "get_fundamental_context", lambda code, **kwargs: {})
    monkeypatch.setattr("src.config.get_config", lambda: Config(enable_fuyao=True, fuyao_endpoint="https://fixture.invalid", fuyao_api_key="fixture", fundamental_stage_timeout_seconds=1))
    def get(url: str, **kwargs: Any):
        data = {"report_period": "2025年报", "parent_holder_net_profit": 20, "operating_income": 100, "operating_costs": 50, "unit": "元", "currency": currency}
        if "balance-sheets" in url:
            data = {"report_period": period, "holder_equity_total": 100, "unit": "元", "currency": currency}
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"code": 0, "data": {"item": [data]}})
    monkeypatch.setattr(requests, "get", get)
    ctx = SharedContext("600519", "样本", "2026-10-09")
    ctx.fundamental = _safe_fundamental("600519", ctx)
    assert ctx.fundamental["roe"] == expected
    f1 = build_fundamental_dim(ctx)
    assert f1.profitability["roe"]["score"] is None
    if expected is not None:
        evidence = f1.data_quality.fields["roe"]
        assert evidence.reading.caliber == "parent_net_profit/ending_equity" and evidence.reading.period == "2025-12-31"


@pytest.mark.parametrize("values", [{}, {"revenue_yoy": 10}, {"pe_ratio": 0}, {"roe": 18, "revenue_yoy": 10}])
def test_service_api_history_preserve_quality_and_actual_markdown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, values: dict[str, float]) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.v1.endpoints.fundamentals import router
    from src.services import fundamentals_service as svc
    from src.storage import get_db
    from src.deep_research_dims import context as context_module
    ctx = context(values)
    # This seam replaces normalized transport facts; F1, rule consumers, service,
    # storage, actual template and HTTP serialization all execute unchanged.
    monkeypatch.setattr(context_module, "build_shared_context", lambda *args: ctx)
    monkeypatch.setattr(svc, "_research_dim", lambda *args: {"status": "degraded"})
    monkeypatch.setattr(svc, "_REPORT_DIR", tmp_path)
    app = FastAPI()
    app.include_router(router, prefix="/fundamentals")
    with TestClient(app) as client:
        out = client.post("/fundamentals/generate", params={"stock_code": "600519", "stock_name": "样本"}).json()
        assert out["status"] == "success"
        report_id = out["report_id"]
        try:
            expected = build_fundamental_dim(ctx).model_dump(mode="json")
            assert out["dims"]["financial"] == expected
            detail = client.get(f"/fundamentals/reports/{report_id}").json()["data"]
            assert json.loads(detail["dims_json"])["financial"] == expected
            assert "取得 " in detail["markdown"] and "局部" in detail["markdown"]
        finally:
            get_db().delete_fundamentals_report(report_id)


def test_legacy_history_is_unverified_without_rewriting_saved_file(tmp_path: Path) -> None:
    from src.services.fundamentals_service import report_markdown
    path = tmp_path / "legacy.md"
    path.write_text("旧财务结论", encoding="utf-8")
    assert "未核验" in report_markdown({"md_path": str(path), "dims_json": "{}"})
    assert path.read_text(encoding="utf-8") == "旧财务结论"


@pytest.mark.parametrize("template", ["deep_research_dual_track.j2", "deep_research_report_v2.j2"])
def test_actual_dual_track_templates_share_specialist_f1_quality(template: str) -> None:
    from tests.test_deep_research_dims import _all_default_dims
    from src.deep_research_dims.render import build_view, render_markdown
    dims = _all_default_dims()
    dims["fundamental"] = build_fundamental_dim(context({"revenue_yoy": 10}))
    md = render_markdown(build_view("样本", "600519", "2026-10-09", [], dims, []), template_name=template)
    assert "取得 1/6" in md and "规则可用 1/6" in md and "评分组件 1/5" in md
    assert "局部" in md and "未核验" in md


@pytest.mark.parametrize("status", ["unverified", "verified", "conflict", "not_comparable"])
def test_public_overview_passes_existing_quality_without_provider_io(financial_sources: dict[str, Any], status: str) -> None:
    from src.agent.tools import data_tools
    from src.services.analysis_context_builder import AnalysisContextBuilder, PipelineAnalysisArtifacts
    from src.analysis_context_pack_overview import render_analysis_context_pack_overview, extract_analysis_context_pack_overview
    from api.v1.schemas.history import AnalysisContextPackOverview
    raw = data_tools._get_fetcher_manager().get_fundamental_context("600519")
    raw["growth"]["field_meta"]["revenue_yoy"]["raw_value"] = "secret-supplier-body"
    raw["growth"]["field_meta"]["revenue_yoy"]["quality"] = {"status": status}
    artifacts = PipelineAnalysisArtifacts("600519", "样本", "cn", None, {}, {}, None, None, None, raw, None, 0, {})
    pack = AnalysisContextBuilder.build(artifacts)
    overview = render_analysis_context_pack_overview(pack)
    assert "secret-supplier-body" not in json.dumps(overview)
    quality = next(b for b in overview["blocks"] if b["key"] == "fundamentals")["financial_quality"]
    assert quality["revenue_yoy"]["status"] == status
    persisted = extract_analysis_context_pack_overview({"analysis_context_pack_overview": overview})
    assert persisted["blocks"] == overview["blocks"]
    assert AnalysisContextPackOverview.model_validate(persisted).blocks


@pytest.mark.parametrize("budget", [0, 0.05])
def test_shared_stage_zero_or_slow_validation_exits_within_budget(financial_sources: dict[str, Any], monkeypatch: pytest.MonkeyPatch, budget: float) -> None:
    from time import monotonic
    from threading import Event
    from src.agent.tools import cross_validation_helpers
    from src.deep_research_dims.context import build_shared_context
    from data_provider.cross_source_validator import CrossSourceValidator
    cfg = Config(deep_research_cross_validate=True, realtime_source_priority="efinance", fundamental_cache_ttl_seconds=0, fundamental_stage_timeout_seconds=budget)
    monkeypatch.setattr("src.config.get_config", lambda: cfg)
    event = Event()
    reads: list[str] = []
    def slow(code: str, field: str, period: str | None = None):
        reads.append(field)
        event.wait(2)
        return None
    monkeypatch.setattr(cross_validation_helpers, "_validator_instance", CrossSourceValidator([SimpleNamespace(name="ifind", read=slow)]))
    start = monotonic()
    try:
        ctx = build_shared_context("600519", "样本")
        assert monotonic() - start < 0.3
        assert "cross_validation" in ctx.fundamental
        if budget == 0:
            assert not reads
        else:
            assert reads
            assert any("timeout" in a["quality"]["reason_codes"] for a in ctx.fundamental["cross_validation"]["anchors"].values())
    finally:
        event.set()
