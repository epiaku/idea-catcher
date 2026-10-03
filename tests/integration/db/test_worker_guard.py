"""One worker at a time: a Postgres advisory lock on a dedicated connection."""

import time

import pytest
from sqlalchemy import Engine, text

from catcher.modules.worker.guard import WorkerAlreadyRunning, WorkerLock, WorkerLockLost


def _backend_is_gone(engine: Engine, pid: int | None, wait_s: float = 10.0) -> bool:
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        with engine.connect() as admin:
            if (
                admin.execute(text("select 1 from pg_stat_activity where pid = :pid"), {"pid": pid}).first()
                is None
            ):
                return True
        time.sleep(0.05)
    return False


def test_a_second_worker_lock_is_refused(pg_engine: Engine) -> None:
    with WorkerLock(pg_engine):
        with pytest.raises(WorkerAlreadyRunning, match="one worker at a time"):
            WorkerLock(pg_engine).__enter__()
        assert pg_engine.pool.checkedout() == 1  # only the holder; the refused attempt returned its own


def test_a_failed_unlock_drops_the_backend_so_the_lock_is_not_kept(
    pg_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder = WorkerLock(pg_engine)
    holder.__enter__()
    connection = holder._connection
    assert connection is not None
    real_execute = connection.execute

    def failing_execute(statement, *args, **kwargs):
        if "pg_advisory_unlock" in str(statement):
            raise RuntimeError("unlock interrupted")
        return real_execute(statement, *args, **kwargs)

    monkeypatch.setattr(connection, "execute", failing_execute)
    holder.__exit__(None, None, None)  # must not raise
    assert _backend_is_gone(pg_engine, holder.backend_pid)
    with WorkerLock(pg_engine):
        pass


def test_an_unlock_that_returns_false_drops_the_backend(
    pg_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    holder = WorkerLock(pg_engine, key=1)
    holder.__enter__()
    holder._key = 2  # releases a key it does not hold, so Postgres answers false
    with caplog.at_level("WARNING", logger="catcher.worker"):
        holder.__exit__(None, None, None)
    assert "not held" in caplog.text
    assert _backend_is_gone(pg_engine, holder.backend_pid)


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
    assert _backend_is_gone(pg_engine, holder.backend_pid)  # terminate only signals; wait for the exit
    try:
        with WorkerLock(pg_engine):
            pass
    finally:
        holder.__exit__(None, None, None)  # the dead connection must not raise


def test_check_passes_while_the_lock_is_held(pg_engine: Engine) -> None:
    with WorkerLock(pg_engine) as lock:
        lock.check()
        lock.check()


def test_check_raises_once_the_lock_connection_is_gone(pg_engine: Engine) -> None:
    with WorkerLock(pg_engine) as lock:
        with pg_engine.connect() as admin:
            admin.execute(text("select pg_terminate_backend(:pid)"), {"pid": lock.backend_pid})
            admin.commit()
        assert _backend_is_gone(pg_engine, lock.backend_pid)
        with pytest.raises(WorkerLockLost, match="lost its database lock"):
            lock.check()
        with pytest.raises(WorkerLockLost):  # stays lost: a reconnected session would not hold the lock
            lock.check()


def test_check_raises_when_the_lock_was_released_on_its_connection(pg_engine: Engine) -> None:
    with WorkerLock(pg_engine, key=7) as lock:
        assert lock._connection is not None
        lock._connection.execute(text("select pg_advisory_unlock(7)"))
        with pytest.raises(WorkerLockLost):
            lock.check()
