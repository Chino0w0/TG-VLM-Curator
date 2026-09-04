import unittest
from datetime import UTC, datetime, timedelta

from tgcurator.domain.processing import (
    BoundaryKind,
    ProcessingRange,
    RangeExecution,
    latest_boundary_is_stable,
)
from tgcurator.shared import DomainValidationError

UTC = UTC
START = datetime(2026, 1, 1, tzinfo=UTC)
END = datetime(2026, 1, 2, tzinfo=UTC)


class ProcessingRangeTests(unittest.TestCase):
    def test_fixed_range_has_immutable_right_boundary(self) -> None:
        processing_range = ProcessingRange("range-1", START, BoundaryKind.FIXED, fixed_end_at=END)
        execution = processing_range.create_execution(execution_id="execution-1", from_at=START)
        self.assertIsNotNone(execution)
        assert execution is not None
        self.assertEqual(execution.to_at, END)

    def test_latest_range_waits_for_quiet_period_then_freezes(self) -> None:
        processing_range = ProcessingRange(
            "range-latest",
            START,
            BoundaryKind.LATEST,
            latest_quiet_period=timedelta(minutes=5),
        )
        latest = START + timedelta(hours=1)
        self.assertIsNone(
            processing_range.create_execution(
                execution_id="execution-1",
                from_at=START,
                latest_message_at=latest,
                observed_at=latest + timedelta(minutes=4),
            )
        )
        execution = processing_range.create_execution(
            execution_id="execution-2",
            from_at=START,
            latest_message_at=latest,
            observed_at=latest + timedelta(minutes=5),
        )
        self.assertIsNotNone(execution)
        assert execution is not None
        self.assertEqual(execution.to_at, latest)
        self.assertTrue(
            latest_boundary_is_stable(latest, latest + timedelta(minutes=5), timedelta(minutes=5))
        )

    def test_watermark_is_monotonic_and_completion_requires_right_boundary(self) -> None:
        execution = RangeExecution("execution-1", "range-1", START, END)
        advanced = execution.advance_watermark(START + timedelta(hours=4))
        with self.assertRaises(DomainValidationError):
            advanced.advance_watermark(START + timedelta(hours=3))
        with self.assertRaises(DomainValidationError):
            advanced.complete()
        self.assertEqual(advanced.advance_watermark(END).complete().watermark_at, END)


if __name__ == "__main__":
    unittest.main()
