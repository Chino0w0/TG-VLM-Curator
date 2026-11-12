from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from apps.telegram_ingestor import TelegramIngestorRuntime
from tgcurator.application import (
    MediaGroupAggregationBuffer,
    MessageIngestService,
    RealtimeTelegramIngestion,
    ReconnectTelegramReconciliation,
    SourceMessageLifecycleService,
    SourceReconciliationService,
)
from tgcurator.application.ports.processing import LatestMessageBoundary
from tgcurator.domain.messages import (
    MediaAsset,
    MediaKind,
    NormalizedTelegramMessage,
    TelegramMessage,
)

NOW = datetime(2026, 9, 20, 14, 0, tzinfo=UTC)
SOURCE_ID = str(uuid4())


@dataclass
class FakeDatabase:
    disposed: bool = False

    async def dispose(self) -> None:
        self.disposed = True


@dataclass
class FakeIngestRepository:
    upserted: list[NormalizedTelegramMessage] = field(default_factory=list)

    async def upsert_message(self, *, message: NormalizedTelegramMessage) -> None:
        self.upserted.append(message)


@dataclass
class FakeCursorRepository:
    latest_seen: dict[str, int] = field(default_factory=dict)
    last_seen: dict[str, int] = field(default_factory=dict)

    async def advance_latest_seen_message_id(
        self, *, source_channel_id: str, telegram_message_id: int
    ) -> bool:
        prev = self.latest_seen.get(source_channel_id, 0)
        if telegram_message_id > prev:
            self.latest_seen[source_channel_id] = telegram_message_id
            return True
        return False

    async def advance_last_seen_message_id(
        self, *, source_channel_id: str, telegram_message_id: int
    ) -> bool:
        prev = self.last_seen.get(source_channel_id, 0)
        if telegram_message_id > prev:
            self.last_seen[source_channel_id] = telegram_message_id
            return True
        return False

    async def get_latest_seen_message_id(self, *, source_channel_id: str) -> int | None:
        return self.latest_seen.get(source_channel_id)

    async def get_last_seen_message_id(self, *, source_channel_id: str) -> int | None:
        return self.last_seen.get(source_channel_id)


@dataclass
class FakeLifecycleRepository:
    edits: list[tuple[str, int, str | None]] = field(default_factory=list)
    deletions: list[tuple[str, int]] = field(default_factory=list)

    async def record_edit(
        self,
        *,
        source_channel_id: str,
        telegram_message_id: int,
        original_text: str | None,
        telegram_metadata: dict[str, Any],
        media: tuple[MediaAsset, ...],
        edited_at: datetime,
    ) -> bool:
        _ = (telegram_metadata, media, edited_at)
        self.edits.append((source_channel_id, telegram_message_id, original_text))
        return True

    async def record_deletion(
        self,
        *,
        source_channel_id: str,
        telegram_message_id: int,
        deleted_at: datetime,
    ) -> bool:
        _ = deleted_at
        self.deletions.append((source_channel_id, telegram_message_id))
        return True


@dataclass
class FakeTelegramGateway:
    messages: tuple[TelegramMessage, ...]

    async def newest_message(self, *, source_channel_id: str) -> LatestMessageBoundary | None:
        scoped = [m for m in self.messages if m.source_channel_id == source_channel_id]
        if not scoped:
            return None
        top = max(scoped, key=lambda m: m.telegram_message_id)
        return LatestMessageBoundary(
            message_id=top.telegram_message_id,
            published_at=top.published_at,
        )

    async def resolve_earliest_message_at_or_after(
        self, *, source_channel_id: str, published_at: datetime
    ) -> int | None:
        _ = (source_channel_id, published_at)
        return None

    async def resolve_latest_message_at_or_before(
        self, *, source_channel_id: str, published_at: datetime
    ) -> int | None:
        _ = (source_channel_id, published_at)
        return None

    async def fetch_history(
        self,
        *,
        source_channel_id: str,
        from_message_id_exclusive: int,
        to_message_id_inclusive: int,
    ) -> tuple[TelegramMessage, ...]:
        return tuple(
            m
            for m in self.messages
            if m.source_channel_id == source_channel_id
            and from_message_id_exclusive < m.telegram_message_id <= to_message_id_inclusive
        )


def make_tg_message(
    msg_id: int,
    *,
    text: str | None = "hello",
    grouped_id: int | None = None,
) -> TelegramMessage:
    media = (
        (
            MediaAsset(
                asset_id=f"img-{msg_id}",
                kind=MediaKind.IMAGE,
                source_telegram_message_id=msg_id,
            ),
        )
        if grouped_id is not None
        else ()
    )
    return TelegramMessage(
        source_channel_id=SOURCE_ID,
        telegram_message_id=msg_id,
        published_at=NOW,
        original_text=text,
        media=media,
        telegram_grouped_id=grouped_id,
    )


class TelegramIngestorRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_realtime_album_buffering_reconnect_reconciliation_lifecycle_and_shutdown(
        self,
    ) -> None:
        db = FakeDatabase()
        ingest_repo = FakeIngestRepository()
        cursor_repo = FakeCursorRepository()
        lifecycle_repo = FakeLifecycleRepository()
        gateway = FakeTelegramGateway(
            messages=(
                make_tg_message(10, text="missed-1"),
                make_tg_message(11, text="missed-2"),
            )
        )
        ingest_service = MessageIngestService(repository=ingest_repo)
        cursor_service = SourceReconciliationService(repository=cursor_repo)
        buffer = MediaGroupAggregationBuffer(aggregation_window=timedelta(seconds=3))
        runtime = TelegramIngestorRuntime(
            database=db,  # type: ignore[arg-type]
            realtime_ingestion=RealtimeTelegramIngestion(
                message_ingest_service=ingest_service,
                media_group_buffer=buffer,
                cursor_service=cursor_service,
            ),
            source_lifecycle=SourceMessageLifecycleService(repository=lifecycle_repo),
            reconciliation=ReconnectTelegramReconciliation(
                telegram_gateway=gateway,
                message_ingest_service=ingest_service,
                cursor_service=cursor_service,
            ),
        )

        # Initially degraded while disconnected from Telegram.
        self.assertEqual(runtime.health_snapshot().status, "degraded")
        runtime.mark_connected(connected=True, now=NOW)
        self.assertEqual(runtime.health_snapshot().status, "ok")

        # Reconnect reconciliation catches up missed window (0, 11].
        reports = await runtime.reconcile_sources(source_channel_ids=(SOURCE_ID,), now=NOW)
        self.assertEqual(reports[SOURCE_ID].logical_messages, 2)
        self.assertEqual(cursor_repo.last_seen[SOURCE_ID], 11)

        # Two album parts arrive at NOW+1s; held in buffer until window expires or shutdown flushes!
        p1 = await runtime.handle_new_message(
            message=make_tg_message(20, text="album caption", grouped_id=777),
            now=NOW + timedelta(seconds=1),
        )
        p2 = await runtime.handle_new_message(
            message=make_tg_message(21, text=None, grouped_id=777),
            now=NOW + timedelta(seconds=2),
        )
        self.assertEqual(p1.logical_messages, 0)
        self.assertEqual(p2.logical_messages, 0)
        self.assertEqual(runtime.health_snapshot().pending_media_groups, 1)

        # Edit and deletion events delegate cleanly to lifecycle service.
        self.assertTrue(
            await runtime.handle_message_edit(
                source_channel_id=SOURCE_ID,
                telegram_message_id=10,
                original_text="edited text",
                telegram_metadata={},
                media=(),
                edited_at=NOW + timedelta(seconds=2),
            )
        )
        self.assertTrue(
            await runtime.handle_message_deletion(
                source_channel_id=SOURCE_ID,
                telegram_message_id=11,
                deleted_at=NOW + timedelta(seconds=3),
            )
        )
        self.assertEqual(len(lifecycle_repo.edits), 1)
        self.assertEqual(len(lifecycle_repo.deletions), 1)

        # Graceful shutdown flushes the buffered 2-part album as a single normalized LogicalMessage!
        flushed = await runtime.shutdown()
        self.assertEqual(flushed.logical_messages, 1)
        self.assertEqual(flushed.component_messages, 2)
        self.assertEqual(ingest_repo.upserted[-1].telegram_message_ids, (20, 21))
        self.assertEqual(cursor_repo.last_seen[SOURCE_ID], 21)
        self.assertTrue(db.disposed)


if __name__ == "__main__":
    unittest.main()
