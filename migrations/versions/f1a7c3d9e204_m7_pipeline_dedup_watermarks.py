"""Add M7 pipeline orchestration, visual deduplication, and continuous watermarks.

Revision ID: f1a7c3d9e204
Revises: e6d4a8b2c951
Create Date: 2026-09-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1a7c3d9e204"
down_revision: str | Sequence[str] | None = "e6d4a8b2c951"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Allow RangeExecution to release its short worker lease while async pipeline stages finish.
    op.drop_index("uq_range_execution_one_active", table_name="range_executions")
    op.drop_constraint(
        op.f("ck_range_executions_range_execution_status"),
        "range_executions",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_range_executions_range_execution_status"),
        "range_executions",
        "status IN ('pending', 'running', 'awaiting_pipeline', "
        "'retry_wait', 'completed', 'failed')",
    )
    op.create_index(
        "uq_range_execution_one_active",
        "range_executions",
        ["processing_range_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending', 'running', 'awaiting_pipeline', 'retry_wait')"
        ),
    )

    # Extend messages with visual deduplication, terminal watermark reason, and failure stage.
    op.add_column("messages", sa.Column("canonical_message_id", sa.Uuid(), nullable=True))
    op.add_column(
        "messages",
        sa.Column(
            "duplicate_status",
            sa.String(length=32),
            server_default=sa.text("'unchecked'"),
            nullable=False,
        ),
    )
    op.add_column("messages", sa.Column("duplicate_distance", sa.Integer(), nullable=True))
    op.add_column("messages", sa.Column("terminal_reason", sa.String(length=32), nullable=True))
    op.add_column("messages", sa.Column("failure_stage", sa.String(length=32), nullable=True))
    op.add_column("messages", sa.Column("last_error_code", sa.String(length=128), nullable=True))

    # Backfill already-processed legacy rows so the new terminal_reason check holds cleanly.
    op.execute(
        "UPDATE messages SET terminal_reason = CASE "
        "WHEN blocked_from_analysis IS TRUE THEN 'blocked_negative_gate' "
        "ELSE 'completed' END "
        "WHERE processing_status = 'processed' AND terminal_reason IS NULL"
    )

    op.create_foreign_key(
        "fk_messages_canonical_message",
        "messages",
        "messages",
        ["canonical_message_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_messages_message_duplicate_status"),
        "messages",
        "duplicate_status IN ('unchecked', 'unique', 'duplicate_skipped', 'duplicate_reclassify')",
    )
    op.create_check_constraint(
        op.f("ck_messages_message_duplicate_canonical_state"),
        "messages",
        "(duplicate_status IN ('unchecked', 'unique') "
        "AND canonical_message_id IS NULL AND duplicate_distance IS NULL) OR "
        "(duplicate_status IN ('duplicate_skipped', 'duplicate_reclassify') "
        "AND canonical_message_id IS NOT NULL AND canonical_message_id <> id "
        "AND duplicate_distance IS NOT NULL "
        "AND duplicate_distance >= 0 AND duplicate_distance <= 64)",
    )
    op.create_check_constraint(
        op.f("ck_messages_message_terminal_reason_state"),
        "messages",
        "terminal_reason IS NULL OR (processing_status = 'processed' AND terminal_reason IN "
        "('completed', 'blocked_negative_gate', 'duplicate_skipped', "
        "'no_eligible_stages', 'unsupported', 'admin_skipped'))",
    )
    op.create_check_constraint(
        op.f("ck_messages_message_failure_stage_state"),
        "messages",
        "failure_stage IS NULL OR failure_stage IN "
        "('media_failed', 'analysis_failed', 'routing_failed', 'publish_failed')",
    )
    op.create_index(
        "ix_messages_source_primary_status",
        "messages",
        ["source_channel_id", "primary_telegram_message_id", "processing_status"],
    )
    op.create_index(
        "ix_messages_visual_fingerprint",
        "messages",
        ["visual_fingerprint"],
        postgresql_where=sa.text("visual_fingerprint IS NOT NULL"),
    )

    op.create_table(
        "duplicate_matches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_message_id", sa.Uuid(), nullable=False),
        sa.Column("match_kind", sa.String(length=32), nullable=False),
        sa.Column("hamming_distance", sa.Integer(), nullable=False),
        sa.Column("duplicate_policy", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "message_id <> canonical_message_id",
            name=op.f("ck_duplicate_matches_duplicate_match_not_self"),
        ),
        sa.CheckConstraint(
            "match_kind IN ('exact_fingerprint', 'perceptual_multiset')",
            name=op.f("ck_duplicate_matches_duplicate_match_kind"),
        ),
        sa.CheckConstraint(
            "hamming_distance >= 0 AND hamming_distance <= 64",
            name=op.f("ck_duplicate_matches_duplicate_match_distance"),
        ),
        sa.CheckConstraint(
            "duplicate_policy IN ('skip', 'reclassify_with_text')",
            name=op.f("ck_duplicate_matches_duplicate_match_policy"),
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name="fk_duplicate_matches_message",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["canonical_message_id"],
            ["messages.id"],
            name="fk_duplicate_matches_canonical",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_duplicate_matches"),
        sa.UniqueConstraint("message_id", name="uq_duplicate_matches_message"),
    )
    op.create_index(
        "ix_duplicate_matches_canonical",
        "duplicate_matches",
        ["canonical_message_id", "created_at"],
    )

    # Keep old runs as history while assigning the newest formal run to the current
    # component revision. This also tolerates historical duplicate formal runs.
    op.execute("""
        WITH ranked AS (
            SELECT a.id, m.telegram_message_ids,
                   row_number() OVER (
                       PARTITION BY a.message_id, a.analysis_pipeline_version_id
                       ORDER BY a.created_at DESC, a.id DESC
                   ) AS rank
            FROM analysis_runs a JOIN messages m ON m.id = a.message_id
            WHERE a.run_mode = 'formal'
        )
        UPDATE analysis_runs a SET facts_snapshot = a.facts_snapshot || jsonb_build_object(
            '_pipeline_input_revision', CASE WHEN r.rank = 1 THEN
                array_to_string(ARRAY(SELECT unnest(r.telegram_message_ids) ORDER BY 1), ',')
                ELSE 'legacy:' || a.id::text END
        ) FROM ranked r WHERE a.id = r.id
    """)
    op.create_index(
        "uq_analysis_runs_formal_input",
        "analysis_runs",
        [
            "message_id",
            "analysis_pipeline_version_id",
            sa.text("(COALESCE(facts_snapshot->>'_pipeline_input_revision', ''))"),
        ],
        unique=True,
        postgresql_where=sa.text("run_mode = 'formal'"),
    )
    # Cover legacy/direct formal writers too: omitted revision metadata must not
    # create a second business run in the expression index's empty-string bucket.
    op.execute("""
        CREATE FUNCTION tgcurator_m7_bind_formal_input() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE current_revision text;
        BEGIN
            IF NEW.run_mode = 'formal' THEN
                SELECT array_to_string(
                    ARRAY(SELECT unnest(m.telegram_message_ids) ORDER BY 1), ','
                ) INTO current_revision FROM messages m WHERE m.id = NEW.message_id FOR UPDATE;
                IF current_revision IS NULL THEN
                    RAISE EXCEPTION 'formal run message does not exist';
                END IF;
                IF NEW.facts_snapshot ? '_pipeline_input_revision'
                   AND NEW.facts_snapshot->>'_pipeline_input_revision' <> current_revision THEN
                    RAISE EXCEPTION 'formal run input revision is stale';
                END IF;
                NEW.facts_snapshot := NEW.facts_snapshot || jsonb_build_object(
                    '_pipeline_input_revision', current_revision
                );
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER trg_m7_bind_formal_input
        BEFORE INSERT ON analysis_runs FOR EACH ROW
        EXECUTE FUNCTION tgcurator_m7_bind_formal_input()
    """)
    # Ingestion retains a stable album parent. New components invalidate its terminal
    # processing/dedup state atomically, before assets/wakeups are added in that txn.
    op.execute("""
        CREATE FUNCTION tgcurator_m7_reopen_album_components() RETURNS trigger
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
    """)
    op.execute("""
        CREATE TRIGGER trg_m7_reopen_album_components
        BEFORE UPDATE OF telegram_message_ids ON messages
        FOR EACH ROW EXECUTE FUNCTION tgcurator_m7_reopen_album_components()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_m7_bind_formal_input ON analysis_runs")
    op.execute("DROP FUNCTION tgcurator_m7_bind_formal_input()")
    op.execute("DROP TRIGGER trg_m7_reopen_album_components ON messages")
    op.execute("DROP FUNCTION tgcurator_m7_reopen_album_components()")
    op.drop_index("uq_analysis_runs_formal_input", table_name="analysis_runs")
    op.drop_index("ix_duplicate_matches_canonical", table_name="duplicate_matches")
    op.drop_table("duplicate_matches")

    op.drop_index("ix_messages_visual_fingerprint", table_name="messages")
    op.drop_index("ix_messages_source_primary_status", table_name="messages")
    op.drop_constraint(op.f("ck_messages_message_failure_stage_state"), "messages", type_="check")
    op.drop_constraint(op.f("ck_messages_message_terminal_reason_state"), "messages", type_="check")
    op.drop_constraint(
        op.f("ck_messages_message_duplicate_canonical_state"), "messages", type_="check"
    )
    op.drop_constraint(op.f("ck_messages_message_duplicate_status"), "messages", type_="check")
    op.drop_constraint("fk_messages_canonical_message", "messages", type_="foreignkey")

    op.drop_column("messages", "last_error_code")
    op.drop_column("messages", "failure_stage")
    op.drop_column("messages", "terminal_reason")
    op.drop_column("messages", "duplicate_distance")
    op.drop_column("messages", "duplicate_status")
    op.drop_column("messages", "canonical_message_id")

    op.execute(
        "UPDATE range_executions SET status = 'failed', "
        "last_error_code = COALESCE(last_error_code, 'DOWNGRADED_AWAITING_PIPELINE'), "
        "last_error_type = COALESCE(last_error_type, 'DowngradedAwaitingPipeline'), "
        "last_failure_at = COALESCE(last_failure_at, now()) "
        "WHERE status = 'awaiting_pipeline'"
    )
    op.drop_index("uq_range_execution_one_active", table_name="range_executions")
    op.drop_constraint(
        op.f("ck_range_executions_range_execution_status"),
        "range_executions",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_range_executions_range_execution_status"),
        "range_executions",
        "status IN ('pending', 'running', 'retry_wait', 'completed', 'failed')",
    )
    op.create_index(
        "uq_range_execution_one_active",
        "range_executions",
        ["processing_range_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running', 'retry_wait')"),
    )
