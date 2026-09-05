from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class SecretStatus:
    """Safe metadata suitable for an admin UI; it contains no secret material."""

    secret_id: UUID
    secret_type: str
    key_id: str
    created_at: datetime


class SecretVault(Protocol):
    """Stores encrypted material and only resolves it for trusted application use cases."""

    async def store(self, *, secret_type: str, plaintext: bytes) -> UUID: ...

    async def resolve(
        self, *, secret_id: UUID, expected_secret_type: str | None = None
    ) -> bytes: ...

    async def status(self, *, secret_id: UUID) -> SecretStatus | None: ...
