from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from itertools import chain
from typing import Any
from uuid import UUID

from tgcurator.domain.messages.models import MediaAsset, MessageContent
from tgcurator.shared import DomainValidationError, ensure_aware


@dataclass(frozen=True, slots=True)
class TelegramMessage:
    """Platform-neutral Telegram message received from history or an Update."""

    source_channel_id: str
    telegram_message_id: int
    published_at: datetime
    edited_at: datetime | None = None
    original_text: str | None = None
    telegram_grouped_id: int | None = None
    telegram_metadata: Mapping[str, Any] | None = None
    media: tuple[MediaAsset, ...] = ()

    def __post_init__(self) -> None:
        _validate_uuid(self.source_channel_id, field="source_channel_id")
        _validate_message_id(self.telegram_message_id, field="telegram_message_id")
        if self.telegram_grouped_id is not None:
            _validate_message_id(self.telegram_grouped_id, field="telegram_grouped_id")
        ensure_aware(self.published_at, field="published_at")
        if self.edited_at is not None:
            ensure_aware(self.edited_at, field="edited_at")
            if self.edited_at < self.published_at:
                raise DomainValidationError("edited_at must not be before published_at")
        if self.original_text is not None and not isinstance(self.original_text, str):
            raise DomainValidationError("original_text must be a string or None")
        metadata = {} if self.telegram_metadata is None else dict(self.telegram_metadata)
        _validate_json_value(metadata, field="telegram_metadata")
        object.__setattr__(self, "telegram_metadata", metadata)
        MessageContent(text=self.original_text, media=self.media)

    def as_part(self) -> TelegramMessagePart:
        return TelegramMessagePart(
            telegram_message_id=self.telegram_message_id,
            published_at=self.published_at,
            edited_at=self.edited_at,
            original_text=self.original_text,
            telegram_metadata=dict(self.telegram_metadata or {}),
            media=self.media,
        )


@dataclass(frozen=True, slots=True)
class TelegramMessagePart:
    """One retained Telegram component of a regular message or native media group."""

    telegram_message_id: int
    published_at: datetime
    edited_at: datetime | None = None
    original_text: str | None = None
    telegram_metadata: Mapping[str, Any] | None = None
    media: tuple[MediaAsset, ...] = ()

    def __post_init__(self) -> None:
        _validate_message_id(self.telegram_message_id, field="telegram_message_id")
        ensure_aware(self.published_at, field="published_at")
        if self.edited_at is not None:
            ensure_aware(self.edited_at, field="edited_at")
            if self.edited_at < self.published_at:
                raise DomainValidationError("edited_at must not be before published_at")
        if self.original_text is not None and not isinstance(self.original_text, str):
            raise DomainValidationError("original_text must be a string or None")
        metadata = {} if self.telegram_metadata is None else dict(self.telegram_metadata)
        _validate_json_value(metadata, field="telegram_metadata")
        object.__setattr__(self, "telegram_metadata", metadata)
        MessageContent(text=self.original_text, media=self.media)


@dataclass(frozen=True, slots=True)
class NormalizedTelegramMessage:
    """One logical Message ready for idempotent persistence."""

    source_channel_id: str
    primary_telegram_message_id: int
    telegram_message_ids: tuple[int, ...]
    published_at: datetime
    edited_at: datetime | None
    original_text: str | None
    telegram_grouped_id: int | None
    telegram_metadata: Mapping[str, Any]
    parts: tuple[TelegramMessagePart, ...]
    media: tuple[MediaAsset, ...]

    def __post_init__(self) -> None:
        _validate_uuid(self.source_channel_id, field="source_channel_id")
        _validate_message_id(self.primary_telegram_message_id, field="primary_telegram_message_id")
        if not self.telegram_message_ids:
            raise DomainValidationError("telegram_message_ids must not be empty")
        if tuple(sorted(self.telegram_message_ids)) != self.telegram_message_ids:
            raise DomainValidationError("telegram_message_ids must be strictly sorted")
        if len(set(self.telegram_message_ids)) != len(self.telegram_message_ids):
            raise DomainValidationError("telegram_message_ids must be unique")
        if self.primary_telegram_message_id != self.telegram_message_ids[0]:
            raise DomainValidationError("primary Telegram message ID must be the smallest part ID")
        for message_id in self.telegram_message_ids:
            _validate_message_id(message_id, field="telegram_message_ids")
        if self.telegram_grouped_id is not None:
            _validate_message_id(self.telegram_grouped_id, field="telegram_grouped_id")
        elif len(self.telegram_message_ids) != 1:
            raise DomainValidationError("a non-grouped message must have exactly one component ID")
        ensure_aware(self.published_at, field="published_at")
        if self.edited_at is not None:
            ensure_aware(self.edited_at, field="edited_at")
            if self.edited_at < self.published_at:
                raise DomainValidationError("edited_at must not be before published_at")
        if self.original_text is not None and not isinstance(self.original_text, str):
            raise DomainValidationError("original_text must be a string or None")
        if tuple(part.telegram_message_id for part in self.parts) != self.telegram_message_ids:
            raise DomainValidationError("parts must match telegram_message_ids in sorted order")
        _validate_json_value(dict(self.telegram_metadata), field="telegram_metadata")
        MessageContent(text=self.original_text, media=self.media)

    @property
    def content(self) -> MessageContent:
        return MessageContent(text=self.original_text, media=self.media)

    @property
    def media_count(self) -> int:
        return len(self.media)

    @property
    def visual_fingerprint(self) -> str | None:
        return self.content.visual_fingerprint


def normalize_telegram_messages(
    messages: Iterable[TelegramMessage],
) -> tuple[NormalizedTelegramMessage, ...]:
    """Normalize regular messages and Telegram-native media groups deterministically."""

    by_component: dict[tuple[str, int], TelegramMessage] = {}
    groups: dict[tuple[str, int], list[TelegramMessage]] = defaultdict(list)
    regular: list[TelegramMessage] = []
    for message in messages:
        if not isinstance(message, TelegramMessage):
            raise DomainValidationError("ingestion values must be TelegramMessage instances")
        component_key = (message.source_channel_id, message.telegram_message_id)
        if component_key in by_component:
            raise DomainValidationError(
                "a Telegram component message may occur only once in one ingestion batch"
            )
        by_component[component_key] = message
        if message.telegram_grouped_id is None:
            regular.append(message)
        else:
            groups[(message.source_channel_id, message.telegram_grouped_id)].append(message)

    normalized = [
        _normalize_parts(message.source_channel_id, None, (message,)) for message in regular
    ]
    normalized.extend(
        _normalize_parts(source_channel_id, grouped_id, tuple(parts))
        for (source_channel_id, grouped_id), parts in groups.items()
    )
    return tuple(
        sorted(
            normalized,
            key=lambda message: (
                message.source_channel_id,
                message.primary_telegram_message_id,
                message.telegram_grouped_id is not None,
            ),
        )
    )


def normalized_from_parts(
    *,
    source_channel_id: str,
    telegram_grouped_id: int | None,
    parts: Iterable[TelegramMessagePart],
) -> NormalizedTelegramMessage:
    """Recompute a logical message from all durable component snapshots."""

    messages = tuple(
        TelegramMessage(
            source_channel_id=source_channel_id,
            telegram_message_id=part.telegram_message_id,
            published_at=part.published_at,
            edited_at=part.edited_at,
            original_text=part.original_text,
            telegram_grouped_id=telegram_grouped_id,
            telegram_metadata=part.telegram_metadata,
            media=part.media,
        )
        for part in parts
    )
    return _normalize_parts(source_channel_id, telegram_grouped_id, messages)


def _normalize_parts(
    source_channel_id: str,
    telegram_grouped_id: int | None,
    messages: tuple[TelegramMessage, ...],
) -> NormalizedTelegramMessage:
    ordered = tuple(sorted(messages, key=lambda item: item.telegram_message_id))
    if not ordered:
        raise DomainValidationError("a logical Telegram message must contain at least one part")
    part_values = tuple(message.as_part() for message in ordered)
    component_ids = tuple(message.telegram_message_id for message in ordered)
    media = tuple(chain.from_iterable(message.media for message in ordered))
    original_text = next(
        (message.original_text for message in ordered if message.original_text is not None),
        None,
    )
    MessageContent(text=original_text, media=media)
    edited_values = tuple(message.edited_at for message in ordered if message.edited_at is not None)
    return NormalizedTelegramMessage(
        source_channel_id=source_channel_id,
        primary_telegram_message_id=component_ids[0],
        telegram_message_ids=component_ids,
        published_at=min(message.published_at for message in ordered),
        edited_at=max(edited_values) if edited_values else None,
        original_text=original_text,
        telegram_grouped_id=telegram_grouped_id,
        telegram_metadata={
            "parts": [
                {
                    "telegram_message_id": message.telegram_message_id,
                    "metadata": dict(message.telegram_metadata or {}),
                }
                for message in ordered
            ]
        },
        parts=part_values,
        media=media,
    )


def _validate_uuid(value: str, *, field: str) -> None:
    try:
        UUID(value)
    except (AttributeError, TypeError, ValueError) as error:
        raise DomainValidationError(f"{field} must be a UUID") from error


def _validate_message_id(value: int, *, field: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise DomainValidationError(f"{field} must be a positive integer")


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
