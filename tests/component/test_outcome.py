import pytest

from catcher.modules.llm.profiles import UnknownProfile
from catcher.modules.llm.service import (
    BackendUnavailable,
    BudgetExhausted,
    InputRejected,
    InvalidOutput,
    UsageLimitReached,
)
from catcher.modules.pipeline.outcome import classify
from catcher.modules.youtube.facts import FactsDeferred, FactsUnavailable, FetchSkipped


@pytest.mark.parametrize(
    ("error", "kind", "backend", "budget", "config_error", "unexpected", "message"),
    [
        (KeyboardInterrupt(), "interrupted", None, False, False, False, "back in inbox/"),
        (
            BudgetExhausted("out", "openai"),
            "deferred",
            "openai",
            True,
            False,
            False,
            "budget reached (openai)",
        ),
        (
            UsageLimitReached("slow down", "free"),
            "deferred",
            "free",
            False,
            False,
            False,
            "usage limit (free): slow down",
        ),
        (BackendUnavailable("down"), "deferred", None, False, False, False, "down"),
        (UnknownProfile("no such profile"), "deferred", None, False, True, False, "no such profile"),
        (InvalidOutput("bad json"), "failed", None, False, False, False, "bad json"),
        (InputRejected("too long"), "failed", None, False, False, False, "too long"),
        (FactsDeferred("gap"), "waiting", None, False, False, False, "gap (back in inbox/)"),
        (FetchSkipped("dry run"), "would_fetch", None, False, False, False, "dry run"),
        (FactsUnavailable("gone"), "deferred", None, False, False, False, "gone"),
        (RuntimeError("boom"), "failed", None, False, False, True, "unexpected RuntimeError: boom"),
    ],
)
def test_each_exception_maps_to_its_outcome(error, kind, backend, budget, config_error, unexpected, message):
    outcome = classify(error)
    assert outcome.kind == kind
    assert outcome.backend == backend
    assert outcome.budget is budget
    assert outcome.config_error is config_error
    assert outcome.unexpected is unexpected
    assert outcome.message == message


def test_a_budget_error_is_not_classified_as_a_plain_usage_limit():
    outcome = classify(BudgetExhausted("out", "openai"))
    assert outcome.budget is True
    assert "usage limit" not in outcome.message


def test_facts_deferred_and_fetch_skipped_are_not_plain_facts_unavailable():
    assert classify(FactsDeferred("x")).kind == "waiting"
    assert classify(FetchSkipped("x")).kind == "would_fetch"
    assert classify(FactsUnavailable("x")).kind == "deferred"


def test_an_unknown_exception_is_a_failed_outcome_naming_its_type():
    outcome = classify(KeyError("k"))
    assert outcome.kind == "failed"
    assert outcome.unexpected is True
    assert outcome.message.startswith("unexpected KeyError")
