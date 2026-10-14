"""The backfill logic shared by the CLI and the worker (real Postgres; no YouTube, no LLM)."""

from datetime import UTC, datetime, timedelta

import pytest

from catcher.modules.backfill import store
from catcher.modules.backfill.service import release_within, store_listing
from catcher.modules.queue.models import BackfillVideo, JobItem

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
KNOWN, NEW1, NEW2 = (f"{c * 10}1" for c in "knm")
URL = "https://www.youtube.com/@chan"


def test_store_listing_skips_known_and_stores_the_rest(session, tmp_path) -> None:
    session.add(
        JobItem(
            calculated_name=f"youtube/{KNOWN}.md",
            doc_id=KNOWN,
            doc_class="youtube",
            status="published",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.flush()

    result = store_listing(session, [KNOWN, NEW1, NEW2], URL, tmp_path / "ideas", tmp_path / "docs", NOW)

    assert result == (1, 2)
    for vid in (NEW1, NEW2):
        row = session.get(BackfillVideo, vid)
        assert row is not None
        assert (row.source, row.found_in) == ("channel", URL)
    assert session.get(BackfillVideo, KNOWN) is None


def _pending(session, count: int) -> list[str]:
    ids = [f"p{n:010d}1"[:11] for n in range(count)]
    for n, vid in enumerate(ids):
        store.add_pending(session, {vid: ("docs", f"{vid}.md")}, NOW - timedelta(days=1, seconds=-n))
    return ids


def test_release_within_respects_the_rolling_cap(session, tmp_path) -> None:
    ideas = tmp_path / "ideas"
    ids = _pending(session, 13)
    for vid in ids[:3]:
        store.mark_released(session, vid, NOW - timedelta(hours=1))

    allowance, result = release_within(session, ideas, 5, NOW)

    assert allowance == 2
    assert len(result.released) == 2
    assert len(list((ideas / "inbox" / "clippings").glob("*.md"))) == 2


def test_release_within_zero_cap_releases_nothing(session, tmp_path) -> None:
    ideas = tmp_path / "ideas"
    _pending(session, 3)

    allowance, result = release_within(session, ideas, 0, NOW)

    assert allowance == 0
    assert result.released == []
    assert not (ideas / "inbox" / "clippings").exists()
