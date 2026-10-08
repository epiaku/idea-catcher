from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from catcher.core.db import session_scope
from catcher.modules.backfill import store
from catcher.modules.queue.models import BackfillVideo

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def test_add_pending_inserts_new_ids_only_and_counts_them(session):
    store.add_pending(session, {"a": ("docs", "ideas/a.md")}, NOW)
    added = store.add_pending(session, {"a": ("docs", "ideas/a.md"), "b": ("channel", "UC1")}, NOW)
    assert added == 1
    assert store.known(session) == {"a", "b"}
    assert store.counts(session) == {"pending": 2, "released": 0}


def test_add_pending_twice_is_a_noop(session):
    found = {"a": ("docs", "ideas/a.md"), "b": ("channel", "UC1")}
    assert store.add_pending(session, found, NOW) == 2
    assert store.add_pending(session, found, NOW + timedelta(hours=1)) == 0
    assert [row.found_at for row in store.pending(session)] == [NOW, NOW]
    assert store.add_pending(session, {}, NOW) == 0


def test_pending_is_oldest_first_and_honours_the_limit(session):
    store.add_pending(session, {"c": ("docs", "x")}, NOW + timedelta(hours=1))
    store.add_pending(session, {"b": ("docs", "x"), "a": ("docs", "x")}, NOW)
    assert [row.video_id for row in store.pending(session)] == ["a", "b", "c"]
    assert [row.video_id for row in store.pending(session, limit=2)] == ["a", "b"]
    store.mark_released(session, "a", NOW)
    assert [row.video_id for row in store.pending(session)] == ["b", "c"]


def test_mark_released_changes_a_pending_row_once(session):
    store.add_pending(session, {"a": ("docs", "x")}, NOW)
    later = NOW + timedelta(hours=2)
    assert store.mark_released(session, "a", later) is True
    assert store.mark_released(session, "a", later + timedelta(hours=1)) is False
    row = session.get(BackfillVideo, "a")
    assert (row.status, row.released_at) == ("released", later)
    assert store.counts(session) == {"pending": 0, "released": 1}


def test_mark_released_on_an_unknown_id_is_false(session):
    assert store.mark_released(session, "nope", NOW) is False


@pytest.mark.parametrize(("source", "status"), [("web", "pending"), ("docs", "bogus")])
def test_the_checks_refuse_a_bad_status_or_source(session, source, status):
    with pytest.raises(IntegrityError, match="ck_backfill_videos_"), session.begin_nested():
        session.add(BackfillVideo(video_id="a", source=source, status=status, found_at=NOW))


def test_two_sessions_adding_the_same_id_do_not_fail(pg_engine):
    with session_scope(pg_engine) as first, session_scope(pg_engine) as second:
        assert store.add_pending(first, {"a": ("docs", "x")}, NOW) == 1
        first.commit()
        assert store.add_pending(second, {"a": ("docs", "x")}, NOW) == 0
    with session_scope(pg_engine) as check:
        assert store.known(check) == {"a"}
