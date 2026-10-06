"""The one writer of item state: `ItemStates.transition` changes `job_items.status` (B5 decision 2).

A transition sets the status, `stage_reason`, `stage_since` (only when the status really changes) and
`updated_at`, and writes one `job_events` row, all in the caller's session; it does not commit. A transition
that changes neither the status nor the reason changes nothing and writes no event, so a handler that runs
twice leaves no trace. The database is the truth; the optional mirror (the frontmatter of the working copy)
is best effort and is written only after the caller's commit, by `mirror_after_commit`.

`mark_stuck` moves items that have been `deferred` for too long to `stuck` (B5 decision 4); `ItemStates.defer`
keeps a stuck item `stuck`, with its clock, when its retry is deferred again.

`record_metrics` and `record_llm_tried` put the LLM metrics of `llm.reason` on the item in the same session
as its transition (B5 decision 7). They take plain values (`LlmMetrics`), so the queue never imports the
pipeline."""

import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from catcher.core.db import require_aware
from catcher.modules.queue.items import TERMINAL_STATUSES, set_item_status
from catcher.modules.queue.models import EVENT_LEVELS, ITEM_STATUSES, JobEvent, JobItem

log = logging.getLogger("catcher.queue")

# the key in the data of a `stuck` event (`mark_stuck`, and a stuck item deferred again) that holds when the
# item became stuck: what `stuck_since` reads back after a retry
STUCK_SINCE = "stuck_since"


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
        only when the status changed, and one `job_events` row (`level`, `job_id`, and `data`: the caller's
        keys plus `{"from": <old status>, "to": <status>}`, which always win; `stuck_since` reads them)."""
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
        event_data = {**(data or {}), "from": old, "to": status}
        session.add(
            JobEvent(job_id=job_id, item_id=item.id, ts=now, level=level, message=message, data=event_data)
        )
        session.flush()
        return item

    def defer(
        self,
        session: Session,
        calculated_name: str,
        *,
        now: datetime,
        reason: str | None = None,
        job_id: uuid.UUID | None = None,
    ) -> JobItem | None:
        """A `deferred` outcome (a warning event). An item that `mark_stuck` made `stuck` and that was retried
        since (`stuck_since` finds it) stays `stuck`: the status is `stuck` with the new reason and its
        `stage_since` stays the time it became stuck, so the clock is not reset and it is never marked stuck
        twice. None when there is no such row. Does not commit."""
        since = stuck_since(session, calculated_name)
        if since is None:
            return self.transition(
                session, calculated_name, "deferred", now=now, reason=reason, job_id=job_id, level="warning"
            )
        item = self.transition(
            session,
            calculated_name,
            "stuck",
            now=now,
            reason=reason,
            job_id=job_id,
            level="warning",
            data={STUCK_SINCE: since.isoformat()},
        )
        if item is not None and item.stage_since != since:
            item.stage_since = since
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


@dataclass(frozen=True)
class LlmMetrics:
    """What one `llm.reason` learned about its model call, in plain values.

    `saved` is true when the reply came from a saved reply: no model was called and the tokens are the
    recorded ones (kept, so cost queries can exclude them by the flag). `warnings` are the other warnings
    (e.g. the YouTube summary checks), one string each; the dropped tags come first as `dropped tags: a, b`.
    `docs_page` is the page's path relative to the docs repo."""

    profile: str
    backend: str
    model: str | None
    prompt_version: str
    tokens_in: int | None
    tokens_out: int | None
    duration_ms: int | None
    attempts: int
    saved: bool
    docs_page: str | None = None
    dropped_tags: Sequence[str] = ()
    warnings: Sequence[str] = field(default_factory=tuple)

    def warning_lines(self) -> list[str] | None:
        """The item's `warnings`: the dropped tags, then the others; None when there are none."""
        lines = [f"dropped tags: {', '.join(self.dropped_tags)}"] if self.dropped_tags else []
        lines += list(self.warnings)
        return lines or None


def _locked_item(session: Session, calculated_name: str) -> JobItem | None:
    return session.scalars(
        select(JobItem).where(JobItem.calculated_name == calculated_name).with_for_update()
    ).one_or_none()


def record_metrics(
    session: Session,
    calculated_name: str,
    metrics: LlmMetrics,
    *,
    now: datetime,
    job_id: uuid.UUID | None = None,
) -> JobItem | None:
    """Put the metrics of a finished model call on the item; None when there is no such row. Does not commit.

    Call it in the session of the item's transition (after it). When tags were dropped and the item's
    `warnings` change, one warning event (with `data={"dropped_tags": [...]}`) is written; the same warnings
    again (a rerun) write none."""
    require_aware(now)
    item = _locked_item(session, calculated_name)
    if item is None:
        return None
    lines = metrics.warning_lines()
    changed = item.warnings != lines
    item.llm_profile = metrics.profile
    item.llm_backend = metrics.backend
    item.llm_model = metrics.model
    item.prompt_version = metrics.prompt_version
    item.tokens_in = metrics.tokens_in
    item.tokens_out = metrics.tokens_out
    item.llm_duration_ms = metrics.duration_ms
    item.llm_result = {"attempts": metrics.attempts, "saved": metrics.saved}
    item.warnings = lines
    item.docs_page = metrics.docs_page
    if changed and metrics.dropped_tags:
        dropped = list(metrics.dropped_tags)
        session.add(
            JobEvent(
                job_id=job_id,
                item_id=item.id,
                ts=now,
                level="warning",
                message=f"{calculated_name}: dropped tags: {', '.join(dropped)}",
                data={"dropped_tags": dropped},
            )
        )
    session.flush()
    return item


def record_llm_tried(session: Session, calculated_name: str, *, profile: str, backend: str) -> JobItem | None:
    """A deferred or failed model call: the item gets the profile and backend that were tried; its tokens and
    the other metrics are left as they are. None when there is no such row. Does not commit."""
    item = _locked_item(session, calculated_name)
    if item is None:
        return None
    item.llm_profile = profile
    item.llm_backend = backend
    session.flush()
    return item


def _transition_target(calculated_name: str, event: JobEvent) -> str | None:
    """The new status of a transition event, or None when the event is not a transition (a dropped-tags
    warning). It is `data["to"]` (every transition since the B5 fix wave, and reconcile's created event); an
    older event without it is read from its message (`<name>: <old> -> <new>[: <reason>]`)."""
    data = event.data or {}
    if "to" in data:
        target = data["to"]
        return target if isinstance(target, str) and target in ITEM_STATUSES else None
    prefix = f"{calculated_name}: "
    if not event.message.startswith(prefix):
        return None
    old, arrow, rest = event.message.removeprefix(prefix).partition(" -> ")
    new = rest.split(":", 1)[0]
    return new if arrow and old in ITEM_STATUSES and new in ITEM_STATUSES else None


def stuck_since(session: Session, calculated_name: str) -> datetime | None:
    """When the item became `stuck`, if its last outcome was `stuck` by `mark_stuck` (or a row `reconcile`
    created `stuck`; a retry may be running since); None otherwise. The last outcome is the item's newest
    event that moved it to a final status (read from the event data, see `_transition_target`); a `stuck`
    without the `STUCK_SINCE` mark (an active leftover `_requeue` found) is not an outcome and is passed
    over."""
    item_id = session.scalar(select(JobItem.id).where(JobItem.calculated_name == calculated_name))
    if item_id is None:
        return None
    events = session.scalars(select(JobEvent).where(JobEvent.item_id == item_id).order_by(JobEvent.id.desc()))
    for event in events:
        target = _transition_target(calculated_name, event)
        if target not in TERMINAL_STATUSES:
            continue
        mark = (event.data or {}).get(STUCK_SINCE) if target == "stuck" else None
        if target == "stuck" and not isinstance(mark, str):
            continue
        return datetime.fromisoformat(mark) if isinstance(mark, str) else None
    return None


def _days_text(delta: timedelta) -> str:
    """`3` for three days, `3.5` for three and a half (one decimal)."""
    return f"{delta.total_seconds() / 86400:.1f}".removesuffix(".0")


def mark_stuck(
    session: Session, *, now: datetime, after_days: float, states: ItemStates | None = None
) -> list[JobItem]:
    """Move every item that has been `deferred` for `after_days` days or more to `stuck` (B5 decision 4) and
    return those items, so the caller can mirror them after its commit. The time is `stage_since` (or
    `updated_at` for a row written before B5). Each goes through `ItemStates.transition` with the reason
    `deferred for <n> days: <old reason>` and a warning event that carries `STUCK_SINCE`. The rows are locked
    (`FOR UPDATE SKIP LOCKED`: a row another session holds is left for the next call). A stuck item is
    never marked again; it stays retried by `retry_deferred`. Does not commit."""
    require_aware(now)
    if not 0 < after_days < float("inf"):
        raise ValueError(f"after_days must be more than 0, not {after_days!r}")
    writer = states if states is not None else ItemStates()
    since = func.coalesce(JobItem.stage_since, JobItem.updated_at)
    rows = list(
        session.scalars(
            select(JobItem)
            .where(JobItem.status == "deferred", since <= now - timedelta(days=after_days))
            .order_by(since, JobItem.calculated_name)
            .with_for_update(skip_locked=True)
        )
    )
    stuck: list[JobItem] = []
    for row in rows:
        began = row.stage_since or row.updated_at
        old = row.stage_reason or row.error
        reason = f"deferred for {_days_text(now - began)} days" + (f": {old}" if old else "")
        data = {STUCK_SINCE: now.isoformat(), "deferred_since": began.isoformat()}
        item = writer.transition(
            session, row.calculated_name, "stuck", now=now, reason=reason, level="warning", data=data
        )
        if item is not None:
            log.warning("%s: stuck, %s", item.calculated_name, reason)
            stuck.append(item)
    return stuck
