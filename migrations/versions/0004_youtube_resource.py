"""Seed the open `youtube` row in `resources`: the gate for YouTube lives in Postgres.

Revision ID: 0004
Revises: 0003
"""

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

SEEDED_AT = datetime(2026, 10, 4, tzinfo=UTC)
# 'youtube' below must match YOUTUBE_RESOURCE in catcher/modules/youtube/pg_gate.py. A migration keeps its
# literal (it must not change when the code does), so the two are not linked by an import.


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(
            "INSERT INTO resources (name, next_allowed_at, blocked_until, blocked_at, streak, concurrency,"
            " updated_at) VALUES ('youtube', NULL, NULL, NULL, 0, 1, :seeded_at)"
            " ON CONFLICT (name) DO NOTHING"
        ),
        {"seeded_at": SEEDED_AT},
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM resources WHERE name = 'youtube'"))
