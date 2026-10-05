from contextlib import suppress

from catcher.core.files import write_atomic


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
