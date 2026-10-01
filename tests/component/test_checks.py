from catcher.modules.llm.backends.fake import CANNED
from catcher.modules.llm.schemas import Link, YoutubeSummary
from catcher.modules.youtube.checks import MAX_LINKS, check_summary, verified_links
from catcher.modules.youtube.facts import Segment

SUMMARY = YoutubeSummary.model_validate(CANNED["youtube"])


def test_clean_summary_has_no_warnings(yt_facts):
    assert check_summary(SUMMARY, yt_facts) == []


def test_flags_timestamps_after_the_end_of_the_video(yt_facts):
    summary = SUMMARY.model_copy(update={"action_plan": ["At 25:00 do the review"]})
    [warning] = check_summary(summary, yt_facts)
    assert warning.kind == "wrong_timestamp"
    assert "25:00" in warning.fix and "11:50" in warning.fix


def test_flags_tools_that_are_not_in_the_transcript(yt_facts):
    summary = SUMMARY.model_copy(update={"tools": ["YouTube", "Notion"]})
    [warning] = check_summary(summary, yt_facts)
    assert (warning.kind, warning.severity, warning.excerpt) == ("unsupported_claim", "low", "Notion")


def test_without_a_duration_timestamps_are_not_checked(yt_facts):
    summary = SUMMARY.model_copy(update={"action_plan": ["At 99:00 do the review"]})
    facts = yt_facts.model_copy(update={"duration_s": None})
    assert check_summary(summary, facts) == []


def test_without_a_transcript_tools_are_not_checked(yt_facts):
    summary = SUMMARY.model_copy(update={"tools": ["SomethingMadeUp"]})
    facts = yt_facts.model_copy(update={"transcript": None})
    assert check_summary(summary, facts) == []


def test_frontmatter_is_compact():
    from catcher.modules.youtube.checks import SummaryWarning

    warning = SummaryWarning(kind="unsupported_claim", severity="low", excerpt="x" * 500, fix="y" * 500)
    fm = warning.frontmatter()
    assert len(fm["excerpt"]) == 200 and len(fm["fix"]) == 300


DESCRIPTION = "Code: https://github.com/a/b\nData: https://example.com/data\nSocial: https://twitter.com/x"


def test_verified_links_keeps_only_urls_that_are_in_the_description():
    links = [
        Link(label="Code", url="https://github.com/a/b"),
        Link(label="Invented", url="https://github.com/a/other"),
        Link(label="Not a web link", url="ftp://example.com/data"),
        Link(label="", url="https://example.com/data"),
    ]
    assert verified_links(links, DESCRIPTION) == [Link(label="Code", url="https://github.com/a/b")]


def test_verified_links_tidies_labels_and_drops_duplicates():
    links = [
        Link(label="  The [code]\n repo ", url=" https://github.com/a/b "),
        Link(label="Again", url="https://github.com/a/b"),
    ]
    assert verified_links(links, DESCRIPTION) == [Link(label="The (code) repo", url="https://github.com/a/b")]


def test_verified_links_without_a_description_keeps_nothing():
    assert verified_links([Link(label="Code", url="https://github.com/a/b")], None) == []
    assert verified_links([Link(label="Code", url="https://github.com/a/b")], "") == []


def test_verified_links_is_capped():
    urls = [f"https://example.com/{n}" for n in range(MAX_LINKS + 3)]
    links = [Link(label=f"L{n}", url=u) for n, u in enumerate(urls)]
    assert len(verified_links(links, " ".join(urls))) == MAX_LINKS


def test_a_tool_whose_name_the_captions_split_in_two_words_is_found(yt_facts):
    facts = yt_facts.model_copy(
        update={"transcript": [Segment(start_s=4, text="it is called a type form, one of these")]}
    )
    summary = SUMMARY.model_copy(update={"tools": ["Typeform"]})
    assert check_summary(summary, facts) == []


def test_squashing_does_not_hide_a_tool_that_is_really_missing(yt_facts):
    facts = yt_facts.model_copy(
        update={"transcript": [Segment(start_s=4, text="it is called a type form, one of these")]}
    )
    summary = SUMMARY.model_copy(update={"tools": ["Typeform", "Notion"]})
    [warning] = check_summary(summary, facts)
    assert warning.excerpt == "Notion"
