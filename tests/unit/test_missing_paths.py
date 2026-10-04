"""A wrong path (a typo, an unset variable) is logged as an error and ends the command cleanly:
no traceback, no crash, a non-zero exit code, and nothing is touched."""

import logging
from pathlib import Path

import pytest
from typer.testing import CliRunner

from catcher.cli import app
from catcher.modules.pipeline.run import RunOptions, run_pipeline

MISSING = "no/such/folder/nothing-here.md"


def errors(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.parametrize("command", ["reason", "render"])
def test_a_document_that_does_not_exist_is_logged_and_ends_with_exit_2(command, tmp_path, caplog):
    missing = tmp_path / MISSING
    result = CliRunner().invoke(app, [command, str(missing)])
    assert result.exit_code == 2
    assert f"no such file: {missing}" in errors(caplog)
    assert result.exception is None or isinstance(result.exception, SystemExit)  # not a crash
    assert "Traceback" not in result.output
    assert not missing.exists() and not missing.parent.exists()  # nothing was created


@pytest.mark.parametrize("command", ["reason", "render"])
def test_a_directory_instead_of_a_document_is_logged_too(command, tmp_path, caplog):
    result = CliRunner().invoke(app, [command, str(tmp_path)])
    assert result.exit_code == 2
    assert any("cannot read" in e for e in errors(caplog))


def test_run_pipeline_with_a_missing_ideas_folder_reports_a_problem_and_touches_nothing(
    tmp_path, make_services, caplog
):
    docs = tmp_path / "docs"
    docs.mkdir()
    report = run_pipeline(tmp_path / "nope", docs, RunOptions(), make_services())
    assert report.items == [] and report.committed == {}
    [problem] = report.problems
    assert "idea-bucket inbox/ not found" in problem and "IDEAS_REPO" in problem
    assert problem in errors(caplog)
    assert list(tmp_path.iterdir()) == [docs] and not list(docs.iterdir())


def test_run_pipeline_with_a_missing_docs_folder_reports_a_problem(tmp_path, make_services, caplog):
    (tmp_path / "ideas/inbox").mkdir(parents=True)
    report = run_pipeline(tmp_path / "ideas", tmp_path / "nope", RunOptions(), make_services())
    [problem] = report.problems
    assert "epiaku-docs not found" in problem and problem in errors(caplog)


def test_a_dry_run_does_not_need_the_docs_folder(tmp_path, make_services):
    (tmp_path / "ideas/inbox").mkdir(parents=True)
    report = run_pipeline(tmp_path / "ideas", tmp_path / "nope", RunOptions(dry_run=True), make_services())
    assert report.problems == []


def test_the_pipeline_command_with_wrong_paths_exits_2_and_says_which_path(tmp_path, caplog, no_run_lock):
    args = ["run", "pipeline", "--ideas", str(tmp_path / "nope"), "--docs", str(tmp_path / "also-nope")]
    result = CliRunner().invoke(app, [*args, "--profile", "fake"])
    assert result.exit_code == 2
    assert "idea-bucket inbox/ not found" in result.output
    assert any("nope" in e for e in errors(caplog))


def test_scan_with_a_missing_ideas_folder_is_logged_and_exits_2(tmp_path, caplog):
    result = CliRunner().invoke(app, ["scan", "--ideas", str(tmp_path / "nope")])
    assert result.exit_code == 2
    assert any("no inbox/ folder" in e for e in errors(caplog))
    assert not Path(tmp_path / "nope").exists()


def test_a_named_file_that_does_not_exist_is_a_warning_not_a_crash(tmp_path, make_services, caplog):
    (tmp_path / "ideas/inbox/notes").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    report = run_pipeline(
        tmp_path / "ideas",
        tmp_path / "docs",
        RunOptions(only=["not a real note.md"], requeue=["nor this one"]),
        make_services(),
    )
    assert report.not_found == ["not a real note.md"] and report.not_in_archive == ["nor this one"]
    assert report.items == []
