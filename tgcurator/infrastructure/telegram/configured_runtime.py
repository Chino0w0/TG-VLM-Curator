from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select

from tgcurator.application.ports.media import TelegramMediaDownloadRequest
from tgcurator.application.ports.processing import LatestMessageBoundary
from tgcurator.application.ports.publishing import TelegramSessionExpired
from tgcurator.application.ports.secrets import SecretVault
from tgcurator.application.settings import Settings
from tgcurator.domain.messages import TelegramMessage
from tgcurator.infrastructure.database.models import (
    EncryptedSecret,
    SourceChannel,
    TelegramIdentity,
)
from tgcurator.infrastructure.database.session import AsyncDatabase
from tgcurator.shared import DomainValidationError

from .telethon_media_downloader import TelethonMediaDownloader
from .telethon_reader import TelethonMessageMapper, TelethonReadGateway


@dataclass(frozen=True, slots=True)
class TelegramCredentials:
    api_id: int
    api_hash: str = field(repr=False)
    session_string: str = field(default="", repr=False)
    bot_token: str | None = field(default=None, repr=False)

    @classmethod
    def from_secret(cls, *, plaintext: bytes, identity_type: str, settings: Settings):
        try:
            raw = plaintext.decode("utf-8").strip()
            if raw.startswith("{"):
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError
            else:
                data = {"bot_token" if identity_type == "bot" else "session_string": raw}
            api_id = data.get("api_id") or settings.telegram_api_id
            fallback_hash = settings.telegram_api_hash
            api_hash = data.get("api_hash") or (
                fallback_hash.get_secret_value() if fallback_hash is not None else None
            )
            if isinstance(api_id, bool) or int(api_id or 0) <= 0 or not api_hash:
                raise ValueError
            session = str(data.get("session_string") or data.get("session") or "")
            token = data.get("bot_token")
            if identity_type == "mtproto_user" and not session:
                raise ValueError
            if identity_type == "bot" and not isinstance(token, str):
                raise ValueError
            return cls(int(api_id), str(api_hash), session, token)
        except (UnicodeError, ValueError, TypeError, AttributeError) as error:
            raise TelegramSessionExpired("TelegramCredentialsInvalid") from error


def _default_client_factory(*, credentials: TelegramCredentials, receive_updates: bool) -> Any:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    return TelegramClient(
        StringSession(credentials.session_string),
        credentials.api_id,
        credentials.api_hash,
        receive_updates=receive_updates,
        flood_sleep_threshold=0,
        request_retries=1,
        connection_retries=3,
    )


class ConfiguredTelegramClientPool:
    """Process/loop-local clients loaded from encrypted, database-owned identity configuration.

    Authentication is never interactive in a daemon. A user identity must import a
    valid StringSession; bot sign-in uses only its configured token. Secret rotation
    invalidates cached clients using the encrypted record nonce, never plaintext logs.
    """

    def __init__(
        self,
        *,
        database: AsyncDatabase,
        vault: SecretVault,
        settings: Settings,
        client_factory: Callable[..., Any] | None = None,
        receive_updates: bool = False,
    ) -> None:
        self.database = database
        self.vault = vault
        self.settings = settings
        self._factory = client_factory or _default_client_factory
        self._receive_updates = receive_updates
        self._clients: dict[str, Any] = {}
        self._versions: dict[str, tuple[UUID, bytes]] = {}
        self._lock = asyncio.Lock()

    async def resolve_client(self, *, identity_id: str) -> Any:
        async with self._lock:
            async with self.database.session() as session:
                selected = (
                    await session.execute(
                        select(TelegramIdentity, EncryptedSecret.nonce)
                        .join(EncryptedSecret, EncryptedSecret.id == TelegramIdentity.secret_id)
                        .where(TelegramIdentity.id == UUID(identity_id))
                    )
                ).one_or_none()
                available = selected is not None and selected[0].enabled
                if available:
                    identity, nonce = selected
                    secret_id, identity_type = identity.secret_id, identity.identity_type
                    revision = (secret_id, bytes(nonce))
            if not available:
                stale = self._clients.pop(identity_id, None)
                self._versions.pop(identity_id, None)
                if stale is not None:
                    await stale.disconnect()
                raise TelegramSessionExpired("TelegramIdentityUnavailable")
            cached = self._clients.get(identity_id)
            if cached is not None and self._versions.get(identity_id) == revision:
                if cached.is_connected():
                    return cached
                await cached.disconnect()
                self._clients.pop(identity_id, None)
            elif cached is not None:
                await cached.disconnect()
                self._clients.pop(identity_id, None)
            client = None
            try:
                expected_secret_type = "bot_token" if identity_type == "bot" else "telegram_session"
                try:
                    plaintext = await self.vault.resolve(
                        secret_id=secret_id,
                        expected_secret_type=expected_secret_type,
                    )
                except TypeError:
                    plaintext = await self.vault.resolve(secret_id=secret_id)
                credentials = TelegramCredentials.from_secret(
                    plaintext=plaintext, identity_type=identity_type, settings=self.settings
                )
                client = self._factory(
                    credentials=credentials, receive_updates=self._receive_updates
                )
                await client.connect()
                if not await client.is_user_authorized():
                    if identity_type != "bot":
                        raise TelegramSessionExpired("TelegramSessionUnauthorized")
                    await client.sign_in(bot_token=credentials.bot_token)
                    if not await client.is_user_authorized():
                        raise TelegramSessionExpired("TelegramBotUnauthorized")
                # StringSession stores authentication, not the peer access-hash cache.
                # User dialogs populate that cache for private subscribed sources/destinations.
                if identity_type == "mtproto_user":
                    await client.get_dialogs(limit=None)
                self._clients[identity_id] = client
                self._versions[identity_id] = revision
                await self._record_health(identity_id, healthy=True)
                return client
            except Exception as error:
                if client is not None:
                    await client.disconnect()
                await self._record_health(
                    identity_id, healthy=False, error_type=type(error).__name__
                )
                if isinstance(error, TelegramSessionExpired):
                    raise
                raise TelegramSessionExpired("TelegramIdentityConnectionFailed") from error

    async def _record_health(
        self, identity_id: str, *, healthy: bool, error_type: str | None = None
    ) -> None:
        async with self.database.session() as session:
            async with session.begin():
                identity = await session.get(
                    TelegramIdentity, UUID(identity_id), with_for_update=True
                )
                if identity is not None:
                    identity.health_status = "healthy" if healthy else "degraded"
                    identity.last_error = error_type
                    identity.updated_at = datetime.now(UTC)
                    if healthy:
                        identity.last_connected_at = identity.updated_at

    async def revoke_identity(self, *, identity_id: str) -> None:
        async with self._lock:
            client = self._clients.pop(identity_id, None)
            self._versions.pop(identity_id, None)
            if client is not None:
                await client.disconnect()

    async def close(self) -> None:
        clients, self._clients = tuple(self._clients.values()), {}
        self._versions.clear()
        await asyncio.gather(*(client.disconnect() for client in clients), return_exceptions=True)


class ConfiguredTelegramGateway:
    """Resolve the source's configured read identity before every durable history read."""

    def __init__(
        self, *, database: AsyncDatabase, client_pool: ConfiguredTelegramClientPool
    ) -> None:
        self.database = database
        self.client_pool = client_pool
        self.message_mapper = TelethonMessageMapper()

    async def source_configuration(self, source_channel_id: str) -> tuple[str, int | str]:
        async with self.database.session() as session:
            source = await session.get(SourceChannel, UUID(source_channel_id))
            if source is None or not source.enabled:
                raise DomainValidationError("source channel is unavailable")
            return str(source.default_read_identity_id), source.username or source.telegram_peer_id

    async def resolve_peer(self, *, source_channel_id: str) -> int | str:
        _, peer = await self.source_configuration(source_channel_id)
        return peer

    async def read_client(self, *, source_channel_id: str) -> Any:
        identity_id, _ = await self.source_configuration(source_channel_id)
        return await self.client_pool.resolve_client(identity_id=identity_id)

    async def _reader(self, source_channel_id: str) -> TelethonReadGateway:
        client = await self.read_client(source_channel_id=source_channel_id)
        return TelethonReadGateway(
            client=client, source_peer_resolver=self, message_mapper=self.message_mapper
        )

    async def newest_message(self, *, source_channel_id: str) -> LatestMessageBoundary | None:
        reader = await self._reader(source_channel_id)
        return await reader.newest_message(source_channel_id=source_channel_id)

    async def fetch_history(
        self,
        *,
        source_channel_id: str,
        from_message_id_exclusive: int,
        to_message_id_inclusive: int,
    ) -> tuple[TelegramMessage, ...]:
        reader = await self._reader(source_channel_id)
        return await reader.fetch_history(
            source_channel_id=source_channel_id,
            from_message_id_exclusive=from_message_id_exclusive,
            to_message_id_inclusive=to_message_id_inclusive,
        )


class ConfiguredTelegramMediaDownloader:
    def __init__(self, gateway: ConfiguredTelegramGateway) -> None:
        self.gateway = gateway

    async def download(self, *, request: TelegramMediaDownloadRequest) -> bytes:
        client = await self.gateway.read_client(source_channel_id=request.source_channel_id)
        return await TelethonMediaDownloader(
            client=client, source_peer_resolver=self.gateway
        ).download(request=request)
