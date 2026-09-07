from __future__ import annotations

import base64
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

from tgcurator.infrastructure.database.models import EncryptedSecret
from tgcurator.infrastructure.database.secret_vault import (
    SecretTypeMismatchError,
    SqlAlchemySecretVault,
)
from tgcurator.infrastructure.security import AesGcmSecretCipher, SecretDecryptionError

MASTER_KEY = base64.b64encode(b"v" * 32).decode("ascii")


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        return None


class FakeResult:
    def __init__(self, value: object | None) -> None:
        self.value = value

    def one_or_none(self) -> object | None:
        return self.value


class FakeSession:
    def __init__(self) -> None:
        self.secrets: dict[object, EncryptedSecret] = {}

    def begin(self) -> FakeTransaction:
        return FakeTransaction()

    def add(self, secret: EncryptedSecret) -> None:
        secret.id = uuid4()
        self.secrets[secret.id] = secret

    async def flush(self) -> None:
        return None

    async def get(self, model: type[EncryptedSecret], secret_id: object) -> EncryptedSecret | None:
        self.assert_model(model)
        return self.secrets.get(secret_id)

    async def execute(self, statement: object) -> FakeResult:
        del statement
        secret = next(iter(self.secrets.values()), None)
        if secret is None:
            return FakeResult(None)
        return FakeResult(
            SimpleNamespace(
                id=secret.id,
                secret_type=secret.secret_type,
                key_id=secret.key_id,
                created_at=secret.created_at,
            )
        )

    @staticmethod
    def assert_model(model: type[EncryptedSecret]) -> None:
        if model is not EncryptedSecret:
            raise AssertionError("unexpected model")


class FakeDatabase:
    def __init__(self) -> None:
        self.fake_session = FakeSession()

    @asynccontextmanager
    async def session(self):
        yield self.fake_session


class SecretVaultTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.database = FakeDatabase()
        self.vault = SqlAlchemySecretVault(
            self.database,  # type: ignore[arg-type]
            AesGcmSecretCipher(master_key=MASTER_KEY, key_id="m1-key"),
        )

    async def test_secret_type_is_normalized_and_bound_as_authenticated_data(self) -> None:
        secret_id = await self.vault.store(
            secret_type="  telegram_session  ",
            plaintext=b"session-material",
        )

        resolved = await self.vault.resolve(
            secret_id=secret_id,
            expected_secret_type="telegram_session",
        )
        self.assertEqual(resolved, b"session-material")

        persisted = self.database.fake_session.secrets[secret_id]
        persisted.secret_type = "bot_token"
        with self.assertRaises(SecretDecryptionError):
            await self.vault.resolve(secret_id=secret_id)

    async def test_rejects_wrong_expected_type_before_decryption(self) -> None:
        secret_id = await self.vault.store(secret_type="bot_token", plaintext=b"token")

        with self.assertRaises(SecretTypeMismatchError):
            await self.vault.resolve(
                secret_id=secret_id,
                expected_secret_type="telegram_session",
            )

    async def test_status_returns_only_safe_metadata(self) -> None:
        secret_id = await self.vault.store(secret_type="bot_token", plaintext=b"token")
        status = await self.vault.status(secret_id=secret_id)

        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual(status.secret_id, secret_id)
        self.assertEqual(status.secret_type, "bot_token")
        self.assertEqual(status.key_id, "m1-key")
        self.assertFalse(hasattr(status, "ciphertext"))
        self.assertFalse(hasattr(status, "plaintext"))


if __name__ == "__main__":
    unittest.main()
