from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from tgcurator.application import (
    MediaGroupAggregationBuffer,
    RealtimeTelegramIngestion,
    SourceReconciliationService,
)
from tgcurator.application.ports.ingestion import IngestReport
from tgcurator.domain.messages import TelegramMessage, normalize_telegram_messages
from tgcurator.shared import DomainValidationError

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class MediaGroupAggregationBufferTests(unittest.TestCase):
    def test_regular_messages_are_ready_immediately(self) -> None:
        buffer = MediaGroupAggregationBuffer()
        message = _message(message_id=1)

        ready = buffer.add(message=message, now=NOW)

        self.assertEqual(ready, (message,))
        self.assertEqual(buffer.pending_group_count, 0)

    def test_media_group_parts_wait_then_release_in_component_id_order(self) -> None:
        buffer = MediaGroupAggregationBuffer(aggregation_window=timedelta(seconds=3))
        source_channel_id = str(uuid4())
        first = _message(message_id=12, grouped_id=700, source_channel_id=source_channel_id)
        second = _message(message_id=10, grouped_id=700, source_channel_id=source_channel_id)

        self.assertEqual(buffer.add(message=first, now=NOW), ())
        self.assertEqual(buffer.add(message=second, now=NOW + timedelta(seconds=2)), ())
        self.assertEqual(buffer.flush_due(now=NOW + timedelta(seconds=2)), ())

        self.assertEqual(
            buffer.flush_due(now=NOW + timedelta(seconds=3)),
            (second, first),
        )
        self.assertEqual(buffer.pending_group_count, 0)

    def test_repeated_component_delivery_replaces_the_pending_dto(self) -> None:
        buffer = MediaGroupAggregationBuffer()
        source_channel_id = str(uuid4())
        original = _message(
            message_id=5,
            grouped_id=70,
            source_channel_id=source_channel_id,
            text="before",
        )
        replay = _message(
            message_id=5,
            grouped_id=70,
            source_channel_id=source_channel_id,
            text="after",
        )

        buffer.add(message=original, now=NOW)
        buffer.add(message=replay, now=NOW + timedelta(seconds=1))

        self.assertEqual(buffer.flush_all(), (replay,))

    def test_same_group_id_from_different_sources_is_not_merged(self) -> None:
        buffer = MediaGroupAggregationBuffer()
        first = _message(message_id=2, grouped_id=11, source_channel_id=str(uuid4()))
        second = _message(message_id=1, grouped_id=11, source_channel_id=str(uuid4()))

        buffer.add(message=first, now=NOW)
        buffer.add(message=second, now=NOW)

        self.assertEqual(
            buffer.flush_all(),
            tuple(sorted((first, second), key=lambda message: message.source_channel_id)),
        )

    def test_rejects_nonpositive_window_and_naive_flush_time(self) -> None:
        with self.assertRaises(DomainValidationError):
            MediaGroupAggregationBuffer(aggregation_window=timedelta())

        with self.assertRaises(DomainValidationError):
            MediaGroupAggregationBuffer().flush_due(now=datetime(2026, 9, 4, 12, 0))


class RealtimeTelegramIngestionTests(unittest.TestCase):
    def test_update_uses_common_ingestion_path_after_media_group_window(self) -> None:
        service = _RecordingIngestService()
        cursors = _RecordingCursorRepository()
        ingestion = RealtimeTelegramIngestion(
            message_ingest_service=service,
            media_group_buffer=MediaGroupAggregationBuffer(aggregation_window=timedelta(seconds=3)),
            cursor_service=SourceReconciliationService(cursors),
        )
        source_channel_id = str(uuid4())
        first = _message(message_id=3, grouped_id=50, source_channel_id=source_channel_id)
        second = _message(message_id=4, grouped_id=50, source_channel_id=source_channel_id)

        first_report = asyncio.run(ingestion.ingest_update(message=first, now=NOW))
        second_report = asyncio.run(
            ingestion.ingest_update(message=second, now=NOW + timedelta(seconds=1))
        )
        self.assertEqual(cursors.requests, [])
        released_report = asyncio.run(ingestion.flush_due(now=NOW + timedelta(seconds=3)))

        self.assertEqual(first_report, IngestReport(logical_messages=0, component_messages=0))
        self.assertEqual(second_report, IngestReport(logical_messages=0, component_messages=0))
        self.assertEqual(released_report, IngestReport(logical_messages=1, component_messages=2))
        self.assertEqual(service.history_batches, [(first, second)])
        self.assertEqual(
            cursors.requests,
            [
                ("advance_latest", source_channel_id, 4),
                ("advance_last", source_channel_id, 4),
            ],
        )

    def test_regular_update_and_controlled_shutdown_flush_are_persisted(self) -> None:
        service = _RecordingIngestService()
        cursors = _RecordingCursorRepository()
        ingestion = RealtimeTelegramIngestion(
            message_ingest_service=service,
            media_group_buffer=MediaGroupAggregationBuffer(),
            cursor_service=SourceReconciliationService(cursors),
        )
        regular_source = str(uuid4())
        album_source = str(uuid4())
        regular = _message(message_id=1, source_channel_id=regular_source)
        album_part = _message(message_id=2, grouped_id=9, source_channel_id=album_source)

        regular_report = asyncio.run(ingestion.ingest_update(message=regular, now=NOW))
        buffered_report = asyncio.run(ingestion.ingest_update(message=album_part, now=NOW))
        self.assertEqual(
            cursors.requests,
            [
                ("advance_latest", regular_source, 1),
                ("advance_last", regular_source, 1),
            ],
        )
        shutdown_report = asyncio.run(ingestion.flush_all())

        self.assertEqual(regular_report, IngestReport(logical_messages=1, component_messages=1))
        self.assertEqual(buffered_report, IngestReport(logical_messages=0, component_messages=0))
        self.assertEqual(shutdown_report, IngestReport(logical_messages=1, component_messages=1))
        self.assertEqual(service.history_batches, [(regular,), (album_part,)])
        self.assertEqual(
            cursors.requests[-2:],
            [
                ("advance_latest", album_source, 2),
                ("advance_last", album_source, 2),
            ],
        )

    def test_ingestion_failure_never_advances_realtime_cursors(self) -> None:
        cursors = _RecordingCursorRepository()
        ingestion = RealtimeTelegramIngestion(
            message_ingest_service=_RecordingIngestService(fail=True),
            media_group_buffer=MediaGroupAggregationBuffer(),
            cursor_service=SourceReconciliationService(cursors),
        )

        with self.assertRaisesRegex(RuntimeError, "ingest failed"):
            asyncio.run(ingestion.ingest_update(message=_message(message_id=1), now=NOW))

        self.assertEqual(cursors.requests, [])


class _RecordingIngestService:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.history_batches: list[tuple[TelegramMessage, ...]] = []

    async def ingest_history(self, messages: tuple[TelegramMessage, ...]) -> IngestReport:
        self.history_batches.append(messages)
        if self.fail:
            raise RuntimeError("ingest failed")
        normalized = normalize_telegram_messages(messages)
        return IngestReport(
            logical_messages=len(normalized),
            component_messages=sum(len(message.telegram_message_ids) for message in normalized),
        )


class _RecordingCursorRepository:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, int]] = []
        self.latest: dict[str, int] = {}
        self.last: dict[str, int] = {}

    async def advance_latest_seen_message_id(
        self, *, source_channel_id: str, telegram_message_id: int
    ) -> bool:
        self.requests.append(("advance_latest", source_channel_id, telegram_message_id))
        previous = self.latest.get(source_channel_id, 0)
        self.latest[source_channel_id] = max(previous, telegram_message_id)
        return telegram_message_id > previous

    async def advance_last_seen_message_id(
        self, *, source_channel_id: str, telegram_message_id: int
    ) -> bool:
        self.requests.append(("advance_last", source_channel_id, telegram_message_id))
        previous = self.last.get(source_channel_id, 0)
        self.last[source_channel_id] = max(previous, telegram_message_id)
        return telegram_message_id > previous

    async def get_latest_seen_message_id(self, *, source_channel_id: str) -> int | None:
        return self.latest.get(source_channel_id)

    async def get_last_seen_message_id(self, *, source_channel_id: str) -> int | None:
        return self.last.get(source_channel_id)


def _message(
    *,
    message_id: int,
    grouped_id: int | None = None,
    source_channel_id: str | None = None,
    text: str | None = None,
) -> TelegramMessage:
    return TelegramMessage(
        source_channel_id=source_channel_id or str(uuid4()),
        telegram_message_id=message_id,
        telegram_grouped_id=grouped_id,
        published_at=NOW,
        original_text=text,
    )


if __name__ == "__main__":
    unittest.main()
