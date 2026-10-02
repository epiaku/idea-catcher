"""Small file helpers: a write that is never half done, and a lock that works on every system."""

import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

if sys.platform == "win32":
    import msvcrt

    def _try_lock(handle: int) -> bool:
        try:
            os.lseek(handle, 0, os.SEEK_SET)
            msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(handle: int) -> None:
        os.lseek(handle, 0, os.SEEK_SET)
        msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(handle: int) -> bool:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(handle: int) -> None:
        fcntl.flock(handle, fcntl.LOCK_UN)


def write_atomic(path: Path, content: str | bytes) -> None:
    """Write a file so that a crash or Ctrl-C leaves the old file or the new one, never half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")  # a dot name: no scan or glob picks it up
    try:
        if isinstance(content, bytes):
            temp.write_bytes(content)
        else:
            temp.write_text(content, encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def file_lock(path: Path, *, blocking: bool = True, poll_s: float = 0.05) -> Iterator[bool]:
    """An exclusive lock on `path` (a small file next to the thing it protects), shared by every process on
    the machine. Yields True when the lock is held. With `blocking=False` it yields False at once when
    another process holds it; with `blocking=True` it waits, so it always yields True."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as lock:
        handle = lock.fileno()
        held = _try_lock(handle)
        while not held and blocking:
            time.sleep(poll_s)
            held = _try_lock(handle)
        try:
            yield held
        finally:
            if held:
                _unlock(handle)
