"""Crash injection: a worker killed mid-job, a handler that outlives its lease, a lost lease, a poison job and
staging leftovers, each recovered by the real reaper on the real queue, Git repos and handlers.

A kill is a `SystemExit` raised from a wrapper around a real step. It propagates out of `run_job` and
`Worker.run_once` by design and leaves the job `running`; the test catches it, lets the lease expire (the
frozen clock moves past `lease_s`), reaps and runs again, as a restarted worker would.

Review Focus #1 (staging leftovers), #2 (one page and one model call after a crash) and #4 (the heartbeat
keeps a long handler alive; a lost lease discards the result and the repeat is harmless)."""

import dataclasses
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from worker_harness import CLIPPING, NOTES, WorkerHarness

from catcher.core.db import session_scope
from catcher.core.frontmatter import load
from catcher.modules.pipeline import inbox
from catcher.modules.queue import queue
from catcher.modules.queue.models import Job, JobItem
from catcher.modules.worker import handlers_pipeline
from catcher.modules.worker.app import build_handlers
from catcher.modules.worker.handlers import HandlerContext, HandlerResult
from catcher.modules.worker.handlers_pipeline import handle_llm_reason
from catcher.modules.worker.loop import Worker

YOUTUBE = "hugo/content/en/docs/idea-bucket/youtube"
PAST_LEASE_S = 121  # the harness worker's lease is 120 s
WAIT_S = 10  # generous: a deterministic wait that only times out when something is broken


def killed() -> SystemExit:
    return SystemExit("worker killed")


def items(h: WorkerHarness) -> dict[str, JobItem]:
    """Every item row, by the name it was captured under."""
    with session_scope(h.ctx.engine) as session:
        return {i.original_filename or i.calculated_name: i for i in session.scalars(select(JobItem))}


def jobs_of(h: WorkerHarness, type: str) -> list[Job]:
    with session_scope(h.ctx.engine) as session:
        return [j for j in h.jobs(session) if j.type == type]


def pages(docs: Path, folder: str) -> list[Path]:
    return sorted(p for p in (docs / folder).glob("*.md") if p.name != "_index.md")


def tree(root: Path) -> dict[Path, bytes]:
    """Every file under `root` but `.git`, with its bytes."""
    return {
        p.relative_to(root): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(root).parts
    }


def from_capture(ideas: Path, folder: str, original: str) -> list[Path]:
    """The files in `folder` that came from the document captured as `original`."""
    root = ideas / folder
    return [p for p in sorted(root.rglob("*.md")) if load(p).fm.get("original_filename") == original]


def run_killed(worker: Worker) -> None:
    """Run one job that is killed: the SystemExit leaves `run_once`, the job stays running."""
    with pytest.raises(SystemExit, match="worker killed"):
        worker.run_once()


def expire_and_reap(h: WorkerHarness, worker: Worker | None = None) -> int:
    """Move the frozen clock past the lease and run the real reaper; how many jobs it changed."""
    h.clock.advance(PAST_LEASE_S)
    return (worker or h.worker).reap_safely()


@pytest.mark.parametrize("crash_point", ["after write_page", "after the item commit"])
def test_a_worker_killed_mid_llm_reason_is_recovered(harness, monkeypatch, crash_point):
    real_write_page, real_complete = handlers_pipeline.write_page, queue.complete
    kills: list[str] = []

    def write_then_die(*args: Any, **kwargs: Any):
        written = real_write_page(*args, **kwargs)
        if not kills:
            kills.append("write_page")
            raise killed()  # the page is in docs, output/ still holds the working copy, the item waits
        return written

    def die_before_complete(session, job, **kwargs: Any) -> bool:
        if job.type == "llm.reason" and not kills:
            kills.append("complete")
            raise killed()  # the page is made and the item published, but the job never completes
        return real_complete(session, job, **kwargs)

    if crash_point == "after write_page":
        monkeypatch.setattr(handlers_pipeline, "write_page", write_then_die)
    else:
        monkeypatch.setattr(queue, "complete", die_before_complete)
    harness.add_job("pipeline.run", only=["YouTube walks"])
    assert harness.worker.run_once() == "succeeded"

    run_killed(harness.worker)

    assert len(kills) == 1
    [job] = jobs_of(harness, "llm.reason")
    assert (job.status, job.attempts) == ("running", 1)
    [page] = pages(harness.docs, NOTES)
    first = page.read_bytes()
    assert harness.worker.reap_safely() == 0  # the lease has not expired yet
    assert expire_and_reap(harness) == 1

    assert harness.drain(max_jobs=2) == ["succeeded"]

    [job] = jobs_of(harness, "llm.reason")
    assert (job.status, job.attempts) == ("succeeded", 2)
    assert pages(harness.docs, NOTES) == [page] and page.read_bytes() == first  # overwritten by id
    [item] = items(harness).values()
    assert item.status == "published"
    [final] = from_capture(harness.ideas, "output", "YouTube walks.md")
    assert "stage" not in load(final).fm and final.read_bytes() == first
    assert len(harness.backends.note.prompts) == 1  # the re-run reused the saved reply
    assert harness.fetch_calls == []


class RunningClock:
    """An aware clock that starts at `start` and runs at real speed, for the heartbeat and the lease."""

    def __init__(self, start: datetime) -> None:
        self.start, self.t0 = start, time.monotonic()

    def __call__(self) -> datetime:
        return self.start + timedelta(seconds=time.monotonic() - self.t0)


def _job_row(h: WorkerHarness, job_id) -> Job:
    with session_scope(h.ctx.engine) as session:
        return session.scalars(select(Job).where(Job.id == job_id)).one()


def test_a_handler_that_outlives_its_lease_is_not_reaped_while_it_beats(harness):
    lease_s, beat_s = 0.6, 0.1
    harness.add_job("pipeline.run", only=["YouTube walks"])
    assert harness.worker.run_once() == "succeeded"
    started, release = threading.Event(), threading.Event()

    def slow_reason(ctx: HandlerContext, job: Job) -> HandlerResult:
        started.set()
        if not release.wait(WAIT_S):
            raise AssertionError("the test never released the handler")
        return handle_llm_reason(ctx, job)

    clock = RunningClock(harness.clock.now)
    ctx = dataclasses.replace(harness.ctx, clock=clock)
    slow = Worker(
        ctx,
        {**build_handlers(), "llm.reason": slow_reason},
        worker_id="slow",
        lease_s=lease_s,
        heartbeat_s=beat_s,
    )
    outcome: list[Any] = []

    def work() -> None:
        try:
            outcome.append(slow.run_once())
        except BaseException as e:  # a daemon thread must report, not swallow
            outcome.append(e)

    thread = threading.Thread(target=work, name="slow-worker", daemon=True)
    thread.start()
    try:
        assert started.wait(WAIT_S)
        [job] = jobs_of(harness, "llm.reason")
        first_lease = job.lease_until
        assert first_lease is not None and job.status == "running"
        time.sleep(lease_s * 2.5)  # the handler runs well past its first lease

        now = clock()
        assert now > first_lease
        with session_scope(harness.ctx.engine) as session:  # another worker's reaper, mid-handler
            assert queue.reap(session, now=now) == []
        row = _job_row(harness, job.id)
        assert (row.status, row.locked_by, row.claim_seq, row.attempts) == (
            "running",
            "slow",
            job.claim_seq,
            1,
        )
        assert row.lease_until is not None and row.lease_until > now
    finally:
        release.set()
        thread.join(WAIT_S)
    assert not thread.is_alive()

    assert outcome == ["succeeded"]
    row = _job_row(harness, job.id)
    assert (row.status, row.attempts) == ("succeeded", 1)
    assert items(harness)["YouTube walks.md"].status == "published"
    assert len(pages(harness.docs, NOTES)) == 1


def test_a_lost_lease_discards_the_result_and_the_repeat_is_harmless(harness, monkeypatch):
    real_write_page = handlers_pipeline.write_page
    harness.add_job("pipeline.run", only=["YouTube walks"])
    assert harness.worker.run_once() == "succeeded"
    second = Worker(harness.ctx, build_handlers(), worker_id="second")
    seen: dict[str, Any] = {}

    def stall_after_the_page(*args: Any, **kwargs: Any):
        written = real_write_page(*args, **kwargs)
        if "first_page" not in seen:
            # the first worker stalls (a long pause) right after its page: its lease expires, the job is
            # reaped and a second worker runs it all the way, before the first one goes on
            [page] = pages(harness.docs, NOTES)
            seen["first_page"] = (page, page.read_bytes())
            seen["reaped"] = expire_and_reap(harness, second)
            seen["second"] = second.run_once()
            seen["after_second"] = (tree(harness.ideas), tree(harness.docs))
        return written

    monkeypatch.setattr(handlers_pipeline, "write_page", stall_after_the_page)

    assert harness.worker.run_once() == "lost"  # the stale worker's complete is refused

    assert seen["reaped"] == 1 and seen["second"] == "succeeded"
    [job] = jobs_of(harness, "llm.reason")
    assert (job.status, job.attempts, job.claim_seq, job.locked_by) == ("succeeded", 2, 2, None)
    page, first_bytes = seen["first_page"]
    assert pages(harness.docs, NOTES) == [page] and page.read_bytes() == first_bytes
    assert (tree(harness.ideas), tree(harness.docs)) == seen["after_second"]  # the stale run changed nothing
    assert seen["after_second"][1][page.relative_to(harness.docs)] == first_bytes
    assert items(harness)["YouTube walks.md"].status == "published"
    assert len(harness.backends.note.prompts) == 1  # the second run used the saved reply
    assert harness.drain() == []


def run_with_kills(h: WorkerHarness, max_runs: int = 30) -> list[str]:
    """Run jobs until nothing is due or running; a kill leaves its job running until its lease expires and
    the reaper takes it. The labels of the runs ("killed" for a kill)."""
    labels: list[str] = []
    while len(labels) < max_runs:
        try:
            outcome = h.worker.run_once()
        except SystemExit:
            labels.append("killed")
            continue
        if outcome is not None:
            labels.append(outcome)
            continue
        with session_scope(h.ctx.engine) as session:
            running = [j for j in h.jobs(session) if j.status == "running"]
        if not running:
            return labels
        assert expire_and_reap(h) >= 1
    raise AssertionError(f"the worker did not go idle after {max_runs} runs: {labels}")


def test_a_poison_clip_fails_after_max_attempts(harness):
    clip_names: list[str] = []

    def poison_for_the_clip(ctx: HandlerContext, job: Job) -> HandlerResult:
        name = job.params["calculated_name"]
        with session_scope(ctx.engine) as session:
            [item] = session.scalars(select(JobItem).where(JobItem.calculated_name == name))
            original = item.original_filename
        if original == "systeme.md":
            clip_names.append(name)
            raise killed()  # this document kills its worker every time
        return handle_llm_reason(ctx, job)

    harness.worker.handlers = {**harness.worker.handlers, "llm.reason": poison_for_the_clip}
    harness.add_job("pipeline.run")

    labels = run_with_kills(harness)

    assert labels.count("killed") == 3 and len(set(clip_names)) == 1
    [poison] = [j for j in jobs_of(harness, "llm.reason") if j.params["calculated_name"] == clip_names[0]]
    assert (poison.status, poison.attempts) == ("failed", 3)
    assert poison.error == "lease expired too often (3 attempts)"
    by_name = items(harness)
    assert (by_name["systeme.md"].status, by_name["systeme.md"].error) == (
        "failed",
        "llm.reason: lease expired too often (3 attempts)",
    )
    assert from_capture(harness.ideas, "output", "systeme.md")  # its working copy stays for a requeue
    # the other jobs still ran: the note and the YouTube clip are published
    assert by_name["YouTube walks.md"].status == "published"
    assert by_name["yt.md"].status == "published"
    others = [j for j in jobs_of(harness, "llm.reason") if j.id != poison.id]
    assert [(j.status, j.attempts) for j in others] == [("succeeded", 1), ("succeeded", 1)]
    assert pages(harness.docs, CLIPPING) == []
    assert len(pages(harness.docs, NOTES)) == 1 and len(pages(harness.docs, YOUTUBE)) == 1
    assert len(harness.fetch_calls) == 1


def test_staging_leftovers_are_adopted_after_a_worker_restart(harness, monkeypatch):
    real_start_work = handlers_pipeline.start_work
    crashed: list[str] = []

    def die_once(ideas: Path, note, now=None):
        if not crashed:
            crashed.append(note.target_rel.as_posix())
            inbox.archive_copy(ideas, note)  # a partial start: the archive copy is written, then the kill
            raise killed()
        return real_start_work(ideas, note, now)

    monkeypatch.setattr(handlers_pipeline, "start_work", die_once)
    run_job_id = harness.add_job("pipeline.run")

    run_killed(harness.worker)

    [leftover] = items(harness).values()
    assert leftover.status == "staging" and leftover.calculated_name == crashed[0]
    original = leftover.original_filename
    assert original is not None and leftover.inbox_path is not None
    assert _job_row(harness, run_job_id).status == "running"

    restarted = Worker(harness.ctx, build_handlers(), worker_id="restarted")  # a fresh worker process
    assert expire_and_reap(harness, restarted) == 1
    harness.worker = restarted
    labels = harness.drain(max_jobs=6)

    assert labels and set(labels) == {"succeeded"}
    run_job = _job_row(harness, run_job_id)
    assert (run_job.status, run_job.attempts) == ("succeeded", 2)
    assert run_job.result is not None and (run_job.result["adopted"], run_job.result["staged"]) == (1, 2)
    by_name = items(harness)
    assert len(by_name) == 3 and all(i.status == "published" for i in by_name.values())
    adopted = by_name[original]
    assert (adopted.id, adopted.calculated_name) == (leftover.id, leftover.calculated_name)
    assert [
        p.relative_to(harness.ideas / "archive").as_posix()
        for p in from_capture(harness.ideas, "archive", original)
    ] == [crashed[0]]
    assert [
        p.relative_to(harness.ideas / "output").as_posix()
        for p in from_capture(harness.ideas, "output", original)
    ] == [crashed[0]]
    assert not (harness.ideas / "inbox" / leftover.inbox_path.removeprefix("inbox/")).exists()
    mine = [
        j
        for j in jobs_of(harness, "llm.reason") + jobs_of(harness, "youtube.fetch")
        if j.params["calculated_name"] == crashed[0]
    ]
    assert [(j.type, j.status, j.attempts) for j in mine] == [("llm.reason", "succeeded", 1)]
    assert [len(pages(harness.docs, folder)) for folder in (NOTES, CLIPPING, YOUTUBE)] == [1, 1, 1]
