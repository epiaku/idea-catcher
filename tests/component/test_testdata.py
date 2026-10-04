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


def test_reset_fresh_llm_and_youtube_leaves_out_the_saved_replies_and_facts(tmp_path):
    """The idea-bucket copy has no `facts/` and no `llm/`, so a run calls the LLM and YouTube for real."""
    source = DEFAULT_SOURCE / "idea-bucket"
    assert (source / "facts").is_dir() and (source / "llm").is_dir()  # the committed data has them
    repos = reset_test_repos(tmp_path / "ic", fresh_llm_and_youtube=True)
    assert not (repos["idea-bucket"] / "facts").exists() and not (repos["idea-bucket"] / "llm").exists()
    left_out = {p for p in tree(source) if p.split("/")[0] in ("facts", "llm")}
    assert left_out  # something was left out
    assert tree(repos["idea-bucket"]) == tree(source) - left_out  # everything else arrives
    assert tree(repos["epiaku-docs"]) == tree(DEFAULT_SOURCE / "epiaku-docs")
    assert git(repos["idea-bucket"], "status", "--porcelain").strip() == ""


def test_reset_command_fresh_llm_and_youtube(tmp_path):
    target = tmp_path / "ic"
    result = CliRunner().invoke(
        app, ["testdata", "reset", "--target", str(target), "--fresh-llm-and-youtube"]
    )
    assert result.exit_code == 0, result.output
    assert "no saved LLM replies or YouTube facts" in result.output
    assert not (target / "idea-bucket" / "llm").exists()
    plain = CliRunner().invoke(app, ["testdata", "reset", "--target", str(target)])
    assert "no saved LLM replies" not in plain.output and (target / "idea-bucket" / "llm").is_dir()
