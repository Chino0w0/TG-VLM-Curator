"""M3 image archive metadata and READY transitions."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f6b38e5ca911"
down_revision = "e21a7c6b9d02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "image_assets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("source_asset_id", sa.String(length=512), nullable=False),
        sa.Column("source_phash", sa.String(length=128), nullable=True),
        sa.Column("archive_state", sa.String(length=16), nullable=False),
        sa.Column("storage_backend", sa.String(length=64), nullable=True),
        sa.Column("storage_key", sa.String(length=1024), nullable=True),
        sa.Column("content_type", sa.String(length=128), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("source_sha256", sa.String(length=64), nullable=True),
        sa.Column("perceptual_hash", sa.String(length=128), nullable=True),
        sa.Column("archive_sha256", sa.String(length=64), nullable=True),
        sa.Column("archive_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("archive_ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archive_deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archive_failure_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "archive_state IN ('pending', 'ready', 'deleted', 'failed')",
            name="ck_image_asset_archive_state",
        ),
        sa.CheckConstraint(
            "archive_state <> 'ready' OR (storage_backend IS NOT NULL AND "
            "storage_key IS NOT NULL AND content_type IS NOT NULL AND "
            "width IS NOT NULL AND height IS NOT NULL AND "
            "source_sha256 IS NOT NULL AND archive_sha256 IS NOT NULL AND "
            "perceptual_hash IS NOT NULL AND archive_size_bytes IS NOT NULL AND "
            "archive_ready_at IS NOT NULL)",
            name="ck_image_asset_ready_metadata",
        ),
        sa.CheckConstraint(
            "archive_state <> 'deleted' OR archive_deleted_at IS NOT NULL",
            name="ck_image_asset_deleted_timestamp",
        ),
        sa.CheckConstraint("width IS NULL OR width > 0", name="ck_image_asset_width_positive"),
        sa.CheckConstraint("height IS NULL OR height > 0", name="ck_image_asset_height_positive"),
        sa.CheckConstraint(
            "archive_size_bytes IS NULL OR archive_size_bytes > 0",
            name="ck_image_asset_archive_size_positive",
        ),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id", "source_asset_id", name="uq_image_asset_message_source"),
    )
    op.create_index(
        "ix_image_assets_archive_state", "image_assets", ["archive_state"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_image_assets_archive_state", table_name="image_assets")
    op.drop_table("image_assets")
