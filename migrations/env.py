"""Alembic environment. The URL comes from, in order: `config.attributes["url"]` (programmatic callers
and the tests), `-x url=...` on the command line, then `Settings().database_url`."""

from alembic import context
from sqlalchemy import create_engine

from catcher.core.config import Settings
from catcher.modules.queue.models import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    given = config.attributes.get("url") or context.get_x_argument(as_dictionary=True).get("url")
    return given or Settings().database_url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url())
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
