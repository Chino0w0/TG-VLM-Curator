from __future__ import annotations

import unittest

from sqlalchemy import BigInteger, DateTime
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, dialect
from sqlalchemy.schema import CreateTable

from tgcurator.infrastructure.database import models  # noqa: F401
from tgcurator.infrastructure.database.base import Base

M1_TABLES = {
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
}
M2_TABLES = {"range_executions", "durable_wakeups"}
M3_TABLES = {"message_parts", "image_assets", "video_assets", "video_frames"}
M4_TABLES = {
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
}


class DatabaseSchemaTests(unittest.TestCase):
    def test_schema_contains_exactly_the_documented_m1_through_m4_tables(self) -> None:
        self.assertEqual(
            set(Base.metadata.tables),
            M1_TABLES | M2_TABLES | M3_TABLES | M4_TABLES,
        )

    def test_processing_range_has_boundary_and_watermark_constraints(self) -> None:
        constraints = {
            str(constraint.name)
            for constraint in Base.metadata.tables["processing_ranges"].constraints
        }
        expected = {
            "ck_processing_ranges_processing_range_end_mode",
            "ck_processing_ranges_processing_range_status",
            "ck_processing_ranges_processing_range_boundary_fields",
            "ck_processing_ranges_processing_range_fixed_order",
            "ck_processing_ranges_processing_range_start_message_positive",
            "ck_processing_ranges_processing_range_end_message_positive",
            "ck_processing_ranges_processing_range_watermark_nonnegative",
            "ck_processing_ranges_processing_range_active_high_positive",
            "ck_processing_ranges_processing_range_watermark_high_ceiling",
            "ck_processing_ranges_processing_range_watermark_start_floor",
            "ck_processing_ranges_processing_range_watermark_fixed_ceiling",
        }
        self.assertTrue(expected.issubset(constraints))

    def test_range_execution_has_durable_state_constraints_and_indexes(self) -> None:
        table = Base.metadata.tables["range_executions"]
        constraints = {str(constraint.name) for constraint in table.constraints}
        expected_constraints = {
            "ck_range_executions_range_execution_status",
            "ck_range_executions_range_execution_bounds",
            "ck_range_executions_range_execution_watermark_bounds",
            "ck_range_executions_range_execution_attempts",
            "ck_range_executions_range_execution_completion",
            "ck_range_executions_range_execution_lease",
            "ck_range_executions_range_execution_retry_schedule",
            "ck_range_executions_range_execution_failure_metadata",
            "uq_range_execution_bounds",
        }
        self.assertTrue(expected_constraints.issubset(constraints))
        indexes = {index.name: index for index in table.indexes}
        self.assertIn("ix_range_executions_status_lease", indexes)
        self.assertIn("ix_range_executions_retry_due", indexes)
        one_active = indexes["uq_range_execution_one_active"]
        self.assertTrue(one_active.unique)
        self.assertIsNotNone(one_active.dialect_options["postgresql"]["where"])
        self.assertIsInstance(table.c.from_message_id_exclusive.type, BigInteger)
        self.assertIsInstance(table.c.to_message_id_inclusive.type, BigInteger)
        self.assertIsInstance(table.c.watermark_message_id.type, BigInteger)

    def test_durable_wakeup_has_lease_constraints_and_unique_signal(self) -> None:
        table = Base.metadata.tables["durable_wakeups"]
        constraints = {str(constraint.name) for constraint in table.constraints}
        self.assertTrue(
            {
                "ck_durable_wakeups_durable_wakeup_status",
                "ck_durable_wakeups_durable_wakeup_attempts",
                "ck_durable_wakeups_durable_wakeup_lease",
                "uq_durable_wakeup_queue_entity",
            }.issubset(constraints)
        )
        self.assertIn("ix_durable_wakeups_due", {index.name for index in table.indexes})

    def test_admin_and_published_configuration_invariants_are_in_metadata(self) -> None:
        admin_indexes = {index.name: index for index in Base.metadata.tables["admin_users"].indexes}
        active_admin_index = admin_indexes["uq_admin_users_single_active"]
        self.assertTrue(active_admin_index.unique)
        self.assertIsNotNone(active_admin_index.dialect_options["postgresql"]["where"])

        version_table = Base.metadata.tables["source_channel_profile_versions"]
        version_constraints = {str(constraint.name) for constraint in version_table.constraints}
        self.assertIn("ck_source_channel_profile_versions_state_valid", version_constraints)
        self.assertIn(
            "ck_source_channel_profile_versions_published_at_state",
            version_constraints,
        )
        self.assertIn("ck_source_channel_profile_versions_hash_format", version_constraints)
        version_indexes = {index.name: index for index in version_table.indexes}
        published_index = version_indexes["uq_source_profile_one_published"]
        self.assertTrue(published_index.unique)
        self.assertIsNotNone(published_index.dialect_options["postgresql"]["where"])

    def test_active_profile_foreign_key_is_deferred_until_versions_exist(self) -> None:
        source_table = Base.metadata.tables["source_channels"]
        active_profile_fk = next(
            constraint
            for constraint in source_table.foreign_key_constraints
            if constraint.name == "fk_source_channels_active_profile_version"
        )
        self.assertTrue(active_profile_fk.use_alter)
        self.assertEqual(active_profile_fk.referred_table.name, "source_channel_profile_versions")

    def test_postgresql_native_types_are_used_for_business_payloads(self) -> None:
        message_table = Base.metadata.tables["messages"]
        self.assertIsInstance(message_table.c.telegram_message_ids.type, ARRAY)
        self.assertIsInstance(message_table.c.telegram_metadata.type, JSONB)
        version_table = Base.metadata.tables["source_channel_profile_versions"]
        self.assertIsInstance(version_table.c.content.type, JSONB)
        audit_table = Base.metadata.tables["audit_events"]
        self.assertIsInstance(audit_table.c.payload.type, JSONB)
        self.assertIsInstance(
            Base.metadata.tables["prompt_template_versions"].c.declared_variables.type,
            ARRAY,
        )
        self.assertIsInstance(
            Base.metadata.tables["analysis_stage_template_versions"].c.input_policy.type,
            JSONB,
        )
        self.assertIsInstance(
            Base.metadata.tables["pipeline_stage_nodes"].c.depends_on.type,
            ARRAY,
        )
        self.assertIsInstance(
            Base.metadata.tables["inference_calls"].c.structured_output_schema.type,
            JSONB,
        )

    def test_m4_version_lifecycle_constraints_and_published_indexes(self) -> None:
        version_tables = {
            "label_definition_versions": (
                "uq_label_definition_one_published",
                "uq_label_definition_published_key",
            ),
            "label_set_versions": ("uq_label_set_one_published",),
            "prompt_template_versions": ("uq_prompt_template_one_published",),
            "inference_profile_versions": ("uq_inference_profile_one_published",),
            "analysis_stage_template_versions": ("uq_analysis_stage_one_published",),
            "analysis_pipeline_versions": ("uq_analysis_pipeline_one_published",),
        }
        for table_name, published_index_names in version_tables.items():
            table = Base.metadata.tables[table_name]
            constraints = {str(constraint.name) for constraint in table.constraints}
            self.assertTrue(
                {
                    f"ck_{table_name}_version_positive",
                    f"ck_{table_name}_version_state",
                    f"ck_{table_name}_version_lifecycle",
                }.issubset(constraints),
                table_name,
            )
            indexes = {index.name: index for index in table.indexes}
            for index_name in published_index_names:
                index = indexes[index_name]
                self.assertTrue(index.unique, index_name)
                self.assertIsNotNone(index.dialect_options["postgresql"]["where"], index_name)

    def test_analysis_run_has_lifecycle_failure_constraints_and_indexes(self) -> None:
        table = Base.metadata.tables["analysis_runs"]
        constraints = {str(constraint.name) for constraint in table.constraints}
        self.assertTrue(
            {
                "ck_analysis_runs_mode",
                "ck_analysis_runs_status",
                "ck_analysis_runs_facts_object",
                "ck_analysis_runs_lifecycle",
                "ck_analysis_runs_failure_metadata",
            }.issubset(constraints)
        )
        self.assertTrue(
            {"ix_analysis_runs_message_created", "ix_analysis_runs_status"}.issubset(
                {index.name for index in table.indexes}
            )
        )

    def test_stage_run_has_lease_retry_cache_constraints_and_indexes(self) -> None:
        table = Base.metadata.tables["stage_runs"]
        constraints = {str(constraint.name) for constraint in table.constraints}
        self.assertTrue(
            {
                "ck_stage_runs_target_scope",
                "ck_stage_runs_target_kind",
                "ck_stage_runs_target_identity",
                "ck_stage_runs_status",
                "ck_stage_runs_attempts",
                "ck_stage_runs_lease",
                "ck_stage_runs_retry_schedule",
                "ck_stage_runs_failure_metadata",
                "ck_stage_runs_terminal_timestamp",
                "ck_stage_runs_success_result",
                "ck_stage_runs_result_origin",
                "ck_stage_runs_cache_source",
                "ck_stage_runs_cache_policy",
                "ck_stage_runs_cache_key_format",
                "ck_stage_runs_result_object",
                "uq_stage_run_business_target",
            }.issubset(constraints)
        )
        indexes = {index.name: index for index in table.indexes}
        self.assertTrue(
            {
                "ix_stage_runs_status_lease",
                "ix_stage_runs_retry_due",
                "ix_stage_runs_analysis_node",
                "uq_stage_run_direct_target",
                "ix_stage_runs_semantic_cache",
            }.issubset(indexes)
        )
        direct_target = indexes["uq_stage_run_direct_target"]
        self.assertTrue(direct_target.unique)
        self.assertIsNotNone(direct_target.dialect_options["postgresql"]["where"])
        semantic_cache = indexes["ix_stage_runs_semantic_cache"]
        self.assertIsNotNone(semantic_cache.dialect_options["postgresql"]["where"])

    def test_input_manifest_and_inference_call_audit_constraints(self) -> None:
        manifest = Base.metadata.tables["input_manifests"]
        manifest_constraints = {str(constraint.name) for constraint in manifest.constraints}
        self.assertTrue(
            {
                "ck_input_manifests_version_positive",
                "ck_input_manifests_content_object",
                "ck_input_manifests_hash_format",
            }.issubset(manifest_constraints)
        )
        call = Base.metadata.tables["inference_calls"]
        call_constraints = {str(constraint.name) for constraint in call.constraints}
        self.assertTrue(
            {
                "ck_inference_calls_status",
                "ck_inference_calls_attempt_positive",
                "ck_inference_calls_lifecycle",
                "ck_inference_calls_schema_hash_format",
                "ck_inference_calls_request_object",
                "ck_inference_calls_schema_object",
                "ck_inference_calls_response_object",
                "ck_inference_calls_usage_object",
            }.issubset(call_constraints)
        )

    def test_message_negative_gate_state_and_circular_foreign_keys_are_explicit(self) -> None:
        table = Base.metadata.tables["messages"]
        for column_name in (
            "blocked_from_analysis",
            "blocked_at",
            "blocked_by_stage_run_id",
            "blocked_by_label_assignment_id",
        ):
            self.assertIn(column_name, table.c)
        constraints = {str(constraint.name) for constraint in table.constraints}
        self.assertIn("ck_messages_message_analysis_block_state", constraints)
        foreign_keys = {constraint.name: constraint for constraint in table.foreign_key_constraints}
        for name, referred_table in (
            ("fk_messages_blocked_stage_run", "stage_runs"),
            ("fk_messages_blocked_assignment", "model_label_assignments"),
        ):
            constraint = foreign_keys[name]
            self.assertTrue(constraint.use_alter)
            self.assertEqual(constraint.referred_table.name, referred_table)

    def test_all_m4_timestamps_and_message_block_time_are_timezone_aware(self) -> None:
        for table_name in M4_TABLES:
            table = Base.metadata.tables[table_name]
            timestamp_columns = [column for column in table.c if isinstance(column.type, DateTime)]
            self.assertTrue(timestamp_columns, table_name)
            for column in timestamp_columns:
                self.assertTrue(column.type.timezone, f"{table_name}.{column.name}")
        blocked_at = Base.metadata.tables["messages"].c.blocked_at.type
        self.assertIsInstance(blocked_at, DateTime)
        self.assertTrue(blocked_at.timezone)

    def test_business_timestamps_are_timezone_aware(self) -> None:
        timestamp_columns = {
            "telegram_identities": ("last_connected_at", "created_at", "updated_at"),
            "source_channels": (
                "last_activity_at",
                "created_at",
                "updated_at",
            ),
            "source_channel_profile_versions": ("published_at", "created_at"),
            "processing_ranges": ("start_at", "end_at", "created_at", "updated_at"),
            "range_executions": (
                "next_retry_at",
                "lease_expires_at",
                "last_failure_at",
                "started_at",
                "completed_at",
                "created_at",
                "updated_at",
            ),
            "durable_wakeups": (
                "next_attempt_at",
                "lease_expires_at",
                "last_dispatched_at",
                "completed_at",
                "created_at",
                "updated_at",
            ),
            "messages": (
                "published_at",
                "edited_at",
                "source_deleted_at",
                "created_at",
                "updated_at",
            ),
            "message_parts": (
                "published_at",
                "edited_at",
                "created_at",
                "updated_at",
            ),
            "image_assets": (
                "archive_next_retry_at",
                "archive_lease_expires_at",
                "archive_last_failure_at",
                "archive_ready_at",
                "archive_deleted_at",
                "created_at",
                "updated_at",
            ),
            "video_assets": (
                "archive_next_retry_at",
                "archive_lease_expires_at",
                "archive_last_failure_at",
                "archive_ready_at",
                "archive_deleted_at",
                "created_at",
                "updated_at",
            ),
            "video_frames": ("created_at", "updated_at"),
        }
        for table_name, column_names in timestamp_columns.items():
            table = Base.metadata.tables[table_name]
            for column_name in column_names:
                column_type = table.c[column_name].type
                self.assertIsInstance(column_type, DateTime)
                self.assertTrue(column_type.timezone, f"{table_name}.{column_name}")

    def test_postgresql_ddl_compiles_with_valid_identifier_lengths(self) -> None:
        postgresql = dialect()
        for table in Base.metadata.sorted_tables:
            ddl = str(CreateTable(table).compile(dialect=postgresql))
            self.assertIn(f"CREATE TABLE {table.name}", ddl)
            for constraint in table.constraints:
                if constraint.name is not None:
                    self.assertLessEqual(len(str(constraint.name)), 63)
            for index in table.indexes:
                if index.name is not None:
                    self.assertLessEqual(len(str(index.name)), 63)


if __name__ == "__main__":
    unittest.main()
