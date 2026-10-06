"""What a failed or interrupted document means: one pure mapping from exception to outcome (`classify`),
and what the handlers do with it (`apply_outcome`: the report item and the files; `log_outcome`: the log
line)."""

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from catcher.modules.llm.profiles import UnknownProfile
from catcher.modules.llm.service import (
    BackendBlocked,
    BackendUnavailable,
    BudgetExhausted,
    InputRejected,
    InvalidOutput,
    UsageLimitReached,
)
from catcher.modules.pipeline.inbox import Note, mark_deferred, move_to_failed, return_to_inbox
from catcher.modules.pipeline.report import ItemReport
from catcher.modules.youtube.facts import FactsDeferred, FactsUnavailable, FetchSkipped

log = logging.getLogger("catcher.run")

OutcomeKind = Literal["deferred", "failed", "waiting", "would_fetch", "interrupted"]


@dataclass(frozen=True)
class Outcome:
    kind: OutcomeKind
    message: str
    backend: str | None = None
    budget: bool = False
    config_error: bool = False
    unexpected: bool = False  # only the "anything else" branch: `log_outcome` logs a traceback for it


def classify(error: BaseException) -> Outcome:
    """Map an exception to its outcome. Order matters: several of these are subclasses of others."""
    if isinstance(error, KeyboardInterrupt):
        return Outcome("interrupted", "back in inbox/")
    if isinstance(error, BudgetExhausted):
        return Outcome("deferred", f"budget reached ({error.backend})", backend=error.backend, budget=True)
    if isinstance(error, BackendBlocked):  # not called at all: the message says until when, and why
        return Outcome("deferred", f"{error.backend}: {error}", backend=error.backend)
    if isinstance(error, UsageLimitReached):
        return Outcome("deferred", f"usage limit ({error.backend}): {error}", backend=error.backend)
    if isinstance(error, BackendUnavailable):
        return Outcome("deferred", str(error))
    if isinstance(error, UnknownProfile):
        return Outcome("deferred", str(error), config_error=True)
    if isinstance(error, InvalidOutput | InputRejected):
        return Outcome("failed", str(error))
    if isinstance(error, FactsDeferred):
        return Outcome("waiting", f"{error} (back in inbox/)")
    if isinstance(error, FetchSkipped):
        return Outcome("would_fetch", str(error))
    if isinstance(error, FactsUnavailable):
        return Outcome("deferred", str(error))
    return Outcome("failed", f"unexpected {type(error).__name__}: {error}", unexpected=True)


@dataclass
class RunState:
    """What `apply_outcome` records an outcome against. The handlers pass a fresh one per document (it was
    the state the Stage A loop kept from one document to the next)."""

    blocked: set[str]  # backends not to call again in this run (a usage limit or budget was hit)
    budget_blocked: dict[str, int]  # backend -> notes waiting because its budget is used up
    attempted: int  # documents worked on, counted against --limit
    seen_ids: set[str]  # ids processed so far, to warn when a page replaces an earlier one


def outcome_message(outcome: Outcome, state: RunState) -> str:
    """The report message: a usage limit on a backend whose budget is already used up is the budget too.
    `classify` cannot know that (it is stateless), so the rule lives here."""
    backend = outcome.backend
    if backend is not None and not outcome.budget and backend in state.budget_blocked:
        return f"budget reached ({backend})"
    return outcome.message


def log_outcome(who: str, outcome: Outcome, message: str, error: BaseException) -> None:
    """One log line per outcome, at the level the run has always used. Call it inside the `except` block,
    so `log.exception` has the traceback."""
    if outcome.unexpected:
        log.exception("%s: failed, %s", who, message)
    elif outcome.kind == "failed":
        log.error("%s: failed, %s", who, message)
    elif outcome.config_error:
        log.error("%s: deferred, configuration error: %s", who, message)
    elif outcome.budget:
        log.warning("%s: deferred, %s: %s", who, message, error)
    elif outcome.kind == "deferred":
        log.warning("%s: deferred, %s", who, message)
    elif outcome.kind == "interrupted":
        log.warning("%s: interrupted, back in inbox/", who)
    elif outcome.kind == "waiting":
        log.info("%s: waiting, %s", who, message)
    else:  # would_fetch
        log.info("%s: %s", who, message)


def apply_outcome(
    ideas: Path,
    note: Note,
    outcome: Outcome,
    state: RunState,
    item: ItemReport,
    *,
    dry_run: bool,
    now: datetime | None = None,
) -> list[Path]:
    """A document did not get published: record why in its report item and in the run state, and put the
    document where it belongs (a dry run moves nothing). Returns the touched idea-bucket paths."""
    message = outcome_message(outcome, state)
    item.status, item.message = outcome.kind, message
    backend = outcome.backend
    if backend is not None:
        state.blocked.add(backend)
        if outcome.budget or backend in state.budget_blocked:
            state.budget_blocked[backend] = state.budget_blocked.get(backend, 0) + 1
    if outcome.kind == "waiting":
        state.attempted -= 1  # it was not worked on, so it does not count against --limit
    if dry_run:
        return []
    if outcome.kind == "deferred":  # a temporary error: the working copy stays in output/ and says why
        return mark_deferred(ideas, note, message, now)
    if outcome.kind == "failed":
        return move_to_failed(
            ideas, note.output_path(ideas), message, doc_id=note.doc_id, doc_class=note.doctype.name, now=now
        )
    if outcome.kind in ("waiting", "interrupted"):  # leave it where the next run finds it
        return return_to_inbox(ideas, note)
    return []  # would_fetch: a dry run only
