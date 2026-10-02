import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml

TAGS_FILE = Path(__file__).parent / "tags.yaml"
MAX_TOPICS = 4


@dataclass(frozen=True)
class TagList:
    idea_types: tuple[str, ...]
    topics: tuple[str, ...]
    projects: tuple[str, ...]

    @property
    def allowed(self) -> frozenset[str]:
        return frozenset(self.idea_types + self.topics + self.projects)

    def as_prompt_dict(self) -> dict[str, list[str]]:
        return {
            "idea_types": list(self.idea_types),
            "topics": list(self.topics),
            "projects": list(self.projects),
        }


@dataclass
class TagResult:
    tags: list[str]
    dropped: list[str]


def load_tags(path: Path = TAGS_FILE) -> TagList:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return TagList(
        tuple(str(t) for t in data.get("idea_types") or []),
        tuple(str(t) for t in data.get("topics") or []),
        tuple(str(t) for t in data.get("projects") or []),
    )


def clean_tag(tag: object) -> str:
    return re.sub(r"[\s_]+", "-", str(tag).strip().lower().lstrip("#"))


def normalize_tags(raw: Iterable[str], tags: TagList) -> TagResult:
    cleaned: list[str] = []
    for tag in map(clean_tag, raw):
        if tag and tag not in cleaned:
            cleaned.append(tag)
    # The idea type that is first in tags.idea_types (highest priority) wins.
    candidates = [t for t in cleaned if t in tags.idea_types]
    winner = min(candidates, key=tags.idea_types.index) if candidates else None
    idea: list[str] = [winner] if winner else []
    topics: list[str] = []
    projects: list[str] = []
    dropped: list[str] = []
    for tag in cleaned:
        if tag in tags.idea_types:
            if tag != winner:
                dropped.append(tag)
        elif tag in tags.topics and len(topics) < MAX_TOPICS:
            topics.append(tag)
        elif tag in tags.projects and not projects:
            projects.append(tag)
        else:
            dropped.append(tag)
    return TagResult(idea + topics + projects, dropped)
