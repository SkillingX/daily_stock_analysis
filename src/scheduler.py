# -*- coding: utf-8 -*-
"""
===================================
定时调度模块
===================================

职责：
1. 支持每日定时执行股票分析
2. 支持定时执行大盘复盘
3. 优雅处理信号，确保可靠退出

依赖：
- schedule: 轻量级定时任务库
"""

import json
import logging
import os
import re
import signal
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

TASK_TIMEOUT_SECONDS = 7200  # 2 hours


class GracefulShutdown:
    """
    优雅退出处理器

    捕获 SIGTERM/SIGINT 信号，确保任务完成后再退出
    """

    def __init__(self) -> None:
        self.shutdown_requested: bool = False
        self._lock = threading.Lock()

        # 注册信号处理器
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum: Any, frame: Any) -> None:
        """信号处理函数"""
        with self._lock:
            if not self.shutdown_requested:
                logger.info(f"收到退出信号 ({signum})，等待当前任务完成...")
                self.shutdown_requested = True

    @property
    def should_shutdown(self) -> bool:
        """检查是否应该退出"""
        with self._lock:
            return self.shutdown_requested


class Scheduler:
    """
    定时任务调度器

    基于 schedule 库实现，支持：
    - 每日定时执行（单任务或多任务）
    - 启动时立即执行
    - 优雅退出
    """

    def __init__(
        self,
        schedule_time: str = "18:00",
        schedule_time_provider: Optional[Callable[[], str]] = None,
        heartbeat_path: Optional[Path] = None,
    ):
        """
        初始化调度器

        Args:
            schedule_time: 每日执行时间，格式 "HH:MM"（单任务模式兼容）
        """
        try:
            import schedule

            self.schedule = schedule
        except ImportError:
            logger.error("schedule 库未安装，请执行: pip install schedule")
            raise ImportError("请安装 schedule 库: pip install schedule")

        self.schedule_time = schedule_time
        self._schedule_time_provider = schedule_time_provider
        self._heartbeat_path = heartbeat_path
        self.shutdown_handler = GracefulShutdown()
        self._task_callback: Optional[Callable[..., Any]] = None
        self._daily_job: Optional[Any] = None
        self._daily_jobs: Dict[str, Any] = {}  # 多任务调度：name -> job
        self._daily_task_callbacks: Dict[
            str, Callable[..., Any]
        ] = {}  # 多任务调度：name -> callback
        self._background_tasks: List[Dict[str, Any]] = []
        self._running_named_tasks: set[str] = set()
        self._named_task_threads: Dict[str, threading.Thread] = {}
        self._named_task_start_time: Dict[str, float] = {}
        self._single_task_running: bool = False
        self._single_task_start_time: float = 0.0
        self._running = False

    def set_daily_task(
        self, task: Callable[..., Any], run_immediately: bool = True
    ) -> None:
        """
        设置每日定时任务

        Args:
            task: 要执行的任务函数（无参数）
            run_immediately: 是否在设置后立即执行一次
        """
        self._task_callback = task
        if not self._configure_daily_task(self.schedule_time):
            raise ValueError(f"无效的定时执行时间: {self.schedule_time!r}")

        if run_immediately:
            logger.info("立即执行一次任务...")
            self._safe_run_task()

    @staticmethod
    def _is_valid_schedule_time(schedule_time: str) -> bool:
        """Validate time string in HH:MM 24-hour format."""
        candidate = (schedule_time or "").strip()
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", candidate):
            return False
        return True

    def _cancel_daily_job(self) -> None:
        """Remove the currently registered daily job if one exists."""
        if self._daily_job is None:
            return

        if hasattr(self.schedule, "cancel_job"):
            self.schedule.cancel_job(self._daily_job)
        else:  # pragma: no cover - compatibility fallback
            jobs = getattr(self.schedule, "jobs", None)
            if isinstance(jobs, list) and self._daily_job in jobs:
                jobs.remove(self._daily_job)

        self._daily_job = None

    def _configure_daily_task(self, schedule_time: str) -> bool:
        """(Re)register the daily job at the requested time."""
        candidate = (schedule_time or "").strip()
        if not self._is_valid_schedule_time(candidate):
            logger.warning(
                "检测到无效的定时执行时间 %r，继续沿用当前时间 %s",
                schedule_time,
                self.schedule_time,
            )
            return False

        previous_time = self.schedule_time
        self._cancel_daily_job()
        self._daily_job = (
            self.schedule.every().day.at(candidate).do(self._safe_run_task)
        )
        self.schedule_time = candidate

        if previous_time == candidate:
            logger.info("已设置每日定时任务，执行时间: %s", self.schedule_time)
        else:
            logger.info(
                "检测到 SCHEDULE_TIME 变更，已将每日定时任务从 %s 更新为 %s",
                previous_time,
                self.schedule_time,
            )
        return True

    def _refresh_daily_schedule_if_needed(self) -> None:
        """Reload daily schedule time from the latest runtime config if needed."""
        if self._task_callback is None or self._schedule_time_provider is None:
            return

        try:
            latest_schedule_time = (self._schedule_time_provider() or "").strip()
        except Exception as exc:  # pragma: no cover - defensive branch
            logger.warning(
                "读取最新 SCHEDULE_TIME 失败，继续沿用 %s: %s", self.schedule_time, exc
            )
            return

        if not latest_schedule_time or latest_schedule_time == self.schedule_time:
            return

        if self._configure_daily_task(latest_schedule_time):
            logger.info("更新后的下次执行时间: %s", self._get_next_run_time())

    def _safe_run_task(self) -> None:
        """在后台线程中执行单任务模式，不阻塞调度器主循环。"""
        if self._task_callback is None:
            return

        if self._single_task_running:
            elapsed = time.time() - self._single_task_start_time
            if elapsed > TASK_TIMEOUT_SECONDS:
                logger.warning(
                    "单任务已超时（运行 %.1fs > %ds），清理守卫后重新执行",
                    elapsed,
                    TASK_TIMEOUT_SECONDS,
                )
                self._single_task_running = False
            else:
                logger.info("单任务已在执行中，跳过本次触发。")
                return

        self._single_task_running = True
        self._single_task_start_time = time.time()

        task_callback = self._task_callback
        if task_callback is None:
            self._single_task_running = False
            self._single_task_start_time = 0.0
            logger.warning("定时任务回调未注册，跳过本次执行")
            return

        def _wrapped() -> None:
            try:
                logger.info("=" * 50)
                logger.info(
                    f"定时任务开始执行 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                logger.info("=" * 50)

                task_callback()

                logger.info(
                    f"定时任务执行完成 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
            except Exception as e:
                logger.exception(f"定时任务执行失败: {e}")
            finally:
                self._single_task_running = False
                self._single_task_start_time = 0.0

        thread = threading.Thread(
            target=_wrapped,
            daemon=True,
            name="scheduler-single-task",
        )
        thread.start()

    def add_daily_task(
        self,
        name: str,
        task: Callable[..., Any],
        schedule_time: str,
        run_immediately: bool = False,
    ) -> bool:
        """
        添加一个每日定时任务（多任务调度模式）

        Args:
            name: 任务名称（唯一标识）
            task: 要执行的任务函数（无参数）
            schedule_time: 执行时间，格式 "HH:MM"
            run_immediately: 是否在设置后立即执行一次

        Returns:
            是否成功添加
        """
        if not self._is_valid_schedule_time(schedule_time):
            logger.warning("无效的定时执行时间: %r", schedule_time)
            return False

        # 取消同名任务（如果存在）
        if name in self._daily_jobs:
            self._cancel_named_daily_job(name)

        self._daily_task_callbacks[name] = task
        job = (
            self.schedule.every()
            .day.at(schedule_time)
            .do(self._safe_run_named_task, name)
        )
        self._daily_jobs[name] = job
        logger.info("已添加每日定时任务 [%s]，执行时间: %s", name, schedule_time)

        if run_immediately:
            logger.info("立即执行一次任务 [%s]...", name)
            self._safe_run_named_task(name)

        return True

    def _cancel_named_daily_job(self, name: str) -> None:
        """取消指定名称的每日任务"""
        job = self._daily_jobs.pop(name, None)
        if job is None:
            return

        if hasattr(self.schedule, "cancel_job"):
            self.schedule.cancel_job(job)
        else:  # pragma: no cover - compatibility fallback
            jobs = getattr(self.schedule, "jobs", None)
            if isinstance(jobs, list) and job in jobs:
                jobs.remove(job)

        self._daily_task_callbacks.pop(name, None)
        logger.info("已取消每日定时任务 [%s]", name)

    def _safe_run_named_task(self, name: str) -> None:
        """在后台线程中执行指定名称的任务，不阻塞调度器主循环。"""
        callback = self._daily_task_callbacks.get(name)
        if callback is None:
            return

        if name in self._running_named_tasks:
            elapsed = time.time() - self._named_task_start_time.get(name, 0)
            if elapsed > TASK_TIMEOUT_SECONDS:
                logger.warning(
                    "定时任务 [%s] 已超时（运行 %.1fs > %ds），清理守卫后重新执行",
                    name,
                    elapsed,
                    TASK_TIMEOUT_SECONDS,
                )
                self._running_named_tasks.discard(name)
                self._named_task_threads.pop(name, None)
                self._named_task_start_time.pop(name, None)
            else:
                logger.info("定时任务 [%s] 已在执行中，跳过本次触发。", name)
                return

        self._running_named_tasks.add(name)
        self._named_task_start_time[name] = time.time()

        def _wrapped() -> None:
            try:
                logger.info("=" * 50)
                logger.info(
                    "定时任务 [%s] 开始执行 - %s",
                    name,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                )
                logger.info("=" * 50)

                callback()

                logger.info(
                    "定时任务 [%s] 执行完成 - %s",
                    name,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                )
            except Exception as e:
                logger.exception("定时任务 [%s] 执行失败: %s", name, e)
            finally:
                self._running_named_tasks.discard(name)
                self._named_task_threads.pop(name, None)
                self._named_task_start_time.pop(name, None)

        thread = threading.Thread(
            target=_wrapped,
            daemon=True,
            name=f"scheduler-task-{name}",
        )
        self._named_task_threads[name] = thread
        thread.start()

    def add_background_task(
        self,
        task: Callable[..., Any],
        interval_seconds: int,
        run_immediately: bool = False,
        name: Optional[str] = None,
    ) -> None:
        """Register a periodic background task executed inside the scheduler loop.

        Note: The scheduler loop polls every 30 seconds, so *interval_seconds*
        below 30 will be clamped to 30 to avoid promising unreachable precision.
        """
        clamped_interval = max(30, int(interval_seconds))
        if int(interval_seconds) < 30:
            logger.warning(
                "后台任务 %s 请求间隔 %ds，但调度循环每 30s 轮询一次，已自动调整为 30s",
                name or getattr(task, "__name__", "background_task"),
                interval_seconds,
            )
        entry = {
            "task": task,
            "interval_seconds": clamped_interval,
            "last_run": 0.0,
            "name": name or getattr(task, "__name__", "background_task"),
            "thread": None,
            "running": False,
        }
        if not run_immediately:
            entry["last_run"] = time.time()
        self._background_tasks.append(entry)
        logger.info(
            "已注册后台任务: %s（间隔 %s 秒，立即执行=%s）",
            entry["name"],
            entry["interval_seconds"],
            run_immediately,
        )
        if run_immediately:
            self._start_background_task(entry)

    def _start_background_task(self, entry: Dict[str, Any]) -> bool:
        """Start one background task in a dedicated daemon thread."""
        worker = entry.get("thread")
        if worker is not None and worker.is_alive():
            return False

        def _runner() -> None:
            try:
                logger.info("后台任务开始执行: %s", entry["name"])
                entry["task"]()
            except Exception as exc:
                logger.exception("后台任务执行失败 [%s]: %s", entry["name"], exc)
            finally:
                entry["running"] = False
                entry["thread"] = None

        entry["last_run"] = time.time()
        entry["running"] = True
        worker = threading.Thread(
            target=_runner,
            daemon=True,
            name=f"scheduler-bg-{entry['name']}",
        )
        entry["thread"] = worker
        worker.start()
        return True

    def _run_background_tasks(self) -> None:
        """Execute any background tasks whose interval has elapsed."""
        if not self._background_tasks:
            return

        now = time.time()
        for entry in self._background_tasks:
            worker = entry.get("thread")
            if worker is not None and worker.is_alive():
                continue
            if entry.get("running"):
                entry["running"] = False
                entry["thread"] = None
            if now - entry["last_run"] < entry["interval_seconds"]:
                continue
            self._start_background_task(entry)

    def run(self) -> None:
        """
        运行调度器主循环

        阻塞运行，直到收到退出信号
        """
        self._running = True
        logger.info("调度器开始运行...")
        logger.info(f"下次执行时间: {self._get_next_run_time()}")

        while self._running and not self.shutdown_handler.should_shutdown:
            self._refresh_daily_schedule_if_needed()
            self.schedule.run_pending()
            self._run_background_tasks()
            self._write_heartbeat()
            time.sleep(30)  # 每30秒检查一次

            # 每小时打印一次心跳
            if datetime.now().minute == 0 and datetime.now().second < 30:
                logger.info(f"调度器运行中... 下次执行: {self._get_next_run_time()}")

        logger.info("调度器已停止")

    def _write_heartbeat(self) -> None:
        """Write a heartbeat file so external monitors can detect liveness."""
        if not self._heartbeat_path:
            return
        try:
            self._heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            jobs_info = {}
            for name, cb in self._daily_task_callbacks.items():
                jobs_info[name] = getattr(cb, "__name__", name)
            content = json.dumps(
                {
                    "timestamp": datetime.now().isoformat(),
                    "pid": os.getpid(),
                    "registered_tasks": jobs_info,
                    "next_run": self._get_next_run_time(),
                },
                ensure_ascii=False,
            )
            self._heartbeat_path.write_text(content, encoding="utf-8")
        except Exception as exc:
            logger.debug("写入 heartbeat 失败: %s", exc)

    def _get_next_run_time(self) -> str:
        """获取下次执行时间"""
        jobs = self.schedule.get_jobs()
        if jobs:
            # schedule lib has no type stubs; next_run is Any. Coerce to
            # datetime defensively before strftime to avoid AttributeError
            # on schedule version bumps.
            from datetime import datetime as _dt
            candidate = min(j.next_run for j in jobs if j.next_run is not None)
            next_run = _dt.fromtimestamp(float(candidate)) if not isinstance(candidate, _dt) else candidate
            return next_run.strftime("%Y-%m-%d %H:%M:%S")
        return "未设置"

    def stop(self) -> None:
        """停止调度器"""
        self._running = False


def run_with_schedule(
    task: Optional[Callable[..., Any]] = None,
    schedule_time: str = "18:00",
    run_immediately: bool = True,
    background_tasks: Optional[List[Dict[str, Any]]] = None,
    schedule_time_provider: Optional[Callable[[], str]] = None,
    daily_tasks: Optional[List[Dict[str, Any]]] = None,
    heartbeat_path: Optional[Path] = None,
) -> None:
    """
    便捷函数：使用定时调度运行任务

    Args:
        task: 要执行的任务函数（单任务模式，向后兼容）
        schedule_time: 每日执行时间（单任务模式）
        run_immediately: 是否立即执行一次（单任务模式）
        background_tasks: 可选的后台任务定义列表。每项为一个字典，
            需包含 `task` 与 `interval_seconds`，可选包含 `name`
            和 `run_immediately`。`interval_seconds` 单位为秒。
        schedule_time_provider: 可选的时间提供器；调度器每轮检查前会读取，
            当返回值变化时自动重建 daily job。
        daily_tasks: 多任务调度列表。每项为一个字典，需包含：
            - name: 任务名称（唯一标识）
            - task: 要执行的任务函数
            - schedule_time: 执行时间（HH:MM 格式）
            - run_immediately: 是否立即执行一次（可选，默认 False）
        heartbeat_path: 可选的 heartbeat 文件路径；调度器每轮写入时间戳。
    """
    scheduler = Scheduler(
        schedule_time=schedule_time,
        schedule_time_provider=schedule_time_provider,
        heartbeat_path=heartbeat_path,
    )
    for entry in background_tasks or []:
        scheduler.add_background_task(
            task=entry["task"],
            interval_seconds=entry["interval_seconds"],
            run_immediately=entry.get("run_immediately", False),
            name=entry.get("name"),
        )

    # 多任务调度模式
    if daily_tasks:
        for entry in daily_tasks:
            scheduler.add_daily_task(
                name=entry["name"],
                task=entry["task"],
                schedule_time=entry["schedule_time"],
                run_immediately=entry.get("run_immediately", False),
            )
    # 单任务模式（向后兼容）
    elif task is not None:
        scheduler.set_daily_task(task, run_immediately=run_immediately)

    scheduler.run()


if __name__ == "__main__":
    # 测试定时调度
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s",
    )

    def test_task() -> None:
        print(f"任务执行中... {datetime.now()}")
        time.sleep(2)
        print("任务完成!")

    print("启动测试调度器（按 Ctrl+C 退出）")
    run_with_schedule(test_task, schedule_time="23:59", run_immediately=True)
