import hashlib
import json
import logging

import pytest

from catcher.modules.llm.backends.fake import CANNED, FakeBackend
from catcher.modules.llm.profiles import Profile, ProfilesConfig
from catcher.modules.llm.prompts import render_prompt
from catcher.modules.llm.schemas import NoteSummary
from catcher.modules.llm.service import (
    BackendReply,
    BackendUnavailable,
    InvalidOutput,
    LlmRequest,
    LlmTrace,
    Recorder,
    Usage,
    content_key,
    reason,
)

BODY = "Create YouTube content walking around"


def note_request(prompt_tags: dict, **extra) -> LlmRequest:
    return LlmRequest(
        task="note",
        input={
            "title_hint": "YouTube walks",
            "body": BODY,
            "source": None,
            "tags": prompt_tags,
            "capture_tags": ["obsidian"],
            "glossary": [],
            **extra,
        },
        schema_name="NoteSummary",
        profile="fake",
    )


class ScriptedBackend:
    """Returns the given replies in order; an Exception in the list is raised instead."""

    name = "scripted"

    def __init__(self, replies: list[str | Exception]) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, model: str | None, task: str) -> BackendReply:
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return BackendReply(text=reply, usage=Usage(tokens_in=10, tokens_out=5, duration_ms=7), model="m-1")


def never_build(profile):
    raise AssertionError("the backend must not be built during a replay")


def recording() -> tuple[list[LlmTrace], Recorder]:
    traces: list[LlmTrace] = []
    return traces, traces.append


def test_a_successful_call_records_one_attempt_and_the_validated_output(fake_profiles, prompt_tags):
    traces, rec = recording()
    fake = FakeBackend()
    req = note_request(prompt_tags)
    result = reason(req, profiles=fake_profiles, backends=lambda p: fake, recorder=rec)
    assert len(traces) == 1
    t = traces[0]
    assert (t.task, t.profile, t.backend, t.model, t.prompt_version) == (
        "note",
        "fake",
        "fake",
        "fake",
        "note-7",
    )
    assert t.outcome == "ok" and t.error is None
    assert t.output == result.output.model_dump(mode="json")
    assert t.content_key == content_key("note", BODY, None)
    assert len(t.attempts) == 1
    a = t.attempts[0]
    assert a.reply == json.dumps(CANNED["note"]) and a.error is None and a.model == "fake"
    assert a.tokens_in and a.tokens_out


def test_an_invalid_reply_then_a_valid_one_records_two_attempts(fake_profiles, prompt_tags):
    traces, rec = recording()
    backend = ScriptedBackend(["not json", json.dumps(CANNED["note"])])
    result = reason(
        note_request(prompt_tags), profiles=fake_profiles, backends=lambda p: backend, recorder=rec
    )
    assert result.attempts == 2
    (t,) = traces
    assert t.outcome == "ok" and t.backend == "scripted" and t.model == "m-1"
    assert [a.reply for a in t.attempts] == ["not json", json.dumps(CANNED["note"])]
    assert t.attempts[0].error and "no JSON object" in t.attempts[0].error
    assert t.attempts[1].error is None
    assert (t.attempts[0].tokens_in, t.attempts[0].tokens_out, t.attempts[0].duration_ms) == (10, 5, 7)


def test_two_invalid_replies_record_invalid_output_and_still_raise(fake_profiles, prompt_tags):
    traces, rec = recording()
    backend = ScriptedBackend(['{"title": ""}', "{}"])
    with pytest.raises(InvalidOutput) as exc:
        reason(note_request(prompt_tags), profiles=fake_profiles, backends=lambda p: backend, recorder=rec)
    (t,) = traces
    assert t.outcome == "invalid_output" and t.output is None
    assert [a.reply for a in t.attempts] == ['{"title": ""}', "{}"]
    assert all(a.error for a in t.attempts)
    assert t.error and t.error in str(exc.value)
    assert t.error == t.attempts[1].error


def test_a_backend_error_records_backend_error_and_propagates(fake_profiles, prompt_tags):
    traces, rec = recording()
    down = BackendUnavailable("down")
    backend = ScriptedBackend(["not json", down])
    with pytest.raises(BackendUnavailable) as exc:
        reason(note_request(prompt_tags), profiles=fake_profiles, backends=lambda p: backend, recorder=rec)
    assert exc.value is down
    (t,) = traces
    assert t.outcome == "backend_error" and t.error == "down" and t.output is None
    assert [a.reply for a in t.attempts] == ["not json"]


def test_the_prompt_is_only_kept_when_asked(fake_profiles, prompt_tags):
    # attempt 1's prompt: the rendered prompt plus the schema instructions, never the retry suffix
    traces, rec = recording()
    fake = FakeBackend()
    req = note_request(prompt_tags)
    reason(req, profiles=fake_profiles, backends=lambda p: fake, recorder=rec)
    reason(req, profiles=fake_profiles, backends=lambda p: fake, recorder=rec, keep_prompt=True)
    default, kept = traces
    rendered, _ = render_prompt("note", req.input)
    assert default.prompt is None
    assert kept.prompt == fake.prompts[1] and kept.prompt.startswith(rendered)
    expected = hashlib.sha256(fake.prompts[0].encode()).hexdigest()
    assert default.prompt_sha256 == kept.prompt_sha256 == expected


def test_content_key_ignores_everything_but_task_body_and_transcript(fake_profiles, prompt_tags):
    expected = hashlib.sha256(b"note\0body\0").hexdigest()
    assert content_key("note", "body", None) == content_key("note", "body", "") == expected
    assert content_key("note", "body", "t") != expected
    assert content_key("web-clip", "body", None) != expected
    assert content_key("note", "other", None) != expected

    traces, rec = recording()
    fake = FakeBackend()
    reason(note_request(prompt_tags), profiles=fake_profiles, backends=lambda p: fake, recorder=rec)
    other = note_request({"idea_types": [], "topics": [], "projects": []}, title_hint="x", source="https://a")
    reason(other, profiles=fake_profiles, backends=lambda p: fake, recorder=rec)
    with_transcript = note_request(prompt_tags, transcript="words")
    reason(with_transcript, profiles=fake_profiles, backends=lambda p: fake, recorder=rec)
    assert traces[0].content_key == traces[1].content_key == content_key("note", BODY, None)
    assert traces[2].content_key == content_key("note", BODY, "words") != traces[0].content_key


def test_a_replayer_supplies_the_replies_and_the_backend_is_never_built(fake_profiles, prompt_tags):
    traces, rec = recording()
    seen: list[tuple[LlmRequest, str]] = []

    def replayer(req: LlmRequest, key: str) -> list[str]:
        seen.append((req, key))
        return [json.dumps(CANNED["note"])]

    profiles = ProfilesConfig(default="fake", profiles={"fake": Profile(backend="fake", model="m-replay")})
    req = note_request(prompt_tags)
    result = reason(req, profiles=profiles, backends=never_build, replayer=replayer, recorder=rec)
    assert isinstance(result.output, NoteSummary) and result.output.title == CANNED["note"]["title"]
    assert (result.backend, result.model, result.attempts) == ("replay", "m-replay", 1)
    assert (result.usage.tokens_in, result.usage.tokens_out) == (0, 0)
    assert seen == [(req, content_key("note", BODY, None))]
    (t,) = traces
    assert (t.backend, t.model, t.outcome) == ("replay", "m-replay", "ok")
    assert t.attempts[0].tokens_in == 0 and t.attempts[0].model == "m-replay"


def test_a_replayed_invalid_reply_goes_through_the_same_retry_rule(fake_profiles, prompt_tags):
    replies = ["not json", json.dumps(CANNED["note"])]
    result = reason(
        note_request(prompt_tags), profiles=fake_profiles, backends=never_build, replayer=lambda r, k: replies
    )
    assert result.attempts == 2 and result.backend == "replay"

    with pytest.raises(InvalidOutput, match="note: invalid output after 2 attempts"):
        reason(
            note_request(prompt_tags),
            profiles=fake_profiles,
            backends=never_build,
            replayer=lambda r, k: ["not json", "{}"],
        )


def test_replay_without_enough_replies_raises_invalid_output(fake_profiles, prompt_tags):
    traces, rec = recording()
    with pytest.raises(InvalidOutput, match="replay has no more replies"):
        reason(
            note_request(prompt_tags),
            profiles=fake_profiles,
            backends=never_build,
            replayer=lambda r, k: ["not json"],
            recorder=rec,
        )
    (t,) = traces
    assert t.outcome == "invalid_output" and [a.reply for a in t.attempts] == ["not json"]


def test_a_replayer_returning_none_calls_the_backend(fake_profiles, prompt_tags):
    fake = FakeBackend()
    result = reason(
        note_request(prompt_tags), profiles=fake_profiles, backends=lambda p: fake, replayer=lambda r, k: None
    )
    assert result.backend == "fake" and len(fake.prompts) == 1


def test_a_recorder_that_raises_does_not_break_the_call(fake_profiles, prompt_tags, caplog):
    def broken(trace: LlmTrace) -> None:
        raise OSError("disk full")

    fake = FakeBackend()
    with caplog.at_level(logging.WARNING, logger="catcher.llm"):
        result = reason(
            note_request(prompt_tags), profiles=fake_profiles, backends=lambda p: fake, recorder=broken
        )
    assert result.output.title == CANNED["note"]["title"]  # type: ignore[attr-defined]
    assert any("disk full" in r.getMessage() for r in caplog.records)
