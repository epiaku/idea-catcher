import pytest
from pydantic import ValidationError

from catcher.modules.llm.schemas import SCHEMAS, WebClipSummary, YoutubeSummary


def test_registry_has_every_output_schema():
    assert set(SCHEMAS) == {"NoteSummary", "ChatSummary", "WebClipSummary", "YoutubeSummary"}


def test_youtube_summary_requires_every_section():
    with pytest.raises(ValidationError):
        YoutubeSummary.model_validate({"title": "x"})


def test_web_clip_summary_needs_a_title_a_description_and_a_summary():
    with pytest.raises(ValidationError):
        WebClipSummary.model_validate({"title": "x", "description": "d", "summary": []})
    ok = WebClipSummary(
        title="t", description="d", summary=["a"], key_points=[], ideas_to_use=[], body="", tags=["todo"]
    )
    assert ok.summary == ["a"]
