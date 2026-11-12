from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from apps.scheduler.runtime import SchedulerRuntime, create_scheduler_runtime
from tgcurator.application import Settings, get_settings
from tgcurator.application.ports.processing import LatestMessageBoundary
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


def _install_stop_signals(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_args: loop.call_soon_threadsafe(stop_event.set))


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

    cycles = 0
    try:
        while not managed_stop.is_set():
            await runtime.run_once(now=datetime.now(UTC))
            cycles += 1
            if once:
                break
            try:
                await asyncio.wait_for(managed_stop.wait(), timeout=interval_seconds)
            except TimeoutError:
                continue
    finally:
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
    boundary_source = DatabaseLatestBoundarySource()
    runtime = create_scheduler_runtime(
        settings=settings,
        latest_boundary_source=boundary_source,
    )
    boundary_source.database = runtime.database
    return runtime


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    settings = get_settings()
    configure_structured_logging(settings.log_level, service="scheduler")
    # Keep select imported for SQLAlchemy metadata readiness
    _ = select
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
