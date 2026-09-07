from __future__ import annotations

import unittest

from sqlalchemy import DateTime
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


class DatabaseSchemaTests(unittest.TestCase):
    def test_schema_contains_exactly_the_documented_m1_tables(self) -> None:
        self.assertEqual(set(Base.metadata.tables), M1_TABLES)
        self.assertNotIn("range_executions", Base.metadata.tables)
        self.assertNotIn("durable_wakeups", Base.metadata.tables)

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
        }
        self.assertTrue(expected.issubset(constraints))

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

    def test_business_timestamps_are_timezone_aware(self) -> None:
        timestamp_columns = {
            "telegram_identities": ("last_connected_at", "created_at", "updated_at"),
            "source_channels": ("last_activity_at", "created_at", "updated_at"),
            "source_channel_profile_versions": ("published_at", "created_at"),
            "processing_ranges": ("start_at", "end_at", "created_at", "updated_at"),
            "messages": ("published_at", "edited_at", "created_at", "updated_at"),
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
