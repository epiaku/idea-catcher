"""A released backfill clip runs through the normal pipeline, at the low priority (real Postgres, fakes).

The note's frontmatter `backfill: true` makes the staging row `origin='backfill'`; every job queued for that
item (`youtube.fetch`, `llm.reason`, and a fetch queued again) gets `BACKFILL_PRIORITY`. A normal clip keeps
priority 0 and is claimed first; the YouTube gate holds a backfill fetch like any other."""

import pytest
from sqlalchemy import select, update

from catcher.core.db import session_scope
from catcher.core.frontmatter import load
from catcher.modules.backfill import store
from catcher.modules.backfill.release import release
from catcher.modules.pipeline.doctypes import YOUTUBE
from catcher.modules.queue.models import Job, JobItem
from catcher.modules.worker.handlers import Done
from catcher.modules.worker.handlers_pipeline import (
    handle_llm_reason,
    handle_pipeline_run,
    handle_youtube_fetch,
)

pytestmark = pytest.mark.db

VID = "nGVZS_wUDGM"  # the normal clip of the seed (inbox/clippings/yt.md)
BACK = "BBBBBBBBBB1"
CLIP = f"youtube source - {BACK}"


def _release(harness) -> None:
    now = harness.clock()
    with session_scope(harness.ctx.engine) as session:
        store.add_pending(session, {BACK: ("docs", "page.md")}, now)
        assert release(session, harness.ideas, 1, now).released == [BACK]


def _stage(harness, *only: str) -> None:
    job_id = harness.add_job("pipeline.run", only=list(only))
    with session_scope(harness.ctx.engine) as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert isinstance(handle_pipeline_run(harness.ctx, job), Done)
        session.execute(update(Job).where(Job.id == job_id).values(status="succeeded"))


def _item(harness, doc_id: str) -> JobItem:
    with session_scope(harness.ctx.engine) as session:
        [item] = session.scalars(select(JobItem).where(JobItem.doc_id == doc_id)).all()
        session.expunge(item)
        return item


def _jobs(harness, type: str, name: str) -> list[Job]:
    with session_scope(harness.ctx.engine) as session:
        jobs = [
            j for j in harness.jobs(session) if j.type == type and j.params.get("calculated_name") == name
        ]
        session.expunge_all()
        return jobs


def _run(harness, handler, job: Job):
    with session_scope(harness.ctx.engine) as session:
        fresh = session.get(Job, job.id)
        session.expunge(fresh)
    result = handler(harness.ctx, fresh)
    with session_scope(harness.ctx.engine) as session:
        session.execute(update(Job).where(Job.id == job.id).values(status="succeeded"))
    return result


def test_the_released_note_is_detected_as_a_youtube_clip_and_staged_with_origin_backfill(harness) -> None:
    _release(harness)

    _stage(harness, CLIP)

    item = _item(harness, BACK)
    assert (item.doc_class, item.origin, item.status) == (YOUTUBE.name, "backfill", "waiting_youtube")
    assert item.inbox_path == f"inbox/clippings/{CLIP}.md"
    archived = load(harness.ideas / "archive" / item.calculated_name)
    assert archived.fm["backfill"] is True  # the archive keeps the note as it was released
    assert load(harness.ideas / "output" / item.calculated_name).fm["backfill"] is True


def test_follow_up_jobs_of_a_backfill_item_have_the_low_priority_and_a_normal_clip_has_zero(
    harness, yt_facts
) -> None:
    assert harness.ctx.settings.backfill_priority == -10
    _release(harness)
    _stage(harness, CLIP, "yt")
    back, normal = _item(harness, BACK), _item(harness, VID)
    assert normal.origin == "inbox"
    [back_fetch] = _jobs(harness, "youtube.fetch", back.calculated_name)
    [normal_fetch] = _jobs(harness, "youtube.fetch", normal.calculated_name)
    assert (back_fetch.priority, normal_fetch.priority) == (-10, 0)

    assert _run(harness, handle_youtube_fetch, back_fetch) == Done({"item": "waiting_llm"})  # the hand-off
    harness.clock.advance(600)  # the gate's gap
    assert _run(harness, handle_youtube_fetch, normal_fetch) == Done({"item": "waiting_llm"})
    [back_reason] = _jobs(harness, "llm.reason", back.calculated_name)
    [normal_reason] = _jobs(harness, "llm.reason", normal.calculated_name)
    assert (back_reason.priority, normal_reason.priority) == (-10, 0)

    (harness.ideas / "facts" / f"{BACK}.json").unlink()  # facts lost: the reason job queues the fetch again
    assert _run(harness, handle_llm_reason, back_reason) == Done({"item": "waiting_youtube"})
    queued = [j for j in _jobs(harness, "youtube.fetch", back.calculated_name) if j.status == "queued"]
    assert [j.priority for j in queued] == [-10]


def test_a_backfill_clip_with_saved_facts_goes_straight_to_reason_at_the_low_priority(
    harness, yt_facts
) -> None:
    harness.ctx.services.youtube.cache(harness.ideas / "facts").put(
        yt_facts.model_copy(update={"video_id": BACK})
    )
    _release(harness)

    _stage(harness, CLIP)

    back = _item(harness, BACK)
    assert back.status == "waiting_llm"
    assert [j.priority for j in _jobs(harness, "llm.reason", back.calculated_name)] == [-10]


def test_a_new_clip_is_claimed_before_waiting_backfill_jobs(harness) -> None:
    _release(harness)
    _stage(harness, CLIP)  # the backfill fetch is queued first: without priority it would be claimed first
    harness.clock.advance(5)
    _stage(harness, "yt")

    assert harness.worker.run_once() == "succeeded"  # one claim: the gate allows one fetch now

    assert harness.fetch_calls == [VID]
    back = _item(harness, BACK)
    [waiting] = _jobs(harness, "youtube.fetch", back.calculated_name)
    assert (waiting.status, waiting.attempts) == ("queued", 0)


def test_the_gate_still_defers_a_backfill_fetch(harness) -> None:
    _release(harness)
    _stage(harness, CLIP)
    assert harness.gate.reserve() is None  # another fetch took the slot: the gate is closed for 10 minutes

    assert harness.drain(max_jobs=2) == []  # the claim leaves the fetch queued

    back = _item(harness, BACK)
    [fetch] = _jobs(harness, "youtube.fetch", back.calculated_name)
    assert (fetch.status, fetch.attempts, fetch.priority) == ("queued", 0, -10)
    assert harness.fetch_calls == []
    assert back.status == "waiting_youtube"

    harness.clock.advance(600)
    [first, *_] = harness.drain(max_jobs=4)
    assert first == "succeeded"
    assert harness.fetch_calls == [BACK]


def test_the_finished_page_has_no_backfill_marker(harness) -> None:
    _release(harness)
    harness.add_job("pipeline.run", only=[CLIP])

    assert harness.drain(max_jobs=4) == ["succeeded", "succeeded", "succeeded"]

    item = _item(harness, BACK)
    assert item.status == "published"
    pages = [p for p in harness.docs.rglob("*.md") if BACK in p.read_text(encoding="utf-8")]
    assert pages
    for page in pages:
        assert "backfill" not in load(page).fm


def test_a_note_without_the_marker_or_with_a_false_one_is_staged_as_inbox(harness) -> None:
    clips = harness.ideas / "inbox" / "clippings"
    (clips / "f.md").write_text(
        "---\nsource: https://www.youtube.com/watch?v=FFFFFFFFFF1\nbackfill: false\n---\n", encoding="utf-8"
    )
    (clips / "s.md").write_text(
        '---\nsource: https://www.youtube.com/watch?v=SSSSSSSSSS1\nbackfill: "yes"\n---\n', encoding="utf-8"
    )

    _stage(harness, "f", "s")

    for doc_id in ("FFFFFFFFFF1", "SSSSSSSSSS1"):
        item = _item(harness, doc_id)
        assert item.origin == "inbox"
        assert [j.priority for j in _jobs(harness, "youtube.fetch", item.calculated_name)] == [0]
