"""`reconcile`: rebuild the item rows from the idea-bucket folders (B5 decision 8).

Postgres is the truth while it has the rows; after a lost or new database the folders are what is left. The
files are found and read by a scan the caller passes in (`pipeline.scan_state.scan_item_files`), so the queue
never imports the pipeline; this module only compares what was found with the rows:

- a file with no row: a row is created (origin `inbox`, the status the file gives, its reason and time, or
  `now`) with one info event `reconcile: created ...`;
- a row whose status differs from its file's: it goes to the file's status through `ItemStates.transition`
  (a warning event; the reason starts with `reconcile:`);
- a row with no file (not in `output/`, `failed/`, `duplicates/`, nor in `archive/` or `inbox/`): reported in
  `missing_files`, never changed, never deleted;
- a file that cannot be read, or a name found in two folders: reported in `skipped` with the reason, and its
  row (if any) is left alone.

It never writes, moves or deletes a file and never deletes a row. With `apply=False` nothing is written: the
report says what a real run would do. A second run finds nothing to do (idempotent). It does not commit."""

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from catcher.core.db import require_aware
from catcher.modules.queue.models import ITEM_STATUSES, JobEvent, JobItem
from catcher.modules.queue.states import STUCK_SINCE, ItemStates

_REASON_IN_ERROR = ("failed", "deferred")  # as `set_item_status` keeps `error`
_PATH_COLUMNS = {"output": "output_path", "failed": "failed_path"}  # duplicates/ has no column


class FoundFile(Protocol):
    """One document file the scan found (see `pipeline.scan_state.FoundItem`)."""

    @property
    def name(self) -> str: ...  # the calculated name, `<subfolder>/<name>.md`
    @property
    def folder(self) -> str: ...  # output, failed or duplicates
    @property
    def status(self) -> str | None: ...  # None when `error` is set
    @property
    def doc_id(self) -> str | None: ...
    @property
    def doc_class(self) -> str | None: ...
    @property
    def reason(self) -> str | None: ...
    @property
    def since(self) -> datetime | None: ...
    @property
    def original_filename(self) -> str | None: ...
    @property
    def error(self) -> str | None: ...


@dataclass
class ReconcileReport:
    created: list[str] = field(default_factory=list)
    status_fixed: list[tuple[str, str, str]] = field(default_factory=list)  # (name, old, new)
    missing_files: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)  # name -> why


def _inside(ideas: Path, relative: str | None) -> Path | None:
    """`ideas / relative` when `relative` is a plain relative path (a path from a row is never trusted)."""
    if not relative:
        return None
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        return None
    return ideas / path


def _has_another_file(ideas: Path, item: JobItem) -> bool:
    """True when the row's document is still somewhere the scan does not look: its archived original, the
    inbox under its calculated name (a requeue) or its own inbox path (a staging row)."""
    candidates = [
        _inside(ideas, f"archive/{item.calculated_name}"),
        _inside(ideas, f"inbox/{item.calculated_name}"),
        _inside(ideas, item.inbox_path),
    ]
    return any(path is not None and path.is_file() for path in candidates)


def _check(found: FoundFile) -> str | None:
    """Why a found file cannot be used, or None."""
    if found.error:
        return found.error
    if found.status not in ITEM_STATUSES:
        return f"unknown status {found.status!r}"
    if not found.doc_id or not found.doc_class:
        return "no id or class"
    return None


def _create(session: Session, ideas: Path, found: FoundFile, now: datetime) -> None:
    status = str(found.status)
    since = found.since if found.since is not None else now
    archived = _inside(ideas, f"archive/{found.name}")
    paths: dict[str, Any] = {column: None for column in _PATH_COLUMNS.values()}
    if found.folder in _PATH_COLUMNS:
        paths[_PATH_COLUMNS[found.folder]] = f"{found.folder}/{found.name}"
    item = JobItem(
        calculated_name=found.name,
        doc_id=found.doc_id,
        doc_class=found.doc_class,
        origin="inbox",
        status=status,
        stage_reason=found.reason,
        stage_since=since,
        error=found.reason if status in _REASON_IN_ERROR else None,
        archive_path=f"archive/{found.name}" if archived is not None and archived.is_file() else None,
        original_filename=found.original_filename,
        created_at=now,
        updated_at=now,
        **paths,
    )
    session.add(item)
    session.flush()
    data: dict[str, Any] = {"reconcile": "created", "folder": found.folder, "to": status}
    if status == "stuck":
        data[STUCK_SINCE] = since.isoformat()
    message = f"reconcile: created {found.name} as {status} (from {found.folder}/)"
    session.add(JobEvent(item_id=item.id, ts=now, level="info", message=message, data=data))
    session.flush()


def _fix(session: Session, states: ItemStates, item: JobItem, found: FoundFile, now: datetime) -> None:
    old, new = item.status, str(found.status)
    reason = f"reconcile: {found.reason}" if found.reason else f"reconcile: found in {found.folder}/"
    data: dict[str, Any] = {"reconcile": "status", "folder": found.folder, "from": old, "to": new}
    if new == "stuck":
        data[STUCK_SINCE] = (found.since or now).isoformat()
    fixed = states.transition(
        session, item.calculated_name, new, now=now, reason=reason, level="warning", data=data
    )
    if fixed is not None and found.folder in _PATH_COLUMNS:
        setattr(fixed, _PATH_COLUMNS[found.folder], f"{found.folder}/{found.name}")
        session.flush()


def reconcile(
    session: Session,
    ideas: Path,
    *,
    now: datetime,
    apply: bool,
    scan: Callable[[Path], Iterable[FoundFile]],
) -> ReconcileReport:
    """Compare the files `scan(ideas)` finds with the item rows and, when `apply`, create the missing rows
    and fix the statuses (see the module text). Returns what it did (or, without `apply`, would do), each
    list in name order. Does not commit; never touches a file and never deletes a row."""
    require_aware(now)
    report = ReconcileReport()
    by_name: dict[str, list[FoundFile]] = defaultdict(list)
    for found in scan(ideas):
        by_name[found.name].append(found)
    rows = {
        item.calculated_name: item
        for item in session.scalars(select(JobItem), execution_options={"populate_existing": True})
    }
    states = ItemStates()  # no mirror: reconcile writes no file
    for name in sorted(by_name):
        files = by_name[name]
        if len(files) > 1:
            folders = ", ".join(f"{f.folder}/" for f in sorted(files, key=lambda f: f.folder))
            count = "two" if len(files) == 2 else str(len(files))
            report.skipped[name] = f"in {count} folders: {folders}"
            continue
        found = files[0]
        problem = _check(found)
        if problem is not None:
            report.skipped[name] = problem
            continue
        item = rows.get(name)
        if item is None:
            report.created.append(name)
            if apply:
                _create(session, ideas, found, now)
        elif item.status != found.status:
            report.status_fixed.append((name, item.status, str(found.status)))
            if apply:
                _fix(session, states, item, found, now)
    for name in sorted(rows):
        if name not in by_name and not _has_another_file(ideas, rows[name]):
            report.missing_files.append(name)
    return report
