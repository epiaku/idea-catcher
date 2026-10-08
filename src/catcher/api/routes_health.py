import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from catcher.api.deps import get_engine
from catcher.modules.worker.guard import worker_running

log = logging.getLogger("catcher.api")

router = APIRouter()


class Health(BaseModel):
    api: Literal["ok"] = "ok"
    database: Literal["ok", "down"]
    worker: Literal["running", "none", "unknown"]


@router.get(
    "/health",
    response_model=Health,
    responses={503: {"model": Health, "description": "the database is down or no worker holds the lock"}},
)
def health(engine: Annotated[Engine, Depends(get_engine)]) -> JSONResponse:
    """200 when the database answers and a worker holds the one-worker lock; 503 with the failing part
    named otherwise. Needs no key and holds no secret."""
    try:
        running = worker_running(engine)
    except SQLAlchemyError as error:
        log.warning("health: the database did not answer (%s)", type(error).__name__)
        body = Health(database="down", worker="unknown")
    else:
        body = Health(database="ok", worker="running" if running else "none")
    healthy = body.database == "ok" and body.worker == "running"
    return JSONResponse(body.model_dump(), status_code=200 if healthy else 503)
