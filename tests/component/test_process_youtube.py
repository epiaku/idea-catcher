import json

import pytest

from catcher.core.frontmatter import Doc, dump, parse
from catcher.modules.llm.backends.fake import CANNED, FakeBackend
from catcher.modules.pipeline.doctypes import YOUTUBE
from catcher.modules.pipeline.process import ProcessOptions, process_note
from catcher.modules.youtube.facts import FactsUnavailable

GEMINI = "https://gemini.google.com/app/925d9b0b4ca21b63?is_sa=1"
YT_CHAT = (
    "**You**\n\nSummarize this YouTube video: https://www.youtube.com/watch?v=MBPHU7aaklM\n\n---\n\n"
    "**Gemini**\n\n**Success Is Hard** by *Someone*\n\n| Views | 99M |\n"
)
NO_REVIEW = ProcessOptions(review=False)


def youtube_note(make_note, tmp_path):
    return make_note(
        "youtube",
        doc_id="MBPHU7aaklM",
        root=tmp_path,
        source="https://www.youtube.com/watch?v=MBPHU7aaklM&list=PL1&t=1s",
        body="page scrape\n",
    )


def test_youtube_page_has_python_metrics_and_embed(make_note, make_services, yt_facts, tmp_path):
    chats = FakeBackend()
    note = youtube_note(make_note, tmp_path)
    processed = process_note(note, make_services(chat_backend=chats, facts=lambda vid: yt_facts), NO_REVIEW)
    assert processed.problems == []
    doc = parse(processed.page)
    assert doc.fm["video_id"] == "MBPHU7aaklM"
    assert doc.fm["source"] == "https://www.youtube.com/watch?v=MBPHU7aaklM"
    assert "| Views               | 1,400,000 |" in doc.body
    assert "| Channel Subscribers | 2,260,000 |" in doc.body
    assert "| Metrics As Of       | 2026-09-27 |" in doc.body
    assert "{{< youtube-lite MBPHU7aaklM `Success Is Hard Until You Build Systems Like This` >}}" in doc.body
    assert "[6:50] I plan my week in Obsidian every Sunday." in chats.prompts[0]


def test_the_facts_come_back_with_the_page_and_nothing_is_written(
    make_note, make_services, yt_facts, tmp_path
):
    calls = []

    def fetch(vid):
        calls.append(vid)
        return yt_facts

    note = youtube_note(make_note, tmp_path)
    processed = process_note(note, make_services(facts=fetch), NO_REVIEW)
    assert processed.facts == yt_facts and calls == ["MBPHU7aaklM"]
    assert sorted(p.name for p in note.path.parent.iterdir()) == [note.path.name]


def test_no_transcript_is_said_on_the_page(make_note, make_services, yt_facts, tmp_path):
    no_captions = yt_facts.model_copy(update={"transcript": None})
    chats = FakeBackend()
    note = youtube_note(make_note, tmp_path)
    processed = process_note(
        note, make_services(chat_backend=chats, facts=lambda vid: no_captions), NO_REVIEW
    )
    assert "No transcript was available" in processed.page
    assert "There is NO transcript" in chats.prompts[0]


def test_facts_failure_propagates(make_note, make_services, tmp_path):
    with pytest.raises(FactsUnavailable):
        process_note(youtube_note(make_note, tmp_path), make_services(), NO_REVIEW)


def test_table_cells_are_escaped(make_note, make_services, yt_facts, tmp_path):
    tip = {"tip": "Use A | B", "explanation": "line one\nline two", "how_to_apply": "x"}
    reply = json.dumps({**CANNED["youtube"], "tips": [tip]})
    services = make_services(chat_backend=FakeBackend([reply]), facts=lambda vid: yt_facts)
    processed = process_note(youtube_note(make_note, tmp_path), services, NO_REVIEW)
    assert "| Use A \\| B | line one line two | x |" in processed.page


def test_gemini_youtube_chat_is_converted(make_note, make_services, yt_facts, tmp_path):
    chats = FakeBackend()
    note = make_note(
        "youtube-gemini", doc_id="MBPHU7aaklM-gemini", root=tmp_path, source=GEMINI, body=YT_CHAT
    )
    processed = process_note(note, make_services(chat_backend=chats, facts=lambda vid: yt_facts), NO_REVIEW)
    assert processed.problems == []
    assert "Gemini web chat" in chats.prompts[0]
    assert "| Views | 99M |" in chats.prompts[0]
    assert "| Views               | 1,400,000 |" in processed.page
    assert parse(processed.page).fm["video_id"] == "MBPHU7aaklM"


def test_sibling_page_is_linked(make_note, make_services, yt_facts, tmp_path):
    docs = tmp_path / "docs"
    other = docs / YOUTUBE.out_dir / "20260927_MBPHU7aaklM-gemini_success.md"
    other.parent.mkdir(parents=True)
    other.write_text(dump(Doc({"title": "x", "id": "MBPHU7aaklM-gemini"}, "x\n")))
    note = youtube_note(make_note, tmp_path)
    opts = ProcessOptions(review=False, docs_repo=docs)
    processed = process_note(note, make_services(facts=lambda vid: yt_facts), opts)
    assert "(../20260927_mbphu7aaklm-gemini_success/)" in processed.page
