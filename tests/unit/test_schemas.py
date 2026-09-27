import pytest
from pydantic import ValidationError

from catcher.modules.llm.schemas import SCHEMAS, Review, YoutubeSummary


def test_registry_has_every_output_schema():
    assert set(SCHEMAS) == {"NoteSummary", "ChatSummary", "YoutubeSummary", "Review"}


def test_youtube_summary_requires_every_section():
    with pytest.raises(ValidationError):
        YoutubeSummary.model_validate({"title": "x"})


def test_review_rejects_unknown_issue_kinds():
    with pytest.raises(ValidationError):
        Review.model_validate(
            {
                "verdict": "ok",
                "issues": [{"kind": "typo", "severity": "low", "excerpt": "a", "fix": "b"}],
                "revised": {},
            }
        )
