from __future__ import annotations

import asyncio

from celery import Celery

from apps.worker.runtime import (
    run_analysis_stage_run_task,
    run_image_archive_task,
    run_range_execution_task,
    run_video_archive_task,
)
from tgcurator.application import get_settings
from tgcurator.infrastructure.queue import (
    ANALYSIS_TASK_NAME,
    IMAGE_ARCHIVE_TASK_NAME,
    RANGE_EXECUTION_TASK_NAME,
    VIDEO_ARCHIVE_TASK_NAME,
    create_celery_client,
)


def create_worker_celery_app() -> Celery:
    """Create the worker Celery application without placing business state in Redis."""

    broker = get_settings().celery_broker_url
    if broker is None or not broker.get_secret_value().strip():
        raise RuntimeError("TGCURATOR_CELERY_BROKER_URL is required")
    return create_celery_client(
        broker_url=broker.get_secret_value(), application_name="tgcurator.worker"
    )


celery_app = create_worker_celery_app()


@celery_app.task(name=ANALYSIS_TASK_NAME, ignore_result=True)
def process_analysis_stage_run(stage_run_id: str) -> None:
    """Celery transport entry point; payload is only the durable StageRun UUID."""

    asyncio.run(run_analysis_stage_run_task(stage_run_id))


@celery_app.task(name=RANGE_EXECUTION_TASK_NAME, ignore_result=True)
def process_range_execution(execution_id: str) -> None:
    """Celery transport entry point; payload is only the RangeExecution UUID."""

    asyncio.run(run_range_execution_task(execution_id))


@celery_app.task(name=IMAGE_ARCHIVE_TASK_NAME, ignore_result=True)
def process_image_archive(image_asset_id: str) -> None:
    """Celery transport entry point; payload is only the durable ImageAsset UUID."""

    asyncio.run(run_image_archive_task(image_asset_id))


@celery_app.task(name=VIDEO_ARCHIVE_TASK_NAME, ignore_result=True)
def process_video_archive(video_asset_id: str) -> None:
    """Celery transport entry point; payload is only the durable VideoAsset UUID."""

    asyncio.run(run_video_archive_task(video_asset_id))
