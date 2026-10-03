"""The `youtube.fetch` handler: the facts of one staged clip, then `llm.reason`.

A closed gate (the gap between two calls, or the breaker after a 429) defers the *job* to the gate's time with
no attempt counted, and the item keeps waiting. A video YouTube cannot give facts for is an *item* state: the
working copy is marked deferred and the job succeeds. Saved facts mean no second call to YouTube."""

from datetime import timedelta

import pytest
from sqlalchemy import select, update
from worker_harness import YT_CLIP

from catcher.core.db import session_scope
from catcher.core.frontmatter import load
from catcher.modules.queue.items import set_item_status
from catcher.modules.queue.models import Job, JobItem
from catcher.modules.worker import handlers_pipeline
from catcher.modules.worker.app import build_handlers
from catcher.modules.worker.handlers import Defer, Done, Fail
from catcher.modules.worker.handlers_pipeline import (
    DEFER_FALLBACK_S,
    handle_pipeline_run,
    handle_youtube_fetch,
)
from catcher.modules.youtube.facts import FactsDeferred, FactsUnavailable

VID = "nGVZS_wUDGM"
OTHER_VID = "AAAAAAAAAAA"


class HttpError(Exception):
    status = 429


def stage(harness, **params) -> None:
    """Run `pipeline.run` with `params` (its job then ends succeeded, so only the next jobs stay queued)."""
    job_id = harness.add_job("pipeline.run", **params)
    with session_scope(harness.ctx.engine) as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert isinstance(handle_pipeline_run(harness.ctx, job), Done)
        session.execute(update(Job).where(Job.id == job_id).values(status="succeeded"))


def item_of(harness, original: str) -> JobItem:
    with session_scope(harness.ctx.engine) as session:
        [item] = [i for i in session.scalars(select(JobItem)) if i.original_filename == original]
        return item


def jobs_of(harness, type: str) -> list[Job]:
    with session_scope(harness.ctx.engine) as session:
        return [j for j in harness.jobs(session) if j.type == type]


def staged_clip(harness, **params) -> tuple[str, Job]:
    """Stage the YouTube clip (no saved facts): the item waits for YouTube and its fetch job is queued."""
    stage(harness, only=["yt"], **params)
    item = item_of(harness, "yt.md")
    assert item.status == "waiting_youtube"
    [job] = jobs_of(harness, "youtube.fetch")
    return item.calculated_name, job


def test_the_handler_is_registered():
    assert build_handlers()["youtube.fetch"] is handle_youtube_fetch


def test_fetch_saves_the_facts_and_enqueues_llm_reason(harness):
    name, job = staged_clip(harness)

    assert handle_youtube_fetch(harness.ctx, job) == Done({"item": "waiting_llm"})

    assert harness.fetch_calls == [VID]
    assert (harness.ideas / "facts" / f"{VID}.json").is_file()
    assert item_of(harness, "yt.md").status == "waiting_llm"
    [reason] = jobs_of(harness, "llm.reason")
    assert (reason.status, reason.params, reason.dedupe_key) == (
        "queued",
        {"calculated_name": name},
        f"reason:{name}",
    )
    assert harness.backends.chat.prompts == []  # the model is the next job's business


def test_profile_and_refresh_llm_travel_into_the_llm_reason_job(harness):
    name, job = staged_clip(harness, profile="fake", refresh_llm=True)
    assert job.params == {"calculated_name": name, "profile": "fake", "refresh_llm": True}

    assert handle_youtube_fetch(harness.ctx, job) == Done({"item": "waiting_llm"})

    [reason] = jobs_of(harness, "llm.reason")
    assert reason.params == {"calculated_name": name, "profile": "fake", "refresh_llm": True}


def test_the_clip_flow_runs_through_the_worker(harness):
    harness.add_job("pipeline.run", only=["yt"])

    assert harness.drain(max_jobs=4) == ["succeeded", "succeeded", "succeeded"]

    assert item_of(harness, "yt.md").status == "published"
    assert harness.fetch_calls == [VID]
    assert len(harness.backends.chat.prompts) == 1


def test_youtube_is_called_with_no_database_session_open(harness):
    _, job = staged_clip(harness)
    checked_out: list[int] = []
    fake = harness.fetcher

    def watching(video_id):
        checked_out.append(harness.ctx.engine.pool.checkedout())
        return fake(video_id)

    harness.fetcher = watching
    assert handle_youtube_fetch(harness.ctx, job) == Done({"item": "waiting_llm"})
    assert checked_out == [0]


def test_a_closed_gate_defers_the_job_until_the_next_slot(harness):
    (harness.ideas / "inbox" / "clippings" / "yt2.md").write_text(
        YT_CLIP.replace(VID, OTHER_VID), encoding="utf-8"
    )
    fake = harness.fetcher

    def two_videos(video_id):
        facts = fake(video_id)
        return facts if video_id == VID else facts.model_copy(update={"title": "Another video"})

    harness.fetcher = two_videos
    harness.add_job("pipeline.run", only=["yt", "yt2"])
    slot = harness.clock() + timedelta(seconds=600)

    labels = harness.drain(max_jobs=6)

    assert sorted(labels) == ["deferred", "succeeded", "succeeded", "succeeded"]
    assert len(harness.fetch_calls) == 1  # the gate allowed one call
    [waiting] = [j for j in jobs_of(harness, "youtube.fetch") if j.status == "queued"]
    assert (waiting.run_after, waiting.attempts) == (slot, 0)
    assert "next call allowed at" in (waiting.reason or "")
    waiting_clip = "yt2.md" if harness.fetch_calls == [VID] else "yt.md"
    assert item_of(harness, waiting_clip).status == "waiting_youtube"

    harness.clock.advance(600)  # the slot has come
    assert harness.drain(max_jobs=3) == ["succeeded", "succeeded"]
    assert sorted(harness.fetch_calls) == sorted([VID, OTHER_VID])
    assert item_of(harness, "yt.md").status == item_of(harness, "yt2.md").status == "published"


def test_a_429_opens_the_breaker_and_defers_without_counting_an_attempt(harness):
    def blocked(video_id):
        raise HttpError("HTTP Error 429: Too Many Requests")

    harness.fetcher = blocked
    harness.add_job("pipeline.run", only=["yt"])

    assert harness.drain(max_jobs=3) == ["succeeded", "deferred"]

    [job] = jobs_of(harness, "youtube.fetch")
    assert (job.status, job.attempts) == ("queued", 0)
    assert job.run_after == harness.clock() + timedelta(hours=6)
    assert "blocked until" in (job.reason or "")
    assert item_of(harness, "yt.md").status == "waiting_youtube"
    wait = harness.gate.peek()
    assert wait is not None and wait.blocked

    harness.clock.advance(3600)  # an hour later the breaker is still open: the job is not even due
    assert harness.drain(max_jobs=1) == []
    assert harness.fetch_calls == [VID]


@pytest.mark.parametrize(
    ("fetcher", "reason"),
    [
        ("no transcript", f"no transcript available for {VID}"),
        (FactsUnavailable(f"yt-dlp failed for {VID}: boom"), f"yt-dlp failed for {VID}: boom"),
        (FactsUnavailable("Private video"), "Private video"),
    ],
)
def test_no_transcript_defers_the_item_not_the_job(harness, yt_facts, fetcher, reason):
    def fetch(video_id):
        if isinstance(fetcher, Exception):
            raise fetcher
        return yt_facts.model_copy(update={"transcript": None})

    harness.fetcher = fetch
    harness.add_job("pipeline.run", only=["yt"])

    assert harness.drain(max_jobs=3) == ["succeeded", "succeeded"]

    [job] = jobs_of(harness, "youtube.fetch")
    assert (job.status, job.result) == ("succeeded", {"item": "deferred"})
    item = item_of(harness, "yt.md")
    assert item.status == "deferred" and reason in (item.error or "")
    out = load(harness.ideas / "output" / item.calculated_name).fm
    assert out["stage"] == "deferred" and reason in out["deferred_reason"]
    assert jobs_of(harness, "llm.reason") == []
    assert harness.backends.chat.prompts == []


def test_saved_facts_mean_no_second_call(harness):
    name, job = staged_clip(harness)
    assert handle_youtube_fetch(harness.ctx, job) == Done({"item": "waiting_llm"})
    with session_scope(harness.ctx.engine) as session:  # a crash before the commit: the item still waits
        set_item_status(session, name, "waiting_youtube", now=harness.clock())
    harness.clock.advance(3600)  # long after the gap: a call would be allowed

    assert handle_youtube_fetch(harness.ctx, job) == Done({"item": "waiting_llm"})
    assert harness.fetch_calls == [VID]
    assert len(jobs_of(harness, "llm.reason")) == 1  # the dedupe key: still one

    assert harness.drain(max_jobs=3) == ["succeeded", "succeeded"]  # the fetch job (a no-op) and llm.reason
    stage(harness, requeue=["yt"])  # a requeue, then the whole flow again
    harness.drain(max_jobs=3)
    assert item_of(harness, "yt.md").status == "published"
    assert harness.fetch_calls == [VID]


def test_facts_deferred_without_a_time_defers_by_the_fallback(harness):
    def later(video_id):
        raise FactsDeferred("try again later")

    harness.fetcher = later
    _, job = staged_clip(harness)

    result = handle_youtube_fetch(harness.ctx, job)

    assert result == Defer(
        run_after=harness.clock() + timedelta(seconds=DEFER_FALLBACK_S), reason="try again later"
    )
    assert item_of(harness, "yt.md").status == "waiting_youtube"


def test_an_unexpected_error_fails_the_item_and_the_job(harness):
    def bug(video_id):
        raise RuntimeError("a bug")

    harness.fetcher = bug
    name, job = staged_clip(harness)

    result = handle_youtube_fetch(harness.ctx, job)

    assert isinstance(result, Fail) and "a bug" in result.error
    item = item_of(harness, "yt.md")
    assert item.status == "failed" and "a bug" in (item.error or "")
    assert (harness.ideas / "failed" / name).is_file() and not (harness.ideas / "output" / name).exists()
    assert jobs_of(harness, "llm.reason") == []


def test_a_file_error_fails_the_item_and_the_job(harness, monkeypatch):
    _, job = staged_clip(harness)

    def unreadable(*args, **kwargs):
        raise OSError("disk error")

    monkeypatch.setattr(handlers_pipeline, "load_staged_note", unreadable)
    result = handle_youtube_fetch(harness.ctx, job)

    assert isinstance(result, Fail) and "disk error" in result.error
    item = item_of(harness, "yt.md")
    assert item.status == "failed" and "disk error" in (item.error or "")
    assert harness.fetch_calls == []


def test_a_finished_fetch_is_not_redone(harness):
    name, job = staged_clip(harness)
    assert handle_youtube_fetch(harness.ctx, job) == Done({"item": "waiting_llm"})
    harness.clock.advance(3600)

    assert handle_youtube_fetch(harness.ctx, job) == Done({"item": "waiting_llm"})
    assert harness.fetch_calls == [VID]


def test_a_missing_working_copy_fails_the_item(harness):
    name, job = staged_clip(harness)
    (harness.ideas / "output" / name).unlink()

    assert handle_youtube_fetch(harness.ctx, job) == Done({"item": "failed"})
    item = item_of(harness, "yt.md")
    assert (item.status, item.error) == ("failed", f"no working copy in output/{name}")
    assert harness.fetch_calls == []


def test_bad_params_or_a_missing_item_fail_the_job(harness):
    for params in (
        {},
        {"calculated_name": "../x.md"},
        {"calculated_name": "clippings/x.md", "other": 1},
        {"calculated_name": "clippings/20261002-abcdef-nothing.md"},
    ):
        job_id = harness.add_job("youtube.fetch", **params)
        with session_scope(harness.ctx.engine) as session:
            job = session.get(Job, job_id)
        result = handle_youtube_fetch(harness.ctx, job)
        assert isinstance(result, Fail), params


@pytest.mark.parametrize(
    "name", ["../x.md", "clippings/../../x.md", "/etc/x.md", "clippings//x.md", "..\\x.md", ".git/x.md", ""]
)
def test_a_name_that_could_leave_the_ideas_folder_fails_the_job(harness, name):
    result = handle_youtube_fetch(harness.ctx, Job(type="youtube.fetch", params={"calculated_name": name}))
    assert isinstance(result, Fail) and "calculated_name" in result.error
    assert harness.fetch_calls == []


def test_a_clip_in_a_nested_folder_is_fetched_and_handed_to_llm_reason(harness, sh):
    nested = harness.ideas / "inbox/clippings/2026/yt.md"
    nested.parent.mkdir(parents=True)
    (harness.ideas / "inbox/clippings/yt.md").rename(nested)
    stage(harness, only=["clippings/2026/yt.md"])
    item = item_of(harness, "yt.md")
    assert item.status == "waiting_youtube" and item.calculated_name.startswith("clippings/2026/")

    assert harness.drain(max_jobs=3) == ["succeeded", "succeeded"]

    assert harness.fetch_calls == [VID]
    assert item_of(harness, "yt.md").status == "published"
