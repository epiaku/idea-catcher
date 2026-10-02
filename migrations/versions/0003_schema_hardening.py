"""Schema hardening: CHECKs for the job counters, the lease and the item origin, and the missing indexes.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint("ck_jobs_counters_non_negative", "jobs", "attempts >= 0 AND claim_seq >= 0")
    op.create_check_constraint(
        "ck_jobs_running_has_lease", "jobs", "status <> 'running' OR lease_until IS NOT NULL"
    )
    op.create_check_constraint("ck_job_items_origin", "job_items", "origin IN ('inbox', 'backfill')")
    op.create_index("ix_job_events_job_id", "job_events", ["job_id"])
    op.create_index("ix_job_events_item_id", "job_events", ["item_id"])
    op.create_index("ix_job_items_root_job_id", "job_items", ["root_job_id"])
    op.create_index(
        "ix_jobs_running_lease", "jobs", ["lease_until"], postgresql_where=sa.text("status = 'running'")
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_running_lease", table_name="jobs")
    op.drop_index("ix_job_items_root_job_id", table_name="job_items")
    op.drop_index("ix_job_events_item_id", table_name="job_events")
    op.drop_index("ix_job_events_job_id", table_name="job_events")
    op.drop_constraint("ck_job_items_origin", "job_items", type_="check")
    op.drop_constraint("ck_jobs_running_has_lease", "jobs", type_="check")
    op.drop_constraint("ck_jobs_counters_non_negative", "jobs", type_="check")
