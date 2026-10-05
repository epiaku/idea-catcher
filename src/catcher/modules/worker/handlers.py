"""The contract between the worker and its job handlers."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine

from catcher.core.config import Settings
from catcher.core.db import require_aware
from catcher.modules.pipeline.mirror import write_mirror
from catcher.modules.pipeline.process import Services
from catcher.modules.queue.models import Job, JobItem
from catcher.modules.queue.states import ItemStates
from catcher.modules.worker.blocks import BackendBlocks

UNMIRRORED = ("published", "duplicate")  # the finished page replaces the working copy; a duplicate has none


@dataclass(frozen=True)
class Done:
    """The job succeeded; `result` is stored on it."""

    result: dict[str, Any] | None = None


@dataclass(frozen=True)
class Defer:
    """Not now: put the job back until `run_after`. The attempt is given back."""

    run_after: datetime
    reason: str

    def __post_init__(self) -> None:
        require_aware(self.run_after)


@dataclass(frozen=True)
class Fail:
    """The job failed for good (no retry)."""

    error: str


HandlerResult = Done | Defer | Fail


@dataclass
class HandlerContext:
    settings: Settings
    services: Services
    engine: Engine
    ideas: Path
    docs: Path
    clock: Callable[[], datetime]
    backend_blocks: BackendBlocks = field(default_factory=BackendBlocks)  # per worker process, not persisted
    item_states: ItemStates = field(default_factory=ItemStates)  # no mirror unless given (build_context does)


class MirrorNotWritten(Exception):
    """The item's file was not there to mirror into, or could not be read."""


def frontmatter_mirror(ideas: Path) -> Callable[[JobItem], None]:
    """The mirror of `ItemStates` for the idea-bucket at `ideas`: the item's status, `stage_reason` and
    `stage_since` go into the frontmatter of `output/<calculated name>`, or of `failed/<calculated name>` for
    `failed` (the file was moved there first). Nothing for `published` (the finished page already says so)
    or `duplicate`. A file that is missing or unreadable raises MirrorNotWritten, which `mirror_after_commit`
    logs as a warning; the next transition writes the mirror again."""

    def mirror(item: JobItem) -> None:
        if item.status in UNMIRRORED:
            return
        folder = "failed" if item.status == "failed" else "output"
        path = ideas / folder / item.calculated_name
        since = item.stage_since or item.updated_at
        if not write_mirror(path, stage=item.status, reason=item.stage_reason, since=since):
            raise MirrorNotWritten(f"no readable working copy at {folder}/{item.calculated_name}")

    return mirror


Handler = Callable[[HandlerContext, Job], HandlerResult]
