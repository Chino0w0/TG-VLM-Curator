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
M5_REVISION = "d7b3f9a1e6c2"
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

    def test_upgrade_renders_complete_m5_postgresql_ddl_without_connecting(self) -> None:
        result = self.run_alembic("upgrade", M5_REVISION, "--sql")

        self.assertEqual(result.returncode, 0, result.stderr)
        for table_name in (
            "manual_label_assignments",
            "message_review_events",
            "routing_policies",
            "routing_policy_versions",
            "routing_rules",
            "rendering_templates",
            "rendering_template_versions",
            "routing_actions",
            "routing_evaluations",
            "publication_intents",
        ):
            self.assertIn(f"CREATE TABLE {table_name}", result.stdout)
        self.assertIn(
            "ALTER TABLE messages ADD COLUMN review_status VARCHAR(24) "
            "DEFAULT 'unreviewed' NOT NULL",
            result.stdout,
        )
        for constraint_name in (
            "ck_messages_message_review_status",
            "ck_manual_label_assignments_manual_label_target_identity",
            "ck_manual_label_assignments_manual_label_payload",
            "ck_message_review_events_review_event_status_changed",
            "ck_routing_policy_versions_routing_version_lifecycle",
            "ck_routing_rules_routing_rule_condition_object",
            "ck_routing_actions_routing_action_rendering_template",
            "ck_rendering_template_versions_rendering_content_hash_format",
            "ck_routing_evaluations_routing_evaluation_hash_format",
            "ck_publication_intents_publication_intent_status",
            "ck_publication_intents_publication_intent_rendering_template",
            "uq_publication_intent_evaluation_action",
            "uq_publication_intents_business_idempotency_key",
        ):
            self.assertIn(constraint_name, result.stdout)
        for foreign_key_name in (
            "fk_routing_rules_policy_version",
            "fk_rendering_versions_template",
            "fk_routing_actions_rendering_version",
            "fk_routing_evaluations_policy_version",
            "fk_publication_intents_evaluation",
            "fk_publication_intents_policy_version",
            "fk_publication_intents_destination",
            "fk_publication_intents_rendering_version",
        ):
            self.assertIn(foreign_key_name, result.stdout)
        self.assertIn("facts_snapshot JSONB NOT NULL", result.stdout)
        self.assertIn("rule_outcomes JSONB NOT NULL", result.stdout)
        self.assertIn("condition JSONB NOT NULL", result.stdout)
        self.assertIn(
            "CREATE UNIQUE INDEX uq_routing_policy_one_published",
            result.stdout,
        )
        self.assertIn(
            "CREATE UNIQUE INDEX uq_rendering_template_one_published",
            result.stdout,
        )
        self.assertIn(
            "CREATE INDEX ix_publication_intents_pending ON publication_intents "
            "(status, created_at) WHERE status = 'pending'",
            result.stdout,
        )
        for function_name in (
            "tgcurator_reject_manual_label_assignment_mutation",
            "tgcurator_reject_message_review_event_mutation",
            "tgcurator_enforce_routing_version_immutability",
            "tgcurator_require_draft_routing_policy_version",
            "tgcurator_require_draft_routing_action_owner",
            "tgcurator_reject_routing_evaluation_mutation",
            "tgcurator_enforce_publication_intent_identity",
        ):
            self.assertIn(f"CREATE FUNCTION {function_name}()", result.stdout)
        for trigger_name in (
            "trg_manual_label_assignments_append_only",
            "trg_message_review_events_append_only",
            "trg_routing_policy_version_immutable",
            "trg_rendering_template_version_immutable",
            "trg_routing_rules_draft_only",
            "trg_routing_actions_draft_only",
            "trg_routing_evaluations_immutable",
            "trg_publication_intents_identity_immutable",
        ):
            self.assertIn(f"CREATE TRIGGER {trigger_name}", result.stdout)
        for invariant_message in (
            "manual label assignments are append-only",
            "message review events are append-only",
            "published routing configuration version content is immutable",
            "routing rules may mutate only for draft routing policy versions",
            "routing actions may mutate only for draft routing policy versions",
            "routing evaluations are immutable",
            "publication intents cannot be deleted",
            "publication intent identity is immutable",
        ):
            self.assertIn(invariant_message, result.stdout)
        self.assertIn(
            "to_jsonb(NEW) - ''state'' - ''archived_at'' - ''updated_at''",
            result.stdout,
        )
        self.assertIn(
            "to_jsonb(NEW) - ''status'' - ''updated_at''",
            result.stdout,
        )
        self.assertIn("TIMESTAMP WITH TIME ZONE", result.stdout)
        self.assertNotIn("sqlite", result.stdout.lower())

    def test_m5_downgrade_restores_the_m4_schema(self) -> None:
        result = self.run_alembic(
            "downgrade",
            f"{M5_REVISION}:{M4_REVISION}",
            "--sql",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        trigger_names = (
            "trg_publication_intents_identity_immutable",
            "trg_routing_evaluations_immutable",
            "trg_routing_actions_draft_only",
            "trg_routing_rules_draft_only",
            "trg_rendering_template_version_immutable",
            "trg_routing_policy_version_immutable",
            "trg_message_review_events_append_only",
            "trg_manual_label_assignments_append_only",
        )
        for trigger_name in trigger_names:
            self.assertIn(f"DROP TRIGGER {trigger_name}", result.stdout)
        function_names = (
            "tgcurator_enforce_publication_intent_identity",
            "tgcurator_reject_routing_evaluation_mutation",
            "tgcurator_require_draft_routing_action_owner",
            "tgcurator_require_draft_routing_policy_version",
            "tgcurator_enforce_routing_version_immutability",
            "tgcurator_reject_message_review_event_mutation",
            "tgcurator_reject_manual_label_assignment_mutation",
        )
        for function_name in function_names:
            self.assertIn(f"DROP FUNCTION {function_name}()", result.stdout)
        for index_name in (
            "ix_publication_intents_pending",
            "ix_publication_intents_message",
            "ix_routing_evaluations_message",
            "ix_routing_actions_rule",
            "ix_routing_rules_policy_order",
            "uq_rendering_template_one_published",
            "uq_routing_policy_one_published",
            "ix_review_events_message_created",
            "ix_manual_labels_target_label",
            "ix_manual_labels_message_created",
        ):
            self.assertIn(f"DROP INDEX {index_name}", result.stdout)
        table_names = (
            "publication_intents",
            "routing_evaluations",
            "routing_actions",
            "routing_rules",
            "rendering_template_versions",
            "routing_policy_versions",
            "rendering_templates",
            "routing_policies",
            "message_review_events",
            "manual_label_assignments",
        )
        for table_name in table_names:
            self.assertIn(f"DROP TABLE {table_name}", result.stdout)
        self.assertIn(
            "ALTER TABLE messages DROP CONSTRAINT ck_messages_message_review_status",
            result.stdout,
        )
        self.assertIn("ALTER TABLE messages DROP COLUMN review_status", result.stdout)

        first_table_drop = min(
            result.stdout.index(f"DROP TABLE {table_name}") for table_name in table_names
        )
        last_trigger_drop = max(
            result.stdout.index(f"DROP TRIGGER {trigger_name}") for trigger_name in trigger_names
        )
        last_function_drop = max(
            result.stdout.index(f"DROP FUNCTION {function_name}()")
            for function_name in function_names
        )
        self.assertLess(last_trigger_drop, first_table_drop)
        self.assertLess(last_function_drop, first_table_drop)

        ordered_tables = (
            "publication_intents",
            "routing_evaluations",
            "routing_actions",
            "routing_rules",
            "rendering_template_versions",
            "routing_policy_versions",
            "rendering_templates",
            "routing_policies",
        )
        table_positions = [
            result.stdout.index(f"DROP TABLE {table_name}") for table_name in ordered_tables
        ]
        self.assertEqual(table_positions, sorted(table_positions))
        self.assertLess(
            result.stdout.index("DROP TABLE manual_label_assignments"),
            result.stdout.index("DROP COLUMN review_status"),
        )

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
