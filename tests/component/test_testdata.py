from pathlib import Path

import pytest
from typer.testing import CliRunner

from catcher.cli import app
from catcher.core.testdata import DEFAULT_SOURCE, MARKER, TestDataError, reset_test_repos


def git(repo: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def test_the_committed_test_data_holds_only_what_the_idea_catcher_needs():
    assert sorted(p.name for p in DEFAULT_SOURCE.iterdir()) == ["epiaku-docs", "idea-bucket"]
    assert [p.name for p in (DEFAULT_SOURCE / "idea-bucket").iterdir()] == ["inbox"]
    assert sorted(p.name for p in (DEFAULT_SOURCE / "epiaku-docs").iterdir()) == ["hugo", "idea-bucket"]
    assert (DEFAULT_SOURCE / "epiaku-docs/idea-bucket/artifacts").is_dir()  # where artifacts are sent
    assert (DEFAULT_SOURCE / "idea-bucket/inbox/notes").is_dir() and (
        DEFAULT_SOURCE / "idea-bucket/inbox/clippings"
    ).is_dir()
    assert (DEFAULT_SOURCE / "epiaku-docs/hugo/content/en/docs/idea-bucket/_index.md").is_file()
    assert (DEFAULT_SOURCE / "idea-bucket/inbox/sample-report.pdf").is_file()  # a file to try artifacts with


def test_reset_makes_two_git_repos_without_a_remote(tmp_path):
    repos = reset_test_repos(tmp_path / "ic")
    assert sorted(repos) == ["epiaku-docs", "idea-bucket"]
    for repo in repos.values():
        assert git(repo, "log", "--format=%s").strip() == "test data"
        assert git(repo, "remote").strip() == ""  # nothing can be pulled or pushed
        assert git(repo, "status", "--porcelain").strip() == ""
    assert (repos["idea-bucket"] / "inbox/sample-report.pdf").exists()
    assert (repos["epiaku-docs"] / "idea-bucket/artifacts").is_dir()


def test_reset_starts_fresh_every_time(tmp_path):
    target = tmp_path / "ic"
    repos = reset_test_repos(target)
    (repos["idea-bucket"] / "inbox/notes/leftover.md").write_text("a leftover from a test run\n")
    (repos["idea-bucket"] / "archive").mkdir()
    (repos["epiaku-docs"] / "junk.txt").write_text("x")
    again = reset_test_repos(target)
    assert not (again["idea-bucket"] / "inbox/notes/leftover.md").exists()
    assert not (again["idea-bucket"] / "archive").exists()
    assert not (again["epiaku-docs"] / "junk.txt").exists()
    assert git(again["idea-bucket"], "rev-list", "--count", "HEAD").strip() == "1"


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
