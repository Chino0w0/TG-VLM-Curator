from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from apps.runtime_dependencies import (
    clear_heartbeat_file,
    configured_secret_vault,
    upsert_service_heartbeat,
    write_heartbeat_file,
)
from apps.scheduler.runtime import SchedulerRuntime, create_scheduler_runtime
from tgcurator.application import Settings, get_settings
from tgcurator.application.ports.processing import LatestBoundarySource, LatestMessageBoundary
from tgcurator.infrastructure.database import AsyncDatabase
from tgcurator.infrastructure.database.models import SourceChannel
from tgcurator.infrastructure.observability import configure_structured_logging

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DatabaseLatestBoundarySource:
    """Reads latest observed channel boundaries persisted by telegram-ingestor."""

    database: AsyncDatabase | None = None

    async def newest_message(
        self,
        *,
        source_channel_id: str,
    ) -> LatestMessageBoundary | None:
        if self.database is None:
            return None
        channel_uuid = UUID(source_channel_id)
        async with self.database.session() as session:
            row = await session.get(SourceChannel, channel_uuid)
            if row is None:
                return None
            if row.latest_seen_message_id is None or row.last_activity_at is None:
                return None
            return LatestMessageBoundary(
                message_id=int(row.latest_seen_message_id),
                published_at=row.last_activity_at,
            )


@dataclass(slots=True)
class FallbackLatestBoundarySource:
    """Queries live Telegram boundary first, falling back to DB cursor when unavailable."""

    primary: LatestBoundarySource
    fallback: LatestBoundarySource

    async def newest_message(
        self,
        *,
        source_channel_id: str,
    ) -> LatestMessageBoundary | None:
        try:
            boundary = await self.primary.newest_message(source_channel_id=source_channel_id)
            if boundary is not None:
                return boundary
        except Exception:
            logger.warning(
                "Live Telegram boundary lookup failed for source %s; falling back to DB cursor",
                source_channel_id,
                exc_info=True,
            )
        return await self.fallback.newest_message(source_channel_id=source_channel_id)


def _install_stop_signals(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_args: loop.call_soon_threadsafe(stop_event.set))


async def _await_or_cancel_on_stop[T](
    coro: Coroutine[Any, Any, T],
    stop_event: asyncio.Event,
) -> tuple[bool, T | None]:
    task = asyncio.create_task(coro)
    stop_waiter = asyncio.create_task(stop_event.wait())
    try:
        done, _ = await asyncio.wait(
            {task, stop_waiter},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if task in done:
            return True, task.result()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return False, None
    finally:
        if not stop_waiter.done():
            stop_waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop_waiter


async def run_scheduler_daemon(
    *,
    runtime: SchedulerRuntime,
    interval_seconds: float = 5.0,
    once: bool = False,
    stop_event: asyncio.Event | None = None,
) -> int:
    managed_stop = stop_event or asyncio.Event()
    if stop_event is None:
        _install_stop_signals(managed_stop)

    started_at = datetime.now(UTC)
    write_heartbeat_file("scheduler", status="running", now=started_at)
    await upsert_service_heartbeat(
        getattr(runtime, "database", None),
        service_name="scheduler",
        database_ready=True,
        broker_ready=True,
        now=started_at,
        connect_if_idle=True,
    )

    cycles = 0
    try:
        while not managed_stop.is_set():
            now = datetime.now(UTC)
            try:
                completed, _ = await _await_or_cancel_on_stop(
                    runtime.run_once(now=now),
                    managed_stop,
                )
                if not completed:
                    break
                cycles += 1
                write_heartbeat_file("scheduler", status="running", now=datetime.now(UTC))
            except Exception:
                if once:
                    raise
                logger.exception("Scheduler cycle failed; continuing daemon loop")
                write_heartbeat_file("scheduler", status="running", now=datetime.now(UTC))
            if once:
                break
            try:
                await asyncio.wait_for(managed_stop.wait(), timeout=interval_seconds)
            except TimeoutError:
                continue
    finally:
        clear_heartbeat_file("scheduler")
        await runtime.close()
    return cycles


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the TG-VLM-Curator durable ProcessingRange & wake-up scheduler."
    )
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=5.0,
        help="Seconds between scheduler repair and dispatch cycles (default: 5.0).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Execute a single scheduler cycle and exit cleanly.",
    )
    return parser.parse_args(argv)


def build_default_scheduler_runtime(settings: Settings) -> SchedulerRuntime:
    db_boundary = DatabaseLatestBoundarySource()
    runtime = create_scheduler_runtime(
        settings=settings,
        latest_boundary_source=db_boundary,
    )
    db_boundary.database = runtime.database
    vault = configured_secret_vault(settings=settings, database=runtime.database)
    if vault is not None:
        from tgcurator.infrastructure.telegram.configured_runtime import (
            ConfiguredTelegramClientPool,
            ConfiguredTelegramGateway,
        )

        pool = ConfiguredTelegramClientPool(
            database=runtime.database,
            vault=vault,
            settings=settings,
            receive_updates=False,
        )
        gateway = ConfiguredTelegramGateway(database=runtime.database, client_pool=pool)
        runtime.range_scheduler.latest_boundary_source = FallbackLatestBoundarySource(
            primary=gateway,
            fallback=db_boundary,
        )
        runtime.telegram_client_pool = pool
    return runtime


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    settings = get_settings()
    configure_structured_logging(settings.log_level, service="scheduler")
    runtime = build_default_scheduler_runtime(settings)
    asyncio.run(
        run_scheduler_daemon(
            runtime=runtime,
            interval_seconds=args.interval_seconds,
            once=args.once,
        )
    )


if __name__ == "__main__":
    main()
