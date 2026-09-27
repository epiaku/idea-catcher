from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from catcher.core.git import commit_paths, pull, push
from catcher.modules.llm.service import BackendUnavailable, InvalidOutput, UsageLimitReached
from catcher.modules.pipeline.process import ProcessOptions, Services, process_note
from catcher.modules.pipeline.publish import archive_staged, write_page
from catcher.modules.pipeline.staging import load_staged, stage_inbox

Status = Literal["published", "would_publish", "deferred", "failed", "skipped"]


@dataclass
class ItemReport:
    doc_id: str
    doc_class: str
    status: Status
    message: str = ""
    page: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None


@dataclass
class RunOptions:
    profile: str | None = None
    review: bool = True
    review_profile: str | None = None
    dry_run: bool = False
    push: bool = False
    limit: int | None = None


@dataclass
class RunReport:
    items: list[ItemReport] = field(default_factory=list)
    staging_errors: dict[str, str] = field(default_factory=dict)
    committed: dict[str, bool] = field(default_factory=dict)
    pushed: bool = False

    def counts(self) -> dict[str, int]:
        return dict(Counter(item.status for item in self.items))


def run_pipeline(ideas: Path, docs: Path, opts: RunOptions, svc: Services) -> RunReport:
    report = RunReport()
    if opts.push:
        pull(ideas)
        pull(docs)

    staging = stage_inbox(ideas, dry_run=opts.dry_run)
    report.staging_errors = staging.errors
    touched_ideas: list[Path] = list(staging.touched)
    touched_docs: list[Path] = []

    notes = {note.doc_id: note for note in load_staged(ideas)}
    notes.update({note.doc_id: note for note in staging.staged})
    ordered = sorted(notes.values(), key=lambda n: (str(n.doc.fm.get("captured", "")), n.doc_id))

    blocked: set[str] = set()
    attempted = 0
    for note in ordered:
        item = ItemReport(note.doc_id, note.doctype.name, "skipped")
        report.items.append(item)
        if opts.limit is not None and attempted >= opts.limit:
            item.message = "run limit reached"
            continue
        attempted += 1
        popts = ProcessOptions(
            profile=opts.profile,
            review=opts.review,
            review_profile=opts.review_profile,
            dry_run=opts.dry_run,
            docs_repo=docs,
            blocked_backends=frozenset(blocked),
        )
        try:
            processed = process_note(note, svc, popts)
        except UsageLimitReached as e:
            blocked.add(e.backend)
            item.status, item.message = "deferred", f"usage limit ({e.backend}): {e}"
            continue
        except BackendUnavailable as e:
            item.status, item.message = "deferred", str(e)
            continue
        except InvalidOutput as e:
            item.status, item.message = "failed", str(e)
            continue

        touched_ideas += processed.written
        item.tokens_in, item.tokens_out = processed.llm.usage.tokens_in, processed.llm.usage.tokens_out
        item.page = processed.filename
        if processed.problems:
            item.status, item.message = "failed", "; ".join(processed.problems)
        elif opts.dry_run:
            item.status = "would_publish"
        else:
            touched_docs += write_page(docs, note.doctype, note.doc_id, processed.filename, processed.page)
            touched_ideas += archive_staged(ideas, note)
            item.status = "published"
        if processed.dropped_tags:
            dropped = f"dropped tags: {', '.join(processed.dropped_tags)}"
            item.message = f"{item.message}; {dropped}" if item.message else dropped

    if not opts.dry_run:
        author = (svc.settings.git_author_name, svc.settings.git_author_email)
        published = report.counts().get("published", 0)
        report.committed["docs"] = commit_paths(
            docs, touched_docs, f"idea-catcher: publish {published} page(s)", author=author
        )
        report.committed["ideas"] = commit_paths(
            ideas,
            touched_ideas,
            f"idea-catcher: stage and archive captures ({published} published)",
            author=author,
        )
        if opts.push:
            push(docs)
            push(ideas)
            report.pushed = True
    return report
