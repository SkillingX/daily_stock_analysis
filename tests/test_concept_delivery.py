# -*- coding: utf-8 -*-
"""issue #51 概念归属报告交付：阶段 0 已取得事实必须确定性落到报告产物。

覆盖真实消费链：SharedContext 快照 → DataDim 字段 → 模板成文 → 中断降级
报告 → 历史 API 详情（_dims.json 合并）；历史载荷缺字段保持可读且不触发
任何供应商补查。全部离线。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from src.deep_research_dims.context import SharedContext
from src.deep_research_dims.data_dim import adopt_concept_membership, build_data_dim
from src.deep_research_dims.render import build_view, render_markdown, validate_structure
from src.schemas.deep_research_dims import DataDim, parse_dim
from tests.test_deep_research_dims import _all_default_dims, _fake_history

# 真实 300260 运行取得的公开工具载荷形态（concepts 为 {name, source, as_of_nature} 列表）。
PUBLIC_PAYLOAD = {
    "concepts": [
        {"name": "食品安全", "source": "ifind", "as_of_nature": "latest"},
        {"name": "液冷服务器", "source": "ifind", "as_of_nature": "latest"},
        {"name": "半导体设备", "source": "ifind", "as_of_nature": "latest"},
        {"name": "深股通", "source": "ifind", "as_of_nature": "latest"},
        {"name": "专精特新", "source": "ifind", "as_of_nature": "latest"},
    ],
    "source": "ifind",
    "as_of_nature": "latest",
    "error": None,
}


def _ctx(**overrides) -> SharedContext:
    ctx = SharedContext(stock_code="300260", stock_name="新莱应材", as_of="2026-10-26T10:00:00")
    ctx.history = _fake_history()
    ctx.quote = {"price": 13.0}
    ctx.concept_membership = json.loads(json.dumps(PUBLIC_PAYLOAD))
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


def _render(dims: dict) -> str:
    return render_markdown(build_view("新莱应材", "300260", "2026-10-26T10:00:00", [], dims, []))


class TestConceptAdoption:
    def test_stage0_snapshot_adopted_in_full_without_truncation(self) -> None:
        membership = adopt_concept_membership(_ctx())
        assert membership is not None
        assert membership.code == "300260"
        assert membership.concepts == [entry["name"] for entry in PUBLIC_PAYLOAD["concepts"]]
        assert membership.source == "ifind"
        assert membership.as_of_nature == "latest"
        assert membership.errors == []

    def test_supplier_error_semantics_survive_adoption(self) -> None:
        payload = {"concepts": [], "source": "ifind", "as_of_nature": "latest", "error": "field_error"}
        membership = adopt_concept_membership(_ctx(concept_membership=payload))
        assert membership is not None
        assert membership.concepts == []
        assert membership.errors == ["field_error"]

    def test_stage0_not_run_keeps_field_absent(self) -> None:
        assert adopt_concept_membership(_ctx(concept_membership={})) is None

    def test_malformed_payload_degrades_without_blocking_report(self) -> None:
        assert adopt_concept_membership(_ctx(concept_membership={"concepts": "bad"})) is None


class TestDataDimDelivery:
    def test_ok_data_dim_carries_concepts_into_markdown(self) -> None:
        dims = _all_default_dims()
        dims["data"] = build_data_dim(_ctx())
        assert dims["data"].status == "ok"
        assert dims["data"].concept_membership is not None
        markdown = _render(dims)
        line = next(row for row in markdown.splitlines() if "最新概念归属" in row)
        for name in PUBLIC_PAYLOAD["concepts"]:
            assert name["name"] in line
        assert "300260" in line and "ifind" in line and "查询时点最新，非历史区间" in line

    def test_short_history_degraded_data_dim_still_carries_concepts(self) -> None:
        dims = _all_default_dims()
        dims["data"] = build_data_dim(_ctx(history=[]))
        assert dims["data"].status == "degraded"
        markdown = _render(dims)
        assert "食品安全" in markdown and "深股通" in markdown

    def test_disclosed_failure_does_not_fabricate_concepts_or_relations(self) -> None:
        payload = {"concepts": [], "source": "ifind", "as_of_nature": "latest", "error": "field_error"}
        dims = _all_default_dims()
        dims["data"] = build_data_dim(_ctx(concept_membership=payload))
        markdown = _render(dims)
        assert "本次未取得（field_error）" in markdown
        assert "不把概念推断为行业、客户或供应链关系" in markdown

    def test_absent_field_renders_without_concept_block(self) -> None:
        dims = _all_default_dims()
        dims["data"] = build_data_dim(_ctx(concept_membership={}))
        assert dims["data"].concept_membership is None
        markdown = _render(dims)
        assert "最新概念归属" not in markdown
        assert not validate_structure(markdown)

    def test_guardrail_roundtrip_preserves_field(self) -> None:
        from src.deep_research_dims.guardrail import apply_guardrails

        dims = _all_default_dims()
        dims["data"] = build_data_dim(_ctx())
        payloads = {key: model.model_dump() for key, model in dims.items()}
        events, adjusted = apply_guardrails(payloads)
        assert isinstance(events, list)
        data = parse_dim("data", adjusted["data"])
        assert isinstance(data, DataDim)
        assert data.concept_membership is not None
        assert data.concept_membership.concepts == [entry["name"] for entry in PUBLIC_PAYLOAD["concepts"]]

    def test_cached_dim_payload_roundtrip_keeps_field(self) -> None:
        dim = build_data_dim(_ctx())
        restored = parse_dim("data", json.loads(json.dumps(dim.model_dump(mode="json"))))
        assert restored.concept_membership is not None
        assert restored.concept_membership.code == "300260"


class TestInterruptedReport:
    def test_interrupted_data_dim_keeps_stage0_concepts(self) -> None:
        from src.agent.deep_research.orchestrator import _interrupted_result
        from src.core.research_run import ResearchRun

        run = ResearchRun(10)
        run.context = _ctx(quote={"price": 12.5})
        run.stop("cancelled")
        result = _interrupted_result(run, "300260", "新莱应材", {"data"}, None)
        assert result.success and result.status == "partial"
        membership = result.dims["data"].concept_membership
        assert membership is not None and membership.concepts[0] == "食品安全"
        payload = result.dims_payload["data"]["concept_membership"]
        assert payload["code"] == "300260" and payload["concepts"][0] == "食品安全"
        assert "食品安全" in result.markdown and "ifind" in result.markdown

    def test_data_dim_rebuilt_per_report_never_loaded_from_cache(self, monkeypatch) -> None:
        """context_dims 含 data：try_cache 直接短路，旧缓存载荷不可进入本报告。"""
        from src.agent.deep_research import orchestrator
        from src.deep_research_dims import dim_cache

        calls: list[tuple[str, str]] = []

        def _recorder(code: str, dim: str):
            calls.append((code, dim))
            return None

        monkeypatch.setattr(orchestrator, "build_shared_context", lambda *a: _ctx())
        monkeypatch.setattr(dim_cache, "load_cached_dim", _recorder)
        result = orchestrator.run_dual_track(
            "300260", "新莱应材", object(), dims_filter={"data"}, force_refresh=False,
            skip_narrations=True, write_cache=False,
        )
        assert calls == []  # data 属 context_dims，缓存读取被短路
        membership = result.dims["data"].concept_membership
        assert membership is not None and membership.concepts[0] == "食品安全"
        assert "食品安全" in result.markdown

    def test_concepts_alone_persist_an_interrupted_partial_report(self) -> None:
        """root 反例：仅有已验证概念、无行情/历史/财务 → 报告仍成文保留事实。"""
        from src.agent.deep_research.orchestrator import _interrupted_result
        from src.core.research_run import ResearchRun

        run = ResearchRun(10)
        run.context = SharedContext(
            "300260", "新莱应材", "2026-10-26T10:00:00",
            concept_membership=json.loads(json.dumps(PUBLIC_PAYLOAD)),
        )
        run.stop("cancelled")
        result = _interrupted_result(run, "300260", "新莱应材", {"data"}, None)
        assert result.success and result.status == "partial"
        assert "食品安全" in result.markdown and "专精特新" in result.markdown

    def test_error_only_concepts_do_not_alone_persist_a_report(self) -> None:
        from src.agent.deep_research.orchestrator import _interrupted_result
        from src.core.research_run import ResearchRun

        run = ResearchRun(10)
        run.context = SharedContext(
            "300260", "新莱应材", "2026-10-26T10:00:00",
            concept_membership={"concepts": [], "source": "ifind", "as_of_nature": "latest", "error": "field_error"},
        )
        run.stop("cancelled")
        result = _interrupted_result(run, "300260", "新莱应材", {"data"}, None)
        assert not result.success and result.status == "failed"
        assert result.markdown == ""


class TestHistoricalPayloadCompatibility:
    def test_legacy_dims_json_without_field_stays_readable_and_offline(self) -> None:
        legacy = {
            "dim": "data",
            "status": "degraded",
            "degraded_reason": "日线历史不足 20 条，无法计算均线与点位",
            "retained_facts": [],
        }
        dim = parse_dim("data", legacy)
        assert isinstance(dim, DataDim) and dim.concept_membership is None
        dims = _all_default_dims()
        dims["data"] = dim
        markdown = _render(dims)
        assert "最新概念归属" not in markdown
        assert not validate_structure(markdown)

    def test_report_detail_api_serves_concepts_from_saved_dims_json(self, monkeypatch, tmp_path) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from api.v1.endpoints import deep_research as endpoint
        from src.services import deep_research_service as service

        report_id = "300260_202610261042"
        dims = _all_default_dims()
        dims["data"] = build_data_dim(_ctx())
        (tmp_path / f"{report_id}.md").write_text(_render(dims), encoding="utf-8")
        (tmp_path / f"{report_id}_dims.json").write_text(
            json.dumps(
                {
                    "report_id": report_id,
                    "engine": "dual_track",
                    "run_id": "fixture",
                    "end_reason": None,
                    "guardrail_events": [],
                    "dimensions": {key: model.model_dump(mode="json") for key, model in dims.items()},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        metadata = {
            "id": report_id,
            "md_path": str(tmp_path / f"{report_id}.md"),
            "stock_code": "300260",
            "stock_name": "新莱应材",
        }
        record = SimpleNamespace(md_path=metadata["md_path"], to_dict=lambda: metadata.copy())
        monkeypatch.setattr(service, "get_db", lambda: SimpleNamespace(get_deep_research_report=lambda _: record))
        monkeypatch.setattr(service, "get_deep_research_dir", lambda: tmp_path)
        monkeypatch.setattr(endpoint, "get_deep_research_dir", lambda: tmp_path)
        app = FastAPI()
        app.include_router(endpoint.router, prefix="/api/v1/deep-research")
        with TestClient(app) as client:
            detail = client.get(f"/api/v1/deep-research/reports/{report_id}")
        assert detail.status_code == 200
        data = detail.json()["data"]
        membership = data["dimensions"]["data"]["concept_membership"]
        assert membership["code"] == "300260" and membership["source"] == "ifind"
        assert membership["concepts"] == [entry["name"] for entry in PUBLIC_PAYLOAD["concepts"]]
        assert "食品安全" in data["markdown"] and "液冷服务器" in data["markdown"]
