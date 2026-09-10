from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
M1_REVISION = "94c2d3062de4"
M2_REVISION = "2f1c6d8e4a90"
M3_REVISION = "b8e6c4f2a137"
M4_REVISION = "c4a9e7d2f5b1"
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
        result = self.run_alembic("upgrade", M1_REVISION, "--sql")

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

    def test_upgrade_renders_complete_m2_postgresql_ddl_without_connecting(self) -> None:
        result = self.run_alembic("upgrade", M2_REVISION, "--sql")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CREATE TABLE range_executions", result.stdout)
        self.assertIn("CREATE TABLE durable_wakeups", result.stdout)
        self.assertIn("CREATE UNIQUE INDEX uq_range_execution_one_active", result.stdout)
        self.assertIn(
            "ck_processing_ranges_processing_range_watermark_start_floor",
            result.stdout,
        )
        self.assertIn(
            "ck_processing_ranges_processing_range_watermark_fixed_ceiling",
            result.stdout,
        )
        self.assertNotIn("sqlite", result.stdout.lower())

    def test_upgrade_renders_complete_m3_postgresql_ddl_without_connecting(self) -> None:
        result = self.run_alembic("upgrade", M3_REVISION, "--sql")

        self.assertEqual(result.returncode, 0, result.stderr)
        for table_name in ("message_parts", "image_assets", "video_assets", "video_frames"):
            self.assertIn(f"CREATE TABLE {table_name}", result.stdout)
        self.assertIn("ADD COLUMN last_seen_message_id BIGINT", result.stdout)
        self.assertIn("ADD COLUMN source_changed_after_processing BOOLEAN", result.stdout)
        self.assertIn("ADD COLUMN source_deleted BOOLEAN", result.stdout)
        self.assertIn("CREATE UNIQUE INDEX uq_messages_source_regular", result.stdout)
        self.assertIn("CREATE UNIQUE INDEX uq_messages_source_grouped", result.stdout)
        self.assertIn("CREATE INDEX ix_image_assets_archive_due", result.stdout)
        self.assertIn("CREATE INDEX ix_video_assets_archive_due", result.stdout)
        self.assertNotIn("sqlite", result.stdout.lower())

    def test_upgrade_renders_complete_m4_postgresql_ddl_without_connecting(self) -> None:
        result = self.run_alembic("upgrade", M4_REVISION, "--sql")

        self.assertEqual(result.returncode, 0, result.stderr)
        for table_name in (
            "label_definitions",
            "label_definition_versions",
            "label_sets",
            "label_set_versions",
            "label_bindings",
            "prompt_templates",
            "prompt_template_versions",
            "inference_profiles",
            "inference_profile_versions",
            "analysis_stage_templates",
            "analysis_stage_template_versions",
            "analysis_pipelines",
            "analysis_pipeline_versions",
            "pipeline_stage_nodes",
            "analysis_runs",
            "input_manifests",
            "inference_calls",
            "stage_runs",
            "model_label_assignments",
        ):
            self.assertIn(f"CREATE TABLE {table_name}", result.stdout)
        for column_name in (
            "blocked_from_analysis",
            "blocked_at",
            "blocked_by_stage_run_id",
            "blocked_by_label_assignment_id",
        ):
            self.assertIn(f"ALTER TABLE messages ADD COLUMN {column_name}", result.stdout)
        self.assertIn("ck_messages_message_analysis_block_state", result.stdout)
        self.assertIn("fk_messages_blocked_stage_run", result.stdout)
        self.assertIn("fk_messages_blocked_assignment", result.stdout)
        self.assertIn("CREATE UNIQUE INDEX uq_label_definition_one_published", result.stdout)
        self.assertIn("CREATE UNIQUE INDEX uq_analysis_stage_one_published", result.stdout)
        self.assertIn("CREATE UNIQUE INDEX uq_stage_run_direct_target", result.stdout)
        self.assertIn("CREATE INDEX ix_stage_runs_status_lease", result.stdout)
        self.assertIn("CREATE INDEX ix_stage_runs_retry_due", result.stdout)
        self.assertIn("CREATE INDEX ix_stage_runs_semantic_cache", result.stdout)
        self.assertIn("tgcurator_enforce_analysis_version_immutability", result.stdout)
        self.assertIn("published analysis version content is immutable", result.stdout)
        self.assertIn("trg_label_bindings_draft_only", result.stdout)
        self.assertIn("label bindings may mutate only for draft label-set versions", result.stdout)
        self.assertIn("trg_pipeline_stage_nodes_draft_only", result.stdout)
        self.assertIn("pipeline nodes may mutate only for draft pipeline versions", result.stdout)
        self.assertIn("trg_input_manifests_immutable", result.stdout)
        self.assertIn("input manifests are immutable", result.stdout)
        self.assertIn("TIMESTAMP WITH TIME ZONE", result.stdout)
        self.assertNotIn("sqlite", result.stdout.lower())

    def test_m4_downgrade_restores_the_m3_schema(self) -> None:
        result = self.run_alembic(
            "downgrade",
            f"{M4_REVISION}:{M3_REVISION}",
            "--sql",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        assignment_fk = result.stdout.index("DROP CONSTRAINT fk_messages_blocked_assignment")
        stage_fk = result.stdout.index("DROP CONSTRAINT fk_messages_blocked_stage_run")
        first_table_drop = result.stdout.index("DROP TABLE model_label_assignments")
        self.assertLess(assignment_fk, first_table_drop)
        self.assertLess(stage_fk, first_table_drop)
        for column_name in (
            "blocked_by_label_assignment_id",
            "blocked_by_stage_run_id",
            "blocked_at",
            "blocked_from_analysis",
        ):
            self.assertIn(f"DROP COLUMN {column_name}", result.stdout)
        for trigger_name in (
            "trg_input_manifests_immutable",
            "trg_pipeline_stage_nodes_draft_only",
            "trg_label_bindings_draft_only",
            "trg_analysis_pipeline_version_immutable",
            "trg_analysis_stage_version_immutable",
            "trg_inference_profile_version_immutable",
            "trg_prompt_template_version_immutable",
            "trg_label_set_version_immutable",
            "trg_label_definition_version_immutable",
        ):
            self.assertIn(f"DROP TRIGGER {trigger_name}", result.stdout)
        for function_name in (
            "tgcurator_reject_input_manifest_mutation",
            "tgcurator_require_draft_analysis_pipeline_version",
            "tgcurator_require_draft_label_set_version",
            "tgcurator_enforce_analysis_version_immutability",
        ):
            self.assertIn(f"DROP FUNCTION {function_name}()", result.stdout)
        for table_name in (
            "model_label_assignments",
            "stage_runs",
            "inference_calls",
            "analysis_runs",
            "pipeline_stage_nodes",
            "input_manifests",
            "label_bindings",
            "analysis_stage_template_versions",
            "prompt_template_versions",
            "label_set_versions",
            "label_definition_versions",
            "inference_profile_versions",
            "analysis_pipeline_versions",
            "prompt_templates",
            "label_sets",
            "label_definitions",
            "inference_profiles",
            "analysis_stage_templates",
            "analysis_pipelines",
        ):
            self.assertIn(f"DROP TABLE {table_name}", result.stdout)

    def test_m3_downgrade_restores_the_m2_schema(self) -> None:
        result = self.run_alembic(
            "downgrade",
            f"{M3_REVISION}:{M2_REVISION}",
            "--sql",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        for table_name in ("video_frames", "video_assets", "image_assets", "message_parts"):
            self.assertIn(f"DROP TABLE {table_name}", result.stdout)
        self.assertIn("DROP COLUMN source_changed_after_processing", result.stdout)
        self.assertIn("DROP COLUMN source_deleted", result.stdout)
        self.assertIn("DROP COLUMN last_seen_message_id", result.stdout)
        self.assertIn("ADD CONSTRAINT uq_message_source_primary UNIQUE", result.stdout)

    def test_m2_downgrade_renders_execution_and_wakeup_cleanup(self) -> None:
        result = self.run_alembic(
            "downgrade",
            f"{M2_REVISION}:{M1_REVISION}",
            "--sql",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DROP TABLE durable_wakeups", result.stdout)
        self.assertIn("DROP TABLE range_executions", result.stdout)
        self.assertIn(
            "DROP CONSTRAINT ck_processing_ranges_processing_range_watermark_fixed_ceiling",
            result.stdout,
        )
        self.assertIn(
            "DROP CONSTRAINT ck_processing_ranges_processing_range_watermark_start_floor",
            result.stdout,
        )

    def test_m1_downgrade_renders_trigger_constraint_and_table_cleanup(self) -> None:
        result = self.run_alembic("downgrade", f"{M1_REVISION}:base", "--sql")

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
