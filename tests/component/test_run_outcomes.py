"""`apply_outcome`: what each outcome does to the report item, the run state and the files."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from catcher.core.frontmatter import load
from catcher.modules.llm.service import BudgetExhausted, UsageLimitReached
from catcher.modules.pipeline.inbox import Note, scan_inbox, start_work
from catcher.modules.pipeline.outcome import Outcome, OutcomeKind, RunState, apply_outcome, classify
from catcher.modules.pipeline.report import ItemReport

NOW = datetime(2026, 9, 27, 18, 0, 0, tzinfo=UTC)


def started(root: Path) -> Note:
    """One note in inbox/notes/, taken out of the inbox by `start_work` (as the run loop does)."""
    path = root / "inbox/notes/idea.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("An idea\n", encoding="utf-8")
    [note] = scan_inbox(root, now=NOW).notes
    start_work(root, note, now=NOW)
    return note


def files(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def new_item(note: Note) -> ItemReport:
    return ItemReport(note.doc_id, note.doctype.name, "skipped")


def new_state(attempted: int = 1) -> RunState:
    return RunState(blocked=set(), budget_blocked={}, attempted=attempted, seen_ids=set())


def test_deferred_keeps_the_working_copy_in_output_with_the_reason(tmp_path):
    note = started(tmp_path)
    item, state = new_item(note), new_state()
    outcome = Outcome("deferred", "the backend is down")
    touched = apply_outcome(tmp_path, note, outcome, state, item, dry_run=False)
    out = note.output_path(tmp_path)
    assert (item.status, item.message) == ("deferred", "the backend is down")
    assert touched == [out]
    stalled = load(out)
    assert stalled.fm["stage"] == "deferred" and stalled.fm["deferred_reason"] == "the backend is down"
    assert state == new_state()  # no backend named: nothing is blocked


def test_failed_moves_the_working_copy_to_failed_with_an_error_file(tmp_path):
    note = started(tmp_path)
    item, state = new_item(note), new_state()
    touched = apply_outcome(tmp_path, note, Outcome("failed", "invalid output"), state, item, dry_run=False)
    failed = tmp_path / "failed/notes" / str(note.name)
    error = failed.with_suffix(".error.txt")
    assert (item.status, item.message) == ("failed", "invalid output")
    assert failed.is_file() and not note.output_path(tmp_path).exists()
    text = error.read_text()
    assert "reason: invalid output" in text and f"id: {note.doc_id}" in text and "class: note" in text
    assert touched == [note.output_path(tmp_path), failed, error]
    assert state == new_state()


def test_waiting_returns_the_document_to_the_inbox(tmp_path):
    note = started(tmp_path)
    item, state = new_item(note), new_state()
    touched = apply_outcome(
        tmp_path, note, Outcome("waiting", "gap (back in inbox/)"), state, item, dry_run=False
    )
    assert (item.status, item.message) == ("waiting", "gap (back in inbox/)")
    assert note.path.is_file() and not note.output_path(tmp_path).exists()
    archived = tmp_path / "archive/notes" / str(note.name)
    assert not archived.exists()
    assert touched == [archived, note.path, note.output_path(tmp_path)]


def test_interrupted_returns_the_document_to_the_inbox(tmp_path):
    note = started(tmp_path)
    item, state = new_item(note), new_state()
    touched = apply_outcome(tmp_path, note, classify(KeyboardInterrupt()), state, item, dry_run=False)
    assert (item.status, item.message) == ("interrupted", "back in inbox/")
    assert note.path.is_file() and not note.output_path(tmp_path).exists()
    assert len(touched) == 3 and note.path in touched
    assert state == new_state()  # an interrupted document still counts as attempted


def test_would_fetch_touches_nothing(tmp_path):
    note = started(tmp_path)
    before = files(tmp_path)
    item, state = new_item(note), new_state()
    touched = apply_outcome(tmp_path, note, Outcome("would_fetch", "dry run"), state, item, dry_run=False)
    assert (item.status, item.message) == ("would_fetch", "dry run")
    assert touched == [] and files(tmp_path) == before and state == new_state()


def test_a_budget_outcome_blocks_the_backend_and_counts_the_note(tmp_path):
    note = started(tmp_path)
    item, state = new_item(note), new_state()
    outcome = classify(BudgetExhausted("spent", "openai"))
    apply_outcome(tmp_path, note, outcome, state, item, dry_run=False)
    assert (item.status, item.message) == ("deferred", "budget reached (openai)")
    assert state.blocked == {"openai"} and state.budget_blocked == {"openai": 1}


def test_a_usage_limit_blocks_the_backend_but_is_not_a_budget(tmp_path):
    note = started(tmp_path)
    item, state = new_item(note), new_state()
    outcome = classify(UsageLimitReached("429", "openai"))
    apply_outcome(tmp_path, note, outcome, state, item, dry_run=False)
    assert item.status == "deferred" and item.message == outcome.message
    assert item.message.startswith("usage limit (openai): ")
    assert state.blocked == {"openai"} and state.budget_blocked == {}


def test_a_second_notes_usage_limit_on_a_budget_blocked_backend_says_budget_reached(tmp_path):
    note = started(tmp_path)
    item = new_item(note)
    state = RunState(blocked={"openai"}, budget_blocked={"openai": 1}, attempted=2, seen_ids=set())
    apply_outcome(tmp_path, note, classify(UsageLimitReached("429", "openai")), state, item, dry_run=False)
    assert (item.status, item.message) == ("deferred", "budget reached (openai)")
    assert state.budget_blocked == {"openai": 2}
    assert load(note.output_path(tmp_path)).fm["deferred_reason"] == "budget reached (openai)"


def test_a_waiting_outcome_gives_the_attempt_back(tmp_path):
    note = started(tmp_path)
    state = new_state(attempted=3)
    apply_outcome(tmp_path, note, Outcome("waiting", "x"), state, new_item(note), dry_run=False)
    assert state.attempted == 2


KINDS: list[OutcomeKind] = ["deferred", "failed", "waiting", "would_fetch", "interrupted"]


@pytest.mark.parametrize("kind", KINDS)
def test_dry_run_changes_no_file_for_any_kind(tmp_path, kind):
    note = started(tmp_path)  # a dry run never starts work; this checks no effect runs at all
    before = files(tmp_path)
    item, state = new_item(note), new_state(attempted=1)
    touched = apply_outcome(tmp_path, note, Outcome(kind, "why"), state, item, dry_run=True)
    assert touched == [] and files(tmp_path) == before
    assert (item.status, item.message) == (kind, "why")
    assert state.attempted == (0 if kind == "waiting" else 1)  # the state is kept the same way
