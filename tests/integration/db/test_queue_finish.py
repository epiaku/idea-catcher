from datetime import timedelta

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from catcher.core.db import session_scope
from catcher.modules.queue.models import Job
from catcher.modules.queue.queue import claim, complete, defer, enqueue, fail, heartbeat


def _row(engine: Engine, job_id) -> Job:
    """The job as the database has it now, read in a session of its own."""
    with session_scope(engine) as fresh:
        return fresh.scalars(select(Job).where(Job.id == job_id)).one()


def _claimed(session: Session, clock, worker: str = "w1", lease_s: float = 30) -> Job:
    job = claim(session, worker=worker, now=clock.now, lease_s=lease_s)
    assert job is not None
    return job


def test_complete_marks_the_job_succeeded(pg_engine: Engine, session: Session, clock) -> None:
    enqueue(session, type="note", now=clock.now)
    job = _claimed(session, clock)
    clock.advance(5)

    assert complete(session, job, now=clock.now, result={"pages": 2}) is True

    assert (job.status, job.finished_at, job.result) == ("succeeded", clock.now, {"pages": 2})
    assert (job.locked_by, job.lease_until) == (None, None)
    session.commit()
    row = _row(pg_engine, job.id)
    assert (row.status, row.finished_at, row.result) == ("succeeded", clock.now, {"pages": 2})
    assert (row.locked_by, row.lease_until, row.attempts) == (None, None, 1)


def test_fail_marks_it_failed_with_the_error_and_does_not_requeue(
    pg_engine: Engine, session: Session, clock
) -> None:
    enqueue(session, type="note", now=clock.now, max_attempts=3)
    job = _claimed(session, clock)
    clock.advance(5)

    assert fail(session, job, now=clock.now, error="boom") is True

    assert (job.status, job.error, job.finished_at) == ("failed", "boom", clock.now)
    session.commit()
    row = _row(pg_engine, job.id)
    assert (row.status, row.error, row.finished_at) == ("failed", "boom", clock.now)
    assert (row.locked_by, row.lease_until, row.attempts) == (None, None, 1)
    clock.advance(3600)
    assert claim(session, worker="w1", now=clock.now, lease_s=30) is None


def test_defer_requeues_with_a_future_run_after_and_does_not_count_an_attempt(
    pg_engine: Engine, session: Session, clock
) -> None:
    enqueue(session, type="youtube", now=clock.now)
    job = _claimed(session, clock)
    later = clock.now + timedelta(minutes=2)

    assert defer(session, job, now=clock.now, run_after=later, reason="youtube gap") is True

    assert (job.status, job.run_after, job.reason, job.attempts) == ("queued", later, "youtube gap", 0)
    assert (job.locked_by, job.lease_until) == (None, None)
    session.commit()
    row = _row(pg_engine, job.id)
    assert (row.status, row.run_after, row.reason, row.attempts) == ("queued", later, "youtube gap", 0)
    assert (row.locked_by, row.lease_until, row.finished_at) == (None, None, None)

    clock.advance(119)
    assert claim(session, worker="w1", now=clock.now, lease_s=30) is None
    clock.advance(1)
    again = _claimed(session, clock)
    assert (again.id, again.attempts) == (job.id, 1)


def test_defer_rejects_a_naive_run_after(session: Session, clock) -> None:
    enqueue(session, type="note", now=clock.now)
    job = _claimed(session, clock)

    with pytest.raises(ValueError):
        defer(session, job, now=clock.now, run_after=clock.now.replace(tzinfo=None), reason="x")


def test_heartbeat_extends_the_lease(pg_engine: Engine, session: Session, clock) -> None:
    enqueue(session, type="note", now=clock.now)
    job = _claimed(session, clock, lease_s=30)
    clock.advance(20)

    assert heartbeat(session, job, now=clock.now, lease_s=45) is True

    assert (job.heartbeat_at, job.lease_until) == (clock.now, clock.now + timedelta(seconds=45))
    session.commit()
    row = _row(pg_engine, job.id)
    assert (row.status, row.locked_by, row.attempts) == ("running", "w1", 1)
    assert (row.heartbeat_at, row.lease_until) == (clock.now, clock.now + timedelta(seconds=45))


def test_heartbeat_and_claim_reject_a_non_positive_or_nan_lease(session: Session, clock) -> None:
    enqueue(session, type="note", now=clock.now)
    for bad in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            claim(session, worker="w1", now=clock.now, lease_s=bad)
    job = _claimed(session, clock)
    for bad in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            heartbeat(session, job, now=clock.now, lease_s=bad)


def test_claim_rejects_a_bare_string_for_types(session: Session, clock) -> None:
    with pytest.raises(TypeError):
        claim(session, worker="w1", now=clock.now, lease_s=30, types="note")


def test_a_stale_worker_cannot_complete_a_job_another_worker_has_reclaimed(
    pg_engine: Engine, session: Session, clock
) -> None:
    with session_scope(pg_engine) as setup:
        job_id = enqueue(setup, type="note", now=clock.now)[0].id
    with session_scope(pg_engine) as first:
        stale = _claimed(first, clock, worker="w1", lease_s=30)
    clock.advance(31)
    # `reap` comes in a later step; put the expired job back by hand.
    with session_scope(pg_engine) as reaper:
        reaper.execute(
            text("update jobs set status = 'queued', locked_by = null, lease_until = null where id = :id"),
            {"id": job_id},
        )
    with session_scope(pg_engine) as second:
        _claimed(second, clock, worker="w2", lease_s=30)
    before = _row(pg_engine, job_id)
    stale_view = (stale.status, stale.locked_by, stale.attempts, stale.lease_until)
    clock.advance(1)

    assert heartbeat(session, stale, now=clock.now, lease_s=30) is False
    assert complete(session, stale, now=clock.now, result={"x": 1}) is False
    assert fail(session, stale, now=clock.now, error="late") is False
    assert defer(session, stale, now=clock.now, run_after=clock.now, reason="late") is False

    assert (stale.status, stale.locked_by, stale.attempts, stale.lease_until) == stale_view
    session.commit()
    after = _row(pg_engine, job_id)
    assert (after.status, after.locked_by, after.attempts) == ("running", "w2", 2)
    columns = [c.key for c in Job.__table__.columns]
    assert {c: getattr(after, c) for c in columns} == {c: getattr(before, c) for c in columns}


def test_complete_on_a_job_that_is_not_running_returns_false(
    pg_engine: Engine, session: Session, clock
) -> None:
    enqueue(session, type="note", now=clock.now)
    job = _claimed(session, clock)
    assert complete(session, job, now=clock.now) is True

    assert complete(session, job, now=clock.now, result={"again": True}) is False
    assert fail(session, job, now=clock.now, error="late") is False

    session.commit()
    row = _row(pg_engine, job.id)
    assert (row.status, row.result, row.error) == ("succeeded", None, None)


def test_after_a_defer_the_deferred_job_object_cannot_finish_the_next_claim(
    pg_engine: Engine, session: Session, clock
) -> None:
    with session_scope(pg_engine) as setup:
        job_id = enqueue(setup, type="note", now=clock.now)[0].id
    with session_scope(pg_engine) as first:
        old = _claimed(first, clock, worker="w1")
        assert defer(first, old, now=clock.now, run_after=clock.now, reason="later") is True
    with session_scope(pg_engine) as second:
        new = _claimed(second, clock, worker="w1")
    assert (new.locked_by, new.attempts, new.claim_seq) == ("w1", 1, 2)

    # `defer` refreshed `old` to the queued state, so its token no longer matches the new claim.
    assert (old.locked_by, old.claim_seq) == (None, 1)
    assert complete(session, old, now=clock.now) is False
    session.commit()
    assert _row(pg_engine, job_id).status == "running"


def test_a_copy_of_the_first_claim_cannot_finish_the_same_workers_next_claim(
    pg_engine: Engine, session: Session, clock
) -> None:
    with session_scope(pg_engine) as setup:
        job_id = enqueue(setup, type="note", now=clock.now)[0].id
    with session_scope(pg_engine) as first:
        old = _claimed(first, clock, worker="w1")
    assert (old.locked_by, old.attempts, old.claim_seq) == ("w1", 1, 1)
    with session_scope(pg_engine) as owner:
        current = owner.get(Job, job_id)
        assert current is not None
        assert defer(owner, current, now=clock.now, run_after=clock.now, reason="later") is True
        assert (current.attempts, current.claim_seq) == (0, 1)  # the attempt is given back, the claim is not
    with session_scope(pg_engine) as second:
        new = _claimed(second, clock, worker="w1")
    # Same worker and same attempts as the first claim: only claim_seq tells the two claims apart.
    assert (new.locked_by, new.attempts, new.claim_seq) == ("w1", 1, 2)
    before = _row(pg_engine, job_id)
    old_view = (old.status, old.locked_by, old.attempts, old.claim_seq, old.lease_until)

    assert heartbeat(session, old, now=clock.now, lease_s=30) is False
    assert complete(session, old, now=clock.now) is False
    assert fail(session, old, now=clock.now, error="late") is False
    assert defer(session, old, now=clock.now, run_after=clock.now, reason="late") is False

    assert (old.status, old.locked_by, old.attempts, old.claim_seq, old.lease_until) == old_view
    session.commit()
    after = _row(pg_engine, job_id)
    assert (after.status, after.locked_by, after.attempts, after.claim_seq) == ("running", "w1", 1, 2)
    columns = [c.key for c in Job.__table__.columns]
    assert {c: getattr(after, c) for c in columns} == {c: getattr(before, c) for c in columns}


def test_claim_seq_goes_up_by_one_per_claim(session: Session, clock) -> None:
    job, _ = enqueue(session, type="note", now=clock.now)
    assert job.claim_seq == 0
    for expected in (1, 2, 3):
        claimed = _claimed(session, clock)
        assert claimed.claim_seq == expected
        assert defer(session, claimed, now=clock.now, run_after=clock.now, reason="again") is True
        assert claimed.claim_seq == expected


def test_an_expired_job_is_refused_rather_than_reloaded_with_the_current_owners_token(
    session: Session, clock
) -> None:
    enqueue(session, type="note", now=clock.now)
    job = _claimed(session, clock)
    session.expire(job)

    with pytest.raises(ValueError):
        complete(session, job, now=clock.now)
