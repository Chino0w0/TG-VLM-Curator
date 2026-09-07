from __future__ import annotations

import unittest
from uuid import UUID, uuid4

from tgcurator.application.admin import BootstrapAdminService, BootstrapAlreadyCompleteError
from tgcurator.infrastructure.security import Argon2idPasswordHasher


class FakeRepository:
    def __init__(self, result: UUID | None) -> None:
        self.result = result
        self.username: str | None = None
        self.password_hash: str | None = None

    async def create_first_active_admin(self, *, username: str, password_hash: str) -> UUID | None:
        self.username = username
        self.password_hash = password_hash
        return self.result


class RecordingPasswordHasher:
    def __init__(self) -> None:
        self.password: str | None = None

    def hash(self, password: str) -> str:
        self.password = password
        return f"hashed:{password}"

    def verify(self, password_hash: str, password: str) -> bool:
        return password_hash == self.hash(password)


class BootstrapAdminServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_bootstrap_hashes_password_and_normalizes_username(self) -> None:
        admin_id = uuid4()
        repository = FakeRepository(admin_id)
        hasher = RecordingPasswordHasher()
        service = BootstrapAdminService(repository, hasher)

        result = await service.bootstrap(username="  curator  ", password="a secure password")

        self.assertEqual(result, admin_id)
        self.assertEqual(repository.username, "curator")
        self.assertEqual(repository.password_hash, "hashed:a secure password")
        self.assertEqual(hasher.password, "a secure password")

    async def test_bootstrap_rejects_invalid_input_before_hashing(self) -> None:
        repository = FakeRepository(uuid4())
        hasher = RecordingPasswordHasher()
        service = BootstrapAdminService(repository, hasher)

        for username, password in (("", "a secure password"), ("curator", "short")):
            with self.subTest(username=username, password=password):
                with self.assertRaises(ValueError):
                    await service.bootstrap(username=username, password=password)
        self.assertIsNone(hasher.password)
        self.assertIsNone(repository.password_hash)

    async def test_bootstrap_is_one_time_only(self) -> None:
        service = BootstrapAdminService(FakeRepository(None), RecordingPasswordHasher())

        with self.assertRaises(BootstrapAlreadyCompleteError):
            await service.bootstrap(username="curator", password="a secure password")

    async def test_real_password_adapter_uses_argon2id_and_verifies(self) -> None:
        hasher = Argon2idPasswordHasher()
        password_hash = hasher.hash("a secure password")

        self.assertEqual(password_hash.split(chr(36))[1], "argon2id")
        self.assertTrue(hasher.verify(password_hash, "a secure password"))
        self.assertFalse(hasher.verify(password_hash, "wrong password"))
        self.assertFalse(hasher.verify("not-an-argon2-hash", "a secure password"))


if __name__ == "__main__":
    unittest.main()
