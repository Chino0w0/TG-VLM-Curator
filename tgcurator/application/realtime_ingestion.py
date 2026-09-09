from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from tgcurator.application.ingestion import MessageIngestService
from tgcurator.application.media_group_buffer import MediaGroupAggregationBuffer
from tgcurator.application.ports.ingestion import IngestReport
from tgcurator.application.reconciliation import SourceReconciliationService
from tgcurator.domain.messages import TelegramMessage


@dataclass(slots=True)
class RealtimeTelegramIngestion:
    """Apply Telegram Updates through the same durable path as history ingestion."""

    message_ingest_service: MessageIngestService
    media_group_buffer: MediaGroupAggregationBuffer
    cursor_service: SourceReconciliationService

    async def ingest_update(self, *, message: TelegramMessage, now: datetime) -> IngestReport:
        ready = self.media_group_buffer.add(message=message, now=now)
        return await self._ingest_ready(messages=ready)

    async def flush_due(self, *, now: datetime) -> IngestReport:
        return await self._ingest_ready(messages=self.media_group_buffer.flush_due(now=now))

    async def flush_all(self) -> IngestReport:
        return await self._ingest_ready(messages=self.media_group_buffer.flush_all())

    async def _ingest_ready(self, *, messages: tuple[TelegramMessage, ...]) -> IngestReport:
        if not messages:
            return IngestReport(logical_messages=0, component_messages=0)
        report = await self.message_ingest_service.ingest_history(messages)
        greatest_by_source: dict[str, int] = {}
        for message in messages:
            greatest_by_source[message.source_channel_id] = max(
                message.telegram_message_id,
                greatest_by_source.get(message.source_channel_id, 0),
            )
        for source_channel_id, telegram_message_id in sorted(greatest_by_source.items()):
            await self.cursor_service.record_latest_seen_message(
                source_channel_id=source_channel_id,
                telegram_message_id=telegram_message_id,
            )
            await self.cursor_service.record_ingested_message(
                source_channel_id=source_channel_id,
                telegram_message_id=telegram_message_id,
            )
        return report
