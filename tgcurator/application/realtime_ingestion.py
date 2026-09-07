from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from tgcurator.application.ingestion import MessageIngestService
from tgcurator.application.media_group_buffer import MediaGroupAggregationBuffer
from tgcurator.application.ports.ingestion import IngestReport
from tgcurator.domain.messages import TelegramMessage


@dataclass(slots=True)
class RealtimeTelegramIngestion:
    """Apply Telegram Updates through the same durable path as history ingestion."""

    message_ingest_service: MessageIngestService
    media_group_buffer: MediaGroupAggregationBuffer

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
        # A released album is a batch, so use the common normalization/upsert path.
        return await self.message_ingest_service.ingest_history(messages)
