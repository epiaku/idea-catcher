"""Release backfill videos: write each as a clip note into `inbox/clippings/`, so the normal pipeline makes
its page (decision 4). The note carries `backfill: true`, which stages its item with `origin='backfill'` and
gives every job of it the low `BACKFILL_PRIORITY` (decision 6).

A note is written atomically (a dot-named temp file in the same folder, then a hard link that fails when the
name exists), so a scan never reads half a note and an existing file is never overwritten. A video whose note
already exists (a crash between the write and the row update, or a race) or that is already a clip somewhere
in the idea bucket is marked `released` without a write: a repair. A note that cannot be written leaves its
row `pending` for the next release. Nothing is committed here: not the session (the caller's scope does) and
not the idea-bucket repo (`publish` does)."""

import errno
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from catcher.core.db import require_aware
from catcher.modules.backfill import store
from catcher.modules.backfill.scan import clip_ids

log = logging.getLogger("catcher.backfill")

WINDOW = timedelta(hours=24)  # the cap of `--limit` is per rolling 24 hours (decision 5)
CLIP_FOLDER = Path("inbox") / "clippings"
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")  # a row from anywhere is checked before it names a file
_NO_LINKS = {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV}  # a file system without hard links


@dataclass(frozen=True)
class ReleaseResult:
    released: list[str] = field(default_factory=list)  # a note was written
    repaired: list[str] = field(default_factory=list)  # marked released without a write


def daily_allowance(session: Session, limit: int, now: datetime) -> int:
    """How many more videos may be released now: `limit` minus those released in the last 24 hours, never
    below 0."""
    require_aware(now)
    return max(0, limit - store.released_since(session, now - WINDOW))


def note_path(ideas_repo: Path, vid: str) -> Path:
    return ideas_repo / CLIP_FOLDER / f"youtube source - {vid}.md"


def note_text(vid: str, now: datetime) -> str:
    url = f"https://www.youtube.com/watch?v={vid}"
    created = now.astimezone().date().isoformat()
    return f"---\nsource: {url}\ntags: [clippings]\nbackfill: true\ncreated: {created}\n---\n![]({url})\n"


def _write_new(path: Path, text: str) -> None:
    """Write `path` atomically; FileExistsError when it exists (it is never overwritten)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")  # a dot name: no scan picks it up
    try:
        temp.write_text(text, encoding="utf-8")
        try:
            os.link(temp, path)  # atomic and exclusive: fails when the name is taken
        except FileExistsError:
            raise
        except OSError as e:
            if e.errno not in _NO_LINKS:
                raise
            if path.exists():  # no hard links here: check, then rename (a race is still a repair later)
                raise FileExistsError(str(path)) from e
            os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def release(session: Session, ideas_repo: Path, limit: int, now: datetime) -> ReleaseResult:
    """Release up to `limit` of the oldest `pending` videos (the caller passes the allowance left)."""
    require_aware(now)
    result = ReleaseResult()
    if limit <= 0:
        return result
    rows = store.pending(session, limit)
    if not rows:
        return result
    clips = clip_ids(ideas_repo)
    for row in rows:
        vid = row.video_id
        if not _VIDEO_ID.match(vid):
            log.error("backfill: %r is not a YouTube video id; it stays pending", vid)
            continue
        path = note_path(ideas_repo, vid)
        repaired = vid in clips
        if not repaired:
            try:
                _write_new(path, note_text(vid, now))
            except FileExistsError:
                repaired = True
            except OSError as e:
                log.error("backfill: cannot write %s: %s; it stays pending", path, e)
                continue
        if not store.mark_released(session, vid, now):
            continue  # another release took it first
        if repaired:
            log.info("backfill: %s already has a clip in the idea bucket: marked released", vid)
            result.repaired.append(vid)
        else:
            log.info("backfill: released %s -> %s", vid, path.relative_to(ideas_repo).as_posix())
            result.released.append(vid)
    return result
