import threading
import time
from datetime import timedelta

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from catcher.core.db import session_scope
from catcher.modules.queue.models import Job
from catcher.modules.queue.queue import claim, complete, defer, enqueue, fail, heartbeat, reap


@pytest.fixture(autouse=True)
def _empty_queue(pg_engine: Engine) -> None:
    """Other test files leave committed jobs behind; `reap` acts on every expired job, so start clean."""
    with session_scope(pg_engine) as cleanup:
        cleanup.execute(text("truncate jobs restart identity cascade"))


def _row(engine: Engine, job_id) -> Job:
    """The job as the database has it now, read in a session of its own."""
    with session_scope(engine) as fresh:
        return fresh.scalars(select(Job).where(Job.id == job_id)).one()


def _claimed(session: Session, clock, worker: str = "w1", lease_s: float = 30) -> Job:
    job = claim(session, worker=worker, now=clock.now, lease_s=lease_s)
    assert job is not None
    return job


def test_a_job_whose_lease_has_expired_comes_back_to_the_queue(
    pg_engine: Engine, session: Session, clock
) -> None:
    enqueue(session, type="note", now=clock.now, max_attempts=3)
    job = _claimed(session, clock, lease_s=30)
    claimed_at = clock.now
    clock.advance(31)

    reaped = reap(session, now=clock.now)

    assert reaped == [job]
    assert (job.status, job.run_after, job.reason) == ("queued", clock.now, "lease expired")
    assert (job.locked_by, job.lease_until, job.attempts, job.claim_seq) == (None, None, 1, 1)
    session.commit()
    row = _row(pg_engine, job.id)
    assert (row.status, row.run_after, row.reason) == ("queued", clock.now, "lease expired")
    assert (row.locked_by, row.lease_until, row.attempts, row.claim_seq) == (None, None, 1, 1)
    assert (row.heartbeat_at, row.finished_at, row.error) == (claimed_at, None, None)
    assert _claimed(session, clock, worker="w2").id == job.id


def test_a_job_with_a_live_lease_is_left_alone(pg_engine: Engine, session: Session, clock) -> None:
    enqueue(session, type="note", now=clock.now)
    job = _claimed(session, clock, lease_s=30)
    clock.advance(20)
    assert heartbeat(session, job, now=clock.now, lease_s=30) is True
    clock.advance(20)  # 40 s after the claim, but only 20 s after the heartbeat

    assert reap(session, now=clock.now) == []

    session.commit()
    row = _row(pg_engine, job.id)
    assert (row.status, row.locked_by, row.attempts) == ("running", "w1", 1)
    assert row.lease_until == clock.now + timedelta(seconds=10)


def test_reap_changes_nothing_when_no_lease_has_expired(pg_engine: Engine, session: Session, clock) -> None:
    enqueue(session, type="note", now=clock.now)
    enqueue(session, type="note", now=clock.now, run_after=clock.now + timedelta(hours=1))
    running = _claimed(session, clock, lease_s=30)
    clock.advance(30)  # lease_until == now is not yet expired
    with session_scope(pg_engine) as done:
        enqueue(done, type="note", now=clock.now)
        finished = _claimed(done, clock, worker="w9")
        assert complete(done, finished, now=clock.now) is True
    session.commit()
    columns = [c.key for c in Job.__table__.columns]

    def snapshot() -> list[dict]:
        with session_scope(pg_engine) as fresh:
            jobs = fresh.scalars(select(Job).order_by(Job.created_at, Job.id)).all()
            return [{c: getattr(job, c) for c in columns} for job in jobs]

    before = snapshot()
    assert reap(session, now=clock.now) == []
    session.commit()

    assert snapshot() == before
    assert _row(pg_engine, running.id).status == "running"


def test_reap_requires_an_aware_now(session: Session, clock) -> None:
    with pytest.raises(ValueError):
        reap(session, now=clock.now.replace(tzinfo=None))


def test_a_poison_job_ends_failed_after_max_attempts(pg_engine: Engine, session: Session, clock) -> None:
    enqueue(session, type="note", now=clock.now, max_attempts=3)
    session.commit()

    for attempt in (1, 2):
        job = _claimed(session, clock, lease_s=30)
        assert job.attempts == attempt
        clock.advance(31)
        assert reap(session, now=clock.now) == [job]
        assert (job.status, job.reason, job.attempts) == ("queued", "lease expired", attempt)
        session.commit()

    job = _claimed(session, clock, lease_s=30)
    clock.advance(31)
    assert reap(session, now=clock.now) == [job]

    assert (job.status, job.error, job.finished_at) == (
        "failed",
        "lease expired too often (3 attempts)",
        clock.now,
    )
    assert (job.locked_by, job.lease_until, job.attempts, job.claim_seq) == (None, None, 3, 3)
    session.commit()
    row = _row(pg_engine, job.id)
    assert (row.status, row.error, row.finished_at) == (
        "failed",
        "lease expired too often (3 attempts)",
        clock.now,
    )
    clock.advance(3600)
    assert claim(session, worker="w1", now=clock.now, lease_s=30) is None


def test_the_reaped_job_is_claimable_by_another_worker_and_the_old_worker_is_refused(
    pg_engine: Engine, session: Session, clock
) -> None:
    with session_scope(pg_engine) as setup:
        job_id = enqueue(setup, type="note", now=clock.now)[0].id
    with session_scope(pg_engine) as first:
        stale = _claimed(first, clock, worker="w1", lease_s=30)
    clock.advance(31)
    with session_scope(pg_engine) as reaper:
        assert [j.id for j in reap(reaper, now=clock.now)] == [job_id]
    with session_scope(pg_engine) as second:
        taken = _claimed(second, clock, worker="w2", lease_s=30)
    assert (taken.locked_by, taken.attempts, taken.claim_seq) == ("w2", 2, 2)
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
    columns = [c.key for c in Job.__table__.columns]
    assert {c: getattr(after, c) for c in columns} == {c: getattr(before, c) for c in columns}
    assert (after.status, after.locked_by) == ("running", "w2")


def test_a_heartbeat_that_extends_the_lease_wins_over_a_reaper_that_is_waiting_for_the_row(
    pg_engine: Engine, clock
) -> None:
    with session_scope(pg_engine) as setup:
        job_id = enqueue(setup, type="note", now=clock.now)[0].id
    with session_scope(pg_engine) as first:
        _claimed(first, clock, lease_s=30)
    clock.advance(31)  # the lease has expired, but the worker has not been reaped yet
    reaped: list[list[Job]] = []
    errors: list[BaseException] = []

    def run_reaper(reap_now) -> None:
        try:
            with session_scope(pg_engine) as reaper_session:
                reaper_session.execute(text("set local lock_timeout = '10s'"))
                reaped.append(reap(reaper_session, now=reap_now))
        except BaseException as error:  # reported on the test thread
            errors.append(error)

    with session_scope(pg_engine) as worker_session:
        # The worker's late heartbeat revives the lease, and holds the row lock until it commits.
        mine = worker_session.get(Job, job_id)
        assert mine is not None
        assert heartbeat(worker_session, mine, now=clock.now, lease_s=30) is True
        reaper_thread = threading.Thread(target=run_reaper, args=(clock.now,))
        reaper_thread.start()
        deadline = time.monotonic() + 10
        with pg_engine.connect() as watcher:
            while time.monotonic() < deadline:
                waiting = watcher.execute(
                    text("select count(*) from pg_stat_activity where wait_event_type = 'Lock'")
                ).scalar_one()
                if waiting:
                    break
                time.sleep(0.02)
            else:
                pytest.fail("the reaper never started waiting for the row lock")
    reaper_thread.join(timeout=15)

    assert not errors
    assert reaped == [[]]  # the WHERE was checked again after the wait: the lease is live
    row = _row(pg_engine, job_id)
    assert (row.status, row.locked_by, row.attempts) == ("running", "w1", 1)
    assert row.lease_until == clock.now + timedelta(seconds=30)
