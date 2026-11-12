from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select

from tgcurator.infrastructure.database.models import SourceChannel, TelegramIdentity

from .configured_runtime import ConfiguredTelegramClientPool, ConfiguredTelegramGateway

logger = logging.getLogger(__name__)


class TelegramSubscriptionManager:
    """Subscribe configured read identities and periodically reconcile durable history gaps."""

    def __init__(
        self,
        *,
        gateway: ConfiguredTelegramGateway,
        pool: ConfiguredTelegramClientPool,
        reconcile_interval: timedelta = timedelta(seconds=60),
    ) -> None:
        self.gateway = gateway
        self.pool = pool
        self.reconcile_interval = reconcile_interval
        self._sources_by_identity: dict[str, dict[int, str]] = {}
        self._subscribed: dict[str, Any] = {}
        self._handlers: list[tuple[Any, Any, Any]] = []
        self._last_reconciled: datetime | None = None

    async def poll(self, *, runtime: Any, now: datetime) -> None:
        from telethon import events

        async with self.gateway.database.session() as session:
            sources = tuple(
                await session.scalars(
                    select(SourceChannel)
                    .join(
                        TelegramIdentity,
                        TelegramIdentity.id == SourceChannel.default_read_identity_id,
                    )
                    .where(SourceChannel.enabled.is_(True), TelegramIdentity.enabled.is_(True))
                    .order_by(SourceChannel.id)
                )
            )
        current: dict[str, dict[int, str]] = {}
        for source in sources:
            current.setdefault(str(source.default_read_identity_id), {})[
                source.telegram_peer_id
            ] = str(source.id)
        self._sources_by_identity = current
        connected_sources: list[str] = []
        all_connected = bool(current)
        changed = False
        for identity_id, source_map in current.items():
            try:
                client = await self.pool.resolve_client(identity_id=identity_id)
                if self._subscribed.get(identity_id) is not client:
                    old = self._subscribed.get(identity_id)
                    if old is not None:
                        self._remove_handlers(old)
                    new_handler = self._new_message_handler(runtime, identity_id)
                    edit_handler = self._edit_message_handler(runtime, identity_id)
                    delete_handler = self._delete_message_handler(runtime, identity_id)
                    for handler, builder in (
                        (new_handler, events.NewMessage()),
                        (edit_handler, events.MessageEdited()),
                        (delete_handler, events.MessageDeleted()),
                    ):
                        client.add_event_handler(handler, builder)
                        self._handlers.append((client, handler, builder))
                    self._subscribed[identity_id] = client
                    changed = True
                connected_sources.extend(source_map.values())
            except Exception as error:
                all_connected = False
                old = self._subscribed.pop(identity_id, None)
                if old is not None:
                    self._remove_handlers(old)
                self._sources_by_identity.pop(identity_id, None)
                logger.warning(
                    "Telegram identity subscription unavailable",
                    extra={
                        "event": "telegram.subscription_unavailable",
                        "error_type": type(error).__name__,
                    },
                )
        for identity_id in set(self._subscribed) - set(current):
            self._remove_handlers(self._subscribed.pop(identity_id))
            await self.pool.revoke_identity(identity_id=identity_id)
        runtime.mark_connected(connected=all_connected, now=now)
        due = (
            changed
            or self._last_reconciled is None
            or now - self._last_reconciled >= self.reconcile_interval
        )
        if connected_sources and due:
            try:
                await runtime.reconcile_sources(source_channel_ids=connected_sources, now=now)
                self._last_reconciled = now
            except Exception as error:
                runtime.mark_connected(connected=False, now=now)
                logger.warning(
                    "Telegram source reconciliation unavailable",
                    extra={
                        "event": "telegram.reconciliation_unavailable",
                        "error_type": type(error).__name__,
                    },
                )

    async def _source_for_event(self, identity_id: str, event: Any) -> str | None:
        source_id = self._sources_by_identity.get(identity_id, {}).get(
            getattr(event, "chat_id", None)
        )
        if source_id is None:
            return None
        # A callback already queued before the next poll must not outlive identity
        # disablement or source reassignment. Revalidate persisted authority here.
        async with self.gateway.database.session() as session:
            enabled_id = await session.scalar(
                select(SourceChannel.id)
                .join(
                    TelegramIdentity, TelegramIdentity.id == SourceChannel.default_read_identity_id
                )
                .where(
                    SourceChannel.id == UUID(source_id),
                    SourceChannel.default_read_identity_id == UUID(identity_id),
                    SourceChannel.enabled.is_(True),
                    TelegramIdentity.enabled.is_(True),
                )
            )
        return source_id if enabled_id is not None else None

    def _new_message_handler(self, runtime: Any, identity_id: str):
        async def handler(event: Any) -> None:
            source_id = await self._source_for_event(identity_id, event)
            if source_id is None:
                return
            try:
                mapped = self.gateway.message_mapper.map(
                    source_channel_id=source_id, message=event.message
                )
                await runtime.handle_new_message(
                    message=mapped, now=datetime.now(mapped.published_at.tzinfo)
                )
            except Exception as error:
                logger.warning(
                    "Telegram update ingestion failed; durable reconciliation will retry",
                    extra={"event": "telegram.update_failed", "error_type": type(error).__name__},
                )

        return handler

    def _edit_message_handler(self, runtime: Any, identity_id: str):
        async def handler(event: Any) -> None:
            source_id = await self._source_for_event(identity_id, event)
            if source_id is None:
                return
            try:
                mapped = self.gateway.message_mapper.map(
                    source_channel_id=source_id, message=event.message
                )
                await runtime.handle_message_edit(
                    source_channel_id=source_id,
                    telegram_message_id=mapped.telegram_message_id,
                    original_text=mapped.original_text,
                    telegram_metadata=mapped.telegram_metadata,
                    media=mapped.media,
                    edited_at=mapped.edited_at or mapped.published_at,
                )
            except Exception as error:
                logger.warning(
                    "Telegram edit ingestion failed",
                    extra={"event": "telegram.edit_failed", "error_type": type(error).__name__},
                )

        return handler

    def _delete_message_handler(self, runtime: Any, identity_id: str):
        async def handler(event: Any) -> None:
            source_id = await self._source_for_event(identity_id, event)
            if source_id is None:
                return
            from datetime import UTC

            for message_id in event.deleted_ids:
                try:
                    await runtime.handle_message_deletion(
                        source_channel_id=source_id,
                        telegram_message_id=message_id,
                        deleted_at=datetime.now(UTC),
                    )
                except Exception as error:
                    logger.warning(
                        "Telegram deletion ingestion failed",
                        extra={
                            "event": "telegram.delete_failed",
                            "error_type": type(error).__name__,
                        },
                    )

        return handler

    def _remove_handlers(self, client: Any) -> None:
        keep = []
        for owner, handler, builder in self._handlers:
            if owner is client:
                owner.remove_event_handler(handler, builder)
            else:
                keep.append((owner, handler, builder))
        self._handlers = keep

    async def close(self) -> None:
        for client in tuple(self._subscribed.values()):
            self._remove_handlers(client)
        self._subscribed.clear()
        await self.pool.close()
