"""The backfill backlog: which YouTube videos were found in the old links and which are released."""

from collections.abc import Mapping
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from catcher.core.db import require_aware
from catcher.modules.queue.models import BackfillVideo


def add_pending(session: Session, found: Mapping[str, tuple[str, str]], now: datetime) -> int:
    """Insert the unknown video ids (id -> (source, found_in)) as `pending`; return how many were new."""
    require_aware(now)
    if not found:
        return 0
    rows = [
        {"video_id": video_id, "source": source, "found_in": found_in, "status": "pending", "found_at": now}
        for video_id, (source, found_in) in found.items()
    ]
    statement = (
        insert(BackfillVideo)
        .values(rows)
        .on_conflict_do_nothing(index_elements=[BackfillVideo.video_id])
        .returning(BackfillVideo.video_id)
    )
    return len(session.execute(statement).all())


def pending(session: Session, limit: int | None = None) -> list[BackfillVideo]:
    statement = (
        select(BackfillVideo)
        .where(BackfillVideo.status == "pending")
        .order_by(BackfillVideo.found_at, BackfillVideo.video_id)
    )
    if limit is not None:
        statement = statement.limit(limit)
    return list(session.scalars(statement))


def mark_released(session: Session, video_id: str, now: datetime) -> bool:
    """Release a `pending` video; False when it is unknown or already released."""
    require_aware(now)
    statement = (
        update(BackfillVideo)
        .where(BackfillVideo.video_id == video_id, BackfillVideo.status == "pending")
        .values(status="released", released_at=now)
        .returning(BackfillVideo.video_id)
        .execution_options(synchronize_session="fetch")
    )
    return session.execute(statement).first() is not None


def counts(session: Session) -> dict[str, int]:
    grouped = select(BackfillVideo.status, func.count()).group_by(BackfillVideo.status)
    found = dict(session.execute(grouped).all())
    return {"pending": found.get("pending", 0), "released": found.get("released", 0)}


def known(session: Session) -> set[str]:
    return set(session.scalars(select(BackfillVideo.video_id)))
