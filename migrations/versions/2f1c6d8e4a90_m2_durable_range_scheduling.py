"""Add durable message-ID range execution scheduling.

Revision ID: 2f1c6d8e4a90
Revises: 94c2d3062de4
Create Date: 2026-09-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2f1c6d8e4a90"
down_revision: str | Sequence[str] | None = "94c2d3062de4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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


def upgrade() -> None:
    op.create_check_constraint(
        op.f("ck_processing_ranges_processing_range_watermark_start_floor"),
        "processing_ranges",
        "processing_watermark_message_id IS NULL "
        "OR resolved_start_message_id IS NULL "
        "OR processing_watermark_message_id >= resolved_start_message_id - 1",
    )
    op.create_check_constraint(
        op.f("ck_processing_ranges_processing_range_watermark_fixed_ceiling"),
        "processing_ranges",
        "end_mode <> 'fixed' "
        "OR processing_watermark_message_id IS NULL "
        "OR resolved_fixed_end_message_id IS NULL "
        "OR processing_watermark_message_id <= resolved_fixed_end_message_id",
    )

    op.create_table(
        "range_executions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("processing_range_id", sa.Uuid(), nullable=False),
        sa.Column("source_profile_version_id", sa.Uuid(), nullable=False),
        sa.Column("from_message_id_exclusive", sa.BigInteger(), nullable=False),
        sa.Column("to_message_id_inclusive", sa.BigInteger(), nullable=False),
        sa.Column("watermark_message_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
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
            "status IN ('pending', 'running', 'retry_wait', 'completed', 'failed')",
            name=op.f("ck_range_executions_range_execution_status"),
        ),
        sa.CheckConstraint(
            "from_message_id_exclusive >= 0 "
            "AND to_message_id_inclusive > from_message_id_exclusive",
            name=op.f("ck_range_executions_range_execution_bounds"),
        ),
        sa.CheckConstraint(
            "watermark_message_id >= from_message_id_exclusive "
            "AND watermark_message_id <= to_message_id_inclusive",
            name=op.f("ck_range_executions_range_execution_watermark_bounds"),
        ),
        sa.CheckConstraint(
            "max_attempts > 0 AND attempt_count >= 0 AND attempt_count <= max_attempts",
            name=op.f("ck_range_executions_range_execution_attempts"),
        ),
        sa.CheckConstraint(
            "status <> 'completed' "
            "OR (watermark_message_id = to_message_id_inclusive AND completed_at IS NOT NULL)",
            name=op.f("ck_range_executions_range_execution_completion"),
        ),
        sa.CheckConstraint(
            "(status = 'running' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status <> 'running' AND lease_token IS NULL AND lease_expires_at IS NULL)",
            name=op.f("ck_range_executions_range_execution_lease"),
        ),
        sa.CheckConstraint(
            "(status = 'retry_wait' AND next_retry_at IS NOT NULL) "
            "OR (status <> 'retry_wait' AND next_retry_at IS NULL)",
            name=op.f("ck_range_executions_range_execution_retry_schedule"),
        ),
        sa.CheckConstraint(
            "status NOT IN ('retry_wait', 'failed') "
            "OR (last_error_code IS NOT NULL "
            "AND last_error_type IS NOT NULL AND last_failure_at IS NOT NULL)",
            name=op.f("ck_range_executions_range_execution_failure_metadata"),
        ),
        sa.ForeignKeyConstraint(
            ["processing_range_id"],
            ["processing_ranges.id"],
            name="fk_range_executions_processing_range_id_processing_ranges",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_profile_version_id"],
            ["source_channel_profile_versions.id"],
            name="fk_range_executions_profile_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_range_executions"),
        sa.UniqueConstraint(
            "processing_range_id",
            "from_message_id_exclusive",
            "to_message_id_inclusive",
            name="uq_range_execution_bounds",
        ),
    )
    op.create_index(
        "ix_range_executions_status_lease",
        "range_executions",
        ["status", "lease_expires_at"],
    )
    op.create_index(
        "ix_range_executions_retry_due",
        "range_executions",
        ["status", "next_retry_at"],
    )
    op.create_index(
        "uq_range_execution_one_active",
        "range_executions",
        ["processing_range_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running', 'retry_wait')"),
    )

    op.create_table(
        "durable_wakeups",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("queue", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispatch_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('pending', 'leased', 'completed', 'cancelled')",
            name=op.f("ck_durable_wakeups_durable_wakeup_status"),
        ),
        sa.CheckConstraint(
            "dispatch_attempts >= 0",
            name=op.f("ck_durable_wakeups_durable_wakeup_attempts"),
        ),
        sa.CheckConstraint(
            "(status = 'leased' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status <> 'leased' AND lease_token IS NULL AND lease_expires_at IS NULL)",
            name=op.f("ck_durable_wakeups_durable_wakeup_lease"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_durable_wakeups"),
        sa.UniqueConstraint("queue", "entity_id", name="uq_durable_wakeup_queue_entity"),
    )
    op.create_index(
        "ix_durable_wakeups_due",
        "durable_wakeups",
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_durable_wakeups_due", table_name="durable_wakeups")
    op.drop_table("durable_wakeups")
    op.drop_index("uq_range_execution_one_active", table_name="range_executions")
    op.drop_index("ix_range_executions_retry_due", table_name="range_executions")
    op.drop_index("ix_range_executions_status_lease", table_name="range_executions")
    op.drop_table("range_executions")
    op.drop_constraint(
        op.f("ck_processing_ranges_processing_range_watermark_fixed_ceiling"),
        "processing_ranges",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_processing_ranges_processing_range_watermark_start_floor"),
        "processing_ranges",
        type_="check",
    )
