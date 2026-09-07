from __future__ import annotations

import unittest
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from tgcurator.domain.messages import (
    MessageContent,
    NormalizedTelegramMessage,
    TelegramMessage,
    normalize_telegram_messages,
)
from tgcurator.infrastructure.database.message_ingest_repository import (
    image_archive_wakeups_insert_statement,
    message_parts_upsert_statement,
    message_upsert_statement,
)

NOW = datetime(2026, 9, 4, 12, tzinfo=UTC)


class MessageIngestRepositoryStatementTests(unittest.TestCase):
    def test_regular_message_uses_stable_source_anchor_constraint(self) -> None:
        message = normalize_telegram_messages((TelegramMessage(str(uuid4()), 5, NOW),))[0]

        sql = str(
            message_upsert_statement(message).compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}
            )
        )

        self.assertIn("ON CONFLICT ON CONSTRAINT uq_message_source_anchor DO UPDATE", sql)
        self.assertIn("RETURNING messages.id", sql)

    def test_album_uses_source_group_partial_unique_index_and_parts_do_nothing(self) -> None:
        message = NormalizedTelegramMessage(
            source_channel_id=str(uuid4()),
            telegram_anchor_message_id=5,
            telegram_message_ids=(5, 7),
            sent_at=NOW,
            content=MessageContent(),
            telegram_group_id=90,
        )

        album_sql = str(
            message_upsert_statement(message).compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}
            )
        )
        parts_sql = str(
            message_parts_upsert_statement(message_id=uuid4(), message=message).compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}
            )
        )

        self.assertIn(
            (
                "ON CONFLICT (source_channel_id, telegram_group_id) "
                "WHERE telegram_group_id IS NOT NULL"
            ),
            album_sql,
        )
        self.assertIn(
            "ON CONFLICT ON CONSTRAINT uq_message_part_source_telegram DO NOTHING", parts_sql
        )

    def test_image_archive_wakeups_are_durable_uuid_only_signals(self) -> None:
        statement = image_archive_wakeups_insert_statement(image_asset_ids=(uuid4(), uuid4()))
        self.assertIsNotNone(statement)
        assert statement is not None
        compiled = statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}
        )
        sql = str(compiled)

        self.assertIn("INSERT INTO durable_wakeups", sql)
        self.assertIn("ON CONFLICT ON CONSTRAINT uq_durable_wakeup_queue_entity DO NOTHING", sql)
        self.assertIn("image_archive", compiled.params.values())
        self.assertIsNone(image_archive_wakeups_insert_statement(image_asset_ids=()))


if __name__ == "__main__":
    unittest.main()
