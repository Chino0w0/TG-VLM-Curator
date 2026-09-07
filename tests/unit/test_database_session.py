from __future__ import annotations

import unittest

from tgcurator.infrastructure.database import (
    AsyncDatabase,
    InvalidDatabaseUrlError,
    validate_async_database_url,
)


class DatabaseSessionTests(unittest.IsolatedAsyncioTestCase):
    def test_runtime_database_requires_postgresql_asyncpg(self) -> None:
        valid = validate_async_database_url(
            "postgresql+asyncpg://curator:secret@localhost:5432/tgcurator"
        )
        self.assertEqual(valid.drivername, "postgresql+asyncpg")

        for invalid in (
            "postgresql://curator:secret@localhost/tgcurator",
            "sqlite+aiosqlite:///curator.db",
            "not a url",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(InvalidDatabaseUrlError):
                    validate_async_database_url(invalid)

    async def test_unconfigured_database_is_not_ready_without_creating_an_engine(self) -> None:
        database = AsyncDatabase(None)
        self.assertFalse(database.configured)
        self.assertFalse(await database.ping())
        await database.dispose()

    def test_database_rejects_invalid_url_during_composition(self) -> None:
        with self.assertRaises(InvalidDatabaseUrlError):
            AsyncDatabase("sqlite+aiosqlite:///curator.db")


if __name__ == "__main__":
    unittest.main()
