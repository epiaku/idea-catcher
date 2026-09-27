import re
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from catcher.cli import app
from catcher.core.frontmatter import load
from catcher.modules.pipeline.staging import facts_sidecar, load_staged, stage_inbox

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


def test_dictated_note_gets_an_id_and_leaves_the_inbox(tmp_path):
    src = put(tmp_path, "inbox/notes/YouTube walks.md", "Create YouTube content walking around\n")
    result = stage_inbox(tmp_path, now=NOW)
    [note] = result.staged
    assert note.doctype.name == "note"
    assert re.fullmatch(r"[0-9a-f]{6}", note.doc_id)
    staged_path = tmp_path / "staging" / f"{note.doc_id}.md"
    assert not src.exists()
    staged = load(staged_path)
    assert staged.body == "Create YouTube content walking around\n"
    assert staged.fm == {
        "id": note.doc_id,
        "class": "note",
        "captured": "2026-09-27",
        "source_file": "inbox/notes/YouTube walks.md",
        "staged_at": "2026-09-27T18:00:00+00:00",
    }
    assert set(result.touched) == {src, staged_path}
    assert result.errors == {}


def test_clip_body_is_kept_byte_for_byte(tmp_path):
    body = "**You**\n\nsysteme.io \\[paid\\] question\n\n---\n\n**Gemini**\n\nYes.\n"
    put(tmp_path, "inbox/clippings/systeme.io.md", gemini_clip("cf81e40b020519ef", body, "2026-09-25"))
    [note] = stage_inbox(tmp_path, now=NOW).staged
    staged = load(tmp_path / "staging" / "cf81e40b020519ef.md")
    assert note.doctype.name == "ai-chat"
    assert staged.body == body
    assert staged.fm["captured"] == "2026-09-25"
    assert staged.fm["tags"] == ["clippings"]


def test_repeated_clips_of_one_chat_are_staged_once(tmp_path):
    put(tmp_path, "inbox/clippings/New chat.md", gemini_clip("2446cd9c762c9cc9", "short\n"))
    put(
        tmp_path,
        "inbox/clippings/Idea catcher.md",
        gemini_clip("2446cd9c762c9cc9", "the longest version\n" * 5),
    )
    put(tmp_path, "inbox/clippings/obsidian github link.md", gemini_clip("2446cd9c762c9cc9", "medium\n" * 2))
    result = stage_inbox(tmp_path, now=NOW)
    assert [n.doc_id for n in result.staged] == ["2446cd9c762c9cc9"]
    assert load(tmp_path / "staging" / "2446cd9c762c9cc9.md").body == "the longest version\n" * 5
    superseded = sorted(p.name for p in (tmp_path / "archive/clippings/superseded").iterdir())
    assert superseded == [
        "2446cd9c762c9cc9--20260927180000-1.md",
        "2446cd9c762c9cc9--20260927180000-2.md",
    ]
    assert not list((tmp_path / "inbox").rglob("*.md"))


def test_new_inbox_copy_replaces_a_waiting_staged_copy(tmp_path):
    put(tmp_path, "staging/cf81e40b020519ef.md", gemini_clip("cf81e40b020519ef", "old and long\n" * 9))
    put(tmp_path, "inbox/clippings/again.md", gemini_clip("cf81e40b020519ef", "newer\n"))
    stage_inbox(tmp_path, now=NOW)
    assert load(tmp_path / "staging" / "cf81e40b020519ef.md").body == "newer\n"
    [old] = (tmp_path / "archive/clippings/superseded").iterdir()
    assert load(old).body == "old and long\n" * 9


def test_replay_from_the_archive_keeps_the_id(tmp_path):
    archived = "---\nid: a7b2c9\nclass: note\ncaptured: '2026-09-20'\n---\nAn old idea\n"
    put(tmp_path, "inbox/notes/a7b2c9.md", archived)
    [note] = stage_inbox(tmp_path, now=NOW).staged
    assert note.doc_id == "a7b2c9"
    assert note.doc.fm["captured"] == "2026-09-20"


def test_unreadable_capture_stays_in_the_inbox(tmp_path):
    bad = put(tmp_path, "inbox/notes/bad.md", "---\ntitle: [oops\n---\nbody\n")
    put(tmp_path, "inbox/notes/good.md", "A good idea\n")
    result = stage_inbox(tmp_path, now=NOW)
    assert bad.exists()
    assert list(result.errors) == ["inbox/notes/bad.md"]
    assert len(result.staged) == 1


def test_dry_run_changes_nothing(tmp_path):
    put(tmp_path, "inbox/notes/idea.md", "An idea\n")
    before = files(tmp_path)
    result = stage_inbox(tmp_path, dry_run=True, now=NOW)
    assert files(tmp_path) == before
    assert len(result.staged) == 1
    assert result.touched == []


def test_hidden_folders_are_ignored(tmp_path):
    put(tmp_path, "inbox/.trash/deleted.md", "gone\n")
    assert stage_inbox(tmp_path, now=NOW).staged == []


def test_load_staged_reads_back_what_was_staged(tmp_path):
    put(tmp_path, "inbox/notes/idea.md", "An idea\n")
    put(tmp_path, "inbox/clippings/chat.md", gemini_clip("cf81e40b020519ef", "q\n"))
    staged = stage_inbox(tmp_path, now=NOW).staged
    loaded = load_staged(tmp_path)
    assert sorted((n.doc_id, n.doctype.name) for n in loaded) == sorted(
        (n.doc_id, n.doctype.name) for n in staged
    )


def test_facts_sidecar_sits_next_to_the_note(tmp_path):
    assert facts_sidecar(tmp_path / "staging" / "abc.md") == tmp_path / "staging" / "abc.youtube.json"


def test_stage_command(tmp_path):
    put(tmp_path, "inbox/notes/idea.md", "An idea\n")
    result = CliRunner().invoke(app, ["stage", "--ideas", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "staged" in result.output
    assert len(list((tmp_path / "staging").glob("*.md"))) == 1
