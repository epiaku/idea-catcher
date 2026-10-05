"""The `llm.reason` handler: saved facts only, the saved reply first, the page, and the item outcome.

An LLM or facts problem is an item state (the job succeeds); the handler never calls YouTube and runs no git
command; running it twice gives one page and, thanks to the saved reply, one model call."""

import json
from pathlib import Path

import pytest
from sqlalchemy import select, update
from worker_harness import GEMINI_CHAT, NOTES, WEB_CLIPS, RaisingBackend

from catcher.core.db import session_scope
from catcher.core.frontmatter import Doc, dump, load
from catcher.modules.llm.backends.fake import CANNED, FakeBackend
from catcher.modules.llm.service import BackendUnavailable, BudgetExhausted, UsageLimitReached
from catcher.modules.pipeline.mirror import MirrorState, read_mirror
from catcher.modules.queue.items import set_item_status
from catcher.modules.queue.models import Job, JobEvent, JobItem
from catcher.modules.worker import handlers_pipeline
from catcher.modules.worker.app import build_handlers
from catcher.modules.worker.handlers import Done, Fail
from catcher.modules.worker.handlers_pipeline import (
    handle_llm_reason,
    handle_pipeline_run,
    handle_youtube_fetch,
)
from catcher.modules.youtube.facts import FactsUnavailable

BAD_PAGE = json.dumps({**CANNED["note"], "body": "{{< nope >}}"})  # a valid reply, but an unknown shortcode


def stage(harness, **params) -> None:
    """Run `pipeline.run` with `params` (its job then ends succeeded, so only the next jobs stay queued)."""
    job_id = harness.add_job("pipeline.run", **params)
    with session_scope(harness.ctx.engine) as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert isinstance(handle_pipeline_run(harness.ctx, job), Done)
        session.execute(update(Job).where(Job.id == job_id).values(status="succeeded"))


def item_of(harness, original: str) -> JobItem:
    with session_scope(harness.ctx.engine) as session:
        [item] = [i for i in session.scalars(select(JobItem)) if i.original_filename == original]
        return item


def jobs_of(harness, type: str) -> list[Job]:
    with session_scope(harness.ctx.engine) as session:
        return [j for j in harness.jobs(session) if j.type == type]


def reason_job(harness, name: str, status: str = "queued") -> Job:
    """The `llm.reason` job of `name` in `status` (the dedupe key allows one queued at a time)."""
    [job] = [
        j
        for j in jobs_of(harness, "llm.reason")
        if j.params["calculated_name"] == name and j.status == status
    ]
    return job


def reason(harness, name: str):
    """Run the queued `llm.reason` job of `name` through the handler directly, then mark it succeeded (as the
    worker would), so a later `pipeline.run` can queue a new one under the same dedupe key."""
    job = reason_job(harness, name)
    result = handle_llm_reason(harness.ctx, job)
    with session_scope(harness.ctx.engine) as session:
        session.execute(update(Job).where(Job.id == job.id).values(status="succeeded"))
    return result


def rerun(harness, name: str):
    """Run the (only) finished `llm.reason` job of `name` again: a crash or a lost lease made the worker
    run it a second time."""
    return handle_llm_reason(harness.ctx, reason_job(harness, name, status="succeeded"))


def pages(docs: Path, folder: str) -> list[Path]:
    return sorted(p for p in (docs / folder).glob("*.md") if p.name != "_index.md")


def staged_note(harness, **params) -> str:
    stage(harness, only=["YouTube walks"], **params)
    item = item_of(harness, "YouTube walks.md")
    assert item.status == "waiting_llm"
    return item.calculated_name


def test_the_handler_is_registered():
    assert build_handlers()["llm.reason"] is handle_llm_reason


def test_llm_reason_publishes_a_note_page_and_marks_the_item_published(harness, sh):
    name = staged_note(harness)

    assert reason(harness, name) == Done({"item": "published"})

    [page] = pages(harness.docs, NOTES)
    final = harness.ideas / "output" / name
    assert load(final).fm["stage"] == "published"
    assert final.read_text(encoding="utf-8") == page.read_text(encoding="utf-8")
    assert item_of(harness, "YouTube walks.md").status == "published"
    assert harness.fetch_calls == []
    # no git command: the changes are only in the files (pipeline.publish commits them)
    assert sh(harness.docs, "status", "--porcelain") != ""
    assert sh(harness.docs, "rev-list", "--count", "HEAD").strip() == "1"


def test_the_whole_flow_runs_through_the_worker(harness):
    harness.add_job("pipeline.run", only=["YouTube walks"])
    assert harness.drain(max_jobs=3) == ["succeeded", "succeeded"]
    assert item_of(harness, "YouTube walks.md").status == "published"
    assert len(pages(harness.docs, NOTES)) == 1


def test_the_same_job_run_twice_gives_one_page(harness):
    name = staged_note(harness)
    job = reason_job(harness, name)

    assert handle_llm_reason(harness.ctx, job) == Done({"item": "published"})
    [page] = pages(harness.docs, NOTES)
    first = page.read_bytes()
    assert handle_llm_reason(harness.ctx, job) == Done({"item": "published"})

    assert pages(harness.docs, NOTES) == [page] and page.read_bytes() == first
    assert len(harness.backends.note.prompts) == 1


def test_a_crash_after_the_page_and_before_the_item_update_is_finished_by_the_rerun(harness):
    name = staged_note(harness)
    assert reason(harness, name) == Done({"item": "published"})
    [page] = pages(harness.docs, NOTES)
    with session_scope(harness.ctx.engine) as session:  # the crash: the status update never happened
        set_item_status(session, name, "waiting_llm", now=harness.clock())

    assert rerun(harness, name) == Done({"item": "published"})
    assert pages(harness.docs, NOTES) == [page]
    assert len(harness.backends.note.prompts) == 1
    assert item_of(harness, "YouTube walks.md").status == "published"


def test_a_second_run_reuses_the_saved_reply_and_never_calls_the_model(harness):
    name = staged_note(harness)
    working_copy = (harness.ideas / "output" / name).read_bytes()
    assert reason(harness, name) == Done({"item": "published"})
    [page] = pages(harness.docs, NOTES)
    published = page.read_bytes()

    # a crash between write_page and finish: output/ still holds the working copy, the item is waiting_llm
    (harness.ideas / "output" / name).write_bytes(working_copy)
    with session_scope(harness.ctx.engine) as session:
        set_item_status(session, name, "waiting_llm", now=harness.clock())
    calls: list[str] = []
    harness.backends.note = RaisingBackend(calls)

    assert rerun(harness, name) == Done({"item": "published"})
    assert calls == []
    assert pages(harness.docs, NOTES) == [page] and page.read_bytes() == published
    assert load(harness.ideas / "output" / name).fm["stage"] == "published"


def test_refresh_llm_in_the_job_params_calls_the_model_again(harness):
    name = staged_note(harness)
    assert reason(harness, name) == Done({"item": "published"})
    assert len(harness.backends.note.prompts) == 1

    stage(harness, requeue=["YouTube walks"], refresh_llm=True)
    assert reason_job(harness, name).params == {"calculated_name": name, "refresh_llm": True}
    harness.backends.note = FakeBackend()
    assert reason(harness, name) == Done({"item": "published"})
    assert len(harness.backends.note.prompts) == 1
    assert len(pages(harness.docs, NOTES)) == 1


def test_a_backend_outage_defers_the_item_and_succeeds_the_job(harness):
    harness.backends.note = FakeBackend([BackendUnavailable("the provider is down")])
    harness.add_job("pipeline.run", only=["YouTube walks"])

    assert harness.drain(max_jobs=3) == ["succeeded", "succeeded"]

    item = item_of(harness, "YouTube walks.md")
    assert item.status == "deferred" and "the provider is down" in (item.error or "")
    out = load(harness.ideas / "output" / item.calculated_name).fm
    assert out["stage"] == "deferred" and "the provider is down" in out["deferred_reason"]
    assert pages(harness.docs, NOTES) == []
    [job] = jobs_of(harness, "llm.reason")
    assert (job.status, job.result) == ("succeeded", {"item": "deferred"})


def test_invalid_output_moves_the_note_to_failed(harness):
    harness.backends.note = FakeBackend(["nope", "still nope"])
    name = staged_note(harness)

    assert reason(harness, name) == Done({"item": "failed"})

    item = item_of(harness, "YouTube walks.md")
    assert item.status == "failed" and "invalid output" in (item.error or "")
    assert not (harness.ideas / "output" / name).exists()
    failed = harness.ideas / "failed" / name
    assert failed.is_file() and "invalid output" in failed.with_suffix(".error.txt").read_text()
    assert pages(harness.docs, NOTES) == []
    # a rerun of the job leaves it failed and does not ask again
    assert rerun(harness, name) == Done({"item": "failed"})
    assert len(harness.backends.note.prompts) == 2


def test_an_invalid_page_marks_the_trace_invalid_page_and_a_requeue_calls_the_model(harness):
    harness.backends.note = FakeBackend([BAD_PAGE])
    name = staged_note(harness)

    assert reason(harness, name) == Done({"item": "failed"})

    item = item_of(harness, "YouTube walks.md")
    assert item.status == "failed" and (item.error or "").startswith("page is invalid: ")
    assert (harness.ideas / "failed" / name).is_file() and pages(harness.docs, NOTES) == []
    trace = json.loads((harness.ideas / "llm" / name).with_suffix(".json").read_text())
    assert trace["outcome"] == "invalid_page" and trace["error"].startswith("page is invalid: ")

    stage(harness, requeue=["YouTube walks"])
    harness.backends.note = FakeBackend()
    assert reason(harness, name) == Done({"item": "published"})
    assert len(harness.backends.note.prompts) == 1  # the marked reply is not reused
    assert len(pages(harness.docs, NOTES)) == 1


def test_the_trace_file_is_written_next_to_the_page(harness):
    name = staged_note(harness)
    assert reason(harness, name) == Done({"item": "published"})

    trace = (harness.ideas / "llm" / name).with_suffix(".json")
    assert trace.is_file()
    assert json.loads(trace.read_text())["outcome"] == "ok"
    assert (harness.ideas / "output" / name).is_file()


def test_a_youtube_clip_without_saved_facts_requeues_the_fetch_and_makes_no_call(harness, yt_facts):
    facts = harness.ideas / "facts" / "nGVZS_wUDGM.json"
    harness.ctx.services.youtube.cache(harness.ideas / "facts").put(yt_facts)
    stage(harness, only=["yt"], profile="fake")
    item = item_of(harness, "yt.md")
    assert item.status == "waiting_llm" and jobs_of(harness, "youtube.fetch") == []
    facts.unlink()  # the saved facts are gone (lost, or never committed)

    assert reason(harness, item.calculated_name) == Done({"item": "waiting_youtube"})

    assert harness.fetch_calls == []
    assert harness.backends.chat.prompts == [] and harness.backends.note.prompts == []
    assert item_of(harness, "yt.md").status == "waiting_youtube"
    [fetch] = jobs_of(harness, "youtube.fetch")
    assert (fetch.status, fetch.params, fetch.dedupe_key) == (
        "queued",
        {"calculated_name": item.calculated_name, "profile": "fake"},
        f"fetch:{item.calculated_name}",
    )
    out = load(harness.ideas / "output" / item.calculated_name).fm
    assert out["stage"] == "waiting_youtube"  # the working copy waits in output/ for the facts (mirrored)


def test_a_youtube_clip_with_saved_facts_publishes(harness, yt_facts):
    harness.ctx.services.youtube.cache(harness.ideas / "facts").put(yt_facts)
    stage(harness, only=["yt"])
    item = item_of(harness, "yt.md")

    assert reason(harness, item.calculated_name) == Done({"item": "published"})

    assert harness.fetch_calls == []
    assert len(harness.backends.chat.prompts) == 1
    assert item_of(harness, "yt.md").status == "published"
    youtube = Path(WEB_CLIPS).parent / "youtube"
    assert len(pages(harness.docs, youtube.as_posix())) == 1


def test_a_missing_item_fails_the_job(harness):
    job_id = harness.add_job("llm.reason", calculated_name="notes/20261002-abcdef-nothing.md")
    with session_scope(harness.ctx.engine) as session:
        job = session.get(Job, job_id)
    assert isinstance(handle_llm_reason(harness.ctx, job), Fail)


def test_bad_params_fail_the_job(harness):
    for params in ({}, {"calculated_name": "../x.md"}, {"calculated_name": "notes/x.md", "other": 1}):
        job_id = harness.add_job("llm.reason", **params)
        with session_scope(harness.ctx.engine) as session:
            job = session.get(Job, job_id)
        assert isinstance(handle_llm_reason(harness.ctx, job), Fail), params


def test_the_frozen_data_publishes_from_saved_facts_and_replies_with_no_external_call(frozen_harness):
    harness = frozen_harness
    stage(harness, limit=3)
    names = [j.params["calculated_name"] for j in jobs_of(harness, "llm.reason")]
    assert names
    for name in names:
        assert reason(harness, name) == Done({"item": "published"}), name
    assert harness.model_calls == [] and harness.fetch_calls == []


def test_an_unexpected_error_fails_the_item_and_the_job(harness):
    harness.backends.note = FakeBackend([RuntimeError("a bug")])
    name = staged_note(harness)

    result = reason(harness, name)

    assert isinstance(result, Fail) and "a bug" in result.error
    item = item_of(harness, "YouTube walks.md")
    assert item.status == "failed" and "a bug" in (item.error or "")
    assert (harness.ideas / "failed" / name).is_file() and not (harness.ideas / "output" / name).exists()


def test_a_file_error_writing_the_page_fails_the_item_and_the_job_and_a_requeue_recovers(
    harness, monkeypatch
):
    name = staged_note(harness)

    def no_disk(*args, **kwargs):
        raise PermissionError("epiaku-docs is read-only")

    monkeypatch.setattr(handlers_pipeline, "write_page", no_disk)
    result = reason(harness, name)

    assert isinstance(result, Fail) and "epiaku-docs is read-only" in result.error
    item = item_of(harness, "YouTube walks.md")
    assert item.status == "failed" and "epiaku-docs is read-only" in (item.error or "")

    monkeypatch.undo()
    stage(harness, requeue=["YouTube walks"])  # a failed item can be requeued
    assert item_of(harness, "YouTube walks.md").status == "waiting_llm"
    assert reason(harness, name) == Done({"item": "published"})
    assert len(pages(harness.docs, NOTES)) == 1


def test_a_file_error_reading_the_working_copy_fails_the_item_and_the_job(harness, monkeypatch):
    name = staged_note(harness)

    def unreadable(*args, **kwargs):
        raise OSError("disk error")

    monkeypatch.setattr(handlers_pipeline, "load_staged_note", unreadable)
    result = reason(harness, name)

    assert isinstance(result, Fail) and "disk error" in result.error
    item = item_of(harness, "YouTube walks.md")
    assert item.status == "failed" and "disk error" in (item.error or "")


def test_the_worker_records_a_file_error_as_a_failed_job(harness, monkeypatch):
    def no_disk(*args, **kwargs):
        raise PermissionError("read-only")

    monkeypatch.setattr(handlers_pipeline, "write_page", no_disk)
    harness.add_job("pipeline.run", only=["YouTube walks"])
    assert harness.drain(max_jobs=3) == ["succeeded", "failed"]
    assert item_of(harness, "YouTube walks.md").status == "failed"


def test_a_missing_working_copy_fails_the_item(harness):
    name = staged_note(harness)
    (harness.ideas / "output" / name).unlink()

    assert reason(harness, name) == Done({"item": "failed"})
    item = item_of(harness, "YouTube walks.md")
    assert item.status == "failed" and item.error == f"no working copy in output/{name}"
    assert harness.backends.note.prompts == []


def test_a_working_copy_already_in_failed_keeps_its_reason(harness):
    name = staged_note(harness)
    out, failed = harness.ideas / "output" / name, harness.ideas / "failed" / name
    failed.parent.mkdir(parents=True, exist_ok=True)
    out.rename(failed)  # a crash after the move to failed/, before the status commit
    failed.with_suffix(".error.txt").write_text("time: x\nreason: invalid output: nope\n", encoding="utf-8")

    assert reason(harness, name) == Done({"item": "failed"})
    item = item_of(harness, "YouTube walks.md")
    assert (item.status, item.error) == ("failed", "invalid output: nope")
    assert harness.backends.note.prompts == []


def test_an_unreadable_working_copy_moves_to_failed(harness):
    name = staged_note(harness)
    (harness.ideas / "output" / name).write_text("---\nid: [unclosed\n---\nbody\n", encoding="utf-8")

    assert reason(harness, name) == Done({"item": "failed"})
    item = item_of(harness, "YouTube walks.md")
    assert item.status == "failed" and (item.error or "").startswith(f"cannot read output/{name}")
    assert (harness.ideas / "failed" / name).is_file() and not (harness.ideas / "output" / name).exists()


def test_a_rerun_of_a_deferred_working_copy_publishes(harness):
    harness.backends.note = FakeBackend([BackendUnavailable("down")])
    name = staged_note(harness)
    assert reason(harness, name) == Done({"item": "deferred"})
    assert load(harness.ideas / "output" / name).fm["stage"] == "deferred"
    with session_scope(harness.ctx.engine) as session:  # a crash before the deferred status was committed
        set_item_status(session, name, "waiting_llm", now=harness.clock())
    harness.clock.advance(harness.ctx.settings.llm_block_s + 1)  # the outage blocked the backend for a while

    assert rerun(harness, name) == Done({"item": "published"})
    assert load(harness.ideas / "output" / name).fm["stage"] == "published"
    assert len(pages(harness.docs, NOTES)) == 1


GOOD_NAMES = [
    "20261002-abcdef-x.md",  # a capture directly in inbox/
    "notes/20261002-abcdef-x.md",
    "clippings/2026/20261002-abcdef-x.md",  # a capture in a nested folder
    "clippings/a b/2026/deep/20261002-abcdef-x.md",
]
TRAVERSAL_NAMES = [
    "../x.md",
    "notes/../x.md",
    "notes/../../../outside.md",
    "/etc/x.md",
    "/x.md",
    "notes//x.md",
    "notes/./x.md",
    "./x.md",
    "notes\\..\\..\\x.md",
    "..\\x.md",
    "notes/x.md/",
    ".hidden/x.md",
    "notes/.x.md",
    "notes/x.txt",
    "notes/.md",
    "notes/x\x00.md",
    "",
    3,
    None,
]


@pytest.mark.parametrize("name", GOOD_NAMES)
def test_every_name_assign_name_can_make_is_accepted(name):
    assert handlers_pipeline.parse_reason_params({"calculated_name": name}).calculated_name == name


@pytest.mark.parametrize("name", TRAVERSAL_NAMES)
def test_a_name_that_could_leave_the_ideas_folder_is_refused(harness, name):
    with pytest.raises(ValueError, match="calculated_name"):
        handlers_pipeline.parse_reason_params({"calculated_name": name})
    job = Job(type="llm.reason", params={"calculated_name": name})  # Postgres cannot even store a NUL
    result = handle_llm_reason(harness.ctx, job)
    assert isinstance(result, Fail) and "calculated_name" in result.error
    assert harness.backends.note.prompts == [] and harness.backends.chat.prompts == []


def test_a_top_level_and_a_nested_capture_publish_through_the_worker(harness, sh):
    """A capture directly in inbox/ and one in a nested folder: pipeline.run, drain, publish."""
    (harness.ideas / "inbox/clippings/2026").mkdir(parents=True)
    (harness.ideas / "inbox/top.md").write_text("A capture right in the inbox\n", encoding="utf-8")
    (harness.ideas / "inbox/clippings/2026/nested.md").write_text("A capture in a nested folder\n")
    sh(harness.ideas, "add", "-A")
    sh(harness.ideas, "commit", "-qm", "two more captures")
    sh(harness.ideas, "push", "-q")

    harness.add_job("pipeline.run", only=["top", "nested"])
    assert harness.drain(max_jobs=4) == ["succeeded"] * 3
    top, nested = item_of(harness, "top.md"), item_of(harness, "nested.md")
    assert "/" not in top.calculated_name
    assert nested.calculated_name.startswith("clippings/2026/")
    assert (top.status, nested.status) == ("published", "published")
    for item in (top, nested):
        assert (harness.ideas / "output" / item.calculated_name).is_file()
        assert (harness.ideas / "archive" / item.calculated_name).is_file()
    assert len(pages(harness.docs, NOTES)) == 2

    harness.add_job("pipeline.publish")
    assert harness.drain(max_jobs=2) == ["succeeded"]
    for repo in (harness.ideas, harness.docs):
        assert sh(repo, "status", "--porcelain") == ""
    assert sh(harness.ideas, "rev-list", "--count", "HEAD", "^origin/main").strip() == "0"  # pushed
    published = sh(harness.ideas, "ls-files", "output").splitlines()
    assert f"output/{top.calculated_name}" in published and f"output/{nested.calculated_name}" in published


# A backend that is down, rate limited or out of budget is not called again for LLM_BLOCK_S (F4)

BLOCKED_UNTIL = "2026-10-02T12:10:00+00:00"  # the frozen clock (12:00) plus the default 600 s


def two_notes(harness, *others: str) -> tuple[str, str]:
    """Stage "YouTube walks" and a second note (plus `others`); both use the notes profile (the `fake`
    backend in the tests)."""
    (harness.ideas / "inbox/notes/second.md").write_text("A second idea to write up\n", encoding="utf-8")
    stage(harness, only=["YouTube walks", "second", *others])
    return item_of(harness, "YouTube walks.md").calculated_name, item_of(harness, "second.md").calculated_name


def test_a_usage_limit_blocks_the_backend_and_the_next_document_makes_no_call(harness):
    first, second = two_notes(harness)
    harness.backends.note = FakeBackend([UsageLimitReached("freellmapi rate limit: 429", backend="fake")])

    assert reason(harness, first) == Done({"item": "deferred"})
    assert reason(harness, second) == Done({"item": "deferred"})

    assert len(harness.backends.note.prompts) == 1  # only the first document called the backend
    item = item_of(harness, "second.md")
    assert item.status == "deferred"
    assert f"fake: not called again until {BLOCKED_UNTIL}" in (item.error or "")
    assert "429" in (item.error or "")  # the cause of the block
    out = load(harness.ideas / "output" / second).fm
    assert out["stage"] == "deferred" and "not called again" in out["deferred_reason"]


def test_a_used_up_budget_blocks_the_backend_through_the_worker(harness):
    (harness.ideas / "inbox/notes/second.md").write_text("A second idea to write up\n", encoding="utf-8")
    harness.backends.note = FakeBackend([BudgetExhausted("freellmapi budget reached", backend="fake")])
    harness.add_job("pipeline.run", only=["YouTube walks", "second"])

    assert harness.drain(max_jobs=4) == ["succeeded"] * 3
    assert len(harness.backends.note.prompts) == 1
    assert [j.result for j in jobs_of(harness, "llm.reason")] == [{"item": "deferred"}] * 2
    # the two notes have random ids and one capture time, so either may run first
    errors = [item_of(harness, name).error or "" for name in ("YouTube walks.md", "second.md")]
    assert len([e for e in errors if "budget reached" in e and "not called again" not in e]) == 1
    assert len([e for e in errors if f"fake: not called again until {BLOCKED_UNTIL}" in e]) == 1


def test_a_backend_that_is_down_is_not_called_for_the_next_document(harness):
    first, second = two_notes(harness)
    harness.backends.note = FakeBackend([BackendUnavailable("freellmapi unreachable: connection refused")])

    assert reason(harness, first) == Done({"item": "deferred"})
    assert reason(harness, second) == Done({"item": "deferred"})

    assert len(harness.backends.note.prompts) == 1
    error = item_of(harness, "second.md").error or ""
    assert "not called again" in error and "connection refused" in error


def test_after_the_cool_down_one_probe_is_made_and_a_failure_blocks_again(harness):
    harness.ctx.settings = harness.ctx.settings.model_copy(update={"llm_block_s": 60})
    first, second = two_notes(harness)
    harness.backends.note = FakeBackend([BackendUnavailable("down")])
    assert reason(harness, first) == Done({"item": "deferred"})
    assert reason(harness, second) == Done({"item": "deferred"})
    assert len(harness.backends.note.prompts) == 1

    harness.clock.advance(61)  # the cool-down is over: the next document tries once (a probe)
    stage(harness, requeue=["YouTube walks", "second"])
    harness.backends.note = FakeBackend([BackendUnavailable("still down")])
    assert reason(harness, first) == Done({"item": "deferred"})
    assert reason(harness, second) == Done({"item": "deferred"})
    assert len(harness.backends.note.prompts) == 1  # the probe failed: blocked again, no second call

    harness.clock.advance(61)
    stage(harness, requeue=["YouTube walks", "second"])
    harness.backends.note = FakeBackend()  # it works again
    assert reason(harness, first) == Done({"item": "published"})
    assert reason(harness, second) == Done({"item": "published"})
    assert len(harness.backends.note.prompts) == 2
    assert len(pages(harness.docs, NOTES)) == 2


def test_a_saved_reply_is_served_while_its_backend_is_blocked(harness):
    first = staged_note(harness)
    assert reason(harness, first) == Done({"item": "published"})  # its good reply is saved in llm/
    [page] = pages(harness.docs, NOTES)

    (harness.ideas / "inbox/notes/second.md").write_text("A second idea to write up\n", encoding="utf-8")
    stage(harness, only=["second"])
    second = item_of(harness, "second.md").calculated_name
    harness.backends.note = FakeBackend([UsageLimitReached("429", backend="fake")])
    assert reason(harness, second) == Done({"item": "deferred"})  # its backend is now blocked

    stage(harness, requeue=["YouTube walks"])
    assert reason(harness, first) == Done({"item": "published"})
    assert len(harness.backends.note.prompts) == 1  # only the second document's call: the first was saved
    assert pages(harness.docs, NOTES) == [page]


def test_a_block_of_one_backend_leaves_the_other_backends_alone(harness):
    first, second = two_notes(harness, "systeme")
    harness.backends.note = FakeBackend([UsageLimitReached("429", backend="fake")])
    assert reason(harness, first) == Done({"item": "deferred"})

    chat = item_of(harness, "systeme.md").calculated_name  # the Gemini chat: clippings profile, openai
    assert reason(harness, chat) == Done({"item": "published"})
    assert len(harness.backends.chat.prompts) == 1


def test_a_block_does_not_defer_a_document_run_with_the_fake_profile(harness):
    stage(harness, only=["systeme"])  # the Gemini chat: clippings profile, openai
    chat = item_of(harness, "systeme.md").calculated_name
    harness.backends.chat = FakeBackend([UsageLimitReached("openai rate limit: 429", backend="openai")])
    assert reason(harness, chat) == Done({"item": "deferred"})
    assert harness.ctx.backend_blocks.active(harness.clock()) == frozenset({"openai"})

    other = GEMINI_CHAT.replace("cf81e40b020519ef", "925d9b0b4ca21b63")  # another chat, same class
    (harness.ideas / "inbox/clippings/other.md").write_text(other, encoding="utf-8")
    stage(harness, only=["other"], profile="fake")
    assert reason(harness, item_of(harness, "other.md").calculated_name) == Done({"item": "published"})
    assert len(harness.backends.note.prompts) == 1 and len(harness.backends.chat.prompts) == 1


def test_invalid_output_does_not_block_the_backend(harness):
    first, second = two_notes(harness)
    harness.backends.note = FakeBackend(["nope", "still nope"])

    assert reason(harness, first) == Done({"item": "failed"})
    assert reason(harness, second) == Done({"item": "published"})
    assert len(harness.backends.note.prompts) == 3


def test_a_document_deferred_for_its_facts_does_not_block_the_backend(harness, yt_facts):
    harness.ctx.services.youtube.cache(harness.ideas / "facts").put(
        yt_facts.model_copy(update={"transcript": None})  # saved facts without a transcript: deferred
    )
    stage(harness, only=["yt"])
    clip = item_of(harness, "yt.md").calculated_name
    assert reason(harness, clip) == Done({"item": "deferred"})

    assert harness.ctx.backend_blocks.active(harness.clock()) == frozenset()


def test_a_document_deferred_by_a_running_block_does_not_extend_it(harness):
    """Otherwise the backend is never tried again while documents keep coming."""
    first, second = two_notes(harness)
    harness.backends.note = FakeBackend([UsageLimitReached("429", backend="fake")])
    assert reason(harness, first) == Done({"item": "deferred"})
    until = harness.ctx.backend_blocks.entries(harness.clock())["fake"].until

    harness.clock.advance(30)
    assert reason(harness, second) == Done({"item": "deferred"})  # deferred by the block, no call

    assert len(harness.backends.note.prompts) == 1
    assert harness.ctx.backend_blocks.entries(harness.clock())["fake"].until == until


def test_refresh_llm_while_the_backend_is_blocked_is_deferred_with_no_call(harness):
    first = staged_note(harness)
    assert reason(harness, first) == Done({"item": "published"})  # a good reply is saved

    (harness.ideas / "inbox/notes/second.md").write_text("A second idea to write up\n", encoding="utf-8")
    stage(harness, only=["second"])
    harness.backends.note = FakeBackend([UsageLimitReached("429", backend="fake")])
    assert reason(harness, item_of(harness, "second.md").calculated_name) == Done({"item": "deferred"})

    stage(harness, requeue=["YouTube walks"], refresh_llm=True)  # skip the saved reply: needs a call
    assert reason(harness, first) == Done({"item": "deferred"})
    assert len(harness.backends.note.prompts) == 1  # only the 429 of the second note: no call for the first
    assert "not called again" in (item_of(harness, "YouTube walks.md").error or "")


# --- B5: every status change goes through the one writer, and is mirrored into the frontmatter ---

LEVELS = {"deferred": "warning", "stuck": "warning", "failed": "error"}
MIRROR_KEYS = ("stage_reason", "stage_since")


def events_of(harness, name: str) -> list[JobEvent]:
    with session_scope(harness.ctx.engine) as session:
        item_id = session.scalars(select(JobItem.id).where(JobItem.calculated_name == name)).one()
        return list(
            session.scalars(select(JobEvent).where(JobEvent.item_id == item_id).order_by(JobEvent.id))
        )


def agrees(harness, original: str, folder: str = "output") -> JobItem:
    """The item row, its last event and the frontmatter of its file in `folder` tell the same state."""
    item = item_of(harness, original)
    last = events_of(harness, item.calculated_name)[-1]
    assert last.message.startswith(f"{item.calculated_name}: ") and f" -> {item.status}" in last.message
    assert (last.level, last.ts, last.job_id is not None) == (
        LEVELS.get(item.status, "info"),
        item.stage_since,
        True,
    )
    mirrored = read_mirror(harness.ideas / folder / item.calculated_name)
    assert mirrored == MirrorState(item.status, item.stage_reason, item.stage_since)
    return item


def private_video(video_id: str):
    raise FactsUnavailable("Private video")


def test_every_status_change_of_the_handlers_goes_through_the_writer(harness):
    harness.backends.chat = FakeBackend(["nope", "still nope"])  # the Gemini chat: invalid output, failed
    harness.fetcher = private_video  # the clip: no facts to be had, deferred
    stage(harness, only=["YouTube walks", "systeme", "yt"])
    note = agrees(harness, "YouTube walks.md")
    chat = agrees(harness, "systeme.md")
    clip = agrees(harness, "yt.md")
    assert (note.status, chat.status, clip.status) == ("waiting_llm", "waiting_llm", "waiting_youtube")
    assert note.stage_reason is None and note.stage_since == harness.clock()

    harness.clock.advance(60)
    assert reason(harness, note.calculated_name) == Done({"item": "published"})
    published = item_of(harness, "YouTube walks.md")
    assert (published.status, published.stage_since) == ("published", harness.clock())
    assert [e.message for e in events_of(harness, note.calculated_name)] == [
        f"{note.calculated_name}: staging -> waiting_llm",
        f"{note.calculated_name}: waiting_llm -> published",
    ]
    [page] = pages(harness.docs, NOTES)
    final = harness.ideas / "output" / note.calculated_name
    assert final.read_bytes() == page.read_bytes()  # the finished page is the docs page, untouched
    assert not any(key in load(page).fm for key in MIRROR_KEYS)

    assert reason(harness, chat.calculated_name) == Done({"item": "failed"})
    failed = agrees(harness, "systeme.md", folder="failed")
    assert failed.status == "failed" and "invalid output" in (failed.stage_reason or "")

    [fetch] = jobs_of(harness, "youtube.fetch")
    assert handle_youtube_fetch(harness.ctx, fetch) == Done({"item": "deferred"})
    deferred = agrees(harness, "yt.md")
    assert deferred.status == "deferred" and "Private video" in (deferred.stage_reason or "")
    assert events_of(harness, clip.calculated_name)[-1].job_id == fetch.id
    out = load(harness.ideas / "output" / clip.calculated_name).fm
    assert out["deferred_reason"] == out["stage_reason"]  # the Stage A key stays, the new ones on top


def test_the_finished_page_gets_no_mirror_keys(harness):
    harness.backends.note = FakeBackend([BackendUnavailable("down")])
    name = staged_note(harness)
    assert reason(harness, name) == Done({"item": "deferred"})
    assert {"stage_reason", "stage_since"} <= set(load(harness.ideas / "output" / name).fm)
    with session_scope(harness.ctx.engine) as session:  # a crash before the deferred status was committed
        set_item_status(session, name, "waiting_llm", now=harness.clock())
    harness.clock.advance(harness.ctx.settings.llm_block_s + 1)

    assert rerun(harness, name) == Done({"item": "published"})

    [page] = pages(harness.docs, NOTES)
    final = harness.ideas / "output" / name
    assert final.read_bytes() == page.read_bytes()
    assert not any(key in load(final).fm for key in (*MIRROR_KEYS, "deferred_reason", "deferred_at"))


def _read_only_after_start_work(monkeypatch, folders: list[Path]) -> None:
    """`start_work` runs, then the folder of the working copy becomes read-only: the mirror cannot write."""
    real = handlers_pipeline.start_work

    def start_then_lock(ideas: Path, note, now=None):
        touched = real(ideas, note, now)
        folder = note.output_path(ideas).parent
        folder.chmod(0o555)
        folders.append(folder)
        return touched

    monkeypatch.setattr(handlers_pipeline, "start_work", start_then_lock)


def test_a_failing_mirror_does_not_fail_the_job(harness, monkeypatch, caplog):
    folders: list[Path] = []
    _read_only_after_start_work(monkeypatch, folders)
    harness.add_job("pipeline.run", only=["YouTube walks"])
    try:
        with caplog.at_level("WARNING", logger="catcher.queue"):
            assert harness.worker.run_once() == "succeeded"
    finally:
        for folder in folders:
            folder.chmod(0o755)

    [run_job] = jobs_of(harness, "pipeline.run")
    assert run_job.status == "succeeded"
    item = item_of(harness, "YouTube walks.md")
    assert item.status == "waiting_llm"  # the transition is committed
    assert [e.message for e in events_of(harness, item.calculated_name)] == [
        f"{item.calculated_name}: staging -> waiting_llm"
    ]
    assert load(harness.ideas / "output" / item.calculated_name).fm["stage"] == "analyzed"  # not mirrored
    assert any(
        r.levelname == "WARNING" and "could not mirror the state of" in r.getMessage() for r in caplog.records
    )


def test_a_missing_working_copy_only_logs_a_warning(harness, caplog):
    name = staged_note(harness)
    (harness.ideas / "output" / name).unlink()

    with caplog.at_level("WARNING"):
        assert reason(harness, name) == Done({"item": "failed"})

    item = item_of(harness, "YouTube walks.md")
    assert item.status == "failed" and events_of(harness, name)[-1].level == "error"
    assert not (harness.ideas / "failed" / name).exists()
    assert any("could not mirror" in r.getMessage() and name in r.getMessage() for r in caplog.records)


def test_the_next_transition_repairs_the_mirror(harness, monkeypatch):
    folders: list[Path] = []
    _read_only_after_start_work(monkeypatch, folders)
    try:
        stage(harness, only=["YouTube walks"])
    finally:
        for folder in folders:
            folder.chmod(0o755)
    name = item_of(harness, "YouTube walks.md").calculated_name
    assert read_mirror(harness.ideas / "output" / name).stage == "analyzed"  # the mirror write failed

    harness.backends.note = FakeBackend([BackendUnavailable("the provider is down")])
    harness.clock.advance(30)
    assert reason(harness, name) == Done({"item": "deferred"})

    item = agrees(harness, "YouTube walks.md")
    assert item.stage_since == harness.clock() and "the provider is down" in (item.stage_reason or "")


@pytest.mark.parametrize("stage_value", ["analyzed", "waiting_llm", "waiting_youtube", "deferred", "stuck"])
def test_a_working_copy_with_any_working_stage_is_still_processed(harness, stage_value):
    name = staged_note(harness)
    out = harness.ideas / "output" / name
    doc = load(out)
    out.write_text(dump(Doc({**doc.fm, "stage": stage_value}, doc.body)), encoding="utf-8")

    assert reason(harness, name) == Done({"item": "published"})

    assert len(pages(harness.docs, NOTES)) == 1 and len(harness.backends.note.prompts) == 1


def test_a_working_copy_that_says_published_is_taken_as_the_finished_page(harness):
    name = staged_note(harness)
    out = harness.ideas / "output" / name
    doc = load(out)
    out.write_text(dump(Doc({**doc.fm, "stage": "published"}, doc.body)), encoding="utf-8")

    assert reason(harness, name) == Done({"item": "published"})

    assert pages(harness.docs, NOTES) == [] and harness.backends.note.prompts == []  # nothing made again
