from __future__ import annotations

import unittest
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from tgcurator.application.source_lifecycle import SourceMessageLifecycleService
from tgcurator.infrastructure.database.models import MessageRecord
from tgcurator.infrastructure.database.source_lifecycle_repository import (
    source_message_deletion_statement,
    source_message_edit_statement,
)
from tgcurator.shared import DomainValidationError

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class FakeSourceMessageLifecycleRepository:
    def __init__(self) -> None:
        self.edit_requests: list[tuple[str, int, str | None, datetime]] = []
        self.deletion_requests: list[tuple[str, int, datetime]] = []

    async def record_edit(
        self,
        *,
        source_channel_id: str,
        telegram_message_id: int,
        text: str | None,
        edited_at: datetime,
    ) -> bool:
        self.edit_requests.append((source_channel_id, telegram_message_id, text, edited_at))
        return True

    async def record_deletion(
        self,
        *,
        source_channel_id: str,
        telegram_message_id: int,
        deleted_at: datetime,
    ) -> bool:
        self.deletion_requests.append((source_channel_id, telegram_message_id, deleted_at))
        return True


class SourceMessageLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_preserves_edit_and_deletion_events_without_external_io(self) -> None:
        source_channel_id = str(uuid4())
        repository = FakeSourceMessageLifecycleRepository()
        service = SourceMessageLifecycleService(repository)

        self.assertTrue(
            await service.record_edit(
                source_channel_id=source_channel_id,
                telegram_message_id=71,
                text="corrected caption",
                edited_at=NOW,
            )
        )
        self.assertTrue(
            await service.record_deletion(
                source_channel_id=source_channel_id,
                telegram_message_id=71,
                deleted_at=NOW,
            )
        )

        self.assertEqual(
            repository.edit_requests,
            [(source_channel_id, 71, "corrected caption", NOW)],
        )
        self.assertEqual(repository.deletion_requests, [(source_channel_id, 71, NOW)])

    async def test_service_rejects_invalid_events_before_repository_access(self) -> None:
        repository = FakeSourceMessageLifecycleRepository()
        service = SourceMessageLifecycleService(repository)

        with self.assertRaises(DomainValidationError):
            await service.record_edit(
                source_channel_id="not-a-uuid",
                telegram_message_id=71,
                text="caption",
                edited_at=NOW,
            )
        with self.assertRaises(DomainValidationError):
            await service.record_edit(
                source_channel_id=str(uuid4()),
                telegram_message_id=True,
                text="caption",
                edited_at=NOW,
            )
        with self.assertRaises(DomainValidationError):
            await service.record_edit(
                source_channel_id=str(uuid4()),
                telegram_message_id=71,
                text=object(),  # type: ignore[arg-type]
                edited_at=NOW,
            )
        with self.assertRaises(DomainValidationError):
            await service.record_deletion(
                source_channel_id=str(uuid4()),
                telegram_message_id=71,
                deleted_at=datetime(2026, 9, 4, 12, 0),
            )

        self.assertEqual(repository.edit_requests, [])
        self.assertEqual(repository.deletion_requests, [])

    def test_metadata_and_postgresql_updates_are_monotonic_and_non_destructive(self) -> None:
        constraints = {item.name for item in MessageRecord.__table__.constraints}
        self.assertIn("ck_message_source_edited_after_sent", constraints)

        source_channel_id = uuid4()
        edit_sql = str(
            source_message_edit_statement(
                source_channel_id=source_channel_id,
                telegram_message_id=71,
                text="corrected caption",
                edited_at=NOW,
            ).compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        deletion_sql = str(
            source_message_deletion_statement(
                source_channel_id=source_channel_id,
                telegram_message_id=71,
                deleted_at=NOW,
            ).compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )

        self.assertIn("EXISTS (SELECT message_parts.id", edit_sql)
        self.assertIn("source_edited_at IS NULL", edit_sql)
        self.assertIn("source_edited_at <", edit_sql)
        self.assertIn("source_changed_after_processing=true", edit_sql)
        self.assertIn("source_deleted_at IS NULL", deletion_sql)
        self.assertIn("source_changed_after_processing=true", deletion_sql)


if __name__ == "__main__":
    unittest.main()
