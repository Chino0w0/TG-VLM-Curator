from __future__ import annotations

import asyncio
import unittest
from uuid import uuid4

from tgcurator.application.media import IMAGE_ARCHIVE_QUEUE
from tgcurator.application.processing.scheduler import RANGE_EXECUTION_QUEUE
from tgcurator.infrastructure.queue import (
    IMAGE_ARCHIVE_TASK_NAME,
    RANGE_EXECUTION_TASK_NAME,
    CeleryTaskDispatcher,
    create_celery_client,
)
from tgcurator.shared import DomainValidationError


class FakeCelery:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[object] | None, dict[str, object]]] = []

    def send_task(
        self,
        name: str,
        args: list[object] | None = None,
        kwargs: dict[str, object] | None = None,
        **options: object,
    ) -> None:
        self.calls.append((name, args, options))


class CeleryTaskDispatcherTests(unittest.TestCase):
    def test_dispatches_only_a_normalized_range_execution_uuid(self) -> None:
        celery = FakeCelery()
        dispatcher = CeleryTaskDispatcher(celery)
        execution_id = str(uuid4()).upper()

        asyncio.run(dispatcher.dispatch(queue=RANGE_EXECUTION_QUEUE, entity_id=execution_id))

        normalized_execution_id = execution_id.lower()
        self.assertEqual(
            celery.calls,
            [
                (
                    RANGE_EXECUTION_TASK_NAME,
                    [normalized_execution_id],
                    {"queue": RANGE_EXECUTION_QUEUE},
                )
            ],
        )

    def test_dispatches_only_a_normalized_image_asset_uuid(self) -> None:
        celery = FakeCelery()
        dispatcher = CeleryTaskDispatcher(celery)
        image_asset_id = str(uuid4()).upper()

        asyncio.run(dispatcher.dispatch(queue=IMAGE_ARCHIVE_QUEUE, entity_id=image_asset_id))

        self.assertEqual(
            celery.calls,
            [
                (
                    IMAGE_ARCHIVE_TASK_NAME,
                    [image_asset_id.lower()],
                    {"queue": IMAGE_ARCHIVE_QUEUE},
                )
            ],
        )

    def test_rejects_unknown_queues_and_non_uuid_payloads(self) -> None:
        celery = FakeCelery()
        dispatcher = CeleryTaskDispatcher(celery)

        with self.assertRaises(DomainValidationError):
            asyncio.run(dispatcher.dispatch(queue="other", entity_id=str(uuid4())))
        with self.assertRaises(DomainValidationError):
            asyncio.run(dispatcher.dispatch(queue=RANGE_EXECUTION_QUEUE, entity_id="payload"))

        self.assertEqual(celery.calls, [])

    def test_client_declares_both_durable_worker_queues(self) -> None:
        client = create_celery_client(broker_url="redis://localhost:6379/0")

        self.assertEqual(client.conf.task_default_queue, RANGE_EXECUTION_QUEUE)
        self.assertEqual(
            tuple(queue.name for queue in client.conf.task_queues),
            (RANGE_EXECUTION_QUEUE, IMAGE_ARCHIVE_QUEUE),
        )


if __name__ == "__main__":
    unittest.main()
