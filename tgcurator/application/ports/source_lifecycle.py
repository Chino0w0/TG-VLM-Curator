from __future__ import annotations

from datetime import datetime
from typing import Protocol


class SourceMessageLifecycleRepository(Protocol):
    """Persist source-side edits and deletion markers without deleting historical evidence."""

    async def record_edit(
        self,
        *,
        source_channel_id: str,
        telegram_message_id: int,
        text: str | None,
        edited_at: datetime,
    ) -> bool:
        """Apply a newer edit to the logical message containing the Telegram component."""

    async def record_deletion(
        self,
        *,
        source_channel_id: str,
        telegram_message_id: int,
        deleted_at: datetime,
    ) -> bool:
        """Record the first observed source deletion without removing retained metadata."""
