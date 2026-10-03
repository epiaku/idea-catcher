"""The worker loop: claim, run, finish, reap. Real Postgres and a frozen clock."""

import threading
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, inspect, select, text
from sqlalchemy.exc import OperationalError
from worker_harness import NOTES, YOUTUBE_GAP_S

from catcher.core.db import make_worker_engine, session_scope
from catcher.modules.queue import queue
from catcher.modules.queue.items import get_item, set_item_status, stage_item
from catcher.modules.queue.models import Job, JobItem
from catcher.modules.queue.queue import claim, enqueue
from catcher.modules.worker import handlers_pipeline, loop
from catcher.modules.worker.handlers import Defer, Done, Handler, HandlerContext, HandlerResult
from catcher.modules.worker.handlers_pipeline import handle_llm_reason, handle_youtube_fetch
from catcher.modules.worker.loop import Worker

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


def _ctx(engine: Engine, clock) -> HandlerContext:
    placeholder: Any = object()  # the loop never looks inside settings or services
    return HandlerContext(
        settings=placeholder,
        services=placeholder,
        engine=engine,
        ideas=Path("ideas"),
        docs=Path("docs"),
        clock=clock,
    )


def _worker(engine: Engine, clock, handlers: dict[str, Handler], **kwargs: Any) -> Worker:
    return Worker(_ctx(engine, clock), handlers, worker_id="w1", **kwargs)


def _enqueue(engine: Engine, clock, type: str = "note", priority: int = 0, **params: Any):
    with session_scope(engine) as s:
        job, _ = enqueue(s, type=type, now=clock.now, priority=priority, params=params)
        return job.id


def _row(engine: Engine, job_id) -> Job:
    with session_scope(engine) as fresh:
        return fresh.scalars(select(Job).where(Job.id == job_id)).one()


def _stop_when_idle(stop: threading.Event):
    """An injected sleep: the first idle poll ends the loop, so run_forever drains the due jobs."""

    def sleep(seconds: float) -> None:
        stop.set()

    return sleep


def test_run_once_returns_none_when_idle(pg_engine: Engine, clock) -> None:
    assert _worker(pg_engine, clock, {}).run_once() is None


def test_run_once_runs_one_job_per_call_in_priority_order(pg_engine: Engine, clock) -> None:
    low = _enqueue(pg_engine, clock, name="low", priority=0)
    high = _enqueue(pg_engine, clock, name="high", priority=5)
    seen: list[str] = []

    def handler(ctx: HandlerContext, job: Job) -> HandlerResult:
        seen.append(job.params["name"])
        return Done()

    worker = _worker(pg_engine, clock, {"note": handler})

    assert worker.run_once() == "succeeded"
    assert seen == ["high"]
    assert worker.run_once() == "succeeded"
    assert seen == ["high", "low"]
    assert worker.run_once() is None
    assert {_row(pg_engine, high).status, _row(pg_engine, low).status} == {"succeeded"}


def test_a_failing_handler_does_not_stop_the_worker(pg_engine: Engine, clock) -> None:
    first = _enqueue(pg_engine, clock, name="bad", priority=1)
    second = _enqueue(pg_engine, clock, name="good")

    def handler(ctx: HandlerContext, job: Job) -> HandlerResult:
        if job.params["name"] == "bad":
            raise RuntimeError("kaput")
        return Done()

    stop = threading.Event()
    _worker(pg_engine, clock, {"note": handler}, sleep=_stop_when_idle(stop)).run_forever(stop)

    assert (_row(pg_engine, first).status, _row(pg_engine, first).error) == ("failed", "RuntimeError: kaput")
    assert _row(pg_engine, second).status == "succeeded"


def test_a_deferred_job_is_not_picked_up_again_before_its_time(pg_engine: Engine, clock) -> None:
    job_id = _enqueue(pg_engine, clock)
    calls: list[int] = []

    def handler(ctx: HandlerContext, job: Job) -> HandlerResult:
        calls.append(1)
        if len(calls) == 1:
            return Defer(ctx.clock() + timedelta(seconds=60), "not yet")
        return Done()

    worker = _worker(pg_engine, clock, {"note": handler})

    assert worker.run_once() == "deferred"
    assert worker.run_once() is None
    clock.advance(59)
    assert worker.run_once() is None
    clock.advance(1)
    assert worker.run_once() == "succeeded"
    assert len(calls) == 2
    assert _row(pg_engine, job_id).status == "succeeded"


def test_the_reaper_requeues_a_crashed_job_at_start(pg_engine: Engine, clock) -> None:
    job_id = _enqueue(pg_engine, clock)
    with session_scope(pg_engine) as s:
        assert claim(s, worker="dead-worker", now=clock.now, lease_s=30) is not None
    clock.advance(31)  # the dead worker's lease expired
    stop = threading.Event()
    ran: list[Any] = []

    def handler(ctx: HandlerContext, job: Job) -> HandlerResult:
        ran.append(job.id)
        stop.set()
        return Done()

    _worker(pg_engine, clock, {"note": handler}, sleep=_stop_when_idle(stop)).run_forever(stop)

    assert ran == [job_id]
    row = _row(pg_engine, job_id)
    assert (row.status, row.attempts) == ("succeeded", 2)


def test_reap_safely_survives_a_database_error(
    pg_engine: Engine, clock, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    real_reap = queue.reap
    reaps: list[int] = []

    def flaky_reap(session, *, now):
        reaps.append(1)
        if len(reaps) == 1:
            raise RuntimeError("database gone")
        return real_reap(session, now=now)

    monkeypatch.setattr(queue, "reap", flaky_reap)
    job_id = _enqueue(pg_engine, clock)
    stop = threading.Event()
    worker = _worker(
        pg_engine, clock, {"note": lambda ctx, j: Done()}, reap_every_s=1e-9, sleep=_stop_when_idle(stop)
    )

    with caplog.at_level("ERROR", logger="catcher.worker"):
        worker.run_forever(stop)

    assert _row(pg_engine, job_id).status == "succeeded"
    assert len(reaps) >= 2  # the timer reaped again after the first one failed
    assert any(r.exc_info for r in caplog.records if r.name == "catcher.worker")
    assert worker.reap_safely() == 0


def test_reap_safely_returns_the_number_of_reaped_jobs(pg_engine: Engine, clock) -> None:
    _enqueue(pg_engine, clock)
    with session_scope(pg_engine) as s:
        assert claim(s, worker="dead-worker", now=clock.now, lease_s=30) is not None
    worker = _worker(pg_engine, clock, {})

    assert worker.reap_safely() == 0
    clock.advance(31)
    assert worker.reap_safely() == 1


def _item(engine: Engine, clock, name: str, status: str) -> None:
    with session_scope(engine) as s:
        stage_item(
            s,
            calculated_name=name,
            doc_id=name,
            doc_class="note",
            now=clock.now,
            inbox_path=None,
            original_filename=None,
        )
        set_item_status(s, name, status, now=clock.now)


def _item_row(engine: Engine, name: str) -> JobItem:
    with session_scope(engine) as fresh:
        item = get_item(fresh, name)
        assert item is not None
        return item


def _abandoned(
    engine: Engine, clock, name: str | None, *, max_attempts: int, type: str = "llm.reason"
) -> Any:
    """A job of item `name` (`type`, `llm.reason` by default; no `calculated_name` param when `name` is None)
    whose worker died: claimed, then its lease expired."""
    with session_scope(engine) as s:
        job, _ = enqueue(
            s,
            type=type,
            now=clock.now,
            params={"calculated_name": name} if name is not None else {},
            dedupe_key=f"{type}:{name}" if name is not None else None,
            max_attempts=max_attempts,
        )
        job_id = job.id
    with session_scope(engine) as s:
        assert claim(s, worker="dead-worker", now=clock.now, lease_s=30) is not None
    return job_id


def test_the_reaper_fails_the_item_of_a_job_that_ran_out_of_attempts(pg_engine: Engine, clock) -> None:
    _item(pg_engine, clock, "notes/poison.md", "waiting_llm")
    job_id = _abandoned(pg_engine, clock, "notes/poison.md", max_attempts=1)
    clock.advance(31)

    assert _worker(pg_engine, clock, {}).reap_safely() == 1

    assert _row(pg_engine, job_id).status == "failed"
    item = _item_row(pg_engine, "notes/poison.md")
    assert (item.status, item.error) == ("failed", "llm.reason: lease expired too often (1 attempts)")
    assert item.updated_at == clock.now


def test_the_reaper_leaves_the_item_of_a_requeued_or_finished_job_alone(pg_engine: Engine, clock) -> None:
    _item(pg_engine, clock, "notes/again.md", "waiting_llm")
    _item(pg_engine, clock, "notes/done.md", "published")
    _abandoned(pg_engine, clock, "notes/again.md", max_attempts=3)
    _abandoned(pg_engine, clock, "notes/done.md", max_attempts=1)  # it published, then died before complete
    clock.advance(31)

    assert _worker(pg_engine, clock, {}).reap_safely() == 2

    assert _item_row(pg_engine, "notes/again.md").status == "waiting_llm"  # its job runs again
    assert _item_row(pg_engine, "notes/done.md").status == "published"


def test_the_reaper_fails_the_item_of_a_youtube_fetch_that_ran_out_of_attempts(
    pg_engine: Engine, clock
) -> None:
    _item(pg_engine, clock, "clippings/clip.md", "waiting_youtube")
    job_id = _abandoned(pg_engine, clock, "clippings/clip.md", max_attempts=1, type="youtube.fetch")
    clock.advance(31)

    assert _worker(pg_engine, clock, {}).reap_safely() == 1

    assert _row(pg_engine, job_id).status == "failed"
    item = _item_row(pg_engine, "clippings/clip.md")
    assert (item.status, item.error) == ("failed", "youtube.fetch: lease expired too often (1 attempts)")


def test_the_reaper_fails_a_pipeline_run_and_touches_no_item(pg_engine: Engine, clock) -> None:
    _item(pg_engine, clock, "notes/leftover.md", "staging")
    job_id = _abandoned(pg_engine, clock, None, max_attempts=1, type="pipeline.run")
    clock.advance(31)

    assert _worker(pg_engine, clock, {}).reap_safely() == 1

    row = _row(pg_engine, job_id)
    assert (row.status, row.error) == ("failed", "lease expired too often (1 attempts)")
    item = _item_row(pg_engine, "notes/leftover.md")
    assert (item.status, item.updated_at) == ("staging", clock.now - timedelta(seconds=31))


def test_an_item_error_does_not_stop_the_reaper(
    pg_engine: Engine, clock, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    real_set_item_status = loop.set_item_status

    def broken_for_one(session, name, status, **kwargs):
        if name == "notes/broken.md":
            session.execute(text("select 1/0"))  # a database error, inside the savepoint
        return real_set_item_status(session, name, status, **kwargs)

    monkeypatch.setattr(loop, "set_item_status", broken_for_one)
    _item(pg_engine, clock, "notes/broken.md", "waiting_llm")
    _item(pg_engine, clock, "notes/poison.md", "waiting_llm")
    broken = _abandoned(pg_engine, clock, "notes/broken.md", max_attempts=1)
    poison = _abandoned(pg_engine, clock, "notes/poison.md", max_attempts=1)
    requeued = _abandoned(pg_engine, clock, "notes/again.md", max_attempts=3)
    clock.advance(31)

    with caplog.at_level("ERROR", logger="catcher.worker"):
        assert _worker(pg_engine, clock, {}).reap_safely() == 3

    assert [_row(pg_engine, j).status for j in (broken, poison, requeued)] == ["failed", "failed", "queued"]
    assert _item_row(pg_engine, "notes/broken.md").status == "waiting_llm"  # logged, left as it was
    assert _item_row(pg_engine, "notes/poison.md").status == "failed"
    assert any("notes/broken.md" in r.getMessage() and r.exc_info for r in caplog.records)


def _die_after_the_hand_off(harness, type: str, handler: Handler) -> Any:
    """Claim the queued `type` job as a worker that dies on its last attempt right after the handler's
    hand-off commit (the next job is queued, the item moved on), before `complete`; the lease expires."""
    with session_scope(harness.ctx.engine) as s:
        [job] = [j for j in harness.jobs(s) if j.type == type and j.status == "queued"]
        job.max_attempts = 1
    with session_scope(harness.ctx.engine) as s:
        job = claim(s, worker="dead-worker", now=harness.clock(), lease_s=30, types=[type])
    assert job is not None
    assert isinstance(handler(harness.ctx, job), Done)
    harness.clock.advance(YOUTUBE_GAP_S + 1)  # past the lease, and past the YouTube gate's gap
    return job.id


def _harness_item(harness, original: str) -> JobItem:
    with session_scope(harness.ctx.engine) as s:
        [item] = [i for i in s.scalars(select(JobItem)) if i.original_filename == original]
        return item


def test_a_fetch_that_died_after_handing_off_to_llm_reason_leaves_the_item_to_it(harness) -> None:
    harness.add_job("pipeline.run", only=["yt"])
    assert harness.worker.run_once() == "succeeded"
    assert _harness_item(harness, "yt.md").status == "waiting_youtube"
    fetch_id = _die_after_the_hand_off(harness, "youtube.fetch", handle_youtube_fetch)
    assert _harness_item(harness, "yt.md").status == "waiting_llm"

    assert harness.worker.reap_safely() == 1

    assert _row(harness.ctx.engine, fetch_id).status == "failed"
    assert _harness_item(harness, "yt.md").status == "waiting_llm"  # the queued llm.reason owns it
    assert harness.drain(max_jobs=2) == ["succeeded"]
    assert _harness_item(harness, "yt.md").status == "published"
    assert len(harness.fetch_calls) == 1


def test_an_llm_reason_that_died_after_handing_back_to_youtube_fetch_leaves_the_item_to_it(
    harness, yt_facts
) -> None:
    facts = harness.ctx.services.youtube.cache(harness.ideas / "facts")
    facts.put(yt_facts)
    harness.add_job("pipeline.run", only=["yt"])
    assert harness.worker.run_once() == "succeeded"
    assert _harness_item(harness, "yt.md").status == "waiting_llm"
    (harness.ideas / "facts" / f"{yt_facts.video_id}.json").unlink()  # the saved facts are gone
    reason_id = _die_after_the_hand_off(harness, "llm.reason", handle_llm_reason)
    assert _harness_item(harness, "yt.md").status == "waiting_youtube"

    assert harness.worker.reap_safely() == 1

    assert _row(harness.ctx.engine, reason_id).status == "failed"
    assert _harness_item(harness, "yt.md").status == "waiting_youtube"  # the queued fetch owns it
    assert harness.drain(max_jobs=3) == ["succeeded", "succeeded"]  # the fetch, then a new llm.reason
    assert _harness_item(harness, "yt.md").status == "published"
    assert len(harness.fetch_calls) == 1


def _poisoned_note(harness) -> tuple[str, Any]:
    """The note staged with its working copy in output/, and its llm.reason job reaped out of attempts."""
    harness.add_job("pipeline.run", only=["YouTube walks"])
    assert harness.worker.run_once() == "succeeded"
    name = _harness_item(harness, "YouTube walks.md").calculated_name
    with session_scope(harness.ctx.engine) as s:
        [job] = [j for j in harness.jobs(s) if j.type == "llm.reason"]
        job.max_attempts = 1
    with session_scope(harness.ctx.engine) as s:
        job = claim(s, worker="dead-worker", now=harness.clock(), lease_s=30)
    assert job is not None
    harness.clock.advance(31)
    return name, job.id


def test_the_reaper_moves_a_failed_items_working_copy_to_failed(harness) -> None:
    name, _ = _poisoned_note(harness)
    assert (harness.ideas / "output" / name).is_file()

    assert harness.worker.reap_safely() == 1

    assert _harness_item(harness, "YouTube walks.md").status == "failed"
    assert not (harness.ideas / "output" / name).exists()
    assert (harness.ideas / "failed" / name).is_file()
    error = (harness.ideas / "failed" / name).with_suffix(".error.txt").read_text(encoding="utf-8")
    assert "reason: llm.reason: lease expired too often (1 attempts)" in error.splitlines()


def test_a_file_error_moving_to_failed_is_logged_and_the_reap_stands(
    harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    name, job_id = _poisoned_note(harness)

    def no_disk(*args: Any, **kwargs: Any):
        raise OSError("disk full")

    monkeypatch.setattr(loop, "move_to_failed", no_disk)

    with caplog.at_level("ERROR", logger="catcher.worker"):
        assert harness.worker.reap_safely() == 1

    assert _row(harness.ctx.engine, job_id).status == "failed"
    assert _harness_item(harness, "YouTube walks.md").status == "failed"
    assert (harness.ideas / "output" / name).is_file()  # it stays where it was
    assert any("disk full" in r.getMessage() for r in caplog.records)


def test_stop_lets_the_current_job_finish(pg_engine: Engine, clock) -> None:
    current = _enqueue(pg_engine, clock, name="current", priority=1)
    next_one = _enqueue(pg_engine, clock, name="next")
    started, release = threading.Event(), threading.Event()

    def handler(ctx: HandlerContext, job: Job) -> HandlerResult:
        started.set()
        assert release.wait(WAIT_S)
        return Done()

    stop = threading.Event()
    worker = _worker(pg_engine, clock, {"note": handler}, sleep=lambda s: None)
    thread = threading.Thread(target=worker.run_forever, args=(stop,), daemon=True)
    thread.start()
    assert started.wait(WAIT_S)
    stop.set()
    release.set()
    thread.join(WAIT_S)

    assert not thread.is_alive()
    assert _row(pg_engine, current).status == "succeeded"
    assert _row(pg_engine, next_one).status == "queued"


def test_an_error_out_of_run_job_is_logged_and_the_worker_keeps_running(
    pg_engine: Engine, clock, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    first = _enqueue(pg_engine, clock, name="first", priority=1)
    second = _enqueue(pg_engine, clock, name="second")
    real_run_job = loop.run_job

    def run_job(ctx, job, handlers, **kwargs):
        if job.params["name"] == "first":
            raise RuntimeError("database gone while finishing")
        return real_run_job(ctx, job, handlers, **kwargs)

    monkeypatch.setattr(loop, "run_job", run_job)
    worker = _worker(pg_engine, clock, {"note": lambda ctx, j: Done()})

    with caplog.at_level("ERROR", logger="catcher.worker"):
        assert worker.run_once() == "error"
    assert worker.run_once() == "succeeded"

    assert any(r.exc_info for r in caplog.records if r.name == "catcher.worker")
    assert _row(pg_engine, first).status == "running"  # left for the reaper
    assert _row(pg_engine, second).status == "succeeded"


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_keyboard_interrupt_and_system_exit_propagate(
    pg_engine: Engine, clock, monkeypatch: pytest.MonkeyPatch, interrupt: type[BaseException]
) -> None:
    _enqueue(pg_engine, clock)

    def run_job(ctx, job, handlers, **kwargs):
        raise interrupt

    monkeypatch.setattr(loop, "run_job", run_job)

    with pytest.raises(interrupt):
        _worker(pg_engine, clock, {}).run_once()


def test_run_job_receives_the_job_that_claim_returned(
    pg_engine: Engine, clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enqueue(pg_engine, clock)
    claimed: list[Job | None] = []
    passed: list[Job] = []
    real_claim, real_run_job = queue.claim, loop.run_job

    def spy_claim(session, **kwargs):
        job = real_claim(session, **kwargs)
        claimed.append(job)
        return job

    def spy_run_job(ctx, job, handlers, **kwargs):
        passed.append(job)
        return real_run_job(ctx, job, handlers, **kwargs)

    monkeypatch.setattr(queue, "claim", spy_claim)
    monkeypatch.setattr(loop, "run_job", spy_run_job)

    assert _worker(pg_engine, clock, {"note": lambda ctx, j: Done()}).run_once() == "succeeded"
    assert len(passed) == 1 and passed[0] is claimed[0]
    assert {"id", "locked_by", "claim_seq"} <= inspect(passed[0]).dict.keys()


def test_the_worker_passes_its_settings_to_claim_and_run_job(
    pg_engine: Engine, clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = _enqueue(pg_engine, clock)
    kwargs_seen: list[dict[str, Any]] = []

    def spy_run_job(ctx, job, handlers, **kwargs):
        kwargs_seen.append(kwargs)
        return "succeeded"

    monkeypatch.setattr(loop, "run_job", spy_run_job)
    worker = Worker(_ctx(pg_engine, clock), {}, worker_id="w7", lease_s=45, heartbeat_s=15)

    assert worker.run_once() == "succeeded"
    assert kwargs_seen == [{"lease_s": 45, "heartbeat_s": 15}]
    row = _row(pg_engine, job_id)
    assert (row.locked_by, row.lease_until) == ("w7", clock.now + timedelta(seconds=45))


def test_an_idle_worker_sleeps_poll_s(pg_engine: Engine, clock) -> None:
    stop = threading.Event()
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) == 2:
            stop.set()

    _worker(pg_engine, clock, {}, poll_s=3.5, sleep=sleep).run_forever(stop)

    assert slept == [3.5, 3.5]


def test_worker_sessions_have_a_lock_timeout(pg_url: str) -> None:
    engine = make_worker_engine(pg_url)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("show lock_timeout")).scalar_one() == "10s"
    finally:
        engine.dispose()


def _claim_failing(monkeypatch: pytest.MonkeyPatch, times: int) -> list[int]:
    """Make the first `times` claims raise a database error; later claims are real."""
    real_claim = queue.claim
    calls: list[int] = []

    def flaky_claim(session, **kwargs):
        calls.append(1)
        if len(calls) <= times:
            raise OperationalError("select ... for update", {}, Exception("connection refused"))
        return real_claim(session, **kwargs)

    monkeypatch.setattr(queue, "claim", flaky_claim)
    return calls


def _sleeps_until(stop: threading.Event, count: int) -> tuple[list[float], Any]:
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) == count:
            stop.set()

    return slept, sleep


def test_a_claim_error_backs_off_then_resets_and_logs_the_recovery(
    pg_engine: Engine, clock, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    job_id = _enqueue(pg_engine, clock)
    _claim_failing(monkeypatch, times=3)
    stop = threading.Event()
    slept, sleep = _sleeps_until(stop, 4)

    with caplog.at_level("INFO", logger="catcher.worker"):
        _worker(pg_engine, clock, {"note": lambda ctx, j: Done()}, poll_s=2, sleep=sleep).run_forever(stop)

    assert slept == [2, 4, 8, 2]  # doubles while the claim fails, back to poll_s once it works
    assert _row(pg_engine, job_id).status == "succeeded"
    records = [r for r in caplog.records if r.name == "catcher.worker"]
    failures = [r for r in records if "could not claim" in r.getMessage()]
    assert [(r.levelname, r.exc_info is not None) for r in failures] == [
        ("ERROR", True),
        ("WARNING", False),
        ("WARNING", False),
    ]
    assert len([r for r in records if "recovered" in r.getMessage() and r.levelname == "INFO"]) == 1


def test_the_claim_backoff_is_capped(pg_engine: Engine, clock, monkeypatch: pytest.MonkeyPatch) -> None:
    _claim_failing(monkeypatch, times=10)
    stop = threading.Event()
    slept, sleep = _sleeps_until(stop, 5)

    _worker(pg_engine, clock, {}, poll_s=20, sleep=sleep).run_forever(stop)

    assert slept == [20, 40, 60, 60, 60]


def test_run_once_returns_none_after_a_claim_error_and_works_on_the_next_call(
    pg_engine: Engine, clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enqueue(pg_engine, clock)
    _claim_failing(monkeypatch, times=1)
    worker = _worker(pg_engine, clock, {"note": lambda ctx, j: Done()})

    assert worker.run_once() is None
    assert worker.run_once() == "succeeded"


def test_a_bad_lease_fails_fast_instead_of_retrying(pg_engine: Engine, clock) -> None:
    _enqueue(pg_engine, clock)
    stop = threading.Event()
    slept, sleep = _sleeps_until(stop, 1)  # ends the loop if the error were swallowed

    with pytest.raises(ValueError, match="lease_s"):
        _worker(pg_engine, clock, {}, lease_s=0, sleep=sleep).run_forever(stop)
    assert slept == []


def test_an_idle_worker_stops_at_once_by_default(
    pg_engine: Engine, clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    polled = threading.Event()
    real_run_once = Worker.run_once

    def run_once(self: Worker):
        result = real_run_once(self)
        polled.set()  # the loop is about to wait poll_s
        return result

    monkeypatch.setattr(Worker, "run_once", run_once)
    stop = threading.Event()
    worker = _worker(pg_engine, clock, {}, poll_s=3600)  # the default wait is the stop event
    thread = threading.Thread(target=worker.run_forever, args=(stop,), daemon=True)
    thread.start()
    assert polled.wait(WAIT_S)
    stop.set()
    thread.join(WAIT_S)

    assert not thread.is_alive()


def _staged_note(harness) -> str:
    """The note staged by a real pipeline.run: waiting_llm, its llm.reason queued, working copy in output/."""
    harness.add_job("pipeline.run", only=["YouTube walks"])
    assert harness.worker.run_once() == "succeeded"
    item = _harness_item(harness, "YouTube walks.md")
    assert item.status == "waiting_llm"
    return item.calculated_name


def _assert_filed_as_failed(harness, name: str, *reason_parts: str) -> None:
    item = _harness_item(harness, "YouTube walks.md")
    assert item.status == "failed"
    for part in reason_parts:
        assert part in (item.error or "")
    assert not (harness.ideas / "output" / name).exists()
    assert (harness.ideas / "failed" / name).is_file()
    error = (harness.ideas / "failed" / name).with_suffix(".error.txt").read_text(encoding="utf-8")
    assert all(part in error for part in reason_parts)


def test_a_handler_that_raises_after_the_page_fails_the_item_and_files_it(
    harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = _staged_note(harness)

    def broken_finish(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("a bug after write_page")

    monkeypatch.setattr(handlers_pipeline, "finish", broken_finish)

    assert harness.drain(max_jobs=2) == ["failed"]

    assert len(list((harness.docs / NOTES).glob("2*.md"))) == 1  # the page was written before the bug
    _assert_filed_as_failed(harness, name, "llm.reason", "RuntimeError: a bug after write_page")


def test_a_param_error_fails_the_item_and_files_it(harness) -> None:
    name = _staged_note(harness)
    with session_scope(harness.ctx.engine) as s:
        [job] = [j for j in harness.jobs(s) if j.type == "llm.reason"]
        job.params = {**job.params, "colour": "blue"}

    assert harness.drain(max_jobs=2) == ["failed"]

    _assert_filed_as_failed(harness, name, "llm.reason", "unknown parameter(s) for llm.reason: colour")


def test_a_failed_job_leaves_an_item_another_live_job_carries_alone(harness) -> None:
    name = _staged_note(harness)
    harness.add_job("llm.reason", priority=5, calculated_name=name, colour="blue")  # runs first, fails

    assert harness.drain(max_jobs=3) == ["failed", "succeeded"]

    assert _harness_item(harness, "YouTube walks.md").status == "published"
    assert not (harness.ideas / "failed" / name).exists()


def test_an_item_error_after_a_failed_job_is_logged_and_the_worker_goes_on(
    harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    name = _staged_note(harness)
    with session_scope(harness.ctx.engine) as s:
        [job] = [j for j in harness.jobs(s) if j.type == "llm.reason"]
        job.params = {**job.params, "colour": "blue"}

    def broken(*args: Any, **kwargs: Any):
        raise OperationalError("update job_items", {}, Exception("connection lost"))

    monkeypatch.setattr(loop, "fail_items_of", broken)
    with caplog.at_level("ERROR", logger="catcher.worker"):
        assert harness.drain(max_jobs=2) == ["failed"]

    assert _harness_item(harness, "YouTube walks.md").status == "waiting_llm"  # B5's reconcile, or a requeue
    assert (harness.ideas / "output" / name).is_file()
    assert any("could not fail the item" in r.getMessage() for r in caplog.records)
