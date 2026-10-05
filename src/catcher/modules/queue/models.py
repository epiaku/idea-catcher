import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JOB_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")
ITEM_STATUSES = (
    "staging",
    "waiting_youtube",
    "waiting_llm",
    "ready",
    "published",
    "deferred",
    "stuck",
    "failed",
    "duplicate",
)
ITEM_ORIGINS = ("inbox", "backfill")
EVENT_LEVELS = ("info", "warning", "error")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _timestamp() -> DateTime:
    return DateTime(timezone=True)


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "pk": "pk_%(table_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        }
    )


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(_in("status", JOB_STATUSES), name="ck_jobs_status"),
        CheckConstraint("attempts >= 0 AND claim_seq >= 0", name="ck_jobs_counters_non_negative"),
        CheckConstraint("status <> 'running' OR lease_until IS NOT NULL", name="ck_jobs_running_has_lease"),
        Index(
            "uq_jobs_active_dedupe_key",
            "dedupe_key",
            unique=True,
            postgresql_where=text("dedupe_key IS NOT NULL AND status IN ('queued','running')"),
        ),
        Index(
            "ix_jobs_claim",
            text("priority DESC"),
            "run_after",
            "created_at",
            postgresql_where=text("status = 'queued'"),
        ),
        Index("ix_jobs_running_lease", "lease_until", postgresql_where=text("status = 'running'")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    type: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    run_after: Mapped[datetime] = mapped_column(_timestamp())
    reason: Mapped[str | None] = mapped_column(Text)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    # Goes up by one on every claim and never down; `(locked_by, claim_seq)` fences a claim's writes.
    claim_seq: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, server_default=text("3"))
    locked_by: Mapped[str | None] = mapped_column(Text)
    lease_until: Mapped[datetime | None] = mapped_column(_timestamp())
    heartbeat_at: Mapped[datetime | None] = mapped_column(_timestamp())
    dedupe_key: Mapped[str | None] = mapped_column(Text)
    resource: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(_timestamp())
    started_at: Mapped[datetime | None] = mapped_column(_timestamp())
    finished_at: Mapped[datetime | None] = mapped_column(_timestamp())


class JobItem(Base):
    __tablename__ = "job_items"
    __table_args__ = (
        CheckConstraint(_in("status", ITEM_STATUSES), name="ck_job_items_status"),
        CheckConstraint(_in("origin", ITEM_ORIGINS), name="ck_job_items_origin"),
        Index("ix_job_items_root_job_id", "root_job_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    calculated_name: Mapped[str] = mapped_column(Text, unique=True)  # <subfolder>/<name>.md
    doc_id: Mapped[str] = mapped_column(Text)
    doc_class: Mapped[str] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(Text, default="inbox", server_default=text("'inbox'"))
    status: Mapped[str] = mapped_column(Text)
    stage_reason: Mapped[str | None] = mapped_column(Text)
    stage_since: Mapped[datetime | None] = mapped_column(_timestamp())  # when it entered `status`
    inbox_path: Mapped[str | None] = mapped_column(Text)
    archive_path: Mapped[str | None] = mapped_column(Text)
    output_path: Mapped[str | None] = mapped_column(Text)
    failed_path: Mapped[str | None] = mapped_column(Text)
    docs_page: Mapped[str | None] = mapped_column(Text)
    original_filename: Mapped[str | None] = mapped_column(Text)
    llm_profile: Mapped[str | None] = mapped_column(Text)
    llm_backend: Mapped[str | None] = mapped_column(Text)
    llm_model: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    llm_duration_ms: Mapped[int | None] = mapped_column(Integer)
    llm_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # none_as_null: None is SQL NULL (not JSON `null`), so `warnings IS NULL` finds an item without warnings
    warnings: Mapped[list[Any] | None] = mapped_column(JSONB(none_as_null=True))
    error: Mapped[str | None] = mapped_column(Text)
    root_job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("jobs.id"))
    created_at: Mapped[datetime] = mapped_column(_timestamp())  # also "first seen"
    updated_at: Mapped[datetime] = mapped_column(_timestamp())


class JobEvent(Base):
    __tablename__ = "job_events"
    __table_args__ = (
        CheckConstraint(_in("level", EVENT_LEVELS), name="ck_job_events_level"),
        Index("ix_job_events_job_id", "job_id"),
        Index("ix_job_events_item_id", "item_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("jobs.id"))
    item_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("job_items.id"))
    ts: Mapped[datetime] = mapped_column(_timestamp())
    level: Mapped[str] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class Resource(Base):
    __tablename__ = "resources"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    next_allowed_at: Mapped[datetime | None] = mapped_column(_timestamp())
    blocked_until: Mapped[datetime | None] = mapped_column(_timestamp())
    blocked_at: Mapped[datetime | None] = mapped_column(_timestamp())
    streak: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    concurrency: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    reason: Mapped[str | None] = mapped_column(Text)  # why it is blocked
    updated_at: Mapped[datetime] = mapped_column(_timestamp())


class Schedule(Base):
    __tablename__ = "schedules"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    last_fired_at: Mapped[datetime | None] = mapped_column(_timestamp())
