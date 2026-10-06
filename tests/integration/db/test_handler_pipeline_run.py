"""The `pipeline.run` handler: the database row first, then the file move, and a crash leftover is adopted."""

import json
import logging
from pathlib import Path

import pytest
from sqlalchemy import select, text, update

from catcher import __version__
from catcher.core.db import session_scope
from catcher.core.frontmatter import load, parse
from catcher.modules.pipeline import inbox
from catcher.modules.pipeline.inbox import load_staged_note, mark_deferred
from catcher.modules.pipeline.mirror import MirrorState, read_mirror
from catcher.modules.pipeline.scan_state import scan_item_files
from catcher.modules.queue.items import set_item_status, stage_item
from catcher.modules.queue.models import Job, JobEvent, JobItem
from catcher.modules.queue.queue import claim
from catcher.modules.queue.reconcile import reconcile
from catcher.modules.queue.states import ItemStates
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
    zero = {"staged": 0, "adopted": 0, "duplicates": 0, "artifacts": 0, "unreadable": 0, "errors": 0}
    return {**zero, **changed}


def without_names(result):
    """The result with its counts only: the names behind them (`names`) are pinned by their own tests."""
    if isinstance(result, Done) and result.result is not None:
        return Done({key: value for key, value in result.result.items() if key != "names"})
    return result


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

    assert without_names(result) == Done(counts(staged=1))
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
    assert load(out).fm["stage"] == "waiting_llm"  # B5: the mirror shows the item's state
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


def test_profile_and_refresh_llm_are_copied_into_the_llm_reason_and_fetch_params(harness):
    run(harness, only=["YouTube walks", "yt"], profile="fake", refresh_llm=True)
    note = item_named(harness, "notes", "YouTube walks.md").calculated_name
    clip = item_named(harness, "clippings", "yt.md").calculated_name
    extra = {"profile": "fake", "refresh_llm": True}
    assert {j.type: j.params for j in next_jobs(harness)} == {
        "llm.reason": {"calculated_name": note, **extra},
        "youtube.fetch": {"calculated_name": clip, **extra},
    }


def test_a_youtube_clip_is_staged_waiting_youtube_with_a_fetch_job(harness):
    result = run(harness, only=["yt"], profile="fake")

    assert without_names(result) == Done(counts(staged=1))
    item = item_named(harness, "clippings", "yt.md")
    assert (item.status, item.doc_class, item.doc_id) == ("waiting_youtube", "youtube", VIDEO)
    [out] = in_folder(harness.ideas, "output", "yt.md")
    assert load(out).fm["stage"] == "waiting_youtube"  # B5: the mirror shows the item's state
    [job] = next_jobs(harness)
    assert (job.type, job.params, job.dedupe_key) == (
        "youtube.fetch",
        {"calculated_name": item.calculated_name, "profile": "fake"},  # passed on to llm.reason later
        f"fetch:{item.calculated_name}",
    )
    assert harness.fetch_calls == []


def test_a_youtube_clip_with_saved_facts_goes_straight_to_llm_reason(harness, yt_facts):
    youtube = harness.ctx.services.youtube
    cache = youtube.cache(harness.ideas / "facts")
    cache.put(yt_facts)

    assert without_names(run(harness, only=["yt"])) == Done(counts(staged=1))

    item = item_named(harness, "clippings", "yt.md")
    assert item.status == "waiting_llm"
    [job] = next_jobs(harness)
    assert (job.type, job.dedupe_key) == ("llm.reason", f"reason:{item.calculated_name}")
    assert harness.fetch_calls == []


def test_running_twice_stages_nothing_twice(harness):
    assert without_names(run(harness)) == Done(counts(staged=3))
    first_items, first_jobs = items(harness), next_jobs(harness)

    assert without_names(run(harness)) == Done(counts())  # the inbox is empty

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

    assert without_names(result) == Done(counts(staged=4, duplicates=1, unreadable=1, artifacts=1))
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


def test_a_capture_with_a_broken_source_line_goes_to_failed_and_the_others_are_staged(harness):
    ideas = harness.ideas
    broken = "---\nsource: https://[oops/x\n---\nbody\n"
    (ideas / "inbox/clippings/broken.md").write_text(broken)

    result = run(harness, only=["YouTube walks", "broken.md"])

    assert without_names(result) == Done(counts(staged=1, unreadable=1))
    [failed] = (ideas / "failed/clippings").glob("*-broken.md")
    assert failed.read_text() == broken
    assert "cannot analyse: " in failed.with_suffix(".error.txt").read_text()
    assert not (ideas / "inbox/clippings/broken.md").exists()
    assert item_named(harness, "notes", "YouTube walks.md").status == "waiting_llm"


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

    assert without_names(run(harness)) == Done(counts(staged=2, adopted=1))

    item = items(harness)[leftover.calculated_name]  # the same calculated name
    assert item.status == "waiting_llm" and item.id == leftover.id
    assert len(items(harness)) == 3
    assert [p.name for p in in_folder(harness.ideas, "archive", "systeme.md")] == [crashed[0]]
    [out] = in_folder(harness.ideas, "output", "systeme.md")
    assert out.name == crashed[0] and load(out).fm["stage"] == "waiting_llm"  # mirrored
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

    assert without_names(run(harness)) == Done(counts(staged=2, adopted=1))

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

    assert without_names(run(harness, only=["nothing"])) == Done(counts())

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

    assert without_names(result) == Done(counts(staged=1))  # only the requeued document ran
    again = item_named(harness, "notes", "YouTube walks.md")
    assert (again.id, again.calculated_name, again.status) == (first.id, first.calculated_name, "waiting_llm")
    assert again.updated_at > first.updated_at
    assert len(items(harness)) == 1
    assert (harness.ideas / "inbox/clippings/systeme.md").exists()  # not selected
    [archived] = in_folder(harness.ideas, "archive", "YouTube walks.md")
    assert archived.relative_to(harness.ideas / "archive").as_posix() == first.calculated_name
    [out] = in_folder(harness.ideas, "output", "YouTube walks.md")
    assert load(out).fm["stage"] == "waiting_llm"  # a fresh working copy, mirrored
    queued = [j for j in next_jobs(harness) if j.status == "queued"]
    assert [(j.type, j.params["calculated_name"]) for j in queued] == [("llm.reason", first.calculated_name)]


def test_requeue_leaves_an_active_item_alone(harness):
    run(harness, only=["YouTube walks"])
    item = item_named(harness, "notes", "YouTube walks.md")

    assert without_names(run(harness, requeue=["YouTube walks"])) == Done(counts())

    assert item_named(harness, "notes", "YouTube walks.md").status == "waiting_llm"
    assert (harness.ideas / "output" / item.calculated_name).exists()
    assert (harness.ideas / "archive" / item.calculated_name).exists()
    assert len(next_jobs(harness)) == 1


def test_requeue_resets_a_stuck_active_item_that_no_job_carries(harness):
    run(harness, only=["YouTube walks"])
    stuck = item_named(harness, "notes", "YouTube walks.md")
    _finish_jobs(harness)  # its llm.reason ended without moving the item on: it is a leftover
    assert stuck.status == "waiting_llm"

    harness.clock.advance(60)
    assert without_names(run(harness, requeue=["YouTube walks"])) == Done(counts(staged=1))

    again = item_named(harness, "notes", "YouTube walks.md")
    assert (again.id, again.calculated_name, again.status) == (stuck.id, stuck.calculated_name, "waiting_llm")
    assert again.updated_at > stuck.updated_at
    queued = [j for j in next_jobs(harness) if j.status == "queued"]
    assert [(j.type, j.params["calculated_name"]) for j in queued] == [("llm.reason", stuck.calculated_name)]


@pytest.mark.parametrize("status", ["queued", "running"])
def test_requeue_leaves_an_item_a_live_job_carries_alone(harness, status):
    run(harness, only=["YouTube walks"])
    item = item_named(harness, "notes", "YouTube walks.md")
    if status == "running":
        with session_scope(harness.ctx.engine) as session:
            assert claim(session, worker="other", now=harness.clock(), lease_s=60, types=["llm.reason"])
    with session_scope(harness.ctx.engine) as session:
        set_item_status(session, item.calculated_name, "published", now=harness.clock())  # even if final

    assert without_names(run(harness, requeue=["YouTube walks"])) == Done(counts())

    assert item_named(harness, "notes", "YouTube walks.md").status == "published"
    assert (harness.ideas / "output" / item.calculated_name).exists()
    assert not (harness.ideas / "inbox" / item.calculated_name).exists()
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

    assert without_names(result) == Done(counts(staged=1))
    again = item_named(harness, "clippings", "systeme.md")
    assert (again.id, again.status, again.error) == (stalled.id, "waiting_llm", None)
    assert load(out).fm["stage"] == "waiting_llm"  # no longer deferred, mirrored
    assert len(in_folder(harness.ideas, "archive", "systeme.md")) == 1
    assert (harness.ideas / "inbox/notes/YouTube walks.md").exists()  # not selected
    queued = [j for j in next_jobs(harness) if j.status == "queued"]
    assert [(j.type, j.params["calculated_name"]) for j in queued] == [
        ("llm.reason", stalled.calculated_name)
    ]


def test_limit_counts_the_staged_notes(harness):
    assert without_names(run(harness, limit=1)) == Done(counts(staged=1))
    assert len(items(harness)) == 1
    assert len([p for p in (harness.ideas / "inbox").rglob("*.md")]) == 2


def test_a_big_inbox_is_logged_in_two_lines_not_one_per_document(harness, caplog):
    with caplog.at_level("INFO", logger="catcher.worker"):
        run(harness, limit=1)
    lines = [r.getMessage() for r in caplog.records if r.name == "catcher.worker.pipeline"]
    assert [m for m in lines if m.startswith("inbox:")] == [
        f"inbox: 3 document(s) to process, at most 1 now (catcher version {__version__})"
    ]
    assert [m for m in lines if m.startswith("limit of")] == [
        "limit of 1 reached: 2 document(s) stay in inbox/"
    ]
    assert not [m for m in lines if "stays in inbox/" in m]  # no line per document that waits


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
        ({"profile": ""}, "profile"),
        ({"refresh_llm": 1}, "refresh_llm"),
        ({"dry_run": True}, "dry_run"),
    ],
)
def test_bad_params_fail_the_job_and_touch_nothing(harness, params, message):
    result = run(harness, **params)
    assert isinstance(result, Fail) and message in result.error
    assert items(harness) == {} and next_jobs(harness) == []
    assert (harness.ideas / "inbox/notes/YouTube walks.md").exists()


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"only": ["../x"]}, "only"),
        ({"only": ["notes/../../x.md"]}, "only"),
        ({"only": ["..\\x.md"]}, "only"),
        ({"only": [""]}, "only"),
        ({"requeue": ["/etc/passwd"]}, "requeue"),
        ({"requeue": ["notes/../../../x"]}, "requeue"),
    ],
)
def test_a_name_that_could_leave_the_ideas_folder_fails_the_job_and_touches_nothing(harness, params, message):
    result = run(harness, **params)
    assert isinstance(result, Fail) and message in result.error
    assert items(harness) == {} and next_jobs(harness) == []
    assert (harness.ideas / "inbox/notes/YouTube walks.md").exists()


MIRROR_KEYS = ("stage", "stage_reason", "stage_since")


def without_mirror(text: str) -> tuple[list[tuple[str, object]], str]:
    """The keys (in their order) and the body, without the three keys the B5 mirror writes: what the mirror
    must leave untouched."""
    doc = parse(text)
    return [(k, v) for k, v in doc.fm.items() if k not in MIRROR_KEYS], doc.body


def _crash_before_the_unlink(monkeypatch, replacement: bytes | None = None) -> list[str]:
    """`start_work` runs for real once, then the inbox file is back (the unlink never happened, or a new
    capture landed at the same path when `replacement` is given) and the worker dies before step 3."""
    real_start_work = handlers_pipeline.start_work
    crashed: list[str] = []

    def crash_once(ideas, note, now=None):
        if crashed:
            return real_start_work(ideas, note, now)
        original = note.path.read_bytes()
        real_start_work(ideas, note, now)
        note.path.write_bytes(original if replacement is None else replacement)
        crashed.append(note.target_rel.as_posix())
        raise RuntimeError("worker killed")

    monkeypatch.setattr(handlers_pipeline, "start_work", crash_once)
    return crashed


def test_a_crash_between_the_output_copy_and_the_unlink_is_adopted_once(harness, monkeypatch):
    crashed = _crash_before_the_unlink(monkeypatch)
    with pytest.raises(RuntimeError, match="worker killed"):
        run(harness, only=["YouTube walks"])
    [leftover] = items(harness).values()
    assert leftover.status == "staging" and leftover.calculated_name == crashed[0]
    assert (harness.ideas / "inbox/notes/YouTube walks.md").exists()
    assert (harness.ideas / "output" / crashed[0]).exists()

    assert without_names(run(harness, only=["YouTube walks"])) == Done(counts(adopted=1))

    assert not (harness.ideas / "inbox/notes/YouTube walks.md").exists()
    assert [p.name for p in in_folder(harness.ideas, "archive", "YouTube walks.md")] == [
        Path(crashed[0]).name
    ]
    [out] = in_folder(harness.ideas, "output", "YouTube walks.md")
    item = items(harness)[crashed[0]]
    assert item.status == "waiting_llm" and len(items(harness)) == 1
    assert load(out).fm["id"] == item.doc_id == leftover.doc_id  # the id on the row is kept
    assert [j.params["calculated_name"] for j in next_jobs(harness)] == [crashed[0]]


def test_a_new_capture_at_the_same_inbox_path_does_not_overwrite_the_adopted_document(harness, monkeypatch):
    other = b"A different idea that was captured under the same file name\n"
    crashed = _crash_before_the_unlink(monkeypatch, replacement=other)
    with pytest.raises(RuntimeError, match="worker killed"):
        run(harness, only=["YouTube walks"])
    archived = (harness.ideas / "archive" / crashed[0]).read_bytes()
    output = (harness.ideas / "output" / crashed[0]).read_bytes()

    assert without_names(run(harness, only=["YouTube walks"])) == Done(counts(adopted=1, staged=1))

    assert (harness.ideas / "archive" / crashed[0]).read_bytes() == archived  # the first document is kept
    # the adopted working copy is kept: its body and every key but the three the mirror writes
    adopted = harness.ideas / "output" / crashed[0]
    assert without_mirror(adopted.read_text(encoding="utf-8")) == without_mirror(output.decode("utf-8"))
    row = items(harness)[crashed[0]]
    assert read_mirror(adopted) == MirrorState(row.status, row.stage_reason, row.stage_since)
    assert items(harness)[crashed[0]].status == "waiting_llm"
    names = sorted(
        p.relative_to(harness.ideas / "archive").as_posix()
        for p in in_folder(harness.ideas, "archive", "YouTube walks.md")
    )
    assert len(names) == 2 and crashed[0] in names  # the new capture got its own name
    [new] = [n for n in names if n != crashed[0]]
    assert b"A different idea" in (harness.ideas / "archive" / new).read_bytes()
    assert items(harness)[new].status == "waiting_llm"
    assert not (harness.ideas / "inbox/notes/YouTube walks.md").exists()


def _start_work_fails_for(monkeypatch, original: str) -> None:
    real_start_work = handlers_pipeline.start_work

    def failing(ideas, note, now=None):
        if note.original_name == original:
            inbox.archive_copy(ideas, note)  # it got as far as the archive copy
            raise PermissionError(13, "Permission denied", str(note.path))
        return real_start_work(ideas, note, now)

    monkeypatch.setattr(handlers_pipeline, "start_work", failing)


def test_a_file_error_in_start_work_fails_that_document_and_the_run_goes_on(harness, monkeypatch):
    _start_work_fails_for(monkeypatch, "systeme.md")

    assert without_names(run(harness)) == Done(counts(staged=2, errors=1))

    failed = item_named(harness, "clippings", "systeme.md")
    assert failed.status == "failed" and "could not start work" in (failed.error or "")
    back = harness.ideas / "inbox/clippings/systeme.md"
    assert back.exists() and load(back).fm["calculated_filename"] == Path(failed.calculated_name).name
    assert not (harness.ideas / "archive" / failed.calculated_name).exists()  # as Stage A's return_to_inbox
    assert not (harness.ideas / "output" / failed.calculated_name).exists()
    assert sorted(j.type for j in next_jobs(harness)) == ["llm.reason", "youtube.fetch"]

    monkeypatch.undo()
    # a later run is not blocked: same name, row reset
    assert without_names(run(harness)) == Done(counts(staged=1))
    again = item_named(harness, "clippings", "systeme.md")
    assert (again.id, again.calculated_name, again.status) == (
        failed.id,
        failed.calculated_name,
        "waiting_llm",
    )


def test_a_file_error_while_adopting_does_not_block_the_other_documents(harness, monkeypatch):
    real_start_work = handlers_pipeline.start_work

    def crash_once(ideas, note, now=None):
        raise RuntimeError("worker killed")

    monkeypatch.setattr(handlers_pipeline, "start_work", crash_once)
    with pytest.raises(RuntimeError, match="worker killed"):
        run(harness)
    [leftover] = items(harness).values()
    assert leftover.original_filename == "systeme.md" and leftover.status == "staging"
    monkeypatch.setattr(handlers_pipeline, "start_work", real_start_work)
    _start_work_fails_for(monkeypatch, "systeme.md")

    # tried once in this run, not again by the scan
    assert without_names(run(harness)) == Done(counts(staged=2, errors=1))

    item = items(harness)[leftover.calculated_name]
    assert item.status == "failed" and "could not start work" in (item.error or "")
    assert (harness.ideas / "inbox/clippings/systeme.md").exists()
    assert len(items(harness)) == 3 and len(next_jobs(harness)) == 2


# ---- retry_deferred also recovers active items that have no job (B5 final review I1) -----------------------


def _lose_the_database_and_reconcile(harness) -> None:
    """The rows and jobs are gone (a new database); `reconcile` rebuilds the rows from the folders: active
    rows with no job to move them on."""
    with session_scope(harness.ctx.engine) as session:
        for table in ("job_events", "job_items", "jobs"):
            session.execute(text(f"delete from {table}"))
    with session_scope(harness.ctx.engine) as session:
        reconcile(session, harness.ideas, now=harness.clock(), apply=True, scan=scan_item_files)


def _item_events(harness, name: str) -> list[str]:
    with session_scope(harness.ctx.engine) as session:
        item = session.scalars(select(JobItem).where(JobItem.calculated_name == name)).one()
        return [e.message for e in session.scalars(select(JobEvent).where(JobEvent.item_id == item.id))]


@pytest.mark.parametrize("archived", [True, False], ids=["archive-copy", "working-copy-only"])
def test_retry_deferred_publishes_a_waiting_llm_row_that_reconcile_left_without_a_job(harness, archived):
    run(harness, only=["YouTube walks"])
    name = item_named(harness, "notes", "YouTube walks.md").calculated_name
    if not archived:
        (harness.ideas / "archive" / name).unlink()
    _lose_the_database_and_reconcile(harness)
    assert item_named(harness, "notes", "YouTube walks.md").status == "waiting_llm"
    assert next_jobs(harness) == []

    harness.add_job("pipeline.run", retry_deferred=True, only=["YouTube walks"])
    assert harness.drain(max_jobs=3) == ["succeeded", "succeeded"]

    assert item_named(harness, "notes", "YouTube walks.md").status == "published"
    assert len(harness.backends.note.prompts) == 1


def test_retry_deferred_queues_a_fetch_for_a_waiting_youtube_row_without_a_job(harness):
    run(harness, only=["yt"])
    clip = item_named(harness, "clippings", "yt.md")
    assert clip.status == "waiting_youtube"
    (harness.ideas / "archive" / clip.calculated_name).unlink()  # only the working copy is left
    _lose_the_database_and_reconcile(harness)
    assert item_named(harness, "clippings", "yt.md").status == "waiting_youtube"

    result = run(harness, retry_deferred=True, only=["yt"])

    assert without_names(result) == Done(counts(adopted=1))
    again = item_named(harness, "clippings", "yt.md")
    assert again.status == "waiting_youtube"
    assert [(j.type, j.status, j.params, j.resource) for j in next_jobs(harness)] == [
        ("youtube.fetch", "queued", {"calculated_name": clip.calculated_name}, "youtube")
    ]
    assert harness.fetch_calls == []  # queued, not fetched
    assert any(
        "no job: re-queued from the working copy" in m for m in _item_events(harness, clip.calculated_name)
    )
    assert load(harness.ideas / "output" / clip.calculated_name).fm["stage"] == "waiting_youtube"


def test_retry_deferred_leaves_an_active_row_with_a_live_job_alone(harness):
    run(harness, only=["YouTube walks"])
    before = item_named(harness, "notes", "YouTube walks.md")
    events = _item_events(harness, before.calculated_name)

    result = run(harness, retry_deferred=True, only=["YouTube walks"])

    assert without_names(result) == Done(counts())
    after = item_named(harness, "notes", "YouTube walks.md")
    assert (after.status, after.stage_since) == ("waiting_llm", before.stage_since)
    assert _item_events(harness, before.calculated_name) == events
    assert (harness.ideas / "archive" / before.calculated_name).is_file()
    assert [(j.type, j.status) for j in next_jobs(harness)] == [("llm.reason", "queued")]


def test_retry_deferred_requeues_a_deferred_row_without_an_archive_copy_from_its_working_copy(harness):
    run(harness, only=["systeme"], profile="clippings", refresh_llm=True)
    stalled = item_named(harness, "clippings", "systeme.md")
    _finish_jobs(harness)
    with session_scope(harness.ctx.engine) as session:
        ItemStates().transition(
            session, stalled.calculated_name, "deferred", now=harness.clock(), reason="down"
        )
    (harness.ideas / "archive" / stalled.calculated_name).unlink()

    result = run(harness, retry_deferred=True, only=["systeme"], profile="clippings", refresh_llm=True)

    assert without_names(result) == Done(counts(adopted=1))
    assert item_named(harness, "clippings", "systeme.md").status == "waiting_llm"
    queued = [j for j in next_jobs(harness) if j.status == "queued"]
    assert [(j.type, j.params, j.dedupe_key) for j in queued] == [
        (
            "llm.reason",
            {"calculated_name": stalled.calculated_name, "profile": "clippings", "refresh_llm": True},
            f"reason:{stalled.calculated_name}",
        )
    ]


def test_retry_deferred_warns_about_a_row_with_no_file_to_start_from_and_leaves_it(harness, caplog):
    run(harness, only=["systeme"])
    stalled = item_named(harness, "clippings", "systeme.md")
    _finish_jobs(harness)
    with session_scope(harness.ctx.engine) as session:
        ItemStates().transition(
            session, stalled.calculated_name, "deferred", now=harness.clock(), reason="down"
        )
    (harness.ideas / "archive" / stalled.calculated_name).unlink()
    (harness.ideas / "output" / stalled.calculated_name).unlink()

    with caplog.at_level("WARNING", logger="catcher.worker"):
        result = run(harness, retry_deferred=True, only=["systeme"])

    assert without_names(result) == Done(counts())
    assert item_named(harness, "clippings", "systeme.md").status == "deferred"
    assert [j for j in next_jobs(harness) if j.status == "queued"] == []
    assert any(
        r.levelno == logging.WARNING and stalled.calculated_name in r.getMessage() for r in caplog.records
    )


# ---- the names behind the counts (B72): what leaves no item row, for the report -------------------------


def names_of(result) -> dict:
    assert isinstance(result, Done) and result.result is not None
    return result.result["names"]


def test_pipeline_run_returns_the_names(harness, caplog):
    ideas = harness.ideas
    (ideas / "inbox/clippings/chat short.md").write_text(_chat(_turns(3)))
    (ideas / "inbox/clippings/chat long.md").write_text(_chat(_turns(6)))
    (ideas / "inbox/notes/bad.md").write_text("---\ntitle: [oops\n---\nbody\n")
    (ideas / "inbox/report.pdf").write_bytes(b"%PDF-1.7 binary \x00\x01")

    with caplog.at_level("WARNING", logger="catcher.worker"):
        result = run(harness, only=["chat short", "chat long", "bad", "report.pdf", "nothing here"], limit=0)

    assert result.result is not None
    assert {k: v for k, v in result.result.items() if k != "names"} == counts(
        duplicates=1, unreadable=1, artifacts=1
    )
    names = names_of(result)
    assert names["duplicates"] == [
        {
            "name": "clippings/chat short.md",
            "doc_id": "2446cd9c762c9cc9",
            "doc_class": "ai-chat",
            "winner": "clippings/chat long.md",
        }
    ]
    assert list(names["unreadable"]) == ["inbox/notes/bad.md"]
    [artifact] = names["artifacts"]
    assert (artifact["name"], artifact["status"]) == ("report.pdf", "artifact")
    assert artifact["page"].endswith("-report.pdf")
    assert names["not_found"] == ["nothing here"]
    assert names["left_by_limit"] == [
        {"name": "clippings/chat long.md", "doc_id": "2446cd9c762c9cc9", "doc_class": "ai-chat"}
    ]
    assert (names["requeued"], names["requeue_skipped"], names["not_in_archive"]) == ([], [], [])
    assert "truncated" not in names
    assert any('no document named "nothing here" found in inbox/' in r.getMessage() for r in caplog.records)


def test_pipeline_run_names_the_requeue_moves_skips_and_names_not_in_archive(harness):
    run(harness, only=["YouTube walks", "systeme"])
    note = item_named(harness, "notes", "YouTube walks.md")
    chat = item_named(harness, "clippings", "systeme.md")
    _finish_jobs(harness)
    with session_scope(harness.ctx.engine) as session:
        set_item_status(session, note.calculated_name, "published", now=harness.clock())
    (harness.ideas / "inbox" / note.calculated_name).write_text("edited in the inbox\n")  # inbox/ wins
    run(harness, only=["yt"])  # the clip's fetch job is queued: a requeue leaves the clip alone
    clip = item_named(harness, "clippings", "yt.md")
    with session_scope(harness.ctx.engine) as session:
        set_item_status(session, chat.calculated_name, "deferred", now=harness.clock(), reason="down")

    result = run(harness, requeue=["YouTube walks", "systeme", "yt", "never captured"])

    names = names_of(result)
    assert names["requeued"] == [
        {"name": chat.calculated_name, "doc_id": chat.doc_id, "doc_class": "ai-chat"}
    ]
    assert names["requeue_skipped"] == [  # the ones a job holds first, then the ones inbox/ already has
        {
            "name": clip.calculated_name,
            "doc_id": clip.doc_id,
            "doc_class": "youtube",
            "reason": "still being processed by a job (status waiting_youtube)",
        },
        {
            "name": note.calculated_name,
            "doc_id": note.doc_id,
            "doc_class": "note",
            "reason": f"inbox/{note.calculated_name} already exists, not overwritten",
        },
    ]
    assert names["not_in_archive"] == ["never captured"]


def test_an_artifact_over_the_size_limit_is_named_skipped_and_stays_in_the_inbox(harness, caplog):
    (harness.ideas / "inbox/report.pdf").write_bytes(b"%PDF-1.7 binary \x00\x01")
    harness.ctx.services.settings.artifact_max_mb = 0

    with caplog.at_level("WARNING", logger="catcher"):
        result = run(harness, only=["report.pdf"])

    [artifact] = names_of(result)["artifacts"]
    assert (artifact["name"], artifact["status"], artifact["page"]) == ("report.pdf", "skipped", None)
    assert "ARTIFACT_MAX_MB" in artifact["message"]
    assert (harness.ideas / "inbox/report.pdf").exists()
    assert any(r.levelno == logging.WARNING and "ARTIFACT_MAX_MB" in r.getMessage() for r in caplog.records)


def test_two_staged_documents_with_one_id_warn_that_the_later_page_replaces_the_earlier(harness, caplog):
    clips = harness.ideas / "inbox/clippings"
    (clips / "one.md").write_text(_chat(_turns(3).replace("question 2", "something else")))
    (clips / "two.md").write_text(_chat(_turns(6)))

    with caplog.at_level("WARNING", logger="catcher.worker"):
        result = run(harness, only=["one", "two"])

    assert result.result is not None and result.result["staged"] == 2
    [later] = names_of(result)["same_id"]  # the one staged second; which one that is, is the scan's order
    assert later in {item_named(harness, "clippings", f"{n}.md").calculated_name for n in ("one", "two")}
    assert "replaces the earlier one" in caplog.text


def test_the_names_are_bounded(harness, monkeypatch):
    monkeypatch.setattr(handlers_pipeline, "NAMES_MAX", 2)
    for i in range(4):
        (harness.ideas / f"inbox/notes/n{i}.md").write_text(f"note {i}\n")

    result = run(harness, limit=0)

    names = names_of(result)
    assert len(names["left_by_limit"]) == 2 and names["truncated"] is True
    assert result.result is not None and result.result["staged"] == 0
    assert json.loads(json.dumps(result.result)) == result.result  # plain JSON, stored on the job as it is


def test_refresh_facts_is_copied_into_the_fetch_job_even_when_facts_are_saved(harness, yt_facts):
    harness.ctx.services.youtube.cache(harness.ideas / "facts").put(yt_facts)

    run(harness, only=["yt"], refresh_facts=True)

    item = item_named(harness, "clippings", "yt.md")
    assert item.status == "waiting_youtube"
    [job] = next_jobs(harness)
    assert (job.type, job.params) == (
        "youtube.fetch",
        {"calculated_name": item.calculated_name, "refresh_facts": True},
    )
