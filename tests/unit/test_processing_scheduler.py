from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from tgcurator.application.ports.processing import ScheduledProcessingRange
from tgcurator.application.processing import ProcessingRangeScheduler
from tgcurator.domain.processing import BoundaryKind, ProcessingRange, RangeExecution

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
START = NOW - timedelta(days=2)


class FakeRangeRepository:
    def __init__(
        self, ranges: tuple[ScheduledProcessingRange, ...], *, create_result: bool = True
    ) -> None:
        self.ranges = ranges
        self.create_result = create_result
        self.created: list[tuple[RangeExecution, str, str, datetime]] = []

    async def list_enabled_ranges(self) -> tuple[ScheduledProcessingRange, ...]:
        return self.ranges

    async def create_execution_and_wakeup(
        self,
        *,
        execution: RangeExecution,
        source_profile_version_id: str,
        queue: str,
        available_at: datetime,
    ) -> bool:
        self.created.append((execution, source_profile_version_id, queue, available_at))
        return self.create_result


class FakeTelegramGateway:
    def __init__(self, latest_by_channel: dict[str, datetime | None]) -> None:
        self.latest_by_channel = latest_by_channel
        self.calls: list[str] = []

    async def newest_message_at(self, *, source_channel_id: str) -> datetime | None:
        self.calls.append(source_channel_id)
        return self.latest_by_channel[source_channel_id]


class ProcessingRangeSchedulerTests(unittest.TestCase):
    def test_fixed_range_creates_execution_without_telegram_lookup(self) -> None:
        source_channel_id = str(uuid4())
        profile_version_id = str(uuid4())
        processing_range = ProcessingRange(
            range_id=str(uuid4()),
            start_at=START,
            boundary_kind=BoundaryKind.FIXED,
            fixed_end_at=NOW - timedelta(days=1),
        )
        repository = FakeRangeRepository(
            (
                ScheduledProcessingRange(
                    processing_range=processing_range,
                    source_channel_id=source_channel_id,
                    source_profile_version_id=profile_version_id,
                    processing_watermark_at=None,
                ),
            )
        )
        telegram = FakeTelegramGateway({source_channel_id: None})

        report = asyncio.run(ProcessingRangeScheduler(repository, telegram).schedule(now=NOW))

        self.assertEqual(report.scanned_ranges, 1)
        self.assertEqual(report.created_executions, 1)
        self.assertEqual(telegram.calls, [])
        self.assertEqual(len(repository.created), 1)
        execution, stored_profile_id, queue, available_at = repository.created[0]
        self.assertEqual(execution.from_at, START)
        self.assertEqual(execution.to_at, NOW - timedelta(days=1))
        self.assertEqual(stored_profile_id, profile_version_id)
        self.assertEqual(queue, "range_execution")
        self.assertEqual(available_at, NOW)

    def test_latest_range_freezes_only_after_quiet_window(self) -> None:
        source_channel_id = str(uuid4())
        latest_message_at = NOW - timedelta(minutes=10)
        processing_range = ProcessingRange(
            range_id=str(uuid4()),
            start_at=START,
            boundary_kind=BoundaryKind.LATEST,
            latest_quiet_period=timedelta(minutes=5),
        )
        repository = FakeRangeRepository(
            (
                ScheduledProcessingRange(
                    processing_range=processing_range,
                    source_channel_id=source_channel_id,
                    source_profile_version_id=str(uuid4()),
                    processing_watermark_at=START + timedelta(hours=1),
                ),
            )
        )
        telegram = FakeTelegramGateway({source_channel_id: latest_message_at})

        report = asyncio.run(ProcessingRangeScheduler(repository, telegram).schedule(now=NOW))

        self.assertEqual(report.created_executions, 1)
        self.assertEqual(telegram.calls, [source_channel_id])
        execution = repository.created[0][0]
        self.assertEqual(execution.from_at, START + timedelta(hours=1))
        self.assertEqual(execution.to_at, latest_message_at)

    def test_latest_range_does_not_schedule_an_unstable_or_duplicate_window(self) -> None:
        source_channel_id = str(uuid4())
        processing_range = ProcessingRange(
            range_id=str(uuid4()),
            start_at=START,
            boundary_kind=BoundaryKind.LATEST,
            latest_quiet_period=timedelta(minutes=15),
        )
        scheduled = ScheduledProcessingRange(
            processing_range=processing_range,
            source_channel_id=source_channel_id,
            source_profile_version_id=str(uuid4()),
            processing_watermark_at=None,
        )
        unstable_repository = FakeRangeRepository((scheduled,))
        telegram = FakeTelegramGateway({source_channel_id: NOW - timedelta(minutes=5)})

        unstable_report = asyncio.run(
            ProcessingRangeScheduler(unstable_repository, telegram).schedule(now=NOW)
        )
        self.assertEqual(unstable_report.created_executions, 0)
        self.assertEqual(unstable_repository.created, [])

        duplicate_repository = FakeRangeRepository((scheduled,), create_result=False)
        stable_telegram = FakeTelegramGateway({source_channel_id: NOW - timedelta(minutes=20)})
        duplicate_report = asyncio.run(
            ProcessingRangeScheduler(duplicate_repository, stable_telegram).schedule(now=NOW)
        )
        self.assertEqual(duplicate_report.created_executions, 0)
        self.assertEqual(len(duplicate_repository.created), 1)


if __name__ == "__main__":
    unittest.main()
