import pytest
from pydantic import ValidationError

from catcher.modules.llm.schemas import (
    SCHEMAS,
    ChapterItem,
    GeminiMetrics,
    NoteSummary,
    WebClipSummary,
    YoutubeSummary,
)


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


YOUTUBE = {
    "title": "t",
    "creator": "c",
    "description": "d",
    "summary": "s",
    "main_purpose": "m",
    "key_examples": [],
    "action_plan": [],
    "tools": [],
    "tips": [],
    "channel_application": "a",
    "tags": ["todo"],
}


def test_youtube_chapters_keep_the_usable_ones_and_drop_the_rest_without_failing():
    given = [
        {"time": "0:00", "title": "Intro"},
        {"time": " 1:02:05 ", "title": "  Wrap   up "},
        {"time": "soon", "title": "Not a time"},
        {"time": "3:10", "title": "  "},
        {"title": "no time"},
        "a plain string",
    ]
    chapters = YoutubeSummary.model_validate({**YOUTUBE, "chapters": given}).chapters
    assert chapters == [ChapterItem(time="0:00", title="Intro"), ChapterItem(time="1:02:05", title="Wrap up")]


def test_youtube_chapters_default_to_none_and_a_non_list_is_ignored():
    assert YoutubeSummary.model_validate(YOUTUBE).chapters == []
    assert YoutubeSummary.model_validate({**YOUTUBE, "chapters": "Not available"}).chapters == []


def test_gemini_metrics_are_kept_as_written_and_not_available_becomes_nothing():
    m = GeminiMetrics.model_validate(
        {"as_of": " October 1,  2026 ", "views": 9954, "likes": "170", "subscribers": "Not available"}
    )
    assert (m.as_of, m.views, m.likes, m.subscribers) == ("October 1, 2026", "9,954", "170", None)
    assert m.has_values


def test_gemini_metrics_with_nothing_in_them_have_no_values():
    for empty in ({}, {"views": None, "likes": "", "subscribers": "N/A", "as_of": "unknown"}):
        assert not GeminiMetrics.model_validate(empty).has_values
    assert YoutubeSummary.model_validate(YOUTUBE).metrics is None
