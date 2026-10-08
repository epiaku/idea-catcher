import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import Engine

from catcher.api.auth import require
from catcher.api.deps import get_engine
from catcher.api.schemas import GateOut
from catcher.core.db import utc_now
from catcher.modules.youtube.gate import GateUnavailable
from catcher.modules.youtube.gate_rules import wait_for
from catcher.modules.youtube.pg_gate import PostgresGate

log = logging.getLogger("catcher.api")

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require("read"))])


def _moment(seconds: float) -> datetime | None:
    return datetime.fromtimestamp(seconds, UTC) if seconds > 0 else None


@router.get("/youtube/gate", response_model=GateOut)
def youtube_gate(request: Request, engine: Annotated[Engine, Depends(get_engine)]) -> GateOut:
    """The state `catcher youtube gate` shows: the same `PostgresGate.snapshot` and the same rules."""
    gate = PostgresGate(
        engine,
        block_hours=request.app.state.youtube_block_hours,
        clock=lambda: utc_now().timestamp(),
    )
    try:
        state = gate.snapshot()
    except GateUnavailable as error:
        log.warning("gate unavailable: %s", type(error.__cause__ or error).__name__)
        raise HTTPException(status_code=503, detail="database unavailable") from None
    wait = wait_for(state, utc_now().timestamp())
    return GateOut(
        state="open" if wait is None else "blocked" if wait.blocked else "gap",
        until=None if wait is None else _moment(wait.until),
        streak=state.streak,
        next_allowed_at=_moment(state.next_allowed_at),
        blocked_until=_moment(state.blocked_until),
    )
