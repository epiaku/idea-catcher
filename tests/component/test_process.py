import json

import pytest

from catcher.core.frontmatter import parse
from catcher.modules.llm.backends.fake import CANNED, FakeBackend
from catcher.modules.llm.service import UsageLimitReached
from catcher.modules.pipeline.glossary import Glossary, Term
from catcher.modules.pipeline.process import ProcessOptions, process_note


def test_note_becomes_a_valid_page(make_note, make_services):
    processed = process_note(make_note("note"), make_services(), ProcessOptions())
    assert processed.problems == []
    assert processed.filename == "20260927_a7b2c9_fake-note.md"
    assert processed.llm.profile == "notes"
    assert "A cleaned-up idea." in processed.page


def test_chat_uses_the_class_default_profile(make_note, make_services):
    chats = FakeBackend()
    note = make_note(
        "ai-chat", doc_id="cf81e40b020519ef", source="https://gemini.google.com/app/cf81e40b020519ef"
    )
    processed = process_note(note, make_services(chat_backend=chats), ProcessOptions())
    assert processed.llm.profile == "clippings"
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
            ProcessOptions(blocked_backends=frozenset({"openai"})),
        )
    assert chats.prompts == []


def test_a_web_clip_uses_its_own_prompt_the_clippings_profile_and_its_own_folder(make_note, make_services):
    chats = FakeBackend()
    note = make_note("web-clip", doc_id="a1b2c3d4e5f6", source="https://example.com/blog/hugo")
    processed = process_note(note, make_services(chat_backend=chats), ProcessOptions())
    assert processed.problems == []
    assert processed.llm.profile == "clippings" and processed.llm.prompt_version == "web-clip-1"
    assert "<page>" in chats.prompts[0]  # the web-clip prompt, not the chat or note prompt
    assert "## 🔑 Key Points" in processed.page and "Fake Web Clip" in processed.page


def test_a_note_is_sent_to_the_llm_with_the_glossary(make_note, make_services):
    notes = FakeBackend()
    services = make_services(note_backend=notes)
    services.glossary = Glossary((Term("epiaku", ("epicu",)),))
    process_note(make_note("note"), services, ProcessOptions())
    assert "- epiaku (often heard as: epicu)" in notes.prompts[0]


def test_other_classes_do_not_get_the_glossary(make_note, make_services):
    chats = FakeBackend()
    services = make_services(chat_backend=chats)
    services.glossary = Glossary((Term("epiaku", ("epicu",)),))
    note = make_note(
        "ai-chat", doc_id="cf81e40b020519ef", source="https://gemini.google.com/app/cf81e40b020519ef"
    )
    process_note(note, services, ProcessOptions())
    assert "epicu" not in chats.prompts[0]


def test_a_notes_original_language_goes_into_the_frontmatter(make_note, make_services):
    reply = json.dumps({**CANNED["note"], "language": "NL"})
    processed = process_note(
        make_note("note"), make_services(note_backend=FakeBackend([reply])), ProcessOptions()
    )
    assert processed.problems == []
    assert parse(processed.page).fm["language"] == "nl"


def test_a_note_without_a_language_has_no_language_line(make_note, make_services):
    processed = process_note(make_note("note"), make_services(), ProcessOptions())
    assert "language" not in parse(processed.page).fm


def test_other_classes_have_no_language_line(make_note, make_services):
    note = make_note("ai-chat", doc_id="cf81e40b020519ef")
    processed = process_note(note, make_services(chat_backend=FakeBackend()), ProcessOptions())
    assert "language" not in parse(processed.page).fm


def test_the_business_context_is_only_for_the_youtube_prompts(make_note, make_services):
    chats = FakeBackend()
    services = make_services(chat_backend=chats)
    services.context = "- Epiaku makes demos."
    note = make_note(
        "ai-chat", doc_id="cf81e40b020519ef", source="https://gemini.google.com/app/cf81e40b020519ef"
    )
    process_note(note, services, ProcessOptions())
    assert "Epiaku makes demos" not in chats.prompts[0]
