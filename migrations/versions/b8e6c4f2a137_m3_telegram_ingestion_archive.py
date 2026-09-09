"""Add M3 Telegram ingestion and durable media archive workflows.

Revision ID: b8e6c4f2a137
Revises: 2f1c6d8e4a90
Create Date: 2026-09-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b8e6c4f2a137"
down_revision: str | Sequence[str] | None = "2f1c6d8e4a90"
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
    op.add_column(
        "source_channels",
        sa.Column("last_seen_message_id", sa.BigInteger(), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_source_channels_source_channel_last_seen_positive"),
        "source_channels",
        "last_seen_message_id IS NULL OR last_seen_message_id > 0",
    )

    op.drop_constraint("uq_message_source_primary", "messages", type_="unique")
    op.add_column(
        "messages",
        sa.Column(
            "source_changed_after_processing",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "messages",
        sa.Column(
            "source_deleted",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "messages",
        sa.Column("source_deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_messages_message_primary_positive"),
        "messages",
        "primary_telegram_message_id > 0",
    )
    op.create_check_constraint(
        op.f("ck_messages_message_grouped_positive"),
        "messages",
        "telegram_grouped_id IS NULL OR telegram_grouped_id > 0",
    )
    op.create_check_constraint(
        op.f("ck_messages_message_edited_after_published"),
        "messages",
        "edited_at IS NULL OR edited_at >= published_at",
    )
    op.create_check_constraint(
        op.f("ck_messages_message_source_deleted_state"),
        "messages",
        "(source_deleted IS FALSE AND source_deleted_at IS NULL) OR "
        "(source_deleted IS TRUE AND source_deleted_at IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_messages_message_source_deleted_after_published"),
        "messages",
        "source_deleted_at IS NULL OR source_deleted_at >= published_at",
    )
    op.create_index(
        "uq_messages_source_regular",
        "messages",
        ["source_channel_id", "primary_telegram_message_id"],
        unique=True,
        postgresql_where=sa.text("telegram_grouped_id IS NULL"),
    )
    op.create_index(
        "uq_messages_source_grouped",
        "messages",
        ["source_channel_id", "telegram_grouped_id"],
        unique=True,
        postgresql_where=sa.text("telegram_grouped_id IS NOT NULL"),
    )

    op.create_table(
        "message_parts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("source_channel_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("original_text", sa.Text(), nullable=True),
        sa.Column(
            "telegram_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "media",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("media_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "telegram_message_id > 0",
            name=op.f("ck_message_parts_message_part_telegram_message_positive"),
        ),
        sa.CheckConstraint(
            "edited_at IS NULL OR edited_at >= published_at",
            name=op.f("ck_message_parts_message_part_edited_after_published"),
        ),
        sa.CheckConstraint(
            "media_count >= 0",
            name=op.f("ck_message_parts_message_part_media_count_nonnegative"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(media) = 'array' AND jsonb_array_length(media) = media_count",
            name=op.f("ck_message_parts_message_part_media_snapshot_count"),
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_message_parts_message_id_messages"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_channel_id"],
            ["source_channels.id"],
            name=op.f("fk_message_parts_source_channel_id_source_channels"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message_parts")),
        sa.UniqueConstraint(
            "source_channel_id",
            "telegram_message_id",
            name="uq_message_part_source_telegram",
        ),
    )
    op.create_index("ix_message_parts_message", "message_parts", ["message_id"])

    op.create_table(
        "image_assets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("source_asset_id", sa.String(length=512), nullable=False),
        sa.Column("source_phash", sa.String(length=128), nullable=True),
        sa.Column("source_telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("perceptual_hash", sa.String(length=128), nullable=True),
        sa.Column(
            "archive_state",
            sa.String(length=16),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "archive_attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "archive_max_attempts", sa.Integer(), server_default=sa.text("5"), nullable=False
        ),
        sa.Column("archive_next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archive_lease_token", sa.Uuid(), nullable=True),
        sa.Column("archive_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archive_last_error_code", sa.String(length=64), nullable=True),
        sa.Column("archive_last_error_type", sa.String(length=128), nullable=True),
        sa.Column("archive_last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("storage_backend", sa.String(length=64), nullable=True),
        sa.Column("storage_key", sa.String(length=1024), nullable=True),
        sa.Column("content_type", sa.String(length=128), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("source_sha256", sa.String(length=64), nullable=True),
        sa.Column("archive_sha256", sa.String(length=64), nullable=True),
        sa.Column("archive_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("archive_ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archive_deleted_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "archive_state IN ('pending', 'processing', 'retry_wait', 'ready', "
            "'deleted', 'failed')",
            name=op.f("ck_image_assets_image_asset_archive_state"),
        ),
        sa.CheckConstraint(
            "source_telegram_message_id IS NULL OR source_telegram_message_id > 0",
            name=op.f("ck_image_assets_image_asset_source_message_positive"),
        ),
        sa.CheckConstraint(
            "archive_attempt_count >= 0 AND archive_attempt_count <= archive_max_attempts "
            "AND archive_max_attempts > 0",
            name=op.f("ck_image_assets_image_asset_archive_attempts"),
        ),
        sa.CheckConstraint(
            "(archive_state = 'processing' AND archive_lease_token IS NOT NULL "
            "AND archive_lease_expires_at IS NOT NULL AND archive_attempt_count > 0) OR "
            "(archive_state <> 'processing' AND archive_lease_token IS NULL "
            "AND archive_lease_expires_at IS NULL)",
            name=op.f("ck_image_assets_image_asset_archive_lease"),
        ),
        sa.CheckConstraint(
            "(archive_state = 'retry_wait' AND archive_next_retry_at IS NOT NULL) OR "
            "(archive_state <> 'retry_wait' AND archive_next_retry_at IS NULL)",
            name=op.f("ck_image_assets_image_asset_archive_retry_schedule"),
        ),
        sa.CheckConstraint(
            "archive_state NOT IN ('retry_wait', 'failed') OR "
            "(archive_last_error_code IS NOT NULL AND archive_last_error_type IS NOT NULL "
            "AND archive_last_failure_at IS NOT NULL)",
            name=op.f("ck_image_assets_image_asset_archive_failure_metadata"),
        ),
        sa.CheckConstraint(
            "archive_state <> 'ready' OR (storage_backend IS NOT NULL AND storage_key IS NOT NULL "
            "AND content_type IS NOT NULL AND width IS NOT NULL AND height IS NOT NULL "
            "AND source_sha256 IS NOT NULL AND archive_sha256 IS NOT NULL "
            "AND perceptual_hash IS NOT NULL AND archive_size_bytes IS NOT NULL "
            "AND archive_ready_at IS NOT NULL)",
            name=op.f("ck_image_assets_image_asset_ready_metadata"),
        ),
        sa.CheckConstraint(
            "archive_state <> 'deleted' OR archive_deleted_at IS NOT NULL",
            name=op.f("ck_image_assets_image_asset_deleted_timestamp"),
        ),
        sa.CheckConstraint(
            "width IS NULL OR width > 0",
            name=op.f("ck_image_assets_image_asset_width_positive"),
        ),
        sa.CheckConstraint(
            "height IS NULL OR height > 0",
            name=op.f("ck_image_assets_image_asset_height_positive"),
        ),
        sa.CheckConstraint(
            "archive_size_bytes IS NULL OR archive_size_bytes > 0",
            name=op.f("ck_image_assets_image_asset_archive_size_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_image_assets_message_id_messages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_image_assets")),
        sa.UniqueConstraint("message_id", "source_asset_id", name="uq_image_asset_message_source"),
    )
    op.create_index("ix_image_assets_archive_state", "image_assets", ["archive_state"])
    op.create_index(
        "ix_image_assets_archive_due",
        "image_assets",
        ["archive_state", "archive_next_retry_at"],
    )
    op.create_index(
        "ix_image_assets_archive_lease",
        "image_assets",
        ["archive_state", "archive_lease_expires_at"],
    )

    op.create_table(
        "video_assets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("source_asset_id", sa.String(length=512), nullable=False),
        sa.Column("source_telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("source_cover_phash", sa.String(length=128), nullable=True),
        sa.Column(
            "archive_state",
            sa.String(length=16),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "archive_attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "archive_max_attempts", sa.Integer(), server_default=sa.text("5"), nullable=False
        ),
        sa.Column("archive_next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archive_lease_token", sa.Uuid(), nullable=True),
        sa.Column("archive_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archive_last_error_code", sa.String(length=64), nullable=True),
        sa.Column("archive_last_error_type", sa.String(length=128), nullable=True),
        sa.Column("archive_last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_content_type", sa.String(length=128), nullable=True),
        sa.Column("source_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("source_sha256", sa.String(length=64), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("source_width", sa.Integer(), nullable=True),
        sa.Column("source_height", sa.Integer(), nullable=True),
        sa.Column("archive_ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archive_deleted_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "archive_state IN ('pending', 'processing', 'retry_wait', 'ready', "
            "'deleted', 'failed')",
            name=op.f("ck_video_assets_video_asset_archive_state"),
        ),
        sa.CheckConstraint(
            "source_telegram_message_id IS NULL OR source_telegram_message_id > 0",
            name=op.f("ck_video_assets_video_asset_source_message_positive"),
        ),
        sa.CheckConstraint(
            "archive_attempt_count >= 0 AND archive_attempt_count <= archive_max_attempts "
            "AND archive_max_attempts > 0",
            name=op.f("ck_video_assets_video_asset_archive_attempts"),
        ),
        sa.CheckConstraint(
            "(archive_state = 'processing' AND archive_lease_token IS NOT NULL "
            "AND archive_lease_expires_at IS NOT NULL AND archive_attempt_count > 0) OR "
            "(archive_state <> 'processing' AND archive_lease_token IS NULL "
            "AND archive_lease_expires_at IS NULL)",
            name=op.f("ck_video_assets_video_asset_archive_lease"),
        ),
        sa.CheckConstraint(
            "(archive_state = 'retry_wait' AND archive_next_retry_at IS NOT NULL) OR "
            "(archive_state <> 'retry_wait' AND archive_next_retry_at IS NULL)",
            name=op.f("ck_video_assets_video_asset_archive_retry_schedule"),
        ),
        sa.CheckConstraint(
            "archive_state NOT IN ('retry_wait', 'failed') OR "
            "(archive_last_error_code IS NOT NULL AND archive_last_error_type IS NOT NULL "
            "AND archive_last_failure_at IS NOT NULL)",
            name=op.f("ck_video_assets_video_asset_archive_failure_metadata"),
        ),
        sa.CheckConstraint(
            "archive_state <> 'ready' OR (duration_seconds IS NOT NULL "
            "AND source_sha256 IS NOT NULL AND archive_ready_at IS NOT NULL)",
            name=op.f("ck_video_assets_video_asset_ready_metadata"),
        ),
        sa.CheckConstraint(
            "archive_state <> 'deleted' OR archive_deleted_at IS NOT NULL",
            name=op.f("ck_video_assets_video_asset_deleted_timestamp"),
        ),
        sa.CheckConstraint(
            "duration_seconds IS NULL OR duration_seconds > 0",
            name=op.f("ck_video_assets_video_asset_duration_positive"),
        ),
        sa.CheckConstraint(
            "source_size_bytes IS NULL OR source_size_bytes > 0",
            name=op.f("ck_video_assets_video_asset_source_size_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_video_assets_message_id_messages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_video_assets")),
        sa.UniqueConstraint("message_id", "source_asset_id", name="uq_video_asset_message_source"),
    )
    op.create_index("ix_video_assets_archive_state", "video_assets", ["archive_state"])
    op.create_index(
        "ix_video_assets_archive_due",
        "video_assets",
        ["archive_state", "archive_next_retry_at"],
    )
    op.create_index(
        "ix_video_assets_archive_lease",
        "video_assets",
        ["archive_state", "archive_lease_expires_at"],
    )

    op.create_table(
        "video_frames",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("video_asset_id", sa.Uuid(), nullable=False),
        sa.Column("frame_role", sa.String(length=24), nullable=False),
        sa.Column("frame_index", sa.Integer(), nullable=False),
        sa.Column("timestamp_seconds", sa.Float(), nullable=False),
        sa.Column("storage_backend", sa.String(length=64), nullable=False),
        sa.Column("storage_key", sa.String(length=1024), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("archive_sha256", sa.String(length=64), nullable=False),
        sa.Column("perceptual_hash", sa.String(length=128), nullable=False),
        sa.Column("archive_size_bytes", sa.BigInteger(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "frame_role IN ('cover', 'representative')",
            name=op.f("ck_video_frames_video_frame_role"),
        ),
        sa.CheckConstraint(
            "frame_index >= 0 AND (frame_role <> 'cover' OR frame_index = 0)",
            name=op.f("ck_video_frames_video_frame_index"),
        ),
        sa.CheckConstraint(
            "timestamp_seconds >= 0",
            name=op.f("ck_video_frames_video_frame_timestamp_nonnegative"),
        ),
        sa.CheckConstraint(
            "content_type = 'image/webp'",
            name=op.f("ck_video_frames_video_frame_content_type"),
        ),
        sa.CheckConstraint(
            "width > 0",
            name=op.f("ck_video_frames_video_frame_width_positive"),
        ),
        sa.CheckConstraint(
            "height > 0",
            name=op.f("ck_video_frames_video_frame_height_positive"),
        ),
        sa.CheckConstraint(
            "archive_size_bytes > 0",
            name=op.f("ck_video_frames_video_frame_archive_size_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["video_asset_id"],
            ["video_assets.id"],
            name=op.f("fk_video_frames_video_asset_id_video_assets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_video_frames")),
        sa.UniqueConstraint(
            "video_asset_id",
            "frame_role",
            "frame_index",
            name="uq_video_frame_role_index",
        ),
        sa.UniqueConstraint("storage_backend", "storage_key", name="uq_video_frame_storage_key"),
    )
    op.create_index(
        "ix_video_frames_video",
        "video_frames",
        ["video_asset_id", "frame_role", "frame_index"],
    )


def downgrade() -> None:
    op.drop_index("ix_video_frames_video", table_name="video_frames")
    op.drop_table("video_frames")

    op.drop_index("ix_video_assets_archive_lease", table_name="video_assets")
    op.drop_index("ix_video_assets_archive_due", table_name="video_assets")
    op.drop_index("ix_video_assets_archive_state", table_name="video_assets")
    op.drop_table("video_assets")

    op.drop_index("ix_image_assets_archive_lease", table_name="image_assets")
    op.drop_index("ix_image_assets_archive_due", table_name="image_assets")
    op.drop_index("ix_image_assets_archive_state", table_name="image_assets")
    op.drop_table("image_assets")

    op.drop_index("ix_message_parts_message", table_name="message_parts")
    op.drop_table("message_parts")

    op.drop_index("uq_messages_source_grouped", table_name="messages")
    op.drop_index("uq_messages_source_regular", table_name="messages")
    op.drop_constraint(
        op.f("ck_messages_message_source_deleted_after_published"),
        "messages",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_messages_message_source_deleted_state"),
        "messages",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_messages_message_edited_after_published"),
        "messages",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_messages_message_grouped_positive"),
        "messages",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_messages_message_primary_positive"),
        "messages",
        type_="check",
    )
    op.drop_column("messages", "source_deleted_at")
    op.drop_column("messages", "source_deleted")
    op.drop_column("messages", "source_changed_after_processing")
    op.create_unique_constraint(
        "uq_message_source_primary",
        "messages",
        ["source_channel_id", "primary_telegram_message_id"],
    )

    op.drop_constraint(
        op.f("ck_source_channels_source_channel_last_seen_positive"),
        "source_channels",
        type_="check",
    )
    op.drop_column("source_channels", "last_seen_message_id")
