from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from tgcurator.domain.messages.deduplication import DuplicatePolicy
from tgcurator.shared import DomainValidationError


class MessageTerminalReason(StrEnum):
    COMPLETED = "completed"
    BLOCKED_NEGATIVE_GATE = "blocked_negative_gate"
    DUPLICATE_SKIPPED = "duplicate_skipped"
    NO_ELIGIBLE_STAGES = "no_eligible_stages"
    UNSUPPORTED = "unsupported"
    ADMIN_SKIPPED = "admin_skipped"


class MessageFailureStage(StrEnum):
    MEDIA_FAILED = "media_failed"
    ANALYSIS_FAILED = "analysis_failed"
    ROUTING_FAILED = "routing_failed"
    PUBLISH_FAILED = "publish_failed"


VALID_WATERMARK_TERMINAL_REASONS: frozenset[str] = frozenset(
    reason.value for reason in MessageTerminalReason
)


@dataclass(frozen=True, slots=True)
class SourceChannelProfileConfig:
    """Validated frozen pipeline/dedup/routing configuration from SourceChannelProfileVersion."""

    analysis_pipeline_version_id: str | None = None
    routing_policy_version_id: str | None = None
    media_processing_profile_version_id: str | None = None
    duplicate_policy: DuplicatePolicy = DuplicatePolicy.SKIP
    phash_distance_threshold: int = 6
    prescreen_enabled: bool = True

    def __post_init__(self) -> None:
        for field_name in (
            "analysis_pipeline_version_id",
            "routing_policy_version_id",
            "media_processing_profile_version_id",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _normalize_uuid(value, field=field_name))
        if not isinstance(self.duplicate_policy, DuplicatePolicy):
            raise DomainValidationError("duplicate_policy must be a DuplicatePolicy")
        if (
            not isinstance(self.phash_distance_threshold, int)
            or isinstance(self.phash_distance_threshold, bool)
            or not 0 <= self.phash_distance_threshold <= 64
        ):
            raise DomainValidationError("phash_distance_threshold must be an integer in [0, 64]")
        if not isinstance(self.prescreen_enabled, bool):
            raise DomainValidationError("prescreen_enabled must be a boolean")

    @classmethod
    def from_content(cls, content: Mapping[str, Any]) -> SourceChannelProfileConfig:
        if not isinstance(content, Mapping):
            raise DomainValidationError("source channel profile content must be an object")
        raw_policy = content.get("duplicate_policy", DuplicatePolicy.SKIP.value)
        try:
            duplicate_policy = (
                raw_policy
                if isinstance(raw_policy, DuplicatePolicy)
                else DuplicatePolicy(str(raw_policy))
            )
        except ValueError as exc:
            raise DomainValidationError(
                "duplicate_policy must be 'skip' or 'reclassify_with_text'"
            ) from exc

        raw_threshold = content.get("phash_distance_threshold", 6)
        raw_prescreen = content.get("prescreen_enabled", True)
        return cls(
            analysis_pipeline_version_id=_optional_str(
                content.get("analysis_pipeline_version_id"),
                field="analysis_pipeline_version_id",
            ),
            routing_policy_version_id=_optional_str(
                content.get("routing_policy_version_id"),
                field="routing_policy_version_id",
            ),
            media_processing_profile_version_id=_optional_str(
                content.get("media_processing_profile_version_id"),
                field="media_processing_profile_version_id",
            ),
            duplicate_policy=duplicate_policy,
            phash_distance_threshold=raw_threshold,
            prescreen_enabled=raw_prescreen,
        )

    def to_content(self) -> dict[str, Any]:
        return {
            "analysis_pipeline_version_id": self.analysis_pipeline_version_id,
            "routing_policy_version_id": self.routing_policy_version_id,
            "media_processing_profile_version_id": self.media_processing_profile_version_id,
            "duplicate_policy": self.duplicate_policy.value,
            "phash_distance_threshold": self.phash_distance_threshold,
            "prescreen_enabled": self.prescreen_enabled,
        }


@dataclass(frozen=True, slots=True)
class WindowMessageProgress:
    """Watermark-relevant processing state of one logical message inside a RangeExecution."""

    primary_telegram_message_id: int
    processing_status: str
    terminal_reason: str | None = None
    has_blocking_work: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.primary_telegram_message_id, int)
            or isinstance(self.primary_telegram_message_id, bool)
            or self.primary_telegram_message_id <= 0
        ):
            raise DomainValidationError("primary_telegram_message_id must be a positive integer")
        if not isinstance(self.processing_status, str) or not self.processing_status.strip():
            raise DomainValidationError("processing_status must not be blank")
        if not isinstance(self.has_blocking_work, bool):
            raise DomainValidationError("has_blocking_work must be a boolean")

    @property
    def is_watermark_terminal(self) -> bool:
        return (
            self.processing_status == "processed"
            and self.terminal_reason in VALID_WATERMARK_TERMINAL_REASONS
            and not self.has_blocking_work
        )


@dataclass(frozen=True, slots=True)
class WatermarkAdvancement:
    """Monotonic gap-free watermark decision for one RangeExecution window."""

    next_watermark_message_id: int
    window_completed: bool
    blocked_at_message_id: int | None = None


def compute_continuous_watermark(
    *,
    from_message_id_exclusive: int,
    current_watermark_message_id: int,
    to_message_id_inclusive: int,
    history_ingested: bool,
    messages: Sequence[WindowMessageProgress],
) -> WatermarkAdvancement:
    """Advance watermark monotonically across contiguous terminal messages without gaps."""

    for name, value in (
        ("from_message_id_exclusive", from_message_id_exclusive),
        ("current_watermark_message_id", current_watermark_message_id),
        ("to_message_id_inclusive", to_message_id_inclusive),
    ):
        if not isinstance(value, int) or isinstance(value, bool):
            raise DomainValidationError(f"{name} must be an integer")
    if from_message_id_exclusive < 0 or to_message_id_inclusive <= from_message_id_exclusive:
        raise DomainValidationError("invalid range execution bounds")
    if not from_message_id_exclusive <= current_watermark_message_id <= to_message_id_inclusive:
        raise DomainValidationError("current_watermark_message_id must lie within window bounds")
    if not isinstance(history_ingested, bool):
        raise DomainValidationError("history_ingested must be a boolean")

    if not history_ingested:
        return WatermarkAdvancement(
            next_watermark_message_id=current_watermark_message_id,
            window_completed=False,
            blocked_at_message_id=None,
        )

    in_window = sorted(
        (
            item
            for item in messages
            if from_message_id_exclusive
            < item.primary_telegram_message_id
            <= to_message_id_inclusive
        ),
        key=lambda item: item.primary_telegram_message_id,
    )

    for item in in_window:
        if item.primary_telegram_message_id <= current_watermark_message_id:
            if not item.is_watermark_terminal:
                raise DomainValidationError(
                    "a non-terminal message exists at or below current_watermark_message_id"
                )
            continue
        if not item.is_watermark_terminal:
            next_watermark = max(
                current_watermark_message_id,
                item.primary_telegram_message_id - 1,
            )
            return WatermarkAdvancement(
                next_watermark_message_id=next_watermark,
                window_completed=False,
                blocked_at_message_id=item.primary_telegram_message_id,
            )

    return WatermarkAdvancement(
        next_watermark_message_id=to_message_id_inclusive,
        window_completed=True,
        blocked_at_message_id=None,
    )


def _optional_str(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DomainValidationError(f"{field} must be a UUID string or null")
    stripped = value.strip()
    return stripped or None


def _normalize_uuid(value: str, *, field: str) -> str:
    try:
        return str(UUID(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise DomainValidationError(f"{field} must be a valid UUID") from exc
