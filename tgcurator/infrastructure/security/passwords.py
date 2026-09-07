from __future__ import annotations

from argon2 import PasswordHasher as Argon2LibraryHasher
from argon2.exceptions import InvalidHashError, VerificationError


class Argon2idPasswordHasher:
    """Argon2id password hashing adapter for administrator credentials."""

    def __init__(self) -> None:
        self._hasher = Argon2LibraryHasher()

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except (InvalidHashError, VerificationError):
            return False
