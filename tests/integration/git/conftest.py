import os
import subprocess
from pathlib import Path

import pytest


def _sh(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    for key, value in {
        "GIT_AUTHOR_NAME": "Tester",
        "GIT_AUTHOR_EMAIL": "tester@example.com",
        "GIT_COMMITTER_NAME": "Tester",
        "GIT_COMMITTER_EMAIL": "tester@example.com",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def sh():
    return _sh


@pytest.fixture
def make_repo(tmp_path):
    def _make(name: str, files: dict[str, str]) -> tuple[Path, Path]:
        bare = tmp_path / f"{name}.git"
        _sh(tmp_path, "init", "--bare", "-b", "main", str(bare))
        work = tmp_path / name
        _sh(tmp_path, "clone", str(bare), str(work))
        _sh(work, "checkout", "-B", "main")
        for rel, text in {"README.md": "# test\n", **files}.items():
            path = work / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        _sh(work, "add", "-A")
        _sh(work, "commit", "-m", "seed")
        _sh(work, "push", "-u", "origin", "main")
        return bare, work

    return _make
