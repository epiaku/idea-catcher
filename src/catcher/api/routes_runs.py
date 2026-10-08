"""The trigger endpoints (scope `run`): they enqueue a job and answer; the worker does the work.

`POST /pipeline/runs` and `/pipeline/publish` answer `200` with the oldest `pipeline.run` (or
`pipeline.publish`) that is queued or running, whoever queued it (the scheduler, the CLI or the API), and
otherwise insert one and answer `202`. The insert carries the dedupe key `api:<type>`, so of two requests at
the same moment only one inserts (the partial unique index on active dedupe keys); the other gets that job
back.
A preview (`dry_run: true`, job `pipeline.preview`) is read-only and dedupes only against a queued or running
preview with the same params. A requeue is always its own job. No answer holds the job's params or a key."""

import hashlib
import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from catcher.api.auth import require
from catcher.api.deps import get_session
from catcher.core.db import utc_now
from catcher.modules.queue.models import Job, JobItem
from catcher.modules.queue.queue import enqueue
from catcher.modules.worker.app import check_job

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require("run"))])

RUN = "pipeline.run"
PREVIEW = "pipeline.preview"
PUBLISH = "pipeline.publish"
PUBLISH_PARAMS = {"pull": True, "push": True}  # the scheduled publish's params
ACTIVE = ("queued", "running")
NAME_MAX = 512  # a calculated name is `<subfolder>/<file name>.md`


class RunRequest(BaseModel):
    """`POST /pipeline/runs`. Every field is optional; an unknown field or a value of the wrong type is 422
    (no coercion: `"3"` is not a limit). The values themselves are checked by the handler's own parser."""

    model_config = ConfigDict(extra="forbid")

    dry_run: StrictBool = False
    profile: Annotated[StrictStr | None, Field(max_length=64)] = None
    limit: Annotated[StrictInt | None, Field(le=2**31 - 1)] = None
    retry_deferred: StrictBool = False


class NoBody(BaseModel):
    """A trigger without options: no body or `{}`; any field is 422 (never silently ignored)."""

    model_config = ConfigDict(extra="forbid")


class Enqueued(BaseModel):
    job_id: str
    existing: bool


def active_job(session: Session, job_type: str, params: dict[str, Any] | None = None) -> Job | None:
    """The oldest queued or running job of `job_type` (with exactly `params`, when given), or None."""
    statement = select(Job).where(Job.type == job_type, Job.status.in_(ACTIVE))
    if params is not None:
        statement = statement.where(Job.params == params)
    return session.scalars(statement.order_by(Job.created_at, Job.id).limit(1)).first()


def _checked(job_type: str, params: dict[str, Any]) -> None:
    """422 with the handler's own message when it would refuse `params`."""
    try:
        check_job(job_type, params)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None


def _answer(response: Response, job: Job, existing: bool) -> Enqueued:
    response.status_code = status.HTTP_200_OK if existing else status.HTTP_202_ACCEPTED
    return Enqueued(job_id=str(job.id), existing=existing)


def _once(
    session: Session,
    response: Response,
    job_type: str,
    params: dict[str, Any],
    dedupe_key: str,
    *,
    same_params: bool,
) -> Enqueued:
    """The active job of `job_type` (`200`), or a new one with `dedupe_key` (`202`). A request that loses the
    race to insert gets the winner's job from `enqueue` (`200`)."""
    found = active_job(session, job_type, params if same_params else None)
    if found is not None:
        return _answer(response, found, existing=True)
    job, created = enqueue(session, type=job_type, now=utc_now(), params=params, dedupe_key=dedupe_key)
    session.commit()
    return _answer(response, job, existing=not created)


def _preview_key(params: dict[str, Any]) -> str:
    """One dedupe key per set of preview params, so two previews with other params both run."""
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return f"api:{PREVIEW}:{hashlib.sha256(canonical.encode()).hexdigest()[:32]}"


@router.post(
    "/pipeline/runs",
    response_model=Enqueued,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        200: {"model": Enqueued, "description": "a run (or the same preview) is already queued or running"}
    },
)
def start_run(
    response: Response, session: Annotated[Session, Depends(get_session)], body: RunRequest | None = None
) -> Enqueued:
    """Start a `pipeline.run`, or with `dry_run: true` a read-only `pipeline.preview` whose result is the
    report. Returns at once: the worker runs the job."""
    request = body or RunRequest()
    given: dict[str, Any] = {"profile": request.profile, "limit": request.limit}
    params = {key: value for key, value in given.items() if value is not None}
    if request.retry_deferred:
        params["retry_deferred"] = True
    if request.dry_run:
        _checked(PREVIEW, params)
        return _once(session, response, PREVIEW, params, _preview_key(params), same_params=True)
    _checked(RUN, params)
    return _once(session, response, RUN, params, f"api:{RUN}", same_params=False)


@router.post(
    "/pipeline/publish",
    response_model=Enqueued,
    status_code=status.HTTP_202_ACCEPTED,
    responses={200: {"model": Enqueued, "description": "a publish is already queued or running"}},
)
def publish(
    response: Response, session: Annotated[Session, Depends(get_session)], body: NoBody | None = None
) -> Enqueued:
    """Commit, pull and push both repos (`pipeline.publish` `{"pull": true, "push": true}`)."""
    params = dict(PUBLISH_PARAMS)
    _checked(PUBLISH, params)
    return _once(session, response, PUBLISH, params, f"api:{PUBLISH}", same_params=False)


@router.post("/items/{name:path}/requeue", response_model=Enqueued, status_code=status.HTTP_202_ACCEPTED)
def requeue(
    name: Annotated[str, Path(max_length=NAME_MAX)],
    response: Response,
    session: Annotated[Session, Depends(get_session)],
) -> Enqueued:
    """Run one item again from `archive/` (`pipeline.run` `{"requeue": [name]}`), `name` being its calculated
    name (`notes/x.md`). Always its own job. 404 when no item has that name."""
    params = {"requeue": [name]}
    _checked(RUN, params)
    if not session.scalar(select(exists().where(JobItem.calculated_name == name))):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="item not found")
    job, _ = enqueue(session, type=RUN, now=utc_now(), params=params)
    session.commit()
    return _answer(response, job, existing=False)
