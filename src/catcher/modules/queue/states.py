"""The one writer of item state: `ItemStates.transition` changes `job_items.status` (B5 decision 2).

A transition sets the status, `stage_reason`, `stage_since` (only when the status really changes) and
`updated_at`, and writes one `job_events` row, all in the caller's session; it does not commit. A transition
that changes neither the status nor the reason changes nothing and writes no event, so a handler that runs
twice leaves no trace. The database is the truth; the optional mirror (the frontmatter of the working copy)
is best effort and is written only after the caller's commit, by `mirror_after_commit`."""

import logging
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from catcher.core.db import require_aware
from catcher.modules.queue.items import set_item_status
from catcher.modules.queue.models import EVENT_LEVELS, ITEM_STATUSES, JobEvent, JobItem

log = logging.getLogger("catcher.queue")


class ItemStates:
    """Changes item states in Postgres and, after the caller's commit, mirrors them (best effort)."""

    def __init__(self, *, mirror: Callable[[JobItem], None] | None = None) -> None:
        self._mirror = mirror

    def transition(
        self,
        session: Session,
        calculated_name: str,
        status: str,
        *,
        now: datetime,
        reason: str | None = None,
        job_id: uuid.UUID | None = None,
        level: str = "info",
        data: dict[str, Any] | None = None,
    ) -> JobItem | None:
        """Move the item to `status` with `reason`; None when there is no such row. Does not commit.

        The row is locked (`FOR UPDATE`). It changes only when the status or the reason differs from the
        stored one: then `stage_reason = reason` (for every status), `error` as `set_item_status` keeps it
        (the reason for `failed` and `deferred`, cleared otherwise), `updated_at = now`, `stage_since = now`
        only when the status changed, and one `job_events` row (`level`, `job_id`, `data` as given)."""
        require_aware(now)
        if status not in ITEM_STATUSES:
            raise ValueError(f"unknown item status {status!r}")
        if level not in EVENT_LEVELS:
            raise ValueError(f"unknown event level {level!r}")
        item = session.scalars(
            select(JobItem).where(JobItem.calculated_name == calculated_name).with_for_update(),
            execution_options={"populate_existing": True},
        ).one_or_none()
        if item is None:
            return None
        old = item.status
        if old == status and item.stage_reason == reason:
            return item
        set_item_status(session, calculated_name, status, now=now, reason=reason)
        item.stage_reason = reason
        if old != status:
            item.stage_since = now
        message = f"{calculated_name}: {old} -> {status}" + (f": {reason}" if reason else "")
        session.add(JobEvent(job_id=job_id, item_id=item.id, ts=now, level=level, message=message, data=data))
        session.flush()
        return item

    def mirror_after_commit(self, item: JobItem) -> None:
        """Write the item's state to the mirror, if there is one. Call it after the commit of the transition.

        A failing mirror is logged and never raised: the database already holds the truth and the next
        transition writes the mirror again."""
        if self._mirror is None:
            return
        try:
            self._mirror(item)
        except Exception as error:
            log.warning("could not mirror the state of %s: %s", item.calculated_name, error)
