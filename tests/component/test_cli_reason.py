from pathlib import Path

from typer.testing import CliRunner

from catcher.cli import app
from catcher.core.frontmatter import Doc, dump
from catcher.modules.pipeline.inbox import read_note
from catcher.modules.pipeline.inputs import capture_tags, prompt_input, title_hint
from catcher.modules.pipeline.tags import load_tags

REPO = Path(__file__).parents[2]


def inbox_note(tmp_path: Path, fm: dict, body: str = "An idea\n") -> Path:
    path = tmp_path / "inbox/notes" / "YouTube walks.md"
    path.parent.mkdir(parents=True)
    path.write_text(dump(Doc(fm, body)))
    return path


BASE = {
    "id": "a7b2c9",
    "class": "note",
    "captured": "2026-09-27",
    "source_file": "inbox/notes/YouTube walks.md",
}


def test_prompt_input_for_a_note(tmp_path):
    note = read_note(inbox_note(tmp_path, {**BASE, "tags": ["clippings", "obsidian"]}))
    data = prompt_input(note, load_tags())
    assert data["title_hint"] == "YouTube walks"
    assert data["body"] == "An idea\n"
    assert data["source"] is None
    assert data["capture_tags"] == ["obsidian"]
    assert "app-idea" in data["tags"]["idea_types"]


def test_title_hint_prefers_the_clip_title(tmp_path):
    note = read_note(inbox_note(tmp_path, {**BASE, "title": "Idea Catcher"}))
    assert title_hint(note) == "Idea Catcher"
    assert capture_tags(note) == []


def test_reason_command_prints_validated_json(tmp_path, monkeypatch):
    monkeypatch.setenv("PROFILES_FILE", str(REPO / "profiles.yaml"))
    path = inbox_note(tmp_path, BASE)
    result = CliRunner().invoke(app, ["reason", str(path), "--profile", "fake"])
    assert result.exit_code == 0, result.output
    assert '"title": "Fake Note"' in result.output


def test_reason_command_refuses_youtube_notes(tmp_path, monkeypatch):
    monkeypatch.setenv("PROFILES_FILE", str(REPO / "profiles.yaml"))
    path = inbox_note(tmp_path, {**BASE, "id": "MBPHU7aaklM", "class": "youtube"})
    result = CliRunner().invoke(app, ["reason", str(path), "--profile", "fake"])
    assert result.exit_code == 2
    assert "catcher render" in result.output
