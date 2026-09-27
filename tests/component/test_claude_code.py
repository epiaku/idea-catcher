import json
import os
import tempfile
from pathlib import Path

import pytest

from catcher.core.config import Settings
from catcher.modules.llm.backends import make_backend
from catcher.modules.llm.backends.claude_code import ClaudeCodeBackend
from catcher.modules.llm.backends.fake import CANNED, FakeBackend
from catcher.modules.llm.profiles import Profile, ProfilesConfig
from catcher.modules.llm.service import BackendUnavailable, LlmRequest, UsageLimitReached, reason

FAKE_CLAUDE = Path(__file__).parents[1] / "bin" / "claude"


def backend() -> ClaudeCodeBackend:
    return ClaudeCodeBackend(str(FAKE_CLAUDE), timeout_s=30)


def test_ok_returns_the_result_and_usage(monkeypatch, tmp_path):
    log = tmp_path / "call.json"
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    monkeypatch.setenv("FAKE_CLAUDE_RESULT", '{"title": "x"}')
    monkeypatch.setenv("FAKE_CLAUDE_ARGS_LOG", str(log))
    reply = backend().complete("PROMPT", model="sonnet", task="note")
    assert reply.text == '{"title": "x"}'
    assert (reply.usage.tokens_in, reply.usage.tokens_out, reply.usage.duration_ms) == (125, 50, 1234)
    call = json.loads(log.read_text())
    assert call["argv"] == ["-p", "--output-format", "json", "--max-turns", "1", "--model", "sonnet"]
    assert call["prompt"] == "PROMPT"
    assert os.path.realpath(call["cwd"]) == os.path.realpath(tempfile.gettempdir())


@pytest.mark.parametrize("mode", ["limit", "limit-text"])
def test_usage_limit_is_recognised(monkeypatch, mode):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    with pytest.raises(UsageLimitReached) as info:
        backend().complete("p", model=None, task="note")
    assert info.value.backend == "claude-code"


def test_crash_is_backend_unavailable_with_stderr(monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "crash")
    with pytest.raises(BackendUnavailable, match="boom"):
        backend().complete("p", model=None, task="note")


def test_unparseable_output_is_backend_unavailable(monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "garbage")
    with pytest.raises(BackendUnavailable, match="unparseable"):
        backend().complete("p", model=None, task="note")


def test_missing_binary_is_backend_unavailable():
    with pytest.raises(BackendUnavailable, match="not found"):
        ClaudeCodeBackend("/nonexistent/claude").complete("p", model=None, task="note")


def test_reason_through_the_fake_cli(monkeypatch, prompt_tags):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    monkeypatch.setenv("FAKE_CLAUDE_RESULT", "Here you go:\n" + json.dumps(CANNED["note"]))
    profiles = ProfilesConfig(default="c", profiles={"c": Profile(backend="claude-code", model="sonnet")})
    settings = Settings(claude_bin=str(FAKE_CLAUDE))
    request = LlmRequest(
        task="note",
        input={"title_hint": "t", "body": "b", "source": None, "tags": prompt_tags, "capture_tags": []},
        schema_name="NoteSummary",
        profile="c",
    )
    result = reason(request, profiles=profiles, backends=lambda p: make_backend(p, settings))
    assert (result.backend, result.model, result.output.title) == ("claude-code", "sonnet", "Fake Note")


def test_make_backend_builds_fake_and_claude():
    settings = Settings(claude_bin="/usr/local/bin/claude")
    assert isinstance(make_backend(Profile(backend="fake"), settings), FakeBackend)
    claude = make_backend(Profile(backend="claude-code"), settings)
    assert isinstance(claude, ClaudeCodeBackend) and claude.claude_bin == "/usr/local/bin/claude"
