from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from tgcurator.application.ports.operations import OperationsTelemetry, PublicationMetric
from tgcurator.application.ports.publishing import (
    ClaimedPublicationIntent,
    PublicationRepository,
    TelegramFloodWait,
    TelegramProtectedContent,
    TelegramPublicationError,
    TelegramPublisher,
    TelegramPublishRequest,
    TelegramPublishResult,
)
from tgcurator.domain.publishing import (
    ProtectedContentFallback,
    PublicationOperation,
    PublicationPart,
    publication_plan,
)
from tgcurator.shared import DomainValidationError, ensure_aware, ensure_positive_duration

PUBLICATION_QUEUE = "publication"


@dataclass(slots=True)
class PublicationWorker:
    """Execute one leased publication intent with PostgreSQL-owned retry state."""

    repository: PublicationRepository
    publisher: TelegramPublisher
    telemetry: OperationsTelemetry | None = None
    lease_duration: timedelta = timedelta(minutes=5)
    failure_retry_delay: timedelta = timedelta(minutes=1)

    def __post_init__(self) -> None:
        ensure_positive_duration(self.lease_duration, field="lease_duration")
        ensure_positive_duration(self.failure_retry_delay, field="failure_retry_delay")

    async def process(self, *, intent_id: str, now: datetime) -> bool:
        ensure_aware(now, field="now")
        _require_uuid(intent_id, field="intent_id")
        claim = await self.repository.claim(
            intent_id=intent_id,
            now=now,
            lease_duration=self.lease_duration,
        )
        if claim is None:
            await self.repository.complete_wakeup_if_terminal(intent_id=intent_id, now=now)
            return False
        if self.telemetry is not None:
            self.telemetry.record_publication(PublicationMetric.CLAIMED)

        completed_parts = set(claim.completed_parts)
        main_remote_message_ids = claim.main_remote_message_ids
        try:
            if claim.in_flight_part is not None and claim.in_flight_operation is not None:
                result = await self._reconcile_or_publish(
                    claim=claim,
                    part=claim.in_flight_part,
                    operation=claim.in_flight_operation,
                    main_remote_message_ids=main_remote_message_ids,
                )
                if not await self.repository.record_part(
                    claim=claim,
                    part=claim.in_flight_part,
                    operation=claim.in_flight_operation,
                    remote_message_ids=result.remote_message_ids,
                    reconciled=result.reconciled,
                    now=now,
                ):
                    return False
                if result.reconciled and self.telemetry is not None:
                    self.telemetry.record_publication(PublicationMetric.RECONCILED_PART)
                completed_parts.add(claim.in_flight_part)
                if claim.in_flight_part is PublicationPart.MAIN:
                    main_remote_message_ids = result.remote_message_ids

            plan = publication_plan(claim.mode)
            while step := plan.next_step(completed_parts):
                operation = step.operation
                if not await self.repository.begin_part(
                    claim=claim,
                    part=step.part,
                    operation=operation,
                    now=now,
                ):
                    return False
                try:
                    published = await self.publisher.publish(
                        request=self._request(
                            claim=claim,
                            part=step.part,
                            operation=operation,
                            main_remote_message_ids=main_remote_message_ids,
                        )
                    )
                except TelegramProtectedContent as error:
                    fallback_result = await self._handle_protected_content(
                        claim=claim,
                        part=step.part,
                        operation=operation,
                        main_remote_message_ids=main_remote_message_ids,
                        error=error,
                        now=now,
                    )
                    if isinstance(fallback_result, bool):
                        return fallback_result
                    operation, published = fallback_result

                if not await self.repository.record_part(
                    claim=claim,
                    part=step.part,
                    operation=operation,
                    remote_message_ids=published.remote_message_ids,
                    reconciled=False,
                    now=now,
                ):
                    return False
                completed_parts.add(step.part)
                if step.part is PublicationPart.MAIN:
                    main_remote_message_ids = published.remote_message_ids

            completed = await self.repository.complete(claim=claim, now=now)
            if completed and self.telemetry is not None:
                self.telemetry.record_publication(PublicationMetric.SENT)
            return completed
        except TelegramFloodWait as error:
            recorded = await self._fail(
                claim=claim,
                error=error,
                retry_at=error.retry_at,
                now=now,
            )
            if recorded and self.telemetry is not None:
                self.telemetry.record_flood_wait(
                    observed_at=now,
                    retry_at=error.retry_at if error.retry_at >= now else now,
                )
            return recorded
        except TelegramPublicationError as error:
            return await self._fail(
                claim=claim,
                error=error,
                retry_at=now + self.failure_retry_delay,
                now=now,
            )
        except Exception as error:
            return await self._record_retry_or_fail(
                claim=claim,
                error_code="TELEGRAM_PUBLICATION_FAILED",
                error_type=type(error).__name__,
                retryable=True,
                retry_at=now + self.failure_retry_delay,
                now=now,
            )

    async def _reconcile_or_publish(
        self,
        *,
        claim: ClaimedPublicationIntent,
        part: PublicationPart,
        operation: PublicationOperation,
        main_remote_message_ids: tuple[int, ...],
    ) -> _ExecutionResult:
        request = self._request(
            claim=claim,
            part=part,
            operation=operation,
            main_remote_message_ids=main_remote_message_ids,
        )
        reconciled = await self.publisher.reconcile(request=request)
        if reconciled is not None:
            return _ExecutionResult(
                remote_message_ids=reconciled.remote_message_ids,
                reconciled=True,
            )
        published = await self.publisher.publish(request=request)
        return _ExecutionResult(
            remote_message_ids=published.remote_message_ids,
            reconciled=False,
        )

    async def _handle_protected_content(
        self,
        *,
        claim: ClaimedPublicationIntent,
        part: PublicationPart,
        operation: PublicationOperation,
        main_remote_message_ids: tuple[int, ...],
        error: TelegramProtectedContent,
        now: datetime,
    ) -> tuple[PublicationOperation, TelegramPublishResult] | bool:
        fallback = claim.protected_content_fallback
        if fallback is ProtectedContentFallback.SKIP:
            cancelled = await self.repository.cancel(
                claim=claim,
                error_code="TELEGRAM_PROTECTED_CONTENT_SKIPPED",
                error_type=error.error_type,
                now=now,
            )
            if cancelled and self.telemetry is not None:
                self.telemetry.record_publication(PublicationMetric.CANCELLED)
            return cancelled
        if (
            fallback is ProtectedContentFallback.FAIL
            or operation is PublicationOperation.SEND_TEXT
            or claim.rendered_text is None
        ):
            return await self._fail(
                claim=claim,
                error=error,
                retry_at=now,
                now=now,
            )

        fallback_operation = PublicationOperation.SEND_TEXT
        if not await self.repository.begin_part(
            claim=claim,
            part=part,
            operation=fallback_operation,
            now=now,
        ):
            return False
        published = await self.publisher.publish(
            request=self._request(
                claim=claim,
                part=part,
                operation=fallback_operation,
                main_remote_message_ids=main_remote_message_ids,
            )
        )
        return fallback_operation, published

    async def _fail(
        self,
        *,
        claim: ClaimedPublicationIntent,
        error: TelegramPublicationError,
        retry_at: datetime,
        now: datetime,
    ) -> bool:
        return await self._record_retry_or_fail(
            claim=claim,
            error_code=error.code,
            error_type=error.error_type,
            retryable=error.retryable,
            retry_at=retry_at,
            now=now,
        )

    async def _record_retry_or_fail(
        self,
        *,
        claim: ClaimedPublicationIntent,
        error_code: str,
        error_type: str,
        retryable: bool,
        retry_at: datetime,
        now: datetime,
    ) -> bool:
        recorded = await self.repository.retry_or_fail(
            claim=claim,
            error_code=error_code,
            error_type=error_type,
            retryable=retryable,
            retry_at=retry_at,
            now=now,
        )
        if recorded and self.telemetry is not None:
            metric = (
                PublicationMetric.RETRY_SCHEDULED
                if retryable and claim.attempt_number < claim.max_attempts
                else PublicationMetric.FAILED
            )
            self.telemetry.record_publication(metric)
        return recorded

    @staticmethod
    def _request(
        *,
        claim: ClaimedPublicationIntent,
        part: PublicationPart,
        operation: PublicationOperation,
        main_remote_message_ids: tuple[int, ...],
    ) -> TelegramPublishRequest:
        random_ids = claim.random_ids_for(part)
        if operation is PublicationOperation.SEND_TEXT:
            random_ids = (random_ids[0],)
        text = claim.rendered_text if operation is not PublicationOperation.NATIVE_FORWARD else None
        reply_to_message_id = None
        if (
            part is PublicationPart.SUPPLEMENT
            and claim.supplement_as_reply
            and main_remote_message_ids
        ):
            reply_to_message_id = main_remote_message_ids[0]
        return TelegramPublishRequest(
            intent_id=claim.intent_id,
            part=part,
            operation=operation,
            identity_id=claim.publish_identity_id,
            source_peer_id=claim.source_peer_id,
            source_message_ids=claim.source_message_ids,
            destination_peer_id=claim.destination_peer_id,
            random_ids=random_ids,
            text=text,
            reply_to_message_id=reply_to_message_id,
        )


@dataclass(frozen=True, slots=True)
class _ExecutionResult:
    remote_message_ids: tuple[int, ...]
    reconciled: bool


def _require_uuid(value: str, *, field: str) -> str:
    try:
        return str(UUID(value))
    except (AttributeError, TypeError, ValueError) as error:
        raise DomainValidationError(f"{field} must be a UUID") from error
