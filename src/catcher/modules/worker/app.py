"""What `catcher worker` runs with: the handler registry and the handler context."""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from catcher.core.config import Settings
from catcher.core.db import make_worker_engine, utc_now
from catcher.modules.pipeline.process import default_services
from catcher.modules.worker.blocks import BackendBlocks
from catcher.modules.worker.handlers import Handler, HandlerContext
from catcher.modules.worker.handlers_pipeline import (
    handle_llm_reason,
    handle_pipeline_publish,
    handle_pipeline_run,
    handle_youtube_fetch,
    parse_params,
    parse_publish_params,
    parse_reason_params,
)
from catcher.modules.youtube.pg_gate import YOUTUBE_RESOURCE, PostgresGate

# Test-only seam: handlers merged into the registry. Production code never writes to it.
EXTRA_HANDLERS: dict[str, Handler] = {}


def build_handlers() -> dict[str, Handler]:
    """The job type -> handler registry (plus EXTRA_HANDLERS)."""
    handlers: dict[str, Handler] = {
        "pipeline.run": handle_pipeline_run,
        "youtube.fetch": handle_youtube_fetch,
        "llm.reason": handle_llm_reason,
        "pipeline.publish": handle_pipeline_publish,
    }
    handlers.update(EXTRA_HANDLERS)
    return handlers


# The param parser each handler runs first, so `catcher jobs add` refuses what the handler would refuse.
PARAM_CHECKS: dict[str, Callable[[dict[str, Any]], object]] = {
    "pipeline.run": parse_params,
    "youtube.fetch": lambda params: parse_reason_params(params, "youtube.fetch"),
    "llm.reason": parse_reason_params,
    "pipeline.publish": parse_publish_params,
}


# The `resources` row a job type waits for: the claim skips such a job while that row is closed. The handlers
# that queue a fetch pass the same resource themselves (they cannot import this module: it imports them).
JOB_RESOURCES: dict[str, str] = {"youtube.fetch": YOUTUBE_RESOURCE}


def check_job(job_type: str, params: dict[str, Any]) -> None:
    """Raise ValueError when no handler runs `job_type` (EXTRA_HANDLERS included), or when its handler would
    refuse `params`. A type without a param parser (a test handler) takes any params."""
    handlers = build_handlers()
    if job_type not in handlers:
        raise ValueError(f"unknown job type {job_type!r}: use one of {', '.join(sorted(handlers))}")
    check = PARAM_CHECKS.get(job_type)
    if check is not None:
        check(dict(params))


def build_context(
    settings: Settings,
    *,
    ideas: Path | None = None,
    docs: Path | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> HandlerContext:
    """The context every handler gets: the real services (`default_services`), a worker engine for
    DATABASE_URL (sessions with a lock timeout), and the two checkouts. The caller disposes the engine.

    The YouTube gate is the row `youtube` in Postgres on that same engine (not the file gate of Stage A), and
    it and the YouTube access read the same `clock`, so a frozen clock freezes both."""
    engine = make_worker_engine(settings.database_url)

    def epoch() -> float:
        return clock().timestamp()

    gate = PostgresGate(
        engine,
        min_gap_s=settings.youtube_min_gap_s,
        jitter_s=settings.youtube_gap_jitter_s,
        block_hours=settings.youtube_block_hours,
        clock=epoch,
    )
    return HandlerContext(
        settings=settings,
        services=default_services(settings, gate=gate, clock=epoch),
        engine=engine,
        ideas=ideas or settings.ideas_repo,
        docs=docs or settings.docs_repo,
        clock=clock,
        backend_blocks=BackendBlocks(),  # this worker's memory of blocked LLM backends (in memory only)
    )
