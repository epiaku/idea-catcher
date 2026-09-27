import shutil
from pathlib import Path

from catcher.core.frontmatter import FrontmatterError, load
from catcher.modules.pipeline.doctypes import DocType
from catcher.modules.pipeline.staging import StagedNote, facts_sidecar


def find_pages_by_id(out_dir: Path, doc_id: str) -> list[Path]:
    if not out_dir.is_dir():
        return []
    found: list[Path] = []
    for path in sorted(out_dir.glob("*.md")):
        if path.name == "_index.md":
            continue
        try:
            fm = load(path).fm
        except (FrontmatterError, UnicodeDecodeError):
            continue
        if str(fm.get("id", "")) == doc_id:
            found.append(path)
    return found


def write_page(docs_repo: Path, doctype: DocType, doc_id: str, filename: str, page: str) -> list[Path]:
    out_dir = docs_repo / doctype.out_dir
    target = out_dir / filename
    old = [p for p in find_pages_by_id(out_dir, doc_id) if p != target]
    for path in old:
        path.unlink()
    out_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(page, encoding="utf-8")
    return [target, *old]


def archive_staged(ideas_repo: Path, note: StagedNote) -> list[Path]:
    dest_dir = ideas_repo / note.doctype.archive_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    touched: list[Path] = []
    for src in (note.path, facts_sidecar(note.path)):
        if src.exists():
            dest = dest_dir / src.name
            shutil.move(src, dest)
            touched += [src, dest]
    return touched
