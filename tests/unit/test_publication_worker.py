from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from tgcurator.application.ports.publishing import (
    ClaimedPublicationIntent,
    TelegramFloodWait,
    TelegramMediaUnavailable,
    TelegramPermissionDenied,
    TelegramProtectedContent,
    TelegramPublisherUnavailable,
    TelegramPublishRequest,
    TelegramPublishResult,
    TelegramSessionExpired,
)
from tgcurator.application.publishing import PublicationWorker
from tgcurator.domain.publishing import (
    ProtectedContentFallback,
    PublicationMode,
    PublicationOperation,
    PublicationPart,
)
from tgcurator.infrastructure.observability import OperationsMonitor

NOW = datetime(2026, 9, 10, 8, tzinfo=UTC)


class PublicationWorkerTests(unittest.TestCase):
    def test_executes_all_four_publication_modes(self) -> None:
        cases = (
            (
                PublicationMode.NATIVE_FORWARD_WITH_SUPPLEMENT,
                (PublicationOperation.NATIVE_FORWARD, PublicationOperation.SEND_TEXT),
            ),
            (PublicationMode.COPY_WITH_CAPTION, (PublicationOperation.COPY_WITH_CAPTION,)),
            (PublicationMode.FORWARD_ONLY, (PublicationOperation.NATIVE_FORWARD,)),
            (PublicationMode.METADATA_ONLY, (PublicationOperation.SEND_TEXT,)),
        )

        for mode, expected_operations in cases:
            with self.subTest(mode=mode):
                events: list[tuple[str, Any]] = []
                repository = _Repository(claim=_claim(mode=mode), events=events)
                publisher = _Publisher(
                    publish_results=[TelegramPublishResult((901 + index,)) for index in range(2)],
                    events=events,
                )

                handled = asyncio.run(
                    PublicationWorker(repository=repository, publisher=publisher).process(
                        intent_id=repository.claim_value.intent_id,
                        now=NOW,
                    )
                )

                self.assertTrue(handled)
                self.assertEqual(
                    tuple(request.operation for request in publisher.publish_requests),
                    expected_operations,
                )
                self.assertEqual(len(repository.records), len(expected_operations))
                self.assertTrue(repository.completed)
                for operation in expected_operations:
                    begin_index = events.index(("begin", operation))
                    publish_index = events.index(("publish", operation))
                    self.assertLess(begin_index, publish_index)

    def test_retries_only_missing_supplement_and_replies_to_forward(self) -> None:
        claim = _claim(
            mode=PublicationMode.NATIVE_FORWARD_WITH_SUPPLEMENT,
            completed_parts=(PublicationPart.MAIN,),
            main_remote_message_ids=(501, 502),
        )
        repository = _Repository(claim=claim)
        publisher = _Publisher(publish_results=[TelegramPublishResult((601,))])

        handled = asyncio.run(
            PublicationWorker(repository=repository, publisher=publisher).process(
                intent_id=claim.intent_id,
                now=NOW,
            )
        )

        self.assertTrue(handled)
        self.assertEqual(len(publisher.publish_requests), 1)
        request = publisher.publish_requests[0]
        self.assertEqual(request.part, PublicationPart.SUPPLEMENT)
        self.assertEqual(request.operation, PublicationOperation.SEND_TEXT)
        self.assertEqual(request.reply_to_message_id, 501)

    def test_reconciles_an_in_flight_part_before_any_resend(self) -> None:
        claim = _claim(
            mode=PublicationMode.FORWARD_ONLY,
            in_flight_part=PublicationPart.MAIN,
            in_flight_operation=PublicationOperation.NATIVE_FORWARD,
        )
        repository = _Repository(claim=claim)
        publisher = _Publisher(reconcile_results=[TelegramPublishResult((701, 702))])

        handled = asyncio.run(
            PublicationWorker(repository=repository, publisher=publisher).process(
                intent_id=claim.intent_id,
                now=NOW,
            )
        )

        self.assertTrue(handled)
        self.assertEqual(len(publisher.reconcile_requests), 1)
        self.assertEqual(publisher.publish_requests, [])
        self.assertTrue(repository.records[0][3])
        self.assertEqual(repository.records[0][2], (701, 702))

    def test_resends_with_same_persisted_random_ids_when_reconciliation_has_no_evidence(
        self,
    ) -> None:
        claim = _claim(
            mode=PublicationMode.FORWARD_ONLY,
            in_flight_part=PublicationPart.MAIN,
            in_flight_operation=PublicationOperation.NATIVE_FORWARD,
        )
        repository = _Repository(claim=claim)
        publisher = _Publisher(
            reconcile_results=[None],
            publish_results=[TelegramPublishResult((801, 802))],
        )

        asyncio.run(
            PublicationWorker(repository=repository, publisher=publisher).process(
                intent_id=claim.intent_id,
                now=NOW,
            )
        )

        self.assertEqual(
            publisher.reconcile_requests[0].random_ids,
            publisher.publish_requests[0].random_ids,
        )
        self.assertFalse(repository.records[0][3])

    def test_persists_exact_flood_wait_without_sleeping(self) -> None:
        retry_at = NOW + timedelta(hours=7, minutes=3)
        claim = _claim(mode=PublicationMode.FORWARD_ONLY)
        repository = _Repository(claim=claim)
        publisher = _Publisher(publish_results=[TelegramFloodWait(retry_at=retry_at)])

        handled = asyncio.run(
            PublicationWorker(repository=repository, publisher=publisher).process(
                intent_id=claim.intent_id,
                now=NOW,
            )
        )

        self.assertTrue(handled)
        self.assertEqual(repository.failures[0][0], "TELEGRAM_FLOOD_WAIT")
        self.assertTrue(repository.failures[0][2])
        self.assertEqual(repository.failures[0][3], retry_at)

    def test_maps_typed_capability_errors_to_persisted_failure_policy(self) -> None:
        cases = (
            (TelegramPermissionDenied(), "TELEGRAM_PERMISSION_DENIED", False),
            (TelegramSessionExpired(), "TELEGRAM_SESSION_EXPIRED", False),
            (TelegramMediaUnavailable(), "TELEGRAM_MEDIA_UNAVAILABLE", False),
            (TelegramPublisherUnavailable(), "TELEGRAM_PUBLISHER_UNAVAILABLE", True),
        )

        for error, expected_code, expected_retryable in cases:
            with self.subTest(error=type(error).__name__):
                claim = _claim(mode=PublicationMode.FORWARD_ONLY)
                repository = _Repository(claim=claim)
                publisher = _Publisher(publish_results=[error])

                handled = asyncio.run(
                    PublicationWorker(repository=repository, publisher=publisher).process(
                        intent_id=claim.intent_id,
                        now=NOW,
                    )
                )

                self.assertTrue(handled)
                self.assertEqual(repository.failures[0][0], expected_code)
                self.assertEqual(repository.failures[0][2], expected_retryable)

    def test_protected_content_default_fails_and_skip_cancels(self) -> None:
        cases = (
            (ProtectedContentFallback.FAIL, "failed"),
            (ProtectedContentFallback.SKIP, "cancelled"),
        )
        for fallback, expected in cases:
            with self.subTest(fallback=fallback):
                claim = _claim(
                    mode=PublicationMode.COPY_WITH_CAPTION,
                    protected_content_fallback=fallback,
                )
                repository = _Repository(claim=claim)
                publisher = _Publisher(publish_results=[TelegramProtectedContent()])

                handled = asyncio.run(
                    PublicationWorker(repository=repository, publisher=publisher).process(
                        intent_id=claim.intent_id,
                        now=NOW,
                    )
                )

                self.assertTrue(handled)
                self.assertEqual(bool(repository.failures), expected == "failed")
                self.assertEqual(bool(repository.cancellations), expected == "cancelled")

    def test_protected_content_metadata_fallback_is_persisted_before_text_send(self) -> None:
        events: list[tuple[str, Any]] = []
        claim = _claim(
            mode=PublicationMode.COPY_WITH_CAPTION,
            protected_content_fallback=ProtectedContentFallback.METADATA_ONLY,
        )
        repository = _Repository(claim=claim, events=events)
        publisher = _Publisher(
            publish_results=[TelegramProtectedContent(), TelegramPublishResult((991,))],
            events=events,
        )

        handled = asyncio.run(
            PublicationWorker(repository=repository, publisher=publisher).process(
                intent_id=claim.intent_id,
                now=NOW,
            )
        )

        self.assertTrue(handled)
        self.assertEqual(
            [request.operation for request in publisher.publish_requests],
            [PublicationOperation.COPY_WITH_CAPTION, PublicationOperation.SEND_TEXT],
        )
        fallback_begin = events.index(("begin", PublicationOperation.SEND_TEXT))
        fallback_publish = events.index(("publish", PublicationOperation.SEND_TEXT))
        self.assertLess(fallback_begin, fallback_publish)
        self.assertEqual(repository.records[0][1], PublicationOperation.SEND_TEXT)

    def test_duplicate_or_terminal_wakeup_is_safely_completed(self) -> None:
        repository = _Repository(claim=None)
        intent_id = str(uuid4())

        handled = asyncio.run(
            PublicationWorker(repository=repository, publisher=_Publisher()).process(
                intent_id=intent_id,
                now=NOW,
            )
        )

        self.assertFalse(handled)
        self.assertEqual(repository.completed_wakeups, [intent_id])

    def test_records_publication_and_flood_wait_telemetry_when_monitor_is_injected(self) -> None:
        monitor = OperationsMonitor()
        retry_at = NOW + timedelta(minutes=2)

        reconciled_claim = _claim(
            mode=PublicationMode.FORWARD_ONLY,
            in_flight_part=PublicationPart.MAIN,
            in_flight_operation=PublicationOperation.NATIVE_FORWARD,
        )
        asyncio.run(
            PublicationWorker(
                repository=_Repository(claim=reconciled_claim),
                publisher=_Publisher(reconcile_results=[TelegramPublishResult((777, 778))]),
                telemetry=monitor,
            ).process(intent_id=reconciled_claim.intent_id, now=NOW)
        )

        flood_claim = _claim(mode=PublicationMode.FORWARD_ONLY)
        asyncio.run(
            PublicationWorker(
                repository=_Repository(claim=flood_claim),
                publisher=_Publisher(publish_results=[TelegramFloodWait(retry_at=retry_at)]),
                telemetry=monitor,
            ).process(intent_id=flood_claim.intent_id, now=NOW)
        )

        terminal_claim = _claim(mode=PublicationMode.FORWARD_ONLY)
        asyncio.run(
            PublicationWorker(
                repository=_Repository(claim=terminal_claim),
                publisher=_Publisher(publish_results=[TelegramPermissionDenied()]),
                telemetry=monitor,
            ).process(intent_id=terminal_claim.intent_id, now=NOW)
        )

        skip_claim = _claim(
            mode=PublicationMode.COPY_WITH_CAPTION,
            protected_content_fallback=ProtectedContentFallback.SKIP,
        )
        asyncio.run(
            PublicationWorker(
                repository=_Repository(claim=skip_claim),
                publisher=_Publisher(publish_results=[TelegramProtectedContent()]),
                telemetry=monitor,
            ).process(intent_id=skip_claim.intent_id, now=NOW)
        )

        snapshot = monitor.snapshot(now=NOW)
        self.assertEqual(snapshot.publication.claimed, 4)
        self.assertEqual(snapshot.publication.reconciled_parts, 1)
        self.assertEqual(snapshot.publication.sent, 1)
        self.assertEqual(snapshot.publication.retry_scheduled, 1)
        self.assertEqual(snapshot.publication.failed, 1)
        self.assertEqual(snapshot.publication.cancelled, 1)
        self.assertEqual(snapshot.flood_wait.events, 1)
        self.assertEqual(snapshot.flood_wait.wait_seconds_total, 120.0)
        self.assertEqual(snapshot.flood_wait.last_retry_at, retry_at)


class _Repository:
    def __init__(
        self,
        *,
        claim: ClaimedPublicationIntent | None,
        events: list[tuple[str, Any]] | None = None,
    ) -> None:
        self.claim_value = claim
        self.events = events if events is not None else []
        self.records: list[tuple[PublicationPart, PublicationOperation, tuple[int, ...], bool]] = []
        self.failures: list[tuple[str, str, bool, datetime]] = []
        self.cancellations: list[tuple[str, str]] = []
        self.completed = False
        self.completed_wakeups: list[str] = []

    async def claim(self, **_: Any) -> ClaimedPublicationIntent | None:
        return self.claim_value

    async def begin_part(
        self,
        *,
        part: PublicationPart,
        operation: PublicationOperation,
        **_: Any,
    ) -> bool:
        self.events.append(("begin", operation))
        return True

    async def record_part(
        self,
        *,
        part: PublicationPart,
        operation: PublicationOperation,
        remote_message_ids: tuple[int, ...],
        reconciled: bool,
        **_: Any,
    ) -> bool:
        self.records.append((part, operation, remote_message_ids, reconciled))
        return True

    async def complete(self, **_: Any) -> bool:
        self.completed = True
        return True

    async def release(self, **_: Any) -> bool:
        return True

    async def retry_or_fail(
        self,
        *,
        error_code: str,
        error_type: str,
        retryable: bool,
        retry_at: datetime,
        **_: Any,
    ) -> bool:
        self.failures.append((error_code, error_type, retryable, retry_at))
        return True

    async def cancel(self, *, error_code: str, error_type: str, **_: Any) -> bool:
        self.cancellations.append((error_code, error_type))
        return True

    async def complete_wakeup_if_terminal(self, *, intent_id: str, **_: Any) -> bool:
        self.completed_wakeups.append(intent_id)
        return True

    async def repair_wakeups(self, **_: Any) -> int:
        return 0


class _Publisher:
    def __init__(
        self,
        *,
        publish_results: list[TelegramPublishResult | Exception] | None = None,
        reconcile_results: list[TelegramPublishResult | None | Exception] | None = None,
        events: list[tuple[str, Any]] | None = None,
    ) -> None:
        self.publish_results = list(publish_results or [])
        self.reconcile_results = list(reconcile_results or [])
        self.events = events if events is not None else []
        self.publish_requests: list[TelegramPublishRequest] = []
        self.reconcile_requests: list[TelegramPublishRequest] = []

    async def publish(self, *, request: TelegramPublishRequest) -> TelegramPublishResult:
        self.publish_requests.append(request)
        self.events.append(("publish", request.operation))
        outcome = (
            self.publish_results.pop(0) if self.publish_results else TelegramPublishResult((1,))
        )
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def reconcile(self, *, request: TelegramPublishRequest) -> TelegramPublishResult | None:
        self.reconcile_requests.append(request)
        outcome = self.reconcile_results.pop(0) if self.reconcile_results else None
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _claim(
    *,
    mode: PublicationMode,
    protected_content_fallback: ProtectedContentFallback = ProtectedContentFallback.FAIL,
    completed_parts: tuple[PublicationPart, ...] = (),
    main_remote_message_ids: tuple[int, ...] = (),
    in_flight_part: PublicationPart | None = None,
    in_flight_operation: PublicationOperation | None = None,
) -> ClaimedPublicationIntent:
    source_message_ids = (11, 12)
    return ClaimedPublicationIntent(
        intent_id=str(uuid4()),
        attempt_id=str(uuid4()),
        lease_token=str(uuid4()),
        attempt_number=1,
        max_attempts=5,
        message_id=str(uuid4()),
        source_channel_id=str(uuid4()),
        source_peer_id=-100100,
        source_message_ids=source_message_ids,
        destination_channel_id=str(uuid4()),
        destination_peer_id=-100200,
        publish_identity_id=str(uuid4()),
        mode=mode,
        protected_content_fallback=protected_content_fallback,
        rendered_text=(
            None if mode is PublicationMode.FORWARD_ONLY else "Rendered publication metadata"
        ),
        main_random_ids=(101,) if mode is PublicationMode.METADATA_ONLY else (101, 102),
        supplement_random_id=(
            103 if mode is PublicationMode.NATIVE_FORWARD_WITH_SUPPLEMENT else None
        ),
        completed_parts=completed_parts,
        main_remote_message_ids=main_remote_message_ids,
        in_flight_part=in_flight_part,
        in_flight_operation=in_flight_operation,
    )


if __name__ == "__main__":
    unittest.main()
