from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from tgcurator.application.ports.reconciliation import SourceReconciliationCursorRepository
from tgcurator.shared import DomainValidationError


@dataclass(slots=True)
class SourceReconciliationService:
    """Application boundary for a durable source-channel reconciliation cursor.

    Update adapters call this only after their message has successfully entered the common
    idempotent ingestion path. The database owns monotonicity, so duplicate or out-of-order
    deliveries cannot move the cursor backwards.
    """

    repository: SourceReconciliationCursorRepository

    async def record_ingested_message(
        self, *, source_channel_id: str, telegram_message_id: int
    ) -> bool:
        self._validate_source_channel_id(source_channel_id)
        self._validate_message_id(telegram_message_id)
        return await self.repository.advance_last_seen_message_id(
            source_channel_id=source_channel_id,
            telegram_message_id=telegram_message_id,
        )

    async def last_seen_message_id(self, *, source_channel_id: str) -> int | None:
        self._validate_source_channel_id(source_channel_id)
        return await self.repository.get_last_seen_message_id(source_channel_id=source_channel_id)

    @staticmethod
    def _validate_source_channel_id(source_channel_id: str) -> None:
        if not source_channel_id.strip():
            raise DomainValidationError("source_channel_id must not be blank")
        try:
            UUID(source_channel_id)
        except (AttributeError, TypeError, ValueError) as error:
            raise DomainValidationError("source_channel_id must be a UUID") from error

    @staticmethod
    def _validate_message_id(telegram_message_id: int) -> None:
        if (
            not isinstance(telegram_message_id, int)
            or isinstance(telegram_message_id, bool)
            or telegram_message_id <= 0
        ):
            raise DomainValidationError("telegram_message_id must be a positive integer")
