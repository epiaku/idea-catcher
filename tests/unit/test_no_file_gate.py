"""Postgres is the only gate: the Stage A file gate, its state folder and the `pipeline.lock` file are gone.

Review Focus 6: no file-held state is left in `src` (the gate is the `resources` row `youtube`, the run lock
is the worker's Postgres advisory lock)."""

import re
from pathlib import Path

from typer.testing import CliRunner

import catcher.modules.youtube.gate as gate_module
from catcher.cli import app
from catcher.core.config import Settings

SRC = Path(__file__).parents[2] / "src"


def test_the_file_gate_is_gone():
    assert not hasattr(gate_module, "YoutubeGate")
    assert "YoutubeGate" not in gate_module.__all__
    result = CliRunner().invoke(app, ["youtube", "gate", "--help"])
    assert result.exit_code == 0, result.output
    assert "--import-file" not in result.output


def test_no_file_state_is_left():
    words = re.compile(r"state_dir|youtube-gate|pipeline\.lock", re.IGNORECASE)
    for planted in ("CATCHER_STATE_DIR", "catcher_state_dir", "YouTube-Gate.json", "PIPELINE.LOCK"):
        assert words.search(planted), f"the search misses {planted!r}"  # it would find what it looks for
    found = [
        f"{path.relative_to(SRC)}:{number}: {line.strip()}"
        for path in sorted(SRC.rglob("*"))
        if path.is_file() and path.suffix in {".py", ".yaml", ".yml", ".toml", ".md", ".txt", ".ini"}
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if words.search(line)
    ]
    assert found == []


def test_an_old_catcher_state_dir_in_the_environment_is_ignored(monkeypatch, tmp_path):
    """A user's `.env` may still hold CATCHER_STATE_DIR: it is ignored, not an error."""
    monkeypatch.setenv("CATCHER_STATE_DIR", str(tmp_path / "state"))
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"CATCHER_STATE_DIR={tmp_path / 'old'}\n", encoding="utf-8")
    for settings in (Settings(), Settings(_env_file=dotenv)):  # type: ignore[call-arg]
        assert not hasattr(settings, "catcher_state_dir")
