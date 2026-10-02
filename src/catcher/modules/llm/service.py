import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ValidationError

from catcher.modules.llm.profiles import Profile, ProfilesConfig
from catcher.modules.llm.prompts import render_prompt
from catcher.modules.llm.schemas import SCHEMAS

log = logging.getLogger("catcher.llm")


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


class InputRejected(LlmError):
    """The document itself cannot be sent (empty, or too long for the model). A retry would fail the same
    way and cost the same, so it is a failure of the document, not a deferral."""


class TransientBackendError(BackendUnavailable):
    """A failure that may not happen on the next call (a 5xx, a timeout, a dropped connection)."""


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
    from_saved: bool = False  # made from a saved reply: no model was called
    saved_from: Path | None = None  # the trace file the saved reply came from (None for a live call)
    content_key: str | None = None  # the request's content_key, when a recorder or replayer was given


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


@dataclass
class LlmAttempt:
    reply: str | None
    tokens_in: int | None
    tokens_out: int | None
    duration_ms: int | None
    model: str | None
    error: str | None


@dataclass
class LlmTrace:
    task: str
    profile: str
    backend: str
    model: str | None
    prompt_version: str
    content_key: str
    prompt_sha256: str
    prompt: str | None
    attempts: list[LlmAttempt]
    outcome: Literal["ok", "invalid_output", "backend_error"]
    error: str | None
    output: dict[str, Any] | None


Recorder = Callable[[LlmTrace], None]


@dataclass(frozen=True)
class SavedReply:
    """What a saved trace says about a good call: its raw replies in order, and who answered at what cost."""

    replies: list[str]
    backend: str
    model: str | None
    tokens_in: int  # summed over the attempts
    tokens_out: int
    duration_ms: int | None  # of the last attempt, like a live result
    path: Path | None = None  # the trace file it was read from


Replayer = Callable[[LlmRequest, str, str], SavedReply | None]  # request, content_key, prompt_version


def content_key(task: str, body: str, transcript: str | None) -> str:
    """The replay key: the document text, not its id (random per scan) and not the prompt (a prompt edit
    must not break the replays)."""
    return hashlib.sha256(f"{task}\0{body}\0{transcript or ''}".encode()).hexdigest()


def reason(
    req: LlmRequest,
    *,
    profiles: ProfilesConfig,
    backends: BackendFactory,
    recorder: Recorder | None = None,
    replayer: Replayer | None = None,
    keep_prompt: bool = False,
) -> LlmResult:
    profile = profiles.profiles[req.profile]
    schema = SCHEMAS[req.schema_name]
    prompt, version = render_prompt(req.task, req.input)
    prompt += _schema_instructions(schema)
    key = ""
    if recorder is not None or replayer is not None:
        key = content_key(req.task, req.input["body"], req.input.get("transcript"))
    saved = replayer(req, key, version) if replayer is not None else None
    if saved is not None:
        try:
            return _from_saved(req, saved, schema, profile, version, key)
        except InvalidOutput as e:  # edited by hand, or the schema changed: ask the model instead
            log.warning("%s: the saved LLM reply is no longer valid, calling the model: %s", req.task, e)
    backend = backends(profile)
    backend_name = backend.name
    attempts: list[LlmAttempt] = []
    outcome: Literal["ok", "invalid_output", "backend_error"] = "backend_error"
    trace_error: str | None = None
    output_json: dict[str, Any] | None = None
    started = False
    tokens_in = tokens_out = 0
    error = ""
    try:
        for attempt in (1, 2):
            retry_suffix = (
                f"\n\n## Your previous reply was rejected\n\n{error}"
                "\n\nReturn ONLY the corrected JSON object."
            )
            full = prompt if attempt == 1 else f"{prompt}{retry_suffix}"
            started = True
            try:
                reply = backend.complete(full, model=profile.model, task=req.task)
            except Exception as e:
                outcome, trace_error = "backend_error", str(e)
                raise
            tokens_in += reply.usage.tokens_in or 0
            tokens_out += reply.usage.tokens_out or 0
            record = LlmAttempt(
                reply=reply.text,
                tokens_in=reply.usage.tokens_in,
                tokens_out=reply.usage.tokens_out,
                duration_ms=reply.usage.duration_ms,
                model=reply.model or profile.model,
                error=None,
            )
            attempts.append(record)
            try:
                output = schema.model_validate_json(extract_json(reply.text))
            except (ValueError, ValidationError) as e:
                error = str(e)[:2000]
                record.error = error
                continue
            usage = Usage(tokens_in=tokens_in, tokens_out=tokens_out, duration_ms=reply.usage.duration_ms)
            outcome = "ok"
            if recorder is not None:
                output_json = output.model_dump(mode="json")
            return LlmResult(
                output,
                req.profile,
                backend_name,
                reply.model or profile.model,
                version,
                usage,
                attempt,
                content_key=key or None,
            )
        outcome, trace_error = "invalid_output", error
        raise InvalidOutput(f"{req.task}: invalid output after 2 attempts: {error}")
    finally:
        if recorder is not None and started:
            trace = LlmTrace(
                task=req.task,
                profile=req.profile,
                backend=backend_name,
                model=attempts[-1].model if attempts else profile.model,
                prompt_version=version,
                content_key=key,
                prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                prompt=prompt if keep_prompt else None,
                attempts=attempts,
                outcome=outcome,
                error=trace_error,
                output=output_json,
            )
            try:
                recorder(trace)
            except Exception as e:
                log.warning("could not record the LLM trace for %s: %s", req.task, e)


def _from_saved(
    req: LlmRequest, saved: SavedReply, schema: type[BaseModel], profile: Profile, version: str, key: str
) -> LlmResult:
    """The result from saved raw replies, through the same parsing, validation and second-attempt rule as
    a real call. It mirrors the recorded call (backend, model, tokens), so the page equals the live one.
    Raises InvalidOutput when the replies no longer validate. Nothing is called and nothing recorded."""
    error = "no saved reply"
    for attempt, text in enumerate(saved.replies[:2], start=1):
        try:
            output = schema.model_validate_json(extract_json(text))
        except (ValueError, ValidationError) as e:
            error = str(e)[:2000]
            continue
        usage = Usage(tokens_in=saved.tokens_in, tokens_out=saved.tokens_out, duration_ms=saved.duration_ms)
        model = saved.model or profile.model
        return LlmResult(
            output,
            req.profile,
            saved.backend,
            model,
            version,
            usage,
            attempt,
            from_saved=True,
            saved_from=saved.path,
            content_key=key,
        )
    raise InvalidOutput(f"{req.task}: the saved replies are not valid: {error}")
