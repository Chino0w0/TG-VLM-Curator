"""Add durable range scheduling and broker-repair wake-ups.

Revision ID: f5b4a0b8c61a
Revises: 94c2d3062de4
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f5b4a0b8c61a"
down_revision: str | None = "94c2d3062de4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create finite execution state and PostgreSQL-owned durable wake-up leases."""
    op.add_column(
        "processing_ranges",
        sa.Column("processing_watermark_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_processing_range_watermark_floor",
        "processing_ranges",
        "processing_watermark_at IS NULL OR processing_watermark_at >= start_at",
    )
    op.create_check_constraint(
        "ck_processing_range_watermark_fixed_ceiling",
        "processing_ranges",
        "fixed_end_at IS NULL OR processing_watermark_at IS NULL "
        "OR processing_watermark_at <= fixed_end_at",
    )

    op.create_table(
        "range_executions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("processing_range_id", sa.Uuid(), nullable=False),
        sa.Column("source_profile_version_id", sa.Uuid(), nullable=False),
        sa.Column("from_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("to_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("watermark_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed')",
            name="ck_range_execution_status",
        ),
        sa.CheckConstraint("from_at < to_at", name="ck_range_execution_bounds"),
        sa.CheckConstraint(
            "watermark_at IS NULL OR (watermark_at >= from_at AND watermark_at <= to_at)",
            name="ck_range_execution_watermark_bounds",
        ),
        sa.CheckConstraint(
            "status <> 'completed' OR (watermark_at = to_at AND completed_at IS NOT NULL)",
            name="ck_range_execution_completion",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status <> 'running' AND lease_token IS NULL AND lease_expires_at IS NULL)",
            name="ck_range_execution_lease",
        ),
        sa.ForeignKeyConstraint(
            ["processing_range_id"], ["processing_ranges.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_profile_version_id"],
            ["source_channel_profile_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "processing_range_id", "from_at", "to_at", name="uq_range_execution_bounds"
        ),
    )
    op.create_index(
        "ix_range_executions_status_lease",
        "range_executions",
        ["status", "lease_expires_at"],
        unique=False,
    )
    op.create_index(
        "uq_range_execution_one_active",
        "range_executions",
        ["processing_range_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )

    op.create_table(
        "durable_wakeups",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("queue", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispatch_attempts", sa.Integer(), nullable=False),
        sa.Column("last_dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'leased', 'completed', 'cancelled')",
            name="ck_durable_wakeup_status",
        ),
        sa.CheckConstraint("dispatch_attempts >= 0", name="ck_durable_wakeup_attempts"),
        sa.CheckConstraint(
            "(status = 'leased' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status <> 'leased' AND lease_token IS NULL AND lease_expires_at IS NULL)",
            name="ck_durable_wakeup_lease",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("queue", "entity_id", name="uq_durable_wakeup_queue_entity"),
    )
    op.create_index(
        "ix_durable_wakeups_due",
        "durable_wakeups",
        ["status", "next_attempt_at"],
        unique=False,
    )


def downgrade() -> None:
    """Remove M2 durable scheduling state."""
    op.drop_index("ix_durable_wakeups_due", table_name="durable_wakeups")
    op.drop_table("durable_wakeups")
    op.drop_index("uq_range_execution_one_active", table_name="range_executions")
    op.drop_index("ix_range_executions_status_lease", table_name="range_executions")
    op.drop_table("range_executions")
    op.drop_constraint(
        "ck_processing_range_watermark_fixed_ceiling",
        "processing_ranges",
        type_="check",
    )
    op.drop_constraint("ck_processing_range_watermark_floor", "processing_ranges", type_="check")
    op.drop_column("processing_ranges", "processing_watermark_at")
