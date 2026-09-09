from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from tgcurator.application.ports.processing import LatestMessageBoundary
from tgcurator.domain.messages import MediaAsset, MediaKind, TelegramMessage
from tgcurator.shared import DomainValidationError, ensure_aware


class TelethonHistoryClient(Protocol):
    def iter_messages(
        self,
        entity: int | str,
        *,
        min_id: int = 0,
        max_id: int = 0,
        reverse: bool = False,
        limit: int | None = None,
    ) -> AsyncIterator[Any]: ...


class TelegramSourcePeerResolver(Protocol):
    async def resolve_peer(self, *, source_channel_id: str) -> int | str: ...


@dataclass(frozen=True, slots=True)
class StaticTelegramSourcePeerResolver:
    """Temporary composition adapter until source-channel lookup is persisted separately."""

    peers_by_source_id: Mapping[str, int | str]

    async def resolve_peer(self, *, source_channel_id: str) -> int | str:
        try:
            return self.peers_by_source_id[source_channel_id]
        except KeyError as error:
            raise DomainValidationError("source channel has no configured Telegram peer") from error


class TelethonMessageMapper:
    """Convert Telethon-shaped messages to the platform-neutral application DTO."""

    _JSON_METADATA_FIELDS = (
        "post",
        "silent",
        "legacy",
        "edit_hide",
        "pinned",
        "noforwards",
        "views",
        "forwards",
        "via_bot_id",
        "post_author",
    )

    def map(self, *, source_channel_id: str, message: Any) -> TelegramMessage:
        message_id = getattr(message, "id", None)
        published_at = getattr(message, "date", None)
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            raise DomainValidationError("Telethon message is missing an integer id")
        if not isinstance(published_at, datetime):
            raise DomainValidationError("Telethon message is missing a timestamp")
        ensure_aware(published_at, field="Telethon message date")

        edited_at = getattr(message, "edit_date", None)
        if edited_at is not None:
            if not isinstance(edited_at, datetime):
                raise DomainValidationError("Telethon edit_date must be a datetime when present")
            ensure_aware(edited_at, field="Telethon message edit_date")

        telegram_grouped_id = getattr(message, "grouped_id", None)
        if telegram_grouped_id is not None and (
            not isinstance(telegram_grouped_id, int) or isinstance(telegram_grouped_id, bool)
        ):
            raise DomainValidationError("Telethon grouped_id must be an integer when present")
        original_text = getattr(message, "raw_text", None)
        if original_text is not None and not isinstance(original_text, str):
            raise DomainValidationError("Telethon raw_text must be a string or None")

        return TelegramMessage(
            source_channel_id=source_channel_id,
            telegram_message_id=message_id,
            published_at=published_at,
            edited_at=edited_at,
            original_text=original_text,
            telegram_grouped_id=telegram_grouped_id,
            telegram_metadata=self._metadata(message),
            media=self._media(source_channel_id=source_channel_id, message=message),
        )

    @classmethod
    def _metadata(cls, message: Any) -> dict[str, str | int | bool | None]:
        metadata: dict[str, str | int | bool | None] = {}
        for field in cls._JSON_METADATA_FIELDS:
            value = getattr(message, field, None)
            if value is None or isinstance(value, (str, int, bool)):
                metadata[field] = value
        return metadata

    @staticmethod
    def _media(*, source_channel_id: str, message: Any) -> tuple[MediaAsset, ...]:
        kind: MediaKind | None = None
        if getattr(message, "photo", None) is not None:
            kind = MediaKind.IMAGE
        elif getattr(message, "video", None) is not None:
            kind = MediaKind.VIDEO
        elif getattr(message, "audio", None) is not None:
            kind = MediaKind.AUDIO
        elif getattr(message, "document", None) is not None:
            kind = MediaKind.DOCUMENT
        if kind is None:
            return ()
        return (
            MediaAsset(
                asset_id=f"telegram:{source_channel_id}:{message.id}:0",
                kind=kind,
                source_telegram_message_id=message.id,
            ),
        )


@dataclass(slots=True)
class TelethonReadGateway:
    """Read-only Telethon adapter for immutable Telegram message-ID windows."""

    client: TelethonHistoryClient
    source_peer_resolver: TelegramSourcePeerResolver
    message_mapper: TelethonMessageMapper

    async def newest_message(self, *, source_channel_id: str) -> LatestMessageBoundary | None:
        peer = await self.source_peer_resolver.resolve_peer(source_channel_id=source_channel_id)
        async for raw_message in self.client.iter_messages(peer, limit=1):
            message = self.message_mapper.map(
                source_channel_id=source_channel_id, message=raw_message
            )
            return LatestMessageBoundary(
                message_id=message.telegram_message_id,
                published_at=message.published_at,
            )
        return None

    async def fetch_history(
        self,
        *,
        source_channel_id: str,
        from_message_id_exclusive: int,
        to_message_id_inclusive: int,
    ) -> tuple[TelegramMessage, ...]:
        self._validate_window(
            from_message_id_exclusive=from_message_id_exclusive,
            to_message_id_inclusive=to_message_id_inclusive,
        )
        peer = await self.source_peer_resolver.resolve_peer(source_channel_id=source_channel_id)
        messages: list[TelegramMessage] = []
        async for raw_message in self.client.iter_messages(
            peer,
            min_id=from_message_id_exclusive,
            max_id=to_message_id_inclusive + 1,
            reverse=True,
            limit=None,
        ):
            message = self.message_mapper.map(
                source_channel_id=source_channel_id, message=raw_message
            )
            if from_message_id_exclusive < message.telegram_message_id <= to_message_id_inclusive:
                messages.append(message)
        return tuple(sorted(messages, key=lambda item: item.telegram_message_id))

    @staticmethod
    def _validate_window(*, from_message_id_exclusive: int, to_message_id_inclusive: int) -> None:
        if (
            not isinstance(from_message_id_exclusive, int)
            or isinstance(from_message_id_exclusive, bool)
            or from_message_id_exclusive < 0
        ):
            raise DomainValidationError("from_message_id_exclusive must be a nonnegative integer")
        if (
            not isinstance(to_message_id_inclusive, int)
            or isinstance(to_message_id_inclusive, bool)
            or to_message_id_inclusive <= 0
        ):
            raise DomainValidationError("to_message_id_inclusive must be a positive integer")
        if from_message_id_exclusive >= to_message_id_inclusive:
            raise DomainValidationError(
                "from_message_id_exclusive must be less than to_message_id_inclusive"
            )
