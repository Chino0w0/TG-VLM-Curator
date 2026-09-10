from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from tgcurator.domain.review import LabelValue, ManualLabelEvent, ReviewStatus
from tgcurator.domain.routing import RoutingDecision, RoutingPolicy
from tgcurator.shared import DomainValidationError


def _nonblank(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainValidationError(f"{field} must not be blank")
    return value


@dataclass(frozen=True, slots=True)
class RoutingRequest:
    request_id: str
    message_id: str
    routing_policy_version_id: str
    dry_run: bool = False

    def __post_init__(self) -> None:
        _nonblank(self.request_id, field="request_id")
        _nonblank(self.message_id, field="message_id")
        _nonblank(
            self.routing_policy_version_id,
            field="routing_policy_version_id",
        )
        if not isinstance(self.dry_run, bool):
            raise DomainValidationError("dry_run must be a boolean")


@dataclass(frozen=True, slots=True)
class RoutingSnapshot:
    message_id: str
    message: Mapping[str, Any]
    source_channel: Mapping[str, Any]
    duplicate_state: Mapping[str, Any]
    media: tuple[Mapping[str, Any], ...]
    review_status: ReviewStatus
    model_assignments: tuple[LabelValue, ...]
    manual_events: tuple[ManualLabelEvent, ...]
    policy: RoutingPolicy
    destination_publish_identity_ids: Mapping[str, str]

    def __post_init__(self) -> None:
        _nonblank(self.message_id, field="message_id")
        if not isinstance(self.review_status, ReviewStatus):
            raise DomainValidationError("review_status must be a ReviewStatus")
        if not isinstance(self.policy, RoutingPolicy):
            raise DomainValidationError("policy must be a RoutingPolicy")


@dataclass(frozen=True, slots=True)
class RoutingEvaluationDraft:
    evaluation_id: str
    routing_request_id: str
    message_id: str
    routing_policy_version_id: str
    facts_snapshot: Mapping[str, Any]
    facts_hash: str
    rule_outcomes: tuple[Mapping[str, Any], ...]
    stopped_at_rule_id: str | None


@dataclass(frozen=True, slots=True)
class PublicationIntentDraft:
    intent_id: str
    routing_evaluation_id: str
    routing_request_id: str
    message_id: str
    routing_policy_version_id: str
    routing_rule_id: str
    routing_action_id: str
    destination_channel_id: str
    publish_identity_id: str
    publication_mode: str
    rendering_template_version_id: str | None
    business_idempotency_key: str
    status: str = "pending"


@dataclass(frozen=True, slots=True)
class RoutingExecutionResult:
    evaluation_id: str
    routing_request_id: str
    message_id: str
    routing_policy_version_id: str
    facts_snapshot: Mapping[str, Any]
    facts_hash: str
    decision: RoutingDecision
    intents: tuple[PublicationIntentDraft, ...]
    persisted: bool
    reused: bool = False
    dry_run: bool = False


def validate_routing_request_reuse(
    *,
    result: RoutingExecutionResult,
    routing_request_id: str,
    message_id: str,
    routing_policy_version_id: str,
) -> None:
    """Reject request-ID reuse for a different routing input identity."""

    if not isinstance(result, RoutingExecutionResult):
        raise DomainValidationError("result must be a RoutingExecutionResult")
    expected = (
        _nonblank(routing_request_id, field="routing_request_id"),
        _nonblank(message_id, field="message_id"),
        _nonblank(
            routing_policy_version_id,
            field="routing_policy_version_id",
        ),
    )
    actual = (
        result.routing_request_id,
        result.message_id,
        result.routing_policy_version_id,
    )
    if actual != expected:
        raise DomainValidationError(
            "routing_request_id is already bound to a different message or routing policy version"
        )


class RoutingRepository(Protocol):
    async def find_by_request_id(
        self, *, routing_request_id: str
    ) -> RoutingExecutionResult | None: ...

    async def load_snapshot(
        self, *, message_id: str, routing_policy_version_id: str
    ) -> RoutingSnapshot: ...

    async def persist_evaluation(
        self,
        *,
        evaluation: RoutingEvaluationDraft,
        decision: RoutingDecision,
        intents: tuple[PublicationIntentDraft, ...],
    ) -> RoutingExecutionResult: ...
