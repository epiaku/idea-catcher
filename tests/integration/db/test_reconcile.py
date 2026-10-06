"""`catcher reconcile`: rebuild the item rows from the idea-bucket folders (real Postgres).

It reads the files and never touches one; it creates the rows that are missing, fixes the status of a row
whose file moved, reports a row without a file (never deletes it), skips what it cannot read, changes
nothing on a second run, and closes the YouTube gate unless `--keep-gate` (decision 10)."""

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select, text, update
from typer.testing import CliRunner

from catcher import cli
from catcher.cli import app
from catcher.core.db import session_scope
from catcher.core.frontmatter import Doc, dump
from catcher.core.testdata import reset_test_repos
from catcher.modules.pipeline.inbox import (
    assign_name,
    mark_deferred,
    move_to_duplicates,
    move_to_failed,
    scan_inbox,
    start_work,
)
from catcher.modules.pipeline.mirror import write_mirror
from catcher.modules.pipeline.scan_state import scan_item_files
from catcher.modules.queue.items import stage_item
from catcher.modules.queue.models import JobEvent, JobItem, Resource
from catcher.modules.queue.reconcile import ReconcileReport, reconcile
from catcher.modules.queue.states import ItemStates
from catcher.modules.worker.guard import WorkerLock
from catcher.modules.youtube.gate_rules import clock_text

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
SINCE = datetime(2026, 10, 1, 9, 30, tzinfo=UTC)
REAL_NOW = datetime.now(UTC).replace(microsecond=0)  # the gate prints local times: a real moment
CLOSED_DATABASE = "postgresql+psycopg://catcher:s3cr3t-pw@127.0.0.1:1/catcher"  # nothing listens on port 1


# ---- helpers ---------------------------------------------------------------------------------------------


def url_of(engine: Engine) -> str:
    return engine.url.render_as_string(hide_password=False)


def write(path: Path, fm: dict, body: str = "Some text\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump(Doc(fm, body)), encoding="utf-8")
    return path


def working_copy(ideas: Path, name: str, stage: str, **extra) -> Path:
    stem = Path(name).stem
    fm = {"id": f"id-{stem}", "class": "note", "captured": "2026-10-01", "original_filename": f"{stem}.md"}
    return write(ideas / "output" / name, {**fm, "stage": stage, **extra})


def finished_page(ideas: Path, name: str, **extra) -> Path:
    stem = Path(name).stem
    fm = {
        "title": "A page",
        "type": "docs",
        "id": f"id-{stem}",
        "destination": "notes",
        "stage": "published",
        "created_by": "idea catcher",
        "source_file": name,
        "original_filename": f"{stem}.md",
        **extra,
    }
    return write(ideas / "output" / name, fm, "## Summary\n\nDone.\n")


def failed_file(ideas: Path, name: str, reason: str = "the LLM reply was not valid") -> Path:
    stem = Path(name).stem
    path = write(
        ideas / "failed" / name,
        {"id": f"id-{stem}", "class": "note", "original_filename": f"{stem}.md", "stage": "waiting_llm"},
    )
    path.with_suffix(".error.txt").write_text(
        f"time: 2026-10-02T08:00:00+00:00\noriginal file: {stem}.md\ncalculated file: {name}\n"
        f"id: id-{stem}\nclass: note\nreason: {reason}\n",
        encoding="utf-8",
    )
    return path


def duplicate_file(ideas: Path, name: str) -> Path:
    stem = Path(name).stem
    fm = {"id": f"id-{stem}", "class": "ai-chat", "original_filename": f"{stem}.md", "duplicate_of": "x.md"}
    return write(ideas / "duplicates" / name, fm)


def make_ideas(tmp_path: Path) -> Path:
    ideas = tmp_path / "idea-bucket"
    (ideas / "inbox").mkdir(parents=True)
    return ideas


def run(engine: Engine, ideas: Path, *, apply: bool = True, now: datetime = NOW) -> ReconcileReport:
    with session_scope(engine) as session:
        return reconcile(session, ideas, now=now, apply=apply, scan=scan_item_files)


def rows(engine: Engine) -> dict[str, JobItem]:
    with session_scope(engine) as session:
        return {i.calculated_name: i for i in session.scalars(select(JobItem))}


def events(engine: Engine) -> list[JobEvent]:
    with session_scope(engine) as session:
        return list(session.scalars(select(JobEvent).order_by(JobEvent.id)))


def snapshot(root: Path) -> dict[str, str]:
    """Every file under `root` and a hash of its bytes: reconcile must never change one."""
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".git" not in p.parts
    }


def seed_row(engine: Engine, name: str, status: str, *, reason: str | None = None) -> None:
    with session_scope(engine) as session:
        stage_item(
            session,
            calculated_name=name,
            doc_id=f"id-{Path(name).stem}",
            doc_class="note",
            now=SINCE,
            inbox_path=f"inbox/{name}",
            original_filename=Path(name).name,
        )
        if status != "staging":
            ItemStates().transition(session, name, status, now=SINCE, reason=reason)


def the_tree(ideas: Path) -> None:
    working_copy(ideas, "notes/a.md", "waiting_llm", stage_reason="queued", stage_since=SINCE.isoformat())
    finished_page(ideas, "notes/b.md")
    failed_file(ideas, "notes/c.md")
    duplicate_file(ideas, "clippings/d.md")


# ---- reconcile ---------------------------------------------------------------------------------------------


def test_reconcile_creates_rows_for_files_without_one(pg_engine, tmp_path):
    ideas = make_ideas(tmp_path)
    the_tree(ideas)
    (ideas / "archive" / "notes").mkdir(parents=True)
    (ideas / "archive" / "notes" / "b.md").write_text("---\nid: id-b\n---\noriginal\n", encoding="utf-8")
    before = snapshot(ideas)

    report = run(pg_engine, ideas)

    assert sorted(report.created) == ["clippings/d.md", "notes/a.md", "notes/b.md", "notes/c.md"]
    assert report.status_fixed == [] and report.missing_files == [] and report.skipped == {}
    found = rows(pg_engine)
    assert {name: item.status for name, item in found.items()} == {
        "notes/a.md": "waiting_llm",
        "notes/b.md": "published",
        "notes/c.md": "failed",
        "clippings/d.md": "duplicate",
    }
    a, b, c, d = (found[n] for n in ("notes/a.md", "notes/b.md", "notes/c.md", "clippings/d.md"))
    assert (a.doc_id, a.doc_class, a.origin, a.original_filename) == ("id-a", "note", "inbox", "a.md")
    assert (a.stage_reason, a.stage_since, a.output_path) == ("queued", SINCE, "output/notes/a.md")
    assert (a.created_at, a.updated_at) == (NOW, NOW)
    assert (b.stage_since, b.archive_path, b.output_path) == (NOW, "archive/notes/b.md", "output/notes/b.md")
    assert c.failed_path == "failed/notes/c.md" and c.stage_reason == "the LLM reply was not valid"
    assert c.stage_since == datetime(2026, 10, 2, 8, 0, tzinfo=UTC)
    assert (d.doc_class, d.status) == ("ai-chat", "duplicate")
    logged = events(pg_engine)
    assert len(logged) == 4
    assert all(e.level == "info" and e.message.startswith("reconcile: created ") for e in logged)
    assert {e.item_id for e in logged} == {item.id for item in found.values()}
    assert snapshot(ideas) == before  # no file was touched


def test_reconcile_fixes_the_status_of_a_row_whose_file_moved_to_failed(pg_engine, tmp_path):
    ideas = make_ideas(tmp_path)
    seed_row(pg_engine, "notes/c.md", "waiting_llm")
    failed_file(ideas, "notes/c.md", reason="the page is invalid")
    before_events = len(events(pg_engine))

    report = run(pg_engine, ideas)

    assert report.status_fixed == [("notes/c.md", "waiting_llm", "failed")]
    assert report.created == [] and report.missing_files == [] and report.skipped == {}
    item = rows(pg_engine)["notes/c.md"]
    assert (item.status, item.stage_since, item.failed_path) == ("failed", NOW, "failed/notes/c.md")
    assert item.stage_reason is not None and "the page is invalid" in item.stage_reason
    new = events(pg_engine)[before_events:]
    assert len(new) == 1
    assert new[0].level == "warning" and "reconcile" in new[0].message
    assert "waiting_llm -> failed" in new[0].message


def test_a_row_without_a_file_is_reported_not_deleted(pg_engine, tmp_path):
    ideas = make_ideas(tmp_path)
    seed_row(pg_engine, "notes/gone.md", "deferred", reason="the model is down")
    seed_row(pg_engine, "notes/requeued.md", "deferred")  # its file is back in inbox/: not missing
    write(ideas / "inbox" / "notes" / "requeued.md", {"id": "id-requeued"})
    before = rows(pg_engine)
    before_events = len(events(pg_engine))

    report = run(pg_engine, ideas)

    assert report.missing_files == ["notes/gone.md"]
    assert report.created == [] and report.status_fixed == [] and report.skipped == {}
    after = rows(pg_engine)
    assert set(after) == {"notes/gone.md", "notes/requeued.md"}  # nothing deleted
    for name in after:
        old, new = before[name], after[name]
        assert (new.status, new.stage_reason, new.stage_since, new.updated_at) == (
            old.status,
            old.stage_reason,
            old.stage_since,
            old.updated_at,
        )
    assert len(events(pg_engine)) == before_events


def test_a_damaged_frontmatter_is_skipped_with_a_reason(pg_engine, tmp_path):
    ideas = make_ideas(tmp_path)
    bad = ideas / "output" / "notes" / "bad.md"
    bad.parent.mkdir(parents=True)
    bad.write_text("---\nid: [never closed\nstage: waiting_llm\n---\nbody\n", encoding="utf-8")
    no_close = ideas / "failed" / "notes" / "open.md"
    no_close.parent.mkdir(parents=True)
    no_close.write_text("---\nid: x\nbody without an end\n", encoding="utf-8")
    binary = ideas / "duplicates" / "notes" / "binary.md"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"\xff\xfe\x00 not utf-8")
    unknown = working_copy(ideas, "notes/odd.md", "halfway")
    no_id = write(ideas / "output" / "notes" / "noid.md", {"class": "note", "stage": "waiting_llm"})
    working_copy(ideas, "notes/good.md", "deferred", stage_reason="model down")

    report = run(pg_engine, ideas)

    assert report.created == ["notes/good.md"]
    assert set(report.skipped) == {
        "notes/bad.md",
        "notes/open.md",
        "notes/binary.md",
        "notes/odd.md",
        "notes/noid.md",
    }
    assert all(reason for reason in report.skipped.values())
    assert "frontmatter" in report.skipped["notes/bad.md"]
    assert "halfway" in report.skipped["notes/odd.md"]
    assert "id" in report.skipped["notes/noid.md"]
    assert set(rows(pg_engine)) == {"notes/good.md"}
    assert unknown.exists() and no_id.exists() and bad.exists()


def test_reconcile_is_idempotent(pg_engine, tmp_path):
    ideas = make_ideas(tmp_path)
    the_tree(ideas)
    seed_row(pg_engine, "notes/e.md", "waiting_llm")
    failed_file(ideas, "notes/e.md")
    seed_row(pg_engine, "notes/gone.md", "deferred")
    (ideas / "output" / "notes").mkdir(parents=True, exist_ok=True)
    (ideas / "output" / "notes" / "bad.md").write_text("---\nid: [\n---\n", encoding="utf-8")
    first = run(pg_engine, ideas)
    assert first.created and first.status_fixed and first.missing_files and first.skipped
    after_first = {
        n: (i.status, i.stage_reason, i.stage_since, i.updated_at) for n, i in rows(pg_engine).items()
    }
    events_after_first = len(events(pg_engine))

    second = run(pg_engine, ideas, now=NOW + timedelta(hours=1))

    assert second.created == [] and second.status_fixed == []
    assert second.missing_files == first.missing_files and second.skipped == first.skipped  # reported again
    after_second = {
        n: (i.status, i.stage_reason, i.stage_since, i.updated_at) for n, i in rows(pg_engine).items()
    }
    assert after_second == after_first
    assert len(events(pg_engine)) == events_after_first


def test_dry_run_changes_nothing(pg_engine, tmp_path):
    ideas = make_ideas(tmp_path)
    the_tree(ideas)
    seed_row(pg_engine, "notes/e.md", "waiting_llm")
    failed_file(ideas, "notes/e.md")
    before_rows = {n: (i.status, i.updated_at) for n, i in rows(pg_engine).items()}
    before_events = len(events(pg_engine))
    before_files = snapshot(ideas)

    dry = run(pg_engine, ideas, apply=False)

    assert sorted(dry.created) == ["clippings/d.md", "notes/a.md", "notes/b.md", "notes/c.md"]
    assert dry.status_fixed == [("notes/e.md", "waiting_llm", "failed")]
    assert {n: (i.status, i.updated_at) for n, i in rows(pg_engine).items()} == before_rows
    assert len(events(pg_engine)) == before_events
    assert snapshot(ideas) == before_files
    real = run(pg_engine, ideas)  # the dry run said what the real run does
    assert (sorted(real.created), real.status_fixed) == (sorted(dry.created), dry.status_fixed)


def test_a_name_in_two_folders_is_skipped(pg_engine, tmp_path):
    ideas = make_ideas(tmp_path)
    working_copy(ideas, "notes/twice.md", "waiting_llm")
    failed_file(ideas, "notes/twice.md")
    finished_page(ideas, "notes/both.md")
    duplicate_file(ideas, "notes/both.md")
    seed_row(pg_engine, "notes/both.md", "deferred")
    before = rows(pg_engine)["notes/both.md"]

    report = run(pg_engine, ideas)

    assert set(report.skipped) == {"notes/twice.md", "notes/both.md"}
    assert report.skipped["notes/twice.md"].startswith("in two folders")
    assert "output/" in report.skipped["notes/twice.md"] and "failed/" in report.skipped["notes/twice.md"]
    assert report.created == [] and report.status_fixed == [] and report.missing_files == []
    after = rows(pg_engine)
    assert set(after) == {"notes/both.md"}
    assert (after["notes/both.md"].status, after["notes/both.md"].updated_at) == (
        "deferred",
        before.updated_at,
    )


def test_stage_a_names_are_understood(pg_engine, tmp_path):
    ideas = make_ideas(tmp_path)
    analyzed_at = datetime(2026, 10, 3, 7, 0, tzinfo=UTC)
    deferred_at = datetime(2026, 10, 4, 6, 0, tzinfo=UTC)
    working_copy(ideas, "notes/working.md", "analyzed", analyzed_at=analyzed_at.isoformat())
    working_copy(
        ideas,
        "notes/stalled.md",
        "deferred",
        deferred_at=deferred_at.isoformat(),
        deferred_reason="the LLM is down",
    )
    write(ideas / "output" / "notes" / "old-page.md", {"id": "id-old", "title": "no stage key"})

    report = run(pg_engine, ideas)

    assert sorted(report.created) == ["notes/old-page.md", "notes/stalled.md", "notes/working.md"]
    found = rows(pg_engine)
    working, stalled, page = found["notes/working.md"], found["notes/stalled.md"], found["notes/old-page.md"]
    assert (working.status, working.stage_since, working.stage_reason) == ("waiting_llm", analyzed_at, None)
    assert (stalled.status, stalled.stage_since, stalled.stage_reason) == (
        "deferred",
        deferred_at,
        "the LLM is down",
    )
    assert page.status == "published"


def test_reconcile_after_deleting_the_database(pg_engine, tmp_path):
    """The committed test data (copied by `reset_test_repos`) taken to a mixed state with the pipeline's own
    file helpers; then the rows are gone, as after a new database: every document gets its row back."""
    repos = reset_test_repos(tmp_path / "ic")
    ideas = repos["idea-bucket"]
    notes = sorted(scan_inbox(ideas, now=NOW).notes, key=lambda n: n.rel.as_posix())
    assert len(notes) > 12
    expected: dict[str, tuple[str, str, str]] = {}  # name -> (status, doc id, class)
    winner = notes[0]
    for index, note in enumerate(notes[:-1]):
        assign_name(ideas, note)
        name = note.target_rel.as_posix()
        start_work(ideas, note, NOW)  # Stage A: `stage: analyzed` in output/
        out = ideas / "output" / name
        kind = index % 6
        status = "waiting_llm"
        if kind == 1:
            mark_deferred(ideas, note, "the LLM is down", NOW)
            status = "deferred"
        elif kind == 2:
            write_mirror(out, stage="waiting_youtube", reason=None, since=NOW)
            status = "waiting_youtube"
        elif kind == 3:
            write_mirror(out, stage="stuck", reason="deferred for 3 days", since=NOW)
            status = "stuck"
        elif kind == 4:  # the finished page replaces the working copy
            fm = {
                "title": "t",
                "type": "docs",
                "id": note.doc_id,
                "stage": "published",
                "created_by": "idea catcher",
                "source_file": name,
                "original_filename": note.original_name,
            }
            out.write_text(dump(Doc(fm, "page\n")), encoding="utf-8")
            status = "published"
        elif kind == 5:
            move_to_failed(ideas, out, "the page is invalid", doc_id=note.doc_id, doc_class=note.doctype.name)
            status = "failed"
        expected[name] = (status, note.doc_id, note.doctype.name)
    last = notes[-1]
    move_to_duplicates(ideas, last, winner)
    expected[last.target_rel.as_posix()] = ("duplicate", last.doc_id, last.doctype.name)
    files = snapshot(ideas)

    first = run(pg_engine, ideas)
    assert first.skipped == {} and sorted(first.created) == sorted(expected)
    with session_scope(pg_engine) as session:  # the database is lost
        session.execute(text("delete from job_events"))
        session.execute(text("delete from job_items"))

    again = run(pg_engine, ideas, now=NOW + timedelta(days=1))

    assert again.skipped == {} and again.missing_files == [] and again.status_fixed == []
    assert sorted(again.created) == sorted(expected)
    found = rows(pg_engine)
    assert {n: (i.status, i.doc_id, i.doc_class) for n, i in found.items()} == expected
    assert {s for s, _, _ in expected.values()} == {
        "waiting_llm",
        "deferred",
        "waiting_youtube",
        "stuck",
        "published",
        "failed",
        "duplicate",
    }
    assert snapshot(ideas) == files


# ---- catcher reconcile -------------------------------------------------------------------------------------


@pytest.fixture
def runner(pg_engine, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    monkeypatch.setenv("DATABASE_URL", url_of(pg_engine))
    monkeypatch.setenv("YOUTUBE_BLOCK_HOURS", "6")
    monkeypatch.setattr(cli, "utc_now", lambda: REAL_NOW)
    return CliRunner()


def gate_row(engine: Engine) -> Resource:
    with session_scope(engine) as session:
        found = session.get(Resource, "youtube")
        assert found is not None
        return found


def test_the_gate_is_closed_unless_keep_gate(runner: CliRunner, pg_engine, tmp_path):
    ideas = make_ideas(tmp_path)
    the_tree(ideas)

    kept = runner.invoke(app, ["reconcile", "--ideas", str(ideas), "--keep-gate"])
    assert kept.exit_code == 0, kept.output
    assert gate_row(pg_engine).blocked_until is None
    assert "YouTube gate closed" not in kept.output

    dry = runner.invoke(app, ["reconcile", "--ideas", str(ideas), "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert gate_row(pg_engine).blocked_until is None

    closed = runner.invoke(app, ["reconcile", "--ideas", str(ideas)])
    assert closed.exit_code == 0, closed.output
    row = gate_row(pg_engine)
    until = REAL_NOW + timedelta(hours=6)
    assert row.blocked_until == until and row.blocked_at == REAL_NOW and row.streak == 1
    assert (
        f"YouTube gate closed until {clock_text(until.timestamp(), REAL_NOW.timestamp())} "
        "(reconcile; use --keep-gate to skip)"
    ) in closed.output
    shown = runner.invoke(app, ["youtube", "gate"])
    assert shown.exit_code == 0 and "blocked until" in shown.output

    longer = REAL_NOW + timedelta(hours=20)  # a longer block is never shortened, nor its streak lowered
    with session_scope(pg_engine) as session:
        session.execute(
            update(Resource).where(Resource.name == "youtube").values(blocked_until=longer, streak=3)
        )
    again = runner.invoke(app, ["reconcile", "--ideas", str(ideas)])
    assert again.exit_code == 0, again.output
    row = gate_row(pg_engine)
    assert (row.blocked_until, row.streak) == (longer, 3)


def test_reconcile_prints_one_line_per_change_and_a_summary(runner: CliRunner, pg_engine, tmp_path):
    ideas = make_ideas(tmp_path)
    the_tree(ideas)
    seed_row(pg_engine, "notes/gone.md", "deferred")

    result = runner.invoke(app, ["reconcile", "--ideas", str(ideas), "--keep-gate"])

    assert result.exit_code == 0, result.output
    lines = result.output.strip().splitlines()
    assert sum(line.startswith("created") for line in lines) == 4
    assert any(line.startswith("missing") and "notes/gone.md" in line for line in lines)
    assert "created=4" in lines[-1] and "missing=1" in lines[-1]
    with session_scope(pg_engine) as session:
        assert session.scalar(select(func.count()).select_from(JobItem)) == 5


def test_reconcile_exits_2_while_a_worker_holds_the_lock(runner: CliRunner, pg_engine, tmp_path):
    ideas = make_ideas(tmp_path)
    the_tree(ideas)
    with WorkerLock(pg_engine):
        result = runner.invoke(app, ["reconcile", "--ideas", str(ideas)])
    assert result.exit_code == 2, result.output
    assert "another worker or run is already running; one at a time" in result.output
    assert rows(pg_engine) == {}
    assert gate_row(pg_engine).blocked_until is None


def test_reconcile_exits_2_without_the_database(runner: CliRunner, tmp_path, monkeypatch):
    ideas = make_ideas(tmp_path)
    the_tree(ideas)
    monkeypatch.setenv("DATABASE_URL", CLOSED_DATABASE)
    down = runner.invoke(app, ["reconcile", "--ideas", str(ideas)])
    assert down.exit_code == 2, down.output
    assert "cannot reach the database in DATABASE_URL" in down.output
    assert "s3cr3t-pw" not in down.output and "Traceback" not in down.output
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://catcher:s3cr3t-pw@localhost:notaport/catcher")
    malformed = runner.invoke(app, ["reconcile", "--ideas", str(ideas)])
    assert malformed.exit_code == 2, malformed.output
    assert "DATABASE_URL is not a valid database URL" in malformed.output
    assert "s3cr3t-pw" not in malformed.output


# ---- fix round 1: one bad file never stops the rest -------------------------------------------------------

BROKEN_SOURCE = "https://[oops/x"  # urlparse raises ValueError (Invalid IPv6 URL) on it


def test_a_file_whose_source_cannot_be_parsed_is_skipped_and_the_rest_reconciled(pg_engine, tmp_path):
    ideas = make_ideas(tmp_path)
    write(
        ideas / "output" / "notes" / "noclass.md",
        {"id": "id-x", "source": BROKEN_SOURCE, "stage": "waiting_llm"},
    )
    write(ideas / "failed" / "notes" / "noid.md", {"source": BROKEN_SOURCE})  # no class, no id
    finished_page(ideas, "notes/page.md")
    write(ideas / "archive" / "notes" / "page.md", {"source": BROKEN_SOURCE})  # its class comes from here
    working_copy(ideas, "notes/good.md", "deferred")

    report = run(pg_engine, ideas)

    assert report.created == ["notes/good.md"]
    assert set(report.skipped) == {"notes/noclass.md", "notes/noid.md", "notes/page.md"}
    for reason in report.skipped.values():
        assert reason.startswith("cannot read") and "Traceback" not in reason and "\n" not in reason
    assert set(rows(pg_engine)) == {"notes/good.md"}


def test_odd_files_are_skipped_not_fatal(pg_engine, tmp_path):
    ideas = make_ideas(tmp_path)
    (ideas / "output" / "notes").mkdir(parents=True)
    (ideas / "output" / "notes" / "list.md").write_text("---\n- a\n- b\n---\nbody\n", encoding="utf-8")
    (ideas / "output" / "notes" / "empty.md").write_text("", encoding="utf-8")
    (ideas / "output" / "notes" / "binary.md").write_bytes(b"\x00\xff\xfe\x81 not text")
    working_copy(ideas, "notes/good.md", "waiting_llm")

    report = run(pg_engine, ideas)

    assert report.created == ["notes/good.md"]
    assert set(report.skipped) == {"notes/list.md", "notes/empty.md", "notes/binary.md"}
    assert "mapping" in report.skipped["notes/list.md"]


def test_a_failure_after_the_gate_was_closed_says_so(runner: CliRunner, pg_engine, tmp_path, monkeypatch):
    from sqlalchemy.exc import OperationalError

    ideas = make_ideas(tmp_path)
    the_tree(ideas)

    def broken(*args, **kwargs):
        raise OperationalError("select 1", {}, Exception("server closed the connection"))

    monkeypatch.setattr(cli, "reconcile", broken)
    result = runner.invoke(app, ["reconcile", "--ideas", str(ideas)])

    assert result.exit_code == 1, result.output
    until = clock_text((REAL_NOW + timedelta(hours=6)).timestamp(), REAL_NOW.timestamp())
    assert f"the YouTube gate was already closed until {until}" in result.output
    assert "no row was written" in result.output
    assert gate_row(pg_engine).blocked_until == REAL_NOW + timedelta(hours=6)
    assert rows(pg_engine) == {}
