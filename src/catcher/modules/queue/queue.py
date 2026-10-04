"""The job queue: put work on it, claim it, finish it, and reap the ones that were abandoned."""

import math
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, and_, case, exists, func, inspect, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from catcher.core.db import require_aware
from catcher.modules.queue.models import Job, Resource
from catcher.modules.youtube.gate_rules import MAX_BLOCK_HOURS

# Same predicate as the partial unique index `uq_jobs_active_dedupe_key` on the model.
_ACTIVE_DEDUPE = text("dedupe_key IS NOT NULL AND status IN ('queued','running')")
_DEDUPE_ATTEMPTS = 3  # the active job can finish between our failed insert and our select
_UNLOCKED: dict[str, Any] = {"locked_by": None, "lease_until": None}
# A resource time further ahead than this is damage, not a wait: the claim treats it as open, so a job reaches
# the gate, which repairs the row (the same 24 hours as the gate's own ceiling).
_RESOURCE_HORIZON = timedelta(hours=MAX_BLOCK_HOURS)


_RESOURCE_TIMES = (Resource.blocked_until, Resource.next_allowed_at)


def _holds(column: Any, now: datetime) -> ColumnElement[bool]:
    """A resource time that holds its jobs back at `now`: after `now`, and at most 24 hours ahead."""
    return and_(column > now, column <= now + _RESOURCE_HORIZON)


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
    `(locked_by, claim_seq)` on the returned job is the claim token for fencing later writes.
    `types` is a sequence of type names (a bare str is a TypeError); an empty sequence matches nothing.

    A job whose `resource` names a `resources` row that is closed at `now` (`blocked_until` or
    `next_allowed_at` after `now`, see `resource_closed_until`) is not claimable: it waits in the queue
    without an attempt or an event. A job without a resource, or whose resource has no row, is not affected
    (a missing row is the gate's business), and neither is one whose time is more than 24 hours ahead
    (damage, which the gate repairs once a job reaches it).
    The claim only reads the row and never locks it; the gate's own reserve holds the lock that keeps the gap.
    """
    require_aware(now)
    _check_lease(lease_s)
    if isinstance(types, str):
        raise TypeError("types must be a sequence of type names, not a str")
    closed = exists().where(
        Resource.name == Job.resource,
        or_(*(_holds(column, now) for column in _RESOURCE_TIMES)),
    )
    statement = select(Job).where(Job.status == "queued", Job.run_after <= now, ~closed)
    if types is not None:
        statement = statement.where(Job.type.in_(types))
    statement = (
        statement.order_by(Job.priority.desc(), Job.run_after, Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True, of=Job)  # the jobs row only: the claim never locks a resource
    )
    job = session.scalars(statement, execution_options={"populate_existing": True}).one_or_none()
    if job is None:
        return None
    job.status = "running"
    job.locked_by = worker
    job.attempts += 1
    job.claim_seq += 1
    if job.started_at is None:
        job.started_at = now
    job.heartbeat_at = now
    job.lease_until = now + timedelta(seconds=lease_s)
    session.flush()
    return job


def resource_closed_until(now: datetime) -> ColumnElement[datetime | None]:
    """A column for a select on `Job`: until when the claim leaves that job queued for its resource, or NULL
    when it is claimable as far as the resource goes. The later of `blocked_until` and `next_allowed_at` that
    holds the job at `now` (the claim's own rule), computed in SQL so a damaged value ('infinity') is never
    loaded into Python."""
    require_aware(now)
    times = [case((_holds(column, now), column)) for column in _RESOURCE_TIMES]
    return select(func.greatest(*times)).where(Resource.name == Job.resource).correlate(Job).scalar_subquery()


def _fenced_update(session: Session, job: Job, values: dict[str, Any], *, attempts_delta: int = 0) -> bool:
    """Write `values` only while `job` still holds its claim; refresh `job` when it did.

    The token is read from what `job` already holds, never reloaded: a reload would fetch the
    current owner's token and let a stale caller through. A `job` with unsaved changes is refused,
    and autoflush is off for the UPDATE: a flush would write those changes without the fence."""
    state = inspect(job)
    if state.modified:
        raise ValueError("the job has unsaved changes; they would be written without the fence")
    loaded = state.dict
    if not {"id", "locked_by", "claim_seq"} <= loaded.keys():
        raise ValueError("the job's claim token is not loaded; pass the Job that claim returned")
    job_id, worker, claim_seq = loaded["id"], loaded["locked_by"], loaded["claim_seq"]
    if attempts_delta:
        values = {**values, "attempts": Job.attempts + attempts_delta}
    statement = (
        update(Job)
        .where(Job.id == job_id, Job.status == "running", Job.locked_by == worker, Job.claim_seq == claim_seq)
        .values(**values)
        .returning(*Job.__table__.columns)
        .execution_options(synchronize_session=False)
    )
    with session.no_autoflush:
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
    return _fenced_update(
        session, job, {"heartbeat_at": now, "lease_until": now + timedelta(seconds=lease_s)}
    )


def complete(session: Session, job: Job, *, now: datetime, result: dict[str, Any] | None = None) -> bool:
    """Mark the caller's job succeeded. False (and nothing written) when it is no longer theirs."""
    require_aware(now)
    return _fenced_update(
        session, job, {"status": "succeeded", "finished_at": now, "result": result, **_UNLOCKED}
    )


def fail(session: Session, job: Job, *, now: datetime, error: str) -> bool:
    """Mark the caller's job failed for good (no retry). False when it is no longer theirs."""
    require_aware(now)
    return _fenced_update(session, job, {"status": "failed", "finished_at": now, "error": error, **_UNLOCKED})


def defer(session: Session, job: Job, *, now: datetime, run_after: datetime, reason: str) -> bool:
    """Put the caller's job back in the queue until `run_after`. A deferral is not a failure, so the
    attempt is given back. False when the job is no longer theirs."""
    require_aware(now)
    require_aware(run_after)
    values = {"status": "queued", "run_after": run_after, "reason": reason, **_UNLOCKED}
    return _fenced_update(session, job, values, attempts_delta=-1)


def reap(session: Session, *, now: datetime) -> list[Job]:
    """Recover jobs whose worker went silent: every running job with `lease_until < now`.

    With attempts left it goes back to `queued` (due now, `reason='lease expired'`); the claim already
    counted the attempt, and `claim_seq` stays put so the old worker's token stays dead. Out of attempts
    (a poison job that keeps killing its worker) it becomes `failed`. `heartbeat_at` is left as it was:
    it shows when the worker was last seen. Returns the jobs it changed, as they are now. Does not commit.

    A heartbeat after the lease expired but before a reap still succeeds and revives the lease: the reap
    is the authority, not the clock. Each UPDATE re-checks its WHERE after waiting for a row lock, so a
    concurrent heartbeat, complete or reaper wins cleanly. Run it at worker start and on a timer."""
    require_aware(now)
    expired = (Job.status == "running", Job.lease_until < now)
    requeue = (
        update(Job)
        .where(*expired, Job.attempts < Job.max_attempts)
        .values(status="queued", run_after=now, reason="lease expired", **_UNLOCKED)
    )
    give_up = (
        update(Job)
        .where(*expired, Job.attempts >= Job.max_attempts)
        .values(
            status="failed",
            error=func.concat("lease expired too often (", Job.attempts, " attempts)"),
            finished_at=now,
            **_UNLOCKED,
        )
    )
    changed: list[Job] = []
    with session.no_autoflush:  # a flush would write unrelated pending changes mid-reap
        for statement in (requeue, give_up):
            statement = statement.returning(Job).execution_options(
                synchronize_session=False, populate_existing=True
            )
            changed.extend(session.scalars(statement))
    return changed


def live_job_carries(session: Session, calculated_name: str, *, besides: uuid.UUID | None = None) -> bool:
    """True when a queued or running job (other than `besides`) carries the item `calculated_name` in its
    `calculated_name` param: that job owns the item and will move it on."""
    statement = select(Job.id).where(
        Job.status.in_(("queued", "running")),
        Job.params["calculated_name"].astext == calculated_name,
    )
    if besides is not None:
        statement = statement.where(Job.id != besides)
    return session.scalars(statement.limit(1)).first() is not None
