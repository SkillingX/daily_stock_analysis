# -*- coding: utf-8 -*-
"""Generic task-level execution lock for scheduled tasks.

Provides mutual exclusion for any named task (e.g. watchlist_analysis).
Follows the same file-lock pattern as market_review_lock.py but is
generic enough to cover multiple task types.
"""

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, cast

from src.config import Config

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_DEFAULT_LOCK_TIMEOUT_SECONDS = 7200  # 2 hours

# Per-task in-process guards
_task_locks: dict[str, threading.Lock] = {}
_task_running: dict[str, bool] = {}
_task_running_start: dict[str, float] = {}


def _get_task_lock(task_name: str) -> threading.Lock:
    if task_name not in _task_locks:
        _task_locks[task_name] = threading.Lock()
    return _task_locks[task_name]


def _get_task_running(task_name: str) -> bool:
    return _task_running.get(task_name, False)


def _set_task_running(task_name: str, value: bool) -> None:
    _task_running[task_name] = value
    if value:
        _task_running_start[task_name] = time.time()
    else:
        _task_running_start.pop(task_name, None)


@dataclass
class TaskLockToken:
    handle: Any
    path: Path
    uses_flock: bool
    task_name: str


def task_lock_path(config: Config, task_name: str) -> Path:
    database_path = getattr(config, "database_path", "./data/stock_analysis.db")
    return Path(database_path).parent / "locks" / f"{task_name}.lock"


def _write_lock_metadata(handle: Any, task_name: str) -> None:
    handle.seek(0)
    handle.truncate()
    handle.write(
        json.dumps(
            {
                "pid": os.getpid(),
                "task_name": task_name,
                "started_at": datetime.now().isoformat(),
            }
        )
    )
    handle.flush()


def _read_lock_metadata(lock_path: Path) -> dict[str, Any]:
    try:
        return cast(Dict[str, Any], json.loads(lock_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return {}


def _is_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _is_lock_expired(lock_path: Path, timeout_seconds: int) -> bool:
    try:
        modified_at = datetime.fromtimestamp(lock_path.stat().st_mtime)
    except OSError:
        return False
    return datetime.now() - modified_at > timedelta(seconds=timeout_seconds)


def _is_stale_lock(lock_path: Path, timeout_seconds: int) -> bool:
    """仅当 owner 进程已死（或元数据不可读且文件超龄）时锁才算过期。

    issue #66：存活 owner 的锁不因运行时长被判过期——超时不等于所有权丢失，
    清除活 owner 的锁路径会让同一任务在另一个锁 inode 上重复执行。
    """
    metadata = _read_lock_metadata(lock_path)
    pid_raw = metadata.get("pid")
    if not pid_raw:
        return _is_lock_expired(lock_path, timeout_seconds)

    try:
        pid = int(pid_raw)
    except ValueError:
        return _is_lock_expired(lock_path, timeout_seconds)

    # 存活 owner 一律视为持锁（无论运行多久）；已死 owner 的锁立即过期。
    return not _is_process_alive(pid)


def acquire_task_lock(
    config: Config,
    task_name: str,
    timeout_seconds: int = _DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> Optional[TaskLockToken]:
    """Acquire a process-local + cross-process file lock for a named task.

    Returns None if the lock is already held by another running task.
    """
    lock_guard = _get_task_lock(task_name)

    with lock_guard:
        if _get_task_running(task_name):
            # issue #66：运行标记只在真实持锁期间为 True（acquire/release 配对），
            # 超龄也不强制清理——清标记会让同一任务在本进程内重复执行。
            running_since = _task_running_start.get(task_name, 0)
            logger.info(
                "进程内任务 [%s] 已在执行中（%.1fs），跳过本次触发。",
                task_name,
                time.time() - running_since if running_since else 0.0,
            )
            return None

        lock_path = task_lock_path(config, task_name)
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        if fcntl is not None:
            for attempt in range(2):
                handle = open(lock_path, "a+", encoding="utf-8")
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (BlockingIOError, OSError):
                    handle.close()
                    if _is_stale_lock(lock_path, timeout_seconds):
                        logger.info(
                            "检测到过期的 %s.lock，尝试清理后重试。",
                            task_name,
                        )
                        try:
                            lock_path.unlink()
                            continue
                        except OSError:
                            pass
                    return None
                break
            else:
                return None
            uses_flock = True
        else:  # pragma: no cover - Windows without fcntl
            fd: Optional[int] = None
            for _ in range(2):
                try:
                    fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                    break
                except FileExistsError:
                    if not _is_stale_lock(lock_path, timeout_seconds):
                        return None
                    logger.warning("检测到过期的 %s.lock，尝试清理后重试。", task_name)
                    try:
                        lock_path.unlink()
                    except OSError as exc:
                        logger.warning("清理过期 %s.lock 失败: %s", task_name, exc)
                        return None

            if fd is None:
                return None

            handle = os.fdopen(fd, "w+", encoding="utf-8")
            uses_flock = False

        _write_lock_metadata(handle, task_name)
        _set_task_running(task_name, True)
        return TaskLockToken(
            handle=handle, path=lock_path, uses_flock=uses_flock, task_name=task_name
        )


def release_task_lock(token: Optional[TaskLockToken]) -> None:
    """Release a previously acquired task lock."""
    if token is None:
        return

    lock_guard = _get_task_lock(token.task_name)
    with lock_guard:
        _set_task_running(token.task_name, False)

    try:
        if token.uses_flock and fcntl is not None:
            fcntl.flock(token.handle.fileno(), fcntl.LOCK_UN)
    finally:
        token.handle.close()
        if not token.uses_flock:
            try:
                token.path.unlink()
            except FileNotFoundError:
                pass


def is_task_locked(config: Config, task_name: str) -> bool:
    """Check if a task lock file exists and is not stale."""
    lock_path = task_lock_path(config, task_name)
    if not lock_path.exists():
        return False
    metadata = _read_lock_metadata(lock_path)
    pid_raw = metadata.get("pid")
    if pid_raw:
        try:
            pid = int(pid_raw)
            if not _is_process_alive(pid):
                return False
        except ValueError:
            return False
    return True


def cleanup_stale_locks(
    config: Config,
    timeout_seconds: int = _DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> list[str]:
    """Remove stale lock files. Returns list of cleaned task names."""
    locks_dir = (
        Path(getattr(config, "database_path", "./data/stock_analysis.db")).parent
        / "locks"
    )
    if not locks_dir.is_dir():
        return []

    cleaned = []
    for lock_file in locks_dir.glob("*.lock"):
        task_name = lock_file.stem
        if _is_stale_lock(lock_file, timeout_seconds):
            try:
                lock_file.unlink()
                cleaned.append(task_name)
                logger.info("清理过期锁: %s", task_name)
            except OSError as exc:
                logger.warning("清理过期锁 %s 失败: %s", task_name, exc)
    return cleaned
