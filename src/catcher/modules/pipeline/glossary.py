from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

GLOSSARY_FILE = Path(__file__).parent / "glossary.yaml"


@dataclass(frozen=True)
class Term:
    name: str  # the right spelling
    heard_as: tuple[str, ...] = ()  # what the dictation often writes instead


@dataclass(frozen=True)
class Glossary:
    terms: tuple[Term, ...] = ()

    def as_prompt_list(self) -> list[dict[str, Any]]:
        return [{"term": t.name, "heard_as": list(t.heard_as)} for t in self.terms]


def _variants(value: object) -> tuple[str, ...]:
    raw = value if isinstance(value, list) else [value]
    return tuple(str(v).strip() for v in raw if v is not None and str(v).strip())


def load_glossary(path: Path = GLOSSARY_FILE) -> Glossary:
    """The known terms. A missing file is an empty glossary, so the prompt simply has no such section."""
    if not path.is_file():
        return Glossary()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    terms: list[Term] = []
    for entry in data.get("terms") or []:
        if isinstance(entry, dict):
            terms += [Term(str(name), _variants(heard)) for name, heard in entry.items()]
        elif str(entry).strip():
            terms.append(Term(str(entry).strip()))
    return Glossary(tuple(terms))
