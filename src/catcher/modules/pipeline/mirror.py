"""The frontmatter mirror of an item's state (B5 decision 3): `stage`, `stage_reason` and `stage_since`.

Postgres holds the truth (`job_items`); the working copy in `output/` (or the file in `failed/`) shows it in
three frontmatter keys, so a person reading the idea-bucket sees where a document stands. The mirror is best
effort and written after the commit of the transition: it rewrites those three keys and keeps the body and
every other key in their order. It never touches a finished page (`stage: published`, identical to the
docs page) and never writes `published` (the finished page replaces the working copy), and it never
overwrites a file whose frontmatter it cannot read.

`read_mirror` also understands the Stage A names: `stage: analyzed` and `analyzed_at`, `stage: deferred`
with `deferred_at` and `deferred_reason`, used when the new keys are absent."""

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from catcher.core.db import require_aware
from catcher.core.files import write_atomic
from catcher.core.frontmatter import Doc, FrontmatterError, dump, load
from catcher.modules.pipeline.inbox import STAGE_ANALYZED, STAGE_DEFERRED, STAGE_PUBLISHED

STAGE_KEY = "stage"
REASON_KEY = "stage_reason"
SINCE_KEY = "stage_since"


@dataclass(frozen=True)
class MirrorState:
    """What a file's frontmatter says about its state; any part may be missing."""

    stage: str | None
    reason: str | None
    since: datetime | None


def _read(path: Path) -> Doc | None:
    """The file's frontmatter and body, or None when it is missing or cannot be read."""
    try:
        return load(path)
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError, FrontmatterError, UnicodeDecodeError):
        return None


def _since_text(since: datetime) -> str:
    return since.isoformat(timespec="seconds")


def write_mirror(path: Path, *, stage: str, reason: str | None, since: datetime) -> bool:
    """Set `stage`, `stage_reason` (removed when `reason` is None) and `stage_since` in the frontmatter of
    `path`, keeping the body and every other key in their order; an atomic write. True when the file holds
    that state afterwards (a file that already does is not rewritten).

    False, and the file is left as it is, when it does not exist, its frontmatter cannot be read or there is
    none, it is a finished page (`stage: published`), or `stage` is `published`. Any other file error (a
    read-only folder) is raised: the caller logs it."""
    require_aware(since)
    if stage == STAGE_PUBLISHED:
        return False
    doc = _read(path)
    if doc is None or not doc.fm or doc.fm.get(STAGE_KEY) == STAGE_PUBLISHED:
        return False
    fm: dict[str, Any] = dict(doc.fm)
    fm[STAGE_KEY] = stage  # an existing key keeps its place
    if reason is None:
        fm.pop(REASON_KEY, None)
    else:
        fm[REASON_KEY] = reason
    fm[SINCE_KEY] = _since_text(since)
    if fm == doc.fm and list(fm) == list(doc.fm):
        return True  # already mirrored: no rewrite
    write_atomic(path, dump(Doc(fm, doc.body)))
    return True


def _as_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return None  # a bare date is not a moment
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _as_text(value: object) -> str | None:
    return None if value is None else str(value)


def read_mirror(path: Path) -> MirrorState | None:
    """The state the frontmatter of `path` shows, or None when the file is missing or cannot be read.

    The new keys win; without them the Stage A names are read: `deferred_reason` and `deferred_at` for
    `stage: deferred`, `analyzed_at` for `stage: analyzed`."""
    doc = _read(path)
    if doc is None:
        return None
    fm = doc.fm
    stage = _as_text(fm.get(STAGE_KEY))
    if REASON_KEY in fm or SINCE_KEY in fm:
        return MirrorState(stage, _as_text(fm.get(REASON_KEY)), _as_datetime(fm.get(SINCE_KEY)))
    if stage == STAGE_DEFERRED:
        return MirrorState(stage, _as_text(fm.get("deferred_reason")), _as_datetime(fm.get("deferred_at")))
    if stage == STAGE_ANALYZED:
        return MirrorState(stage, None, _as_datetime(fm.get("analyzed_at")))
    return MirrorState(stage, None, None)
