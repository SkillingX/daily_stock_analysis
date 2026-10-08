# -*- coding: utf-8 -*-
"""基本面分析模块端点（5 个，挂 /api/v1/fundamentals，继承全局 AuthMiddleware）。"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse

from src.services import fundamentals_service
from src.storage import get_db

logger = logging.getLogger(__name__)

router = APIRouter()

_REPORT_ID_RE = re.compile(r"^fd_\d{12}(_\d+)?$")


def _validate_id(report_id: str) -> str:
    if not report_id or not _REPORT_ID_RE.fullmatch(report_id):
        raise HTTPException(status_code=404, detail="报告不存在")
    return report_id


@router.post("/generate")
async def generate(stock_code: str, stock_name: Optional[str] = None):
    """同步生成基本面专项报告（研究员并行+缓存，约 1-2 分钟）。"""
    try:
        return await asyncio.to_thread(
            fundamentals_service.generate_fundamentals_report, stock_code, stock_name
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.error("[Fundamentals] 生成失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"生成失败：{exc}")


@router.get("/reports")
async def list_reports(
    stock_code: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    rows, total = await asyncio.to_thread(
        get_db().list_fundamentals_reports, stock_code, limit, offset
    )
    return {"success": True, "data": rows, "total": total}


@router.get("/reports/{report_id}")
async def get_report(report_id: str):
    _validate_id(report_id)
    record = await asyncio.to_thread(get_db().get_fundamentals_report, report_id)
    if record is None:
        raise HTTPException(status_code=404, detail="报告不存在")
    record["markdown"] = fundamentals_service.report_markdown(record)
    return {"success": True, "data": record}


@router.get("/reports/{report_id}/markdown")
async def download_markdown(report_id: str, download: int = 0):
    _validate_id(report_id)
    record = await asyncio.to_thread(get_db().get_fundamentals_report, report_id)
    if record is None:
        raise HTTPException(status_code=404, detail="报告不存在")
    path = Path(record["md_path"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="报告文件不存在")
    import json
    dims = json.loads(record.get("dims_json") or "{}")
    if not (dims.get("financial") or {}).get("data_quality"):
        return PlainTextResponse(fundamentals_service.report_markdown(record), media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{report_id}.md"'} if download else None)
    kwargs = {"media_type": "text/markdown; charset=utf-8"}
    if download:
        kwargs["filename"] = f"{report_id}.md"
    return FileResponse(str(path), **kwargs)


@router.delete("/reports/{report_id}")
async def delete_report(report_id: str):
    _validate_id(report_id)
    paths = await asyncio.to_thread(get_db().delete_fundamentals_report, report_id)
    if paths is None:
        raise HTTPException(status_code=404, detail="报告不存在")
    for key in ("md_path", "pdf_path"):
        p = paths.get(key)
        if p:
            try:
                Path(p).unlink(missing_ok=True)
            except OSError:
                pass
    return {"success": True, "deleted": report_id}
