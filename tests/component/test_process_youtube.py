import json

import pytest

from catcher.core.frontmatter import parse
from catcher.modules.llm.backends.fake import CANNED, FakeBackend
from catcher.modules.llm.service import UsageLimitReached
from catcher.modules.pipeline.process import ProcessOptions, process_note
from catcher.modules.youtube.facts import Chapter, FactsUnavailable

GEMINI = "https://gemini.google.com/app/925d9b0b4ca21b63?is_sa=1"
YT_CHAT = (
    "**You**\n\nSummarize this YouTube video: https://www.youtube.com/watch?v=nGVZS_wUDGM\n\n---\n\n"
    "**Gemini**\n\n**Success Is Hard** by *Someone*\n\n| Views | 99M |\n"
)


def youtube_note(make_note, tmp_path):
    return make_note(
        "youtube",
        doc_id="nGVZS_wUDGM",
        root=tmp_path,
        source="https://www.youtube.com/watch?v=nGVZS_wUDGM&list=PL1&t=1s",
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
    assert doc.fm["video_id"] == "nGVZS_wUDGM"
    assert doc.fm["source"] == "https://www.youtube.com/watch?v=nGVZS_wUDGM"
    assert "| Views               | 8,901 |" in doc.body
    assert "| Channel Subscribers | 131,000 |" in doc.body
    assert "| Metrics As Of       | 2026-09-30 |" in doc.body
    assert "| Published           | 2026-09-28 |" in doc.body  # the upload date, from the facts
    assert (
        "{{< youtube-lite nGVZS_wUDGM `I blew up a coaching business to prove its not luck` >}}" in doc.body
    )
    assert "[6:50] stadium, and make a video about getting" in chats.prompts[0]
    assert processed.llm.prompt_version == "youtube-5"
    assert "## 🛠️ Tech Stack" in doc.body and "Tools and Services" not in doc.body


def test_the_facts_come_back_with_the_page_and_nothing_is_written(
    make_note, make_services, yt_facts, tmp_path
):
    calls = []

    def fetch(vid):
        calls.append(vid)
        return yt_facts

    note = youtube_note(make_note, tmp_path)
    processed = process_note(note, make_services(facts=fetch), ProcessOptions())
    assert processed.facts == yt_facts and calls == ["nGVZS_wUDGM"]
    assert sorted(p.name for p in note.path.parent.iterdir()) == [note.path.name]


def test_no_transcript_defers_without_calling_the_llm(make_note, make_services, yt_facts, tmp_path):
    no_captions = yt_facts.model_copy(update={"transcript": None})
    chats = FakeBackend()
    note = youtube_note(make_note, tmp_path)
    with pytest.raises(FactsUnavailable):
        process_note(note, make_services(chat_backend=chats, facts=lambda vid: no_captions), ProcessOptions())
    assert chats.prompts == []  # no transcript, no point paying for an LLM call


def test_facts_failure_propagates(make_note, make_services, tmp_path):
    with pytest.raises(FactsUnavailable):
        process_note(youtube_note(make_note, tmp_path), make_services(), ProcessOptions())


def test_table_cells_are_escaped(make_note, make_services, yt_facts, tmp_path):
    tip = {"tip": "Use A | B", "explanation": "line one\nline two", "how_to_apply": "x"}
    reply = json.dumps({**CANNED["youtube"], "tips": [tip]})
    services = make_services(chat_backend=FakeBackend([reply]), facts=lambda vid: yt_facts)
    processed = process_note(youtube_note(make_note, tmp_path), services, ProcessOptions())
    assert "| Use A \\| B | line one line two | x |" in processed.page


def test_gemini_youtube_chat_is_converted_in_one_call_with_no_youtube_api_call_at_all(
    make_note, make_services, tmp_path
):
    chats = FakeBackend()

    def no_facts(vid):
        raise AssertionError("youtube-gemini must never call the facts fetcher")

    note = make_note(
        "youtube-gemini", doc_id="nGVZS_wUDGM-gemini", root=tmp_path, source=GEMINI, body=YT_CHAT
    )
    processed = process_note(note, make_services(chat_backend=chats, facts=no_facts), ProcessOptions())
    assert processed.problems == []
    assert len(chats.prompts) == 1
    assert "Gemini web chat" in chats.prompts[0]
    assert "| Views | 99M |" in chats.prompts[0]
    assert "facts" not in chats.prompts[0].lower()
    assert "Metrics" not in processed.page  # no facts, so no metrics table on the page
    assert processed.facts is None
    assert parse(processed.page).fm["video_id"] == "nGVZS_wUDGM"
    assert processed.llm.prompt_version == "youtube-gemini-6"
    assert "## 🛠️ Tech Stack" in processed.page and "Tools and Services" not in processed.page


def test_gemini_note_with_no_video_id_fails(make_note, make_services, tmp_path):
    note = make_note(
        "youtube-gemini",
        doc_id="no-video",
        root=tmp_path,
        source=GEMINI,
        body="**You**\n\nSummarize this\n\n---\n\n**Gemini**\n\nno video here\n",
    )
    with pytest.raises(FactsUnavailable):
        process_note(note, make_services(), ProcessOptions())


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


def test_a_youtube_page_never_links_to_a_page_for_the_same_video(
    make_note, make_services, yt_facts, tmp_path
):
    """A direct clip and a Gemini chat about one video are separate pages. Neither gets a cross-reference."""
    for name in ("youtube", "youtube-gemini"):
        note = make_note(
            name,
            doc_id="nGVZS_wUDGM" + ("-gemini" if name == "youtube-gemini" else ""),
            root=tmp_path,
            source=GEMINI if name == "youtube-gemini" else "https://www.youtube.com/watch?v=nGVZS_wUDGM",
            body=YT_CHAT if name == "youtube-gemini" else "page scrape\n",
        )
        processed = process_note(note, make_services(facts=lambda vid: yt_facts), ProcessOptions())
        assert "also summarized" not in processed.page and "compare the two" not in processed.page


DESCRIPTION_LINK = (
    "https://story-os.ai/launch-room?video=nGVZS_wUDGM"  # the link in the test video's description
)


def link_reply(*links: tuple[str, str]) -> str:
    return json.dumps({**CANNED["youtube"], "links": [{"label": a, "url": b} for a, b in links]})


def test_chapters_from_the_facts_become_an_outline_with_timestamps(
    make_note, make_services, yt_facts, tmp_path
):
    facts = yt_facts.model_copy(
        update={
            "chapters": [Chapter(start_s=0, title="What is RAG?"), Chapter(start_s=3725, title="Wrap up")]
        }
    )
    processed = process_note(
        youtube_note(make_note, tmp_path),
        make_services(chat_backend=FakeBackend(), facts=lambda vid: facts),
        ProcessOptions(),
    )
    body = parse(processed.page).body
    assert "## 🗂️ Chapters\n\n- 0:00 What is RAG?\n- 1:02:05 Wrap up\n" in body
    assert body.index("## 📊 Metrics") < body.index("## 🗂️ Chapters") < body.index("## 🎯 Main Purpose")


def test_a_video_without_chapters_has_no_chapters_section(make_note, make_services, yt_facts, tmp_path):
    assert yt_facts.chapters == []
    processed = process_note(
        youtube_note(make_note, tmp_path),
        make_services(chat_backend=FakeBackend(), facts=lambda vid: yt_facts),
        ProcessOptions(),
    )
    assert "Chapters" not in processed.page


def test_only_links_that_are_in_the_description_reach_the_page(make_note, make_services, yt_facts, tmp_path):
    reply = link_reply(("Launch [room]", DESCRIPTION_LINK), ("Invented repo", "https://example.com/made-up"))
    processed = process_note(
        youtube_note(make_note, tmp_path),
        make_services(chat_backend=FakeBackend([reply]), facts=lambda vid: yt_facts),
        ProcessOptions(),
    )
    body = parse(processed.page).body
    assert f"## 🔗 Links\n\n- [Launch (room)]({DESCRIPTION_LINK})\n" in body
    assert "made-up" not in processed.page
    assert (
        body.index("## 📺 Channel Application")
        < body.index("## 🔗 Links")
        < body.index("## 📄 YouTube Source")
    )


def test_no_links_means_no_links_section(make_note, make_services, yt_facts, tmp_path):
    processed = process_note(
        youtube_note(make_note, tmp_path),
        make_services(chat_backend=FakeBackend(), facts=lambda vid: yt_facts),
        ProcessOptions(),
    )
    assert "Links" not in processed.page


def test_the_gemini_page_has_no_chapters_or_links_and_its_prompt_keeps_advice_out_of_the_stack(
    make_note, make_services, tmp_path
):
    chats = FakeBackend([link_reply(("Anything", "https://example.com/x"))])
    note = make_note(
        "youtube-gemini", doc_id="nGVZS_wUDGM-gemini", root=tmp_path, source=GEMINI, body=YT_CHAT
    )
    processed = process_note(note, make_services(chat_backend=chats), ProcessOptions())
    assert "Links" not in processed.page and "Chapters" not in processed.page  # nothing to check them against
    prompt = chats.prompts[0]
    assert "from its Tech Stack and Action Plan sections" in prompt
    assert "Do not take tools from Gemini's Channel Application advice" in prompt
    assert "not from the Channel Application advice" in prompt


CONTEXT = "- Epiaku makes Vibe Coding Tech Stack demos for solo builders."


def test_only_the_direct_youtube_prompt_gets_the_business_context(
    make_note, make_services, yt_facts, tmp_path
):
    direct = FakeBackend()
    services = make_services(chat_backend=direct, facts=lambda vid: yt_facts)
    services.context = CONTEXT
    process_note(youtube_note(make_note, tmp_path), services, ProcessOptions())
    prompt = direct.prompts[0]
    assert f"About Epiaku (only for channel_application):\n{CONTEXT}" in prompt
    assert "using the context about Epiaku above" in prompt
    assert "Give 2 to 4 concrete suggestions" in prompt and "instead of forcing a fit" in prompt
    assert "as a Markdown bulleted list" in prompt

    gemini = FakeBackend()
    services = make_services(chat_backend=gemini)
    services.context = CONTEXT
    note = make_note(
        "youtube-gemini", doc_id="nGVZS_wUDGM-gemini", root=tmp_path, source=GEMINI, body=YT_CHAT
    )
    process_note(note, services, ProcessOptions())
    text = gemini.prompts[0]
    assert "About Epiaku" not in text and CONTEXT not in text  # Gemini's advice is only restructured
    assert "Gemini's own advice for applying the video to Epiaku, in Gemini's words" in text
    assert "Do not add ideas of your own" in text and "as a Markdown bulleted list" in text


def test_without_a_context_the_prompts_keep_the_one_line_description(
    make_note, make_services, yt_facts, tmp_path
):
    direct = FakeBackend()
    services = make_services(chat_backend=direct, facts=lambda vid: yt_facts)
    assert services.context == ""
    process_note(youtube_note(make_note, tmp_path), services, ProcessOptions())
    prompt = direct.prompts[0]
    assert "About Epiaku" not in prompt and "context about Epiaku" not in prompt
    assert "Vibe Coding Tech Stack demos, and building phone apps or small SaaS applications" in prompt


def chapters_reply(*chapters: tuple[str, str]) -> str:
    return json.dumps({**CANNED["youtube"], "chapters": [{"time": t, "title": n} for t, n in chapters]})


def test_the_gemini_page_keeps_the_chapters_gemini_listed(make_note, make_services, tmp_path):
    chats = FakeBackend(
        [chapters_reply(("0:00", "What is RAG?"), ("1:36", "Preparing the Data"), ("bad", "x"))]
    )
    note = make_note(
        "youtube-gemini", doc_id="nGVZS_wUDGM-gemini", root=tmp_path, source=GEMINI, body=YT_CHAT
    )
    processed = process_note(note, make_services(chat_backend=chats), ProcessOptions())
    body = parse(processed.page).body
    assert (
        "## 🗂️ Chapters\n\n- 0:00 What is RAG?\n- 1:36 Preparing the Data\n" in body
    )  # the bad one is dropped
    assert "bad" not in body
    assert "- chapters: the chapters Gemini lists" in chats.prompts[0]


def test_the_direct_page_takes_chapters_from_the_facts_never_from_the_llm(
    make_note, make_services, yt_facts, tmp_path
):
    reply = chapters_reply(("0:00", "Invented by the LLM"))
    processed = process_note(
        youtube_note(make_note, tmp_path),
        make_services(chat_backend=FakeBackend([reply]), facts=lambda vid: yt_facts),
        ProcessOptions(),
    )
    assert yt_facts.chapters == []
    assert "Chapters" not in processed.page and "Invented" not in processed.page


GEMINI_METRICS = {
    "as_of": "October 1, 2026",
    "views": "9,954",
    "likes": "170",
    "subscribers": "Not available",
}


def metrics_reply(metrics) -> str:
    return json.dumps({**CANNED["youtube"], "metrics": metrics})


def gemini_note(make_note, tmp_path):
    return make_note(
        "youtube-gemini", doc_id="nGVZS_wUDGM-gemini", root=tmp_path, source=GEMINI, body=YT_CHAT
    )


def test_the_gemini_page_shows_the_metrics_gemini_wrote(make_note, make_services, tmp_path):
    chats = FakeBackend([metrics_reply(GEMINI_METRICS)])
    processed = process_note(
        gemini_note(make_note, tmp_path), make_services(chat_backend=chats), ProcessOptions()
    )
    body = parse(processed.page).body
    assert "## 📊 Metrics\n\n| Metric              | Value |" in body  # no extra note line under the heading
    assert "Reported by Gemini" not in body and "not checked" not in body
    assert "| Metrics As Of       | October 1, 2026 |" in body
    assert "| Views               | 9,954 |" in body and "| Likes               | 170 |" in body
    assert "| Channel Subscribers | Not available |" in body  # Gemini said so, so the row says so
    assert body.index("## 📝 Summary") < body.index("## 📊 Metrics") < body.index("## 🎯 Main Purpose")
    assert "- metrics: the values in Gemini's Metrics table" in chats.prompts[0]


def test_a_gemini_page_without_usable_metrics_has_no_metrics_section(make_note, make_services, tmp_path):
    for metrics in (None, {}, {"views": "Not available", "likes": None}):
        chats = FakeBackend([metrics_reply(metrics)])
        processed = process_note(
            gemini_note(make_note, tmp_path), make_services(chat_backend=chats), ProcessOptions()
        )
        assert "Metrics" not in processed.page


def test_the_direct_page_uses_the_real_counts_never_what_the_llm_says(
    make_note, make_services, yt_facts, tmp_path
):
    reply = metrics_reply({"views": "1", "likes": "2", "subscribers": "3", "as_of": "never"})
    processed = process_note(
        youtube_note(make_note, tmp_path),
        make_services(chat_backend=FakeBackend([reply]), facts=lambda vid: yt_facts),
        ProcessOptions(),
    )
    body = parse(processed.page).body
    assert "| Views               | 8,901 |" in body
    assert "never" not in body


def test_the_summary_prompt_ignores_promotional_descriptions_and_uses_the_transcript(
    make_note, make_services, yt_facts, tmp_path
):
    chats = FakeBackend()
    process_note(
        youtube_note(make_note, tmp_path),
        make_services(chat_backend=chats, facts=lambda vid: yt_facts),
        ProcessOptions(),
    )
    prompt = chats.prompts[0]
    assert "written from the transcript" in prompt
    assert "only links, a free offer" in prompt and "ignore them completely" in prompt
    assert "from the transcript and not from the description" in prompt
    assert "YouTube's own description condensed" not in prompt


def test_the_gemini_prompt_writes_about_the_video_and_lists_only_named_tools(
    make_note, make_services, tmp_path
):
    gemini = FakeBackend()
    note = make_note(
        "youtube-gemini", doc_id="nGVZS_wUDGM-gemini", root=tmp_path, source=GEMINI, body=YT_CHAT
    )
    process_note(note, make_services(chat_backend=gemini), ProcessOptions())
    text = gemini.prompts[0]
    assert 'do not write "Gemini says", "Gemini describes" or "Gemini suggests"' in text
    assert "never generic categories such as" in text


def test_the_direct_prompt_also_lists_only_named_tools(make_note, make_services, yt_facts, tmp_path):
    direct = FakeBackend()
    process_note(
        youtube_note(make_note, tmp_path),
        make_services(chat_backend=direct, facts=lambda vid: yt_facts),
        ProcessOptions(),
    )
    assert "never generic categories such as" in direct.prompts[0]
