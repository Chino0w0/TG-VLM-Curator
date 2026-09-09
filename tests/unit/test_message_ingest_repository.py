from __future__ import annotations

import unittest
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.dialects import postgresql

from tgcurator.domain.messages import (
    MediaAsset,
    MediaKind,
    TelegramMessage,
    TelegramMessagePart,
    media_assets_to_json,
    normalize_telegram_messages,
    normalized_from_parts,
)
from tgcurator.infrastructure.database.message_ingest_repository import (
    SqlAlchemyTelegramMessageIngestRepository,
    image_archive_wakeups_insert_statement,
    image_assets_upsert_statement,
    message_parent_update_statement,
    message_parts_upsert_statement,
    message_upsert_statement,
    video_archive_wakeups_insert_statement,
    video_assets_upsert_statement,
)
from tgcurator.infrastructure.database.models import MessagePartRecord
from tgcurator.shared import DomainValidationError

NOW = datetime(2026, 9, 4, 12, tzinfo=UTC)


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
        *,
        scalar_value: object | None = None,
        rows: list[tuple[object, ...]] | None = None,
        scalar_values: list[object] | None = None,
    ) -> None:
        self._scalar_value = scalar_value
        self._rows = rows or []
        self._scalar_values = scalar_values or []

    def scalar_one(self) -> object:
        if self._scalar_value is None:
            raise AssertionError("scalar_one() has no configured value")
        return self._scalar_value

    def all(self) -> list[tuple[object, ...]]:
        return self._rows

    def scalars(self) -> _Scalars:
        return _Scalars(self._scalar_values)


class _Session:
    def __init__(self, results: list[_Result]) -> None:
        self._results = list(results)
        self.statements: list[object] = []

    def begin(self) -> _Transaction:
        return _Transaction()

    async def execute(self, statement: object) -> _Result:
        self.statements.append(statement)
        if not self._results:
            raise AssertionError("unexpected execute() call")
        return self._results.pop(0)


class _Database:
    def __init__(self, session: _Session) -> None:
        self._session = session

    @asynccontextmanager
    async def session(self):
        yield self._session


class MessageIngestRepositoryStatementTests(unittest.TestCase):
    def test_regular_and_album_parents_use_stable_partial_unique_indexes(self) -> None:
        source_channel_id = str(uuid4())
        regular = normalize_telegram_messages((TelegramMessage(source_channel_id, 5, NOW),))[0]
        album = normalize_telegram_messages(
            (
                TelegramMessage(source_channel_id, 7, NOW, telegram_grouped_id=90),
                TelegramMessage(source_channel_id, 9, NOW, telegram_grouped_id=90),
            )
        )[0]

        regular_sql = _compile(message_upsert_statement(regular))
        album_sql = _compile(message_upsert_statement(album))

        self.assertIn(
            "ON CONFLICT (source_channel_id, primary_telegram_message_id) "
            "WHERE telegram_grouped_id IS NULL",
            regular_sql,
        )
        self.assertIn(
            "ON CONFLICT (source_channel_id, telegram_grouped_id) "
            "WHERE telegram_grouped_id IS NOT NULL",
            album_sql,
        )
        self.assertIn("RETURNING messages.id", regular_sql)
        self.assertIn("RETURNING messages.id", album_sql)

    def test_part_upsert_is_rich_owner_safe_and_rejects_stale_edits(self) -> None:
        message = normalize_telegram_messages(
            (
                TelegramMessage(
                    str(uuid4()),
                    5,
                    NOW,
                    edited_at=NOW + timedelta(minutes=1),
                    original_text="corrected",
                    telegram_metadata={"views": 4},
                    media=(
                        MediaAsset(
                            "image-5",
                            MediaKind.IMAGE,
                            original_visual_phash="a" * 16,
                            source_telegram_message_id=5,
                        ),
                    ),
                ),
            )
        )[0]

        sql = _compile(message_parts_upsert_statement(message_id=uuid4(), message=message))

        for column in (
            "published_at",
            "edited_at",
            "original_text",
            "telegram_metadata",
            "media",
            "media_count",
        ):
            self.assertIn(column, sql)
        self.assertIn("ON CONFLICT ON CONSTRAINT uq_message_part_source_telegram DO UPDATE", sql)
        self.assertIn("message_parts.message_id = excluded.message_id", sql)
        self.assertIn("message_parts.edited_at IS NULL", sql)
        self.assertIn("excluded.edited_at IS NOT NULL", sql)
        self.assertIn("message_parts.edited_at <= excluded.edited_at", sql)

    def test_asset_upserts_preserve_archive_state_and_wakeups_are_uuid_only(self) -> None:
        source_channel_id = str(uuid4())
        message = normalize_telegram_messages(
            (
                TelegramMessage(
                    source_channel_id,
                    5,
                    NOW,
                    media=(
                        MediaAsset(
                            "image-5",
                            MediaKind.IMAGE,
                            original_visual_phash="a" * 16,
                            source_telegram_message_id=5,
                        ),
                        MediaAsset(
                            "video-5",
                            MediaKind.VIDEO,
                            video_cover_phash="b" * 16,
                            source_telegram_message_id=5,
                        ),
                    ),
                ),
            )
        )[0]

        image_statement = image_assets_upsert_statement(message_id=uuid4(), message=message)
        video_statement = video_assets_upsert_statement(message_id=uuid4(), message=message)
        assert image_statement is not None
        assert video_statement is not None
        image_update_sql = _compile(image_statement).split("DO UPDATE SET", maxsplit=1)[1]
        video_update_sql = _compile(video_statement).split("DO UPDATE SET", maxsplit=1)[1]

        self.assertNotIn("archive_state", image_update_sql)
        self.assertNotIn("archive_state", video_update_sql)
        self.assertNotIn("archive_attempt_count", image_update_sql)
        self.assertNotIn("archive_attempt_count", video_update_sql)

        image_asset_id = uuid4()
        video_asset_id = uuid4()
        image_wakeup = image_archive_wakeups_insert_statement(image_asset_ids=(image_asset_id,))
        video_wakeup = video_archive_wakeups_insert_statement(video_asset_ids=(video_asset_id,))
        assert image_wakeup is not None
        assert video_wakeup is not None
        for statement, queue, entity_id in (
            (image_wakeup, "image_archive", image_asset_id),
            (video_wakeup, "video_archive", video_asset_id),
        ):
            compiled = statement.compile(dialect=postgresql.dialect())
            sql = str(compiled)
            self.assertIn("INSERT INTO durable_wakeups", sql)
            self.assertIn(
                "ON CONFLICT ON CONSTRAINT uq_durable_wakeup_queue_entity DO NOTHING", sql
            )
            self.assertIn(queue, compiled.params.values())
            self.assertIn(entity_id, compiled.params.values())
            self.assertNotIn("source_channel_id", sql)
            self.assertNotIn("telegram_message_id", sql)

    def test_parent_update_changes_only_current_source_snapshot(self) -> None:
        message = normalize_telegram_messages(
            (TelegramMessage(str(uuid4()), 5, NOW, original_text="caption"),)
        )[0]

        sql = _compile(message_parent_update_statement(message_id=uuid4(), message=message))

        self.assertIn("UPDATE messages SET", sql)
        self.assertNotIn("processing_status=", sql)
        self.assertNotIn("source_deleted=", sql)
        self.assertNotIn("source_changed_after_processing=", sql)


class MessageIngestRepositoryBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_album_replay_rebuilds_parent_from_all_retained_parts(self) -> None:
        source_channel_id = str(uuid4())
        parent_id = uuid4()
        image_asset_id = uuid4()
        video_asset_id = uuid4()
        image = MediaAsset(
            "image-5",
            MediaKind.IMAGE,
            original_visual_phash="a" * 16,
            source_telegram_message_id=5,
        )
        video = MediaAsset(
            "video-7",
            MediaKind.VIDEO,
            video_cover_phash="b" * 16,
            source_telegram_message_id=7,
        )
        incoming = normalize_telegram_messages(
            (
                TelegramMessage(
                    source_channel_id,
                    7,
                    NOW + timedelta(seconds=2),
                    original_text="second caption",
                    telegram_grouped_id=90,
                    media=(video,),
                ),
            )
        )[0]
        retained = (
            _part_record(
                message_id=parent_id,
                source_channel_id=source_channel_id,
                part=TelegramMessagePart(
                    telegram_message_id=5,
                    published_at=NOW,
                    original_text="first caption",
                    media=(image,),
                ),
            ),
            _part_record(
                message_id=parent_id,
                source_channel_id=source_channel_id,
                part=incoming.parts[0],
            ),
        )
        expected = normalized_from_parts(
            source_channel_id=source_channel_id,
            telegram_grouped_id=90,
            parts=tuple(
                TelegramMessagePart(
                    telegram_message_id=record.telegram_message_id,
                    published_at=record.published_at,
                    edited_at=record.edited_at,
                    original_text=record.original_text,
                    telegram_metadata=record.telegram_metadata,
                    media=(image,) if record.telegram_message_id == 5 else (video,),
                )
                for record in retained
            ),
        )
        session = _Session(
            [
                _Result(scalar_value=parent_id),
                _Result(),
                _Result(rows=[(7, parent_id)]),
                _Result(scalar_values=list(retained)),
                _Result(),
                _Result(),
                _Result(scalar_values=[image_asset_id]),
                _Result(),
                _Result(),
                _Result(scalar_values=[video_asset_id]),
                _Result(),
            ]
        )
        repository = SqlAlchemyTelegramMessageIngestRepository(_Database(session))  # type: ignore[arg-type]

        await repository.upsert_message(message=incoming)

        parent_update = session.statements[4].compile(dialect=postgresql.dialect())
        self.assertIn(list(expected.telegram_message_ids), parent_update.params.values())
        self.assertIn(expected.primary_telegram_message_id, parent_update.params.values())
        self.assertIn(expected.original_text, parent_update.params.values())
        self.assertIn(expected.media_count, parent_update.params.values())
        self.assertIn(expected.visual_fingerprint, parent_update.params.values())
        image_wakeup = session.statements[7].compile(dialect=postgresql.dialect())
        video_wakeup = session.statements[10].compile(dialect=postgresql.dialect())
        self.assertIn("image_archive", image_wakeup.params.values())
        self.assertIn(image_asset_id, image_wakeup.params.values())
        self.assertIn("video_archive", video_wakeup.params.values())
        self.assertIn(video_asset_id, video_wakeup.params.values())

    async def test_component_owned_by_another_parent_aborts_before_rebuild(self) -> None:
        source_channel_id = str(uuid4())
        parent_id = uuid4()
        other_parent_id = uuid4()
        incoming = normalize_telegram_messages((TelegramMessage(source_channel_id, 7, NOW),))[0]
        session = _Session(
            [
                _Result(scalar_value=parent_id),
                _Result(),
                _Result(rows=[(7, other_parent_id)]),
            ]
        )
        repository = SqlAlchemyTelegramMessageIngestRepository(_Database(session))  # type: ignore[arg-type]

        with self.assertRaisesRegex(DomainValidationError, "different logical message: 7"):
            await repository.upsert_message(message=incoming)

        self.assertEqual(len(session.statements), 3)


def _part_record(
    *, message_id: UUID, source_channel_id: str, part: TelegramMessagePart
) -> MessagePartRecord:
    return MessagePartRecord(
        id=uuid4(),
        message_id=message_id,
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
