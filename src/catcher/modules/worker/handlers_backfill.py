"""The `backfill.docs` handler: scan the docs repo for YouTube links, store the videos without a page in the
backlog and, when asked, release some of the backlog within the daily cap.

The docs repo is only pulled here (never committed); a dry run pulls nothing and writes nothing."""

import logging
from dataclasses import asdict, dataclass
from typing import Any

from catcher.core.db import session_scope
from catcher.core.git import GIT_LOCK, GitError, has_remote, pull, require_on_branch
from catcher.modules.backfill import store
from catcher.modules.backfill.importer import run_import
from catcher.modules.backfill.service import release_within
from catcher.modules.queue.models import Job
from catcher.modules.worker.handlers import Done, Fail, HandlerContext, HandlerResult

log = logging.getLogger("catcher.worker.backfill")

DOCS_PARAMS = frozenset({"dry_run", "pull", "release"})


@dataclass(frozen=True)
class DocsParams:
    dry_run: bool = False
    pull: bool = True
    release: int | None = None


def parse_docs_params(params: dict[str, Any]) -> DocsParams:
    """The `backfill.docs` params; raises ValueError naming what is wrong."""
    unknown = sorted(set(params) - DOCS_PARAMS)
    if unknown:
        raise ValueError(f"unknown parameter(s) for backfill.docs: {', '.join(unknown)}")
    for flag in ("dry_run", "pull"):
        if flag in params and not isinstance(params[flag], bool):
            raise ValueError(f"{flag} must be true or false, not {params[flag]!r}")
    release = params.get("release")
    if release is not None and (isinstance(release, bool) or not isinstance(release, int) or release < 0):
        raise ValueError(f"release must be a whole number of 0 or more, not {release!r}")
    return DocsParams(dry_run=params.get("dry_run", False), pull=params.get("pull", True), release=release)


def handle_backfill_docs(ctx: HandlerContext, job: Job) -> HandlerResult:
    """Pull the docs repo (unless `pull` is false or `dry_run`), store the new links as pending and, with
    `release`, release up to that daily cap. A failed pull is `Fail(git's message)` and stores nothing."""
    try:
        params = parse_docs_params(dict(job.params or {}))
    except ValueError as e:
        return Fail(str(e))
    tag = f"backfill.docs {job.id}"
    if params.pull and not params.dry_run:
        author = (ctx.settings.git_author_name, ctx.settings.git_author_email)
        with GIT_LOCK:
            try:
                require_on_branch(ctx.docs, unattended=True)
                if has_remote(ctx.docs, unattended=True):
                    pull(ctx.docs, author=author, unattended=True)
                    log.info("%s: pulled %s", tag, ctx.docs)
                else:
                    log.info("%s: %s has no git remote, nothing to pull", tag, ctx.docs)
            except GitError as e:
                log.error("%s: pull failed: %s", tag, e)
                return Fail(str(e))
    now = ctx.clock()
    allowance: int | None = None
    released: list[str] = []
    repaired: list[str] = []
    still_pending = 0
    with session_scope(ctx.engine) as session:
        imported = run_import(session, ctx.ideas, ctx.docs, now, dry_run=params.dry_run)
        log.info("%s: found %d, new %d, known %d", tag, imported.found, imported.new, imported.known)
        still_pending = imported.pending
        if params.release is not None and not params.dry_run:
            allowance, outcome = release_within(session, ctx.ideas, params.release, now)
            released, repaired = outcome.released, outcome.repaired
            still_pending = store.counts(session)["pending"]
            log.info(
                "%s: allowance %d, released %d, repaired %d", tag, allowance, len(released), len(repaired)
            )
    result = asdict(imported)
    del result["new_ids"]
    result.update(
        dry_run=params.dry_run,
        released=released,
        repaired=repaired,
        still_pending=still_pending,
        allowance=allowance,
    )
    return Done(result)
