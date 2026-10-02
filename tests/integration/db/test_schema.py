import uuid
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from typer.testing import CliRunner

from catcher.cli import app
from catcher.core.db import alembic_config, make_engine
from catcher.modules.queue.models import Base

pytestmark = pytest.mark.db

TABLES = {"jobs", "job_items", "job_events", "resources", "schedules"}
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def _tables(url: str) -> set[str]:
    engine = make_engine(url)
    try:
        return set(inspect(engine).get_table_names()) - {"alembic_version"}
    finally:
        engine.dispose()


def test_upgrade_from_empty_creates_the_five_tables(fresh_database_url):
    assert _tables(fresh_database_url) == set()
    command.upgrade(alembic_config(fresh_database_url), "head")
    assert _tables(fresh_database_url) == TABLES


def test_downgrade_to_base_drops_them(fresh_database_url):
    config = alembic_config(fresh_database_url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    assert _tables(fresh_database_url) == set()


def test_models_and_migration_agree(pg_engine):
    with pg_engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []


def _job(session, status="queued", dedupe_key=None):
    session.execute(
        text(
            "insert into jobs (id, type, status, run_after, params, dedupe_key, created_at)"
            " values (:id, 'note', :status, :now, '{}', :key, :now)"
        ),
        {"id": uuid.uuid4(), "status": status, "key": dedupe_key, "now": NOW},
    )
    session.flush()


def test_a_bad_job_status_is_rejected(session):
    with pytest.raises(IntegrityError):
        _job(session, status="bogus")


def _item(session, name):
    session.execute(
        text(
            "insert into job_items (id, calculated_name, doc_id, doc_class, status, created_at, updated_at)"
            " values (:id, :name, 'd', 'note', 'staging', :now, :now)"
        ),
        {"id": uuid.uuid4(), "name": name, "now": NOW},
    )
    session.flush()


def test_two_items_cannot_share_a_calculated_name(session):
    _item(session, "ideas/a.md")
    with pytest.raises(IntegrityError):
        _item(session, "ideas/a.md")


def test_two_active_jobs_cannot_share_a_dedupe_key_but_a_finished_one_can(session):
    _job(session, "queued", "k")
    _job(session, "succeeded", "k")
    _job(session, "failed", "k")
    with pytest.raises(IntegrityError):
        _job(session, "running", "k")


def test_catcher_db_upgrade_runs_from_the_command_line(fresh_database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", fresh_database_url)
    runner = CliRunner()
    result = runner.invoke(app, ["db", "upgrade"])
    assert result.exit_code == 0, result.output
    assert _tables(fresh_database_url) == TABLES
    result = runner.invoke(app, ["db", "downgrade"])
    assert result.exit_code == 0, result.output
    assert _tables(fresh_database_url) == set()
