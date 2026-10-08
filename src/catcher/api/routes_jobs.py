import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from catcher.api.auth import require
from catcher.api.deps import get_session
from catcher.api.schemas import EventOut, JobDetailOut, JobOut, Page
from catcher.modules.queue.models import Job, JobEvent, JobItem

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require("read"))])

EVENT_CAP = 200
RUN_JOB = "pipeline.run"
JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]


def _utc(moment: datetime | None) -> datetime | None:
    """A naive datetime is UTC."""
    return moment.replace(tzinfo=UTC) if moment is not None and moment.tzinfo is None else moment


@router.get("/jobs", response_model=Page[JobOut])
def list_jobs(
    session: Annotated[Session, Depends(get_session)],
    status: Annotated[JobStatus | None, Query()] = None,
    type: Annotated[str | None, Query(max_length=64)] = None,
    created_from: Annotated[datetime | None, Query(alias="from")] = None,
    created_to: Annotated[datetime | None, Query(alias="to")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=2**31 - 1)] = 0,
) -> Page[JobOut]:
    """Jobs, newest first. `from` and `to` bound `created_at` (inclusive); a naive time is UTC."""
    conditions = []
    if status is not None:
        conditions.append(Job.status == status)
    if type is not None:
        conditions.append(Job.type == type)
    if created_from is not None:
        conditions.append(Job.created_at >= _utc(created_from))
    if created_to is not None:
        conditions.append(Job.created_at <= _utc(created_to))
    total = session.scalar(select(func.count()).select_from(Job).where(*conditions)) or 0
    rows = session.scalars(
        select(Job).where(*conditions).order_by(Job.created_at.desc(), Job.id).limit(limit).offset(offset)
    ).all()
    return Page[JobOut](
        items=[JobOut.model_validate(row) for row in rows], total=total, limit=limit, offset=offset
    )


@router.get("/jobs/{job_id}", response_model=JobDetailOut)
def job_detail(job_id: str, session: Annotated[Session, Depends(get_session)]) -> JobDetailOut:
    """One job with its first 200 events (oldest first) and, for a `pipeline.run`, the items of that run
    counted per class and status. A malformed id is as unknown as a missing one: 404."""
    try:
        wanted = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="job not found") from None
    job = session.get(Job, wanted)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    events = session.scalars(
        select(JobEvent).where(JobEvent.job_id == wanted).order_by(JobEvent.ts, JobEvent.id).limit(EVENT_CAP)
    ).all()
    counts: dict[str, dict[str, int]] = {}
    if job.type == RUN_JOB:
        grouped = session.execute(
            select(JobItem.doc_class, JobItem.status, func.count())
            .where(JobItem.root_job_id == wanted)
            .group_by(JobItem.doc_class, JobItem.status)
        )
        for doc_class, item_status, number in grouped:
            counts.setdefault(doc_class, {})[item_status] = number
    return JobDetailOut(
        **JobOut.model_validate(job).model_dump(),
        events=[EventOut.model_validate(event) for event in events],
        item_counts=counts,
    )
