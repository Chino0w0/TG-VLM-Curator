from __future__ import annotations

from typing import Protocol


class SourceReconciliationCursorRepository(Protocol):
    """Own monotonic newest-observed and successfully-ingested Telegram cursors."""

    async def advance_latest_seen_message_id(
        self, *, source_channel_id: str, telegram_message_id: int
    ) -> bool:
        """Advance the newest Telegram boundary if and only if it is greater."""

    async def advance_last_seen_message_id(
        self, *, source_channel_id: str, telegram_message_id: int
    ) -> bool:
        """Advance the successfully-ingested cursor if and only if it is greater."""

    async def get_latest_seen_message_id(self, *, source_channel_id: str) -> int | None:
        """Return the greatest observed Telegram boundary, if any."""

    async def get_last_seen_message_id(self, *, source_channel_id: str) -> int | None:
        """Return the durable ingestion cursor, or None before successful ingestion."""
