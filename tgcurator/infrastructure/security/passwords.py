from __future__ import annotations

from argon2 import PasswordHasher as Argon2LibraryHasher
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.low_level import Type


class Argon2idPasswordHasher:
    """Argon2id password hashing adapter for administrator credentials."""

    def __init__(self) -> None:
        self._hasher = Argon2LibraryHasher(type=Type.ID)

    def hash(self, password: str) -> str:
        if not isinstance(password, str) or not password:
            raise ValueError("password must not be empty")
        return self._hasher.hash(password)

    def verify(self, password_hash: str, password: str) -> bool:
        if not isinstance(password_hash, str) or not isinstance(password, str):
            return False
        try:
            return self._hasher.verify(password_hash, password)
        except (InvalidHashError, VerificationError):
            return False

    def needs_rehash(self, password_hash: str) -> bool:
        try:
            return self._hasher.check_needs_rehash(password_hash)
        except InvalidHashError:
            return True
