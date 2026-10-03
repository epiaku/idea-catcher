from pathlib import Path

from catcher.core.files import write_atomic
from catcher.core.frontmatter import FrontmatterError, load
from catcher.modules.pipeline.doctypes import destination_dir
from catcher.modules.pipeline.inbox import Note


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


def write_page(docs_repo: Path, destination: str, doc_id: str, filename: str, page: str) -> list[Path]:
    """Write the page into the destination folder (notes, youtube or web-clips) and delete the older pages
    with the same id there. Other folders are not looked at. Raises ValueError for any other destination."""
    out_dir = docs_repo / destination_dir(destination)
    target = out_dir / filename
    old = [p for p in find_pages_by_id(out_dir, doc_id) if p != target]
    for path in old:
        path.unlink()
    write_atomic(target, page)
    return [target, *old]


def write_output(ideas_repo: Path, note: Note, page: str) -> list[Path]:
    """Write the final page to `output/<same subfolder and name>`."""
    target = note.output_path(ideas_repo)
    write_atomic(target, page)
    return [target]
