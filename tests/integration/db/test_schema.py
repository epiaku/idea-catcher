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

TABLES = {"jobs", "job_items", "job_events", "resources", "schedules", "backfill_videos"}
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def _tables(url: str) -> set[str]:
    engine = make_engine(url)
    try:
        return set(inspect(engine).get_table_names()) - {"alembic_version"}
    finally:
        engine.dispose()


def test_upgrade_from_empty_creates_the_six_tables(fresh_database_url):
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
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "compare_server_default": True}
        )
        assert compare_metadata(context, Base.metadata) == []


CATALOG_SQL = {
    "indexes": "select tablename || ' ' || indexdef from pg_indexes where schemaname = 'public'"
    " and tablename <> 'alembic_version'",
    "constraints": "select conrelid::regclass || ' ' || conname || ' ' || pg_get_constraintdef(oid)"
    " from pg_constraint where connamespace = 'public'::regnamespace"
    " and conrelid::regclass::text <> 'alembic_version'",
    "columns": "select table_name || '.' || column_name || ' ' || data_type || ' null=' || is_nullable"
    " || ' default=' || coalesce(column_default, '-') from information_schema.columns"
    " where table_schema = 'public' and table_name <> 'alembic_version'",
}


def _catalog(engine) -> dict[str, list[str]]:
    with engine.connect() as connection:
        return {
            name: sorted(row[0] for row in connection.execute(text(sql))) for name, sql in CATALOG_SQL.items()
        }


def test_migration_and_models_have_the_same_indexes_checks_and_defaults(pg_engine, fresh_database_url):
    """Autogenerate ignores partial-index predicates and CHECK constraints, so compare the catalogs."""
    created = make_engine(fresh_database_url)
    try:
        Base.metadata.create_all(created)
        from_models = _catalog(created)
    finally:
        created.dispose()
    from_migration = _catalog(pg_engine)
    assert from_migration == from_models
    joined = "\n".join(from_migration["indexes"] + from_migration["constraints"])
    for expected in (
        "status = 'queued'",
        "'queued'::text, 'running'::text",
        "ck_jobs_status",
        "ck_job_items_status",
        "ck_job_events_level",
    ):
        assert expected in joined


def _job(session, status="queued", dedupe_key=None):
    lease = (
        NOW if status == "running" else None
    )  # a running job must hold a lease (ck_jobs_running_has_lease)
    session.execute(
        text(
            "insert into jobs (id, type, status, run_after, params, dedupe_key, created_at, lease_until)"
            " values (:id, 'note', :status, :now, '{}', :key, :now, :lease)"
        ),
        {"id": uuid.uuid4(), "status": status, "key": dedupe_key, "now": NOW, "lease": lease},
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
    result = runner.invoke(app, ["db", "downgrade", "base", "--yes"])
    assert result.exit_code == 0, result.output
    assert _tables(fresh_database_url) == set()


def _upgraded_cli(fresh_database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", fresh_database_url)
    runner = CliRunner()
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    return runner


def _version(url: str) -> str | None:
    engine = make_engine(url)
    try:
        with engine.connect() as connection:
            return connection.execute(text("select version_num from alembic_version")).scalar()
    finally:
        engine.dispose()


def test_downgrade_without_a_revision_is_a_usage_error_and_drops_nothing(fresh_database_url, monkeypatch):
    runner = _upgraded_cli(fresh_database_url, monkeypatch)
    result = runner.invoke(app, ["db", "downgrade"])
    assert result.exit_code == 2
    assert _tables(fresh_database_url) == TABLES


def test_downgrade_to_base_asks_first_and_a_no_changes_nothing(fresh_database_url, monkeypatch):
    runner = _upgraded_cli(fresh_database_url, monkeypatch)
    result = runner.invoke(app, ["db", "downgrade", "base"], input="n\n")
    assert result.exit_code != 0
    assert _tables(fresh_database_url) == TABLES


def test_downgrade_to_base_with_yes_drops_without_asking(fresh_database_url, monkeypatch):
    runner = _upgraded_cli(fresh_database_url, monkeypatch)
    result = runner.invoke(app, ["db", "downgrade", "base", "--yes"])
    assert result.exit_code == 0, result.output
    assert _tables(fresh_database_url) == set()


def test_downgrade_to_base_after_answering_yes_drops(fresh_database_url, monkeypatch):
    runner = _upgraded_cli(fresh_database_url, monkeypatch)
    result = runner.invoke(app, ["db", "downgrade", "base"], input="y\n")
    assert result.exit_code == 0, result.output
    assert _tables(fresh_database_url) == set()


def test_downgrade_minus_one_steps_back_one_migration_without_asking(fresh_database_url, monkeypatch):
    runner = _upgraded_cli(fresh_database_url, monkeypatch)
    head = _version(fresh_database_url)
    result = runner.invoke(app, ["db", "downgrade", "-1"])
    assert result.exit_code == 0, result.output
    assert _version(fresh_database_url) not in (head, None)
    assert _tables(fresh_database_url) == TABLES - {"backfill_videos"}


def test_a_bad_item_status_is_rejected(session):
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "insert into job_items (id, calculated_name, doc_id, doc_class, status, created_at,"
                " updated_at) values (:id, 'x/y.md', 'd', 'note', 'bogus', :now, :now)"
            ),
            {"id": uuid.uuid4(), "now": NOW},
        )


def test_a_bad_event_level_is_rejected(session):
    with pytest.raises(IntegrityError):
        session.execute(
            text("insert into job_events (ts, level, message) values (:now, 'fatal', 'm')"), {"now": NOW}
        )


def _insert_job(session, **values):
    row = {"status": "queued", "attempts": 0, "claim_seq": 0, "lease": None} | values
    session.execute(
        text(
            "insert into jobs (id, type, status, run_after, params, created_at, attempts, claim_seq,"
            " lease_until) values (:id, 'note', :status, :now, '{}', :now, :attempts, :claim_seq, :lease)"
        ),
        {"id": uuid.uuid4(), "now": NOW} | row,
    )
    session.flush()


@pytest.mark.parametrize("column", ["attempts", "claim_seq"])
def test_the_job_counters_cannot_go_negative(session, column):
    _insert_job(session, **{column: 0})
    with pytest.raises(IntegrityError, match="ck_jobs_counters_non_negative"):
        _insert_job(session, **{column: -1})


def test_a_running_job_needs_a_lease_but_a_queued_one_does_not(session):
    _insert_job(session, status="queued")
    _insert_job(session, status="running", lease=NOW)
    with pytest.raises(IntegrityError, match="ck_jobs_running_has_lease"):
        _insert_job(session, status="running", lease=None)


def test_a_job_cannot_be_moved_to_running_without_a_lease(session):
    _insert_job(session, status="queued")
    with pytest.raises(IntegrityError, match="ck_jobs_running_has_lease"):
        session.execute(text("update jobs set status = 'running'"))


def test_an_item_origin_must_be_inbox_or_backfill(session):
    sql = text(
        "insert into job_items (id, calculated_name, doc_id, doc_class, origin, status, created_at,"
        " updated_at) values (:id, :name, 'd', 'note', :origin, 'staging', :now, :now)"
    )
    for name, origin in (("a/1.md", "inbox"), ("a/2.md", "backfill")):
        session.execute(sql, {"id": uuid.uuid4(), "name": name, "origin": origin, "now": NOW})
    with pytest.raises(IntegrityError, match="ck_job_items_origin"):
        session.execute(sql, {"id": uuid.uuid4(), "name": "a/3.md", "origin": "web", "now": NOW})


def test_the_migration_0003_indexes_exist(pg_engine):
    with pg_engine.connect() as connection:
        defs = dict(
            connection.execute(
                text("select indexname, indexdef from pg_indexes where schemaname = 'public'")
            ).all()
        )
    assert defs["ix_job_events_job_id"].endswith("ON public.job_events USING btree (job_id)")
    assert defs["ix_job_events_item_id"].endswith("ON public.job_events USING btree (item_id)")
    assert defs["ix_job_items_root_job_id"].endswith("ON public.job_items USING btree (root_job_id)")
    assert defs["ix_jobs_running_lease"].endswith(
        "ON public.jobs USING btree (lease_until) WHERE (status = 'running'::text)"
    )


def _youtube_rows(url: str) -> list[tuple]:
    engine = make_engine(url)
    try:
        with engine.connect() as connection:
            return [
                tuple(row)
                for row in connection.execute(
                    text(
                        "select name, next_allowed_at, blocked_until, blocked_at, streak, concurrency,"
                        " updated_at from resources where name = 'youtube'"
                    )
                )
            ]
    finally:
        engine.dispose()


def test_upgrade_seeds_the_open_youtube_row_and_downgrade_removes_it(fresh_database_url):
    config = alembic_config(fresh_database_url)
    command.upgrade(config, "0003")
    assert _youtube_rows(fresh_database_url) == []
    command.upgrade(config, "0004")
    assert _youtube_rows(fresh_database_url) == [
        ("youtube", None, None, None, 0, 1, datetime(2026, 10, 4, tzinfo=UTC))
    ]
    command.downgrade(config, "-1")
    assert _youtube_rows(fresh_database_url) == []
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    assert _tables(fresh_database_url) == set()
    command.upgrade(config, "head")
    assert len(_youtube_rows(fresh_database_url)) == 1


def test_every_db_test_starts_with_the_open_youtube_row(pg_engine):
    with pg_engine.connect() as connection:
        rows = connection.execute(
            text(
                "select next_allowed_at, blocked_until, blocked_at, streak, concurrency from resources"
                " where name = 'youtube'"
            )
        ).all()
    assert [tuple(row) for row in rows] == [(None, None, None, 0, 1)]


def _columns(url: str, table: str) -> set[str]:
    engine = make_engine(url)
    try:
        return {column["name"] for column in inspect(engine).get_columns(table)}
    finally:
        engine.dispose()


def test_upgrade_adds_stage_since_and_reason_and_downgrade_removes_them(fresh_database_url):
    config = alembic_config(fresh_database_url)
    command.upgrade(config, "0004")
    assert "stage_since" not in _columns(fresh_database_url, "job_items")
    assert "reason" not in _columns(fresh_database_url, "resources")
    updated = datetime(2026, 10, 3, 8, 30, tzinfo=UTC)
    engine = make_engine(fresh_database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "insert into job_items (id, calculated_name, doc_id, doc_class, status, created_at,"
                    " updated_at) values (:id, 'ideas/old.md', 'd', 'note', 'deferred', :created, :updated)"
                ),
                {"id": uuid.uuid4(), "created": NOW, "updated": updated},
            )

        command.upgrade(config, "0005")

        with engine.connect() as connection:
            assert connection.execute(text("select stage_since from job_items")).scalar_one() == updated
            assert connection.execute(text("select reason from resources")).scalar_one() is None
    finally:
        engine.dispose()
    command.downgrade(config, "0004")
    assert "stage_since" not in _columns(fresh_database_url, "job_items")
    assert "reason" not in _columns(fresh_database_url, "resources")


def test_the_migration_upgrades_from_head_and_downgrades(fresh_database_url):
    config = alembic_config(fresh_database_url)
    command.upgrade(config, "0005")
    assert "backfill_videos" not in _tables(fresh_database_url)
    command.upgrade(config, "0006")
    assert _version(fresh_database_url) == "0006"
    assert "backfill_videos" in _tables(fresh_database_url)
    assert _columns(fresh_database_url, "backfill_videos") == {
        "video_id",
        "source",
        "found_in",
        "status",
        "found_at",
        "released_at",
    }
    command.downgrade(config, "0005")
    assert _version(fresh_database_url) == "0005"
    assert "backfill_videos" not in _tables(fresh_database_url)
    command.upgrade(config, "head")
    assert _version(fresh_database_url) == "0006"
