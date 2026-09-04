from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
VALID_DATABASE_URL = "postgresql+asyncpg://curator:curator@localhost:5432/tgcurator"


class AlembicOfflineSqlTests(unittest.TestCase):
    def run_alembic(self, database_url: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.pop("DATABASE_URL", None)
        environment["TGCURATOR_DATABASE_URL"] = database_url
        return subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
            cwd=REPOSITORY_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_migrations_render_postgresql_ddl_without_connecting(self) -> None:
        result = self.run_alembic(VALID_DATABASE_URL)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CREATE TABLE processing_ranges", result.stdout)
        self.assertIn("CREATE TABLE range_executions", result.stdout)
        self.assertIn("CREATE TABLE durable_wakeups", result.stdout)
        self.assertIn("CREATE TABLE message_parts", result.stdout)
        self.assertIn("CREATE TABLE image_assets", result.stdout)
        self.assertIn("ck_image_asset_ready_metadata", result.stdout)
        self.assertIn("uq_image_asset_message_source", result.stdout)
        self.assertIn("ck_range_execution_watermark_bounds", result.stdout)
        self.assertIn("uq_durable_wakeup_queue_entity", result.stdout)
        self.assertIn("uq_messages_source_group", result.stdout)
        self.assertIn("uq_message_part_source_telegram", result.stdout)
        self.assertIn("CREATE UNIQUE INDEX uq_range_execution_one_active", result.stdout)
        self.assertIn("JSONB", result.stdout)
        self.assertIn("TIMESTAMP WITH TIME ZONE", result.stdout)
        self.assertIn("CREATE UNIQUE INDEX uq_admin_users_single_active", result.stdout)
        self.assertIn("trg_source_profile_version_immutable", result.stdout)
        self.assertNotIn("sqlite", result.stdout.lower())

    def test_migrations_reject_non_postgresql_urls(self) -> None:
        result = self.run_alembic("sqlite+aiosqlite://")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PostgreSQL only", result.stderr)


if __name__ == "__main__":
    unittest.main()
