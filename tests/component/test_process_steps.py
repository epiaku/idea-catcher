import pytest

from catcher.modules.llm.backends.fake import CANNED
from catcher.modules.llm.schemas import NoteSummary, YoutubeSummary
from catcher.modules.pipeline.process import (
    ProcessOptions,
    ask_llm,
    build_page,
    get_facts,
    process_note,
)

GEMINI = "https://gemini.google.com/app/925d9b0b4ca21b63?is_sa=1"
YT_CHAT = (
    "**You**\n\nSummarize this YouTube video: https://www.youtube.com/watch?v=nGVZS_wUDGM\n\n---\n\n"
    "**Gemini**\n\n**Success Is Hard** by *Someone*\n\n| Views | 99M |\n"
)
CLASSES = ["note", "ai-chat", "youtube", "youtube-gemini"]


def make(name, make_note, tmp_path):
    if name == "youtube":
        return make_note(
            "youtube",
            doc_id="nGVZS_wUDGM",
            root=tmp_path,
            source="https://www.youtube.com/watch?v=nGVZS_wUDGM&list=PL1&t=1s",
            body="page scrape\n",
        )
    if name == "youtube-gemini":
        return make_note(
            "youtube-gemini", doc_id="nGVZS_wUDGM-gemini", root=tmp_path, source=GEMINI, body=YT_CHAT
        )
    return make_note(name, doc_id="cf81e40b020519ef", source="https://gemini.google.com/app/cf81e40b020519ef")


@pytest.mark.parametrize("name", CLASSES)
def test_the_three_steps_give_the_same_page_as_process_note(
    name, make_note, make_services, yt_facts, tmp_path
):
    note = make(name, make_note, tmp_path)
    opts = ProcessOptions()
    svc = make_services(facts=lambda vid: yt_facts)
    whole = process_note(note, svc, opts)
    profile = whole.llm.profile
    facts = get_facts(note, svc, opts)
    result = ask_llm(note, svc, profile, facts)
    stepped = build_page(note, svc, result, facts)
    assert (stepped.page, stepped.filename, stepped.problems) == (whole.page, whole.filename, whole.problems)
    assert stepped.facts == whole.facts


@pytest.mark.parametrize("name", ["note", "ai-chat"])
def test_get_facts_is_none_for_text_classes(name, make_note, make_services):
    assert get_facts(make_note(name), make_services(), ProcessOptions()) is None


def test_get_facts_is_none_for_gemini_and_raises_without_a_video(make_note, make_services, tmp_path):
    from catcher.modules.youtube.facts import FactsUnavailable

    assert get_facts(make("youtube-gemini", make_note, tmp_path), make_services(), ProcessOptions()) is None
    bad = make_note("youtube-gemini", root=tmp_path, source=GEMINI, body="nothing here\n")
    with pytest.raises(FactsUnavailable):
        get_facts(bad, make_services(), ProcessOptions())


@pytest.mark.parametrize("name", ["note", "youtube-gemini"])
def test_build_page_needs_no_backend(name, make_note, make_services, make_result, tmp_path):
    svc = make_services()

    def boom(_profile):
        raise AssertionError("build_page must not need a backend")

    svc.backends = boom
    note = make(name, make_note, tmp_path)
    if name == "note":
        result = make_result(NoteSummary(**CANNED["note"]))
    else:
        result = make_result(YoutubeSummary(**CANNED["youtube-gemini"]), version="youtube-gemini-7")
    built = build_page(note, svc, result, None)
    assert built.problems == [] and built.llm is result
