from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from alembic.config import Config
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from catcher.core.config import PROJECT_ROOT


def make_engine(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True)


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    """One unit of work: commit when the block ends, roll back when it raises."""
    session = Session(engine, expire_on_commit=False)
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def require_aware(moment: datetime) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("naive datetime")
    return moment


def utc_now() -> datetime:
    return datetime.now(UTC)


def alembic_config(url: str) -> Config:
    """The Alembic setup for one database; `migrations/env.py` reads the URL from the attribute."""
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.attributes["url"] = url
    return config
