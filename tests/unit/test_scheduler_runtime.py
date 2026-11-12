from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime

from apps.scheduler.runtime import SchedulerRuntime
from tgcurator.application.ports.operations import BacklogMetricsSnapshot
from tgcurator.application.processing import RangeScheduleReport, WakeupDispatchReport
from tgcurator.infrastructure.observability import OperationsMonitor

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


class FakePublicationRepository:
    def __init__(self, events: list[str], *, repaired: int = 3) -> None:
        self.events = events
        self.repaired = repaired

    async def repair_wakeups(self, *, now: datetime, limit: int = 500) -> int:
        self.events.append(f"repair:{now.isoformat()}:{limit}")
        return self.repaired


class FakeCleanupRepository:
    def __init__(self, events: list[str], *, repaired: int = 5) -> None:
        self.events = events
        self.repaired = repaired

    async def repair_wakeups(self, *, now: datetime, limit: int = 500) -> int:
        self.events.append(f"cleanup-repair:{now.isoformat()}:{limit}")
        return self.repaired


class SchedulerRuntimeTests(unittest.TestCase):
    def test_repairs_publication_wakeups_before_scheduling_and_dispatch(self) -> None:
        events: list[str] = []
        runtime = SchedulerRuntime(
            database=FakeDatabase(),
            range_scheduler=FakeRangeScheduler(events),
            wakeup_dispatcher=FakeWakeupDispatcher(events),
            publication_repository=FakePublicationRepository(events),  # type: ignore[arg-type]
            cleanup_repository=FakeCleanupRepository(events),  # type: ignore[arg-type]
        )

        report = asyncio.run(
            runtime.run_once(
                now=NOW,
                dispatch_limit=17,
                publication_repair_limit=23,
                cleanup_repair_limit=29,
            )
        )

        self.assertEqual(
            events,
            [
                f"repair:{NOW.isoformat()}:23",
                f"cleanup-repair:{NOW.isoformat()}:29",
                f"schedule:{NOW.isoformat()}",
                f"dispatch:{NOW.isoformat()}:17",
            ],
        )
        self.assertEqual(report.range_schedule.created_executions, 2)
        self.assertEqual(report.wakeup_dispatch.dispatched_wakeups, 2)
        self.assertEqual(report.repaired_publication_wakeups, 3)
        self.assertEqual(report.repaired_cleanup_wakeups, 5)

    def test_observes_backlog_and_records_scheduler_heartbeat_and_broker_health(self) -> None:
        events: list[str] = []
        monitor = OperationsMonitor()

        class FakeBacklogRepository:
            async def observe_backlog(self, *, now: datetime) -> BacklogMetricsSnapshot:
                events.append(f"backlog:{now.isoformat()}")
                return BacklogMetricsSnapshot(
                    publication_pending=2,
                    publication_partial=1,
                    publication_retry_due=1,
                    cleanup_pending=1,
                    cleanup_retry_due=0,
                    durable_wakeups_due=3,
                    observed_at=now,
                )

        runtime = SchedulerRuntime(
            database=FakeDatabase(),
            range_scheduler=FakeRangeScheduler(events),
            wakeup_dispatcher=FakeWakeupDispatcher(events),
            publication_repository=FakePublicationRepository(events),  # type: ignore[arg-type]
            cleanup_repository=FakeCleanupRepository(events),  # type: ignore[arg-type]
            backlog_repository=FakeBacklogRepository(),
            telemetry=monitor,
        )

        asyncio.run(runtime.run_once(now=NOW))
        snapshot = monitor.snapshot(now=NOW)
        self.assertTrue(snapshot.scheduler.alive)
        self.assertTrue(snapshot.scheduler.ready)
        self.assertEqual(snapshot.backlog.publication_pending, 2)
        self.assertEqual(snapshot.backlog.publication_partial, 1)
        self.assertEqual(snapshot.backlog.durable_wakeups_due, 3)


if __name__ == "__main__":
    unittest.main()
