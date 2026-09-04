from __future__ import annotations

import unittest
from uuid import UUID, uuid4

from tgcurator.application.admin import BootstrapAdminService, BootstrapAlreadyCompleteError


class FakeRepository:
    def __init__(self, result: UUID | None) -> None:
        self.result = result
        self.username: str | None = None
        self.password_hash: str | None = None

    async def create_first_active_admin(self, *, username: str, password_hash: str) -> UUID | None:
        self.username = username
        self.password_hash = password_hash
        return self.result


class FakePasswordHasher:
    def hash(self, password: str) -> str:
        return f"argon2id:{password}"

    def verify(self, password_hash: str, password: str) -> bool:
        return password_hash == self.hash(password)


class BootstrapAdminServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_bootstrap_hashes_password_and_normalizes_username(self) -> None:
        admin_id = uuid4()
        repository = FakeRepository(admin_id)
        service = BootstrapAdminService(repository, FakePasswordHasher())

        result = await service.bootstrap(username="  curator  ", password="a secure password")

        self.assertEqual(result, admin_id)
        self.assertEqual(repository.username, "curator")
        self.assertEqual(repository.password_hash, "argon2id:a secure password")

    async def test_bootstrap_rejects_weak_password_before_hashing(self) -> None:
        repository = FakeRepository(uuid4())
        service = BootstrapAdminService(repository, FakePasswordHasher())

        with self.assertRaisesRegex(ValueError, "at least 12"):
            await service.bootstrap(username="curator", password="short")
        self.assertIsNone(repository.password_hash)

    async def test_bootstrap_is_one_time_only(self) -> None:
        service = BootstrapAdminService(FakeRepository(None), FakePasswordHasher())

        with self.assertRaises(BootstrapAlreadyCompleteError):
            await service.bootstrap(username="curator", password="a secure password")


if __name__ == "__main__":
    unittest.main()
