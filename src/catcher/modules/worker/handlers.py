"""The contract between the worker and its job handlers."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine

from catcher.core.config import Settings
from catcher.core.db import require_aware
from catcher.modules.pipeline.process import Services
from catcher.modules.queue.models import Job


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


Handler = Callable[[HandlerContext, Job], HandlerResult]
