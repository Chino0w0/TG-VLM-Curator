"""Add M6 publication execution and archive cleanup lifecycle.

Revision ID: e6d4a8b2c951
Revises: d7b3f9a1e6c2
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e6d4a8b2c951"
down_revision: str | Sequence[str] | None = "d7b3f9a1e6c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_LEGACY_RENDERED_HASH = "b77f40b58e5a500b7e66561203197c25a9ff3095f75edaa72e849e7790938aab"


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )


def _replace_publication_identity_function(*, m6: bool) -> None:
    mutable_fields = (
        "status",
        "updated_at",
    )
    if m6:
        mutable_fields += (
            "main_random_ids",
            "supplement_random_id",
            "completed_parts",
            "main_remote_message_ids",
            "in_flight_part",
            "in_flight_operation",
            "in_flight_started_at",
            "last_reconciled_at",
            "attempt_count",
            "next_retry_at",
            "lease_token",
            "lease_expires_at",
            "last_error_code",
            "last_error_type",
            "last_failure_at",
            "started_at",
            "completed_at",
        )
    new_expression = " ".join(f"- ''{field}''" for field in mutable_fields)
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION tgcurator_enforce_publication_intent_identity()
        RETURNS trigger
        LANGUAGE plpgsql
        AS 'BEGIN
            IF TG_OP = ''DELETE'' THEN
                RAISE EXCEPTION ''publication intents cannot be deleted'';
            END IF;
            IF (to_jsonb(NEW) {new_expression})
                IS DISTINCT FROM
               (to_jsonb(OLD) {new_expression})
            THEN
                RAISE EXCEPTION ''publication intent identity is immutable'';
            END IF;
            RETURN NEW;
        END;'
        """
    )


def upgrade() -> None:
    _upgrade_routing_actions()
    _upgrade_publication()
    _upgrade_cleanup()


def _upgrade_routing_actions() -> None:
    # Frozen publication policy is selected from routing actions, not only intents.
    # Server defaults also give existing M5 (including published) policies valid values.
    op.add_column(
        "routing_actions",
        sa.Column(
            "protected_content_fallback",
            sa.String(length=24),
            server_default=sa.text("'fail'"),
            nullable=False,
        ),
    )
    op.add_column(
        "routing_actions",
        sa.Column(
            "supplement_as_reply",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_routing_actions_routing_action_protected_fallback"),
        "routing_actions",
        "protected_content_fallback IN ('fail', 'metadata_only', 'skip')",
    )


def _upgrade_publication() -> None:
    op.add_column(
        "publication_intents",
        sa.Column(
            "protected_content_fallback",
            sa.String(length=24),
            server_default=sa.text("'fail'"),
            nullable=False,
        ),
    )
    op.add_column(
        "publication_intents",
        sa.Column(
            "supplement_as_reply",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
    )
    op.add_column("publication_intents", sa.Column("rendered_text", sa.Text(), nullable=True))
    op.add_column(
        "publication_intents",
        sa.Column(
            "rendered_context",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "publication_intents",
        sa.Column("rendered_snapshot_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "publication_intents",
        sa.Column(
            "main_random_ids",
            postgresql.ARRAY(sa.BigInteger()),
            server_default=sa.text("'{}'::bigint[]"),
            nullable=False,
        ),
    )
    op.add_column(
        "publication_intents",
        sa.Column("supplement_random_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "publication_intents",
        sa.Column(
            "completed_parts",
            postgresql.ARRAY(sa.String(length=16)),
            server_default=sa.text("'{}'::varchar[]"),
            nullable=False,
        ),
    )
    op.add_column(
        "publication_intents",
        sa.Column(
            "main_remote_message_ids",
            postgresql.ARRAY(sa.BigInteger()),
            server_default=sa.text("'{}'::bigint[]"),
            nullable=False,
        ),
    )
    op.add_column(
        "publication_intents", sa.Column("in_flight_part", sa.String(length=16), nullable=True)
    )
    op.add_column(
        "publication_intents",
        sa.Column("in_flight_operation", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "publication_intents",
        sa.Column("in_flight_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "publication_intents",
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "publication_intents",
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "publication_intents",
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("5"), nullable=False),
    )
    op.add_column(
        "publication_intents",
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("publication_intents", sa.Column("lease_token", sa.Uuid(), nullable=True))
    op.add_column(
        "publication_intents",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "publication_intents",
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "publication_intents",
        sa.Column("last_error_type", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "publication_intents",
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "publication_intents", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "publication_intents", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True)
    )

    # M5 could persist pending intents but had no publisher. Preserve them as valid, explicitly
    # empty frozen renderings so the new constraints can be installed without inventing Telegram
    # results or random identifiers. Administrators may reroute them to obtain a richer snapshot.
    # M5's whole-row identity trigger also sees the new rendering columns.
    # Alembic runs this migration in one PostgreSQL DDL transaction: exclude
    # concurrent writers, suspend only this trigger for the backfill, then restore
    # it immediately. Any failure rolls back both the data and trigger state; do
    # not use autocommit or a session-wide replication-role bypass here.
    op.execute("LOCK TABLE publication_intents IN ACCESS EXCLUSIVE MODE")
    op.execute(
        "ALTER TABLE publication_intents DISABLE TRIGGER trg_publication_intents_identity_immutable"
    )
    op.execute(
        "UPDATE publication_intents "
        "SET rendered_text = '', rendered_snapshot_hash = '"
        + _LEGACY_RENDERED_HASH
        + "' WHERE publication_mode <> 'forward_only' AND rendered_text IS NULL"
    )
    op.execute(
        "ALTER TABLE publication_intents ENABLE TRIGGER trg_publication_intents_identity_immutable"
    )

    for name, condition in (
        (
            "publication_intent_protected_fallback",
            "protected_content_fallback IN ('fail', 'metadata_only', 'skip')",
        ),
        (
            "publication_intent_attempts",
            "attempt_count >= 0 AND attempt_count <= max_attempts AND max_attempts > 0",
        ),
        (
            "publication_intent_lease",
            "(status = 'sending' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND attempt_count > 0) OR (status <> 'sending' AND lease_token IS NULL "
            "AND lease_expires_at IS NULL)",
        ),
        (
            "publication_intent_retry_schedule",
            "(status = 'retry_wait' AND next_retry_at IS NOT NULL) OR "
            "(status <> 'retry_wait' AND next_retry_at IS NULL)",
        ),
        (
            "publication_intent_failure_metadata",
            "status NOT IN ('retry_wait', 'failed') OR (last_error_code IS NOT NULL "
            "AND last_error_type IS NOT NULL AND last_failure_at IS NOT NULL)",
        ),
        (
            "publication_intent_in_flight",
            "(in_flight_part IS NULL AND in_flight_operation IS NULL "
            "AND in_flight_started_at IS NULL) OR (in_flight_part IN ('main', 'supplement') "
            "AND in_flight_operation IN ('native_forward', 'copy_with_caption', 'send_text') "
            "AND in_flight_started_at IS NOT NULL)",
        ),
        (
            "publication_intent_completed_parts",
            "completed_parts <@ ARRAY['main', 'supplement']::varchar[]",
        ),
        (
            "publication_intent_main_random_ids",
            "cardinality(main_random_ids) = 0 OR 0 < ALL(main_random_ids)",
        ),
        (
            "publication_intent_random_ids_claimed",
            "status = 'pending' OR cardinality(main_random_ids) > 0",
        ),
        (
            "publication_intent_supplement_random_id",
            "supplement_random_id IS NULL OR supplement_random_id > 0",
        ),
        (
            "publication_intent_remote_message_ids",
            "cardinality(main_remote_message_ids) = 0 OR 0 < ALL(main_remote_message_ids)",
        ),
        (
            "publication_intent_supplement_mode",
            "(publication_mode = 'native_forward_with_supplement' "
            "AND (status = 'pending' OR supplement_random_id IS NOT NULL)) OR "
            "(publication_mode <> 'native_forward_with_supplement' "
            "AND supplement_random_id IS NULL)",
        ),
        ("publication_intent_rendered_context", "jsonb_typeof(rendered_context) = 'object'"),
        (
            "publication_intent_rendered_hash",
            "rendered_snapshot_hash IS NULL OR (char_length(rendered_snapshot_hash) = 64 "
            "AND rendered_snapshot_hash !~ '[^0-9a-f]')",
        ),
        (
            "publication_intent_rendered_snapshot",
            "(publication_mode = 'forward_only' AND rendered_text IS NULL "
            "AND rendered_snapshot_hash IS NULL) OR "
            "(publication_mode <> 'forward_only' AND rendered_text IS NOT NULL "
            "AND rendered_snapshot_hash IS NOT NULL)",
        ),
        (
            "publication_intent_completion",
            "(status IN ('sent', 'failed', 'cancelled') AND completed_at IS NOT NULL) OR "
            "(status NOT IN ('sent', 'failed', 'cancelled') AND completed_at IS NULL)",
        ),
    ):
        op.create_check_constraint(
            op.f(f"ck_publication_intents_{name}"), "publication_intents", condition
        )

    op.drop_index("ix_publication_intents_pending", table_name="publication_intents")
    op.create_index(
        "ix_publication_intents_pending",
        "publication_intents",
        ["status", "created_at"],
        postgresql_where=sa.text("status IN ('pending', 'partial')"),
    )
    op.create_index(
        "ix_publication_intents_status_lease",
        "publication_intents",
        ["status", "lease_expires_at"],
    )
    op.create_index(
        "ix_publication_intents_retry_due",
        "publication_intents",
        ["status", "next_retry_at"],
    )

    op.create_table(
        "publish_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("publication_intent_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("retryable", sa.Boolean(), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_type", sa.String(length=128), nullable=True),
        sa.Column("reconciled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "attempt_number > 0",
            name=op.f("ck_publish_attempts_publish_attempt_number_positive"),
        ),
        sa.CheckConstraint(
            "status IN ('sending', 'sent', 'retry_wait', 'failed', 'cancelled')",
            name=op.f("ck_publish_attempts_publish_attempt_status"),
        ),
        sa.CheckConstraint(
            "status <> 'retry_wait' OR next_retry_at IS NOT NULL",
            name=op.f("ck_publish_attempts_publish_attempt_retry_schedule"),
        ),
        sa.ForeignKeyConstraint(
            ["publication_intent_id"],
            ["publication_intents.id"],
            name=op.f("fk_publish_attempts_publication_intent_id_publication_intents"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_publish_attempts")),
        sa.UniqueConstraint(
            "publication_intent_id",
            "attempt_number",
            name="uq_publish_attempt_number",
        ),
        sa.UniqueConstraint("lease_token", name="uq_publish_attempt_lease_token"),
    )
    op.create_index(
        "ix_publish_attempts_intent",
        "publish_attempts",
        ["publication_intent_id", "attempt_number"],
    )

    op.create_table(
        "publish_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("publication_intent_id", sa.Uuid(), nullable=False),
        sa.Column("publish_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("part", sa.String(length=16), nullable=False),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("remote_message_ids", postgresql.ARRAY(sa.BigInteger()), nullable=False),
        sa.Column("random_ids", postgresql.ARRAY(sa.BigInteger()), nullable=False),
        sa.Column("rendered_text", sa.Text(), nullable=True),
        sa.Column("rendered_snapshot_hash", sa.String(length=64), nullable=True),
        sa.Column("reconciled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        _created_at(),
        sa.CheckConstraint(
            "part IN ('main', 'supplement')",
            name=op.f("ck_publish_records_publish_record_part"),
        ),
        sa.CheckConstraint(
            "operation IN ('native_forward', 'copy_with_caption', 'send_text')",
            name=op.f("ck_publish_records_publish_record_operation"),
        ),
        sa.CheckConstraint(
            "cardinality(remote_message_ids) > 0 AND 0 < ALL(remote_message_ids)",
            name=op.f("ck_publish_records_publish_record_remote_ids"),
        ),
        sa.CheckConstraint(
            "cardinality(random_ids) > 0 AND 0 < ALL(random_ids)",
            name=op.f("ck_publish_records_publish_record_random_ids"),
        ),
        sa.ForeignKeyConstraint(
            ["publication_intent_id"],
            ["publication_intents.id"],
            name=op.f("fk_publish_records_publication_intent_id_publication_intents"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["publish_attempt_id"],
            ["publish_attempts.id"],
            name=op.f("fk_publish_records_publish_attempt_id_publish_attempts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_publish_records")),
        sa.UniqueConstraint("publication_intent_id", "part", name="uq_publish_record_intent_part"),
    )
    op.create_index(
        "ix_publish_records_intent",
        "publish_records",
        ["publication_intent_id", "sent_at"],
    )

    _replace_publication_identity_function(m6=True)


def _upgrade_cleanup() -> None:
    op.create_table(
        "archive_cleanup_dry_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor_admin_user_id", sa.Uuid(), nullable=False),
        sa.Column("filter_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("filter_hash", sa.String(length=64), nullable=False),
        sa.Column("estimated_asset_count", sa.BigInteger(), nullable=False),
        sa.Column("estimated_message_count", sa.BigInteger(), nullable=False),
        sa.Column("estimated_bytes", sa.BigInteger(), nullable=False),
        _created_at(),
        sa.CheckConstraint(
            "jsonb_typeof(filter_snapshot) = 'object'",
            name=op.f("ck_archive_cleanup_dry_runs_cleanup_dry_run_filter_object"),
        ),
        sa.CheckConstraint(
            "char_length(filter_hash) = 64 AND filter_hash !~ '[^0-9a-f]'",
            name=op.f("ck_archive_cleanup_dry_runs_cleanup_dry_run_hash_format"),
        ),
        sa.CheckConstraint(
            "estimated_asset_count >= 0 AND estimated_message_count >= 0 AND estimated_bytes >= 0",
            name=op.f("ck_archive_cleanup_dry_runs_cleanup_dry_run_estimates"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_admin_user_id"],
            ["admin_users.id"],
            name=op.f("fk_archive_cleanup_dry_runs_actor_admin_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_archive_cleanup_dry_runs")),
    )
    op.create_index(
        "ix_cleanup_dry_runs_actor_created",
        "archive_cleanup_dry_runs",
        ["actor_admin_user_id", "created_at"],
    )

    op.create_table(
        "archive_cleanup_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dry_run_id", sa.Uuid(), nullable=False),
        sa.Column("confirmed_by_admin_user_id", sa.Uuid(), nullable=False),
        sa.Column("filter_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("filter_hash", sa.String(length=64), nullable=False),
        sa.Column("estimated_asset_count", sa.BigInteger(), nullable=False),
        sa.Column("estimated_message_count", sa.BigInteger(), nullable=False),
        sa.Column("estimated_bytes", sa.BigInteger(), nullable=False),
        sa.Column("item_count", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("5"), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column("last_error_type", sa.String(length=128), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'retry_wait', 'completed', 'failed', 'cancelled')",
            name=op.f("ck_archive_cleanup_jobs_cleanup_job_status"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(filter_snapshot) = 'object'",
            name=op.f("ck_archive_cleanup_jobs_cleanup_job_filter_object"),
        ),
        sa.CheckConstraint(
            "char_length(filter_hash) = 64 AND filter_hash !~ '[^0-9a-f]'",
            name=op.f("ck_archive_cleanup_jobs_cleanup_job_hash_format"),
        ),
        sa.CheckConstraint(
            "estimated_asset_count >= 0 AND estimated_message_count >= 0 "
            "AND estimated_bytes >= 0 AND item_count >= 0",
            name=op.f("ck_archive_cleanup_jobs_cleanup_job_estimates_nonnegative"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND attempt_count <= max_attempts AND max_attempts > 0",
            name=op.f("ck_archive_cleanup_jobs_cleanup_job_attempts"),
        ),
        sa.CheckConstraint(
            "(status = 'running' AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL AND attempt_count > 0) OR "
            "(status <> 'running' AND lease_token IS NULL AND lease_expires_at IS NULL)",
            name=op.f("ck_archive_cleanup_jobs_cleanup_job_lease"),
        ),
        sa.CheckConstraint(
            "(status = 'retry_wait' AND next_retry_at IS NOT NULL) OR "
            "(status <> 'retry_wait' AND next_retry_at IS NULL)",
            name=op.f("ck_archive_cleanup_jobs_cleanup_job_retry_schedule"),
        ),
        sa.CheckConstraint(
            "status NOT IN ('retry_wait', 'failed') OR "
            "(last_error_code IS NOT NULL AND last_error_type IS NOT NULL "
            "AND last_failure_at IS NOT NULL)",
            name=op.f("ck_archive_cleanup_jobs_cleanup_job_failure_metadata"),
        ),
        sa.CheckConstraint(
            "(status IN ('completed', 'failed', 'cancelled') AND completed_at IS NOT NULL) OR "
            "(status NOT IN ('completed', 'failed', 'cancelled') AND completed_at IS NULL)",
            name=op.f("ck_archive_cleanup_jobs_cleanup_job_completion"),
        ),
        sa.ForeignKeyConstraint(
            ["dry_run_id"],
            ["archive_cleanup_dry_runs.id"],
            name=op.f("fk_archive_cleanup_jobs_dry_run_id_archive_cleanup_dry_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["confirmed_by_admin_user_id"],
            ["admin_users.id"],
            name=op.f("fk_archive_cleanup_jobs_confirmed_by_admin_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_archive_cleanup_jobs")),
        sa.UniqueConstraint("dry_run_id", name=op.f("uq_archive_cleanup_jobs_dry_run_id")),
    )
    op.create_index(
        "ix_cleanup_jobs_status_lease",
        "archive_cleanup_jobs",
        ["status", "lease_expires_at"],
    )
    op.create_index(
        "ix_cleanup_jobs_retry_due",
        "archive_cleanup_jobs",
        ["status", "next_retry_at"],
    )

    for table_name in ("image_assets", "video_assets"):
        op.add_column(
            table_name,
            sa.Column("cleanup_job_id", sa.Uuid(), nullable=True),
        )
        op.add_column(
            table_name,
            sa.Column("previous_archive_size", sa.BigInteger(), nullable=True),
        )
        op.create_foreign_key(
            op.f(f"fk_{table_name}_cleanup_job_id_archive_cleanup_jobs"),
            table_name,
            "archive_cleanup_jobs",
            ["cleanup_job_id"],
            ["id"],
            ondelete="RESTRICT",
        )

    op.drop_constraint(
        op.f("ck_image_assets_image_asset_archive_state"),
        "image_assets",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_image_assets_image_asset_archive_state"),
        "image_assets",
        "archive_state IN ('pending', 'processing', 'retry_wait', 'ready', "
        "'deleting', 'deleted', 'failed')",
    )
    op.create_check_constraint(
        op.f("ck_image_assets_image_asset_cleanup_audit"),
        "image_assets",
        "archive_state NOT IN ('deleting', 'deleted') OR "
        "(cleanup_job_id IS NOT NULL AND previous_archive_size IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_image_assets_image_asset_previous_size_positive"),
        "image_assets",
        "previous_archive_size IS NULL OR previous_archive_size > 0",
    )

    op.drop_constraint(
        op.f("ck_video_assets_video_asset_archive_state"),
        "video_assets",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_video_assets_video_asset_archive_state"),
        "video_assets",
        "archive_state IN ('pending', 'processing', 'retry_wait', 'ready', "
        "'deleting', 'deleted', 'failed')",
    )
    op.create_check_constraint(
        op.f("ck_video_assets_video_asset_cleanup_audit"),
        "video_assets",
        "archive_state NOT IN ('deleting', 'deleted') OR "
        "(cleanup_job_id IS NOT NULL AND previous_archive_size IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_video_assets_video_asset_previous_size_nonnegative"),
        "video_assets",
        "previous_archive_size IS NULL OR previous_archive_size >= 0",
    )

    op.add_column(
        "video_frames",
        sa.Column(
            "archive_state",
            sa.String(length=16),
            server_default=sa.text("'ready'"),
            nullable=False,
        ),
    )
    op.add_column(
        "video_frames",
        sa.Column("archive_deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("video_frames", sa.Column("cleanup_job_id", sa.Uuid(), nullable=True))
    op.add_column(
        "video_frames", sa.Column("previous_archive_size", sa.BigInteger(), nullable=True)
    )
    op.create_foreign_key(
        op.f("fk_video_frames_cleanup_job_id_archive_cleanup_jobs"),
        "video_frames",
        "archive_cleanup_jobs",
        ["cleanup_job_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_video_frames_video_frame_archive_state"),
        "video_frames",
        "archive_state IN ('ready', 'deleting', 'deleted')",
    )
    op.create_check_constraint(
        op.f("ck_video_frames_video_frame_deleted_timestamp"),
        "video_frames",
        "archive_state <> 'deleted' OR archive_deleted_at IS NOT NULL",
    )
    op.create_check_constraint(
        op.f("ck_video_frames_video_frame_cleanup_audit"),
        "video_frames",
        "archive_state = 'ready' OR "
        "(cleanup_job_id IS NOT NULL AND previous_archive_size IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_video_frames_video_frame_previous_size_positive"),
        "video_frames",
        "previous_archive_size IS NULL OR previous_archive_size > 0",
    )

    op.create_table(
        "archive_cleanup_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("cleanup_job_id", sa.Uuid(), nullable=False),
        sa.Column("asset_kind", sa.String(length=16), nullable=False),
        sa.Column("image_asset_id", sa.Uuid(), nullable=True),
        sa.Column("video_asset_id", sa.Uuid(), nullable=True),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("deletion_token", sa.Uuid(), nullable=True),
        sa.Column(
            "blob_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("previous_archive_size", sa.BigInteger(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("skipped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column("last_error_type", sa.String(length=128), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "asset_kind IN ('image', 'video')",
            name=op.f("ck_archive_cleanup_items_cleanup_item_asset_kind"),
        ),
        sa.CheckConstraint(
            "(asset_kind = 'image' AND image_asset_id IS NOT NULL "
            "AND video_asset_id IS NULL) OR "
            "(asset_kind = 'video' AND video_asset_id IS NOT NULL "
            "AND image_asset_id IS NULL)",
            name=op.f("ck_archive_cleanup_items_cleanup_item_asset_identity"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'deleting', 'deleted', 'skipped_in_use', 'failed')",
            name=op.f("ck_archive_cleanup_items_cleanup_item_status"),
        ),
        sa.CheckConstraint(
            "(status = 'deleting' AND deletion_token IS NOT NULL) OR "
            "(status <> 'deleting' AND deletion_token IS NULL)",
            name=op.f("ck_archive_cleanup_items_cleanup_item_deletion_token"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(blob_snapshot) = 'array'",
            name=op.f("ck_archive_cleanup_items_cleanup_item_blob_array"),
        ),
        sa.CheckConstraint(
            "previous_archive_size >= 0",
            name=op.f("ck_archive_cleanup_items_cleanup_item_previous_size_nonnegative"),
        ),
        sa.CheckConstraint(
            "(status = 'deleted' AND deleted_at IS NOT NULL) OR "
            "(status <> 'deleted' AND deleted_at IS NULL)",
            name=op.f("ck_archive_cleanup_items_cleanup_item_deleted_timestamp"),
        ),
        sa.CheckConstraint(
            "(status = 'skipped_in_use' AND skipped_at IS NOT NULL) OR "
            "(status <> 'skipped_in_use' AND skipped_at IS NULL)",
            name=op.f("ck_archive_cleanup_items_cleanup_item_skipped_timestamp"),
        ),
        sa.CheckConstraint(
            "status <> 'failed' OR (last_error_code IS NOT NULL "
            "AND last_error_type IS NOT NULL AND last_failure_at IS NOT NULL)",
            name=op.f("ck_archive_cleanup_items_cleanup_item_failure_metadata"),
        ),
        sa.ForeignKeyConstraint(
            ["cleanup_job_id"],
            ["archive_cleanup_jobs.id"],
            name=op.f("fk_archive_cleanup_items_cleanup_job_id_archive_cleanup_jobs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["image_asset_id"],
            ["image_assets.id"],
            name=op.f("fk_archive_cleanup_items_image_asset_id_image_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["video_asset_id"],
            ["video_assets.id"],
            name=op.f("fk_archive_cleanup_items_video_asset_id_video_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_archive_cleanup_items_message_id_messages"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_archive_cleanup_items")),
        sa.UniqueConstraint("cleanup_job_id", "image_asset_id", name="uq_cleanup_item_job_image"),
        sa.UniqueConstraint("cleanup_job_id", "video_asset_id", name="uq_cleanup_item_job_video"),
    )
    op.create_index(
        "ix_cleanup_items_job_status",
        "archive_cleanup_items",
        ["cleanup_job_id", "status", "created_at"],
    )


def downgrade() -> None:
    _downgrade_cleanup()
    _downgrade_publication()
    op.drop_constraint(
        op.f("ck_routing_actions_routing_action_protected_fallback"),
        "routing_actions",
        type_="check",
    )
    op.drop_column("routing_actions", "supplement_as_reply")
    op.drop_column("routing_actions", "protected_content_fallback")


def _downgrade_cleanup() -> None:
    op.drop_index("ix_cleanup_items_job_status", table_name="archive_cleanup_items")
    op.drop_table("archive_cleanup_items")

    for constraint_name in (
        "video_frame_previous_size_positive",
        "video_frame_cleanup_audit",
        "video_frame_deleted_timestamp",
        "video_frame_archive_state",
    ):
        op.drop_constraint(
            op.f(f"ck_video_frames_{constraint_name}"),
            "video_frames",
            type_="check",
        )
    op.drop_constraint(
        op.f("fk_video_frames_cleanup_job_id_archive_cleanup_jobs"),
        "video_frames",
        type_="foreignkey",
    )
    for column_name in (
        "previous_archive_size",
        "cleanup_job_id",
        "archive_deleted_at",
        "archive_state",
    ):
        op.drop_column("video_frames", column_name)

    for table_name, prefix, previous_constraint in (
        ("video_assets", "video_asset", "previous_size_nonnegative"),
        ("image_assets", "image_asset", "previous_size_positive"),
    ):
        op.drop_constraint(
            op.f(f"ck_{table_name}_{prefix}_{previous_constraint}"),
            table_name,
            type_="check",
        )
        op.drop_constraint(
            op.f(f"ck_{table_name}_{prefix}_cleanup_audit"),
            table_name,
            type_="check",
        )
        op.drop_constraint(
            op.f(f"fk_{table_name}_cleanup_job_id_archive_cleanup_jobs"),
            table_name,
            type_="foreignkey",
        )
        op.drop_constraint(
            op.f(f"ck_{table_name}_{prefix}_archive_state"),
            table_name,
            type_="check",
        )
        op.create_check_constraint(
            op.f(f"ck_{table_name}_{prefix}_archive_state"),
            table_name,
            "archive_state IN ('pending', 'processing', 'retry_wait', 'ready', "
            "'deleted', 'failed')",
        )
        op.drop_column(table_name, "previous_archive_size")
        op.drop_column(table_name, "cleanup_job_id")

    op.drop_index("ix_cleanup_jobs_retry_due", table_name="archive_cleanup_jobs")
    op.drop_index("ix_cleanup_jobs_status_lease", table_name="archive_cleanup_jobs")
    op.drop_table("archive_cleanup_jobs")
    op.drop_index("ix_cleanup_dry_runs_actor_created", table_name="archive_cleanup_dry_runs")
    op.drop_table("archive_cleanup_dry_runs")


def _downgrade_publication() -> None:
    _replace_publication_identity_function(m6=False)

    op.drop_index("ix_publish_records_intent", table_name="publish_records")
    op.drop_table("publish_records")
    op.drop_index("ix_publish_attempts_intent", table_name="publish_attempts")
    op.drop_table("publish_attempts")

    op.drop_index("ix_publication_intents_retry_due", table_name="publication_intents")
    op.drop_index("ix_publication_intents_status_lease", table_name="publication_intents")
    op.drop_index("ix_publication_intents_pending", table_name="publication_intents")
    op.create_index(
        "ix_publication_intents_pending",
        "publication_intents",
        ["status", "created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    for constraint_name in (
        "publication_intent_completion",
        "publication_intent_rendered_snapshot",
        "publication_intent_rendered_hash",
        "publication_intent_rendered_context",
        "publication_intent_supplement_mode",
        "publication_intent_remote_message_ids",
        "publication_intent_supplement_random_id",
        "publication_intent_random_ids_claimed",
        "publication_intent_main_random_ids",
        "publication_intent_completed_parts",
        "publication_intent_in_flight",
        "publication_intent_failure_metadata",
        "publication_intent_retry_schedule",
        "publication_intent_lease",
        "publication_intent_attempts",
        "publication_intent_protected_fallback",
    ):
        op.drop_constraint(
            op.f(f"ck_publication_intents_{constraint_name}"),
            "publication_intents",
            type_="check",
        )

    for column_name in (
        "completed_at",
        "started_at",
        "last_failure_at",
        "last_error_type",
        "last_error_code",
        "lease_expires_at",
        "lease_token",
        "next_retry_at",
        "max_attempts",
        "attempt_count",
        "last_reconciled_at",
        "in_flight_started_at",
        "in_flight_operation",
        "in_flight_part",
        "main_remote_message_ids",
        "completed_parts",
        "supplement_random_id",
        "main_random_ids",
        "rendered_snapshot_hash",
        "rendered_context",
        "rendered_text",
        "supplement_as_reply",
        "protected_content_fallback",
    ):
        op.drop_column("publication_intents", column_name)
