from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
REVISION = "94c2d3062de4"
VALID_DATABASE_URL = "postgresql+asyncpg://curator:curator@localhost:5432/tgcurator"


class AlembicOfflineSqlTests(unittest.TestCase):
    def run_alembic(
        self,
        *arguments: str,
        database_url: str = VALID_DATABASE_URL,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.pop("DATABASE_URL", None)
        environment["TGCURATOR_DATABASE_URL"] = database_url
        return subprocess.run(
            [sys.executable, "-m", "alembic", *arguments],
            cwd=REPOSITORY_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    def test_upgrade_renders_complete_m1_postgresql_ddl_without_connecting(self) -> None:
        result = self.run_alembic("upgrade", "head", "--sql")

        self.assertEqual(result.returncode, 0, result.stderr)
        for table_name in (
            "admin_users",
            "encrypted_secrets",
            "audit_events",
            "telegram_identities",
            "source_channels",
            "destination_channels",
            "source_channel_profiles",
            "source_channel_profile_versions",
            "processing_ranges",
            "messages",
        ):
            self.assertIn(f"CREATE TABLE {table_name}", result.stdout)
        self.assertNotIn("range_executions", result.stdout)
        self.assertNotIn("durable_wakeups", result.stdout)
        self.assertIn("JSONB", result.stdout)
        self.assertIn("BIGINT[]", result.stdout)
        self.assertIn("TIMESTAMP WITH TIME ZONE", result.stdout)
        self.assertIn("CREATE UNIQUE INDEX uq_admin_users_single_active", result.stdout)
        self.assertIn("CREATE UNIQUE INDEX uq_source_profile_one_published", result.stdout)
        self.assertIn("BEFORE UPDATE OR DELETE", result.stdout)
        self.assertIn("retired profile versions are immutable", result.stdout)
        self.assertIn("draft profile versions can only be published", result.stdout)
        self.assertNotIn("sqlite", result.stdout.lower())

    def test_downgrade_renders_trigger_constraint_and_table_cleanup(self) -> None:
        result = self.run_alembic("downgrade", f"{REVISION}:base", "--sql")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DROP TRIGGER trg_source_profile_version_immutable", result.stdout)
        self.assertIn(
            "DROP FUNCTION tgcurator_enforce_source_profile_version_immutability", result.stdout
        )
        self.assertIn("DROP TABLE messages", result.stdout)
        self.assertIn("DROP TABLE admin_users", result.stdout)

    def test_migrations_reject_non_postgresql_urls(self) -> None:
        result = self.run_alembic(
            "upgrade",
            "head",
            "--sql",
            database_url="sqlite+aiosqlite:///curator.db",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PostgreSQL through asyncpg only", result.stderr)

        sync_result = self.run_alembic(
            "upgrade",
            "head",
            "--sql",
            database_url="postgresql://curator:curator@localhost/tgcurator",
        )
        self.assertNotEqual(sync_result.returncode, 0)
        self.assertIn("PostgreSQL through asyncpg only", sync_result.stderr)


if __name__ == "__main__":
    unittest.main()
