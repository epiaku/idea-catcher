import json

import jinja2
import pytest

from catcher.modules.llm.backends.fake import CANNED, FakeBackend
from catcher.modules.llm.prompts import render_prompt
from catcher.modules.llm.schemas import NoteSummary
from catcher.modules.llm.service import (
    BackendUnavailable,
    InvalidOutput,
    LlmRequest,
    extract_json,
    reason,
)

FENCE = "`" * 3


def note_request(prompt_tags: dict) -> LlmRequest:
    return LlmRequest(
        task="note",
        input={
            "title_hint": "YouTube walks",
            "body": "Create YouTube content walking around",
            "source": None,
            "tags": prompt_tags,
            "capture_tags": ["obsidian"],
        },
        schema_name="NoteSummary",
        profile="fake",
    )


def test_valid_reply_is_parsed(fake_profiles, prompt_tags):
    fake = FakeBackend()
    result = reason(note_request(prompt_tags), profiles=fake_profiles, backends=lambda p: fake)
    assert isinstance(result.output, NoteSummary)
    assert result.output.title == CANNED["note"]["title"]
    assert (result.attempts, result.backend, result.prompt_version) == (1, "fake", "note-1")
    assert result.usage.tokens_in and result.usage.tokens_out


def test_prompt_contains_note_tags_and_schema(fake_profiles, prompt_tags):
    fake = FakeBackend()
    reason(note_request(prompt_tags), profiles=fake_profiles, backends=lambda p: fake)
    prompt = fake.prompts[0]
    assert "Create YouTube content walking around" in prompt
    assert "Idea-type tags: app-idea, tech-note" in prompt
    assert "obsidian" in prompt
    assert '"title"' in prompt and "JSON Schema" in prompt


def test_invalid_reply_is_retried_once_with_the_error(fake_profiles, prompt_tags):
    fake = FakeBackend(["not json", json.dumps(CANNED["note"])])
    result = reason(note_request(prompt_tags), profiles=fake_profiles, backends=lambda p: fake)
    assert result.attempts == 2
    assert "Your previous reply was rejected" in fake.prompts[1]
    assert "no JSON object" in fake.prompts[1]


def test_two_invalid_replies_raise(fake_profiles, prompt_tags):
    fake = FakeBackend(['{"title": ""}', "{}"])
    with pytest.raises(InvalidOutput, match="note: invalid output after 2 attempts"):
        reason(note_request(prompt_tags), profiles=fake_profiles, backends=lambda p: fake)


def test_reply_with_prose_and_code_fence_is_accepted(fake_profiles, prompt_tags):
    wrapped = f"Sure!\n{FENCE}json\n{json.dumps(CANNED['note'])}\n{FENCE}\nDone."
    fake = FakeBackend([wrapped])
    assert reason(note_request(prompt_tags), profiles=fake_profiles, backends=lambda p: fake).attempts == 1


def test_backend_errors_are_not_retried(fake_profiles, prompt_tags):
    fake = FakeBackend([BackendUnavailable("down")])
    with pytest.raises(BackendUnavailable):
        reason(note_request(prompt_tags), profiles=fake_profiles, backends=lambda p: fake)
    assert len(fake.prompts) == 1


def test_missing_prompt_variable_fails_loudly():
    with pytest.raises(jinja2.UndefinedError):
        render_prompt("note", {"body": "x"})


def test_ai_chat_prompt_renders(prompt_tags):
    text, version = render_prompt(
        "ai-chat",
        {
            "title_hint": "New chat",
            "body": "**You**\n\nq",
            "source": None,
            "tags": prompt_tags,
            "capture_tags": [],
        },
    )
    assert version == "ai-chat-1"
    assert 'Never "New chat"' in text


def test_extract_json_without_object():
    with pytest.raises(ValueError):
        extract_json("no braces")


def test_web_clip_prompt_renders(prompt_tags):
    text, version = render_prompt(
        "web-clip",
        {
            "title_hint": "Hugo shortcodes explained",
            "body": "Shortcodes are snippets you call from Markdown.",
            "source": "https://example.com/blog/hugo",
            "tags": prompt_tags,
            "capture_tags": ["hugo"],
        },
    )
    assert version == "web-clip-1"
    assert "Shortcodes are snippets" in text and "https://example.com/blog/hugo" in text
    assert "menus, cookie notices" in text and "Do not copy long passages" in text
