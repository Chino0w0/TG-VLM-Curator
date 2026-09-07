"""M3 preserve exact Telegram source message references for image assets."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a7d90f24b3ce"
down_revision = "f6b38e5ca911"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "image_assets",
        sa.Column("source_telegram_message_id", sa.BigInteger(), nullable=True),
    )
    op.create_check_constraint(
        "ck_image_asset_source_message_positive",
        "image_assets",
        "source_telegram_message_id IS NULL OR source_telegram_message_id > 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_image_asset_source_message_positive", "image_assets", type_="check")
    op.drop_column("image_assets", "source_telegram_message_id")
