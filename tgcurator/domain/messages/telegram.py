from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from itertools import chain
from uuid import UUID

from tgcurator.domain.messages.models import MediaAsset, MessageContent
from tgcurator.shared import DomainValidationError, ensure_aware


@dataclass(frozen=True, slots=True)
class TelegramMessage:
    """Platform-neutral Telegram message received from history or an Update.

    The DTO deliberately contains no Telethon object. Adapters translate Telegram-specific
    fields before crossing the application boundary, so history and real-time updates share
    exactly the same persistence path.
    """

    source_channel_id: str
    telegram_message_id: int
    sent_at: datetime
    text: str | None = None
    grouped_id: int | None = None
    media: tuple[MediaAsset, ...] = ()

    def __post_init__(self) -> None:
        _validate_uuid(self.source_channel_id, field="source_channel_id")
        if self.telegram_message_id <= 0:
            raise DomainValidationError("telegram_message_id must be greater than zero")
        if self.grouped_id is not None and self.grouped_id <= 0:
            raise DomainValidationError("grouped_id must be greater than zero when present")
        ensure_aware(self.sent_at, field="sent_at")
        MessageContent(text=self.text, media=self.media)


@dataclass(frozen=True, slots=True)
class NormalizedTelegramMessage:
    """One logical Message ready for idempotent persistence.

    A regular Telegram message owns one component ID. An album owns all of its actual Telegram
    component IDs and uses the smallest observed component ID as a deterministic anchor; database
    identity for an album is still `(source_channel_id, grouped_id)`, not that anchor.
    """

    source_channel_id: str
    telegram_anchor_message_id: int
    telegram_message_ids: tuple[int, ...]
    sent_at: datetime
    content: MessageContent
    telegram_group_id: int | None = None

    def __post_init__(self) -> None:
        _validate_uuid(self.source_channel_id, field="source_channel_id")
        if self.telegram_anchor_message_id <= 0:
            raise DomainValidationError("telegram_anchor_message_id must be greater than zero")
        if not self.telegram_message_ids:
            raise DomainValidationError("telegram_message_ids must not be empty")
        if tuple(sorted(self.telegram_message_ids)) != self.telegram_message_ids:
            raise DomainValidationError("telegram_message_ids must be strictly sorted")
        if len(set(self.telegram_message_ids)) != len(self.telegram_message_ids):
            raise DomainValidationError("telegram_message_ids must be unique")
        if self.telegram_anchor_message_id != self.telegram_message_ids[0]:
            raise DomainValidationError("telegram anchor must be the smallest component message ID")
        if any(message_id <= 0 for message_id in self.telegram_message_ids):
            raise DomainValidationError("telegram_message_ids must be greater than zero")
        if self.telegram_group_id is not None and self.telegram_group_id <= 0:
            raise DomainValidationError("telegram_group_id must be greater than zero when present")
        if self.telegram_group_id is None and len(self.telegram_message_ids) != 1:
            raise DomainValidationError("a non-grouped message must have exactly one component ID")
        ensure_aware(self.sent_at, field="sent_at")


def normalize_telegram_messages(
    messages: Iterable[TelegramMessage],
) -> tuple[NormalizedTelegramMessage, ...]:
    """Normalize one source batch into regular messages and Telegram-native media groups.

    Grouped messages are merged only when their source and Telegram `grouped_id` match.  The
    earliest non-empty caption by component message ID is selected deterministically; media stay
    in component-ID order.  Callers may feed a short aggregation-window batch for complete albums,
    while partial batches remain safely upsertable because their group identity is stable.
    """

    incoming = tuple(messages)
    by_component: dict[tuple[str, int], TelegramMessage] = {}
    groups: dict[tuple[str, int], list[TelegramMessage]] = defaultdict(list)
    regular: list[TelegramMessage] = []

    for message in incoming:
        component_key = (message.source_channel_id, message.telegram_message_id)
        if component_key in by_component:
            raise DomainValidationError(
                "a Telegram component message may occur only once in one ingestion batch"
            )
        by_component[component_key] = message
        if message.grouped_id is None:
            regular.append(message)
        else:
            groups[(message.source_channel_id, message.grouped_id)].append(message)

    normalized: list[NormalizedTelegramMessage] = []
    for message in regular:
        normalized.append(
            NormalizedTelegramMessage(
                source_channel_id=message.source_channel_id,
                telegram_anchor_message_id=message.telegram_message_id,
                telegram_message_ids=(message.telegram_message_id,),
                sent_at=message.sent_at,
                content=MessageContent(text=message.text, media=message.media),
            )
        )

    for (source_channel_id, grouped_id), parts in groups.items():
        ordered_parts = tuple(sorted(parts, key=lambda part: part.telegram_message_id))
        component_ids = tuple(part.telegram_message_id for part in ordered_parts)
        captions = (part.text for part in ordered_parts if part.text is not None)
        normalized.append(
            NormalizedTelegramMessage(
                source_channel_id=source_channel_id,
                telegram_anchor_message_id=component_ids[0],
                telegram_message_ids=component_ids,
                sent_at=min(part.sent_at for part in ordered_parts),
                content=MessageContent(
                    text=next(captions, None),
                    media=tuple(chain.from_iterable(part.media for part in ordered_parts)),
                ),
                telegram_group_id=grouped_id,
            )
        )

    return tuple(
        sorted(
            normalized,
            key=lambda message: (
                message.source_channel_id,
                message.telegram_anchor_message_id,
                message.telegram_group_id is not None,
            ),
        )
    )


def _validate_uuid(value: str, *, field: str) -> None:
    try:
        UUID(value)
    except (AttributeError, ValueError) as error:
        raise DomainValidationError(f"{field} must be a UUID") from error
