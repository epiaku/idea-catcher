from catcher.modules.llm.backends.fake import CANNED
from catcher.modules.llm.schemas import YoutubeSummary
from catcher.modules.youtube.checks import check_summary

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
