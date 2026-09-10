"""Best-effort broker adapters. Database rows remain the source of work truth."""

from .celery_dispatcher import (
    ANALYSIS_TASK_NAME,
    IMAGE_ARCHIVE_TASK_NAME,
    RANGE_EXECUTION_TASK_NAME,
    VIDEO_ARCHIVE_TASK_NAME,
    CeleryTaskDispatcher,
    create_celery_client,
)

__all__ = [
    "ANALYSIS_TASK_NAME",
    "IMAGE_ARCHIVE_TASK_NAME",
    "RANGE_EXECUTION_TASK_NAME",
    "VIDEO_ARCHIVE_TASK_NAME",
    "CeleryTaskDispatcher",
    "create_celery_client",
]
