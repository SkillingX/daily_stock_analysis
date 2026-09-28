# -*- coding: utf-8 -*-
"""Data access layer for scheduled_task_log table."""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.storage import DatabaseManager, ScheduledTaskLog

logger = logging.getLogger(__name__)


class ScheduledTaskLogRepository:
    """CRUD for scheduled task execution logs."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()

    def save(
        self,
        task_name: str,
        scheduled_at: datetime,
        status: str,
        started_at: Optional[datetime] = None,
        finished_at: Optional[datetime] = None,
        detail: Optional[Dict[str, Any]] = None,
        report_path: Optional[str] = None,
    ) -> int:
        """Insert a new log entry and return its id."""
        session = self.db.get_session()
        try:
            entry = ScheduledTaskLog(
                task_name=task_name,
                scheduled_at=scheduled_at,
                started_at=started_at,
                finished_at=finished_at,
                status=status,
                detail=json.dumps(detail, ensure_ascii=False) if detail else None,
                report_path=report_path,
            )
            session.add(entry)
            session.commit()
            entry_id = entry.id
            logger.info(
                "scheduled_task_log saved: task=%s status=%s id=%s",
                task_name,
                status,
                entry_id,
            )
            return entry_id
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_recent(
        self,
        task_name: Optional[str] = None,
        limit: int = 10,
    ) -> List[ScheduledTaskLog]:
        """Return most recent log entries, newest first."""
        session = self.db.get_session()
        try:
            query = session.query(ScheduledTaskLog).order_by(
                ScheduledTaskLog.scheduled_at.desc()
            )
            if task_name:
                query = query.filter(ScheduledTaskLog.task_name == task_name)
            return query.limit(limit).all()
        finally:
            session.close()

    def get_latest_by_task(
        self, task_name: str
    ) -> Optional[ScheduledTaskLog]:
        """Return the most recent log entry for a given task."""
        session = self.db.get_session()
        try:
            return (
                session.query(ScheduledTaskLog)
                .filter(ScheduledTaskLog.task_name == task_name)
                .order_by(ScheduledTaskLog.scheduled_at.desc())
                .first()
            )
        finally:
            session.close()
