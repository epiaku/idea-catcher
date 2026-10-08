"""Response models of the read endpoints. They list the fields on purpose: a column added to a table is
not served until it is added here (file contents, inbox and failed paths and error texts stay out)."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: str
    status: str
    priority: int
    params: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None
    reason: str | None
    attempts: int
    run_after: datetime
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ts: datetime
    level: str
    message: str
    data: dict[str, Any] | None
    item_id: uuid.UUID | None


class JobDetailOut(JobOut):
    events: list[EventOut]
    item_counts: dict[str, dict[str, int]]  # doc_class -> status -> n; only for a pipeline.run job


class ItemOut(BaseModel):
    name: str
    doc_id: str
    doc_class: str
    status: str
    stage_reason: str | None
    stage_since: datetime | None
    profile: str | None
    backend: str | None
    model: str | None
    tokens_in: int | None
    tokens_out: int | None
    origin: str
    updated_at: datetime
    output_path: str | None


class GateOut(BaseModel):
    """The YouTube gate as `catcher youtube gate` reads it: open, the gap between two fetches, or a block."""

    state: Literal["open", "gap", "blocked"]
    until: datetime | None  # when a call is allowed again (None when open)
    streak: int
    next_allowed_at: datetime | None
    blocked_until: datetime | None
