"""The network guard (tests/support/blocknet.py) blocks and records outgoing attempts and fails the session.

It runs a tiny pytest in a subprocess. The guard raises before any packet is sent, so nothing leaves
the machine.
"""

import os
import subprocess
import sys
from pathlib import Path

SUPPORT = Path(__file__).resolve().parents[1] / "support"
PROBE = "tests/unit/blocknet_default_probe.py"  # not collected by name: only this test runs it, by path

EXTERNAL = """
import socket

def test_connect():
    try:
        socket.create_connection(("203.0.113.1", 80), timeout=1)
    except RuntimeError as e:  # swallowed on purpose: the session must still fail
        assert "NETWORK BLOCKED" in str(e)

def test_raw_connect():
    s = socket.socket()
    try:
        s.connect(("203.0.113.1", 80))
    except RuntimeError as e:
        assert "NETWORK BLOCKED" in str(e)

def test_dns():
    try:
        socket.getaddrinfo("example.com", 80)
    except RuntimeError as e:
        assert "NETWORK BLOCKED" in str(e)
"""

LOCAL = """
import socket

def test_localhost_is_allowed():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    client = socket.create_connection(server.getsockname(), timeout=2)
    client.close()
    server.close()
    socket.getaddrinfo("localhost", 80)
"""


def run(
    tmp_path: Path, source: str, *, block: bool = True, allow: bool = False
) -> subprocess.CompletedProcess[str]:
    """`block`: the old explicit switch (kept working); `allow`: the one opt-out for live tests. With
    neither, the guard must still be on: it is the default."""
    (tmp_path / "test_inner.py").write_text(source)
    env = {k: v for k, v in os.environ.items() if k not in ("CATCHER_BLOCK_NETWORK", "CATCHER_ALLOW_NETWORK")}
    env["PYTHONPATH"] = str(SUPPORT)
    if block:
        env["CATCHER_BLOCK_NETWORK"] = "1"
    if allow:
        env["CATCHER_ALLOW_NETWORK"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "blocknet", "-p", "no:cacheprovider", "-q", "test_inner.py"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_external_connections_and_dns_are_blocked_recorded_and_fail_the_session(tmp_path):
    done = run(tmp_path, EXTERNAL)
    out = done.stdout + done.stderr
    assert "3 passed" in out  # the tests swallowed the error ...
    assert "BLOCKNET: 3 blocked outgoing attempt(s)" in out  # ... the guard recorded it
    assert "203.0.113.1" in out and "example.com" in out
    assert done.returncode != 0  # ... and failed the session


def test_localhost_is_not_blocked(tmp_path):
    done = run(tmp_path, LOCAL)
    out = done.stdout + done.stderr
    assert "1 passed" in out
    assert "BLOCKNET: 0 blocked outgoing attempt(s)" in out
    assert done.returncode == 0


def test_the_guard_does_nothing_only_with_the_explicit_allow_switch(tmp_path):
    """The old default (nothing without the env var) is gone: only CATCHER_ALLOW_NETWORK=1 turns it off."""
    done = run(tmp_path, LOCAL, block=False, allow=True)
    assert "blocked outgoing attempt" not in done.stdout + done.stderr  # nothing is recorded
    assert done.returncode == 0


def test_a_run_with_the_guard_off_says_so_before_the_first_test(tmp_path):
    """A shell that kept CATCHER_ALLOW_NETWORK=1 after a live run must not run the suite silently unguarded.
    Only a localhost connection runs here, so nothing leaves the machine with the guard off."""
    done = run(tmp_path, LOCAL, block=False, allow=True)
    out = done.stdout + done.stderr
    assert "BLOCKNET: guard OFF (CATCHER_ALLOW_NETWORK=1)" in out
    assert out.index("BLOCKNET: guard OFF") < out.index("1 passed")  # at the start, not only in the summary
    assert done.returncode == 0
    on = run(tmp_path, LOCAL, block=False)
    assert "guard OFF" not in on.stdout + on.stderr


INSTALLED = """
import blocknet

def test_the_guard_is_installed():
    assert blocknet._originals != {}
"""


def test_the_guard_is_on_by_default_without_any_switch(tmp_path):
    """Checked without any real connection: if the guard were off, this test only reads a module flag."""
    done = run(tmp_path, INSTALLED, block=False)
    assert "1 passed" in done.stdout + done.stderr


NOT_INSTALLED = """
import blocknet

def test_the_guard_is_not_installed():
    assert blocknet._originals == {}
"""


def test_the_only_way_off_is_the_explicit_allow_switch(tmp_path):
    done = run(tmp_path, NOT_INSTALLED, block=False, allow=True)
    assert "1 passed" in done.stdout + done.stderr
    on = run(tmp_path, NOT_INSTALLED, block=False)
    assert "1 failed" in on.stdout + on.stderr  # without the switch the guard is installed (no connection)


def test_a_plain_pytest_run_of_this_project_loads_the_guard():
    """No `-p blocknet`, no env: tests/conftest.py loads it, so a plain `uv run pytest` is blocked too."""
    root = Path(__file__).resolve().parents[2]
    env = {k: v for k, v in os.environ.items() if k not in ("CATCHER_BLOCK_NETWORK", "CATCHER_ALLOW_NETWORK")}
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", PROBE],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert "1 passed" in done.stdout + done.stderr, done.stdout + done.stderr
