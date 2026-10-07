"""The Scheduler on a real Postgres with a fake clock: one transaction per schedule, a row lock, dedupe."""

import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Engine, select, update
from sqlalchemy.exc import OperationalError
from worker_harness import FrozenClock

from catcher.core.config import Settings
from catcher.core.db import session_scope
from catcher.modules.queue import queue as queue_module
from catcher.modules.queue.models import Job, Schedule
from catcher.modules.scheduler import loop as loop_module
from catcher.modules.scheduler.loop import Scheduler
from catcher.modules.scheduler.schedule import ScheduleSpec, parse_schedules

AMS = ZoneInfo("Europe/Amsterdam")
# 2026-10-02 07:59 in Amsterdam (CEST, UTC+2)
START = datetime(2026, 10, 2, 5, 59, tzinfo=UTC)


def specs(**crons: str) -> list[ScheduleSpec]:
    base = {"schedule_ideas_pull": "", "schedule_pipeline_run": "", "schedule_publish": ""}
    values = {**base, **{f"schedule_{name}": cron for name, cron in crons.items()}}
    return parse_schedules(Settings(_env_file=None, **values))


def scheduler(engine: Engine, clock: FrozenClock, **crons: str) -> Scheduler:
    return Scheduler(engine, specs(**crons), AMS, clock)


def jobs(engine: Engine) -> list[Job]:
    with session_scope(engine) as s:
        return list(s.scalars(select(Job).order_by(Job.created_at, Job.type)))


def last_fired(engine: Engine) -> dict[str, datetime | None]:
    with session_scope(engine) as s:
        return {row.name: row.last_fired_at for row in s.scalars(select(Schedule))}


def finish_all(engine: Engine) -> None:
    with session_scope(engine) as s:
        s.execute(update(Job).where(Job.status == "queued").values(status="succeeded"))


def test_a_new_schedule_fires_nothing_and_stores_now(pg_engine: Engine) -> None:
    clock = FrozenClock(START)
    sched = scheduler(pg_engine, clock, ideas_pull="* * * * *", publish="0 8 * * *")

    assert sched.tick() == []

    assert jobs(pg_engine) == []
    assert last_fired(pg_engine) == {"ideas_pull": START, "publish": START}


def test_it_fires_on_the_slot_and_stores_last_fired(pg_engine: Engine) -> None:
    clock = FrozenClock(START)
    sched = scheduler(pg_engine, clock, ideas_pull="0 8 * * *", pipeline_run="0 8 * * *", publish="0 8 * * *")
    assert sched.tick() == []

    clock.advance(50)  # 07:59:50, still before 08:00
    assert sched.tick() == []
    assert jobs(pg_engine) == []

    clock.advance(20)  # 08:00:10 Amsterdam
    assert sorted(sched.tick()) == ["ideas_pull", "pipeline_run", "publish"]

    got = {job.type: (job.params, job.dedupe_key, job.status, job.run_after) for job in jobs(pg_engine)}
    assert got == {
        "ideas.pull": ({}, "schedule:ideas_pull", "queued", clock.now),
        "pipeline.run": ({"retry_deferred": True}, "schedule:pipeline_run", "queued", clock.now),
        "pipeline.publish": ({"pull": True, "push": True}, "schedule:publish", "queued", clock.now),
    }
    assert set(last_fired(pg_engine).values()) == {clock.now}


def test_the_pull_fires_more_often_than_the_publish(pg_engine: Engine) -> None:
    # 2026-10-01 23:59 in Amsterdam: the rows are created, then one local day minute by minute
    clock = FrozenClock(datetime(2026, 10, 1, 21, 59, tzinfo=UTC))
    sched = scheduler(pg_engine, clock, ideas_pull="*/30 * * * *", publish="0 6,12,18,23 * * *")
    assert sched.tick() == []

    counts = {"ideas_pull": 0, "publish": 0}
    publish_hours = []
    for _ in range(24 * 60):
        clock.advance(60)
        for name in sched.tick():
            counts[name] += 1
            if name == "publish":
                publish_hours.append(clock.now.astimezone(AMS).hour)
        finish_all(pg_engine)  # the worker did the job, so the next slot is not deduped

    assert counts == {"ideas_pull": 48, "publish": 4}
    assert publish_hours == [6, 12, 18, 23]


def test_downtime_fires_once_and_a_second_tick_fires_nothing(pg_engine: Engine) -> None:
    clock = FrozenClock(START)
    sched = scheduler(
        pg_engine, clock, ideas_pull="*/30 * * * *", pipeline_run="0 8 * * *", publish="0 6 * * *"
    )
    assert sched.tick() == []

    clock.advance(3 * 24 * 3600)  # three days down
    assert sorted(sched.tick()) == ["ideas_pull", "pipeline_run", "publish"]
    clock.advance(5)  # the next tick, same minute
    assert sched.tick() == []

    assert sorted(job.type for job in jobs(pg_engine)) == ["ideas.pull", "pipeline.publish", "pipeline.run"]
    assert set(last_fired(pg_engine).values()) == {clock.now - timedelta(seconds=5)}


def test_a_running_previous_job_consumes_the_slot_without_a_second_job(pg_engine: Engine) -> None:
    clock = FrozenClock(START)
    sched = scheduler(pg_engine, clock, pipeline_run="*/30 * * * *")
    assert sched.tick() == []
    clock.advance(120)  # 08:01
    assert sched.tick() == ["pipeline_run"]
    with session_scope(pg_engine) as s:  # the worker claimed it and is still busy with it
        s.execute(
            update(Job).values(status="running", locked_by="w", lease_until=clock.now + timedelta(hours=2))
        )

    clock.advance(30 * 60)  # 08:31: the 08:30 slot is due while the 08:00 job runs
    assert sched.tick() == []
    assert len(jobs(pg_engine)) == 1
    assert last_fired(pg_engine) == {"pipeline_run": clock.now}  # the slot is consumed

    clock.advance(10)
    assert sched.tick() == []  # and nothing piles up for it
    assert len(jobs(pg_engine)) == 1

    with session_scope(pg_engine) as s:
        s.execute(update(Job).values(status="succeeded", locked_by=None, lease_until=None))
    clock.advance(30 * 60)  # 09:01: the 09:00 slot fires again
    assert sched.tick() == ["pipeline_run"]
    assert len(jobs(pg_engine)) == 2


def _tick_together(schedulers: list[Scheduler]) -> list[list[str]]:
    barrier = threading.Barrier(len(schedulers))
    results: list[list[str] | None] = [None] * len(schedulers)
    errors: list[BaseException] = []

    def run(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            results[index] = schedulers[index].tick()
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(schedulers))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    return [r for r in results if r is not None]


def test_two_schedulers_ticking_together_queue_one_job(pg_engine: Engine, monkeypatch) -> None:
    """The job dedupe would hide a missing lock, so it is switched off here and the enqueue is slowed down:
    only the row lock on `schedules` keeps the second scheduler from queueing a second job."""
    real_enqueue = queue_module.enqueue

    def slow_enqueue_without_dedupe(session, **kwargs):
        time.sleep(0.3)  # both schedulers would have read last_fired_at by now if nothing locked the row
        return real_enqueue(session, **{**kwargs, "dedupe_key": None})

    monkeypatch.setattr(loop_module, "enqueue", slow_enqueue_without_dedupe)
    clock = FrozenClock(START)
    first = scheduler(pg_engine, clock, publish="0 8 * * *")
    second = scheduler(pg_engine, clock, publish="0 8 * * *")

    # Creating the row at the same moment: one row, nothing fires, no error
    assert _tick_together([first, second]) == [[], []]
    assert last_fired(pg_engine) == {"publish": START}

    clock.advance(90)  # 08:00:30, the same minute for both
    results = _tick_together([first, second])

    assert sorted(results) == [[], ["publish"]]
    assert len(jobs(pg_engine)) == 1
    assert last_fired(pg_engine) == {"publish": clock.now}


def test_a_database_error_in_one_schedule_does_not_stop_the_others(
    pg_engine: Engine, monkeypatch, caplog
) -> None:
    real_enqueue = queue_module.enqueue
    broken = {"on": True}

    def enqueue_failing_for_the_run(session, **kwargs):
        if broken["on"] and kwargs["type"] == "pipeline.run":
            raise OperationalError("insert into jobs", {}, Exception("server closed the connection"))
        return real_enqueue(session, **kwargs)

    monkeypatch.setattr(loop_module, "enqueue", enqueue_failing_for_the_run)
    clock = FrozenClock(START)
    sched = scheduler(pg_engine, clock, ideas_pull="0 8 * * *", pipeline_run="0 8 * * *", publish="0 8 * * *")
    assert sched.tick() == []

    clock.advance(90)
    with caplog.at_level(logging.WARNING, logger="catcher.scheduler"):
        assert sorted(sched.tick()) == ["ideas_pull", "publish"]
        clock.advance(10)
        assert sched.tick() == []  # still failing: not logged again
    failures = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(failures) == 1 and "pipeline_run" in failures[0].getMessage()

    # its transaction was rolled back: the slot is not consumed, so it fires once the database is back
    assert last_fired(pg_engine)["pipeline_run"] == START
    broken["on"] = False
    clock.advance(10)
    assert sched.tick() == ["pipeline_run"]
    assert sorted(job.type for job in jobs(pg_engine)) == ["ideas.pull", "pipeline.publish", "pipeline.run"]


def test_run_forever_survives_a_failing_tick_and_stops_on_the_event(pg_engine: Engine, caplog) -> None:
    sched = scheduler(pg_engine, FrozenClock(START), publish="0 8 * * *")
    stop = threading.Event()
    outcomes = ["fail", "fail", "ok", "fail", "ok", "stop"]
    calls: list[str] = []

    def scripted_tick() -> list[str]:
        outcome = outcomes[len(calls)]
        calls.append(outcome)
        if outcome == "fail":
            raise OperationalError("select", {}, Exception("connection refused"))
        if outcome == "stop":
            stop.set()
        return []

    sched.tick = scripted_tick  # type: ignore[method-assign]
    with caplog.at_level(logging.INFO, logger="catcher.scheduler"):
        thread = threading.Thread(target=sched.run_forever, args=(stop, 0.01))
        thread.start()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert calls == outcomes
    # one error per failure streak, not per failing tick
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 2

    # a long wait ends as soon as the event is set
    stop.clear()
    sched.tick = lambda: []  # type: ignore[method-assign]
    thread = threading.Thread(target=sched.run_forever, args=(stop, 3600))
    thread.start()
    time.sleep(0.1)
    started = time.monotonic()
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert time.monotonic() - started < 2


def test_a_schedule_row_without_last_fired_starts_now(pg_engine: Engine) -> None:
    with session_scope(pg_engine) as s:
        s.add(Schedule(name="publish", last_fired_at=None))
    clock = FrozenClock(START + timedelta(hours=1))
    assert scheduler(pg_engine, clock, publish="* * * * *").tick() == []
    assert last_fired(pg_engine) == {"publish": clock.now}


def test_a_clock_behind_last_fired_fires_nothing(pg_engine: Engine) -> None:
    with session_scope(pg_engine) as s:
        s.add(Schedule(name="publish", last_fired_at=START + timedelta(days=1)))
    assert scheduler(pg_engine, FrozenClock(START), publish="* * * * *").tick() == []
    assert last_fired(pg_engine) == {"publish": START + timedelta(days=1)}
    assert jobs(pg_engine) == []


@pytest.fixture(autouse=True)
def _no_stray_threads():
    before = set(threading.enumerate())
    yield
    assert {t for t in threading.enumerate() if t not in before and t.is_alive()} == set()
