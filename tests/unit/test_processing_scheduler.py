from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from tgcurator.application.ports.processing import (
    LatestMessageBoundary,
    PendingRangeExecution,
    ScheduledProcessingRange,
)
from tgcurator.application.processing import ProcessingRangeScheduler

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


class FakeRangeRepository:
    def __init__(
        self, ranges: tuple[ScheduledProcessingRange, ...], *, create_result: bool = True
    ) -> None:
        self.ranges = ranges
        self.create_result = create_result
        self.created: list[tuple[PendingRangeExecution, str, datetime]] = []

    async def list_enabled_ranges(self) -> tuple[ScheduledProcessingRange, ...]:
        return self.ranges

    async def create_execution_and_wakeup(
        self, *, execution: PendingRangeExecution, queue: str, available_at: datetime
    ) -> bool:
        self.created.append((execution, queue, available_at))
        return self.create_result


class FakeLatestBoundarySource:
    def __init__(self, boundaries: dict[str, LatestMessageBoundary | None]) -> None:
        self.boundaries = boundaries
        self.calls: list[str] = []

    async def newest_message(self, *, source_channel_id: str) -> LatestMessageBoundary | None:
        self.calls.append(source_channel_id)
        return self.boundaries[source_channel_id]


def scheduled_range(
    *,
    end_mode: str,
    start_message_id: int = 100,
    fixed_end_message_id: int | None = None,
    watermark_message_id: int | None = None,
    steady_after_seconds: int | None = None,
) -> ScheduledProcessingRange:
    return ScheduledProcessingRange(
        range_id=str(uuid4()),
        source_channel_id=str(uuid4()),
        source_profile_version_id=str(uuid4()),
        end_mode=end_mode,
        resolved_start_message_id=start_message_id,
        resolved_fixed_end_message_id=fixed_end_message_id,
        processing_watermark_message_id=watermark_message_id,
        steady_after_seconds=steady_after_seconds,
    )


class ProcessingRangeSchedulerTests(unittest.TestCase):
    def test_fixed_range_freezes_message_id_window_without_boundary_lookup(self) -> None:
        item = scheduled_range(end_mode="fixed", fixed_end_message_id=125)
        repository = FakeRangeRepository((item,))
        boundary_source = FakeLatestBoundarySource({})

        report = asyncio.run(
            ProcessingRangeScheduler(repository, boundary_source).schedule(now=NOW)
        )

        self.assertEqual(report.scanned_ranges, 1)
        self.assertEqual(report.created_executions, 1)
        self.assertEqual(report.skipped_ranges, 0)
        self.assertEqual(boundary_source.calls, [])
        execution, queue, available_at = repository.created[0]
        self.assertEqual(execution.processing_range_id, item.range_id)
        self.assertEqual(execution.source_profile_version_id, item.source_profile_version_id)
        self.assertEqual(execution.from_message_id_exclusive, 99)
        self.assertEqual(execution.to_message_id_inclusive, 125)
        self.assertEqual(execution.max_attempts, 5)
        self.assertEqual(queue, "range_execution")
        self.assertEqual(available_at, NOW)

    def test_latest_range_waits_for_stability_then_freezes_current_message_id(self) -> None:
        item = scheduled_range(
            end_mode="latest",
            watermark_message_id=110,
            steady_after_seconds=300,
        )
        stable_boundary = LatestMessageBoundary(
            message_id=140,
            published_at=NOW - timedelta(minutes=5),
        )
        repository = FakeRangeRepository((item,))
        boundary_source = FakeLatestBoundarySource({item.source_channel_id: stable_boundary})

        report = asyncio.run(
            ProcessingRangeScheduler(repository, boundary_source).schedule(now=NOW)
        )

        self.assertEqual(report.created_executions, 1)
        self.assertEqual(boundary_source.calls, [item.source_channel_id])
        execution = repository.created[0][0]
        self.assertEqual(execution.from_message_id_exclusive, 110)
        self.assertEqual(execution.to_message_id_inclusive, 140)

    def test_latest_range_does_not_expand_until_quiet_period_or_past_watermark(self) -> None:
        unstable = scheduled_range(end_mode="latest", steady_after_seconds=600)
        caught_up = scheduled_range(
            end_mode="latest",
            watermark_message_id=200,
            steady_after_seconds=60,
        )
        boundary_source = FakeLatestBoundarySource(
            {
                unstable.source_channel_id: LatestMessageBoundary(
                    message_id=150,
                    published_at=NOW - timedelta(minutes=9),
                ),
                caught_up.source_channel_id: LatestMessageBoundary(
                    message_id=200,
                    published_at=NOW - timedelta(hours=1),
                ),
            }
        )
        repository = FakeRangeRepository((unstable, caught_up))

        report = asyncio.run(
            ProcessingRangeScheduler(repository, boundary_source).schedule(now=NOW)
        )

        self.assertEqual(report.created_executions, 0)
        self.assertEqual(report.skipped_ranges, 2)
        self.assertEqual(repository.created, [])

    def test_repository_conflict_keeps_duplicate_window_as_a_skip(self) -> None:
        item = scheduled_range(end_mode="fixed", fixed_end_message_id=125)
        repository = FakeRangeRepository((item,), create_result=False)

        report = asyncio.run(
            ProcessingRangeScheduler(repository, FakeLatestBoundarySource({})).schedule(now=NOW)
        )

        self.assertEqual(report.created_executions, 0)
        self.assertEqual(report.skipped_ranges, 1)
        self.assertEqual(len(repository.created), 1)


if __name__ == "__main__":
    unittest.main()
