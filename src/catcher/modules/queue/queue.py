"""The job queue: put work on it, claim it, finish it, and (in a later step) reap it."""

import math
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import inspect, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from catcher.core.db import require_aware
from catcher.modules.queue.models import Job

# Same predicate as the partial unique index `uq_jobs_active_dedupe_key` on the model.
_ACTIVE_DEDUPE = text("dedupe_key IS NOT NULL AND status IN ('queued','running')")
_DEDUPE_ATTEMPTS = 3  # the active job can finish between our failed insert and our select
_UNLOCKED: dict[str, Any] = {"locked_by": None, "lease_until": None}


def _check_lease(lease_s: float) -> None:
    if not (math.isfinite(lease_s) and lease_s > 0):
        raise ValueError(f"lease_s must be a finite number above 0, not {lease_s!r}")


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


def claim(
    session: Session,
    *,
    worker: str,
    now: datetime,
    lease_s: float,
    types: Sequence[str] | None = None,
) -> Job | None:
    """Take the most urgent due job and mark it running for `worker`, or return None.

    Order: priority (high first), then run_after, then created_at. Rows other workers have locked
    are skipped, not waited for. The row stays locked until the caller commits, so commit promptly.
    `(locked_by, attempts)` on the returned job is the attempt token for fencing later writes.
    `types` is a sequence of type names (a bare str is a TypeError); an empty sequence matches nothing."""
    require_aware(now)
    _check_lease(lease_s)
    if isinstance(types, str):
        raise TypeError("types must be a sequence of type names, not a str")
    statement = select(Job).where(Job.status == "queued", Job.run_after <= now)
    if types is not None:
        statement = statement.where(Job.type.in_(types))
    statement = (
        statement.order_by(Job.priority.desc(), Job.run_after, Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = session.scalars(statement, execution_options={"populate_existing": True}).one_or_none()
    if job is None:
        return None
    job.status = "running"
    job.locked_by = worker
    job.attempts += 1
    if job.started_at is None:
        job.started_at = now
    job.heartbeat_at = now
    job.lease_until = now + timedelta(seconds=lease_s)
    session.flush()
    return job


def _finish(session: Session, job: Job, values: dict[str, Any], *, attempts_delta: int = 0) -> bool:
    """Write `values` only while `job` still holds its claim; refresh `job` when it did.

    The token is read from what `job` already holds, never reloaded: a reload would fetch the
    current owner's token and let a stale caller through."""
    loaded = inspect(job).dict
    if not {"id", "locked_by", "attempts"} <= loaded.keys():
        raise ValueError("the job's claim token is not loaded; pass the Job that claim returned")
    job_id, worker, attempts = loaded["id"], loaded["locked_by"], loaded["attempts"]
    statement = (
        update(Job)
        .where(Job.id == job_id, Job.status == "running", Job.locked_by == worker, Job.attempts == attempts)
        .values(**values, attempts=attempts + attempts_delta)
        .returning(*Job.__table__.columns)
        .execution_options(synchronize_session=False)
    )
    row = session.execute(statement).one_or_none()
    if row is None:
        return False
    for key, value in row._mapping.items():
        set_committed_value(job, key, value)
    return True


def heartbeat(session: Session, job: Job, *, now: datetime, lease_s: float) -> bool:
    """Extend the lease of a job the caller still owns. False when it is no longer theirs."""
    require_aware(now)
    _check_lease(lease_s)
    return _finish(session, job, {"heartbeat_at": now, "lease_until": now + timedelta(seconds=lease_s)})


def complete(session: Session, job: Job, *, now: datetime, result: dict[str, Any] | None = None) -> bool:
    """Mark the caller's job succeeded. False (and nothing written) when it is no longer theirs."""
    require_aware(now)
    return _finish(session, job, {"status": "succeeded", "finished_at": now, "result": result, **_UNLOCKED})


def fail(session: Session, job: Job, *, now: datetime, error: str) -> bool:
    """Mark the caller's job failed for good (no retry). False when it is no longer theirs."""
    require_aware(now)
    return _finish(session, job, {"status": "failed", "finished_at": now, "error": error, **_UNLOCKED})


def defer(session: Session, job: Job, *, now: datetime, run_after: datetime, reason: str) -> bool:
    """Put the caller's job back in the queue until `run_after`. A deferral is not a failure, so the
    attempt is given back. False when the job is no longer theirs."""
    require_aware(now)
    require_aware(run_after)
    values = {"status": "queued", "run_after": run_after, "reason": reason, **_UNLOCKED}
    return _finish(session, job, values, attempts_delta=-1)
