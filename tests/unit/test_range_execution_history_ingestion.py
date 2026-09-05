from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from tgcurator.application.ports.processing import ClaimedRangeExecution
from tgcurator.application.processing import RangeExecutionHistoryIngestion
from tgcurator.domain.messages import TelegramMessage
from tgcurator.domain.processing import RangeExecution, RangeExecutionStatus
from tgcurator.shared import DomainValidationError

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
FROM_AT = NOW - timedelta(hours=1)
TO_AT = NOW


class RangeExecutionHistoryIngestionTests(unittest.TestCase):
    def test_ingests_before_advancing_and_completing_execution(self) -> None:
        claim = _claim()
        message = TelegramMessage(
            source_channel_id=claim.source_channel_id,
            telegram_message_id=10,
            sent_at=FROM_AT + timedelta(minutes=5),
            text="history message",
        )
        gateway = _Gateway((message,))
        ingestion = _IngestionService()
        worker = _RangeExecutionWorker()
        service = RangeExecutionHistoryIngestion(
            range_execution_worker=worker,
            telegram_gateway=gateway,
            message_ingest_service=ingestion,
        )

        completed = asyncio.run(service.process(claim=claim, now=NOW))

        self.assertTrue(completed)
        self.assertEqual(
            gateway.requests,
            [(claim.source_channel_id, FROM_AT, TO_AT)],
        )
        self.assertEqual(ingestion.history_batches, [(message,)])
        self.assertEqual(
            worker.advance_requests,
            [(claim.execution.execution_id, claim.lease_token, TO_AT, NOW)],
        )
        self.assertEqual(
            worker.complete_requests,
            [(claim.execution.execution_id, claim.lease_token, NOW)],
        )

    def test_an_empty_history_window_still_advances_and_completes(self) -> None:
        claim = _claim()
        ingestion = _IngestionService()
        worker = _RangeExecutionWorker()
        service = RangeExecutionHistoryIngestion(
            range_execution_worker=worker,
            telegram_gateway=_Gateway(()),
            message_ingest_service=ingestion,
        )

        completed = asyncio.run(service.process(claim=claim, now=NOW))

        self.assertTrue(completed)
        self.assertEqual(ingestion.history_batches, [()])
        self.assertEqual(len(worker.advance_requests), 1)
        self.assertEqual(len(worker.complete_requests), 1)

    def test_rejects_source_or_time_outside_the_claimed_window_before_ingesting(self) -> None:
        claim = _claim()
        cases = (
            TelegramMessage(
                source_channel_id=str(uuid4()),
                telegram_message_id=10,
                sent_at=FROM_AT + timedelta(minutes=1),
            ),
            TelegramMessage(
                source_channel_id=claim.source_channel_id,
                telegram_message_id=11,
                sent_at=TO_AT,
            ),
        )
        for message in cases:
            with self.subTest(message=message.telegram_message_id):
                ingestion = _IngestionService()
                worker = _RangeExecutionWorker()
                service = RangeExecutionHistoryIngestion(
                    range_execution_worker=worker,
                    telegram_gateway=_Gateway((message,)),
                    message_ingest_service=ingestion,
                )

                with self.assertRaises(DomainValidationError):
                    asyncio.run(service.process(claim=claim, now=NOW))

                self.assertEqual(ingestion.history_batches, [])
                self.assertEqual(worker.advance_requests, [])
                self.assertEqual(worker.complete_requests, [])

    def test_failed_watermark_update_never_completes_the_execution(self) -> None:
        claim = _claim()
        worker = _RangeExecutionWorker(advance_result=False)
        service = RangeExecutionHistoryIngestion(
            range_execution_worker=worker,
            telegram_gateway=_Gateway(()),
            message_ingest_service=_IngestionService(),
        )

        completed = asyncio.run(service.process(claim=claim, now=NOW))

        self.assertFalse(completed)
        self.assertEqual(len(worker.advance_requests), 1)
        self.assertEqual(worker.complete_requests, [])


class _Gateway:
    def __init__(self, messages: tuple[TelegramMessage, ...]) -> None:
        self._messages = messages
        self.requests: list[tuple[str, datetime, datetime]] = []

    async def fetch_history(
        self, *, source_channel_id: str, from_at: datetime, to_at: datetime
    ) -> tuple[TelegramMessage, ...]:
        self.requests.append((source_channel_id, from_at, to_at))
        return self._messages


class _IngestionService:
    def __init__(self) -> None:
        self.history_batches: list[tuple[TelegramMessage, ...]] = []

    async def ingest_history(self, messages: tuple[TelegramMessage, ...]) -> None:
        self.history_batches.append(messages)


class _RangeExecutionWorker:
    def __init__(self, *, advance_result: bool = True, complete_result: bool = True) -> None:
        self._advance_result = advance_result
        self._complete_result = complete_result
        self.advance_requests: list[tuple[str, str, datetime, datetime]] = []
        self.complete_requests: list[tuple[str, str, datetime]] = []

    async def advance_watermark(
        self, *, execution_id: str, lease_token: str, watermark_at: datetime, now: datetime
    ) -> bool:
        self.advance_requests.append((execution_id, lease_token, watermark_at, now))
        return self._advance_result

    async def complete(self, *, execution_id: str, lease_token: str, now: datetime) -> bool:
        self.complete_requests.append((execution_id, lease_token, now))
        return self._complete_result


def _claim() -> ClaimedRangeExecution:
    return ClaimedRangeExecution(
        execution=RangeExecution(
            execution_id=str(uuid4()),
            range_id=str(uuid4()),
            from_at=FROM_AT,
            to_at=TO_AT,
            status=RangeExecutionStatus.RUNNING,
        ),
        source_channel_id=str(uuid4()),
        source_profile_version_id=str(uuid4()),
        lease_token=str(uuid4()),
    )


if __name__ == "__main__":
    unittest.main()
