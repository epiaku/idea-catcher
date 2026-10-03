"""The `pipeline.run` handler: the database row first, then the file move, and a crash leftover is adopted."""

from pathlib import Path

import pytest
from sqlalchemy import select, update

from catcher.core.db import session_scope
from catcher.core.frontmatter import load
from catcher.modules.pipeline import inbox
from catcher.modules.pipeline.inbox import load_staged_note, mark_deferred
from catcher.modules.queue.items import set_item_status, stage_item
from catcher.modules.queue.models import Job, JobItem
from catcher.modules.worker import handlers_pipeline
from catcher.modules.worker.app import build_handlers
from catcher.modules.worker.handlers import Done, Fail
from catcher.modules.worker.handlers_pipeline import handle_pipeline_run

CHAT_ID = "cf81e40b020519ef"
VIDEO = "nGVZS_wUDGM"


def run(harness, **params):
    """Queue a `pipeline.run` job with `params` and run its handler directly (no other job runs)."""
    job_id = harness.add_job("pipeline.run", **params)
    with session_scope(harness.ctx.engine) as session:
        job = session.get(Job, job_id)
    assert job is not None
    return handle_pipeline_run(harness.ctx, job)


def counts(**changed: int) -> dict[str, int]:
    return {"staged": 0, "adopted": 0, "duplicates": 0, "artifacts": 0, "unreadable": 0, **changed}


def items(harness) -> dict[str, JobItem]:
    with session_scope(harness.ctx.engine) as session:
        return {i.calculated_name: i for i in session.scalars(select(JobItem))}


def next_jobs(harness) -> list[Job]:
    """The jobs a run enqueued (every job but the pipeline.run ones), oldest first."""
    with session_scope(harness.ctx.engine) as session:
        return [j for j in harness.jobs(session) if j.type != "pipeline.run"]


def item_named(harness, sub: str, original: str) -> JobItem:
    [item] = [i for i in items(harness).values() if i.original_filename == original]
    assert item.calculated_name.startswith(f"{sub}/")
    return item


def in_folder(ideas: Path, folder: str, original: str) -> list[Path]:
    """The files in `folder` that came from the document captured as `original`."""
    root = ideas / folder
    found = []
    for path in sorted(root.rglob("*.md")) if root.is_dir() else []:
        try:
            name = load(path).fm.get("original_filename")
        except ValueError:
            continue
        if name == original:
            found.append(path)
    return found


def test_the_handler_is_registered():
    assert build_handlers()["pipeline.run"] is handle_pipeline_run


def test_run_stages_a_note_and_enqueues_llm_reason(harness, sh):
    result = run(harness, only=["YouTube walks"])

    assert result == Done(counts(staged=1))
    assert not (harness.ideas / "inbox/notes/YouTube walks.md").exists()
    item = item_named(harness, "notes", "YouTube walks.md")
    assert (item.status, item.doc_class, item.inbox_path) == (
        "waiting_llm",
        "note",
        "inbox/notes/YouTube walks.md",
    )
    [out] = in_folder(harness.ideas, "output", "YouTube walks.md")
    [archived] = in_folder(harness.ideas, "archive", "YouTube walks.md")
    assert out.relative_to(harness.ideas / "output").as_posix() == item.calculated_name
    assert archived.relative_to(harness.ideas / "archive").as_posix() == item.calculated_name
    assert load(out).fm["stage"] == "analyzed"
    [job] = next_jobs(harness)
    assert (job.type, job.status, job.params, job.dedupe_key) == (
        "llm.reason",
        "queued",
        {"calculated_name": item.calculated_name},
        f"reason:{item.calculated_name}",
    )
    # the handler runs no git command: the changes are in the files only (publishing commits them)
    assert sh(harness.ideas, "rev-list", "--count", "HEAD").strip() == "1"
    assert sh(harness.ideas, "status", "--porcelain") != ""


def test_profile_and_refresh_llm_are_copied_into_the_llm_reason_params(harness):
    run(harness, only=["YouTube walks"], profile="fake", refresh_llm=True)
    [job] = next_jobs(harness)
    assert job.params == {
        "calculated_name": item_named(harness, "notes", "YouTube walks.md").calculated_name,
        "profile": "fake",
        "refresh_llm": True,
    }


def test_a_youtube_clip_is_staged_waiting_youtube_with_a_fetch_job(harness):
    result = run(harness, only=["yt"], profile="fake")

    assert result == Done(counts(staged=1))
    item = item_named(harness, "clippings", "yt.md")
    assert (item.status, item.doc_class, item.doc_id) == ("waiting_youtube", "youtube", VIDEO)
    [out] = in_folder(harness.ideas, "output", "yt.md")
    assert load(out).fm["stage"] == "analyzed"
    [job] = next_jobs(harness)
    assert (job.type, job.params, job.dedupe_key) == (
        "youtube.fetch",
        {"calculated_name": item.calculated_name},
        f"fetch:{item.calculated_name}",
    )
    assert harness.fetch_calls == []


def test_a_youtube_clip_with_saved_facts_goes_straight_to_llm_reason(harness, yt_facts):
    youtube = harness.ctx.services.youtube
    cache = youtube.cache(harness.ideas / "facts")
    cache.put(yt_facts)

    assert run(harness, only=["yt"]) == Done(counts(staged=1))

    item = item_named(harness, "clippings", "yt.md")
    assert item.status == "waiting_llm"
    [job] = next_jobs(harness)
    assert (job.type, job.dedupe_key) == ("llm.reason", f"reason:{item.calculated_name}")
    assert harness.fetch_calls == []


def test_running_twice_stages_nothing_twice(harness):
    assert run(harness) == Done(counts(staged=3))
    first_items, first_jobs = items(harness), next_jobs(harness)

    assert run(harness) == Done(counts())  # the inbox is empty

    assert items(harness).keys() == first_items.keys() and len(first_items) == 3
    assert [j.id for j in next_jobs(harness)] == [j.id for j in first_jobs]
    assert sorted(j.dedupe_key or "" for j in first_jobs) == sorted(
        f"{'fetch' if i.doc_class == 'youtube' else 'reason'}:{name}" for name, i in first_items.items()
    )
    assert not [p for p in (harness.ideas / "inbox").rglob("*") if p.is_file()]


def _chat(body: str) -> str:
    return (
        '---\nsource : "https://gemini.google.com/app/2446cd9c762c9cc9?is_sa=1"\n'
        f'created: 2026-09-25\ntags:\n  - "clippings"\n---\n{body}'
    )


def _turns(n: int) -> str:
    return "\n".join(f"**You**\n\nquestion {i}\n\n---\n\n**Gemini**\n\nanswer {i}\n" for i in range(1, n + 1))


def test_duplicates_unreadable_and_artifacts_are_handled_like_stage_a(harness, sh):
    ideas = harness.ideas
    (ideas / "inbox/clippings/chat short.md").write_text(_chat(_turns(3)))
    (ideas / "inbox/clippings/chat long.md").write_text(_chat(_turns(6)))
    (ideas / "inbox/notes/bad.md").write_text("---\ntitle: [oops\n---\nbody\n")
    (ideas / "inbox/report.pdf").write_bytes(b"%PDF-1.7 binary \x00\x01")

    result = run(harness)

    assert result == Done(counts(staged=4, duplicates=1, unreadable=1, artifacts=1))
    # the earlier snapshot: archived and moved to duplicates/, no working copy, no item
    [dup] = in_folder(ideas, "duplicates", "chat short.md")
    [archived] = in_folder(ideas, "archive", "chat short.md")
    assert dup.name == archived.name and load(dup).fm["duplicate_of"] == "clippings/chat long.md"
    assert in_folder(ideas, "output", "chat short.md") == []
    assert item_named(harness, "clippings", "chat long.md").status == "waiting_llm"
    # the unreadable capture: archived and moved to failed/ with its reason, bytes unchanged
    [failed] = (ideas / "failed/notes").glob("*-bad.md")
    assert failed.read_text() == "---\ntitle: [oops\n---\nbody\n"
    assert (ideas / "archive/notes" / failed.name).exists()
    assert "original file: bad.md" in failed.with_suffix(".error.txt").read_text()
    # the artifact: archived and copied to epiaku-docs under one name
    [art] = (ideas / "archive/artifacts").iterdir()
    assert [p.name for p in (harness.docs / "idea-bucket/artifacts").iterdir()] == [art.name]
    assert not (ideas / "inbox/report.pdf").exists()
    # rows only for the notes that were staged
    assert len(items(harness)) == 4 and len(next_jobs(harness)) == 4
    assert not [p for p in (ideas / "inbox").rglob("*") if p.is_file()]
    assert sh(ideas, "rev-list", "--count", "HEAD").strip() == "1"
    assert sh(harness.docs, "rev-list", "--count", "HEAD").strip() == "1"


def test_a_crash_after_the_staging_row_is_adopted_with_the_same_name(harness, monkeypatch):
    real_start_work = handlers_pipeline.start_work
    crashed: list[str] = []

    def crash_once(ideas, note, now=None):
        if not crashed:
            crashed.append(note.name)
            inbox.archive_copy(ideas, note)  # a partial start: the archive copy is written, then the crash
            raise RuntimeError("worker killed")
        return real_start_work(ideas, note, now)

    monkeypatch.setattr(handlers_pipeline, "start_work", crash_once)

    with pytest.raises(RuntimeError, match="worker killed"):
        run(harness)

    [leftover] = items(harness).values()  # committed before the move
    assert leftover.status == "staging" and leftover.original_filename == "systeme.md"
    assert leftover.calculated_name.endswith(f"/{crashed[0]}")
    assert (harness.ideas / "inbox/clippings/systeme.md").exists()
    assert next_jobs(harness) == []

    assert run(harness) == Done(counts(staged=2, adopted=1))

    item = items(harness)[leftover.calculated_name]  # the same calculated name
    assert item.status == "waiting_llm" and item.id == leftover.id
    assert len(items(harness)) == 3
    assert [p.name for p in in_folder(harness.ideas, "archive", "systeme.md")] == [crashed[0]]
    [out] = in_folder(harness.ideas, "output", "systeme.md")
    assert out.name == crashed[0] and load(out).fm["stage"] == "analyzed"
    assert not (harness.ideas / "inbox/clippings/systeme.md").exists()
    mine = [j for j in next_jobs(harness) if j.params["calculated_name"] == item.calculated_name]
    assert [j.type for j in mine] == ["llm.reason"]


def test_a_crash_after_the_move_before_the_job_is_adopted(harness, monkeypatch):
    real_enqueue = handlers_pipeline.enqueue
    crashed: list[bool] = []

    def crash_once(session, **kwargs):
        if not crashed:
            crashed.append(True)
            raise RuntimeError("worker killed")
        return real_enqueue(session, **kwargs)

    monkeypatch.setattr(handlers_pipeline, "enqueue", crash_once)

    with pytest.raises(RuntimeError, match="worker killed"):
        run(harness)

    [leftover] = items(harness).values()  # the status change rolled back with the failed enqueue
    assert leftover.status == "staging" and leftover.original_filename == "systeme.md"
    assert not (harness.ideas / "inbox/clippings/systeme.md").exists()
    assert next_jobs(harness) == []

    assert run(harness) == Done(counts(staged=2, adopted=1))

    item = items(harness)[leftover.calculated_name]
    assert item.status == "waiting_llm"
    assert len(in_folder(harness.ideas, "archive", "systeme.md")) == 1
    assert len(in_folder(harness.ideas, "output", "systeme.md")) == 1
    mine = [j for j in next_jobs(harness) if j.params["calculated_name"] == item.calculated_name]
    assert [(j.type, j.dedupe_key) for j in mine] == [("llm.reason", f"reason:{item.calculated_name}")]


def test_a_staging_row_without_a_document_is_failed(harness):
    with session_scope(harness.ctx.engine) as session:
        stage_item(
            session,
            calculated_name="notes/20261002-abcdef-gone.md",
            doc_id="abcdef",
            doc_class="note",
            now=harness.clock(),
            inbox_path="inbox/notes/gone.md",
            original_filename="gone.md",
        )

    assert run(harness, only=["nothing"]) == Done(counts())

    gone = items(harness)["notes/20261002-abcdef-gone.md"]
    assert (gone.status, gone.error) == ("failed", "staging row without a document")


def _finish_jobs(harness) -> None:
    """Pretend the enqueued jobs ran (there are no llm.reason/youtube.fetch handlers in these tests)."""
    with session_scope(harness.ctx.engine) as session:
        session.execute(update(Job).where(Job.type != "pipeline.run").values(status="succeeded"))


def test_requeue_resets_the_existing_item(harness):
    run(harness, only=["YouTube walks"])
    first = item_named(harness, "notes", "YouTube walks.md")
    _finish_jobs(harness)
    with session_scope(harness.ctx.engine) as session:
        set_item_status(session, first.calculated_name, "published", now=harness.clock())

    harness.clock.advance(60)
    result = run(harness, requeue=["YouTube walks"])

    assert result == Done(counts(staged=1))  # only the requeued document ran
    again = item_named(harness, "notes", "YouTube walks.md")
    assert (again.id, again.calculated_name, again.status) == (first.id, first.calculated_name, "waiting_llm")
    assert again.updated_at > first.updated_at
    assert len(items(harness)) == 1
    assert (harness.ideas / "inbox/clippings/systeme.md").exists()  # not selected
    [archived] = in_folder(harness.ideas, "archive", "YouTube walks.md")
    assert archived.relative_to(harness.ideas / "archive").as_posix() == first.calculated_name
    [out] = in_folder(harness.ideas, "output", "YouTube walks.md")
    assert load(out).fm["stage"] == "analyzed"
    queued = [j for j in next_jobs(harness) if j.status == "queued"]
    assert [(j.type, j.params["calculated_name"]) for j in queued] == [("llm.reason", first.calculated_name)]


def test_requeue_leaves_an_active_item_alone(harness):
    run(harness, only=["YouTube walks"])
    item = item_named(harness, "notes", "YouTube walks.md")

    assert run(harness, requeue=["YouTube walks"]) == Done(counts())

    assert item_named(harness, "notes", "YouTube walks.md").status == "waiting_llm"
    assert (harness.ideas / "output" / item.calculated_name).exists()
    assert (harness.ideas / "archive" / item.calculated_name).exists()
    assert len(next_jobs(harness)) == 1


def test_retry_deferred_requeues_stalled_items(harness):
    run(harness, only=["systeme"])
    stalled = item_named(harness, "clippings", "systeme.md")
    _finish_jobs(harness)
    out = harness.ideas / "output" / stalled.calculated_name
    with session_scope(harness.ctx.engine) as session:
        set_item_status(session, stalled.calculated_name, "deferred", now=harness.clock(), reason="LLM down")
    mark_deferred(harness.ideas, load_staged_note(harness.ideas, out), "LLM down")
    assert load(out).fm["stage"] == "deferred"

    result = run(harness, retry_deferred=True, only=["systeme"])  # `only` limits the scan, as in Stage A

    assert result == Done(counts(staged=1))
    again = item_named(harness, "clippings", "systeme.md")
    assert (again.id, again.status, again.error) == (stalled.id, "waiting_llm", None)
    assert load(out).fm["stage"] == "analyzed"
    assert len(in_folder(harness.ideas, "archive", "systeme.md")) == 1
    assert (harness.ideas / "inbox/notes/YouTube walks.md").exists()  # not selected
    queued = [j for j in next_jobs(harness) if j.status == "queued"]
    assert [(j.type, j.params["calculated_name"]) for j in queued] == [
        ("llm.reason", stalled.calculated_name)
    ]


def test_limit_counts_the_staged_notes(harness):
    assert run(harness, limit=1) == Done(counts(staged=1))
    assert len(items(harness)) == 1
    assert len([p for p in (harness.ideas / "inbox").rglob("*.md")]) == 2


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"only": "YouTube walks"}, "only"),
        ({"requeue": [1]}, "requeue"),
        ({"retry_deferred": "yes"}, "retry_deferred"),
        ({"limit": "3"}, "limit"),
        ({"limit": -1}, "limit"),
        ({"limit": True}, "limit"),
        ({"profile": 3}, "profile"),
        ({"refresh_llm": 1}, "refresh_llm"),
        ({"dry_run": True}, "dry_run"),
    ],
)
def test_bad_params_fail_the_job_and_touch_nothing(harness, params, message):
    result = run(harness, **params)
    assert isinstance(result, Fail) and message in result.error
    assert items(harness) == {} and next_jobs(harness) == []
    assert (harness.ideas / "inbox/notes/YouTube walks.md").exists()
