from __future__ import annotations

import asyncio
import unittest
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

from sqlalchemy.dialects import postgresql

from tgcurator.infrastructure.database import (
    SqlAlchemyOperationsBacklogRepository,
    operations_backlog_statement,
)
from tgcurator.infrastructure.observability import (
    OperationsMonitor,
    PublicationMetric,
)
from tgcurator.shared import DomainValidationError

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


class OperationsMonitorTests(unittest.TestCase):
    def test_publication_counters_have_typed_deterministic_snapshots(self) -> None:
        monitor = OperationsMonitor()

        monitor.record_publication(PublicationMetric.CLAIMED, count=2)
        monitor.record_publication("sent")
        monitor.record_publication("retry_scheduled", count=3)
        monitor.record_publication("failed")
        monitor.record_publication("cancelled")
        monitor.record_publication("reconciled_part", count=2)
        monitor.record_publication("sent", count=0)

        publication = monitor.snapshot(now=NOW).publication
        self.assertEqual(publication.claimed, 2)
        self.assertEqual(publication.sent, 1)
        self.assertEqual(publication.retry_scheduled, 3)
        self.assertEqual(publication.failed, 1)
        self.assertEqual(publication.cancelled, 1)
        self.assertEqual(publication.reconciled_parts, 2)
        with self.assertRaises(FrozenInstanceError):
            publication.sent = 9  # type: ignore[misc]

    def test_flood_wait_metrics_track_count_total_max_and_latest_boundary(self) -> None:
        monitor = OperationsMonitor()
        monitor.record_flood_wait(observed_at=NOW, retry_at=NOW + timedelta(seconds=30))
        monitor.record_flood_wait(
            observed_at=NOW + timedelta(minutes=1),
            retry_at=NOW + timedelta(minutes=3),
        )

        flood_wait = monitor.snapshot(now=NOW + timedelta(minutes=3)).flood_wait
        self.assertEqual(flood_wait.events, 2)
        self.assertEqual(flood_wait.wait_seconds_total, 150.0)
        self.assertEqual(flood_wait.wait_seconds_max, 120.0)
        self.assertEqual(flood_wait.last_observed_at, NOW + timedelta(minutes=1))
        self.assertEqual(flood_wait.last_retry_at, NOW + timedelta(minutes=3))

        with self.assertRaisesRegex(DomainValidationError, "must not be before"):
            monitor.record_flood_wait(observed_at=NOW, retry_at=NOW - timedelta(seconds=1))

    def test_backlog_observation_replaces_gauges_and_rejects_invalid_counts(self) -> None:
        monitor = OperationsMonitor()
        monitor.observe_backlog(
            publication_pending=5,
            publication_partial=2,
            publication_retry_due=1,
            cleanup_pending=4,
            cleanup_retry_due=3,
            durable_wakeups_due=7,
            observed_at=NOW,
        )

        backlog = monitor.snapshot(now=NOW).backlog
        self.assertEqual(backlog.publication_pending, 5)
        self.assertEqual(backlog.publication_partial, 2)
        self.assertEqual(backlog.publication_retry_due, 1)
        self.assertEqual(backlog.cleanup_pending, 4)
        self.assertEqual(backlog.cleanup_retry_due, 3)
        self.assertEqual(backlog.durable_wakeups_due, 7)
        self.assertEqual(backlog.observed_at, NOW)

        with self.assertRaisesRegex(DomainValidationError, "cleanup_pending"):
            monitor.observe_backlog(
                publication_pending=0,
                publication_partial=0,
                publication_retry_due=0,
                cleanup_pending=-1,
                cleanup_retry_due=0,
                durable_wakeups_due=0,
                observed_at=NOW,
            )

    def test_worker_and_scheduler_health_require_fresh_database_and_broker_heartbeat(self) -> None:
        monitor = OperationsMonitor(liveness_timeout=timedelta(seconds=30))

        missing = monitor.snapshot(now=NOW)
        self.assertFalse(missing.worker.alive)
        self.assertFalse(missing.worker.ready)
        self.assertEqual(
            missing.worker.blockers,
            ("heartbeat_missing", "database_unavailable", "broker_unavailable"),
        )

        monitor.heartbeat_worker(at=NOW, database_ready=True, broker_ready=False)
        monitor.heartbeat_scheduler(at=NOW, database_ready=True, broker_ready=True)
        current = monitor.snapshot(now=NOW + timedelta(seconds=10))
        self.assertTrue(current.worker.alive)
        self.assertFalse(current.worker.ready)
        self.assertEqual(current.worker.blockers, ("broker_unavailable",))
        self.assertTrue(current.scheduler.alive)
        self.assertTrue(current.scheduler.ready)
        self.assertEqual(current.scheduler.blockers, ())

        stale = monitor.snapshot(now=NOW + timedelta(seconds=31))
        self.assertFalse(stale.worker.alive)
        self.assertFalse(stale.scheduler.ready)
        self.assertIn("heartbeat_stale", stale.scheduler.blockers)

    def test_validation_rejects_unknown_metrics_naive_time_and_regressing_heartbeat(self) -> None:
        monitor = OperationsMonitor()
        with self.assertRaisesRegex(DomainValidationError, "unsupported publication metric"):
            monitor.record_publication("unknown")
        with self.assertRaisesRegex(DomainValidationError, "non-negative"):
            monitor.record_publication("sent", count=-1)
        with self.assertRaisesRegex(DomainValidationError, "timezone-aware"):
            monitor.worker_health(now=datetime(2026, 9, 16, 12, 0))

        monitor.heartbeat_worker(at=NOW, database_ready=True, broker_ready=True)
        with self.assertRaisesRegex(DomainValidationError, "monotonic"):
            monitor.heartbeat_worker(
                at=NOW - timedelta(seconds=1),
                database_ready=True,
                broker_ready=True,
            )

    def test_backlog_statement_and_repository_count_pending_partial_and_due_work(self) -> None:
        statement = operations_backlog_statement(now=NOW)
        sql = str(statement.compile(dialect=postgresql.dialect()))
        self.assertIn("FROM publication_intents", sql)
        self.assertIn("cardinality(publication_intents.completed_parts)", sql)
        self.assertIn("FROM archive_cleanup_jobs", sql)
        self.assertIn("FROM durable_wakeups", sql)

        class _RowResult:
            def one(self):
                return {
                    "publication_pending": 4,
                    "publication_partial": 2,
                    "publication_retry_due": 1,
                    "cleanup_pending": 3,
                    "cleanup_retry_due": 1,
                    "durable_wakeups_due": 6,
                }

        class _Session:
            async def execute(self, _statement):
                return _RowResult()

        class _Database:
            @asynccontextmanager
            async def session(self):
                yield _Session()

        repo = SqlAlchemyOperationsBacklogRepository(_Database())  # type: ignore[arg-type]
        backlog = asyncio.run(repo.observe_backlog(now=NOW))
        self.assertEqual(backlog.publication_pending, 4)
        self.assertEqual(backlog.publication_partial, 2)
        self.assertEqual(backlog.publication_retry_due, 1)
        self.assertEqual(backlog.cleanup_pending, 3)
        self.assertEqual(backlog.cleanup_retry_due, 1)
        self.assertEqual(backlog.durable_wakeups_due, 6)
        self.assertEqual(backlog.observed_at, NOW)


if __name__ == "__main__":
    unittest.main()
