from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from tgcurator.domain.publishing import (
    ProtectedContentFallback,
    PublicationMode,
    PublicationOperation,
    PublicationPart,
    publication_plan,
    validate_random_id,
)
from tgcurator.shared import DomainValidationError, ensure_aware


def _uuid(value: str, *, field: str) -> str:
    try:
        return str(UUID(value))
    except (AttributeError, TypeError, ValueError) as error:
        raise DomainValidationError(f"{field} must be a UUID") from error


def _telegram_peer(value: int, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value == 0:
        raise DomainValidationError(f"{field} must be a non-zero integer")
    return value


def _positive_message_ids(values: tuple[int, ...], *, field: str) -> None:
    if not values or any(
        not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in values
    ):
        raise DomainValidationError(f"{field} must contain positive integers")


@dataclass(frozen=True, slots=True)
class ClaimedPublicationIntent:
    intent_id: str
    attempt_id: str
    lease_token: str
    attempt_number: int
    max_attempts: int
    message_id: str
    source_channel_id: str
    source_peer_id: int
    source_message_ids: tuple[int, ...]
    destination_channel_id: str
    destination_peer_id: int
    publish_identity_id: str
    mode: PublicationMode
    protected_content_fallback: ProtectedContentFallback
    rendered_text: str | None
    main_random_ids: tuple[int, ...]
    supplement_random_id: int | None
    supplement_as_reply: bool = True
    completed_parts: tuple[PublicationPart, ...] = ()
    main_remote_message_ids: tuple[int, ...] = ()
    in_flight_part: PublicationPart | None = None
    in_flight_operation: PublicationOperation | None = None

    def __post_init__(self) -> None:
        for field in (
            "intent_id",
            "attempt_id",
            "lease_token",
            "message_id",
            "source_channel_id",
            "destination_channel_id",
            "publish_identity_id",
        ):
            _uuid(getattr(self, field), field=field)
        if (
            not isinstance(self.attempt_number, int)
            or isinstance(self.attempt_number, bool)
            or not isinstance(self.max_attempts, int)
            or isinstance(self.max_attempts, bool)
            or self.attempt_number <= 0
            or self.max_attempts <= 0
        ):
            raise DomainValidationError("publication attempt numbers must be positive integers")
        if self.attempt_number > self.max_attempts:
            raise DomainValidationError("attempt_number must not exceed max_attempts")
        _telegram_peer(self.source_peer_id, field="source_peer_id")
        _telegram_peer(self.destination_peer_id, field="destination_peer_id")
        _positive_message_ids(self.source_message_ids, field="source_message_ids")
        if not isinstance(self.mode, PublicationMode):
            raise DomainValidationError("mode must be a PublicationMode")
        if not isinstance(self.protected_content_fallback, ProtectedContentFallback):
            raise DomainValidationError(
                "protected_content_fallback must be a ProtectedContentFallback"
            )
        if not isinstance(self.supplement_as_reply, bool):
            raise DomainValidationError("supplement_as_reply must be a boolean")
        if not self.main_random_ids:
            raise DomainValidationError("main_random_ids must not be empty")
        for random_id in self.main_random_ids:
            validate_random_id(random_id)
        if self.supplement_random_id is not None:
            validate_random_id(self.supplement_random_id)
        if self.mode is PublicationMode.NATIVE_FORWARD_WITH_SUPPLEMENT:
            if self.supplement_random_id is None:
                raise DomainValidationError("supplement_random_id is required for supplement mode")
            if self.rendered_text is None:
                raise DomainValidationError("rendered_text is required for supplement mode")
        elif self.supplement_random_id is not None:
            raise DomainValidationError("supplement_random_id is only valid for supplement mode")
        if self.mode in {PublicationMode.COPY_WITH_CAPTION, PublicationMode.METADATA_ONLY}:
            if self.rendered_text is None:
                raise DomainValidationError("rendered_text is required for the selected mode")
        if self.mode is PublicationMode.METADATA_ONLY:
            if len(self.main_random_ids) != 1:
                raise DomainValidationError("metadata_only requires exactly one main random_id")
        elif len(self.main_random_ids) != len(self.source_message_ids):
            raise DomainValidationError(
                "media publication requires one main random_id per source message"
            )

        plan = publication_plan(self.mode)
        if len(set(self.completed_parts)) != len(self.completed_parts):
            raise DomainValidationError("completed_parts must not contain duplicates")
        if not set(self.completed_parts).issubset(set(plan.required_parts)):
            raise DomainValidationError("completed_parts contains a part outside the mode plan")
        if self.main_remote_message_ids:
            _positive_message_ids(
                self.main_remote_message_ids,
                field="main_remote_message_ids",
            )
        if PublicationPart.MAIN in self.completed_parts and not self.main_remote_message_ids:
            raise DomainValidationError("completed main part requires remote message ids")
        if PublicationPart.MAIN not in self.completed_parts and self.main_remote_message_ids:
            raise DomainValidationError("main remote message ids require a completed main part")
        if (self.in_flight_part is None) != (self.in_flight_operation is None):
            raise DomainValidationError(
                "in_flight_part and in_flight_operation must either both be set or both be omitted"
            )
        if self.in_flight_part is not None:
            if self.in_flight_part not in plan.required_parts:
                raise DomainValidationError("in_flight_part is outside the mode plan")
            if self.in_flight_part in self.completed_parts:
                raise DomainValidationError("a completed publication part cannot remain in flight")

    def random_ids_for(self, part: PublicationPart) -> tuple[int, ...]:
        if part is PublicationPart.MAIN:
            return self.main_random_ids
        if part is PublicationPart.SUPPLEMENT and self.supplement_random_id is not None:
            return (self.supplement_random_id,)
        raise DomainValidationError("publication part has no persisted random_id")


@dataclass(frozen=True, slots=True)
class TelegramPublishRequest:
    intent_id: str
    part: PublicationPart
    operation: PublicationOperation
    identity_id: str
    source_peer_id: int
    source_message_ids: tuple[int, ...]
    destination_peer_id: int
    random_ids: tuple[int, ...]
    text: str | None = None
    reply_to_message_id: int | None = None

    def __post_init__(self) -> None:
        _uuid(self.intent_id, field="intent_id")
        _uuid(self.identity_id, field="identity_id")
        _telegram_peer(self.source_peer_id, field="source_peer_id")
        _telegram_peer(self.destination_peer_id, field="destination_peer_id")
        _positive_message_ids(self.source_message_ids, field="source_message_ids")
        if not isinstance(self.part, PublicationPart):
            raise DomainValidationError("part must be a PublicationPart")
        if not isinstance(self.operation, PublicationOperation):
            raise DomainValidationError("operation must be a PublicationOperation")
        if not self.random_ids:
            raise DomainValidationError("random_ids must not be empty")
        for value in self.random_ids:
            validate_random_id(value)
        if self.operation is PublicationOperation.SEND_TEXT:
            if len(self.random_ids) != 1 or self.text is None:
                raise DomainValidationError("send_text requires one random_id and text")
        else:
            if len(self.random_ids) != len(self.source_message_ids):
                raise DomainValidationError(
                    "media operations require one persisted random_id per source message"
                )
            if self.operation is PublicationOperation.NATIVE_FORWARD and self.text is not None:
                raise DomainValidationError("native_forward does not accept rendered text")
            if self.operation is PublicationOperation.COPY_WITH_CAPTION and self.text is None:
                raise DomainValidationError("copy_with_caption requires rendered text")
        if self.reply_to_message_id is not None:
            if (
                not isinstance(self.reply_to_message_id, int)
                or isinstance(self.reply_to_message_id, bool)
                or self.reply_to_message_id <= 0
            ):
                raise DomainValidationError("reply_to_message_id must be positive when present")
            if self.operation is not PublicationOperation.SEND_TEXT:
                raise DomainValidationError("only send_text may reply to a remote message")


@dataclass(frozen=True, slots=True)
class TelegramPublishResult:
    remote_message_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        _positive_message_ids(self.remote_message_ids, field="remote_message_ids")


class TelegramPublicationError(RuntimeError):
    code = "TELEGRAM_PUBLICATION_FAILED"
    retryable = True

    def __init__(self, error_type: str | None = None) -> None:
        super().__init__(error_type or type(self).__name__)
        self.error_type = error_type or type(self).__name__


class TelegramFloodWait(TelegramPublicationError):
    code = "TELEGRAM_FLOOD_WAIT"
    retryable = True

    def __init__(self, *, retry_at: datetime, error_type: str = "FloodWaitError") -> None:
        ensure_aware(retry_at, field="retry_at")
        super().__init__(error_type)
        self.retry_at = retry_at


class TelegramPermissionDenied(TelegramPublicationError):
    code = "TELEGRAM_PERMISSION_DENIED"
    retryable = False


class TelegramSessionExpired(TelegramPublicationError):
    code = "TELEGRAM_SESSION_EXPIRED"
    retryable = False


class TelegramProtectedContent(TelegramPublicationError):
    code = "TELEGRAM_PROTECTED_CONTENT"
    retryable = False


class TelegramMediaUnavailable(TelegramPublicationError):
    code = "TELEGRAM_MEDIA_UNAVAILABLE"
    retryable = False


class TelegramPublisherUnavailable(TelegramPublicationError):
    code = "TELEGRAM_PUBLISHER_UNAVAILABLE"
    retryable = True


class TelegramPublisher(Protocol):
    """Identity-aware MTProto boundary with no database or SDK types in its contract."""

    async def publish(self, *, request: TelegramPublishRequest) -> TelegramPublishResult: ...

    async def reconcile(
        self, *, request: TelegramPublishRequest
    ) -> TelegramPublishResult | None: ...


class PublicationRepository(Protocol):
    async def claim(
        self, *, intent_id: str, now: datetime, lease_duration: timedelta
    ) -> ClaimedPublicationIntent | None: ...

    async def begin_part(
        self,
        *,
        claim: ClaimedPublicationIntent,
        part: PublicationPart,
        operation: PublicationOperation,
        now: datetime,
    ) -> bool: ...

    async def record_part(
        self,
        *,
        claim: ClaimedPublicationIntent,
        part: PublicationPart,
        operation: PublicationOperation,
        remote_message_ids: tuple[int, ...],
        reconciled: bool,
        now: datetime,
    ) -> bool: ...

    async def complete(self, *, claim: ClaimedPublicationIntent, now: datetime) -> bool: ...

    async def release(self, *, claim: ClaimedPublicationIntent, now: datetime) -> bool: ...

    async def retry_or_fail(
        self,
        *,
        claim: ClaimedPublicationIntent,
        error_code: str,
        error_type: str,
        retryable: bool,
        retry_at: datetime,
        now: datetime,
    ) -> bool: ...

    async def cancel(
        self,
        *,
        claim: ClaimedPublicationIntent,
        error_code: str,
        error_type: str,
        now: datetime,
    ) -> bool: ...

    async def complete_wakeup_if_terminal(self, *, intent_id: str, now: datetime) -> bool: ...

    async def repair_wakeups(self, *, now: datetime, limit: int = 500) -> int: ...
