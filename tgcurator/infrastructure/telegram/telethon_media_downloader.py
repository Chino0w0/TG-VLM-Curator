from __future__ import annotations

from typing import Any, Protocol

from tgcurator.application.media import TelegramMediaUnavailableError, TelegramProtectedContentError
from tgcurator.application.ports.media import TelegramMediaDownloadRequest

from .telethon_reader import TelegramSourcePeerResolver


class TelethonMediaDownloadClient(Protocol):
    """Minimal Telethon-shaped client surface needed for source-media retrieval."""

    async def get_messages(self, entity: int | str, *, ids: int) -> Any: ...

    async def download_media(self, message: Any, *, file: type[bytes]) -> bytes | None: ...


class TelethonMediaDownloader:
    """Download source bytes and translate Telegram content-protection failures.

    The adapter deliberately does not bypass Telegram protection. It reads the exact source
    message selected by a durable media reference and returns only in-memory bytes; callers own
    the subsequent normalization/archive workflow and must not keep this I/O inside a database
    transaction.
    """

    def __init__(
        self,
        *,
        client: TelethonMediaDownloadClient,
        source_peer_resolver: TelegramSourcePeerResolver,
    ) -> None:
        self._client = client
        self._source_peer_resolver = source_peer_resolver

    async def download(self, *, request: TelegramMediaDownloadRequest) -> bytes:
        try:
            peer = await self._source_peer_resolver.resolve_peer(
                source_channel_id=request.source_channel_id
            )
            message = await self._client.get_messages(peer, ids=request.telegram_message_id)
            if message is None or getattr(message, "media", None) is None:
                raise TelegramMediaUnavailableError("source Telegram message has no media")
            if getattr(message, "noforwards", False):
                raise TelegramProtectedContentError("source Telegram message is content-protected")
            content = await self._client.download_media(message, file=bytes)
        except TelegramMediaUnavailableError:
            raise
        except TelegramProtectedContentError:
            raise
        except Exception as error:
            if _is_protected_content_error(error):
                raise TelegramProtectedContentError(
                    "Telegram refused source-media download because content is protected"
                ) from error
            raise
        if not isinstance(content, bytes) or not content:
            raise TelegramMediaUnavailableError("source Telegram media could not be downloaded")
        return content


def _is_protected_content_error(error: Exception) -> bool:
    """Avoid importing Telethon error classes so the adapter remains unit-testable without it."""

    return type(error).__name__ in {"ChatForwardsRestrictedError", "ChatRestrictedError"}
