import threading
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from catcher.core.db import session_scope
from catcher.modules.queue.queue import claim, enqueue


def test_claim_returns_none_when_nothing_is_due(session: Session, clock) -> None:
    assert claim(session, worker="w1", now=clock.now, lease_s=30) is None

    enqueue(session, type="note", now=clock.now, run_after=clock.now + timedelta(seconds=1))

    assert claim(session, worker="w1", now=clock.now, lease_s=30) is None


def test_a_naive_now_is_rejected(session: Session) -> None:
    with pytest.raises(ValueError):
        claim(session, worker="w1", now=datetime(2026, 10, 2, 12, 0), lease_s=30)


def test_a_job_with_a_future_run_after_is_invisible_until_its_time(session: Session, clock) -> None:
    job, _ = enqueue(session, type="note", now=clock.now, run_after=clock.now + timedelta(minutes=5))

    clock.advance(299)
    assert claim(session, worker="w1", now=clock.now, lease_s=30) is None

    clock.advance(1)
    claimed = claim(session, worker="w1", now=clock.now, lease_s=30)
    assert claimed is not None
    assert claimed.id == job.id


def test_higher_priority_goes_first_then_earlier_run_after_then_earlier_created_at(
    session: Session, clock
) -> None:
    now = clock.now
    old_low, _ = enqueue(
        session, type="note", now=now - timedelta(seconds=30), run_after=now - timedelta(seconds=10)
    )
    late_created, _ = enqueue(
        session, type="note", now=now - timedelta(seconds=5), run_after=now - timedelta(seconds=20)
    )
    early_created, _ = enqueue(
        session, type="note", now=now - timedelta(seconds=10), run_after=now - timedelta(seconds=20)
    )
    urgent, _ = enqueue(session, type="note", now=now, priority=5)

    order = []
    while (job := claim(session, worker="w1", now=now, lease_s=30)) is not None:
        order.append(job.id)

    assert order == [urgent.id, early_created.id, late_created.id, old_low.id]


def test_claim_sets_the_lease_and_counts_the_attempt(session: Session, clock) -> None:
    first_claim_at = clock.now
    enqueue(session, type="note", now=clock.now)

    job = claim(session, worker="w1", now=clock.now, lease_s=30)

    assert job is not None
    assert job.status == "running"
    assert job.locked_by == "w1"
    assert job.attempts == 1
    assert job.started_at == first_claim_at
    assert job.heartbeat_at == first_claim_at
    assert job.lease_until == first_claim_at + timedelta(seconds=30)

    # Put it back as a retry would, and claim it again later with another worker.
    job.status = "queued"
    session.flush()
    clock.advance(60)

    again = claim(session, worker="w2", now=clock.now, lease_s=12.5)

    assert again is not None
    assert again.id == job.id
    assert again.locked_by == "w2"
    assert again.attempts == 2
    assert again.started_at == first_claim_at  # the first claim only
    assert again.heartbeat_at == clock.now
    assert again.lease_until == clock.now + timedelta(seconds=12.5)

    session.expire(again)
    session.refresh(again)
    assert (again.locked_by, again.attempts, again.status) == ("w2", 2, "running")


def test_types_filter(session: Session, clock) -> None:
    note, _ = enqueue(session, type="note", now=clock.now, priority=9)
    video, _ = enqueue(session, type="youtube", now=clock.now)

    first = claim(session, worker="w1", now=clock.now, lease_s=30, types=["youtube", "clipping"])
    assert first is not None
    assert first.id == video.id
    assert claim(session, worker="w1", now=clock.now, lease_s=30, types=["youtube"]) is None
    assert claim(session, worker="w1", now=clock.now, lease_s=30, types=[]) is None

    rest = claim(session, worker="w1", now=clock.now, lease_s=30)
    assert rest is not None
    assert rest.id == note.id


def test_two_workers_claiming_at_once_never_get_the_same_job(
    pg_engine: Engine, session: Session, clock
) -> None:
    with session_scope(pg_engine) as setup:
        expected = {enqueue(setup, type="note", now=clock.now)[0].id for _ in range(10)}

    barrier = threading.Barrier(4)
    claimed: list[uuid.UUID] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        try:
            barrier.wait(timeout=10)
            while True:
                with session_scope(pg_engine) as own:
                    job = claim(own, worker=name, now=clock.now, lease_s=30)
                    if job is None:
                        return
                    with lock:
                        claimed.append(job.id)
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=worker, args=(f"w{i}",), daemon=True) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(claimed) == len(set(claimed))
    assert set(claimed) == expected


@pytest.mark.parametrize("jobs", [1, 2])
def test_a_job_locked_by_an_open_transaction_is_skipped_not_waited_for(
    pg_engine: Engine, session: Session, clock, jobs: int
) -> None:
    with session_scope(pg_engine) as setup:
        ids = [
            enqueue(setup, type="note", now=clock.now + timedelta(seconds=n), run_after=clock.now)[0].id
            for n in range(jobs)
        ]
    later = clock.now + timedelta(seconds=jobs)

    with session_scope(pg_engine) as a:
        held = claim(a, worker="a", now=later, lease_s=30)
        assert held is not None
        assert held.id == ids[0]

        with session_scope(pg_engine) as b:
            # Without SKIP LOCKED, b would wait for a; this turns that wait into an error.
            b.execute(text("set local lock_timeout = '2s'"))
            other = claim(b, worker="b", now=later, lease_s=30)
            if jobs == 1:
                assert other is None
            else:
                assert other is not None
                assert other.id == ids[1]
        # a commits here, after b.
