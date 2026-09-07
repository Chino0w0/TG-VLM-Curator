from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from tgcurator.application.ports.admin import AdminBootstrapRepository, PasswordHasher


class BootstrapAlreadyCompleteError(RuntimeError):
    """Raised when an administrator has already been initialized."""


@dataclass(slots=True)
class BootstrapAdminService:
    repository: AdminBootstrapRepository
    password_hasher: PasswordHasher

    async def bootstrap(self, *, username: str, password: str) -> UUID:
        normalized_username = username.strip()
        if not normalized_username:
            raise ValueError("username must not be blank")
        if len(normalized_username) > 128:
            raise ValueError("username must be at most 128 characters")
        if len(password) < 12:
            raise ValueError("password must be at least 12 characters")

        password_hash = self.password_hasher.hash(password)
        admin_id = await self.repository.create_first_active_admin(
            username=normalized_username,
            password_hash=password_hash,
        )
        if admin_id is None:
            raise BootstrapAlreadyCompleteError("an active administrator already exists")
        return admin_id
