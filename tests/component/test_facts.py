import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import yt_dlp
from typer.testing import CliRunner

import catcher.cli as cli
from catcher.modules.youtube import facts as facts_mod
from catcher.modules.youtube.facts import FactsUnavailable, Segment, YoutubeFacts, fetch_facts, fmt_ts
from catcher.modules.youtube.gate import is_block_error

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


def patch_extract(monkeypatch, info=INFO, transcript=None):
    monkeypatch.setattr(facts_mod, "_extract", lambda *args, **kwargs: (info, transcript))


def test_fetch_facts_maps_every_field(monkeypatch):
    patch_extract(monkeypatch, transcript=[Segment(start_s=0, text="hi"), Segment(start_s=3725, text="late")])
    facts = fetch_facts("MBPHU7aaklM", today=date(2026, 9, 27), now=datetime(2026, 9, 27, 8, 30, tzinfo=UTC))
    assert facts.url == "https://www.youtube.com/watch?v=MBPHU7aaklM"
    assert (facts.subscribers, facts.views, facts.likes) == (2260000, 1400000, 46000)
    assert (facts.upload_date, facts.duration_s, facts.fetched_at) == ("2026-09-20", 1260, "2026-09-27")
    assert facts.fetched_utc == "2026-09-27T08:30:00+00:00"
    assert facts.chapters[0].title == "Intro"
    assert facts.transcript_text() == "[0:00] hi\n[1:02:05] late"


def test_video_without_captions_has_no_transcript(monkeypatch):
    patch_extract(monkeypatch, transcript=None)
    facts = fetch_facts("MBPHU7aaklM")
    assert facts.transcript is None and facts.transcript_text() is None


def test_a_yt_dlp_failure_is_facts_unavailable_and_keeps_its_cause(monkeypatch):
    class HttpError(Exception):
        status = 429

    def boom(*args, **kwargs):
        raise HttpError("HTTP Error 429")

    monkeypatch.setattr(facts_mod, "_extract", boom)
    with pytest.raises(FactsUnavailable, match="yt-dlp") as info:
        fetch_facts("MBPHU7aaklM")
    assert is_block_error(info.value)  # the gate can still see that it was a block


# ---- the real extraction, with a fake yt-dlp: how many requests, and how fast -------------------------


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self) -> bytes:
        return self.body


class FakeYoutubeDL:
    """Stands in for yt_dlp.YoutubeDL and counts what we ask of it."""

    instances: list["FakeYoutubeDL"] = []
    captions_error: Exception | None = None
    tracks: dict | None = {"en": {"url": "https://example.test/captions"}}

    def __init__(self, params):
        self.params = params
        self.extractions: list[str] = []
        self.opened: list[str] = []
        FakeYoutubeDL.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        assert download is False
        self.extractions.append(url)
        return {**INFO, "requested_subtitles": self.tracks}

    def urlopen(self, url):
        self.opened.append(url)
        if self.captions_error:
            raise self.captions_error
        return FakeResponse(json.dumps({"events": [{"tStartMs": 0, "segs": [{"utf8": "hello"}]}]}).encode())


@pytest.fixture
def fake_ytdlp(monkeypatch):
    FakeYoutubeDL.instances = []
    FakeYoutubeDL.captions_error = None
    FakeYoutubeDL.tracks = {"en": {"url": "https://example.test/captions"}}
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYoutubeDL)
    return FakeYoutubeDL


def test_one_extraction_gives_the_info_and_the_captions_with_every_request_paced(fake_ytdlp):
    sleeps: list[float] = []
    facts = fetch_facts("MBPHU7aaklM", request_delay_s=10, sleep=sleeps.append)
    [ydl] = fake_ytdlp.instances
    assert len(ydl.extractions) == 1 and ydl.opened == ["https://example.test/captions"]  # 1 + 1, not 6 or 7
    assert facts.transcript_text() == "[0:00] hello"
    assert (
        ydl.params["sleep_interval_requests"] == 10
    )  # inside the extraction, yt-dlp sleeps between requests
    assert sleeps == [10]  # and we sleep before the caption file, which yt-dlp does not pace for us
    assert ydl.params["retries"] == 1 and ydl.params["extractor_retries"] == 1  # no hammering on its own
    assert "extractor_args" not in ydl.params  # the manifest request is kept unless asked otherwise


def test_the_delay_comes_from_the_setting_not_a_constant(fake_ytdlp):
    sleeps: list[float] = []
    fetch_facts("MBPHU7aaklM", request_delay_s=3.5, sleep=sleeps.append)
    assert fake_ytdlp.instances[0].params["sleep_interval_requests"] == 3.5 and sleeps == [3.5]


def test_skipping_the_manifests_is_an_option(fake_ytdlp):
    fetch_facts("MBPHU7aaklM", skip_manifests=True, sleep=lambda s: None)
    params = fake_ytdlp.instances[0].params
    assert params["extractor_args"] == {"youtube": {"skip": ["hls", "dash", "translated_subs"]}}
    assert params["ignore_no_formats_error"] is True


def test_a_video_with_no_caption_track_makes_no_caption_request(fake_ytdlp):
    fake_ytdlp.tracks = None
    sleeps: list[float] = []
    facts = fetch_facts("MBPHU7aaklM", sleep=sleeps.append)
    assert facts.transcript is None and fake_ytdlp.instances[0].opened == [] and sleeps == []


def test_a_block_on_the_caption_file_is_not_hidden_as_no_captions(fake_ytdlp):
    class HttpError(Exception):
        status = 429

    fake_ytdlp.captions_error = HttpError("HTTP Error 429: Too Many Requests")
    with pytest.raises(FactsUnavailable) as info:
        fetch_facts("MBPHU7aaklM", sleep=lambda s: None)
    assert is_block_error(info.value)


def test_another_caption_failure_still_gives_the_facts_without_a_transcript(fake_ytdlp):
    fake_ytdlp.captions_error = RuntimeError("broken caption file")
    facts = fetch_facts("MBPHU7aaklM", sleep=lambda s: None)
    assert facts.title == "T" and facts.transcript is None


def test_cli_prints_facts_json(monkeypatch):
    fake = YoutubeFacts(video_id="MBPHU7aaklM", url="u", fetched_at="2026-09-27")

    class FakeAccess:
        def get(self, video_id, *, facts_dir):
            assert facts_dir is None  # the hand command saves nothing
            return fake

    monkeypatch.setattr(cli, "build_access", lambda settings: FakeAccess())
    result = CliRunner().invoke(cli.app, ["youtube", "facts", "https://youtu.be/MBPHU7aaklM"])
    assert result.exit_code == 0, result.output
    assert '"video_id": "MBPHU7aaklM"' in result.output


def test_cli_facts_says_when_the_gap_or_the_breaker_stops_it(monkeypatch):
    from catcher.modules.youtube.facts import FactsDeferred

    class FakeAccess:
        def get(self, video_id, *, facts_dir):
            raise FactsDeferred("YouTube: next call allowed at 14:35")

    monkeypatch.setattr(cli, "build_access", lambda settings: FakeAccess())
    result = CliRunner().invoke(cli.app, ["youtube", "facts", "MBPHU7aaklM"])
    assert result.exit_code == 2 and "next call allowed at 14:35" in result.output


def test_fmt_ts():
    assert (fmt_ts(0), fmt_ts(410), fmt_ts(3725)) == ("0:00", "6:50", "1:02:05")


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


VTT_FIXTURE = Path(__file__).parent.parent / "fixtures" / "youtube" / "nGVZS_wUDGM.en.vtt"


def test_parse_vtt_captions_on_a_real_auto_caption_file():
    segments = facts_mod._parse_vtt_captions(VTT_FIXTURE.read_text())
    assert segments[0] == Segment(start_s=0.0, text="This is Marine and she is a rockstar")
    assert segments[1].text == "with a YouTube channel that teaches"
    assert segments[-1].text == "it."
    assert 11 * 60 + 40 < segments[-1].start_s < 12 * 60
    texts = [s.text for s in segments]
    assert all(a != b for a, b in zip(texts, texts[1:], strict=False))  # rolling repeats are gone
    assert not any("<" in t for t in texts)  # inline word timestamps are stripped
    assert [s.start_s for s in segments] == sorted(s.start_s for s in segments)


def test_parse_vtt_captions_decodes_entities_and_ignores_the_header():
    vtt = "WEBVTT\nKind: captions\n\n00:00:01.000 --> 00:00:02.000\nfish &amp; chips\n"
    assert facts_mod._parse_vtt_captions(vtt) == [Segment(start_s=1.0, text="fish & chips")]


def test_fetch_facts_on_a_real_video_from_its_saved_info_and_captions(monkeypatch):
    """Both files are from the same video (nGVZS_wUDGM); yt-dlp output trimmed to the fields we read."""
    fixtures = VTT_FIXTURE.parent
    info = json.loads((fixtures / "nGVZS_wUDGM.info.json").read_text())
    captions = facts_mod._parse_vtt_captions(VTT_FIXTURE.read_text())
    patch_extract(monkeypatch, info=info, transcript=captions)
    facts = fetch_facts("nGVZS_wUDGM", today=date(2026, 9, 30))
    assert facts.title == "I blew up a coaching business to prove its not luck"
    assert facts.channel == "Ed Lawrence"
    assert (facts.subscribers, facts.views, facts.likes) == (131000, 8901, 146)
    assert (facts.upload_date, facts.duration_s) == ("2026-09-28", 710)
    assert facts.chapters == []
    assert (facts.transcript_text() or "").startswith(
        "[0:00] This is Marine and she is a rockstar\n[0:02] with"
    )
