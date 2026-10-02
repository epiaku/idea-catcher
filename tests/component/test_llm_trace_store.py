import json
import logging
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from catcher.modules.llm.service import LlmAttempt, LlmRequest, LlmTrace, SavedReply
from catcher.modules.llm.trace import TraceStore

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
REL = Path("notes/20261002-a1b2c3-my-idea.md")
SAME = {"profile": "notes", "prompt_version": "v1"}


def make_trace(
    *,
    task: str = "note",
    key: str = "k1",
    replies: tuple[str | None, ...] = ('{"title": "Ünï"}',),
    outcome: str = "ok",
    profile: str = "notes",
    prompt_version: str = "v1",
    backend: str = "fake",
) -> LlmTrace:
    return LlmTrace(
        task=task,
        profile=profile,
        backend=backend,
        model="m",
        prompt_version=prompt_version,
        content_key=key,
        prompt_sha256="abc",
        prompt=None,
        attempts=[LlmAttempt(r, 1, 2, 3, "m", None) for r in replies],
        outcome=outcome,  # type: ignore[arg-type]
        error=None,
        output={"title": "Ünï"} if outcome == "ok" else None,
    )


def found(store: TraceStore, task: str, key: str, **match: str) -> list[str] | None:
    """The replies `find` returns, or None."""
    saved = store.find(task, key, **{**SAME, **match})
    assert saved is None or saved.replies, "find never returns a saved reply without replies"
    return None if saved is None else saved.replies


def test_put_writes_the_file_at_the_calculated_name_and_it_round_trips(tmp_path):
    store = TraceStore(tmp_path / "llm")
    path = store.put(REL, make_trace(), now=NOW)
    assert path == tmp_path / "llm" / "notes" / "20261002-a1b2c3-my-idea.json"
    assert path == store.path_for(REL)
    text = path.read_text(encoding="utf-8")
    assert "Ünï" in text  # ensure_ascii=False
    data = json.loads(text)
    assert data["version"] == 1
    assert data["saved_at"] == "2026-10-02T12:00:00+00:00"
    assert data["attempts"][0]["reply"] == '{"title": "Ünï"}'
    assert found(store, "note", "k1") == ['{"title": "Ünï"}']


def test_put_overwrites_the_same_file_on_a_requeue(tmp_path):
    store = TraceStore(tmp_path)
    store.put(REL, make_trace(replies=("one",)), now=NOW)
    store.put(REL, make_trace(replies=("two",)), now=NOW + timedelta(minutes=1))
    assert len(list(tmp_path.rglob("*.json"))) == 1
    assert found(store, "note", "k1") == ["two"]


def test_put_leaves_no_temp_file(tmp_path):
    store = TraceStore(tmp_path)
    store.put(REL, make_trace(), now=NOW)
    assert [p.name for p in (tmp_path / "notes").iterdir()] == ["20261002-a1b2c3-my-idea.json"]


def test_find_returns_the_replies_for_a_matching_task_and_key(tmp_path):
    store = TraceStore(tmp_path)
    store.put(REL, make_trace(replies=("bad", "good"), outcome="ok"), now=NOW)
    assert found(store, "note", "k1") == ["bad", "good"]
    req = LlmRequest(task="note", input={}, schema_name="NoteSummary", profile="notes")
    saved = store.replayer()(req, "k1", "v1")
    assert saved is not None and saved.replies == ["bad", "good"]
    assert store.replayer()(req, "k1", "v2") is None


def test_find_returns_none_for_another_key_or_task(tmp_path):
    store = TraceStore(tmp_path)
    assert found(store, "note", "k1") is None  # no folder at all
    store.put(REL, make_trace(), now=NOW)
    assert found(store, "note", "other") is None
    assert found(store, "clipping", "k1") is None


def test_find_never_returns_an_empty_list(tmp_path):
    store = TraceStore(tmp_path)
    store.put(Path("a.md"), make_trace(replies=(None,), outcome="backend_error"), now=NOW)
    store.put(Path("b.md"), make_trace(replies=(), outcome="ok"), now=NOW)
    store.put(Path("c.md"), make_trace(replies=(None,), outcome="ok"), now=NOW)
    store.put(Path("d.md"), make_trace(replies=("bad", "worse"), outcome="invalid_output"), now=NOW)
    assert found(store, "note", "k1") is None
    store.put(Path("e.md"), make_trace(replies=(None, "late"), outcome="ok"), now=NOW)
    assert found(store, "note", "k1") == ["late"]  # an attempt without a reply is skipped


def test_find_prefers_the_newest_file(tmp_path):
    store = TraceStore(tmp_path)
    store.put(Path("a.md"), make_trace(replies=("old",)), now=NOW)
    store.put(Path("b.md"), make_trace(replies=("new",)), now=NOW + timedelta(hours=1))
    store.put(Path("c.md"), make_trace(replies=("older",)), now=NOW - timedelta(hours=1))
    assert found(store, "note", "k1") == ["new"]


def test_find_ties_go_to_the_last_path_name(tmp_path):
    store = TraceStore(tmp_path)
    store.put(Path("a.md"), make_trace(replies=("a",)), now=NOW)
    store.put(Path("b.md"), make_trace(replies=("b",)), now=NOW)
    assert found(store, "note", "k1") == ["b"]


def test_an_unreadable_or_future_version_file_is_skipped_not_fatal(tmp_path, caplog):
    store = TraceStore(tmp_path)
    store.put(Path("good.md"), make_trace(replies=("good",)), now=NOW)
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "invalid.json").write_text('{"version": 1}', encoding="utf-8")
    future = json.loads(store.path_for(Path("good.md")).read_text(encoding="utf-8"))
    future["version"] = 2
    future["saved_at"] = "2030-01-01T00:00:00+00:00"
    future["attempts"][0]["reply"] = "future"
    (tmp_path / "future.json").write_text(json.dumps(future), encoding="utf-8")
    (tmp_path / "binary.json").write_bytes(b"\xff\xfe\x00")
    with caplog.at_level(logging.WARNING, logger="catcher.llm"):
        assert found(store, "note", "k1") == ["good"]
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 4


def test_a_trace_never_holds_the_api_key(tmp_path):
    trace = make_trace()
    trace.api_key = "sk-secret"  # type: ignore[attr-defined]
    trace.prompt = None
    path = TraceStore(tmp_path).put(REL, trace, now=NOW)
    assert "sk-secret" not in path.read_text(encoding="utf-8")


def test_a_lone_surrogate_in_a_reply_still_writes_the_trace(tmp_path):
    store = TraceStore(tmp_path)
    path = store.put(REL, make_trace(replies=("ok \ud83d done",)), now=NOW)
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["attempts"][0]["reply"] == "ok \ud83d done"
    assert found(store, "note", "k1") == ["ok \ud83d done"]


def test_a_normal_trace_keeps_non_ascii_characters_literal(tmp_path):
    path = TraceStore(tmp_path).put(REL, make_trace(replies=("grüß",)), now=NOW)
    assert "grüß" in path.read_text(encoding="utf-8")


def test_a_failed_retry_does_not_overwrite_a_trace_with_replies(tmp_path, caplog):
    store = TraceStore(tmp_path)
    path = store.put(REL, make_trace(replies=("paid",)), now=NOW)
    before = path.read_bytes()
    with caplog.at_level(logging.INFO, logger="catcher.llm"):
        again = store.put(REL, make_trace(replies=(), outcome="backend_error"), now=NOW + timedelta(hours=1))
    assert again == path and path.read_bytes() == before
    assert caplog.records
    store.put(REL, make_trace(replies=(None,), outcome="backend_error"), now=NOW + timedelta(hours=2))
    assert path.read_bytes() == before


def test_a_new_trace_with_replies_overwrites_an_old_one_with_replies(tmp_path):
    store = TraceStore(tmp_path)
    store.put(REL, make_trace(replies=("old",)), now=NOW)
    store.put(REL, make_trace(replies=("new",)), now=NOW + timedelta(hours=1))
    assert found(store, "note", "k1") == ["new"]


def test_an_old_trace_without_replies_is_replaced(tmp_path):
    store = TraceStore(tmp_path)
    store.put(REL, make_trace(replies=(), outcome="backend_error"), now=NOW)
    store.put(REL, make_trace(replies=("fresh",)), now=NOW + timedelta(hours=1))
    assert found(store, "note", "k1") == ["fresh"]


def test_a_zero_reply_trace_is_written_when_there_is_no_old_file(tmp_path):
    path = TraceStore(tmp_path).put(REL, make_trace(replies=(), outcome="backend_error"), now=NOW)
    assert json.loads(path.read_text(encoding="utf-8"))["outcome"] == "backend_error"


def test_an_unreadable_old_file_is_replaced_by_a_zero_reply_trace(tmp_path):
    store = TraceStore(tmp_path)
    path = store.path_for(REL)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe{not json")
    store.put(REL, make_trace(replies=(), outcome="backend_error"), now=NOW)
    assert json.loads(path.read_text(encoding="utf-8"))["outcome"] == "backend_error"


def test_find_only_returns_ok_traces(tmp_path):
    store = TraceStore(tmp_path)
    store.put(Path("a.md"), make_trace(replies=("bad", "worse"), outcome="invalid_output"), now=NOW)
    store.put(Path("b.md"), make_trace(replies=("half",), outcome="backend_error"), now=NOW)
    assert found(store, "note", "k1") is None
    store.put(Path("c.md"), make_trace(replies=("good",)), now=NOW - timedelta(days=1))
    assert found(store, "note", "k1") == ["good"]  # older, but the only ok one


def test_find_needs_the_same_profile_and_prompt_version(tmp_path):
    store = TraceStore(tmp_path)
    store.put(REL, make_trace(replies=("good",)), now=NOW)
    assert found(store, "note", "k1", profile="notes", prompt_version="v1") == ["good"]
    assert found(store, "note", "k1", profile="clippings", prompt_version="v1") is None
    assert found(store, "note", "k1", profile="notes", prompt_version="v2") is None
    assert found(store, "note", "k2") is None
    assert found(store, "web-clip", "k1") is None


def test_find_picks_the_newest_by_parsed_time_not_by_string(tmp_path, caplog):
    store = TraceStore(tmp_path)
    plus_two = timezone(timedelta(hours=2))
    # 13:00+02:00 is 11:00 UTC: as a string it sorts after 12:00+00:00, as a time it is an hour earlier
    store.put(Path("z.md"), make_trace(replies=("earlier",)), now=datetime(2026, 10, 2, 13, tzinfo=plus_two))
    store.put(Path("a.md"), make_trace(replies=("later",)), now=datetime(2026, 10, 2, 12, tzinfo=UTC))
    assert found(store, "note", "k1") == ["later"]

    for name, saved_at in (("bad", "yesterday"), ("naive", "2030-01-01T00:00:00")):
        path = store.path_for(Path(f"{name}.md"))
        data = json.loads(store.path_for(Path("a.md")).read_text(encoding="utf-8"))
        data["saved_at"], data["attempts"][0]["reply"] = saved_at, name
        path.write_text(json.dumps(data), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="catcher.llm"):
        assert found(store, "note", "k1") == ["later"]
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


def test_a_fake_trace_never_replaces_a_real_one(tmp_path, caplog):
    store = TraceStore(tmp_path)
    path = store.put(REL, make_trace(replies=("paid",), backend="openai"), now=NOW)
    before = path.read_bytes()
    with caplog.at_level(logging.WARNING, logger="catcher.llm"):
        again = store.put(REL, make_trace(replies=("free",), backend="fake"), now=NOW + timedelta(hours=1))
    assert again == path and path.read_bytes() == before
    assert [r.levelno for r in caplog.records] == [logging.WARNING]

    store.put(REL, make_trace(replies=("paid again",), backend="openai"), now=NOW + timedelta(hours=2))
    assert found(store, "note", "k1") == ["paid again"]  # a real trace still replaces a real one

    other = Path("notes/other.md")
    store.put(other, make_trace(replies=("free",), backend="fake"), now=NOW)
    store.put(other, make_trace(replies=("free 2",), backend="fake"), now=NOW + timedelta(hours=1))
    assert json.loads(store.path_for(other).read_text())["attempts"][0]["reply"] == "free 2"
    store.put(other, make_trace(replies=("paid",), backend="openai"), now=NOW + timedelta(hours=2))
    assert json.loads(store.path_for(other).read_text())["attempts"][0]["reply"] == "paid"

    empty = Path("notes/empty.md")
    store.put(empty, make_trace(replies=(), backend="openai", outcome="backend_error"), now=NOW)
    store.put(empty, make_trace(replies=("free",), backend="fake"), now=NOW + timedelta(hours=1))
    assert json.loads(store.path_for(empty).read_text())["backend"] == "fake"  # no paid reply to keep


def test_find_returns_the_recorded_backend_model_and_tokens(tmp_path):
    trace = make_trace(replies=("bad", "good"), backend="openai")
    trace.model = "gpt-x-2026"
    trace.attempts = [
        LlmAttempt("bad", 100, 20, 7, "gpt-x-2026", "e"),
        LlmAttempt("good", 120, 30, 9, "gpt-x-2026", None),
    ]
    store = TraceStore(tmp_path)
    store.put(REL, trace, now=NOW)
    saved = store.find("note", "k1", **SAME)
    assert saved == SavedReply(["bad", "good"], "openai", "gpt-x-2026", 220, 50, 9, path=store.path_for(REL))


MADE_IT = {"output": {"title": "Ünï"}, "content_key": "k1", "backend": "fake"}  # what make_trace() holds


def test_mark_unusable_changes_only_an_ok_trace(tmp_path, caplog):
    store = TraceStore(tmp_path)
    path = store.put(REL, make_trace(replies=("ok \ud83d done",)), now=NOW)
    before = json.loads(path.read_text(encoding="utf-8"))
    reason = "page is invalid: unknown shortcodes: nope"
    assert store.mark_unusable(path, reason=reason, **MADE_IT) is True
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["outcome"] == "invalid_page" and after["error"] == reason
    assert {k: v for k, v in after.items() if k not in ("outcome", "error")} == {
        k: v for k, v in before.items() if k not in ("outcome", "error")
    }  # replies (a lone surrogate too), output and time stay
    assert found(store, "note", "k1") is None  # no longer reused
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # atomic: no temp file left
    unchanged = path.read_bytes()
    assert store.mark_unusable(path, reason="again", **MADE_IT) is False and path.read_bytes() == unchanged

    failed = store.put(
        Path("notes/failed.md"), make_trace(replies=("x", "y"), outcome="invalid_output"), now=NOW
    )
    text = failed.read_bytes()
    assert store.mark_unusable(failed, reason="r", **MADE_IT) is False and failed.read_bytes() == text

    assert store.mark_unusable(store.path_for(Path("notes/missing.md")), reason="r", **MADE_IT) is False
    broken = store.path_for(Path("notes/broken.md"))
    broken.write_bytes(b"\xff{not json")
    with caplog.at_level(logging.WARNING, logger="catcher.llm"):
        assert store.mark_unusable(broken, reason="r", **MADE_IT) is False
    assert broken.read_bytes() == b"\xff{not json"


def test_mark_unusable_only_marks_the_trace_that_made_the_page(tmp_path, caplog):
    store = TraceStore(tmp_path)
    path = store.put(REL, make_trace(replies=("paid",), backend="openai"), now=NOW)
    before = path.read_bytes()
    paid = {**MADE_IT, "backend": "openai"}
    with caplog.at_level(logging.INFO, logger="catcher.llm"):
        assert (
            store.mark_unusable(path, reason="r", **MADE_IT) is False
        )  # the page came from the fake backend
        assert store.mark_unusable(path, reason="r", **{**paid, "content_key": "edited"}) is False
        assert store.mark_unusable(path, reason="r", **{**paid, "output": {"title": "other"}}) is False
    assert path.read_bytes() == before
    assert store.mark_unusable(path, reason="r", **paid) is True


def test_mark_unusable_never_raises(tmp_path, caplog, monkeypatch):
    from catcher.modules.llm import trace as trace_mod

    store = TraceStore(tmp_path)
    path = store.put(REL, make_trace(), now=NOW)

    def fail(path, text):
        raise OSError("read-only")

    monkeypatch.setattr(trace_mod, "write_atomic", fail)
    with caplog.at_level(logging.WARNING, logger="catcher.llm"):
        assert store.mark_unusable(path, reason="r", **MADE_IT) is False
    assert any("read-only" in r.getMessage() for r in caplog.records)


def test_a_new_reply_replaces_an_invalid_page_trace(tmp_path):
    store = TraceStore(tmp_path)
    path = store.put(REL, make_trace(replies=("made a bad page",)), now=NOW)
    store.mark_unusable(path, reason="page is invalid", **MADE_IT)
    store.put(REL, make_trace(replies=("fresh",)), now=NOW + timedelta(hours=1))
    assert found(store, "note", "k1") == ["fresh"]
    store.mark_unusable(path, reason="page is invalid", **MADE_IT)
    store.put(
        REL, make_trace(replies=("bad", "worse"), outcome="invalid_output"), now=NOW + timedelta(hours=2)
    )
    assert json.loads(path.read_text())["outcome"] == "invalid_output"  # an invalid_page is not "ok"


def test_an_ok_trace_is_never_replaced_by_a_failed_one(tmp_path, caplog):
    store = TraceStore(tmp_path)
    path = store.put(REL, make_trace(replies=("paid",)), now=NOW)
    before = path.read_bytes()
    for outcome in ("invalid_output", "backend_error"):
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="catcher.llm"):
            again = store.put(
                REL, make_trace(replies=("bad", "worse"), outcome=outcome), now=NOW + timedelta(hours=1)
            )
        assert again == path and path.read_bytes() == before
        assert [r.levelno for r in caplog.records] == [logging.WARNING]
    store.put(REL, make_trace(replies=("paid again",)), now=NOW + timedelta(hours=2))
    assert found(store, "note", "k1") == ["paid again"]  # ok over ok: what --refresh-llm is for
