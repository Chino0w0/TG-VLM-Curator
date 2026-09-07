from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class SecretStatus:
    """Safe secret metadata suitable for an admin UI."""

    secret_id: UUID
    secret_type: str
    key_id: str
    created_at: datetime


class SecretVault(Protocol):
    """Encrypted storage that resolves plaintext only for trusted application code."""

    async def store(self, *, secret_type: str, plaintext: bytes) -> UUID: ...

    async def resolve(
        self,
        *,
        secret_id: UUID,
        expected_secret_type: str | None = None,
    ) -> bytes: ...

    async def status(self, *, secret_id: UUID) -> SecretStatus | None: ...
