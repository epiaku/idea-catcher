import json
import re
import subprocess
import tempfile
from typing import Any

from catcher.modules.llm.service import BackendReply, BackendUnavailable, Usage, UsageLimitReached

_USAGE_LIMIT = re.compile(r"usage limit|limit reached|hit your limit|rate limit", re.IGNORECASE)


class ClaudeCodeBackend:
    name = "claude-code"

    def __init__(self, claude_bin: str = "claude", timeout_s: int = 600) -> None:
        self.claude_bin = claude_bin
        self.timeout_s = timeout_s

    def complete(self, prompt: str, *, model: str | None, task: str) -> BackendReply:
        cmd = [self.claude_bin, "-p", "--output-format", "json", "--max-turns", "1"]
        if model:
            cmd += ["--model", model]
        try:
            out = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                cwd=tempfile.gettempdir(),
                timeout=self.timeout_s,
            )
        except FileNotFoundError as e:
            raise BackendUnavailable(f"claude CLI not found: {self.claude_bin}") from e
        except subprocess.TimeoutExpired as e:
            raise BackendUnavailable(f"claude -p timed out after {self.timeout_s}s") from e

        data: dict[str, Any] | None = None
        try:
            parsed = json.loads(out.stdout) if out.stdout.strip() else None
            data = parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            data = None
        result = str(data.get("result") or "") if data else ""

        if out.returncode != 0 or (data and data.get("is_error")):
            message = " ".join(part for part in (result, out.stderr.strip()) if part)[:500]
            message = message or f"claude -p exited with code {out.returncode}"
            if _USAGE_LIMIT.search(message):
                raise UsageLimitReached(message, backend=self.name)
            raise BackendUnavailable(message)
        if data is None:
            raise BackendUnavailable(f"unparseable claude -p output: {out.stdout[:200]!r}")
        if not result.lstrip().startswith("{") and _USAGE_LIMIT.search(result[:300]):
            raise UsageLimitReached(result[:300], backend=self.name)

        usage = data.get("usage") or {}
        tokens_in = sum(
            int(usage.get(key) or 0)
            for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        )
        return BackendReply(
            text=result,
            usage=Usage(
                tokens_in=tokens_in,
                tokens_out=usage.get("output_tokens"),
                duration_ms=data.get("duration_ms"),
            ),
            model=model,
        )
