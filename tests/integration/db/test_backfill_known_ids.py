from datetime import UTC, datetime
from pathlib import Path

import pytest

from catcher.modules.backfill import store
from catcher.modules.backfill.scan import known_ids
from catcher.modules.queue.models import JobItem

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
P, J, G, V, X, K, N = (f"{c}{c}{c}{c}{c}{c}{c}{c}{c}{c}1" for c in "pjgvxkn")


def _item(doc_id: str) -> JobItem:
    return JobItem(
        calculated_name=f"youtube/{doc_id}.md",
        doc_id=doc_id,
        doc_class="youtube",
        status="published",
        created_at=NOW,
        updated_at=NOW,
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_known_ids_unions_pages_job_items_ideas_clips_and_the_backlog(session, tmp_path):
    docs, ideas = tmp_path / "docs", tmp_path / "ideas"
    _write(docs / "p.md", f"---\nvideo_id: {P}\n---\n")
    session.add_all([_item(J), _item(f"{G}-gemini")])
    store.add_pending(session, {K: ("docs", "x.md")}, NOW)
    session.flush()
    _write(ideas / "inbox/clippings/a.md", f'---\nsource : "https://www.youtube.com/watch?v={V}"\n---\n')
    _write(ideas / "archive/clippings/b.md", f'---\nsource: "https://youtu.be/{X}"\n---\n')
    _write(ideas / "failed/c.md", f'---\nsource: "https://www.youtube.com/shorts/{N}"\n---\n')
    _write(ideas / "inbox/notes/n.md", "---\nsource: https://example.com/a\n---\n")
    assert known_ids(session, ideas, docs) == {P, J, G, V, X, K, N}


def test_known_ids_with_no_folders_is_just_the_database(session, tmp_path):
    session.add(_item(J))
    session.flush()
    assert known_ids(session, tmp_path / "none", tmp_path / "nodocs") == {J}
