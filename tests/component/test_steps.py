from dataclasses import replace
from pathlib import Path

from catcher.modules.pipeline.steps import order_notes, split_duplicates

ONE = "**You**\n\nfirst question\n\n---\n\n**Gemini**\n\nAnswer one\n"
TWO = ONE + "\n---\n\n**You**\n\nsecond question\n\n---\n\n**Gemini**\n\nAnswer two\n"
THREE = TWO + "\n---\n\n**You**\n\nthird question\n\n---\n\n**Gemini**\n\nAnswer three\n"


def clip(make_note, name: str, body: str, *, doc_id: str = "cafe01", captured: str = "2026-09-27", root=None):
    note = make_note("note", doc_id=doc_id, body=body, root=root, captured=captured)
    return replace(note, path=Path("inbox/notes") / name)


def test_notes_are_ordered_by_captured_date_then_id_then_path(make_note):
    late = clip(make_note, "z.md", "x", doc_id="aaaaaa", captured="2026-09-28")
    b2 = clip(make_note, "b.md", "x", doc_id="bbbbbb")
    b1 = clip(make_note, "a.md", "x", doc_id="bbbbbb")
    a = clip(make_note, "c.md", "x", doc_id="aaaaaa")
    assert order_notes([late, b2, b1, a]) == [a, b1, b2, late]


def test_the_longest_clip_wins_and_earlier_snapshots_are_duplicates(make_note):
    one, two, three = (clip(make_note, f"{i}.md", b) for i, b in enumerate([ONE, TWO, THREE]))
    to_process, duplicates = split_duplicates([one, two, three])
    assert to_process == [three]
    assert duplicates == [(one, three), (two, three)]


def test_a_clip_with_other_content_and_the_same_id_is_processed_not_a_duplicate(make_note):
    other = clip(
        make_note, "0.md", "**You**\n\nsomething else\n\n---\n\n**Gemini**\n\nNo\n\n---\n\n**You**\n\nmore\n"
    )
    longest = clip(make_note, "1.md", THREE)
    to_process, duplicates = split_duplicates([other, longest])
    assert to_process == [other, longest]
    assert duplicates == []


def test_equal_clips_are_both_processed_because_a_snapshot_must_be_shorter(make_note):
    first = clip(make_note, "0.md", ONE)
    second = clip(make_note, "1.md", ONE)
    to_process, duplicates = split_duplicates([first, second])
    assert to_process == [first, second]
    assert duplicates == []


def test_split_does_not_touch_the_files(make_note, tmp_path):
    one = make_note("note", doc_id="cafe01", body=ONE, root=tmp_path)
    two = replace(
        make_note("note", doc_id="cafe01", body=THREE, root=tmp_path), path=tmp_path / "inbox/notes/b.md"
    )
    two.path.write_text("three", encoding="utf-8")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    split_duplicates(order_notes([one, two]))
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
