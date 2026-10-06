import logging
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from catcher.modules.queue.items import stage_item
from catcher.modules.queue.models import JobEvent, JobItem
from catcher.modules.queue.queue import enqueue
from catcher.modules.queue.states import STUCK_SINCE, ItemStates, LlmMetrics, record_metrics, stuck_since

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
    assert event.data == {"from": "staging", "to": "deferred"}


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
    assert event.data == {"attempts": 3, "from": "staging", "to": "failed"}


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


def test_a_published_item_without_warnings_has_sql_null_warnings(session: Session, clock) -> None:
    """`warnings = None` is SQL NULL, not JSON `null`: `WHERE warnings IS NULL` finds the item and
    `jsonb_array_length(warnings)` does not raise on it (also after a run that had warnings)."""
    _stage(session, clock.now)
    ItemStates().transition(session, NAME, "published", now=clock.now)
    metrics = LlmMetrics(
        profile="notes",
        backend="fake",
        model="fake",
        prompt_version="v1",
        tokens_in=10,
        tokens_out=5,
        duration_ms=7,
        attempts=1,
        saved=False,
    )
    assert metrics.warning_lines() is None
    first = LlmMetrics(**{**metrics.__dict__, "dropped_tags": ("x",)})  # an earlier run dropped a tag
    record_metrics(session, NAME, first, now=clock.now)
    session.commit()
    record_metrics(session, NAME, metrics, now=clock.now)  # this run has no warnings
    session.commit()

    row = session.execute(
        text(
            "select warnings is null, coalesce(jsonb_array_length(warnings), 0)"
            " from job_items where calculated_name = :name"
        ),
        {"name": NAME},
    ).one()
    assert tuple(row) == (True, 0)


# ---- the stuck clock reads the event data, not the message ---------------------------------------


def test_every_transition_event_says_from_and_to_and_keeps_the_callers_other_keys(
    session: Session, clock
) -> None:
    _stage(session, clock.now)
    states = ItemStates()

    states.transition(session, NAME, "deferred", now=clock.now, reason="blocked")
    states.transition(
        session, NAME, "stuck", now=clock.now, data={"attempts": 3, "from": "nonsense", "to": "nonsense"}
    )

    assert [event.data for event in _events(session)] == [
        {"from": "staging", "to": "deferred"},
        {"attempts": 3, "from": "deferred", "to": "stuck"},  # `from` and `to` are always the real ones
    ]


def _mark_stuck_by_hand(session: Session, clock) -> datetime:
    """The item deferred, then made `stuck` as `mark_stuck` does it (with the STUCK_SINCE mark)."""
    _stage(session, clock.now)
    states = ItemStates()
    states.transition(session, NAME, "deferred", now=clock.now, reason="blocked")
    since = clock.now + timedelta(days=3)
    states.transition(
        session, NAME, "stuck", now=since, reason="deferred for 3 days", data={STUCK_SINCE: since.isoformat()}
    )
    return since


def test_a_reworded_event_message_does_not_change_the_stuck_clock(session: Session, clock) -> None:
    since = _mark_stuck_by_hand(session, clock)
    for event in _events(session):
        event.message = f"something else entirely about {event.id}"
    session.flush()

    assert stuck_since(session, NAME) == since


def test_an_old_event_without_from_and_to_is_still_read_from_its_message(session: Session, clock) -> None:
    """Events written before the event data said `from`/`to` keep working (an existing database)."""
    _stage(session, clock.now)
    item = session.scalars(select(JobItem).where(JobItem.calculated_name == NAME)).one()
    since = clock.now + timedelta(days=3)
    session.add_all(
        [
            JobEvent(
                item_id=item.id, ts=clock.now, level="warning", message=f"{NAME}: staging -> deferred: x"
            ),
            JobEvent(
                item_id=item.id,
                ts=since,
                level="warning",
                message=f"{NAME}: deferred -> stuck: deferred for 3 days: x",
                data={STUCK_SINCE: since.isoformat(), "deferred_since": clock.now.isoformat()},
            ),
            JobEvent(item_id=item.id, ts=since, level="info", message=f"{NAME}: staging -> waiting_llm"),
        ]
    )
    session.flush()

    assert stuck_since(session, NAME) == since


def test_a_stuck_row_that_reconcile_created_is_read_from_its_created_event(session: Session, clock) -> None:
    """Reconcile writes `reconcile: created <name> as stuck ...` with `{"from": None, "to": "stuck"}`."""
    _stage(session, clock.now)
    item = session.scalars(select(JobItem).where(JobItem.calculated_name == NAME)).one()
    since = clock.now - timedelta(days=1)
    session.add(
        JobEvent(
            item_id=item.id,
            ts=clock.now,
            level="info",
            message=f"reconcile: created {NAME} as stuck (from output/)",
            data={
                "reconcile": "created",
                "folder": "output",
                "from": None,
                "to": "stuck",
                STUCK_SINCE: since.isoformat(),
            },
        )
    )
    session.flush()

    assert stuck_since(session, NAME) == since
