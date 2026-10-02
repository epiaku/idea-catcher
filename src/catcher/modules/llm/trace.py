"""A store for LLM traces: one JSON file per processed document, under the `llm` folder."""

import json
import logging
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from catcher.core.files import write_atomic
from catcher.modules.llm.service import LlmTrace, Replayer

log = logging.getLogger("catcher.llm")

LLM_DIR = "llm"
VERSION = 1


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
        if not any(a.reply is not None for a in model.attempts) and self._holds_replies(path):
            log.warning("keeping the existing trace %s: it holds replies and the new one holds none", path)
            return path
        dump = model.model_dump()
        try:
            text = json.dumps(dump, indent=2, ensure_ascii=False) + "\n"
            text.encode("utf-8")
        except UnicodeEncodeError:  # a lone surrogate in a reply: escaped JSON still reads back exactly
            text = json.dumps(dump, indent=2, ensure_ascii=True) + "\n"
        write_atomic(path, text)
        return path

    def _holds_replies(self, path: Path) -> bool:
        if not path.is_file():
            return False
        old = self._read(path)
        return old is not None and any(a.reply is not None for a in old.attempts)

    def find(self, task: str, content_key: str) -> list[str] | None:
        """The replies of the newest usable trace for this task and key, or None (never an empty list)."""
        if not self.directory.is_dir():
            return None
        best: tuple[str, str, list[str]] | None = None
        for path in sorted(self.directory.rglob("*.json")):
            trace = self._read(path)
            if trace is None or trace.task != task or trace.content_key != content_key:
                continue
            if trace.outcome not in ("ok", "invalid_output"):
                continue
            replies = [a.reply for a in trace.attempts if a.reply is not None]
            if not replies:
                continue
            candidate = (trace.saved_at, path.as_posix(), replies)
            if best is None or candidate[:2] >= best[:2]:
                best = candidate
        return best[2] if best else None

    def replayer(self) -> Replayer:
        return lambda req, key: self.find(req.task, key)

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
