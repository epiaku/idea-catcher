"""The `backfill.release` handler: release the oldest pending videos of any source (no live calls)."""

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from catcher.core.db import session_scope
from catcher.modules.backfill import store
from catcher.modules.queue.models import BackfillVideo, Job
from catcher.modules.worker.app import JOB_RESOURCES, build_handlers, check_job
from catcher.modules.worker.handlers import Done, HandlerResult
from catcher.modules.worker.handlers_backfill import handle_backfill_release, parse_release_params

pytestmark = pytest.mark.db

A, B, C = (f"{c * 10}1" for c in "abc")


@pytest.mark.parametrize("params", [{}, {"limit": 5}])
def test_release_params_accept(params) -> None:
    parse_release_params(params)
    check_job("backfill.release", params)


@pytest.mark.parametrize(
    "params", [{"limit": 0}, {"limit": -1}, {"limit": "5"}, {"limit": True}, {"limit": 1.5}, {"x": 1}]
)
def test_release_params_reject(params) -> None:
    with pytest.raises(ValueError):
        parse_release_params(params)
    with pytest.raises(ValueError):
        check_job("backfill.release", params)


def test_registration_and_no_youtube_resource() -> None:
    assert build_handlers()["backfill.release"] is handle_backfill_release
    assert "backfill.release" not in JOB_RESOURCES
    assert parse_release_params({}).limit is None


def add(harness, entries: dict[str, tuple[str, str]], offset: int = 0) -> None:
    with session_scope(harness.ctx.engine) as session:
        base = harness.ctx.clock() - timedelta(days=1)
        for i, (vid, source) in enumerate(entries.items()):
            store.add_pending(session, {vid: source}, base + timedelta(seconds=offset + i))


def run_job(harness, **params: Any) -> HandlerResult:
    job_id = harness.add_job("backfill.release", **params)
    with session_scope(harness.ctx.engine) as session:
        job = session.get(Job, job_id)
        assert job is not None
        return handle_backfill_release(harness.ctx, job)


def statuses(harness) -> dict[str, str]:
    with session_scope(harness.ctx.engine) as session:
        return {r.video_id: r.status for r in session.scalars(select(BackfillVideo))}


def test_releases_the_oldest_pending_of_any_source(harness) -> None:
    add(harness, {A: ("docs", "a.md"), B: ("channel", "https://www.youtube.com/@small")})

    result = run_job(harness, limit=5)

    assert isinstance(result, Done)
    assert result.result["released"] == [A, B]
    assert statuses(harness) == {A: "released", B: "released"}
    assert (harness.ideas / "inbox" / "clippings" / f"youtube source - {A}.md").exists()


def test_limit_is_the_cap(harness) -> None:
    add(harness, {A: ("docs", "a"), B: ("docs", "b"), C: ("docs", "c")})

    result = run_job(harness, limit=1)

    assert isinstance(result, Done)
    assert result.result["released"] == [A]
    assert result.result["allowance"] == 1
    assert result.result["still_pending"] == 2


def test_default_cap_is_the_worker_setting(harness) -> None:
    harness.ctx.settings = harness.ctx.settings.model_copy(update={"backfill_daily_limit": 2})
    add(harness, {A: ("docs", "a"), B: ("docs", "b"), C: ("docs", "c")})

    result = run_job(harness)

    assert isinstance(result, Done)
    assert result.result["released"] == [A, B]
    assert result.result["still_pending"] == 1


def test_cap_used_up_releases_nothing_and_succeeds(harness) -> None:
    add(harness, {A: ("docs", "a"), B: ("docs", "b")})
    assert run_job(harness, limit=1).result["released"] == [A]  # type: ignore[union-attr]

    result = run_job(harness, limit=1)

    assert isinstance(result, Done)
    assert result.result["released"] == [] and result.result["allowance"] == 0
    assert result.result["still_pending"] == 1


def test_backfill_daily_limit_zero_releases_nothing(harness, monkeypatch) -> None:
    harness.ctx.settings = harness.ctx.settings.model_copy(update={"backfill_daily_limit": 0})
    add(harness, {A: ("docs", "a")})
    from catcher.modules.worker import handlers_backfill

    def boom(*args, **kwargs):
        raise AssertionError("release_within must not be called")

    monkeypatch.setattr(handlers_backfill, "release_within", boom)

    result = run_job(harness)

    assert isinstance(result, Done)
    assert result.result["released"] == [] and result.result["allowance"] == 0
    assert statuses(harness) == {A: "pending"}


def test_result_keys(harness) -> None:
    add(harness, {A: ("docs", "a")})

    result = run_job(harness)

    assert isinstance(result, Done)
    assert set(result.result) == {"released", "repaired", "still_pending", "allowance"}
