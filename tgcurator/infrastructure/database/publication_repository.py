from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert

from tgcurator.application.ports.publishing import ClaimedPublicationIntent
from tgcurator.domain.publishing import (
    ProtectedContentFallback,
    PublicationMode,
    PublicationOperation,
    PublicationPart,
    PublicationStatus,
    publication_plan,
    publication_status_for_parts,
    stable_random_id,
    stable_random_ids,
)
from tgcurator.shared import (
    DomainValidationError,
    ensure_aware,
    ensure_positive_duration,
)

from .models import (
    DestinationChannel,
    DurableWakeupRecord,
    MessageRecord,
    PublicationIntentRecord,
    PublishAttemptRecord,
    PublishRecord,
    SourceChannel,
)
from .session import AsyncDatabase

PUBLICATION_QUEUE = "publication"
_TERMINAL_STATUSES = ("sent", "failed", "cancelled")
_ACTIVE_WAKEUP_STATUSES = ("pending", "leased")


def publication_intent_claim_statement(*, intent_id: UUID, now: datetime):
    """Lock one claimable publication intent without waiting behind another worker."""

    return (
        select(
            PublicationIntentRecord,
            MessageRecord,
            SourceChannel,
            DestinationChannel,
        )
        .join(MessageRecord, MessageRecord.id == PublicationIntentRecord.message_id)
        .join(SourceChannel, SourceChannel.id == MessageRecord.source_channel_id)
        .join(
            DestinationChannel,
            DestinationChannel.id == PublicationIntentRecord.destination_channel_id,
        )
        .where(
            PublicationIntentRecord.id == intent_id,
            or_(
                PublicationIntentRecord.status.in_(("pending", "partial")),
                and_(
                    PublicationIntentRecord.status == "retry_wait",
                    PublicationIntentRecord.next_retry_at <= now,
                ),
                and_(
                    PublicationIntentRecord.status == "sending",
                    PublicationIntentRecord.lease_expires_at <= now,
                ),
            ),
        )
        .with_for_update(of=PublicationIntentRecord, skip_locked=True)
    )


def publication_complete_wakeup_statement(*, intent_id: UUID, now: datetime):
    """Complete a wake-up only when its publication intent is terminal."""

    terminal_intent = exists(
        select(PublicationIntentRecord.id).where(
            PublicationIntentRecord.id == intent_id,
            PublicationIntentRecord.status.in_(_TERMINAL_STATUSES),
        )
    )
    return (
        update(DurableWakeupRecord)
        .where(
            DurableWakeupRecord.queue == PUBLICATION_QUEUE,
            DurableWakeupRecord.entity_id == intent_id,
            DurableWakeupRecord.status.in_(_ACTIVE_WAKEUP_STATUSES),
            terminal_intent,
        )
        .values(
            status="completed",
            lease_token=None,
            lease_expires_at=None,
            completed_at=now,
            updated_at=now,
        )
    )


def publication_repair_candidates_statement(*, now: datetime, limit: int):
    """Find active publication work whose broker signal can be reconstructed."""

    return (
        select(PublicationIntentRecord)
        .where(
            or_(
                PublicationIntentRecord.status.in_(("pending", "partial")),
                and_(
                    PublicationIntentRecord.status == "retry_wait",
                    PublicationIntentRecord.next_retry_at <= now,
                ),
                and_(
                    PublicationIntentRecord.status == "sending",
                    PublicationIntentRecord.lease_expires_at <= now,
                ),
            )
        )
        .order_by(PublicationIntentRecord.created_at, PublicationIntentRecord.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )


class SqlAlchemyPublicationRepository:
    """Persist effectively-once Telegram publication execution and recovery state."""

    def __init__(
        self,
        database: AsyncDatabase,
        *,
        id_factory: Callable[[], UUID] | None = None,
    ) -> None:
        self._database = database
        self._id_factory = id_factory or uuid4

    async def claim(
        self,
        *,
        intent_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> ClaimedPublicationIntent | None:
        intent_uuid = _as_uuid(intent_id, field="intent_id")
        ensure_aware(now, field="now")
        ensure_positive_duration(lease_duration, field="lease_duration")

        async with self._database.session() as session:
            async with session.begin():
                selected = (
                    await session.execute(
                        publication_intent_claim_statement(intent_id=intent_uuid, now=now)
                    )
                ).one_or_none()
                if selected is None:
                    return None

                intent, message, source, destination = selected
                if intent.attempt_count >= intent.max_attempts:
                    await self._terminalize_exhausted(session=session, intent=intent, now=now)
                    return None

                if intent.status == "sending":
                    await self._close_expired_attempt(session=session, intent=intent, now=now)

                mode = _publication_mode(intent.publication_mode)
                main_random_count = (
                    1
                    if mode is PublicationMode.METADATA_ONLY
                    else len(message.telegram_message_ids)
                )
                if not intent.main_random_ids:
                    intent.main_random_ids = list(
                        stable_random_ids(
                            intent_id=str(intent.id),
                            part=PublicationPart.MAIN,
                            count=main_random_count,
                        )
                    )
                if (
                    mode is PublicationMode.NATIVE_FORWARD_WITH_SUPPLEMENT
                    and intent.supplement_random_id is None
                ):
                    intent.supplement_random_id = stable_random_id(
                        intent_id=str(intent.id),
                        part=PublicationPart.SUPPLEMENT,
                    )

                lease_token = self._id_factory()
                attempt_id = self._id_factory()
                intent.status = "sending"
                intent.attempt_count += 1
                intent.next_retry_at = None
                intent.lease_token = lease_token
                intent.lease_expires_at = now + lease_duration
                intent.started_at = intent.started_at or now
                intent.updated_at = now
                session.add(
                    PublishAttemptRecord(
                        id=attempt_id,
                        publication_intent_id=intent.id,
                        attempt_number=intent.attempt_count,
                        lease_token=lease_token,
                        status="sending",
                        reconciled=False,
                        started_at=now,
                    )
                )

                return _claimed(
                    intent=intent,
                    attempt_id=attempt_id,
                    lease_token=lease_token,
                    message=message,
                    source=source,
                    destination=destination,
                )

    async def begin_part(
        self,
        *,
        claim: ClaimedPublicationIntent,
        part: PublicationPart,
        operation: PublicationOperation,
        now: datetime,
    ) -> bool:
        ensure_aware(now, field="now")
        _validate_part_operation(claim=claim, part=part, operation=operation)
        async with self._database.session() as session:
            async with session.begin():
                intent = await self._locked_active_intent(
                    session=session,
                    claim=claim,
                    now=now,
                )
                if intent is None or part.value in intent.completed_parts:
                    return False
                if intent.in_flight_part is not None:
                    if intent.in_flight_part != part.value:
                        return False
                    if (
                        intent.in_flight_operation != operation.value
                        and operation is not PublicationOperation.SEND_TEXT
                    ):
                        return False

                intent.in_flight_part = part.value
                intent.in_flight_operation = operation.value
                intent.in_flight_started_at = now
                intent.updated_at = now
                return True

    async def record_part(
        self,
        *,
        claim: ClaimedPublicationIntent,
        part: PublicationPart,
        operation: PublicationOperation,
        remote_message_ids: tuple[int, ...],
        reconciled: bool,
        now: datetime,
    ) -> bool:
        ensure_aware(now, field="now")
        _validate_part_operation(claim=claim, part=part, operation=operation)
        _validate_remote_message_ids(
            operation=operation,
            remote_message_ids=remote_message_ids,
            source_message_count=len(claim.source_message_ids),
        )
        if not isinstance(reconciled, bool):
            raise DomainValidationError("reconciled must be a boolean")

        async with self._database.session() as session:
            async with session.begin():
                intent = await self._locked_active_intent(
                    session=session,
                    claim=claim,
                    now=now,
                )
                if intent is None:
                    return False
                if (
                    intent.in_flight_part != part.value
                    or intent.in_flight_operation != operation.value
                    or part.value in intent.completed_parts
                ):
                    return False

                random_ids = claim.random_ids_for(part)
                if operation is PublicationOperation.SEND_TEXT:
                    random_ids = (random_ids[0],)
                session.add(
                    PublishRecord(
                        id=self._id_factory(),
                        publication_intent_id=intent.id,
                        publish_attempt_id=_as_uuid(claim.attempt_id, field="attempt_id"),
                        part=part.value,
                        operation=operation.value,
                        remote_message_ids=list(remote_message_ids),
                        random_ids=list(random_ids),
                        rendered_text=(
                            None
                            if operation is PublicationOperation.NATIVE_FORWARD
                            else intent.rendered_text
                        ),
                        rendered_snapshot_hash=(
                            None
                            if operation is PublicationOperation.NATIVE_FORWARD
                            else intent.rendered_snapshot_hash
                        ),
                        reconciled=reconciled,
                        sent_at=now,
                    )
                )
                completed = set(intent.completed_parts)
                completed.add(part.value)
                plan = publication_plan(_publication_mode(intent.publication_mode))
                intent.completed_parts = [
                    required.value
                    for required in plan.required_parts
                    if required.value in completed
                ]
                if part is PublicationPart.MAIN:
                    intent.main_remote_message_ids = list(remote_message_ids)
                intent.in_flight_part = None
                intent.in_flight_operation = None
                intent.in_flight_started_at = None
                if reconciled:
                    intent.last_reconciled_at = now
                    await session.execute(
                        update(PublishAttemptRecord)
                        .where(
                            PublishAttemptRecord.id
                            == _as_uuid(claim.attempt_id, field="attempt_id"),
                            PublishAttemptRecord.publication_intent_id == intent.id,
                            PublishAttemptRecord.lease_token
                            == _as_uuid(claim.lease_token, field="lease_token"),
                            PublishAttemptRecord.status == "sending",
                        )
                        .values(reconciled=True, updated_at=now)
                    )
                intent.updated_at = now
                return True

    async def complete(self, *, claim: ClaimedPublicationIntent, now: datetime) -> bool:
        ensure_aware(now, field="now")
        async with self._database.session() as session:
            async with session.begin():
                intent = await self._locked_active_intent(
                    session=session,
                    claim=claim,
                    now=now,
                )
                if intent is None:
                    return False
                plan = publication_plan(_publication_mode(intent.publication_mode))
                completed_parts = tuple(PublicationPart(value) for value in intent.completed_parts)
                if not set(plan.required_parts).issubset(set(completed_parts)):
                    return False

                intent.status = publication_status_for_parts(
                    plan=plan,
                    completed_parts=completed_parts,
                ).value
                intent.next_retry_at = None
                intent.lease_token = None
                intent.lease_expires_at = None
                intent.in_flight_part = None
                intent.in_flight_operation = None
                intent.in_flight_started_at = None
                intent.completed_at = now
                intent.updated_at = now
                await self._finish_attempt(
                    session=session,
                    claim=claim,
                    status="sent",
                    now=now,
                )
                await self._set_wakeup_terminal(session=session, entity_id=intent.id, now=now)
                return True

    async def release(self, *, claim: ClaimedPublicationIntent, now: datetime) -> bool:
        """Cooperatively release a lease into pending or partial state when no step is in flight."""

        ensure_aware(now, field="now")
        async with self._database.session() as session:
            async with session.begin():
                intent = await self._locked_active_intent(
                    session=session,
                    claim=claim,
                    now=now,
                )
                if intent is None or intent.in_flight_part is not None:
                    return False
                plan = publication_plan(_publication_mode(intent.publication_mode))
                completed_parts = tuple(PublicationPart(value) for value in intent.completed_parts)
                next_status = publication_status_for_parts(
                    plan=plan,
                    completed_parts=completed_parts,
                )
                if next_status is PublicationStatus.SENT:
                    return False

                intent.status = next_status.value
                intent.next_retry_at = None
                intent.lease_token = None
                intent.lease_expires_at = None
                intent.updated_at = now
                await self._finish_attempt(
                    session=session,
                    claim=claim,
                    status="retry_wait",
                    now=now,
                    retryable=True,
                    retry_at=now,
                    error_code="PUBLICATION_LEASE_RELEASED",
                    error_type="PublicationLeaseReleased",
                )
                await self._reschedule_wakeup(
                    session=session,
                    entity_id=intent.id,
                    retry_at=now,
                    now=now,
                )
                return True

    async def retry_or_fail(
        self,
        *,
        claim: ClaimedPublicationIntent,
        error_code: str,
        error_type: str,
        retryable: bool,
        retry_at: datetime,
        now: datetime,
    ) -> bool:
        ensure_aware(now, field="now")
        ensure_aware(retry_at, field="retry_at")
        code = _error_label(error_code, field="error_code", max_length=128)
        error_name = _error_label(error_type, field="error_type", max_length=128)
        if not isinstance(retryable, bool):
            raise DomainValidationError("retryable must be a boolean")

        async with self._database.session() as session:
            async with session.begin():
                intent = await self._locked_active_intent(
                    session=session,
                    claim=claim,
                    now=now,
                )
                if intent is None:
                    return False

                intent.last_error_code = code
                intent.last_error_type = error_name
                intent.last_failure_at = now
                intent.lease_token = None
                intent.lease_expires_at = None
                intent.updated_at = now
                can_retry = retryable and intent.attempt_count < intent.max_attempts
                if can_retry:
                    intent.status = "retry_wait"
                    intent.next_retry_at = retry_at
                    await self._finish_attempt(
                        session=session,
                        claim=claim,
                        status="retry_wait",
                        now=now,
                        retryable=True,
                        retry_at=retry_at,
                        error_code=code,
                        error_type=error_name,
                    )
                    await self._reschedule_wakeup(
                        session=session,
                        entity_id=intent.id,
                        retry_at=retry_at,
                        now=now,
                    )
                else:
                    intent.status = "failed"
                    intent.next_retry_at = None
                    intent.completed_at = now
                    await self._finish_attempt(
                        session=session,
                        claim=claim,
                        status="failed",
                        now=now,
                        retryable=retryable,
                        error_code=code,
                        error_type=error_name,
                    )
                    await self._set_wakeup_terminal(
                        session=session,
                        entity_id=intent.id,
                        now=now,
                    )
                return True

    async def cancel(
        self,
        *,
        claim: ClaimedPublicationIntent,
        error_code: str,
        error_type: str,
        now: datetime,
    ) -> bool:
        ensure_aware(now, field="now")
        code = _error_label(error_code, field="error_code", max_length=128)
        error_name = _error_label(error_type, field="error_type", max_length=128)
        async with self._database.session() as session:
            async with session.begin():
                intent = await self._locked_active_intent(
                    session=session,
                    claim=claim,
                    now=now,
                )
                if intent is None:
                    return False

                intent.status = "cancelled"
                intent.next_retry_at = None
                intent.lease_token = None
                intent.lease_expires_at = None
                intent.in_flight_part = None
                intent.in_flight_operation = None
                intent.in_flight_started_at = None
                intent.last_error_code = code
                intent.last_error_type = error_name
                intent.last_failure_at = now
                intent.completed_at = now
                intent.updated_at = now
                await self._finish_attempt(
                    session=session,
                    claim=claim,
                    status="cancelled",
                    now=now,
                    retryable=False,
                    error_code=code,
                    error_type=error_name,
                )
                await self._set_wakeup_terminal(session=session, entity_id=intent.id, now=now)
                return True

    async def complete_wakeup_if_terminal(self, *, intent_id: str, now: datetime) -> bool:
        ensure_aware(now, field="now")
        async with self._database.session() as session:
            async with session.begin():
                result = await session.execute(
                    publication_complete_wakeup_statement(
                        intent_id=_as_uuid(intent_id, field="intent_id"),
                        now=now,
                    )
                )
                return result.rowcount == 1

    async def repair_wakeups(self, *, now: datetime, limit: int = 500) -> int:
        ensure_aware(now, field="now")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise DomainValidationError("limit must be a positive integer")
        async with self._database.session() as session:
            async with session.begin():
                intents = tuple(
                    await session.scalars(
                        publication_repair_candidates_statement(now=now, limit=limit)
                    )
                )
                repaired = 0
                for intent in intents:
                    wakeup_id = await session.scalar(
                        postgresql_insert(DurableWakeupRecord)
                        .values(
                            id=self._id_factory(),
                            queue=PUBLICATION_QUEUE,
                            entity_id=intent.id,
                            status="pending",
                            next_attempt_at=now,
                            lease_token=None,
                            lease_expires_at=None,
                            dispatch_attempts=0,
                            completed_at=None,
                            updated_at=now,
                        )
                        .on_conflict_do_update(
                            index_elements=[
                                DurableWakeupRecord.queue,
                                DurableWakeupRecord.entity_id,
                            ],
                            set_={
                                "status": "pending",
                                "next_attempt_at": now,
                                "lease_token": None,
                                "lease_expires_at": None,
                                "completed_at": None,
                                "updated_at": now,
                            },
                            where=or_(
                                DurableWakeupRecord.status != "leased",
                                DurableWakeupRecord.lease_expires_at <= now,
                            ),
                        )
                        .returning(DurableWakeupRecord.id)
                    )
                    if wakeup_id is not None:
                        repaired += 1
                return repaired

    @staticmethod
    async def _locked_active_intent(*, session, claim: ClaimedPublicationIntent, now: datetime):
        return await session.scalar(
            select(PublicationIntentRecord)
            .where(
                PublicationIntentRecord.id == _as_uuid(claim.intent_id, field="intent_id"),
                PublicationIntentRecord.status == "sending",
                PublicationIntentRecord.lease_token
                == _as_uuid(claim.lease_token, field="lease_token"),
                PublicationIntentRecord.lease_expires_at > now,
            )
            .with_for_update()
        )

    @staticmethod
    async def _finish_attempt(
        *,
        session,
        claim: ClaimedPublicationIntent,
        status: str,
        now: datetime,
        retryable: bool | None = None,
        retry_at: datetime | None = None,
        error_code: str | None = None,
        error_type: str | None = None,
    ) -> None:
        await session.execute(
            update(PublishAttemptRecord)
            .where(
                PublishAttemptRecord.id == _as_uuid(claim.attempt_id, field="attempt_id"),
                PublishAttemptRecord.publication_intent_id
                == _as_uuid(claim.intent_id, field="intent_id"),
                PublishAttemptRecord.lease_token
                == _as_uuid(claim.lease_token, field="lease_token"),
                PublishAttemptRecord.status == "sending",
            )
            .values(
                status=status,
                retryable=retryable,
                next_retry_at=retry_at,
                error_code=error_code,
                error_type=error_type,
                completed_at=now,
                updated_at=now,
            )
        )

    @staticmethod
    async def _close_expired_attempt(*, session, intent: PublicationIntentRecord, now: datetime):
        if intent.lease_token is None:
            return
        await session.execute(
            update(PublishAttemptRecord)
            .where(
                PublishAttemptRecord.publication_intent_id == intent.id,
                PublishAttemptRecord.lease_token == intent.lease_token,
                PublishAttemptRecord.status == "sending",
            )
            .values(
                status="retry_wait",
                retryable=True,
                next_retry_at=now,
                error_code="PUBLICATION_LEASE_EXPIRED",
                error_type="PublicationLeaseExpired",
                completed_at=now,
                updated_at=now,
            )
        )

    @staticmethod
    async def _reschedule_wakeup(
        *,
        session,
        entity_id: UUID,
        retry_at: datetime,
        now: datetime,
    ) -> None:
        await session.execute(
            update(DurableWakeupRecord)
            .where(
                DurableWakeupRecord.queue == PUBLICATION_QUEUE,
                DurableWakeupRecord.entity_id == entity_id,
                DurableWakeupRecord.status != "cancelled",
            )
            .values(
                status="pending",
                next_attempt_at=retry_at,
                lease_token=None,
                lease_expires_at=None,
                completed_at=None,
                updated_at=now,
            )
        )

    @staticmethod
    async def _set_wakeup_terminal(*, session, entity_id: UUID, now: datetime) -> None:
        await session.execute(
            update(DurableWakeupRecord)
            .where(
                DurableWakeupRecord.queue == PUBLICATION_QUEUE,
                DurableWakeupRecord.entity_id == entity_id,
                DurableWakeupRecord.status != "cancelled",
            )
            .values(
                status="completed",
                lease_token=None,
                lease_expires_at=None,
                completed_at=now,
                updated_at=now,
            )
        )

    async def _terminalize_exhausted(
        self,
        *,
        session,
        intent: PublicationIntentRecord,
        now: datetime,
    ) -> None:
        if intent.status == "sending" and intent.lease_token is not None:
            await session.execute(
                update(PublishAttemptRecord)
                .where(
                    PublishAttemptRecord.publication_intent_id == intent.id,
                    PublishAttemptRecord.lease_token == intent.lease_token,
                    PublishAttemptRecord.status == "sending",
                )
                .values(
                    status="failed",
                    retryable=False,
                    next_retry_at=None,
                    error_code="PUBLICATION_ATTEMPTS_EXHAUSTED",
                    error_type="PublicationAttemptsExhausted",
                    completed_at=now,
                    updated_at=now,
                )
            )
        intent.status = "failed"
        intent.next_retry_at = None
        intent.lease_token = None
        intent.lease_expires_at = None
        intent.last_error_code = "PUBLICATION_ATTEMPTS_EXHAUSTED"
        intent.last_error_type = "PublicationAttemptsExhausted"
        intent.last_failure_at = now
        intent.completed_at = now
        intent.updated_at = now
        await self._set_wakeup_terminal(session=session, entity_id=intent.id, now=now)


def _claimed(
    *,
    intent: PublicationIntentRecord,
    attempt_id: UUID,
    lease_token: UUID,
    message: MessageRecord,
    source: SourceChannel,
    destination: DestinationChannel,
) -> ClaimedPublicationIntent:
    return ClaimedPublicationIntent(
        intent_id=str(intent.id),
        attempt_id=str(attempt_id),
        lease_token=str(lease_token),
        attempt_number=intent.attempt_count,
        max_attempts=intent.max_attempts,
        message_id=str(message.id),
        source_channel_id=str(source.id),
        source_peer_id=source.telegram_peer_id,
        source_message_ids=tuple(message.telegram_message_ids),
        destination_channel_id=str(destination.id),
        destination_peer_id=destination.telegram_peer_id,
        publish_identity_id=str(intent.publish_identity_id),
        mode=_publication_mode(intent.publication_mode),
        protected_content_fallback=_protected_fallback(intent.protected_content_fallback),
        rendered_text=intent.rendered_text,
        main_random_ids=tuple(intent.main_random_ids),
        supplement_random_id=intent.supplement_random_id,
        supplement_as_reply=intent.supplement_as_reply,
        completed_parts=tuple(PublicationPart(value) for value in intent.completed_parts),
        main_remote_message_ids=tuple(intent.main_remote_message_ids),
        in_flight_part=(
            PublicationPart(intent.in_flight_part) if intent.in_flight_part is not None else None
        ),
        in_flight_operation=(
            PublicationOperation(intent.in_flight_operation)
            if intent.in_flight_operation is not None
            else None
        ),
    )


def _publication_mode(value: str) -> PublicationMode:
    try:
        return PublicationMode(value)
    except ValueError as error:
        raise DomainValidationError("stored publication mode is invalid") from error


def _protected_fallback(value: str) -> ProtectedContentFallback:
    try:
        return ProtectedContentFallback(value)
    except ValueError as error:
        raise DomainValidationError("stored protected-content fallback is invalid") from error


def _validate_part_operation(
    *,
    claim: ClaimedPublicationIntent,
    part: PublicationPart,
    operation: PublicationOperation,
) -> None:
    if not isinstance(part, PublicationPart):
        raise DomainValidationError("part must be a PublicationPart")
    if not isinstance(operation, PublicationOperation):
        raise DomainValidationError("operation must be a PublicationOperation")
    plan = publication_plan(claim.mode)
    expected = next((step.operation for step in plan.steps if step.part is part), None)
    if expected is None:
        raise DomainValidationError("publication part is outside the mode plan")
    if operation is not expected:
        fallback_allowed = (
            operation is PublicationOperation.SEND_TEXT
            and part is PublicationPart.MAIN
            and claim.protected_content_fallback is ProtectedContentFallback.METADATA_ONLY
        )
        if not fallback_allowed:
            raise DomainValidationError("operation is outside the publication mode plan")
    if operation is PublicationOperation.SEND_TEXT and claim.rendered_text is None:
        raise DomainValidationError("send_text requires the frozen rendered text")


def _validate_remote_message_ids(
    *,
    operation: PublicationOperation,
    remote_message_ids: tuple[int, ...],
    source_message_count: int,
) -> None:
    if not isinstance(remote_message_ids, tuple) or not remote_message_ids:
        raise DomainValidationError("remote_message_ids must be a non-empty tuple")
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value <= 0
        for value in remote_message_ids
    ):
        raise DomainValidationError("remote_message_ids must contain positive integers")
    expected = 1 if operation is PublicationOperation.SEND_TEXT else source_message_count
    if len(remote_message_ids) != expected:
        raise DomainValidationError("remote message count does not match the publication operation")


def _error_label(value: str, *, field: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainValidationError(f"{field} must not be blank")
    return value.strip()[:max_length]


def _as_uuid(value: str, *, field: str) -> UUID:
    try:
        return UUID(value)
    except (AttributeError, TypeError, ValueError) as error:
        raise DomainValidationError(f"{field} must be a UUID") from error
