"""M3 persisted source reconciliation cursor."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d4e8f1a9b725"
down_revision = "c3b7d2a914ef"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "source_channels", sa.Column("last_seen_message_id", sa.BigInteger(), nullable=True)
    )
    op.create_check_constraint(
        "ck_source_channel_last_seen_positive",
        "source_channels",
        "last_seen_message_id IS NULL OR last_seen_message_id > 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_source_channel_last_seen_positive", "source_channels", type_="check")
    op.drop_column("source_channels", "last_seen_message_id")
