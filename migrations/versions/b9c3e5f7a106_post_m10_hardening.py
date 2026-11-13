"""Post-M10 hardening: service heartbeats, hot-path indexes, append-only & dedup triggers.

Revision ID: b9c3e5f7a106
Revises: a8b2d4e1f305
Create Date: 2026-11-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b9c3e5f7a106"
down_revision: str | Sequence[str] | None = "a8b2d4e1f305"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "service_heartbeats",
        sa.Column("service_name", sa.String(length=64), nullable=False),
        sa.Column("database_ready", sa.Boolean(), nullable=False),
        sa.Column("broker_ready", sa.Boolean(), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(btrim(service_name)) > 0",
            name=op.f("ck_service_heartbeats_service_heartbeat_name_not_blank"),
        ),
        sa.PrimaryKeyConstraint("service_name", name=op.f("pk_service_heartbeats")),
    )

    op.create_index(
        "ix_durable_wakeups_status_lease",
        "durable_wakeups",
        ["status", "lease_expires_at"],
    )
    op.create_index(
        "ix_messages_dedup_candidates",
        "messages",
        ["published_at", "id"],
        postgresql_where=sa.text(
            "media_count > 0 AND duplicate_status = 'unique' AND processing_status <> 'failed'"
        ),
    )
    op.create_index(
        "ix_stage_runs_batch_colease",
        "stage_runs",
        [
            "analysis_stage_template_version_id",
            "pipeline_stage_node_id",
            "target_kind",
            "status",
            "created_at",
        ],
    )
    op.create_index(
        "ix_stage_runs_message_status",
        "stage_runs",
        ["message_id", "status"],
    )
    op.create_index(
        "ix_stage_runs_target_status",
        "stage_runs",
        ["target_id", "status"],
    )
    op.create_index(
        "ix_model_assignments_analysis_run",
        "model_label_assignments",
        ["analysis_run_id"],
    )
    op.create_index(
        "ix_manual_labels_target_key_created",
        "manual_label_assignments",
        ["message_id", "target_scope", "target_id", "label_key", "created_at"],
    )

    op.execute(
        """
        CREATE FUNCTION tgcurator_reject_audit_event_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS 'BEGIN
            RAISE EXCEPTION ''audit events are append-only'';
        END;'
        """
    )
    op.execute(
        "CREATE TRIGGER trg_audit_events_append_only "
        "BEFORE UPDATE OR DELETE ON audit_events "
        "FOR EACH ROW EXECUTE FUNCTION "
        "tgcurator_reject_audit_event_mutation()"
    )

    op.execute(
        """
        CREATE FUNCTION tgcurator_reject_publish_record_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS 'BEGIN
            RAISE EXCEPTION ''publish records are append-only'';
        END;'
        """
    )
    op.execute(
        "CREATE TRIGGER trg_publish_records_append_only "
        "BEFORE UPDATE OR DELETE ON publish_records "
        "FOR EACH ROW EXECUTE FUNCTION "
        "tgcurator_reject_publish_record_mutation()"
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION tgcurator_m7_reopen_album_components() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT (OLD.telegram_message_ids @> NEW.telegram_message_ids
                    AND NEW.telegram_message_ids @> OLD.telegram_message_ids) THEN
                DELETE FROM duplicate_matches WHERE message_id = NEW.id;
                NEW.source_changed_after_processing :=
                    OLD.source_changed_after_processing OR OLD.processing_status = 'processed';
                NEW.duplicate_status := 'unchecked';
                NEW.canonical_message_id := NULL;
                NEW.duplicate_distance := NULL;
                IF NOT OLD.blocked_from_analysis
                   AND COALESCE(OLD.terminal_reason, '')
                       NOT IN ('admin_skipped', 'unsupported') THEN
                    NEW.processing_status := 'ingested';
                    NEW.terminal_reason := NULL;
                    NEW.failure_stage := NULL;
                    NEW.last_error_code := NULL;
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )


def downgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION tgcurator_m7_reopen_album_components() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT (OLD.telegram_message_ids @> NEW.telegram_message_ids
                    AND NEW.telegram_message_ids @> OLD.telegram_message_ids) THEN
                NEW.source_changed_after_processing :=
                    OLD.source_changed_after_processing OR OLD.processing_status = 'processed';
                NEW.duplicate_status := 'unchecked';
                NEW.canonical_message_id := NULL;
                NEW.duplicate_distance := NULL;
                IF NOT OLD.blocked_from_analysis
                   AND COALESCE(OLD.terminal_reason, '')
                       NOT IN ('admin_skipped', 'unsupported') THEN
                    NEW.processing_status := 'ingested';
                    NEW.terminal_reason := NULL;
                    NEW.failure_stage := NULL;
                    NEW.last_error_code := NULL;
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )

    op.execute("DROP TRIGGER trg_publish_records_append_only ON publish_records")
    op.execute("DROP FUNCTION tgcurator_reject_publish_record_mutation()")
    op.execute("DROP TRIGGER trg_audit_events_append_only ON audit_events")
    op.execute("DROP FUNCTION tgcurator_reject_audit_event_mutation()")

    op.drop_index("ix_manual_labels_target_key_created", table_name="manual_label_assignments")
    op.drop_index("ix_model_assignments_analysis_run", table_name="model_label_assignments")
    op.drop_index("ix_stage_runs_target_status", table_name="stage_runs")
    op.drop_index("ix_stage_runs_message_status", table_name="stage_runs")
    op.drop_index("ix_stage_runs_batch_colease", table_name="stage_runs")
    op.drop_index("ix_messages_dedup_candidates", table_name="messages")
    op.drop_index("ix_durable_wakeups_status_lease", table_name="durable_wakeups")

    op.drop_table("service_heartbeats")
