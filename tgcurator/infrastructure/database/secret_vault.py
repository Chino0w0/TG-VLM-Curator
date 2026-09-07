from __future__ import annotations

from uuid import UUID

from sqlalchemy import select

from tgcurator.application.ports.secrets import SecretStatus
from tgcurator.infrastructure.database.models import EncryptedSecret
from tgcurator.infrastructure.database.session import AsyncDatabase
from tgcurator.infrastructure.security.aes_gcm import AesGcmSecretCipher, EncryptedPayload


class SecretNotFoundError(LookupError):
    """Raised when a secret reference does not exist."""


class SecretTypeMismatchError(ValueError):
    """Raised when a secret reference is used as the wrong secret type."""


class SqlAlchemySecretVault:
    """Persist AES-GCM payloads without exposing plaintext as metadata."""

    def __init__(self, database: AsyncDatabase, cipher: AesGcmSecretCipher) -> None:
        self._database = database
        self._cipher = cipher

    async def store(self, *, secret_type: str, plaintext: bytes) -> UUID:
        normalized_type = self._normalize_secret_type(secret_type)
        payload = self._cipher.encrypt(
            plaintext,
            associated_data=normalized_type.encode("utf-8"),
        )
        async with self._database.session() as session:
            async with session.begin():
                secret = EncryptedSecret(
                    secret_type=normalized_type,
                    ciphertext=payload.ciphertext,
                    nonce=payload.nonce,
                    key_id=payload.key_id,
                )
                session.add(secret)
                await session.flush()
                return secret.id

    async def resolve(
        self,
        *,
        secret_id: UUID,
        expected_secret_type: str | None = None,
    ) -> bytes:
        normalized_expected_type = (
            self._normalize_secret_type(expected_secret_type)
            if expected_secret_type is not None
            else None
        )
        async with self._database.session() as session:
            secret = await session.get(EncryptedSecret, secret_id)
            if secret is None:
                raise SecretNotFoundError("secret reference does not exist")
            if (
                normalized_expected_type is not None
                and secret.secret_type != normalized_expected_type
            ):
                raise SecretTypeMismatchError("secret reference has an unexpected type")
            return self._cipher.decrypt(
                EncryptedPayload(
                    ciphertext=secret.ciphertext,
                    nonce=secret.nonce,
                    key_id=secret.key_id,
                ),
                associated_data=secret.secret_type.encode("utf-8"),
            )

    async def status(self, *, secret_id: UUID) -> SecretStatus | None:
        async with self._database.session() as session:
            row = await session.execute(
                select(
                    EncryptedSecret.id,
                    EncryptedSecret.secret_type,
                    EncryptedSecret.key_id,
                    EncryptedSecret.created_at,
                ).where(EncryptedSecret.id == secret_id)
            )
            secret = row.one_or_none()
            if secret is None:
                return None
            return SecretStatus(
                secret_id=secret.id,
                secret_type=secret.secret_type,
                key_id=secret.key_id,
                created_at=secret.created_at,
            )

    @staticmethod
    def _normalize_secret_type(secret_type: str) -> str:
        if not isinstance(secret_type, str):
            raise TypeError("secret_type must be a string")
        normalized_type = secret_type.strip()
        if not normalized_type:
            raise ValueError("secret_type must not be blank")
        if len(normalized_type) > 64:
            raise ValueError("secret_type must be at most 64 characters")
        return normalized_type
