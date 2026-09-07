import unittest

from sqlalchemy import DateTime

from tgcurator.infrastructure.database import models  # noqa: F401
from tgcurator.infrastructure.database.base import Base


class DatabaseSchemaTests(unittest.TestCase):
    def test_schema_contains_required_m1_m2_and_m3_tables(self) -> None:
        expected = {
            "admin_users",
            "encrypted_secrets",
            "telegram_identities",
            "source_channels",
            "destination_channels",
            "source_channel_profiles",
            "source_channel_profile_versions",
            "processing_ranges",
            "range_executions",
            "durable_wakeups",
            "messages",
            "audit_events",
        }
        self.assertTrue(expected.issubset(Base.metadata.tables))

    def test_processing_range_has_database_constraints(self) -> None:
        constraints = {
            constraint.name for constraint in Base.metadata.tables["processing_ranges"].constraints
        }
        self.assertIn("ck_processing_range_boundary_fields", constraints)
        self.assertIn("ck_processing_range_fixed_order", constraints)
        self.assertIn("ck_processing_range_quiet_positive", constraints)
        self.assertIn("ck_processing_range_watermark_floor", constraints)
        self.assertIn("ck_processing_range_watermark_fixed_ceiling", constraints)

    def test_range_execution_and_wakeup_constraints_are_in_metadata(self) -> None:
        execution_constraints = {
            constraint.name for constraint in Base.metadata.tables["range_executions"].constraints
        }
        self.assertIn("uq_range_execution_bounds", execution_constraints)
        self.assertIn("ck_range_execution_watermark_bounds", execution_constraints)
        self.assertIn("ck_range_execution_lease", execution_constraints)
        execution_indexes = {
            index.name for index in Base.metadata.tables["range_executions"].indexes
        }
        self.assertIn("uq_range_execution_one_active", execution_indexes)

        wakeup_constraints = {
            constraint.name for constraint in Base.metadata.tables["durable_wakeups"].constraints
        }
        self.assertIn("uq_durable_wakeup_queue_entity", wakeup_constraints)
        self.assertIn("ck_durable_wakeup_lease", wakeup_constraints)
        wakeup_indexes = {index.name for index in Base.metadata.tables["durable_wakeups"].indexes}
        self.assertIn("ix_durable_wakeups_due", wakeup_indexes)

    def test_normalized_telegram_message_membership_constraints_are_in_metadata(self) -> None:
        message_constraints = {
            constraint.name for constraint in Base.metadata.tables["messages"].constraints
        }
        self.assertIn("ck_message_anchor_positive", message_constraints)
        self.assertIn("ck_message_group_positive", message_constraints)
        self.assertIn("ck_message_media_count_nonnegative", message_constraints)
        message_indexes = {index.name: index for index in Base.metadata.tables["messages"].indexes}
        self.assertTrue(message_indexes["uq_messages_source_group"].unique)
        self.assertIsNotNone(
            message_indexes["uq_messages_source_group"].dialect_options["postgresql"]["where"]
        )

        part_constraints = {
            constraint.name for constraint in Base.metadata.tables["message_parts"].constraints
        }
        self.assertIn("uq_message_part_source_telegram", part_constraints)
        self.assertIn("ck_message_part_telegram_positive", part_constraints)
        part_indexes = {index.name for index in Base.metadata.tables["message_parts"].indexes}
        self.assertIn("ix_message_parts_message", part_indexes)

    def test_admin_and_published_configuration_invariants_are_in_metadata(self) -> None:
        admin_indexes = {index.name: index for index in Base.metadata.tables["admin_users"].indexes}
        active_admin_index = admin_indexes["uq_admin_users_single_active"]
        self.assertTrue(active_admin_index.unique)
        self.assertIsNotNone(active_admin_index.dialect_options["postgresql"]["where"])

        profile_constraints = {
            constraint.name
            for constraint in Base.metadata.tables["source_channel_profile_versions"].constraints
        }
        self.assertIn("ck_source_profile_version_published_at", profile_constraints)

    def test_business_timestamps_are_timezone_aware(self) -> None:
        timestamp_columns = {
            "telegram_identities": ("last_connected_at",),
            "source_channel_profile_versions": ("published_at",),
            "processing_ranges": ("start_at", "fixed_end_at", "processing_watermark_at"),
            "range_executions": (
                "from_at",
                "to_at",
                "watermark_at",
                "lease_expires_at",
                "completed_at",
            ),
            "durable_wakeups": ("next_attempt_at", "lease_expires_at", "completed_at"),
            "messages": ("sent_at", "source_deleted_at"),
        }
        for table_name, column_names in timestamp_columns.items():
            table = Base.metadata.tables[table_name]
            for column_name in column_names:
                column_type = table.c[column_name].type
                self.assertIsInstance(column_type, DateTime)
                self.assertTrue(column_type.timezone, f"{table_name}.{column_name}")


if __name__ == "__main__":
    unittest.main()
