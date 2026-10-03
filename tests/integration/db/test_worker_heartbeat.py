"""The heartbeat thread: real Postgres and the real clock, with a short lease and interval."""

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select

from catcher.core.db import session_scope, utc_now
from catcher.modules.queue import queue
from catcher.modules.queue.models import Job
from catcher.modules.queue.queue import claim, enqueue, reap
from catcher.modules.worker import dispatch, heartbeat
from catcher.modules.worker.dispatch import run_job
from catcher.modules.worker.handlers import Done, HandlerContext, HandlerResult
from catcher.modules.worker.heartbeat import Heartbeat

LEASE_S = 0.6
INTERVAL_S = 0.1
WAIT_S = 10  # generous: a deterministic wait that only times out when something is broken


@pytest.fixture(autouse=True)
def thread_errors(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[BaseException]]:
    """Collect exceptions that escape any thread; a test passes only when there are none."""
    errors: list[BaseException] = []

    def hook(args: threading.ExceptHookArgs) -> None:
        errors.append(args.exc_value if args.exc_value is not None else RuntimeError(str(args.exc_type)))

    monkeypatch.setattr(threading, "excepthook", hook)
    yield errors
    assert errors == []


def _row(engine: Engine, job_id) -> Job:
    with session_scope(engine) as fresh:
        return fresh.scalars(select(Job).where(Job.id == job_id)).one()


def _claimed(engine: Engine, worker: str = "w1", lease_s: float = LEASE_S) -> Job:
    with session_scope(engine) as s:
        enqueue(s, type="note", now=utc_now())
    with session_scope(engine) as s:
        job = claim(s, worker=worker, now=utc_now(), lease_s=lease_s)
    assert job is not None
    return job


def _take_over(engine: Engine, worker: str = "w2") -> None:
    """Expire every lease, reap, and let another worker claim the job: the old token is dead."""
    later = utc_now() + timedelta(hours=1)
    with session_scope(engine) as s:
        reap(s, now=later)
    with session_scope(engine) as s:
        assert claim(s, worker=worker, now=later, lease_s=30) is not None


def _beat(engine: Engine, job: Job) -> Heartbeat:
    return Heartbeat(engine, job, lease_s=LEASE_S, interval_s=INTERVAL_S, clock=utc_now)


def test_a_long_handler_keeps_its_lease_alive(pg_engine: Engine) -> None:
    job = _claimed(pg_engine)
    first_lease = _row(pg_engine, job.id).lease_until
    assert first_lease is not None

    with _beat(pg_engine, job) as beat:
        time.sleep(LEASE_S * 2.5)  # the handler runs well past the original lease

    now = utc_now()
    row = _row(pg_engine, job.id)
    assert not beat.lost
    assert row.lease_until is not None and row.lease_until > now > first_lease
    assert (row.status, row.locked_by, row.claim_seq) == ("running", "w1", job.claim_seq)
    with session_scope(pg_engine) as s:
        assert reap(s, now=now) == []


def test_the_thread_stops_with_the_context(pg_engine: Engine) -> None:
    job = _claimed(pg_engine)

    with _beat(pg_engine, job) as beat:
        assert beat.thread.is_alive()
        assert beat.thread.daemon
    assert not beat.thread.is_alive()

    seen = _row(pg_engine, job.id).heartbeat_at
    time.sleep(INTERVAL_S * 4)
    assert _row(pg_engine, job.id).heartbeat_at == seen  # nobody beats any more


def test_the_heartbeat_never_touches_the_handlers_job(pg_engine: Engine) -> None:
    job = _claimed(pg_engine)
    before = dict(vars(job))

    with _beat(pg_engine, job):
        time.sleep(INTERVAL_S * 4)

    assert _row(pg_engine, job.id).heartbeat_at != job.heartbeat_at  # the database moved on ...
    assert dict(vars(job)) == before  # ... the handler's Job did not


def test_a_lost_lease_sets_lost(pg_engine: Engine) -> None:
    job = _claimed(pg_engine)

    with _beat(pg_engine, job) as beat:
        _take_over(pg_engine)
        beat.thread.join(WAIT_S)  # the thread stops by itself at its next beat
        assert not beat.thread.is_alive()
        assert beat.lost

    row = _row(pg_engine, job.id)
    assert (row.status, row.locked_by) == ("running", "w2")


def test_a_failing_beat_is_logged_and_the_thread_keeps_trying(
    pg_engine: Engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    job = _claimed(pg_engine)
    calls: list[int] = []
    recovered = threading.Event()

    @contextmanager
    def flaky_scope(engine: Engine):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("database hiccup")
        with session_scope(engine) as s:
            yield s
        if len(calls) >= 3:
            recovered.set()

    monkeypatch.setattr(heartbeat, "session_scope", flaky_scope)

    with caplog.at_level("WARNING", logger="catcher.worker"), _beat(pg_engine, job) as beat:
        assert recovered.wait(WAIT_S)
        assert beat.thread.is_alive()

    assert not beat.lost
    failures = [r for r in caplog.records if r.name == "catcher.worker" and r.exc_info]
    assert len(failures) == 1
    assert failures[0].levelname == "WARNING"
    assert "database hiccup" in failures[0].getMessage() + str(failures[0].exc_text or "")
    row = _row(pg_engine, job.id)
    assert row.lease_until is not None and row.lease_until > utc_now()


def test_a_beat_only_extends_the_lease_of_the_job_it_belongs_to(pg_engine: Engine) -> None:
    other = _claimed(pg_engine, worker="w1", lease_s=30)
    job = _claimed(pg_engine)
    other_before = _row(pg_engine, other.id)

    with _beat(pg_engine, job):
        time.sleep(INTERVAL_S * 4)

    other_after = _row(pg_engine, other.id)
    assert (other_after.heartbeat_at, other_after.lease_until) == (
        other_before.heartbeat_at,
        other_before.lease_until,
    )
    assert _row(pg_engine, job.id).heartbeat_at != job.heartbeat_at


@pytest.mark.parametrize(("lease_s", "interval_s"), [(0, 0.1), (-1, 0.1), (0.6, 0), (0.6, -0.1)])
def test_lease_and_interval_must_be_above_zero(pg_engine: Engine, lease_s: float, interval_s: float) -> None:
    job = _claimed(pg_engine)
    with pytest.raises(ValueError):
        Heartbeat(pg_engine, job, lease_s=lease_s, interval_s=interval_s, clock=utc_now)


def _ctx(engine: Engine) -> HandlerContext:
    placeholder: Any = object()
    return HandlerContext(
        settings=placeholder,
        services=placeholder,
        engine=engine,
        ideas=Path("ideas"),
        docs=Path("docs"),
        clock=utc_now,
    )


def test_run_job_returns_lost_when_the_lease_was_lost_while_the_handler_ran(
    pg_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _claimed(pg_engine)
    beats: list[Heartbeat] = []

    class Recorded(Heartbeat):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            beats.append(self)

    finished: list[str] = []
    for name in ("complete", "defer", "fail"):
        real = getattr(queue, name)

        def spy(*args: Any, _real=real, _name=name, **kwargs: Any) -> bool:
            finished.append(_name)
            return _real(*args, **kwargs)

        monkeypatch.setattr(queue, name, spy)
    monkeypatch.setattr(dispatch, "Heartbeat", Recorded)

    def handler(ctx: HandlerContext, j: Job) -> HandlerResult:
        _take_over(pg_engine)
        beats[0].thread.join(WAIT_S)  # wait until the heartbeat has seen the lease go
        return Done({"x": 1})

    outcome = run_job(_ctx(pg_engine), job, {"note": handler}, lease_s=LEASE_S, heartbeat_s=INTERVAL_S)

    assert outcome == "lost"
    assert beats[0].lost
    assert finished == []  # a lost job is not finished at all
    row = _row(pg_engine, job.id)
    assert (row.status, row.locked_by, row.result) == ("running", "w2", None)
