"""pytest plugin: block and record every outgoing connection except localhost and unix sockets.

Loaded for every run of this project's tests (tests/conftest.py) and ON by default, so a plain
`uv run pytest` can never reach YouTube, an LLM or any other outside host. The only way off is the explicit
switch for the manual live tests:

    CATCHER_ALLOW_NETWORK=1 uv run pytest -m live

With the switch the run says so, at the start and at the end: `BLOCKNET: guard OFF (CATCHER_ALLOW_NETWORK=1)`.
Postgres connections (psycopg uses libpq, C code) do not go through Python sockets: the guard never sees
them; the autouse `DATABASE_URL` on a closed local port in tests/conftest.py is what keeps tests off a real
database. Subprocesses (`catcher worker` in tests/integration/db/test_worker_cli.py) run without the guard.

(`CATCHER_BLOCK_NETWORK=1` still works and changes nothing: it is the old explicit switch.)
A blocked attempt raises RuntimeError("NETWORK BLOCKED: ...") before any packet is sent, DNS lookups
of non-local hosts included.
Every attempt is recorded, and the session FAILS if there was one, so a test that swallowed the error
cannot hide it.
"""

import ipaddress
import os
import socket

import pytest

ENV = "CATCHER_BLOCK_NETWORK"  # the old explicit switch: kept, it is on anyway
ALLOW_ENV = "CATCHER_ALLOW_NETWORK"  # the only way off: for the manual live tests
LOCAL_NAMES = {"localhost", "localhost.localdomain"}

ATTEMPTS: list[str] = []
_originals: dict[str, object] = {}


def _is_local_host(host: object) -> bool:
    if host is None or host in ("", b""):
        return True  # getaddrinfo(None, port): the loopback or wildcard address, no lookup
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    host = host.rstrip(".").lower()
    if host in LOCAL_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _block(what: str) -> RuntimeError:
    ATTEMPTS.append(what)
    return RuntimeError(f"NETWORK BLOCKED: {what}")


def _check_address(sock: socket.socket, address: object) -> None:
    if sock.family == socket.AF_UNIX or isinstance(address, str | bytes):
        return
    host = address[0] if isinstance(address, tuple) and address else address
    if not _is_local_host(host):
        raise _block(f"connection to {address!r}")


def install() -> None:
    if _originals:
        return
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create = socket.create_connection
    real_gai = socket.getaddrinfo
    _originals.update(connect=real_connect, connect_ex=real_connect_ex, create=real_create, gai=real_gai)

    def connect(self, address):
        _check_address(self, address)
        return real_connect(self, address)

    def connect_ex(self, address):
        _check_address(self, address)
        return real_connect_ex(self, address)

    def create_connection(address, *args, **kwargs):
        if not _is_local_host(address[0]):
            raise _block(f"connection to {address!r}")
        return real_create(address, *args, **kwargs)

    def getaddrinfo(host, *args, **kwargs):
        if not _is_local_host(host):
            raise _block(f"DNS lookup for {host!r}")
        return real_gai(host, *args, **kwargs)

    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
    socket.create_connection = create_connection
    socket.getaddrinfo = getaddrinfo


def uninstall() -> None:
    if not _originals:
        return
    socket.socket.connect = _originals.pop("connect")  # type: ignore[method-assign,assignment]
    socket.socket.connect_ex = _originals.pop("connect_ex")  # type: ignore[method-assign,assignment]
    socket.create_connection = _originals.pop("create")  # type: ignore[assignment]
    socket.getaddrinfo = _originals.pop("gai")  # type: ignore[assignment]


def _enabled() -> bool:
    return os.environ.get(ALLOW_ENV) != "1"


def pytest_configure(config: pytest.Config) -> None:
    if _enabled():
        install()


OFF_LINE = f"BLOCKNET: guard OFF ({ALLOW_ENV}=1)"


def _say(config: pytest.Config, line: str) -> None:
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(line)
    else:
        print(line)


def pytest_sessionstart(session: pytest.Session) -> None:
    if not _enabled():  # a shell that kept the switch after a live run must not run unguarded in silence
        _say(session.config, OFF_LINE)


def pytest_unconfigure(config: pytest.Config) -> None:
    uninstall()


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if not _enabled():
        _say(session.config, OFF_LINE)  # once more next to the summary, where it is read
        return
    line = f"BLOCKNET: {len(ATTEMPTS)} blocked outgoing attempt(s)"
    if ATTEMPTS:
        line += ": " + "; ".join(ATTEMPTS[:10])
    _say(session.config, line)
    if ATTEMPTS:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
