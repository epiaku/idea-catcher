"""The item's `warnings` line up as the dropped tags first, then the other warnings, one string each."""

from catcher.modules.queue.states import LlmMetrics
from catcher.modules.worker.handlers_pipeline import _warning_line
from catcher.modules.youtube.checks import SummaryWarning


def metrics(**changes) -> LlmMetrics:
    base = dict(
        profile="notes",
        backend="fake",
        model="fake",
        prompt_version="note-7",
        tokens_in=10,
        tokens_out=5,
        duration_ms=None,
        attempts=1,
        saved=False,
    )
    return LlmMetrics(**{**base, **changes})


def test_no_dropped_tags_and_no_warnings_is_none():
    assert metrics().warning_lines() is None


def test_the_dropped_tags_come_first_then_the_other_warnings():
    lines = metrics(dropped_tags=("a", "b"), warnings=("wrong_timestamp (low): at 12:00",)).warning_lines()
    assert lines == ["dropped tags: a, b", "wrong_timestamp (low): at 12:00"]


def test_a_summary_check_is_one_line_with_its_kind_severity_and_excerpt():
    warning = SummaryWarning("unsupported_claim", "medium", "x" * 300, "remove it")
    assert _warning_line(warning) == f"unsupported_claim (medium): {'x' * 200}"
