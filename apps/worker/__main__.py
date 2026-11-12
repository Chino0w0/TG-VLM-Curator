from __future__ import annotations

import argparse
import json

from celery.signals import worker_process_shutdown, worker_shutting_down

from tgcurator.application import get_settings
from tgcurator.application.analysis import ANALYSIS_QUEUE
from tgcurator.application.cleanup import CLEANUP_QUEUE
from tgcurator.application.media import IMAGE_ARCHIVE_QUEUE, VIDEO_ARCHIVE_QUEUE
from tgcurator.application.processing.scheduler import RANGE_EXECUTION_QUEUE
from tgcurator.application.publishing import PUBLICATION_QUEUE
from tgcurator.infrastructure.observability import configure_structured_logging

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
        "-Ofair",
    ]


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

    from apps.worker.celery_app import celery_app
    from apps.worker.runtime import terminate_registered_media_extractors

    @worker_shutting_down.connect
    @worker_process_shutdown.connect
    def _on_worker_shutdown(**_kwargs: object) -> None:
        terminate_registered_media_extractors()

    celery_app.worker_main(argv=worker_argv)


if __name__ == "__main__":
    main()
