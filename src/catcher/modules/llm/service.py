import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from catcher.modules.llm.profiles import Profile, ProfilesConfig
from catcher.modules.llm.prompts import render_prompt
from catcher.modules.llm.schemas import SCHEMAS


class Usage(BaseModel):
    tokens_in: int | None = None
    tokens_out: int | None = None
    duration_ms: int | None = None


@dataclass
class BackendReply:
    text: str
    usage: Usage
    model: str | None = None


class Backend(Protocol):
    name: str

    def complete(self, prompt: str, *, model: str | None, task: str) -> BackendReply: ...


BackendFactory = Callable[[Profile], Backend]


class LlmError(Exception):
    pass


class BackendUnavailable(LlmError):
    pass


class UsageLimitReached(BackendUnavailable):
    def __init__(self, message: str, backend: str) -> None:
        super().__init__(message)
        self.backend = backend


class BudgetExhausted(UsageLimitReached):
    """The API key's budget is used up: calls fail until it is raised or renewed."""


class InvalidOutput(LlmError):
    pass


class LlmRequest(BaseModel):
    task: str
    input: dict[str, Any]
    schema_name: str
    profile: str


@dataclass
class LlmResult:
    output: BaseModel
    profile: str
    backend: str
    model: str | None
    prompt_version: str
    usage: Usage
    attempts: int


def extract_json(text: str) -> str:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object in the reply")
    return text[start : end + 1]


def _schema_instructions(schema: type[BaseModel]) -> str:
    return (
        "\n\n## Output format\n\nReturn ONLY one JSON object, with no prose and no code fences, "
        "that matches this JSON Schema:\n\n" + json.dumps(schema.model_json_schema())
    )


def reason(req: LlmRequest, *, profiles: ProfilesConfig, backends: BackendFactory) -> LlmResult:
    profile = profiles.profiles[req.profile]
    schema = SCHEMAS[req.schema_name]
    prompt, version = render_prompt(req.task, req.input)
    prompt += _schema_instructions(schema)
    backend = backends(profile)
    tokens_in = tokens_out = 0
    error = ""
    for attempt in (1, 2):
        retry_suffix = (
            f"\n\n## Your previous reply was rejected\n\n{error}\n\nReturn ONLY the corrected JSON object."
        )
        full = prompt if attempt == 1 else f"{prompt}{retry_suffix}"
        reply = backend.complete(full, model=profile.model, task=req.task)
        tokens_in += reply.usage.tokens_in or 0
        tokens_out += reply.usage.tokens_out or 0
        try:
            output = schema.model_validate_json(extract_json(reply.text))
        except (ValueError, ValidationError) as e:
            error = str(e)[:2000]
            continue
        usage = Usage(tokens_in=tokens_in, tokens_out=tokens_out, duration_ms=reply.usage.duration_ms)
        return LlmResult(
            output, req.profile, backend.name, reply.model or profile.model, version, usage, attempt
        )
    raise InvalidOutput(f"{req.task}: invalid output after 2 attempts: {error}")
