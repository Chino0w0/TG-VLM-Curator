from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from numbers import Real

from tgcurator.shared import DomainValidationError


class ManualLabelOperation(StrEnum):
    SET = "set"
    CLEAR = "clear"


class ReviewStatus(StrEnum):
    UNREVIEWED = "unreviewed"
    IN_REVIEW = "in_review"
    REVIEWED = "reviewed"
    NEEDS_ATTENTION = "needs_attention"


@dataclass(frozen=True, slots=True)
class LabelValue:
    message_id: str
    target_scope: str
    target_kind: str
    target_id: str
    label_definition_version_id: str
    label_key: str
    score: float
    activated: bool
    image_asset_id: str | None = None
    video_asset_id: str | None = None

    def __post_init__(self) -> None:
        _validate_target_identity(
            message_id=self.message_id,
            target_scope=self.target_scope,
            target_kind=self.target_kind,
            target_id=self.target_id,
            image_asset_id=self.image_asset_id,
            video_asset_id=self.video_asset_id,
        )
        _require_nonblank(self.label_definition_version_id, field="label_definition_version_id")
        _require_nonblank(self.label_key, field="label_key")
        if not isinstance(self.score, Real) or isinstance(self.score, bool):
            raise DomainValidationError("score must be numeric")
        if not 0.0 <= float(self.score) <= 1.0:
            raise DomainValidationError("score must be in [0, 1]")
        if not isinstance(self.activated, bool):
            raise DomainValidationError("activated must be a boolean")

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.target_scope, self.target_id, self.label_definition_version_id


def _require_nonblank(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainValidationError(f"{field} must not be blank")
    return value


def _validate_target_identity(
    *,
    message_id: str,
    target_scope: str,
    target_kind: str,
    target_id: str,
    image_asset_id: str | None,
    video_asset_id: str | None,
) -> None:
    _require_nonblank(message_id, field="message_id")
    _require_nonblank(target_id, field="target_id")
    if target_kind == "message":
        valid = (
            target_scope == "global"
            and target_id == message_id
            and image_asset_id is None
            and video_asset_id is None
        )
    elif target_kind == "image_asset":
        valid = target_scope == "media" and image_asset_id == target_id and video_asset_id is None
    elif target_kind == "video_asset":
        valid = target_scope == "media" and video_asset_id == target_id and image_asset_id is None
    else:
        valid = False
    if not valid:
        raise DomainValidationError("target identity is inconsistent")


@dataclass(frozen=True, slots=True)
class ManualLabelEvent:
    event_id: str
    message_id: str
    target_scope: str
    target_kind: str
    target_id: str
    label_definition_version_id: str
    label_key: str
    operation: ManualLabelOperation
    actor_admin_user_id: str
    created_at: datetime
    score: float | None = None
    activated: bool | None = None
    image_asset_id: str | None = None
    video_asset_id: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        _require_nonblank(self.event_id, field="event_id")
        _validate_target_identity(
            message_id=self.message_id,
            target_scope=self.target_scope,
            target_kind=self.target_kind,
            target_id=self.target_id,
            image_asset_id=self.image_asset_id,
            video_asset_id=self.video_asset_id,
        )
        _require_nonblank(self.label_definition_version_id, field="label_definition_version_id")
        _require_nonblank(self.label_key, field="label_key")
        _require_nonblank(self.actor_admin_user_id, field="actor_admin_user_id")
        if not isinstance(self.operation, ManualLabelOperation):
            raise DomainValidationError("operation must be a ManualLabelOperation")
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise DomainValidationError("created_at must be timezone-aware")
        if self.reason is not None and not isinstance(self.reason, str):
            raise DomainValidationError("reason must be a string")
        if self.operation is ManualLabelOperation.CLEAR:
            if self.score is not None or self.activated is not None:
                raise DomainValidationError("clear events cannot contain score or activated")
        else:
            LabelValue(
                message_id=self.message_id,
                target_scope=self.target_scope,
                target_kind=self.target_kind,
                target_id=self.target_id,
                image_asset_id=self.image_asset_id,
                video_asset_id=self.video_asset_id,
                label_definition_version_id=self.label_definition_version_id,
                label_key=self.label_key,
                score=self.score,  # type: ignore[arg-type]
                activated=self.activated,  # type: ignore[arg-type]
            )

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.target_scope, self.target_id, self.label_definition_version_id

    def as_label_value(self) -> LabelValue:
        if self.operation is not ManualLabelOperation.SET:
            raise DomainValidationError("only set events have a label value")
        assert self.score is not None and self.activated is not None
        return LabelValue(
            message_id=self.message_id,
            target_scope=self.target_scope,
            target_kind=self.target_kind,
            target_id=self.target_id,
            image_asset_id=self.image_asset_id,
            video_asset_id=self.video_asset_id,
            label_definition_version_id=self.label_definition_version_id,
            label_key=self.label_key,
            score=self.score,
            activated=self.activated,
        )


@dataclass(frozen=True, slots=True)
class ResolvedLabelNamespaces:
    model: tuple[LabelValue, ...]
    manual: tuple[LabelValue, ...]
    effective: tuple[LabelValue, ...]


@dataclass(frozen=True, slots=True)
class ReviewTransition:
    message_id: str
    old_status: ReviewStatus
    new_status: ReviewStatus
    actor_admin_user_id: str
    reason: str | None = None

    def __post_init__(self) -> None:
        _require_nonblank(self.message_id, field="message_id")
        _require_nonblank(self.actor_admin_user_id, field="actor_admin_user_id")
        if not isinstance(self.old_status, ReviewStatus) or not isinstance(
            self.new_status, ReviewStatus
        ):
            raise DomainValidationError("review statuses must be ReviewStatus values")
        if self.old_status is self.new_status:
            raise DomainValidationError("review status transition must change status")
        if self.reason is not None and not isinstance(self.reason, str):
            raise DomainValidationError("reason must be a string")


@dataclass(frozen=True, slots=True)
class MessageReviewEvent:
    event_id: str
    message_id: str
    old_status: ReviewStatus
    new_status: ReviewStatus
    actor_admin_user_id: str
    created_at: datetime
    reason: str | None = None

    def __post_init__(self) -> None:
        _require_nonblank(self.event_id, field="event_id")
        ReviewTransition(
            message_id=self.message_id,
            old_status=self.old_status,
            new_status=self.new_status,
            actor_admin_user_id=self.actor_admin_user_id,
            reason=self.reason,
        )
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise DomainValidationError("created_at must be timezone-aware")

    def as_transition(self) -> ReviewTransition:
        return ReviewTransition(
            message_id=self.message_id,
            old_status=self.old_status,
            new_status=self.new_status,
            actor_admin_user_id=self.actor_admin_user_id,
            reason=self.reason,
        )
