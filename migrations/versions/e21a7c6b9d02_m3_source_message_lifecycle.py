"""M3 source-message edit lifecycle."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e21a7c6b9d02"
down_revision = "d4e8f1a9b725"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("source_edited_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_message_source_edited_after_sent",
        "messages",
        "source_edited_at IS NULL OR source_edited_at >= sent_at",
    )


def downgrade() -> None:
    op.drop_constraint("ck_message_source_edited_after_sent", "messages", type_="check")
    op.drop_column("messages", "source_edited_at")
