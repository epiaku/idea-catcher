"""The `pipeline.preview` handler: the worker runs the read-only preview of `run pipeline --dry-run` for the
API (`dry_run: true`). It changes no file, no row and no gate, calls neither a model nor YouTube, and stores
the report (counts and bounded name lists) as its job result."""

import hashlib
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import Engine, func, select, text
from worker_harness import DOCS_SEED, IDEAS_SEED, ExternalCall, RaisingBackend, WorkerHarness

from catcher.core.db import session_scope
from catcher.modules.queue.models import Job, JobItem
from catcher.modules.worker.app import PARAM_CHECKS, build_handlers, check_job
from catcher.modules.worker.handlers import Done, Fail, HandlerResult
from catcher.modules.worker.handlers_pipeline import handle_pipeline_preview


def _harness(make_repo, worker_engine, make_services) -> WorkerHarness:
    """The seeded repos, with a model and a YouTube that record the attempt and raise."""
    _, ideas = make_repo("idea-bucket", IDEAS_SEED)
    _, docs = make_repo("epiaku-docs", DOCS_SEED)
    calls: list[str] = []

    def no_youtube(video_id: str):
        calls.append(video_id)
        raise ExternalCall(f"tests must not call YouTube ({video_id})")

    raising = RaisingBackend(calls)
    harness = WorkerHarness(
        ideas=ideas,
        docs=docs,
        engine=worker_engine,
        services=make_services(),
        fetcher=no_youtube,
        backends=SimpleNamespace(note=raising, chat=raising),
    )
    harness.external_calls = calls  # type: ignore[attr-defined]
    return harness


def _hash(*roots: Path) -> dict[str, str]:
    """Every file of the repos (the .git folders too) by content."""
    return {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for root in roots
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _state(engine: Engine) -> dict[str, object]:
    with session_scope(engine) as session:
        return {
            "items": session.scalar(select(func.count()).select_from(JobItem)),
            "jobs": sorted(session.execute(text("select id, type, status from jobs")).all()),
            "resources": sorted(session.execute(text("select * from resources")).all()),
        }


def _run(harness: WorkerHarness, **params) -> HandlerResult:
    job_id = harness.add_job("pipeline.preview", **params)
    with session_scope(harness.ctx.engine) as session:
        job = session.get(Job, job_id)
        assert job is not None
        return handle_pipeline_preview(harness.ctx, job)


def test_preview_job_returns_the_report_and_changes_nothing(make_repo, worker_engine, make_services):
    harness = _harness(make_repo, worker_engine, make_services)
    (harness.ideas / "inbox/clippings/pic.png").write_bytes(b"\x89PNG")
    (harness.ideas / "inbox/notes/broken.md").write_text("---\n: [bad\n---\nx\n", encoding="utf-8")
    harness.add_job("pipeline.preview")  # the job row exists before the snapshot
    files = _hash(harness.ideas, harness.docs)
    with session_scope(worker_engine) as session:
        job = session.scalars(select(Job)).one()
    before = _state(worker_engine)

    result = handle_pipeline_preview(harness.ctx, job)

    assert isinstance(result, Done)
    assert result.result is not None and set(result.result) == {"report"}
    report = result.result["report"]
    assert report["counts"] == {"would_call_llm": 2, "would_fetch": 1, "would_copy": 1}
    assert set(report["names"]) == {"items", "unreadable", "not_found", "not_in_archive"}
    assert list(report["names"]["unreadable"]) == ["inbox/notes/broken.md"]
    statuses = sorted(item["status"] for item in report["names"]["items"])
    assert statuses == ["would_call_llm", "would_call_llm", "would_copy", "would_fetch"]
    assert {"doc_id", "doc_class", "status", "message", "page"} <= set(report["names"]["items"][0])
    assert "truncated" not in report["names"]

    assert _hash(harness.ideas, harness.docs) == files  # no file written, moved or deleted
    assert _state(worker_engine) == before  # no item row, no job, no gate or block change
    assert harness.external_calls == []  # type: ignore[attr-defined]


def test_preview_job_bounds_its_name_lists(make_repo, worker_engine, make_services, monkeypatch):
    harness = _harness(make_repo, worker_engine, make_services)
    monkeypatch.setattr("catcher.modules.worker.handlers_pipeline.NAMES_MAX", 2)

    result = _run(harness)

    assert isinstance(result, Done) and result.result is not None
    report = result.result["report"]
    assert len(report["names"]["items"]) == 2 and report["names"]["truncated"] is True
    assert sum(report["counts"].values()) == 3  # the counts stay whole


def test_preview_job_refuses_bad_params(make_repo, worker_engine, make_services):
    harness = _harness(make_repo, worker_engine, make_services)
    files = _hash(harness.ideas, harness.docs)

    assert _run(harness, limit=-1) == Fail("limit must be a whole number of 0 or more, not -1")
    refused = _run(harness, bogus=1)
    assert isinstance(refused, Fail) and "unknown parameter(s) for pipeline.preview: bogus" in refused.error
    refused = _run(harness, requeue=["../etc/passwd"])
    assert isinstance(refused, Fail) and "not a document name inside the ideas folder" in refused.error
    assert _hash(harness.ideas, harness.docs) == files

    assert PARAM_CHECKS["pipeline.preview"] is not None
    check_job("pipeline.preview", {"limit": 2, "profile": "notes", "retry_deferred": True})


def test_preview_job_fails_when_the_inbox_is_missing(make_repo, worker_engine, make_services):
    harness = _harness(make_repo, worker_engine, make_services)
    harness.ctx.ideas = harness.ideas / "nowhere"

    result = _run(harness)

    assert isinstance(result, Fail) and "inbox/ not found" in result.error


def test_the_worker_runs_a_preview_job_end_to_end(make_repo, worker_engine, make_services):
    harness = _harness(make_repo, worker_engine, make_services)
    assert build_handlers()["pipeline.preview"] is handle_pipeline_preview
    job_id = harness.add_job("pipeline.preview", limit=1)
    files = _hash(harness.ideas, harness.docs)

    assert harness.drain(max_jobs=2) == ["succeeded"]

    with session_scope(worker_engine) as session:
        job = session.get(Job, job_id)
        assert job is not None and job.status == "succeeded"
        assert job.result is not None
        counts = job.result["report"]["counts"]
        assert counts.get("skipped") == 2 and sum(counts.values()) == 3  # the limit left two
        assert session.scalar(select(func.count()).select_from(JobItem)) == 0
        assert [j.type for j in session.scalars(select(Job))] == ["pipeline.preview"]  # queued nothing
    assert _hash(harness.ideas, harness.docs) == files
    assert harness.external_calls == []  # type: ignore[attr-defined]
