import pytest
from pydantic import ValidationError

from catcher.modules.llm.schemas import SCHEMAS, NoteSummary, WebClipSummary, YoutubeSummary


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


NOTE = {"title": "t", "description": "d", "body": "b", "tags": ["todo"]}


@pytest.mark.parametrize(
    "given, expected",
    [
        ("nl", "nl"),
        (" NL ", "nl"),
        ("en", "en"),
        (None, None),
        ("Dutch", None),
        ("dut", None),
        ("", None),
        (5, None),
    ],
)
def test_note_language_is_a_two_letter_code_or_nothing(given, expected):
    assert NoteSummary.model_validate({**NOTE, "language": given}).language == expected


def test_a_note_without_a_language_is_still_valid():
    assert NoteSummary.model_validate(NOTE).language is None
