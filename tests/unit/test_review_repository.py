from __future__ import annotations

import unittest
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.dialects import postgresql

from tgcurator.domain.review import ManualLabelEvent, ManualLabelOperation, ReviewStatus
from tgcurator.infrastructure.database.models import (
    LabelDefinitionVersionRecord,
    ManualLabelAssignmentRecord,
    MessageRecord,
    MessageReviewEventRecord,
)
from tgcurator.infrastructure.database.review_repository import SqlAlchemyReviewRepository
from tgcurator.shared import DomainValidationError

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
MESSAGE_ID = UUID("11111111-1111-4111-8111-111111111111")
LABEL_VERSION_ID = UUID("22222222-2222-4222-8222-222222222222")
ADMIN_ID = UUID("33333333-3333-4333-8333-333333333333")


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        return None


class FakeSession:
    def __init__(
        self,
        *,
        scalar_results: list[object | None] | None = None,
        scalar_batches: list[tuple[object, ...]] | None = None,
    ) -> None:
        self.scalar_results = list(scalar_results or [])
        self.scalar_batches = list(scalar_batches or [])
        self.scalar_statements: list[object] = []
        self.scalars_statements: list[object] = []
        self.added: list[object] = []

    def begin(self) -> FakeTransaction:
        return FakeTransaction()

    async def scalar(self, statement: object) -> object | None:
        self.scalar_statements.append(statement)
        if not self.scalar_results:
            raise AssertionError("unexpected scalar call")
        return self.scalar_results.pop(0)

    async def scalars(self, statement: object) -> tuple[object, ...]:
        self.scalars_statements.append(statement)
        if not self.scalar_batches:
            raise AssertionError("unexpected scalars call")
        return self.scalar_batches.pop(0)

    def add(self, value: object) -> None:
        self.added.append(value)


class FakeDatabase:
    def __init__(self, session: FakeSession) -> None:
        self.fake_session = session

    @asynccontextmanager
    async def session(self):
        yield self.fake_session


def compiled_sql(statement: object) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def message(*, review_status: str = "unreviewed") -> MessageRecord:
    return MessageRecord(id=MESSAGE_ID, review_status=review_status)


def label_version(*, key: str = "real_ugc", scope: str = "global") -> LabelDefinitionVersionRecord:
    return LabelDefinitionVersionRecord(
        id=LABEL_VERSION_ID,
        state="published",
        enabled=True,
        key=key,
        scope=scope,
    )


def manual_event(
    *,
    target_kind: str = "message",
    target_scope: str = "global",
    target_id: UUID = MESSAGE_ID,
) -> ManualLabelEvent:
    return ManualLabelEvent(
        event_id=str(uuid4()),
        message_id=str(MESSAGE_ID),
        target_scope=target_scope,
        target_kind=target_kind,
        target_id=str(target_id),
        image_asset_id=(str(target_id) if target_kind == "image_asset" else None),
        video_asset_id=(str(target_id) if target_kind == "video_asset" else None),
        label_definition_version_id=str(LABEL_VERSION_ID),
        label_key="real_ugc",
        operation=ManualLabelOperation.SET,
        score=0.88,
        activated=True,
        actor_admin_user_id=str(ADMIN_ID),
        created_at=NOW,
    )


class ReviewRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_label_append_locks_message_and_adds_only_history(self) -> None:
        session = FakeSession(scalar_results=[message(), label_version()])
        repository = SqlAlchemyReviewRepository(FakeDatabase(session))  # type: ignore[arg-type]
        event = manual_event()

        returned = await repository.append_manual_label(event=event)

        self.assertIs(returned, event)
        self.assertIn("FOR UPDATE", compiled_sql(session.scalar_statements[0]))
        self.assertEqual(len(session.added), 1)
        row = session.added[0]
        self.assertIsInstance(row, ManualLabelAssignmentRecord)
        assert isinstance(row, ManualLabelAssignmentRecord)
        self.assertEqual(row.message_id, MESSAGE_ID)
        self.assertEqual(row.operation, "set")
        self.assertEqual(row.label_key, "real_ugc")

    async def test_label_version_and_media_ownership_are_validated_before_insert(self) -> None:
        mismatched_session = FakeSession(
            scalar_results=[message(), label_version(key="advertisement")]
        )
        mismatched = SqlAlchemyReviewRepository(
            FakeDatabase(mismatched_session)  # type: ignore[arg-type]
        )
        with self.assertRaisesRegex(DomainValidationError, "label key"):
            await mismatched.append_manual_label(event=manual_event())
        self.assertEqual(mismatched_session.added, [])

        image_id = uuid4()
        media_session = FakeSession(scalar_results=[message(), label_version(scope="media"), None])
        media_repository = SqlAlchemyReviewRepository(
            FakeDatabase(media_session)  # type: ignore[arg-type]
        )
        media = manual_event(
            target_kind="image_asset",
            target_scope="media",
            target_id=image_id,
        )
        with self.assertRaisesRegex(DomainValidationError, "does not belong"):
            await media_repository.append_manual_label(event=media)
        self.assertEqual(media_session.added, [])

    async def test_review_transition_updates_only_workflow_state_and_appends_event(self) -> None:
        row = message()
        session = FakeSession(scalar_results=[row])
        repository = SqlAlchemyReviewRepository(FakeDatabase(session))  # type: ignore[arg-type]

        event = await repository.transition_review_status(
            event_id=str(uuid4()),
            message_id=str(MESSAGE_ID),
            new_status=ReviewStatus.REVIEWED,
            actor_admin_user_id=str(ADMIN_ID),
            created_at=NOW,
            reason="checked",
        )

        self.assertEqual(row.review_status, "reviewed")
        self.assertEqual(row.updated_at, NOW)
        self.assertEqual(event.old_status, ReviewStatus.UNREVIEWED)
        self.assertEqual(event.new_status, ReviewStatus.REVIEWED)
        self.assertEqual(len(session.added), 1)
        self.assertIsInstance(session.added[0], MessageReviewEventRecord)
        self.assertNotIsInstance(session.added[0], ManualLabelAssignmentRecord)

    async def test_history_queries_are_stably_ordered(self) -> None:
        manual = ManualLabelAssignmentRecord(
            id=uuid4(),
            message_id=MESSAGE_ID,
            target_scope="global",
            target_kind="message",
            target_id=MESSAGE_ID,
            image_asset_id=None,
            video_asset_id=None,
            label_definition_version_id=LABEL_VERSION_ID,
            label_key="real_ugc",
            operation="set",
            score=0.8,
            activated=True,
            actor_admin_user_id=ADMIN_ID,
            reason=None,
            created_at=NOW,
        )
        review = MessageReviewEventRecord(
            id=uuid4(),
            message_id=MESSAGE_ID,
            old_status="unreviewed",
            new_status="reviewed",
            actor_admin_user_id=ADMIN_ID,
            reason=None,
            created_at=NOW,
        )
        session = FakeSession(scalar_batches=[(manual,), (review,)])
        repository = SqlAlchemyReviewRepository(FakeDatabase(session))  # type: ignore[arg-type]

        manual_history = await repository.list_manual_label_history(message_id=str(MESSAGE_ID))
        review_history = await repository.list_review_history(message_id=str(MESSAGE_ID))

        self.assertEqual(manual_history[0].event_id, str(manual.id))
        self.assertEqual(review_history[0].event_id, str(review.id))
        for statement in session.scalars_statements:
            sql = compiled_sql(statement)
            self.assertIn("created_at", sql)
            self.assertIn("ORDER BY", sql)


if __name__ == "__main__":
    unittest.main()
