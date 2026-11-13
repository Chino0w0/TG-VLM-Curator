from __future__ import annotations

import argparse
import asyncio
import json
import logging
import threading
from datetime import UTC, datetime

from apps.runtime_dependencies import (
    clear_heartbeat_file,
    resolve_database_url,
    write_heartbeat_file,
)
from tgcurator.application import Settings, get_settings
from tgcurator.application.analysis import ANALYSIS_QUEUE
from tgcurator.application.cleanup import CLEANUP_QUEUE
from tgcurator.application.media import IMAGE_ARCHIVE_QUEUE, VIDEO_ARCHIVE_QUEUE
from tgcurator.application.processing.scheduler import RANGE_EXECUTION_QUEUE
from tgcurator.application.publishing import PUBLICATION_QUEUE
from tgcurator.infrastructure.database import AsyncDatabase
from tgcurator.infrastructure.observability import configure_structured_logging

logger = logging.getLogger(__name__)

QUEUES_BY_ROLE: dict[str, tuple[str, ...]] = {
    "media": (
        IMAGE_ARCHIVE_QUEUE,
        VIDEO_ARCHIVE_QUEUE,
        RANGE_EXECUTION_QUEUE,
    ),
    "analysis": (ANALYSIS_QUEUE,),
    "publish": (PUBLICATION_QUEUE,),
    "maintenance": (CLEANUP_QUEUE,),
    "all": (
        RANGE_EXECUTION_QUEUE,
        IMAGE_ARCHIVE_QUEUE,
        VIDEO_ARCHIVE_QUEUE,
        ANALYSIS_QUEUE,
        PUBLICATION_QUEUE,
        CLEANUP_QUEUE,
    ),
}

DEFAULT_CONCURRENCY_BY_ROLE: dict[str, int] = {
    "media": 2,
    "analysis": 4,
    "publish": 1,
    "maintenance": 1,
    "all": 2,
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Launch a queue-specialized TG-VLM-Curator Celery worker."
    )
    parser.add_argument(
        "--role",
        choices=tuple(QUEUES_BY_ROLE.keys()),
        required=True,
        help="Worker queue role: media, analysis, publish, maintenance, or all.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="Override default worker process/thread concurrency for this role.",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate resolved queues and concurrency without connecting to Redis.",
    )
    return parser.parse_args(argv)


def resolve_worker_argv(
    *,
    role: str,
    concurrency: int | None = None,
    log_level: str = "INFO",
) -> list[str]:
    queues = QUEUES_BY_ROLE[role]
    resolved_concurrency = (
        concurrency
        if concurrency is not None and concurrency > 0
        else DEFAULT_CONCURRENCY_BY_ROLE[role]
    )
    return [
        "worker",
        f"--queues={','.join(queues)}",
        f"--concurrency={resolved_concurrency}",
        f"--loglevel={log_level}",
        "--without-gossip",
        "--without-mingle",
        "--without-heartbeat",
        "-Ofair",
    ]


def _start_worker_heartbeat_keepalive(
    *,
    settings: Settings,
    stop_event: threading.Event,
    interval_seconds: float = 20.0,
) -> threading.Thread:
    from apps.worker.runtime import _record_worker_external_heartbeats

    database_url = resolve_database_url(settings)

    async def _loop() -> None:
        database = AsyncDatabase(database_url) if database_url else None
        try:
            while not stop_event.is_set():
                now = datetime.now(UTC)
                write_heartbeat_file("worker", status="running", now=now)
                if database is not None:
                    db_ok = await database.ping()
                    await _record_worker_external_heartbeats(
                        database,
                        database_ready=db_ok,
                        broker_ready=True,
                        now=now,
                        connect_if_idle=True,
                    )
                await asyncio.to_thread(stop_event.wait, interval_seconds)
        finally:
            if database is not None:
                await database.dispose()

    def _run() -> None:
        try:
            asyncio.run(_loop())
        except Exception:
            logger.debug("Worker heartbeat keepalive exited", exc_info=True)

    thread = threading.Thread(target=_run, name="tgcurator-worker-heartbeat", daemon=True)
    thread.start()
    return thread


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    settings = get_settings()
    configure_structured_logging(settings.log_level, service=f"worker-{args.role}")
    worker_argv = resolve_worker_argv(
        role=args.role,
        concurrency=args.concurrency,
        log_level=settings.log_level,
    )
    if args.check_config:
        print(
            json.dumps(
                {
                    "role": args.role,
                    "queues": list(QUEUES_BY_ROLE[args.role]),
                    "argv": worker_argv,
                },
                sort_keys=True,
            )
        )
        return

    broker = settings.celery_broker_url
    broker_url = broker.get_secret_value().strip() if broker is not None else ""
    if not broker_url:
        raise RuntimeError("TGCURATOR_CELERY_BROKER_URL is required")

    from apps.worker.celery_app import celery_app
    from apps.worker.runtime import (
        install_worker_child_sigterm_handler,
        set_worker_service_role,
        shutdown_process_worker_runtime,
        sweep_stale_media_temp_entries,
    )

    celery_app.conf.broker_url = broker_url
    set_worker_service_role(args.role)
    install_worker_child_sigterm_handler()
    if args.role in {"media", "all"}:
        swept = sweep_stale_media_temp_entries(settings.temp_media_dir)
        if swept > 0:
            logger.info("Swept %d stale temporary FFmpeg entries on worker startup", swept)

    write_heartbeat_file("worker", status="running", now=datetime.now(UTC))
    stop_keepalive = threading.Event()
    keepalive_thread = _start_worker_heartbeat_keepalive(
        settings=settings,
        stop_event=stop_keepalive,
    )
    try:
        celery_app.worker_main(argv=worker_argv)
    finally:
        stop_keepalive.set()
        keepalive_thread.join(timeout=5.0)
        clear_heartbeat_file("worker")
        shutdown_process_worker_runtime(deactivate=True)


if __name__ == "__main__":
    main()
