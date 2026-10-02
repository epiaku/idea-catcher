import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from catcher.cli import app
from catcher.core.frontmatter import load
from catcher.modules.pipeline.inbox import (
    Artifact,
    archive_copy,
    artifact_name,
    assign_name,
    calculated_stem,
    copy_artifact,
    deferred_in_output,
    is_snapshot_of,
    load_staged_note,
    mark_deferred,
    move_to_duplicates,
    move_to_failed,
    name_matches,
    name_title,
    read_note,
    return_to_inbox,
    scan_inbox,
    start_work,
    with_filename_fields,
)

NOW = datetime(2026, 9, 27, 18, 0, 0, tzinfo=UTC)


def put(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def gemini_clip(chat_id: str, body: str, created: str = "2026-09-03") -> str:
    return (
        f'---\nsource : "https://gemini.google.com/app/{chat_id}?is_sa=1&utm_source=sem"\n'
        f'author:\ncreated: {created}\ntags:\n  - "clippings"\n---\n{body}'
    )


def files(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): p.read_text() for p in root.rglob("*") if p.is_file()}


def test_scan_reads_the_inbox_and_writes_nothing(tmp_path):
    put(tmp_path, "inbox/notes/YouTube walks.md", "Create YouTube content walking around\n")
    before = files(tmp_path)
    result = scan_inbox(tmp_path, now=NOW)
    [note] = result.notes
    assert files(tmp_path) == before  # not a single file was created, changed or moved
    assert note.doctype.name == "note" and re.fullmatch(r"[0-9a-f]{6}", note.doc_id)
    assert note.path == tmp_path / "inbox/notes/YouTube walks.md"
    assert note.rel == Path("notes/YouTube walks.md")
    assert note.doc.body == "Create YouTube content walking around\n"
    assert note.doc.fm == {
        "id": note.doc_id,
        "class": "note",
        "captured": "2026-09-27",
        "source_file": "inbox/notes/YouTube walks.md",
    }
    assert result.errors == {}


def test_clip_body_is_kept_byte_for_byte_and_the_class_and_id_come_from_the_source(tmp_path):
    body = "**You**\n\nsysteme.io \\[paid\\] question\n\n---\n\n**Gemini**\n\nYes.\n"
    put(tmp_path, "inbox/clippings/systeme.io.md", gemini_clip("cf81e40b020519ef", body, "2026-09-25"))
    [note] = scan_inbox(tmp_path, now=NOW).notes
    assert note.doctype.name == "ai-chat" and note.doc_id == "cf81e40b020519ef"
    assert note.doc.body == body
    assert note.doc.fm["captured"] == "2026-09-25" and note.doc.fm["tags"] == ["clippings"]


def test_a_file_with_an_id_keeps_it(tmp_path):
    put(
        tmp_path,
        "inbox/notes/a7b2c9.md",
        "---\nid: a7b2c9\nclass: note\ncaptured: '2026-09-20'\n---\nAn old idea\n",
    )
    [note] = scan_inbox(tmp_path, now=NOW).notes
    assert note.doc_id == "a7b2c9" and note.doc.fm["captured"] == "2026-09-20"


def test_unreadable_files_are_reported_and_left_alone(tmp_path):
    bad = put(tmp_path, "inbox/notes/bad.md", "---\ntitle: [oops\n---\nbody\n")
    put(tmp_path, "inbox/notes/good.md", "A good idea\n")
    result = scan_inbox(tmp_path, now=NOW)
    assert bad.exists() and list(result.errors) == ["inbox/notes/bad.md"]
    assert len(result.notes) == 1


def test_hidden_folders_and_other_folders_are_ignored(tmp_path):
    put(tmp_path, "inbox/.trash/deleted.md", "gone\n")
    put(tmp_path, "output/notes/final.md", "---\ntitle: T\n---\nx\n")
    put(tmp_path, "archive/notes/old.md", "old\n")
    put(tmp_path, "failed/notes/f.md", "f\n")
    put(tmp_path, "duplicates/notes/d.md", "d\n")
    assert scan_inbox(tmp_path, now=NOW).notes == []


def test_a_document_can_be_named_in_several_ways():
    rel = Path("clippings/New chat.md")
    for query in ["New chat.md", "New chat", "clippings/New chat.md", "clippings/New chat", "new CHAT"]:
        assert name_matches(query, rel), query
    for query in ["New", "chat.md", "notes/New chat.md", ""]:
        assert not name_matches(query, rel), query


def test_only_the_named_documents_are_scanned(tmp_path):
    put(tmp_path, "inbox/notes/One.md", "first\n")
    put(tmp_path, "inbox/notes/Two.md", "second\n")
    put(tmp_path, "inbox/clippings/Three.md", gemini_clip("cf81e40b020519ef", "q\n"))
    result = scan_inbox(tmp_path, now=NOW, only=["Two", "clippings/Three.md", "Missing"])
    assert sorted(n.path.name for n in result.notes) == ["Three.md", "Two.md"]
    assert result.matched == {"Two", "clippings/Three.md"}


def test_read_note_reads_a_file_from_anywhere(tmp_path):
    path = put(tmp_path, "somewhere/My idea.md", "An idea\n")
    note = read_note(path, now=NOW)
    assert note.doctype.name == "note" and note.doc.body == "An idea\n"


def one(root: Path, folder: str, sub: str) -> Path:
    files_ = [p for p in (root / folder / sub).glob("*.md")]
    assert len(files_) == 1, [p.name for p in files_]
    return files_[0]


def test_a_calculated_name_is_date_guid_and_title():
    stem = calculated_stem("2026-09-25", "a1b2c3", "New chat: Sell bundles?!")
    assert stem == "20260925-a1b2c3-new-chat-sell-bundles"


def test_a_calculated_name_is_never_longer_than_128_characters_with_every_suffix():
    stem = calculated_stem("2026-09-25", "a1b2c3", "very long title " * 30)
    assert len(stem + ".youtube.json") <= 128 and stem.startswith("20260925-a1b2c3-")
    assert not stem.endswith("-")


def test_a_title_without_letters_becomes_untitled_and_accents_are_dropped():
    assert calculated_stem("2026-09-25", "a1b2c3", "???").endswith("-untitled")
    assert calculated_stem("2026-09-25", "a1b2c3", "Café résumé").endswith("-cafe-resume")


def test_a_chat_without_a_useful_title_is_named_after_the_first_words_of_your_first_message(tmp_path):
    message = "How do I sell digital bundles on systeme.io https://x.y/z today and more words"
    body = f"**You**\n\n{message}\n\n---\n\n**Gemini**\n\nAnswer\n"
    put(tmp_path, "inbox/clippings/New chat.md", gemini_clip("cf81e40b020519ef", body))
    [note] = scan_inbox(tmp_path, now=NOW).notes
    assert name_title(note) == "How do I sell digital bundles on systeme.io"  # 8 words, no link


def test_a_clip_title_wins_and_other_kinds_use_the_file_name(tmp_path):
    put(tmp_path, "inbox/notes/Domain idea.md", "Buy a domain\n")
    clip = gemini_clip("cf81e40b020519ef", "**You**\n\nhi there\n").replace(
        "author:", "title: My chat\nauthor:"
    )
    put(tmp_path, "inbox/clippings/x.md", clip)
    notes = {n.path.name: n for n in scan_inbox(tmp_path, now=NOW).notes}
    assert name_title(notes["Domain idea.md"]) == "Domain idea"
    assert name_title(notes["x.md"]) == "My chat"


def test_filename_fields_are_inserted_into_the_existing_frontmatter_as_text():
    raw = '---\r\nsource : "https://x"\r\n# a comment\r\ntags:\r\n  - "clippings"\r\n---\r\nBody\r\n'
    out = with_filename_fields(raw, "New chat.md", "20260925-a1b2c3-x.md")
    assert out.startswith('---\r\nsource : "https://x"\r\n# a comment\r\ntags:\r\n  - "clippings"\r\n')
    assert out.endswith(
        'original_filename: "New chat.md"\r\ncalculated_filename: "20260925-a1b2c3-x.md"\r\n---\r\nBody\r\n'
    )


def test_a_file_without_frontmatter_gets_a_block_and_the_text_is_unchanged():
    out = with_filename_fields("Just a thought\n", "idea.md", "20260925-a1b2c3-idea.md")
    fields = 'original_filename: "idea.md"\ncalculated_filename: "20260925-a1b2c3-idea.md"\n'
    assert out == f"---\n{fields}---\nJust a thought\n"


def test_fields_that_are_already_there_are_kept_so_a_requeued_file_comes_back_unchanged():
    raw = '---\noriginal_filename: "a.md"\ncalculated_filename: "20260925-a1b2c3-a.md"\n---\nx\n'
    assert with_filename_fields(raw, "other.md", "20260925-ffffff-other.md") == raw


def test_the_archive_copy_has_the_calculated_name_and_only_two_added_lines(tmp_path):
    source = "https://gemini.google.com/app/cf81e40b020519ef"
    original = f'---\nsource: "{source}"\ncreated: 2026-09-25\n---\n**You**\n\nsell bundles?\n'
    put(tmp_path, "inbox/clippings/systeme.md", original)
    [note] = scan_inbox(tmp_path, now=NOW).notes
    dest = archive_copy(tmp_path, note)
    assert dest == tmp_path / "archive/clippings" / note.name and note.name.startswith("20260925-")
    assert note.name.endswith("-sell-bundles.md") and note.original == "systeme.md"
    added = ['original_filename: "systeme.md"\n', f'calculated_filename: "{note.name}"\n']
    assert dest.read_text().replace(added[0], "").replace(added[1], "") == original
    assert (tmp_path / "inbox/clippings/systeme.md").read_text() == original  # archive_copy does not move it


def test_two_documents_with_the_same_file_name_never_overwrite_each_other(tmp_path):
    put(tmp_path, "inbox/clippings/New chat.md", gemini_clip("aaaaaaaaaaaaaaaa", "**You**\n\nfirst topic\n"))
    [first] = scan_inbox(tmp_path, now=NOW).notes
    start_work(tmp_path, first, now=NOW)
    put(tmp_path, "inbox/clippings/New chat.md", gemini_clip("bbbbbbbbbbbbbbbb", "**You**\n\nsecond topic\n"))
    [second] = scan_inbox(tmp_path, now=NOW).notes
    start_work(tmp_path, second, now=NOW)
    archived = sorted((tmp_path / "archive/clippings").glob("*.md"))
    assert len(archived) == 2 and first.name != second.name
    assert "first topic" in archived[0].read_text() + archived[1].read_text()
    assert "second topic" in archived[0].read_text() + archived[1].read_text()


def test_a_name_is_never_reused_from_any_folder(tmp_path):
    note_doc = put(tmp_path, "inbox/notes/idea.md", "An idea\n")
    [note] = scan_inbox(tmp_path, now=NOW).notes
    taken = None
    for folder in ("archive", "output", "failed", "duplicates"):
        stem = calculated_stem("2026-09-27", "aaaaaa", "idea")
        put(tmp_path, f"{folder}/notes/{stem}.md", "x")
        taken = stem
    assert taken and note_doc.exists()
    name = assign_name(tmp_path, note)
    assert name != f"{taken}.md"


def test_start_work_moves_the_document_out_of_the_inbox_and_writes_the_copies(tmp_path):
    original = "Create YouTube content walking around\n"
    put(tmp_path, "inbox/notes/YouTube walks.md", original)
    [note] = scan_inbox(tmp_path, now=NOW).notes
    touched = start_work(tmp_path, note, now=NOW)
    assert not (tmp_path / "inbox/notes/YouTube walks.md").exists()
    archived, working = one(tmp_path, "archive", "notes"), one(tmp_path, "output", "notes")
    assert archived.name == working.name == note.name and note.name.endswith("-youtube-walks.md")
    assert load(archived).body == original == load(working).body
    assert load(archived).fm == {"original_filename": "YouTube walks.md", "calculated_filename": note.name}
    assert load(working).fm == {
        "id": note.doc_id,
        "class": "note",
        "captured": "2026-09-27",
        "source_file": "inbox/notes/YouTube walks.md",
        "original_filename": "YouTube walks.md",
        "calculated_filename": note.name,
        "analyzed_at": "2026-09-27T18:00:00+00:00",
        "stage": "analyzed",
    }
    assert working in touched and archived in touched


def test_a_requeued_document_keeps_its_name_and_overwrites_the_stalled_copy(tmp_path):
    put(tmp_path, "inbox/notes/idea.md", "An idea\n")
    [first] = scan_inbox(tmp_path, now=NOW).notes
    start_work(tmp_path, first, now=NOW)
    mark_deferred(tmp_path, first, "budget reached (openai)", now=NOW)
    archived = one(tmp_path, "archive", "notes")
    archived.replace(tmp_path / "inbox/notes" / archived.name)  # the manual retry
    [again] = scan_inbox(tmp_path, now=NOW).notes
    assert again.name == first.name and again.original == "idea.md"
    start_work(tmp_path, again, now=NOW)
    assert one(tmp_path, "archive", "notes").name == first.name  # still one archive file
    working = load(one(tmp_path, "output", "notes"))
    assert working.fm["stage"] == "analyzed" and "deferred_reason" not in working.fm


def test_a_requeued_document_is_found_by_its_original_name_too(tmp_path):
    fields = 'original_filename: "New chat.md"\ncalculated_filename: "20260925-a1b2c3-x.md"\n'
    put(tmp_path, "inbox/clippings/20260925-a1b2c3-x.md", f"---\n{fields}---\n**You**\n\nhi\n")
    for query in ("New chat", "New chat.md", "20260925-a1b2c3-x", "clippings/New chat.md"):
        assert len(scan_inbox(tmp_path, now=NOW, only=[query]).notes) == 1, query


def test_mark_deferred_keeps_the_working_copy_and_records_why(tmp_path):
    put(tmp_path, "inbox/notes/idea.md", "An idea\n")
    [note] = scan_inbox(tmp_path, now=NOW).notes
    start_work(tmp_path, note, now=NOW)
    mark_deferred(tmp_path, note, "budget reached (openai)", now=NOW)
    stalled = load(one(tmp_path, "output", "notes"))
    assert stalled.fm["stage"] == "deferred"
    assert stalled.fm["deferred_reason"] == "budget reached (openai)"
    assert stalled.fm["deferred_at"] == "2026-09-27T18:00:00+00:00"
    assert stalled.body == "An idea\n"


def test_an_unreadable_file_is_archived_and_failed_under_a_calculated_name_with_its_bytes_unchanged(tmp_path):
    bad = "---\ntitle: [oops\n---\nbody\n"
    src = put(tmp_path, "inbox/clippings/vid.md", bad)
    touched = move_to_failed(tmp_path, src, "cannot read", doc_id="x", doc_class="youtube", now=NOW)
    archived, failed = one(tmp_path, "archive", "clippings"), one(tmp_path, "failed", "clippings")
    assert (
        archived.name == failed.name
        and failed.name.startswith("20260927-")
        and failed.name.endswith("-vid.md")
    )
    assert archived.read_text() == failed.read_text() == bad and not src.exists()
    error = failed.with_suffix(".error.txt").read_text()
    assert "original file: vid.md" in error and f"calculated file: clippings/{failed.name}" in error
    assert "reason: cannot read" in error and src in touched and failed in touched


def test_move_to_failed_from_output_does_not_archive_the_working_copy_again(tmp_path):
    put(tmp_path, "inbox/notes/idea.md", "An idea\n")
    [note] = scan_inbox(tmp_path, now=NOW).notes
    start_work(tmp_path, note, now=NOW)
    move_to_failed(tmp_path, note.output_path(tmp_path), "invalid output", now=NOW)
    failed = one(tmp_path, "failed", "notes")
    assert failed.name == note.name and not list((tmp_path / "output/notes").glob("*.md"))
    assert "invalid output" in failed.with_suffix(".error.txt").read_text()
    assert (
        "stage" not in load(one(tmp_path, "archive", "notes")).fm
    )  # the archive copy is not the working copy


def test_a_file_moved_back_from_failed_is_a_new_capture(tmp_path):
    src = put(tmp_path, "inbox/notes/retry.md", "---\ntitle: [oops\n---\nx\n")
    move_to_failed(tmp_path, src, "cannot read", now=NOW)
    failed = one(tmp_path, "failed", "notes")
    (tmp_path / "inbox/notes").mkdir(parents=True, exist_ok=True)
    failed.rename(tmp_path / "inbox/notes/retry.md")
    assert list(scan_inbox(tmp_path, now=NOW).errors) == ["inbox/notes/retry.md"]


def test_move_to_duplicates_archives_moves_and_records_the_winner(tmp_path):
    put(tmp_path, "inbox/clippings/long.md", gemini_clip("2446cd9c762c9cc9", "long\n" * 5))
    put(tmp_path, "inbox/clippings/short.md", gemini_clip("2446cd9c762c9cc9", "short\n"))
    notes = {n.path.name: n for n in scan_inbox(tmp_path, now=NOW).notes}
    touched = move_to_duplicates(tmp_path, notes["short.md"], notes["long.md"])
    assert not (tmp_path / "inbox/clippings/short.md").exists()
    moved = one(tmp_path, "duplicates", "clippings")
    assert moved.name == notes["short.md"].name == one(tmp_path, "archive", "clippings").name
    fm = load(moved).fm
    assert fm["duplicate_of"] == "clippings/long.md" and fm["original_filename"] == "short.md"
    assert load(moved).body == "short\n" and moved in touched
    assert (tmp_path / "inbox/clippings/long.md").exists()


def chat_turns(n: int, last: str = "the last answer") -> str:
    turns = [f"**You**\n\nquestion {i}\n\n---\n\n**Gemini**\n\nanswer {i}\n" for i in range(1, n)]
    return "\n".join([*turns, f"**You**\n\nquestion {n}\n\n---\n\n**Gemini**\n\n{last}\n"])


def test_an_earlier_snapshot_is_recognised_even_with_a_cut_off_last_message():
    assert is_snapshot_of(chat_turns(3, "the last ans"), chat_turns(6))
    assert is_snapshot_of(chat_turns(3), chat_turns(6))


def test_a_different_or_longer_or_equal_clip_is_not_a_snapshot():
    assert not is_snapshot_of(chat_turns(6), chat_turns(3))
    assert not is_snapshot_of(chat_turns(4), chat_turns(4))
    other = chat_turns(3).replace("question 2", "a different question")
    assert not is_snapshot_of(other, chat_turns(6))


def test_text_without_messages_is_a_snapshot_only_when_it_is_an_exact_prefix():
    assert is_snapshot_of("one idea\n", "one idea\nand a second\n")
    assert not is_snapshot_of("one idea\n", "another idea\nand a second\n")


def test_scan_command_lists_the_inbox_and_changes_nothing(tmp_path):
    put(tmp_path, "inbox/notes/idea.md", "An idea\n")
    put(tmp_path, "inbox/clippings/short.md", gemini_clip("2446cd9c762c9cc9", chat_turns(3)))
    put(tmp_path, "inbox/clippings/long.md", gemini_clip("2446cd9c762c9cc9", chat_turns(6)))
    before = files(tmp_path)
    result = CliRunner().invoke(app, ["scan", "--ideas", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "would process" in result.output
    assert "duplicate of clippings/long.md" in result.output
    assert files(tmp_path) == before


def test_scan_command_warns_about_an_unknown_name(tmp_path):
    put(tmp_path, "inbox/notes/idea.md", "An idea\n")
    result = CliRunner().invoke(app, ["scan", "--ideas", str(tmp_path), "--file", "Nope"])
    assert result.exit_code == 1
    assert 'not-found      no document named "Nope" in inbox/' in result.output


def test_files_that_are_not_markdown_are_artifacts_and_junk_is_ignored(tmp_path):
    put(tmp_path, "inbox/notes/idea.md", "An idea\n")
    (tmp_path / "inbox/report.pdf").write_bytes(b"%PDF-1.7 fake")
    (tmp_path / "inbox/photo.PNG").write_bytes(b"\x89PNG")
    (tmp_path / "inbox/.DS_Store").write_bytes(b"junk")
    (tmp_path / "inbox/Thumbs.db").write_bytes(b"junk")
    put(tmp_path, "inbox/.trash/old.pdf", "gone")
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    result = scan_inbox(tmp_path, now=NOW)
    assert [n.path.name for n in result.notes] == ["idea.md"]
    assert sorted(a.path.name for a in result.artifacts) == ["photo.PNG", "report.pdf"]
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before  # writes nothing


def test_an_artifact_name_is_date_guid_and_the_original_name(tmp_path):
    name = artifact_name("My Report (final).pdf", NOW, lambda n: False)
    assert re.fullmatch(r"20260927-[0-9a-f]{6}-My Report \(final\)\.pdf", name)


def test_an_artifact_name_replaces_illegal_characters_and_is_cut_to_128_keeping_the_extension():
    name = artifact_name('a/b:c*"d.pdf', NOW, lambda n: False)
    assert name.endswith("-a_b_c__d.pdf")
    long = artifact_name("x" * 300 + ".pdf", NOW, lambda n: False)
    assert len(long) == 128 and long.endswith(".pdf") and long.startswith("20260927-")


def test_a_name_that_already_has_date_and_guid_is_kept():
    assert artifact_name("20260925-a1b2c3-report.pdf", NOW, lambda n: False) == "20260925-a1b2c3-report.pdf"
    assert artifact_name("2026925-a1b2c3-report.pdf", NOW, lambda n: False) != "2026925-a1b2c3-report.pdf"
    assert artifact_name("20260925-ZZZZZZ-report.pdf", NOW, lambda n: False) != "20260925-ZZZZZZ-report.pdf"


def test_an_artifact_name_is_never_reused():
    seen = []

    def taken(name: str) -> bool:
        seen.append(name)
        return len(seen) < 3

    assert artifact_name("a.pdf", NOW, taken) == seen[-1] and len(set(seen)) == 3


def test_copy_artifact_archives_publishes_and_removes_it_from_the_inbox(tmp_path):
    ideas, docs = tmp_path / "ideas", tmp_path / "docs"
    src = ideas / "inbox/report.pdf"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"%PDF-1.7 \x00\x01 binary")
    artifact = Artifact(src)
    ideas_paths, docs_paths = copy_artifact(ideas, docs, artifact, now=NOW)
    name = artifact.name
    assert name and name.startswith("20260927-") and name.endswith("-report.pdf")
    assert (ideas / "archive/artifacts" / name).read_bytes() == b"%PDF-1.7 \x00\x01 binary"
    assert (docs / "idea-bucket/artifacts" / name).read_bytes() == b"%PDF-1.7 \x00\x01 binary"
    assert not src.exists()
    assert ideas / "archive/artifacts" / name in ideas_paths and src in ideas_paths
    assert docs_paths == [docs / "idea-bucket/artifacts" / name]


def test_a_requeued_artifact_keeps_its_name_and_overwrites_the_same_files(tmp_path):
    ideas, docs = tmp_path / "ideas", tmp_path / "docs"
    (ideas / "archive/artifacts").mkdir(parents=True)
    (docs / "idea-bucket/artifacts").mkdir(parents=True)
    name = "20260925-a1b2c3-report.pdf"
    (ideas / "archive/artifacts" / name).write_bytes(b"old")
    (docs / "idea-bucket/artifacts" / name).write_bytes(b"old")
    src = ideas / "inbox" / name
    src.parent.mkdir(parents=True)
    src.write_bytes(b"new")
    copy_artifact(ideas, docs, Artifact(src), now=NOW)
    assert sorted(p.name for p in (ideas / "archive/artifacts").iterdir()) == [name]
    assert (ideas / "archive/artifacts" / name).read_bytes() == b"new"
    assert (docs / "idea-bucket/artifacts" / name).read_bytes() == b"new"


def test_file_finds_an_artifact_by_its_original_name_too(tmp_path):
    (tmp_path / "inbox").mkdir()
    (tmp_path / "inbox/20260925-a1b2c3-report.pdf").write_bytes(b"x")
    (tmp_path / "inbox/other.pdf").write_bytes(b"x")
    for query in ("report.pdf", "report", "20260925-a1b2c3-report.pdf"):
        assert [a.path.name for a in scan_inbox(tmp_path, now=NOW, only=[query]).artifacts] == [
            "20260925-a1b2c3-report.pdf"
        ], query


# ---- names read from a file are not trusted; a repo path may contain "inbox"; undoing start_work ---------


def test_a_name_with_a_path_in_it_is_ignored_and_never_reaches_the_file_system(tmp_path, caplog):
    evil = '---\ncalculated_filename: "../../outside.md"\noriginal_filename: "../x/y.md"\n---\nbody\n'
    put(tmp_path, "inbox/notes/evil.md", evil)
    [note] = scan_inbox(tmp_path, now=NOW).notes
    assert note.name is None and note.original is None  # both ignored
    assert "not one of our names" in caplog.text
    start_work(tmp_path, note, now=NOW)
    assert note.name and "/" not in note.name
    assert not (tmp_path.parent / "outside.md").exists()
    assert (tmp_path / "output/notes" / note.name).exists()


def test_a_name_we_wrote_ourselves_is_kept(tmp_path):
    put(tmp_path, "inbox/notes/a.md", '---\ncalculated_filename: "20260927-a1b2c3-my-idea.md"\n---\nbody\n')
    [note] = scan_inbox(tmp_path, now=NOW).notes
    assert note.name == "20260927-a1b2c3-my-idea.md"


def test_the_path_under_inbox_is_right_when_the_repo_path_also_holds_the_word_inbox(tmp_path):
    repo = tmp_path / "inbox" / "idea-bucket"
    (repo / ".git").mkdir(parents=True)
    put(repo, "inbox/notes/a.md", "an idea\n")
    [note] = scan_inbox(repo, now=NOW).notes
    assert note.rel == Path("notes/a.md")
    assert read_note(repo / "inbox/notes/a.md", NOW).rel == Path("notes/a.md")  # also when read from anywhere
    start_work(repo, note, now=NOW)
    assert (repo / "archive/notes" / note.name).exists() and not (tmp_path / "archive").exists()


def test_return_to_inbox_undoes_start_work_and_keeps_the_name(tmp_path):
    original = "---\nsource: x\n---\nan idea\n"
    path = put(tmp_path, "inbox/notes/a.md", original)
    [note] = scan_inbox(tmp_path, now=NOW).notes
    start_work(tmp_path, note, now=NOW)
    assert not path.exists()
    return_to_inbox(tmp_path, note)
    assert path.exists() and not list((tmp_path / "archive").rglob("*.md"))
    assert not list((tmp_path / "output").rglob("*.md"))  # in one place only
    [again] = scan_inbox(tmp_path, now=NOW).notes
    assert again.name == note.name  # the same calculated name, so nothing is archived twice


def test_return_to_inbox_of_a_document_that_never_started_changes_nothing(tmp_path):
    path = put(tmp_path, "inbox/notes/a.md", "an idea\n")
    [note] = scan_inbox(tmp_path, now=NOW).notes
    assert return_to_inbox(tmp_path, note) == [] and path.read_text() == "an idea\n"


def test_a_write_is_never_half_done_no_temp_files_are_left(tmp_path):
    put(tmp_path, "inbox/notes/a.md", "an idea\n")
    [note] = scan_inbox(tmp_path, now=NOW).notes
    start_work(tmp_path, note, now=NOW)
    mark_deferred(tmp_path, note, "limit", now=NOW)
    assert not [p for p in tmp_path.rglob("*") if p.name.endswith(".tmp")]


def test_deferred_in_output_lists_only_the_stalled_documents(tmp_path):
    put(tmp_path, "inbox/notes/a.md", "one\n")
    put(tmp_path, "inbox/notes/b.md", "two\n")
    a, b = scan_inbox(tmp_path, now=NOW).notes
    start_work(tmp_path, a, now=NOW)
    start_work(tmp_path, b, now=NOW)
    mark_deferred(tmp_path, a, "limit", now=NOW)
    assert deferred_in_output(tmp_path) == [f"notes/{a.name}"]


def _started(tmp_path):
    put(tmp_path, "inbox/notes/Walk.md", "Create YouTube content walking around\n")
    [note] = scan_inbox(tmp_path, now=NOW).notes
    start_work(tmp_path, note, now=NOW)
    return note


def test_a_note_loaded_from_output_keeps_its_subfolder(tmp_path):
    started = _started(tmp_path)
    out = started.output_path(tmp_path)
    note = load_staged_note(tmp_path, out)
    assert note.rel == Path("notes") / started.name
    assert note.path == out
    assert note.output_path(tmp_path) == out


def test_a_note_loaded_from_archive_keeps_its_subfolder(tmp_path):
    started = _started(tmp_path)
    archived = tmp_path / "archive" / "notes" / started.name
    note = load_staged_note(tmp_path, archived)
    assert note.rel == Path("notes") / started.name
    assert note.output_path(tmp_path) == started.output_path(tmp_path)


def test_a_note_loaded_from_failed_keeps_its_subfolder(tmp_path):
    started = _started(tmp_path)
    move_to_failed(tmp_path, started.output_path(tmp_path), "boom", now=NOW)
    failed = tmp_path / "failed" / "notes" / started.name
    note = load_staged_note(tmp_path, failed)
    assert note.rel == Path("notes") / started.name
    assert note.output_path(tmp_path) == started.output_path(tmp_path)


def test_a_note_loaded_from_duplicates_keeps_its_subfolder(tmp_path):
    started = _started(tmp_path)
    dup = put(tmp_path, f"duplicates/clippings/{started.name}", started.output_path(tmp_path).read_text())
    note = load_staged_note(tmp_path, dup)
    assert note.rel == Path("clippings") / started.name


def test_a_path_outside_the_five_folders_is_refused(tmp_path):
    other = put(tmp_path, "elsewhere/notes/x.md", "text\n")
    with pytest.raises(ValueError):
        load_staged_note(tmp_path, other)
    outside = put(tmp_path.parent, f"{tmp_path.name}-outside.md", "text\n")
    with pytest.raises(ValueError):
        load_staged_note(tmp_path, outside)


def test_the_repo_root_and_a_stage_folder_itself_are_refused(tmp_path):
    (tmp_path / "output").mkdir()
    outside = put(tmp_path.parent, f"{tmp_path.name}-outside2.md", "text\n")
    for path in (tmp_path, tmp_path / "output", outside):
        with pytest.raises(ValueError):
            load_staged_note(tmp_path, path)
