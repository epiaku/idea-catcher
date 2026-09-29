from datetime import date

import pytest
from typer.testing import CliRunner

import catcher.cli as cli
from catcher.modules.youtube import facts as facts_mod
from catcher.modules.youtube.facts import FactsUnavailable, Segment, YoutubeFacts, fetch_facts, fmt_ts

INFO = {
    "title": "T",
    "channel": "C",
    "channel_follower_count": 2260000,
    "view_count": 1400000,
    "like_count": 46000,
    "upload_date": "20260920",
    "duration": 1260.0,
    "description": "D",
    "chapters": [{"start_time": 0.0, "end_time": 95.0, "title": "Intro"}],
}


def test_fetch_facts_maps_every_field(monkeypatch):
    monkeypatch.setattr(facts_mod, "_extract_info", lambda url: INFO)
    monkeypatch.setattr(
        facts_mod,
        "_fetch_transcript",
        lambda vid, langs: [Segment(start_s=0, text="hi"), Segment(start_s=3725, text="late")],
    )
    facts = fetch_facts("MBPHU7aaklM", today=date(2026, 9, 27))
    assert facts.url == "https://www.youtube.com/watch?v=MBPHU7aaklM"
    assert (facts.subscribers, facts.views, facts.likes) == (2260000, 1400000, 46000)
    assert (facts.upload_date, facts.duration_s, facts.fetched_at) == ("2026-09-20", 1260, "2026-09-27")
    assert facts.chapters[0].title == "Intro"
    assert facts.transcript_text() == "[0:00] hi\n[1:02:05] late"


def test_video_without_captions_has_no_transcript(monkeypatch):
    monkeypatch.setattr(facts_mod, "_extract_info", lambda url: INFO)
    monkeypatch.setattr(facts_mod, "_fetch_transcript", lambda vid, langs: None)
    facts = fetch_facts("MBPHU7aaklM")
    assert facts.transcript is None and facts.transcript_text() is None


def test_yt_dlp_failure_is_facts_unavailable(monkeypatch):
    def boom(url):
        raise RuntimeError("HTTP Error 429")

    monkeypatch.setattr(facts_mod, "_extract_info", boom)
    with pytest.raises(FactsUnavailable, match="yt-dlp"):
        fetch_facts("MBPHU7aaklM")


def test_blocked_transcript_falls_back_to_ytdlp(monkeypatch):
    def blocked(vid, langs):
        raise RuntimeError("IpBlocked")

    monkeypatch.setattr(facts_mod, "_extract_info", lambda url: INFO)
    monkeypatch.setattr(facts_mod, "_fetch_transcript", blocked)
    monkeypatch.setattr(
        facts_mod, "_fetch_transcript_via_ytdlp", lambda vid, langs: [Segment(start_s=0, text="from yt-dlp")]
    )
    facts = fetch_facts("MBPHU7aaklM")
    assert facts.transcript_text() == "[0:00] from yt-dlp"


def test_blocked_transcript_continues_without_one_when_the_fallback_also_fails(monkeypatch, caplog):
    def blocked(vid, langs):
        raise RuntimeError("IpBlocked")

    monkeypatch.setattr(facts_mod, "_extract_info", lambda url: INFO)
    monkeypatch.setattr(facts_mod, "_fetch_transcript", blocked)
    monkeypatch.setattr(facts_mod, "_fetch_transcript_via_ytdlp", lambda vid, langs: None)
    caplog.set_level("WARNING", logger="catcher.youtube")
    facts = fetch_facts("MBPHU7aaklM")  # not raised: the page is written from title/description instead
    assert facts.transcript is None
    assert facts.title == "T"  # the rest of the facts (from yt-dlp) are still there
    assert any("IpBlocked" in r.getMessage() for r in caplog.records)


def test_blocked_transcript_continues_without_one_when_the_fallback_also_raises(monkeypatch):
    def blocked(vid, langs):
        raise RuntimeError("IpBlocked")

    def also_blocked(vid, langs):
        raise RuntimeError("also blocked")

    monkeypatch.setattr(facts_mod, "_extract_info", lambda url: INFO)
    monkeypatch.setattr(facts_mod, "_fetch_transcript", blocked)
    monkeypatch.setattr(facts_mod, "_fetch_transcript_via_ytdlp", also_blocked)
    facts = fetch_facts("MBPHU7aaklM")
    assert facts.transcript is None


def test_fmt_ts():
    assert (fmt_ts(0), fmt_ts(410), fmt_ts(3725)) == ("0:00", "6:50", "1:02:05")


def test_cli_prints_facts_json(monkeypatch):
    fake = YoutubeFacts(video_id="MBPHU7aaklM", url="u", fetched_at="2026-09-27")
    monkeypatch.setattr(cli, "fetch_facts", lambda vid, languages: fake)
    result = CliRunner().invoke(cli.app, ["youtube", "facts", "https://youtu.be/MBPHU7aaklM"])
    assert result.exit_code == 0, result.output
    assert '"video_id": "MBPHU7aaklM"' in result.output


def test_parse_json3_captions_concatenates_segments_and_skips_empty_events():
    data = {
        "events": [
            {"tStartMs": 0, "segs": [{"utf8": "hi"}]},
            {"tStartMs": 4100, "segs": [{"utf8": "there"}, {"utf8": " again"}]},
            {"tStartMs": 8000},  # a positioning event with no text: skipped
        ]
    }
    assert facts_mod._parse_json3_captions(data) == [
        Segment(start_s=0.0, text="hi"),
        Segment(start_s=4.1, text="there again"),
    ]


def test_parse_json3_captions_on_no_events_is_empty():
    assert facts_mod._parse_json3_captions({}) == []
