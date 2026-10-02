"""A store for LLM traces: one JSON file per processed document, under the `llm` folder."""

import json
import logging
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from catcher.core.files import write_atomic
from catcher.modules.llm.service import LlmTrace, Replayer, SavedReply

log = logging.getLogger("catcher.llm")

LLM_DIR = "llm"
VERSION = 1
INVALID_PAGE = "invalid_page"  # the outcome of an `ok` trace whose reply made an invalid page: not reused
FAKE_BACKEND = "fake"  # its replies never replace the saved replies of a real model


def _aware(text: str) -> datetime | None:
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return when if when.tzinfo is not None else None


class TraceAttemptFile(BaseModel):
    reply: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    duration_ms: int | None = None
    model: str | None = None
    error: str | None = None


class TraceFile(BaseModel):
    version: int = VERSION
    saved_at: str
    task: str
    profile: str
    backend: str
    model: str | None = None
    prompt_version: str
    content_key: str
    prompt_sha256: str
    prompt: str | None = None
    attempts: list[TraceAttemptFile]
    outcome: str
    error: str | None = None
    output: dict[str, Any] | None = None


class TraceStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def path_for(self, target_rel: Path) -> Path:
        return (self.directory / target_rel).with_suffix(".json")

    def put(self, target_rel: Path, trace: LlmTrace, *, now: datetime) -> Path:
        """Write the trace atomically. `now` should be timezone-aware (it is written as given)."""
        data = asdict(trace)
        fields = {name: data[name] for name in TraceFile.model_fields if name in data}
        model = TraceFile(saved_at=now.isoformat(timespec="seconds"), **fields)  # only TraceFile fields
        path = self.path_for(target_rel)
        old = self._read(path) if path.is_file() else None
        if old is not None and any(a.reply is not None for a in old.attempts):
            if not any(a.reply is not None for a in model.attempts):
                log.warning(
                    "keeping the existing trace %s: it holds replies and the new one holds none", path
                )
                return path
            if model.backend == FAKE_BACKEND and old.backend != FAKE_BACKEND:
                log.warning(
                    "keeping the existing trace %s: it holds replies from %s, the new one is from the fake "
                    "backend",
                    path,
                    old.backend,
                )
                return path
        _write(path, model)
        return path

    def mark_unusable(self, target_rel: Path, *, reason: str) -> bool:
        """The reply of this `ok` trace made an invalid page: it is not a good reply, so it is not reused.
        Rewrites the file with `outcome = "invalid_page"` and `error = reason` (the replies stay). Returns
        whether anything changed; never raises."""
        path = self.path_for(target_rel)
        try:
            if not path.is_file():
                return False
            old = self._read(path)
            if old is None or old.outcome != "ok":
                return False
            _write(path, old.model_copy(update={"outcome": INVALID_PAGE, "error": reason}))
            return True
        except Exception as e:
            log.warning("could not mark the trace %s as not reusable: %s", path, e)
            return False

    def find(self, task: str, content_key: str, *, profile: str, prompt_version: str) -> SavedReply | None:
        """The saved reply of the newest `ok` trace for this task, profile, prompt version and document text,
        or None (never one without replies). The newest by `saved_at` read as a time; on a tie, the last
        path."""
        if not self.directory.is_dir():
            return None
        best: tuple[datetime, str, SavedReply] | None = None
        for path in sorted(self.directory.rglob("*.json")):
            trace = self._read(path)
            if trace is None or trace.outcome != "ok":
                continue
            wanted = (task, profile, prompt_version, content_key)
            if (trace.task, trace.profile, trace.prompt_version, trace.content_key) != wanted:
                continue
            replies = [a.reply for a in trace.attempts if a.reply is not None]
            if not replies:
                continue
            saved_at = _aware(trace.saved_at)
            if saved_at is None:
                log.warning(
                    "skipping trace %s: saved_at %r is not a time with a UTC offset", path, trace.saved_at
                )
                continue
            saved = SavedReply(
                replies=replies,
                backend=trace.backend,
                model=trace.model,
                tokens_in=sum(a.tokens_in or 0 for a in trace.attempts),
                tokens_out=sum(a.tokens_out or 0 for a in trace.attempts),
                duration_ms=trace.attempts[-1].duration_ms,
            )
            candidate = (saved_at, path.as_posix(), saved)
            if best is None or candidate[:2] >= best[:2]:
                best = candidate
        return best[2] if best else None

    def replayer(self) -> Replayer:
        return lambda req, key, version: self.find(req.task, key, profile=req.profile, prompt_version=version)

    @staticmethod
    def _read(path: Path) -> TraceFile | None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("version") != VERSION:
                log.warning("skipping trace %s: unsupported version", path)
                return None
            return TraceFile.model_validate(raw)
        except (OSError, ValueError) as e:  # includes json and pydantic validation errors
            log.warning("skipping trace %s: %s", path, e)
            return None


def _write(path: Path, model: TraceFile) -> None:
    dump = model.model_dump()
    try:
        text = json.dumps(dump, indent=2, ensure_ascii=False) + "\n"
        text.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate in a reply: escaped JSON still reads back exactly
        text = json.dumps(dump, indent=2, ensure_ascii=True) + "\n"
    write_atomic(path, text)
