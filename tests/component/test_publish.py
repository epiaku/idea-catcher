from pathlib import Path

from typer.testing import CliRunner

from catcher.cli import app
from catcher.core.frontmatter import Doc, dump
from catcher.modules.pipeline.doctypes import NOTE, YOUTUBE
from catcher.modules.pipeline.publish import archive_staged, find_pages_by_id, write_page
from catcher.modules.pipeline.staging import facts_sidecar

REPO = Path(__file__).parents[2]


def put(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def page(doc_id: str | None, title: str = "T") -> str:
    fm = {"title": title} if doc_id is None else {"title": title, "id": doc_id}
    return dump(Doc(fm, "x\n"))


def test_write_page_replaces_the_page_with_the_same_id(tmp_path):
    out = tmp_path / NOTE.out_dir
    old = put(out / "20260927_a7b2c9_old-title.md", page("a7b2c9"))
    index = put(out / "_index.md", page("a7b2c9"))
    hand = put(out / "hand-written.md", page(None))
    broken = put(out / "broken.md", "---\ntitle: [\n---\n")
    touched = write_page(tmp_path, NOTE, "a7b2c9", "20260927_a7b2c9_new-title.md", "NEW")
    new = out / "20260927_a7b2c9_new-title.md"
    assert new.read_text() == "NEW"
    assert touched == [new, old]
    assert not old.exists()
    assert index.exists() and hand.exists() and broken.exists()


def test_same_filename_is_simply_overwritten(tmp_path):
    out = tmp_path / NOTE.out_dir
    same = put(out / "20260927_a7b2c9_x.md", page("a7b2c9"))
    assert write_page(tmp_path, NOTE, "a7b2c9", "20260927_a7b2c9_x.md", "NEW") == [same]
    assert same.read_text() == "NEW"


def test_ids_match_exactly_even_with_underscores(tmp_path):
    out = tmp_path / YOUTUBE.out_dir
    put(out / "20260927_ab_cd-efghi_x.md", page("ab_cd-efghi"))
    put(out / "20260927_ab_y.md", page("ab"))
    assert [p.name for p in find_pages_by_id(out, "ab")] == ["20260927_ab_y.md"]


def test_archive_moves_the_note_and_its_facts(tmp_path, make_note):
    note = make_note("youtube", doc_id="MBPHU7aaklM", root=tmp_path)
    sidecar = put(facts_sidecar(note.path), "{}")
    touched = archive_staged(tmp_path, note)
    dest = tmp_path / "archive/youtube/MBPHU7aaklM.md"
    assert dest.exists() and (tmp_path / "archive/youtube/MBPHU7aaklM.youtube.json").exists()
    assert not note.path.exists() and not sidecar.exists()
    assert touched == [note.path, dest, sidecar, tmp_path / "archive/youtube/MBPHU7aaklM.youtube.json"]


def test_archive_overwrites_an_earlier_archived_copy(tmp_path, make_note):
    put(tmp_path / "archive/notes/a7b2c9.md", "old")
    note = make_note("note", root=tmp_path)
    archive_staged(tmp_path, note)
    assert "An idea." in (tmp_path / "archive/notes/a7b2c9.md").read_text()


def test_render_command_writes_the_page(tmp_path, make_note, monkeypatch):
    monkeypatch.setenv("PROFILES_FILE", str(REPO / "profiles.yaml"))
    note = make_note("note", root=tmp_path / "ideas")
    docs = tmp_path / "docs"
    result = CliRunner().invoke(app, ["render", str(note.path), "--docs", str(docs), "--profile", "fake"])
    assert result.exit_code == 0, result.output
    assert (docs / NOTE.out_dir / "20260927_a7b2c9_fake-note.md").exists()
    assert note.path.exists()
