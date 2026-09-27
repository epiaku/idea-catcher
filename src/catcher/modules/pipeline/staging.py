import secrets
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from catcher.core.frontmatter import Doc, FrontmatterError, dump, load
from catcher.modules.pipeline.doctypes import DOC_TYPES, DocType, derive_id, detect


@dataclass
class StagedNote:
    doc_id: str
    doctype: DocType
    doc: Doc
    path: Path


@dataclass
class StagingResult:
    staged: list[StagedNote] = field(default_factory=list)
    touched: list[Path] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


@dataclass
class _Candidate:
    path: Path
    doc: Doc
    doctype: DocType


def new_id() -> str:
    return secrets.token_hex(3)


def facts_sidecar(staged_path: Path) -> Path:
    return staged_path.with_suffix(".youtube.json")


def captured_date(fm: dict[str, Any], now: datetime) -> str:
    raw = str(fm.get("captured") or fm.get("created") or "")[:10]
    try:
        return datetime.strptime(raw, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return now.strftime("%Y-%m-%d")


def stage_inbox(ideas_repo: Path, *, dry_run: bool = False, now: datetime | None = None) -> StagingResult:
    now = now or datetime.now().astimezone()
    inbox = ideas_repo / "inbox"
    staging = ideas_repo / "staging"
    result = StagingResult()

    groups: dict[str, list[_Candidate]] = {}
    for path in sorted(inbox.rglob("*.md")) if inbox.is_dir() else []:
        if any(part.startswith(".") for part in path.relative_to(inbox).parts):
            continue
        rel = path.relative_to(ideas_repo).as_posix()
        try:
            doc = load(path)
        except (FrontmatterError, UnicodeDecodeError) as e:
            result.errors[rel] = str(e)
            continue
        doctype = detect(doc.fm, doc.body)
        doc_id = derive_id(doctype, doc.fm, doc.body) or new_id()
        groups.setdefault(doc_id, []).append(_Candidate(path, doc, doctype))

    stamp = now.strftime("%Y%m%d%H%M%S")
    for doc_id, group in groups.items():
        winner = max(group, key=lambda c: len(c.doc.body))
        fm = {
            **winner.doc.fm,
            "id": doc_id,
            "class": winner.doctype.name,
            "captured": captured_date(winner.doc.fm, now),
            "source_file": winner.path.relative_to(ideas_repo).as_posix(),
            "staged_at": now.isoformat(timespec="seconds"),
        }
        staged_path = staging / f"{doc_id}.md"
        note = StagedNote(doc_id, winner.doctype, Doc(fm, winner.doc.body), staged_path)
        result.staged.append(note)
        if dry_run:
            continue

        replaced = [c.path for c in group if c is not winner]
        if staged_path.exists():
            replaced.append(staged_path)
        superseded_dir = ideas_repo / winner.doctype.archive_dir / "superseded"
        for k, old in enumerate(replaced, start=1):
            dest = superseded_dir / f"{doc_id}--{stamp}-{k}.md"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(old, dest)
            result.touched += [old, dest]

        staging.mkdir(parents=True, exist_ok=True)
        staged_path.write_text(dump(note.doc), encoding="utf-8")
        winner.path.unlink()
        result.touched += [winner.path, staged_path]
    return result


def load_staged_note(path: Path) -> StagedNote:
    doc = load(path)
    return StagedNote(str(doc.fm["id"]), DOC_TYPES[str(doc.fm["class"])], doc, path)


def load_staged(ideas_repo: Path) -> list[StagedNote]:
    staging = ideas_repo / "staging"
    if not staging.is_dir():
        return []
    return [load_staged_note(path) for path in sorted(staging.glob("*.md"))]
