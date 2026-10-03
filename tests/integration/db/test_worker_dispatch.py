from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select

from catcher.core.db import session_scope
from catcher.modules.queue.models import Job
from catcher.modules.queue.queue import claim, enqueue, reap
from catcher.modules.worker.dispatch import run_job
from catcher.modules.worker.handlers import Defer, Done, Fail, Handler, HandlerContext, HandlerResult


def _ctx(engine: Engine, clock) -> HandlerContext:
    placeholder: Any = object()  # the dispatch never looks inside settings or services
    return HandlerContext(
        settings=placeholder,
        services=placeholder,
        engine=engine,
        ideas=Path("ideas"),
        docs=Path("docs"),
        clock=clock,
    )


def _row(engine: Engine, job_id) -> Job:
    with session_scope(engine) as fresh:
        return fresh.scalars(select(Job).where(Job.id == job_id)).one()


def _claimed(engine: Engine, clock, worker: str = "w1", type: str = "note", **params: Any) -> Job:
    with session_scope(engine) as s:
        if worker == "w1":
            enqueue(s, type=type, now=clock.now, params=params)
    with session_scope(engine) as s:
        job = claim(s, worker=worker, now=clock.now, lease_s=30)
    assert job is not None
    return job


def _run(ctx: HandlerContext, job: Job, handler: Handler, type: str = "note"):
    return run_job(ctx, job, {type: handler}, lease_s=30, heartbeat_s=10)


def test_done_completes_the_job_with_its_result(pg_engine: Engine, clock) -> None:
    job = _claimed(pg_engine, clock)
    clock.advance(5)

    outcome = _run(_ctx(pg_engine, clock), job, lambda ctx, j: Done({"pages": 2}))

    assert outcome == "succeeded"
    row = _row(pg_engine, job.id)
    assert (row.status, row.result, row.finished_at, row.locked_by) == (
        "succeeded",
        {"pages": 2},
        clock.now,
        None,
    )


def test_defer_requeues_without_counting_an_attempt(pg_engine: Engine, clock) -> None:
    job = _claimed(pg_engine, clock)
    later = clock.now + timedelta(minutes=2)

    outcome = _run(_ctx(pg_engine, clock), job, lambda ctx, j: Defer(later, "youtube gap"))

    assert outcome == "deferred"
    row = _row(pg_engine, job.id)
    assert (row.status, row.run_after, row.reason, row.attempts) == ("queued", later, "youtube gap", 0)


def test_defer_requires_an_aware_run_after() -> None:
    from datetime import datetime

    with pytest.raises(ValueError):
        Defer(datetime(2026, 10, 2, 12, 0), "naive")


def test_fail_marks_the_job_failed(pg_engine: Engine, clock) -> None:
    job = _claimed(pg_engine, clock)

    outcome = _run(_ctx(pg_engine, clock), job, lambda ctx, j: Fail("bad input"))

    assert outcome == "failed"
    row = _row(pg_engine, job.id)
    assert (row.status, row.error, row.attempts) == ("failed", "bad input", 1)


def test_a_handler_exception_fails_the_job_and_does_not_propagate(
    pg_engine: Engine, clock, caplog: pytest.LogCaptureFixture
) -> None:
    job = _claimed(pg_engine, clock)

    def boom(ctx: HandlerContext, j: Job) -> HandlerResult:
        raise RuntimeError("kaput")

    with caplog.at_level("ERROR", logger="catcher.worker"):
        outcome = _run(_ctx(pg_engine, clock), job, boom)

    assert outcome == "failed"
    assert _row(pg_engine, job.id).error == "RuntimeError: kaput"
    assert any(r.exc_info for r in caplog.records if r.name == "catcher.worker")


def test_an_unknown_job_type_fails_the_job(pg_engine: Engine, clock) -> None:
    job = _claimed(pg_engine, clock, type="mystery")

    outcome = run_job(_ctx(pg_engine, clock), job, {}, lease_s=30, heartbeat_s=10)

    assert outcome == "failed"
    assert _row(pg_engine, job.id).error == "no handler for mystery"


def test_a_result_after_a_lost_lease_is_discarded(pg_engine: Engine, clock) -> None:
    stale = _claimed(pg_engine, clock, worker="w1")
    clock.advance(31)
    with session_scope(pg_engine) as s:
        reap(s, now=clock.now)
    with session_scope(pg_engine) as s:
        assert claim(s, worker="w2", now=clock.now, lease_s=30) is not None
    before = _row(pg_engine, stale.id)

    outcome = _run(_ctx(pg_engine, clock), stale, lambda ctx, j: Done({"x": 1}))

    assert outcome == "lost"
    after = _row(pg_engine, stale.id)
    assert (after.status, after.locked_by, after.result, after.claim_seq) == (
        "running",
        "w2",
        None,
        before.claim_seq,
    )


def test_keyboard_interrupt_propagates_and_leaves_the_job_running(pg_engine: Engine, clock) -> None:
    job = _claimed(pg_engine, clock)

    def interrupted(ctx: HandlerContext, j: Job) -> HandlerResult:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _run(_ctx(pg_engine, clock), job, interrupted)

    row = _row(pg_engine, job.id)
    assert (row.status, row.locked_by) == ("running", "w1")


def test_a_handler_receives_the_claimed_job_and_the_context(pg_engine: Engine, clock) -> None:
    job = _claimed(pg_engine, clock, path="a.md")
    ctx = _ctx(pg_engine, clock)
    seen: list[tuple[Any, Any, Any]] = []

    def handler(c: HandlerContext, j: Job) -> HandlerResult:
        seen.append((c, j.id, j.params))
        return Done()

    assert _run(ctx, job, handler) == "succeeded"
    assert seen == [(ctx, job.id, {"path": "a.md"})]


@pytest.mark.parametrize(("returned", "name"), [(None, "NoneType"), ({"a": 1}, "dict")])
def test_a_handler_returning_a_non_result_fails_the_job(pg_engine: Engine, clock, returned, name) -> None:
    job = _claimed(pg_engine, clock)
    handler: Any = lambda ctx, j: returned  # noqa: E731

    outcome = _run(_ctx(pg_engine, clock), job, handler)

    assert outcome == "failed"
    row = _row(pg_engine, job.id)
    assert (row.status, row.error) == ("failed", f"handler returned {name}, not a HandlerResult")
