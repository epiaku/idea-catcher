"""Item rows: one `job_items` row per document, keyed by its calculated name (`<subfolder>/<name>.md`).

The row is written before any file moves (decision 5). A requeue resets the existing row; it never
inserts a second one. Every function takes an aware `now` from the caller and none of them commits."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from catcher.core.db import require_aware
from catcher.modules.queue.models import ITEM_STATUSES, JobItem

ACTIVE_STATUSES = ("staging", "waiting_youtube", "waiting_llm", "ready")
TERMINAL_STATUSES = ("published", "deferred", "failed", "stuck", "duplicate")
_REASON_STATUSES = ("failed", "deferred")  # the reason is kept in `error` until B5 adds stage_reason
_STAGE_ATTEMPTS = 3  # the existing row can be deleted between our failed insert and our select


class ItemExists(Exception):
    """The item is already being processed (an active status); staging it again is refused."""

    def __init__(self, calculated_name: str, status: str) -> None:
        super().__init__(f"item {calculated_name!r} is already active (status {status!r})")
        self.calculated_name = calculated_name
        self.status = status


def _check_statuses(statuses: tuple[str, ...]) -> None:
    unknown = [status for status in statuses if status not in ITEM_STATUSES]
    if unknown:
        raise ValueError(f"unknown item status {', '.join(map(repr, unknown))}")


def stage_item(
    session: Session,
    *,
    calculated_name: str,
    doc_id: str,
    doc_class: str,
    now: datetime,
    inbox_path: str | None,
    original_filename: str | None,
    root_job_id: uuid.UUID | None = None,
    origin: str = "inbox",
) -> JobItem:
    """Create the item in status `staging`, or reset a row in a terminal status to `staging`.

    A reset keeps `id` and `created_at` (first seen), clears `error` and `stage_reason`, sets `updated_at` and
    `stage_since` to `now` (a requeued deferred row must not keep its old reason or its old time) and takes
    `doc_id`, `doc_class`, `inbox_path`, `original_filename`, `root_job_id` and `origin` from this call.
    A row in an active status raises `ItemExists`. The insert is guarded (`ON CONFLICT DO NOTHING`) and
    an existing row is locked (`FOR UPDATE`) while it is checked and reset, so two sessions staging the
    same name at once end with one row and one winner. Does not commit."""
    require_aware(now)
    fresh: dict[str, Any] = {
        "doc_id": doc_id,
        "doc_class": doc_class,
        "origin": origin,
        "status": "staging",
        "inbox_path": inbox_path,
        "original_filename": original_filename,
        "root_job_id": root_job_id,
        "error": None,
        "stage_reason": None,
        "stage_since": now,
        "updated_at": now,
    }
    for _ in range(_STAGE_ATTEMPTS):
        statement = (
            insert(JobItem)
            .values(id=uuid.uuid4(), calculated_name=calculated_name, created_at=now, **fresh)
            .on_conflict_do_nothing(index_elements=[JobItem.calculated_name])
            .returning(JobItem)
        )
        inserted = session.scalars(statement, execution_options={"populate_existing": True}).one_or_none()
        if inserted is not None:
            return inserted
        existing = session.scalars(
            select(JobItem).where(JobItem.calculated_name == calculated_name).with_for_update(),
            execution_options={"populate_existing": True},
        ).one_or_none()
        if existing is None:
            continue  # deleted between the insert and the select: the name is free, insert again
        if existing.status not in TERMINAL_STATUSES:
            raise ItemExists(calculated_name, existing.status)
        for key, value in fresh.items():
            setattr(existing, key, value)
        session.flush()
        return existing
    raise RuntimeError(f"could not stage or find the item {calculated_name!r}")


def set_item_status(
    session: Session, calculated_name: str, status: str, *, now: datetime, reason: str | None = None
) -> JobItem | None:
    """Move the item to `status` and set `updated_at`; None when there is no such row.

    The reason is stored in `error` for `failed` and `deferred` and cleared for every other status.
    Does not commit."""
    require_aware(now)
    _check_statuses((status,))
    item = session.scalars(
        select(JobItem).where(JobItem.calculated_name == calculated_name).with_for_update(),
        execution_options={"populate_existing": True},
    ).one_or_none()
    if item is None:
        return None
    item.status = status
    item.error = reason if status in _REASON_STATUSES else None
    item.updated_at = now
    session.flush()
    return item


def get_item(session: Session, calculated_name: str) -> JobItem | None:
    """The item with this calculated name, or None."""
    return session.scalars(
        select(JobItem).where(JobItem.calculated_name == calculated_name),
        execution_options={"populate_existing": True},
    ).one_or_none()


def items_in_status(session: Session, *statuses: str) -> list[JobItem]:
    """The items in any of `statuses`, oldest first: by `created_at` (first seen), then `calculated_name`,
    so items waiting for YouTube are served in arrival order. No statuses gives an empty list."""
    _check_statuses(statuses)
    if not statuses:
        return []
    statement = (
        select(JobItem)
        .where(JobItem.status.in_(statuses))
        .order_by(JobItem.created_at, JobItem.calculated_name)
    )
    return list(session.scalars(statement, execution_options={"populate_existing": True}))
