from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from tgcurator.domain.messages import MediaAsset, MediaKind, TelegramMessage
from tgcurator.shared import DomainValidationError, ensure_aware


class TelethonHistoryClient(Protocol):
    def iter_messages(
        self, entity: int | str, *, offset_date: datetime | None = None, limit: int | None = None
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

    def map(self, *, source_channel_id: str, message: Any) -> TelegramMessage:
        message_id = getattr(message, "id", None)
        sent_at = getattr(message, "date", None)
        if not isinstance(message_id, int) or not isinstance(sent_at, datetime):
            raise DomainValidationError("Telethon message is missing an integer id or timestamp")
        ensure_aware(sent_at, field="Telethon message date")
        grouped_id = getattr(message, "grouped_id", None)
        if grouped_id is not None and not isinstance(grouped_id, int):
            raise DomainValidationError("Telethon grouped_id must be an integer when present")
        text = getattr(message, "raw_text", None)
        if text is not None and not isinstance(text, str):
            raise DomainValidationError("Telethon raw_text must be a string or None")
        return TelegramMessage(
            source_channel_id=source_channel_id,
            telegram_message_id=message_id,
            sent_at=sent_at,
            text=text,
            grouped_id=grouped_id,
            media=self._media(source_channel_id=source_channel_id, message=message),
        )

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
    """Read-only Telethon adapter for bounded history scans.

    Telethon yields newest-to-oldest by default. The adapter starts at the immutable right
    boundary and stops immediately after it reaches content older than the left boundary.
    """

    client: TelethonHistoryClient
    source_peer_resolver: TelegramSourcePeerResolver
    message_mapper: TelethonMessageMapper

    async def newest_message_at(self, *, source_channel_id: str) -> datetime | None:
        peer = await self.source_peer_resolver.resolve_peer(source_channel_id=source_channel_id)
        async for raw_message in self.client.iter_messages(peer, limit=1):
            return self.message_mapper.map(
                source_channel_id=source_channel_id, message=raw_message
            ).sent_at
        return None

    async def fetch_history(
        self, *, source_channel_id: str, from_at: datetime, to_at: datetime
    ) -> tuple[TelegramMessage, ...]:
        ensure_aware(from_at, field="from_at")
        ensure_aware(to_at, field="to_at")
        if from_at >= to_at:
            raise DomainValidationError("from_at must be before to_at")
        peer = await self.source_peer_resolver.resolve_peer(source_channel_id=source_channel_id)
        messages: list[TelegramMessage] = []
        async for raw_message in self.client.iter_messages(peer, offset_date=to_at, limit=None):
            message = self.message_mapper.map(
                source_channel_id=source_channel_id, message=raw_message
            )
            if message.sent_at < from_at:
                break
            if message.sent_at < to_at:
                messages.append(message)
        return tuple(sorted(messages, key=lambda item: item.telegram_message_id))
