# Idea Catcher Stage A Implementation Plan

> **Note (2026-10-03):** the epiaku-docs layout changed after this plan: the idea-bucket pages go to `notes/`, `youtube/` or `web-clips/` only (no `clippings/`), chosen by a `destination` field. This plan is kept as written; see the decision of 2026-10-03 in `docs/idea-catcher-service-architecture.md`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local Python CLI (`catcher`) that turns captures in the `idea-bucket` repo into validated Hugo pages in `epiaku-docs`: ingest, the LLM step through API-key backends (FreeLLMApi and the OpenAI API, three profiles), the YouTube reviewer, rendering, filing and committing. There is no database, Docker or API yet.

**Architecture:** The core logic is plain functions with no knowledge of a queue or API: `ingest_inbox()`, `reason()`, `review_summary()`, `render_page()`, `write_page()` / `finalize_output()` / `move_to_failed()`, `commit_paths()`. A thin Typer CLI calls them. Stage B will wrap these same functions in Postgres jobs, and Stage C will put an API in front. Python does everything deterministic: IDs, classes, paths, frontmatter, metrics and Git. The LLM only returns JSON, which Pydantic validates.

**Tech Stack:** Python 3.12, uv, Typer, Pydantic v2 + pydantic-settings, Jinja2, PyYAML, OpenAI SDK (FreeLLMApi and the OpenAI API), yt-dlp, youtube-transcript-api 1.x, pytest + respx + syrupy, ruff, pyright, pre-commit.

> **LLM access revised on 2026-09-28.** Every LLM call is now an API call with an API key. `claude -p` (subscription) is removed, together with the `when` / evening option. There are three profiles: `notes` → FreeLLMApi, `clippings` (AI chats) → OpenAI API, `youtube` (both YouTube classes and the reviewer) → OpenAI API. Tasks 1, 6, 7, 8, 9, 12, 14, 16 and 18 still show the old `claude-code` backend and the profile names `free-fast`, `claude-sub-evening` and `claude-sub-now`; they are kept as history. **Task 20 does the refactor**, and where they disagree, Task 20 wins.

> **Folder layout revised on 2026-09-28.** Tasks 1–18 were built on a `staging/` folder. The design now uses four folders: `inbox/` → `archive/` (untouched copy), `output/` (in progress, then the final page) and `failed/`, each with `notes/` and `clippings/`, and a file keeps its inbox name everywhere. The state per document moves to Postgres in Stage B; in Stage A it is the folder, a `stage` frontmatter field and the Python log. **Task 19 does the refactor**, so the code samples in Tasks 4, 12, 14, 15, 16 and 18 still show `staging/` and are kept as history. Where they disagree with Task 19 and the [design](../../idea-catcher-pipeline.md#repo-layout), Task 19 wins.

> **Outcome (2026-10-01).** Stage A is done. The result differs from this plan in several places (the separate reviewer was replaced by free Python checks, `staging/` by the inbox-only flow, the idea-type tag became optional, and more was added: `--requeue`, retries, a glossary, translation, chapters and links, a business context). See [Stage A: what we built and what we learned](../../idea-catcher-stage-a-lessons-learned.md). This plan is kept as the record of what was intended.

**Spec:** `docs/idea-catcher-service-architecture.md` (Stage A steps A1–A8, LLM Step & Profiles, Reviewer, Processing Flow, Testing) and `docs/idea-catcher-pipeline.md` (Implementation Reference, Stage 3 ID & naming, Stage 4 document classes, Stage 5 tag rules).

## Global Constraints

- Python `>=3.12` (the production image uses Python 3.12 + uv); dependencies are managed with **uv**.
- **Raw first:** the captured text (the note body) is never changed, and the original of every processed inbox file is kept in `archive/` under its calculated file name, with only two frontmatter lines added (`original_filename`, `calculated_filename`). Ingest only adds frontmatter fields to the `output/` copy: `id`, `class`, `captured`, `source_file`, `analyzed_at`, `stage`.
- **Five folders, one rule, and a run only looks at `inbox/` for work.** `inbox/`, `archive/`, `output/`, `failed/`, `duplicates/`, each with the same subfolders (`notes/`, `clippings/`), and the file name never changes. A document leaves `inbox/` right before its own LLM step: the untouched original goes to `archive/` and a working copy (`stage: analyzed`) to `output/`, which becomes the final page. A permanent failure moves to `failed/` (with an `.error.txt`), an earlier snapshot of a longer clip to `duplicates/`, and a temporary error leaves the working copy in `output/` with `stage: deferred` and the reason. The pipeline never reads `output/`: **to retry, move the file from `archive/` back into `inbox/`** and the next run overwrites the stalled copy. There is no `staging/` and no `superseded/` folder. A helper such as `catcher requeue` is a possible later addition.
- **One stable ID per capture, one calculated file name per document.** A document gets `YYYYMMDD-<short guid>-<title>.md` when work starts, and that name is used in `archive/`, `output/`, `failed/`, `duplicates/` and `epiaku-docs`. Pages are still **overwritten by ID**: the existing page is found by the `id` in its frontmatter, never by the file name.
- **Python first, AI last:** the LLM returns only JSON matching a Pydantic schema. Python owns IDs, paths, frontmatter, tags and Git. **Metrics (views, likes, subscribers, dates) come only from Python (`yt-dlp`)**, never from an LLM.
- **Tags from the fixed list only:** **at most one idea-type tag** (optional, changed on 2026-10-01: a missing tag must never fail a page), **1–4 topic tags**, **0–1 project tag**. Unknown tags are dropped and reported.
- **No fallback between backends.** If an LLM call fails for a temporary reason, the note stays in `output/` with `stage: analyzed` and is retried on the next run. Invalid JSON is retried **once, immediately, on the same backend**, with the validation error added to the prompt.
- **Every LLM call is an API call with an API key** (FreeLLMApi for `notes`, the OpenAI API for `clippings` and `youtube`). There is no CLI, no subscription login and no `claude` binary anywhere. Keys come from `.env`, are never logged and never written to a page, a sidecar or an error file. Provider and model are set per profile in `profiles.yaml`, so changing a provider is a config change.
- Every page's frontmatter has `title`, `description`, `weight` (int), `type: docs`, `id`, `tags`. The only shortcodes allowed in pages are `youtube-lite` and `alert`.
- **One review round** for YouTube summaries. Review is on by default and can be turned off with `--no-review`. The review profile defaults to `youtube`.
- **The default `pytest` run never calls a real LLM and never runs end-to-end tests.** Tests use the `fake` backend, HTTP mocked with `respx`, recorded YouTube data and local bare Git repos. Tests that call a real LLM or a real outside service carry the marker `live`, end-to-end tests carry `e2e`, and both are **run manually** (`uv run pytest -m live`, `uv run pytest -m e2e`). `pyproject.toml` deselects them by default with `addopts = "-m 'not live and not e2e'"`. The manual checks in Tasks 20 and 21 (real OpenAI and FreeLLMApi calls, the real run on copies) are not part of the suite.
- **`--push` is off by default.** Without it, the run commits locally and pushes nothing. With it, epiaku-docs is pushed first, then idea-bucket.
- The pipeline commits **only the files it wrote or removed** (`git commit --only`). Other changes in either working tree are never committed.
- **Stage A scope:** no Postgres, no queue, no API, no Docker. There is no `when` option: every profile is an API and runs immediately. In this stage, "deferred" means the working copy stalls in `output/` with `stage: deferred` until you move the file back into `inbox/`. **No document state is stored** (that is Stage B's Postgres): the folders, the `stage` field and the **Python log** show what happened, so every step logs one line per file.
- **Deviations from the spec, decided while planning against the real data** (to be recorded in the docs in Task 21):
  - AI chats publish to the existing site folder `idea-bucket/clippings/`, not `gemini/` + `claude/`. There is one `ai-chat` class for both.
  - YouTube facts are stored in a sidecar file `output/<sub>/<name>.youtube.json` instead of in the note's frontmatter, so the Obsidian properties panel stays readable. The sidecar stays next to the final page.
  - `reason()` is synchronous; Stage B can call it through `asyncio.to_thread`.
  - `YoutubeSummary` gains a `description` field, because every page needs one.
  - **Snapshots of one conversation cost one LLM call.** Every inbox file is archived and ingested, the same filename overwrites its earlier copies, and the page in `epiaku-docs` is overwritten by ID. Of several clips with one ID, only the longest goes to the LLM; earlier snapshots of it (same messages, last one possibly cut off) move from `output/` to `duplicates/` with `duplicate_of` in the frontmatter, and their `archive/` copy stays. Clips with different content are all processed. (Replaces the earlier "largest body wins, losers go to a `superseded/` folder" rule: nothing moves to a folder, and nothing is deleted.)

## Review Focus

These five inputs are the most likely to break the pipeline on real use, most likely first. Each has a test in the task named in brackets.

1. **The same Gemini conversation clipped several times** in one inbox batch. This already happens: `2446cd9c762c9cc9` is clipped 5 times in the current inbox. Expected: all five files are archived and ingested, but only the longest (`obsidian github link.md`, 68 messages) goes to the LLM. The other four move to `duplicates/clippings/` with `duplicate_of`, and `epiaku-docs` ends with **one** page for that id. A clip whose content differs from the longest is not hidden: it is processed on its own. [Tasks 4 and 19]
2. **An epiaku-docs working tree with unrelated uncommitted changes.** This is the case right now: two deleted `clipping/` pages. Expected: the pipeline commits only its own pages, and the user's changes stay exactly as they were. [Tasks 13 and 14]
3. **Captures without the expected frontmatter.** Phone notes have no frontmatter at all, newer Web Clipper notes write `source :` with a space and no `title`, and YouTube URLs carry `&list=…&t=…` or `\_` escapes. Expected: correct class and stable ID, with a title hint taken from the file name. [Tasks 2, 3 and 4]
4. **LLM text that breaks Markdown or Hugo:** a `|` or a newline inside a Tips table cell, or an unknown `{{< shortcode >}}` in the body. Expected: cells are escaped, and a page with an unknown shortcode is rejected and its note moves to `failed/` with the reason. [Tasks 10, 11, 16 and 19]
5. **The OpenAI quota or rate limit hit halfway through a run** (or a bad API key). Expected: that note and every later `openai` note are deferred (they stay in `output/`, not in `failed/`) without any further calls, and FreeLLMApi notes are still published. A bad key logs one clear `ERROR`. [Tasks 14, 19 and 20]

---

## File Structure

```text
idea-catcher/
├── pyproject.toml              uv project, dependencies, ruff/pyright/pytest config
├── .python-version             3.12
├── .pre-commit-config.yaml     ruff, pyright, fast tests
├── .env.example                every setting, no secrets
├── profiles.yaml               the three LLM profiles (notes, clippings, youtube), review profile
├── src/catcher/
│   ├── __init__.py             __version__
│   ├── cli.py                  Typer app: version | ingest | reason | render | run pipeline | youtube facts
│   ├── core/
│   │   ├── config.py           Settings (env + .env)
│   │   ├── frontmatter.py      tolerant frontmatter parse/dump (Doc)
│   │   ├── logging.py          configure_logging() (Task 19)
│   │   └── git.py              pull / commit_paths / push
│   └── modules/
│       ├── llm/
│       │   ├── schemas.py      NoteSummary, ChatSummary, YoutubeSummary, Review, SCHEMAS
│       │   ├── profiles.py     Profile, ProfilesConfig, load_profiles, resolve_profile
│       │   ├── prompts.py      render_prompt(task, vars) -> (text, version)
│       │   ├── prompts/        note.md, ai-chat.md, youtube.md, youtube-from-gemini.md, review.md
│       │   ├── service.py      reason(), errors, Usage, BackendReply, LlmResult
│       │   └── backends/       __init__.py (make_backend), fake.py, openai_compatible.py (freellmapi + openai)
│       ├── pipeline/
│       │   ├── doctypes.py     DocType registry, detect(), derive_id(), canonical_source()
│       │   ├── ingest.py       ingest_inbox(), load_pending(), facts_sidecar()   (was staging.py before Task 19)
│       │   ├── tags.yaml       the fixed tag list
│       │   ├── tags.py         load_tags(), normalize_tags()
│       │   ├── inputs.py       prompt_input(), capture_tags()
│       │   ├── render.py       slugify, page_filename, render_page (Jinja + YAML)
│       │   ├── templates/      note.md.j2, ai-chat.md.j2, youtube.md.j2
│       │   ├── validate.py     validate_page()
│       │   ├── process.py      Services, ProcessOptions, process_note()
│       │   ├── publish.py      find_pages_by_id, write_page, finalize_output, move_to_failed
│       │   └── run.py          run_pipeline() -> RunReport
│       └── youtube/
│           ├── urls.py         host_of, video_id, find_youtube_url
│           ├── facts.py        YoutubeFacts, fetch_facts (yt-dlp + transcript)
│           └── review.py       review_summary, python_checks, ReviewOutcome
└── tests/
    ├── conftest.py             shared fixtures (profiles, notes, results, services)
    ├── fixtures/youtube/       recorded facts JSON
    ├── unit/                   pure functions
    ├── component/              one module with its outside world faked
    └── integration/git/        real git against local bare repos
```

---

### Task 1: Project scaffold, settings and CLI skeleton (A1)

> **Superseded in part by Task 20.** `CLAUDE_BIN`, `claude_bin`, `CLAUDE_CODE_OAUTH_TOKEN` and the Claude Code CLI install are removed. Settings gain `OPENAI_API_KEY` and `OPENAI_BASE_URL`, and `.env.example` gains the two OpenAI model variables.

**Files:**
- Create: `pyproject.toml`, `.python-version`, `.gitignore`, `.env.example`, `.pre-commit-config.yaml`
- Create: `src/catcher/__init__.py`, `src/catcher/cli.py`, `src/catcher/core/__init__.py`, `src/catcher/core/config.py`, `src/catcher/modules/__init__.py`
- Create: `tests/unit/test_cli.py`, `tests/unit/test_config.py`, `tests/component/.gitkeep`, `tests/integration/git/.gitkeep`

**Interfaces:**
- Produces: `catcher.__version__ = "0.1.0"`; `catcher.cli.app` (Typer); `catcher.core.config.Settings` with fields `ideas_repo: Path`, `docs_repo: Path`, `profiles_file: Path`, `freellmapi_url: str`, `freellmapi_model: str`, `freellmapi_api_key: str`, `llm_timeout_s: int`, `transcript_languages: str`, `git_author_name: str`, `git_author_email: str`, and the property `transcript_language_list -> list[str]`.

- [ ] **Step 1: Install the tools**

Run: `brew install uv && uv --version`
Expected: a version such as `uv 0.8.x`.

Run: `uv run python --version`
Expected: `Python 3.12.x`. (An earlier version of this task also installed the Claude Code CLI. It is no longer needed: all LLM calls use API keys.)

- [ ] **Step 2: Write the project files**

`pyproject.toml`:

```toml
[project]
name = "idea-catcher"
version = "0.1.0"
description = "Turns idea-bucket captures into epiaku-docs pages"
requires-python = ">=3.12"
dependencies = [
  "jinja2>=3.1",
  "openai>=1.40",
  "pydantic>=2.8",
  "pydantic-settings>=2.4",
  "python-dotenv>=1.0",
  "pyyaml>=6.0",
  "typer>=0.12",
  "youtube-transcript-api>=1.2",
  "yt-dlp>=2025.1.1",
]

[project.scripts]
catcher = "catcher.cli:app"

[dependency-groups]
dev = [
  "pre-commit>=3.8",
  "pyright>=1.1.380",
  "pytest>=8.3",
  "respx>=0.21",
  "ruff>=0.6",
  "syrupy>=4.6",
  "types-PyYAML>=6.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/catcher"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra --import-mode=importlib"

[tool.ruff]
line-length = 110
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]

[tool.pyright]
include = ["src"]
pythonVersion = "3.12"
typeCheckingMode = "standard"
```

`.gitignore`:

```text
.venv/
__pycache__/
.pytest_cache/
.ruff_cache/
dist/
.env
.env.local
```

`.env.example`:

```bash
IDEAS_REPO=../idea-bucket
DOCS_REPO=../epiaku-docs
PROFILES_FILE=profiles.yaml
FREELLMAPI_URL=http://<h4-ip>:3001/v1
FREELLMAPI_MODEL=auto
FREELLMAPI_API_KEY=not-needed
CLAUDE_BIN=claude
LLM_TIMEOUT_S=600
TRANSCRIPT_LANGUAGES=en
GIT_AUTHOR_NAME=idea-catcher
GIT_AUTHOR_EMAIL=idea-catcher@users.noreply.github.com
# On the server only (claude setup-token). On the Mac, claude -p uses your own login.
# CLAUDE_CODE_OAUTH_TOKEN=
```

`.pre-commit-config.yaml`:

```yaml
repos:
  - repo: local
    hooks:
      - id: ruff-check
        name: ruff check
        entry: uv run ruff check
        language: system
        types: [python]
      - id: ruff-format
        name: ruff format --check
        entry: uv run ruff format --check
        language: system
        types: [python]
      - id: pyright
        name: pyright
        entry: uv run pyright
        language: system
        pass_filenames: false
        types: [python]
      - id: pytest-fast
        name: pytest unit + component
        entry: uv run pytest tests/unit tests/component -q
        language: system
        pass_filenames: false
        types: [python]
```

Create empty files `src/catcher/core/__init__.py`, `src/catcher/modules/__init__.py`, `tests/component/.gitkeep`, `tests/integration/git/.gitkeep`.

Run: `uv python pin 3.12 && uv sync`
Expected: `.python-version` contains `3.12`, and `.venv/` is created with all dependencies installed.

- [ ] **Step 3: Write the failing tests**

`tests/unit/test_cli.py`:

```python
from typer.testing import CliRunner

from catcher.cli import app

runner = CliRunner()


def test_help_describes_the_tool():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Idea Catcher" in result.output


def test_version_command():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == "0.1.0"
```

`tests/unit/test_config.py`:

```python
from pathlib import Path

from catcher.core.config import Settings


def test_settings_read_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("IDEAS_REPO", str(tmp_path / "ib"))
    monkeypatch.setenv("TRANSCRIPT_LANGUAGES", "en, nl")
    settings = Settings()
    assert settings.ideas_repo == tmp_path / "ib"
    assert settings.transcript_language_list == ["en", "nl"]


def test_settings_defaults():
    settings = Settings()
    assert settings.profiles_file == Path("profiles.yaml")
    assert settings.claude_bin == "claude"
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `uv run pytest tests/unit -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'catcher.cli'`.

- [ ] **Step 5: Write the implementation**

`src/catcher/__init__.py`:

```python
__version__ = "0.1.0"
```

`src/catcher/core/config.py`:

```python
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    ideas_repo: Path = Path("../idea-bucket")
    docs_repo: Path = Path("../epiaku-docs")
    profiles_file: Path = Path("profiles.yaml")
    freellmapi_url: str = "http://localhost:3001/v1"
    freellmapi_model: str = "auto"
    freellmapi_api_key: str = "not-needed"
    claude_bin: str = "claude"
    llm_timeout_s: int = 600
    transcript_languages: str = "en"
    git_author_name: str = "idea-catcher"
    git_author_email: str = "idea-catcher@users.noreply.github.com"

    @property
    def transcript_language_list(self) -> list[str]:
        return [lang.strip() for lang in self.transcript_languages.split(",") if lang.strip()]
```

`src/catcher/cli.py`:

```python
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv

from catcher import __version__

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Idea Catcher: turns idea-bucket captures into epiaku-docs pages.",
)

IdeasOpt = Annotated[Path | None, typer.Option("--ideas", help="idea-bucket checkout (default: IDEAS_REPO)")]
DocsOpt = Annotated[Path | None, typer.Option("--docs", help="epiaku-docs checkout (default: DOCS_REPO)")]
ProfileOpt = Annotated[
    str | None, typer.Option("--profile", "--llm-profile", help="LLM profile from profiles.yaml")
]


@app.callback()
def main() -> None:
    load_dotenv(override=False)


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/unit -q && uv run catcher --help`
Expected: 4 passed, and the help text shows "Idea Catcher".

- [ ] **Step 7: Turn on the hooks and commit**

```bash
uv run pre-commit install
uv run ruff format src tests
git add pyproject.toml uv.lock .python-version .gitignore .env.example .pre-commit-config.yaml src tests
git commit -m "feat: scaffold the idea-catcher CLI with settings and tooling"
```

---

### Task 2: Tolerant frontmatter parsing

**Files:**
- Create: `src/catcher/core/frontmatter.py`
- Test: `tests/unit/test_frontmatter.py`

**Interfaces:**
- Produces: `Doc(fm: dict[str, Any], body: str)` (dataclass, compared by value); `parse(text: str) -> Doc`; `load(path: Path) -> Doc`; `dump(doc: Doc) -> str`; `FrontmatterError(ValueError)`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_frontmatter.py`:

```python
import pytest

from catcher.core.frontmatter import Doc, FrontmatterError, dump, parse

DICTATED = "Create YouTube content walking around and talk about your career\n"

CLIP = """---
source : "https://gemini.google.com/app/cf81e40b020519ef?is_sa=1&utm_source=sem"
author:
published:
created: 2026-09-25
description: "Gemini conversation with 12 messages"
tags:
  - "clippings"
---
**You**

systeme.io sell group of items

---

**Gemini**

Yes, you can.
"""


def test_note_without_frontmatter_is_all_body():
    doc = parse(DICTATED)
    assert doc.fm == {}
    assert doc.body == DICTATED


def test_web_clipper_key_with_space_before_colon():
    doc = parse(CLIP)
    assert doc.fm["source"].startswith("https://gemini.google.com/app/cf81e40b020519ef")
    assert doc.fm["tags"] == ["clippings"]
    assert doc.fm["author"] is None
    assert str(doc.fm["created"]) == "2026-09-25"


def test_body_keeps_chat_turn_separators():
    doc = parse(CLIP)
    assert doc.body.startswith("**You**\n")
    assert "\n---\n\n**Gemini**\n" in doc.body


def test_crlf_and_bom_are_normalized():
    doc = parse("﻿---\r\nid: 20260924103015\r\ntype: note\r\n---\r\nHello\r\n")
    assert doc.fm == {"id": 20260924103015, "type": "note"}
    assert doc.body == "Hello\n"


def test_empty_frontmatter():
    assert parse("---\n---\nbody\n") == Doc({}, "body\n")


@pytest.mark.parametrize(
    "text",
    [
        "---\ntitle: [unclosed\n---\nbody\n",
        "---\ntitle: no closing line\nbody\n",
        "---\n- a\n- b\n---\nbody\n",
    ],
)
def test_unreadable_frontmatter_raises(text):
    with pytest.raises(FrontmatterError):
        parse(text)


def test_dump_round_trips():
    doc = Doc({"id": "abc123", "title": "Idée: café | test", "tags": ["app-idea"]}, "Body\n\n---\n\nMore\n")
    assert parse(dump(doc)) == doc


def test_dump_quotes_ids_that_look_like_numbers():
    doc = Doc({"id": "1234567e890"}, "x\n")
    assert parse(dump(doc)).fm["id"] == "1234567e890"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_frontmatter.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'catcher.core.frontmatter'`.

- [ ] **Step 3: Write the implementation**

`src/catcher/core/frontmatter.py`:

```python
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class FrontmatterError(ValueError):
    """The file starts with '---' but its frontmatter can't be read."""


@dataclass
class Doc:
    fm: dict[str, Any] = field(default_factory=dict)
    body: str = ""


_CLOSING_LINE = re.compile(r"^---[ \t]*$", re.MULTILINE)


def parse(text: str) -> Doc:
    text = text.lstrip("﻿").replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return Doc({}, text)
    rest = text[4:]
    match = _CLOSING_LINE.search(rest)
    if match is None:
        raise FrontmatterError("frontmatter has no closing '---' line")
    try:
        data = yaml.safe_load(rest[: match.start()]) or {}
    except yaml.YAMLError as e:
        raise FrontmatterError(f"invalid YAML frontmatter: {e}") from e
    if not isinstance(data, dict):
        raise FrontmatterError("frontmatter is not a mapping")
    body = rest[match.end() :]
    if body.startswith("\n"):
        body = body[1:]
    return Doc({str(k).strip(): v for k, v in data.items()}, body)


def load(path: Path) -> Doc:
    return parse(path.read_text(encoding="utf-8"))


def dump(doc: Doc) -> str:
    fm = yaml.safe_dump(doc.fm, sort_keys=False, allow_unicode=True, width=10_000)
    return f"---\n{fm}---\n{doc.body}"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_frontmatter.py -q`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add src/catcher/core/frontmatter.py tests/unit/test_frontmatter.py
git commit -m "feat: parse capture frontmatter tolerantly and dump it back"
```

---

### Task 3: YouTube URL helpers, document types, detection and IDs

**Files:**
- Create: `src/catcher/modules/youtube/__init__.py` (empty), `src/catcher/modules/youtube/urls.py`
- Create: `src/catcher/modules/pipeline/__init__.py` (empty), `src/catcher/modules/pipeline/doctypes.py`
- Test: `tests/unit/test_youtube_urls.py`, `tests/unit/test_doctypes.py`

**Interfaces:**
- Produces (urls): `YOUTUBE_HOSTS: frozenset[str]`; `host_of(url: str) -> str`; `video_id(url: str) -> str | None`; `find_youtube_url(text: str) -> str | None`.
- Produces (doctypes): the frozen dataclass `DocType(name, task, schema_name, template, out_dir, archive_dir, llm_profile, reviewed=False)`; the constants `NOTE`, `AI_CHAT`, `YOUTUBE`, `YOUTUBE_GEMINI`; `DOC_TYPES: dict[str, DocType]`; `DOCS_ROOT = "hugo/content/en/docs/idea-bucket"`; `first_user_turn(body: str) -> str`; `gemini_video_id(body: str) -> str | None`; `detect(fm: dict, body: str) -> DocType`; `safe_id(raw: str | None) -> str | None`; `derive_id(doctype: DocType, fm: dict, body: str) -> str | None`; `canonical_source(doctype: DocType, fm: dict) -> str | None`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_youtube_urls.py`:

```python
import pytest

from catcher.modules.youtube.urls import find_youtube_url, host_of, video_id


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://www.youtube.com/watch?v=tcqEUSNCn8I&list=PL4RKwZPB4EJDizVLBstGU-ocFQxb6RtRO&index=10&t=1s",
            "tcqEUSNCn8I",
        ),
        ("https://youtu.be/AeV5F0ppaGw?si=abc", "AeV5F0ppaGw"),
        ("https://m.youtube.com/watch?v=MBPHU7aaklM", "MBPHU7aaklM"),
        ("https://www.youtube.com/shorts/AeV5F0ppaGw", "AeV5F0ppaGw"),
        ("https://www.youtube.com/embed/AeV5F0ppaGw", "AeV5F0ppaGw"),
        ("https://www.youtube.com/watch?v=Ab\\_cdEfGhIj", "Ab_cdEfGhIj"),
        ("https://www.youtube.com/@pixegami", None),
        ("https://www.youtube.com/watch?v=short", None),
        ("https://gemini.google.com/app/2446cd9c762c9cc9", None),
        ("", None),
    ],
)
def test_video_id(url, expected):
    assert video_id(url) == expected


def test_host_of_strips_www_and_mobile():
    assert host_of("https://www.youtube.com/watch?v=x") == "youtube.com"
    assert host_of("https://m.youtube.com/watch?v=x") == "youtube.com"
    assert host_of("https://gemini.google.com/app/1?x=2") == "gemini.google.com"


def test_find_youtube_url_in_chat_text():
    text = "Summarize this YouTube video: https://www.youtube.com/watch?v=MBPHU7aaklM\n\nFormat"
    assert find_youtube_url(text) == "https://www.youtube.com/watch?v=MBPHU7aaklM"
    assert find_youtube_url("no video here https://example.com") is None
```

`tests/unit/test_doctypes.py`:

```python
from catcher.modules.pipeline.doctypes import (
    AI_CHAT,
    NOTE,
    YOUTUBE,
    YOUTUBE_GEMINI,
    canonical_source,
    derive_id,
    detect,
    first_user_turn,
)

GEMINI = "https://gemini.google.com/app/cf81e40b020519ef?is_sa=1&utm_source=sem&gclid=Cj0"
CHAT_BODY = "**You**\n\nsysteme.io sell group of items\n\n---\n\n**Gemini**\n\nYes.\n"
YT_CHAT_BODY = (
    "**You**\n\nSummarize this YouTube video: https://www.youtube.com/watch?v=MBPHU7aaklM\n\n"
    "Format Requirements\n\n---\n\n**Gemini**\n\n**Success Is Hard** by *Someone*\n"
)


def test_dictated_note_without_frontmatter():
    assert detect({}, "an idea") is NOTE
    assert derive_id(NOTE, {}, "an idea") is None


def test_template_note_keeps_its_numeric_id():
    fm = {"id": 20260924103015, "type": "note"}
    assert detect(fm, "") is NOTE
    assert derive_id(NOTE, fm, "") == "20260924103015"


def test_gemini_chat_id_comes_from_the_url_without_tracking():
    fm = {"source": GEMINI}
    assert detect(fm, CHAT_BODY) is AI_CHAT
    assert derive_id(AI_CHAT, fm, CHAT_BODY) == "cf81e40b020519ef"
    assert canonical_source(AI_CHAT, fm) == "https://gemini.google.com/app/cf81e40b020519ef"


def test_claude_chat():
    fm = {"source": "https://claude.ai/chat/0b1c2d3e-aaaa-bbbb-cccc-1234567890ab"}
    assert detect(fm, "chat") is AI_CHAT
    assert derive_id(AI_CHAT, fm, "chat") == "0b1c2d3e-aaaa-bbbb-cccc-1234567890ab"


def test_gemini_chat_that_summarized_a_video():
    fm = {"source": GEMINI}
    assert detect(fm, YT_CHAT_BODY) is YOUTUBE_GEMINI
    assert derive_id(YOUTUBE_GEMINI, fm, YT_CHAT_BODY) == "MBPHU7aaklM-gemini"


def test_explicit_type_overrides_detection():
    assert detect({"source": GEMINI, "type": "ai-chat"}, YT_CHAT_BODY) is AI_CHAT


def test_class_from_a_previous_staging_is_kept_on_replay():
    fm = {"source": GEMINI, "class": "youtube-gemini", "id": "MBPHU7aaklM-gemini"}
    assert detect(fm, "body without a link") is YOUTUBE_GEMINI
    assert derive_id(YOUTUBE_GEMINI, fm, "body without a link") == "MBPHU7aaklM-gemini"


def test_youtube_clip_with_playlist_parameters():
    fm = {"source": "https://www.youtube.com/watch?v=tcqEUSNCn8I&list=PL4RK&index=10&t=1s"}
    assert detect(fm, "page scrape") is YOUTUBE
    assert derive_id(YOUTUBE, fm, "page scrape") == "tcqEUSNCn8I"
    assert canonical_source(YOUTUBE, fm) == "https://www.youtube.com/watch?v=tcqEUSNCn8I"


def test_youtube_link_without_a_video_is_a_note():
    assert detect({"source": "https://www.youtube.com/@pixegami"}, "x") is NOTE


def test_empty_or_unknown_source_is_a_note():
    assert detect({"source": ""}, "x") is NOTE
    assert detect({"source": "https://example.com/post"}, "x") is NOTE
    assert canonical_source(NOTE, {"source": "https://example.com/post"}) == "https://example.com/post"
    assert canonical_source(NOTE, {}) is None


def test_unsafe_explicit_id_is_cleaned():
    assert derive_id(NOTE, {"id": "a/b c"}, "") == "a-b-c"
    assert derive_id(NOTE, {"id": "///"}, "") is None


def test_first_user_turn_stops_at_the_first_answer():
    assert first_user_turn(YT_CHAT_BODY).strip().startswith("Summarize this YouTube video")
    assert "Success Is Hard" not in first_user_turn(YT_CHAT_BODY)
    assert first_user_turn("no markers at all") == "no markers at all"


def test_output_and_archive_folders():
    assert AI_CHAT.out_dir == "hugo/content/en/docs/idea-bucket/clippings"
    assert AI_CHAT.archive_dir == "archive/clippings"
    assert YOUTUBE_GEMINI.out_dir == YOUTUBE.out_dir == "hugo/content/en/docs/idea-bucket/youtube"
    assert YOUTUBE.archive_dir == "archive/youtube"
    assert NOTE.out_dir == "hugo/content/en/docs/idea-bucket/notes"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_youtube_urls.py tests/unit/test_doctypes.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

`src/catcher/modules/youtube/urls.py`:

```python
import re
from urllib.parse import parse_qs, urlparse

YOUTUBE_HOSTS = frozenset({"youtube.com", "youtu.be", "music.youtube.com", "youtube-nocookie.com"})
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_URL = re.compile(r"https?://(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)/[^\s)\]>\"'<*]+")


def host_of(url: str) -> str:
    netloc = urlparse(url.strip()).netloc.lower().split("@")[-1].split(":")[0]
    for prefix in ("www.", "m."):
        netloc = netloc.removeprefix(prefix)
    return netloc


def video_id(url: str) -> str | None:
    url = url.replace("\\", "").strip()
    parsed = urlparse(url)
    host = host_of(url)
    parts = [p for p in parsed.path.split("/") if p]
    candidate: str | None = None
    if host == "youtu.be":
        candidate = parts[0] if parts else None
    elif host in YOUTUBE_HOSTS:
        if parts[:1] == ["watch"]:
            candidate = (parse_qs(parsed.query).get("v") or [None])[0]
        elif len(parts) >= 2 and parts[0] in {"shorts", "embed", "live", "v"}:
            candidate = parts[1]
    return candidate if candidate and _VIDEO_ID.match(candidate) else None


def find_youtube_url(text: str) -> str | None:
    for match in _YOUTUBE_URL.finditer(text.replace("\\", "")):
        if video_id(match.group(0)):
            return match.group(0)
    return None
```

`src/catcher/modules/pipeline/doctypes.py`:

```python
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from catcher.modules.youtube.urls import YOUTUBE_HOSTS, find_youtube_url, host_of, video_id

DOCS_ROOT = "hugo/content/en/docs/idea-bucket"
CHAT_HOSTS = frozenset({"gemini.google.com", "claude.ai"})


@dataclass(frozen=True)
class DocType:
    name: str
    task: str
    schema_name: str
    template: str
    out_dir: str
    archive_dir: str
    llm_profile: str
    reviewed: bool = False


NOTE = DocType(
    "note", "note", "NoteSummary", "note.md.j2", f"{DOCS_ROOT}/notes", "archive/notes", "free-fast"
)
AI_CHAT = DocType(
    "ai-chat",
    "ai-chat",
    "ChatSummary",
    "ai-chat.md.j2",
    f"{DOCS_ROOT}/clippings",
    "archive/clippings",
    "claude-sub-evening",
)
YOUTUBE = DocType(
    "youtube",
    "youtube",
    "YoutubeSummary",
    "youtube.md.j2",
    f"{DOCS_ROOT}/youtube",
    "archive/youtube",
    "claude-sub-evening",
    reviewed=True,
)
YOUTUBE_GEMINI = DocType(
    "youtube-gemini",
    "youtube-from-gemini",
    "YoutubeSummary",
    "youtube.md.j2",
    f"{DOCS_ROOT}/youtube",
    "archive/youtube",
    "claude-sub-evening",
    reviewed=True,
)
DOC_TYPES: dict[str, DocType] = {t.name: t for t in (NOTE, AI_CHAT, YOUTUBE, YOUTUBE_GEMINI)}

_UNSAFE_ID = re.compile(r"[^A-Za-z0-9_-]+")
_TURN_END = {"**Gemini**", "**Claude**", "---"}


def first_user_turn(body: str) -> str:
    lines = body.split("\n")
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "**You**")
    except StopIteration:
        return body[:2000]
    turn: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip() in _TURN_END:
            break
        turn.append(line)
    return "\n".join(turn)


def gemini_video_id(body: str) -> str | None:
    url = find_youtube_url(first_user_turn(body))
    return video_id(url) if url else None


def detect(fm: dict[str, Any], body: str) -> DocType:
    for key in ("type", "class"):
        value = str(fm.get(key) or "").strip()
        if value in DOC_TYPES:
            return DOC_TYPES[value]
    source = str(fm.get("source") or "")
    host = host_of(source) if source else ""
    if host in YOUTUBE_HOSTS and video_id(source):
        return YOUTUBE
    if host in CHAT_HOSTS:
        if host == "gemini.google.com" and gemini_video_id(body):
            return YOUTUBE_GEMINI
        return AI_CHAT
    return NOTE


def safe_id(raw: str | None) -> str | None:
    if raw is None:
        return None
    cleaned = _UNSAFE_ID.sub("-", raw).strip("-")
    return cleaned or None


def _last_path_segment(url: str) -> str | None:
    parts = [p for p in urlparse(url.strip()).path.split("/") if p]
    return parts[-1] if parts else None


def derive_id(doctype: DocType, fm: dict[str, Any], body: str) -> str | None:
    if fm.get("id") not in (None, ""):
        return safe_id(str(fm["id"]).strip())
    source = str(fm.get("source") or "")
    if doctype is YOUTUBE:
        return safe_id(video_id(source))
    if doctype is YOUTUBE_GEMINI:
        vid = gemini_video_id(body)
        return f"{vid}-gemini" if vid else None
    if doctype is AI_CHAT:
        return safe_id(_last_path_segment(source))
    return None


def canonical_source(doctype: DocType, fm: dict[str, Any]) -> str | None:
    source = str(fm.get("source") or "").strip()
    if not source:
        return None
    if doctype is YOUTUBE:
        vid = video_id(source)
        return f"https://www.youtube.com/watch?v={vid}" if vid else None
    if doctype in (AI_CHAT, YOUTUBE_GEMINI):
        parsed = urlparse(source)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}" if parsed.netloc else None
    return source
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/catcher/modules tests/unit/test_youtube_urls.py tests/unit/test_doctypes.py
git commit -m "feat: detect capture classes and derive stable ids"
```

---

### Task 4: Staging, including repeated clips, replay and `catcher stage` (A2)

> **Superseded in part by Task 19.** This task built `stage_inbox()` with a flat `staging/` folder, deduplication by id and `archive/<class>/superseded/`. Task 19 replaces all three: `ingest_inbox()` archives every inbox file untouched, writes `output/<sub>/<name>.md` under the **same name**, and does not deduplicate. `catcher stage` becomes `catcher ingest`. Read this task for the frontmatter, id and body-preservation tests, which still hold.

**Files:**
- Create: `src/catcher/modules/pipeline/staging.py`
- Modify: `src/catcher/cli.py` (add the `stage` command)
- Test: `tests/component/test_staging.py`

**Interfaces:**
- Consumes: `load`, `dump`, `Doc`, `FrontmatterError` (Task 2); `detect`, `derive_id`, `DOC_TYPES`, `DocType` (Task 3).
- Produces: the dataclass `StagedNote(doc_id: str, doctype: DocType, doc: Doc, path: Path)`; the dataclass `StagingResult(staged: list[StagedNote], touched: list[Path], errors: dict[str, str])`; `stage_inbox(ideas_repo: Path, *, dry_run: bool = False, now: datetime | None = None) -> StagingResult`; `load_staged_note(path: Path) -> StagedNote`; `load_staged(ideas_repo: Path) -> list[StagedNote]`; `facts_sidecar(staged_path: Path) -> Path` (returns `<id>.youtube.json` next to the note); `new_id() -> str` (6 hex characters).

- [ ] **Step 1: Write the failing tests**

`tests/component/test_staging.py`:

```python
import re
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from catcher.cli import app
from catcher.core.frontmatter import load
from catcher.modules.pipeline.staging import facts_sidecar, load_staged, stage_inbox

NOW = datetime(2026, 9, 27, 18, 0, 0, tzinfo=UTC)


def put(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def gemini_clip(chat_id: str, body: str, created: str = "2026-09-03") -> str:
    return (
        f'---\nsource : "https://gemini.google.com/app/{chat_id}?is_sa=1&utm_source=sem"\n'
        f'author:\ncreated: {created}\ntags:\n  - "clippings"\n---\n{body}'
    )


def files(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): p.read_text() for p in root.rglob("*") if p.is_file()}


def test_dictated_note_gets_an_id_and_leaves_the_inbox(tmp_path):
    src = put(tmp_path, "inbox/notes/YouTube walks.md", "Create YouTube content walking around\n")
    result = stage_inbox(tmp_path, now=NOW)
    [note] = result.staged
    assert note.doctype.name == "note"
    assert re.fullmatch(r"[0-9a-f]{6}", note.doc_id)
    staged_path = tmp_path / "staging" / f"{note.doc_id}.md"
    assert not src.exists()
    staged = load(staged_path)
    assert staged.body == "Create YouTube content walking around\n"
    assert staged.fm == {
        "id": note.doc_id,
        "class": "note",
        "captured": "2026-09-27",
        "source_file": "inbox/notes/YouTube walks.md",
        "staged_at": "2026-09-27T18:00:00+00:00",
    }
    assert set(result.touched) == {src, staged_path}
    assert result.errors == {}


def test_clip_body_is_kept_byte_for_byte(tmp_path):
    body = "**You**\n\nsysteme.io \\[paid\\] question\n\n---\n\n**Gemini**\n\nYes.\n"
    put(tmp_path, "inbox/clippings/systeme.io.md", gemini_clip("cf81e40b020519ef", body, "2026-09-25"))
    [note] = stage_inbox(tmp_path, now=NOW).staged
    staged = load(tmp_path / "staging" / "cf81e40b020519ef.md")
    assert note.doctype.name == "ai-chat"
    assert staged.body == body
    assert staged.fm["captured"] == "2026-09-25"
    assert staged.fm["tags"] == ["clippings"]


def test_repeated_clips_of_one_chat_are_staged_once(tmp_path):
    put(tmp_path, "inbox/clippings/New chat.md", gemini_clip("2446cd9c762c9cc9", "short\n"))
    put(
        tmp_path,
        "inbox/clippings/Idea catcher.md",
        gemini_clip("2446cd9c762c9cc9", "the longest version\n" * 5),
    )
    put(tmp_path, "inbox/clippings/obsidian github link.md", gemini_clip("2446cd9c762c9cc9", "medium\n" * 2))
    result = stage_inbox(tmp_path, now=NOW)
    assert [n.doc_id for n in result.staged] == ["2446cd9c762c9cc9"]
    assert load(tmp_path / "staging" / "2446cd9c762c9cc9.md").body == "the longest version\n" * 5
    superseded = sorted(p.name for p in (tmp_path / "archive/clippings/superseded").iterdir())
    assert superseded == [
        "2446cd9c762c9cc9--20260927180000-1.md",
        "2446cd9c762c9cc9--20260927180000-2.md",
    ]
    assert not list((tmp_path / "inbox").rglob("*.md"))


def test_new_inbox_copy_replaces_a_waiting_staged_copy(tmp_path):
    put(tmp_path, "staging/cf81e40b020519ef.md", gemini_clip("cf81e40b020519ef", "old and long\n" * 9))
    put(tmp_path, "inbox/clippings/again.md", gemini_clip("cf81e40b020519ef", "newer\n"))
    stage_inbox(tmp_path, now=NOW)
    assert load(tmp_path / "staging" / "cf81e40b020519ef.md").body == "newer\n"
    [old] = (tmp_path / "archive/clippings/superseded").iterdir()
    assert load(old).body == "old and long\n" * 9


def test_replay_from_the_archive_keeps_the_id(tmp_path):
    archived = "---\nid: a7b2c9\nclass: note\ncaptured: '2026-09-20'\n---\nAn old idea\n"
    put(tmp_path, "inbox/notes/a7b2c9.md", archived)
    [note] = stage_inbox(tmp_path, now=NOW).staged
    assert note.doc_id == "a7b2c9"
    assert note.doc.fm["captured"] == "2026-09-20"


def test_unreadable_capture_stays_in_the_inbox(tmp_path):
    bad = put(tmp_path, "inbox/notes/bad.md", "---\ntitle: [oops\n---\nbody\n")
    put(tmp_path, "inbox/notes/good.md", "A good idea\n")
    result = stage_inbox(tmp_path, now=NOW)
    assert bad.exists()
    assert list(result.errors) == ["inbox/notes/bad.md"]
    assert len(result.staged) == 1


def test_dry_run_changes_nothing(tmp_path):
    put(tmp_path, "inbox/notes/idea.md", "An idea\n")
    before = files(tmp_path)
    result = stage_inbox(tmp_path, dry_run=True, now=NOW)
    assert files(tmp_path) == before
    assert len(result.staged) == 1
    assert result.touched == []


def test_hidden_folders_are_ignored(tmp_path):
    put(tmp_path, "inbox/.trash/deleted.md", "gone\n")
    assert stage_inbox(tmp_path, now=NOW).staged == []


def test_load_staged_reads_back_what_was_staged(tmp_path):
    put(tmp_path, "inbox/notes/idea.md", "An idea\n")
    put(tmp_path, "inbox/clippings/chat.md", gemini_clip("cf81e40b020519ef", "q\n"))
    staged = stage_inbox(tmp_path, now=NOW).staged
    loaded = load_staged(tmp_path)
    assert sorted((n.doc_id, n.doctype.name) for n in loaded) == sorted(
        (n.doc_id, n.doctype.name) for n in staged
    )


def test_facts_sidecar_sits_next_to_the_note(tmp_path):
    assert facts_sidecar(tmp_path / "staging" / "abc.md") == tmp_path / "staging" / "abc.youtube.json"


def test_stage_command(tmp_path):
    put(tmp_path, "inbox/notes/idea.md", "An idea\n")
    result = CliRunner().invoke(app, ["stage", "--ideas", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "staged" in result.output
    assert len(list((tmp_path / "staging").glob("*.md"))) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/component/test_staging.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'catcher.modules.pipeline.staging'`.

- [ ] **Step 3: Write the implementation**

`src/catcher/modules/pipeline/staging.py`:

```python
import secrets
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from catcher.core.frontmatter import Doc, FrontmatterError, dump, load
from catcher.modules.pipeline.doctypes import DOC_TYPES, DocType, derive_id, detect


@dataclass
class StagedNote:
    doc_id: str
    doctype: DocType
    doc: Doc
    path: Path


@dataclass
class StagingResult:
    staged: list[StagedNote] = field(default_factory=list)
    touched: list[Path] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


@dataclass
class _Candidate:
    path: Path
    doc: Doc
    doctype: DocType


def new_id() -> str:
    return secrets.token_hex(3)


def facts_sidecar(staged_path: Path) -> Path:
    return staged_path.with_suffix(".youtube.json")


def captured_date(fm: dict[str, Any], now: datetime) -> str:
    raw = str(fm.get("captured") or fm.get("created") or "")[:10]
    try:
        return datetime.strptime(raw, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return now.strftime("%Y-%m-%d")


def stage_inbox(ideas_repo: Path, *, dry_run: bool = False, now: datetime | None = None) -> StagingResult:
    now = now or datetime.now().astimezone()
    inbox = ideas_repo / "inbox"
    staging = ideas_repo / "staging"
    result = StagingResult()

    groups: dict[str, list[_Candidate]] = {}
    for path in sorted(inbox.rglob("*.md")) if inbox.is_dir() else []:
        if any(part.startswith(".") for part in path.relative_to(inbox).parts):
            continue
        rel = path.relative_to(ideas_repo).as_posix()
        try:
            doc = load(path)
        except (FrontmatterError, UnicodeDecodeError) as e:
            result.errors[rel] = str(e)
            continue
        doctype = detect(doc.fm, doc.body)
        doc_id = derive_id(doctype, doc.fm, doc.body) or new_id()
        groups.setdefault(doc_id, []).append(_Candidate(path, doc, doctype))

    stamp = now.strftime("%Y%m%d%H%M%S")
    for doc_id, group in groups.items():
        winner = max(group, key=lambda c: len(c.doc.body))
        fm = {
            **winner.doc.fm,
            "id": doc_id,
            "class": winner.doctype.name,
            "captured": captured_date(winner.doc.fm, now),
            "source_file": winner.path.relative_to(ideas_repo).as_posix(),
            "staged_at": now.isoformat(timespec="seconds"),
        }
        staged_path = staging / f"{doc_id}.md"
        note = StagedNote(doc_id, winner.doctype, Doc(fm, winner.doc.body), staged_path)
        result.staged.append(note)
        if dry_run:
            continue

        replaced = [c.path for c in group if c is not winner]
        if staged_path.exists():
            replaced.append(staged_path)
        superseded_dir = ideas_repo / winner.doctype.archive_dir / "superseded"
        for k, old in enumerate(replaced, start=1):
            dest = superseded_dir / f"{doc_id}--{stamp}-{k}.md"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(old, dest)
            result.touched += [old, dest]

        staging.mkdir(parents=True, exist_ok=True)
        staged_path.write_text(dump(note.doc), encoding="utf-8")
        winner.path.unlink()
        result.touched += [winner.path, staged_path]
    return result


def load_staged_note(path: Path) -> StagedNote:
    doc = load(path)
    return StagedNote(str(doc.fm["id"]), DOC_TYPES[str(doc.fm["class"])], doc, path)


def load_staged(ideas_repo: Path) -> list[StagedNote]:
    staging = ideas_repo / "staging"
    if not staging.is_dir():
        return []
    return [load_staged_note(path) for path in sorted(staging.glob("*.md"))]
```

In `src/catcher/cli.py`, add these imports under the existing ones:

```python
from catcher.core.config import Settings
from catcher.modules.pipeline.staging import stage_inbox
```

and add this command at the end of the file:

```python
@app.command()
def stage(
    ideas: IdeasOpt = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="show what would be staged")] = False,
) -> None:
    """Move inbox captures to staging/ with a stable id and class."""
    settings = Settings()
    result = stage_inbox(ideas or settings.ideas_repo, dry_run=dry_run)
    verb = "would stage" if dry_run else "staged"
    for note in result.staged:
        typer.echo(f"{verb:<12} {note.doctype.name:<15} {note.doc_id:<40} <- {note.doc.fm['source_file']}")
    for rel, error in result.errors.items():
        typer.echo(f"{'error':<12} {rel}: {error}", err=True)
    raise typer.Exit(1 if result.errors else 0)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/component/test_staging.py -q`
Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add src/catcher/modules/pipeline/staging.py src/catcher/cli.py tests/component/test_staging.py
git commit -m "feat: stage inbox captures with stable ids, keeping superseded copies"
```

---

### Task 5: The fixed tag list

**Files:**
- Create: `src/catcher/modules/pipeline/tags.yaml`, `src/catcher/modules/pipeline/tags.py`
- Test: `tests/unit/test_tags.py`

**Interfaces:**
- Produces: the frozen dataclass `TagList(idea_types: tuple[str, ...], topics: tuple[str, ...], projects: tuple[str, ...])`, with `.allowed -> frozenset[str]` and `.as_prompt_dict() -> dict[str, list[str]]`; `load_tags(path: Path = TAGS_FILE) -> TagList`; the dataclass `TagResult(tags: list[str], dropped: list[str])`; `normalize_tags(raw: Iterable[str], tags: TagList) -> TagResult` (output order: idea type, topics, project).

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_tags.py`:

```python
from catcher.modules.pipeline.tags import load_tags, normalize_tags

TAGS = load_tags()


def test_starter_list_is_loaded():
    assert "app-idea" in TAGS.idea_types
    assert "obsidian" in TAGS.topics
    assert "idea-catcher" in TAGS.projects
    assert TAGS.as_prompt_dict()["idea_types"][0] == "app-idea"


def test_unknown_tags_are_dropped():
    result = normalize_tags(["app-idea", "apps", "nonsense"], TAGS)
    assert result.tags == ["app-idea"]
    assert result.dropped == ["apps", "nonsense"]


def test_tags_are_cleaned_before_matching():
    result = normalize_tags(["App-Idea", "#obsidian", "Home Lab", "second_brain"], TAGS)
    assert result.tags == ["app-idea", "obsidian", "home-lab", "second-brain"]


def test_only_one_idea_type_and_one_project():
    result = normalize_tags(["todo", "app-idea", "idea-catcher", "yummystream"], TAGS)
    assert result.tags == ["todo", "idea-catcher"]
    assert result.dropped == ["app-idea", "yummystream"]


def test_at_most_four_topics_and_idea_type_first():
    raw = ["obsidian", "hugo", "automation", "home-lab", "crm", "tech-note"]
    result = normalize_tags(raw, TAGS)
    assert result.tags == ["tech-note", "obsidian", "hugo", "automation", "home-lab"]
    assert result.dropped == ["crm"]


def test_duplicates_are_ignored():
    assert normalize_tags(["obsidian", "Obsidian", "app-idea"], TAGS).tags == ["app-idea", "obsidian"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_tags.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

`src/catcher/modules/pipeline/tags.yaml`:

```yaml
# The one source of truth for page tags. Adding a tag = adding one line here.
idea_types:
  - app-idea
  - saas-idea
  - youtube-idea
  - digital-product-idea
  - ai-influencer-idea
  - todo
  - tech-note
  - strategy
topics:
  # AI & coding
  - ai-agents
  - llm-models
  - freellmapi
  - claude-code
  - codex
  - harnesses
  - skills
  - token-usage
  - vibe-coding
  - ai-coding-workflow
  # Knowledge & tooling
  - second-brain
  - obsidian
  - hugo
  - automation
  - home-lab
  # YouTube
  - tech-channel
  - senior-wisdom
  - content-creation
  - script-writing
  - video-production
  - youtube-growth
  # Business
  - marketing
  - monetization
  - online-courses
  - crm
  - instagram
  - freelancing
  - bookkeeping
  - administration
projects:
  - idea-catcher
  - yummystream
```

`src/catcher/modules/pipeline/tags.py`:

```python
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml

TAGS_FILE = Path(__file__).parent / "tags.yaml"
MAX_TOPICS = 4


@dataclass(frozen=True)
class TagList:
    idea_types: tuple[str, ...]
    topics: tuple[str, ...]
    projects: tuple[str, ...]

    @property
    def allowed(self) -> frozenset[str]:
        return frozenset(self.idea_types + self.topics + self.projects)

    def as_prompt_dict(self) -> dict[str, list[str]]:
        return {
            "idea_types": list(self.idea_types),
            "topics": list(self.topics),
            "projects": list(self.projects),
        }


@dataclass
class TagResult:
    tags: list[str]
    dropped: list[str]


def load_tags(path: Path = TAGS_FILE) -> TagList:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return TagList(
        tuple(str(t) for t in data.get("idea_types") or []),
        tuple(str(t) for t in data.get("topics") or []),
        tuple(str(t) for t in data.get("projects") or []),
    )


def clean_tag(tag: object) -> str:
    return re.sub(r"[\s_]+", "-", str(tag).strip().lower().lstrip("#"))


def normalize_tags(raw: Iterable[str], tags: TagList) -> TagResult:
    idea: list[str] = []
    topics: list[str] = []
    projects: list[str] = []
    dropped: list[str] = []
    seen: set[str] = set()
    for tag in map(clean_tag, raw):
        if not tag or tag in seen:
            continue
        seen.add(tag)
        if tag in tags.idea_types and not idea:
            idea.append(tag)
        elif tag in tags.topics and len(topics) < MAX_TOPICS:
            topics.append(tag)
        elif tag in tags.projects and not projects:
            projects.append(tag)
        else:
            dropped.append(tag)
    return TagResult(idea + topics + projects, dropped)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_tags.py -q`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/catcher/modules/pipeline/tags.yaml src/catcher/modules/pipeline/tags.py tests/unit/test_tags.py
git commit -m "feat: enforce the fixed tag list"
```

---

### Task 6: LLM output schemas and profiles

> **Superseded in part by Task 20.** `Profile.when`, `evening_window`, the `claude-code` backend name and the profile names `free-fast`, `claude-sub-evening` and `claude-sub-now` are replaced by `Profile(backend, model)` and the three profiles `notes`, `clippings`, `youtube`. The schemas are unchanged.

**Files:**
- Create: `src/catcher/modules/llm/__init__.py` (empty), `src/catcher/modules/llm/schemas.py`, `src/catcher/modules/llm/profiles.py`, `profiles.yaml`
- Test: `tests/unit/test_schemas.py`, `tests/unit/test_profiles.py`

**Interfaces:**
- Produces (schemas): `NoteSummary(title, description, body, tags)`; `ChatSummary(title, description, summary: list[str], decisions, options, open_questions, body, tags)`; `Tip(tip, explanation, how_to_apply)`; `YoutubeSummary(title, creator, description, summary, main_purpose, key_examples, action_plan, tools, tips: list[Tip], channel_application, tags)`; `ReviewIssue(kind, severity, excerpt, evidence: str | None, fix)`; `Review(verdict, issues, revised: YoutubeSummary)`; the type alias `Summary = NoteSummary | ChatSummary | YoutubeSummary`; `SCHEMAS: dict[str, type[BaseModel]]`, keyed by class name.
- Produces (profiles): `Profile(backend: Literal["freellmapi","claude-code","fake"], model: str | None, when: Literal["now","evening"])`; `ProfilesConfig(default: str, review_profile: str, profiles: dict[str, Profile])`; `UnknownProfile(ValueError)`; `expand_env(text, env) -> str`; `load_profiles(path: Path, env: Mapping[str, str] | None = None) -> ProfilesConfig`; `resolve_profile(cfg, *, requested: str | None, class_default: str | None) -> tuple[str, Profile]`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_schemas.py`:

```python
import pytest
from pydantic import ValidationError

from catcher.modules.llm.schemas import SCHEMAS, Review, YoutubeSummary


def test_registry_has_every_output_schema():
    assert set(SCHEMAS) == {"NoteSummary", "ChatSummary", "YoutubeSummary", "Review"}


def test_youtube_summary_requires_every_section():
    with pytest.raises(ValidationError):
        YoutubeSummary.model_validate({"title": "x"})


def test_review_rejects_unknown_issue_kinds():
    with pytest.raises(ValidationError):
        Review.model_validate(
            {
                "verdict": "ok",
                "issues": [{"kind": "typo", "severity": "low", "excerpt": "a", "fix": "b"}],
                "revised": {},
            }
        )
```

`tests/unit/test_profiles.py`:

```python
from pathlib import Path

import pytest

from catcher.modules.llm.profiles import (
    Profile,
    ProfilesConfig,
    UnknownProfile,
    expand_env,
    load_profiles,
    resolve_profile,
)

REPO = Path(__file__).parents[2]


def test_expand_env_uses_value_or_default():
    assert expand_env("m: ${FREELLMAPI_MODEL:-auto}", {}) == "m: auto"
    assert expand_env("m: ${FREELLMAPI_MODEL:-auto}", {"FREELLMAPI_MODEL": "llama"}) == "m: llama"
    assert expand_env("m: ${FREELLMAPI_MODEL:-auto}", {"FREELLMAPI_MODEL": ""}) == "m: auto"


def test_repo_profiles_file_loads():
    cfg = load_profiles(REPO / "profiles.yaml", env={})
    assert cfg.default == "free-fast"
    assert cfg.review_profile == "claude-sub-evening"
    assert cfg.profiles["free-fast"] == Profile(backend="freellmapi", model="auto", when="now")
    assert cfg.profiles["claude-sub-evening"] == Profile(
        backend="claude-code", model="sonnet", when="evening"
    )


def test_resolution_order_is_request_then_class_then_global():
    cfg = ProfilesConfig(
        default="a",
        profiles={"a": Profile(backend="fake"), "b": Profile(backend="fake"), "c": Profile(backend="fake")},
    )
    assert resolve_profile(cfg, requested="c", class_default="b")[0] == "c"
    assert resolve_profile(cfg, requested=None, class_default="b")[0] == "b"
    assert resolve_profile(cfg, requested=None, class_default=None)[0] == "a"


def test_unknown_profile_names_the_known_ones():
    cfg = ProfilesConfig(default="a", profiles={"a": Profile(backend="fake")})
    with pytest.raises(UnknownProfile, match="known: a"):
        resolve_profile(cfg, requested="nope", class_default=None)


def test_config_must_define_its_default(tmp_path):
    path = tmp_path / "p.yaml"
    path.write_text("default: missing\nprofiles:\n  a: {backend: fake}\n")
    with pytest.raises(UnknownProfile):
        load_profiles(path, env={})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_schemas.py tests/unit/test_profiles.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

`src/catcher/modules/llm/schemas.py`:

```python
from typing import Literal

from pydantic import BaseModel, Field


class NoteSummary(BaseModel):
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    body: str = Field(min_length=1)
    tags: list[str]


class ChatSummary(BaseModel):
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    summary: list[str] = Field(min_length=1)
    decisions: list[str]
    options: list[str]
    open_questions: list[str]
    body: str
    tags: list[str]


class Tip(BaseModel):
    tip: str
    explanation: str
    how_to_apply: str


class YoutubeSummary(BaseModel):
    title: str = Field(min_length=1)
    creator: str
    description: str = Field(min_length=1)
    summary: str
    main_purpose: str
    key_examples: list[str]
    action_plan: list[str]
    tools: list[str]
    tips: list[Tip]
    channel_application: str
    tags: list[str]


class ReviewIssue(BaseModel):
    kind: Literal["unsupported_claim", "wrong_fact", "missing_point", "wrong_metric", "format"]
    severity: Literal["low", "medium", "high"]
    excerpt: str
    evidence: str | None = None
    fix: str


class Review(BaseModel):
    verdict: Literal["ok", "fixed", "needs_attention"]
    issues: list[ReviewIssue]
    revised: YoutubeSummary


Summary = NoteSummary | ChatSummary | YoutubeSummary

SCHEMAS: dict[str, type[BaseModel]] = {
    model.__name__: model for model in (NoteSummary, ChatSummary, YoutubeSummary, Review)
}
```

`src/catcher/modules/llm/profiles.py`:

```python
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

BackendName = Literal["freellmapi", "claude-code", "fake"]


class Profile(BaseModel):
    backend: BackendName
    model: str | None = None
    when: Literal["now", "evening"] = "now"


class ProfilesConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    default: str
    review_profile: str = "claude-sub-evening"
    profiles: dict[str, Profile]


class UnknownProfile(ValueError):
    pass


_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(text: str, env: Mapping[str, str]) -> str:
    return _VAR.sub(lambda m: env.get(m.group(1)) or (m.group(2) or ""), text)


def load_profiles(path: Path, env: Mapping[str, str] | None = None) -> ProfilesConfig:
    raw = expand_env(path.read_text(encoding="utf-8"), os.environ if env is None else env)
    cfg = ProfilesConfig.model_validate(yaml.safe_load(raw))
    for name in (cfg.default, cfg.review_profile):
        if name not in cfg.profiles:
            raise UnknownProfile(f"profile {name!r} is not defined in {path}")
    return cfg


def resolve_profile(
    cfg: ProfilesConfig, *, requested: str | None, class_default: str | None
) -> tuple[str, Profile]:
    name = requested or class_default or cfg.default
    if name not in cfg.profiles:
        raise UnknownProfile(f"unknown LLM profile {name!r}; known: {', '.join(sorted(cfg.profiles))}")
    return name, cfg.profiles[name]
```

`profiles.yaml` (repo root):

```yaml
default: free-fast
review_profile: claude-sub-evening
evening_window: { start: "21:00", end: "06:00" }   # used from stage B; stage A runs every profile immediately
retry_time: "08:00"
stuck_after_days: 3
profiles:
  free-fast:          { backend: freellmapi,  model: "${FREELLMAPI_MODEL:-auto}", when: now }
  claude-sub-evening: { backend: claude-code, model: sonnet, when: evening }
  claude-sub-now:     { backend: claude-code, model: sonnet, when: now }
  fake:               { backend: fake, when: now }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/catcher/modules/llm profiles.yaml tests/unit/test_schemas.py tests/unit/test_profiles.py
git commit -m "feat: add LLM output schemas and named profiles"
```

---

### Task 7: The generic `reason()` step, prompts and the fake backend

**Files:**
- Create: `src/catcher/modules/llm/service.py`, `src/catcher/modules/llm/prompts.py`
- Create: `src/catcher/modules/llm/prompts/note.md`, `src/catcher/modules/llm/prompts/ai-chat.md`
- Create: `src/catcher/modules/llm/backends/__init__.py` (empty for now), `src/catcher/modules/llm/backends/fake.py`
- Create: `tests/conftest.py`
- Test: `tests/component/test_reason.py`

**Interfaces:**
- Consumes: `SCHEMAS` and `ProfilesConfig` / `Profile` (Task 6); `load` (Task 2).
- Produces (service): `Usage(tokens_in, tokens_out, duration_ms)` (Pydantic); the dataclass `BackendReply(text: str, usage: Usage, model: str | None = None)`; the `Backend` Protocol with `name: str` and `complete(self, prompt: str, *, model: str | None, task: str) -> BackendReply`; `BackendFactory = Callable[[Profile], Backend]`; the errors `LlmError`, `BackendUnavailable(LlmError)`, `UsageLimitReached(BackendUnavailable)` (constructor `(message: str, backend: str)`, attribute `.backend`) and `InvalidOutput(LlmError)`; `LlmRequest(task: str, input: dict, schema_name: str, profile: str)`; the dataclass `LlmResult(output: BaseModel, profile: str, backend: str, model: str | None, prompt_version: str, usage: Usage, attempts: int)`; `extract_json(text) -> str`; `reason(req: LlmRequest, *, profiles: ProfilesConfig, backends: BackendFactory) -> LlmResult`.
- Produces (prompts): `render_prompt(task: str, variables: dict) -> tuple[str, str]`, which returns (text, version) and raises `jinja2.UndefinedError` if a variable is missing. Prompt variables for `note` and `ai-chat`: `title_hint`, `body`, `source`, `tags` (dict with `idea_types` / `topics` / `projects`), `capture_tags`.
- Produces (fake): `FakeBackend(replies: list[str | Exception] | None = None)` with the attribute `prompts: list[str]`. Each queued reply is used once; when the queue is empty, it answers with `CANNED[task]`. `CANNED: dict[str, dict]` has keys `note`, `ai-chat`, `youtube`, `youtube-from-gemini`, `review`.
- Produces (conftest fixtures): `prompt_tags() -> dict[str, list[str]]`, `fake_profiles() -> ProfilesConfig`.

- [ ] **Step 1: Write the shared fixtures and the failing tests**

`tests/conftest.py`:

```python
import pytest

from catcher.modules.llm.profiles import Profile, ProfilesConfig


@pytest.fixture
def prompt_tags() -> dict[str, list[str]]:
    return {
        "idea_types": ["app-idea", "tech-note"],
        "topics": ["automation", "ai-agents"],
        "projects": ["idea-catcher"],
    }


@pytest.fixture
def fake_profiles() -> ProfilesConfig:
    return ProfilesConfig(default="fake", review_profile="fake", profiles={"fake": Profile(backend="fake")})
```

`tests/component/test_reason.py`:

```python
import json

import jinja2
import pytest

from catcher.modules.llm.backends.fake import CANNED, FakeBackend
from catcher.modules.llm.prompts import render_prompt
from catcher.modules.llm.schemas import NoteSummary
from catcher.modules.llm.service import (
    BackendUnavailable,
    InvalidOutput,
    LlmRequest,
    extract_json,
    reason,
)

FENCE = "`" * 3


def note_request(prompt_tags: dict) -> LlmRequest:
    return LlmRequest(
        task="note",
        input={
            "title_hint": "YouTube walks",
            "body": "Create YouTube content walking around",
            "source": None,
            "tags": prompt_tags,
            "capture_tags": ["obsidian"],
        },
        schema_name="NoteSummary",
        profile="fake",
    )


def test_valid_reply_is_parsed(fake_profiles, prompt_tags):
    fake = FakeBackend()
    result = reason(note_request(prompt_tags), profiles=fake_profiles, backends=lambda p: fake)
    assert isinstance(result.output, NoteSummary)
    assert result.output.title == CANNED["note"]["title"]
    assert (result.attempts, result.backend, result.prompt_version) == (1, "fake", "note-1")
    assert result.usage.tokens_in and result.usage.tokens_out


def test_prompt_contains_note_tags_and_schema(fake_profiles, prompt_tags):
    fake = FakeBackend()
    reason(note_request(prompt_tags), profiles=fake_profiles, backends=lambda p: fake)
    prompt = fake.prompts[0]
    assert "Create YouTube content walking around" in prompt
    assert "Idea-type tags: app-idea, tech-note" in prompt
    assert "obsidian" in prompt
    assert '"title"' in prompt and "JSON Schema" in prompt


def test_invalid_reply_is_retried_once_with_the_error(fake_profiles, prompt_tags):
    fake = FakeBackend(["not json", json.dumps(CANNED["note"])])
    result = reason(note_request(prompt_tags), profiles=fake_profiles, backends=lambda p: fake)
    assert result.attempts == 2
    assert "Your previous reply was rejected" in fake.prompts[1]
    assert "no JSON object" in fake.prompts[1]


def test_two_invalid_replies_raise(fake_profiles, prompt_tags):
    fake = FakeBackend(['{"title": ""}', "{}"])
    with pytest.raises(InvalidOutput, match="note: invalid output after 2 attempts"):
        reason(note_request(prompt_tags), profiles=fake_profiles, backends=lambda p: fake)


def test_reply_with_prose_and_code_fence_is_accepted(fake_profiles, prompt_tags):
    wrapped = f"Sure!\n{FENCE}json\n{json.dumps(CANNED['note'])}\n{FENCE}\nDone."
    fake = FakeBackend([wrapped])
    assert reason(note_request(prompt_tags), profiles=fake_profiles, backends=lambda p: fake).attempts == 1


def test_backend_errors_are_not_retried(fake_profiles, prompt_tags):
    fake = FakeBackend([BackendUnavailable("down")])
    with pytest.raises(BackendUnavailable):
        reason(note_request(prompt_tags), profiles=fake_profiles, backends=lambda p: fake)
    assert len(fake.prompts) == 1


def test_missing_prompt_variable_fails_loudly():
    with pytest.raises(jinja2.UndefinedError):
        render_prompt("note", {"body": "x"})


def test_ai_chat_prompt_renders(prompt_tags):
    text, version = render_prompt(
        "ai-chat",
        {
            "title_hint": "New chat",
            "body": "**You**\n\nq",
            "source": None,
            "tags": prompt_tags,
            "capture_tags": [],
        },
    )
    assert version == "ai-chat-1"
    assert 'Never "New chat"' in text


def test_extract_json_without_object():
    with pytest.raises(ValueError):
        extract_json("no braces")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/component/test_reason.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

`src/catcher/modules/llm/prompts.py`:

```python
from pathlib import Path
from typing import Any

import jinja2

from catcher.core.frontmatter import load

PROMPTS_DIR = Path(__file__).parent / "prompts"
_env = jinja2.Environment(
    undefined=jinja2.StrictUndefined, keep_trailing_newline=True, trim_blocks=True, lstrip_blocks=True
)


def render_prompt(task: str, variables: dict[str, Any]) -> tuple[str, str]:
    doc = load(PROMPTS_DIR / f"{task}.md")
    return _env.from_string(doc.body).render(**variables), str(doc.fm["version"])
```

`src/catcher/modules/llm/service.py`:

```python
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from catcher.modules.llm.profiles import Profile, ProfilesConfig
from catcher.modules.llm.prompts import render_prompt
from catcher.modules.llm.schemas import SCHEMAS


class Usage(BaseModel):
    tokens_in: int | None = None
    tokens_out: int | None = None
    duration_ms: int | None = None


@dataclass
class BackendReply:
    text: str
    usage: Usage
    model: str | None = None


class Backend(Protocol):
    name: str

    def complete(self, prompt: str, *, model: str | None, task: str) -> BackendReply: ...


BackendFactory = Callable[[Profile], Backend]


class LlmError(Exception):
    pass


class BackendUnavailable(LlmError):
    pass


class UsageLimitReached(BackendUnavailable):
    def __init__(self, message: str, backend: str) -> None:
        super().__init__(message)
        self.backend = backend


class InvalidOutput(LlmError):
    pass


class LlmRequest(BaseModel):
    task: str
    input: dict[str, Any]
    schema_name: str
    profile: str


@dataclass
class LlmResult:
    output: BaseModel
    profile: str
    backend: str
    model: str | None
    prompt_version: str
    usage: Usage
    attempts: int


def extract_json(text: str) -> str:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object in the reply")
    return text[start : end + 1]


def _schema_instructions(schema: type[BaseModel]) -> str:
    return (
        "\n\n## Output format\n\nReturn ONLY one JSON object, with no prose and no code fences, "
        "that matches this JSON Schema:\n\n" + json.dumps(schema.model_json_schema())
    )


def reason(req: LlmRequest, *, profiles: ProfilesConfig, backends: BackendFactory) -> LlmResult:
    profile = profiles.profiles[req.profile]
    schema = SCHEMAS[req.schema_name]
    prompt, version = render_prompt(req.task, req.input)
    prompt += _schema_instructions(schema)
    backend = backends(profile)
    tokens_in = tokens_out = 0
    error = ""
    for attempt in (1, 2):
        full = (
            prompt
            if attempt == 1
            else f"{prompt}\n\n## Your previous reply was rejected\n\n{error}\n\nReturn ONLY the corrected JSON object."
        )
        reply = backend.complete(full, model=profile.model, task=req.task)
        tokens_in += reply.usage.tokens_in or 0
        tokens_out += reply.usage.tokens_out or 0
        try:
            output = schema.model_validate_json(extract_json(reply.text))
        except (ValueError, ValidationError) as e:
            error = str(e)[:2000]
            continue
        usage = Usage(tokens_in=tokens_in, tokens_out=tokens_out, duration_ms=reply.usage.duration_ms)
        return LlmResult(
            output, req.profile, backend.name, reply.model or profile.model, version, usage, attempt
        )
    raise InvalidOutput(f"{req.task}: invalid output after 2 attempts: {error}")
```

`src/catcher/modules/llm/backends/fake.py`:

```python
import json

from catcher.modules.llm.service import BackendReply, Usage

_YOUTUBE = {
    "title": "Fake Video Summary",
    "creator": "Fake Creator",
    "description": "A canned YouTube summary used in tests.",
    "summary": "The video explains a simple system.",
    "main_purpose": "Show a repeatable system.",
    "key_examples": ["Example one"],
    "action_plan": ["Do step one"],
    "tools": ["Obsidian"],
    "tips": [{"tip": "Start small", "explanation": "Small steps stick.", "how_to_apply": "Pick one habit."}],
    "channel_application": "Use it for a demo video.",
    "tags": ["youtube-idea", "content-creation"],
}

CANNED: dict[str, dict] = {
    "note": {
        "title": "Fake Note",
        "description": "A canned note summary used in tests.",
        "body": "A cleaned-up idea.",
        "tags": ["app-idea", "automation"],
    },
    "ai-chat": {
        "title": "Fake Chat",
        "description": "A canned chat summary used in tests.",
        "summary": ["Point one"],
        "decisions": ["Decision one"],
        "options": [],
        "open_questions": [],
        "body": "## 🧩 Details\n\nMore detail.",
        "tags": ["tech-note", "ai-agents"],
    },
    "youtube": _YOUTUBE,
    "youtube-from-gemini": _YOUTUBE,
    "review": {"verdict": "ok", "issues": [], "revised": _YOUTUBE},
}


class FakeBackend:
    name = "fake"

    def __init__(self, replies: list[str | Exception] | None = None) -> None:
        self.replies: list[str | Exception] = list(replies or [])
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, model: str | None, task: str) -> BackendReply:
        self.prompts.append(prompt)
        if self.replies:
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            text = reply
        else:
            text = json.dumps(CANNED[task])
        return BackendReply(
            text=text, usage=Usage(tokens_in=len(prompt) // 4, tokens_out=len(text) // 4), model="fake"
        )
```

`src/catcher/modules/llm/prompts/note.md`:

```markdown
---
version: note-1
---
You turn a short idea note, usually dictated on a phone, into a small documentation page.

The note may contain speech-to-text mistakes and filler words. Fix them, keep the author's tone and meaning, and do not add facts or ideas that are not in the note. A two-line idea stays short: never pad it into a long page.

Title hint (the note's file name, may be wrong): {{ title_hint }}

<note>
{{ body }}
</note>

Fill in:
- title: a short, specific title (max 70 characters).
- description: one sentence (max 160 characters) saying what the idea is.
- body: the cleaned-up note as Markdown. Use short paragraphs or bullets. Only use `##` headings, each starting with an emoji, if the note has several distinct parts. Do not repeat the title as a heading. Do not use Hugo shortcodes.
- tags: exactly one idea-type tag, 1 to 4 topic tags and at most one project tag, chosen ONLY from the lists below.

Idea-type tags: {{ tags.idea_types | join(", ") }}
Topic tags: {{ tags.topics | join(", ") }}
Project tags: {{ tags.projects | join(", ") }}
{% if capture_tags %}
Tags the author added while capturing (use them if they are in the lists): {{ capture_tags | join(", ") }}
{% endif %}
```

`src/catcher/modules/llm/prompts/ai-chat.md`:

```markdown
---
version: ai-chat-1
---
You condense a long AI chat (Gemini or Claude) into a structured documentation page for the Epiaku docs site.

The chat has detours, repeated answers and step-by-step click guides. Keep the decisions, the options that were compared and the open questions. Drop fluff and detours. If code appears in several versions, keep only the latest version. Do not invent anything that is not in the chat.

Title hint (may be wrong or just "New chat"): {{ title_hint }}
Source: {{ source or "unknown" }}

<chat>
{{ body }}
</chat>

Fill in:
- title: short and specific (max 70 characters). Never "New chat".
- description: one sentence (max 160 characters).
- summary: 3 to 7 bullet points with the key takeaways.
- decisions: what was decided, one per item (empty if nothing).
- options: the alternatives compared, one per item with the main trade-off (empty if none).
- open_questions: what is still open, one per item (empty if nothing).
- body: the detailed page in Markdown: `##` headings that each start with an emoji, tables where options are compared, and only the latest version of any code in fenced code blocks. Do not repeat the summary, decisions, options or open questions. Do not use Hugo shortcodes.
- tags: exactly one idea-type tag, 1 to 4 topic tags and at most one project tag, chosen ONLY from the lists below.

Idea-type tags: {{ tags.idea_types | join(", ") }}
Topic tags: {{ tags.topics | join(", ") }}
Project tags: {{ tags.projects | join(", ") }}
{% if capture_tags %}
Tags the author added while capturing (use them if they are in the lists): {{ capture_tags | join(", ") }}
{% endif %}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/component/test_reason.py -q`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add src/catcher/modules/llm tests/conftest.py tests/component/test_reason.py
git commit -m "feat: add the generic reason() step with prompts and a fake backend"
```

---

### Task 8: The `claude -p` backend, backend factory, prompt inputs and `catcher reason` (A3)

> **Superseded in part by Task 20.** The `claude -p` backend, the fake `claude` script and its tests are deleted in Task 20. `make_backend`, the prompt inputs and `catcher reason` stay, with the new profile names.

**Files:**
- Create: `src/catcher/modules/llm/backends/claude_code.py`, `src/catcher/modules/pipeline/inputs.py`, `tests/bin/claude` (executable)
- Modify: `src/catcher/modules/llm/backends/__init__.py`, `src/catcher/cli.py`
- Test: `tests/component/test_claude_code.py`, `tests/component/test_cli_reason.py`

**Interfaces:**
- Consumes: `BackendReply`, `Usage`, `BackendUnavailable`, `UsageLimitReached`, `reason`, `LlmRequest`, `LlmError` (Task 7); `Profile`, `load_profiles`, `resolve_profile` (Task 6); `StagedNote`, `load_staged_note` (Task 4); `TagList`, `load_tags` (Task 5); `canonical_source` (Task 3); `Settings` (Task 1).
- Produces: `ClaudeCodeBackend(claude_bin: str = "claude", timeout_s: int = 600)` with `name = "claude-code"`; `make_backend(profile: Profile, settings: Settings) -> Backend`; `capture_tags(note: StagedNote) -> list[str]` (frontmatter tags without `clippings`); `title_hint(note: StagedNote) -> str`; `prompt_input(note: StagedNote, tags: TagList) -> dict[str, Any]` (keys: `title_hint`, `body`, `source`, `tags`, `capture_tags`); the CLI command `catcher reason <staged-note> [--profile P]`.

- [ ] **Step 1: Write the fake `claude` CLI**

`tests/bin/claude`:

```python
#!/usr/bin/env python3
"""Fake `claude` CLI for tests. FAKE_CLAUDE_MODE picks the behaviour."""

import json
import os
import sys

prompt = sys.stdin.read()
log = os.environ.get("FAKE_CLAUDE_ARGS_LOG")
if log:
    with open(log, "w") as f:
        json.dump({"argv": sys.argv[1:], "cwd": os.getcwd(), "prompt": prompt}, f)

mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
if mode == "ok":
    print(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": os.environ.get("FAKE_CLAUDE_RESULT", "{}"),
                "duration_ms": 1234,
                "usage": {
                    "input_tokens": 100,
                    "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 5,
                    "output_tokens": 50,
                },
            }
        )
    )
elif mode == "limit":
    print(
        json.dumps({"type": "result", "is_error": True, "result": "Claude AI usage limit reached|1760000000"})
    )
    sys.exit(1)
elif mode == "limit-text":
    print(json.dumps({"type": "result", "is_error": False, "result": "You've hit your limit · resets 9pm"}))
elif mode == "crash":
    print("boom", file=sys.stderr)
    sys.exit(2)
elif mode == "garbage":
    print("not json at all")
```

Run: `chmod +x tests/bin/claude`

- [ ] **Step 2: Write the failing tests**

`tests/component/test_claude_code.py`:

```python
import json
import os
import tempfile
from pathlib import Path

import pytest

from catcher.core.config import Settings
from catcher.modules.llm.backends import make_backend
from catcher.modules.llm.backends.claude_code import ClaudeCodeBackend
from catcher.modules.llm.backends.fake import CANNED, FakeBackend
from catcher.modules.llm.profiles import Profile, ProfilesConfig
from catcher.modules.llm.service import BackendUnavailable, LlmRequest, UsageLimitReached, reason

FAKE_CLAUDE = Path(__file__).parents[1] / "bin" / "claude"


def backend() -> ClaudeCodeBackend:
    return ClaudeCodeBackend(str(FAKE_CLAUDE), timeout_s=30)


def test_ok_returns_the_result_and_usage(monkeypatch, tmp_path):
    log = tmp_path / "call.json"
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    monkeypatch.setenv("FAKE_CLAUDE_RESULT", '{"title": "x"}')
    monkeypatch.setenv("FAKE_CLAUDE_ARGS_LOG", str(log))
    reply = backend().complete("PROMPT", model="sonnet", task="note")
    assert reply.text == '{"title": "x"}'
    assert (reply.usage.tokens_in, reply.usage.tokens_out, reply.usage.duration_ms) == (125, 50, 1234)
    call = json.loads(log.read_text())
    assert call["argv"] == ["-p", "--output-format", "json", "--max-turns", "1", "--model", "sonnet"]
    assert call["prompt"] == "PROMPT"
    assert os.path.realpath(call["cwd"]) == os.path.realpath(tempfile.gettempdir())


@pytest.mark.parametrize("mode", ["limit", "limit-text"])
def test_usage_limit_is_recognised(monkeypatch, mode):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    with pytest.raises(UsageLimitReached) as info:
        backend().complete("p", model=None, task="note")
    assert info.value.backend == "claude-code"


def test_crash_is_backend_unavailable_with_stderr(monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "crash")
    with pytest.raises(BackendUnavailable, match="boom"):
        backend().complete("p", model=None, task="note")


def test_unparseable_output_is_backend_unavailable(monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "garbage")
    with pytest.raises(BackendUnavailable, match="unparseable"):
        backend().complete("p", model=None, task="note")


def test_missing_binary_is_backend_unavailable():
    with pytest.raises(BackendUnavailable, match="not found"):
        ClaudeCodeBackend("/nonexistent/claude").complete("p", model=None, task="note")


def test_reason_through_the_fake_cli(monkeypatch, prompt_tags):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    monkeypatch.setenv("FAKE_CLAUDE_RESULT", "Here you go:\n" + json.dumps(CANNED["note"]))
    profiles = ProfilesConfig(default="c", profiles={"c": Profile(backend="claude-code", model="sonnet")})
    settings = Settings(claude_bin=str(FAKE_CLAUDE))
    request = LlmRequest(
        task="note",
        input={"title_hint": "t", "body": "b", "source": None, "tags": prompt_tags, "capture_tags": []},
        schema_name="NoteSummary",
        profile="c",
    )
    result = reason(request, profiles=profiles, backends=lambda p: make_backend(p, settings))
    assert (result.backend, result.model, result.output.title) == ("claude-code", "sonnet", "Fake Note")


def test_make_backend_builds_fake_and_claude():
    settings = Settings(claude_bin="/usr/local/bin/claude")
    assert isinstance(make_backend(Profile(backend="fake"), settings), FakeBackend)
    claude = make_backend(Profile(backend="claude-code"), settings)
    assert isinstance(claude, ClaudeCodeBackend) and claude.claude_bin == "/usr/local/bin/claude"
```

`tests/component/test_cli_reason.py`:

```python
from pathlib import Path

from typer.testing import CliRunner

from catcher.cli import app
from catcher.core.frontmatter import Doc, dump
from catcher.modules.pipeline.inputs import capture_tags, prompt_input, title_hint
from catcher.modules.pipeline.staging import load_staged_note
from catcher.modules.pipeline.tags import load_tags

REPO = Path(__file__).parents[2]


def staged(tmp_path: Path, fm: dict, body: str = "An idea\n") -> Path:
    path = tmp_path / "staging" / f"{fm['id']}.md"
    path.parent.mkdir(parents=True)
    path.write_text(dump(Doc(fm, body)))
    return path


BASE = {
    "id": "a7b2c9",
    "class": "note",
    "captured": "2026-09-27",
    "source_file": "inbox/notes/YouTube walks.md",
}


def test_prompt_input_for_a_note(tmp_path):
    note = load_staged_note(staged(tmp_path, {**BASE, "tags": ["clippings", "obsidian"]}))
    data = prompt_input(note, load_tags())
    assert data["title_hint"] == "YouTube walks"
    assert data["body"] == "An idea\n"
    assert data["source"] is None
    assert data["capture_tags"] == ["obsidian"]
    assert "app-idea" in data["tags"]["idea_types"]


def test_title_hint_prefers_the_clip_title(tmp_path):
    note = load_staged_note(staged(tmp_path, {**BASE, "title": "Idea Catcher"}))
    assert title_hint(note) == "Idea Catcher"
    assert capture_tags(note) == []


def test_reason_command_prints_validated_json(tmp_path, monkeypatch):
    monkeypatch.setenv("PROFILES_FILE", str(REPO / "profiles.yaml"))
    path = staged(tmp_path, BASE)
    result = CliRunner().invoke(app, ["reason", str(path), "--profile", "fake"])
    assert result.exit_code == 0, result.output
    assert '"title": "Fake Note"' in result.output


def test_reason_command_refuses_youtube_notes(tmp_path, monkeypatch):
    monkeypatch.setenv("PROFILES_FILE", str(REPO / "profiles.yaml"))
    path = staged(tmp_path, {**BASE, "id": "MBPHU7aaklM", "class": "youtube"})
    result = CliRunner().invoke(app, ["reason", str(path), "--profile", "fake"])
    assert result.exit_code == 2
    assert "catcher render" in result.output
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/component/test_claude_code.py tests/component/test_cli_reason.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'catcher.modules.llm.backends.claude_code'`.

- [ ] **Step 4: Write the implementation**

`src/catcher/modules/llm/backends/claude_code.py`:

```python
import json
import re
import subprocess
import tempfile
from typing import Any

from catcher.modules.llm.service import BackendReply, BackendUnavailable, Usage, UsageLimitReached

_USAGE_LIMIT = re.compile(r"usage limit|limit reached|hit your limit|rate limit", re.IGNORECASE)


class ClaudeCodeBackend:
    name = "claude-code"

    def __init__(self, claude_bin: str = "claude", timeout_s: int = 600) -> None:
        self.claude_bin = claude_bin
        self.timeout_s = timeout_s

    def complete(self, prompt: str, *, model: str | None, task: str) -> BackendReply:
        cmd = [self.claude_bin, "-p", "--output-format", "json", "--max-turns", "1"]
        if model:
            cmd += ["--model", model]
        try:
            out = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                cwd=tempfile.gettempdir(),
                timeout=self.timeout_s,
            )
        except FileNotFoundError as e:
            raise BackendUnavailable(f"claude CLI not found: {self.claude_bin}") from e
        except subprocess.TimeoutExpired as e:
            raise BackendUnavailable(f"claude -p timed out after {self.timeout_s}s") from e

        data: dict[str, Any] | None = None
        try:
            parsed = json.loads(out.stdout) if out.stdout.strip() else None
            data = parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            data = None
        result = str(data.get("result") or "") if data else ""

        if out.returncode != 0 or (data and data.get("is_error")):
            message = " ".join(part for part in (result, out.stderr.strip()) if part)[:500]
            message = message or f"claude -p exited with code {out.returncode}"
            if _USAGE_LIMIT.search(message):
                raise UsageLimitReached(message, backend=self.name)
            raise BackendUnavailable(message)
        if data is None:
            raise BackendUnavailable(f"unparseable claude -p output: {out.stdout[:200]!r}")
        if not result.lstrip().startswith("{") and _USAGE_LIMIT.search(result[:300]):
            raise UsageLimitReached(result[:300], backend=self.name)

        usage = data.get("usage") or {}
        tokens_in = sum(
            int(usage.get(key) or 0)
            for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        )
        return BackendReply(
            text=result,
            usage=Usage(
                tokens_in=tokens_in,
                tokens_out=usage.get("output_tokens"),
                duration_ms=data.get("duration_ms"),
            ),
            model=model,
        )
```

`src/catcher/modules/llm/backends/__init__.py`:

```python
from catcher.core.config import Settings
from catcher.modules.llm.backends.claude_code import ClaudeCodeBackend
from catcher.modules.llm.backends.fake import FakeBackend
from catcher.modules.llm.profiles import Profile
from catcher.modules.llm.service import Backend, BackendUnavailable


def make_backend(profile: Profile, settings: Settings) -> Backend:
    if profile.backend == "fake":
        return FakeBackend()
    if profile.backend == "claude-code":
        return ClaudeCodeBackend(settings.claude_bin, timeout_s=settings.llm_timeout_s)
    raise BackendUnavailable(f"backend {profile.backend!r} is not available")
```

`src/catcher/modules/pipeline/inputs.py`:

```python
from pathlib import Path
from typing import Any

from catcher.modules.pipeline.doctypes import canonical_source
from catcher.modules.pipeline.staging import StagedNote
from catcher.modules.pipeline.tags import TagList


def capture_tags(note: StagedNote) -> list[str]:
    raw = note.doc.fm.get("tags") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(t) for t in raw if str(t).strip().lower() != "clippings"]


def title_hint(note: StagedNote) -> str:
    title = note.doc.fm.get("title")
    if title:
        return str(title)
    return Path(str(note.doc.fm.get("source_file") or note.path.name)).stem


def prompt_input(note: StagedNote, tags: TagList) -> dict[str, Any]:
    return {
        "title_hint": title_hint(note),
        "body": note.doc.body,
        "source": canonical_source(note.doctype, note.doc.fm),
        "tags": tags.as_prompt_dict(),
        "capture_tags": capture_tags(note),
    }
```

In `src/catcher/cli.py`, add these imports:

```python
from catcher.modules.llm.backends import make_backend
from catcher.modules.llm.profiles import load_profiles, resolve_profile
from catcher.modules.llm.service import LlmError, LlmRequest, reason
from catcher.modules.pipeline.inputs import prompt_input
from catcher.modules.pipeline.staging import load_staged_note
from catcher.modules.pipeline.tags import load_tags
```

and add this command:

```python
@app.command("reason")
def reason_cmd(staged_note: Path, profile: ProfileOpt = None) -> None:
    """Run the LLM step on one staged note and print the validated JSON."""
    settings = Settings()
    note = load_staged_note(staged_note)
    if note.doctype.name in ("youtube", "youtube-gemini"):
        typer.echo("YouTube notes need facts first: use `catcher render` for them.")
        raise typer.Exit(2)
    profiles = load_profiles(settings.profiles_file)
    name, _ = resolve_profile(profiles, requested=profile, class_default=note.doctype.llm_profile)
    request = LlmRequest(
        task=note.doctype.task,
        input=prompt_input(note, load_tags()),
        schema_name=note.doctype.schema_name,
        profile=name,
    )
    try:
        result = reason(request, profiles=profiles, backends=lambda p: make_backend(p, settings))
    except LlmError as e:
        typer.echo(f"LLM step failed: {e}", err=True)
        raise typer.Exit(2) from e
    typer.echo(result.output.model_dump_json(indent=2))
    typer.echo(
        f"profile={result.profile} backend={result.backend} model={result.model} "
        f"prompt={result.prompt_version} attempts={result.attempts} "
        f"tokens_in={result.usage.tokens_in} tokens_out={result.usage.tokens_out}",
        err=True,
    )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/component -q`
Expected: all pass.

- [ ] **Step 6: Check `claude -p` by hand on the Mac**

Run: `claude --help | grep -E -- "--output-format|--max-turns|--model|--json-schema"`
Expected: the first three flags are listed. If `--json-schema` is also listed, write that down in the task's PR description as a later improvement. Do not change the code for it now.

- [ ] **Step 7: Commit**

```bash
git add tests/bin/claude src/catcher/modules/llm/backends src/catcher/modules/pipeline/inputs.py src/catcher/cli.py tests/component/test_claude_code.py tests/component/test_cli_reason.py
git commit -m "feat: add the claude -p backend and the catcher reason command"
```

---

### Task 9: The FreeLLMApi backend

> **Superseded in part by Task 20.** `FreeLlmApiBackend` becomes `OpenAiCompatibleBackend`, shared with the new `openai` backend, in Task 20.

**Files:**
- Create: `src/catcher/modules/llm/backends/freellmapi.py`
- Modify: `src/catcher/modules/llm/backends/__init__.py`
- Test: `tests/component/test_freellmapi.py`

**Interfaces:**
- Consumes: `BackendReply`, `Usage`, `BackendUnavailable` (Task 7); `make_backend` (Task 8).
- Produces: `FreeLlmApiBackend(base_url: str, api_key: str, timeout_s: int = 120)` with `name = "freellmapi"`. `make_backend` returns it for `backend: freellmapi`.

- [ ] **Step 1: Write the failing tests**

`tests/component/test_freellmapi.py`:

```python
import json

import httpx
import pytest
import respx

from catcher.core.config import Settings
from catcher.modules.llm.backends import make_backend
from catcher.modules.llm.backends.freellmapi import FreeLlmApiBackend
from catcher.modules.llm.profiles import Profile
from catcher.modules.llm.service import BackendUnavailable

BASE = "http://freellmapi.test/v1"


def completion(content: str) -> dict:
    return {
        "id": "x",
        "object": "chat.completion",
        "created": 0,
        "model": "llama-3.3-70b",
        "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
    }


@respx.mock
def test_returns_text_usage_and_model():
    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=completion('{"a": 1}'))
    )
    reply = FreeLlmApiBackend(BASE, "k").complete("PROMPT", model="auto", task="note")
    assert reply.text == '{"a": 1}'
    assert (reply.usage.tokens_in, reply.usage.tokens_out, reply.model) == (12, 3, "llama-3.3-70b")
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "auto"
    assert body["messages"] == [{"role": "user", "content": "PROMPT"}]


@respx.mock
@pytest.mark.parametrize("status", [429, 500, 413])
def test_http_errors_are_backend_unavailable(status):
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(status, json={"error": {"message": "x"}})
    )
    with pytest.raises(BackendUnavailable, match=str(status)):
        FreeLlmApiBackend(BASE, "k").complete("p", model="auto", task="note")


@respx.mock
def test_connection_error_is_backend_unavailable():
    respx.post(f"{BASE}/chat/completions").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(BackendUnavailable):
        FreeLlmApiBackend(BASE, "k").complete("p", model="auto", task="note")


def test_make_backend_builds_freellmapi():
    backend = make_backend(Profile(backend="freellmapi", model="auto"), Settings(freellmapi_url=BASE))
    assert isinstance(backend, FreeLlmApiBackend)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/component/test_freellmapi.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

`src/catcher/modules/llm/backends/freellmapi.py`:

```python
import time

import openai
from openai import OpenAI

from catcher.modules.llm.service import BackendReply, BackendUnavailable, Usage


class FreeLlmApiBackend:
    name = "freellmapi"

    def __init__(self, base_url: str, api_key: str, timeout_s: int = 120) -> None:
        self.client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout_s, max_retries=0)

    def complete(self, prompt: str, *, model: str | None, task: str) -> BackendReply:
        start = time.monotonic()
        try:
            response = self.client.chat.completions.create(
                model=model or "auto",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
            )
        except openai.APIStatusError as e:
            raise BackendUnavailable(f"FreeLLMApi HTTP {e.status_code}: {str(e)[:300]}") from e
        except openai.APIConnectionError as e:
            raise BackendUnavailable(f"FreeLLMApi unreachable: {e}") from e
        if not response.choices:
            raise BackendUnavailable("FreeLLMApi returned no choices")
        usage = response.usage
        return BackendReply(
            text=response.choices[0].message.content or "",
            usage=Usage(
                tokens_in=usage.prompt_tokens if usage else None,
                tokens_out=usage.completion_tokens if usage else None,
                duration_ms=int((time.monotonic() - start) * 1000),
            ),
            model=response.model,
        )
```

Replace `src/catcher/modules/llm/backends/__init__.py` with:

```python
from catcher.core.config import Settings
from catcher.modules.llm.backends.claude_code import ClaudeCodeBackend
from catcher.modules.llm.backends.fake import FakeBackend
from catcher.modules.llm.backends.freellmapi import FreeLlmApiBackend
from catcher.modules.llm.profiles import Profile
from catcher.modules.llm.service import Backend, BackendUnavailable


def make_backend(profile: Profile, settings: Settings) -> Backend:
    if profile.backend == "fake":
        return FakeBackend()
    if profile.backend == "claude-code":
        return ClaudeCodeBackend(settings.claude_bin, timeout_s=settings.llm_timeout_s)
    if profile.backend == "freellmapi":
        return FreeLlmApiBackend(
            settings.freellmapi_url, settings.freellmapi_api_key, timeout_s=settings.llm_timeout_s
        )
    raise BackendUnavailable(f"backend {profile.backend!r} is not available")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/component -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/catcher/modules/llm/backends tests/component/test_freellmapi.py
git commit -m "feat: add the FreeLLMApi backend"
```

---

### Task 10: Rendering note and chat pages

**Files:**
- Create: `src/catcher/modules/pipeline/render.py`, `src/catcher/modules/pipeline/templates/note.md.j2`, `src/catcher/modules/pipeline/templates/ai-chat.md.j2`
- Modify: `tests/conftest.py` (add the `make_note` and `make_result` fixtures)
- Test: `tests/component/test_render.py`

**Interfaces:**
- Consumes: `Doc`, `dump` (Task 2); `canonical_source`, `DOC_TYPES` (Task 3); `StagedNote` (Task 4); `Summary` (Task 6); `LlmResult`, `Usage` (Task 7).
- Produces: `PAGE_WEIGHT = 100`; `slugify(title: str, max_len: int = 60) -> str`; `page_filename(captured: str, doc_id: str, title: str) -> str`; `md_cell(value: object) -> str`; `fmt_count(n: int | None) -> str`; the dataclass `PageContext(note: StagedNote, summary: Summary, tags: list[str], llm: LlmResult)`; `build_frontmatter(ctx, extra_fm: dict | None = None) -> dict`; `render_page(ctx, *, extra_fm: dict | None = None, **template_vars) -> str`; `page_name(ctx) -> str`. Templates receive `s` (summary), `source`, and any `template_vars`.
- Produces (conftest): `make_note(doctype="note", *, doc_id="a7b2c9", body="An idea.\n", root: Path | None = None, **fm) -> StagedNote` (when `root` is given, it writes the note to `root/staging/<id>.md`); `make_result(output, *, profile="fake", backend="fake", model="fake", version="note-1") -> LlmResult`.

- [ ] **Step 1: Add the fixtures**

Append to `tests/conftest.py`:

```python
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from catcher.core.frontmatter import Doc, dump
from catcher.modules.llm.service import LlmResult, Usage
from catcher.modules.pipeline.doctypes import DOC_TYPES
from catcher.modules.pipeline.staging import StagedNote


@pytest.fixture
def make_note():
    def _make(
        doctype: str = "note",
        *,
        doc_id: str = "a7b2c9",
        body: str = "An idea.\n",
        root: Path | None = None,
        **fm: Any,
    ) -> StagedNote:
        base = {
            "id": doc_id,
            "class": doctype,
            "captured": "2026-09-27",
            "source_file": f"inbox/notes/{doc_id}.md",
            "staged_at": "2026-09-27T18:00:00+00:00",
        }
        doc = Doc({**base, **fm}, body)
        path = (root / "staging" / f"{doc_id}.md") if root else Path("staging") / f"{doc_id}.md"
        if root:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(dump(doc), encoding="utf-8")
        return StagedNote(doc_id, DOC_TYPES[doctype], doc, path)

    return _make


@pytest.fixture
def make_result():
    def _make(
        output: BaseModel,
        *,
        profile: str = "fake",
        backend: str = "fake",
        model: str = "fake",
        version: str = "note-1",
    ) -> LlmResult:
        return LlmResult(output, profile, backend, model, version, Usage(tokens_in=10, tokens_out=5), 1)

    return _make
```

Move the new imports to the top of the file, next to the existing ones, so ruff's import sorting passes.

- [ ] **Step 2: Write the failing tests**

`tests/component/test_render.py`:

```python
from catcher.core.frontmatter import parse
from catcher.modules.llm.schemas import ChatSummary, NoteSummary
from catcher.modules.pipeline.render import (
    PageContext,
    fmt_count,
    md_cell,
    page_filename,
    page_name,
    render_page,
    slugify,
)


def test_slugify():
    assert slugify("Idée: Café & Co / 2026!") == "idee-cafe-co-2026"
    assert slugify("🚀🚀") == "untitled"
    long = slugify("word " * 30)
    assert len(long) <= 60 and not long.endswith("-")


def test_page_filename():
    assert (
        page_filename("2026-09-27", "a7b2c9", "Idea Catcher Pipeline")
        == "20260927_a7b2c9_idea-catcher-pipeline.md"
    )


def test_md_cell_escapes_pipes_and_newlines():
    assert md_cell("a | b\nc") == "a \\| b c"


def test_fmt_count():
    assert fmt_count(1400000) == "1,400,000"
    assert fmt_count(None) == "Not available"


def test_note_page(make_note, make_result, snapshot):
    note = make_note("note")
    summary = NoteSummary(
        title="Walk-and-talk  videos",
        description="Film career stories\nwhile walking.",
        body="Record videos while walking and talk about your career.",
        tags=["youtube-idea"],
    )
    ctx = PageContext(note, summary, ["youtube-idea", "content-creation"], make_result(summary))
    page = render_page(ctx)
    doc = parse(page)
    assert doc.fm["title"] == "Walk-and-talk videos"
    assert doc.fm["description"] == "Film career stories while walking."
    assert (doc.fm["type"], doc.fm["weight"], doc.fm["id"], doc.fm["date"]) == (
        "docs",
        100,
        "a7b2c9",
        "2026-09-27",
    )
    assert doc.fm["tags"] == ["youtube-idea", "content-creation"]
    assert doc.fm["llm"] == {
        "profile": "fake",
        "backend": "fake",
        "model": "fake",
        "prompt_version": "note-1",
    }
    assert "source" not in doc.fm
    assert doc.body == "Record videos while walking and talk about your career.\n"
    assert page_name(ctx) == "20260927_a7b2c9_walk-and-talk-videos.md"
    assert page == snapshot


def test_chat_page_sections_and_canonical_source(make_note, make_result, snapshot):
    note = make_note(
        "ai-chat",
        doc_id="cf81e40b020519ef",
        body="**You**\n\nq\n",
        source="https://gemini.google.com/app/cf81e40b020519ef?is_sa=1&utm_source=sem",
    )
    summary = ChatSummary(
        title="Selling course bundles on systeme.io",
        description="Sell a full course before every part exists.",
        summary=["You can sell a bundle before all parts exist."],
        decisions=["Sell the full course now"],
        options=[],
        open_questions=["How are updates delivered?"],
        body="## 🧩 Details\n\nUse one product with drip content.",
        tags=["digital-product-idea"],
    )
    page = render_page(
        PageContext(note, summary, ["digital-product-idea", "online-courses"], make_result(summary))
    )
    doc = parse(page)
    assert doc.fm["source"] == "https://gemini.google.com/app/cf81e40b020519ef"
    assert doc.body.startswith("## 📝 Summary\n\n- You can sell a bundle before all parts exist.\n")
    assert "## ✅ Decisions\n\n- Sell the full course now\n" in doc.body
    assert "## 🔀 Options" not in doc.body
    assert "## ❓ Open Questions\n\n- How are updates delivered?\n" in doc.body
    assert doc.body.endswith("Source: <https://gemini.google.com/app/cf81e40b020519ef>\n")
    assert page == snapshot


def test_extra_frontmatter_is_added_before_llm(make_note, make_result):
    note = make_note("note")
    summary = NoteSummary(title="T", description="D", body="B", tags=[])
    page = render_page(PageContext(note, summary, ["todo"], make_result(summary)), extra_fm={"video_id": "x"})
    keys = list(parse(page).fm)
    assert keys.index("video_id") < keys.index("llm")
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/component/test_render.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 4: Write the implementation**

`src/catcher/modules/pipeline/render.py`:

```python
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jinja2

from catcher.core.frontmatter import Doc, dump
from catcher.modules.llm.schemas import Summary
from catcher.modules.llm.service import LlmResult
from catcher.modules.pipeline.doctypes import canonical_source
from catcher.modules.pipeline.staging import StagedNote

TEMPLATES_DIR = Path(__file__).parent / "templates"
PAGE_WEIGHT = 100


def slugify(title: str, max_len: int = 60) -> str:
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")
    if len(slug) > max_len:
        cut = slug[:max_len]
        slug = cut.rsplit("-", 1)[0] if "-" in cut else cut
    return slug or "untitled"


def page_filename(captured: str, doc_id: str, title: str) -> str:
    return f"{captured.replace('-', '')}_{doc_id}_{slugify(title)}.md"


def md_cell(value: object) -> str:
    return " ".join(str(value).split()).replace("|", "\\|")


def fmt_count(n: int | None) -> str:
    return f"{n:,}" if isinstance(n, int) else "Not available"


_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
    undefined=jinja2.StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
    autoescape=False,
)
_env.filters["md_cell"] = md_cell
_env.filters["num"] = fmt_count


@dataclass
class PageContext:
    note: StagedNote
    summary: Summary
    tags: list[str]
    llm: LlmResult


def _one_line(text: str) -> str:
    return " ".join(text.split())


def build_frontmatter(ctx: PageContext, extra_fm: dict[str, Any] | None = None) -> dict[str, Any]:
    fm: dict[str, Any] = {
        "title": _one_line(ctx.summary.title),
        "description": _one_line(ctx.summary.description),
        "date": str(ctx.note.doc.fm["captured"]),
        "weight": PAGE_WEIGHT,
        "type": "docs",
        "id": ctx.note.doc_id,
        "tags": ctx.tags,
    }
    source = canonical_source(ctx.note.doctype, ctx.note.doc.fm)
    if source:
        fm["source"] = source
    fm.update(extra_fm or {})
    fm["llm"] = {
        "profile": ctx.llm.profile,
        "backend": ctx.llm.backend,
        "model": ctx.llm.model,
        "prompt_version": ctx.llm.prompt_version,
    }
    return fm


def render_page(ctx: PageContext, *, extra_fm: dict[str, Any] | None = None, **template_vars: Any) -> str:
    body = _env.get_template(ctx.note.doctype.template).render(
        s=ctx.summary, source=canonical_source(ctx.note.doctype, ctx.note.doc.fm), **template_vars
    )
    return dump(Doc(build_frontmatter(ctx, extra_fm), body))


def page_name(ctx: PageContext) -> str:
    return page_filename(str(ctx.note.doc.fm["captured"]), ctx.note.doc_id, ctx.summary.title)
```

`src/catcher/modules/pipeline/templates/note.md.j2`:

```jinja
{{ s.body | trim }}
{% if source %}

---

Source: <{{ source }}>
{% endif %}
```

`src/catcher/modules/pipeline/templates/ai-chat.md.j2`:

```jinja
## 📝 Summary

{% for item in s.summary %}
- {{ item }}
{% endfor %}
{% if s.decisions %}

## ✅ Decisions

{% for item in s.decisions %}
- {{ item }}
{% endfor %}
{% endif %}
{% if s.options %}

## 🔀 Options

{% for item in s.options %}
- {{ item }}
{% endfor %}
{% endif %}
{% if s.open_questions %}

## ❓ Open Questions

{% for item in s.open_questions %}
- {{ item }}
{% endfor %}
{% endif %}
{% if s.body.strip() %}

{{ s.body | trim }}
{% endif %}
{% if source %}

---

Source: <{{ source }}>
{% endif %}
```

- [ ] **Step 5: Run the tests, create the snapshots and inspect them**

Run: `uv run pytest tests/component/test_render.py -q --snapshot-update`
Expected: all pass, and `tests/component/__snapshots__/test_render.ambr` is created. Open it and check that both pages read well: frontmatter first, headings with emoji, a blank line before each heading, and no doubled blank lines.

Run: `uv run pytest tests/component/test_render.py -q`
Expected: 7 passed.

- [ ] **Step 6: Commit**

```bash
git add src/catcher/modules/pipeline/render.py src/catcher/modules/pipeline/templates tests/conftest.py tests/component/test_render.py tests/component/__snapshots__
git commit -m "feat: render note and chat pages with Python-owned frontmatter"
```

---

### Task 11: Page validation

**Files:**
- Create: `src/catcher/modules/pipeline/validate.py`
- Test: `tests/unit/test_validate.py`

**Interfaces:**
- Consumes: `parse`, `FrontmatterError` (Task 2); `TagList`, `load_tags` (Task 5).
- Produces: `ALLOWED_SHORTCODES = frozenset({"youtube-lite", "alert"})`; `validate_page(page: str, tags: TagList) -> list[str]` (an empty list means the page is valid).

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_validate.py`:

```python
from catcher.core.frontmatter import Doc, dump
from catcher.modules.pipeline.tags import load_tags
from catcher.modules.pipeline.validate import validate_page

TAGS = load_tags()
GOOD_FM = {
    "title": "T",
    "description": "D",
    "date": "2026-09-27",
    "weight": 100,
    "type": "docs",
    "id": "a7b2c9",
    "tags": ["app-idea", "obsidian"],
}


def page(fm: dict | None = None, body: str = "Body text.\n") -> str:
    return dump(Doc(GOOD_FM if fm is None else fm, body))


def test_good_page_has_no_problems():
    assert validate_page(page(), TAGS) == []


def test_known_shortcodes_are_allowed():
    body = '{{< youtube-lite AeV5F0ppaGw `Title` >}}\n{{% alert title="x" %}}\nhi\n{{% /alert %}}\n'
    assert validate_page(page(body=body), TAGS) == []


def test_unknown_shortcode_is_rejected():
    problems = validate_page(page(body="See {{< figure src=x >}} here.\n"), TAGS)
    assert problems == ["unknown shortcodes: figure"]


def test_missing_fields_and_wrong_types():
    fm = {**GOOD_FM, "title": "", "type": "blog", "weight": "100"}
    problems = validate_page(page(fm), TAGS)
    assert "missing frontmatter field 'title'" in problems
    assert "frontmatter 'type' must be 'docs'" in problems
    assert "frontmatter 'weight' must be an integer" in problems


def test_tag_rules():
    assert "tags not in the allowed list: apps" in validate_page(
        page({**GOOD_FM, "tags": ["app-idea", "apps"]}), TAGS
    )
    assert "need exactly one idea-type tag, found 0" in validate_page(
        page({**GOOD_FM, "tags": ["obsidian"]}), TAGS
    )
    assert "need exactly one idea-type tag, found 2" in validate_page(
        page({**GOOD_FM, "tags": ["app-idea", "todo"]}), TAGS
    )


def test_empty_body():
    assert "page body is empty" in validate_page(page(body="\n\n"), TAGS)


def test_broken_frontmatter():
    assert validate_page("---\ntitle: [x\n---\nbody\n", TAGS)[0].startswith("invalid YAML")


def test_page_without_frontmatter():
    assert "page has no frontmatter" in validate_page("just text\n", TAGS)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_validate.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

`src/catcher/modules/pipeline/validate.py`:

```python
import re

from catcher.core.frontmatter import FrontmatterError, parse
from catcher.modules.pipeline.tags import TagList

ALLOWED_SHORTCODES = frozenset({"youtube-lite", "alert"})
REQUIRED_FIELDS = ("title", "description", "weight", "type", "id")
_SHORTCODE = re.compile(r"\{\{[<%]\s*/?\s*([A-Za-z0-9_.-]+)")


def validate_page(page: str, tags: TagList) -> list[str]:
    if not page.startswith("---\n"):
        return ["page has no frontmatter"]
    try:
        doc = parse(page)
    except FrontmatterError as e:
        return [str(e)]
    fm = doc.fm
    problems: list[str] = []
    for name in REQUIRED_FIELDS:
        if fm.get(name) in (None, ""):
            problems.append(f"missing frontmatter field {name!r}")
    if fm.get("type") not in (None, "", "docs"):
        problems.append("frontmatter 'type' must be 'docs'")
    if "weight" in fm and (not isinstance(fm["weight"], int) or isinstance(fm["weight"], bool)):
        problems.append("frontmatter 'weight' must be an integer")

    page_tags = fm.get("tags") or []
    if not isinstance(page_tags, list):
        problems.append("frontmatter 'tags' must be a list")
    else:
        unknown = [str(t) for t in page_tags if t not in tags.allowed]
        if unknown:
            problems.append(f"tags not in the allowed list: {', '.join(unknown)}")
        idea_types = [t for t in page_tags if t in tags.idea_types]
        if len(idea_types) != 1:
            problems.append(f"need exactly one idea-type tag, found {len(idea_types)}")

    if not doc.body.strip():
        problems.append("page body is empty")
    bad = sorted({name for name in _SHORTCODE.findall(doc.body) if name not in ALLOWED_SHORTCODES})
    if bad:
        problems.append(f"unknown shortcodes: {', '.join(bad)}")
    return problems
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_validate.py -q`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add src/catcher/modules/pipeline/validate.py tests/unit/test_validate.py
git commit -m "feat: validate rendered pages before they are written"
```

---

### Task 12: Processing one note, overwrite-by-ID publishing and `catcher render` (A4)

> **Superseded in part by Task 19.** `archive_staged()` and the `staging/` paths in this task are replaced by `finalize_output()` and `move_to_failed()` in Task 19. Rendering, overwrite-by-id and `write_page()` are unchanged.

**Files:**
- Create: `src/catcher/modules/pipeline/process.py`, `src/catcher/modules/pipeline/publish.py`
- Modify: `src/catcher/cli.py`, `tests/conftest.py` (add `make_services`)
- Test: `tests/component/test_process.py`, `tests/component/test_publish.py`

**Interfaces:**
- Consumes: `reason`, `LlmRequest`, `LlmResult`, `UsageLimitReached`, `BackendFactory` (Task 7); `make_backend` (Tasks 8/9); `resolve_profile`, `load_profiles`, `Profile`, `ProfilesConfig` (Task 6); `prompt_input`, `capture_tags` (Task 8); `normalize_tags`, `load_tags`, `TagList` (Task 5); `PageContext`, `render_page`, `page_name` (Task 10); `validate_page` (Task 11); `StagedNote`, `facts_sidecar` (Task 4); `DocType` (Task 3); `load`, `FrontmatterError` (Task 2).
- Produces (process): the dataclass `Services(settings: Settings, profiles: ProfilesConfig, backends: BackendFactory, tags: TagList)`; the dataclass `ProcessOptions(profile: str | None = None, review: bool = True, review_profile: str | None = None, dry_run: bool = False, docs_repo: Path | None = None, blocked_backends: frozenset[str] = frozenset())`; the dataclass `ProcessedPage(note, filename, page, problems: list[str], llm: LlmResult, dropped_tags: list[str], written: list[Path] = [])`; `default_services(settings) -> Services`; `check_not_blocked(profile: Profile, opts: ProcessOptions) -> None`; `process_note(note: StagedNote, svc: Services, opts: ProcessOptions) -> ProcessedPage`.
- Produces (publish): `find_pages_by_id(out_dir: Path, doc_id: str) -> list[Path]`; `write_page(docs_repo: Path, doctype: DocType, doc_id: str, filename: str, page: str) -> list[Path]` (returns the written path first, then the removed ones); `archive_staged(ideas_repo: Path, note: StagedNote) -> list[Path]` (moves the note and its facts sidecar to `archive_dir`, overwriting any earlier copy, and returns the touched paths).
- Produces (conftest): `make_services(note_backend=None, chat_backend=None) -> Services`, with profiles `free-fast` / `fake` (backend `fake`) and `claude-sub-evening` / `claude-sub-now` (backend `claude-code`). The factory returns `chat_backend` for `claude-code` profiles and `note_backend` otherwise.
- Produces (CLI): `catcher render <staged-note> [--docs PATH] [--profile P] [--no-review]`.

- [ ] **Step 1: Add the fixture**

Append to `tests/conftest.py`, moving the imports to the top:

```python
from catcher.core.config import Settings
from catcher.modules.llm.backends.fake import FakeBackend
from catcher.modules.pipeline.process import Services
from catcher.modules.pipeline.tags import load_tags


def _profiles_for_tests() -> ProfilesConfig:
    return ProfilesConfig(
        default="free-fast",
        review_profile="claude-sub-evening",
        profiles={
            "free-fast": Profile(backend="fake"),
            "fake": Profile(backend="fake"),
            "claude-sub-evening": Profile(backend="claude-code", model="sonnet", when="evening"),
            "claude-sub-now": Profile(backend="claude-code", model="sonnet"),
        },
    )


@pytest.fixture
def make_services():
    def _make(note_backend: FakeBackend | None = None, chat_backend: FakeBackend | None = None) -> Services:
        notes = note_backend or FakeBackend()
        chats = chat_backend or FakeBackend()
        return Services(
            settings=Settings(),
            profiles=_profiles_for_tests(),
            backends=lambda p: chats if p.backend == "claude-code" else notes,
            tags=load_tags(),
        )

    return _make
```

- [ ] **Step 2: Write the failing tests**

`tests/component/test_process.py`:

```python
import json

import pytest

from catcher.modules.llm.backends.fake import CANNED, FakeBackend
from catcher.modules.llm.service import UsageLimitReached
from catcher.modules.pipeline.process import ProcessOptions, process_note


def test_note_becomes_a_valid_page(make_note, make_services):
    processed = process_note(make_note("note"), make_services(), ProcessOptions())
    assert processed.problems == []
    assert processed.filename == "20260927_a7b2c9_fake-note.md"
    assert processed.llm.profile == "free-fast"
    assert "A cleaned-up idea." in processed.page


def test_chat_uses_the_class_default_profile(make_note, make_services):
    chats = FakeBackend()
    note = make_note(
        "ai-chat", doc_id="cf81e40b020519ef", source="https://gemini.google.com/app/cf81e40b020519ef"
    )
    processed = process_note(note, make_services(chat_backend=chats), ProcessOptions())
    assert processed.llm.profile == "claude-sub-evening"
    assert len(chats.prompts) == 1


def test_requested_profile_wins(make_note, make_services):
    notes = FakeBackend()
    note = make_note("ai-chat", doc_id="cf81e40b020519ef")
    processed = process_note(note, make_services(note_backend=notes), ProcessOptions(profile="fake"))
    assert processed.llm.profile == "fake"
    assert len(notes.prompts) == 1


def test_capture_tags_are_merged_and_unknown_tags_dropped(make_note, make_services):
    reply = json.dumps({**CANNED["note"], "tags": ["app-idea", "nonsense"]})
    note = make_note("note", tags=["clippings", "obsidian"])
    processed = process_note(note, make_services(note_backend=FakeBackend([reply])), ProcessOptions())
    assert "- obsidian" in processed.page
    assert processed.dropped_tags == ["nonsense"]


def test_page_without_an_idea_type_has_a_problem(make_note, make_services):
    reply = json.dumps({**CANNED["note"], "tags": ["obsidian"]})
    processed = process_note(
        make_note("note"), make_services(note_backend=FakeBackend([reply])), ProcessOptions()
    )
    assert processed.problems == ["need exactly one idea-type tag, found 0"]


def test_blocked_backend_is_not_called(make_note, make_services):
    chats = FakeBackend()
    note = make_note("ai-chat", doc_id="cf81e40b020519ef")
    with pytest.raises(UsageLimitReached):
        process_note(
            note,
            make_services(chat_backend=chats),
            ProcessOptions(blocked_backends=frozenset({"claude-code"})),
        )
    assert chats.prompts == []
```

`tests/component/test_publish.py`:

```python
from pathlib import Path

from typer.testing import CliRunner

from catcher.cli import app
from catcher.core.frontmatter import Doc, dump
from catcher.modules.pipeline.doctypes import NOTE, YOUTUBE
from catcher.modules.pipeline.publish import archive_staged, find_pages_by_id, write_page
from catcher.modules.pipeline.staging import facts_sidecar

REPO = Path(__file__).parents[2]


def put(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def page(doc_id: str | None, title: str = "T") -> str:
    fm = {"title": title} if doc_id is None else {"title": title, "id": doc_id}
    return dump(Doc(fm, "x\n"))


def test_write_page_replaces_the_page_with_the_same_id(tmp_path):
    out = tmp_path / NOTE.out_dir
    old = put(out / "20260927_a7b2c9_old-title.md", page("a7b2c9"))
    index = put(out / "_index.md", page("a7b2c9"))
    hand = put(out / "hand-written.md", page(None))
    broken = put(out / "broken.md", "---\ntitle: [\n---\n")
    touched = write_page(tmp_path, NOTE, "a7b2c9", "20260927_a7b2c9_new-title.md", "NEW")
    new = out / "20260927_a7b2c9_new-title.md"
    assert new.read_text() == "NEW"
    assert touched == [new, old]
    assert not old.exists()
    assert index.exists() and hand.exists() and broken.exists()


def test_same_filename_is_simply_overwritten(tmp_path):
    out = tmp_path / NOTE.out_dir
    same = put(out / "20260927_a7b2c9_x.md", page("a7b2c9"))
    assert write_page(tmp_path, NOTE, "a7b2c9", "20260927_a7b2c9_x.md", "NEW") == [same]
    assert same.read_text() == "NEW"


def test_ids_match_exactly_even_with_underscores(tmp_path):
    out = tmp_path / YOUTUBE.out_dir
    put(out / "20260927_ab_cd-efghi_x.md", page("ab_cd-efghi"))
    put(out / "20260927_ab_y.md", page("ab"))
    assert [p.name for p in find_pages_by_id(out, "ab")] == ["20260927_ab_y.md"]


def test_archive_moves_the_note_and_its_facts(tmp_path, make_note):
    note = make_note("youtube", doc_id="MBPHU7aaklM", root=tmp_path)
    sidecar = put(facts_sidecar(note.path), "{}")
    touched = archive_staged(tmp_path, note)
    dest = tmp_path / "archive/youtube/MBPHU7aaklM.md"
    assert dest.exists() and (tmp_path / "archive/youtube/MBPHU7aaklM.youtube.json").exists()
    assert not note.path.exists() and not sidecar.exists()
    assert touched == [note.path, dest, sidecar, tmp_path / "archive/youtube/MBPHU7aaklM.youtube.json"]


def test_archive_overwrites_an_earlier_archived_copy(tmp_path, make_note):
    put(tmp_path / "archive/notes/a7b2c9.md", "old")
    note = make_note("note", root=tmp_path)
    archive_staged(tmp_path, note)
    assert "An idea." in (tmp_path / "archive/notes/a7b2c9.md").read_text()


def test_render_command_writes_the_page(tmp_path, make_note, monkeypatch):
    monkeypatch.setenv("PROFILES_FILE", str(REPO / "profiles.yaml"))
    note = make_note("note", root=tmp_path / "ideas")
    docs = tmp_path / "docs"
    result = CliRunner().invoke(app, ["render", str(note.path), "--docs", str(docs), "--profile", "fake"])
    assert result.exit_code == 0, result.output
    assert (docs / NOTE.out_dir / "20260927_a7b2c9_fake-note.md").exists()
    assert note.path.exists()
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/component/test_process.py tests/component/test_publish.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'catcher.modules.pipeline.process'`.

- [ ] **Step 4: Write the implementation**

`src/catcher/modules/pipeline/process.py`:

```python
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from catcher.core.config import Settings
from catcher.modules.llm.backends import make_backend
from catcher.modules.llm.profiles import Profile, ProfilesConfig, load_profiles, resolve_profile
from catcher.modules.llm.schemas import Summary
from catcher.modules.llm.service import BackendFactory, LlmRequest, LlmResult, UsageLimitReached, reason
from catcher.modules.pipeline.inputs import capture_tags, prompt_input
from catcher.modules.pipeline.render import PageContext, page_name, render_page
from catcher.modules.pipeline.staging import StagedNote
from catcher.modules.pipeline.tags import TagList, load_tags, normalize_tags
from catcher.modules.pipeline.validate import validate_page


@dataclass
class Services:
    settings: Settings
    profiles: ProfilesConfig
    backends: BackendFactory
    tags: TagList


@dataclass
class ProcessOptions:
    profile: str | None = None
    review: bool = True
    review_profile: str | None = None
    dry_run: bool = False
    docs_repo: Path | None = None
    blocked_backends: frozenset[str] = frozenset()


@dataclass
class ProcessedPage:
    note: StagedNote
    filename: str
    page: str
    problems: list[str]
    llm: LlmResult
    dropped_tags: list[str]
    written: list[Path] = field(default_factory=list)


def default_services(settings: Settings) -> Services:
    return Services(
        settings=settings,
        profiles=load_profiles(settings.profiles_file),
        backends=lambda p: make_backend(p, settings),
        tags=load_tags(),
    )


def check_not_blocked(profile: Profile, opts: ProcessOptions) -> None:
    if profile.backend in opts.blocked_backends:
        raise UsageLimitReached("usage limit was reached earlier in this run", backend=profile.backend)


def process_note(note: StagedNote, svc: Services, opts: ProcessOptions) -> ProcessedPage:
    profile_name, profile = resolve_profile(
        svc.profiles, requested=opts.profile, class_default=note.doctype.llm_profile
    )
    check_not_blocked(profile, opts)
    request = LlmRequest(
        task=note.doctype.task,
        input=prompt_input(note, svc.tags),
        schema_name=note.doctype.schema_name,
        profile=profile_name,
    )
    result = reason(request, profiles=svc.profiles, backends=svc.backends)
    summary = cast(Summary, result.output)
    tag_result = normalize_tags([*summary.tags, *capture_tags(note)], svc.tags)
    ctx = PageContext(note=note, summary=summary, tags=tag_result.tags, llm=result)
    page = render_page(ctx)
    return ProcessedPage(
        note, page_name(ctx), page, validate_page(page, svc.tags), result, tag_result.dropped
    )
```

`src/catcher/modules/pipeline/publish.py`:

```python
import shutil
from pathlib import Path

from catcher.core.frontmatter import FrontmatterError, load
from catcher.modules.pipeline.doctypes import DocType
from catcher.modules.pipeline.staging import StagedNote, facts_sidecar


def find_pages_by_id(out_dir: Path, doc_id: str) -> list[Path]:
    if not out_dir.is_dir():
        return []
    found: list[Path] = []
    for path in sorted(out_dir.glob("*.md")):
        if path.name == "_index.md":
            continue
        try:
            fm = load(path).fm
        except (FrontmatterError, UnicodeDecodeError):
            continue
        if str(fm.get("id", "")) == doc_id:
            found.append(path)
    return found


def write_page(docs_repo: Path, doctype: DocType, doc_id: str, filename: str, page: str) -> list[Path]:
    out_dir = docs_repo / doctype.out_dir
    target = out_dir / filename
    old = [p for p in find_pages_by_id(out_dir, doc_id) if p != target]
    for path in old:
        path.unlink()
    out_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(page, encoding="utf-8")
    return [target, *old]


def archive_staged(ideas_repo: Path, note: StagedNote) -> list[Path]:
    dest_dir = ideas_repo / note.doctype.archive_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    touched: list[Path] = []
    for src in (note.path, facts_sidecar(note.path)):
        if src.exists():
            dest = dest_dir / src.name
            shutil.move(src, dest)
            touched += [src, dest]
    return touched
```

In `src/catcher/cli.py`, add these imports:

```python
from catcher.modules.pipeline.process import ProcessOptions, default_services, process_note
from catcher.modules.pipeline.publish import write_page
```

and add this command:

```python
@app.command()
def render(
    staged_note: Path,
    docs: DocsOpt = None,
    profile: ProfileOpt = None,
    no_review: Annotated[bool, typer.Option("--no-review", help="skip the YouTube reviewer")] = False,
) -> None:
    """Summarize one staged note and write its page into the docs checkout (no archive, no git)."""
    settings = Settings()
    docs_repo = docs or settings.docs_repo
    note = load_staged_note(staged_note)
    opts = ProcessOptions(profile=profile, review=not no_review, docs_repo=docs_repo)
    try:
        processed = process_note(note, default_services(settings), opts)
    except LlmError as e:
        typer.echo(f"LLM step failed: {e}", err=True)
        raise typer.Exit(2) from e
    for problem in processed.problems:
        typer.echo(f"problem: {problem}", err=True)
    if processed.problems:
        raise typer.Exit(1)
    touched = write_page(docs_repo, note.doctype, note.doc_id, processed.filename, processed.page)
    typer.echo(f"wrote   {touched[0]}")
    for old in touched[1:]:
        typer.echo(f"removed {old}")
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/catcher/modules/pipeline/process.py src/catcher/modules/pipeline/publish.py src/catcher/cli.py tests/conftest.py tests/component/test_process.py tests/component/test_publish.py
git commit -m "feat: process one note into a page and publish it by id"
```

---

### Task 13: Git helpers that commit only our own files (A5)

**Files:**
- Create: `src/catcher/core/git.py`, `tests/integration/git/conftest.py`
- Test: `tests/integration/git/test_git.py`

**Interfaces:**
- Produces: `GitError(RuntimeError)`; `git(repo: Path, *args: str) -> str`; `has_remote(repo: Path) -> bool`; `pull(repo: Path) -> None` (does nothing without a remote; otherwise runs `pull --rebase --autostash`); `commit_paths(repo: Path, paths: Iterable[Path], message: str, *, author: tuple[str, str]) -> bool` (runs `git commit --only` on exactly those paths, returns False when nothing changed, and ignores paths git never knew); `push(repo: Path) -> None` (does nothing without a remote; if rejected, rebases and retries once).
- Produces (fixtures in `tests/integration/git/conftest.py`): autouse `git_identity`; `sh() -> Callable[[Path, *str], str]`; `make_repo() -> Callable[[str, dict[str, str]], tuple[Path, Path]]`, which returns `(bare, working clone)` with a pushed `seed` commit on `main`.

- [ ] **Step 1: Write the fixtures and the failing tests**

`tests/integration/git/conftest.py`:

```python
import os
import subprocess
from pathlib import Path

import pytest


def _sh(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    for key, value in {
        "GIT_AUTHOR_NAME": "Tester",
        "GIT_AUTHOR_EMAIL": "tester@example.com",
        "GIT_COMMITTER_NAME": "Tester",
        "GIT_COMMITTER_EMAIL": "tester@example.com",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def sh():
    return _sh


@pytest.fixture
def make_repo(tmp_path):
    def _make(name: str, files: dict[str, str]) -> tuple[Path, Path]:
        bare = tmp_path / f"{name}.git"
        _sh(tmp_path, "init", "--bare", "-b", "main", str(bare))
        work = tmp_path / name
        _sh(tmp_path, "clone", str(bare), str(work))
        _sh(work, "checkout", "-B", "main")
        for rel, text in {"README.md": "# test\n", **files}.items():
            path = work / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        _sh(work, "add", "-A")
        _sh(work, "commit", "-m", "seed")
        _sh(work, "push", "-u", "origin", "main")
        return bare, work

    return _make
```

`tests/integration/git/test_git.py`:

```python
from catcher.core.git import commit_paths, pull, push

AUTHOR = ("idea-catcher", "bot@example.com")


def test_commit_paths_leaves_unrelated_changes_alone(make_repo, sh):
    _, work = make_repo(
        "docs", {"hugo/content/en/docs/idea-bucket/clippings/old.md": "old\n", "other.md": "x\n"}
    )
    (work / "hugo/content/en/docs/idea-bucket/clippings/old.md").unlink()
    (work / "other.md").write_text("changed\n")
    (work / "staged-by-user.md").write_text("u\n")
    sh(work, "add", "staged-by-user.md")
    new_page = work / "hugo/content/en/docs/idea-bucket/notes/20260927_a7b2c9_x.md"
    new_page.parent.mkdir(parents=True)
    new_page.write_text("page\n")

    assert commit_paths(work, [new_page], "idea-catcher: publish 1 page(s)", author=AUTHOR)

    assert sh(work, "show", "--name-only", "--format=", "HEAD").splitlines() == [
        "hugo/content/en/docs/idea-bucket/notes/20260927_a7b2c9_x.md"
    ]
    status = sh(work, "status", "--porcelain").splitlines()
    assert " D hugo/content/en/docs/idea-bucket/clippings/old.md" in status
    assert " M other.md" in status
    assert "A  staged-by-user.md" in status
    assert sh(work, "log", "-1", "--format=%an <%ae>").strip() == "idea-catcher <bot@example.com>"


def test_commit_paths_records_deletions_and_skips_unknown_paths(make_repo, sh):
    _, work = make_repo("ideas", {"inbox/notes/a note.md": "hi\n"})
    (work / "inbox/notes/a note.md").unlink()
    staged = work / "staging/a7b2c9.md"
    staged.parent.mkdir()
    staged.write_text("staged\n")
    ghost = work / "inbox/notes/never committed.md"
    assert commit_paths(work, [work / "inbox/notes/a note.md", staged, ghost], "stage", author=AUTHOR)
    assert sh(work, "show", "--name-status", "--format=", "HEAD").splitlines() == [
        "D\tinbox/notes/a note.md",
        "A\tstaging/a7b2c9.md",
    ]


def test_nothing_to_commit_returns_false(make_repo):
    _, work = make_repo("ideas", {})
    assert commit_paths(work, [work / "README.md"], "noop", author=AUTHOR) is False
    assert commit_paths(work, [], "noop", author=AUTHOR) is False


def test_pull_and_push_do_nothing_without_a_remote(tmp_path, sh):
    sh(tmp_path, "init", "-b", "main", "local")
    pull(tmp_path / "local")
    push(tmp_path / "local")


def test_push_rebases_over_a_phone_push(make_repo, sh, tmp_path):
    bare, work = make_repo("ideas", {})
    phone = tmp_path / "phone"
    sh(tmp_path, "clone", str(bare), str(phone))
    (phone / "inbox").mkdir()
    (phone / "inbox/new.md").write_text("from the phone\n")
    sh(phone, "add", "-A")
    sh(phone, "commit", "-m", "phone")
    sh(phone, "push")

    (work / "staging").mkdir()
    (work / "staging/x.md").write_text("x\n")
    commit_paths(work, [work / "staging/x.md"], "stage", author=AUTHOR)
    push(work)

    assert sh(bare, "log", "--format=%s", "main").splitlines() == ["stage", "phone", "seed"]


def test_pull_brings_in_remote_changes(make_repo, sh, tmp_path):
    bare, work = make_repo("docs", {})
    other = tmp_path / "other"
    sh(tmp_path, "clone", str(bare), str(other))
    (other / "new.md").write_text("n\n")
    sh(other, "add", "-A")
    sh(other, "commit", "-m", "other")
    sh(other, "push")
    (work / "README.md").write_text("dirty local change\n")
    pull(work)
    assert (work / "new.md").exists()
    assert (work / "README.md").read_text() == "dirty local change\n"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/integration/git/test_git.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'catcher.core.git'`.

- [ ] **Step 3: Write the implementation**

`src/catcher/core/git.py`:

```python
import subprocess
from collections.abc import Iterable
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if out.returncode != 0:
        raise GitError(f"git {' '.join(args[:3])} failed in {repo}: {out.stderr.strip()[:500]}")
    return out.stdout


def has_remote(repo: Path) -> bool:
    return bool(git(repo, "remote").strip())


def pull(repo: Path) -> None:
    if has_remote(repo):
        git(repo, "pull", "--rebase", "--autostash")


def _relative(repo: Path, paths: Iterable[Path]) -> list[str]:
    root = repo.resolve()
    return sorted({p.resolve().relative_to(root).as_posix() for p in paths})


def commit_paths(repo: Path, paths: Iterable[Path], message: str, *, author: tuple[str, str]) -> bool:
    rels = _relative(repo, paths)
    if not rels:
        return False
    existing = [rel for rel in rels if (repo / rel).exists()]
    if existing:
        git(repo, "add", "--", *existing)
    in_index = [rel for rel in git(repo, "ls-files", "-z", "--", *rels).split("\0") if rel]
    if not in_index or not git(repo, "status", "--porcelain", "--", *in_index).strip():
        return False
    name, email = author
    git(
        repo,
        "-c",
        f"user.name={name}",
        "-c",
        f"user.email={email}",
        "commit",
        "--only",
        f"--author={name} <{email}>",
        "-m",
        message,
        "--",
        *in_index,
    )
    return True


def push(repo: Path) -> None:
    if not has_remote(repo):
        return
    try:
        git(repo, "push")
    except GitError:
        git(repo, "pull", "--rebase", "--autostash")
        git(repo, "push")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/integration/git/test_git.py -q`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/catcher/core/git.py tests/integration/git
git commit -m "feat: commit only the pipeline's own files and push with one rebase retry"
```

---

### Task 14: The whole run, `catcher run pipeline` (A5 + A8 for notes and chats)

> **Superseded in part by Task 19.** The `stage_inbox` / `load_staged` / `archive_staged` calls and the "stays in `staging/`" expectations are replaced in Task 19: failures move to `failed/`, deferrals stay in `output/`, published notes end as the page in `output/`.

**Files:**
- Create: `src/catcher/modules/pipeline/run.py`
- Modify: `src/catcher/cli.py`
- Test: `tests/integration/git/test_run.py`

**Interfaces:**
- Consumes: `stage_inbox`, `load_staged` (Task 4); `process_note`, `ProcessOptions`, `Services` (Task 12); `write_page`, `archive_staged` (Task 12); `commit_paths`, `pull`, `push` (Task 13); `UsageLimitReached`, `BackendUnavailable`, `InvalidOutput` (Task 7).
- Produces: `Status = Literal["published", "would_publish", "deferred", "failed", "skipped"]`; the dataclass `ItemReport(doc_id, doc_class, status, message="", page=None, tokens_in=None, tokens_out=None)`; the dataclass `RunOptions(profile=None, review=True, review_profile=None, dry_run=False, push=False, limit: int | None = None)`; the dataclass `RunReport(items, staging_errors, committed: dict[str, bool], pushed: bool)` with `.counts() -> dict[str, int]`; `run_pipeline(ideas: Path, docs: Path, opts: RunOptions, svc: Services) -> RunReport`. The CLI command is `catcher run pipeline [--ideas] [--docs] [--profile] [--no-review] [--dry-run] [--push] [--limit N]`. It exits with 1 if anything failed or could not be staged.

- [ ] **Step 1: Write the failing tests**

`tests/integration/git/test_run.py`:

```python
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from catcher.cli import app
from catcher.core.frontmatter import load
from catcher.modules.llm.backends.fake import CANNED, FakeBackend
from catcher.modules.llm.service import UsageLimitReached
from catcher.modules.pipeline.run import RunOptions, run_pipeline

REPO = Path(__file__).parents[3]
NOTES = "hugo/content/en/docs/idea-bucket/notes"
CLIPPING = "hugo/content/en/docs/idea-bucket/clippings"


def chat(chat_id: str, body: str = "**You**\n\nsell bundles?\n\n---\n\n**Gemini**\n\nYes.\n") -> str:
    return (
        f'---\nsource : "https://gemini.google.com/app/{chat_id}?is_sa=1"\n'
        f'created: 2026-09-25\ntags:\n  - "clippings"\n---\n{body}'
    )


@pytest.fixture
def repos(make_repo):
    ideas_bare, ideas = make_repo(
        "idea-bucket",
        {
            "inbox/notes/YouTube walks.md": "Create YouTube content walking around\n",
            "inbox/clippings/systeme.md": chat("cf81e40b020519ef"),
        },
    )
    docs_bare, docs = make_repo(
        "epiaku-docs",
        {
            f"{NOTES}/_index.md": "---\ntitle: Notes\n---\n",
            f"{CLIPPING}/_index.md": "---\ntitle: Clippings\n---\n",
        },
    )
    return SimpleNamespace(ideas=ideas, docs=docs, ideas_bare=ideas_bare, docs_bare=docs_bare)


def files(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): p.read_text()
        for p in root.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(root).parts
    }


def test_run_publishes_archives_and_commits(repos, make_services, sh):
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    assert report.counts() == {"published": 2}
    [note_page] = [p.name for p in (repos.docs / NOTES).glob("*.md") if p.name != "_index.md"]
    assert note_page.endswith("_fake-note.md")
    assert [p.name for p in (repos.docs / CLIPPING).glob("2026*.md")] == [
        "20260925_cf81e40b020519ef_fake-chat.md"
    ]
    assert not list((repos.ideas / "inbox").rglob("*.md"))
    assert not list((repos.ideas / "staging").glob("*"))
    assert (repos.ideas / "archive/clippings/cf81e40b020519ef.md").exists()
    assert report.committed == {"docs": True, "ideas": True}
    assert sh(repos.docs, "status", "--porcelain") == ""
    assert sh(repos.ideas, "status", "--porcelain") == ""
    assert sh(repos.docs, "log", "-1", "--format=%s").strip() == "idea-catcher: publish 2 page(s)"
    assert sh(repos.docs_bare, "log", "--format=%s", "main").strip() == "seed"


def test_push_updates_both_remotes(repos, make_services, sh):
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(push=True), make_services())
    assert report.pushed
    assert (
        sh(repos.docs_bare, "log", "-1", "--format=%s", "main").strip() == "idea-catcher: publish 2 page(s)"
    )
    assert sh(repos.ideas_bare, "log", "-1", "--format=%s", "main").strip().startswith("idea-catcher:")


def test_dry_run_touches_nothing(repos, make_services):
    before = (files(repos.ideas), files(repos.docs))
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(dry_run=True), make_services())
    assert report.counts() == {"would_publish": 2}
    assert (files(repos.ideas), files(repos.docs)) == before
    assert report.committed == {}


def test_invalid_output_fails_one_note_and_keeps_it_staged(repos, make_services, sh):
    chats = FakeBackend(["nope", "still nope"])
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services(chat_backend=chats))
    assert report.counts() == {"published": 1, "failed": 1}
    assert (repos.ideas / "staging/cf81e40b020519ef.md").exists()
    assert sh(repos.ideas, "status", "--porcelain") == ""


def test_usage_limit_defers_every_claude_note_but_not_free_notes(repos, make_services):
    (repos.ideas / "inbox/clippings/second.md").write_text(chat("925d9b0b4ca21b63"))
    chats = FakeBackend([UsageLimitReached("Claude AI usage limit reached", backend="claude-code")])
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services(chat_backend=chats))
    assert report.counts() == {"published": 1, "deferred": 2}
    assert len(chats.prompts) == 1
    assert {i.doc_id for i in report.items if i.status == "deferred"} == {
        "cf81e40b020519ef",
        "925d9b0b4ca21b63",
    }


def test_limit_processes_at_most_n_notes(repos, make_services):
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(limit=1), make_services())
    assert report.counts() == {"published": 1, "skipped": 1}
    assert len(list((repos.ideas / "staging").glob("*.md"))) == 1


def test_reclipped_chat_overwrites_its_page(repos, make_services):
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    (repos.ideas / "inbox/clippings/systeme again.md").write_text(
        chat("cf81e40b020519ef", "**You**\n\nlonger\n" * 3)
    )
    v2 = json.dumps({**CANNED["ai-chat"], "title": "Bundles v2"})
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services(chat_backend=FakeBackend([v2])))
    pages = sorted((repos.docs / CLIPPING).glob("2026*.md"))
    assert [p.name for p in pages] == ["20260925_cf81e40b020519ef_bundles-v2.md"]
    assert load(pages[0]).fm["title"] == "Bundles v2"


def test_unrelated_docs_changes_are_not_committed(repos, make_services, sh):
    (repos.docs / "README.md").write_text("my own edit\n")
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    assert sh(repos.docs, "status", "--porcelain").splitlines() == [" M README.md"]


def test_cli_run_pipeline(repos, monkeypatch):
    monkeypatch.setenv("PROFILES_FILE", str(REPO / "profiles.yaml"))
    args = ["run", "pipeline", "--ideas", str(repos.ideas), "--docs", str(repos.docs), "--profile", "fake"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "published" in result.output
    assert "summary:" in result.output
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/integration/git/test_run.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'catcher.modules.pipeline.run'`.

- [ ] **Step 3: Write the implementation**

`src/catcher/modules/pipeline/run.py`:

```python
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from catcher.core.git import commit_paths, pull, push
from catcher.modules.llm.service import BackendUnavailable, InvalidOutput, UsageLimitReached
from catcher.modules.pipeline.process import ProcessOptions, Services, process_note
from catcher.modules.pipeline.publish import archive_staged, write_page
from catcher.modules.pipeline.staging import load_staged, stage_inbox

Status = Literal["published", "would_publish", "deferred", "failed", "skipped"]


@dataclass
class ItemReport:
    doc_id: str
    doc_class: str
    status: Status
    message: str = ""
    page: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None


@dataclass
class RunOptions:
    profile: str | None = None
    review: bool = True
    review_profile: str | None = None
    dry_run: bool = False
    push: bool = False
    limit: int | None = None


@dataclass
class RunReport:
    items: list[ItemReport] = field(default_factory=list)
    staging_errors: dict[str, str] = field(default_factory=dict)
    committed: dict[str, bool] = field(default_factory=dict)
    pushed: bool = False

    def counts(self) -> dict[str, int]:
        return dict(Counter(item.status for item in self.items))


def run_pipeline(ideas: Path, docs: Path, opts: RunOptions, svc: Services) -> RunReport:
    report = RunReport()
    if opts.push:
        pull(ideas)
        pull(docs)

    staging = stage_inbox(ideas, dry_run=opts.dry_run)
    report.staging_errors = staging.errors
    touched_ideas: list[Path] = list(staging.touched)
    touched_docs: list[Path] = []

    notes = {note.doc_id: note for note in load_staged(ideas)}
    notes.update({note.doc_id: note for note in staging.staged})
    ordered = sorted(notes.values(), key=lambda n: (str(n.doc.fm.get("captured", "")), n.doc_id))

    blocked: set[str] = set()
    attempted = 0
    for note in ordered:
        item = ItemReport(note.doc_id, note.doctype.name, "skipped")
        report.items.append(item)
        if opts.limit is not None and attempted >= opts.limit:
            item.message = "run limit reached"
            continue
        attempted += 1
        popts = ProcessOptions(
            profile=opts.profile,
            review=opts.review,
            review_profile=opts.review_profile,
            dry_run=opts.dry_run,
            docs_repo=docs,
            blocked_backends=frozenset(blocked),
        )
        try:
            processed = process_note(note, svc, popts)
        except UsageLimitReached as e:
            blocked.add(e.backend)
            item.status, item.message = "deferred", f"usage limit ({e.backend}): {e}"
            continue
        except BackendUnavailable as e:
            item.status, item.message = "deferred", str(e)
            continue
        except InvalidOutput as e:
            item.status, item.message = "failed", str(e)
            continue

        touched_ideas += processed.written
        item.tokens_in, item.tokens_out = processed.llm.usage.tokens_in, processed.llm.usage.tokens_out
        item.page = processed.filename
        if processed.problems:
            item.status, item.message = "failed", "; ".join(processed.problems)
        elif opts.dry_run:
            item.status = "would_publish"
        else:
            touched_docs += write_page(docs, note.doctype, note.doc_id, processed.filename, processed.page)
            touched_ideas += archive_staged(ideas, note)
            item.status = "published"
        if processed.dropped_tags:
            dropped = f"dropped tags: {', '.join(processed.dropped_tags)}"
            item.message = f"{item.message}; {dropped}" if item.message else dropped

    if not opts.dry_run:
        author = (svc.settings.git_author_name, svc.settings.git_author_email)
        published = report.counts().get("published", 0)
        report.committed["docs"] = commit_paths(
            docs, touched_docs, f"idea-catcher: publish {published} page(s)", author=author
        )
        report.committed["ideas"] = commit_paths(
            ideas,
            touched_ideas,
            f"idea-catcher: stage and archive captures ({published} published)",
            author=author,
        )
        if opts.push:
            push(docs)
            push(ideas)
            report.pushed = True
    return report
```

In `src/catcher/cli.py`, add this import:

```python
from catcher.modules.pipeline.run import RunOptions, run_pipeline
```

and add the `run` group:

```python
run_app = typer.Typer(no_args_is_help=True, help="Run a whole flow.")
app.add_typer(run_app, name="run")


@run_app.callback()
def run_group() -> None:
    """Run a whole flow."""


@run_app.command("pipeline")
def run_pipeline_cmd(
    ideas: IdeasOpt = None,
    docs: DocsOpt = None,
    profile: ProfileOpt = None,
    no_review: Annotated[bool, typer.Option("--no-review", help="skip the YouTube reviewer")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="change no files, commit nothing")] = False,
    push: Annotated[bool, typer.Option("--push", help="push both repos (off by default)")] = False,
    limit: Annotated[int | None, typer.Option("--limit", help="process at most N staged notes")] = None,
) -> None:
    """Stage the inbox, summarize, publish pages, archive and commit."""
    settings = Settings()
    opts = RunOptions(profile=profile, review=not no_review, dry_run=dry_run, push=push, limit=limit)
    report = run_pipeline(
        ideas or settings.ideas_repo, docs or settings.docs_repo, opts, default_services(settings)
    )
    for item in report.items:
        detail = " ".join(part for part in (item.page or "", item.message) if part)
        typer.echo(f"{item.status:<14} {item.doc_class:<15} {item.doc_id:<24} {detail}")
    for rel, error in report.staging_errors.items():
        typer.echo(f"{'stage-error':<14} {rel}: {error}")
    typer.echo(f"summary: {report.counts()} committed={report.committed} pushed={report.pushed}")
    failed = bool(report.staging_errors) or report.counts().get("failed", 0) > 0
    raise typer.Exit(1 if failed else 0)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/catcher/modules/pipeline/run.py src/catcher/cli.py tests/integration/git/test_run.py
git commit -m "feat: run the whole pipeline with per-note errors, usage-limit blocking and --limit"
```

---

### Task 15: YouTube facts (yt-dlp + transcript) and `catcher youtube facts` (A6)

> **Superseded in part by Task 19.** The sidecar path `staging/<id>.youtube.json` becomes `output/<sub>/<name>.youtube.json` in Task 19 (next to the note, kept next to the final page).

**Files:**
- Create: `src/catcher/modules/youtube/facts.py`, `tests/fixtures/youtube/MBPHU7aaklM.json`
- Modify: `src/catcher/cli.py`
- Test: `tests/component/test_facts.py`

**Interfaces:**
- Produces: `Chapter(start_s: float, title: str)`; `Segment(start_s: float, text: str)`; `YoutubeFacts(video_id, url, title, channel, subscribers, views, likes, upload_date: str | None, duration_s: int | None, description, chapters: list[Chapter], transcript: list[Segment] | None, fetched_at: str)` with `.transcript_text() -> str | None` (lines `[m:ss] text` or `[h:mm:ss] text`); `fmt_ts(seconds: float) -> str`; `FactsUnavailable(Exception)`; `FactsFetcher = Callable[[str], YoutubeFacts]`; `fetch_facts(video_id: str, *, languages: Sequence[str] = ("en",), today: date | None = None) -> YoutubeFacts`. The internal functions `_extract_info(url) -> dict` and `_fetch_transcript(video_id, languages) -> list[Segment] | None` are replaced in tests.
- The CLI command is `catcher youtube facts <url>`, which prints the facts JSON. Use it to record new fixtures.

- [ ] **Step 1: Write the fixture and the failing tests**

`tests/fixtures/youtube/MBPHU7aaklM.json`:

```json
{
  "video_id": "MBPHU7aaklM",
  "url": "https://www.youtube.com/watch?v=MBPHU7aaklM",
  "title": "Success Is Hard Until You Build Systems Like This",
  "channel": "Example Channel",
  "subscribers": 2260000,
  "views": 1400000,
  "likes": 46000,
  "upload_date": "2026-09-20",
  "duration_s": 1260,
  "description": "In this video I share the 4-part system I use every week.",
  "chapters": [{"start_s": 0, "title": "Intro"}, {"start_s": 95, "title": "Define productivity"}],
  "transcript": [
    {"start_s": 0, "text": "Productivity is hard until you build systems."},
    {"start_s": 95, "text": "First, define productivity by your own goals."},
    {"start_s": 410, "text": "I plan my week in Obsidian every Sunday."}
  ],
  "fetched_at": "2026-09-27"
}
```

`tests/component/test_facts.py`:

```python
from datetime import date

import pytest
from typer.testing import CliRunner

import catcher.cli as cli
from catcher.modules.youtube import facts as facts_mod
from catcher.modules.youtube.facts import FactsUnavailable, Segment, YoutubeFacts, fetch_facts, fmt_ts

INFO = {
    "title": "T",
    "channel": "C",
    "channel_follower_count": 2260000,
    "view_count": 1400000,
    "like_count": 46000,
    "upload_date": "20260920",
    "duration": 1260.0,
    "description": "D",
    "chapters": [{"start_time": 0.0, "end_time": 95.0, "title": "Intro"}],
}


def test_fetch_facts_maps_every_field(monkeypatch):
    monkeypatch.setattr(facts_mod, "_extract_info", lambda url: INFO)
    monkeypatch.setattr(
        facts_mod,
        "_fetch_transcript",
        lambda vid, langs: [Segment(start_s=0, text="hi"), Segment(start_s=3725, text="late")],
    )
    facts = fetch_facts("MBPHU7aaklM", today=date(2026, 9, 27))
    assert facts.url == "https://www.youtube.com/watch?v=MBPHU7aaklM"
    assert (facts.subscribers, facts.views, facts.likes) == (2260000, 1400000, 46000)
    assert (facts.upload_date, facts.duration_s, facts.fetched_at) == ("2026-09-20", 1260, "2026-09-27")
    assert facts.chapters[0].title == "Intro"
    assert facts.transcript_text() == "[0:00] hi\n[1:02:05] late"


def test_video_without_captions_has_no_transcript(monkeypatch):
    monkeypatch.setattr(facts_mod, "_extract_info", lambda url: INFO)
    monkeypatch.setattr(facts_mod, "_fetch_transcript", lambda vid, langs: None)
    facts = fetch_facts("MBPHU7aaklM")
    assert facts.transcript is None and facts.transcript_text() is None


def test_yt_dlp_failure_is_facts_unavailable(monkeypatch):
    def boom(url):
        raise RuntimeError("HTTP Error 429")

    monkeypatch.setattr(facts_mod, "_extract_info", boom)
    with pytest.raises(FactsUnavailable, match="yt-dlp"):
        fetch_facts("MBPHU7aaklM")


def test_blocked_transcript_is_facts_unavailable(monkeypatch):
    def blocked(vid, langs):
        raise RuntimeError("IpBlocked")

    monkeypatch.setattr(facts_mod, "_extract_info", lambda url: INFO)
    monkeypatch.setattr(facts_mod, "_fetch_transcript", blocked)
    with pytest.raises(FactsUnavailable, match="transcript"):
        fetch_facts("MBPHU7aaklM")


def test_fmt_ts():
    assert (fmt_ts(0), fmt_ts(410), fmt_ts(3725)) == ("0:00", "6:50", "1:02:05")


def test_cli_prints_facts_json(monkeypatch):
    fake = YoutubeFacts(video_id="MBPHU7aaklM", url="u", fetched_at="2026-09-27")
    monkeypatch.setattr(cli, "fetch_facts", lambda vid, languages: fake)
    result = CliRunner().invoke(cli.app, ["youtube", "facts", "https://youtu.be/MBPHU7aaklM"])
    assert result.exit_code == 0, result.output
    assert '"video_id": "MBPHU7aaklM"' in result.output
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/component/test_facts.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'catcher.modules.youtube.facts'`.

- [ ] **Step 3: Write the implementation**

`src/catcher/modules/youtube/facts.py`:

```python
from collections.abc import Callable, Sequence
from datetime import date
from typing import Any

from pydantic import BaseModel


class Chapter(BaseModel):
    start_s: float
    title: str


class Segment(BaseModel):
    start_s: float
    text: str


def fmt_ts(seconds: float) -> str:
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


class YoutubeFacts(BaseModel):
    video_id: str
    url: str
    title: str | None = None
    channel: str | None = None
    subscribers: int | None = None
    views: int | None = None
    likes: int | None = None
    upload_date: str | None = None
    duration_s: int | None = None
    description: str | None = None
    chapters: list[Chapter] = []
    transcript: list[Segment] | None = None
    fetched_at: str

    def transcript_text(self) -> str | None:
        if not self.transcript:
            return None
        return "\n".join(f"[{fmt_ts(s.start_s)}] {s.text}" for s in self.transcript)


class FactsUnavailable(Exception):
    pass


FactsFetcher = Callable[[str], YoutubeFacts]


def _extract_info(url: str) -> dict[str, Any]:
    import yt_dlp

    with yt_dlp.YoutubeDL({"skip_download": True, "quiet": True, "no_warnings": True}) as ydl:
        return ydl.sanitize_info(ydl.extract_info(url, download=False))


def _fetch_transcript(video_id: str, languages: Sequence[str]) -> list[Segment] | None:
    from youtube_transcript_api import NoTranscriptFound, TranscriptsDisabled, YouTubeTranscriptApi

    try:
        fetched = YouTubeTranscriptApi().fetch(video_id, languages=list(languages))
    except (TranscriptsDisabled, NoTranscriptFound):
        return None
    return [Segment(start_s=snippet.start, text=snippet.text) for snippet in fetched]


def fetch_facts(
    video_id: str, *, languages: Sequence[str] = ("en",), today: date | None = None
) -> YoutubeFacts:
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        info = _extract_info(url)
    except Exception as e:
        raise FactsUnavailable(f"yt-dlp failed for {video_id}: {e}") from e
    try:
        transcript = _fetch_transcript(video_id, languages)
    except Exception as e:
        raise FactsUnavailable(f"transcript fetch failed for {video_id}: {e}") from e
    raw_date = str(info.get("upload_date") or "")
    return YoutubeFacts(
        video_id=video_id,
        url=url,
        title=info.get("title"),
        channel=info.get("channel") or info.get("uploader"),
        subscribers=info.get("channel_follower_count"),
        views=info.get("view_count"),
        likes=info.get("like_count"),
        upload_date=f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}" if len(raw_date) == 8 else None,
        duration_s=int(info["duration"]) if info.get("duration") else None,
        description=info.get("description"),
        chapters=[
            Chapter(start_s=c.get("start_time") or 0, title=c.get("title") or "")
            for c in info.get("chapters") or []
        ],
        transcript=transcript,
        fetched_at=(today or date.today()).isoformat(),
    )
```

In `src/catcher/cli.py`, add these imports:

```python
from catcher.modules.youtube.facts import FactsUnavailable, fetch_facts
from catcher.modules.youtube.urls import video_id
```

and add the `youtube` group:

```python
youtube_app = typer.Typer(no_args_is_help=True, help="YouTube helpers.")
app.add_typer(youtube_app, name="youtube")


@youtube_app.callback()
def youtube_group() -> None:
    """YouTube helpers."""


@youtube_app.command("facts")
def youtube_facts(url: str) -> None:
    """Print the facts (counts, description, transcript) for one video as JSON."""
    vid = video_id(url) or url
    try:
        facts = fetch_facts(vid, languages=Settings().transcript_language_list)
    except FactsUnavailable as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2) from e
    typer.echo(facts.model_dump_json(indent=2))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/component/test_facts.py -q`
Expected: 6 passed.

- [ ] **Step 5: Check the real services once, from the home network**

Run: `uv run catcher youtube facts "https://www.youtube.com/watch?v=AeV5F0ppaGw" | head -30`
Expected: JSON with a title, channel, views, likes, subscribers and a non-empty transcript. If `yt-dlp` warns about a missing JavaScript runtime, run `brew install deno` and try again.

- [ ] **Step 6: Commit**

```bash
git add src/catcher/modules/youtube/facts.py src/catcher/cli.py tests/fixtures/youtube tests/component/test_facts.py
git commit -m "feat: fetch YouTube facts and transcripts in Python"
```

---

### Task 16: YouTube summaries in the pipeline (both classes, before the reviewer)

**Files:**
- Create: `src/catcher/modules/llm/prompts/youtube.md`, `src/catcher/modules/llm/prompts/youtube-from-gemini.md`, `src/catcher/modules/pipeline/templates/youtube.md.j2`
- Modify: `src/catcher/modules/pipeline/inputs.py`, `src/catcher/modules/pipeline/process.py`, `src/catcher/modules/pipeline/run.py`, `tests/conftest.py`
- Test: `tests/component/test_process_youtube.py`, and add one test to `tests/integration/git/test_run.py`

**Interfaces:**
- Consumes: `YoutubeFacts`, `FactsUnavailable`, `FactsFetcher`, `fetch_facts` (Task 15); `video_id` (Task 3); `gemini_video_id` (Task 3); `facts_sidecar` (Task 4); `find_pages_by_id` (Task 12).
- Produces: `Services.facts: FactsFetcher` (a new field, default `fetch_facts`); `default_services` sets it to the configured transcript languages. `prompt_input(note, tags, facts: YoutubeFacts | None = None)` adds `facts` (dict without the transcript) and `transcript` (str | None) for YouTube classes. Also produced: `YOUTUBE_CLASSES = ("youtube", "youtube-gemini")`; `youtube_video_id(note) -> str`; `facts_for(note, vid, svc, opts) -> tuple[YoutubeFacts, list[Path]]`; `sibling_link(note, vid, opts) -> dict[str, str | None]`; `youtube_embed(vid: str, title: str) -> str`. YouTube pages get `video_id` in their frontmatter. `run_pipeline` reports `FactsUnavailable` as `deferred`.
- Produces (conftest): `make_services(note_backend=None, chat_backend=None, facts: FactsFetcher | None = None)`. The default `facts` raises `FactsUnavailable`, so no test touches the network. A new fixture `yt_facts() -> YoutubeFacts` loads `tests/fixtures/youtube/MBPHU7aaklM.json`.

- [ ] **Step 1: Update the fixtures**

In `tests/conftest.py`, replace the `make_services` fixture with this version, and add `yt_facts`. Put the imports at the top of the file:

```python
from catcher.modules.youtube.facts import FactsFetcher, FactsUnavailable, YoutubeFacts

FIXTURES = Path(__file__).parent / "fixtures"


def _no_network(vid: str) -> YoutubeFacts:
    raise FactsUnavailable(f"tests must not fetch facts for {vid}")


@pytest.fixture
def make_services():
    def _make(
        note_backend: FakeBackend | None = None,
        chat_backend: FakeBackend | None = None,
        facts: FactsFetcher | None = None,
    ) -> Services:
        notes = note_backend or FakeBackend()
        chats = chat_backend or FakeBackend()
        return Services(
            settings=Settings(),
            profiles=_profiles_for_tests(),
            backends=lambda p: chats if p.backend == "claude-code" else notes,
            tags=load_tags(),
            facts=facts or _no_network,
        )

    return _make


@pytest.fixture
def yt_facts() -> YoutubeFacts:
    return YoutubeFacts.model_validate_json((FIXTURES / "youtube" / "MBPHU7aaklM.json").read_text())
```

- [ ] **Step 2: Write the failing tests**

`tests/component/test_process_youtube.py`:

```python
import json

import pytest

from catcher.core.frontmatter import Doc, dump, parse
from catcher.modules.llm.backends.fake import CANNED, FakeBackend
from catcher.modules.pipeline.doctypes import YOUTUBE
from catcher.modules.pipeline.process import ProcessOptions, process_note
from catcher.modules.pipeline.staging import facts_sidecar
from catcher.modules.youtube.facts import FactsUnavailable

GEMINI = "https://gemini.google.com/app/925d9b0b4ca21b63?is_sa=1"
YT_CHAT = (
    "**You**\n\nSummarize this YouTube video: https://www.youtube.com/watch?v=MBPHU7aaklM\n\n---\n\n"
    "**Gemini**\n\n**Success Is Hard** by *Someone*\n\n| Views | 99M |\n"
)
NO_REVIEW = ProcessOptions(review=False)


def youtube_note(make_note, tmp_path):
    return make_note(
        "youtube",
        doc_id="MBPHU7aaklM",
        root=tmp_path,
        source="https://www.youtube.com/watch?v=MBPHU7aaklM&list=PL1&t=1s",
        body="page scrape\n",
    )


def test_youtube_page_has_python_metrics_and_embed(make_note, make_services, yt_facts, tmp_path):
    chats = FakeBackend()
    note = youtube_note(make_note, tmp_path)
    processed = process_note(note, make_services(chat_backend=chats, facts=lambda vid: yt_facts), NO_REVIEW)
    assert processed.problems == []
    doc = parse(processed.page)
    assert doc.fm["video_id"] == "MBPHU7aaklM"
    assert doc.fm["source"] == "https://www.youtube.com/watch?v=MBPHU7aaklM"
    assert "| Views               | 1,400,000 |" in doc.body
    assert "| Channel Subscribers | 2,260,000 |" in doc.body
    assert "| Metrics As Of       | 2026-09-27 |" in doc.body
    assert "{{< youtube-lite MBPHU7aaklM `Success Is Hard Until You Build Systems Like This` >}}" in doc.body
    assert "[6:50] I plan my week in Obsidian every Sunday." in chats.prompts[0]


def test_facts_are_saved_next_to_the_note_and_reused(make_note, make_services, yt_facts, tmp_path):
    calls = []

    def fetch(vid):
        calls.append(vid)
        return yt_facts

    note = youtube_note(make_note, tmp_path)
    svc = make_services(facts=fetch)
    first = process_note(note, svc, NO_REVIEW)
    assert first.written == [facts_sidecar(note.path)]
    second = process_note(note, svc, NO_REVIEW)
    assert calls == ["MBPHU7aaklM"] and second.written == []


def test_dry_run_does_not_write_facts(make_note, make_services, yt_facts, tmp_path):
    note = youtube_note(make_note, tmp_path)
    process_note(note, make_services(facts=lambda vid: yt_facts), ProcessOptions(review=False, dry_run=True))
    assert not facts_sidecar(note.path).exists()


def test_no_transcript_is_said_on_the_page(make_note, make_services, yt_facts, tmp_path):
    no_captions = yt_facts.model_copy(update={"transcript": None})
    chats = FakeBackend()
    note = youtube_note(make_note, tmp_path)
    processed = process_note(
        note, make_services(chat_backend=chats, facts=lambda vid: no_captions), NO_REVIEW
    )
    assert "No transcript was available" in processed.page
    assert "There is NO transcript" in chats.prompts[0]


def test_facts_failure_propagates(make_note, make_services, tmp_path):
    with pytest.raises(FactsUnavailable):
        process_note(youtube_note(make_note, tmp_path), make_services(), NO_REVIEW)


def test_table_cells_are_escaped(make_note, make_services, yt_facts, tmp_path):
    tip = {"tip": "Use A | B", "explanation": "line one\nline two", "how_to_apply": "x"}
    reply = json.dumps({**CANNED["youtube"], "tips": [tip]})
    services = make_services(chat_backend=FakeBackend([reply]), facts=lambda vid: yt_facts)
    processed = process_note(youtube_note(make_note, tmp_path), services, NO_REVIEW)
    assert "| Use A \\| B | line one line two | x |" in processed.page


def test_gemini_youtube_chat_is_converted(make_note, make_services, yt_facts, tmp_path):
    chats = FakeBackend()
    note = make_note(
        "youtube-gemini", doc_id="MBPHU7aaklM-gemini", root=tmp_path, source=GEMINI, body=YT_CHAT
    )
    processed = process_note(note, make_services(chat_backend=chats, facts=lambda vid: yt_facts), NO_REVIEW)
    assert processed.problems == []
    assert "Gemini web chat" in chats.prompts[0]
    assert "| Views | 99M |" in chats.prompts[0]
    assert "| Views               | 1,400,000 |" in processed.page
    assert parse(processed.page).fm["video_id"] == "MBPHU7aaklM"


def test_sibling_page_is_linked(make_note, make_services, yt_facts, tmp_path):
    docs = tmp_path / "docs"
    other = docs / YOUTUBE.out_dir / "20260927_MBPHU7aaklM-gemini_success.md"
    other.parent.mkdir(parents=True)
    other.write_text(dump(Doc({"title": "x", "id": "MBPHU7aaklM-gemini"}, "x\n")))
    note = youtube_note(make_note, tmp_path)
    opts = ProcessOptions(review=False, docs_repo=docs)
    processed = process_note(note, make_services(facts=lambda vid: yt_facts), opts)
    assert "(../20260927_mbphu7aaklm-gemini_success/)" in processed.page
```

Append to `tests/integration/git/test_run.py`:

```python
YT_CLIP = '---\nsource : "https://www.youtube.com/watch?v=MBPHU7aaklM&list=PL1&t=1s"\ncreated: 2026-09-25\n---\nclip\n'


def test_youtube_without_facts_is_deferred_then_published(repos, make_services, yt_facts):
    (repos.ideas / "inbox/clippings/yt.md").write_text(YT_CLIP)
    first = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    assert {i.doc_id: i.status for i in first.items}["MBPHU7aaklM"] == "deferred"
    assert (repos.ideas / "staging/MBPHU7aaklM.md").exists()

    second = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services(facts=lambda vid: yt_facts))
    assert {i.doc_id: i.status for i in second.items}["MBPHU7aaklM"] == "published"
    assert (repos.ideas / "archive/youtube/MBPHU7aaklM.youtube.json").exists()
    assert list((repos.docs / "hugo/content/en/docs/idea-bucket/youtube").glob("20260925_MBPHU7aaklM_*.md"))
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/component/test_process_youtube.py -q`
Expected: FAIL. `Services` has no `facts` field, and the youtube prompt file does not exist yet.

- [ ] **Step 4: Write the prompts and the template**

`src/catcher/modules/llm/prompts/youtube.md`:

```markdown
---
version: youtube-1
---
You summarize a YouTube video for the Epiaku documentation site. You cannot watch the video: work only from the facts and the transcript below. Never invent views, likes, subscriber counts or dates, because code adds those.

Video facts:
- Title: {{ facts.title or "unknown" }}
- Channel: {{ facts.channel or "unknown" }}
- Published: {{ facts.upload_date or "unknown" }}
- Length in seconds: {{ facts.duration_s or "unknown" }}
{% if facts.chapters %}
- Chapters: {% for c in facts.chapters %}{{ c.title }}{% if not loop.last %}; {% endif %}{% endfor %}

{% endif %}

<description>
{{ facts.description or "" }}
</description>

{% if transcript %}
<transcript>
{{ transcript }}
</transcript>
{% else %}
There is NO transcript for this video. Summarize from the title and description only, and say so in the summary.
{% endif %}

Fill in:
- title: the video's title as it appears on YouTube.
- creator: the channel or presenter name.
- description: one sentence (max 160 characters) with the video's core idea, for someone deciding whether to read the page.
- summary: YouTube's own description condensed to 1 to 3 sentences.
- main_purpose: the video's core message in one short paragraph.
- key_examples: the concrete examples used in the video.
- action_plan: the recommended steps, one per item.
- tools: the tools, services, equipment, apps or platforms mentioned, one per item. Leave it empty if none are mentioned.
- tips: tips that are shared, each with a short explanation and how to apply it.
- channel_application: how to apply the lessons to Epiaku: the YouTube channel, Vibe Coding Tech Stack demos, and building phone apps or small SaaS applications.
- tags: exactly one idea-type tag, 1 to 4 topic tags and at most one project tag, chosen ONLY from the lists below.

When you mention a moment in the video, add its timestamp from the transcript (for example 12:40). Do not use Hugo shortcodes.

Idea-type tags: {{ tags.idea_types | join(", ") }}
Topic tags: {{ tags.topics | join(", ") }}
Project tags: {{ tags.projects | join(", ") }}
```

`src/catcher/modules/llm/prompts/youtube-from-gemini.md`:

```markdown
---
version: youtube-from-gemini-1
---
Below is a Gemini web chat in which someone asked Gemini to summarize a YouTube video. Convert Gemini's final answer into the structured fields below.

Keep Gemini's content, but correct anything the transcript clearly contradicts. Ignore Gemini's metrics table: code adds views, likes and subscriber counts.

<chat>
{{ body }}
</chat>

Video facts:
- Title: {{ facts.title or "unknown" }}
- Channel: {{ facts.channel or "unknown" }}
- Published: {{ facts.upload_date or "unknown" }}

{% if transcript %}
<transcript>
{{ transcript }}
</transcript>
{% else %}
There is NO transcript for this video, so keep Gemini's content as it is.
{% endif %}

Fill in:
- title: the video's title as it appears on YouTube.
- creator: the channel or presenter name.
- description: one sentence (max 160 characters) with the video's core idea.
- summary: 1 to 3 sentences.
- main_purpose: the video's core message in one short paragraph.
- key_examples: the concrete examples used in the video.
- action_plan: the recommended steps, one per item.
- tools: the tools, services, equipment, apps or platforms mentioned, one per item.
- tips: tips that are shared, each with a short explanation and how to apply it.
- channel_application: how to apply the lessons to Epiaku: the YouTube channel, Vibe Coding Tech Stack demos, and building phone apps or small SaaS applications.
- tags: exactly one idea-type tag, 1 to 4 topic tags and at most one project tag, chosen ONLY from the lists below.

Do not use Hugo shortcodes.

Idea-type tags: {{ tags.idea_types | join(", ") }}
Topic tags: {{ tags.topics | join(", ") }}
Project tags: {{ tags.projects | join(", ") }}
```

`src/catcher/modules/pipeline/templates/youtube.md.j2`:

```jinja
**{{ s.title }}** by _{{ s.creator }}_

{% if not facts.transcript %}
> ⚠️ No transcript was available for this video, so this summary is based on the title and description only.

{% endif %}
## 📝 Summary

{{ s.summary | trim }}

## 📊 Metrics

| Metric              | Value |
| ------------------- | ----- |
| Metrics As Of       | {{ facts.fetched_at }} |
| Views               | {{ facts.views | num }} |
| Likes               | {{ facts.likes | num }} |
| Channel Subscribers | {{ facts.subscribers | num }} |

## 🎯 Main Purpose

{{ s.main_purpose | trim }}

## 💡 Key Examples

{% for item in s.key_examples %}
- {{ item }}
{% else %}
Not available.
{% endfor %}

## ✅ Action Plan

{% for item in s.action_plan %}
- {{ item }}
{% else %}
Not available.
{% endfor %}

## 🛠️ Tools and Services

{% for item in s.tools %}
- {{ item }}
{% else %}
Not available.
{% endfor %}
{% if s.tips %}

## 💬 Tips

| Tip | Explanation | How to Apply |
| --- | ----------- | ------------ |
{% for t in s.tips %}
| {{ t.tip | md_cell }} | {{ t.explanation | md_cell }} | {{ t.how_to_apply | md_cell }} |
{% endfor %}
{% endif %}

## 📺 Channel Application

{{ s.channel_application | trim }}
{% if sibling %}

> 🔁 This video was also summarized {{ sibling_label }}: [compare the two summaries]({{ sibling }}).
{% endif %}

## 📄 YouTube Source

{{ embed }}
```

- [ ] **Step 5: Write the code changes**

Replace `src/catcher/modules/pipeline/inputs.py` with:

```python
from pathlib import Path
from typing import Any

from catcher.modules.pipeline.doctypes import canonical_source
from catcher.modules.pipeline.staging import StagedNote
from catcher.modules.pipeline.tags import TagList
from catcher.modules.youtube.facts import YoutubeFacts


def capture_tags(note: StagedNote) -> list[str]:
    raw = note.doc.fm.get("tags") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(t) for t in raw if str(t).strip().lower() != "clippings"]


def title_hint(note: StagedNote) -> str:
    title = note.doc.fm.get("title")
    if title:
        return str(title)
    return Path(str(note.doc.fm.get("source_file") or note.path.name)).stem


def prompt_input(note: StagedNote, tags: TagList, facts: YoutubeFacts | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "title_hint": title_hint(note),
        "body": note.doc.body,
        "source": canonical_source(note.doctype, note.doc.fm),
        "tags": tags.as_prompt_dict(),
        "capture_tags": capture_tags(note),
    }
    if facts is not None:
        data["facts"] = facts.model_dump(exclude={"transcript"})
        data["transcript"] = facts.transcript_text()
    return data
```

Replace `src/catcher/modules/pipeline/process.py` with:

```python
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from catcher.core.config import Settings
from catcher.modules.llm.backends import make_backend
from catcher.modules.llm.profiles import Profile, ProfilesConfig, load_profiles, resolve_profile
from catcher.modules.llm.schemas import Summary, YoutubeSummary
from catcher.modules.llm.service import BackendFactory, LlmRequest, LlmResult, UsageLimitReached, reason
from catcher.modules.pipeline.doctypes import gemini_video_id
from catcher.modules.pipeline.inputs import capture_tags, prompt_input
from catcher.modules.pipeline.publish import find_pages_by_id
from catcher.modules.pipeline.render import PageContext, page_name, render_page
from catcher.modules.pipeline.staging import StagedNote, facts_sidecar
from catcher.modules.pipeline.tags import TagList, load_tags, normalize_tags
from catcher.modules.pipeline.validate import validate_page
from catcher.modules.youtube.facts import FactsFetcher, FactsUnavailable, YoutubeFacts, fetch_facts
from catcher.modules.youtube.urls import video_id

YOUTUBE_CLASSES = ("youtube", "youtube-gemini")


@dataclass
class Services:
    settings: Settings
    profiles: ProfilesConfig
    backends: BackendFactory
    tags: TagList
    facts: FactsFetcher = fetch_facts


@dataclass
class ProcessOptions:
    profile: str | None = None
    review: bool = True
    review_profile: str | None = None
    dry_run: bool = False
    docs_repo: Path | None = None
    blocked_backends: frozenset[str] = frozenset()


@dataclass
class ProcessedPage:
    note: StagedNote
    filename: str
    page: str
    problems: list[str]
    llm: LlmResult
    dropped_tags: list[str]
    written: list[Path] = field(default_factory=list)


def default_services(settings: Settings) -> Services:
    languages = settings.transcript_language_list
    return Services(
        settings=settings,
        profiles=load_profiles(settings.profiles_file),
        backends=lambda p: make_backend(p, settings),
        tags=load_tags(),
        facts=lambda vid: fetch_facts(vid, languages=languages),
    )


def check_not_blocked(profile: Profile, opts: ProcessOptions) -> None:
    if profile.backend in opts.blocked_backends:
        raise UsageLimitReached("usage limit was reached earlier in this run", backend=profile.backend)


def youtube_video_id(note: StagedNote) -> str:
    if note.doctype.name == "youtube":
        vid = video_id(str(note.doc.fm.get("source") or ""))
    else:
        vid = gemini_video_id(note.doc.body)
    if not vid:
        raise FactsUnavailable(f"{note.doc_id}: no YouTube video id found")
    return vid


def facts_for(
    note: StagedNote, vid: str, svc: Services, opts: ProcessOptions
) -> tuple[YoutubeFacts, list[Path]]:
    sidecar = facts_sidecar(note.path)
    if sidecar.exists():
        return YoutubeFacts.model_validate_json(sidecar.read_text(encoding="utf-8")), []
    facts = svc.facts(vid)
    if opts.dry_run:
        return facts, []
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(facts.model_dump_json(indent=2), encoding="utf-8")
    return facts, [sidecar]


def sibling_link(note: StagedNote, vid: str, opts: ProcessOptions) -> dict[str, str | None]:
    none: dict[str, str | None] = {"sibling": None, "sibling_label": None}
    if opts.docs_repo is None:
        return none
    other = f"{vid}-gemini" if note.doctype.name == "youtube" else vid
    pages = find_pages_by_id(opts.docs_repo / note.doctype.out_dir, other)
    if not pages:
        return none
    label = "from a Gemini web chat" if note.doctype.name == "youtube" else "directly from the YouTube clip"
    return {"sibling": f"../{pages[0].stem.lower()}/", "sibling_label": label}


def youtube_embed(vid: str, title: str) -> str:
    return "{{< youtube-lite " + vid + " `" + title.replace("`", "'") + "` >}}"


def _reason(
    note: StagedNote, svc: Services, profile_name: str, facts: YoutubeFacts | None = None
) -> LlmResult:
    request = LlmRequest(
        task=note.doctype.task,
        input=prompt_input(note, svc.tags, facts),
        schema_name=note.doctype.schema_name,
        profile=profile_name,
    )
    return reason(request, profiles=svc.profiles, backends=svc.backends)


def _process_text(note: StagedNote, svc: Services, profile_name: str) -> ProcessedPage:
    result = _reason(note, svc, profile_name)
    summary = cast(Summary, result.output)
    tag_result = normalize_tags([*summary.tags, *capture_tags(note)], svc.tags)
    ctx = PageContext(note=note, summary=summary, tags=tag_result.tags, llm=result)
    page = render_page(ctx)
    return ProcessedPage(
        note, page_name(ctx), page, validate_page(page, svc.tags), result, tag_result.dropped
    )


def _process_youtube(
    note: StagedNote, svc: Services, opts: ProcessOptions, profile_name: str
) -> ProcessedPage:
    vid = youtube_video_id(note)
    facts, written = facts_for(note, vid, svc, opts)
    result = _reason(note, svc, profile_name, facts)
    summary = cast(YoutubeSummary, result.output)
    tag_result = normalize_tags([*summary.tags, *capture_tags(note)], svc.tags)
    ctx = PageContext(note=note, summary=summary, tags=tag_result.tags, llm=result)
    page = render_page(
        ctx,
        extra_fm={"video_id": vid},
        facts=facts,
        embed=youtube_embed(vid, facts.title or summary.title),
        **sibling_link(note, vid, opts),
    )
    problems = validate_page(page, svc.tags)
    return ProcessedPage(note, page_name(ctx), page, problems, result, tag_result.dropped, written)


def process_note(note: StagedNote, svc: Services, opts: ProcessOptions) -> ProcessedPage:
    profile_name, profile = resolve_profile(
        svc.profiles, requested=opts.profile, class_default=note.doctype.llm_profile
    )
    check_not_blocked(profile, opts)
    if note.doctype.name in YOUTUBE_CLASSES:
        return _process_youtube(note, svc, opts, profile_name)
    return _process_text(note, svc, profile_name)
```

In `src/catcher/modules/pipeline/run.py`, add this import:

```python
from catcher.modules.youtube.facts import FactsUnavailable
```

and add this handler after `except InvalidOutput as e:` and its two lines:

```python
        except FactsUnavailable as e:
            item.status, item.message = "deferred", str(e)
            continue
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests -q`
Expected: all pass. The note and chat snapshots from Task 10 are unchanged.

- [ ] **Step 7: Commit**

```bash
git add src/catcher/modules tests/conftest.py tests/component/test_process_youtube.py tests/integration/git/test_run.py
git commit -m "feat: summarize YouTube clips and Gemini video chats with Python-owned metrics"
```

---

### Task 17: The reviewer (A7, core)

**Files:**
- Create: `src/catcher/modules/youtube/review.py`, `src/catcher/modules/llm/prompts/review.md`
- Test: `tests/component/test_review.py`

**Interfaces:**
- Consumes: `reason`, `LlmRequest`, `LlmResult`, `BackendFactory` (Task 7); `Review`, `ReviewIssue`, `YoutubeSummary` (Task 6); `ProfilesConfig` (Task 6); `TagList` (Task 5); `YoutubeFacts`, `fmt_ts` (Task 15).
- Produces: `ReviewStatus = Literal["off", "ok", "fixed", "needs_attention", "limited"]`; the dataclass `ReviewOutcome(status, summary: YoutubeSummary, issues: list[ReviewIssue], llm: LlmResult | None, reviewed_at: str | None)` with `.frontmatter() -> dict` and `.high_issues() -> list[ReviewIssue]`; `not_reviewed(summary) -> ReviewOutcome`; `python_checks(summary, facts) -> list[ReviewIssue]`; `review_summary(summary_text: str, facts: YoutubeFacts, *, profile: str, profiles: ProfilesConfig, backends: BackendFactory, tags: TagList, now: datetime | None = None) -> ReviewOutcome`.

- [ ] **Step 1: Write the failing tests**

`tests/component/test_review.py`:

```python
import json
from datetime import UTC, datetime

from catcher.modules.llm.backends.fake import CANNED, FakeBackend
from catcher.modules.llm.schemas import YoutubeSummary
from catcher.modules.pipeline.tags import load_tags
from catcher.modules.youtube.review import not_reviewed, python_checks, review_summary

NOW = datetime(2026, 9, 27, 21, 0, tzinfo=UTC)
SUMMARY = YoutubeSummary.model_validate(CANNED["youtube"])


def run_review(fake, facts, fake_profiles, text="SUMMARY TEXT"):
    return review_summary(
        text,
        facts,
        profile="fake",
        profiles=fake_profiles,
        backends=lambda p: fake,
        tags=load_tags(),
        now=NOW,
    )


def test_clean_review_is_ok(fake_profiles, yt_facts):
    fake = FakeBackend()
    outcome = run_review(fake, yt_facts, fake_profiles)
    assert outcome.status == "ok"
    assert outcome.issues == []
    assert outcome.summary.title == "Fake Video Summary"
    assert outcome.reviewed_at == "2026-09-27T21:00:00+00:00"
    assert "SUMMARY TEXT" in fake.prompts[0]
    assert "[6:50] I plan my week in Obsidian every Sunday." in fake.prompts[0]


def test_needs_attention_is_kept_and_high_issues_listed(fake_profiles, yt_facts):
    issue = {
        "kind": "unsupported_claim",
        "severity": "high",
        "excerpt": "Uses Notion",
        "evidence": None,
        "fix": "remove",
    }
    reply = json.dumps({"verdict": "needs_attention", "issues": [issue], "revised": CANNED["youtube"]})
    outcome = run_review(FakeBackend([reply]), yt_facts, fake_profiles)
    assert outcome.status == "needs_attention"
    assert [i.excerpt for i in outcome.high_issues()] == ["Uses Notion"]


def test_without_transcript_the_review_is_limited(fake_profiles, yt_facts):
    outcome = run_review(FakeBackend(), yt_facts.model_copy(update={"transcript": None}), fake_profiles)
    assert outcome.status == "limited"


def test_python_flags_timestamps_after_the_end_of_the_video(yt_facts):
    summary = SUMMARY.model_copy(update={"action_plan": ["At 25:00 do the review"]})
    [issue] = python_checks(summary, yt_facts)
    assert issue.kind == "wrong_fact"
    assert "25:00" in issue.fix and "21:00" in issue.fix


def test_python_flags_tools_that_are_not_in_the_transcript(yt_facts):
    summary = SUMMARY.model_copy(update={"tools": ["Obsidian", "Notion"]})
    [issue] = python_checks(summary, yt_facts)
    assert (issue.kind, issue.severity, issue.excerpt) == ("unsupported_claim", "low", "Notion")


def test_python_checks_are_added_to_the_review(fake_profiles, yt_facts):
    reply = json.dumps(
        {"verdict": "fixed", "issues": [], "revised": {**CANNED["youtube"], "tools": ["Notion"]}}
    )
    outcome = run_review(FakeBackend([reply]), yt_facts, fake_profiles)
    assert outcome.status == "fixed"
    assert [i.excerpt for i in outcome.issues] == ["Notion"]


def test_frontmatter_is_compact(fake_profiles, yt_facts):
    issue = {"kind": "format", "severity": "low", "excerpt": "x" * 500, "evidence": None, "fix": "y"}
    reply = json.dumps({"verdict": "fixed", "issues": [issue], "revised": CANNED["youtube"]})
    fm = run_review(FakeBackend([reply]), yt_facts, fake_profiles).frontmatter()
    assert fm["status"] == "fixed" and fm["model"] == "fake"
    assert len(fm["issues"][0]["excerpt"]) == 200


def test_not_reviewed():
    outcome = not_reviewed(SUMMARY)
    assert (outcome.status, outcome.llm, outcome.frontmatter()["status"]) == ("off", None, "off")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/component/test_review.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'catcher.modules.youtube.review'`.

- [ ] **Step 3: Write the prompt and the implementation**

`src/catcher/modules/llm/prompts/review.md`:

```markdown
---
version: review-1
---
You are a strict fact-checker for YouTube video summaries. Compare the summary below with the video's transcript and facts, then return a corrected version.

The summary is either a JSON summary written by another model, or a Gemini web chat in which someone asked Gemini to summarize the video. In the second case, review Gemini's final answer.

<summary>
{{ summary }}
</summary>

Video facts:
- Title: {{ facts.title or "unknown" }}
- Channel: {{ facts.channel or "unknown" }}
- Published: {{ facts.upload_date or "unknown" }}
- Length in seconds: {{ facts.duration_s or "unknown" }}

<description>
{{ facts.description or "" }}
</description>

{% if transcript %}
<transcript>
{{ transcript }}
</transcript>
{% else %}
There is NO transcript for this video. You can only check the summary against the title and description, and its internal consistency.
{% endif %}

Rules:
- List every problem as an issue: a claim the video does not make (unsupported_claim), a wrong fact (wrong_fact), an important point that is missing (missing_point), invented or wrong numbers (wrong_metric), or a formatting problem (format).
- Every issue needs evidence: quote the transcript with its timestamp. If nothing in the transcript supports a claim, set evidence to null and use unsupported_claim.
- Use severity high only for problems that would mislead a reader.
- revised is the full corrected summary. Remove unsupported claims, fix wrong facts and add missing key points. Do not add anything the transcript does not support. Ignore views, likes and subscriber counts, because code adds them.
- verdict: ok if you changed nothing, fixed if you corrected the problems, needs_attention if problems remain that you could not fix from the transcript.
- revised.tags: exactly one idea-type tag, 1 to 4 topic tags and at most one project tag, chosen ONLY from the lists below.
- Do not use Hugo shortcodes.

Idea-type tags: {{ tags.idea_types | join(", ") }}
Topic tags: {{ tags.topics | join(", ") }}
Project tags: {{ tags.projects | join(", ") }}
```

`src/catcher/modules/youtube/review.py`:

```python
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast

from catcher.modules.llm.profiles import ProfilesConfig
from catcher.modules.llm.schemas import Review, ReviewIssue, YoutubeSummary
from catcher.modules.llm.service import BackendFactory, LlmRequest, LlmResult, reason
from catcher.modules.pipeline.tags import TagList
from catcher.modules.youtube.facts import YoutubeFacts, fmt_ts

ReviewStatus = Literal["off", "ok", "fixed", "needs_attention", "limited"]

_TIMESTAMP = re.compile(r"(?<![\d:])(\d{1,2}):([0-5]\d)(?::([0-5]\d))?(?![\d:])")
_PROPER_WORD = re.compile(r"\b[A-Z][A-Za-z0-9+#-]{2,}")
_ANY_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#-]{3,}")


@dataclass
class ReviewOutcome:
    status: ReviewStatus
    summary: YoutubeSummary
    issues: list[ReviewIssue]
    llm: LlmResult | None
    reviewed_at: str | None

    def high_issues(self) -> list[ReviewIssue]:
        return [issue for issue in self.issues if issue.severity == "high"]

    def frontmatter(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "model": self.llm.model if self.llm else None,
            "reviewed_at": self.reviewed_at,
            "issues": [
                {"kind": i.kind, "severity": i.severity, "excerpt": i.excerpt[:200], "fix": i.fix[:300]}
                for i in self.issues
            ],
        }


def not_reviewed(summary: YoutubeSummary) -> ReviewOutcome:
    return ReviewOutcome("off", summary, [], None, None)


def _seconds(match: re.Match[str]) -> int:
    first, second, third = match.groups()
    if third:
        return int(first) * 3600 + int(second) * 60 + int(third)
    return int(first) * 60 + int(second)


def python_checks(summary: YoutubeSummary, facts: YoutubeFacts) -> list[ReviewIssue]:
    issues: list[ReviewIssue] = []
    texts = [
        summary.summary,
        summary.main_purpose,
        summary.channel_application,
        *summary.key_examples,
        *summary.action_plan,
        *(f"{t.tip} {t.explanation} {t.how_to_apply}" for t in summary.tips),
    ]
    if facts.duration_s:
        for text in texts:
            for match in _TIMESTAMP.finditer(text):
                if _seconds(match) > facts.duration_s:
                    issues.append(
                        ReviewIssue(
                            kind="wrong_fact",
                            severity="medium",
                            excerpt=text[:200],
                            fix=f"timestamp {match.group(0)} is after the end of the video ({fmt_ts(facts.duration_s)})",
                        )
                    )
    if facts.transcript:
        source = " ".join(
            part for part in (facts.title, facts.description, facts.transcript_text()) if part
        ).lower()
        for tool in summary.tools:
            plain = tool.replace("*", "").replace("_", " ")
            words = _PROPER_WORD.findall(plain) or _ANY_WORD.findall(plain)
            if words and not any(word.lower() in source for word in words):
                issues.append(
                    ReviewIssue(
                        kind="unsupported_claim",
                        severity="low",
                        excerpt=tool[:200],
                        fix="this tool was not found in the transcript, title or description",
                    )
                )
    return issues


def review_summary(
    summary_text: str,
    facts: YoutubeFacts,
    *,
    profile: str,
    profiles: ProfilesConfig,
    backends: BackendFactory,
    tags: TagList,
    now: datetime | None = None,
) -> ReviewOutcome:
    request = LlmRequest(
        task="review",
        input={
            "summary": summary_text,
            "facts": facts.model_dump(exclude={"transcript"}),
            "transcript": facts.transcript_text(),
            "tags": tags.as_prompt_dict(),
        },
        schema_name="Review",
        profile=profile,
    )
    result = reason(request, profiles=profiles, backends=backends)
    review = cast(Review, result.output)
    issues = [*review.issues, *python_checks(review.revised, facts)]
    status: ReviewStatus
    if review.verdict == "needs_attention":
        status = "needs_attention"
    elif facts.transcript is None:
        status = "limited"
    else:
        status = review.verdict
    reviewed_at = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
    return ReviewOutcome(status, review.revised, issues, result, reviewed_at)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/component/test_review.py -q`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add src/catcher/modules/youtube/review.py src/catcher/modules/llm/prompts/review.md tests/component/test_review.py
git commit -m "feat: add the one-round YouTube reviewer with Python fact checks"
```

---

### Task 18: Wire the reviewer into both YouTube classes (A7)

**Files:**
- Modify: `src/catcher/modules/pipeline/process.py` (`ProcessedPage`, `_process_youtube`, plus the new `review_profile`), `src/catcher/modules/pipeline/templates/youtube.md.j2`
- Test: `tests/component/test_process_review.py`

**Interfaces:**
- Consumes: `review_summary`, `not_reviewed`, `ReviewOutcome` (Task 17); everything from Task 16.
- Produces: `ProcessedPage.review: ReviewOutcome | None = None`; `review_profile(svc, opts) -> tuple[str, Profile]` (`opts.review_profile` wins, otherwise `svc.profiles.review_profile`). YouTube pages get a `review` block in their frontmatter, plus a `needs_attention` alert at the top of the body. For `youtube-gemini` with review on, the reviewer's revised summary is the page, and there is no separate conversion call.

- [ ] **Step 1: Write the failing tests**

`tests/component/test_process_review.py`:

```python
import json

import pytest

from catcher.core.frontmatter import parse
from catcher.modules.llm.backends.fake import CANNED, FakeBackend
from catcher.modules.llm.service import UsageLimitReached
from catcher.modules.pipeline.process import ProcessOptions, process_note

GEMINI = "https://gemini.google.com/app/925d9b0b4ca21b63?is_sa=1"
YT_CHAT = (
    "**You**\n\nSummarize this YouTube video: https://www.youtube.com/watch?v=MBPHU7aaklM\n\n---\n\n"
    "**Gemini**\n\nGEMINI ANSWER\n"
)


def youtube_note(make_note, tmp_path):
    return make_note("youtube", doc_id="MBPHU7aaklM", root=tmp_path, source="https://youtu.be/MBPHU7aaklM")


def test_youtube_summary_is_reviewed_by_default(make_note, make_services, yt_facts, tmp_path):
    chats = FakeBackend()
    processed = process_note(
        youtube_note(make_note, tmp_path),
        make_services(chat_backend=chats, facts=lambda v: yt_facts),
        ProcessOptions(),
    )
    assert processed.problems == []
    assert len(chats.prompts) == 2
    assert "strict fact-checker" in chats.prompts[1]
    fm = parse(processed.page).fm
    assert fm["review"]["status"] == "ok"
    assert processed.review is not None and processed.review.status == "ok"


def test_review_can_be_switched_off(make_note, make_services, yt_facts, tmp_path):
    chats = FakeBackend()
    processed = process_note(
        youtube_note(make_note, tmp_path),
        make_services(chat_backend=chats, facts=lambda v: yt_facts),
        ProcessOptions(review=False),
    )
    assert len(chats.prompts) == 1
    assert parse(processed.page).fm["review"]["status"] == "off"


def test_needs_attention_shows_an_alert(make_note, make_services, yt_facts, tmp_path):
    issue = {
        "kind": "wrong_fact",
        "severity": "high",
        "excerpt": "Claims | 10 steps",
        "evidence": None,
        "fix": "It is 4",
    }
    review = json.dumps({"verdict": "needs_attention", "issues": [issue], "revised": CANNED["youtube"]})
    chats = FakeBackend([json.dumps(CANNED["youtube"]), review])
    processed = process_note(
        youtube_note(make_note, tmp_path),
        make_services(chat_backend=chats, facts=lambda v: yt_facts),
        ProcessOptions(),
    )
    assert processed.problems == []
    body = parse(processed.page).body
    assert body.startswith('{{% alert title="Review: needs attention" color="warning" %}}')
    assert "- **wrong_fact**: Claims \\| 10 steps → It is 4" in body
    assert "{{% /alert %}}" in body


def test_gemini_chat_is_reviewed_directly(make_note, make_services, yt_facts, tmp_path):
    chats = FakeBackend()
    note = make_note(
        "youtube-gemini", doc_id="MBPHU7aaklM-gemini", root=tmp_path, source=GEMINI, body=YT_CHAT
    )
    processed = process_note(
        note, make_services(chat_backend=chats, facts=lambda v: yt_facts), ProcessOptions()
    )
    assert len(chats.prompts) == 1
    assert "strict fact-checker" in chats.prompts[0]
    assert "GEMINI ANSWER" in chats.prompts[0]
    assert processed.llm.prompt_version == "review-1"
    assert parse(processed.page).fm["review"]["status"] == "ok"


def test_gemini_chat_without_review_is_converted(make_note, make_services, yt_facts, tmp_path):
    chats = FakeBackend()
    note = make_note(
        "youtube-gemini", doc_id="MBPHU7aaklM-gemini", root=tmp_path, source=GEMINI, body=YT_CHAT
    )
    processed = process_note(
        note, make_services(chat_backend=chats, facts=lambda v: yt_facts), ProcessOptions(review=False)
    )
    assert processed.llm.prompt_version == "youtube-from-gemini-1"


def test_blocked_review_backend_is_not_called(make_note, make_services, yt_facts, tmp_path):
    notes, chats = FakeBackend(), FakeBackend()
    services = make_services(note_backend=notes, chat_backend=chats, facts=lambda v: yt_facts)
    opts = ProcessOptions(profile="fake", blocked_backends=frozenset({"claude-code"}))
    with pytest.raises(UsageLimitReached):
        process_note(youtube_note(make_note, tmp_path), services, opts)
    assert notes.prompts == [] and chats.prompts == []


def test_review_profile_can_be_chosen(make_note, make_services, yt_facts, tmp_path):
    notes, chats = FakeBackend(), FakeBackend()
    services = make_services(note_backend=notes, chat_backend=chats, facts=lambda v: yt_facts)
    process_note(youtube_note(make_note, tmp_path), services, ProcessOptions(review_profile="fake"))
    assert len(chats.prompts) == 1 and len(notes.prompts) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/component/test_process_review.py -q`
Expected: FAIL, because the frontmatter has no `review` key yet.

- [ ] **Step 3: Write the changes**

In `src/catcher/modules/pipeline/process.py`:

Add these imports:

```python
from catcher.modules.youtube.review import ReviewOutcome, not_reviewed, review_summary
```

Add a field at the end of `ProcessedPage`:

```python
    review: ReviewOutcome | None = None
```

Add this function under `check_not_blocked`:

```python
def review_profile(svc: Services, opts: ProcessOptions) -> tuple[str, Profile]:
    return resolve_profile(
        svc.profiles, requested=opts.review_profile, class_default=svc.profiles.review_profile
    )
```

Replace `_process_youtube` with:

```python
def _process_youtube(
    note: StagedNote, svc: Services, opts: ProcessOptions, profile_name: str
) -> ProcessedPage:
    review_name: str | None = None
    if opts.review:
        review_name, review_prof = review_profile(svc, opts)
        check_not_blocked(review_prof, opts)
    vid = youtube_video_id(note)
    facts, written = facts_for(note, vid, svc, opts)

    def run_review(text: str) -> ReviewOutcome:
        assert review_name is not None
        return review_summary(
            text, facts, profile=review_name, profiles=svc.profiles, backends=svc.backends, tags=svc.tags
        )

    if note.doctype.name == "youtube-gemini" and review_name:
        outcome = run_review(note.doc.body)
        result = cast(LlmResult, outcome.llm)
    else:
        result = _reason(note, svc, profile_name, facts)
        first = cast(YoutubeSummary, result.output)
        outcome = run_review(first.model_dump_json(indent=2)) if review_name else not_reviewed(first)

    summary = outcome.summary
    tag_result = normalize_tags([*summary.tags, *capture_tags(note)], svc.tags)
    ctx = PageContext(note=note, summary=summary, tags=tag_result.tags, llm=result)
    page = render_page(
        ctx,
        extra_fm={"video_id": vid, "review": outcome.frontmatter()},
        facts=facts,
        review=outcome,
        embed=youtube_embed(vid, facts.title or summary.title),
        **sibling_link(note, vid, opts),
    )
    problems = validate_page(page, svc.tags)
    return ProcessedPage(note, page_name(ctx), page, problems, result, tag_result.dropped, written, outcome)
```

In `src/catcher/modules/pipeline/templates/youtube.md.j2`, insert this block at the very top of the file, before `**{{ s.title }}**`:

```jinja
{% if review.status == "needs_attention" %}
{% raw %}{{% alert title="Review: needs attention" color="warning" %}}{% endraw %}

The automatic review found problems it could not fix. Check this page before relying on it.

{% for issue in review.high_issues() %}
- **{{ issue.kind }}**: {{ issue.excerpt | md_cell }} → {{ issue.fix | md_cell }}
{% endfor %}

{% raw %}{{% /alert %}}{% endraw %}

{% endif %}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests -q`
Expected: all pass, including the Task 16 tests (they use `review=False`) and the Task 16 run test (with the default review, the fake reviewer answers `ok`).

- [ ] **Step 5: Commit**

```bash
git add src/catcher/modules/pipeline tests/component/test_process_review.py
git commit -m "feat: review YouTube summaries by default and flag pages that need attention"
```

---

### Task 19: Simplify the folders to inbox / archive / output / failed

> **Status: implemented (2026-09-28), not yet committed.** All 173 tests, ruff and pyright pass, and a run on a scratch repo produced `archive/`, `output/` and `failed/` as designed. The `[ ]` boxes below are the original steps.

> **Superseded in part by Task 23.** Task 19 built a separate `ingest` step that moved every inbox file to `output/` at the start and then re-read `output/` for work. Task 23 keeps the folders and the `stage` field, but a run **only looks at `inbox/`**, a document leaves it right before its own LLM step, and `output/` is never scanned for work. `ingest_inbox`, `load_pending` and `catcher ingest` are gone.

Tasks 1–18 are built on a `staging/` folder. This task replaces it with the four-folder layout from the design ([layout](../../idea-catcher-pipeline.md#repo-layout), [stage of a file](../../idea-catcher-pipeline.md#file-stage), [failures](../../idea-catcher-pipeline.md#failed-folder)). It is a **refactor of working code**: the LLM, rendering, tag, validation, Git and YouTube code does not change. Only the code that moves files, and the names around it, do.

**The target layout** (`<sub>` is the path under `inbox/`, in practice `notes/` or `clippings/`, and is copied unchanged):

```text
inbox/<sub>/<name>.md      ← Obsidian writes here
archive/<sub>/<name>.md    ← byte-for-byte copy of the inbox file, made first, never edited
output/<sub>/<name>.md     ← `stage: analyzed` while in progress; the final page when done (no `stage`)
output/<sub>/<name>.youtube.json   ← YouTube facts sidecar, stays next to the page
failed/<sub>/<name>.md     ← failed for good, with failed/<sub>/<name>.error.txt
duplicates/<sub>/<name>.md ← an earlier snapshot of a longer clip (added after this task), never processed
```

**Rules to implement:**

1. **Same name, same subfolder, everywhere.** No renaming by id. No flat folder. No `superseded/`. The `id` lives in the frontmatter.
2. **Ingest, per inbox file:** copy to `archive/` first (overwrite an existing copy). Then parse. If it cannot be parsed or read → move the **inbox file** to `failed/` with an `.error.txt`. Otherwise derive `id` and `class`, write `output/<sub>/<name>.md` with the frontmatter `id`, `class`, `captured`, `source_file`, `analyzed_at`, `stage: analyzed` (plus the original fields), and delete the inbox file. The body is written byte for byte, as before.
3. **Every inbox file is archived and ingested, but growing snapshots cost one LLM call.** The same filename again overwrites its `archive/` and `output/` copies. Among the waiting notes with one `id`, the longest goes to the LLM. A shorter one that `is_snapshot_of` the longest (all its messages identical, except that its last one may be cut off; text without messages must be an exact prefix) is moved with its sidecar from `output/` to `duplicates/<sub>/<same name>`, gets `duplicate_of: <rel path of the longest>` in its frontmatter (and no `stage`), and produces a log line and an `ItemReport` with status `duplicate`. It gets no LLM call, and its `archive/` copy stays. Clips of one `id` whose content differs are all processed, and the later page overwrites the earlier one (overwrite-by-id is unchanged). Implemented after Task 19, in `ingest.py` (`is_snapshot_of`, `move_to_duplicates`) and `run.py`.
4. **Which files are processed:** `output/` files with `stage: analyzed`. A file without `stage` is a final page and is never touched. A file with any other `stage` value (`reasoned`, reserved for Stage B) is skipped and logged.
5. **Publish:** `write_page()` to `epiaku-docs` is unchanged. Then the same page string is written over `output/<sub>/<name>.md`, so the `stage` field disappears. The sidecar stays.
6. **Failures** move the `output/` file (and its sidecar, if any) to `failed/<sub>/` and write `<name>.error.txt` (timestamp, doc id, class, the reason). They are: invalid LLM output after the one immediate retry, and a page that fails validation. **Deferrals are not failures:** usage limit, backend down, YouTube facts unavailable. The file stays in `output/` with `stage: analyzed` and the next run retries it.
7. **Retry by moving files.** A file moved from `failed/<sub>/` (or `archive/<sub>/`) back to `inbox/<sub>/` is a new capture. No code is needed beyond rule 2, but a test proves it.
8. **Logging is the state record in Stage A, and it is already in the code** (added before this task: `src/catcher/core/log.py`, `LOG_LEVEL` / `LOG_FILE`, `--log-level`). This task only has to keep it working: use the same logger names (`catcher.staging` becomes `catcher.ingest`, `catcher.run`, `catcher.process`, `catcher.git`), keep the `(i/N)` progress prefix and the final `processed X/N` line, and add a log line for each new file move: `archived`, `moved to failed/ (reason)`, `final page written`. Every `failed` and every `deferred` already logs at `ERROR` and `WARNING` with the reason; the move to `failed/` must log the same reason.

**Files:**
- Rename: `src/catcher/modules/pipeline/staging.py` → `ingest.py` (`stage_inbox` → `ingest_inbox`, `StagedNote` → `Note`, `StagingResult` → `IngestResult`, `load_staged` → `load_pending`, `load_staged_note` → `load_note`; `facts_sidecar` keeps its name)
- Modify: `src/catcher/modules/pipeline/doctypes.py` (remove `archive_dir` from `DocType` and from the four registry entries)
- Modify: `src/catcher/modules/pipeline/publish.py` (remove `archive_staged`; add `finalize_output`). `move_to_failed` lives in `ingest.py`, because ingest uses it too and `publish.py` would otherwise be imported by `ingest.py`.
- Modify: `src/catcher/modules/pipeline/process.py` (only the renamed imports and types; `facts_for` writes the sidecar next to the output note as before)
- Modify: `src/catcher/modules/pipeline/inputs.py`, `src/catcher/modules/pipeline/render.py` (renamed types only)
- Modify: `src/catcher/modules/pipeline/run.py` (the new flow, failures, report fields)
- Modify: `src/catcher/cli.py` (`stage` → `ingest`; `reason` and `render` take an output note)
- Modify: `tests/conftest.py` (`make_note` writes to `output/<sub>/` and returns a `Note`)
- Test: `tests/component/test_ingest.py` (replaces `test_staging.py`), `tests/component/test_publish.py`, `tests/integration/git/test_run.py`, `tests/unit/test_doctypes.py`, plus the renamed imports in the other tests

**Interfaces:**
- `Note(doc_id: str, doctype: DocType, doc: Doc, path: Path)` with the property `rel -> Path` (the path under `output/`, for example `clippings/New chat.md`).
- `IngestResult(notes: list[Note], touched: list[Path], errors: dict[str, str])`, where `errors` maps the inbox-relative path to the reason and those files are already in `failed/`.
- `ingest_inbox(ideas_repo: Path, *, dry_run: bool = False, now: datetime | None = None) -> IngestResult`
- `load_pending(ideas_repo: Path) -> list[Note]`: every `output/**/*.md` with `stage == "analyzed"`, sorted by path.
- `finalize_output(note: Note, page: str) -> list[Path]`: writes `page` over `note.path` and returns the touched paths.
- `move_to_failed(ideas_repo: Path, src: Path, reason: str, *, doc_id: str | None = None, doc_class: str | None = None, now: datetime | None = None) -> list[Path]`: moves `src` (and `facts_sidecar(src)` if it exists) to the same relative path under `failed/`, replaces an older failed copy, writes the `.error.txt`, and returns every touched path (sources and destinations).
- `RunReport.staging_errors` → `RunReport.ingest_errors`. `Status` is unchanged.

- [ ] **Step 1: Write the failing tests**

`tests/component/test_ingest.py` (helpers `put`, `gemini_clip`, `files`, `NOW` are copied from the old `test_staging.py`). These are the tests that must exist. Reuse the old bodies of `test_clip_body_is_kept_byte_for_byte`, `test_replay_from_the_archive_keeps_the_id`, `test_dry_run_changes_nothing` and `test_hidden_folders_are_ignored` with the new paths.

```python
def test_dictated_note_is_archived_analyzed_and_leaves_the_inbox(tmp_path):
    src = put(tmp_path, "inbox/notes/YouTube walks.md", "Create YouTube content walking around\n")
    original = src.read_text()
    result = ingest_inbox(tmp_path, now=NOW)
    [note] = result.notes
    out = tmp_path / "output/notes/YouTube walks.md"
    assert (tmp_path / "archive/notes/YouTube walks.md").read_text() == original
    assert not src.exists()
    assert note.path == out and note.rel == Path("notes/YouTube walks.md")
    staged = load(out)
    assert staged.body == original
    assert staged.fm == {
        "id": note.doc_id,
        "class": "note",
        "captured": "2026-09-27",
        "source_file": "inbox/notes/YouTube walks.md",
        "analyzed_at": "2026-09-27T18:00:00+00:00",
        "stage": "analyzed",
    }
    assert not (tmp_path / "staging").exists()


def test_the_archive_copy_is_untouched_even_when_the_output_is_enriched(tmp_path):
    text = gemini_clip("cf81e40b020519ef", "q\n")
    put(tmp_path, "inbox/clippings/chat.md", text)
    ingest_inbox(tmp_path, now=NOW)
    assert (tmp_path / "archive/clippings/chat.md").read_text() == text
    assert load(tmp_path / "output/clippings/chat.md").fm["id"] == "cf81e40b020519ef"


def test_every_file_is_processed_even_with_the_same_id(tmp_path):
    put(tmp_path, "inbox/clippings/New chat.md", gemini_clip("2446cd9c762c9cc9", "short\n"))
    put(
        tmp_path,
        "inbox/clippings/Idea catcher.md",
        gemini_clip("2446cd9c762c9cc9", "the longest version\n" * 5),
    )
    result = ingest_inbox(tmp_path, now=NOW)
    assert sorted(n.path.name for n in result.notes) == ["Idea catcher.md", "New chat.md"]
    assert {n.doc_id for n in result.notes} == {"2446cd9c762c9cc9"}
    assert not (tmp_path / "archive/clippings/superseded").exists()


def test_the_same_filename_again_overwrites_the_archive_and_output_copies(tmp_path):
    put(
        tmp_path,
        "output/clippings/again.md",
        "---\nid: cf81e40b020519ef\nclass: ai-chat\nstage: analyzed\n---\nold\n",
    )
    put(tmp_path, "inbox/clippings/again.md", gemini_clip("cf81e40b020519ef", "newer\n"))
    ingest_inbox(tmp_path, now=NOW)
    assert load(tmp_path / "output/clippings/again.md").body == "newer\n"
    assert load(tmp_path / "archive/clippings/again.md").body == "newer\n"


def test_unreadable_capture_is_archived_and_moved_to_failed(tmp_path):
    bad = put(tmp_path, "inbox/notes/bad.md", "---\ntitle: [oops\n---\nbody\n")
    put(tmp_path, "inbox/notes/good.md", "A good idea\n")
    result = ingest_inbox(tmp_path, now=NOW)
    assert not bad.exists()
    assert (tmp_path / "archive/notes/bad.md").exists()
    assert (tmp_path / "failed/notes/bad.md").read_text() == "---\ntitle: [oops\n---\nbody\n"
    assert "bad.md" in (tmp_path / "failed/notes/bad.error.txt").read_text()
    assert list(result.errors) == ["inbox/notes/bad.md"]
    assert len(result.notes) == 1


def test_a_file_moved_back_from_failed_is_a_new_capture(tmp_path):
    put(tmp_path, "inbox/notes/retry.md", "An idea\n")
    [note] = ingest_inbox(tmp_path, now=NOW).notes
    move_to_failed(tmp_path, note.path, "invalid output", doc_id=note.doc_id, doc_class="note", now=NOW)
    (tmp_path / "failed/notes/retry.error.txt").unlink()
    shutil.move(tmp_path / "failed/notes/retry.md", tmp_path / "inbox/notes/retry.md")
    [again] = ingest_inbox(tmp_path, now=NOW).notes
    assert again.path == tmp_path / "output/notes/retry.md"


def test_load_pending_returns_only_analyzed_files(tmp_path):
    put(tmp_path, "output/notes/waiting.md", "---\nid: a1\nclass: note\nstage: analyzed\n---\nx\n")
    put(tmp_path, "output/notes/final.md", "---\ntitle: T\nid: a2\ntype: docs\n---\nx\n")
    put(tmp_path, "output/notes/later.md", "---\nid: a3\nclass: note\nstage: reasoned\n---\nx\n")
    assert [n.doc_id for n in load_pending(tmp_path)] == ["a1"]


def test_the_facts_sidecar_sits_next_to_the_output_note(tmp_path):
    path = tmp_path / "output/clippings/A video.md"
    assert facts_sidecar(path) == tmp_path / "output/clippings/A video.youtube.json"


def test_ingest_command(tmp_path):
    put(tmp_path, "inbox/notes/idea.md", "An idea\n")
    result = CliRunner().invoke(app, ["ingest", "--ideas", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert len(list((tmp_path / "output/notes").glob("*.md"))) == 1
```

`tests/component/test_publish.py` (keep the `write_page` and `find_pages_by_id` tests, replace the two `archive_staged` tests, and update the `render` command test to an output note):

```python
def test_finalize_output_replaces_the_note_with_the_page_and_keeps_the_sidecar(tmp_path, make_note):
    note = make_note("youtube", doc_id="MBPHU7aaklM", root=tmp_path)
    sidecar = put(facts_sidecar(note.path), "{}")
    touched = finalize_output(note, "---\ntitle: T\nid: MBPHU7aaklM\n---\nbody\n")
    assert load(note.path).fm == {"title": "T", "id": "MBPHU7aaklM"}
    assert "stage" not in load(note.path).fm
    assert sidecar.exists()
    assert touched == [note.path]


def test_move_to_failed_moves_the_note_its_sidecar_and_writes_the_reason(tmp_path, make_note):
    note = make_note("youtube", doc_id="MBPHU7aaklM", root=tmp_path)
    put(facts_sidecar(note.path), "{}")
    touched = move_to_failed(
        tmp_path, note.path, "unknown shortcode", doc_id=note.doc_id, doc_class="youtube", now=NOW
    )
    dest = tmp_path / "failed" / note.rel
    assert dest.exists() and facts_sidecar(dest).exists() and not note.path.exists()
    error = dest.with_suffix(".error.txt").read_text()
    assert "unknown shortcode" in error and "MBPHU7aaklM" in error
    assert note.path in touched and dest in touched


def test_move_to_failed_replaces_an_older_failed_copy(tmp_path, make_note):
    note = make_note(root=tmp_path)
    put(tmp_path / "failed" / note.rel, "old")
    move_to_failed(tmp_path, note.path, "second failure", now=NOW)
    assert "old" not in (tmp_path / "failed" / note.rel).read_text()
```

`tests/integration/git/test_run.py`: update every path from `staging/<id>.md` to `output/<sub>/<name>.md` and add:

```python
def test_a_finished_run_leaves_archive_original_and_page_in_output(repos, make_services, sh):
    # one note and one chat in the inbox, fake LLM
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(profile="fake"), make_services())
    assert report.counts() == {"published": 2}
    assert not list((repos.ideas / "inbox").rglob("*.md"))
    assert (repos.ideas / "archive/notes/idea.md").read_text() == ORIGINAL_NOTE
    final = load(repos.ideas / "output/notes/idea.md")
    assert "stage" not in final.fm and final.fm["id"]
    assert (repos.docs / DOC_PAGE_DIR / page_name_of(final)).read_text() == (
        repos.ideas / "output/notes/idea.md"
    ).read_text()
    assert not (repos.ideas / "staging").exists()


def test_invalid_output_moves_the_note_to_failed_with_the_reason(repos, make_services):
    # fake backend returns invalid JSON twice
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(profile="fake"), make_services())
    assert report.counts() == {"failed": 1}
    assert (repos.ideas / "failed/clippings/chat.md").exists()
    assert "invalid" in (repos.ideas / "failed/clippings/chat.error.txt").read_text().lower()
    assert not (repos.ideas / "output/clippings/chat.md").exists()
    assert (repos.ideas / "archive/clippings/chat.md").exists()


def test_a_usage_limit_leaves_the_note_analyzed_in_output_and_the_next_run_retries_it(repos, make_services):
    first = run_pipeline(repos.ideas, repos.docs, RunOptions(profile="limited"), make_services(limit=True))
    assert first.counts() == {"deferred": 1}
    assert load(repos.ideas / "output/clippings/chat.md").fm["stage"] == "analyzed"
    assert not (repos.ideas / "failed").exists()
    second = run_pipeline(repos.ideas, repos.docs, RunOptions(profile="fake"), make_services())
    assert second.counts() == {"published": 1}


def test_a_final_page_in_output_is_not_processed_again(repos, make_services):
    run_pipeline(repos.ideas, repos.docs, RunOptions(profile="fake"), make_services())
    again = run_pipeline(repos.ideas, repos.docs, RunOptions(profile="fake"), make_services())
    assert again.items == []


def test_the_moves_are_logged_with_the_file_and_the_reason(repos, make_services, caplog):
    caplog.set_level("INFO", logger="catcher")
    run_pipeline(repos.ideas, repos.docs, RunOptions(profile="fake"), make_services())
    text = caplog.text
    assert "archived" in text and "notes/idea.md" in text and "final page written" in text
```

The existing logging tests (`test_run_logs_progress_per_note_and_a_total`, `test_failures_and_deferrals_are_always_logged`, `tests/unit/test_log.py`) must keep passing after the rename; only their expected message text changes (`staged` becomes `ingested`).

`tests/unit/test_doctypes.py`: delete the assertions on `archive_dir`, and assert that `DocType` has no such field.

Run: `uv run pytest -q`
Expected: FAIL. The new imports (`ingest`, `finalize_output`, `move_to_failed`, `configure_logging`) do not exist yet.

- [ ] **Step 2: Write the implementation**

Work in this order, running the affected tests after each item.

1. `git mv src/catcher/modules/pipeline/staging.py src/catcher/modules/pipeline/ingest.py` and `git mv tests/component/test_staging.py tests/component/test_ingest.py`. Apply the renames from the **Files** list with an editor search-and-replace, then run `uv run pytest -q` to see the failures move from `ImportError` to behaviour.
2. `ingest.py`: rewrite `ingest_inbox`. For each `inbox/**/*.md` (skip hidden folders as now): `shutil.copy2` to `archive/<rel>`; `load()`; on `FrontmatterError` / `UnicodeDecodeError` call `move_to_failed` on the **inbox file**, record the error, continue. Otherwise `detect`, `derive_id` (or `new_id()`), build the frontmatter as in rule 2, write `output/<rel>`, delete the inbox file. Remove the grouping by id, `winner`, `superseded_dir` and `stamp`. `dry_run` still changes nothing and returns what would be ingested. Add `load_pending`.
3. `publish.py`: delete `archive_staged`. Add `finalize_output` and `move_to_failed` as specified. `move_to_failed` uses `shutil.move` and writes the `.error.txt` with `path.with_suffix(".error.txt")`.
4. `doctypes.py`: remove `archive_dir`.
5. `run.py`: `run_pipeline` now (a) `ingest_inbox`; (b) `load_pending` merged with the notes just ingested (same `path` = same note, so no duplicates); (c) for each note as today; on `InvalidOutput` or `processed.problems` → `move_to_failed(...)` and `status="failed"`; on `UsageLimitReached`, `BackendUnavailable`, `FactsUnavailable` → `deferred` and nothing moves; on success `write_page` then `finalize_output` instead of `archive_staged`; (d) one `logging` line per step as in rule 8; (e) the ideas commit message becomes `idea-catcher: ingest and publish captures (N published)`. The `--dry-run` path writes no files and no log lines beyond `INFO would publish …`.
6. `config.py`, `logging.py`, `.env.example`, `cli.py`: add the settings and `configure_logging`, call it once from a Typer callback, rename the commands, and update the help texts (`ingest`: "Copy inbox captures to archive/ and write the analyzed note to output/"; `reason` and `render`: "Run … on one output note").
7. `tests/conftest.py`: `make_note` writes `output/<sub>/<name>.md` (default `notes/<doc_id>.md`) with `stage: analyzed` and returns a `Note`.
8. `grep -rn "staging\|staged\|superseded\|archive_staged\|archive_dir" src tests` must return nothing except the word "staged" inside `git` test names about the git index (`staged-by-user.md`).

- [ ] **Step 3: Run the whole suite and the linters**

Run: `uv run ruff check && uv run ruff format --check && uv run pyright && uv run pytest -q`
Expected: no lint or type errors, and all tests pass.

- [ ] **Step 4: Prove it on a copy with the fake profile**

```bash
rm -rf tmp/ic-test && mkdir -p tmp/ic-test
git clone --no-hardlinks ~/Documents/dev/epiaku/idea-bucket tmp/ic-test/idea-bucket
git clone --no-hardlinks ~/Documents/dev/epiaku/epiaku-docs tmp/ic-test/epiaku-docs
git -C tmp/ic-test/idea-bucket remote remove origin && git -C tmp/ic-test/epiaku-docs remote remove origin
uv run catcher run pipeline --ideas tmp/ic-test/idea-bucket --docs tmp/ic-test/epiaku-docs --profile fake --no-review
```

Expected: `inbox/` is empty; `archive/`, `output/` and (only if something failed) `failed/` mirror the old inbox names; every `output/` file is a final page without `stage`; the log shows one line per file and step, with `(i/N)` progress and a final `processed X/N` line. The 5 clips of `2446cd9c762c9cc9` are all present in `archive/` and `output/`, and the docs repo has **one** page for that id.

- [ ] **Step 5: Commit**

```bash
git add -A src tests .env.example
git commit -m "refactor: replace staging/ with inbox, archive, output and failed folders and log every step"
```

---

### Task 20: Replace `claude -p` with API-key backends and three profiles (`notes`, `clippings`, `youtube`)

> **Status: implemented (2026-09-28), not yet committed.** 180 tests, ruff and pyright pass, and no test calls a real LLM. The manual check in Step 4 is for you to run. Two details differ from the text below: a missing model for an `openai` profile is rejected when the profile is **resolved** (`resolve_profile`), not when `profiles.yaml` is loaded, so `--profile notes` still works without the OpenAI variables; and a configuration error (missing model) **defers** the note and logs an `ERROR`, it never moves it to `failed/`.

Decision (2026-09-28): **every LLM call is an API call with an API key.** `claude -p` was unreliable and is removed, together with the subscription token, the `when` / evening option and the Claude Code CLI. The design is in the pipeline page ([LLM options](../../idea-catcher-pipeline.md#llm-options), [backends](../../idea-catcher-pipeline.md#llm-backends)) and the architecture page ([LLM step & profiles](../../idea-catcher-service-architecture.md#mvp-llm)). Tasks 1, 6, 7, 8, 9, 12, 14, 16 and 18 built the `claude-code` backend and the old profile names; this task removes them. Do it **after Task 19** (folders), because both touch `run.py` and the test helpers. It is a refactor plus one new backend, and the reasoning, rendering, reviewer, tag and Git code does not change.

**Rules to implement:**

1. **Three profiles, one per kind of capture**, in `profiles.yaml`:

   ```yaml
   default: notes
   review_profile: youtube
   retry_delay: 60m            # used from stage B
   stuck_after_days: 3         # used from stage B
   profiles:
     notes:     { backend: freellmapi, model: "${FREELLMAPI_MODEL:-auto}" }
     clippings: { backend: openai,     model: "${OPENAI_MODEL_CLIPPINGS}" }
     youtube:   { backend: openai,     model: "${OPENAI_MODEL_YOUTUBE}" }
     fake:      { backend: fake }
   ```

2. **Document class → default profile:** `note` → `notes`, `ai-chat` → `clippings`, `youtube` and `youtube-gemini` → `youtube`. The reviewer uses `review_profile` (`youtube`) unless `--review-profile` says otherwise. `--profile <name>` still overrides the class default for a whole run.
3. **Backends:** `freellmapi`, `openai`, `fake`. `freellmapi` and `openai` are **one OpenAI-compatible client class** with a different name, base URL, API key and, for `openai`, JSON mode (`response_format={"type": "json_object"}`). Native schema-enforced outputs are **not** used in this stage (strict JSON schemas need extra constraints), so the reply is still validated with Pydantic and retried once, exactly as today.
4. **No `when`, no evening window, no `claude-code`.** `Profile` has only `backend` and `model`, and rejects unknown fields (`extra="forbid"`), so an old `profiles.yaml` with `when:` fails loudly instead of being ignored. `ProfilesConfig` loses `evening_window`. A profile with `backend: openai` and an empty `model` is rejected when it is resolved (not at load, so other profiles still work), with a message that says to set its model variable in `.env`.
5. **Error mapping** (all in the one client class, and each logs at `WARNING` or `ERROR` with the reason and never logs the key):
   - HTTP `429` with `rate_limit_exceeded` (a temporary rate limit) → `UsageLimitReached(backend=<name>)`. `run_pipeline` then stops calling that backend for the rest of the run (existing behaviour) and defers its notes; other backends carry on.
   - **Budget reached:** HTTP `429` (or `402`) with the error code `insufficient_quota`, or a message that mentions the quota, billing, credits or budget → `BudgetExhausted(UsageLimitReached)`. The API keys are capped at a budget, so this is a normal condition, not a fault. It blocks the backend for the rest of the run like a rate limit does, but it is reported separately: the run logs **one `ERROR` per backend**, not one per note, with the number of waiting notes, for example `openai budget reached: 4 note(s) waiting; raise the key's budget or point the profile at another provider`. The waiting notes stay in `output/` with `stage: analyzed`, are never moved to `failed/`, and go through on a later run once calls work again. (The longer `budget_retry_delay` is used from Stage B; in Stage A the next run simply tries again.) Check the exact error code the OpenAI API returns for a spend limit on a test key, and adjust the classifier if it differs.
   - HTTP `401` or `403` (bad or revoked key) → `UsageLimitReached` with the message `authentication failed: check OPENAI_API_KEY`, so a bad key stops that backend at once instead of failing every note one by one, and the run logs one clear `ERROR`.
   - Timeout, connection error, HTTP `5xx`, HTTP `413` → `BackendUnavailable` (deferred, retried on the next run).
   - No `OPENAI_API_KEY` set → `make_backend` raises `BackendUnavailable("OPENAI_API_KEY is not set")` before any call.
6. **Settings and `.env.example`:** remove `claude_bin` and `CLAUDE_BIN`, `CLAUDE_CODE_OAUTH_TOKEN`. Add `openai_api_key: str = ""`, `openai_base_url: str = "https://api.openai.com/v1"`, and `.env.example` lines `OPENAI_API_KEY=`, `OPENAI_BASE_URL=https://api.openai.com/v1`, `OPENAI_MODEL_CLIPPINGS=gpt-6-sol`, `OPENAI_MODEL_YOUTUBE=gpt-6-sol` (check the exact model id in the OpenAI docs before the first run). `FREELLMAPI_*` and `LLM_TIMEOUT_S` stay.
7. **Nothing else may need a Claude login.** `claude` is not called by any code or test, and the image and the setup steps no longer install it.

**Files:**
- Rename: `src/catcher/modules/llm/backends/freellmapi.py` → `openai_compatible.py`. The class becomes `OpenAiCompatibleBackend(name: str, base_url: str, api_key: str, *, json_mode: bool = False, timeout_s: int = 120)`.
- Delete: `src/catcher/modules/llm/backends/claude_code.py`, `tests/bin/claude`, `tests/component/test_claude_code.py`
- Modify: `src/catcher/modules/llm/backends/__init__.py` (`make_backend`: `fake`, `freellmapi`, `openai`)
- Modify: `src/catcher/modules/llm/service.py` (add `class BudgetExhausted(UsageLimitReached)`)
- Modify: `src/catcher/modules/pipeline/run.py` (catch `BudgetExhausted`, one `ERROR` per backend)
- Modify: `src/catcher/modules/llm/profiles.py` (`BackendName = Literal["freellmapi", "openai", "fake"]`; `Profile(backend, model)` with `extra="forbid"`, and the empty-model check in `resolve_profile`; `ProfilesConfig.review_profile: str = "youtube"`; no `when`)
- Modify: `profiles.yaml`, `.env.example`, `src/catcher/core/config.py`
- Modify: `src/catcher/modules/pipeline/doctypes.py` (`llm_profile`: `notes` / `clippings` / `youtube` / `youtube`)
- Modify: `src/catcher/cli.py` (no `claude` mention in help texts; `--profile` help lists the three profile names)
- Modify: `tests/conftest.py` (`_profiles_for_tests`: `notes` = `fake`, `clippings` and `youtube` = `Profile(backend="openai", model="gpt-test")`, `review_profile="youtube"`; the backend factory becomes `lambda p: chats if p.backend == "openai" else notes`)
- Test: `tests/component/test_openai_compatible.py` (replaces `test_freellmapi.py` and `test_claude_code.py`), `tests/unit/test_profiles.py`, `tests/unit/test_doctypes.py`, `tests/unit/test_config.py`, and the renamed profile and backend names in `test_reason.py`, `test_process*.py`, `test_cli_reason.py` and `tests/integration/git/test_run.py`

**Interfaces:**
- `make_backend(profile: Profile, settings: Settings) -> Backend` (unchanged signature).
- `OpenAiCompatibleBackend.name` is `"freellmapi"` or `"openai"`, and `complete(prompt, *, model, task) -> BackendReply` is unchanged.
- `UsageLimitReached(message, backend)` and `BackendUnavailable` keep their names. The new `BudgetExhausted(UsageLimitReached)` is a subclass, so the existing blocking in `process.py` and `run.py` covers it, and `run.py` only adds the per-backend budget line. `ItemReport.message` for such a note is `budget reached (<backend>)`.

- [ ] **Step 1: Write the failing tests**

`tests/component/test_openai_compatible.py` (start from the old `test_freellmapi.py`, then add):

```python
import json

import httpx
import pytest
import respx

from catcher.core.config import Settings
from catcher.modules.llm.backends import make_backend
from catcher.modules.llm.backends.openai_compatible import OpenAiCompatibleBackend
from catcher.modules.llm.profiles import Profile
from catcher.modules.llm.service import BackendUnavailable, BudgetExhausted, UsageLimitReached

BASE = "https://api.openai.test/v1"


def completion(content: str) -> dict:
    return {
        "id": "x",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-test-2026",
        "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
    }


@respx.mock
def test_openai_sends_the_key_the_model_and_json_mode():
    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=completion('{"a": 1}'))
    )
    reply = OpenAiCompatibleBackend("openai", BASE, "sk-test", json_mode=True).complete(
        "PROMPT", model="gpt-test", task="ai-chat"
    )
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer sk-test"
    body = json.loads(request.content)
    assert body["model"] == "gpt-test" and body["response_format"] == {"type": "json_object"}
    assert (reply.text, reply.usage.tokens_in, reply.usage.tokens_out, reply.model) == (
        '{"a": 1}',
        12,
        3,
        "gpt-test-2026",
    )


@respx.mock
def test_freellmapi_does_not_send_json_mode():
    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=completion("{}"))
    )
    OpenAiCompatibleBackend("freellmapi", BASE, "k").complete("p", model="auto", task="note")
    assert "response_format" not in json.loads(route.calls[0].request.content)


@respx.mock
@pytest.mark.parametrize("code", ["rate_limit_exceeded", "requests"])
def test_a_429_blocks_the_backend_for_the_run(code):
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(429, json={"error": {"message": "slow down", "code": code}})
    )
    with pytest.raises(UsageLimitReached) as info:
        OpenAiCompatibleBackend("openai", BASE, "k").complete("p", model="m", task="ai-chat")
    assert info.value.backend == "openai"


@respx.mock
@pytest.mark.parametrize(
    ("status", "body"),
    [
        (429, {"error": {"message": "You exceeded your current quota", "code": "insufficient_quota"}}),
        (429, {"error": {"message": "Project budget limit reached", "code": "billing_hard_limit_reached"}}),
        (402, {"error": {"message": "Insufficient credits", "code": "insufficient_credits"}}),
    ],
)
def test_a_used_up_budget_is_reported_as_its_own_condition(status, body):
    respx.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(status, json=body))
    with pytest.raises(BudgetExhausted) as info:
        OpenAiCompatibleBackend("openai", BASE, "k").complete("p", model="m", task="ai-chat")
    assert info.value.backend == "openai" and isinstance(info.value, UsageLimitReached)


@respx.mock
def test_a_plain_rate_limit_is_not_a_budget_problem():
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(
            429, json={"error": {"message": "slow down", "code": "rate_limit_exceeded"}}
        )
    )
    with pytest.raises(UsageLimitReached) as info:
        OpenAiCompatibleBackend("openai", BASE, "k").complete("p", model="m", task="ai-chat")
    assert not isinstance(info.value, BudgetExhausted)


@respx.mock
@pytest.mark.parametrize("status", [401, 403])
def test_a_bad_key_blocks_the_backend_with_a_clear_message(status):
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(status, json={"error": {"message": "bad key"}})
    )
    with pytest.raises(UsageLimitReached, match="authentication failed: check OPENAI_API_KEY"):
        OpenAiCompatibleBackend("openai", BASE, "k").complete("p", model="m", task="ai-chat")


@respx.mock
@pytest.mark.parametrize("status", [500, 502, 413])
def test_server_errors_are_backend_unavailable_and_not_blocking(status):
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(status, json={"error": {"message": "x"}})
    )
    with pytest.raises(BackendUnavailable) as info:
        OpenAiCompatibleBackend("openai", BASE, "k").complete("p", model="m", task="ai-chat")
    assert not isinstance(info.value, UsageLimitReached)


@respx.mock
def test_a_connection_error_is_backend_unavailable():
    respx.post(f"{BASE}/chat/completions").mock(side_effect=httpx.ConnectError("no route"))
    with pytest.raises(BackendUnavailable, match="unreachable"):
        OpenAiCompatibleBackend("openai", BASE, "k").complete("p", model="m", task="ai-chat")


def test_the_key_is_never_in_an_error_message():
    with respx.mock:
        respx.post(f"{BASE}/chat/completions").mock(
            return_value=httpx.Response(401, json={"error": {"message": "bad"}})
        )
        with pytest.raises(UsageLimitReached) as info:
            OpenAiCompatibleBackend("openai", BASE, "sk-secret-123").complete("p", model="m", task="x")
    assert "sk-secret-123" not in str(info.value)


def test_make_backend_needs_the_key_for_openai():
    with pytest.raises(BackendUnavailable, match="OPENAI_API_KEY is not set"):
        make_backend(Profile(backend="openai", model="m"), Settings(openai_api_key=""))


def test_make_backend_builds_both_clients():
    settings = Settings(openai_api_key="sk-x", openai_base_url=BASE, freellmapi_url="http://f/v1")
    assert make_backend(Profile(backend="openai", model="m"), settings).name == "openai"
    assert make_backend(Profile(backend="freellmapi"), settings).name == "freellmapi"
    assert make_backend(Profile(backend="fake"), settings).name == "fake"
```

`tests/unit/test_profiles.py`: the shipped `profiles.yaml` (with `OPENAI_MODEL_CLIPPINGS=gpt-a` and `OPENAI_MODEL_YOUTUBE=gpt-b` in the env) loads as `default == "notes"`, `review_profile == "youtube"`, `notes == Profile("freellmapi", "auto")`, `clippings == Profile("openai", "gpt-a")`, `youtube == Profile("openai", "gpt-b")`; a profile with `when: evening` raises `ValidationError`; `backend: claude-code` raises `ValidationError`; `backend: openai` with an unset model variable raises a `ValueError` naming `model`; `resolve_profile` still follows message > class default > global default.

`tests/unit/test_doctypes.py`: `DOC_TYPES["note"].llm_profile == "notes"`, `ai-chat` → `clippings`, `youtube` and `youtube-gemini` → `youtube`.

`tests/unit/test_config.py`: `Settings()` has no `claude_bin`; `openai_base_url` defaults to `https://api.openai.com/v1`; `openai_api_key` defaults to `""`.

`tests/integration/git/test_run.py`: rename the old usage-limit test to `test_a_quota_error_defers_every_note_of_that_backend_but_not_the_others` and make the fake chat backend raise `UsageLimitReached("insufficient_quota", backend="openai")`. Expected counts are unchanged (`{"published": 1, "deferred": 2}` and one prompt sent). Add two more tests there:

```python
def test_a_used_up_budget_logs_one_error_per_backend_and_keeps_the_notes(repos, make_services, caplog):
    (repos.ideas / "inbox/clippings/second.md").write_text(chat("925d9b0b4ca21b63"))
    chats = FakeBackend([BudgetExhausted("insufficient_quota", backend="openai")])
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services(chat_backend=chats))
    assert report.counts() == {"published": 1, "deferred": 2}
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1 and "openai budget reached: 2 note(s) waiting" in errors[0]
    assert not (repos.ideas / "failed").exists()
    assert len(chats.prompts) == 1


def test_the_waiting_notes_go_through_once_the_budget_is_back(repos, make_services):
    run_pipeline(
        repos.ideas,
        repos.docs,
        RunOptions(),
        make_services(chat_backend=FakeBackend([BudgetExhausted("insufficient_quota", backend="openai")])),
    )
    second = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    assert second.counts() == {"published": 1}
```

Run: `uv run pytest -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'catcher.modules.llm.backends.openai_compatible'`, then with validation errors for the profiles.

- [ ] **Step 2: Write the implementation**

1. `git mv` the backend file and its test as listed, delete `claude_code.py`, `tests/bin/claude` and `test_claude_code.py`.
2. `openai_compatible.py`: one class. In `complete()`, build the request as `FreeLlmApiBackend` does today (same client, `max_retries=0`, `temperature=0.2`), add `response_format={"type": "json_object"}` only when `json_mode`. Map exceptions: `openai.RateLimitError` → `UsageLimitReached`; `openai.AuthenticationError` and `openai.PermissionDeniedError` → `UsageLimitReached("authentication failed: check OPENAI_API_KEY", backend=self.name)` (use the name in the message for `freellmapi`); other `openai.APIStatusError` → `BackendUnavailable(f"{self.name} HTTP {status}: …")` (truncate to 300 characters, and never include headers); `openai.APIConnectionError` and `openai.APITimeoutError` → `BackendUnavailable(f"{self.name} unreachable: …")`. Log each mapped error with the logger `catcher.llm` (`WARNING` for deferrals, `ERROR` for the authentication case).
3. `make_backend`: `fake`; `freellmapi` → `OpenAiCompatibleBackend("freellmapi", settings.freellmapi_url, settings.freellmapi_api_key, timeout_s=settings.llm_timeout_s)`; `openai` → raise `BackendUnavailable("OPENAI_API_KEY is not set")` when the key is empty, else `OpenAiCompatibleBackend("openai", settings.openai_base_url, settings.openai_api_key, json_mode=True, timeout_s=settings.llm_timeout_s)`.
4. `profiles.py`, `profiles.yaml`, `config.py`, `.env.example`, `doctypes.py`, `cli.py`, `conftest.py` as listed under **Files**. `Profile` gets the empty-model check for `backend == "openai"` in `resolve_profile`, raising `UnknownProfile`.
5. `run.py`: catch `BudgetExhausted` before `UsageLimitReached`. Record the backend in a `budget_blocked: dict[str, int]` (backend → number of deferred notes, counting the note that hit the budget), mark each note `deferred` with the message `budget reached (<backend>)`, and after the loop log one `ERROR` per backend in `budget_blocked` with the text from rule 5. The run summary line adds `budget reached: <backend>` when it happened.
6. `grep -rniE "claude.code|claude_code|claude -p|claude_bin|CLAUDE_CODE|claude-sub|free-fast|evening|\bwhen\b" src tests profiles.yaml .env.example` must return nothing, apart from the `claude.ai` host in the class detection, the `**Claude**` chat-turn marker and the `claude-code` **topic tag** in `tags.yaml`.

- [ ] **Step 3: Run the whole suite and the linters**

Run: `uv run ruff check && uv run ruff format --check && uv run pyright && uv run pytest -q`
Expected: no lint or type errors, and all tests pass.

- [ ] **Step 4: Try the two providers by hand (no repo changes)**

Put `OPENAI_API_KEY` in `.env`. Then run:
`uv run catcher reason "<an output note>" --profile clippings`
Expected: validated JSON, plus a stderr line showing `backend=openai model=<the model> tokens_in=… tokens_out=…`. With an empty or wrong key, the run stops with one `ERROR` line `authentication failed: check OPENAI_API_KEY`, and no note is moved to `failed/`.

- [ ] **Step 5: Commit**

```bash
git add -A src tests profiles.yaml .env.example
git commit -m "refactor: call every LLM through an API key with three profiles and drop claude -p"
```

---

### Task 21: A real run on copies, then on the real repos (A8 done-when)

This task is a manual checklist on real data. It ends with one approval gate from your human partner before anything is pushed to GitHub.

**Files:**
- Modify: `docs/idea-catcher-pipeline.md`, `docs/idea-catcher-service-architecture.md` (record the deviations)

- [ ] **Step 1: Run the full default suite and the linters**

Run: `uv run ruff check && uv run ruff format --check && uv run pyright && uv run pytest -q`
Expected: no lint or type errors, and all tests pass.

- [ ] **Step 2: Make isolated copies with no remote**

```bash
rm -rf tmp/ic-test && mkdir -p tmp/ic-test
git clone --no-hardlinks ~/Documents/dev/epiaku/idea-bucket tmp/ic-test/idea-bucket
git clone --no-hardlinks ~/Documents/dev/epiaku/epiaku-docs tmp/ic-test/epiaku-docs
git -C tmp/ic-test/idea-bucket remote remove origin
git -C tmp/ic-test/epiaku-docs remote remove origin
```

Expected: both copies exist, and `git -C tmp/ic-test/idea-bucket remote` prints nothing. Without a remote, `pull` and `push` do nothing, so the copies can never reach GitHub.

- [ ] **Step 3: Dry run with the fake profile**

Run: `uv run catcher run pipeline --ideas tmp/ic-test/idea-bucket --docs tmp/ic-test/epiaku-docs --profile fake --no-review --dry-run`
Expected:
- There is one line per **inbox file**, so `2446cd9c762c9cc9` appears **five times** (it was clipped 5 times). The docs repo will still get one page for it, because each page overwrites the previous one by id.
- The YouTube-summary Gemini chats are shown as `youtube-gemini` with ID `<video-id>-gemini`.
- The Web Clipper YouTube clips are shown as `youtube`, and the phone notes as `note` with 6-hex IDs.
- YouTube items show `deferred` (the fake profile does not stop the facts fetch, so it will try the network: that is expected here) or `would_publish`.
- No files change: `git -C tmp/ic-test/idea-bucket status --porcelain` prints nothing.

- [ ] **Step 4: Stage the copy for real and inspect it**

Run: `uv run catcher ingest --ideas tmp/ic-test/idea-bucket && ls -R tmp/ic-test/idea-bucket/archive tmp/ic-test/idea-bucket/output`
Expected: `inbox/` is empty, and `archive/` and `output/` hold the same file names in the same `notes/` and `clippings/` subfolders. Every `output/` file has `stage: analyzed`, and `diff` of an `archive/` file against the original in `git show HEAD:inbox/...` is empty. Any unreadable file is in `failed/` with an `.error.txt`.

- [ ] **Step 5: One note and one chat through the real APIs**

Put `OPENAI_API_KEY`, `OPENAI_MODEL_CLIPPINGS`, `OPENAI_MODEL_YOUTUBE` and `FREELLMAPI_URL` (the H4 FreeLLMApi LXC) in `.env`.

Run: `uv run catcher reason "tmp/ic-test/idea-bucket/output/notes/<a-note-name>.md" --profile notes`
Expected: validated JSON, plus a stderr line showing `backend=freellmapi` and a token count.

Run: `uv run catcher reason "tmp/ic-test/idea-bucket/output/clippings/<a-chat-name>.md" --profile clippings`
Expected: validated JSON, plus a stderr line showing `backend=openai model=<your model>` and a token count.

Run: `uv run catcher run pipeline --ideas tmp/ic-test/idea-bucket --docs tmp/ic-test/epiaku-docs --limit 3`
Expected: up to 3 `published` lines, with the notes on `freellmapi` and the chats on `openai`. `git -C tmp/ic-test/epiaku-docs log --stat -1` shows only new files under `hugo/content/en/docs/idea-bucket/`. Every step is in the log with the file name and an `(i/N)` counter.

- [ ] **Step 6: One YouTube clip and one Gemini video chat, with the reviewer**

Pick the file names from the Step 3 output, then run:
`uv run catcher render "tmp/ic-test/idea-bucket/output/clippings/<the-youtube-clip>.md" --docs tmp/ic-test/epiaku-docs`
`uv run catcher render "tmp/ic-test/idea-bucket/output/clippings/<the-gemini-chat>.md" --docs tmp/ic-test/epiaku-docs`
Expected: both use the `youtube` profile, the log shows two OpenAI calls each (summary and review, or review only for the Gemini chat), and there are two pages in `idea-bucket/youtube/` with real metrics and a `review:` block in the frontmatter. The second page links to the first.

- [ ] **Step 7: Build the copy with Hugo and look at the pages**

```bash
cd tmp/ic-test/epiaku-docs/hugo
hugo --gc --source . --config hugo.yaml --destination ../../public   # = tmp/ic-test/public
```

If the build fails on PostCSS or missing modules, run `ln -s ~/Documents/dev/epiaku/epiaku-docs/node_modules tmp/ic-test/epiaku-docs/node_modules` and build again.

Expected: the build succeeds with no errors. Then run `hugo server --source . --config hugo.yaml` and open the Notes, Clippings and YouTube cards. Check that the titles are good, the tags link to tag pages, the `youtube-lite` embed plays, and the Tips tables render.

- [ ] **Step 8: Provider failures and a used-up budget are deferrals, not failures**

Run once with `FREELLMAPI_URL` pointing at a closed port, and once with `OPENAI_API_KEY=sk-wrong`:
`uv run catcher run pipeline --ideas tmp/ic-test/idea-bucket --docs tmp/ic-test/epiaku-docs --limit 2`
Expected: the affected notes show `deferred` and stay in `output/` with `stage: analyzed` (nothing in `failed/`). With the wrong key the log has one `ERROR authentication failed: check OPENAI_API_KEY` and no further OpenAI calls in that run. Notes on the other provider are still published.

Then make the budget run out on purpose, on a **throw-away key with a tiny budget** (for example $0.01): run the pipeline until it reaches the cap. Expected: one `ERROR openai budget reached: N note(s) waiting …`, the waiting notes stay in `output/` with `stage: analyzed` (nothing in `failed/`), the FreeLLMApi notes are published, and after the budget is raised, the next run publishes the waiting notes. Note the exact error code the API returned and fix the classifier in Task 20 if it differs.

- [ ] **Step 9: Check that the docs match the code**

The deviations (AI chats publish to `idea-bucket/clippings/`, the facts sidecar, the four-folder layout, no deduplication, `failed/` and the `stage` field, no state store in Stage A, API-key profiles instead of `claude -p`) are recorded in `docs/idea-catcher-pipeline.md` and `docs/idea-catcher-service-architecture.md`. Read them once against what the copy run just did, fix anything that differs, and commit:

```bash
git add docs
git commit -m "docs: match the docs to the four-folder run"
```

- [ ] **Step 10: STOP and ask your human partner before touching the real repos**

Show the results from Steps 5–8 and ask: "Run against the real idea-bucket and epiaku-docs and push to GitHub `main`?" Mention two things:
- All current inbox captures will move to `archive/` (untouched copy) and `output/` on GitHub, which clears the phone's inbox on the next pull. The five clips of `2446cd9c762c9cc9` all go through the LLM, so use `--limit` sensibly, or move four of them out of the inbox first.
- The two uncommitted deletions in `epiaku-docs/hugo/content/en/docs/idea-bucket/clippings/` will stay uncommitted.

Only after an explicit yes, run:
`uv run catcher run pipeline --limit 3 --push`
Expected: `pushed=True`. GitHub shows one `idea-catcher: publish N page(s)` commit on epiaku-docs and one `idea-catcher: ingest and publish captures` commit on idea-bucket. After a `git pull` in the local epiaku-docs, the new pages appear in the local build.

**Stage A is done when:** a real run turns every document class in the inbox into correct pages on GitHub, and we are happy with the prompts, templates and code structure.

---

### Task 22: Run only named documents (`--file`)

> **Status: implemented (2026-09-28), not yet committed.** Added after Task 21 was written, because testing one specific document must not need a whole inbox run. 197 tests pass.

> **Superseded in part by Task 23:** `--file` now searches **only `inbox/`**. Rule 3 below (matching waiting notes in `output/`) no longer applies, because a run never reads `output/`.

**Rules:**

1. `catcher run pipeline` and `catcher ingest` take `--file NAME` (short `-f`), repeatable.
2. A name matches a document by its file name (`New chat.md`), the name without `.md`, or its path under `inbox/` (`clippings/New chat.md`), ignoring case. Partial names never match.
3. Only matching files are archived and ingested. Other inbox files are untouched. Matching **waiting notes in `output/`** (`stage: analyzed`) are processed too. `archive/`, `failed/` and `duplicates/` are not searched.
4. A name that matches nothing logs a `WARNING` (`no document named "X" found in inbox/ or waiting in output/`), is listed in `RunReport.not_found` and printed as `not-found`, and the command exits with code 1. Names that did match are still processed.
5. `--limit` applies after the selection. The duplicate check (`is_snapshot_of`) only compares the selected documents.

**Files:** `ingest.py` (`name_matches`, `ingest_inbox(only=…)`, `load_pending(only=…)`, `IngestResult.matched`), `run.py` (`RunOptions.only`, `RunReport.not_found`), `cli.py` (`--file` on both commands), tests in `tests/component/test_ingest.py` and `tests/integration/git/test_run.py`. Docs: the how-to-run and configuration pages.

---

### Task 23: A run only looks at the inbox

> **Status: implemented (2026-09-28), not yet committed.** 204 tests pass. Decision from the user: a run only looks at `inbox/` to find work, and a document leaves `inbox/` as soon as work on it starts (original to `archive/`, working copy to `output/`), so it is never started twice. A stalled copy stays in `output/` with a status. To retry, move the file from `archive/` back into `inbox/`.

> **Superseded in part by Task 24:** the archive copy is no longer byte for byte. It is renamed (calculated name) and gets two frontmatter lines. Everything else in this task holds.

**Rules:**

1. **`scan_inbox()` is read-only.** It reads `inbox/` (or only the `--file` names), works out class and `id` in memory (`Note.path` is the inbox file) and returns `ScanResult(notes, errors, matched)`. It writes and moves nothing. `catcher scan` prints its result.
2. **A document leaves `inbox/` right before its own LLM step** (`start_work`): copy the original to `archive/`, write the working copy to `output/<same subfolder and name>` with `stage: analyzed` and `analyzed_at`, remove the inbox file. It overwrites a stalled copy from an earlier run. With `--limit` or `--file`, only the chosen documents move. A dry run moves nothing.
3. **Ready:** the working copy is overwritten with the final page (no `stage`), plus `<name>.youtube.json` next to it for YouTube (`write_output`).
4. **Permanent failure:** `move_to_failed` moves the working copy to `failed/` with an `.error.txt`. An unreadable inbox file is archived and moved from `inbox/`. **Earlier snapshot:** `move_to_duplicates` (archive, then move from `inbox/`, with `duplicate_of`).
5. **Temporary problem** (rate limit, budget, provider down, facts unavailable, missing model): `mark_deferred` keeps the working copy in `output/` with `stage: deferred`, `deferred_reason` and `deferred_at`. Nothing retries it by itself.
6. **`--file` only searches `inbox/`.** A name that matches nothing warns (`no document named "X" found in inbox/`), is listed in `RunReport.not_found`, and the command exits with code 1.
7. **Commands:** `catcher scan` (read-only listing: `would process` or `duplicate of …`) replaces `catcher ingest`. `catcher reason` and `catcher render` take a document and read it in memory. `run pipeline` and `scan` have `--file`.
8. **Retry is manual for now.** A helper such as `catcher requeue`, moving every stalled document back in one go, is a possible later addition and is written up as such in the docs.

**Files:** `ingest.py` is now `inbox.py` (`scan_inbox`, `read_note`, `start_work`, `mark_deferred`, `archive_copy`, `move_to_failed`, `move_to_duplicates`, `name_matches`, `is_snapshot_of`); `publish.py` (`write_output` replaces `finalize_output`); `process.py` (`facts_for` no longer reads or writes a sidecar, `ProcessedPage.facts` carries the facts); `run.py` (the flow, `RunReport.unreadable`); `cli.py` (`scan`); tests `tests/component/test_inbox.py` and `tests/integration/git/test_run.py`. Docs: the pipeline, architecture, configuration and how-to-run pages.

---

### Task 24: One calculated file name in every folder

> **Status: implemented (2026-09-28), not yet committed.** 214 tests pass. Decision from the user: files with the same name overwrote each other in `archive/` and valuable information was lost. So a document gets a calculated, unique file name when work starts, and keeps it everywhere.

**Rules:**

1. **Name:** `YYYYMMDD-<short guid>-<title>.md`. Date = capture date (`created`/`captured`), else the processing day. Guid = 6 random hex characters, re-drawn until the name is unused in `archive/`, `output/`, `failed/` and `duplicates/`. Title = the clip `title`; when it is missing or generic (`New chat`, `Untitled`, `Chat`), an `ai-chat` uses the first 8 words of the first `**You**` message (links removed, cut at the answer), everything else its original file name. The title is slugged to lower-case ASCII letters, digits and hyphens (`untitled` when nothing is left). The stem is cut so that `<stem>.youtube.json` is at most 128 characters.
2. **Assigned when work starts** (`start_work`), and also for duplicates (`move_to_duplicates`) and unreadable files (`move_to_failed`), so the same collision cannot come back in `failed/` or `duplicates/`.
3. **Stored in the frontmatter:** `original_filename` and `calculated_filename`, on the archive copy and the working copy. They are **inserted as text** into the existing frontmatter (`with_filename_fields`), so everything else stays byte for byte (line endings and comments included). A file without frontmatter gets a new block. Fields that are already there are kept.
4. **Unreadable files** cannot be edited: their bytes are copied unchanged under a calculated name to `archive/` and `failed/`, and the `.error.txt` records the original and the calculated name.
5. **Requeue:** a file moved from `archive/` to `inbox/` carries `calculated_filename`, so `assign_name` keeps it. `start_work` overwrites the same-named files in `archive/` and `output/` (the stalled copy), and no second archive file appears.
6. **The docs page uses the same name.** `page_name()` returns the calculated name, `write_page` still removes an older page with the same `id`, and the page frontmatter gets `source_file: <subfolder>/<calculated name>`. The `output/` page and the `epiaku-docs` page stay identical.
7. **`--file` and `scan`** match a document by its file name in `inbox/` or by `original_filename` (a requeued file), ignoring case.
8. **`catcher render`** gives the page a calculated name too (random guid, no uniqueness check across folders, because it has no idea-bucket).

**Known limits (documented):** the guid is random, so a name cannot be predicted; a re-clip of one conversation gets its own name, so `output/` can hold several copies for one page in `epiaku-docs`; requeueing an edited file overwrites its archive copy; a note duplicated in Obsidian keeps the copied `calculated_filename` and would overwrite the original's archive file.

**Files:** `inbox.py` (`calculated_stem`, `slugify_title`, `name_title`, `first_words`, `assign_name`, `with_filename_fields`, `Note.name`, `Note.original`, `Note.target_rel`), `publish.py` (`write_output` uses the calculated name), `render.py` (`page_name`, `source_file`), `cli.py`, tests `tests/component/test_inbox.py` and `tests/integration/git/test_run.py`. Docs: the pipeline page (new "File names" section), the architecture, how-to-run pages.

---

### Task 25: Artifacts (files that are not markdown)

> **Status: implemented (2026-09-28), not yet committed.** 228 tests pass. Decision from the user: the run processes `.md` files; other files are sent on, renamed, without any LLM step.

**Rules:**

1. **Every non-markdown file in `inbox/`** (at any depth, hidden files and `Thumbs.db` / `desktop.ini` excluded) is an `Artifact`, found by `scan_inbox()` (`ScanResult.artifacts`).
2. **Name:** `YYYYMMDD-<6 hex guid>-<original name>`, the date being the processing day. The original name and extension stay, illegal characters become `_`, the name is cut to 128 characters keeping the extension, and the guid is redrawn until the name is free in `archive/artifacts/` and `idea-bucket/artifacts/`. A name that already matches `^\d{8}-[0-9a-f]{6}-` is kept, so a file moved back from the archive is a retry that overwrites the same files. The rule applies **only to artifacts**; markdown keeps using the `calculated_filename` frontmatter line.
3. **`copy_artifact()`:** copy to `archive/artifacts/<name>` and to `<epiaku-docs>/idea-bucket/artifacts/<name>` (the root of the docs repo, outside `hugo/`), then remove the inbox file. Nothing goes to `output/`. A copy error (`OSError`) leaves the file in `inbox/` and reports `failed`.
4. **Size limit:** more than `ARTIFACT_MAX_MB` (default 25) is skipped with a `WARNING`, reported as `skipped`, and stays in `inbox/`.
5. **`--limit` does not apply** (no LLM cost). `--file` matches the file name, the name without its prefix, and the path. A dry run reports `would_copy` and changes nothing. The docs commit message becomes `idea-catcher: publish N page(s) and M artifact(s)` when there are artifacts.
6. **`catcher scan`** lists artifacts (`would copy`, or `would skip` when over the limit).

**Files:** `inbox.py` (`Artifact`, `artifact_name`, `copy_artifact`, `scan_inbox`), `run.py` (`copy_artifacts`, statuses `artifact` and `would_copy`), `core/config.py` and `.env.example` (`ARTIFACT_MAX_MB`), `cli.py`, tests in `tests/component/test_inbox.py` and `tests/integration/git/test_run.py`. Docs: the pipeline page (new "Artifacts" section), how-to-run, configuration and architecture pages.

---

### Task 26: Committed test data and `catcher testdata reset`

> **Status: implemented (2026-09-28), not yet committed.** 235 tests pass. Reason: manual tests used fresh clones of `idea-bucket` and `epiaku-docs`, so when those repos change there would be no test data left.

**Rules:**

1. **Only the needed folders** are copied into `tests/data/` (1.2 MB): `idea-bucket/inbox/` (48 markdown captures including one web clip, plus one tiny fake `sample-report.pdf` to try artifacts) and `epiaku-docs/hugo/content/en/docs/idea-bucket/` (the already published pages), plus the empty folder `epiaku-docs/idea-bucket/artifacts/` (a `.gitkeep`) where artifacts are sent. The pipeline reads and writes nothing else. Hidden files (`.DS_Store`, `.trash`) are left out, and the data was scanned for secrets before it was committed (none found).
2. **`reset_test_repos(target=<project>/tmp/ic, source=tests/data)`** deletes the target, copies the data, and makes two git repos (one commit, **no remote**). It only deletes a target that has the marker file `.catcher-testdata` (written when it made it) and refuses anything else, so a wrong `--target` cannot delete real work.
3. **`catcher testdata reset [--target] [--source]`** calls it and prints the run command. The default target is `tmp/ic` in the project root (the project's own `tmp/` folder, which Git ignores), not the system temp folder. Exit code 2 when it refuses.
4. **The suite uses the data too:** `tests/component/test_testdata.py` (what the data holds, fresh every time, no remote, refusal to delete a foreign folder, the command) and `tests/integration/git/test_testdata_run.py` (the whole pipeline on the data with the fake LLM: no failures, one artifact, duplicates found, YouTube clips stalled because facts cannot be fetched in tests, both repos clean afterwards). The assertions are deliberately loose, so refreshing the data does not break them.
5. **The how-to page** has the new commands, what the data is, and how to refresh it.

**Files:** `src/catcher/core/testdata.py`, `src/catcher/cli.py` (`testdata reset`), `tests/data/**`, the two test files above. Docs: how-to-run and architecture pages.

---

### Task 27: The `web-clip` class (clippings that are not chats or videos)

> **Status: implemented (2026-09-28), not yet committed.** 245 tests pass. Reason: a page clipped from any other site was treated as a note, which got the short-idea prompt, the `notes` profile and the `notes/` folder.

**Rules:**

1. **Detection:** after the explicit `type`, YouTube (with a video) and the chat hosts, a `source` that is an `http(s)` address with a host is a `web-clip`. No `source` (or one that is not a web address) is still a `note`. A YouTube address without a video (a channel) is now a `web-clip` too, not a note.
2. **Id:** an `id` in the frontmatter wins. Otherwise 12 hex characters of the SHA-1 of the normalised address: lower-case host without `www.`, path without a trailing slash, query sorted, tracking parameters (`utm_*`, `gclid`, `fbclid`, `igshid`, `mc_cid`, `mc_eid`) and the fragment dropped. So the same page clipped again gets the same id and replaces its page. The page's `source` is the address without tracking parameters and fragment (`clean_url`).
3. **Its own prompt** `prompts/web-clip.md` (`version: web-clip-1`), **schema** `WebClipSummary` (title, description, summary, key_points, ideas_to_use, body, tags), **template** `web-clip.md.j2` (Summary, Key Points, Ideas to Use It, details, source; empty sections left out) and page folder `hugo/content/en/docs/idea-bucket/web-clips/` (which needs an `_index.md` in the epiaku-docs repo).
4. **Profile:** `clippings` (OpenAI). No new profile. The archive name uses the clip title, else the file name.
5. **Tests:** detection and ids (`tests/unit/test_doctypes.py`), the schema, the prompt, the page (`test_render.py`), processing (`test_process.py`), and the test data has a sample article clip and the `web-clips/_index.md`, so the whole-pipeline test publishes a web-clip page.

**Files:** `doctypes.py` (`WEB_CLIP`, `clean_url`, detection, id, canonical source), `llm/schemas.py`, `llm/prompts/web-clip.md`, `pipeline/templates/web-clip.md.j2`, `llm/backends/fake.py` (canned output), tests, `tests/data/`. Docs: the pipeline page (class table, detection order, registry sketch, a "Web clips" section), configuration and architecture pages.

---

### Task 28: A yt-dlp transcript fallback, and made the extra `youtube-gemini` check optional (off by default)

> **Status: implemented (2026-09-28), not yet committed.** 244 tests pass. Reason: YouTube's transcript-serving endpoint got blocked from the dev machine's IP during testing (a 429, not a general YouTube block — normal browsing and `yt-dlp`'s own metadata requests were unaffected), which turned every YouTube document into a permanent `FactsUnavailable` failure. `youtube-gemini` was hit by this too, even though its whole point (per the user) was to already have a good, human-made summary in hand.

**Rules:**

1. **A second transcript source.** `_fetch_transcript_via_ytdlp()` in `facts.py` asks yt-dlp itself for the same auto-captions (`writeautomaticsub`, `subtitlesformat: json3`), fetched through yt-dlp's own HTTP client (`ydl.urlopen`, not a bare `urllib` request — a bare request to the caption URL gets rate-limited even when yt-dlp's own request for the same URL does not, because it lacks yt-dlp's headers). `_parse_json3_captions()` is a small pure function parsing that JSON into `Segment`s, tested directly with no network mocking needed.
2. **A blocked transcript no longer fails the document.** `_best_effort_transcript()` tries `youtube_transcript_api`, then the yt-dlp fallback, and returns `None` (not an exception) if both fail — logged as a `WARNING`, not raised. `fetch_facts()` now only raises `FactsUnavailable` when `yt-dlp`'s own video-info call fails (rare) or no video id can be found; a transcript-only failure degrades to "no transcript available", which the `youtube.md` and `youtube-gemini.md` prompts already handle.
3. **The `youtube-gemini` transcript check became optional, off by default.** `ProcessOptions.check_gemini_against_transcript` / `RunOptions.check_gemini_against_transcript` / `--check-gemini-transcript` (all default `False`). `_prompt_facts()` strips the transcript from what the LLM prompt sees for `youtube-gemini` unless the flag is on, so by default that class's page is Gemini's own answer, reformatted, and checked only against the title/description (the prompt's existing "no transcript" branch) — never blocked by a transcript-fetch failure. With the flag on, the transcript is included and the prompt corrects anything it contradicts, same as before this task. The transcript is still fetched in the background either way (for the free checks and in case the flag is used), but that fetch never blocks the page. `youtube` (direct clips) is unaffected: it always uses whatever transcript is available, since it has no other source to summarize from.

**Files:** `youtube/facts.py` (`_fetch_transcript_via_ytdlp`, `_parse_json3_captions`, `_best_effort_transcript`), `pipeline/process.py` (`ProcessOptions.check_gemini_against_transcript`, `_prompt_facts`), `pipeline/run.py` (`RunOptions.check_gemini_against_transcript`), `cli.py` (`--check-gemini-transcript` on `render` and `run pipeline`, and `FactsUnavailable` now caught cleanly by `render` instead of crashing), tests in `tests/component/test_facts.py`, `test_process_youtube.py`, `test_publish.py`. Docs: pipeline and architecture pages ("one class, one prompt, one call" section), how-to-run and configuration pages (also cleaned up stale `--no-review`/"reviewer" leftovers from Task 25's rename that were missed at the time).

---

### Task 29: `youtube-gemini` makes no YouTube API call at all, ever

> **Status: implemented (2026-09-29), not yet committed.** Reason: even the optional transcript check added in Task 28 still fetched real YouTube facts (counts, and the transcript when the flag was on) for every `youtube-gemini` document, in the foreground, on every run. That keeps the class exposed to exactly the kind of blocking/rate-limiting Task 28 was trying to work around. `youtube-gemini`'s whole point is that a human already produced a good summary in Gemini; there is nothing left to check it against that is worth the risk, so the class is now a pure clipping-reformatting task, like `ai-chat` or `web-clip`, and never touches `yt-dlp` or the transcript endpoint.

**Rules:**

1. **No facts, ever, for this class.** `_process_youtube_gemini()` in `process.py` is a new function, split out of the old shared `_process_youtube()`. It calls `svc.facts` for nothing: the only local, non-network step is parsing the video id out of the YouTube URL already in Gemini's own chat text (`gemini_video_id()`, unchanged, pure text parsing). `_process_youtube()` (the `youtube` class) keeps calling `facts_for()` exactly as before — this task only changes `youtube-gemini`.
2. **The optional transcript check is gone, not just off by default.** `ProcessOptions.check_gemini_against_transcript`, `RunOptions.check_gemini_against_transcript`, `--check-gemini-transcript` on `render` and `run pipeline`, and `_prompt_facts()` are all removed — there is no facts object left for that flag to control. `catcher reason` no longer refuses `youtube-gemini` (it only needs facts first for `youtube`); it now works for both `note`/`ai-chat`/`web-clip` and `youtube-gemini`.
3. **The page has no metrics table for this class.** `youtube.md.j2` wraps the "no transcript" warning and the whole "📊 Metrics" section in `{% if facts %}`; `render_page()` is called with `facts=None` for `youtube-gemini`, and the YouTube embed's title falls back straight to the LLM's own `summary.title` (there is no `facts.title` to prefer). The free, non-LLM checks in `youtube/checks.py` already require a `YoutubeFacts` argument, so they simply never run for this class — nothing new needed there.
4. **A new prompt, `youtube-gemini-2`.** Rewritten to drop every `facts.*` and `{% if transcript %}` reference: it now just asks the LLM to restructure Gemini's own chat answer into the `YoutubeSummary` fields, explicitly told this is a reformatting task with no fact-check, and to ignore Gemini's own metrics table since code no longer adds real ones for this class.

**Files:** `pipeline/process.py` (`_process_youtube_gemini`, `ProcessOptions` and `_process_youtube` trimmed), `pipeline/run.py` (`RunOptions` trimmed), `cli.py` (`render`/`run pipeline` flag removed, `reason`'s youtube-only guard), `templates/youtube.md.j2` (`{% if facts %}` around metrics and the no-transcript warning), `llm/prompts/youtube-gemini.md` (rewritten, `youtube-gemini-2`), `tests/component/test_process_youtube.py` (the two `--check-gemini-transcript` tests replaced with one asserting the facts fetcher is never called and one for a chat with no findable video id). Docs: pipeline, architecture, configuration and how-to-run pages.

**Follow-up, same day: the published page also gets `original_filename`.** Asked live: the page had `source_file` (the calculated name) but nothing said what the document was called when it first entered `inbox/`, before the pipeline renamed it. `build_frontmatter()` in `render.py` now sets `original_filename` (`ctx.note.original_name`) right next to `source_file`, under the same `if ctx.note.name:` guard (both only make sense once a document has gone through naming). `archive/`'s and `output/`'s own `original_filename`/`calculated_filename` lines are unchanged — this only adds the one field to the page written to `epiaku-docs`. Files: `pipeline/render.py`, `tests/component/test_render.py` (new test), `tests/integration/git/test_run.py` (one more assertion on the existing `systeme.md` case), pipeline doc.

**Follow-up, same day: dropped the hardcoded `temperature=0.2` on OpenAI-compatible calls.** Seen live: `OPENAI_MODEL_YOUTUBE` pointed at a model that rejects any `temperature` other than its default (1) with an HTTP 400 (`unsupported_value`) — a class of newer "reasoning" models does this. `openai_compatible.py`'s `complete()` no longer sends `temperature` at all, so every model's own default applies; no test asserted a specific value, so nothing else changed.

**Follow-up, same day: a missing transcript now defers `youtube` instead of calling the LLM.** Seen live: a run where the transcript endpoint was blocked (both `youtube_transcript_api` and the yt-dlp fallback failed) still went on to call OpenAI with just a title and description — a paid call for a summary not worth having. `_process_youtube()` now raises `FactsUnavailable` when `facts.transcript` is empty, before the LLM call, so the document defers (`stage: deferred`) and retries later instead. This makes the `youtube.md` prompt's old "no transcript" branch and the page template's "no transcript" warning unreachable, so both were removed (`youtube-2`). `facts.py`'s `_best_effort_transcript()` is unchanged — `fetch_facts()` still returns real counts even without a transcript, `_process_youtube()` is what now refuses to use them alone. `youtube-gemini` is unaffected: it never depended on a transcript to begin with. Files: `pipeline/process.py`, `templates/youtube.md.j2`, `llm/prompts/youtube.md` (`youtube-2`), `youtube/facts.py` (docstring only), `tests/component/test_process_youtube.py` (`test_no_transcript_is_said_on_the_page` replaced with `test_no_transcript_defers_without_calling_the_llm`), pipeline and architecture docs.
