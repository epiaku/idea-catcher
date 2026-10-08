from collections.abc import Iterator

from fastapi import Request
from sqlalchemy import Engine
from sqlalchemy.orm import Session


def get_engine(request: Request) -> Engine:
    """The app's one engine, made by the lifespan in `create_app`."""
    return request.app.state.engine


def get_session(request: Request) -> Iterator[Session]:
    """One session per request, closed after it; a writer commits itself."""
    session = Session(get_engine(request), expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
