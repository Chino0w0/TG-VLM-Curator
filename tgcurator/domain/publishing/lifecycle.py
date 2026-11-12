from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Any
from uuid import UUID

from tgcurator.shared import DomainValidationError

MAX_TELEGRAM_RANDOM_ID = (1 << 63) - 1


class PublicationStatus(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    PARTIAL = "partial"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PublicationPart(StrEnum):
    MAIN = "main"
    SUPPLEMENT = "supplement"


class PublicationOperation(StrEnum):
    NATIVE_FORWARD = "native_forward"
    COPY_WITH_CAPTION = "copy_with_caption"
    SEND_TEXT = "send_text"


class ProtectedContentFallback(StrEnum):
    FAIL = "fail"
    METADATA_ONLY = "metadata_only"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class PublicationStep:
    part: PublicationPart
    operation: PublicationOperation
    requires_rendered_text: bool


@dataclass(frozen=True, slots=True)
class PublicationPlan:
    mode: PublicationMode
    steps: tuple[PublicationStep, ...]

    @property
    def required_parts(self) -> tuple[PublicationPart, ...]:
        return tuple(step.part for step in self.steps)

    def next_step(self, completed_parts: Iterable[PublicationPart]) -> PublicationStep | None:
        completed = frozenset(completed_parts)
        return next((step for step in self.steps if step.part not in completed), None)


# Kept here rather than importing from idempotency to avoid a circular dependency in callers
# that import all publishing primitives from the package root.
from .idempotency import PublicationMode  # noqa: E402


def publication_plan(mode: PublicationMode) -> PublicationPlan:
    if not isinstance(mode, PublicationMode):
        raise DomainValidationError("mode must be a PublicationMode")
    if mode is PublicationMode.NATIVE_FORWARD_WITH_SUPPLEMENT:
        steps = (
            PublicationStep(
                part=PublicationPart.MAIN,
                operation=PublicationOperation.NATIVE_FORWARD,
                requires_rendered_text=False,
            ),
            PublicationStep(
                part=PublicationPart.SUPPLEMENT,
                operation=PublicationOperation.SEND_TEXT,
                requires_rendered_text=True,
            ),
        )
    elif mode is PublicationMode.COPY_WITH_CAPTION:
        steps = (
            PublicationStep(
                part=PublicationPart.MAIN,
                operation=PublicationOperation.COPY_WITH_CAPTION,
                requires_rendered_text=True,
            ),
        )
    elif mode is PublicationMode.FORWARD_ONLY:
        steps = (
            PublicationStep(
                part=PublicationPart.MAIN,
                operation=PublicationOperation.NATIVE_FORWARD,
                requires_rendered_text=False,
            ),
        )
    else:
        steps = (
            PublicationStep(
                part=PublicationPart.MAIN,
                operation=PublicationOperation.SEND_TEXT,
                requires_rendered_text=True,
            ),
        )
    return PublicationPlan(mode=mode, steps=steps)


def validate_random_id(value: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value <= 0
        or value > MAX_TELEGRAM_RANDOM_ID
    ):
        raise DomainValidationError("Telegram random_id must be a positive signed 63-bit integer")
    return value


def stable_random_id(*, intent_id: str, part: PublicationPart, ordinal: int = 0) -> int:
    """Derive a stable positive MTProto random_id from durable publication identity.

    The value is deterministic so retry/recovery never depends on process memory. Persistence still
    stores the derived value before any Telegram call, making the database the execution authority.
    """

    try:
        normalized_intent_id = str(UUID(intent_id))
    except (AttributeError, TypeError, ValueError) as error:
        raise DomainValidationError("intent_id must be a UUID") from error
    if not isinstance(part, PublicationPart):
        raise DomainValidationError("part must be a PublicationPart")
    if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0:
        raise DomainValidationError("ordinal must be a non-negative integer")
    digest = sha256(
        f"publication-random-id:v1:{normalized_intent_id}:{part.value}:{ordinal}".encode()
    ).digest()
    value = int.from_bytes(digest[:8], byteorder="big", signed=False) & MAX_TELEGRAM_RANDOM_ID
    return value or 1


def stable_random_ids(*, intent_id: str, part: PublicationPart, count: int) -> tuple[int, ...]:
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        raise DomainValidationError("count must be a positive integer")
    return tuple(
        stable_random_id(intent_id=intent_id, part=part, ordinal=index) for index in range(count)
    )


def rendered_snapshot_hash(*, text: str, context: dict[str, Any] | None = None) -> str:
    if not isinstance(text, str):
        raise DomainValidationError("rendered text must be a string")
    document = {"context": context or {}, "text": text}
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(canonical.encode("utf-8")).hexdigest()


def publication_status_for_parts(
    *,
    plan: PublicationPlan,
    completed_parts: Iterable[PublicationPart],
    retry_pending: bool = False,
    terminal_failure: bool = False,
    cancelled: bool = False,
) -> PublicationStatus:
    if not isinstance(plan, PublicationPlan):
        raise DomainValidationError("plan must be a PublicationPlan")
    completed = frozenset(completed_parts)
    if not completed.issubset(set(plan.required_parts)):
        raise DomainValidationError("completed_parts contains a part outside the publication plan")
    if cancelled:
        return PublicationStatus.CANCELLED
    if set(plan.required_parts).issubset(completed):
        return PublicationStatus.SENT
    if terminal_failure:
        return PublicationStatus.FAILED
    if retry_pending:
        return PublicationStatus.RETRY_WAIT
    if completed:
        return PublicationStatus.PARTIAL
    return PublicationStatus.PENDING
