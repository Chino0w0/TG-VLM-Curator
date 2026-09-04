from __future__ import annotations

import unittest
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from tgcurator.application.reconciliation import SourceReconciliationService
from tgcurator.infrastructure.database.models import SourceChannel
from tgcurator.infrastructure.database.reconciliation_repository import (
    source_reconciliation_cursor_advance_statement,
)
from tgcurator.shared import DomainValidationError


class FakeSourceReconciliationCursorRepository:
    def __init__(self) -> None:
        self.cursor: int | None = None
        self.advance_requests: list[tuple[str, int]] = []
        self.read_requests: list[str] = []

    async def advance_last_seen_message_id(
        self, *, source_channel_id: str, telegram_message_id: int
    ) -> bool:
        self.advance_requests.append((source_channel_id, telegram_message_id))
        if self.cursor is None or self.cursor < telegram_message_id:
            self.cursor = telegram_message_id
            return True
        return False

    async def get_last_seen_message_id(self, *, source_channel_id: str) -> int | None:
        self.read_requests.append(source_channel_id)
        return self.cursor


class SourceReconciliationCursorTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_records_only_monotonic_cursor_advancement(self) -> None:
        source_channel_id = str(uuid4())
        repository = FakeSourceReconciliationCursorRepository()
        service = SourceReconciliationService(repository)

        self.assertTrue(
            await service.record_ingested_message(
                source_channel_id=source_channel_id,
                telegram_message_id=42,
            )
        )
        self.assertFalse(
            await service.record_ingested_message(
                source_channel_id=source_channel_id,
                telegram_message_id=41,
            )
        )

        self.assertEqual(
            await service.last_seen_message_id(source_channel_id=source_channel_id), 42
        )
        self.assertEqual(
            repository.advance_requests,
            [(source_channel_id, 42), (source_channel_id, 41)],
        )
        self.assertEqual(repository.read_requests, [source_channel_id])

    async def test_service_rejects_invalid_identifiers_before_repository_access(self) -> None:
        repository = FakeSourceReconciliationCursorRepository()
        service = SourceReconciliationService(repository)

        with self.assertRaises(DomainValidationError):
            await service.record_ingested_message(source_channel_id=" ", telegram_message_id=1)
        with self.assertRaises(DomainValidationError):
            await service.record_ingested_message(
                source_channel_id="not-a-uuid", telegram_message_id=1
            )
        with self.assertRaises(DomainValidationError):
            await service.record_ingested_message(
                source_channel_id=str(uuid4()), telegram_message_id=0
            )
        with self.assertRaises(DomainValidationError):
            await service.record_ingested_message(
                source_channel_id=str(uuid4()), telegram_message_id=True
            )

        self.assertEqual(repository.advance_requests, [])

    def test_metadata_and_postgresql_statement_prevent_regression(self) -> None:
        constraints = {item.name for item in SourceChannel.__table__.constraints}
        self.assertIn("ck_source_channel_last_seen_positive", constraints)

        statement = source_reconciliation_cursor_advance_statement(
            source_channel_id=uuid4(), telegram_message_id=42
        )
        sql = str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )

        self.assertIn("UPDATE source_channels", sql)
        self.assertIn("last_seen_message_id IS NULL", sql)
        self.assertIn("last_seen_message_id < 42", sql)


if __name__ == "__main__":
    unittest.main()
