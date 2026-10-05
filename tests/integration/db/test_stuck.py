"""`stuck`: an item deferred for STUCK_AFTER_DAYS becomes stuck once (the reap timer checks it), a stuck item
is retried by `retry_deferred` like a deferred one, defers again without resetting its clock, and a publish
ends it. Real Postgres, real repos, fake model, frozen clock."""

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from catcher.core.config import Settings
from catcher.core.db import session_scope
from catcher.core.frontmatter import load
from catcher.modules.llm.backends.fake import FakeBackend
from catcher.modules.llm.service import BackendUnavailable
from catcher.modules.queue.models import Job, JobEvent, JobItem
from catcher.modules.queue.queue import claim, enqueue
from catcher.modules.queue.states import mark_stuck
from catcher.modules.worker import loop

DAY = 24 * 3600
HOUR = 3600
NOTE = "YouTube walks.md"
CHAT = "systeme.md"


def item_of(harness, original: str) -> JobItem:
    with session_scope(harness.ctx.engine) as session:
        [item] = [i for i in session.scalars(select(JobItem)) if i.original_filename == original]
        return item


def events_of(harness, name: str) -> list[JobEvent]:
    with session_scope(harness.ctx.engine) as session:
        item = session.scalars(select(JobItem).where(JobItem.calculated_name == name)).one()
        return list(
            session.scalars(select(JobEvent).where(JobEvent.item_id == item.id).order_by(JobEvent.id))
        )


def stuck_events(harness, name: str) -> list[JobEvent]:
    return [e for e in events_of(harness, name) if e.message.split(": ", 2)[1].endswith("-> stuck")]


def deferred_note(harness, why: str = "freellmapi unreachable") -> JobItem:
    """The note staged and its `llm.reason` run against a model that is down: the item is `deferred`."""
    harness.backends.note = FakeBackend([BackendUnavailable(why)])
    harness.add_job("pipeline.run", only=["YouTube walks"])
    assert harness.drain(max_jobs=3) == ["succeeded", "succeeded"]
    item = item_of(harness, NOTE)
    assert item.status == "deferred"
    return item


def run_mark_stuck(harness, after_days: float = 3) -> list[str]:
    with session_scope(harness.ctx.engine) as session:
        stuck = mark_stuck(session, now=harness.clock(), after_days=after_days)
        return [item.calculated_name for item in stuck]


def test_an_item_deferred_for_less_than_the_limit_stays_deferred(harness):
    item = deferred_note(harness)

    harness.clock.advance(2 * DAY + 23 * HOUR)
    assert run_mark_stuck(harness) == []

    again = item_of(harness, NOTE)
    assert (again.status, again.stage_since) == ("deferred", item.stage_since)
    assert stuck_events(harness, item.calculated_name) == []


def test_an_item_deferred_longer_becomes_stuck_once_with_a_warning_event(harness):
    item = deferred_note(harness)
    old_reason = item.stage_reason
    assert old_reason and "freellmapi unreachable" in old_reason

    harness.clock.advance(3 * DAY)  # exactly the limit
    assert run_mark_stuck(harness) == [item.calculated_name]

    stuck = item_of(harness, NOTE)
    assert stuck.status == "stuck"
    assert stuck.stage_since == harness.clock()
    assert stuck.stage_reason == f"deferred for 3 days: {old_reason}"
    [event] = stuck_events(harness, item.calculated_name)
    assert event.level == "warning"
    assert event.message == f"{item.calculated_name}: deferred -> stuck: deferred for 3 days: {old_reason}"

    harness.clock.advance(5 * DAY)  # once: a stuck item is not marked again
    assert run_mark_stuck(harness) == []
    assert len(stuck_events(harness, item.calculated_name)) == 1
    assert item_of(harness, NOTE).stage_since == stuck.stage_since


def test_a_stuck_item_that_defers_again_stays_stuck_and_keeps_its_clock(harness):
    item = deferred_note(harness)
    harness.clock.advance(3 * DAY + HOUR)
    assert run_mark_stuck(harness) == [item.calculated_name]
    stuck_since = item_of(harness, NOTE).stage_since

    harness.clock.advance(DAY)  # the block of the backend is long over: the retry calls it, and it is down
    harness.backends.note = FakeBackend([BackendUnavailable("still unreachable")])
    harness.add_job("pipeline.run", retry_deferred=True, only=["YouTube walks"])
    assert harness.drain(max_jobs=3) == ["succeeded", "succeeded"]

    again = item_of(harness, NOTE)
    assert len(harness.backends.note.prompts) == 1  # it really was retried
    assert again.status == "stuck"
    assert again.stage_since == stuck_since  # the clock is not reset
    assert again.stage_reason and "still unreachable" in again.stage_reason
    assert jobs(harness, "llm.reason")[-1].result == {"item": "stuck"}
    assert load(harness.ideas / "output" / item.calculated_name).fm["stage"] == "stuck"  # mirrored
    harness.clock.advance(10 * DAY)
    assert run_mark_stuck(harness) == []  # stuck is never marked twice


def jobs(harness, type: str) -> list[Job]:
    with session_scope(harness.ctx.engine) as session:
        return [j for j in harness.jobs(session) if j.type == type]


def test_retry_deferred_picks_up_stuck_items_too_and_a_publish_ends_it(harness):
    item = deferred_note(harness)
    harness.clock.advance(3 * DAY)
    assert run_mark_stuck(harness) == [item.calculated_name]
    harness.ctx.item_states.mirror_after_commit(item_of(harness, NOTE))  # as the reap timer does
    assert load(harness.ideas / "output" / item.calculated_name).fm["stage"] == "stuck"
    # a second document that is only deferred: both come back
    harness.backends.chat = FakeBackend([BackendUnavailable("openai unreachable")])
    harness.add_job("pipeline.run", only=["systeme"])
    assert harness.drain(max_jobs=3) == ["succeeded", "succeeded"]
    chat = item_of(harness, CHAT)
    assert chat.status == "deferred"

    harness.clock.advance(HOUR)
    harness.backends.note = FakeBackend()  # both models are back
    harness.backends.chat = FakeBackend()
    harness.add_job("pipeline.run", retry_deferred=True, only=["YouTube walks", "systeme"])
    assert harness.drain(max_jobs=4) == ["succeeded"] * 3

    assert item_of(harness, NOTE).status == "published"
    assert item_of(harness, CHAT).status == "published"
    assert len(harness.backends.note.prompts) == 1 and len(harness.backends.chat.prompts) == 1
    # published ends `stuck`: a later deferral of the same item is a plain deferral again
    harness.clock.advance(DAY)
    harness.backends.note = FakeBackend([BackendUnavailable("down once more")])
    harness.add_job("pipeline.run", requeue=["YouTube walks"], refresh_llm=True)  # not the saved reply
    assert harness.drain(max_jobs=3) == ["succeeded", "succeeded"]
    assert item_of(harness, NOTE).status == "deferred"


def test_the_reap_timer_runs_mark_stuck_and_an_error_in_it_does_not_stop_the_reaper(
    harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    item = deferred_note(harness)
    with session_scope(harness.ctx.engine) as session:  # a job a dead worker left running
        enqueue(session, type="test.noop", now=harness.clock())
    with session_scope(harness.ctx.engine) as session:
        assert claim(session, worker="dead-worker", now=harness.clock(), lease_s=30) is not None
    harness.clock.advance(3 * DAY)

    def broken(session: Session, **kwargs):
        raise RuntimeError("mark_stuck broke")

    monkeypatch.setattr(loop, "mark_stuck", broken)
    with caplog.at_level("ERROR", logger="catcher.worker"):
        assert harness.worker.reap_safely() == 1  # the reap itself stands
    assert any("stuck" in r.getMessage() and r.exc_info for r in caplog.records)
    assert item_of(harness, NOTE).status == "deferred"
    assert [j.status for j in jobs(harness, "test.noop")] == ["queued"]

    monkeypatch.undo()
    assert harness.worker.reap_safely() == 0  # the next reap runs mark_stuck again
    assert item_of(harness, NOTE).status == "stuck"
    assert load(harness.ideas / "output" / item.calculated_name).fm["stage"] == "stuck"  # mirrored


def test_stuck_after_days_is_validated(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("STUCK_AFTER_DAYS", raising=False)
    assert Settings().stuck_after_days == 3
    monkeypatch.setenv("STUCK_AFTER_DAYS", "1.5")
    assert Settings().stuck_after_days == 1.5
    for bad in ("0", "-2", "soon"):
        monkeypatch.setenv("STUCK_AFTER_DAYS", bad)
        with pytest.raises(ValidationError, match="stuck_after_days"):
            Settings()
