from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from tgcurator.application.ports.cleanup import (
    ArchiveCleanupDryRun,
    ArchiveCleanupFilter,
    ArchiveCleanupRepository,
    ConfirmedArchiveCleanupJob,
)
from tgcurator.shared import ensure_aware


@dataclass(slots=True)
class ArchiveCleanupService:
    """Create auditable cleanup previews and confirm an exact frozen preview."""

    repository: ArchiveCleanupRepository

    async def dry_run(
        self,
        *,
        cleanup_filter: ArchiveCleanupFilter,
        actor_admin_user_id: str,
        now: datetime,
    ) -> ArchiveCleanupDryRun:
        ensure_aware(now, field="now")
        return await self.repository.create_dry_run(
            cleanup_filter=cleanup_filter,
            actor_admin_user_id=actor_admin_user_id,
            now=now,
        )

    async def confirm(
        self,
        *,
        dry_run_id: str,
        expected_filter_hash: str,
        confirmed_by_admin_user_id: str,
        now: datetime,
        max_attempts: int = 5,
    ) -> ConfirmedArchiveCleanupJob:
        ensure_aware(now, field="now")
        return await self.repository.confirm(
            dry_run_id=dry_run_id,
            expected_filter_hash=expected_filter_hash,
            confirmed_by_admin_user_id=confirmed_by_admin_user_id,
            now=now,
            max_attempts=max_attempts,
        )
