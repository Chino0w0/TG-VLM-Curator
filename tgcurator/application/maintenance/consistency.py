from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from tgcurator.application.ports.consistency import (
    ArchiveConsistencyRepository,
    ArchiveObjectEnumerator,
)
from tgcurator.domain.archive import (
    StorageConsistencyReport,
    evaluate_storage_consistency,
)


@dataclass(slots=True)
class StorageConsistencyScanner:
    """Maintenance scanner auditing READY archive metadata vs storage files."""

    repository: ArchiveConsistencyRepository
    storage: ArchiveObjectEnumerator
    storage_backend: str = "local"

    async def scan(
        self,
        *,
        now: datetime,
        verify_sha256: bool = False,
    ) -> StorageConsistencyReport:
        records = await self.repository.list_archive_blob_records(
            storage_backend=self.storage_backend,
        )
        observed = await self.storage.list_observed_files(
            compute_sha256=verify_sha256,
        )
        return evaluate_storage_consistency(
            scanned_at=now,
            storage_backend=self.storage_backend,
            database_records=records,
            observed_files=observed,
            verify_sha256=verify_sha256,
        )
