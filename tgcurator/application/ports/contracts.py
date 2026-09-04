from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol

from tgcurator.domain.messages import TelegramMessage


class TaskDispatcher(Protocol):
    """Best-effort wake-up mechanism. It never determines business completion."""

    async def dispatch(self, *, queue: str, entity_id: str) -> None: ...


class ArchiveStorage(Protocol):
    """Storage boundary; callers persist backend/key instead of host paths."""

    async def put(self, *, key: str, content: bytes, content_type: str) -> int: ...

    async def open(self, *, key: str) -> bytes: ...

    async def exists(self, *, key: str) -> bool: ...

    async def delete(self, *, key: str) -> None: ...

    async def size(self, *, key: str) -> int: ...


class TelegramGateway(Protocol):
    """Telegram integration boundary; adapters translate platform errors to application errors."""

    async def newest_message_at(self, *, source_channel_id: str) -> datetime | None: ...

    async def fetch_history(
        self, *, source_channel_id: str, from_at: datetime, to_at: datetime
    ) -> tuple[TelegramMessage, ...]: ...

    async def forward(
        self,
        *,
        identity_id: str,
        destination_channel_id: str,
        source_message_id: str,
        random_id: int,
    ) -> tuple[str, ...]: ...

    async def send_metadata(
        self, *, identity_id: str, destination_channel_id: str, text: str, random_id: int
    ) -> str: ...


class InferenceProvider(Protocol):
    """Versioned HTTP inference capability, isolated from analysis domain rules."""

    async def infer(
        self,
        *,
        profile_version_id: str,
        prompt: str,
        input_manifest: Mapping[str, Any],
        response_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...
