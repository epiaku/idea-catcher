"""`run_on_worker(repos, services, **options)`: what `catcher run pipeline` does, with the test's services.

The tests of the old Stage A loop (`run_pipeline(ideas, docs, RunOptions(...), services)`) call this instead:
it runs the worker path (`run_command`: take the worker's lock, queue `pipeline.run`, drain it with the
`Worker`, queue and run `pipeline.publish`) and returns the `RunReport` built from the database, the shape
the old loop returned. `dry_run=True` is the read-only preview (`preview`), as the command does it.

The options are named like the old `RunOptions` fields (`profile`, `dry_run`, `push`, `limit`, `only`,
`requeue`, `refresh_facts`, `wait_youtube`, `retry_deferred`, `refresh_llm`). Besides them:

- `database_url`: the test database; by default `DATABASE_URL` from the environment (a test module points it
  at the testcontainers database).
- Services without a YouTube access (`make_services(facts=...)`, which the old loop called directly) get one
  around their `facts` fetcher, with an open in-memory gate and no gap: the worker's fetch job saves the
  facts through it for the `llm.reason` job after it, as the command's real services (`default_services`,
  which always build one) do. A test's own access is kept.
- `clock` and `sleep`: the worker's clock (an aware datetime; a frozen one for the YouTube gate tests) and
  the command's sleep for `wait_youtube` (its waits are the sleeps such a test counts).

The command's own lines go into `report.problems` as it prints them: a refused start, a lost lock and an
interrupt (Stage A put the interrupt there too). `run_outcome` returns the whole `RunOutcome`."""

import os
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from memory_gate import InMemoryGate

from catcher.core.db import utc_now
from catcher.modules.pipeline.preview import preview
from catcher.modules.pipeline.process import Services
from catcher.modules.pipeline.report import RunReport
from catcher.modules.worker.runner import RunOutcome, run_command
from catcher.modules.youtube.access import YoutubeAccess

OPTIONS = {
    "profile",
    "dry_run",
    "push",
    "limit",
    "only",
    "requeue",
    "refresh_facts",
    "wait_youtube",
    "retry_deferred",
    "refresh_llm",
}
LOCK_LOST = "the run lock was lost: the run stopped and committed nothing"
INTERRUPTED = "interrupted: {n} job(s) left queued, run catcher worker --once to finish"


def run_params(options: dict[str, Any]) -> dict[str, Any]:
    """The `pipeline.run` params of the old `RunOptions` fields, as the command builds them (only the ones
    given)."""
    unknown = set(options) - OPTIONS
    if unknown:
        raise TypeError(f"not a RunOptions field: {sorted(unknown)}")
    given = {key: options.get(key) for key in ("limit", "only", "requeue", "profile")}
    params = {key: value for key, value in given.items() if value is not None}
    params.update(
        {key: True for key in ("retry_deferred", "refresh_llm", "refresh_facts") if options.get(key)}
    )
    return params


def run_outcome(
    repos: Any,
    services: Services,
    *,
    database_url: str | None = None,
    clock: Callable[[], datetime] = utc_now,
    sleep: Callable[[float], object] = time.sleep,
    **options: Any,
) -> RunOutcome:
    """Run `catcher run pipeline` over the worker path with `services` (see the module docstring)."""
    params = run_params(options)
    url = database_url or os.environ["DATABASE_URL"]
    settings = services.settings.model_copy(
        update={"database_url": url, "ideas_repo": repos.ideas, "docs_repo": repos.docs}
    )
    services.settings = settings  # the handlers read both: they must agree
    if services.youtube is None:
        services.youtube = YoutubeAccess(services.facts, InMemoryGate(min_gap_s=0, jitter_s=0))
    if options.get("dry_run"):
        return RunOutcome(report=preview(repos.ideas, repos.docs, params, services))
    outcome = run_command(
        settings,
        ideas=repos.ideas,
        docs=repos.docs,
        params=params,
        push=bool(options.get("push")),
        wait_youtube_s=_wait_max_s(services) if options.get("wait_youtube") else None,
        services=services,
        clock=clock,
        sleep=sleep,
    )
    if outcome.refused:
        outcome.report.problems.append(outcome.refused)
    if outcome.lock_lost and not outcome.published:
        outcome.report.problems.append(LOCK_LOST)
    if outcome.interrupted:
        outcome.report.problems.append(INTERRUPTED.format(n=outcome.left_queued))
    return outcome


def _wait_max_s(services: Services) -> float:
    """The command's `--wait-youtube` limit: `YOUTUBE_WAIT_MAX_S`, which the real services also give their
    YouTube access; a test's own access (with its own `wait_max_s`) keeps its limit."""
    youtube = services.youtube
    return youtube.wait_max_s if youtube is not None else services.settings.youtube_wait_max_s


def run_on_worker(repos: Any, services: Services, **options: Any) -> RunReport:
    """The report of `run_outcome` (the shape `run_pipeline` returned)."""
    return run_outcome(repos, services, **options).report
