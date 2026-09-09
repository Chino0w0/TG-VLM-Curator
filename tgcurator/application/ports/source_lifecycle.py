from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol

from tgcurator.domain.messages import MediaAsset


class SourceMessageLifecycleRepository(Protocol):
    """Persist source-side edits and deletion markers without deleting historical evidence."""

    async def record_edit(
        self,
        *,
        source_channel_id: str,
        telegram_message_id: int,
        original_text: str | None,
        telegram_metadata: Mapping[str, Any],
        media: tuple[MediaAsset, ...],
        edited_at: datetime,
    ) -> bool:
        """Apply a newer complete part snapshot and rebuild its logical parent."""

    async def record_deletion(
        self,
        *,
        source_channel_id: str,
        telegram_message_id: int,
        deleted_at: datetime,
    ) -> bool:
        """Record the first observed source deletion without removing retained metadata."""
