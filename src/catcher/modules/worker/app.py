"""What `catcher worker` runs with: the handler registry and the handler context."""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from catcher.core.config import Settings
from catcher.core.db import make_worker_engine, utc_now
from catcher.modules.pipeline.process import Services, default_services
from catcher.modules.queue.states import ItemStates
from catcher.modules.worker.blocks import BackendBlocks
from catcher.modules.worker.handlers import Handler, HandlerContext, frontmatter_mirror
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
    services: Services | None = None,
) -> HandlerContext:
    """The context every handler gets: the real services (`default_services`), a worker engine for
    DATABASE_URL (sessions with a lock timeout), and the two checkouts. The caller disposes the engine.

    The YouTube gate is the row `youtube` in Postgres on that same engine (the one gate), and
    it and the YouTube access read the same `clock`, so a frozen clock freezes both. Given `services` are used
    as they are (`run pipeline` passes the command's own, built while it holds the lock)."""
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
    ideas = ideas or settings.ideas_repo
    svc = services if services is not None else default_services(settings, gate=gate, clock=epoch)

    def known_backends() -> set[str]:
        return {profile.backend for profile in svc.profiles.profiles.values()}

    return HandlerContext(
        settings=settings,
        services=svc,
        engine=engine,
        ideas=ideas,
        docs=docs or settings.docs_repo,
        clock=clock,
        # the LLM blocks: `resources` rows on the same engine, the same clock (kept over a restart)
        backend_blocks=BackendBlocks(engine, clock=clock, known_backends=known_backends),
        item_states=ItemStates(mirror=frontmatter_mirror(ideas)),  # Postgres, then the frontmatter mirror
    )
