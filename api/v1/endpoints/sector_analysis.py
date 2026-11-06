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
async def generate(stock_code: str,
                   stock_name: Optional[str] = None,
                   ) -> "SectorAnalysisGenerateResponse":
    """同步生成（报告总预算默认 300s）。终态精确表达，不假成功：

    - success → 200 + 完整载荷（部分报告同样 200，缺口随载荷透出）
    - timeout（预算耗尽，未存档）→ 504
    - failed（保存失败，未存档）→ 500
    """
    from src.core.market_policy import MarketDisabledError, require_stock_market

    # 输入错误窄化（root entry-closure 反例）：仅本专项用户输入错误（无效
    # A 股代码/未启用市场）映射 400；供应商/程序侧 ValueError 属程序缺陷，
    # 走既有通用 500（不吞、不降级、零存档），不新增通用异常框架。
    try:
        require_stock_market(stock_code)
        raw = await asyncio.to_thread(
            sector_analysis_service.generate_report, stock_code, stock_name
        )
    except (sector_analysis_service.SectorInputError, MarketDisabledError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        # 程序缺陷日志与响应都不携带异常原文/参数值/traceback（root 纪律：
        # 500 固定安全文案；日志仅保留异常类型与不含值的位置——filename/
        # lineno/function，不输出参数/源码行/locals）
        tb = exc.__traceback__
        while tb is not None and tb.tb_next is not None:
            tb = tb.tb_next
        frame = tb.tb_frame.f_code if tb is not None else None
        logger.error(
            "[SectorAnalysis] 生成失败（程序缺陷，详情见服务端诊断）: %s @ %s:%s(%s)",
            type(exc).__name__,
            frame.co_filename if frame else "<unknown>",
            tb.tb_lineno if tb else -1,
            frame.co_name if frame else "<unknown>",
        )
        raise HTTPException(status_code=500, detail="生成失败：内部错误（详见服务端日志）")
    status = raw.get("status")
    if status == "timeout":
        raise HTTPException(status_code=504, detail=raw.get("message") or "生成超时，未保存报告")
    if status == "failed":
        raise HTTPException(status_code=500, detail=raw.get("message") or "报告保存失败，未存档")
    # FastAPI 的 response_model 会用 SectorAnalysisGenerateResponse 校验；
    # 这里显式再过一次保证 schema 错误在 service 边界暴露，不污染 HTTP 层
    return SectorAnalysisGenerateResponse.model_validate({
        "report_id": raw["report_id"],
        "stock_code": raw["stock_code"],
        "stock_name": raw.get("stock_name"),
        "markdown": raw["markdown"],
        "analysis": raw["payload"],
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
            markdown = Path(record["md_path"]).read_text(encoding="utf-8")
        except OSError:
            # 存档文件缺失/不可读：明确报错，不吞异常返回 200 空正文
            raise HTTPException(
                status_code=500,
                detail="报告存档文件缺失或不可读（存档不完整），详情不可用；"
                       "可尝试直接下载或删除该记录",
            )
        if not markdown.strip():
            raise HTTPException(
                status_code=500,
                detail="报告存档文件为空（存档不完整），详情不可用",
            )
        record["markdown"] = markdown
    else:
        raise HTTPException(
            status_code=500,
            detail="报告记录缺少存档路径（存档不完整），详情不可用",
        )
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
    media_type = "text/markdown; charset=utf-8"
    filename = f"{report_id}.md" if download else None
    if filename:
        return FileResponse(str(path), media_type=media_type, filename=filename)
    return FileResponse(str(path), media_type=media_type)


@router.delete("/reports/{report_id}", response_model=SectorAnalysisDeleteResponse)
async def delete_report(report_id: str):
    """删除报告记录与存档文件。两步操作如实反馈（不静默假成功）：

    - 记录与文件都清理成功 → file_cleanup=ok
    - DB 已删但文件清理失败（OSError）→ 200 + file_cleanup=failed + 可理解
      message（记录已删除、文件可能残留）——不吞异常伪报完全成功
    """
    _validate_id(report_id)
    paths = await asyncio.to_thread(get_db().delete_sector_analysis_report, report_id)
    if paths is None:
        raise HTTPException(status_code=404, detail="报告不存在")
    p = paths.get("md_path")
    if p:
        try:
            Path(p).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("[SectorAnalysis] 报告 %s 存档文件清理失败: %s", report_id, exc)
            return {"success": True, "deleted": report_id, "file_cleanup": "failed",
                    "message": "记录已删除，但存档文件清理失败（文件可能残留，可手动删除）"}
    return {"success": True, "deleted": report_id, "file_cleanup": "ok"}
