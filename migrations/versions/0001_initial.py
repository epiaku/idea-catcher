"""The Stage B schema: jobs, job_items, job_events, resources, schedules.

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def _ts() -> sa.DateTime:
    return sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("priority", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("run_after", _ts(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("params", postgresql.JSONB(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("3"), nullable=False),
        sa.Column("locked_by", sa.Text(), nullable=True),
        sa.Column("lease_until", _ts(), nullable=True),
        sa.Column("heartbeat_at", _ts(), nullable=True),
        sa.Column("dedupe_key", sa.Text(), nullable=True),
        sa.Column("resource", sa.Text(), nullable=True),
        sa.Column("created_at", _ts(), nullable=False),
        sa.Column("started_at", _ts(), nullable=True),
        sa.Column("finished_at", _ts(), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')", name="ck_jobs_status"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_jobs"),
    )
    op.create_index(
        "uq_jobs_active_dedupe_key",
        "jobs",
        ["dedupe_key"],
        unique=True,
        postgresql_where=sa.text("dedupe_key IS NOT NULL AND status IN ('queued','running')"),
    )
    op.create_index(
        "ix_jobs_claim",
        "jobs",
        [sa.text("priority DESC"), "run_after", "created_at"],
        postgresql_where=sa.text("status = 'queued'"),
    )

    op.create_table(
        "job_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("calculated_name", sa.Text(), nullable=False),
        sa.Column("doc_id", sa.Text(), nullable=False),
        sa.Column("doc_class", sa.Text(), nullable=False),
        sa.Column("origin", sa.Text(), server_default=sa.text("'inbox'"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("stage_reason", sa.Text(), nullable=True),
        sa.Column("inbox_path", sa.Text(), nullable=True),
        sa.Column("archive_path", sa.Text(), nullable=True),
        sa.Column("output_path", sa.Text(), nullable=True),
        sa.Column("failed_path", sa.Text(), nullable=True),
        sa.Column("docs_page", sa.Text(), nullable=True),
        sa.Column("original_filename", sa.Text(), nullable=True),
        sa.Column("llm_profile", sa.Text(), nullable=True),
        sa.Column("llm_backend", sa.Text(), nullable=True),
        sa.Column("llm_model", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("llm_duration_ms", sa.Integer(), nullable=True),
        sa.Column("llm_result", postgresql.JSONB(), nullable=True),
        sa.Column("warnings", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("root_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", _ts(), nullable=False),
        sa.Column("updated_at", _ts(), nullable=False),
        sa.CheckConstraint(
            "status IN ('staging', 'waiting_youtube', 'waiting_llm', 'ready', 'published', 'deferred',"
            " 'stuck', 'failed', 'duplicate')",
            name="ck_job_items_status",
        ),
        sa.ForeignKeyConstraint(["root_job_id"], ["jobs.id"], name="fk_job_items_root_job_id_jobs"),
        sa.PrimaryKeyConstraint("id", name="pk_job_items"),
        sa.UniqueConstraint("calculated_name", name="uq_job_items_calculated_name"),
    )

    op.create_table(
        "job_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ts", _ts(), nullable=False),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=True),
        sa.CheckConstraint("level IN ('info', 'warning', 'error')", name="ck_job_events_level"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], name="fk_job_events_job_id_jobs"),
        sa.ForeignKeyConstraint(["item_id"], ["job_items.id"], name="fk_job_events_item_id_job_items"),
        sa.PrimaryKeyConstraint("id", name="pk_job_events"),
    )

    op.create_table(
        "resources",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("next_allowed_at", _ts(), nullable=True),
        sa.Column("blocked_until", _ts(), nullable=True),
        sa.Column("blocked_at", _ts(), nullable=True),
        sa.Column("streak", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("concurrency", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("updated_at", _ts(), nullable=False),
        sa.PrimaryKeyConstraint("name", name="pk_resources"),
    )

    op.create_table(
        "schedules",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("last_fired_at", _ts(), nullable=True),
        sa.PrimaryKeyConstraint("name", name="pk_schedules"),
    )


def downgrade() -> None:
    op.drop_table("schedules")
    op.drop_table("resources")
    op.drop_table("job_events")
    op.drop_table("job_items")
    op.drop_index("ix_jobs_claim", table_name="jobs")
    op.drop_index("uq_jobs_active_dedupe_key", table_name="jobs")
    op.drop_table("jobs")
