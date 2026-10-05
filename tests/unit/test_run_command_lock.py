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


def test_a_lost_lock_says_who_commits_the_leftovers(monkeypatch, no_run_lock):
    """The next `run pipeline` commits only the files it changes itself: the documents the lost run finished
    are committed by the next `pipeline.publish` job (or by hand), and the message must say so."""
    from catcher.modules.pipeline.run import RunLockLost

    def lose(ideas, docs, opts, svc):
        raise RunLockLost(_report())

    monkeypatch.setattr(cli, "run_pipeline", lose)
    monkeypatch.setattr(cli, "default_services", lambda settings: None)
    result = CliRunner().invoke(cli.app, ["run", "pipeline"])
    assert result.exit_code == 1, result.output
    said = " ".join(result.output.split())
    assert "run it again" not in said
    assert "the next `run pipeline` does not commit them" in said
    assert "`catcher jobs add pipeline.publish`" in said
    assert "then run the worker" in said
    assert "or commit them by hand" in said


def test_the_lost_lock_count_counts_documents_not_report_lines(monkeypatch, no_run_lock):
    from catcher.modules.pipeline.run import RunLockLost

    report = RunReport(
        items=[
            ItemReport("a1", "note", "requeued", "archive/notes/a.md -> inbox/"),
            ItemReport("a1", "note", "published", page="20261004-a1-a.md"),  # the same document: once
            ItemReport("b2", "note", "failed", "could not start work: disk full"),  # back in inbox/: no
            ItemReport("c3", "ai-chat", "deferred", "budget reached (openai)"),
            ItemReport("d4", "note", "waiting", "next YouTube call at 20:41 (stays in inbox/)"),  # untouched
            ItemReport("photo.png", "artifact", "artifact", page="20261004-photo.png"),
            ItemReport("big.mov", "artifact", "skipped", "over the limit"),  # stays in inbox/
        ],
        unreadable={"notes/broken.md": "bad frontmatter"},  # moved to failed/: changed, not committed
    )

    def lose(ideas, docs, opts, svc):
        raise RunLockLost(report)

    monkeypatch.setattr(cli, "run_pipeline", lose)
    monkeypatch.setattr(cli, "default_services", lambda settings: None)
    result = CliRunner().invoke(cli.app, ["run", "pipeline"])
    assert result.exit_code == 1, result.output
    assert "3 document(s) and 1 artifact(s) were finished and are NOT committed" in result.output


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
