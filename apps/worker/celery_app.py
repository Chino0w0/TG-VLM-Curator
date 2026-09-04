from __future__ import annotations

import asyncio

from celery import Celery

from apps.worker.runtime import run_image_archive_task, run_range_execution_task
from tgcurator.application import get_settings
from tgcurator.infrastructure.queue import (
    IMAGE_ARCHIVE_TASK_NAME,
    RANGE_EXECUTION_TASK_NAME,
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


@celery_app.task(name=RANGE_EXECUTION_TASK_NAME, ignore_result=True)
def process_range_execution(execution_id: str) -> None:
    """Celery transport entry point; payload is only the RangeExecution UUID."""

    asyncio.run(run_range_execution_task(execution_id))


@celery_app.task(name=IMAGE_ARCHIVE_TASK_NAME, ignore_result=True)
def process_image_archive(image_asset_id: str) -> None:
    """Celery transport entry point; payload is only the durable ImageAsset UUID."""

    asyncio.run(run_image_archive_task(image_asset_id))
