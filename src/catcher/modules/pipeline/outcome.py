"""What a failed or interrupted document means for the run: one pure mapping from exception to outcome."""

from dataclasses import dataclass
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
from catcher.modules.youtube.facts import FactsDeferred, FactsUnavailable, FetchSkipped

OutcomeKind = Literal["deferred", "failed", "waiting", "would_fetch", "interrupted"]


@dataclass(frozen=True)
class Outcome:
    kind: OutcomeKind
    message: str
    backend: str | None = None
    budget: bool = False
    config_error: bool = False
    unexpected: bool = False  # only the "anything else" branch: the run loop logs a traceback for it


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
