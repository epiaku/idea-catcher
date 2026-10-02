from pathlib import Path
from typing import Any

from catcher.modules.pipeline.doctypes import canonical_source
from catcher.modules.pipeline.glossary import Glossary
from catcher.modules.pipeline.inbox import Note
from catcher.modules.pipeline.tags import TagList
from catcher.modules.youtube.facts import YoutubeFacts


def capture_tags(note: Note) -> list[str]:
    raw = note.doc.fm.get("tags") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(t) for t in raw if str(t).strip().lower() != "clippings"]


def title_hint(note: Note) -> str:
    title = note.doc.fm.get("title")
    if title:
        return str(title)
    return Path(str(note.doc.fm.get("source_file") or note.path.name)).stem


def prompt_input(
    note: Note,
    tags: TagList,
    facts: YoutubeFacts | None = None,
    glossary: Glossary | None = None,
    context: str = "",
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "title_hint": title_hint(note),
        "body": note.doc.body,
        "source": canonical_source(note.doctype, note.doc.fm),
        "tags": tags.as_prompt_dict(),
        "capture_tags": capture_tags(note),
        "glossary": glossary.as_prompt_list() if glossary else [],
        "context": context
        if note.doctype.name == "youtube"
        else "",  # a Gemini page only restructures Gemini
    }
    if facts is not None:
        data["facts"] = facts.model_dump(exclude={"transcript"})
        data["transcript"] = facts.transcript_text()
    return data
