"""`backfill_videos`: the backlog of YouTube videos found in the old links, released into the queue over time.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "backfill_videos",
        sa.Column("video_id", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("found_in", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("found_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("source IN ('docs', 'channel')", name="ck_backfill_videos_source"),
        sa.CheckConstraint("status IN ('pending', 'released')", name="ck_backfill_videos_status"),
        sa.PrimaryKeyConstraint("video_id", name="pk_backfill_videos"),
    )
    op.create_index(
        "ix_backfill_videos_pending",
        "backfill_videos",
        ["found_at", "video_id"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("ix_backfill_videos_pending", table_name="backfill_videos")
    op.drop_table("backfill_videos")
