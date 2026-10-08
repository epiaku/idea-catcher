"""Scan the docs repo for YouTube links, and collect the video ids that are already known."""

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from catcher.core import frontmatter
from catcher.modules.backfill import store
from catcher.modules.pipeline.doctypes import YOUTUBE, YOUTUBE_GEMINI, derive_id, detect
from catcher.modules.queue.models import JobItem
from catcher.modules.youtube.urls import find_youtube_urls, video_id

_GEMINI = "-gemini"
_CLIP_FOLDERS = ("inbox", "archive", "output", "failed", "duplicates")


@dataclass(frozen=True)
class ScanResult:
    found: dict[str, str]  # video id -> first doc path, relative to the repo
    files: int
    unreadable: int


@dataclass(frozen=True)
class _Page:
    rel: str
    text: str
    fm: dict[str, Any]


def _base_id(raw: str) -> str:
    return raw.removesuffix(_GEMINI)


def _markdown_files(root: Path) -> Iterator[Path]:
    """Every *.md under root, sorted; skips .git and symlinks that resolve outside root."""
    real_root = root.resolve()
    for current, dirs, names in os.walk(root, followlinks=True):
        dirs[:] = sorted(d for d in dirs if d != ".git" and _inside(Path(current) / d, real_root))
        for name in sorted(names):
            path = Path(current) / name
            if name.endswith(".md") and _inside(path, real_root):
                yield path


def _inside(path: Path, real_root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(real_root)
    except (OSError, RuntimeError):
        return False


def _walk(docs_repo: Path) -> tuple[list[_Page], int, int]:
    """The one shared walk: readable pages, the file count, and how many files could not be read."""
    pages: list[_Page] = []
    files = unreadable = 0
    if not docs_repo.is_dir():
        return pages, 0, 0
    for path in _markdown_files(docs_repo):
        files += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            fm = frontmatter.parse(text).fm
        except (OSError, ValueError):
            unreadable += 1
            continue
        pages.append(_Page(path.relative_to(docs_repo).as_posix(), text, fm))
    return pages, files, unreadable


def _found(pages: list[_Page]) -> dict[str, str]:
    found: dict[str, str] = {}
    for page in pages:
        for url in find_youtube_urls(page.text):
            vid = video_id(url)
            if vid:
                found.setdefault(vid, page.rel)
    return found


def _page_ids(pages: list[_Page]) -> set[str]:
    ids: set[str] = set()
    for page in pages:
        for key in ("video_id", "id"):
            value = str(page.fm.get(key) or "").strip()
            if value and (key == "video_id" or value.endswith(_GEMINI)):
                ids.add(_base_id(value))
    return ids


def scan_docs(docs_repo: Path) -> ScanResult:
    pages, files, unreadable = _walk(docs_repo)
    return ScanResult(_found(pages), files, unreadable)


def page_ids(docs_repo: Path) -> set[str]:
    return _page_ids(_walk(docs_repo)[0])


def clip_ids(ideas_repo: Path) -> set[str]:
    ids: set[str] = set()
    for folder in _CLIP_FOLDERS:
        for path in _markdown_files(ideas_repo / folder) if (ideas_repo / folder).is_dir() else ():
            try:
                doc = frontmatter.parse(path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, ValueError):
                continue
            doctype = detect(doc.fm, doc.body)
            if doctype in (YOUTUBE, YOUTUBE_GEMINI):
                clip_id = derive_id(doctype, doc.fm, doc.body)
                if clip_id:
                    ids.add(_base_id(clip_id))
    return ids


def known_ids(session: Session, ideas_repo: Path, docs_repo: Path) -> set[str]:
    job_ids = {_base_id(doc_id) for doc_id in session.scalars(select(JobItem.doc_id))}
    return page_ids(docs_repo) | job_ids | clip_ids(ideas_repo) | store.known(session)
