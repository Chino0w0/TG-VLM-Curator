from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime
from uuid import uuid4

from tgcurator.application.ports.processing import ClaimedRangeExecution
from tgcurator.application.processing import RangeExecutionHistoryIngestion
from tgcurator.domain.messages import TelegramMessage
from tgcurator.shared import DomainValidationError

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
FROM_MESSAGE_ID_EXCLUSIVE = 5
TO_MESSAGE_ID_INCLUSIVE = 10


class RangeExecutionHistoryIngestionTests(unittest.TestCase):
    def test_ingests_before_advancing_and_completing_execution(self) -> None:
        claim = _claim()
        message = TelegramMessage(
            source_channel_id=claim.source_channel_id,
            telegram_message_id=10,
            published_at=NOW,
            original_text="history message",
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
            [
                (
                    claim.source_channel_id,
                    FROM_MESSAGE_ID_EXCLUSIVE,
                    TO_MESSAGE_ID_INCLUSIVE,
                )
            ],
        )
        self.assertEqual(ingestion.history_batches, [(message,)])
        self.assertEqual(
            worker.advance_requests,
            [(claim.execution_id, claim.lease_token, TO_MESSAGE_ID_INCLUSIVE, NOW)],
        )
        self.assertEqual(
            worker.complete_requests,
            [(claim.execution_id, claim.lease_token, NOW)],
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

    def test_rejects_source_or_id_outside_the_claimed_window_before_ingesting(self) -> None:
        claim = _claim()
        cases = (
            TelegramMessage(
                source_channel_id=str(uuid4()),
                telegram_message_id=6,
                published_at=NOW,
            ),
            TelegramMessage(
                source_channel_id=claim.source_channel_id,
                telegram_message_id=FROM_MESSAGE_ID_EXCLUSIVE,
                published_at=NOW,
            ),
            TelegramMessage(
                source_channel_id=claim.source_channel_id,
                telegram_message_id=TO_MESSAGE_ID_INCLUSIVE + 1,
                published_at=NOW,
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
        self.requests: list[tuple[str, int, int]] = []

    async def fetch_history(
        self,
        *,
        source_channel_id: str,
        from_message_id_exclusive: int,
        to_message_id_inclusive: int,
    ) -> tuple[TelegramMessage, ...]:
        self.requests.append(
            (source_channel_id, from_message_id_exclusive, to_message_id_inclusive)
        )
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
        self.advance_requests: list[tuple[str, str, int, datetime]] = []
        self.complete_requests: list[tuple[str, str, datetime]] = []

    async def advance_watermark(
        self,
        *,
        execution_id: str,
        lease_token: str,
        watermark_message_id: int,
        now: datetime,
    ) -> bool:
        self.advance_requests.append((execution_id, lease_token, watermark_message_id, now))
        return self._advance_result

    async def complete(self, *, execution_id: str, lease_token: str, now: datetime) -> bool:
        self.complete_requests.append((execution_id, lease_token, now))
        return self._complete_result


def _claim() -> ClaimedRangeExecution:
    return ClaimedRangeExecution(
        execution_id=str(uuid4()),
        processing_range_id=str(uuid4()),
        source_channel_id=str(uuid4()),
        source_profile_version_id=str(uuid4()),
        from_message_id_exclusive=FROM_MESSAGE_ID_EXCLUSIVE,
        to_message_id_inclusive=TO_MESSAGE_ID_INCLUSIVE,
        watermark_message_id=FROM_MESSAGE_ID_EXCLUSIVE,
        attempt_count=1,
        max_attempts=5,
        lease_token=str(uuid4()),
    )


if __name__ == "__main__":
    unittest.main()
