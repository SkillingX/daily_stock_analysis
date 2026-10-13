"""Annual SDK rows reach real statement calculations with their original meaning."""

from datetime import date
from time import monotonic

import pytest

from tests.test_choice_integration import choice_transport
from data_provider.choice_adapter import ChoiceAdapter


@pytest.fixture
def annual_transport(choice_transport, monkeypatch):
    snapshot_transport = ChoiceAdapter._native_request
    def native_request(method, args, options, deadline):
        if method != "css" or "OPERATEREVENEW" not in args[1]:
            return snapshot_transport(method, args, options, deadline)
        assert "Type=1" in options and "CurType=2" in options and "DataAdjustType=1" in options
        year = int(options.split("ReportDate=")[1][:4])
        values = {"OPERATEREVENEW": 100.0 if year == 2025 else 80.0, "OPERATEEXPNEW": 60.0 if year == 2025 else 40.0,
                  "PARENTNETPROFITNEW": 10.0 if year == 2025 else 20.0, "GROSSMARGIN": 40.0,
                  "ROEWA": 25.0, "GPMARGIN": 40.0 if year == 2025 else 50.0, "NETOPERATECASHFLOWNEW": -3.0,
                  "SUMASSETNEW": 1000.0, "SUMLIABNEW": 300.0, "SUMPARENTEQUITYNEW": 200.0,
                  "BASICEPSNEW": 0.0, "ACTUALISSUINGDATE": f"{year+1}/4/28"}
        fields = args[1].split(",")
        return {"codes": [args[0]], "indicators": fields, "dates": [date.today().isoformat()], "data": {args[0]: [values.get(field) for field in fields]}}
    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(native_request))
    return choice_transport


@pytest.fixture
def financial_client(annual_transport, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from api.app import create_app
    from api.deps import get_config_dep
    from src.services import financial_analysis_service
    from src.storage import DatabaseManager
    DatabaseManager.reset_instance()
    annual_transport.database_path = str(tmp_path / "financial.db")
    monkeypatch.setenv("ADMIN_AUTH_ENABLED", "false")
    monkeypatch.setattr(financial_analysis_service, "_REPORT_DIR", tmp_path / "reports")
    app = create_app(static_dir=tmp_path / "static")
    app.dependency_overrides[get_config_dep] = lambda: annual_transport
    yield TestClient(app)
    DatabaseManager.reset_instance()


def test_snapshot_valuation_failure_is_partial_through_report_and_download(financial_client, monkeypatch):
    from data_provider.cross_source_validator import SourceReadError
    transport = ChoiceAdapter._native_request
    def unavailable(method, args, options, deadline):
        if method == "css" and args[1] == "PETTM,PB,MV":
            raise SourceReadError("choice", "permission_denied")
        return transport(method, args, options, deadline)
    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(unavailable))
    response = financial_client.post("/api/v1/financial-analysis/generate?stock_code=300260")
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["status"] == "partial"
    assert report["analysis"]["valuation"]["score"] is None
    assert "估值数据缺失或不可用于规则" in report["analysis"]["gaps"]
    assert "持有或加仓" not in report["markdown"]
    detail = financial_client.get(f"/api/v1/financial-analysis/reports/{report['report_id']}").json()["data"]
    download = financial_client.get(f"/api/v1/financial-analysis/reports/{report['report_id']}/markdown?download=1")
    assert report["markdown"] == detail["markdown"] == download.text
    assert detail["analysis_json"]["gaps"] == report["analysis"]["gaps"]


def test_authentication_failure_is_retried_after_recovery(financial_client, monkeypatch):
    from data_provider.cross_source_validator import SourceReadError
    transport = ChoiceAdapter._native_request
    failed = True
    def native(method, args, options, deadline):
        if failed:
            raise SourceReadError("choice", "authentication_failed")
        return transport(method, args, options, deadline)
    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(native))
    first = financial_client.post("/api/v1/financial-analysis/generate?stock_code=300260").json()
    assert first["status"] == "partial"
    assert first["analysis"]["years"] == []
    failed = False
    second = financial_client.post("/api/v1/financial-analysis/generate?stock_code=300260").json()
    assert second["status"] == "success"
    assert second["analysis"]["gaps"] == []
    assert len(second["analysis"]["years"]) == 4
    assert second["report_id"] != first["report_id"]
    repeated = financial_client.post("/api/v1/financial-analysis/generate?stock_code=300260").json()
    assert repeated["status"] == "already_exists"
    assert repeated["report_id"] == second["report_id"]
    assert financial_client.get(f"/api/v1/financial-analysis/reports/{first['report_id']}").status_code == 200


def test_failed_refresh_does_not_resurface_an_older_complete_report(financial_client, monkeypatch):
    from data_provider.cross_source_validator import SourceReadError
    transport = ChoiceAdapter._native_request
    original = financial_client.post("/api/v1/financial-analysis/generate?stock_code=300260").json()
    def failed(*args, **kwargs):
        raise SourceReadError("choice", "authentication_failed")
    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(failed))
    failed_report = financial_client.post("/api/v1/financial-analysis/generate?stock_code=300260&force_refresh=true").json()
    assert failed_report["status"] == "partial"
    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(transport))
    retry = financial_client.post("/api/v1/financial-analysis/generate?stock_code=300260").json()
    assert retry["status"] == "success"
    assert retry["report_id"] not in {original["report_id"], failed_report["report_id"]}


def test_conflicting_pb_cannot_drive_financial_score_or_action(annual_transport):
    from data_provider.cross_source_validator import AnchorQuality, AnchorReading, adopted_field_record
    from src.services.financial_analysis_service import _score_dims, _render
    series = ChoiceAdapter().annual_reports("300260", deadline=monotonic()+20)
    metadata = adopted_field_record("pb_ratio", AnchorReading("choice", 1.0, unit="multiple", caliber="MRQ"), AnchorQuality(status="conflict"))
    result = _score_dims(series, {"pb": 1.0, "field_meta": {"pb_ratio": metadata}})
    assert result["valuation"]["pb"] is None
    assert result["valuation"]["score"] is None
    markdown = _render("fixture", "300260", "2026-10-13", "fixture", result)
    assert "持有或加仓" not in markdown and "PB 低位" not in markdown


def test_annual_valuation_scalar_cannot_bypass_snapshot_quality(annual_transport):
    from src.services.financial_analysis_service import _score_dims
    series = ChoiceAdapter().annual_reports("300260", deadline=monotonic()+20)
    series["valuation"] = {"pe_ttm": 10.0, "pb_mrq": 1.0}
    result = _score_dims(series, {})
    assert result["valuation"]["pe_ttm"] is None
    assert result["valuation"]["pb"] is None
    assert result["valuation"]["score"] is None


def test_older_annual_gap_stays_partial_until_recovered(financial_client, monkeypatch):
    transport = ChoiceAdapter._native_request
    missing = True
    def native(method, args, options, deadline):
        reply = transport(method, args, options, deadline)
        if missing and method == "css" and "OPERATEREVENEW" in args[1] and "ReportDate=2023-12-31" in options:
            reply["data"][args[0]][reply["indicators"].index("GPMARGIN")] = None
        return reply
    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(native))
    first = financial_client.post("/api/v1/financial-analysis/generate?stock_code=300260").json()
    assert first["status"] == "partial"
    assert first["analysis"]["years"][2]["gross_margin"] is None
    assert "年度财报覆盖不完整" in first["analysis"]["gaps"]
    assert "持有或加仓" not in first["markdown"]
    missing = False
    recovered = financial_client.post("/api/v1/financial-analysis/generate?stock_code=300260").json()
    assert recovered["status"] == "success"
    assert recovered["analysis"]["years"][2]["gross_margin"] == 50
    assert recovered["report_id"] != first["report_id"]


def test_legacy_annual_refresh_replaces_cached_financials(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from src.config import Config
    from src.deep_research_dims import dim_cache
    from src.deep_research_dims.context import fetch_fuyao_financial_series
    from data_provider.mx_data_adapter import financial_refresh
    import requests
    monkeypatch.setattr(Config, "_instance", Config(data_source_profile="legacy"))
    monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("FUYAO_API_KEY", "fixture")
    revenue = 100
    def get(url, **kwargs):
        rows = [{"fiscal_year": 2025, "operating_income": revenue}] if url.endswith("income-statements") else []
        return SimpleNamespace(status_code=200, json=lambda: {"code": 0, "data": {"item": rows}})
    monkeypatch.setattr(requests, "get", get)
    assert fetch_fuyao_financial_series("300260")["years"][0]["revenue"] == 100
    revenue = 150
    token = financial_refresh.set(("300260", "refresh"))
    try:
        assert fetch_fuyao_financial_series("300260")["years"][0]["revenue"] == 150
    finally:
        financial_refresh.reset(token)


def test_four_annual_reports_keep_cash_equity_gross_amount_and_weighted_roe(annual_transport):
    series = ChoiceAdapter().annual_reports("300260", deadline=monotonic()+20)
    assert [row["year"] for row in series["years"]] == [2025, 2024, 2023, 2022]
    latest = series["years"][0]
    assert latest["gross_profit"] == 40
    assert latest["op_cash_flow"] == -3
    assert latest["equity_total"] == 200
    assert latest["roe"] == 25
    assert latest["eps"] == 0
    assert latest["currency"] == "CNY"
    assert latest["adjustment"] == "unadjusted"


def test_real_earnings_entry_uses_weighted_roe_and_negative_cash(annual_transport, monkeypatch, tmp_path):
    from src.services import earnings_analysis_service as service
    monkeypatch.setattr(service, "get_tmp_dir", lambda: tmp_path / "csv")
    monkeypatch.setattr(service, "get_report_dir", lambda: tmp_path / "reports")
    result = service.generate_earnings_report("300260", company_name="fixture")
    values = {row["metric"]: row["value"] for row in result["calculations"]}
    assert values["roe_2025-12-31"] == "25.00%"
    assert values["cash_quality_2025-12-31"] == "-0.30"
    assert values["gross_margin_2025-12-31"] == "40.00%"
    assert "Choice" in (tmp_path / "reports" / f"{result['report_id']}.md").read_text()


def test_year_currency_mismatch_is_rejected_before_ratios(annual_transport):
    from src.services.earnings_analysis_service import _statements_from_series
    series = ChoiceAdapter().annual_reports("300260", deadline=monotonic()+20)
    series["years"][0]["currency"] = "USD"
    with pytest.raises(ValueError):
        _statements_from_series(series)


def test_missing_previous_year_and_zero_denominators_do_not_make_growth(annual_transport):
    from src.services.financial_analysis_service import _score_dims
    series = ChoiceAdapter().annual_reports("300260", deadline=monotonic()+20)
    series["years"] = series["years"][:1]
    scores = _score_dims(series, {})
    assert scores["rev_yoy"] is None and scores["np_yoy"] is None
    series = ChoiceAdapter().annual_reports("300260", deadline=monotonic()+20)
    series["years"][1]["revenue"] = 0
    series["years"][1]["net_profit"] = 0
    scores = _score_dims(series, {})
    assert scores["rev_yoy"] is None and scores["np_yoy"] is None


def test_high_debt_and_zero_cash_are_not_reported_as_low_debt_or_missing(annual_transport):
    from src.services.financial_analysis_service import _score_dims
    series = ChoiceAdapter().annual_reports("300260", deadline=monotonic()+20)
    series["years"][0]["total_debt"] = 750
    series["years"][0]["op_cash_flow"] = 0
    scores = _score_dims(series, {})
    assert scores["cash_quality"] == 0
    assert "偏高" in scores["dims"]["safety"]["label"]


def test_snapshot_pe_reaches_financial_valuation(annual_transport):
    from src.deep_research_dims.context import SharedContext, _safe_fundamental
    from src.services.financial_analysis_service import _score_dims
    series = ChoiceAdapter().annual_reports("300260", deadline=monotonic()+20)
    fund = _safe_fundamental("300260", SharedContext("300260", "fixture", date.today().isoformat()))
    result = _score_dims(series, fund)
    assert result["valuation"]["pe_ttm"] == 121.655277
    assert result["valuation"]["pe_disp"] == "121.66"


def test_render_cannot_restore_growth_rejected_by_validation(annual_transport):
    from src.services.earnings_analysis_service import _statements_from_series, _validate_and_compute, _render_report
    series = ChoiceAdapter().annual_reports("300260", deadline=monotonic()+20)
    series["years"] = [series["years"][0], series["years"][2]]
    statements = _statements_from_series(series)
    calculations = _validate_and_compute(statements)
    markdown = _render_report("300260", "fixture", "2025-12-31", statements, calculations)
    for line in markdown.splitlines():
        if line.startswith(("| 营收 |", "| 毛利 |", "| 净利润 |")):
            assert line.endswith(("| N/A |", "| — |"))


def test_invalid_cached_annual_record_is_a_miss_not_a_user_input_error(annual_transport, tmp_path, monkeypatch):
    from src.deep_research_dims import dim_cache
    from src.deep_research_dims.context import fetch_fuyao_financial_series
    monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path))
    series = ChoiceAdapter().annual_reports("300260", deadline=monotonic()+20)
    series["years"][0]["revenue"] = float("inf")
    dim_cache.save_snapshot(f"{ChoiceAdapter.contract_version}_annual_300260_4", series)
    fresh = fetch_fuyao_financial_series("300260")
    assert fresh["status"] == "ok"
    assert fresh["years"][0]["revenue"] == 100.0
