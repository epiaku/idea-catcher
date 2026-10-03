import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from catcher.core.db import session_scope
from catcher.modules.queue.items import ItemExists, get_item, items_in_status, set_item_status, stage_item
from catcher.modules.queue.models import JobItem
from catcher.modules.queue.queue import enqueue

NAME = "notes/an-idea.md"


def _stage(session: Session, now: datetime, name: str = NAME, **overrides) -> JobItem:
    values = {
        "calculated_name": name,
        "doc_id": "doc-1",
        "doc_class": "note",
        "now": now,
        "inbox_path": "inbox/an idea.md",
        "original_filename": "an idea.md",
    }
    return stage_item(session, **{**values, **overrides})


def _count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(JobItem)) or 0


def test_stage_item_creates_a_staging_row(session: Session, clock) -> None:
    item = _stage(session, clock.now)

    assert item.status == "staging"
    assert item.calculated_name == NAME
    assert item.doc_id == "doc-1"
    assert item.doc_class == "note"
    assert item.origin == "inbox"
    assert item.inbox_path == "inbox/an idea.md"
    assert item.original_filename == "an idea.md"
    assert item.root_job_id is None
    assert item.error is None
    assert item.created_at == clock.now
    assert item.updated_at == clock.now
    assert get_item(session, NAME) is item


def test_get_item_of_a_missing_row_returns_none(session: Session) -> None:
    assert get_item(session, "notes/missing.md") is None


def test_a_naive_now_is_rejected(session: Session) -> None:
    with pytest.raises(ValueError):
        _stage(session, datetime(2026, 10, 2, 12, 0))
    _stage(session, datetime(2026, 10, 2, 12, 0, tzinfo=UTC))
    with pytest.raises(ValueError):
        set_item_status(session, NAME, "ready", now=datetime(2026, 10, 2, 12, 0))


@pytest.mark.parametrize("status", ["staging", "waiting_youtube", "waiting_llm", "ready"])
def test_a_second_stage_of_an_active_item_raises(session: Session, clock, status: str) -> None:
    _stage(session, clock.now)
    set_item_status(session, NAME, status, now=clock.now)

    with pytest.raises(ItemExists):
        _stage(session, clock.now + timedelta(minutes=1), inbox_path="inbox/other.md")

    item = get_item(session, NAME)
    assert item is not None
    assert item.status == status
    assert item.inbox_path == "inbox/an idea.md"
    assert _count(session) == 1


@pytest.mark.parametrize("status", ["published", "deferred", "failed", "stuck", "duplicate"])
def test_a_terminal_item_is_reset_by_a_requeue_not_duplicated(session: Session, clock, status: str) -> None:
    first = _stage(session, clock.now)
    set_item_status(session, NAME, status, now=clock.now, reason="the model said no")
    job, _ = enqueue(session, type="pipeline.run", now=clock.now)
    later = clock.now + timedelta(hours=1)

    again = _stage(
        session,
        later,
        inbox_path="inbox/again.md",
        original_filename="again.md",
        root_job_id=job.id,
        origin="backfill",
    )

    assert again.id == first.id
    assert again.status == "staging"
    assert again.error is None
    assert again.updated_at == later
    assert again.created_at == clock.now
    assert again.inbox_path == "inbox/again.md"
    assert again.original_filename == "again.md"
    assert again.root_job_id == job.id
    assert again.origin == "backfill"
    assert _count(session) == 1


def test_set_item_status_updates_status_and_reason(session: Session, clock) -> None:
    _stage(session, clock.now)
    later = clock.now + timedelta(minutes=5)

    deferred = set_item_status(session, NAME, "deferred", now=later, reason="llm timeout")
    assert deferred is not None
    assert deferred.status == "deferred"
    assert deferred.error == "llm timeout"
    assert deferred.updated_at == later

    failed = set_item_status(session, NAME, "failed", now=later, reason="bad page")
    assert failed is not None
    assert failed.error == "bad page"

    ready = set_item_status(session, NAME, "ready", now=later + timedelta(seconds=1), reason="ignored")
    assert ready is not None
    assert ready.status == "ready"
    assert ready.error is None
    assert ready.updated_at == later + timedelta(seconds=1)


def test_set_item_status_of_a_missing_row_returns_none(session: Session, clock) -> None:
    assert set_item_status(session, "notes/missing.md", "ready", now=clock.now) is None
    assert _count(session) == 0


def test_an_unknown_status_is_rejected_before_the_database(session: Session, clock) -> None:
    _stage(session, clock.now)
    with pytest.raises(ValueError):
        set_item_status(session, NAME, "bogus", now=clock.now)
    with pytest.raises(ValueError):
        items_in_status(session, "bogus")
    item = get_item(session, NAME)
    assert item is not None
    assert item.status == "staging"


def test_items_in_status_returns_the_oldest_first(session: Session, clock) -> None:
    _stage(session, clock.now + timedelta(minutes=2), name="youtube/c.md")
    _stage(session, clock.now, name="youtube/b.md")
    _stage(session, clock.now, name="youtube/a.md")
    _stage(session, clock.now + timedelta(minutes=1), name="notes/d.md")
    for name in ("youtube/a.md", "youtube/b.md", "youtube/c.md"):
        set_item_status(session, name, "waiting_youtube", now=clock.now + timedelta(minutes=3))

    waiting = items_in_status(session, "waiting_youtube")
    assert [item.calculated_name for item in waiting] == ["youtube/a.md", "youtube/b.md", "youtube/c.md"]

    both = items_in_status(session, "waiting_youtube", "staging")
    assert [item.calculated_name for item in both] == [
        "youtube/a.md",
        "youtube/b.md",
        "notes/d.md",
        "youtube/c.md",
    ]
    assert items_in_status(session) == []
    assert items_in_status(session, "published") == []


def test_two_connections_staging_the_same_name_get_one_row(pg_engine: Engine, session: Session) -> None:
    barrier = threading.Barrier(2)
    created: list[uuid.UUID] = []
    refused: list[ItemExists] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    now = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)

    def worker(index: int) -> None:
        try:
            with session_scope(pg_engine) as own:
                barrier.wait(timeout=10)
                try:
                    item = _stage(own, now, inbox_path=f"inbox/{index}.md")
                except ItemExists as error:
                    with lock:
                        refused.append(error)
                    return
                with lock:
                    created.append(item.id)
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(created) == 1
    assert len(refused) == 1
    assert _count(session) == 1
    item = get_item(session, NAME)
    assert item is not None
    assert item.id == created[0]
    assert item.status == "staging"
