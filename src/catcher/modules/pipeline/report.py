"""The report of one run: one line per document, the counts, and what was not found or could not be read.

`RunReport` and `ItemReport` are the shape `catcher run pipeline` prints (`_print_items` in cli.py). The old
loop (`run_pipeline`) fills them as it goes; `report_for_job` builds the same shape from the database after a
worker run: the `job_items` of the run's `pipeline.run` job (`root_job_id`, plus the items it adopted), and
the names in that job's result for what leaves no item row (duplicates, artifacts, unreadable files, names
that matched nothing, requeue moves and skips, the documents `limit` left in `inbox/`)."""

import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from catcher.modules.queue.models import Job, JobItem

Status = Literal[
    "published",
    "would_publish",
    "deferred",
    "stuck",
    "failed",
    "skipped",
    "duplicate",
    "artifact",
    "would_copy",
    "requeued",
    "would_requeue",
    "waiting",
    "would_fetch",
    "interrupted",
]


@dataclass
class ItemReport:
    doc_id: str
    doc_class: str
    status: Status
    message: str = ""
    page: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    llm_saved: bool = False  # the page was made from a saved LLM reply: the tokens are recorded, not spent


@dataclass
class RunReport:
    items: list[ItemReport] = field(default_factory=list)
    unreadable: dict[str, str] = field(default_factory=dict)  # files that could not be read, now in failed/
    not_found: list[str] = field(default_factory=list)  # `--file` names that matched no inbox document
    problems: list[str] = field(default_factory=list)  # a setup problem that stopped the run (a wrong path)
    not_in_archive: list[str] = field(
        default_factory=list
    )  # `--requeue` names that matched no archive document
    committed: dict[str, bool] = field(default_factory=dict)
    pushed: bool = False

    def counts(self) -> dict[str, int]:
        return dict(Counter(item.status for item in self.items))


# The message of a later document with the id of one staged earlier in the same run (Stage A's wording).
SAME_ID = "same id as an earlier document in this run: this page replaces the earlier one"
LIMIT_REACHED = "run limit reached"
# An item status that is not final: the document still waits for YouTube or the LLM (a later worker run
# finishes it). Its report line is `waiting`.
_WAITING_DEFAULT = {
    "waiting_youtube": "waiting for YouTube",
    "waiting_llm": "waiting for the LLM",
    "ready": "ready to publish",
    "staging": "staging: the next pipeline.run adopts it",
}
_NEXT_JOB = {"waiting_youtube": "fetch", "waiting_llm": "reason"}  # the dedupe key prefix of its next job


def report_for_job(session: Session, job_id: uuid.UUID) -> RunReport:
    """The report of the run whose `pipeline.run` job is `job_id`, from the database. Raises LookupError when
    there is no such job.

    The lines come in Stage A's order: the requeue moves and skips, the duplicates, one line per item of the
    run (by calculated name), the documents `limit` left in `inbox/`, then the artifacts. An item's status
    maps to the old one: `published`, `deferred`, `stuck`, `failed`, and `waiting` for an item still on its
    way (its next job is queued). A failed `pipeline.run` job (a wrong path) is a problem."""
    job = session.get(Job, job_id)
    if job is None:
        raise LookupError(f"no job {job_id}")
    report = RunReport()
    if job.status == "failed" and job.error:
        report.problems.append(job.error)
    names: dict[str, Any] = dict((job.result or {}).get("names") or {})

    for entry in names.get("requeued", []):
        report.items.append(
            ItemReport(entry["doc_id"], entry["doc_class"], "requeued", f"archive/{entry['name']} -> inbox/")
        )
    for entry in names.get("requeue_skipped", []):
        report.items.append(ItemReport(entry["doc_id"], entry["doc_class"], "skipped", entry["reason"]))
    for entry in names.get("duplicates", []):
        report.items.append(
            ItemReport(entry["doc_id"], entry["doc_class"], "duplicate", f"duplicate of {entry['winner']}")
        )

    adopted = list(names.get("adopted", []))
    same_id = set(names.get("same_id", []))
    rows = session.scalars(
        select(JobItem)
        .where(or_(JobItem.root_job_id == job_id, JobItem.calculated_name.in_(adopted)))
        .order_by(JobItem.calculated_name)
    )
    for row in rows:
        report.items.append(_item_line(session, row, same=row.calculated_name in same_id))

    for entry in names.get("left_by_limit", []):
        report.items.append(ItemReport(entry["doc_id"], entry["doc_class"], "skipped", LIMIT_REACHED))
    for entry in names.get("artifacts", []):
        message, page = entry.get("message") or "", entry.get("page")
        report.items.append(ItemReport(entry["name"], "artifact", entry["status"], message, page))
    report.unreadable = dict(names.get("unreadable", {}))
    report.not_found = list(names.get("not_found", []))
    report.not_in_archive = list(names.get("not_in_archive", []))
    return report


def _item_line(session: Session, row: JobItem, *, same: bool) -> ItemReport:
    """The report line of one item row. A published item has its page's file name, its tokens, the saved
    reply flag and the dropped tags as its message (as Stage A wrote them); a deferred, stuck or failed one
    its reason."""
    status = row.status
    reason = row.stage_reason or row.error or ""
    if status == "published":
        saved = bool((row.llm_result or {}).get("saved", False))
        dropped = [w for w in row.warnings or [] if isinstance(w, str) and w.startswith("dropped tags: ")]
        message = "; ".join([SAME_ID] * same + dropped)
        page = Path(row.docs_page).name if row.docs_page else None
        return ItemReport(
            row.doc_id, row.doc_class, "published", message, page, row.tokens_in, row.tokens_out, saved
        )
    if status in ("deferred", "stuck", "failed"):
        return ItemReport(row.doc_id, row.doc_class, status, reason)
    if status == "duplicate":
        return ItemReport(row.doc_id, row.doc_class, "duplicate", reason)
    return ItemReport(row.doc_id, row.doc_class, "waiting", reason or _waiting_reason(session, row))


def _waiting_reason(session: Session, row: JobItem) -> str:
    """Why an item still waits: the reason of its queued next job (a closed YouTube gate says until when),
    else what it waits for."""
    prefix = _NEXT_JOB.get(row.status)
    if prefix is not None:
        reason = session.scalar(
            select(Job.reason).where(
                Job.dedupe_key == f"{prefix}:{row.calculated_name}", Job.status.in_(("queued", "running"))
            )
        )
        if reason:
            return reason
    return _WAITING_DEFAULT.get(row.status, row.status)
