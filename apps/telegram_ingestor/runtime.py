from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from tgcurator.application import (
    MediaGroupAggregationBuffer,
    MessageIngestService,
    RealtimeTelegramIngestion,
    ReconnectTelegramReconciliation,
    Settings,
    SourceMessageLifecycleService,
    SourceReconciliationReport,
    SourceReconciliationService,
)
from tgcurator.application.ports.contracts import TelegramGateway
from tgcurator.application.ports.ingestion import IngestReport
from tgcurator.domain.messages import MediaAsset, TelegramMessage
from tgcurator.infrastructure.database import (
    AsyncDatabase,
    SqlAlchemySourceMessageLifecycleRepository,
    SqlAlchemySourceReconciliationCursorRepository,
    SqlAlchemyTelegramMessageIngestRepository,
)
from tgcurator.shared import ensure_aware


@dataclass(frozen=True, slots=True)
class IngestorHealthSnapshot:
    status: str
    connected: bool
    database_ready: bool
    pending_media_groups: int
    last_event_at: datetime | None
    last_reconciled_at: datetime | None


@dataclass(slots=True)
class TelegramIngestorRuntime:
    """Single-instance `telegram-ingestor` composition root.

    Combines realtime Update ingestion (with in-memory media-group aggregation),
    startup/reconnect frozen-window history reconciliation, and source edit/deletion
    lifecycle preservation while deferring media archive wake-ups to pipeline orchestration.
    """

    database: AsyncDatabase
    realtime_ingestion: RealtimeTelegramIngestion
    source_lifecycle: SourceMessageLifecycleService
    reconciliation: ReconnectTelegramReconciliation | None = None
    connected: bool = False
    database_ready: bool = True
    last_event_at: datetime | None = None
    last_reconciled_at: datetime | None = None
    subscriptions: Any | None = None

    def mark_connected(self, *, connected: bool, now: datetime) -> None:
        ensure_aware(now, field="now")
        self.connected = bool(connected)
        self.last_event_at = now

    async def handle_new_message(
        self,
        *,
        message: TelegramMessage,
        now: datetime,
    ) -> IngestReport:
        ensure_aware(now, field="now")
        try:
            report = await self.realtime_ingestion.ingest_update(message=message, now=now)
        except Exception:
            self.database_ready = False
            raise
        self.database_ready = True
        self.last_event_at = now
        return report

    async def flush_due_groups(self, *, now: datetime) -> IngestReport:
        ensure_aware(now, field="now")
        try:
            report = await self.realtime_ingestion.flush_due(now=now)
        except Exception:
            self.database_ready = False
            raise
        self.database_ready = True
        if report.logical_messages > 0:
            self.last_event_at = now
        return report

    async def reconcile_sources(
        self,
        *,
        source_channel_ids: Iterable[str],
        now: datetime,
    ) -> dict[str, SourceReconciliationReport]:
        ensure_aware(now, field="now")
        if self.reconciliation is None:
            return {}
        reports: dict[str, SourceReconciliationReport] = {}
        try:
            for source_channel_id in source_channel_ids:
                reports[source_channel_id] = await self.reconciliation.reconcile(
                    source_channel_id=source_channel_id
                )
        except Exception:
            self.database_ready = False
            raise
        self.database_ready = True
        self.last_reconciled_at = now
        return reports

    async def handle_message_edit(
        self,
        *,
        source_channel_id: str,
        telegram_message_id: int,
        original_text: str | None,
        telegram_metadata: Mapping[str, Any],
        media: tuple[MediaAsset, ...],
        edited_at: datetime,
    ) -> bool:
        try:
            changed = await self.source_lifecycle.record_edit(
                source_channel_id=source_channel_id,
                telegram_message_id=telegram_message_id,
                original_text=original_text,
                telegram_metadata=telegram_metadata,
                media=media,
                edited_at=edited_at,
            )
        except Exception:
            self.database_ready = False
            raise
        self.database_ready = True
        self.last_event_at = edited_at
        return changed

    async def handle_message_deletion(
        self,
        *,
        source_channel_id: str,
        telegram_message_id: int,
        deleted_at: datetime,
    ) -> bool:
        try:
            changed = await self.source_lifecycle.record_deletion(
                source_channel_id=source_channel_id,
                telegram_message_id=telegram_message_id,
                deleted_at=deleted_at,
            )
        except Exception:
            self.database_ready = False
            raise
        self.database_ready = True
        self.last_event_at = deleted_at
        return changed

    async def poll_updates(self, *, now: datetime) -> None:
        ensure_aware(now, field="now")
        if self.subscriptions is not None:
            await self.subscriptions.poll(runtime=self, now=now)

    def health_snapshot(self) -> IngestorHealthSnapshot:
        healthy = self.database_ready and self.connected
        return IngestorHealthSnapshot(
            status="ok" if healthy else "degraded",
            connected=self.connected,
            database_ready=self.database_ready,
            pending_media_groups=self.realtime_ingestion.media_group_buffer.pending_group_count,
            last_event_at=self.last_event_at,
            last_reconciled_at=self.last_reconciled_at,
        )

    async def shutdown(self) -> IngestReport:
        self.connected = False
        try:
            return await self.realtime_ingestion.flush_all()
        finally:
            try:
                if self.subscriptions is not None:
                    await self.subscriptions.close()
            finally:
                await self.database.dispose()


def create_telegram_ingestor_runtime(
    *,
    settings: Settings,
    telegram_gateway: TelegramGateway | None = None,
    defer_media_wakeups: bool = True,
    aggregation_window: timedelta = timedelta(seconds=3),
) -> TelegramIngestorRuntime:
    database_url = settings.database_url
    if database_url is None or not database_url.strip():
        raise RuntimeError("TGCURATOR_DATABASE_URL is required")
    database = AsyncDatabase(database_url)
    subscriptions = None
    if telegram_gateway is None:
        from apps.runtime_dependencies import configured_secret_vault
        from tgcurator.infrastructure.telegram.configured_runtime import (
            ConfiguredTelegramClientPool,
            ConfiguredTelegramGateway,
        )
        from tgcurator.infrastructure.telegram.subscriptions import TelegramSubscriptionManager

        vault = configured_secret_vault(settings=settings, database=database)
        if vault is not None:
            pool = ConfiguredTelegramClientPool(
                database=database, vault=vault, settings=settings, receive_updates=True
            )
            telegram_gateway = ConfiguredTelegramGateway(database=database, client_pool=pool)
            subscriptions = TelegramSubscriptionManager(gateway=telegram_gateway, pool=pool)
    ingest_service = MessageIngestService(
        repository=SqlAlchemyTelegramMessageIngestRepository(
            database,
            defer_media_wakeups=defer_media_wakeups,
        )
    )
    cursor_service = SourceReconciliationService(
        repository=SqlAlchemySourceReconciliationCursorRepository(database)
    )
    media_buffer = MediaGroupAggregationBuffer(aggregation_window=aggregation_window)
    realtime = RealtimeTelegramIngestion(
        message_ingest_service=ingest_service,
        media_group_buffer=media_buffer,
        cursor_service=cursor_service,
    )
    lifecycle = SourceMessageLifecycleService(
        repository=SqlAlchemySourceMessageLifecycleRepository(database)
    )
    reconciliation = (
        ReconnectTelegramReconciliation(
            telegram_gateway=telegram_gateway,
            message_ingest_service=ingest_service,
            cursor_service=cursor_service,
        )
        if telegram_gateway is not None
        else None
    )
    return TelegramIngestorRuntime(
        database=database,
        realtime_ingestion=realtime,
        source_lifecycle=lifecycle,
        reconciliation=reconciliation,
        subscriptions=subscriptions,
    )
