"""One worker at a time: a Postgres advisory lock on a dedicated connection."""

import pytest
from sqlalchemy import Engine, text

from catcher.modules.worker.guard import WorkerAlreadyRunning, WorkerLock


def test_a_second_worker_lock_is_refused(pg_engine: Engine) -> None:
    with (
        WorkerLock(pg_engine),
        pytest.raises(WorkerAlreadyRunning, match="one worker at a time"),
        WorkerLock(pg_engine),
    ):
        pytest.fail("the second lock must not be entered")


def test_the_lock_is_released_on_exit_and_can_be_taken_again(pg_engine: Engine) -> None:
    with WorkerLock(pg_engine):
        pass
    with WorkerLock(pg_engine):
        pass


def test_the_lock_is_released_when_the_holder_connection_dies(pg_engine: Engine) -> None:
    holder = WorkerLock(pg_engine)
    holder.__enter__()
    with pg_engine.connect() as admin:
        admin.execute(text("select pg_terminate_backend(:pid)"), {"pid": holder.backend_pid})
        admin.commit()
    try:
        with WorkerLock(pg_engine):
            pass
    finally:
        holder.__exit__(None, None, None)  # the dead connection must not raise
