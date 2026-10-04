# 💡 idea-catcher

A small Python tool that turns ideas you capture on the go into pages on your docs site.

You capture notes, AI chats, web clips and YouTube links in Obsidian. They land in the **idea-bucket** git repo. The Idea Catcher reads them, asks an LLM to summarize each one, and writes a finished page into the **epiaku-docs** repo. The original is always kept, and every page can be rerun.

```text
Obsidian -> idea-bucket/inbox -> catcher run pipeline -> epiaku-docs (page)
                                      |-> archive/ (the untouched original)
                                      |-> output/ (the working copy), failed/, duplicates/
```

**Status:** Stage A, the local pipeline you run by hand, is done. Stage B (a Postgres queue, schedules) is in progress: the queue, the worker and its jobs are built (B0 to B3); the shared YouTube gate, item states, schedules and Compose come next.

## 📑 Table of contents

- [🚀 Try it safely](#-try-it-safely)
- [⌨️ The commands](#-the-commands)
- [🧠 Worth knowing](#-worth-knowing)
- [📚 Documentation](#-documentation)
  - [Using it](#using-it)
  - [Design](#design)
  - [Diagrams](#diagrams)
  - [Stage A](#stage-a)
  - [YouTube](#youtube)
  - [History](#history)

## 🚀 Try it safely

Use throw-away copies of the repos. Nothing here touches your real `idea-bucket` or `epiaku-docs`, and `--profile fake` makes no LLM call.

```bash
uv sync
cp .env.example .env                 # then fill in the API keys you need
uv run catcher testdata reset        # fresh test repos in tmp/ic
uv run catcher run pipeline --ideas tmp/ic/idea-bucket --docs tmp/ic/epiaku-docs --profile fake --dry-run
```

## ⌨️ The commands

| Command | What it does |
| --- | --- |
| `catcher run pipeline` | Process the inbox: summarize, write pages, archive, commit. `--file`, `--requeue`, `--limit`, `--dry-run`, `--push`, `--profile`, `--wait-youtube`, `--refresh-facts`, `--refresh-llm`, `--retry-deferred` |
| `catcher scan` | List what is in the inbox, without changing anything |
| `catcher reason`, `catcher render` | Try the LLM step, or write one page, on a single document |
| `catcher youtube facts` | Print the facts of one YouTube video (it respects the YouTube rate limits) |
| `catcher db upgrade`, `catcher db downgrade REVISION` (for example `-1`) | Create or roll back the Postgres tables (Stage B; needs `DATABASE_URL`, see [How to run Stage B](docs/idea-catcher-how-to-run-stage-b.md)) |
| `catcher worker` | Run the jobs in the queue, one at a time, until Ctrl-C (`--once`: run what is due, then exit). One worker at a time (Stage B) |
| `catcher jobs add TYPE`, `catcher jobs list` | Put a job on the queue (`pipeline.run`, `pipeline.publish`, with `--param KEY=VALUE`), list the jobs (Stage B) |
| `catcher testdata reset` | Make fresh test repos in `tmp/ic` (`--fresh-llm-and-youtube`: without the saved LLM replies and YouTube facts, so a run calls both for real) |
| `catcher version` | Print the version |

Document classes: `note`, `ai-chat`, `web-clip`, `youtube` (a clipped YouTube page) and `youtube-gemini` (a Gemini chat about a video). Files that are not markdown are copied to `epiaku-docs` as artifacts.

## 🧠 Worth knowing

- **LLM profiles** (`profiles.yaml`): short notes use FreeLLMApi, chats, clips and YouTube use the OpenAI API. Calls that fail for a temporary reason are retried.
- **YouTube blocks IP addresses that ask too fast.** The Idea Catcher saves the facts of each video once, spaces its requests out, keeps at least 2 minutes between fetches, and stops completely after a block. Details: [YouTube and the gap between calls](docs/idea-catcher-how-to-run.md#youtube-gap).
- **Checks before every commit:** `ruff`, `pyright` and the unit and component tests (`pre-commit`). Tests never call a real LLM or YouTube.
- **All checks in one command:** `scripts/check` runs `ruff`, `pyright` and all tests with outgoing network blocked (`scripts/check --fast` skips the database tests, no Docker needed). On GitHub it runs only when you start it by hand (Actions tab, "Run workflow").

## 📚 Documentation

Everything is in the [`docs/`](docs/) folder.

### Using it

| Page | What is in it |
| --- | --- |
| [How to run it](docs/idea-catcher-how-to-run.md) | Every command and option, with recipes on test data and on the real repos |
| [How to run Stage B](docs/idea-catcher-how-to-run-stage-b.md) | The Postgres queue and the worker: start Postgres, add jobs, run the worker on test and real repos, publish, stop it, exit codes |
| [Configuration](docs/idea-catcher-configuration.md) | `.env`, `profiles.yaml`, and every setting |

### Design

| Page | What is in it |
| --- | --- |
| [The pipeline](docs/idea-catcher-pipeline.md) | The flow from phone note to docs page, and the options considered |
| [The service architecture](docs/idea-catcher-service-architecture.md) | The software: the MVP in three stages, and future features |

### Diagrams

| Page | What is in it |
| --- | --- |
| [From Obsidian to epiaku-docs](docs/idea-catcher-diagram-document-flow.md) | The folders a capture travels through, the states of a document, and what changes in the file |
| [The architecture](docs/idea-catcher-diagram-architecture.md) | What runs today and in the planned containers, the code modules, and the external services |
| [The processing loop](docs/idea-catcher-diagram-processing-loop.md) | The loop over the inbox, the parsing, the YouTube calls, the LLM, and the circuit breakers |

### Stage A

| Page | What is in it |
| --- | --- |
| [What we built and what we learned](docs/idea-catcher-stage-a-lessons-learned.md) | How the design changed while building, and 32 lessons |
| [The implementation plan](docs/superpowers/plans/2026-09-27-idea-catcher-stage-a.md) | The task-by-task plan Stage A was built from, with an outcome note |

### YouTube

| Page | What is in it |
| --- | --- |
| [IP bans and the queue](docs/idea-catcher-youtube-bans-and-queue-options.md) | How a ban works, the protections we built, and the Stage B queue options |
| [The two methods compared](docs/idea-catcher-youtube-methods-comparison.md) | A direct clip versus a Gemini chat, checked against the transcripts |
| [Transcript research](docs/idea-catcher-youtube-transcript-research.md) | Why fetching transcripts got blocked, and what another tool does |
| [yt-dlp command-line tests](docs/idea-catcher-youtube-transcript-cli-tests.md) | The by-hand tests that diagnosed the block |

### History

| Page | What is in it |
| --- | --- |
| [The obsolete folder design](docs/idea-bucket-pipeline.md) | An earlier folder structure that is **not used**, kept for reference |
