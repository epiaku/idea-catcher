"""The `backfill.channel` handler: a fake extractor and the real Postgres gate (no yt-dlp, no network)."""

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, text

from catcher.core.db import session_scope
from catcher.modules.backfill.release import note_path
from catcher.modules.queue.models import BackfillVideo, Job, JobItem
from catcher.modules.queue.queue import claim, enqueue
from catcher.modules.worker import handlers_backfill
from catcher.modules.worker.app import JOB_RESOURCES, build_handlers, check_job
from catcher.modules.worker.handlers import Defer, Done, Fail, HandlerResult
from catcher.modules.worker.handlers_backfill import (
    handle_backfill_channel,
    parse_channel_params,
)
from catcher.modules.youtube.pg_gate import YOUTUBE_RESOURCE

pytestmark = pytest.mark.db

IDS = [f"{c * 10}1" for c in "abcde"]
URL = "https://www.youtube.com/@small"


class FakeExtractor:
    def __init__(self, ids: list[str] | None = None, error: Exception | None = None) -> None:
        self.ids = IDS if ids is None else ids
        self.error = error
        self.calls: list[tuple[Any, ...]] = []

    def __call__(self, url: str, *, max_videos: int) -> dict[str, Any]:
        self.calls.append((url, max_videos))
        if self.error is not None:
            raise self.error
        return {
            "entries": [
                {"_type": "url", "id": v, "url": f"https://www.youtube.com/watch?v={v}"} for v in self.ids
            ]
        }


class Http429(Exception):
    status = 429


@pytest.fixture
def extractor(monkeypatch) -> FakeExtractor:
    fake = FakeExtractor()
    monkeypatch.setattr(handlers_backfill, "EXTRACTOR_FACTORY", lambda delay: fake)
    return fake


def run_job(harness, **params) -> HandlerResult:
    params.setdefault("url", URL)
    job_id = harness.add_job("backfill.channel", **params)
    with session_scope(harness.ctx.engine) as session:
        job = session.get(Job, job_id)
        assert job is not None
        return handle_backfill_channel(harness.ctx, job)


def rows(harness) -> dict[str, BackfillVideo]:
    with session_scope(harness.ctx.engine) as session:
        found = list(session.scalars(select(BackfillVideo)))
        session.expunge_all()
    return {r.video_id: r for r in found}


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"url": "https://www.youtube.com/watch?v=aaaaaaaaaaa"},
        {"url": URL, "max_videos": 101},
        {"url": URL, "max_videos": 0},
        {"url": URL, "max_videos": True},
        {"url": URL, "release": -1},
        {"url": URL, "release": "5"},
        {"url": URL, "x": 1},
    ],
)
def test_channel_params_reject_a_video_url_and_a_big_max_videos(params) -> None:
    with pytest.raises(ValueError):
        parse_channel_params(params)
    with pytest.raises(ValueError):
        check_job("backfill.channel", params)


def test_channel_params_defaults_and_registration() -> None:
    parsed = parse_channel_params({"url": URL})
    assert (parsed.url, parsed.max_videos, parsed.release) == (URL, 50, None)
    assert build_handlers()["backfill.channel"] is handle_backfill_channel
    assert JOB_RESOURCES["backfill.channel"] == YOUTUBE_RESOURCE


def test_lists_and_stores_unknown_videos(harness, extractor) -> None:
    now = harness.clock()
    with session_scope(harness.ctx.engine) as session:  # IDS[0] and IDS[1] have a page already
        for vid in IDS[:2]:
            session.add(
                JobItem(
                    calculated_name=f"youtube/{vid}.md",
                    doc_id=vid,
                    doc_class="youtube",
                    status="published",
                    created_at=now,
                    updated_at=now,
                )
            )

    result = run_job(harness)

    assert isinstance(result, Done)
    assert result.result == {
        "url": URL,
        "listed": 5,
        "known": 2,
        "new": 3,
        "released": [],
        "repaired": [],
        "still_pending": 3,
    }
    assert extractor.calls == [(f"{URL}/videos", 50)]
    stored = rows(harness)
    assert sorted(stored) == sorted(IDS[2:])
    assert {r.source for r in stored.values()} == {"channel"}
    assert {r.found_in for r in stored.values()} == {URL}


def test_size_over_the_gap_rule_is_fail_with_the_largest_size(harness, extractor) -> None:
    harness.ctx.settings = harness.ctx.settings.model_copy(
        update={"youtube_min_gap_s": 120.0, "youtube_request_delay_s": 20.0}
    )

    result = run_job(harness, max_videos=100)

    assert isinstance(result, Fail)
    assert "use at most --max-videos 90" in result.error
    assert extractor.calls == [] and rows(harness) == {}


def test_closed_gate_defers_the_job(harness, extractor) -> None:
    assert harness.gate.reserve() is None  # another call took the slot
    slot = harness.clock() + timedelta(seconds=600)

    result = run_job(harness)

    assert isinstance(result, Defer)
    assert result.run_after == slot
    assert extractor.calls == [] and rows(harness) == {}


def test_block_during_listing_fails_and_opens_the_breaker(harness, extractor) -> None:
    extractor.error = Http429("HTTP Error 429: Too Many Requests")

    result = run_job(harness)

    assert isinstance(result, Fail)
    assert harness.gate.snapshot().blocked_until is not None
    wait = harness.gate.peek()
    assert wait is not None and wait.blocked
    assert rows(harness) == {}


def test_offline_is_fail(harness, extractor) -> None:
    harness.ctx.services.youtube.offline = True

    result = run_job(harness)

    assert isinstance(result, Fail)
    assert result.error.startswith("YOUTUBE_OFFLINE is on")
    assert extractor.calls == [] and rows(harness) == {}
    assert harness.gate.peek() is None  # no gate slot was taken


def test_release_after_listing_respects_the_cap(harness, extractor) -> None:
    result = run_job(harness, release=2)

    assert isinstance(result, Done)
    assert len(result.result["released"]) == 2
    assert result.result["still_pending"] == 3
    notes = [note_path(harness.ideas, vid) for vid in result.result["released"]]
    assert all(note.is_file() for note in notes)
    assert sorted(v for v, r in rows(harness).items() if r.status == "released") == sorted(
        result.result["released"]
    )


def test_claim_skips_the_job_while_the_gate_is_closed(harness) -> None:
    now = harness.clock()
    with session_scope(harness.ctx.engine) as session:
        enqueue(
            session,
            type="backfill.channel",
            now=now,
            params={"url": URL},
            resource=JOB_RESOURCES["backfill.channel"],
        )
        session.execute(
            text("update resources set next_allowed_at = :at where name = 'youtube'"),
            {"at": now + timedelta(seconds=60)},
        )

    with session_scope(harness.ctx.engine) as session:
        assert claim(session, worker="w", now=now, lease_s=30) is None
    with session_scope(harness.ctx.engine) as session:
        session.execute(text("update resources set next_allowed_at = null where name = 'youtube'"))
    with session_scope(harness.ctx.engine) as session:
        job = claim(session, worker="w", now=now, lease_s=30)
        assert job is not None and job.type == "backfill.channel"
