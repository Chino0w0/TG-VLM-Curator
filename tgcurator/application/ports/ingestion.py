from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from tgcurator.domain.messages import NormalizedTelegramMessage


@dataclass(frozen=True, slots=True)
class IngestReport:
    """Result of one idempotent history or Update ingestion call."""

    logical_messages: int
    component_messages: int


class TelegramMessageIngestRepository(Protocol):
    """Atomic persistence boundary for normalized Telegram messages and their parts."""

    async def upsert_message(self, *, message: NormalizedTelegramMessage) -> None:
        """Persist one logical message without losing known group members on replay."""
