"""The Dockerfile and .dockerignore, checked as plain text (no Docker needed)."""

import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _dockerfile() -> str:
    return (ROOT / "Dockerfile").read_text(encoding="utf-8")


def _instructions() -> list[str]:
    """Dockerfile instructions with comments dropped and continuation lines joined."""
    text = re.sub(r"\\\n", " ", _dockerfile())
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


def test_the_dockerfile_installs_git_and_deno_and_pins_both_tool_versions():
    text = _dockerfile()
    assert text.startswith("# syntax") or "FROM python:3.12-slim-bookworm" in text
    for package in ("git", "openssh-client", "ca-certificates", "curl", "unzip"):
        assert re.search(rf"\b{package}\b", text)
    assert re.search(r"^ARG DENO_VERSION=\d+\.\d+\.\d+$", text, re.M)
    assert "denoland/deno/releases/download/v${DENO_VERSION}" in text
    assert re.search(r"^ARG UV_VERSION=\d+\.\d+\.\d+$", text, re.M)
    assert "ghcr.io/astral-sh/uv:${UV_VERSION}" in text
    assert "uv sync --frozen --no-dev" in text
    assert "safe.directory" in text


def test_the_dockerfile_runs_as_a_non_root_user():
    text = _dockerfile()
    assert re.search(r"useradd .*--uid 1000 .*catcher", text)
    users = [line for line in _instructions() if line.startswith("USER ")]
    assert users and users[-1] == "USER catcher"
    assert "/data/repos" in text


def test_the_dockerfile_never_copies_env_or_sets_a_secret():
    for line in _instructions():
        if line.startswith("COPY"):
            assert not re.search(r"(^|\s)\.env\b", line)
        if line.startswith(("ENV ", "ARG ")):
            assert not re.search(r"TOKEN|PASSWORD|SECRET|KEY", line, re.I), line


def test_the_dockerfile_has_no_node_claude_or_hugo():
    text = _dockerfile().lower()
    for word in ("nodejs", "npm", "claude", "hugo"):
        assert word not in text


def test_the_image_entrypoint_and_default_command():
    text = _dockerfile()
    assert 'ENTRYPOINT ["docker-entrypoint"]' in text
    assert 'CMD ["catcher", "worker"]' in text
    assert "ENV GIT_ASKPASS=/usr/local/bin/git-askpass" in text
    assert "scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint" in text
    assert "scripts/git-askpass.sh /usr/local/bin/git-askpass" in text
    assert "HEALTHCHECK" not in text


def test_dockerignore_keeps_secrets_and_state_out_of_the_build_context():
    entries = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    wanted = {
        ".env", ".env.*", ".git", ".venv", "tmp", "tests", "docs", "research_notes", "reports",
        ".superpowers", ".claude", ".cline", ".clinerules", ".roo", "*.log",
    }  # fmt: skip
    assert wanted <= entries
    assert not any(entry.startswith("!") for entry in entries)


def test_the_scripts_are_executable_files_in_git():
    out = subprocess.run(
        ["git", "ls-files", "--stage", "scripts/docker-entrypoint.sh", "scripts/git-askpass.sh"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    modes = [line.split()[0] for line in out.splitlines()]
    assert modes == ["100755", "100755"]


def _compose_text() -> str:
    return (ROOT / "compose.yaml").read_text(encoding="utf-8")


def _compose() -> dict:
    return yaml.safe_load(_compose_text())


def test_compose_has_db_migrate_and_worker_and_no_api_yet():
    assert set(_compose()["services"]) == {"db", "migrate", "worker"}
    migrate = _compose()["services"]["migrate"]
    assert migrate["command"] == ["catcher", "db", "upgrade"]
    assert migrate["restart"] == "no"
    assert migrate["depends_on"]["db"]["condition"] == "service_healthy"
    assert not any("repos" in v for v in migrate.get("volumes", []))


def test_migrate_skips_the_cloning_entrypoint():
    # Found by the smoke run: through the entrypoint, migrate cloned the repos into its own throwaway
    # filesystem (no repos volume) and failed when a remote was unreachable from it.
    assert _compose()["services"]["migrate"]["entrypoint"] == []
    assert "entrypoint" not in _compose()["services"]["worker"]


def test_the_worker_waits_for_the_migrations_to_complete():
    worker = _compose()["services"]["worker"]
    assert worker["command"] == ["catcher", "worker"]
    assert worker["depends_on"]["migrate"]["condition"] == "service_completed_successfully"


def test_the_worker_keeps_the_repos_in_a_named_volume_and_restarts_unless_stopped():
    compose = _compose()
    worker = compose["services"]["worker"]
    assert worker["restart"] == "unless-stopped"
    assert "repos:/data/repos" in worker["volumes"]
    assert "repos" in compose["volumes"]
    assert "catcher-pgdata" in compose["volumes"]


def test_the_container_environment_overrides_host_only_paths():
    env = _compose()["services"]["worker"]["environment"]
    assert re.search(r"@db:5432/", env["DATABASE_URL"])
    assert "localhost" not in env["DATABASE_URL"]
    assert env["IDEAS_REPO"] == "/data/repos/idea-bucket"
    assert env["DOCS_REPO"] == "/data/repos/epiaku-docs"
    assert env["IDEAS_REMOTE"] == "${IDEAS_REMOTE:-}"
    assert env["DOCS_REMOTE"] == "${DOCS_REMOTE:-}"


def test_no_secret_value_is_written_in_compose_yaml():
    env = _compose()["services"]["worker"]["environment"]
    assert env["GITHUB_TOKEN"] == "${GITHUB_TOKEN:-}"
    assert "${DB_PASSWORD:-catcher}" in env["DATABASE_URL"]
    text = _compose_text()
    assert not re.search(r"ghp_|github_pat_|sk-", text)
    assert _compose()["services"]["db"]["environment"]["POSTGRES_PASSWORD"] == "${DB_PASSWORD:-catcher}"


def test_the_db_port_is_configurable_and_bound_to_localhost_by_default():
    assert _compose()["services"]["db"]["ports"] == ["${DB_BIND:-127.0.0.1}:${DB_PORT:-5432}:5432"]


def test_the_db_restarts_with_docker_like_the_worker():
    # After a reboot Docker restarts the worker; without the db it would exit 2 and restart-loop.
    assert _compose()["services"]["db"]["restart"] == "unless-stopped"


def test_the_worker_gets_a_grace_period_to_finish_its_job():
    assert _compose()["services"]["worker"]["stop_grace_period"] == "120s"


def test_the_worker_has_a_catcher_health_healthcheck():
    check = _compose()["services"]["worker"]["healthcheck"]
    assert check["test"] == ["CMD", "catcher", "health"]
    timing = (check["interval"], check["timeout"], check["retries"], check["start_period"])
    assert timing == ("30s", "10s", 3, "60s")


def test_env_example_documents_every_variable_compose_reads():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    names = set(re.findall(r"\$\{([A-Z_]+)", _compose_text()))
    assert {"DB_BIND", "DB_PASSWORD", "DB_PORT", "IDEAS_REMOTE", "DOCS_REMOTE", "GITHUB_TOKEN"} <= names
    for name in names:
        assert re.search(rf"^#? ?{name}=", example, re.M), name


def test_the_smoke_overrides_never_read_env_call_an_llm_or_youtube_or_use_port_5432():
    text = (ROOT / "compose.test.yaml").read_text(encoding="utf-8")
    assert "env_file: !reset []" in text
    assert "5432:5432" not in text and "${DB_PORT:?" in text
    assert "CATCHER_ALLOW_NETWORK" not in text
    for line in (
        'OPENAI_API_KEY: ""',
        "OPENAI_BASE_URL: http://127.0.0.1:1/v1",
        "FREELLMAPI_URL: http://127.0.0.1:1/v1",
        'YOUTUBE_OFFLINE: "1"',
        "IDEAS_REMOTE: /remotes/idea-bucket.git",
        "image: ${SMOKE_IMAGE:?",
    ):
        assert line in text, line
    assert not re.search(r"ghp_|github_pat_|sk-", text)
    script = ROOT / "scripts" / "compose-smoke"
    assert script.stat().st_mode & 0o111
    body = script.read_text(encoding="utf-8")
    assert "set -euo pipefail" in body and "trap teardown EXIT" in body
    assert 'docker compose -p "$PROJECT"' in body
    assert "prune" not in body
