# idea-catcher

A small Python tool that turns ideas you capture on the go into pages on your docs site.

You capture notes, AI chats, web clips and YouTube links in Obsidian. They land in the **idea-bucket** git repo. The Idea Catcher reads them, asks an LLM to summarize each one, and writes a finished page into the **epiaku-docs** repo. The original is always kept, and every page can be rerun.

```text
Obsidian -> idea-bucket/inbox -> catcher run pipeline -> epiaku-docs (page)
                                      |-> archive/ (the untouched original)
                                      |-> output/ (the working copy), failed/, duplicates/
```

**Status:** Stage A, the local pipeline you run by hand, is done. Stage B (a Postgres queue, schedules) comes next.

## Try it safely

Use throw-away copies of the repos. Nothing here touches your real `idea-bucket` or `epiaku-docs`, and `--profile fake` makes no LLM call.

```bash
uv sync
cp .env.example .env                 # then fill in the API keys you need
uv run catcher testdata reset        # fresh test repos in tmp/ic
uv run catcher run pipeline --ideas tmp/ic/idea-bucket --docs tmp/ic/epiaku-docs --profile fake --dry-run
```

## The commands

| Command | What it does |
| --- | --- |
| `catcher run pipeline` | Process the inbox: summarize, write pages, archive, commit. `--file`, `--requeue`, `--limit`, `--dry-run`, `--push`, `--profile`, `--wait-youtube`, `--refresh-facts` |
| `catcher scan` | List what is in the inbox, without changing anything |
| `catcher reason`, `catcher render` | Try the LLM step, or write one page, on a single document |
| `catcher youtube facts` | Print the facts of one YouTube video (it respects the YouTube rate limits) |
| `catcher testdata reset` | Make fresh test repos in `tmp/ic` |
| `catcher version` | Print the version |

Document classes: `note`, `ai-chat`, `web-clip`, `youtube` (a clipped YouTube page) and `youtube-gemini` (a Gemini chat about a video). Files that are not markdown are copied to `epiaku-docs` as artifacts.

## Worth knowing

- **LLM profiles** (`profiles.yaml`): short notes use FreeLLMApi, chats, clips and YouTube use the OpenAI API. Calls that fail for a temporary reason are retried.
- **YouTube blocks IP addresses that ask too fast.** The Idea Catcher saves the facts of each video once, spaces its requests out, keeps at least 10 minutes between fetches, and stops completely after a block. Details: [YouTube and the gap between calls](docs/idea-catcher-how-to-run.md).
- **Checks before every commit:** `ruff`, `pyright` and the unit and component tests (`pre-commit`). Tests never call a real LLM or YouTube.

## Documentation

| Page | What is in it |
| --- | --- |
| [How to run it](docs/idea-catcher-how-to-run.md) | Every command and option, with recipes on test data and on the real repos |
| [Configuration](docs/idea-catcher-configuration.md) | `.env`, `profiles.yaml`, and every setting |
| [The pipeline](docs/idea-catcher-pipeline.md) | The flow from phone note to docs page, and the options considered |
| [The service architecture](docs/idea-catcher-service-architecture.md) | The software: the MVP in three stages, and future features |
| [Stage A: what we built and what we learned](docs/idea-catcher-stage-a-lessons-learned.md) | How the design changed while building, and the lessons |
| [YouTube IP bans and the queue](docs/idea-catcher-youtube-bans-and-queue-options.md) | How we stay unblocked, and the Stage B queue options |
| [YouTube methods compared](docs/idea-catcher-youtube-methods-comparison.md) | Direct clip versus Gemini chat, checked against the transcripts |
