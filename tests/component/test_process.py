import json

import pytest

from catcher.modules.llm.backends.fake import CANNED, FakeBackend
from catcher.modules.llm.service import UsageLimitReached
from catcher.modules.pipeline.process import ProcessOptions, process_note


def test_note_becomes_a_valid_page(make_note, make_services):
    processed = process_note(make_note("note"), make_services(), ProcessOptions())
    assert processed.problems == []
    assert processed.filename == "20260927_a7b2c9_fake-note.md"
    assert processed.llm.profile == "free-fast"
    assert "A cleaned-up idea." in processed.page


def test_chat_uses_the_class_default_profile(make_note, make_services):
    chats = FakeBackend()
    note = make_note(
        "ai-chat", doc_id="cf81e40b020519ef", source="https://gemini.google.com/app/cf81e40b020519ef"
    )
    processed = process_note(note, make_services(chat_backend=chats), ProcessOptions())
    assert processed.llm.profile == "claude-sub-evening"
    assert len(chats.prompts) == 1


def test_requested_profile_wins(make_note, make_services):
    notes = FakeBackend()
    note = make_note("ai-chat", doc_id="cf81e40b020519ef")
    processed = process_note(note, make_services(note_backend=notes), ProcessOptions(profile="fake"))
    assert processed.llm.profile == "fake"
    assert len(notes.prompts) == 1


def test_capture_tags_are_merged_and_unknown_tags_dropped(make_note, make_services):
    reply = json.dumps({**CANNED["note"], "tags": ["app-idea", "nonsense"]})
    note = make_note("note", tags=["clippings", "obsidian"])
    processed = process_note(note, make_services(note_backend=FakeBackend([reply])), ProcessOptions())
    assert "- obsidian" in processed.page
    assert processed.dropped_tags == ["nonsense"]


def test_page_without_an_idea_type_has_a_problem(make_note, make_services):
    reply = json.dumps({**CANNED["note"], "tags": ["obsidian"]})
    processed = process_note(
        make_note("note"), make_services(note_backend=FakeBackend([reply])), ProcessOptions()
    )
    assert processed.problems == ["need exactly one idea-type tag, found 0"]


def test_blocked_backend_is_not_called(make_note, make_services):
    chats = FakeBackend()
    note = make_note("ai-chat", doc_id="cf81e40b020519ef")
    with pytest.raises(UsageLimitReached):
        process_note(
            note,
            make_services(chat_backend=chats),
            ProcessOptions(blocked_backends=frozenset({"claude-code"})),
        )
    assert chats.prompts == []
