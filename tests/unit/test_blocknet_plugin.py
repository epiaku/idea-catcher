"""The network guard (tests/support/blocknet.py) blocks and records outgoing attempts and fails the session.

It runs a tiny pytest in a subprocess. The guard raises before any packet is sent, so nothing leaves
the machine.
"""

import os
import subprocess
import sys
from pathlib import Path

SUPPORT = Path(__file__).resolve().parents[1] / "support"

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


def run(tmp_path: Path, source: str, *, block: bool = True) -> subprocess.CompletedProcess[str]:
    (tmp_path / "test_inner.py").write_text(source)
    env = {k: v for k, v in os.environ.items() if k != "CATCHER_BLOCK_NETWORK"}
    env["PYTHONPATH"] = str(SUPPORT)
    if block:
        env["CATCHER_BLOCK_NETWORK"] = "1"
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


def test_the_guard_does_nothing_without_the_environment_variable(tmp_path):
    done = run(tmp_path, LOCAL, block=False)
    assert "BLOCKNET" not in done.stdout + done.stderr
    assert done.returncode == 0
