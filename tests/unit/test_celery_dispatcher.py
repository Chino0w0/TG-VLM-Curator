from __future__ import annotations

import asyncio
import unittest
from uuid import uuid4

from tgcurator.application.processing.scheduler import RANGE_EXECUTION_QUEUE
from tgcurator.infrastructure.queue import RANGE_EXECUTION_TASK_NAME, CeleryTaskDispatcher
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

    def test_rejects_unknown_queues_and_non_uuid_payloads(self) -> None:
        celery = FakeCelery()
        dispatcher = CeleryTaskDispatcher(celery)

        with self.assertRaises(DomainValidationError):
            asyncio.run(dispatcher.dispatch(queue="other", entity_id=str(uuid4())))
        with self.assertRaises(DomainValidationError):
            asyncio.run(dispatcher.dispatch(queue=RANGE_EXECUTION_QUEUE, entity_id="payload"))

        self.assertEqual(celery.calls, [])


if __name__ == "__main__":
    unittest.main()
