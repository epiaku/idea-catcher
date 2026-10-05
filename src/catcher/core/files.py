"""Small file helpers: a write that is never half done."""

import os
from pathlib import Path


def write_atomic(path: Path, content: str | bytes) -> None:
    """Write a file so that a crash or Ctrl-C leaves the old file or the new one, never half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")  # a dot name: no scan or glob picks it up
    try:
        if isinstance(content, bytes):
            temp.write_bytes(content)
        else:
            temp.write_text(content, encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
