"""The chain's facts and its enqueue wrapper on a real Postgres and real Git repos (no model, no YouTube)."""

import shutil
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update

from catcher.core.db import session_scope
from catcher.modules.queue.models import Job, JobItem, Resource
from catcher.modules.worker.chain import Next, queue_next, read_facts


def empty_the_inbox(harness) -> None:
    shutil.rmtree(harness.ideas / "inbox")


def add_item(harness, status: str, *, name: str | None = None, origin: str = "inbox", at=None) -> str:
    name = name or f"notes/{uuid.uuid4().hex[:8]}.md"
    at = at or harness.clock()
    with session_scope(harness.ctx.engine) as session:
        session.add(
            JobItem(
                calculated_name=name,
                doc_id=name,
                doc_class="note",
                origin=origin,
                status=status,
                created_at=at,
                updated_at=at,
            )
        )
    return name


def set_job(harness, job_id: uuid.UUID, **values: Any) -> None:
    with session_scope(harness.ctx.engine) as session:
        session.execute(update(Job).where(Job.id == job_id).values(**values))


def finished_job(harness, type: str, *, result: dict | None = None, **params: Any) -> uuid.UUID:
    job_id = harness.add_job(type, **params)
    set_job(harness, job_id, status="succeeded", result=result, finished_at=harness.clock())
    harness.clock.advance(1)  # the next job is created later, so "the latest" is not a tie
    return job_id


def jobs_of(harness, type: str) -> list[Job]:
    with session_scope(harness.ctx.engine) as session:
        return list(session.scalars(select(Job).where(Job.type == type).order_by(Job.created_at)))


def ended(harness, type: str):
    """A Job as the worker's hook sees it right after it ended."""
    with session_scope(harness.ctx.engine) as session:
        return session.scalars(select(Job).where(Job.type == type).order_by(Job.created_at.desc())).first()


# inbox_empty


def test_the_seed_inbox_is_not_empty_and_an_emptied_one_is(harness) -> None:
    assert read_facts(harness.ctx).inbox_empty is False
    empty_the_inbox(harness)
    assert read_facts(harness.ctx).inbox_empty is True


def test_hidden_and_junk_files_do_not_count_as_a_waiting_inbox(harness) -> None:
    empty_the_inbox(harness)
    (harness.ideas / "inbox").mkdir()
    (harness.ideas / "inbox/.hidden.md").write_text("x\n")
    (harness.ideas / "inbox/.DS_Store").write_text("x\n")
    assert read_facts(harness.ctx).inbox_empty is True


# document_jobs_runnable and run_active


def test_runnable_document_jobs_are_the_due_ones_not_the_delayed_ones(harness) -> None:
    now = harness.clock()
    harness.add_job("llm.reason", calculated_name="notes/a.md")
    delayed = harness.add_job("llm.reason", calculated_name="notes/b.md")
    set_job(harness, delayed, run_after=now + timedelta(hours=2))
    running = harness.add_job("youtube.fetch", calculated_name="clippings/c.md")
    set_job(harness, running, status="running", locked_by="w", lease_until=now + timedelta(hours=1))
    harness.add_job("pipeline.publish")  # another type: not counted

    assert read_facts(harness.ctx).document_jobs_runnable == 2


def test_a_job_held_back_by_a_closed_resource_is_not_runnable_now(harness) -> None:
    now = harness.clock()
    job_id = harness.add_job("youtube.fetch", calculated_name="clippings/c.md")
    set_job(harness, job_id, resource="youtube")
    with session_scope(harness.ctx.engine) as session:
        session.execute(
            update(Resource)
            .where(Resource.name == "youtube")
            .values(next_allowed_at=now + timedelta(minutes=2))
        )
    assert read_facts(harness.ctx).document_jobs_runnable == 0

    harness.clock.advance(180)  # the gate opened
    assert read_facts(harness.ctx).document_jobs_runnable == 1


def test_run_active_is_a_queued_or_running_run(harness) -> None:
    assert read_facts(harness.ctx).run_active is False
    run = harness.add_job("pipeline.run")
    assert read_facts(harness.ctx).run_active is True
    set_job(harness, run, status="succeeded")
    assert read_facts(harness.ctx).run_active is False


# latest_run_limited and last_run_made_progress


def test_the_latest_run_decides_whether_the_cycle_was_limited(harness) -> None:
    assert read_facts(harness.ctx).latest_run_limited is False
    finished_job(harness, "pipeline.run", limit=3)
    assert read_facts(harness.ctx).latest_run_limited is True
    finished_job(harness, "pipeline.run", retry_deferred=True)
    assert read_facts(harness.ctx).latest_run_limited is False
    finished_job(harness, "pipeline.run", only=["a.md"])
    assert read_facts(harness.ctx).latest_run_limited is True
    finished_job(harness, "pipeline.run", requeue=["b.md"])
    assert read_facts(harness.ctx).latest_run_limited is True
    finished_job(harness, "pipeline.run", limit=0)  # a limit of 0 takes nothing, on purpose
    assert read_facts(harness.ctx).latest_run_limited is True


def test_a_run_that_took_no_document_made_no_progress(harness) -> None:
    assert read_facts(harness.ctx).last_run_made_progress is True  # no run yet
    finished_job(harness, "pipeline.run", result={"staged": 0, "adopted": 0, "duplicates": 0})
    assert read_facts(harness.ctx).last_run_made_progress is False
    finished_job(harness, "pipeline.run", result={"staged": 2})
    assert read_facts(harness.ctx).last_run_made_progress is True
    finished_job(harness, "pipeline.run", result={"staged": 0, "duplicates": 1})  # a duplicate left the inbox
    assert read_facts(harness.ctx).last_run_made_progress is True


# deferred_due


def test_a_deferred_or_stuck_item_is_due_but_a_stuck_backfill_one_is_not(harness) -> None:
    assert read_facts(harness.ctx).deferred_due is False
    add_item(harness, "stuck", origin="backfill")
    assert read_facts(harness.ctx).deferred_due is False
    add_item(harness, "deferred")
    assert read_facts(harness.ctx).deferred_due is True


def test_an_active_item_nobody_carries_is_due_for_recovery(harness) -> None:
    name = add_item(harness, "waiting_llm")
    assert read_facts(harness.ctx).deferred_due is True
    harness.add_job("llm.reason", calculated_name=name)  # a queued job carries it
    assert read_facts(harness.ctx).deferred_due is False


# unpublished_work


def test_published_items_are_unpublished_work_until_a_publish_succeeded_after_them(harness) -> None:
    assert read_facts(harness.ctx).unpublished_work is False
    add_item(harness, "published")
    assert read_facts(harness.ctx).unpublished_work is True
    harness.clock.advance(60)
    finished_job(harness, "pipeline.publish")
    assert read_facts(harness.ctx).unpublished_work is False
    harness.clock.advance(60)
    add_item(harness, "published")  # published later than the last publish
    assert read_facts(harness.ctx).unpublished_work is True


def test_a_commit_the_remote_does_not_have_is_unpublished_work(harness, sh) -> None:
    finished_job(harness, "pipeline.publish")
    assert read_facts(harness.ctx).unpublished_work is False
    (harness.docs / "page.md").write_text("a page\n")
    sh(harness.docs, "add", "-A")
    sh(harness.docs, "commit", "-m", "a local commit")  # a push that failed earlier leaves this behind
    assert read_facts(harness.ctx).unpublished_work is True


# the flag of the setting


def test_the_push_setting_is_a_fact(harness) -> None:
    assert read_facts(harness.ctx).chain_publish_push is True
    harness.ctx.settings.chain_publish_push = False
    assert read_facts(harness.ctx).chain_publish_push is False


# queue_next


def test_a_pull_that_found_files_queues_one_run_with_the_chain_key(harness) -> None:
    harness.add_job("ideas.pull")
    pull = ended(harness, "ideas.pull")

    chosen = queue_next(harness.ctx, pull, "succeeded")

    assert chosen == Next("pipeline.run", {"retry_deferred": True}, "chain:run")
    [run] = jobs_of(harness, "pipeline.run")
    assert (run.status, run.params, run.dedupe_key) == ("queued", {"retry_deferred": True}, "chain:run")


def test_two_triggers_at_once_leave_one_active_job_per_key(harness) -> None:
    harness.add_job("ideas.pull")
    pull = ended(harness, "ideas.pull")

    queue_next(harness.ctx, pull, "succeeded")
    queue_next(harness.ctx, pull, "succeeded")

    assert len(jobs_of(harness, "pipeline.run")) == 1


def test_nothing_to_do_queues_nothing(harness) -> None:
    empty_the_inbox(harness)
    harness.add_job("ideas.pull")

    assert queue_next(harness.ctx, ended(harness, "ideas.pull"), "succeeded") is None
    assert jobs_of(harness, "pipeline.run") == []
    assert jobs_of(harness, "pipeline.publish") == []


def test_the_last_document_job_queues_a_publish_that_follows_the_settings(harness) -> None:
    harness.ctx.settings.chain_publish_push = False
    finished_job(harness, "pipeline.run", limit=3, result={"staged": 3})
    harness.add_job("llm.reason", calculated_name="notes/a.md")

    chosen = queue_next(harness.ctx, ended(harness, "llm.reason"), "succeeded")
    assert chosen is None  # the job that just "ended" is still queued here, so another one can run

    with session_scope(harness.ctx.engine) as session:
        session.execute(update(Job).where(Job.type == "llm.reason").values(status="succeeded"))
    chosen = queue_next(harness.ctx, ended(harness, "llm.reason"), "succeeded")

    assert chosen == Next(
        "pipeline.publish", {"pull": False, "push": False, "follow_up": False}, "chain:publish"
    )
    [publish] = jobs_of(harness, "pipeline.publish")
    assert publish.params == {"pull": False, "push": False, "follow_up": False}


# review fixes: read only the facts a job type needs, and judge progress by the latest finished run


def test_a_document_job_reads_no_inbox_and_no_git(harness, monkeypatch) -> None:
    from catcher.modules.worker import chain

    def refuse(*args, **kwargs):
        raise AssertionError("a document job must not scan the inbox or ask git")

    monkeypatch.setattr(chain, "scan_inbox", refuse)
    monkeypatch.setattr(chain, "_git_ahead", refuse)
    harness.add_job("llm.reason", calculated_name="notes/a.md")
    job = ended(harness, "llm.reason")
    set_job(harness, job.id, status="succeeded")

    chosen = queue_next(harness.ctx, job, "succeeded")

    assert chosen is not None and chosen.type == "pipeline.publish"


def test_a_lost_job_reads_no_facts_at_all(harness, monkeypatch) -> None:
    from catcher.modules.worker import chain

    monkeypatch.setattr(
        chain, "read_facts", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no facts"))
    )
    harness.add_job("pipeline.publish")

    assert queue_next(harness.ctx, ended(harness, "pipeline.publish"), "lost") is None


def test_a_newer_failed_run_means_no_progress_even_after_an_older_good_one(harness) -> None:
    finished_job(harness, "pipeline.run", result={"staged": 5})
    failed = harness.add_job("pipeline.run")
    set_job(harness, failed, status="failed", error="no inbox", finished_at=harness.clock())
    harness.clock.advance(1)

    assert read_facts(harness.ctx).last_run_made_progress is False


def test_the_run_key_dedupes_two_backfill_jobs(harness) -> None:
    for round_ in range(2):
        harness.add_job("backfill.docs")
        job = ended(harness, "backfill.docs")
        set_job(harness, job.id, status="succeeded", finished_at=harness.clock())
        harness.clock.advance(1)
        chosen = queue_next(harness.ctx, job, "succeeded")
        # the second job finds the first one's run still active: nothing more is queued
        assert (chosen is not None and chosen.type == "pipeline.run") == (round_ == 0)

    runs = jobs_of(harness, "pipeline.run")
    assert [r.status for r in runs] == ["queued"]
