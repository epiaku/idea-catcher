import pytest
from sqlalchemy import text

from catcher.core.db import session_scope

pytestmark = pytest.mark.db


def test_the_database_fixture_answers_select_1_on_postgres_17(pg_engine):
    with session_scope(pg_engine) as session:
        assert "PostgreSQL 17" in session.execute(text("select version()")).scalar_one()


def test_session_scope_commits_and_rolls_back(pg_engine):
    with session_scope(pg_engine) as session:
        session.execute(text("create table if not exists scope_probe (n int)"))
    with session_scope(pg_engine) as session:
        session.execute(text("insert into scope_probe values (1)"))
    with pytest.raises(RuntimeError), session_scope(pg_engine) as session:
        session.execute(text("insert into scope_probe values (2)"))
        raise RuntimeError("boom")
    with session_scope(pg_engine) as session:
        assert session.execute(text("select n from scope_probe order by n")).scalars().all() == [1]
        session.execute(text("drop table scope_probe"))


def test_the_clock_is_frozen_and_advances(clock):
    first = clock()
    assert first.isoformat() == "2026-10-02T12:00:00+00:00"
    assert clock() == first
    clock.advance(90)
    assert clock().isoformat() == "2026-10-02T12:01:30+00:00"
