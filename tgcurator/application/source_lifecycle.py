from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from tgcurator.application.ports.source_lifecycle import SourceMessageLifecycleRepository
from tgcurator.domain.messages import MediaAsset, MessageContent
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
        original_text: str | None,
        telegram_metadata: Mapping[str, Any],
        media: tuple[MediaAsset, ...],
        edited_at: datetime,
    ) -> bool:
        self._validate_source_message(
            source_channel_id=source_channel_id,
            telegram_message_id=telegram_message_id,
        )
        if not isinstance(telegram_metadata, Mapping):
            raise DomainValidationError("telegram_metadata must be a mapping")
        normalized_metadata = dict(telegram_metadata)
        _validate_json_value(normalized_metadata, field="telegram_metadata")
        MessageContent(text=original_text, media=media)
        ensure_aware(edited_at, field="edited_at")
        return await self.repository.record_edit(
            source_channel_id=source_channel_id,
            telegram_message_id=telegram_message_id,
            original_text=original_text,
            telegram_metadata=normalized_metadata,
            media=media,
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


def _validate_json_value(value: Any, *, field: str) -> None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_json_value(item, field=field)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise DomainValidationError(f"{field} keys must be strings")
            _validate_json_value(item, field=field)
        return
    raise DomainValidationError(f"{field} must contain only JSON-safe values")
