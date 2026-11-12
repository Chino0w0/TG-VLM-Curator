from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from tgcurator.shared import DomainValidationError, ensure_aware


@dataclass(frozen=True, slots=True)
class ExpectedArchiveBlob:
    """Database-recorded archive object metadata for consistency verification."""

    entity_kind: str
    entity_id: str
    storage_backend: str
    storage_key: str
    archive_state: str
    expected_size_bytes: int | None = None
    expected_sha256: str | None = None
    parent_asset_id: str | None = None

    def __post_init__(self) -> None:
        if self.entity_kind not in {"image_asset", "video_frame"}:
            raise DomainValidationError(f"unsupported archive entity_kind: {self.entity_kind!r}")
        if not self.entity_id.strip():
            raise DomainValidationError("entity_id must not be blank")
        if not self.storage_backend.strip():
            raise DomainValidationError("storage_backend must not be blank")
        if not self.storage_key.strip():
            raise DomainValidationError("storage_key must not be blank")
        if not self.archive_state.strip():
            raise DomainValidationError("archive_state must not be blank")


@dataclass(frozen=True, slots=True)
class ObservedStorageFile:
    """Regular file observed inside the local archive storage volume."""

    storage_key: str
    size_bytes: int
    sha256: str | None = None

    def __post_init__(self) -> None:
        if not self.storage_key.strip():
            raise DomainValidationError("storage_key must not be blank")
        if self.size_bytes < 0:
            raise DomainValidationError("size_bytes must be non-negative")


@dataclass(frozen=True, slots=True)
class MissingArchiveBlob:
    """READY database archive asset whose backing storage file is missing."""

    entity_kind: str
    entity_id: str
    storage_backend: str
    storage_key: str
    expected_size_bytes: int | None


@dataclass(frozen=True, slots=True)
class CorruptedArchiveBlob:
    """READY database archive asset whose backing file violates size or SHA-256."""

    entity_kind: str
    entity_id: str
    storage_backend: str
    storage_key: str
    reason: str
    expected_size_bytes: int | None
    actual_size_bytes: int
    expected_sha256: str | None = None
    actual_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class OrphanStorageFile:
    """Regular file present in archive storage without an active DB reference."""

    storage_key: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class StorageConsistencyReport:
    """Post-backup / post-restore archive consistency audit report."""

    scanned_at: datetime
    storage_backend: str
    checked_ready_count: int
    observed_file_count: int
    missing_blobs: tuple[MissingArchiveBlob, ...]
    corrupted_blobs: tuple[CorruptedArchiveBlob, ...]
    orphan_files: tuple[OrphanStorageFile, ...]

    def __post_init__(self) -> None:
        ensure_aware(self.scanned_at, field="scanned_at")
        if not self.storage_backend.strip():
            raise DomainValidationError("storage_backend must not be blank")

    @property
    def consistent(self) -> bool:
        return not self.missing_blobs and not self.corrupted_blobs and not self.orphan_files

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned_at": self.scanned_at.isoformat(),
            "storage_backend": self.storage_backend,
            "consistent": self.consistent,
            "checked_ready_count": self.checked_ready_count,
            "observed_file_count": self.observed_file_count,
            "missing_count": len(self.missing_blobs),
            "corrupted_count": len(self.corrupted_blobs),
            "orphan_count": len(self.orphan_files),
            "missing_blobs": [
                {
                    "entity_kind": item.entity_kind,
                    "entity_id": item.entity_id,
                    "storage_backend": item.storage_backend,
                    "storage_key": item.storage_key,
                    "expected_size_bytes": item.expected_size_bytes,
                }
                for item in self.missing_blobs
            ],
            "corrupted_blobs": [
                {
                    "entity_kind": item.entity_kind,
                    "entity_id": item.entity_id,
                    "storage_backend": item.storage_backend,
                    "storage_key": item.storage_key,
                    "reason": item.reason,
                    "expected_size_bytes": item.expected_size_bytes,
                    "actual_size_bytes": item.actual_size_bytes,
                    "expected_sha256": item.expected_sha256,
                    "actual_sha256": item.actual_sha256,
                }
                for item in self.corrupted_blobs
            ],
            "orphan_files": [
                {
                    "storage_key": item.storage_key,
                    "size_bytes": item.size_bytes,
                }
                for item in self.orphan_files
            ],
        }


def evaluate_storage_consistency(
    *,
    scanned_at: datetime,
    storage_backend: str,
    database_records: Sequence[ExpectedArchiveBlob],
    observed_files: Sequence[ObservedStorageFile],
    verify_sha256: bool = False,
) -> StorageConsistencyReport:
    """Compare database archive metadata against observed storage files.

    - Every `ready` record for `storage_backend` must exist on storage and match
      recorded size (and SHA-256 when `verify_sha256=True` and available).
    - Files referenced by `ready` or `deleting` records are considered known;
      any other observed file (including leftover blobs for `deleted` rows or
      unknown paths) is reported as an `OrphanStorageFile` without mutating DB
      state or deleting files automatically.
    """

    ensure_aware(scanned_at, field="scanned_at")
    normalized_backend = storage_backend.strip()
    if not normalized_backend:
        raise DomainValidationError("storage_backend must not be blank")

    observed_by_key: dict[str, ObservedStorageFile] = {
        item.storage_key: item for item in observed_files
    }
    referenced_active_keys: set[str] = set()
    missing: list[MissingArchiveBlob] = []
    corrupted: list[CorruptedArchiveBlob] = []
    checked_ready = 0

    sorted_records = sorted(
        (rec for rec in database_records if rec.storage_backend == normalized_backend),
        key=lambda r: (r.storage_key, r.entity_kind, r.entity_id),
    )

    for record in sorted_records:
        if record.archive_state in {"ready", "deleting"}:
            referenced_active_keys.add(record.storage_key)

        if record.archive_state != "ready":
            continue

        checked_ready += 1
        observed = observed_by_key.get(record.storage_key)
        if observed is None:
            missing.append(
                MissingArchiveBlob(
                    entity_kind=record.entity_kind,
                    entity_id=record.entity_id,
                    storage_backend=record.storage_backend,
                    storage_key=record.storage_key,
                    expected_size_bytes=record.expected_size_bytes,
                )
            )
            continue

        if (
            record.expected_size_bytes is not None
            and observed.size_bytes != record.expected_size_bytes
        ):
            corrupted.append(
                CorruptedArchiveBlob(
                    entity_kind=record.entity_kind,
                    entity_id=record.entity_id,
                    storage_backend=record.storage_backend,
                    storage_key=record.storage_key,
                    reason="size_mismatch",
                    expected_size_bytes=record.expected_size_bytes,
                    actual_size_bytes=observed.size_bytes,
                    expected_sha256=record.expected_sha256,
                    actual_sha256=observed.sha256,
                )
            )
            continue

        if (
            verify_sha256
            and record.expected_sha256 is not None
            and observed.sha256 is not None
            and observed.sha256.lower() != record.expected_sha256.lower()
        ):
            corrupted.append(
                CorruptedArchiveBlob(
                    entity_kind=record.entity_kind,
                    entity_id=record.entity_id,
                    storage_backend=record.storage_backend,
                    storage_key=record.storage_key,
                    reason="sha256_mismatch",
                    expected_size_bytes=record.expected_size_bytes,
                    actual_size_bytes=observed.size_bytes,
                    expected_sha256=record.expected_sha256,
                    actual_sha256=observed.sha256,
                )
            )

    orphans = [
        OrphanStorageFile(storage_key=obs.storage_key, size_bytes=obs.size_bytes)
        for obs in sorted(observed_files, key=lambda item: item.storage_key)
        if obs.storage_key not in referenced_active_keys
    ]

    return StorageConsistencyReport(
        scanned_at=scanned_at,
        storage_backend=normalized_backend,
        checked_ready_count=checked_ready,
        observed_file_count=len(observed_files),
        missing_blobs=tuple(missing),
        corrupted_blobs=tuple(corrupted),
        orphan_files=tuple(orphans),
    )
