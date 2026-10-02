import hashlib
import json
import logging
from pathlib import Path

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
    SavedReply,
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


def saved(*replies: str, backend: str = "openai", model: str | None = "m-saved") -> SavedReply:
    return SavedReply(
        list(replies),
        backend=backend,
        model=model,
        tokens_in=30,
        tokens_out=12,
        duration_ms=9,
        path=Path("llm/notes/source.json"),
    )


def test_a_replayer_supplies_the_replies_and_the_backend_is_never_built(fake_profiles, prompt_tags):
    traces, rec = recording()
    seen: list[tuple[LlmRequest, str, str]] = []

    def replayer(req: LlmRequest, key: str, version: str) -> SavedReply:
        seen.append((req, key, version))
        return saved(json.dumps(CANNED["note"]), backend="fake", model=None)

    profiles = ProfilesConfig(default="fake", profiles={"fake": Profile(backend="fake", model="m-replay")})
    req = note_request(prompt_tags)
    result = reason(req, profiles=profiles, backends=never_build, replayer=replayer, recorder=rec)
    assert isinstance(result.output, NoteSummary) and result.output.title == CANNED["note"]["title"]
    assert (result.backend, result.model, result.attempts) == ("fake", "m-replay", 1)  # no saved model
    assert result.from_saved and result.prompt_version == "note-7"
    assert seen == [(req, content_key("note", BODY, None), "note-7")]
    assert traces == []  # a saved reply is not recorded again


def test_a_replayed_invalid_reply_goes_through_the_same_retry_rule(fake_profiles, prompt_tags):
    replies = saved("not json", json.dumps(CANNED["note"]))
    result = reason(
        note_request(prompt_tags),
        profiles=fake_profiles,
        backends=never_build,
        replayer=lambda r, k, v: replies,
    )
    assert result.attempts == 2 and result.from_saved  # the attempt that validated, as recorded


def test_saved_replies_that_no_longer_validate_fall_through_to_the_backend(
    fake_profiles, prompt_tags, caplog
):
    for bad in (saved("not json", "{}"), saved("not json")):  # two invalid replies; one invalid, no second
        traces, rec = recording()
        fake = FakeBackend()
        with caplog.at_level(logging.WARNING, logger="catcher.llm"):
            caplog.clear()
            result = reason(
                note_request(prompt_tags),
                profiles=fake_profiles,
                backends=lambda p, fake=fake: fake,
                replayer=lambda r, k, v, bad=bad: bad,
                recorder=rec,
            )
        assert result.backend == "fake" and len(fake.prompts) == 1 and not result.from_saved
        assert result.output.title == CANNED["note"]["title"]  # type: ignore[attr-defined]
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1 and warnings[0].startswith("note: ")
        (t,) = traces  # the real call is recorded as usual
        assert t.backend == "fake" and t.outcome == "ok" and t.attempts[0].tokens_in


def test_a_replayer_returning_none_calls_the_backend(fake_profiles, prompt_tags):
    fake = FakeBackend()
    result = reason(
        note_request(prompt_tags),
        profiles=fake_profiles,
        backends=lambda p: fake,
        replayer=lambda r, k, v: None,
    )
    assert result.backend == "fake" and len(fake.prompts) == 1 and not result.from_saved


def test_a_saved_result_carries_the_recorded_backend_model_and_tokens(prompt_tags):
    calls: list[LlmTrace] = []
    profiles = ProfilesConfig(default="fake", profiles={"fake": Profile(backend="openai", model="gpt-x")})
    result = reason(
        note_request(prompt_tags),
        profiles=profiles,
        backends=never_build,
        replayer=lambda r, k, v: saved(json.dumps(CANNED["note"]), model="gpt-x-2026-01-01"),
        recorder=calls.append,
        keep_prompt=True,
    )
    assert (result.backend, result.model, result.profile) == ("openai", "gpt-x-2026-01-01", "fake")
    assert (result.usage.tokens_in, result.usage.tokens_out, result.usage.duration_ms) == (30, 12, 9)
    assert result.from_saved and result.attempts == 1
    assert result.saved_from == Path("llm/notes/source.json")  # the file to mark if the page is invalid
    assert result.content_key == content_key("note", BODY, None)
    assert calls == []  # no recorder call for a saved reply


def test_a_live_result_says_where_it_did_not_come_from(fake_profiles, prompt_tags):
    fake = FakeBackend()
    req = note_request(prompt_tags)
    plain = reason(req, profiles=fake_profiles, backends=lambda p: fake)
    assert plain.saved_from is None and plain.content_key is None  # no hooks: nothing computed
    traced = reason(req, profiles=fake_profiles, backends=lambda p: fake, recorder=lambda t: None)
    assert traced.saved_from is None and traced.content_key == content_key("note", BODY, None)


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
