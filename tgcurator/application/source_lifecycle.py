from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from tgcurator.application.ports.source_lifecycle import SourceMessageLifecycleRepository
from tgcurator.shared import DomainValidationError, ensure_aware


@dataclass(slots=True)
class SourceMessageLifecycleService:
    """Preserve source edits/deletions while retaining message and archive history.

    A Telegram Update adapter supplies the platform-specific event. This use case validates it and
    delegates one short database transaction; it never calls Telegram, archive storage, or VLMs.
    """

    repository: SourceMessageLifecycleRepository

    async def record_edit(
        self,
        *,
        source_channel_id: str,
        telegram_message_id: int,
        text: str | None,
        edited_at: datetime,
    ) -> bool:
        self._validate_source_message(
            source_channel_id=source_channel_id,
            telegram_message_id=telegram_message_id,
        )
        if text is not None and not isinstance(text, str):
            raise DomainValidationError("text must be a string or None")
        ensure_aware(edited_at, field="edited_at")
        return await self.repository.record_edit(
            source_channel_id=source_channel_id,
            telegram_message_id=telegram_message_id,
            text=text,
            edited_at=edited_at,
        )

    async def record_deletion(
        self,
        *,
        source_channel_id: str,
        telegram_message_id: int,
        deleted_at: datetime,
    ) -> bool:
        self._validate_source_message(
            source_channel_id=source_channel_id,
            telegram_message_id=telegram_message_id,
        )
        ensure_aware(deleted_at, field="deleted_at")
        return await self.repository.record_deletion(
            source_channel_id=source_channel_id,
            telegram_message_id=telegram_message_id,
            deleted_at=deleted_at,
        )

    @staticmethod
    def _validate_source_message(*, source_channel_id: str, telegram_message_id: int) -> None:
        try:
            UUID(source_channel_id)
        except (AttributeError, TypeError, ValueError) as error:
            raise DomainValidationError("source_channel_id must be a UUID") from error
        if (
            not isinstance(telegram_message_id, int)
            or isinstance(telegram_message_id, bool)
            or telegram_message_id <= 0
        ):
            raise DomainValidationError("telegram_message_id must be a positive integer")
