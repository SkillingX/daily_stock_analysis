# -*- coding: utf-8 -*-
"""Schedule status and manual trigger endpoints."""

import argparse
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.deps import get_config_dep
from api.v1.errors import api_error
from api.v1.schemas.common import ErrorResponse
from api.v1.schemas.schedule import (
    ScheduleLogsResponse,
    ScheduleStatusResponse,
    ScheduleTriggerAccepted,
    ScheduleTriggerRequest,
)
from src.config import Config
from src.repositories.scheduled_task_log_repo import ScheduledTaskLogRepository
from src.services.task_queue import get_task_queue

logger = logging.getLogger(__name__)

router = APIRouter()

_VALID_TASKS = {"watchlist", "market_review"}


@router.get(
    "/status",
    response_model=ScheduleStatusResponse,
    summary="Get scheduler status",
    description="Returns recent execution logs, next scheduled runs, and health.",
)
def get_schedule_status(
    config: Config = Depends(get_config_dep),
) -> ScheduleStatusResponse:
    repo = ScheduledTaskLogRepository()

    recent_logs = []
    for entry in repo.get_recent(limit=10):
        recent_logs.append(entry.to_dict())

    next_runs: dict[str, Optional[str]] = {
        "watchlist": None,
        "market_review": None,
    }
    watchlist_time = getattr(config, "watchlist_analysis_time", "") or ""
    if watchlist_time.strip():
        next_runs["watchlist"] = watchlist_time.strip()
    market_time = getattr(config, "market_review_time", "") or ""
    if market_time.strip():
        next_runs["market_review"] = market_time.strip()

    heartbeat_path = Path(getattr(config, "database_path", "./data/stock_analysis.db")).parent / "scheduler_heartbeat"
    health_status = "unknown"
    last_heartbeat = None
    try:
        if heartbeat_path.exists():
            raw = heartbeat_path.read_text(encoding="utf-8").strip()
            last_heartbeat = raw.splitlines()[0] if raw else None
            health_status = "healthy"
    except OSError:
        pass

    return ScheduleStatusResponse(
        recent_logs=recent_logs,
        next_runs=next_runs,
        health={
            "status": health_status,
            "last_heartbeat": last_heartbeat,
        },
    )


@router.post(
    "/trigger",
    response_model=ScheduleTriggerAccepted,
    status_code=202,
    responses={
        202: {"description": "任务已接受，后台异步执行", "model": ScheduleTriggerAccepted},
        400: {"description": "非法任务名", "model": ErrorResponse},
        409: {"description": "任务正在执行", "model": ErrorResponse},
        500: {"description": "提交失败", "model": ErrorResponse},
    },
    summary="Manually trigger a scheduled task",
    description="Submits watchlist or market_review as a background task and "
    "returns 202 immediately with a task_id. Returns 409 if the task is "
    "already running. Progress can be observed via /schedule/status, "
    "/schedule/logs or the SSE progress endpoints.",
)
def trigger_task(
    request: Optional[ScheduleTriggerRequest] = Body(None),
    task_query: Optional[str] = Query(
        None, alias="task", description="任务名：watchlist 或 market_review"
    ),
    config: Config = Depends(get_config_dep),
) -> ScheduleTriggerAccepted:
    # 兼容两种调用形式：query param（历史 curl 用法，docs 原有示例）或 JSON body
    task_name = (request.task if request else None) or task_query
    if task_name is None:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "validation_error",
                "message": "Missing required parameter: task (query or body)",
            },
        )
    if task_name not in _VALID_TASKS:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_task",
                "message": f"Task must be one of: {', '.join(sorted(_VALID_TASKS))}",
            },
        )

    task_id = uuid.uuid4().hex

    if task_name == "watchlist":
        from src.core.scheduled_task_lock import (
            acquire_task_lock,
            release_task_lock,
        )

        lock_timeout = getattr(config, "schedule_lock_timeout", 7200)
        lock_token = acquire_task_lock(
            config, "watchlist_analysis", timeout_seconds=lock_timeout
        )
        if lock_token is None:
            raise api_error(
                409, "duplicate_task", f"Task '{task_name}' is already running."
            )
        try:
            task = get_task_queue().submit_background_task(
                lambda: _run_watchlist_background(config, lock_token),
                stock_code="watchlist_analysis",
                stock_name="自选股分析",
                message="自选股分析任务已提交",
                task_id=task_id,
            )
        except Exception:
            release_task_lock(lock_token)
            raise
    else:
        from src.core.market_review_lock import (
            release_market_review_lock,
            try_acquire_market_review_lock,
        )

        lock_token = try_acquire_market_review_lock(config)
        if lock_token is None:
            raise api_error(
                409,
                "duplicate_task",
                f"Task '{task_name}' is already running.",
            )
        try:
            task = get_task_queue().submit_background_task(
                lambda: _run_market_review_background_logged(
                    config, lock_token, task_id
                ),
                stock_code="market_review",
                stock_name="大盘复盘",
                message="大盘复盘任务已提交",
                task_id=task_id,
            )
        except Exception:
            release_market_review_lock(lock_token)
            raise

    trace_id = getattr(task, "trace_id", None)
    if not isinstance(trace_id, str) or not trace_id.strip():
        trace_id = task.task_id

    return ScheduleTriggerAccepted(
        status="accepted",
        message=f"Task '{task_name}' submitted for background execution.",
        task=task_name,
        task_id=task.task_id,
        trace_id=trace_id,
        triggered_at=datetime.now().isoformat(),
    )


@router.get(
    "/logs",
    response_model=ScheduleLogsResponse,
    summary="Get schedule execution logs",
    description="Returns paginated schedule execution logs.",
)
def get_schedule_logs(
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Page size"),
    task_name: Optional[str] = Query(None, description="Filter by task name"),
) -> ScheduleLogsResponse:
    repo = ScheduledTaskLogRepository()
    logs = repo.get_recent(task_name=task_name, limit=page * page_size)
    start = (page - 1) * page_size
    page_logs = [entry.to_dict() for entry in logs[start : start + page_size]]

    return ScheduleLogsResponse(
        total=len(logs),
        page=page,
        page_size=page_size,
        logs=page_logs,
    )


def _build_default_args() -> argparse.Namespace:
    """Build a minimal args namespace for manual trigger (mirrors CLI defaults)."""
    return argparse.Namespace(
        no_notify=False,
        no_market_review=False,
        single_notify=False,
        force_run=True,
        no_run_immediately=True,
        schedule=False,
        debug=False,
        dry_run=False,
    )


def _run_watchlist_task(config: Config) -> None:
    """Execute the watchlist analysis task (manual trigger, no market review)."""
    from main import _reload_runtime_config, run_full_analysis

    runtime_config = _reload_runtime_config()
    args = _build_default_args()
    args.no_market_review = True
    run_full_analysis(runtime_config, args, None)


def _run_watchlist_background(config: Config, lock_token: Any) -> None:
    """Run the watchlist task in the background with task-log bookkeeping.

    Mirrors main.py's ``watchlist_analysis_task`` lifecycle: running/success/
    failed entries in the scheduled task log, lock released in ``finally``.
    """
    from src.core.scheduled_task_lock import release_task_lock

    task_repo = ScheduledTaskLogRepository()
    scheduled_at = datetime.now()
    task_repo.save(
        task_name="watchlist_analysis",
        scheduled_at=scheduled_at,
        status="running",
        started_at=scheduled_at,
    )
    try:
        _run_watchlist_task(config)
        task_repo.save(
            task_name="watchlist_analysis",
            scheduled_at=scheduled_at,
            status="success",
            started_at=scheduled_at,
            finished_at=datetime.now(),
        )
    except Exception as exc:
        logger.exception("Manual trigger failed for watchlist: %s", exc)
        task_repo.save(
            task_name="watchlist_analysis",
            scheduled_at=scheduled_at,
            status="failed",
            started_at=scheduled_at,
            detail={"error": str(exc)},
        )
    finally:
        release_task_lock(lock_token)


def _run_market_review_background_logged(
    config: Config, lock_token: Any, query_id: str
) -> None:
    """Run market review in the background with task-log bookkeeping.

    Reuses analysis.py's ``_run_market_review_background`` (which releases the
    market-review lock itself); this wrapper adds running/success/failed
    entries in the scheduled task log.
    """
    from api.v1.endpoints.analysis import _run_market_review_background

    task_repo = ScheduledTaskLogRepository()
    scheduled_at = datetime.now()
    task_repo.save(
        task_name="market_review",
        scheduled_at=scheduled_at,
        status="running",
        started_at=scheduled_at,
    )
    try:
        _run_market_review_background(
            send_notification=True,
            override_region=None,
            lock_token=lock_token,
            config=config,
            query_id=query_id,
        )
        task_repo.save(
            task_name="market_review",
            scheduled_at=scheduled_at,
            status="success",
            started_at=scheduled_at,
            finished_at=datetime.now(),
        )
    except Exception as exc:
        logger.exception("Manual trigger failed for market_review: %s", exc)
        task_repo.save(
            task_name="market_review",
            scheduled_at=scheduled_at,
            status="failed",
            started_at=scheduled_at,
            detail={"error": str(exc)},
        )
