"""What `catcher worker` runs with: the handler registry and the handler context."""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from catcher.core.config import Settings
from catcher.core.db import make_worker_engine, utc_now
from catcher.modules.pipeline.process import default_services
from catcher.modules.worker.handlers import Handler, HandlerContext
from catcher.modules.worker.handlers_pipeline import handle_pipeline_run

# Test-only seam: handlers merged into the registry. Production code never writes to it.
EXTRA_HANDLERS: dict[str, Handler] = {}


def build_handlers() -> dict[str, Handler]:
    """The job type -> handler registry (plus EXTRA_HANDLERS)."""
    handlers: dict[str, Handler] = {"pipeline.run": handle_pipeline_run}
    handlers.update(EXTRA_HANDLERS)
    return handlers


def build_context(
    settings: Settings,
    *,
    ideas: Path | None = None,
    docs: Path | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> HandlerContext:
    """The context every handler gets: the real services (`default_services`), a worker engine for
    DATABASE_URL (sessions with a lock timeout), and the two checkouts. The caller disposes the engine."""
    return HandlerContext(
        settings=settings,
        services=default_services(settings),
        engine=make_worker_engine(settings.database_url),
        ideas=ideas or settings.ideas_repo,
        docs=docs or settings.docs_repo,
        clock=clock,
    )
