from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from tgcurator.application import MessageIngestService
from tgcurator.application.ports.ingestion import TelegramMessageIngestRepository
from tgcurator.domain.messages import (
    MediaAsset,
    MediaKind,
    NormalizedTelegramMessage,
    TelegramMessage,
    normalize_telegram_messages,
)
from tgcurator.shared import DomainValidationError

NOW = datetime(2026, 9, 4, 12, tzinfo=UTC)


class FakeMessageIngestRepository(TelegramMessageIngestRepository):
    def __init__(self) -> None:
        self.messages: dict[tuple[str, int | None, int], NormalizedTelegramMessage] = {}
        self.calls: list[NormalizedTelegramMessage] = []

    async def upsert_message(self, *, message: NormalizedTelegramMessage) -> None:
        self.calls.append(message)
        identity = (
            message.source_channel_id,
            message.telegram_group_id,
            message.telegram_group_id or message.telegram_anchor_message_id,
        )
        existing = self.messages.get(identity)
        if existing is None:
            self.messages[identity] = message
            return
        merged_ids = tuple(
            sorted(set(existing.telegram_message_ids) | set(message.telegram_message_ids))
        )
        self.messages[identity] = NormalizedTelegramMessage(
            source_channel_id=message.source_channel_id,
            telegram_anchor_message_id=merged_ids[0],
            telegram_message_ids=merged_ids,
            sent_at=min(existing.sent_at, message.sent_at),
            content=message.content,
            telegram_group_id=message.telegram_group_id,
        )


class TelegramNormalizationTests(unittest.TestCase):
    def test_album_uses_group_identity_and_actual_sorted_component_ids(self) -> None:
        source_channel_id = str(uuid4())
        album = normalize_telegram_messages(
            (
                TelegramMessage(
                    source_channel_id=source_channel_id,
                    telegram_message_id=103,
                    grouped_id=701,
                    sent_at=NOW + timedelta(seconds=2),
                    media=(MediaAsset("asset-103", MediaKind.IMAGE),),
                ),
                TelegramMessage(
                    source_channel_id=source_channel_id,
                    telegram_message_id=101,
                    grouped_id=701,
                    sent_at=NOW,
                    text="album caption",
                    media=(MediaAsset("asset-101", MediaKind.VIDEO),),
                ),
            )
        )

        self.assertEqual(len(album), 1)
        message = album[0]
        self.assertEqual(message.telegram_group_id, 701)
        self.assertEqual(message.telegram_anchor_message_id, 101)
        self.assertEqual(message.telegram_message_ids, (101, 103))
        self.assertEqual(message.sent_at, NOW)
        self.assertEqual(message.content.text, "album caption")
        self.assertEqual(
            [asset.asset_id for asset in message.content.media], ["asset-101", "asset-103"]
        )

    def test_same_grouped_id_from_distinct_sources_does_not_merge(self) -> None:
        first_source = str(uuid4())
        second_source = str(uuid4())

        normalized = normalize_telegram_messages(
            (
                TelegramMessage(first_source, 7, NOW, grouped_id=99),
                TelegramMessage(second_source, 7, NOW, grouped_id=99),
            )
        )

        self.assertEqual(len(normalized), 2)
        self.assertEqual(
            {message.source_channel_id for message in normalized}, {first_source, second_source}
        )

    def test_duplicate_component_in_one_batch_is_rejected(self) -> None:
        source_channel_id = str(uuid4())
        message = TelegramMessage(source_channel_id, 7, NOW)

        with self.assertRaisesRegex(DomainValidationError, "only once"):
            normalize_telegram_messages((message, message))

    def test_duplicate_asset_across_album_parts_is_rejected(self) -> None:
        source_channel_id = str(uuid4())
        with self.assertRaisesRegex(DomainValidationError, "duplicate asset_id"):
            normalize_telegram_messages(
                (
                    TelegramMessage(
                        source_channel_id,
                        7,
                        NOW,
                        grouped_id=99,
                        media=(MediaAsset("same", MediaKind.IMAGE),),
                    ),
                    TelegramMessage(
                        source_channel_id,
                        8,
                        NOW,
                        grouped_id=99,
                        media=(MediaAsset("same", MediaKind.IMAGE),),
                    ),
                )
            )


class MessageIngestServiceTests(unittest.TestCase):
    def test_history_and_update_share_an_idempotent_repository_path(self) -> None:
        repository = FakeMessageIngestRepository()
        service = MessageIngestService(repository)
        source_channel_id = str(uuid4())
        incoming = TelegramMessage(
            source_channel_id=source_channel_id,
            telegram_message_id=55,
            sent_at=NOW,
            text="same Telegram message",
        )

        history_report = asyncio.run(service.ingest_history((incoming,)))
        update_report = asyncio.run(service.ingest_update(incoming))

        self.assertEqual(history_report.logical_messages, 1)
        self.assertEqual(history_report.component_messages, 1)
        self.assertEqual(update_report.logical_messages, 1)
        self.assertEqual(len(repository.calls), 2)
        self.assertEqual(len(repository.messages), 1)

    def test_later_album_part_updates_the_same_logical_group(self) -> None:
        repository = FakeMessageIngestRepository()
        service = MessageIngestService(repository)
        source_channel_id = str(uuid4())

        asyncio.run(
            service.ingest_history((TelegramMessage(source_channel_id, 10, NOW, grouped_id=800),))
        )
        asyncio.run(
            service.ingest_update(
                TelegramMessage(source_channel_id, 11, NOW + timedelta(seconds=1), grouped_id=800)
            )
        )

        stored = next(iter(repository.messages.values()))
        self.assertEqual(stored.telegram_message_ids, (10, 11))
        self.assertEqual(stored.telegram_group_id, 800)


if __name__ == "__main__":
    unittest.main()
