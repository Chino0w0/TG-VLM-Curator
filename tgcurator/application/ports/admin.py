from __future__ import annotations

from typing import Protocol
from uuid import UUID


class AdminBootstrapRepository(Protocol):
    """Persistence boundary for the one-time initial administrator."""

    async def create_first_active_admin(self, *, username: str, password_hash: str) -> UUID | None:
        """Create the only active admin, or return None when one already exists."""


class PasswordHasher(Protocol):
    """Password hashing capability; implementations must use a password KDF."""

    def hash(self, password: str) -> str: ...

    def verify(self, password_hash: str, password: str) -> bool: ...
