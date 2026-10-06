# -*- coding: utf-8 -*-
"""个股板块分析端点（5 个，挂 /api/v1/sector-analysis）。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from src.services import sector_analysis_service
from src.schemas.sector_analysis import (
    SectorAnalysisDeleteResponse,
    SectorAnalysisDetailResponse,
    SectorAnalysisGenerateResponse,
    SectorAnalysisListResponse,
    SectorAnalysisReportItem,
)
from src.storage import get_db

logger = logging.getLogger(__name__)
router = APIRouter()
_REPORT_ID_RE = re.compile(r"^sa_\d{12}([a-hx]|\d+)?$")


def _validate_id(report_id: str) -> str:
    if not report_id or not _REPORT_ID_RE.fullmatch(report_id):
        raise HTTPException(status_code=404, detail="报告不存在")
    return report_id


@router.post("/generate", response_model=SectorAnalysisGenerateResponse)
async def generate(stock_code: str, stock_name: Optional[str] = None):
    try:
        raw = await asyncio.to_thread(
            sector_analysis_service.generate_report, stock_code, stock_name
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.error("[SectorAnalysis] 生成失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"生成失败：{exc}")
    # FastAPI 的 response_model 会用 SectorAnalysisGenerateResponse 校验；
    # 这里显式再过一次保证 schema 错误在 service 边界暴露，不污染 HTTP 层
    return SectorAnalysisGenerateResponse.model_validate({
        "report_id": raw["report_id"],
        "stock_code": raw["stock_code"],
        "stock_name": raw.get("stock_name"),
        "markdown": raw["markdown"],
        "analysis": raw["analysis"],
    })


@router.get("/reports", response_model=SectorAnalysisListResponse)
async def list_reports(stock_code: Optional[str] = None,
                       limit: int = Query(50, ge=1, le=200),
                       offset: int = Query(0, ge=0)):
    rows, _total = await asyncio.to_thread(
        get_db().list_sector_analysis_reports, stock_code, limit, offset
    )
    items = [SectorAnalysisReportItem.model_validate(r) for r in rows]
    return {"success": True, "data": items, "total": _total}


@router.get("/reports/{report_id}", response_model=SectorAnalysisDetailResponse)
async def get_report(report_id: str):
    _validate_id(report_id)
    record = await asyncio.to_thread(get_db().get_sector_analysis_report, report_id)
    if record is None:
        raise HTTPException(status_code=404, detail="报告不存在")
    # storage 层 analysis_json 持久化为 JSON 字符串，schema 期望 dict — 显式反序列化
    raw_analysis = record.get("analysis_json")
    if isinstance(raw_analysis, str) and raw_analysis:
        try:
            record["analysis_json"] = json.loads(raw_analysis)
        except json.JSONDecodeError:
            record["analysis_json"] = None
    elif raw_analysis is None:
        record["analysis_json"] = None
    if record.get("md_path"):
        try:
            record["markdown"] = Path(record["md_path"]).read_text(encoding="utf-8")
        except OSError:
            record["markdown"] = ""
    item = SectorAnalysisReportItem.model_validate(record)
    return {"success": True, "data": item}


@router.get("/reports/{report_id}/markdown")
async def download_markdown(report_id: str, download: int = 0):
    _validate_id(report_id)
    record = await asyncio.to_thread(get_db().get_sector_analysis_report, report_id)
    if record is None:
        raise HTTPException(status_code=404, detail="报告不存在")
    path = Path(record["md_path"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="报告文件不存在")
    kwargs = {"media_type": "text/markdown; charset=utf-8"}
    if download:
        kwargs["filename"] = f"{report_id}.md"
    return FileResponse(str(path), **kwargs)


@router.delete("/reports/{report_id}", response_model=SectorAnalysisDeleteResponse)
async def delete_report(report_id: str):
    _validate_id(report_id)
    paths = await asyncio.to_thread(get_db().delete_sector_analysis_report, report_id)
    if paths is None:
        raise HTTPException(status_code=404, detail="报告不存在")
    p = paths.get("md_path")
    if p:
        try:
            Path(p).unlink(missing_ok=True)
        except OSError:
            pass
    return {"success": True, "deleted": report_id}
