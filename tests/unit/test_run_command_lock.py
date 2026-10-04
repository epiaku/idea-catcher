"""What `catcher run pipeline` says when its database lock is lost or a database error stops the run (the
lock itself is a stand-in here; it is tested on a real database in tests/integration/db)."""

from sqlalchemy.exc import OperationalError
from typer.testing import CliRunner

import catcher.cli as cli
from catcher.modules.pipeline.run import ItemReport, RunReport


def _report() -> RunReport:
    return RunReport(
        items=[
            ItemReport("a7b2c9", "note", "published", page="20261004-a7b2c9-idea.md"),
            ItemReport("cf81e40b020519ef", "ai-chat", "deferred", message="budget reached (openai)"),
            ItemReport("b1", "note", "skipped", message="run limit reached"),
        ]
    )


def test_a_lost_lock_shows_what_was_done_and_that_it_is_not_committed(monkeypatch, no_run_lock):
    from catcher.modules.pipeline.run import RunLockLost

    def lose(ideas, docs, opts, svc):
        raise RunLockLost(_report()) from RuntimeError("the lock connection died")

    monkeypatch.setattr(cli, "run_pipeline", lose)
    monkeypatch.setattr(cli, "default_services", lambda settings: None)
    result = CliRunner().invoke(cli.app, ["run", "pipeline"])
    assert result.exit_code == 1, result.output
    lines = result.output.splitlines()
    assert any(line.startswith("published") and "20261004-a7b2c9-idea.md" in line for line in lines)
    assert any(line.startswith("deferred") and "budget reached" in line for line in lines)
    assert "2 document(s) were finished and are NOT committed" in result.output
    assert "the run lost its database lock" in result.output
    assert "summary:" not in result.output  # no commit line that could look like a normal end
    assert "Traceback" not in result.output


def test_a_database_error_during_the_run_does_not_say_nothing_was_done(monkeypatch, no_run_lock):
    def fail(ideas, docs, opts, svc):
        raise OperationalError("select 1", {}, Exception("server closed the connection unexpectedly"))

    monkeypatch.setattr(cli, "run_pipeline", fail)
    monkeypatch.setattr(cli, "default_services", lambda settings: None)
    result = CliRunner().invoke(cli.app, ["run", "pipeline"])
    assert result.exit_code == 1, result.output
    assert "a database error stopped the run" in result.output
    assert "documents may already have been moved" in result.output
    assert "nothing was done" not in result.output
    assert "Traceback" not in result.output
