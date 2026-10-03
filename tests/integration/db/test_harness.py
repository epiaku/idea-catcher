"""The worker test harness: real Git repos, a real Postgres queue, a fake model and YouTube, a fixed clock."""

from datetime import UTC

import pytest

from catcher.core.db import session_scope
from catcher.modules.llm.profiles import Profile
from catcher.modules.llm.service import BackendUnavailable
from catcher.modules.worker.handlers import Done, HandlerContext
from catcher.modules.worker.loop import Worker


def test_the_harness_builds_two_git_repos_and_an_idle_worker(harness, sh):
    for repo in (harness.ideas, harness.docs):
        assert (repo / ".git").is_dir()
        assert sh(repo, "status", "--porcelain") == ""
    inbox = sorted(
        p.relative_to(harness.ideas / "inbox").as_posix() for p in (harness.ideas / "inbox").rglob("*.md")
    )
    assert inbox == ["clippings/systeme.md", "clippings/yt.md", "notes/YouTube walks.md"]
    assert isinstance(harness.ctx, HandlerContext) and isinstance(harness.worker, Worker)
    assert harness.ctx.ideas == harness.ideas and harness.ctx.docs == harness.docs
    assert harness.ctx.clock().tzinfo is UTC
    assert harness.fetch_calls == []
    assert harness.worker.run_once() is None  # idle: no jobs

    with session_scope(harness.ctx.engine) as session:
        assert harness.jobs(session) == []

    # The fake YouTube counts its calls and sits behind a real YoutubeAccess with a gate in a tmp folder.
    youtube = harness.ctx.services.youtube
    assert youtube is not None
    facts = youtube.get("AAAAAAAAAAA", facts_dir=None)
    assert facts.video_id == "AAAAAAAAAAA"
    assert harness.fetch_calls == ["AAAAAAAAAAA"]
    assert harness.state_dir in youtube.gate.state_file.parents  # never the real state folder

    # The fake backends: the notes profile gets the note backend, the openai ones the chat backend.
    assert harness.ctx.services.backends(Profile(backend="fake")) is harness.backends.note
    assert harness.ctx.services.backends(Profile(backend="openai", model="m")) is harness.backends.chat


def test_drain_returns_no_labels_when_there_are_no_jobs(harness, monkeypatch):
    assert harness.drain() == []

    ran: list[dict] = []
    monkeypatch.setitem(
        harness.worker.handlers, "test.echo", lambda ctx, job: ran.append(job.params) or Done()
    )
    job_id = harness.add_job("test.echo", answer=42)
    assert harness.drain() == ["succeeded"]
    assert ran == [{"answer": 42}]
    with session_scope(harness.ctx.engine) as session:
        [job] = harness.jobs(session)
        assert (job.id, job.type, job.status) == (job_id, "test.echo", "succeeded")


def test_the_frozen_harness_starts_with_the_committed_inbox_facts_and_replies(frozen_harness, sh):
    ideas = frozen_harness.ideas
    assert len([p for p in (ideas / "inbox").rglob("*") if p.is_file()]) == 44
    assert len(list((ideas / "facts").glob("*.json"))) == 2
    assert len([p for p in (ideas / "llm").rglob("*") if p.is_file()]) == 43
    assert sh(ideas, "status", "--porcelain") == "" and sh(frozen_harness.docs, "status", "--porcelain") == ""
    assert frozen_harness.model_calls == [] and frozen_harness.fetch_calls == []

    backend = frozen_harness.ctx.services.backends(Profile(backend="openai", model="m"))
    with pytest.raises(BackendUnavailable):
        backend.complete("prompt", model="m", task="note")
    assert frozen_harness.model_calls == ["note"]

    youtube = frozen_harness.ctx.services.youtube
    assert youtube is not None
    with pytest.raises(AssertionError, match="must not call YouTube"):
        youtube.get("BBBBBBBBBBB", facts_dir=None)
    assert frozen_harness.fetch_calls == ["BBBBBBBBBBB"]
    assert frozen_harness.drain() == []
