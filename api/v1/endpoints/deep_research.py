# -*- coding: utf-8 -*-
"""A股深度投研报告 API endpoints（五层穿透框架）。

形态：表单输入股票 → SSE 流式生成 → 报告展示 + PDF 下载 + 历史列表。
与问股/郑希/供应链的对话框模式不同（无多轮会话），但复用同一套 SSE 线程池包装。

接口（9 个，挂 ``/api/v1/deep-research``，继承全局 AuthMiddleware）：
- ``POST /generate/stream``  SSE 流式生成（thinking/tool_start/tool_done/generating/done/error/heartbeat）
- ``GET  /runs/{run_id}``  查询本次运行及清理状态
- ``POST /runs/{run_id}/cancel`` 主动取消（幂等）
- ``GET  /reports``          历史报告列表（分页）
- ``GET  /reports/{id}``     报告详情（含 Markdown 正文）
- ``DELETE /reports/{id}``   删除报告（元数据 + .md + .pdf + 维度产物）
- ``GET  /reports/{id}/pdf`` PDF 下载（惰性生成，``asyncio.to_thread`` 限流）
- ``GET  /reports/{id}/markdown`` Markdown 原文件下载（双轨/legacy 通用）
- ``GET  /reports/{id}/dims``     双轨维度 JSON 产物（legacy 报告 404）

安全：
- ``report_id`` 白名单 ``^\\d{6}_\\d{12}$`` + ``_resolve_asset_path`` 双重防穿越。
- 入口 A 股校验（``normalize_a_share``），非 A 股直接 HTTP 400。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from queue import Empty, Full, Queue
from uuid import uuid4
from pathlib import Path
from typing import Annotated, Any, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException, Path as PathParam, Query
from fastapi.responses import FileResponse, StreamingResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from src.core.research_run import ResearchRun, ResearchStopped
from src.config import get_config
from src.services.deep_research_service import (
    DeepResearchInputError,
    deep_research_service,
    get_deep_research_dir,
    normalize_a_share,
)
from src.services.report_filename import format_stock_report_pdf_filename
from src.services.task_queue import try_submit_long_task

logger = logging.getLogger(__name__)

router = APIRouter()

# 心跳间隔（保活连接，防 nginx/uvicorn 静默切断）
HEARTBEAT_INTERVAL_S = 30.0

# report_id 白名单：{6位A股代码}_{YYYYMMDDHHmm}，可选 _序号 后缀（同分钟冲突追加，如 _1）
# 后缀仅允许 _\d+，路径穿越字符（../ 等）仍被拒绝
_REPORT_ID_RE = re.compile(r"^\d{6}_\d{12}(_\d+)?$")

# PDF 渲染并发限流（xhtml2pdf 内存密集，模块级 Semaphore(1) 串行化惰性生成）
_PDF_RENDER_SEMAPHORE = asyncio.Semaphore(1)

# Runs live only in this process. Keep bounded terminal receipts for idempotent cancellation.
@dataclass
class _StreamRun:
    control: ResearchRun
    code: str
    events: Queue[Dict[str, Any]] = field(default_factory=lambda: Queue(maxsize=128))
    terminal: Optional[Dict[str, Any]] = None
    connected: bool = True
    cleaned: threading.Event = field(default_factory=threading.Event)


_RUNS: OrderedDict[str, _StreamRun] = OrderedDict()
_IN_FLIGHT_REQUESTS: Dict[str, str] = {}
_RUNS_LOCK = threading.RLock()


# ============================================================
# Schemas
# ============================================================


class DeepResearchRequest(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, validate_assignment=True)
    run_id: Optional[Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]] = None
    stock_code: Annotated[str, Field(min_length=1, max_length=32)]
    stock_name: Optional[str] = None
    report_type: str = "deep"
    # 分析范围：None/空 = 全部19维；传入时自动补依赖闭包
    dims: Optional[List[str]] = None
    # 跳过维度缓存强制重算（默认复用未过期缓存）
    force_refresh: bool = False


class ReportListItem(BaseModel):
    id: str
    stock_code: str
    stock_name: Optional[str] = None
    created_at: Optional[str] = None
    status: Optional[str] = None
    quality_score: Optional[int] = None
    missing_layers: List[str] = []
    has_pdf: bool = False


class ReportListResponse(BaseModel):
    success: bool
    data: List[ReportListItem]
    total: int


class ReportDetailResponse(BaseModel):
    success: bool
    data: Dict[str, Any]


# ============================================================
# Helpers
# ============================================================


def _validate_report_id(report_id: str) -> str:
    """report_id 白名单校验（防路径穿越第一道关）。不通过抛 404。"""
    if not report_id or not _REPORT_ID_RE.fullmatch(report_id):
        raise HTTPException(status_code=404, detail="报告不存在")
    return report_id


def _resolve_safe_path(path_str: str) -> Optional[Path]:
    """将报告文件路径收敛到 deep_research 目录内（防穿越第二道关）。

    复用 app._resolve_asset_path 的思路：resolve 后必须 is_relative_to。
    """
    if not path_str:
        return None
    try:
        root = get_deep_research_dir().resolve()
        candidate = Path(path_str).resolve()
        if candidate.is_relative_to(root):
            return candidate
    except (OSError, ValueError):
        pass
    return None


def _require_agent(config) -> None:
    if not config.is_agent_available():
        raise HTTPException(status_code=400, detail="Agent mode is not enabled")


# ============================================================
# 生成（SSE 流式）
# ============================================================


class ResearchRunStatus(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, validate_assignment=True)
    run_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
    status: Literal["running", "cancelling", "finishing", "finished"]
    end_reason: Literal["report_budget", "cancelled"] | None = None
    report_id: Annotated[str, Field(pattern=_REPORT_ID_RE.pattern)] | None = None


def _run_status(run_id: str, record: _StreamRun) -> ResearchRunStatus:
    control = record.control
    status = "finished" if record.cleaned.is_set() else "cancelling" if control.stop_reason == "cancelled" else "finishing" if control.finalized else "running"
    return ResearchRunStatus(run_id=run_id, status=status, end_reason=control.stop_reason,
                             report_id=(record.terminal or {}).get("report_id"))


@router.get("/runs/{run_id}", response_model=ResearchRunStatus)
async def get_run(run_id: Annotated[str, PathParam(pattern=r"^[a-f0-9]{32}$")]) -> ResearchRunStatus:
    with _RUNS_LOCK:
        record = _RUNS.get(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="本次运行不存在或已超出进程内保留范围")
        return _run_status(run_id, record)


@router.post("/runs/{run_id}/cancel", response_model=ResearchRunStatus)
async def cancel_run(run_id: Annotated[str, PathParam(pattern=r"^[a-f0-9]{32}$")]) -> ResearchRunStatus:
    with _RUNS_LOCK:
        record = _RUNS.get(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="本次运行尚未被接收或已超出保留范围")
        record.control.stop("cancelled")
        return _run_status(run_id, record)


@router.post("/generate/stream")
async def generate_stream(request: DeepResearchRequest) -> StreamingResponse:
    from src.core.market_policy import require_stock_market
    require_stock_market(request.stock_code)
    config = get_config()
    _require_agent(config)
    try:
        code = normalize_a_share(request.stock_code)
    except DeepResearchInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if request.dims:
        from src.schemas.deep_research_dims import (
            RetiredDimRequestError,
            resolve_dim_selection,
        )

        # issue #64：先校验未知 ID（422），再剔除退役 ID；仅退役在受理/取数前 400。
        try:
            resolved_dims = resolve_dim_selection(request.dims)
        except RetiredDimRequestError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    else:
        resolved_dims = None
    run_id = request.run_id or uuid4().hex
    record = _StreamRun(ResearchRun(config.deep_research_timeout_seconds, run_id=run_id), code)
    with _RUNS_LOCK:
        if code in _IN_FLIGHT_REQUESTS:
            raise HTTPException(status_code=409, detail=f"{code}的报告正在生成或收尾，请稍后到历史记录查看。")
        if run_id in _RUNS:
            raise HTTPException(status_code=409, detail="本次运行已提交，请勿重复生成。")
        _RUNS[run_id] = record
        _IN_FLIGHT_REQUESTS[code] = run_id

    def progress(event: Dict[str, Any]) -> None:
        event = {**event, "run_id": run_id}
        with _RUNS_LOCK:
            if event.get("type") in {"done", "error"}:
                record.terminal = event
            elif record.connected:
                try:
                    record.events.put_nowait(event)
                except Full:
                    try:
                        record.events.get_nowait()
                    except Empty:
                        pass
                    record.events.put_nowait(event)

    def run_sync() -> None:
        try:
            result = deep_research_service.generate_report(
                raw_code=code, raw_name=request.stock_name, report_type=request.report_type,
                progress_callback=progress,
                dims=sorted(resolved_dims) if resolved_dims else None,
                force_refresh=request.force_refresh,
                run=record.control,
            )
            if record.terminal is None:
                progress({"type": "done", **result})
        except ResearchStopped:
            # The interrupted-result path folds completed evidence into a
            # normal return; reaching this handler means that fold itself could
            # not deliver a result, so the accepted job still gets exactly one
            # terminal carrying the run's own reason (issue #58).
            logger.warning("[DeepResearch] Run %s stopped: %s", run_id, record.control.reason_text)
            progress({"type": "error", "message": record.control.reason_text})
        except (asyncio.CancelledError, BaseExceptionGroup) as exc:
            # A cancellation-shaped escape is a user cancel only when the run's
            # own state says so (issue #58): an explicit stop_reason or an
            # actually exhausted deadline — finalization alone (the service's
            # own ``finally``) is not budget exhaustion and must not invent a
            # timeout/cancel attribution. An unexpected cancellation with no
            # run stop keeps the honest failure wording.
            from src.core.research_run import is_cancellation_exception

            if isinstance(exc, BaseExceptionGroup) and exc.subgroup((KeyboardInterrupt, SystemExit)) is not None:
                raise
            control = record.control
            stopped = control.stop_reason is not None or control.remaining() <= 0
            if stopped and is_cancellation_exception(exc):
                if control.stop_reason is None:
                    control.stop("report_budget")
                logger.warning("[DeepResearch] Run %s stopped: %s", run_id, control.reason_text)
                progress({"type": "error", "message": control.reason_text})
            else:
                logger.exception("[DeepResearch] Run %s failed", run_id)
                progress({"type": "error", "message": "报告生成失败，请查看服务日志。"})
        except Exception:
            logger.exception("[DeepResearch] Run %s failed", run_id)
            progress({"type": "error", "message": "报告生成失败，请查看服务日志。"})
        finally:
            record.control.finish()

    future = try_submit_long_task(run_sync)
    if future is None:
        with _RUNS_LOCK:
            _RUNS.pop(run_id, None)
            if _IN_FLIGHT_REQUESTS.get(code) == run_id:
                _IN_FLIGHT_REQUESTS.pop(code)
        raise HTTPException(status_code=503, detail="分析任务较多，请稍后重试。")
    record.control.track(future)

    def clean() -> None:
        # The run is finalized and all in-flight attempts have returned: fold
        # late usage into the final terminal event and saved artifacts before
        # the terminal is delivered and the per-stock gate is released.
        terminal = record.terminal
        terminal_usage = terminal.get("model_usage") if isinstance(terminal, dict) else None
        if (
            isinstance(terminal, dict)
            and terminal_usage is not None
            and not terminal.get("cache_hit")
            and (terminal_usage or {}).get("origin_run_id") in (None, run_id)
        ):
            refreshed = deep_research_service.refresh_report_usage(
                record.control, terminal.get("report_id"), code,
                sorted(resolved_dims) if resolved_dims else None,
            )
            if refreshed is not None:
                terminal["model_usage"] = refreshed
        with _RUNS_LOCK:
            record.cleaned.set()
            if _IN_FLIGHT_REQUESTS.get(code) == run_id:
                _IN_FLIGHT_REQUESTS.pop(code)
            for old_id, old_record in list(_RUNS.items()):
                if len(_RUNS) <= 256:
                    break
                if old_record.cleaned.is_set():
                    _RUNS.pop(old_id)

    record.control.when_clean(clean)

    async def event_generator():
        def encode(event: Dict[str, Any]) -> str:
            return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
        last_event = time.monotonic()
        partial_sent = False
        try:
            yield encode({"type": "accepted", "run_id": run_id})
            while True:
                try:
                    event = record.events.get_nowait()
                except Empty:
                    event = None
                if event is not None:
                    last_event = time.monotonic()
                    yield encode(event)
                elif record.terminal is not None and record.cleaned.is_set():
                    yield encode(record.terminal)
                    return
                elif record.terminal is not None and not partial_sent:
                    partial_sent = True
                    yield encode({**record.terminal, "type": "partial_ready", "cleanup_pending": True})
                elif time.monotonic() - last_event >= HEARTBEAT_INTERVAL_S:
                    last_event = time.monotonic()
                    yield encode({"type": "heartbeat", "run_id": run_id,
                                  "cleanup_pending": record.control.finalized and not record.cleaned.is_set()})
                else:
                    await asyncio.sleep(0.05)
        finally:
            # Transport ownership is separate from the accepted job's lifetime.
            with _RUNS_LOCK:
                record.connected = False
            while True:
                try:
                    record.events.get_nowait()
                except Empty:
                    break

    return StreamingResponse(event_generator(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive",
        "X-Research-Run-Id": run_id,
    })


# ============================================================
# 历史报告 CRUD
# ============================================================


@router.get("/reports", response_model=ReportListResponse)
async def list_reports(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    stock_code: Optional[str] = Query(None),
):
    """历史报告列表（分页，按时间倒序）。"""
    rows, total = deep_research_service.list_reports(
        limit=limit, offset=offset, stock_code=stock_code
    )
    items = [
        ReportListItem(
            id=r.get("id", ""),
            stock_code=r.get("stock_code", ""),
            stock_name=r.get("stock_name"),
            created_at=r.get("created_at"),
            status=r.get("status"),
            quality_score=r.get("quality_score"),
            missing_layers=r.get("missing_layers", []),
            has_pdf=bool(r.get("pdf_path")),
        )
        for r in rows
    ]
    return ReportListResponse(success=True, data=items, total=total)


@router.get("/reports/{report_id}", response_model=ReportDetailResponse)
async def get_report(report_id: str):
    """报告详情（含 Markdown 正文）。"""
    _validate_report_id(report_id)
    data = deep_research_service.get_report(report_id)
    if data is None:
        raise HTTPException(status_code=404, detail="报告不存在")
    return ReportDetailResponse(success=True, data=data)


@router.delete("/reports/{report_id}")
async def delete_report(report_id: str):
    """删除报告（元数据 + .md + .pdf 文件）。"""
    _validate_report_id(report_id)
    ok = deep_research_service.delete_report(report_id)
    if not ok:
        raise HTTPException(status_code=404, detail="报告不存在")
    logger.info("[DeepResearch] 删除报告 %s（via API）", report_id)
    return {"success": True, "deleted": report_id}


# ============================================================
# PDF 下载（惰性生成）
# ============================================================


@router.get("/reports/{report_id}/pdf")
async def download_pdf(report_id: str):
    """PDF 下载：惰性生成（首次请求在线程池生成，后续直接发文件）。

    PDF 生成用 ``asyncio.to_thread`` 包 ``md2pdf.markdown_to_pdf_file``，
    避免同步渲染阻塞 event loop；模块级 Semaphore(1) 限并发防爆内存。
    """
    _validate_report_id(report_id)

    # 区分「报告不存在」与「渲染失败」，给出更准确的错误信息
    record = await asyncio.to_thread(deep_research_service.get_report, report_id)
    if record is None:
        raise HTTPException(status_code=404, detail="报告不存在")

    # 惰性生成（线程池 + Semaphore(1) 串行化，防并发渲染打爆内存）
    async with _PDF_RENDER_SEMAPHORE:
        pdf_path_str = await asyncio.to_thread(
            deep_research_service.get_pdf_path, report_id
        )
    if not pdf_path_str:
        raise HTTPException(
            status_code=404,
            detail="PDF 生成失败（渲染依赖不可用或报告正文为空），请稍后重试",
        )

    # 防穿越第二道关：路径必须收敛在 deep_research 目录内
    safe_path = _resolve_safe_path(pdf_path_str)
    if safe_path is None or not safe_path.exists():
        raise HTTPException(status_code=404, detail="PDF 文件不存在")

    # 业务文件名（按 docs/pdf-download-filename-plan.md）：
    # 科瑞技术（002957）深度投研报告20260630.pdf
    #
    # created_at 兜底：从 report_id（格式 ``{code}_{YYYYMMDDHHmm}[_seq]``）推断日期，
    # 防止 record 字段缺失时（如测试 mock / 历史数据迁移）回退到今天导致文件名漂移。
    _record_created_at = record.get("created_at")
    if not _record_created_at:
        _id_date = report_id.split("_", 1)[1][:8] if "_" in report_id else ""
        if len(_id_date) == 8 and _id_date.isdigit():
            _record_created_at = f"{_id_date[:4]}-{_id_date[4:6]}-{_id_date[6:8]}"
    download_filename = format_stock_report_pdf_filename(
        stock_name=record.get("stock_name"),
        stock_code=record.get("stock_code") or report_id.split("_", 1)[0],
        report_type="deep_research",
        created_at=_record_created_at,
    )

    return FileResponse(
        str(safe_path),
        media_type="application/pdf",
        filename=download_filename,  # 触发 Content-Disposition: attachment（前端 download.ts 会优先用）
        headers={
            # 按 docs/pdf-generation-unification-plan.md §6.5：禁止浏览器/中间层缓存 PDF，
            # 避免服务端修复后用户仍看到旧文件。服务端 pdf_path 缓存仍复用，no-store
            # 只要求浏览器回源，不会每次重新渲染。
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


# ============================================================
# Markdown 原文件下载 + 双轨维度产物
# ============================================================


@router.get("/reports/{report_id}/markdown")
async def download_markdown(report_id: str):
    """下载报告 Markdown 原文件（双轨与 legacy 引擎通用）。"""
    _validate_report_id(report_id)
    record = await asyncio.to_thread(deep_research_service.get_report, report_id)
    if record is None:
        raise HTTPException(status_code=404, detail="报告不存在")

    safe_path = _resolve_safe_path(record.get("md_path") or "")
    if safe_path is None or not safe_path.exists():
        raise HTTPException(status_code=404, detail="Markdown 文件不存在")

    _created = record.get("created_at")
    if not _created and "_" in report_id:
        _d = report_id.split("_", 1)[1][:8]
        if len(_d) == 8 and _d.isdigit():
            _created = f"{_d[:4]}-{_d[4:6]}-{_d[6:8]}"
    from src.services.report_filename import (
        _clean_filename_part,
        _format_date,
        STOCK_REPORT_TYPE_LABELS,
    )

    label = STOCK_REPORT_TYPE_LABELS.get("deep_research", "报告")
    name = _clean_filename_part(record.get("stock_name")) if record.get("stock_name") else ""
    code = _clean_filename_part(record.get("stock_code") or report_id.split("_", 1)[0])
    display = name or code or "未知"
    filename = f"{display}（{code}）{label}{_format_date(_created)}.md"

    from urllib.parse import quote
    return Response(
        content=record.get("markdown") or "",
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename),
                 "Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@router.get("/reports/{report_id}/dims")
async def get_dims(report_id: str):
    """双轨引擎维度 JSON 产物（11 维度结构化数据 + 护栏事件表）。

    legacy 引擎报告无产物 → 404。数据仅供复算/钻取，无缓存要求。
    """
    _validate_report_id(report_id)
    payload = await asyncio.to_thread(deep_research_service.get_dims_payload, report_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="维度产物不存在（legacy 引擎报告无此产物）")
    return {"success": True, "data": payload}


@router.get("/reports/{report_id}/dims/{dim_id}")
async def get_dim_report(report_id: str, dim_id: str, download: int = 0):
    """单维度子报告（Markdown）：报告区对应章节的独立详版。

    浏览器直接打开即可内联查看（text/markdown）；``?download=1`` 带文件名
    触发附件下载（前端 <a download> 亦生效）。双轨报告经 ``_write_dim_reports``
    落盘；legacy 报告无子报告 → 404。
    """
    _validate_report_id(report_id)
    from src.schemas.deep_research_dims import DIM_IDS

    if dim_id not in DIM_IDS:
        raise HTTPException(
            status_code=404,
            detail=f"未知维度: {dim_id}（可选: {', '.join(DIM_IDS)}）",
        )
    data = await asyncio.to_thread(
        deep_research_service.get_dim_report, report_id, dim_id
    )
    if data is None:
        raise HTTPException(
            status_code=404, detail="维度子报告不存在（legacy 引擎报告无子报告）"
        )
    safe_path = _resolve_safe_path(data["path"])
    if safe_path is None or not safe_path.exists():
        raise HTTPException(status_code=404, detail="维度子报告文件不存在")
    headers = {"Cache-Control": "no-store, no-cache, must-revalidate"}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{report_id}_dim_{dim_id}.md"'
    return Response(content=data["markdown"], media_type="text/markdown; charset=utf-8", headers=headers)
