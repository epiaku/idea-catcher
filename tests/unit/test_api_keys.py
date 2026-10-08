import pytest
from typer.testing import CliRunner

from catcher.api.auth import ApiKey, parse_api_keys
from catcher.cli import app

MAC = "mac-key-0123456789abcdefghijkl"  # 30 characters
PHONE = "phone-key-0123456789abcdefghij"  # 30 characters


def test_parse_api_keys_accepts_the_documented_format():
    keys = parse_api_keys(f"mac:read,run:{MAC}  \n phone:read:{PHONE}\t")

    assert keys == [
        ApiKey(name="mac", scopes=frozenset({"read", "run"}), secret=MAC),
        ApiKey(name="phone", scopes=frozenset({"read"}), secret=PHONE),
    ]
    # a key may hold a colon: everything after the second colon is the key
    assert parse_api_keys(f"ci_bot-1:run:{MAC}:x")[0].secret == f"{MAC}:x"
    # the secret never shows in a repr (a log line or a traceback that prints the object)
    assert MAC not in repr(keys) and MAC not in str(keys)


@pytest.mark.parametrize(
    ("raw", "problem"),
    [
        ("", "no keys"),
        ("   \n\t ", "no keys"),
        (f"mac:read:{MAC[:23]}", "at least 24"),
        (f"mac:read:{MAC} mac:run:{PHONE}", "duplicate name"),
        (f"mac:read:{MAC} phone:run:{MAC}", "duplicate key"),
        (MAC, "name:scope"),
        (f"mac:{MAC}", "name:scope"),
        (f"Mac:read:{MAC}", "name"),
        (f"{'a' * 33}:read:{MAC}", "name"),
        (f"mac:write:{MAC}", "scope"),
        (f"mac::{MAC}", "scope"),
        (f"mac:read,:{MAC}", "scope"),
        (f"mac:read:{MAC}é", "printable"),
        (f"mac:{MAC}:{PHONE}", "scope"),
    ],
)
def test_parse_api_keys_rejects_empty_short_duplicate_and_malformed_entries_without_echoing_the_key(
    raw, problem
):
    with pytest.raises(ValueError) as caught:
        parse_api_keys(raw)

    message = str(caught.value)
    assert problem in message
    assert "API_KEYS" in message
    for secret in (MAC, PHONE):
        for part in (secret, secret[:8], secret[-8:], secret[:23]):
            assert part not in message


def test_catcher_api_exits_2_without_keys_or_with_the_default_database_url(monkeypatch):
    started = []
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: started.append(kwargs))
    runner = CliRunner()

    # no keys at all
    monkeypatch.delenv("API_KEYS", raising=False)
    result = runner.invoke(app, ["api"])
    assert result.exit_code == 2
    assert "API_KEYS" in result.output

    # a short key: the message names the setting and the problem, never the key
    monkeypatch.setenv("API_KEYS", f"mac:read:{MAC[:20]}")
    result = runner.invoke(app, ["api"])
    assert result.exit_code == 2
    assert "API_KEYS" in result.output
    assert MAC[:20] not in result.output and MAC[:8] not in result.output

    # valid keys, but DATABASE_URL not set: the built-in development URL is refused
    monkeypatch.setenv("API_KEYS", f"mac:read,run:{MAC}")
    monkeypatch.delenv("DATABASE_URL")
    result = runner.invoke(app, ["api"])
    assert result.exit_code == 2
    assert "DATABASE_URL" in result.output
    assert MAC not in result.output and MAC[:8] not in result.output

    # a malformed DATABASE_URL
    monkeypatch.setenv("DATABASE_URL", "not a url")
    result = runner.invoke(app, ["api"])
    assert result.exit_code == 2
    assert "DATABASE_URL" in result.output

    assert started == []


def test_catcher_api_starts_uvicorn_on_the_given_address_with_the_project_logging(monkeypatch):
    started = []
    monkeypatch.setattr("uvicorn.run", lambda app_, **kwargs: started.append((app_, kwargs)))
    monkeypatch.setenv("API_KEYS", f"mac:read,run:{MAC}")

    result = CliRunner().invoke(app, ["api", "--host", "127.0.0.1", "--port", "8123"])

    assert result.exit_code == 0, result.output
    [(server_app, kwargs)] = started
    assert kwargs == {"host": "127.0.0.1", "port": 8123, "log_config": None}
    assert [key.name for key in server_app.state.api_keys] == ["mac"]
    assert MAC not in result.output
