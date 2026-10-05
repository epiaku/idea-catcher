import logging
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from catcher.modules.queue.items import stage_item
from catcher.modules.queue.models import JobEvent, JobItem
from catcher.modules.queue.queue import enqueue
from catcher.modules.queue.states import ItemStates

NAME = "notes/an-idea.md"


def _stage(session: Session, now: datetime) -> JobItem:
    return stage_item(
        session,
        calculated_name=NAME,
        doc_id="doc-1",
        doc_class="note",
        now=now,
        inbox_path="inbox/an idea.md",
        original_filename="an idea.md",
    )


def _events(session: Session) -> list[JobEvent]:
    return list(session.scalars(select(JobEvent).order_by(JobEvent.id)))


def _event_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(JobEvent)) or 0


def test_transition_changes_status_reason_and_stage_since_and_writes_an_event(
    session: Session, clock
) -> None:
    staged = _stage(session, clock.now)
    later = clock.now + timedelta(minutes=5)

    item = ItemStates().transition(session, NAME, "deferred", now=later, reason="the model is blocked")

    assert item is staged
    assert item.status == "deferred"
    assert item.stage_reason == "the model is blocked"
    assert item.error == "the model is blocked"  # the existing `error` semantics stay
    assert item.stage_since == later
    assert item.updated_at == later
    [event] = _events(session)
    assert event.item_id == item.id
    assert event.job_id is None
    assert event.ts == later
    assert event.level == "info"
    assert event.message == f"{NAME}: staging -> deferred: the model is blocked"
    assert event.data is None


def test_a_reason_is_kept_in_stage_reason_for_every_status_but_in_error_only_for_failed_and_deferred(
    session: Session, clock
) -> None:
    _stage(session, clock.now)

    item = ItemStates().transition(
        session, NAME, "waiting_youtube", now=clock.now, reason="the gate is closed"
    )

    assert item is not None
    assert item.stage_reason == "the gate is closed"
    assert item.error is None
    [event] = _events(session)
    assert event.message == f"{NAME}: staging -> waiting_youtube: the gate is closed"


def test_a_second_identical_transition_changes_nothing_and_writes_no_event(session: Session, clock) -> None:
    states = ItemStates()
    _stage(session, clock.now)
    first = clock.now + timedelta(minutes=1)
    states.transition(session, NAME, "deferred", now=first, reason="blocked")

    item = states.transition(session, NAME, "deferred", now=first + timedelta(hours=1), reason="blocked")

    assert item is not None
    assert item.status == "deferred"
    assert item.stage_since == first
    assert item.updated_at == first
    assert _event_count(session) == 1


def test_a_change_of_only_the_reason_keeps_stage_since(session: Session, clock) -> None:
    states = ItemStates()
    _stage(session, clock.now)
    first = clock.now + timedelta(minutes=1)
    second = first + timedelta(hours=1)
    states.transition(session, NAME, "deferred", now=first, reason="blocked")

    item = states.transition(session, NAME, "deferred", now=second, reason="the budget is used up")

    assert item is not None
    assert item.stage_since == first
    assert item.updated_at == second
    assert item.stage_reason == "the budget is used up"
    assert item.error == "the budget is used up"
    assert [event.message for event in _events(session)] == [
        f"{NAME}: staging -> deferred: blocked",
        f"{NAME}: deferred -> deferred: the budget is used up",
    ]


def test_a_transition_without_a_reason_clears_the_old_one(session: Session, clock) -> None:
    states = ItemStates()
    _stage(session, clock.now)
    states.transition(session, NAME, "deferred", now=clock.now, reason="blocked")

    item = states.transition(session, NAME, "ready", now=clock.now + timedelta(minutes=1))

    assert item is not None
    assert item.stage_reason is None
    assert item.error is None
    assert _events(session)[-1].message == f"{NAME}: deferred -> ready"


def test_transition_of_a_missing_item_returns_none(session: Session, clock) -> None:
    assert ItemStates().transition(session, "notes/missing.md", "ready", now=clock.now) is None
    assert _event_count(session) == 0


def test_a_bad_status_or_a_naive_now_is_rejected(session: Session, clock) -> None:
    _stage(session, clock.now)
    states = ItemStates()

    with pytest.raises(ValueError):
        states.transition(session, NAME, "bogus", now=clock.now)
    with pytest.raises(ValueError):
        states.transition(session, NAME, "ready", now=datetime(2026, 10, 2, 12, 0))

    item = session.scalars(select(JobItem).where(JobItem.calculated_name == NAME)).one()
    assert item.status == "staging"
    assert _event_count(session) == 0


def test_the_event_carries_the_job_id_and_level(session: Session, clock) -> None:
    _stage(session, clock.now)
    job, _ = enqueue(session, type="llm.reason", now=clock.now)

    ItemStates().transition(
        session,
        NAME,
        "failed",
        now=clock.now,
        reason="the model said no",
        job_id=job.id,
        level="error",
        data={"attempts": 3},
    )

    [event] = _events(session)
    assert event.job_id == job.id
    assert event.level == "error"
    assert event.data == {"attempts": 3}


def test_the_mirror_is_called_only_by_mirror_after_commit(session: Session, clock) -> None:
    mirrored: list[JobItem] = []
    states = ItemStates(mirror=mirrored.append)
    _stage(session, clock.now)

    item = states.transition(session, NAME, "ready", now=clock.now)
    assert item is not None
    assert mirrored == []

    states.mirror_after_commit(item)
    assert mirrored == [item]


def test_mirror_after_commit_without_a_mirror_does_nothing(session: Session, clock) -> None:
    item = _stage(session, clock.now)
    ItemStates().mirror_after_commit(item)


def test_a_failing_mirror_is_logged_and_never_raises(session: Session, clock, caplog) -> None:
    def broken(item: JobItem) -> None:
        raise OSError("read-only folder")

    item = _stage(session, clock.now)
    states = ItemStates(mirror=broken)

    with caplog.at_level(logging.WARNING, logger="catcher.queue"):
        states.mirror_after_commit(item)

    [record] = [r for r in caplog.records if r.name == "catcher.queue"]
    assert record.levelno == logging.WARNING
    assert NAME in record.getMessage()
    assert "read-only folder" in record.getMessage()
