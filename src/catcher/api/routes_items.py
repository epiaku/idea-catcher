from typing import Annotated, Literal, get_args

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from catcher.api.auth import require
from catcher.api.deps import get_session
from catcher.api.schemas import ItemOut, Page
from catcher.modules.queue.models import ITEM_STATUSES, JobItem

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require("read"))])

ItemStatus = Literal[
    "staging",
    "waiting_youtube",
    "waiting_llm",
    "ready",
    "published",
    "deferred",
    "stuck",
    "failed",
    "duplicate",
]
assert get_args(ItemStatus) == ITEM_STATUSES  # one vocabulary


def _out(item: JobItem) -> ItemOut:
    return ItemOut(
        name=item.calculated_name,
        doc_id=item.doc_id,
        doc_class=item.doc_class,
        status=item.status,
        stage_reason=item.stage_reason,
        stage_since=item.stage_since,
        profile=item.llm_profile,
        backend=item.llm_backend,
        model=item.llm_model,
        tokens_in=item.tokens_in,
        tokens_out=item.tokens_out,
        origin=item.origin,
        updated_at=item.updated_at,
        output_path=item.output_path,
    )


@router.get("/items", response_model=Page[ItemOut])
def list_items(
    session: Annotated[Session, Depends(get_session)],
    status: Annotated[ItemStatus | None, Query()] = None,
    doc_class: Annotated[str | None, Query(max_length=32)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=2**31 - 1)] = 0,
) -> Page[ItemOut]:
    """Items, most recently updated first (a stuck item is `status=stuck`)."""
    conditions = []
    if status is not None:
        conditions.append(JobItem.status == status)
    if doc_class is not None:
        conditions.append(JobItem.doc_class == doc_class)
    total = session.scalar(select(func.count()).select_from(JobItem).where(*conditions)) or 0
    rows = session.scalars(
        select(JobItem)
        .where(*conditions)
        .order_by(JobItem.updated_at.desc(), JobItem.calculated_name)
        .limit(limit)
        .offset(offset)
    ).all()
    return Page[ItemOut](items=[_out(row) for row in rows], total=total, limit=limit, offset=offset)
