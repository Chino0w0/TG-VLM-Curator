from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from datetime import UTC, datetime

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

    ticks = 0
    try:
        while not managed_stop.is_set():
            poll = getattr(runtime, "poll_updates", None)
            if poll is not None:
                await poll(now=datetime.now(UTC))
            await runtime.flush_due_groups(now=datetime.now(UTC))
            ticks += 1
            if once:
                break
            try:
                await asyncio.wait_for(managed_stop.wait(), timeout=flush_interval_seconds)
            except TimeoutError:
                continue
    finally:
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
