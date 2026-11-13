from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any

from apps.runtime_dependencies import (
    clear_heartbeat_file,
    upsert_service_heartbeat,
    write_heartbeat_file,
)
from apps.telegram_ingestor.runtime import (
    TelegramIngestorRuntime,
    create_telegram_ingestor_runtime,
)
from tgcurator.application import get_settings
from tgcurator.infrastructure.observability import configure_structured_logging

logger = logging.getLogger(__name__)


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


async def run_ingestor_daemon(
    *,
    runtime: TelegramIngestorRuntime,
    flush_interval_seconds: float = 1.0,
    once: bool = False,
    stop_event: asyncio.Event | None = None,
) -> int:
    """Single-instance ingestor loop with graceful SIGTERM media-group flush."""

    managed_stop = stop_event or asyncio.Event()
    if stop_event is None:
        _install_stop_signals(managed_stop)

    started_at = datetime.now(UTC)
    write_heartbeat_file("ingestor", status="running", now=started_at)
    await upsert_service_heartbeat(
        getattr(runtime, "database", None),
        service_name="ingestor",
        database_ready=True,
        broker_ready=True,
        now=started_at,
        connect_if_idle=True,
    )

    ticks = 0
    try:
        while not managed_stop.is_set():
            poll = getattr(runtime, "poll_updates", None)
            if poll is not None:
                try:
                    completed, _ = await _await_or_cancel_on_stop(
                        poll(now=datetime.now(UTC)),
                        managed_stop,
                    )
                    if not completed:
                        break
                except Exception:
                    if once:
                        raise
                    logger.exception("Telegram ingestor poll cycle failed; continuing daemon loop")
            if managed_stop.is_set():
                break
            try:
                completed, _ = await _await_or_cancel_on_stop(
                    runtime.flush_due_groups(now=datetime.now(UTC)),
                    managed_stop,
                )
                if not completed:
                    break
                ticks += 1
                write_heartbeat_file("ingestor", status="running", now=datetime.now(UTC))
            except Exception:
                if once:
                    raise
                logger.exception("Telegram ingestor flush cycle failed; continuing daemon loop")
                write_heartbeat_file("ingestor", status="running", now=datetime.now(UTC))
            if once:
                break
            try:
                await asyncio.wait_for(managed_stop.wait(), timeout=flush_interval_seconds)
            except TimeoutError:
                continue
    finally:
        clear_heartbeat_file("ingestor")
        await runtime.shutdown()
    return ticks


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the single-instance TG-VLM-Curator Telegram ingestor daemon."
    )
    parser.add_argument(
        "--flush-interval-seconds",
        type=float,
        default=1.0,
        help="Seconds between bounded media-group buffer flushes (default: 1.0).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one media-group flush cycle, flush on shutdown, and exit.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    settings = get_settings()
    configure_structured_logging(settings.log_level, service="telegram-ingestor")
    runtime = create_telegram_ingestor_runtime(settings=settings)
    asyncio.run(
        run_ingestor_daemon(
            runtime=runtime,
            flush_interval_seconds=args.flush_interval_seconds,
            once=args.once,
        )
    )


if __name__ == "__main__":
    main()
