from __future__ import annotations

import unittest
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from tgcurator.application.ports.ingestion import IngestReport
from tgcurator.application.ports.processing import LatestMessageBoundary
from tgcurator.application.reconciliation import (
    ReconnectTelegramReconciliation,
    SourceReconciliationService,
)
from tgcurator.domain.messages import TelegramMessage
from tgcurator.infrastructure.database.models import SourceChannel
from tgcurator.infrastructure.database.reconciliation_repository import (
    source_last_seen_cursor_advance_statement,
    source_latest_seen_cursor_advance_statement,
)
from tgcurator.shared import DomainValidationError

NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)


class FakeSourceReconciliationCursorRepository:
    def __init__(self, *, latest: int | None = None, last: int | None = None) -> None:
        self.latest = latest
        self.last = last
        self.requests: list[tuple[str, str, int | None]] = []

    async def advance_latest_seen_message_id(
        self, *, source_channel_id: str, telegram_message_id: int
    ) -> bool:
        self.requests.append(("advance_latest", source_channel_id, telegram_message_id))
        if self.latest is None or self.latest < telegram_message_id:
            self.latest = telegram_message_id
            return True
        return False

    async def advance_last_seen_message_id(
        self, *, source_channel_id: str, telegram_message_id: int
    ) -> bool:
        self.requests.append(("advance_last", source_channel_id, telegram_message_id))
        if self.last is None or self.last < telegram_message_id:
            self.last = telegram_message_id
            return True
        return False

    async def get_latest_seen_message_id(self, *, source_channel_id: str) -> int | None:
        self.requests.append(("read_latest", source_channel_id, None))
        return self.latest

    async def get_last_seen_message_id(self, *, source_channel_id: str) -> int | None:
        self.requests.append(("read_last", source_channel_id, None))
        return self.last


class SourceReconciliationCursorTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_owns_independent_monotonic_latest_and_ingested_cursors(self) -> None:
        source_channel_id = str(uuid4())
        repository = FakeSourceReconciliationCursorRepository()
        service = SourceReconciliationService(repository)

        self.assertTrue(
            await service.record_latest_seen_message(
                source_channel_id=source_channel_id, telegram_message_id=43
            )
        )
        self.assertTrue(
            await service.record_ingested_message(
                source_channel_id=source_channel_id, telegram_message_id=42
            )
        )
        self.assertFalse(
            await service.record_ingested_message(
                source_channel_id=source_channel_id, telegram_message_id=41
            )
        )
        self.assertEqual(
            await service.latest_seen_message_id(source_channel_id=source_channel_id), 43
        )
        self.assertEqual(
            await service.last_seen_message_id(source_channel_id=source_channel_id), 42
        )

    async def test_service_rejects_invalid_identifiers_before_repository_access(self) -> None:
        repository = FakeSourceReconciliationCursorRepository()
        service = SourceReconciliationService(repository)

        for source_channel_id, message_id in (
            (" ", 1),
            ("not-a-uuid", 1),
            (str(uuid4()), 0),
            (str(uuid4()), True),
        ):
            with (
                self.subTest(source=source_channel_id, message_id=message_id),
                self.assertRaises(DomainValidationError),
            ):
                await service.record_latest_seen_message(
                    source_channel_id=source_channel_id,
                    telegram_message_id=message_id,
                )

        self.assertEqual(repository.requests, [])

    def test_schema_and_postgresql_statements_prevent_both_cursor_regressions(self) -> None:
        constraints = {item.name for item in SourceChannel.__table__.constraints}
        self.assertIn("ck_source_channels_source_channel_latest_message_positive", constraints)
        self.assertIn("ck_source_channels_source_channel_last_seen_positive", constraints)

        for statement, column in (
            (
                source_latest_seen_cursor_advance_statement(
                    source_channel_id=uuid4(), telegram_message_id=42
                ),
                "latest_seen_message_id",
            ),
            (
                source_last_seen_cursor_advance_statement(
                    source_channel_id=uuid4(), telegram_message_id=42
                ),
                "last_seen_message_id",
            ),
        ):
            sql = str(
                statement.compile(
                    dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
                )
            )
            self.assertIn(f"{column} IS NULL", sql)
            self.assertIn(f"{column} < 42", sql)


class ReconnectTelegramReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def test_freezes_newest_boundary_then_advances_only_after_ingestion(self) -> None:
        source_channel_id = str(uuid4())
        events: list[str] = []
        messages = (
            TelegramMessage(source_channel_id, 6, NOW),
            TelegramMessage(source_channel_id, 8, NOW),
        )
        repository = _EventCursorRepository(events=events, last=5)
        service = ReconnectTelegramReconciliation(
            telegram_gateway=_Gateway(events=events, newest_id=8, messages=messages),
            message_ingest_service=_Ingestion(events=events),
            cursor_service=SourceReconciliationService(repository),
        )

        report = await service.reconcile(source_channel_id=source_channel_id)

        self.assertEqual(report.from_message_id_exclusive, 5)
        self.assertEqual(report.to_message_id_inclusive, 8)
        self.assertEqual(report.component_messages, 2)
        self.assertTrue(report.last_seen_advanced)
        self.assertEqual(
            events,
            ["read_last", "newest", "advance_latest:8", "fetch:5:8", "ingest", "advance_last:8"],
        )

    async def test_empty_frozen_window_may_advance_but_ingest_failure_never_does(self) -> None:
        source_channel_id = str(uuid4())
        repository = FakeSourceReconciliationCursorRepository(last=5)
        empty_service = ReconnectTelegramReconciliation(
            telegram_gateway=_Gateway(events=[], newest_id=8, messages=()),
            message_ingest_service=_Ingestion(events=[]),
            cursor_service=SourceReconciliationService(repository),
        )

        report = await empty_service.reconcile(source_channel_id=source_channel_id)
        self.assertEqual(report.component_messages, 0)
        self.assertEqual(repository.last, 8)

        failed_repository = FakeSourceReconciliationCursorRepository(last=5)
        failed_service = ReconnectTelegramReconciliation(
            telegram_gateway=_Gateway(
                events=[],
                newest_id=8,
                messages=(TelegramMessage(source_channel_id, 6, NOW),),
            ),
            message_ingest_service=_Ingestion(events=[], fail=True),
            cursor_service=SourceReconciliationService(failed_repository),
        )
        with self.assertRaisesRegex(RuntimeError, "ingest failed"):
            await failed_service.reconcile(source_channel_id=source_channel_id)
        self.assertEqual(failed_repository.last, 5)

    async def test_rejects_wrong_source_or_out_of_window_history_before_ingestion(self) -> None:
        source_channel_id = str(uuid4())
        cases = (
            TelegramMessage(str(uuid4()), 6, NOW),
            TelegramMessage(source_channel_id, 5, NOW),
            TelegramMessage(source_channel_id, 9, NOW),
        )
        for message in cases:
            ingestion = _Ingestion(events=[])
            service = ReconnectTelegramReconciliation(
                telegram_gateway=_Gateway(events=[], newest_id=8, messages=(message,)),
                message_ingest_service=ingestion,
                cursor_service=SourceReconciliationService(
                    FakeSourceReconciliationCursorRepository(last=5)
                ),
            )
            with (
                self.subTest(message=message.telegram_message_id),
                self.assertRaises(DomainValidationError),
            ):
                await service.reconcile(source_channel_id=source_channel_id)
            self.assertEqual(ingestion.batches, [])


class _EventCursorRepository(FakeSourceReconciliationCursorRepository):
    def __init__(self, *, events: list[str], last: int | None) -> None:
        super().__init__(last=last)
        self.events = events

    async def get_last_seen_message_id(self, *, source_channel_id: str) -> int | None:
        self.events.append("read_last")
        return self.last

    async def advance_latest_seen_message_id(
        self, *, source_channel_id: str, telegram_message_id: int
    ) -> bool:
        self.events.append(f"advance_latest:{telegram_message_id}")
        return await super().advance_latest_seen_message_id(
            source_channel_id=source_channel_id, telegram_message_id=telegram_message_id
        )

    async def advance_last_seen_message_id(
        self, *, source_channel_id: str, telegram_message_id: int
    ) -> bool:
        self.events.append(f"advance_last:{telegram_message_id}")
        return await super().advance_last_seen_message_id(
            source_channel_id=source_channel_id, telegram_message_id=telegram_message_id
        )


class _Gateway:
    def __init__(
        self,
        *,
        events: list[str],
        newest_id: int | None,
        messages: tuple[TelegramMessage, ...],
    ) -> None:
        self.events = events
        self.newest_id = newest_id
        self.messages = messages

    async def newest_message(self, *, source_channel_id: str):
        self.events.append("newest")
        if self.newest_id is None:
            return None
        return LatestMessageBoundary(message_id=self.newest_id, published_at=NOW)

    async def fetch_history(
        self,
        *,
        source_channel_id: str,
        from_message_id_exclusive: int,
        to_message_id_inclusive: int,
    ) -> tuple[TelegramMessage, ...]:
        self.events.append(f"fetch:{from_message_id_exclusive}:{to_message_id_inclusive}")
        return self.messages


class _Ingestion:
    def __init__(self, *, events: list[str], fail: bool = False) -> None:
        self.events = events
        self.fail = fail
        self.batches: list[tuple[TelegramMessage, ...]] = []

    async def ingest_history(self, messages: tuple[TelegramMessage, ...]) -> IngestReport:
        self.events.append("ingest")
        self.batches.append(messages)
        if self.fail:
            raise RuntimeError("ingest failed")
        return IngestReport(logical_messages=len(messages), component_messages=len(messages))


if __name__ == "__main__":
    unittest.main()
