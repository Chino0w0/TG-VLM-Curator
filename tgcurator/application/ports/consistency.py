from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from tgcurator.domain.archive import ExpectedArchiveBlob, ObservedStorageFile


class ArchiveConsistencyRepository(Protocol):
    """Read-only database port returning expected archive blob records."""

    async def list_archive_blob_records(
        self,
        *,
        storage_backend: str,
    ) -> Sequence[ExpectedArchiveBlob]: ...


class ArchiveObjectEnumerator(Protocol):
    """Read-only storage port enumerating regular files inside an archive root."""

    async def list_observed_files(
        self,
        *,
        compute_sha256: bool = False,
    ) -> Sequence[ObservedStorageFile]: ...
