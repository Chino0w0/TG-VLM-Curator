from __future__ import annotations

from typing import Protocol


class SourceReconciliationCursorRepository(Protocol):
    """Durably record the greatest Telegram message observed for a source channel."""

    async def advance_last_seen_message_id(
        self, *, source_channel_id: str, telegram_message_id: int
    ) -> bool:
        """Advance the cursor if and only if `telegram_message_id` is greater."""

    async def get_last_seen_message_id(self, *, source_channel_id: str) -> int | None:
        """Return the durable cursor, or `None` before any message has been observed."""
