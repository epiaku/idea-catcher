"""Fill the backfill backlog: the YouTube links in the docs that have no page yet."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from catcher.modules.backfill import store
from catcher.modules.backfill.scan import known_ids, scan_docs


@dataclass(frozen=True)
class ImportResult:
    files: int
    found: int
    known: int  # found videos that already have a page, a job item, a clip or a backlog row
    new: int  # stored in the backlog (with dry_run: would be stored)
    pending: int  # pending in all, after the new ones
    unreadable: int


def run_import(session: Session, ideas: Path, docs: Path, now: datetime, *, dry_run: bool) -> ImportResult:
    """Scan the docs, subtract the known ids and store the rest as pending. A dry run writes nothing."""
    scanned = scan_docs(docs)
    known = known_ids(session, ideas, docs)
    fresh = {vid: ("docs", path) for vid, path in scanned.found.items() if vid not in known}
    added = len(fresh) if dry_run else store.add_pending(session, fresh, now)
    pending = store.counts(session)["pending"] + (added if dry_run else 0)
    return ImportResult(
        files=scanned.files,
        found=len(scanned.found),
        known=len(scanned.found) - len(fresh),
        new=added,
        pending=pending,
        unreadable=scanned.unreadable,
    )
