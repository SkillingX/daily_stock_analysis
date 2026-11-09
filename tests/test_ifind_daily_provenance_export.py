# -*- coding: utf-8 -*-
"""issue #86：日线溯源在保存/读回/渲染/导出链路的一致呈现（离线）。

深研：同一已采用共享帧 → DataDim 规范字段 → 既有持久化/API → v2/双轨模板。
普通：已保存 context_snapshot 三字段 → HistoryService Markdown；旧记录无
元数据保持旧内容；原源兜底只报来源与截止日，不发明口径。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.deep_research_dims.context import SharedContext
from src.deep_research_dims.data_dim import build_data_dim, daily_provenance
from src.deep_research_dims.render import build_view, render_markdown, split_dim_sections
from src.schemas.deep_research_dims import DataDim, parse_dim
from tests.test_deep_research_dims import _all_default_dims


def _history(n: int = 30, end: date = date(2026, 11, 6), source: str = "IfindDailyFetcher"):
    rows = []
    for i in range(n):
        rows.append(
            {
                "date": str(end - timedelta(days=n - 1 - i)),
                "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
                "volume": 1000, "amount": 10500, "data_source": source,
            }
        )
    return rows


def _ctx(history=None, history_source: str = "IfindDailyFetcher") -> SharedContext:
    ctx = SharedContext(stock_code="600519", stock_name="贵州茅台", as_of="2026-11-09T02:03:13")
    ctx.history = history if history is not None else _history()
    ctx.history_source = history_source
    ctx.quote = {"price": 10.5}
    return ctx


# ---------------------------------------------------------------------------
# 深研规范装配
# ---------------------------------------------------------------------------
def test_data_dim_carries_ifind_provenance():
    dim = build_data_dim(_ctx())
    assert dim.daily_history_source == "IfindDailyFetcher"
    assert dim.daily_price_mode == "前复权(现金分红)"
    assert dim.daily_history_as_of == "2026-11-06"


def test_data_dim_fallback_source_omits_cash_mode():
    dim = build_data_dim(_ctx(history=_history(source="EfinanceFetcher"), history_source="EfinanceFetcher"))
    assert dim.daily_history_source == "EfinanceFetcher"
    assert dim.daily_price_mode is None
    assert dim.daily_history_as_of == "2026-11-06"


def test_data_dim_without_history_omits_provenance():
    dim = build_data_dim(_ctx(history=[], history_source=""))
    assert dim.daily_history_source is None
    assert dim.daily_price_mode is None
    assert dim.daily_history_as_of is None


def test_short_history_degraded_keeps_provenance():
    dim = build_data_dim(_ctx(history=_history(n=5)))
    assert dim.status == "degraded"
    assert dim.daily_history_source == "IfindDailyFetcher"
    assert dim.daily_price_mode == "前复权(现金分红)"
    assert dim.daily_history_as_of == "2026-11-06"


def test_record_data_source_used_when_history_source_missing():
    ctx = _ctx(history_source="")
    provenance = daily_provenance(ctx)
    assert provenance["daily_history_source"] == "IfindDailyFetcher"
    assert provenance["daily_price_mode"] == "前复权(现金分红)"


def test_old_dim_payload_parses_without_provenance():
    payload = DataDim().model_dump()
    for key in ("daily_history_source", "daily_price_mode", "daily_history_as_of"):
        payload.pop(key, None)
    dim = parse_dim("data", payload)
    assert isinstance(dim, DataDim)
    assert dim.daily_history_source is None
    assert dim.daily_price_mode is None
    assert dim.daily_history_as_of is None


def test_new_dim_payload_round_trips_provenance():
    dim = build_data_dim(_ctx())
    restored = parse_dim("data", dim.model_dump())
    assert restored.daily_history_source == "IfindDailyFetcher"
    assert restored.daily_price_mode == "前复权(现金分红)"
    assert restored.daily_history_as_of == "2026-11-06"


# ---------------------------------------------------------------------------
# 深研模板渲染（v2 与双轨、分维度导出）
# ---------------------------------------------------------------------------
def _render_with_data(data_dim: DataDim, template: str | None = None) -> str:
    dims = _all_default_dims()
    dims["data"] = data_dim
    view = build_view("贵州茅台", "600519", "2026-11-09T02:03:13", [], dims, [])
    if template is None:
        return render_markdown(view)
    return render_markdown(view, template_name=template)


def test_v2_template_renders_daily_provenance_line():
    markdown = _render_with_data(build_data_dim(_ctx()))
    assert "IfindDailyFetcher" in markdown
    assert "前复权(现金分红)" in markdown
    assert "日线实际截止 2026-11-06" in markdown
    # 阶段 0 装配时间与日线实际截止日并列且互不冒充
    assert "2026-11-09" in markdown
    assert "baseDate" not in markdown


def test_dual_track_template_renders_daily_provenance_line():
    markdown = _render_with_data(build_data_dim(_ctx()), "deep_research_dual_track.j2")
    assert "IfindDailyFetcher" in markdown
    assert "前复权(现金分红)" in markdown
    assert "日线实际截止 2026-11-06" in markdown


def test_template_without_provenance_keeps_legacy_content():
    markdown = _render_with_data(DataDim())
    assert "日线窗口" not in markdown
    assert "前复权(现金分红)" not in markdown


def test_full_markdown_retains_provenance_split_subreports_unchanged():
    markdown = _render_with_data(build_data_dim(_ctx()))
    # 盘面在正文（完整 MD/PDF 下载与规范 dims JSON 共用）；既有分维度子报告
    # 本就不含盘面段（v2 VIEW_DIM_ORDER 无 data 子报告），不宣称分片覆盖。
    assert "IfindDailyFetcher" in markdown
    assert "日线实际截止 2026-11-06" in markdown
    sections = split_dim_sections(markdown)
    assert "data" not in sections
    assert all("日线窗口" not in body for body in sections.values())


# ---------------------------------------------------------------------------
# 普通 HistoryService Markdown
# ---------------------------------------------------------------------------
class _StubRecord:
    def __init__(self, context_snapshot=None):
        from datetime import datetime

        self.created_at = datetime(2026, 11, 9, 2, 3, 13)
        self.code = "600519"
        self.name = "贵州茅台"
        self.context_snapshot = (
            __import__("json").dumps({"enhanced_context": context_snapshot}, ensure_ascii=False)
            if context_snapshot is not None
            else None
        )


def _service():
    from src.services.history_service import HistoryService

    return HistoryService.__new__(HistoryService)


def _result():
    from src.analyzer import AnalysisResult

    return AnalysisResult(
        code="600519", name="贵州茅台", sentiment_score=72,
        trend_prediction="看多", operation_advice="持有", analysis_summary="稳健",
    )


def test_ordinary_markdown_reads_saved_snapshot_provenance():
    record = _StubRecord(
        {
            "daily_history_source": "IfindDailyFetcher",
            "daily_price_mode": "前复权(现金分红)",
            "daily_history_as_of": "2026-11-06",
        }
    )
    service = _service()
    provenance = service._extract_daily_provenance(record)
    markdown = service._generate_single_stock_markdown(_result(), record, provenance)
    assert "IfindDailyFetcher" in markdown
    assert "前复权(现金分红)" in markdown
    assert "日线实际截止 2026-11-06" in markdown
    assert "baseDate" not in markdown


def test_ordinary_markdown_fallback_source_without_cash_mode():
    record = _StubRecord(
        {"daily_history_source": "EfinanceFetcher", "daily_history_as_of": "2026-11-06"}
    )
    service = _service()
    markdown = service._generate_single_stock_markdown(
        _result(), record, service._extract_daily_provenance(record)
    )
    assert "EfinanceFetcher" in markdown
    assert "日线实际截止 2026-11-06" in markdown
    assert "前复权" not in markdown


def test_ordinary_markdown_legacy_record_unchanged():
    service = _service()
    record = _StubRecord(None)
    markdown = service._generate_single_stock_markdown(
        _result(), record, service._extract_daily_provenance(record)
    )
    assert "日线窗口" not in markdown
    assert "前复权" not in markdown
    assert markdown.startswith("# 📊")


def test_ordinary_english_mode_line_labels():
    record = _StubRecord(
        {
            "daily_history_source": "IfindDailyFetcher",
            "daily_price_mode": "前复权(现金分红)",
            "daily_history_as_of": "2026-11-06",
        }
    )
    from src.analyzer import AnalysisResult

    result = AnalysisResult(
        code="600519", name="贵州茅台", sentiment_score=72,
        trend_prediction="bullish", operation_advice="hold", analysis_summary="steady",
        report_language="en",
    )
    service = _service()
    markdown = service._generate_single_stock_markdown(
        result, record, service._extract_daily_provenance(record)
    )
    assert "Daily History: Source **IfindDailyFetcher**" in markdown
    assert "Price basis 前复权(现金分红)" in markdown
    assert "Daily cutoff 2026-11-06" in markdown


# ---------------------------------------------------------------------------
# 新 I/O 字段严格校验（AGENTS §1.4 Layer 3，仅约束新增字段）
# ---------------------------------------------------------------------------
def _payload_with(**updates):
    payload = DataDim().model_dump()
    for key in ("daily_history_source", "daily_price_mode", "daily_history_as_of"):
        payload.pop(key, None)
    payload.update(updates)
    return payload


def test_reject_bytes_source():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        parse_dim("data", _payload_with(daily_history_source=b"IfindDailyFetcher"))


def test_reject_blank_source():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        parse_dim("data", _payload_with(daily_history_source="   "))


def test_reject_unsupported_mode():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        parse_dim("data", _payload_with(daily_price_mode="unadjusted"))


def test_reject_impossible_and_malformed_cutoff():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        parse_dim("data", _payload_with(daily_history_as_of="2026-99-99"))
    with pytest.raises(ValidationError):
        parse_dim("data", _payload_with(daily_history_as_of="20261106"))


def test_mode_literal_matches_ifind_contract():
    from data_provider.ifind_daily_adapter import _CASH_QFQ_MODE

    from src.schemas.deep_research_dims import DAILY_CASH_QFQ_MODE

    assert DAILY_CASH_QFQ_MODE == _CASH_QFQ_MODE


def test_canonical_values_still_roundtrip():
    dim = parse_dim(
        "data",
        _payload_with(
            daily_history_source="IfindDailyFetcher",
            daily_price_mode="前复权(现金分红)",
            daily_history_as_of="2026-11-06",
        ),
    )
    assert dim.daily_history_source == "IfindDailyFetcher"
    assert dim.daily_price_mode == "前复权(现金分红)"
    assert dim.daily_history_as_of == "2026-11-06"


# ---------------------------------------------------------------------------
# 中断（取消/预算）在正常 DataDim 装配前的规范溯源
# ---------------------------------------------------------------------------
def _interrupted(stop_reason: str, ctx: SharedContext):
    from src.agent.deep_research.orchestrator import _interrupted_result
    from src.core.research_run import ResearchRun

    run = ResearchRun(timeout_seconds=60)
    run.context = ctx
    run.stop(stop_reason)
    return _interrupted_result(run, "600519", "贵州茅台", {"data"}, None)


def test_cancelled_before_data_assembly_keeps_adopted_provenance():
    result = _interrupted("cancelled", _ctx())
    assert result.status == "partial"
    data = result.dims["data"]
    assert data.status == "degraded"
    assert data.daily_history_source == "IfindDailyFetcher"
    assert data.daily_price_mode == "前复权(现金分红)"
    assert data.daily_history_as_of == "2026-11-06"
    assert "前复权(现金分红)" in result.markdown
    assert "日线实际截止 2026-11-06" in result.markdown


def test_budget_stop_before_data_assembly_keeps_adopted_provenance():
    result = _interrupted("report_budget", _ctx())
    assert result.status == "partial"
    data = result.dims["data"]
    assert data.daily_history_source == "IfindDailyFetcher"
    assert data.daily_price_mode == "前复权(现金分红)"
    assert data.daily_history_as_of == "2026-11-06"


def test_interrupted_without_history_has_no_provenance():
    result = _interrupted("cancelled", _ctx(history=[], history_source=""))
    data = result.dims["data"]
    assert data.daily_history_source is None
    assert data.daily_price_mode is None
    assert data.daily_history_as_of is None
    assert "日线窗口" not in result.markdown


def test_interrupted_short_history_keeps_provenance():
    result = _interrupted("cancelled", _ctx(history=_history(n=5)))
    data = result.dims["data"]
    assert data.daily_history_source == "IfindDailyFetcher"
    assert data.daily_history_as_of == "2026-11-06"


def test_interrupted_fallback_source_omits_cash_mode():
    ctx = _ctx(history=_history(source="EfinanceFetcher"), history_source="EfinanceFetcher")
    result = _interrupted("cancelled", ctx)
    data = result.dims["data"]
    assert data.daily_history_source == "EfinanceFetcher"
    assert data.daily_price_mode is None
    assert "前复权" not in result.markdown


# ---------------------------------------------------------------------------
# 生产渲染结构回归：日线行不得吞掉下一个「当前结构」bullet 的换行
# （root 在冻结 R9 上实测 8/8 粘连，见 root-render-newline-proof.json）
# ---------------------------------------------------------------------------
_TEMPLATES = [None, "deep_research_dual_track.j2"]
_MODE_CUTOFF = [(True, True), (True, False), (False, True), (False, False)]


@pytest.mark.parametrize("template", _TEMPLATES, ids=["v2", "dual_track"])
@pytest.mark.parametrize("mode,cutoff", _MODE_CUTOFF)
def test_daily_provenance_bullet_keeps_next_structure_bullet(template, mode, cutoff):
    base = build_data_dim(_ctx())
    assert base.perspective is not None and base.perspective.trend_status is not None
    data = base.model_copy(
        update={
            "daily_price_mode": base.daily_price_mode if mode else None,
            "daily_history_as_of": base.daily_history_as_of if cutoff else None,
        }
    )
    markdown = _render_with_data(data, template)
    lines = markdown.splitlines()
    daily_idx = [i for i, line in enumerate(lines) if line.startswith("- 日线窗口：")]
    assert len(daily_idx) == 1, "expected exactly one daily provenance bullet"
    daily = lines[daily_idx[0]]
    assert "- 当前结构" not in daily, "daily bullet glued the structure bullet"
    following = [line for line in lines[daily_idx[0] + 1:] if line.strip()]
    assert following and following[0].startswith("- 当前结构："), following[:1]
    assert f"来源 **{base.daily_history_source}**" in daily
    assert ("口径 前复权(现金分红)" in daily) is mode
    assert ("日线实际截止 2026-11-06" in daily) is cutoff


@pytest.mark.parametrize("template", _TEMPLATES, ids=["v2", "dual_track"])
def test_no_source_baseline_has_no_daily_bullet(template):
    markdown = _render_with_data(DataDim(), template)
    assert "- 日线窗口：" not in markdown
    lines = [line for line in markdown.splitlines() if line.strip()]
    # v2 的「当前结构」行以有效 perspective 为前提；双轨模板无条件渲染。
    if template == "deep_research_dual_track.j2":
        assert any(line.startswith("- 当前结构：") for line in lines)
