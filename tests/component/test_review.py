import json
from datetime import UTC, datetime

from catcher.modules.llm.backends.fake import CANNED, FakeBackend
from catcher.modules.llm.schemas import YoutubeSummary
from catcher.modules.pipeline.tags import load_tags
from catcher.modules.youtube.review import not_reviewed, python_checks, review_summary

NOW = datetime(2026, 9, 27, 21, 0, tzinfo=UTC)
SUMMARY = YoutubeSummary.model_validate(CANNED["youtube"])


def run_review(fake, facts, fake_profiles, text="SUMMARY TEXT"):
    return review_summary(
        text,
        facts,
        profile="fake",
        profiles=fake_profiles,
        backends=lambda p: fake,
        tags=load_tags(),
        now=NOW,
    )


def test_clean_review_is_ok(fake_profiles, yt_facts):
    fake = FakeBackend()
    outcome = run_review(fake, yt_facts, fake_profiles)
    assert outcome.status == "ok"
    assert outcome.issues == []
    assert outcome.summary.title == "Fake Video Summary"
    assert outcome.reviewed_at == "2026-09-27T21:00:00+00:00"
    assert "SUMMARY TEXT" in fake.prompts[0]
    assert "[6:50] I plan my week in Obsidian every Sunday." in fake.prompts[0]


def test_needs_attention_is_kept_and_high_issues_listed(fake_profiles, yt_facts):
    issue = {
        "kind": "unsupported_claim",
        "severity": "high",
        "excerpt": "Uses Notion",
        "evidence": None,
        "fix": "remove",
    }
    reply = json.dumps({"verdict": "needs_attention", "issues": [issue], "revised": CANNED["youtube"]})
    outcome = run_review(FakeBackend([reply]), yt_facts, fake_profiles)
    assert outcome.status == "needs_attention"
    assert [i.excerpt for i in outcome.high_issues()] == ["Uses Notion"]


def test_without_transcript_the_review_is_limited(fake_profiles, yt_facts):
    outcome = run_review(FakeBackend(), yt_facts.model_copy(update={"transcript": None}), fake_profiles)
    assert outcome.status == "limited"


def test_python_flags_timestamps_after_the_end_of_the_video(yt_facts):
    summary = SUMMARY.model_copy(update={"action_plan": ["At 25:00 do the review"]})
    [issue] = python_checks(summary, yt_facts)
    assert issue.kind == "wrong_fact"
    assert "25:00" in issue.fix and "21:00" in issue.fix


def test_python_flags_tools_that_are_not_in_the_transcript(yt_facts):
    summary = SUMMARY.model_copy(update={"tools": ["Obsidian", "Notion"]})
    [issue] = python_checks(summary, yt_facts)
    assert (issue.kind, issue.severity, issue.excerpt) == ("unsupported_claim", "low", "Notion")


def test_python_checks_are_added_to_the_review(fake_profiles, yt_facts):
    reply = json.dumps(
        {"verdict": "fixed", "issues": [], "revised": {**CANNED["youtube"], "tools": ["Notion"]}}
    )
    outcome = run_review(FakeBackend([reply]), yt_facts, fake_profiles)
    assert outcome.status == "fixed"
    assert [i.excerpt for i in outcome.issues] == ["Notion"]


def test_frontmatter_is_compact(fake_profiles, yt_facts):
    issue = {"kind": "format", "severity": "low", "excerpt": "x" * 500, "evidence": None, "fix": "y"}
    reply = json.dumps({"verdict": "fixed", "issues": [issue], "revised": CANNED["youtube"]})
    fm = run_review(FakeBackend([reply]), yt_facts, fake_profiles).frontmatter()
    assert fm["status"] == "fixed" and fm["model"] == "fake"
    assert len(fm["issues"][0]["excerpt"]) == 200


def test_not_reviewed():
    outcome = not_reviewed(SUMMARY)
    assert (outcome.status, outcome.llm, outcome.frontmatter()["status"]) == ("off", None, "off")
