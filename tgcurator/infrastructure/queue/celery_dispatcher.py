from __future__ import annotations

import asyncio
from typing import Any, Protocol
from uuid import UUID

from celery import Celery

from tgcurator.application.processing.scheduler import RANGE_EXECUTION_QUEUE
from tgcurator.shared import DomainValidationError

RANGE_EXECUTION_TASK_NAME = "tgcurator.range_execution"


class CeleryTaskSender(Protocol):
    """Minimal synchronous Celery client surface used by the async application port."""

    def send_task(
        self,
        name: str,
        args: list[object] | None = None,
        kwargs: dict[str, object] | None = None,
        **options: Any,
    ) -> Any: ...


def create_celery_client(*, broker_url: str, application_name: str = "tgcurator") -> Celery:
    """Create a JSON-only Celery producer without exposing the broker URL in application state."""

    if not broker_url.strip():
        raise DomainValidationError("celery broker URL must not be blank")
    celery = Celery(application_name, broker=broker_url)
    celery.conf.update(
        accept_content=["json"],
        broker_connection_retry_on_startup=True,
        result_serializer="json",
        task_default_queue=RANGE_EXECUTION_QUEUE,
        task_ignore_result=True,
        task_serializer="json",
    )
    return celery


class CeleryTaskDispatcher:
    """Dispatch a reconstructable RangeExecution wake-up through Celery/Redis.

    The broker payload contains only the immutable execution UUID. It is intentionally not a
    source of business state: a lost delivery is repaired from ``durable_wakeups`` by the
    PostgreSQL-backed dispatcher.
    """

    def __init__(self, celery: CeleryTaskSender) -> None:
        self._celery = celery

    async def dispatch(self, *, queue: str, entity_id: str) -> None:
        if queue != RANGE_EXECUTION_QUEUE:
            raise DomainValidationError(f"unsupported durable wake-up queue: {queue!r}")
        try:
            execution_id = str(UUID(entity_id))
        except (AttributeError, ValueError) as error:
            raise DomainValidationError(
                "range execution wake-up entity_id must be a UUID"
            ) from error

        # Celery producer calls are synchronous. Keep them outside the short database lease
        # transaction and off the scheduler event loop.
        await asyncio.to_thread(
            self._celery.send_task,
            RANGE_EXECUTION_TASK_NAME,
            args=[execution_id],
            queue=RANGE_EXECUTION_QUEUE,
        )
