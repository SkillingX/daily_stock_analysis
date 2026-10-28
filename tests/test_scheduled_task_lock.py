# -*- coding: utf-8 -*-
"""Tests for src/core/scheduled_task_lock.py."""

import fcntl as fcntl_module
import json
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def tmp_db_dir(tmp_path):
    """Create a temporary database directory with a dummy db file."""
    db_file = tmp_path / "stock_analysis.db"
    db_file.touch()
    return tmp_path


@pytest.fixture
def mock_config(tmp_db_dir):
    """Return a Config-like object with database_path pointing to tmp."""
    config = MagicMock()
    config.database_path = str(tmp_db_dir / "stock_analysis.db")
    return config


class TestTaskLockAcquireRelease:
    def test_acquire_release_cycle(self, mock_config):
        from src.core.scheduled_task_lock import (
            acquire_task_lock,
            release_task_lock,
        )

        token = acquire_task_lock(mock_config, "test_task")
        assert token is not None
        assert token.task_name == "test_task"
        assert token.path.exists()

        release_task_lock(token)
        # After release, lock file should be removed (flock mode)
        # or at least not block another acquire
        token2 = acquire_task_lock(mock_config, "test_task")
        assert token2 is not None
        release_task_lock(token2)

    def test_concurrent_acquire_blocks(self, mock_config):
        from src.core.scheduled_task_lock import (
            acquire_task_lock,
            release_task_lock,
        )

        token1 = acquire_task_lock(mock_config, "block_test")
        assert token1 is not None

        # Second acquire should fail
        token2 = acquire_task_lock(mock_config, "block_test")
        assert token2 is None

        release_task_lock(token1)

        # After release, should succeed
        token3 = acquire_task_lock(mock_config, "block_test")
        assert token3 is not None
        release_task_lock(token3)

    def test_release_none_is_noop(self):
        from src.core.scheduled_task_lock import release_task_lock

        release_task_lock(None)  # Should not raise

    def test_lock_metadata_content(self, mock_config):
        from src.core.scheduled_task_lock import (
            acquire_task_lock,
            release_task_lock,
        )

        token = acquire_task_lock(mock_config, "meta_test")
        assert token is not None

        raw = token.path.read_text(encoding="utf-8")
        metadata = json.loads(raw)
        assert metadata["pid"] == os.getpid()
        assert metadata["task_name"] == "meta_test"
        assert "started_at" in metadata

        release_task_lock(token)


class TestIsTaskLocked:
    def test_not_locked_when_no_file(self, mock_config):
        from src.core.scheduled_task_lock import is_task_locked

        assert is_task_locked(mock_config, "nonexistent") is False

    def test_locked_when_file_exists_and_pid_alive(self, mock_config):
        from src.core.scheduled_task_lock import (
            acquire_task_lock,
            is_task_locked,
            release_task_lock,
        )

        token = acquire_task_lock(mock_config, "alive_test")
        assert is_task_locked(mock_config, "alive_test") is True
        release_task_lock(token)


class TestCleanupStaleLocks:
    def test_cleanup_stale(self, mock_config):
        from src.core.scheduled_task_lock import (
            cleanup_stale_locks,
            task_lock_path,
        )

        lock_path = task_lock_path(mock_config, "stale_task")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # Write a lock file with old timestamp
        lock_path.write_text(
            json.dumps({"pid": 999999, "task_name": "stale_task", "started_at": "2020-01-01T00:00:00"}),
            encoding="utf-8",
        )
        # Make it old
        old_time = time.time() - 100000
        os.utime(str(lock_path), (old_time, old_time))

        cleaned = cleanup_stale_locks(mock_config, timeout_seconds=3600)
        assert "stale_task" in cleaned
        assert not lock_path.exists()


class TestStaleLockRetry:
    """Test fix: acquire_task_lock retries after cleaning stale locks."""

    def test_retry_after_stale_lock_cleanup(self, mock_config):
        """When a stale lock file blocks acquisition, it should be cleaned and retried."""
        from src.core.scheduled_task_lock import (
            acquire_task_lock,
            release_task_lock,
            _is_stale_lock,
            task_lock_path,
        )

        lock_path = task_lock_path(mock_config, "stale_retry_test")
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        # Write stale metadata (dead PID)
        lock_path.write_text(
            json.dumps({
                "pid": 999999,
                "task_name": "stale_retry_test",
                "started_at": "2020-01-01T00:00:00",
            }),
            encoding="utf-8",
        )

        # Make the file "old" so it's clearly stale
        old_time = time.time() - 100000
        os.utime(str(lock_path), (old_time, old_time))

        # Hold a real flock from another thread (simulating a stale lock holder)
        held = threading.Event()
        ready = threading.Event()

        def hold_flock():
            fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT)
            fcntl_module.flock(fd, fcntl_module.LOCK_EX)
            ready.set()
            held.wait(timeout=10)
            fcntl_module.flock(fd, fcntl_module.LOCK_UN)
            os.close(fd)

        holder = threading.Thread(target=hold_flock, daemon=True)
        holder.start()
        ready.wait(timeout=5)

        # Now the lock file has stale metadata but a real held flock.
        # acquire_task_lock should detect staleness, clean up, and retry.
        token = acquire_task_lock(mock_config, "stale_retry_test", timeout_seconds=3600)
        held.set()
        holder.join(timeout=5)

        assert token is not None, "acquire_task_lock should succeed after stale cleanup + retry"
        assert token.task_name == "stale_retry_test"
        release_task_lock(token)

    def test_valid_lock_still_blocks(self, mock_config):
        """When a non-stale lock exists, acquire should still return None."""
        from src.core.scheduled_task_lock import (
            acquire_task_lock,
            release_task_lock,
        )

        # Acquire a valid lock
        token1 = acquire_task_lock(mock_config, "valid_block_test")
        assert token1 is not None

        # Second attempt should fail (not stale)
        token2 = acquire_task_lock(mock_config, "valid_block_test")
        assert token2 is None

        release_task_lock(token1)

    def test_inprocess_lock_recovers_after_timeout(self, mock_config):
        """When the in-process guard has exceeded timeout, acquire_task_lock should
        clean it up and acquire successfully."""
        from src.core.scheduled_task_lock import (
            acquire_task_lock,
            release_task_lock,
            _set_task_running,
            _task_running_start,
        )

        # _set_task_running records "now", so the guard is not actually old;
        # backdate the start to make the claimed expired precondition real.
        # _set_task_running 记录的是当前时刻，显式回拨才真正建立"旧开始时间"。
        _set_task_running("inproc_timeout_test", True)
        _task_running_start["inproc_timeout_test"] = time.time() - 3600.0

        # acquire_task_lock with 0-second timeout should detect and recover
        token = acquire_task_lock(mock_config, "inproc_timeout_test", timeout_seconds=0)
        assert token is not None, "should recover from timed-out in-process guard"
        release_task_lock(token)

    def test_inprocess_lock_same_tick_start_still_blocks(self, mock_config, monkeypatch):
        """A same-tick start (elapsed == 0) must not count as expired at
        timeout_seconds=0: recovery requires elapsed > timeout. 本控制用模块
        级受控常量时钟固定 elapsed==0 边界，相邻真实时钟调用会竞速。"""
        import src.core.scheduled_task_lock as lock_module
        from src.core.scheduled_task_lock import (
            acquire_task_lock,
            _set_task_running,
        )

        class _FrozenClock:
            @staticmethod
            def time() -> float:
                return 1000.0

        monkeypatch.setattr(lock_module, "time", _FrozenClock)
        _set_task_running("inproc_zero_timeout_test", True)  # start = 1000.0
        token = acquire_task_lock(
            mock_config, "inproc_zero_timeout_test", timeout_seconds=0
        )
        assert token is None, "a same-tick start (elapsed == 0) must not count as expired"
        # acquire_task_lock returned None, so the guard stays running.
        _set_task_running("inproc_zero_timeout_test", False)


class TestDifferentTaskNames:
    def test_different_tasks_independent(self, mock_config):
        from src.core.scheduled_task_lock import (
            acquire_task_lock,
            release_task_lock,
        )

        t1 = acquire_task_lock(mock_config, "task_a")
        t2 = acquire_task_lock(mock_config, "task_b")
        assert t1 is not None
        assert t2 is not None
        release_task_lock(t1)
        release_task_lock(t2)
