"""M3 lease image archive work outside Telegram and storage I/O."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "da03ce8624f1"
down_revision = "a7d90f24b3ce"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "image_assets",
        sa.Column("archive_lease_token", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "image_assets",
        sa.Column("archive_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_image_asset_archive_lease",
        "image_assets",
        "(archive_lease_token IS NULL AND archive_lease_expires_at IS NULL) OR "
        "(archive_state = 'pending' AND archive_lease_token IS NOT NULL "
        "AND archive_lease_expires_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_image_asset_failure_reason",
        "image_assets",
        "archive_state <> 'failed' OR archive_failure_reason IS NOT NULL",
    )
    op.create_index(
        "ix_image_assets_pending_archive_lease",
        "image_assets",
        ["archive_state", "archive_lease_expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_image_assets_pending_archive_lease", table_name="image_assets")
    op.drop_constraint("ck_image_asset_failure_reason", "image_assets", type_="check")
    op.drop_constraint("ck_image_asset_archive_lease", "image_assets", type_="check")
    op.drop_column("image_assets", "archive_lease_expires_at")
    op.drop_column("image_assets", "archive_lease_token")
