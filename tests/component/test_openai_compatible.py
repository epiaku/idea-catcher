import json

import httpx
import pytest
import respx

from catcher.core.config import Settings
from catcher.modules.llm.backends import make_backend
from catcher.modules.llm.backends.openai_compatible import OpenAiCompatibleBackend
from catcher.modules.llm.profiles import Profile
from catcher.modules.llm.service import BackendUnavailable, BudgetExhausted, UsageLimitReached

BASE = "https://api.openai.test/v1"
URL = f"{BASE}/chat/completions"


def completion(content: str) -> dict:
    return {
        "id": "x",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-test-2026",
        "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
    }


def error(status: int, message: str = "x", code: str | None = None) -> httpx.Response:
    return httpx.Response(status, json={"error": {"message": message, "code": code}})


def openai_backend(key: str = "sk-test") -> OpenAiCompatibleBackend:
    return OpenAiCompatibleBackend("openai", BASE, key, json_mode=True)


@respx.mock
def test_openai_sends_the_key_the_model_and_json_mode():
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=completion('{"a": 1}')))
    reply = openai_backend().complete("PROMPT", model="gpt-test", task="ai-chat")
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer sk-test"
    body = json.loads(request.content)
    assert body["model"] == "gpt-test"
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"] == [{"role": "user", "content": "PROMPT"}]
    assert (reply.text, reply.usage.tokens_in, reply.usage.tokens_out) == ('{"a": 1}', 12, 3)
    assert reply.model == "gpt-test-2026"


@respx.mock
def test_freellmapi_does_not_send_json_mode():
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=completion("{}")))
    OpenAiCompatibleBackend("freellmapi", BASE, "k").complete("p", model="auto", task="note")
    assert "response_format" not in json.loads(route.calls[0].request.content)


@respx.mock
@pytest.mark.parametrize(
    ("status", "message", "code"),
    [
        (
            429,
            "You exceeded your current quota, please check your plan and billing details",
            "insufficient_quota",
        ),
        (429, "Project budget limit reached", "billing_hard_limit_reached"),
        (402, "Insufficient credits", "insufficient_credits"),
    ],
)
def test_a_used_up_budget_is_reported_as_its_own_condition(status, message, code):
    respx.post(URL).mock(return_value=error(status, message, code))
    with pytest.raises(BudgetExhausted) as info:
        openai_backend().complete("p", model="m", task="ai-chat")
    assert info.value.backend == "openai"
    assert isinstance(info.value, UsageLimitReached)


@respx.mock
def test_a_plain_rate_limit_blocks_the_backend_but_is_not_a_budget_problem():
    respx.post(URL).mock(
        return_value=error(429, "Rate limit reached for tokens per min", "rate_limit_exceeded")
    )
    with pytest.raises(UsageLimitReached) as info:
        openai_backend().complete("p", model="m", task="ai-chat")
    assert not isinstance(info.value, BudgetExhausted)
    assert info.value.backend == "openai"


@respx.mock
@pytest.mark.parametrize("status", [401, 403])
def test_a_bad_key_blocks_the_backend_with_a_clear_message(status):
    respx.post(URL).mock(return_value=error(status, "Incorrect API key provided: sk-secret-123"))
    with pytest.raises(UsageLimitReached, match="authentication failed: check OPENAI_API_KEY") as info:
        openai_backend("sk-secret-123").complete("p", model="m", task="ai-chat")
    assert "sk-secret-123" not in str(info.value)
    assert not isinstance(info.value, BudgetExhausted)


@respx.mock
@pytest.mark.parametrize("status", [500, 502, 413])
def test_server_errors_are_backend_unavailable_and_do_not_block(status):
    respx.post(URL).mock(return_value=error(status))
    with pytest.raises(BackendUnavailable, match=str(status)) as info:
        openai_backend().complete("p", model="m", task="ai-chat")
    assert not isinstance(info.value, UsageLimitReached)


@respx.mock
def test_a_gateway_error_that_mentions_a_retry_budget_is_not_a_budget_problem():
    message = "All 2 routed attempt(s) failed (stopped early: retry time budget 45s exceeded)"
    respx.post(URL).mock(return_value=error(502, message))
    with pytest.raises(BackendUnavailable) as info:
        openai_backend().complete("p", model="m", task="ai-chat")
    assert not isinstance(info.value, UsageLimitReached)


@respx.mock
def test_a_connection_error_is_backend_unavailable():
    respx.post(URL).mock(side_effect=httpx.ConnectError("no route"))
    with pytest.raises(BackendUnavailable, match="unreachable"):
        openai_backend().complete("p", model="m", task="ai-chat")


@respx.mock
def test_no_choices_is_backend_unavailable():
    payload = completion("x") | {"choices": []}
    respx.post(URL).mock(return_value=httpx.Response(200, json=payload))
    with pytest.raises(BackendUnavailable, match="no choices"):
        openai_backend().complete("p", model="m", task="ai-chat")


def test_make_backend_needs_the_key_for_openai():
    with pytest.raises(BackendUnavailable, match="OPENAI_API_KEY is not set"):
        make_backend(Profile(backend="openai", model="m"), Settings(openai_api_key=""))


def test_make_backend_builds_all_three():
    settings = Settings(openai_api_key="sk-x", openai_base_url=BASE, freellmapi_url="http://f/v1")
    openai_client = make_backend(Profile(backend="openai", model="m"), settings)
    assert (openai_client.name, openai_client.json_mode) == ("openai", True)
    free = make_backend(Profile(backend="freellmapi"), settings)
    assert (free.name, free.json_mode) == ("freellmapi", False)
    assert make_backend(Profile(backend="fake"), settings).name == "fake"


def retrying_backend(max_attempts: int = 5) -> tuple[OpenAiCompatibleBackend, list[float]]:
    waits: list[float] = []
    backend = OpenAiCompatibleBackend(
        "freellmapi", BASE, "k", max_attempts=max_attempts, retry_wait_s=2, sleep=waits.append
    )
    return backend, waits


@respx.mock
@pytest.mark.parametrize(
    "failure",
    [error(502), error(503), httpx.ConnectError("no route"), httpx.ReadTimeout("slow")],
    ids=["502", "503", "connection", "timeout"],
)
def test_a_transient_failure_is_retried_until_a_call_works(failure):
    respx.post(URL).mock(side_effect=[failure, failure, httpx.Response(200, json=completion("{}"))])
    backend, waits = retrying_backend()
    assert backend.complete("p", model="m", task="ai-chat").text == "{}"
    assert waits == [2, 4]  # the wait doubles


@respx.mock
def test_it_gives_up_after_the_last_attempt_with_the_same_error_as_before():
    route = respx.post(URL).mock(return_value=error(502, "upstream"))
    backend, waits = retrying_backend(max_attempts=5)
    with pytest.raises(BackendUnavailable, match="502") as info:
        backend.complete("p", model="m", task="ai-chat")
    assert not isinstance(info.value, UsageLimitReached)
    assert route.call_count == 5 and waits == [2, 4, 8, 16]


@respx.mock
@pytest.mark.parametrize(
    "status, message, code",
    [
        (402, "x", None),
        (429, "Project budget limit reached", "billing_hard_limit_reached"),
        (429, "slow down", "rate_limit_exceeded"),
        (401, "bad key", None),
        (413, "too large", None),
    ],
)
def test_failures_that_cannot_pass_next_time_are_not_retried(status, message, code):
    route = respx.post(URL).mock(return_value=error(status, message, code))
    backend, waits = retrying_backend()
    with pytest.raises(BackendUnavailable):
        backend.complete("p", model="m", task="ai-chat")
    assert route.call_count == 1 and waits == []


@respx.mock
def test_one_attempt_turns_retrying_off():
    route = respx.post(URL).mock(return_value=error(502))
    backend, waits = retrying_backend(max_attempts=1)
    with pytest.raises(BackendUnavailable):
        backend.complete("p", model="m", task="ai-chat")
    assert route.call_count == 1 and waits == []


def test_the_settings_reach_the_backends():
    backend = make_backend(Profile(backend="freellmapi"), Settings(llm_max_attempts=3, llm_retry_wait_s=0.5))
    assert (backend.max_attempts, backend.retry_wait_s) == (3, 0.5)  # type: ignore[attr-defined]


@respx.mock
@pytest.mark.parametrize(
    "message",
    ["This model's maximum context length is 128000 tokens, you sent 300000", "Please reduce the length"],
)
def test_a_document_too_long_for_the_model_is_rejected_not_retried_forever(message):
    from catcher.modules.llm.service import InputRejected

    route = respx.post(URL).mock(return_value=error(400, message, "context_length_exceeded"))
    with pytest.raises(InputRejected):
        openai_backend().complete("p", model="m", task="ai-chat")
    assert route.call_count == 1


@respx.mock
def test_a_word_like_spend_in_an_unrelated_error_is_not_a_budget_problem():
    respx.post(URL).mock(return_value=error(404, "The model 'x' does not exist; we spend no time on credit"))
    with pytest.raises(BackendUnavailable) as info:
        openai_backend().complete("p", model="m", task="ai-chat")
    assert not isinstance(info.value, UsageLimitReached)
