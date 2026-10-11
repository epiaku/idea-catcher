"""The whole chain on a real Postgres and real Git repos (bare remotes in tmp folders), the fake model, the
fake YouTube and a frozen clock: pull, run, document jobs, publish, then follow-ups until nothing is left."""

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from worker_harness import FrozenClock, RaisingBackend

from catcher.core.db import session_scope
from catcher.modules.queue.models import Job, JobItem
from catcher.modules.worker.handlers import Fail

CAPTURE = "inbox/notes/phone.md"


class TickingClock(FrozenClock):
    """A frozen clock that moves one millisecond on every reading, so the jobs of one test are created in a
    known order (`created_at` is never a tie)."""

    def __call__(self) -> datetime:
        self.now += timedelta(milliseconds=1)
        return self.now


def chain_harness(harness):
    """The chain on, and the worker's clock ticking (the harness clock stays frozen for the YouTube gate)."""
    harness.worker.chain = True
    harness.worker.ctx = replace(harness.worker.ctx, clock=TickingClock(harness.clock.now))
    return harness


def job_types(harness) -> list[str]:
    with session_scope(harness.ctx.engine) as session:
        return [job.type for job in harness.jobs(session)]


def all_jobs(harness) -> list[Job]:
    with session_scope(harness.ctx.engine) as session:
        return harness.jobs(session)


def push_capture(sh, harness, tmp_path: Path, rel: str = CAPTURE, text: str = "from the phone\n") -> None:
    other = tmp_path / "other-ideas"
    if not other.exists():
        sh(tmp_path, "clone", str(harness.ideas.with_name(f"{harness.ideas.name}.git")), str(other))
    sh(other, "pull")
    (other / rel).parent.mkdir(parents=True, exist_ok=True)
    (other / rel).write_text(text)
    sh(other, "add", "-A")
    sh(other, "commit", "-m", "a capture from the phone")
    sh(other, "push")


def inbox_files(harness) -> list[str]:
    inbox = harness.ideas / "inbox"
    return sorted(p.name for p in inbox.rglob("*.md")) if inbox.is_dir() else []


def item_statuses(harness) -> dict[str, str]:
    with session_scope(harness.ctx.engine) as session:
        return {i.calculated_name: i.status for i in session.scalars(select(JobItem))}


def test_a_pull_starts_the_whole_cycle_and_it_ends_when_the_inbox_is_empty(harness) -> None:
    chain_harness(harness)
    harness.add_job("ideas.pull")

    harness.drain(max_jobs=20)

    types = job_types(harness)
    assert types[:2] == ["ideas.pull", "pipeline.run"]
    assert types.count("pipeline.run") == 1
    assert types.count("pipeline.publish") == 1
    assert types[-1] == "pipeline.publish"  # nothing is queued after it: the inbox is empty
    assert {j.status for j in all_jobs(harness)} == {"succeeded"}
    assert inbox_files(harness) == []
    assert set(item_statuses(harness).values()) == {"published"}


def test_the_chained_publish_pushes_to_the_remote(harness, sh) -> None:
    chain_harness(harness)
    harness.add_job("ideas.pull")

    harness.drain(max_jobs=20)

    for repo in (harness.ideas, harness.docs):
        assert sh(repo, "rev-list", "--count", "@{upstream}..HEAD").strip() == "0"  # nothing left to push
    [publish] = [j for j in all_jobs(harness) if j.type == "pipeline.publish"]
    assert publish.params == {"pull": True, "push": True, "follow_up": True}


def test_the_push_setting_turns_the_chained_publish_into_a_local_commit(harness, sh) -> None:
    chain_harness(harness)
    harness.ctx.settings.chain_publish_push = False
    harness.add_job("ideas.pull")

    harness.drain(max_jobs=20)

    [publish] = [j for j in all_jobs(harness) if j.type == "pipeline.publish"]
    assert publish.params == {"pull": False, "push": False, "follow_up": True}
    assert (
        sh(harness.docs, "rev-list", "--count", "@{upstream}..HEAD").strip() != "0"
    )  # committed, not pushed


def test_a_capture_that_arrives_during_the_cycle_starts_one_follow_up_cycle(harness, sh, tmp_path) -> None:
    chain_harness(harness)
    harness.add_job("pipeline.run")
    push_capture(sh, harness, tmp_path)  # only the publish's pull will bring it in

    harness.drain(max_jobs=30)

    types = job_types(harness)
    assert types.count("pipeline.run") == 2
    assert types.count("pipeline.publish") == 2
    assert types[-1] == "pipeline.publish"
    first_publish = types.index("pipeline.publish")
    assert types[first_publish + 1] == "pipeline.run"  # straight after the publish that pulled it in
    follow_up = [j for j in all_jobs(harness) if j.type == "pipeline.run"][1]
    assert follow_up.params == {"retry_deferred": False}
    assert any(name.endswith("-phone.md") for name in item_statuses(harness))
    assert inbox_files(harness) == []


def test_a_run_with_a_limit_runs_once_publishes_and_stops(harness) -> None:
    chain_harness(harness)
    harness.add_job("pipeline.run", limit=1)

    harness.drain(max_jobs=20)

    types = job_types(harness)
    assert types.count("pipeline.run") == 1
    assert types.count("pipeline.publish") == 1
    [publish] = [j for j in all_jobs(harness) if j.type == "pipeline.publish"]
    assert publish.params["follow_up"] is False
    assert len(inbox_files(harness)) == 2  # the rest waits for a run of its own


def test_a_pull_with_a_limit_makes_one_limited_run_and_stops(harness) -> None:
    chain_harness(harness)
    harness.add_job("ideas.pull", limit=1)

    harness.drain(max_jobs=20)

    types = job_types(harness)
    assert types.count("pipeline.run") == 1 and types.count("pipeline.publish") == 1
    [run] = [j for j in all_jobs(harness) if j.type == "pipeline.run"]
    assert run.params == {"retry_deferred": True, "limit": 1}
    [publish] = [j for j in all_jobs(harness) if j.type == "pipeline.publish"]
    assert publish.params["follow_up"] is False
    assert len(inbox_files(harness)) == 2  # the rest waits for a pull without a limit


def test_a_preview_queues_nothing(harness) -> None:
    chain_harness(harness)
    harness.add_job("pipeline.preview")

    harness.drain(max_jobs=5)

    assert job_types(harness) == ["pipeline.preview"]


def test_a_failed_pull_stops_the_cycle(harness) -> None:
    chain_harness(harness)
    harness.worker.handlers = {**harness.worker.handlers, "ideas.pull": lambda ctx, job: Fail("no network")}
    harness.add_job("ideas.pull")

    harness.drain(max_jobs=5)

    assert job_types(harness) == ["ideas.pull"]
    assert len(inbox_files(harness)) == 3  # they wait for the next hourly pull


def test_a_failed_run_stops_the_cycle(harness) -> None:
    chain_harness(harness)
    harness.worker.handlers = {**harness.worker.handlers, "pipeline.run": lambda ctx, job: Fail("no inbox")}
    harness.add_job("ideas.pull")

    harness.drain(max_jobs=5)

    assert job_types(harness) == ["ideas.pull", "pipeline.run"]


def test_a_failed_publish_with_files_left_still_queues_a_follow_up_run(harness) -> None:
    chain_harness(harness)
    real_publish = harness.worker.handlers["pipeline.publish"]
    calls: list[int] = []

    def publish_that_fails_once(ctx, job):
        calls.append(1)
        if len(calls) == 1:
            (ctx.ideas / "inbox/notes").mkdir(parents=True, exist_ok=True)
            (ctx.ideas / CAPTURE).write_text("arrived while the publish ran\n")
            return Fail("push rejected")
        return real_publish(ctx, job)

    harness.worker.handlers = {**harness.worker.handlers, "pipeline.publish": publish_that_fails_once}
    harness.add_job("pipeline.run")

    harness.drain(max_jobs=30)

    types = job_types(harness)
    assert types.count("pipeline.run") == 2 and types.count("pipeline.publish") == 2
    assert types[-1] == "pipeline.publish"
    assert inbox_files(harness) == []


def test_a_deferred_document_neither_holds_the_publish_back_nor_loops(harness) -> None:
    chain_harness(harness)
    harness.backends.note = RaisingBackend([])  # the model is down: the note is deferred
    harness.add_job("ideas.pull")

    harness.drain(max_jobs=30)

    types = job_types(harness)
    assert types.count("pipeline.run") == 1  # the deferred note is not retried inside the cycle
    assert types[-1] == "pipeline.publish"
    assert "deferred" in item_statuses(harness).values()


def test_a_second_trigger_while_the_chain_runs_adds_no_second_cycle(harness) -> None:
    chain_harness(harness)
    harness.add_job("ideas.pull")
    harness.add_job("ideas.pull")  # a manual pull on top of the scheduled one

    harness.drain(max_jobs=30)

    types = job_types(harness)
    assert types.count("pipeline.run") == 1
    assert types.count("pipeline.publish") == 1


def test_a_chain_error_does_not_stop_the_worker(harness, monkeypatch) -> None:
    chain_harness(harness)

    def broken(ctx, job, outcome):
        raise RuntimeError("the chain could not read the facts")

    from catcher.modules.worker import loop

    monkeypatch.setattr(loop, "queue_next", broken)
    harness.add_job("ideas.pull")

    assert harness.drain(max_jobs=5) == ["succeeded"]
    assert job_types(harness) == ["ideas.pull"]


def test_without_the_chain_flag_nothing_is_queued(harness) -> None:
    harness.add_job("ideas.pull")

    harness.drain(max_jobs=5)

    assert job_types(harness) == ["ideas.pull"]
