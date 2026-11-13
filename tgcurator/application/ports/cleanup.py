from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from tgcurator.shared import DomainValidationError, ensure_aware

from .contracts import ArchiveStorage

_ARCHIVE_SCOPES = frozenset({"global", "media"})
_DUPLICATE_STATUSES = frozenset({"unknown", "unique", "canonical", "duplicate"})
_DUPLICATE_STATUS_ALIASES = {
    "duplicate_skipped": "duplicate",
    "duplicate_reclassify": "duplicate",
    "duplicate_analyzed": "duplicate",
}
_MEDIA_TYPES = frozenset({"image", "video"})
_PROCESSING_STATUSES = frozenset({"ingested", "processing", "processed", "failed"})
_REVIEW_STATUSES = frozenset({"unreviewed", "in_review", "reviewed", "needs_attention"})
_JOB_STATUSES = frozenset({"pending", "running", "retry_wait", "completed", "failed", "cancelled"})
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _uuid(value: str, *, field: str) -> str:
    try:
        return str(UUID(value))
    except (AttributeError, TypeError, ValueError) as error:
        raise DomainValidationError(f"{field} must be a UUID") from error


def _normalized_values(
    values: tuple[str, ...],
    *,
    field: str,
    allowed: frozenset[str] | None = None,
    aliases: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise DomainValidationError(f"{field} must contain non-blank strings")
        item = value.strip().lower() if allowed is not None else value.strip()
        if aliases is not None:
            item = aliases.get(item, item)
        if allowed is not None and item not in allowed:
            raise DomainValidationError(f"{field} contains unsupported value: {value!r}")
        normalized.add(item)
    return tuple(sorted(normalized))


def _normalized_uuids(values: tuple[str, ...], *, field: str) -> tuple[str, ...]:
    return tuple(sorted({_uuid(value, field=field) for value in values}))


def _timestamp(value: datetime | None, *, field: str) -> datetime | None:
    if value is None:
        return None
    ensure_aware(value, field=field)
    return value.astimezone(UTC)


def _snapshot_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_snapshot_timestamp(value: object, *, field: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DomainValidationError(f"cleanup filter {field} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise DomainValidationError(f"cleanup filter {field} is not a valid timestamp") from error
    ensure_aware(parsed, field=field)
    return parsed


def canonical_cleanup_filter_json(snapshot: Mapping[str, object]) -> str:
    try:
        return json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError) as error:
        raise DomainValidationError("cleanup filter snapshot must be JSON serializable") from error


def cleanup_filter_hash(snapshot: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_cleanup_filter_json(snapshot).encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class ArchiveCleanupFilter:
    """Canonical AND-across-categories cleanup query.

    Tuple members use OR semantics. Label-key filters match any currently activated label in the
    selected namespace, restricted to the candidate asset for MEDIA labels. Supplying model,
    manual, and effective label filters together requires a match in every supplied namespace.
    """

    archived_from: datetime | None = None
    archived_before: datetime | None = None
    source_channel_ids: tuple[str, ...] = ()
    message_published_from: datetime | None = None
    message_published_before: datetime | None = None
    model_label_keys: tuple[str, ...] = ()
    manual_label_keys: tuple[str, ...] = ()
    effective_label_keys: tuple[str, ...] = ()
    label_scopes: tuple[str, ...] = ()
    negative_label: bool | None = None
    review_statuses: tuple[str, ...] = ()
    processing_statuses: tuple[str, ...] = ()
    duplicate_statuses: tuple[str, ...] = ()
    media_types: tuple[str, ...] = ()
    blocked_from_analysis: bool | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "archived_from", _timestamp(self.archived_from, field="archived_from")
        )
        object.__setattr__(
            self, "archived_before", _timestamp(self.archived_before, field="archived_before")
        )
        object.__setattr__(
            self,
            "message_published_from",
            _timestamp(self.message_published_from, field="message_published_from"),
        )
        object.__setattr__(
            self,
            "message_published_before",
            _timestamp(self.message_published_before, field="message_published_before"),
        )
        if (
            self.archived_from is not None
            and self.archived_before is not None
            and self.archived_before <= self.archived_from
        ):
            raise DomainValidationError("archived_before must be later than archived_from")
        if (
            self.message_published_from is not None
            and self.message_published_before is not None
            and self.message_published_before <= self.message_published_from
        ):
            raise DomainValidationError(
                "message_published_before must be later than message_published_from"
            )
        object.__setattr__(
            self,
            "source_channel_ids",
            _normalized_uuids(self.source_channel_ids, field="source_channel_ids"),
        )
        for field in ("model_label_keys", "manual_label_keys", "effective_label_keys"):
            object.__setattr__(
                self,
                field,
                _normalized_values(getattr(self, field), field=field),
            )
        object.__setattr__(
            self,
            "label_scopes",
            _normalized_values(self.label_scopes, field="label_scopes", allowed=_ARCHIVE_SCOPES),
        )
        object.__setattr__(
            self,
            "review_statuses",
            _normalized_values(
                self.review_statuses, field="review_statuses", allowed=_REVIEW_STATUSES
            ),
        )
        object.__setattr__(
            self,
            "processing_statuses",
            _normalized_values(
                self.processing_statuses,
                field="processing_statuses",
                allowed=_PROCESSING_STATUSES,
            ),
        )
        object.__setattr__(
            self,
            "duplicate_statuses",
            _normalized_values(
                self.duplicate_statuses,
                field="duplicate_statuses",
                allowed=_DUPLICATE_STATUSES,
                aliases=_DUPLICATE_STATUS_ALIASES,
            ),
        )
        object.__setattr__(
            self,
            "media_types",
            _normalized_values(self.media_types, field="media_types", allowed=_MEDIA_TYPES),
        )
        if self.negative_label is not None and not isinstance(self.negative_label, bool):
            raise DomainValidationError("negative_label must be a boolean when present")
        if self.blocked_from_analysis is not None and not isinstance(
            self.blocked_from_analysis, bool
        ):
            raise DomainValidationError("blocked_from_analysis must be a boolean when present")
        if not self.has_meaningful_filter:
            raise DomainValidationError("cleanup query must contain at least one filter")

    @property
    def has_meaningful_filter(self) -> bool:
        return (
            any(
                value not in (None, (), False)
                for value in (
                    self.archived_from,
                    self.archived_before,
                    self.source_channel_ids,
                    self.message_published_from,
                    self.message_published_before,
                    self.model_label_keys,
                    self.manual_label_keys,
                    self.effective_label_keys,
                    self.label_scopes,
                    self.negative_label,
                    self.review_statuses,
                    self.processing_statuses,
                    self.duplicate_statuses,
                    self.media_types,
                    self.blocked_from_analysis,
                )
            )
            or self.negative_label is False
            or self.blocked_from_analysis is False
        )

    def snapshot(self) -> dict[str, object]:
        return {
            "archived_before": _snapshot_timestamp(self.archived_before),
            "archived_from": _snapshot_timestamp(self.archived_from),
            "blocked_from_analysis": self.blocked_from_analysis,
            "duplicate_statuses": list(self.duplicate_statuses),
            "effective_label_keys": list(self.effective_label_keys),
            "label_scopes": list(self.label_scopes),
            "manual_label_keys": list(self.manual_label_keys),
            "media_types": list(self.media_types),
            "message_published_before": _snapshot_timestamp(self.message_published_before),
            "message_published_from": _snapshot_timestamp(self.message_published_from),
            "model_label_keys": list(self.model_label_keys),
            "negative_label": self.negative_label,
            "processing_statuses": list(self.processing_statuses),
            "review_statuses": list(self.review_statuses),
            "source_channel_ids": list(self.source_channel_ids),
        }

    @property
    def canonical_json(self) -> str:
        return canonical_cleanup_filter_json(self.snapshot())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("ascii")).hexdigest()

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, object]) -> ArchiveCleanupFilter:
        expected = {
            "archived_before",
            "archived_from",
            "blocked_from_analysis",
            "duplicate_statuses",
            "effective_label_keys",
            "label_scopes",
            "manual_label_keys",
            "media_types",
            "message_published_before",
            "message_published_from",
            "model_label_keys",
            "negative_label",
            "processing_statuses",
            "review_statuses",
            "source_channel_ids",
        }
        if set(snapshot) != expected:
            raise DomainValidationError("cleanup filter snapshot has an unsupported shape")

        def values(field: str) -> tuple[str, ...]:
            raw = snapshot[field]
            if not isinstance(raw, list) or any(not isinstance(value, str) for value in raw):
                raise DomainValidationError(f"cleanup filter {field} must be a string array")
            return tuple(raw)

        return cls(
            archived_from=_parse_snapshot_timestamp(
                snapshot["archived_from"], field="archived_from"
            ),
            archived_before=_parse_snapshot_timestamp(
                snapshot["archived_before"], field="archived_before"
            ),
            source_channel_ids=values("source_channel_ids"),
            message_published_from=_parse_snapshot_timestamp(
                snapshot["message_published_from"], field="message_published_from"
            ),
            message_published_before=_parse_snapshot_timestamp(
                snapshot["message_published_before"], field="message_published_before"
            ),
            model_label_keys=values("model_label_keys"),
            manual_label_keys=values("manual_label_keys"),
            effective_label_keys=values("effective_label_keys"),
            label_scopes=values("label_scopes"),
            negative_label=snapshot["negative_label"],  # type: ignore[arg-type]
            review_statuses=values("review_statuses"),
            processing_statuses=values("processing_statuses"),
            duplicate_statuses=values("duplicate_statuses"),
            media_types=values("media_types"),
            blocked_from_analysis=snapshot["blocked_from_analysis"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ArchiveCleanupEstimate:
    asset_count: int
    message_count: int
    bytes: int

    def __post_init__(self) -> None:
        for field in ("asset_count", "message_count", "bytes"):
            value = getattr(self, field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise DomainValidationError(f"{field} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class ArchiveCleanupDryRun:
    dry_run_id: str
    actor_admin_user_id: str
    filter_snapshot: Mapping[str, object]
    filter_hash: str
    estimate: ArchiveCleanupEstimate
    created_at: datetime

    def __post_init__(self) -> None:
        _uuid(self.dry_run_id, field="dry_run_id")
        _uuid(self.actor_admin_user_id, field="actor_admin_user_id")
        ensure_aware(self.created_at, field="created_at")
        if self.filter_hash != cleanup_filter_hash(self.filter_snapshot):
            raise DomainValidationError("cleanup dry-run filter hash does not match its snapshot")


@dataclass(frozen=True, slots=True)
class ConfirmedArchiveCleanupJob:
    job_id: str
    dry_run_id: str
    filter_hash: str
    estimate: ArchiveCleanupEstimate
    item_count: int
    status: str
    created_at: datetime

    def __post_init__(self) -> None:
        _uuid(self.job_id, field="job_id")
        _uuid(self.dry_run_id, field="dry_run_id")
        if not isinstance(self.filter_hash, str) or _SHA256_RE.fullmatch(self.filter_hash) is None:
            raise DomainValidationError("filter_hash must be a lowercase SHA-256 digest")
        if (
            not isinstance(self.item_count, int)
            or isinstance(self.item_count, bool)
            or self.item_count < 0
        ):
            raise DomainValidationError("item_count must be a non-negative integer")
        if self.status not in _JOB_STATUSES:
            raise DomainValidationError("unsupported cleanup job status")
        ensure_aware(self.created_at, field="created_at")


@dataclass(frozen=True, slots=True)
class CleanupBlob:
    storage_backend: str
    storage_key: str
    size_bytes: int

    def __post_init__(self) -> None:
        if not self.storage_backend.strip() or not self.storage_key.strip():
            raise DomainValidationError("cleanup blob storage fields must not be blank")
        if (
            not isinstance(self.size_bytes, int)
            or isinstance(self.size_bytes, bool)
            or self.size_bytes <= 0
        ):
            raise DomainValidationError("cleanup blob size_bytes must be positive")


@dataclass(frozen=True, slots=True)
class CleanupAsset:
    asset_kind: str
    asset_id: str
    message_id: str
    blobs: tuple[CleanupBlob, ...]
    previous_archive_size: int

    def __post_init__(self) -> None:
        if self.asset_kind not in _MEDIA_TYPES:
            raise DomainValidationError("cleanup asset_kind must be image or video")
        _uuid(self.asset_id, field="asset_id")
        _uuid(self.message_id, field="message_id")
        if (
            not isinstance(self.previous_archive_size, int)
            or isinstance(self.previous_archive_size, bool)
            or self.previous_archive_size < 0
        ):
            raise DomainValidationError("previous_archive_size must be non-negative")
        if sum(blob.size_bytes for blob in self.blobs) != self.previous_archive_size:
            raise DomainValidationError("cleanup blob sizes must equal previous_archive_size")
        if self.asset_kind == "image" and len(self.blobs) != 1:
            raise DomainValidationError("image cleanup requires exactly one archive blob")


@dataclass(frozen=True, slots=True)
class ClaimedArchiveCleanupJob:
    job_id: str
    lease_token: str
    attempt_number: int
    max_attempts: int

    def __post_init__(self) -> None:
        _uuid(self.job_id, field="job_id")
        _uuid(self.lease_token, field="lease_token")
        if (
            not isinstance(self.attempt_number, int)
            or isinstance(self.attempt_number, bool)
            or not isinstance(self.max_attempts, int)
            or isinstance(self.max_attempts, bool)
            or self.attempt_number <= 0
            or self.attempt_number > self.max_attempts
        ):
            raise DomainValidationError("cleanup attempt numbers are invalid")


@dataclass(frozen=True, slots=True)
class ClaimedCleanupItem:
    item_id: str
    deletion_token: str
    asset: CleanupAsset

    def __post_init__(self) -> None:
        _uuid(self.item_id, field="item_id")
        _uuid(self.deletion_token, field="deletion_token")


class ArchiveCleanupRepository(Protocol):
    async def create_dry_run(
        self,
        *,
        cleanup_filter: ArchiveCleanupFilter,
        actor_admin_user_id: str,
        now: datetime,
    ) -> ArchiveCleanupDryRun: ...

    async def confirm(
        self,
        *,
        dry_run_id: str,
        expected_filter_hash: str,
        confirmed_by_admin_user_id: str,
        now: datetime,
        max_attempts: int = 5,
    ) -> ConfirmedArchiveCleanupJob: ...

    async def claim(
        self, *, job_id: str, now: datetime, lease_duration: timedelta
    ) -> ClaimedArchiveCleanupJob | None: ...

    async def claim_next_item(
        self, *, claim: ClaimedArchiveCleanupJob, now: datetime
    ) -> ClaimedCleanupItem | None: ...

    async def finalize_item_deleted(
        self, *, claim: ClaimedArchiveCleanupJob, item: ClaimedCleanupItem, now: datetime
    ) -> bool: ...

    async def complete(self, *, claim: ClaimedArchiveCleanupJob, now: datetime) -> bool: ...

    async def retry_or_fail(
        self,
        *,
        claim: ClaimedArchiveCleanupJob,
        item: ClaimedCleanupItem | None,
        error_code: str,
        error_type: str,
        retryable: bool,
        retry_at: datetime,
        now: datetime,
    ) -> bool: ...

    async def complete_wakeup_if_terminal(self, *, job_id: str, now: datetime) -> bool: ...

    async def repair_wakeups(self, *, now: datetime, limit: int = 500) -> int: ...


class ArchiveStorageResolver(Protocol):
    def resolve(self, *, storage_backend: str) -> ArchiveStorage: ...


@dataclass(frozen=True, slots=True)
class MappingArchiveStorageResolver:
    storages: Mapping[str, ArchiveStorage]

    def resolve(self, *, storage_backend: str) -> ArchiveStorage:
        storage = self.storages.get(storage_backend)
        if storage is None:
            raise DomainValidationError(f"unsupported archive storage backend: {storage_backend!r}")
        return storage
