from __future__ import annotations

import unittest
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.dialects import postgresql

from tgcurator.application.source_lifecycle import SourceMessageLifecycleService
from tgcurator.domain.messages import (
    MediaAsset,
    MediaKind,
    TelegramMessagePart,
    media_assets_to_json,
    normalized_from_parts,
)
from tgcurator.infrastructure.database.models import MessagePartRecord, MessageRecord
from tgcurator.infrastructure.database.source_lifecycle_repository import (
    SqlAlchemySourceMessageLifecycleRepository,
    source_message_deletion_statement,
    source_message_edit_statement,
    source_message_with_component_statement,
)
from tgcurator.shared import DomainValidationError

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


class FakeSourceMessageLifecycleRepository:
    def __init__(self) -> None:
        self.edit_requests: list[tuple[object, ...]] = []
        self.deletion_requests: list[tuple[str, int, datetime]] = []

    async def record_edit(
        self,
        *,
        source_channel_id: str,
        telegram_message_id: int,
        original_text: str | None,
        telegram_metadata: dict[str, object],
        media: tuple[MediaAsset, ...],
        edited_at: datetime,
    ) -> bool:
        self.edit_requests.append(
            (
                source_channel_id,
                telegram_message_id,
                original_text,
                telegram_metadata,
                media,
                edited_at,
            )
        )
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


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        return None


class _Scalars:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def all(self) -> list[object]:
        return self._values


class _Result:
    def __init__(
        self,
        value: object | None = None,
        *,
        scalar_values: list[object] | None = None,
        rowcount: int = 1,
    ) -> None:
        self.value = value
        self.scalar_values = scalar_values or []
        self.rowcount = rowcount

    def one_or_none(self) -> object | None:
        return self.value

    def scalars(self) -> _Scalars:
        return _Scalars(self.scalar_values)


class _Session:
    def __init__(self, results: list[_Result]) -> None:
        self.results = list(results)
        self.statements: list[object] = []

    def begin(self) -> _Transaction:
        return _Transaction()

    async def execute(self, statement: object) -> _Result:
        self.statements.append(statement)
        if not self.results:
            raise AssertionError("unexpected execute() call")
        return self.results.pop(0)


class _Database:
    def __init__(self, session: _Session) -> None:
        self.session_value = session

    @asynccontextmanager
    async def session(self):
        yield self.session_value


class SourceMessageLifecycleServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_forwards_a_complete_canonical_edit_snapshot(self) -> None:
        source_channel_id = str(uuid4())
        repository = FakeSourceMessageLifecycleRepository()
        service = SourceMessageLifecycleService(repository)
        image = MediaAsset(
            "image-71",
            MediaKind.IMAGE,
            original_visual_phash="a" * 16,
            source_telegram_message_id=71,
        )

        self.assertTrue(
            await service.record_edit(
                source_channel_id=source_channel_id,
                telegram_message_id=71,
                original_text="corrected caption",
                telegram_metadata={"views": 7},
                media=(image,),
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
            [(source_channel_id, 71, "corrected caption", {"views": 7}, (image,), NOW)],
        )
        self.assertEqual(repository.deletion_requests, [(source_channel_id, 71, NOW)])

    async def test_service_rejects_invalid_or_incomplete_events_before_repository_access(
        self,
    ) -> None:
        repository = FakeSourceMessageLifecycleRepository()
        service = SourceMessageLifecycleService(repository)
        valid = {
            "source_channel_id": str(uuid4()),
            "telegram_message_id": 71,
            "original_text": "caption",
            "telegram_metadata": {},
            "media": (),
            "edited_at": NOW,
        }

        invalid_values = (
            {**valid, "source_channel_id": "not-a-uuid"},
            {**valid, "telegram_message_id": True},
            {**valid, "original_text": object()},
            {**valid, "telegram_metadata": {1: "not-string-key"}},
            {**valid, "media": []},
            {**valid, "edited_at": datetime(2026, 9, 9, 12, 0)},
        )
        for values in invalid_values:
            with self.subTest(values=values), self.assertRaises(DomainValidationError):
                await service.record_edit(**values)  # type: ignore[arg-type]

        self.assertEqual(repository.edit_requests, [])


class SourceMessageLifecycleStatementTests(unittest.TestCase):
    def test_statements_lock_components_and_replace_only_rich_source_snapshots(self) -> None:
        source_channel_id = uuid4()
        image = MediaAsset("image-71", MediaKind.IMAGE, source_telegram_message_id=71)

        lock_sql = _compile(
            source_message_with_component_statement(
                source_channel_id=source_channel_id, telegram_message_id=71
            )
        )
        edit_sql = _compile(
            source_message_edit_statement(
                message_part_id=uuid4(),
                original_text="corrected",
                telegram_metadata={"views": 7},
                media=(image,),
                edited_at=NOW,
            )
        )
        deletion_sql = _compile(
            source_message_deletion_statement(
                message_id=uuid4(), deleted_at=NOW, changed_after_processing=True
            )
        )

        self.assertIn("FROM message_parts JOIN messages", lock_sql)
        self.assertIn("FOR UPDATE", lock_sql)
        for column in ("original_text", "telegram_metadata", "media", "media_count", "edited_at"):
            self.assertIn(column, edit_sql)
        self.assertNotIn("processing_status=", edit_sql)
        self.assertIn("source_deleted=", deletion_sql)
        self.assertIn("source_deleted_at IS NULL", deletion_sql)
        self.assertIn("source_changed_after_processing=", deletion_sql)

        message_constraints = {item.name for item in MessageRecord.__table__.constraints}
        part_constraints = {item.name for item in MessagePartRecord.__table__.constraints}
        self.assertIn("ck_messages_message_edited_after_published", message_constraints)
        self.assertIn("ck_message_parts_message_part_edited_after_published", part_constraints)


class SourceMessageLifecycleRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_newer_album_edit_rebuilds_all_parts_and_wakes_new_media(self) -> None:
        source_channel_id = str(uuid4())
        parent_id = uuid4()
        image_asset_id = uuid4()
        video_asset_id = uuid4()
        image = MediaAsset(
            "image-70",
            MediaKind.IMAGE,
            original_visual_phash="a" * 16,
            source_telegram_message_id=70,
        )
        video = MediaAsset(
            "video-71",
            MediaKind.VIDEO,
            video_cover_phash="b" * 16,
            source_telegram_message_id=71,
        )
        first = _part(
            parent_id=parent_id,
            source_channel_id=source_channel_id,
            part=TelegramMessagePart(70, NOW - timedelta(minutes=1), media=(image,)),
        )
        edited = _part(
            parent_id=parent_id,
            source_channel_id=source_channel_id,
            part=TelegramMessagePart(71, NOW, original_text="old"),
        )
        parent = _parent(
            parent_id=parent_id,
            source_channel_id=source_channel_id,
            grouped_id=900,
            processing_status="processed",
        )
        session = _Session(
            [
                _Result((edited, parent)),
                _Result(),
                _Result(scalar_values=[first, edited]),
                _Result(),
                _Result(),
                _Result(scalar_values=[image_asset_id]),
                _Result(),
                _Result(),
                _Result(scalar_values=[video_asset_id]),
                _Result(),
            ]
        )
        repository = SqlAlchemySourceMessageLifecycleRepository(_Database(session))  # type: ignore[arg-type]

        self.assertTrue(
            await repository.record_edit(
                source_channel_id=source_channel_id,
                telegram_message_id=71,
                original_text="corrected",
                telegram_metadata={"views": 9},
                media=(video,),
                edited_at=NOW + timedelta(minutes=1),
            )
        )

        self.assertTrue(parent.source_changed_after_processing)
        expected = normalized_from_parts(
            source_channel_id=source_channel_id,
            telegram_grouped_id=900,
            parts=(
                TelegramMessagePart(70, NOW - timedelta(minutes=1), media=(image,)),
                TelegramMessagePart(
                    71,
                    NOW,
                    edited_at=NOW + timedelta(minutes=1),
                    original_text="corrected",
                    telegram_metadata={"views": 9},
                    media=(video,),
                ),
            ),
        )
        parent_update = session.statements[3].compile(dialect=postgresql.dialect())
        self.assertIn(expected.original_text, parent_update.params.values())
        self.assertIn(expected.visual_fingerprint, parent_update.params.values())
        self.assertIn(
            "image_archive",
            session.statements[6].compile(dialect=postgresql.dialect()).params.values(),
        )
        self.assertIn(
            "video_archive",
            session.statements[9].compile(dialect=postgresql.dialect()).params.values(),
        )

    async def test_missing_or_stale_edit_is_rejected_without_parent_mutation(self) -> None:
        source_channel_id = str(uuid4())
        parent = _parent(parent_id=uuid4(), source_channel_id=source_channel_id, grouped_id=None)
        part = _part(
            parent_id=parent.id,
            source_channel_id=source_channel_id,
            part=TelegramMessagePart(71, NOW, edited_at=NOW + timedelta(minutes=2)),
        )
        for result in (None, (part, parent)):
            session = _Session([_Result(result)])
            repository = SqlAlchemySourceMessageLifecycleRepository(_Database(session))  # type: ignore[arg-type]
            self.assertFalse(
                await repository.record_edit(
                    source_channel_id=source_channel_id,
                    telegram_message_id=71,
                    original_text="stale",
                    telegram_metadata={},
                    media=(),
                    edited_at=NOW + timedelta(minutes=1),
                )
            )
            self.assertEqual(len(session.statements), 1)

    async def test_deletion_records_first_observation_without_regressing_history(self) -> None:
        source_channel_id = str(uuid4())
        parent = _parent(
            parent_id=uuid4(),
            source_channel_id=source_channel_id,
            grouped_id=None,
            processing_status="processed",
        )
        part = _part(
            parent_id=parent.id,
            source_channel_id=source_channel_id,
            part=TelegramMessagePart(71, NOW),
        )
        session = _Session([_Result((part, parent)), _Result(rowcount=1)])
        repository = SqlAlchemySourceMessageLifecycleRepository(_Database(session))  # type: ignore[arg-type]

        self.assertTrue(
            await repository.record_deletion(
                source_channel_id=source_channel_id,
                telegram_message_id=71,
                deleted_at=NOW + timedelta(minutes=1),
            )
        )
        deletion_params = session.statements[1].compile(dialect=postgresql.dialect()).params
        self.assertIn(True, deletion_params.values())

        parent.source_deleted_at = NOW + timedelta(minutes=1)
        duplicate_session = _Session([_Result((part, parent))])
        duplicate_repository = SqlAlchemySourceMessageLifecycleRepository(
            _Database(duplicate_session)  # type: ignore[arg-type]
        )
        self.assertFalse(
            await duplicate_repository.record_deletion(
                source_channel_id=source_channel_id,
                telegram_message_id=71,
                deleted_at=NOW,
            )
        )
        self.assertEqual(len(duplicate_session.statements), 1)


def _parent(
    *,
    parent_id: UUID,
    source_channel_id: str,
    grouped_id: int | None,
    processing_status: str = "ingested",
) -> MessageRecord:
    return MessageRecord(
        id=parent_id,
        source_channel_id=UUID(source_channel_id),
        telegram_message_ids=[71],
        telegram_grouped_id=grouped_id,
        primary_telegram_message_id=71,
        published_at=NOW,
        edited_at=None,
        original_text=None,
        telegram_metadata={},
        processing_status=processing_status,
        media_count=0,
        source_changed_after_processing=False,
        source_deleted=False,
        source_deleted_at=None,
    )


def _part(
    *, parent_id: UUID, source_channel_id: str, part: TelegramMessagePart
) -> MessagePartRecord:
    return MessagePartRecord(
        id=uuid4(),
        message_id=parent_id,
        source_channel_id=UUID(source_channel_id),
        telegram_message_id=part.telegram_message_id,
        published_at=part.published_at,
        edited_at=part.edited_at,
        original_text=part.original_text,
        telegram_metadata=dict(part.telegram_metadata or {}),
        media=media_assets_to_json(part.media),
        media_count=len(part.media),
    )


def _compile(statement: object) -> str:
    return str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False})
    )


if __name__ == "__main__":
    unittest.main()
