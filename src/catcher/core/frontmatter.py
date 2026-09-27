import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class FrontmatterError(ValueError):
    """The file starts with '---' but its frontmatter can't be read."""


@dataclass
class Doc:
    fm: dict[str, Any] = field(default_factory=dict)
    body: str = ""


_CLOSING_LINE = re.compile(r"^---[ \t]*$", re.MULTILINE)


def parse(text: str) -> Doc:
    text = text.lstrip("﻿").replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return Doc({}, text)
    rest = text[4:]
    match = _CLOSING_LINE.search(rest)
    if match is None:
        raise FrontmatterError("frontmatter has no closing '---' line")
    try:
        data = yaml.safe_load(rest[: match.start()]) or {}
    except yaml.YAMLError as e:
        raise FrontmatterError(f"invalid YAML frontmatter: {e}") from e
    if not isinstance(data, dict):
        raise FrontmatterError("frontmatter is not a mapping")
    body = rest[match.end() :]
    if body.startswith("\n"):
        body = body[1:]
    return Doc({str(k).strip(): v for k, v in data.items()}, body)


def load(path: Path) -> Doc:
    return parse(path.read_text(encoding="utf-8"))


def dump(doc: Doc) -> str:
    fm = yaml.safe_dump(doc.fm, sort_keys=False, allow_unicode=True, width=10_000)
    return f"---\n{fm}---\n{doc.body}"
