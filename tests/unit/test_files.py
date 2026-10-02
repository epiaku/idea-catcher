import threading
from contextlib import suppress

from catcher.core.files import file_lock, write_atomic


def test_write_atomic_writes_text_and_bytes_and_leaves_no_temp_file(tmp_path):
    target = tmp_path / "deep" / "file.md"
    write_atomic(target, "hello")
    assert target.read_text() == "hello"
    write_atomic(target, b"bytes")
    assert target.read_bytes() == b"bytes"
    assert [p.name for p in target.parent.iterdir()] == ["file.md"]


def test_a_failed_write_keeps_the_old_file(tmp_path, monkeypatch):
    target = tmp_path / "file.md"
    write_atomic(target, "old")

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("catcher.core.files.os.replace", boom)
    with suppress(OSError):
        write_atomic(target, "new")
    assert target.read_text() == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["file.md"]  # the temp file is gone too


def test_a_non_blocking_lock_says_no_while_another_holder_has_it(tmp_path):
    lock = tmp_path / "x.lock"
    with file_lock(lock, blocking=False) as first:
        assert first is True
        with file_lock(lock, blocking=False) as second:
            assert second is False
    with file_lock(lock, blocking=False) as again:
        assert again is True  # released


def test_a_blocking_lock_waits_for_the_holder(tmp_path):
    lock = tmp_path / "x.lock"
    order: list[str] = []
    started = threading.Event()

    def holder() -> None:
        with file_lock(lock):
            started.set()
            order.append("holder in")
        order.append("holder out")

    with file_lock(lock, blocking=False):
        thread = threading.Thread(target=holder)
        thread.start()
        assert not started.wait(0.2)  # cannot get in while we hold it
    thread.join(5)
    assert order == ["holder in", "holder out"]
