"""`report_for_job`: the old `RunReport` of `run_pipeline`, built from the database after a worker run.

The items are the `job_items` of the run's `pipeline.run` job (its `root_job_id`), plus the names its result
holds for what leaves no item row: duplicates, artifacts, unreadable files, unmatched names, requeue moves and
the documents left in `inbox/` by `limit`."""

import shutil
from pathlib import Path

from sqlalchemy import select

from catcher.core.db import session_scope
from catcher.modules.llm.backends.fake import FakeBackend
from catcher.modules.pipeline.report import ItemReport, RunReport, report_for_job
from catcher.modules.pipeline.run import RunOptions, run_pipeline
from catcher.modules.queue.models import Job, JobItem

VID = "nGVZS_wUDGM"


def _chat(body: str) -> str:
    return (
        '---\nsource : "https://gemini.google.com/app/2446cd9c762c9cc9?is_sa=1"\n'
        f'created: 2026-09-25\ntags:\n  - "clippings"\n---\n{body}'
    )


def _turns(n: int) -> str:
    return "\n".join(f"**You**\n\nquestion {i}\n\n---\n\n**Gemini**\n\nanswer {i}\n" for i in range(1, n + 1))


def add_snapshots(ideas: Path) -> None:
    """Two snapshots of one conversation: the shorter one is a duplicate of the longer one."""
    (ideas / "inbox/clippings/chat short.md").write_text(_chat(_turns(3)))
    (ideas / "inbox/clippings/chat long.md").write_text(_chat(_turns(6)))


def run_on_the_worker(harness, rounds: int = 5, **params) -> RunReport:
    """Queue `pipeline.run` with `params`, run the worker until nothing is due (moving the clock past the
    YouTube gap between rounds), and build the report of that run."""
    job_id = harness.add_job("pipeline.run", **params)
    for _ in range(rounds):
        harness.drain()
        harness.clock.advance(3600)
    with session_scope(harness.ctx.engine) as session:
        return report_for_job(session, job_id)


def by_status(report: RunReport, status: str) -> list[ItemReport]:
    return [item for item in report.items if item.status == status]


def item_rows(harness) -> dict[str, JobItem]:
    with session_scope(harness.ctx.engine) as session:
        return {i.original_filename or "": i for i in session.scalars(select(JobItem))}


def test_report_for_a_run_counts_published_deferred_failed_and_duplicates(harness, yt_facts):
    add_snapshots(harness.ideas)
    harness.fetcher = lambda video_id: yt_facts.model_copy(update={"transcript": None})  # deferred
    harness.backends.chat = FakeBackend(["nope", "still nope"])  # the first chat: invalid output, failed

    report = run_on_the_worker(harness)

    assert report.counts() == {"published": 2, "deferred": 1, "failed": 1, "duplicate": 1}
    rows = item_rows(harness)
    [deferred] = by_status(report, "deferred")
    assert (deferred.doc_class, deferred.doc_id) == ("youtube", VID)
    assert deferred.message == rows["yt.md"].stage_reason and "no transcript" in deferred.message
    [failed] = by_status(report, "failed")
    assert failed.doc_class == "ai-chat" and failed.message and "invalid" in failed.message
    [duplicate] = by_status(report, "duplicate")
    assert (duplicate.doc_class, duplicate.doc_id) == ("ai-chat", "2446cd9c762c9cc9")
    assert duplicate.message == "duplicate of clippings/chat long.md"
    assert (report.unreadable, report.not_found, report.not_in_archive, report.problems) == ({}, [], [], [])


def test_report_marks_the_documents_left_by_the_limit(harness):
    report = run_on_the_worker(harness, limit=1)

    assert report.counts() == {"published": 1, "skipped": 2}
    skipped = by_status(report, "skipped")
    assert {item.message for item in skipped} == {"run limit reached"}
    assert sorted(item.doc_class for item in skipped) == sorted(
        {"note", "ai-chat", "youtube"} - {by_status(report, "published")[0].doc_class}
    )
    assert len([p for p in (harness.ideas / "inbox").rglob("*.md")]) == 2


def test_report_lists_unreadable_files_and_names_not_found(harness):
    (harness.ideas / "inbox/notes/bad.md").write_text("---\ntitle: [oops\n---\nbody\n")

    report = run_on_the_worker(harness, only=["YouTube walks", "bad.md", "no such thing"])

    assert report.counts() == {"published": 1}
    assert list(report.unreadable) == ["inbox/notes/bad.md"]
    assert report.not_found == ["no such thing"]


def test_report_has_the_page_the_tokens_and_the_saved_flag_of_each_item(harness):
    first = run_on_the_worker(harness, only=["YouTube walks"])
    [item] = first.items
    row = item_rows(harness)["YouTube walks.md"]
    assert row.docs_page is not None
    assert (item.status, item.doc_class, item.doc_id) == ("published", "note", row.doc_id)
    assert item.page == Path(row.docs_page).name and item.page.endswith("-youtube-walks.md")
    assert (item.tokens_in, item.tokens_out) == (row.tokens_in, row.tokens_out)
    assert item.tokens_in and item.llm_saved is False

    again = run_on_the_worker(harness, requeue=["YouTube walks"])  # the saved reply is used

    assert again.counts() == {"requeued": 1, "published": 1}
    [requeued] = by_status(again, "requeued")
    assert requeued.message == f"archive/{row.calculated_name} -> inbox/"
    [published] = by_status(again, "published")
    assert (published.page, published.llm_saved) == (item.page, True)
    assert (published.tokens_in, published.tokens_out) == (item.tokens_in, item.tokens_out)


def test_report_shows_artifacts_requeue_skips_and_names_not_in_archive(harness):
    (harness.ideas / "inbox/report.pdf").write_bytes(b"%PDF-1.7 binary \x00\x01")
    first = run_on_the_worker(harness, only=["YouTube walks", "report.pdf"])
    [artifact] = by_status(first, "artifact")
    assert (artifact.doc_id, artifact.doc_class) == ("report.pdf", "artifact")
    assert artifact.page and artifact.page.endswith("-report.pdf")
    # a requeue whose name inbox/ already holds: not overwritten, reported skipped
    name = item_rows(harness)["YouTube walks.md"].calculated_name
    (harness.ideas / "inbox" / name).write_text("edited in the inbox\n")

    report = run_on_the_worker(harness, requeue=["YouTube walks", "never captured"])

    [skipped] = by_status(report, "skipped")
    assert skipped.message == f"inbox/{name} already exists, not overwritten"
    assert report.not_in_archive == ["never captured"]


def test_report_counts_are_the_same_as_the_old_counts_for_the_same_inbox(
    harness, make_services, yt_facts, tmp_path
):
    """The same inbox through the old loop (on a copy of both repos) and through the worker: the same
    counts. A note, two chats of which one is an earlier snapshot, a clip without a transcript, an artifact
    and an unreadable file."""
    add_snapshots(harness.ideas)
    (harness.ideas / "inbox/report.pdf").write_bytes(b"%PDF-1.7 binary \x00\x01")
    (harness.ideas / "inbox/notes/bad.md").write_text("---\ntitle: [oops\n---\nbody\n")
    old_ideas, old_docs = tmp_path / "old-ideas", tmp_path / "old-docs"
    shutil.copytree(harness.ideas, old_ideas)
    shutil.copytree(harness.docs, old_docs)

    def no_transcript(video_id: str):
        return yt_facts.model_copy(update={"transcript": None})

    harness.fetcher = no_transcript
    old = run_pipeline(old_ideas, old_docs, RunOptions(), make_services(facts=no_transcript))

    new = run_on_the_worker(harness)

    assert old.counts() == {"published": 3, "deferred": 1, "duplicate": 1, "artifact": 1}
    assert new.counts() == old.counts()
    assert list(new.unreadable) == list(old.unreadable) == ["inbox/notes/bad.md"]


def test_report_of_a_failed_run_has_its_error_as_a_problem(harness):
    shutil.rmtree(harness.docs)

    report = run_on_the_worker(harness)

    assert report.items == [] and len(report.problems) == 1
    assert report.problems[0].startswith("epiaku-docs not found at ")
    with session_scope(harness.ctx.engine) as session:
        [job] = session.scalars(select(Job)).all()
        assert job.status == "failed"
