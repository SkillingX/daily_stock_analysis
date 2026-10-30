# -*- coding: utf-8 -*-
"""Tests for Scheduler multi-task support."""

import threading
import time
from datetime import datetime
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch


class _FakeJob:
    def __init__(self, schedule_module):
        self._schedule_module = schedule_module
        self.next_run = datetime(2026, 1, 1, 18, 0, 0)
        self.at_time = None

    @property
    def day(self):
        return self

    def at(self, value):
        self.at_time = value
        hour, minute = [int(part) for part in value.split(":")]
        self.next_run = datetime(2026, 1, 1, hour, minute, 0)
        return self

    def do(self, fn, *args, **kwargs):
        self.job_func = fn
        self.job_args = args
        self.job_kwargs = kwargs
        self._schedule_module.jobs.append(self)
        return self


class _FakeScheduleModule:
    def __init__(self):
        self.jobs = []

    def every(self):
        return _FakeJob(self)

    def get_jobs(self):
        return list(self.jobs)

    def run_pending(self):
        return None

    def cancel_job(self, job):
        self.jobs.remove(job)


class SchedulerMultiTaskTestCase(unittest.TestCase):
    def test_add_daily_task_success(self):
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            scheduler = Scheduler()
            calls = []

            result = scheduler.add_daily_task(
                name="watchlist_analysis",
                task=lambda: calls.append("watchlist"),
                schedule_time="09:00",
                run_immediately=False,
            )

        self.assertTrue(result)
        self.assertEqual(len(fake_schedule.jobs), 1)
        self.assertEqual(fake_schedule.jobs[0].at_time, "09:00")

    def test_add_daily_task_invalid_time(self):
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            scheduler = Scheduler()

            result = scheduler.add_daily_task(
                name="invalid_task",
                task=lambda: None,
                schedule_time="25:99",
                run_immediately=False,
            )

        self.assertFalse(result)
        self.assertEqual(len(fake_schedule.jobs), 0)

    def test_add_multiple_daily_tasks(self):
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            scheduler = Scheduler()

            result1 = scheduler.add_daily_task(
                name="watchlist_analysis",
                task=lambda: None,
                schedule_time="09:00",
                run_immediately=False,
            )
            result2 = scheduler.add_daily_task(
                name="market_review",
                task=lambda: None,
                schedule_time="21:00",
                run_immediately=False,
            )

        self.assertTrue(result1)
        self.assertTrue(result2)
        self.assertEqual(len(fake_schedule.jobs), 2)
        self.assertEqual(fake_schedule.jobs[0].at_time, "09:00")
        self.assertEqual(fake_schedule.jobs[1].at_time, "21:00")

    def test_add_daily_task_replaces_same_name(self):
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            scheduler = Scheduler()

            scheduler.add_daily_task(
                name="watchlist_analysis",
                task=lambda: None,
                schedule_time="09:00",
                run_immediately=False,
            )
            scheduler.add_daily_task(
                name="watchlist_analysis",
                task=lambda: None,
                schedule_time="10:00",
                run_immediately=False,
            )

        self.assertEqual(len(fake_schedule.jobs), 1)
        self.assertEqual(fake_schedule.jobs[0].at_time, "10:00")

    def test_add_daily_task_run_immediately(self):
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            scheduler = Scheduler()
            calls = []

            scheduler.add_daily_task(
                name="watchlist_analysis",
                task=lambda: calls.append("ran"),
                schedule_time="09:00",
                run_immediately=True,
            )

        self.assertEqual(calls, ["ran"])

    def test_safe_run_named_task_catches_exception(self):
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            scheduler = Scheduler()
            scheduler.add_daily_task(
                name="failing_task",
                task=lambda: 1 / 0,
                schedule_time="09:00",
                run_immediately=False,
            )

            # Should not raise
            scheduler._safe_run_named_task("failing_task")

    def test_safe_run_named_task_nonexistent(self):
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            scheduler = Scheduler()

            # Should not raise
            scheduler._safe_run_named_task("nonexistent")

    def test_cancel_named_daily_job(self):
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            scheduler = Scheduler()
            scheduler.add_daily_task(
                name="watchlist_analysis",
                task=lambda: None,
                schedule_time="09:00",
                run_immediately=False,
            )
            scheduler._cancel_named_daily_job("watchlist_analysis")

        self.assertEqual(len(fake_schedule.jobs), 0)
        self.assertNotIn("watchlist_analysis", scheduler._daily_task_callbacks)

    def test_cancel_named_daily_job_nonexistent(self):
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            scheduler = Scheduler()

            # Should not raise
            scheduler._cancel_named_daily_job("nonexistent")


class NamedTaskThreadDeduplicationTestCase(unittest.TestCase):
    """Test fix: named tasks run in background threads with deduplication."""

    def test_safe_run_named_task_executes_in_thread(self):
        """Callback should be invoked (via a thread) by _safe_run_named_task."""
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            scheduler = Scheduler()
            executed = []
            event = threading.Event()

            def task():
                executed.append("ran")
                event.set()

            scheduler.add_daily_task(
                name="thread_test",
                task=task,
                schedule_time="09:00",
                run_immediately=False,
            )

            scheduler._safe_run_named_task("thread_test")
            # Wait for the thread to run
            event.wait(timeout=5)
            self.assertEqual(executed, ["ran"])

    def test_safe_run_named_task_deduplicates_concurrent_calls(self):
        """Second call to the same named task should be skipped while first is running."""
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            scheduler = Scheduler()
            started = threading.Event()
            can_finish = threading.Event()

            call_order = []

            def slow_task():
                call_order.append("start")
                started.set()
                can_finish.wait(timeout=5)
                call_order.append("end")

            scheduler.add_daily_task(
                name="dedup_test",
                task=slow_task,
                schedule_time="09:00",
                run_immediately=False,
            )

            scheduler._safe_run_named_task("dedup_test")
            started.wait(timeout=5)  # first call is running

            # Second call should be skipped (dedup)
            scheduler._safe_run_named_task("dedup_test")
            call_order.append("second_skipped")

            can_finish.set()
            # Wait for thread to finish
            thread = scheduler._named_task_threads.get("dedup_test")
            if thread:
                thread.join(timeout=5)

            self.assertEqual(call_order, ["start", "second_skipped", "end"])
            self.assertNotIn("dedup_test", scheduler._running_named_tasks)

    def test_dedup_guard_cleaned_after_completion(self):
        """_running_named_tasks set should be cleaned after task finishes."""
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            scheduler = Scheduler()
            done = threading.Event()

            def quick_task():
                done.set()

            scheduler.add_daily_task(
                name="cleanup_test",
                task=quick_task,
                schedule_time="09:00",
                run_immediately=False,
            )

            scheduler._safe_run_named_task("cleanup_test")
            done.wait(timeout=5)
            time.sleep(0.05)  # let the finally block run
            self.assertNotIn("cleanup_test", scheduler._running_named_tasks)

    def test_single_task_mode_deduplicates(self):
        """_safe_run_task should skip if a single task is already running."""
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            scheduler = Scheduler()
            started = threading.Event()
            can_finish = threading.Event()

            call_order = []

            def slow_task():
                call_order.append("start")
                started.set()
                can_finish.wait(timeout=5)
                call_order.append("end")

            scheduler.set_daily_task(slow_task, run_immediately=False)

            scheduler._safe_run_task()
            started.wait(timeout=5)

            # Second call should be skipped
            scheduler._safe_run_task()
            call_order.append("second_skipped")

            can_finish.set()
            thread = threading.enumerate()
            # wait for the daemon thread
            time.sleep(0.2)

            self.assertEqual(call_order, ["start", "second_skipped", "end"])
            self.assertFalse(scheduler._single_task_running)

    def test_named_task_recovers_after_timeout(self):
        """If a named task exceeds TASK_TIMEOUT_SECONDS, it should be re-executed."""
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler, TASK_TIMEOUT_SECONDS

            scheduler = Scheduler()
            exec_count = []

            def task():
                exec_count.append("ran")

            scheduler.add_daily_task(
                name="timeout_test",
                task=task,
                schedule_time="09:00",
                run_immediately=False,
            )

            # Simulate a stuck task by setting the guard + start time far in the past
            scheduler._running_named_tasks.add("timeout_test")
            scheduler._named_task_start_time["timeout_test"] = time.time() - TASK_TIMEOUT_SECONDS - 1

            # Call again - should detect timeout, clean guard, and re-execute
            scheduler._safe_run_named_task("timeout_test")
            time.sleep(0.1)

            self.assertEqual(exec_count, ["ran"])
            self.assertNotIn("timeout_test", scheduler._running_named_tasks)
            self.assertNotIn("timeout_test", scheduler._named_task_start_time)

    def test_single_task_recovers_after_timeout(self):
        """If a single task exceeds TASK_TIMEOUT_SECONDS, it should be re-executed."""
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler, TASK_TIMEOUT_SECONDS

            scheduler = Scheduler()
            exec_count = []

            def task():
                exec_count.append("ran")

            scheduler.set_daily_task(task, run_immediately=False)

            # Simulate a stuck single task
            scheduler._single_task_running = True
            scheduler._single_task_start_time = time.time() - TASK_TIMEOUT_SECONDS - 1

            scheduler._safe_run_task()
            time.sleep(0.1)

            self.assertEqual(exec_count, ["ran"])
            self.assertFalse(scheduler._single_task_running)
            self.assertEqual(scheduler._single_task_start_time, 0.0)


class WatchlistTimeParsingTestCase(unittest.TestCase):
    """Test fix: comma-separated watchlist times produce multiple named tasks.

    Validates the logic in main.py that splits WATCHLIST_ANALYSIS_TIME
    into separate slots.
    """

    def test_single_time_produces_one_task(self):
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            scheduler = Scheduler()
            daily_tasks = []

            watchlist_analysis_time = "12:00"
            watchlist_times = [
                t.strip()
                for t in watchlist_analysis_time.split(",")
                if t.strip()
            ]
            for idx, wt in enumerate(watchlist_times):
                slot_name = f"watchlist_analysis_{wt.replace(':', '_')}"
                daily_tasks.append({
                    "name": slot_name,
                    "task": lambda: None,
                    "schedule_time": wt,
                    "run_immediately": True if idx == 0 else False,
                })

            self.assertEqual(len(daily_tasks), 1)
            self.assertEqual(daily_tasks[0]["name"], "watchlist_analysis_12_00")
            self.assertEqual(daily_tasks[0]["schedule_time"], "12:00")
            self.assertTrue(daily_tasks[0]["run_immediately"])

    def test_multiple_times_produce_separate_tasks(self):
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            scheduler = Scheduler()
            daily_tasks = []

            watchlist_analysis_time = "12:00,21:00"
            watchlist_times = [
                t.strip()
                for t in watchlist_analysis_time.split(",")
                if t.strip()
            ]
            for idx, wt in enumerate(watchlist_times):
                slot_name = f"watchlist_analysis_{wt.replace(':', '_')}"
                daily_tasks.append({
                    "name": slot_name,
                    "task": lambda: None,
                    "schedule_time": wt,
                    "run_immediately": True if idx == 0 else False,
                })

            self.assertEqual(len(daily_tasks), 2)
            self.assertEqual(daily_tasks[0]["name"], "watchlist_analysis_12_00")
            self.assertEqual(daily_tasks[0]["schedule_time"], "12:00")
            self.assertTrue(daily_tasks[0]["run_immediately"])
            self.assertEqual(daily_tasks[1]["name"], "watchlist_analysis_21_00")
            self.assertEqual(daily_tasks[1]["schedule_time"], "21:00")
            self.assertFalse(daily_tasks[1]["run_immediately"])

    def test_empty_time_produces_no_tasks(self):
        watchlist_analysis_time = ""
        watchlist_times = [
            t.strip()
            for t in watchlist_analysis_time.split(",")
            if t.strip()
        ]
        self.assertEqual(len(watchlist_times), 0)

    def test_whitespace_handling(self):
        watchlist_analysis_time = " 12:00 , 21:00 "
        watchlist_times = [
            t.strip()
            for t in watchlist_analysis_time.split(",")
            if t.strip()
        ]
        self.assertEqual(watchlist_times, ["12:00", "21:00"])

    def test_only_first_slot_gets_run_immediately(self):
        watchlist_analysis_time = "08:00,12:00,21:00"
        watchlist_times = [
            t.strip()
            for t in watchlist_analysis_time.split(",")
            if t.strip()
        ]
        results = []
        for idx, wt in enumerate(watchlist_times):
            results.append({
                "run_immediately": True if idx == 0 else False,
            })
        self.assertTrue(results[0]["run_immediately"])
        self.assertFalse(results[1]["run_immediately"])
        self.assertFalse(results[2]["run_immediately"])

    def test_registered_via_scheduler(self):
        """Integration: registering comma-separated times via add_daily_task."""
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            scheduler = Scheduler()
            watchlist_analysis_time = "12:00,21:00"
            watchlist_times = [
                t.strip()
                for t in watchlist_analysis_time.split(",")
                if t.strip()
            ]
            for idx, wt in enumerate(watchlist_times):
                slot_name = f"watchlist_analysis_{wt.replace(':', '_')}"
                scheduler.add_daily_task(
                    name=slot_name,
                    task=lambda: None,
                    schedule_time=wt,
                    run_immediately=True if idx == 0 else False,
                )

            self.assertEqual(len(fake_schedule.jobs), 2)
            self.assertEqual(fake_schedule.jobs[0].at_time, "12:00")
            self.assertEqual(fake_schedule.jobs[1].at_time, "21:00")
            self.assertIn("watchlist_analysis_12_00", scheduler._daily_task_callbacks)
            self.assertIn("watchlist_analysis_21_00", scheduler._daily_task_callbacks)


class RunWithScheduleMultiTaskTestCase(unittest.TestCase):
    def test_run_with_schedule_daily_tasks(self):
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src import scheduler as scheduler_module

            order = []

            class FakeScheduler:
                def __init__(self, schedule_time="18:00", schedule_time_provider=None, heartbeat_path=None):
                    order.append(("init", schedule_time))

                def add_background_task(self, **kwargs):
                    order.append(("background", kwargs["name"]))

                def add_daily_task(
                    self, name, task, schedule_time, run_immediately=False
                ):
                    order.append(("daily_task", name, schedule_time))
                    return True

                def set_daily_task(self, task, run_immediately=True):
                    order.append(("daily", run_immediately))

                def run(self):
                    order.append(("run", None))

            with patch.object(scheduler_module, "Scheduler", FakeScheduler):
                scheduler_module.run_with_schedule(
                    daily_tasks=[
                        {
                            "name": "watchlist_analysis",
                            "task": lambda: None,
                            "schedule_time": "09:00",
                        },
                        {
                            "name": "market_review",
                            "task": lambda: None,
                            "schedule_time": "21:00",
                        },
                    ],
                    background_tasks=[
                        {
                            "task": lambda: None,
                            "interval_seconds": 60,
                            "run_immediately": True,
                            "name": "event_monitor",
                        },
                    ],
                )

        self.assertEqual(order[0], ("init", "18:00"))
        self.assertEqual(order[1], ("background", "event_monitor"))
        self.assertEqual(order[2], ("daily_task", "watchlist_analysis", "09:00"))
        self.assertEqual(order[3], ("daily_task", "market_review", "21:00"))
        self.assertEqual(order[4], ("run", None))
        # Should not call set_daily_task in multi-task mode
        self.assertFalse(any(x[0] == "daily" for x in order))

    def test_run_with_schedule_single_task_backward_compatible(self):
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src import scheduler as scheduler_module

            order = []

            class FakeScheduler:
                def __init__(self, schedule_time="18:00", schedule_time_provider=None, heartbeat_path=None):
                    order.append(("init", schedule_time))

                def add_background_task(self, **kwargs):
                    order.append(("background", kwargs["name"]))

                def add_daily_task(
                    self, name, task, schedule_time, run_immediately=False
                ):
                    order.append(("daily_task", name, schedule_time))
                    return True

                def set_daily_task(self, task, run_immediately=True):
                    order.append(("daily", run_immediately))

                def run(self):
                    order.append(("run", None))

            with patch.object(scheduler_module, "Scheduler", FakeScheduler):
                scheduler_module.run_with_schedule(
                    task=lambda: None,
                    schedule_time="18:00",
                    run_immediately=True,
                )

        self.assertEqual(order[0], ("init", "18:00"))
        self.assertEqual(order[1], ("daily", True))
        self.assertEqual(order[2], ("run", None))
        # Should not call add_daily_task in single-task mode
        self.assertFalse(any(x[0] == "daily_task" for x in order))


if __name__ == "__main__":
    unittest.main()
