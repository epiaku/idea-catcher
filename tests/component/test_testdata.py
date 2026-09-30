from pathlib import Path

import pytest
from typer.testing import CliRunner

from catcher.cli import app
from catcher.core.testdata import DEFAULT_SOURCE, MARKER, REPOS, TestDataError, reset_test_repos


def git(repo: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def tree(root: Path) -> set[str]:
    """Every file and folder under root (relative paths), so a test can compare what is there
    with what was copied, instead of hardcoding what the test data holds."""
    return {
        p.relative_to(root).as_posix() for p in root.rglob("*") if ".git" not in p.relative_to(root).parts
    }


def test_the_committed_test_data_has_a_folder_for_each_repo_with_something_in_it():
    for name in REPOS:
        assert any((DEFAULT_SOURCE / name).rglob("*")), f"tests/data/{name} is empty"


def test_reset_makes_two_git_repos_without_a_remote(tmp_path):
    repos = reset_test_repos(tmp_path / "ic")
    assert sorted(repos) == sorted(REPOS)
    for repo in repos.values():
        assert git(repo, "log", "--format=%s").strip() == "test data"
        assert git(repo, "remote").strip() == ""  # nothing can be pulled or pushed
        assert git(repo, "status", "--porcelain").strip() == ""


def test_reset_copies_the_whole_test_data_folder_for_folder_and_file_for_file(tmp_path):
    """Files, empty folders (kept by a .gitkeep) and non-markdown files (artifacts) all arrive."""
    repos = reset_test_repos(tmp_path / "ic")
    for name in REPOS:
        assert tree(repos[name]) == tree(DEFAULT_SOURCE / name)


def test_reset_starts_fresh_every_time(tmp_path):
    target = tmp_path / "ic"
    repos = reset_test_repos(target)
    first, second = REPOS
    (repos[first] / "leftover.md").write_text("a leftover from a test run\n")
    (repos[first] / "archive").mkdir()
    (repos[second] / "junk.txt").write_text("x")
    again = reset_test_repos(target)
    assert not (again[first] / "leftover.md").exists()
    assert not (again[first] / "archive").exists()
    assert not (again[second] / "junk.txt").exists()
    assert tree(again[first]) == tree(DEFAULT_SOURCE / first)
    assert git(again[first], "rev-list", "--count", "HEAD").strip() == "1"


def test_reset_does_not_delete_a_folder_it_did_not_make(tmp_path):
    target = tmp_path / "important"
    target.mkdir()
    (target / "my-work.txt").write_text("precious")
    with pytest.raises(TestDataError, match="not made by this command"):
        reset_test_repos(target)
    assert (target / "my-work.txt").read_text() == "precious"


def test_reset_needs_the_test_data(tmp_path):
    with pytest.raises(TestDataError, match="test data not found"):
        reset_test_repos(tmp_path / "ic", source=tmp_path / "nothing")
    assert MARKER  # the marker name is what makes a folder safe to recreate


def test_reset_command(tmp_path):
    result = CliRunner().invoke(app, ["testdata", "reset", "--target", str(tmp_path / "ic")])
    assert result.exit_code == 0, result.output
    assert "made" in result.output and "--profile fake" in result.output
    refused = CliRunner().invoke(app, ["testdata", "reset", "--target", str(tmp_path)])
    assert refused.exit_code == 2 and "not made by this command" in refused.output
