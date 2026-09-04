"""M3 normalized Telegram message membership."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c3b7d2a914ef"
down_revision = "f5b4a0b8c61a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Persist Telegram-native media-group identity and actual component message IDs."""
    op.create_check_constraint(
        "ck_message_anchor_positive", "messages", "telegram_anchor_message_id > 0"
    )
    op.create_check_constraint(
        "ck_message_group_positive",
        "messages",
        "telegram_group_id IS NULL OR telegram_group_id > 0",
    )
    op.create_check_constraint("ck_message_media_count_nonnegative", "messages", "media_count >= 0")
    op.create_index(
        "uq_messages_source_group",
        "messages",
        ["source_channel_id", "telegram_group_id"],
        unique=True,
        postgresql_where=sa.text("telegram_group_id IS NOT NULL"),
    )

    op.create_table(
        "message_parts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("source_channel_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("telegram_message_id > 0", name="ck_message_part_telegram_positive"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_channel_id"], ["source_channels.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_channel_id",
            "telegram_message_id",
            name="uq_message_part_source_telegram",
        ),
    )
    op.create_index("ix_message_parts_message", "message_parts", ["message_id"], unique=False)


def downgrade() -> None:
    """Remove M3 Telegram membership persistence."""
    op.drop_index("ix_message_parts_message", table_name="message_parts")
    op.drop_table("message_parts")
    op.drop_index("uq_messages_source_group", table_name="messages")
    op.drop_constraint("ck_message_media_count_nonnegative", "messages", type_="check")
    op.drop_constraint("ck_message_group_positive", "messages", type_="check")
    op.drop_constraint("ck_message_anchor_positive", "messages", type_="check")
