from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from tgcurator.application.ingestion import MessageIngestService
from tgcurator.application.ports.contracts import TelegramGateway
from tgcurator.application.ports.reconciliation import SourceReconciliationCursorRepository
from tgcurator.domain.messages import TelegramMessage
from tgcurator.shared import DomainValidationError


@dataclass(frozen=True, slots=True)
class SourceReconciliationReport:
    """Outcome of one reconnect scan over a frozen Telegram message-ID window."""

    from_message_id_exclusive: int
    to_message_id_inclusive: int | None
    logical_messages: int
    component_messages: int
    last_seen_advanced: bool


@dataclass(slots=True)
class SourceReconciliationService:
    """Validated monotonic cursor operations shared by realtime and reconnect ingestion."""

    repository: SourceReconciliationCursorRepository

    async def record_latest_seen_message(
        self, *, source_channel_id: str, telegram_message_id: int
    ) -> bool:
        self._validate_source_channel_id(source_channel_id)
        self._validate_message_id(telegram_message_id)
        return await self.repository.advance_latest_seen_message_id(
            source_channel_id=source_channel_id,
            telegram_message_id=telegram_message_id,
        )

    async def record_ingested_message(
        self, *, source_channel_id: str, telegram_message_id: int
    ) -> bool:
        self._validate_source_channel_id(source_channel_id)
        self._validate_message_id(telegram_message_id)
        return await self.repository.advance_last_seen_message_id(
            source_channel_id=source_channel_id,
            telegram_message_id=telegram_message_id,
        )

    async def latest_seen_message_id(self, *, source_channel_id: str) -> int | None:
        self._validate_source_channel_id(source_channel_id)
        return await self.repository.get_latest_seen_message_id(source_channel_id=source_channel_id)

    async def last_seen_message_id(self, *, source_channel_id: str) -> int | None:
        self._validate_source_channel_id(source_channel_id)
        return await self.repository.get_last_seen_message_id(source_channel_id=source_channel_id)

    @staticmethod
    def _validate_source_channel_id(source_channel_id: str) -> None:
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


@dataclass(slots=True)
class ReconnectTelegramReconciliation:
    """Freeze, validate, ingest, and acknowledge one source reconnect history window."""

    telegram_gateway: TelegramGateway
    message_ingest_service: MessageIngestService
    cursor_service: SourceReconciliationService

    async def reconcile(self, *, source_channel_id: str) -> SourceReconciliationReport:
        SourceReconciliationService._validate_source_channel_id(source_channel_id)
        last_seen = await self.cursor_service.last_seen_message_id(
            source_channel_id=source_channel_id
        )
        from_message_id_exclusive = last_seen or 0
        newest = await self.telegram_gateway.newest_message(source_channel_id=source_channel_id)
        if newest is None:
            return SourceReconciliationReport(
                from_message_id_exclusive=from_message_id_exclusive,
                to_message_id_inclusive=None,
                logical_messages=0,
                component_messages=0,
                last_seen_advanced=False,
            )
        SourceReconciliationService._validate_message_id(newest.message_id)
        await self.cursor_service.record_latest_seen_message(
            source_channel_id=source_channel_id,
            telegram_message_id=newest.message_id,
        )
        if newest.message_id <= from_message_id_exclusive:
            return SourceReconciliationReport(
                from_message_id_exclusive=from_message_id_exclusive,
                to_message_id_inclusive=newest.message_id,
                logical_messages=0,
                component_messages=0,
                last_seen_advanced=False,
            )

        frozen_upper = newest.message_id
        messages = await self.telegram_gateway.fetch_history(
            source_channel_id=source_channel_id,
            from_message_id_exclusive=from_message_id_exclusive,
            to_message_id_inclusive=frozen_upper,
        )
        self._validate_history_window(
            messages=messages,
            source_channel_id=source_channel_id,
            from_message_id_exclusive=from_message_id_exclusive,
            to_message_id_inclusive=frozen_upper,
        )
        report = await self.message_ingest_service.ingest_history(messages)
        advanced = await self.cursor_service.record_ingested_message(
            source_channel_id=source_channel_id,
            telegram_message_id=frozen_upper,
        )
        return SourceReconciliationReport(
            from_message_id_exclusive=from_message_id_exclusive,
            to_message_id_inclusive=frozen_upper,
            logical_messages=report.logical_messages,
            component_messages=report.component_messages,
            last_seen_advanced=advanced,
        )

    @staticmethod
    def _validate_history_window(
        *,
        messages: tuple[TelegramMessage, ...],
        source_channel_id: str,
        from_message_id_exclusive: int,
        to_message_id_inclusive: int,
    ) -> None:
        for message in messages:
            if message.source_channel_id != source_channel_id:
                raise DomainValidationError(
                    "Telegram reconciliation returned a message for another source channel"
                )
            if not (
                from_message_id_exclusive < message.telegram_message_id <= to_message_id_inclusive
            ):
                raise DomainValidationError(
                    "Telegram reconciliation returned a message outside the frozen window"
                )
