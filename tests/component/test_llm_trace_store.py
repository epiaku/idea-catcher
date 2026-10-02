import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from catcher.modules.llm.service import LlmAttempt, LlmRequest, LlmTrace
from catcher.modules.llm.trace import TraceStore

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
REL = Path("notes/20261002-a1b2c3-my-idea.md")


def make_trace(
    *,
    task: str = "note",
    key: str = "k1",
    replies: tuple[str | None, ...] = ('{"title": "Ünï"}',),
    outcome: str = "ok",
) -> LlmTrace:
    return LlmTrace(
        task=task,
        profile="notes",
        backend="fake",
        model="m",
        prompt_version="v1",
        content_key=key,
        prompt_sha256="abc",
        prompt=None,
        attempts=[LlmAttempt(r, 1, 2, 3, "m", None) for r in replies],
        outcome=outcome,  # type: ignore[arg-type]
        error=None,
        output={"title": "Ünï"} if outcome == "ok" else None,
    )


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
    assert store.find("note", "k1") == ['{"title": "Ünï"}']


def test_put_overwrites_the_same_file_on_a_requeue(tmp_path):
    store = TraceStore(tmp_path)
    store.put(REL, make_trace(replies=("one",)), now=NOW)
    store.put(REL, make_trace(replies=("two",)), now=NOW + timedelta(minutes=1))
    assert len(list(tmp_path.rglob("*.json"))) == 1
    assert store.find("note", "k1") == ["two"]


def test_put_leaves_no_temp_file(tmp_path):
    store = TraceStore(tmp_path)
    store.put(REL, make_trace(), now=NOW)
    assert [p.name for p in (tmp_path / "notes").iterdir()] == ["20261002-a1b2c3-my-idea.json"]


def test_find_returns_the_replies_for_a_matching_task_and_key(tmp_path):
    store = TraceStore(tmp_path)
    store.put(REL, make_trace(replies=("bad", "good"), outcome="invalid_output"), now=NOW)
    assert store.find("note", "k1") == ["bad", "good"]
    req = LlmRequest(task="note", input={}, schema_name="NoteSummary", profile="notes")
    assert store.replayer()(req, "k1") == ["bad", "good"]


def test_find_returns_none_for_another_key_or_task(tmp_path):
    store = TraceStore(tmp_path)
    assert store.find("note", "k1") is None  # no folder at all
    store.put(REL, make_trace(), now=NOW)
    assert store.find("note", "other") is None
    assert store.find("clipping", "k1") is None


def test_find_never_returns_an_empty_list(tmp_path):
    store = TraceStore(tmp_path)
    store.put(Path("a.md"), make_trace(replies=(None,), outcome="backend_error"), now=NOW)
    store.put(Path("b.md"), make_trace(replies=(), outcome="ok"), now=NOW)
    store.put(Path("c.md"), make_trace(replies=(None,), outcome="ok"), now=NOW)
    assert store.find("note", "k1") is None


def test_find_prefers_the_newest_file(tmp_path):
    store = TraceStore(tmp_path)
    store.put(Path("a.md"), make_trace(replies=("old",)), now=NOW)
    store.put(Path("b.md"), make_trace(replies=("new",)), now=NOW + timedelta(hours=1))
    store.put(Path("c.md"), make_trace(replies=("older",)), now=NOW - timedelta(hours=1))
    assert store.find("note", "k1") == ["new"]


def test_find_ties_go_to_the_last_path_name(tmp_path):
    store = TraceStore(tmp_path)
    store.put(Path("a.md"), make_trace(replies=("a",)), now=NOW)
    store.put(Path("b.md"), make_trace(replies=("b",)), now=NOW)
    assert store.find("note", "k1") == ["b"]


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
        assert store.find("note", "k1") == ["good"]
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 4


def test_a_trace_never_holds_the_api_key(tmp_path):
    trace = make_trace()
    trace.api_key = "sk-secret"  # type: ignore[attr-defined]
    trace.prompt = None
    path = TraceStore(tmp_path).put(REL, trace, now=NOW)
    assert "sk-secret" not in path.read_text(encoding="utf-8")
