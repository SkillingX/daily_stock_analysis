# -*- coding: utf-8 -*-
"""A股深度投研报告 API endpoints（五层穿透框架）。

形态：表单输入股票 → SSE 流式生成 → 报告展示 + PDF 下载 + 历史列表。
与问股/郑希/供应链的对话框模式不同（无多轮会话），但复用同一套 SSE 线程池包装。

接口（7 个，挂 ``/api/v1/deep-research``，继承全局 AuthMiddleware）：
- ``POST /generate/stream``  SSE 流式生成（thinking/tool_start/tool_done/generating/done/error/heartbeat）
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
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

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

# SSE event 间隔 timeout（五层穿透 + 报告生成 2–5 分钟，对齐 executor 1200s）
STREAM_QUEUE_TIMEOUT_S = 1200.0
# 心跳间隔（保活连接，防 nginx/uvicorn 静默切断）
HEARTBEAT_INTERVAL_S = 30.0

# report_id 白名单：{6位A股代码}_{YYYYMMDDHHmm}，可选 _序号 后缀（同分钟冲突追加，如 _1）
# 后缀仅允许 _\d+，路径穿越字符（../ 等）仍被拒绝
_REPORT_ID_RE = re.compile(r"^\d{6}_\d{12}(_\d+)?$")

# PDF 渲染并发限流（xhtml2pdf 内存密集，模块级 Semaphore(1) 串行化惰性生成）
_PDF_RENDER_SEMAPHORE = asyncio.Semaphore(1)

# 深度投研 in-flight 请求去重（防止用户快速点击导致重复生成）
# key: stock_code, value: asyncio.Event（生成完成时置位）
_IN_FLIGHT_REQUESTS: Dict[str, asyncio.Event] = {}


# ============================================================
# Schemas
# ============================================================


class DeepResearchRequest(BaseModel):
    stock_code: str
    stock_name: Optional[str] = None
    report_type: str = "deep"
    # 维度子集（省钱模式）：None/空 = 全部 11 维度；传入时自动补依赖闭包
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


@router.post("/generate/stream")
async def generate_stream(request: DeepResearchRequest):
    """SSE 流式生成深度投研报告。

    入口先同步校验 A 股代码（非 A 股直接 400，不浪费 SSE 连接），
    通过后在线程池跑 ``deep_research_service.generate_report``，
    progress_callback 把事件塞入 asyncio.Queue，event_generator 消费并加 30s 心跳。
    """
    config = get_config()
    _require_agent(config)

    # 入口预校验（非 A 股 / 非法维度直接 400，不浪费 SSE 连接）
    try:
        normalize_a_share(request.stock_code)
    except DeepResearchInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if request.dims:
        from src.schemas.deep_research_dims import DIM_IDS

        unknown = [d for d in request.dims if d not in DIM_IDS]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"未知维度: {', '.join(unknown)}（可选: {', '.join(DIM_IDS)}）",
            )

    # ── In-flight 请求去重 ──
    # 同一股票如果已有进行中的生成请求，直接返回冲突错误，避免重复生成
    dedupe_key = request.stock_code.strip()
    if dedupe_key in _IN_FLIGHT_REQUESTS:
        logger.warning(
            "[DeepResearch] 股票 %s 已有进行中的生成请求，拒绝重复提交",
            dedupe_key,
        )
        raise HTTPException(
            status_code=409,
            detail=f"股票 {dedupe_key} 已有生成任务正在进行中，请等待完成后重试",
        )

    # 标记为进行中（函数返回时清除）
    in_flight_event = asyncio.Event()
    _IN_FLIGHT_REQUESTS[dedupe_key] = in_flight_event

    loop = asyncio.get_running_loop()
    queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue()

    def progress_callback(event: Dict[str, Any]) -> None:
        # executor/service 在工作线程调用，需跨线程把事件塞回 event loop
        try:
            asyncio.run_coroutine_threadsafe(queue.put(event), loop)
        except RuntimeError:
            pass  # loop 已关闭，忽略

    def run_sync() -> None:
        try:
            deep_research_service.generate_report(
                raw_code=request.stock_code,
                raw_name=request.stock_name,
                report_type=request.report_type,
                progress_callback=progress_callback,
                dims=request.dims,
                force_refresh=request.force_refresh,
            )
            # done 事件由 service.generate_report 内部推
        except DeepResearchInputError as exc:
            asyncio.run_coroutine_threadsafe(
                queue.put({"type": "error", "message": str(exc)}), loop
            )
        except Exception as exc:
            logger.error("[DeepResearch] stream error: %s", exc, exc_info=True)
            asyncio.run_coroutine_threadsafe(
                queue.put({"type": "error", "message": f"生成失败：{exc}"}), loop
            )

    async def event_generator():
        import time

        fut_sync = try_submit_long_task(run_sync)
        if fut_sync is None:
            # 线程池满：清理 in-flight 标记后返回错误
            _IN_FLIGHT_REQUESTS.pop(dedupe_key, None)
            yield (
                "data: "
                + json.dumps(
                    {"type": "error", "message": "长任务线程池已满，请稍后重试"},
                    ensure_ascii=False,
                )
                + "\n\n"
            )
            return
        fut = asyncio.wrap_future(fut_sync)
        last_event_time = time.time()
        try:
            while True:
                try:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=HEARTBEAT_INTERVAL_S
                    )
                except asyncio.TimeoutError:
                    # 心跳：保活连接（防 nginx 静默切断）
                    if time.time() - last_event_time >= HEARTBEAT_INTERVAL_S:
                        yield (
                            "data: "
                            + json.dumps({"type": "heartbeat"}, ensure_ascii=False)
                            + "\n\n"
                        )
                    continue
                last_event_time = time.time()
                yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
                if event.get("type") in ("done", "error"):
                    break
        finally:
            # 清理 in-flight 标记（无论正常/异常结束都必须清理）
            _IN_FLIGHT_REQUESTS.pop(dedupe_key, None)
            try:
                await asyncio.wait_for(fut, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception as exc:
                logger.debug("[DeepResearch] executor cleanup error (ignored): %s", exc)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # nginx 关缓冲，配合 proxy_read_timeout >= 1200s
            "Connection": "keep-alive",
        },
    )


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

    return FileResponse(
        str(safe_path),
        media_type="text/markdown; charset=utf-8",
        filename=filename,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
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
async def get_dim_report(report_id: str, dim_id: str):
    """单维度子报告（Markdown）：报告区对应章节的独立详版。

    浏览器直接打开即可查看（text/markdown），加 ?download=1 触发附件下载。
    双轨报告经 ``_write_dim_reports`` 落盘；legacy 报告无子报告 → 404。
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
    return FileResponse(
        str(safe_path),
        media_type="text/markdown; charset=utf-8",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )
