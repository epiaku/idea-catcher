"""Ctrl-C or SIGTERM during an unattended git command: the child runs in its own session, so the signal never
reaches it. The interrupt must stop its whole process group and propagate, not leave git running."""

import os
import subprocess
import time
from pathlib import Path

import pytest

from catcher.core import git as gitmod


def _gone(pid: int) -> bool:
    for _ in range(50):  # allow a moment for the orphan to be reaped
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


def test_an_interrupt_stops_the_whole_process_group_and_propagates(tmp_path: Path, monkeypatch) -> None:
    pid_file = tmp_path / "grandchild.pid"
    script = f"sleep 30 & echo $! > '{pid_file}'; wait"

    class InterruptedPopen(subprocess.Popen):
        calls = 0

        def communicate(self, input=None, timeout=None):  # noqa: A002 - the signature of Popen
            InterruptedPopen.calls += 1
            if InterruptedPopen.calls == 1:  # the first wait: Ctrl-C arrives while git runs
                for _ in range(50):
                    if pid_file.exists() and pid_file.read_text().strip():
                        break
                    time.sleep(0.05)
                raise KeyboardInterrupt
            return super().communicate(input, timeout)

    monkeypatch.setattr(gitmod.subprocess, "Popen", InterruptedPopen)
    monkeypatch.setattr(gitmod, "KILL_GRACE_S", 3)

    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        gitmod._run_unattended(["sh", "-c", script], cwd=tmp_path, env=dict(os.environ), timeout=60)
    assert time.monotonic() - started < 10

    assert _gone(int(pid_file.read_text())), "the grandchild outlived the interrupt"
