# -*- coding: utf-8 -*-
"""Issue #52：新报告来源等级采用到模板、API、Markdown 和 PDF。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from src.deep_research.researchers.business import parse as parse_business
from src.deep_research.researchers.sentiment import parse as parse_sentiment
from src.deep_research_dims.render import _dim_indicator_md, build_view, render_markdown
from src.report_language import adopt_structured_source_grades, strip_retired_chip_structure
from src.scoring.indicators_v2 import aggregate_v2_dimensions
from tests.test_deep_research_dims import _all_default_dims


def _model_payload() -> dict:
    return {
        "business_model": {
            "summary": "无菌包材业务。公告记载股东减持。芯片概念只作行业标签。财经新闻引用了社区讨论，正文保留。",
            "items": [
                {
                    "fact": "客户已导入",
                    "source": "东方财富 F10 聚合互动易回复",
                    "source_grade": "community_cn",
                },
                {
                    "fact": "公告提及聚合页名称",
                    "source": "交易所临时公告，正文提及东方财富 F10 聚合页",
                    "source_grade": "primary",
                },
                {
                    "fact": "股东减持 1.2%",
                    "source": "交易所临时公告",
                    "source_grade": "primary",
                },
                {
                    "fact": "所属芯片概念",
                    "source": "财经新闻",
                    "source_grade": "news",
                    "note": "芯片概念不是客户事实",
                },
                {
                    "fact": "订单推测",
                    "source": "产业新闻线索",
                    "source_grade": "inferred",
                    "basis": "依据：2024年报收入结构与产能利用率",
                },
                {
                    "fact": "未知出处数字",
                    "source": "未署名转载",
                    "source_grade": "community_global",
                },
                {
                    "fact": "品牌组合不能核验",
                    "source": "供应商摘要",
                    "source_grade": "MX+iFinD",
                },
            ],
        },
        "main_products": {"summary": "无菌包材"},
        "narrative": "股东减持见公告。芯片概念保留。社区讨论不升为事实。",
        "data_gaps": ["股东明细原始公告未取得"],
        "score": 60,
    }


def _business_section() -> str:
    dim, _steps = parse_business(_model_payload(), 1)
    dims = _all_default_dims()
    dims["business"] = dim
    markdown = render_markdown(build_view("新莱应材", "300260", "2026-10-26T11:22:00", [], dims, []))
    return markdown.split("<!-- dim:business -->", 1)[1].split("<!-- dim:", 1)[0]


def test_retired_grade_is_not_upgraded_and_titles_are_not_classified():
    section = _business_section()
    f10 = next(line for line in section.splitlines() if "东方财富 F10 聚合互动易回复" in line)
    mentioned = next(line for line in section.splitlines() if "公告提及聚合页名称" in line)
    filing = next(line for line in section.splitlines() if "股东减持 1.2%" in line)
    chip = next(line for line in section.splitlines() if "所属芯片概念" in line)
    inferred = next(line for line in section.splitlines() if "产业新闻线索" in line)
    retired = next(line for line in section.splitlines() if "未署名转载" in line)
    unknown = next(line for line in section.splitlines() if "供应商摘要" in line)

    assert "来源等级：未验证" in f10 and "公告级信源" not in f10
    assert "community_cn" not in f10
    assert "来源等级：公告级信源" in mentioned
    assert "来源等级：公告级信源" in filing
    assert "来源等级：news" in chip and "芯片概念不是客户事实" in chip
    assert "来源等级：推断" in inferred and "依据：2024年报收入结构与产能利用率" in inferred
    assert "来源等级：未验证" in retired and "community_global" not in retired
    assert "来源等级：未验证" in unknown and "MX+iFinD" not in unknown
    assert "primary" not in unknown and "news" not in unknown
    assert "股东减持见公告" in section
    assert "芯片概念保留" in section
    assert "社区讨论不升为事实" in section
    assert "股东明细原始公告未取得" in section


def test_free_text_is_not_rewritten_into_a_valid_grade():
    raw = "正文保留 community_cn 与筹码、芯片概念，不靠替换变成 primary。"
    adopted = adopt_structured_source_grades({"narrative": raw, "source_grade": "community_cn"})
    assert adopted["narrative"] == raw
    assert adopted["source_grade"] == "unverified"
    titled = adopt_structured_source_grades({
        "source": "交易所临时公告，正文提及东方财富 F10 聚合页",
        "source_grade": "primary",
    })
    assert titled["source_grade"] == "primary"
    assert "F10" in titled["source"]


def test_model_residual_fields_use_existing_adoption_boundary():
    dim, _steps = parse_sentiment(
        {
            "institute_score": 40,
            "institute_view": "机构目标价 30 元",
            "community_score": 72,
            "community_view": "社区热度高",
            "narrative": "机构评价保留。社区讨论与芯片概念只留在叙述里。",
            "chip_summary": {"text": "筹码集中度 12%"},
        },
        1,
    )
    dumped = dim.model_dump()
    assert dumped["institute_score"] == 40
    assert "community_score" not in dumped and "community_view" not in dumped and "chip_summary" not in dumped
    assert "社区讨论与芯片概念" in dumped["narrative"]

    scored = aggregate_v2_dimensions({
        "情绪面": {
            "institute_view": {
                "id": "institute_view", "score": 40.0, "confidence": "medium",
                "basis": "llm", "data_gap": False, "summary": "机构目标价",
            },
            "community_view": {
                "id": "community_view", "score": 72.0, "confidence": "high",
                "basis": "llm", "data_gap": False, "summary": "不应采用",
            },
        },
    })
    sentiment = next(item for item in scored if item["dimension"] == "情绪面")
    assert [item["id"] for item in sentiment["indicators"]] == ["institute_view"]
    assert sentiment["score"] == 40.0
    assert sentiment["data_gaps"] == 0
    from src.schemas.deep_research_dims import SixDimDim
    from src.schemas.research_framework import DimensionScore, IndicatorScore, ResearchFramework
    framework = ResearchFramework(
        dimension_total=40.0,
        dimensions=[DimensionScore(
            dimension="情绪面", weight=0.1, score=40.0,
            indicators=[IndicatorScore(**{k: item[k] for k in ("name", "score", "weight", "basis", "confidence", "summary", "data_gap")}) for item in sentiment["indicators"]],
        )],
        scoring_version="v2.1",
    )
    rendered = _dim_indicator_md(SixDimDim(framework=framework), "情绪面")
    assert "机构评价" in rendered and "社区" not in rendered and "缺口" not in rendered

    dashboard = {"data_perspective": {
        "trend_status": {"ma_alignment": "多头排列"},
        "chip_structure": {"concentration": "10%"},
    }}
    strip_retired_chip_structure(dashboard)
    assert "chip_structure" not in dashboard["data_perspective"]
    assert dashboard["data_perspective"]["trend_status"]["ma_alignment"] == "多头排列"


def _write_record(cache, code: str, dim: str, payload: dict, *, version: int | None) -> None:
    record = {
        "schema_version": cache.SCHEMA_VERSION,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "stock_code": code,
        "dim": dim,
        "payload": payload,
    }
    if version is not None:
        record["source_label_version"] = version
    Path(cache._cache_path(code, dim)).write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")


def test_affected_prompt_caches_miss_without_scrubbing_or_global_bump(tmp_path, monkeypatch):
    from src.deep_research_dims import dim_cache
    from src.schemas.deep_research_dims import parse_dim

    monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path))
    old_narrative = "F10聚合互动易回复 community_cn / 未核实"
    _write_record(dim_cache, "300260", "intel", {"narrative": old_narrative, "status": "ok"}, version=None)
    assert dim_cache.load_cached_dim("300260", "intel") is None
    # 若绕过缓存直接解析，正文原样保留；失效的是缓存采用，不是文本清洗。
    assert parse_dim("intel", {"narrative": old_narrative, "status": "ok"}).narrative == old_narrative

    _write_record(dim_cache, "300260", "business", _model_payload(), version=None)
    assert dim_cache.load_cached_dim("300260", "business") is None
    _write_record(dim_cache, "300260", "data", {"dim": "data", "status": "ok", "narrative": "筹码概念保留"}, version=None)
    assert dim_cache.load_cached_dim("300260", "data")["narrative"] == "筹码概念保留"

    dim_cache.save_cached_dim("300260", "intel", {"narrative": "股东减持见公告。芯片概念与社区讨论保留。", "status": "ok"})
    hot = dim_cache.load_cached_dim("300260", "intel")
    assert hot["narrative"] == "股东减持见公告。芯片概念与社区讨论保留。"
    assert "community_cn" not in hot["narrative"]


def test_force_refresh_skips_cache_read_and_old_intel_is_not_adopted(tmp_path, monkeypatch):
    from src.agent.deep_research import orchestrator
    from src.deep_research_dims import dim_cache
    from src.deep_research_dims.context import SharedContext

    monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path))
    old_narrative = "F10聚合互动易回复 community_cn / 未核实"
    _write_record(dim_cache, "300260", "intel", {"narrative": old_narrative, "status": "ok"}, version=None)
    calls: list[str] = []
    real_load = dim_cache.load_cached_dim

    def spy(code: str, dim: str):
        calls.append(dim)
        return real_load(code, dim)

    monkeypatch.setattr(dim_cache, "load_cached_dim", spy)
    monkeypatch.setattr(
        orchestrator,
        "build_shared_context",
        lambda code, name: SharedContext(stock_code=code, stock_name=name, as_of="2026-10-26T11:22:00"),
    )
    monkeypatch.setattr(
        orchestrator,
        "run_intel_agent",
        lambda *args, **kwargs: {"ok": True, "data": {"narrative": "新报告保留股东与芯片概念", "sentiment_summary": "中性"}, "steps": 1},
    )

    refreshed = orchestrator.run_dual_track(
        "300260", "新莱应材", object(), dims_filter={"intel"}, force_refresh=True, skip_narrations=True, write_cache=False,
    )
    assert calls == []
    assert refreshed.dims["intel"].intelligence.sentiment_summary == "中性"
    assert old_narrative not in refreshed.dims["intel"].model_dump_json()

    calls.clear()
    cold = orchestrator.run_dual_track(
        "300260", "新莱应材", object(), dims_filter={"intel"}, force_refresh=False, skip_narrations=True, write_cache=False,
    )
    assert "intel" in calls
    assert cold.dims["intel"].intelligence.sentiment_summary == "中性"
    assert old_narrative not in cold.dims["intel"].model_dump_json()


def test_new_and_historical_reports_reach_api_markdown_and_pdf(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pypdf import PdfReader

    from api.v1.endpoints import deep_research as endpoint
    from src.services import deep_research_service as service

    fresh = render_markdown(build_view(
        "新莱应材", "300260", "2026-10-26T11:22:00", [],
        {**_all_default_dims(), "business": parse_business(_model_payload(), 1)[0]},
        [],
    ))
    history = (
        "# 历史报告\n\n来源等级：community_cn / unverified。\n"
        "**筹码**: 80% | 30 | 10%\n股东减持见公告。芯片概念保留。\n"
    )
    fresh_id = "300260_202610261122"
    history_id = "300260_202610261121"
    fresh_path = tmp_path / f"{fresh_id}.md"
    history_path = tmp_path / f"{history_id}.md"
    fresh_path.write_text(fresh, encoding="utf-8")
    history_path.write_text(history, encoding="utf-8")
    records = {
        fresh_id: SimpleNamespace(
            id=fresh_id, md_path=str(fresh_path), pdf_path=None,
            to_dict=lambda: {"id": fresh_id, "md_path": str(fresh_path), "stock_code": "300260", "stock_name": "新莱应材", "created_at": "2026-10-26T11:22:00"},
        ),
        history_id: SimpleNamespace(
            id=history_id, md_path=str(history_path), pdf_path=None,
            to_dict=lambda: {"id": history_id, "md_path": str(history_path), "stock_code": "300260", "stock_name": "新莱应材", "created_at": "2026-10-26T11:21:00"},
        ),
    }

    class _DB:
        def get_deep_research_report(self, report_id):
            return records.get(report_id)

        def set_deep_research_pdf_path(self, report_id, path):
            records[report_id].pdf_path = path

    monkeypatch.setattr(service, "get_db", lambda: _DB())
    monkeypatch.setattr(endpoint, "get_deep_research_dir", lambda: tmp_path)
    app = FastAPI()
    app.include_router(endpoint.router, prefix="/api/v1/deep-research")
    with TestClient(app) as client:
        detail = client.get(f"/api/v1/deep-research/reports/{fresh_id}")
        download = client.get(f"/api/v1/deep-research/reports/{fresh_id}/markdown")
        pdf = client.get(f"/api/v1/deep-research/reports/{fresh_id}/pdf")
        old_detail = client.get(f"/api/v1/deep-research/reports/{history_id}")
        old_download = client.get(f"/api/v1/deep-research/reports/{history_id}/markdown")
    assert detail.status_code == download.status_code == pdf.status_code == 200
    body = detail.json()["data"]["markdown"]
    assert body == download.text
    assert "东方财富 F10 聚合互动易回复" in body and "来源等级：未验证" in body
    assert "来源等级：公告级信源" in body and "股东减持 1.2%" in body and "芯片概念保留" in body
    saved_pdf = Path(records[fresh_id].pdf_path)
    text = "\n".join(page.extract_text() or "" for page in PdfReader(saved_pdf).pages)
    assert "F10" in text and "股东" in text and "芯片" in text
    assert old_detail.status_code == old_download.status_code == 200
    assert old_detail.json()["data"]["markdown"] == old_download.text == history
    assert history_path.read_text(encoding="utf-8") == history
    assert fresh_path.read_text(encoding="utf-8") == fresh


def test_reachable_prompts_do_not_require_retired_labels():
    from src.agent.deep_research.explore_agents import run_intel_agent
    from src.agent.deep_research_executor import DeepResearchExecutor, build_deep_research_system_prompt
    from src.deep_research_dims.narrate import load_constitution, load_dim_prompt
    import inspect

    prompts = "\n".join([
        load_constitution(),
        load_dim_prompt("intel"),
        load_dim_prompt("business"),
        build_deep_research_system_prompt(),
        DeepResearchExecutor._build_user_message("300260", "新莱应材", "deep"),
        inspect.getsource(run_intel_agent),
    ])
    assert "community_cn" not in prompts and "community_global" not in prompts
    assert "MX+iFinD" not in prompts
    assert "primary" in prompts and "unverified" in prompts
