import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from catcher.core.db import session_scope
from catcher.modules.queue.models import Job
from catcher.modules.queue.queue import enqueue


def test_enqueue_creates_a_queued_job_due_now(session: Session, clock) -> None:
    job, created = enqueue(session, type="note", now=clock.now, params={"a": 1}, priority=2, resource="llm")

    assert created is True
    assert job.status == "queued"
    assert job.attempts == 0
    assert job.max_attempts == 3
    assert job.priority == 2
    assert job.params == {"a": 1}
    assert job.resource == "llm"
    assert job.run_after == clock.now
    assert job.created_at == clock.now
    assert session.get(Job, job.id) is not None


def test_enqueue_honours_run_after(session: Session, clock) -> None:
    later = clock.now + timedelta(minutes=5)
    job, _ = enqueue(session, type="note", now=clock.now, run_after=later)
    assert job.run_after == later


def test_a_naive_now_is_rejected(session: Session) -> None:
    with pytest.raises(ValueError):
        enqueue(session, type="note", now=datetime(2026, 10, 2, 12, 0))


def test_a_second_enqueue_with_the_same_key_returns_the_first_job(session: Session, clock) -> None:
    first, created_first = enqueue(session, type="note", now=clock.now, dedupe_key="k1")
    second, created_second = enqueue(session, type="note", now=clock.now, dedupe_key="k1")

    assert created_first is True
    assert created_second is False
    assert second.id == first.id
    assert session.scalar(select(func.count()).select_from(Job)) == 1


def test_two_connections_enqueueing_the_same_key_at_once_get_one_job(
    pg_engine: Engine, session: Session
) -> None:
    barrier = threading.Barrier(2)
    results: list[tuple] = []
    errors: list[BaseException] = []
    now = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)

    def worker() -> None:
        try:
            with session_scope(pg_engine) as own:
                barrier.wait(timeout=10)
                job, created = enqueue(own, type="note", now=now, dedupe_key="race")
                results.append((job.id, created))
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert len(results) == 2
    assert results[0][0] == results[1][0]
    assert sorted(created for _, created in results) == [False, True]
    assert session.scalar(select(func.count()).select_from(Job)) == 1


def test_the_key_is_free_again_once_the_job_has_finished(session: Session, clock) -> None:
    first, _ = enqueue(session, type="note", now=clock.now, dedupe_key="k2")
    first.status = "succeeded"
    session.flush()

    second, created = enqueue(session, type="note", now=clock.now, dedupe_key="k2")

    assert created is True
    assert second.id != first.id
