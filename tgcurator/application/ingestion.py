from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from tgcurator.application.ports.ingestion import IngestReport, TelegramMessageIngestRepository
from tgcurator.domain.messages import TelegramMessage, normalize_telegram_messages


@dataclass(slots=True)
class MessageIngestService:
    """Single, idempotent business path for Telegram history and real-time updates.

    Adapters perform Telegram I/O before calling this service.  The repository then owns one short
    transaction per normalized logical message; neither history scans nor Update handlers get a
    separate persistence model.
    """

    repository: TelegramMessageIngestRepository

    async def ingest_history(self, messages: Iterable[TelegramMessage]) -> IngestReport:
        return await self._ingest(messages)

    async def ingest_update(self, message: TelegramMessage) -> IngestReport:
        return await self._ingest((message,))

    async def _ingest(self, messages: Iterable[TelegramMessage]) -> IngestReport:
        normalized = normalize_telegram_messages(messages)
        for message in normalized:
            await self.repository.upsert_message(message=message)
        return IngestReport(
            logical_messages=len(normalized),
            component_messages=sum(len(message.telegram_message_ids) for message in normalized),
        )
