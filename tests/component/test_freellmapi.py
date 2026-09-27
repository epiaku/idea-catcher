import json

import httpx
import pytest
import respx

from catcher.core.config import Settings
from catcher.modules.llm.backends import make_backend
from catcher.modules.llm.backends.freellmapi import FreeLlmApiBackend
from catcher.modules.llm.profiles import Profile
from catcher.modules.llm.service import BackendUnavailable

BASE = "http://freellmapi.test/v1"


def completion(content: str) -> dict:
    return {
        "id": "x",
        "object": "chat.completion",
        "created": 0,
        "model": "llama-3.3-70b",
        "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
    }


@respx.mock
def test_returns_text_usage_and_model():
    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=completion('{"a": 1}'))
    )
    reply = FreeLlmApiBackend(BASE, "k").complete("PROMPT", model="auto", task="note")
    assert reply.text == '{"a": 1}'
    assert (reply.usage.tokens_in, reply.usage.tokens_out, reply.model) == (12, 3, "llama-3.3-70b")
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "auto"
    assert body["messages"] == [{"role": "user", "content": "PROMPT"}]


@respx.mock
@pytest.mark.parametrize("status", [429, 500, 413])
def test_http_errors_are_backend_unavailable(status):
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(status, json={"error": {"message": "x"}})
    )
    with pytest.raises(BackendUnavailable, match=str(status)):
        FreeLlmApiBackend(BASE, "k").complete("p", model="auto", task="note")


@respx.mock
def test_connection_error_is_backend_unavailable():
    respx.post(f"{BASE}/chat/completions").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(BackendUnavailable):
        FreeLlmApiBackend(BASE, "k").complete("p", model="auto", task="note")


def test_make_backend_builds_freellmapi():
    backend = make_backend(Profile(backend="freellmapi", model="auto"), Settings(freellmapi_url=BASE))
    assert isinstance(backend, FreeLlmApiBackend)
