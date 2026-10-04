"""The `llm.reason` handler: saved facts only, the saved reply first, the page, and the item outcome.

An LLM or facts problem is an item state (the job succeeds); the handler never calls YouTube and runs no git
command; running it twice gives one page and, thanks to the saved reply, one model call."""

import json
from pathlib import Path

import pytest
from sqlalchemy import select, update
from worker_harness import NOTES, WEB_CLIPS, RaisingBackend

from catcher.core.db import session_scope
from catcher.core.frontmatter import load
from catcher.modules.llm.backends.fake import CANNED, FakeBackend
from catcher.modules.llm.service import BackendUnavailable
from catcher.modules.queue.items import set_item_status
from catcher.modules.queue.models import Job, JobItem
from catcher.modules.worker import handlers_pipeline
from catcher.modules.worker.app import build_handlers
from catcher.modules.worker.handlers import Done, Fail
from catcher.modules.worker.handlers_pipeline import handle_llm_reason, handle_pipeline_run

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
    assert out["stage"] == "analyzed"  # the working copy waits in output/ for the facts


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
