"""The job queue: put work on it, and (in later steps) claim, finish and reap it."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from catcher.core.db import require_aware
from catcher.modules.queue.models import Job

# Same predicate as the partial unique index `uq_jobs_active_dedupe_key` on the model.
_ACTIVE_DEDUPE = text("dedupe_key IS NOT NULL AND status IN ('queued','running')")
_DEDUPE_ATTEMPTS = 3  # the active job can finish between our failed insert and our select


def enqueue(
    session: Session,
    *,
    type: str,
    now: datetime,
    run_after: datetime | None = None,
    priority: int = 0,
    params: dict[str, Any] | None = None,
    resource: str | None = None,
    dedupe_key: str | None = None,
    max_attempts: int = 3,
) -> tuple[Job, bool]:
    """Add a queued job. Returns `(job, created)`; with a `dedupe_key` that is already active,
    returns that active job and `created=False`. Does not commit: the caller's session_scope does."""
    require_aware(now)
    if run_after is not None:
        require_aware(run_after)
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "type": type,
        "status": "queued",
        "priority": priority,
        "run_after": run_after if run_after is not None else now,
        "params": params if params is not None else {},
        "attempts": 0,
        "max_attempts": max_attempts,
        "dedupe_key": dedupe_key,
        "resource": resource,
        "created_at": now,
    }
    if dedupe_key is None:
        job = Job(**values)
        session.add(job)
        session.flush()
        return job, True

    for _ in range(_DEDUPE_ATTEMPTS):
        statement = (
            insert(Job)
            .values(**{**values, "id": uuid.uuid4()})
            .on_conflict_do_nothing(index_elements=[Job.dedupe_key], index_where=_ACTIVE_DEDUPE)
            .returning(Job)
        )
        inserted = session.scalars(statement, execution_options={"populate_existing": True}).one_or_none()
        if inserted is not None:
            return inserted, True
        existing = session.scalars(
            select(Job).where(Job.dedupe_key == dedupe_key, Job.status.in_(("queued", "running")))
        ).one_or_none()
        if existing is not None:
            return existing, False
        # The active job finished between the insert and the select: the key is free, insert again.
    raise RuntimeError(f"could not enqueue or find an active job for dedupe key {dedupe_key!r}")
