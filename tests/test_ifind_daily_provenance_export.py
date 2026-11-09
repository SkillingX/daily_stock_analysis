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


# ---------------------------------------------------------------------------
# PR87 R12：扩窗拒绝在最终报告边界的真实呈现（reconcile→render 全链）
# ---------------------------------------------------------------------------
def _report_harness(monkeypatch, frames, *, days: int = 90, prior_days: int = 30):
    """真实 loader→run→_safe_history→reconcile→render；仅替换外部 manager/DB。"""
    import socket as _socket

    from tests.ifind_daily_testutil import enabled_config, std_frame, strict_calendar
    from tests.test_ifind_daily_cache import _FakeDb, _FakeManager, _install
    from src.core.research_run import ResearchRun, current_research

    def _connect(*_args, **_kwargs):
        raise AssertionError("network forbidden in offline report regression")

    monkeypatch.setattr(_socket.socket, "connect", _connect)
    strict_calendar(monkeypatch)
    enabled_config(monkeypatch)
    db = _FakeDb([])
    manager = _install(monkeypatch, db, _FakeManager(frames))
    run = ResearchRun(timeout_seconds=60)
    research_token = current_research.set(run)
    if prior_days:
        from src.services import history_loader as H

        H.load_history_df("600519", days=prior_days)  # 任务先采用保留帧
    ctx = SharedContext("600519", "贵州茅台", "2026-11-08T10:00:00")
    return run, research_token, manager, ctx, days


def _assemble(ctx, dims=None):
    from src.agent.deep_research.orchestrator import _reconcile_shared_history

    dims = dims if dims is not None else _all_default_dims()
    dims["data"] = build_data_dim(ctx)
    calls_before = None  # 由调用方自行断言 manager.calls
    _reconcile_shared_history(ctx, dims)
    markdown = render_markdown(
        build_view(ctx.stock_name, ctx.stock_code, ctx.as_of, ctx.limitations, dims, [])
    )
    return dims, markdown, calls_before


def test_report_keeps_rejection_note_after_reconcile_changed_source(monkeypatch):
    """root 反例：换源扩窗被拒后 reconcile 复用旧帧，拒绝原因必须留在成文。"""
    from tests.ifind_daily_testutil import std_frame
    from src.core.research_run import current_research
    from src.deep_research_dims.context import _safe_history
    from src.services import history_loader as H

    frames = [std_frame(rows=40), (std_frame(rows=120), "EfinanceFetcher")]
    run, research_token, manager, ctx, days = _report_harness(monkeypatch, frames)
    try:
        ctx.history = _safe_history("600519", days, ctx)
        assert not ctx.history
        dims, markdown, _ = _assemble(ctx)
        assert len(ctx.history) == 40 and ctx.history_source == "IfindDailyFetcher"
        assert dims["data"].status == "ok"  # 保留帧的短期事实照常可用
        assert "daily window expansion rejected" in markdown
        assert "EfinanceFetcher" in markdown and "IfindDailyFetcher" in markdown
        assert "来源 **IfindDailyFetcher**" in markdown  # 事实与拒绝同时在场
        unmet = run.unmet_daily_history_snapshot()
        assert list(unmet) == ["2026-11-06|90"] and "expansion rejected" in unmet["2026-11-06|90"]
        assert len(manager.calls) == 2  # reconcile/render 不取数
        # 更短已满足需求复用保留帧，0 网络
        df, source = H.load_history_df("600519", days=30)
        assert len(df) == 40 and source == "IfindDailyFetcher"
        assert len(manager.calls) == 2
    finally:
        current_research.reset(research_token)


def test_report_keeps_rejection_note_after_reconcile_overlap_conflict(monkeypatch):
    from tests.ifind_daily_testutil import std_frame
    from src.core.research_run import current_research
    from src.deep_research_dims.context import _safe_history

    frames = [std_frame(rows=40), std_frame(rows=120, close=55.5)]
    run, research_token, manager, ctx, days = _report_harness(monkeypatch, frames)
    try:
        ctx.history = _safe_history("600519", days, ctx)
        assert not ctx.history
        dims, markdown, _ = _assemble(ctx)
        assert len(ctx.history) == 40
        assert dims["data"].status == "ok"
        assert "conflicts with adopted window" in markdown
        assert "来源 **IfindDailyFetcher**" in markdown
        assert "2026-11-06|90" in run.unmet_daily_history_snapshot()
        assert len(manager.calls) == 2
    finally:
        current_research.reset(research_token)


def test_report_rejection_note_clears_when_same_demand_satisfied(monkeypatch):
    """后续一致更长帧实际满足同一需求后，警告在成文清除，无假未解决警报。"""
    from tests.ifind_daily_testutil import std_frame
    from src.core.research_run import current_research
    from src.deep_research_dims.context import _safe_history
    from src.services import history_loader as H

    frames = [
        std_frame(rows=40),
        (std_frame(rows=120), "EfinanceFetcher"),
        std_frame(rows=120),  # 后续一致更长窗口（同源重叠一致）
    ]
    run, research_token, manager, ctx, days = _report_harness(monkeypatch, frames)
    try:
        ctx.history = _safe_history("600519", days, ctx)
        assert not ctx.history
        assert "2026-11-06|90" in run.unmet_daily_history_snapshot()
        df, _source = H.load_history_df("600519", days=120)  # 后续需求触发一致扩展
        assert len(df) == 120
        assert run.unmet_daily_history_snapshot() == {}  # 同一 90 天需求已被实际覆盖满足
        dims, markdown, _ = _assemble(ctx)
        assert "daily window expansion rejected" not in markdown
        assert len(ctx.history) == 120 and dims["data"].status == "ok"
        assert "来源 **IfindDailyFetcher**" in markdown
        calls = len(manager.calls)
        assert calls == 3  # reconcile/render 零额外取数
        df_short, short_source = H.load_history_df("600519", days=30)
        assert len(df_short) == 120 and short_source == "IfindDailyFetcher"
        assert len(manager.calls) == calls  # 更短需求 0 网络
    finally:
        current_research.reset(research_token)


def test_report_genuine_short_history_has_no_rejection_note(monkeypatch):
    """真实短历史（IPO/原源首取）不成文拒绝：无扩窗注记，短窗事实保留。"""
    from tests.ifind_daily_testutil import std_frame
    from src.core.research_run import current_research
    from src.deep_research_dims.context import _safe_history

    run, research_token, manager, ctx, days = _report_harness(
        monkeypatch, [std_frame(rows=10)], prior_days=0
    )
    try:
        ctx.history = _safe_history("600519", days, ctx)
        assert len(ctx.history) == 10  # 供应商确实只有 10 行：照常采纳
        dims, markdown, _ = _assemble(ctx)
        assert "daily window expansion rejected" not in markdown
        assert "日线历史数据缺失" not in markdown
        assert run.unmet_daily_history_snapshot() == {}
        assert dims["data"].daily_history_source == "IfindDailyFetcher"  # 短窗事实保留
        assert "来源 **IfindDailyFetcher**" in markdown
    finally:
        current_research.reset(research_token)


def test_report_generic_missing_note_clears_on_adoption(monkeypatch):
    """通用缺失注记（非拒绝形）在采纳斯照常清除：既有恢复语义不变。"""
    from tests.ifind_daily_testutil import std_frame
    from src.core.research_run import current_research
    from src.deep_research_dims.context import HISTORY_MISSING_NOTE, _safe_history
    from src.services import history_loader as H

    run, research_token, manager, ctx, days = _report_harness(
        monkeypatch, [std_frame(rows=40)], prior_days=0,
    )
    original_call = manager.get_daily_data

    def fail_first(*args, **kwargs):
        if not fail_first.done:  # type: ignore[attr-defined]
            fail_first.done = True  # type: ignore[attr-defined]
            raise _FakeManagerError("供应商失败")
        return original_call(*args, **kwargs)

    fail_first.done = False  # type: ignore[attr-defined]
    manager.get_daily_data = fail_first
    try:
        ctx.history = _safe_history("600519", days, ctx)
        assert not ctx.history
        assert any(note.startswith(HISTORY_MISSING_NOTE) for note in ctx.limitations)
        assert run.unmet_daily_history_snapshot() == {}  # 通用失败不记未满足扩窗
        H.load_history_df("600519", days=30)  # 后续需求取得并发布窗口
        dims, markdown, _ = _assemble(ctx)
        assert "日线历史数据缺失" not in markdown
        assert len(ctx.history) == 40 and dims["data"].status == "ok"
    finally:
        current_research.reset(research_token)


class _FakeManagerError(Exception):
    pass


def test_interrupted_report_retains_facts_and_unresolved_rejection(monkeypatch):
    """取消中断：已取得事实（保留帧+溯源）与未解决拒绝注记同时随部分报告留存。"""
    from tests.ifind_daily_testutil import std_frame
    from src.agent.deep_research.orchestrator import _interrupted_result
    from src.core.research_run import current_research
    from src.deep_research_dims.context import _safe_history
    from src.services import history_loader as H

    frames = [std_frame(rows=40), (std_frame(rows=120), "EfinanceFetcher")]
    run, research_token, manager, ctx, days = _report_harness(monkeypatch, frames)
    try:
        H.load_history_df("600519", days=30)
        ctx.history = _safe_history("600519", days, ctx)
        dims, _markdown, _ = _assemble(ctx)  # wave1 对齐：复用保留帧
        run.context = ctx
        run.stop("cancelled")
        result = _interrupted_result(run, "600519", "贵州茅台", {"data"}, None)
        assert result.status == "partial"
        assert "daily window expansion rejected" in result.markdown
        assert "来源 **IfindDailyFetcher**" in result.markdown
        data = result.dims["data"]
        assert data.daily_history_source == "IfindDailyFetcher"
        assert data.daily_history_as_of == "2026-11-06"
        assert "2026-11-06|90" in run.unmet_daily_history_snapshot()  # 未解决：不假装满足
        assert len(manager.calls) == 2  # 中断渲染不取数
    finally:
        current_research.reset(research_token)


# ---------------------------------------------------------------------------
# PR87 R13：迟到拒绝不复活已解决状态（真实 loader 交错 → 最终成文）
# ---------------------------------------------------------------------------
def test_late_rejection_after_satisfied_adoption_no_false_warning(monkeypatch):
    """root 竞态 2：B 一致采纳 120 后，A 的迟到拒绝不得污染最终报告。

    真实采纳入口决策不被替换；仅在真实 adopt 返回后调度 A 的外向恢复。
    终态：权威帧 120、未满足状态为空、真实 90 天读取返回 120、最终 MD 无
    假未解决警告且 120 行事实/溯源在场。
    """
    from threading import Event
    from concurrent.futures import ThreadPoolExecutor

    from tests.ifind_daily_testutil import std_frame
    from src.core.research_run import current_research
    from src.services import history_loader as H

    frames = [
        std_frame(rows=40),
        (std_frame(rows=120), "EfinanceFetcher"),
        std_frame(rows=120),
    ]
    run, research_token, manager, ctx, days = _report_harness(monkeypatch, frames)
    rejected_snapshot = Event()
    release_rejection = Event()
    original_adopt = run.adopt_daily_history

    def scheduled_adopt(as_of, records, source):
        result = original_adopt(as_of, records, source)  # 真实原子采纳先完成
        if source == "EfinanceFetcher":
            rejected_snapshot.set()
            assert release_rejection.wait(10), "successful extension did not finish"
        return result

    monkeypatch.setattr(run, "adopt_daily_history", scheduled_adopt)

    def rejected_load():
        thread_token = current_research.set(run)
        try:
            frame, source = H.load_history_df("600519", days=90)
            return {"records": 0 if frame is None else len(frame), "source": source}
        finally:
            current_research.reset(thread_token)

    try:
        H.load_history_df("600519", days=30)  # call1：采用 40 行 iFinD
        with ThreadPoolExecutor(max_workers=1) as pool:
            late = pool.submit(rejected_load)
            assert rejected_snapshot.wait(10), "conflicting expansion did not reach adoption"
            accepted, source = H.load_history_df("600519", days=120)  # call3 前的 call2 已发生
            assert len(accepted) == 120 and source == "IfindDailyFetcher"
            assert run.unmet_daily_history_snapshot() == {}  # 采纳即消解（尚无记录）
            release_rejection.set()
            late_result = late.result(timeout=10)
        # A 的迟到拒绝如实返回给该次需求，但不复活已解决状态
        assert late_result["records"] == 0
        assert "daily window expansion rejected" in late_result["source"]
        assert run.unmet_daily_history_snapshot() == {}
        current, current_source = H.load_history_df("600519", days=90)  # 缓存复用：120
        assert len(current) == 120 and current_source == "IfindDailyFetcher"
        assert not ctx.history  # 与 root 证明同形：空历史交给真实 reconcile 采纳
        dims, markdown, _ = _assemble(ctx)
        assert len(ctx.history) == 120  # reconcile 采纳完整权威槽
        assert "daily window expansion rejected" not in markdown  # 无假未解决警告
        assert "来源 **IfindDailyFetcher**" in markdown
        assert dims["data"].status == "ok"
        assert len(manager.calls) == 3  # 缓存复用/reconcile/render 零取数
    finally:
        release_rejection.set()
        current_research.reset(research_token)
