import json

import pytest

from catcher.core.frontmatter import parse
from catcher.modules.llm.backends.fake import CANNED, FakeBackend
from catcher.modules.llm.service import UsageLimitReached
from catcher.modules.pipeline.process import ProcessOptions, process_note

GEMINI = "https://gemini.google.com/app/925d9b0b4ca21b63?is_sa=1"
YT_CHAT = (
    "**You**\n\nSummarize this YouTube video: https://www.youtube.com/watch?v=MBPHU7aaklM\n\n---\n\n"
    "**Gemini**\n\nGEMINI ANSWER\n"
)


def youtube_note(make_note, tmp_path):
    return make_note("youtube", doc_id="MBPHU7aaklM", root=tmp_path, source="https://youtu.be/MBPHU7aaklM")


def test_youtube_summary_is_reviewed_by_default(make_note, make_services, yt_facts, tmp_path):
    chats = FakeBackend()
    processed = process_note(
        youtube_note(make_note, tmp_path),
        make_services(chat_backend=chats, facts=lambda v: yt_facts),
        ProcessOptions(),
    )
    assert processed.problems == []
    assert len(chats.prompts) == 2
    assert "strict fact-checker" in chats.prompts[1]
    fm = parse(processed.page).fm
    assert fm["review"]["status"] == "ok"
    assert processed.review is not None and processed.review.status == "ok"


def test_review_can_be_switched_off(make_note, make_services, yt_facts, tmp_path):
    chats = FakeBackend()
    processed = process_note(
        youtube_note(make_note, tmp_path),
        make_services(chat_backend=chats, facts=lambda v: yt_facts),
        ProcessOptions(review=False),
    )
    assert len(chats.prompts) == 1
    assert parse(processed.page).fm["review"]["status"] == "off"


def test_needs_attention_shows_an_alert(make_note, make_services, yt_facts, tmp_path):
    issue = {
        "kind": "wrong_fact",
        "severity": "high",
        "excerpt": "Claims | 10 steps",
        "evidence": None,
        "fix": "It is 4",
    }
    review = json.dumps({"verdict": "needs_attention", "issues": [issue], "revised": CANNED["youtube"]})
    chats = FakeBackend([json.dumps(CANNED["youtube"]), review])
    processed = process_note(
        youtube_note(make_note, tmp_path),
        make_services(chat_backend=chats, facts=lambda v: yt_facts),
        ProcessOptions(),
    )
    assert processed.problems == []
    body = parse(processed.page).body
    assert body.startswith('{{% alert title="Review: needs attention" color="warning" %}}')
    assert "- **wrong_fact**: Claims \\| 10 steps → It is 4" in body
    assert "{{% /alert %}}" in body


def test_gemini_chat_is_reviewed_directly(make_note, make_services, yt_facts, tmp_path):
    chats = FakeBackend()
    note = make_note(
        "youtube-gemini", doc_id="MBPHU7aaklM-gemini", root=tmp_path, source=GEMINI, body=YT_CHAT
    )
    processed = process_note(
        note, make_services(chat_backend=chats, facts=lambda v: yt_facts), ProcessOptions()
    )
    assert len(chats.prompts) == 1
    assert "strict fact-checker" in chats.prompts[0]
    assert "GEMINI ANSWER" in chats.prompts[0]
    assert processed.llm.prompt_version == "review-1"
    assert parse(processed.page).fm["review"]["status"] == "ok"


def test_gemini_chat_without_review_is_converted(make_note, make_services, yt_facts, tmp_path):
    chats = FakeBackend()
    note = make_note(
        "youtube-gemini", doc_id="MBPHU7aaklM-gemini", root=tmp_path, source=GEMINI, body=YT_CHAT
    )
    processed = process_note(
        note, make_services(chat_backend=chats, facts=lambda v: yt_facts), ProcessOptions(review=False)
    )
    assert processed.llm.prompt_version == "youtube-from-gemini-1"


def test_blocked_review_backend_is_not_called(make_note, make_services, yt_facts, tmp_path):
    notes, chats = FakeBackend(), FakeBackend()
    services = make_services(note_backend=notes, chat_backend=chats, facts=lambda v: yt_facts)
    opts = ProcessOptions(profile="fake", blocked_backends=frozenset({"openai"}))
    with pytest.raises(UsageLimitReached):
        process_note(youtube_note(make_note, tmp_path), services, opts)
    assert notes.prompts == [] and chats.prompts == []


def test_review_profile_can_be_chosen(make_note, make_services, yt_facts, tmp_path):
    notes, chats = FakeBackend(), FakeBackend()
    services = make_services(note_backend=notes, chat_backend=chats, facts=lambda v: yt_facts)
    process_note(youtube_note(make_note, tmp_path), services, ProcessOptions(review_profile="fake"))
    assert len(chats.prompts) == 1 and len(notes.prompts) == 1
