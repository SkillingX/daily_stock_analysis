# -*- coding: utf-8 -*-
"""个股财务分析端点（5 个，挂 /api/v1/financial-analysis）。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from src.schemas.financial_analysis import (
    FinancialAnalysisDeleteResponse,
    FinancialAnalysisDetailResponse,
    FinancialAnalysisGenerateResponse,
    FinancialAnalysisListResponse,
    FinancialAnalysisReportItem,
)
from src.services import financial_analysis_service
from src.storage import get_db

logger = logging.getLogger(__name__)
router = APIRouter()
_REPORT_ID_RE = re.compile(r"^fa_\d{12}(_\d+)?$")


def _validate_id(report_id: str) -> str:
    if not report_id or not _REPORT_ID_RE.fullmatch(report_id):
        raise HTTPException(status_code=404, detail="报告不存在")
    return report_id


@router.post("/generate", response_model=FinancialAnalysisGenerateResponse)
async def generate(stock_code: str, stock_name: Optional[str] = None):
    try:
        raw = await asyncio.to_thread(
            financial_analysis_service.generate_report, stock_code, stock_name
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.error("[FinAnalysis] 生成失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"生成失败：{exc}")
    # 走 Pydantic 校验（response_model 会再做一次）
    return FinancialAnalysisGenerateResponse.model_validate({
        "report_id": raw.get("report_id"),
        "stock_code": raw["stock_code"],
        "stock_name": raw["stock_name"],
        "status": raw["status"],
        "markdown": raw["markdown"],
        "analysis": raw["analysis"],
    })


@router.get("/reports", response_model=FinancialAnalysisListResponse)
async def list_reports(stock_code: Optional[str] = None,
                       limit: int = Query(50, ge=1, le=200),
                       offset: int = Query(0, ge=0)):
    rows, _total = await asyncio.to_thread(
        get_db().list_financial_analysis_reports, stock_code, limit, offset
    )
    items = [FinancialAnalysisReportItem.model_validate(r) for r in rows]
    return {"success": True, "data": items, "total": _total}


@router.get("/reports/{report_id}", response_model=FinancialAnalysisDetailResponse)
async def get_report(report_id: str):
    _validate_id(report_id)
    record = await asyncio.to_thread(get_db().get_financial_analysis_report, report_id)
    if record is None:
        raise HTTPException(status_code=404, detail="报告不存在")
    # storage 层 analysis_json 持久化为 JSON 字符串，schema 期望 dict — 显式反序列化
    raw_analysis = record.get("analysis_json")
    if isinstance(raw_analysis, str) and raw_analysis:
        try:
            record["analysis_json"] = json.loads(raw_analysis)
        except json.JSONDecodeError:
            record["analysis_json"] = None
    if record.get("md_path"):
        try:
            record["markdown"] = Path(record["md_path"]).read_text(encoding="utf-8")
        except OSError:
            record["markdown"] = ""
    item = FinancialAnalysisReportItem.model_validate(record)
    return {"success": True, "data": item}


@router.get("/reports/{report_id}/markdown")
async def download_markdown(report_id: str, download: int = 0):
    _validate_id(report_id)
    record = await asyncio.to_thread(get_db().get_financial_analysis_report, report_id)
    if record is None:
        raise HTTPException(status_code=404, detail="报告不存在")
    path = Path(record["md_path"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="报告文件不存在")
    media_type = "text/markdown; charset=utf-8"
    filename = f"{report_id}.md" if download else None
    if filename:
        return FileResponse(str(path), media_type=media_type, filename=filename)
    return FileResponse(str(path), media_type=media_type)


@router.delete("/reports/{report_id}", response_model=FinancialAnalysisDeleteResponse)
async def delete_report(report_id: str):
    _validate_id(report_id)
    paths = await asyncio.to_thread(get_db().delete_financial_analysis_report, report_id)
    if paths is None:
        raise HTTPException(status_code=404, detail="报告不存在")
    p = paths.get("md_path")
    if p:
        try:
            Path(p).unlink(missing_ok=True)
        except OSError:
            pass
    return {"success": True, "deleted": report_id}
