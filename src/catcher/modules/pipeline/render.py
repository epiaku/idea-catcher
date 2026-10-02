import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jinja2

from catcher.core.frontmatter import Doc, dump
from catcher.modules.llm.schemas import Summary
from catcher.modules.llm.service import LlmResult
from catcher.modules.pipeline.doctypes import canonical_source
from catcher.modules.pipeline.inbox import Note
from catcher.modules.youtube.facts import fmt_ts

TEMPLATES_DIR = Path(__file__).parent / "templates"
PAGE_WEIGHT = 100


def slugify(title: str, max_len: int = 60) -> str:
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")
    if len(slug) > max_len:
        cut = slug[:max_len]
        slug = cut.rsplit("-", 1)[0] if "-" in cut else cut
    return slug or "untitled"


def page_filename(captured: str, doc_id: str, title: str) -> str:
    return f"{captured.replace('-', '')}_{doc_id}_{slugify(title)}.md"


def md_cell(value: object) -> str:
    return " ".join(str(value).split()).replace("|", "\\|")


def fmt_count(n: int | None) -> str:
    return f"{n:,}" if isinstance(n, int) else "Not available"


_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
    undefined=jinja2.StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
    autoescape=False,
)
_env.filters["md_cell"] = md_cell
_env.filters["num"] = fmt_count
_env.filters["ts"] = fmt_ts


@dataclass
class PageContext:
    note: Note
    summary: Summary
    tags: list[str]
    llm: LlmResult


def _one_line(text: str) -> str:
    return " ".join(text.split())


def build_frontmatter(ctx: PageContext, extra_fm: dict[str, Any] | None = None) -> dict[str, Any]:
    fm: dict[str, Any] = {
        "title": _one_line(ctx.summary.title),
        "description": _one_line(ctx.summary.description),
        "date": str(ctx.note.doc.fm["captured"]),
        "weight": PAGE_WEIGHT,
        "type": "docs",
        "id": ctx.note.doc_id,
        "tags": ctx.tags,
    }
    source = canonical_source(ctx.note.doctype, ctx.note.doc.fm)
    if source:
        fm["source"] = source
    if ctx.note.name:  # points from the published page to its archive and output files
        fm["source_file"] = ctx.note.target_rel.as_posix()
        fm["original_filename"] = ctx.note.original_name  # the name it had when it first entered inbox/
    fm.update(extra_fm or {})
    fm["llm"] = {
        "profile": ctx.llm.profile,
        "backend": ctx.llm.backend,
        "model": ctx.llm.model,
        "prompt_version": ctx.llm.prompt_version,
    }
    return fm


def render_page(ctx: PageContext, *, extra_fm: dict[str, Any] | None = None, **template_vars: Any) -> str:
    body = _env.get_template(ctx.note.doctype.template).render(
        s=ctx.summary, source=canonical_source(ctx.note.doctype, ctx.note.doc.fm), **template_vars
    )
    return dump(Doc(build_frontmatter(ctx, extra_fm), body))


def page_name(ctx: PageContext) -> str:
    if ctx.note.name:  # the same name in archive/, output/ and epiaku-docs
        return ctx.note.name
    return page_filename(str(ctx.note.doc.fm["captured"]), ctx.note.doc_id, ctx.summary.title)
