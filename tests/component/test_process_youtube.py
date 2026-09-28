import json

import pytest

from catcher.core.frontmatter import Doc, dump, parse
from catcher.modules.llm.backends.fake import CANNED, FakeBackend
from catcher.modules.llm.service import UsageLimitReached
from catcher.modules.pipeline.doctypes import YOUTUBE
from catcher.modules.pipeline.process import ProcessOptions, process_note
from catcher.modules.youtube.facts import FactsUnavailable

GEMINI = "https://gemini.google.com/app/925d9b0b4ca21b63?is_sa=1"
YT_CHAT = (
    "**You**\n\nSummarize this YouTube video: https://www.youtube.com/watch?v=MBPHU7aaklM\n\n---\n\n"
    "**Gemini**\n\n**Success Is Hard** by *Someone*\n\n| Views | 99M |\n"
)


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
    processed = process_note(
        note, make_services(chat_backend=chats, facts=lambda vid: yt_facts), ProcessOptions()
    )
    assert processed.problems == []
    assert len(chats.prompts) == 1  # one call, whatever the class
    doc = parse(processed.page)
    assert doc.fm["video_id"] == "MBPHU7aaklM"
    assert doc.fm["source"] == "https://www.youtube.com/watch?v=MBPHU7aaklM"
    assert "| Views               | 1,400,000 |" in doc.body
    assert "| Channel Subscribers | 2,260,000 |" in doc.body
    assert "| Metrics As Of       | 2026-09-27 |" in doc.body
    assert "{{< youtube-lite MBPHU7aaklM `Success Is Hard Until You Build Systems Like This` >}}" in doc.body
    assert "[6:50] I plan my week in Obsidian every Sunday." in chats.prompts[0]
    assert processed.llm.prompt_version == "youtube-1"


def test_the_facts_come_back_with_the_page_and_nothing_is_written(
    make_note, make_services, yt_facts, tmp_path
):
    calls = []

    def fetch(vid):
        calls.append(vid)
        return yt_facts

    note = youtube_note(make_note, tmp_path)
    processed = process_note(note, make_services(facts=fetch), ProcessOptions())
    assert processed.facts == yt_facts and calls == ["MBPHU7aaklM"]
    assert sorted(p.name for p in note.path.parent.iterdir()) == [note.path.name]


def test_no_transcript_is_said_on_the_page(make_note, make_services, yt_facts, tmp_path):
    no_captions = yt_facts.model_copy(update={"transcript": None})
    chats = FakeBackend()
    note = youtube_note(make_note, tmp_path)
    processed = process_note(
        note, make_services(chat_backend=chats, facts=lambda vid: no_captions), ProcessOptions()
    )
    assert "No transcript was available" in processed.page
    assert "There is NO transcript" in chats.prompts[0]


def test_facts_failure_propagates(make_note, make_services, tmp_path):
    with pytest.raises(FactsUnavailable):
        process_note(youtube_note(make_note, tmp_path), make_services(), ProcessOptions())


def test_table_cells_are_escaped(make_note, make_services, yt_facts, tmp_path):
    tip = {"tip": "Use A | B", "explanation": "line one\nline two", "how_to_apply": "x"}
    reply = json.dumps({**CANNED["youtube"], "tips": [tip]})
    services = make_services(chat_backend=FakeBackend([reply]), facts=lambda vid: yt_facts)
    processed = process_note(youtube_note(make_note, tmp_path), services, ProcessOptions())
    assert "| Use A \\| B | line one line two | x |" in processed.page


def test_gemini_youtube_chat_is_converted_and_checked_in_one_call(
    make_note, make_services, yt_facts, tmp_path
):
    chats = FakeBackend()
    note = make_note(
        "youtube-gemini", doc_id="MBPHU7aaklM-gemini", root=tmp_path, source=GEMINI, body=YT_CHAT
    )
    processed = process_note(
        note, make_services(chat_backend=chats, facts=lambda vid: yt_facts), ProcessOptions()
    )
    assert processed.problems == []
    assert len(chats.prompts) == 1
    assert "Gemini web chat" in chats.prompts[0]
    assert "| Views | 99M |" in chats.prompts[0]
    assert "| Views               | 1,400,000 |" in processed.page
    assert parse(processed.page).fm["video_id"] == "MBPHU7aaklM"
    assert processed.llm.prompt_version == "youtube-gemini-1"


def test_a_warning_from_the_free_checks_shows_an_alert_on_the_page(
    make_note, make_services, yt_facts, tmp_path
):
    reply = json.dumps({**CANNED["youtube"], "tools": ["MadeUpTool"]})
    chats = FakeBackend([reply])
    processed = process_note(
        youtube_note(make_note, tmp_path),
        make_services(chat_backend=chats, facts=lambda vid: yt_facts),
        ProcessOptions(),
    )
    assert processed.problems == []
    assert len(processed.warnings) == 1 and processed.warnings[0].kind == "unsupported_claim"
    body = parse(processed.page).body
    assert body.startswith('{{% alert title="Check this page" color="warning" %}}')
    assert "- **unsupported_claim**: MadeUpTool → this tool was not found" in body
    assert "{{% /alert %}}" in body
    assert parse(processed.page).fm["warnings"][0]["kind"] == "unsupported_claim"


def test_a_clean_summary_has_no_alert_and_no_warnings_in_frontmatter(
    make_note, make_services, yt_facts, tmp_path
):
    chats = FakeBackend()
    processed = process_note(
        youtube_note(make_note, tmp_path),
        make_services(chat_backend=chats, facts=lambda vid: yt_facts),
        ProcessOptions(),
    )
    assert processed.warnings == []
    doc = parse(processed.page)
    assert "warnings" not in doc.fm
    assert "Check this page" not in doc.body


def test_a_blocked_backend_stops_before_fetching_facts(make_note, make_services, yt_facts, tmp_path):
    chats = FakeBackend()
    services = make_services(chat_backend=chats, facts=lambda vid: yt_facts)
    opts = ProcessOptions(blocked_backends=frozenset({"openai"}))
    with pytest.raises(UsageLimitReached):
        process_note(youtube_note(make_note, tmp_path), services, opts)
    assert chats.prompts == []


def test_sibling_page_is_linked(make_note, make_services, yt_facts, tmp_path):
    docs = tmp_path / "docs"
    other = docs / YOUTUBE.out_dir / "20260927_MBPHU7aaklM-gemini_success.md"
    other.parent.mkdir(parents=True)
    other.write_text(dump(Doc({"title": "x", "id": "MBPHU7aaklM-gemini"}, "x\n")))
    note = youtube_note(make_note, tmp_path)
    opts = ProcessOptions(docs_repo=docs)
    processed = process_note(note, make_services(facts=lambda vid: yt_facts), opts)
    assert "(../20260927_mbphu7aaklm-gemini_success/)" in processed.page
