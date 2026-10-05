"""`job_items.stage_since` (when the item entered its status) and `resources.reason` (why it is blocked).

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("job_items", sa.Column("stage_since", sa.DateTime(timezone=True), nullable=True))
    # The best guess for an existing row: its status was last set when the row was last updated.
    op.execute(sa.text("UPDATE job_items SET stage_since = updated_at"))
    op.add_column("resources", sa.Column("reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("resources", "reason")
    op.drop_column("job_items", "stage_since")
