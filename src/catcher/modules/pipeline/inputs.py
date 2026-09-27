from pathlib import Path
from typing import Any

from catcher.modules.pipeline.doctypes import canonical_source
from catcher.modules.pipeline.staging import StagedNote
from catcher.modules.pipeline.tags import TagList


def capture_tags(note: StagedNote) -> list[str]:
    raw = note.doc.fm.get("tags") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(t) for t in raw if str(t).strip().lower() != "clippings"]


def title_hint(note: StagedNote) -> str:
    title = note.doc.fm.get("title")
    if title:
        return str(title)
    return Path(str(note.doc.fm.get("source_file") or note.path.name)).stem


def prompt_input(note: StagedNote, tags: TagList) -> dict[str, Any]:
    return {
        "title_hint": title_hint(note),
        "body": note.doc.body,
        "source": canonical_source(note.doctype, note.doc.fm),
        "tags": tags.as_prompt_dict(),
        "capture_tags": capture_tags(note),
    }
