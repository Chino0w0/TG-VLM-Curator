from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime

from apps.scheduler.runtime import SchedulerRuntime
from tgcurator.application.processing import RangeScheduleReport, WakeupDispatchReport

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class FakeDatabase:
    async def dispose(self) -> None:
        return None


class FakeRangeScheduler:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def schedule(self, *, now: datetime) -> RangeScheduleReport:
        self.events.append(f"schedule:{now.isoformat()}")
        return RangeScheduleReport(scanned_ranges=3, created_executions=2)


class FakeWakeupDispatcher:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def dispatch_due(self, *, now: datetime, limit: int) -> WakeupDispatchReport:
        self.events.append(f"dispatch:{now.isoformat()}:{limit}")
        return WakeupDispatchReport(claimed_wakeups=2, dispatched_wakeups=2, failed_dispatches=0)


class SchedulerRuntimeTests(unittest.TestCase):
    def test_runs_range_scheduling_before_durable_wakeup_dispatch(self) -> None:
        events: list[str] = []
        runtime = SchedulerRuntime(
            database=FakeDatabase(),
            range_scheduler=FakeRangeScheduler(events),
            wakeup_dispatcher=FakeWakeupDispatcher(events),
        )

        report = asyncio.run(runtime.run_once(now=NOW, dispatch_limit=17))

        self.assertEqual(
            events,
            [f"schedule:{NOW.isoformat()}", f"dispatch:{NOW.isoformat()}:17"],
        )
        self.assertEqual(report.range_schedule.created_executions, 2)
        self.assertEqual(report.wakeup_dispatch.dispatched_wakeups, 2)


if __name__ == "__main__":
    unittest.main()
