import time

import openai
from openai import OpenAI

from catcher.modules.llm.service import BackendReply, BackendUnavailable, Usage


class FreeLlmApiBackend:
    name = "freellmapi"

    def __init__(self, base_url: str, api_key: str, timeout_s: int = 120) -> None:
        self.client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout_s, max_retries=0)

    def complete(self, prompt: str, *, model: str | None, task: str) -> BackendReply:
        start = time.monotonic()
        try:
            response = self.client.chat.completions.create(
                model=model or "auto",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
            )
        except openai.APIStatusError as e:
            raise BackendUnavailable(f"FreeLLMApi HTTP {e.status_code}: {str(e)[:300]}") from e
        except openai.APIConnectionError as e:
            raise BackendUnavailable(f"FreeLLMApi unreachable: {e}") from e
        if not response.choices:
            raise BackendUnavailable("FreeLLMApi returned no choices")
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
