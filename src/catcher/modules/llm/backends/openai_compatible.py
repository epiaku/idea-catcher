import logging
import re
import time

import openai
from openai import OpenAI

from catcher.modules.llm.service import (
    BackendReply,
    BackendUnavailable,
    BudgetExhausted,
    Usage,
    UsageLimitReached,
)

log = logging.getLogger("catcher.llm")

_BUDGET_WORDS = re.compile(r"quota|billing|credit|budget|spend", re.IGNORECASE)
_KEY_VARIABLES = {"openai": "OPENAI_API_KEY", "freellmapi": "FREELLMAPI_API_KEY"}


def _is_budget_problem(error: openai.APIStatusError) -> bool:
    code = str(getattr(error, "code", "") or "")
    if code == "rate_limit_exceeded":
        return False
    return error.status_code == 402 or bool(_BUDGET_WORDS.search(f"{code} {error.message}"))


class OpenAiCompatibleBackend:
    """One client for every OpenAI-compatible API: FreeLLMApi (`freellmapi`) and the OpenAI API (`openai`)."""

    def __init__(
        self, name: str, base_url: str, api_key: str, *, json_mode: bool = False, timeout_s: int = 120
    ) -> None:
        self.name = name
        self.json_mode = json_mode
        self.client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout_s, max_retries=0)

    def complete(self, prompt: str, *, model: str | None, task: str) -> BackendReply:
        start = time.monotonic()
        response_format = {"type": "json_object"} if self.json_mode else openai.NOT_GIVEN
        try:
            response = self.client.chat.completions.create(
                model=model or "auto",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                response_format=response_format,  # type: ignore[arg-type]
            )
        except openai.RateLimitError as e:
            if _is_budget_problem(e):
                message = f"{self.name} budget reached: {e.message[:200]}"
                log.warning(message)
                raise BudgetExhausted(message, backend=self.name) from e
            message = f"{self.name} rate limit: {e.message[:200]}"
            log.warning(message)
            raise UsageLimitReached(message, backend=self.name) from e
        except (openai.AuthenticationError, openai.PermissionDeniedError) as e:
            message = f"authentication failed: check {_KEY_VARIABLES.get(self.name, 'the API key')}"
            log.error("%s (%s, HTTP %s)", message, self.name, e.status_code)
            raise UsageLimitReached(message, backend=self.name) from e
        except openai.APIStatusError as e:
            if _is_budget_problem(e):
                message = f"{self.name} budget reached: {e.message[:200]}"
                log.warning(message)
                raise BudgetExhausted(message, backend=self.name) from e
            message = f"{self.name} HTTP {e.status_code}: {e.message[:300]}"
            log.warning(message)
            raise BackendUnavailable(message) from e
        except openai.APIConnectionError as e:
            message = f"{self.name} unreachable: {e}"
            log.warning(message)
            raise BackendUnavailable(message) from e
        if not response.choices:
            raise BackendUnavailable(f"{self.name} returned no choices")
        usage = response.usage
        return BackendReply(
            text=response.choices[0].message.content or "",
            usage=Usage(
                tokens_in=usage.prompt_tokens if usage else None,
                tokens_out=usage.completion_tokens if usage else None,
                duration_ms=int((time.monotonic() - start) * 1000),
            ),
            model=response.model,
        )
