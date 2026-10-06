"""What the idea-bucket folders say about each document, for `catcher reconcile` (B5 decision 8).

`scan_item_files` reads every document in `output/`, `failed/` and `duplicates/` and says, per file, its
calculated name (the path below the folder, the same in `archive/`), its id, class, original file name, and
the item status the folder and its frontmatter give:

- `output/` with a working `stage` (`staging`, `waiting_youtube`, `waiting_llm`, `ready`, `deferred`,
  `stuck`) is that status; the Stage A `analyzed` is `waiting_llm` and the Stage A `deferred` (with
  `deferred_at`, `deferred_reason`) is `deferred`; a finished page (`stage: published`, or no `stage` key at
  all) is `published`; any other stage is not understood;
- `failed/` is `failed`, the reason and time from the file's mirror (`stage: failed`) or else from its
  `.error.txt` (`reason:` and `time:` lines);
- `duplicates/` is `duplicate`.

It reads only: no file is written, moved or deleted. A file it cannot read (damaged frontmatter, not UTF-8,
no id) is returned with an `error` and no status, so the caller can report it."""

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from catcher.core.frontmatter import Doc, FrontmatterError, load
from catcher.modules.pipeline.doctypes import DOC_TYPES, derive_id, detect
from catcher.modules.pipeline.inbox import ORIGINAL_KEY, STAGE_ANALYZED, STAGE_PUBLISHED
from catcher.modules.pipeline.mirror import STAGE_KEY, read_mirror

log = logging.getLogger("catcher.reconcile")

SCANNED_FOLDERS = ("output", "failed", "duplicates")
# the `stage` of a working copy in output/ and the item status it means
WORKING_STAGES = {
    "staging": "staging",
    "waiting_youtube": "waiting_youtube",
    "waiting_llm": "waiting_llm",
    "ready": "ready",
    "deferred": "deferred",
    "stuck": "stuck",
    STAGE_ANALYZED: "waiting_llm",  # Stage A: the working copy while the model is (to be) asked
}
_READ_ERRORS = (OSError, FrontmatterError, UnicodeDecodeError)


@dataclass(frozen=True)
class FoundItem:
    """One document file in `output/`, `failed/` or `duplicates/`. `error` is set (and `status` None) when
    the file cannot be used; then only `name` and `folder` are known."""

    name: str  # the calculated name: the path below the folder, e.g. `notes/20261001-abcdef-x.md`
    folder: str  # output, failed or duplicates
    status: str | None = None
    doc_id: str | None = None
    doc_class: str | None = None
    reason: str | None = None
    since: datetime | None = None
    original_filename: str | None = None
    error: str | None = None


def _aware(moment: datetime | None) -> datetime | None:
    """A naive time is read as local time (the Stage A helpers wrote aware times)."""
    if moment is None or (moment.tzinfo is not None and moment.utcoffset() is not None):
        return moment
    return moment.astimezone()


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _archived(ideas: Path, name: str) -> Doc | None:
    """The archived original of `name`, or None when it is missing or cannot be read."""
    try:
        return load(ideas / "archive" / name)
    except _READ_ERRORS:
        return None


def _doc_class(fm: dict[str, Any], doc: Doc, archived: Doc | None) -> str:
    """The class in the frontmatter (a working copy has it), else the class of the archived original (a
    finished page has none), else what the file itself looks like."""
    given = _text(fm.get("class"))
    if given in DOC_TYPES:
        return given
    source = archived if archived is not None else doc
    return detect(source.fm, source.body).name


def _doc_id(fm: dict[str, Any], doc: Doc) -> str | None:
    given = _text(fm.get("id"))
    return given if given is not None else derive_id(detect(fm, doc.body), fm, doc.body)


def _error_file(path: Path) -> tuple[str | None, datetime | None]:
    """The `reason:` and `time:` lines of the `.error.txt` next to a file in failed/ (first non-empty line
    when there is no `reason:` line); (None, None) when there is no such file."""
    try:
        lines = [line.strip() for line in path.with_suffix(".error.txt").read_text("utf-8").splitlines()]
    except (OSError, UnicodeDecodeError):
        return None, None
    values = {
        key.strip(): value.strip() for key, sep, value in (line.partition(":") for line in lines) if sep
    }
    reason = values.get("reason") or next((line for line in lines if line), None)
    try:
        since = datetime.fromisoformat(values["time"]) if "time" in values else None
    except ValueError:
        since = None
    return reason, since


def _status(folder: str, fm: dict[str, Any]) -> str | None:
    if folder == "failed":
        return "failed"
    if folder == "duplicates":
        return "duplicate"
    if STAGE_KEY not in fm:
        return "published"  # a finished page from before `stage` was written
    stage = _text(fm.get(STAGE_KEY))
    if stage == STAGE_PUBLISHED:
        return "published"
    return WORKING_STAGES.get(stage or "")


def _found(ideas: Path, folder: str, path: Path, name: str) -> FoundItem:
    try:
        doc = load(path)
    except _READ_ERRORS as e:
        return FoundItem(name, folder, error=f"cannot read the frontmatter: {e}")
    fm = doc.fm
    status = _status(folder, fm)
    if status is None:
        return FoundItem(name, folder, error=f"stage {fm.get(STAGE_KEY)!r} in {folder}/ is not understood")
    doc_id = _doc_id(fm, doc)
    if doc_id is None:
        return FoundItem(name, folder, error="no id in the frontmatter")
    archived = _archived(ideas, name)
    reason: str | None = None
    since: datetime | None = None
    mirror = read_mirror(path)
    if status == "failed":
        if mirror is not None and mirror.stage == "failed":
            reason, since = mirror.reason, mirror.since
        if reason is None or since is None:
            error_reason, error_since = _error_file(path)
            reason, since = reason or error_reason, since or error_since
    elif status == "duplicate":
        reason = f"duplicate of {fm['duplicate_of']}" if _text(fm.get("duplicate_of")) else None
    elif status != "published" and mirror is not None:
        reason, since = mirror.reason, mirror.since
    original = _text(fm.get(ORIGINAL_KEY))
    if original is None and archived is not None:
        original = _text(archived.fm.get(ORIGINAL_KEY))
    return FoundItem(
        name,
        folder,
        status=status,
        doc_id=doc_id,
        doc_class=_doc_class(fm, doc, archived),
        reason=reason,
        since=_aware(since),
        original_filename=original,
    )


def scan_item_files(ideas: Path) -> list[FoundItem]:
    """Every `.md` file in `output/`, `failed/` and `duplicates/` (hidden files and folders are left out), in
        folder then name order. A name can appear in more than one folder: each file is returned. A file that
    cannot be read in any way is returned with an `error`, never raised."""
    found: list[FoundItem] = []
    for folder in SCANNED_FOLDERS:
        root = ideas / folder
        for path in sorted(root.rglob("*.md")) if root.is_dir() else []:
            rel = path.relative_to(root)
            if not path.is_file() or any(part.startswith(".") for part in rel.parts):
                continue
            found.append(_found_safely(ideas, folder, path, rel.as_posix()))
    return found


def _found_safely(ideas: Path, folder: str, path: Path, name: str) -> FoundItem:
    """`_found`, but any error while reading one file (a `source:` that cannot be parsed, in the file or in
    its archived original) makes that file a skipped one with a short reason: one file never stops the
    rest of the scan."""
    try:
        return _found(ideas, folder, path, name)
    except Exception as e:
        log.warning("reconcile: cannot read %s/%s: %s: %s", folder, name, type(e).__name__, e)
        detail = " ".join(f"{type(e).__name__}: {e}".split())[:200]
        return FoundItem(name, folder, error=f"cannot read: {detail}")
