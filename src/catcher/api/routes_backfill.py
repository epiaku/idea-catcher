"""The backfill trigger endpoints (scope `run`): they enqueue a job and answer; the worker does the work.

`POST /backfill/docs` queues one `backfill.docs` (dedupe key `api:backfill.docs`); a dry run is its own job
and is never the existing job of a real one. `POST /backfill/channels` queues one `backfill.channel` per URL
(dedupe key `api:backfill.channel:<hash of the url>`, priority `backfill_priority`, the YouTube resource),
after every URL was checked: one URL that is not a channel or playlist is 422 and nothing is queued. A job of
the same URL that is queued or running is answered as it is. No answer holds the job's params or a key."""

import hashlib
import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from catcher.api.auth import require
from catcher.api.deps import get_session
from catcher.api.routes_runs import ACTIVE, Enqueued, _answer, _checked, _once
from catcher.api.schemas import BackfillOut, BackfillVideoOut, Page
from catcher.core.db import utc_now
from catcher.modules.backfill import store
from catcher.modules.backfill.channel import DEFAULT_CHANNEL_VIDEOS, MAX_CHANNEL_VIDEOS, is_channel_url
from catcher.modules.queue.models import BackfillVideo, Job
from catcher.modules.queue.queue import enqueue
from catcher.modules.worker.app import JOB_RESOURCES

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require("run"))])

read_router = APIRouter(prefix="/api/v1", dependencies=[Depends(require("read"))])

DOCS = "backfill.docs"
CHANNEL = "backfill.channel"
MAX_URLS = 10
URL_MAX = 2048

MAX_RELEASE = (
    100  # `release: N` is the 24 h cap itself: the API never lets one call turn the backlog into notes
)
# true / false / N, stored as given; omitted or null adds no key (the job's default, true, applies). Both
# members are strict, so a bool is never an int and `"5"`, `1.5` are 422.
Release = StrictBool | Annotated[StrictInt, Field(ge=0, le=MAX_RELEASE)] | None


class DocsRequest(BaseModel):
    """`POST /backfill/docs`. An unknown field or a value of the wrong type is 422 (no coercion)."""

    model_config = ConfigDict(extra="forbid")

    dry_run: StrictBool = False
    pull: StrictBool = True
    release: Release = None


class ChannelsRequest(BaseModel):
    """`POST /backfill/channels`: one job per URL, all with the same `max_videos` and `release`."""

    model_config = ConfigDict(extra="forbid")

    urls: Annotated[
        list[Annotated[StrictStr, Field(max_length=URL_MAX)]], Field(min_length=1, max_length=MAX_URLS)
    ]
    max_videos: Annotated[StrictInt, Field(ge=1, le=MAX_CHANNEL_VIDEOS)] = DEFAULT_CHANNEL_VIDEOS
    release: Release = None


class ChannelEnqueued(BaseModel):
    url: str
    job_id: str
    existing: bool


class ChannelsEnqueued(BaseModel):
    jobs: list[ChannelEnqueued]


def _normalized(url: str) -> str:
    """Light clean-up of a given URL: no surrounding whitespace, no trailing slashes (`@x`, `@x/` and ` @x `
    are one channel). Nothing else changes (no lowercase host, no `/videos`)."""
    return url.strip().rstrip("/")


def _dry_run_key(params: dict[str, Any]) -> str:
    """One dedupe key per set of dry-run params, so two dry runs with other params both run."""
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return f"api:{DOCS}:dry:{hashlib.sha256(canonical.encode()).hexdigest()[:32]}"


def _real_docs_job(session: Session) -> Job | None:
    """The oldest queued or running `backfill.docs` that is not a dry run (its params may differ)."""
    not_dry = func.coalesce(Job.params["dry_run"].astext, "false") != "true"
    statement = select(Job).where(Job.type == DOCS, Job.status.in_(ACTIVE), not_dry)
    return session.scalars(statement.order_by(Job.created_at, Job.id).limit(1)).first()


def active_channel_job(session: Session, url: str) -> Job | None:
    """The oldest queued or running `backfill.channel` of `url`, or None."""
    statement = select(Job).where(
        Job.type == CHANNEL, Job.status.in_(ACTIVE), Job.params["url"].astext == url
    )
    return session.scalars(statement.order_by(Job.created_at, Job.id).limit(1)).first()


@router.post(
    "/backfill/docs",
    response_model=Enqueued,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        200: {"model": Enqueued, "description": "a backfill of the docs is already queued or running"}
    },
)
def backfill_docs(
    response: Response, session: Annotated[Session, Depends(get_session)], body: DocsRequest | None = None
) -> Enqueued:
    """Queue a `backfill.docs`: store the links found in the docs repo as pending and, with `release`, release
    up to that daily cap. With `dry_run: true` it only counts (its own job). Returns at once."""
    request = body or DocsRequest()
    params: dict[str, Any] = {"dry_run": request.dry_run, "pull": request.pull}
    if request.release is not None:
        params["release"] = request.release
    _checked(DOCS, params)
    if request.dry_run:
        return _once(session, response, DOCS, params, _dry_run_key(params), same_params=True)
    found = _real_docs_job(session)
    if found is not None:
        return _answer(response, found, existing=True)
    job, created = enqueue(session, type=DOCS, now=utc_now(), params=params, dedupe_key=f"api:{DOCS}")
    session.commit()
    return _answer(response, job, existing=not created)


@router.post(
    "/backfill/channels",
    response_model=ChannelsEnqueued,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        200: {"model": ChannelsEnqueued, "description": "every channel already has a queued or running job"},
        422: {"description": "a URL is not a YouTube channel or playlist (nothing was queued)"},
    },
)
def backfill_channels(
    body: ChannelsRequest,
    request: Request,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
) -> ChannelsEnqueued:
    """Queue one `backfill.channel` per URL at the backfill priority. Every URL is checked first: one that is
    not a channel or playlist is 422 naming it, and nothing is queued."""
    urls = [_normalized(url) for url in body.urls]
    for url in urls:
        if not is_channel_url(url):
            raise HTTPException(status_code=422, detail=f"not a YouTube channel or playlist URL: {url}")
    priority = request.app.state.backfill_priority
    answers: list[ChannelEnqueued] = []
    for url in dict.fromkeys(urls):  # a URL given twice (after the clean-up) is one job
        params: dict[str, Any] = {"url": url, "max_videos": body.max_videos}
        if body.release is not None:
            params["release"] = body.release
        _checked(CHANNEL, params)
        found = active_channel_job(session, url)
        if found is not None:
            answers.append(ChannelEnqueued(url=url, job_id=str(found.id), existing=True))
            continue
        key = f"api:{CHANNEL}:{hashlib.sha256(url.encode()).hexdigest()[:32]}"
        job, created = enqueue(
            session,
            type=CHANNEL,
            now=utc_now(),
            params=params,
            priority=priority,
            resource=JOB_RESOURCES.get(CHANNEL),
            dedupe_key=key,
        )
        answers.append(ChannelEnqueued(url=url, job_id=str(job.id), existing=not created))
    session.commit()
    all_existing = all(a.existing for a in answers)
    response.status_code = status.HTTP_200_OK if all_existing else status.HTTP_202_ACCEPTED
    return ChannelsEnqueued(jobs=answers)


@read_router.get("/backfill", response_model=BackfillOut)
def get_backfill(
    session: Annotated[Session, Depends(get_session)],
    status: Annotated[Literal["pending", "released"] | None, Query()] = None,
    source: Annotated[Literal["docs", "channel"] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=2**31 - 1)] = 0,
) -> BackfillOut:
    """The counts per status and a page of the backlog in the release order (oldest `found_at` first)."""
    conditions = []
    if status is not None:
        conditions.append(BackfillVideo.status == status)
    if source is not None:
        conditions.append(BackfillVideo.source == source)
    total = session.scalar(select(func.count()).select_from(BackfillVideo).where(*conditions)) or 0
    rows = session.scalars(
        select(BackfillVideo)
        .where(*conditions)
        .order_by(BackfillVideo.found_at, BackfillVideo.video_id)
        .limit(limit)
        .offset(offset)
    ).all()
    page = Page[BackfillVideoOut](
        items=[BackfillVideoOut.model_validate(row) for row in rows], total=total, limit=limit, offset=offset
    )
    return BackfillOut(counts=store.counts(session), videos=page)
