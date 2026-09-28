---
title: "Idea Catcher: How to Run It"
linkTitle: "Idea Catcher: How to Run"
description: "How to run the Idea Catcher CLI: every command and option with examples, how to read the output, and recipes for a free check, a small first run, publishing and retrying."
weight: 40
type: docs
---

This page shows how to **run** the Idea Catcher (Stage A, the local CLI). The settings it needs are on the [configuration page](../idea-catcher-configuration/), the flow on the [pipeline page](../idea-catcher-pipeline/).

## Before you start

- Work from the `idea-catcher` repo root, with the virtual environment active or with `uv run` in front of every command.
- Copy `.env.example` to `.env` and fill it in. See the [configuration page](../idea-catcher-configuration/).
- Make sure `IDEAS_REPO` and `DOCS_REPO` point to your local checkouts of `idea-bucket` and `epiaku-docs`.
- Every command below starts with `uv run catcher`. `uv run catcher --help` lists all commands.

## The usual order

1. **Free check.** See what would happen, with no LLM call and no change.
2. **Small real run.** Process three notes for real, and look at the result.
3. **Full run.** Process everything, then push when you are happy.

```bash
uv run catcher run pipeline --profile fake --no-review --dry-run     # 1. free check
uv run catcher run pipeline --limit 3                                # 2. small real run
uv run catcher run pipeline --push                                   # 3. full run and push
```

## The commands

- **`run pipeline`**: the whole flow, in one go. This is the one you use.
- **`scan`**: lists what is in `inbox/` and what a run would do with it. It changes nothing.
- **`reason`**: sends one document to the LLM and prints the answer. Nothing is written.
- **`render`**: makes the page for one document and writes it into `epiaku-docs`. No commit.
- **`youtube facts`**: prints the counts and transcript of one YouTube video.
- **`version`**: prints the version.

**A run only looks at `inbox/` to find work.** When work on a document starts, it gets a **calculated file name** (`YYYYMMDD-<short guid>-<title>.md`) and leaves `inbox/`: the original goes to `archive/` and a working copy to `output/`, both under that name. If something temporary goes wrong (the LLM is down, a budget is used up), the working copy stays in `output/` with `stage: deferred` and the reason. The run never reads `output/`, so **to retry, move the file from `archive/` back into `inbox/`**. It keeps its calculated name, so the next run overwrites the stalled copy.

## `run pipeline`: the whole flow

```bash
uv run catcher run pipeline [OPTIONS]
```

What one run does, in order:

1. Reads the documents in `inbox/` (nothing is written yet).
2. Moves earlier snapshots of a longer clip to `duplicates/`. They get no LLM call.
3. For each remaining document, right before its LLM step: gives it its calculated name, copies the original to `archive/` (with two frontmatter lines added), writes a working copy to `output/`, and removes it from `inbox/`. With `--limit` or `--file`, only those documents leave `inbox/`.
4. Sends the document to the LLM (and for YouTube, to the reviewer).
5. When it is ready, writes the page to `epiaku-docs` and over the working copy in `output/`, under the same calculated name.
6. Moves a document to `failed/` if it cannot be processed for good.
7. Leaves the working copy in `output/` with `stage: deferred` if the problem is temporary.
8. Commits both repos. It pushes only when you ask.

### Options

- **`--profile NAME`**: which LLM profile to use for every note. Without it, each kind of note uses its own default (`notes`, `clippings` or `youtube`). Use `--profile fake` for a free run.

  ```bash
  uv run catcher run pipeline --profile fake         # canned answers, costs nothing
  uv run catcher run pipeline --profile clippings    # force the clippings profile for every note
  ```

- **`--no-review`**: skip the YouTube reviewer. This saves one LLM call per video, but the summary is then not checked against the transcript.

  ```bash
  uv run catcher run pipeline --no-review
  ```

- **`--limit N`**: process at most N notes. The rest wait for the next run. Use it to control spend.

  ```bash
  uv run catcher run pipeline --limit 3
  ```

- **`--file NAME`** (or `-f`): process **only the named document**. Use it to test one or a few documents. Repeat it for more.

  ```bash
  uv run catcher run pipeline --file "New chat 2"
  uv run catcher run pipeline --file "clippings/New chat.md" --file "Hello world" --profile fake
  ```

  - The name can be the file name (`New chat.md`), the name without `.md` (`New chat`), or the path with the subfolder (`clippings/New chat.md`). Upper and lower case do not matter. A file you moved back from `archive/` is found by its calculated name **or** by the name it was captured under.
  - Only those documents are processed. The other files stay in `inbox/`, untouched.
  - **Only `inbox/` is searched.** Files in `output/`, `archive/`, `failed/` and `duplicates/` are never looked at. To run one of them again, move the file into `inbox/` first.
  - **If no document has that name, you get a warning** and nothing is processed for that name: `no document named "typo" found in inbox/`. The command then ends with exit code 1.
  - It works together with `--limit`, which then applies to the named documents.
  - The duplicate check only compares the documents you named. If you name a short clip and not its longer copy, the short clip is processed on its own.

- **`--dry-run`**: change no files and commit nothing. Note that with a real profile a dry run **still calls the LLM**. Combine it with `--profile fake` for a free check.

  ```bash
  uv run catcher run pipeline --profile fake --dry-run
  ```

- **`--push`**: push both repos after committing. Off by default, so a normal run only commits locally.

  ```bash
  uv run catcher run pipeline --limit 3 --push
  ```

- **`--ideas PATH`**: use another `idea-bucket` checkout than `IDEAS_REPO`. Handy for a run on a copy.
- **`--docs PATH`**: use another `epiaku-docs` checkout than `DOCS_REPO`.

  ```bash
  uv run catcher run pipeline --ideas /tmp/copy/idea-bucket --docs /tmp/copy/epiaku-docs --profile fake
  ```

- **`--log-level LEVEL`**: how much to log. It goes **before** the command name.

  ```bash
  uv run catcher --log-level DEBUG run pipeline --profile fake --dry-run
  ```

### What your example means

```bash
uv run catcher run pipeline --profile fake --no-review --dry-run
```

- `--profile fake`: use the fake profile, so no LLM is called.
- `--no-review`: skip the YouTube reviewer.
- `--dry-run`: change nothing and commit nothing.

Together they read both repos, show what would happen, and cost nothing.

## The other commands

### `scan`

```bash
uv run catcher scan [--ideas PATH] [--file NAME]
```

Lists every document in `inbox/` with its class and `id`, and says what a run would do with it: `would process`, or `duplicate of <longest clip>`. It reads files only. It moves nothing, writes nothing and calls no LLM.

```bash
uv run catcher scan                       # everything in the inbox
uv run catcher scan --file "New chat"     # check how one document is classified
```

Use it to look before you spend anything.

### `reason`

```bash
uv run catcher reason DOCUMENT [--profile NAME]
```

Sends one document to the LLM and prints the answer as JSON, plus one line with the profile, model and token counts. It writes nothing and moves nothing. It does not handle YouTube documents: use `render` for those.

```bash
uv run catcher reason "../idea-bucket/inbox/notes/Hello world.md" --profile notes
```

This is the best first real test of a profile, because it is one small call.

### `render`

```bash
uv run catcher render DOCUMENT [--docs PATH] [--profile NAME] [--no-review]
```

Makes the page for one document and writes it into `epiaku-docs`. It does not touch the document in `inbox/`, and it does not commit. You can look at the result before you commit anything.

```bash
uv run catcher render "../idea-bucket/inbox/clippings/New chat.md" --profile clippings
uv run catcher render "../idea-bucket/inbox/clippings/A video.md" --no-review
```

### `youtube facts`

```bash
uv run catcher youtube facts URL_OR_VIDEO_ID
```

Prints the counts, description and transcript of one video as JSON. It calls YouTube, not an LLM.

```bash
uv run catcher youtube facts https://www.youtube.com/watch?v=MBPHU7aaklM
```

## Reading the output

`run pipeline` prints one line per note, then a summary:

```text
published      ai-chat         cf81e40b020519ef  20260925-a1b2c3-sell-bundles.md
deferred       youtube         MBPHU7aaklM       YouTube facts unavailable
duplicate      ai-chat         2446cd9c762c9cc9  duplicate of clippings/obsidian github link.md
summary: {'published': 1, 'deferred': 1, 'duplicate': 1} committed={'docs': True, 'ideas': True} pushed=False
```

The status in the first column is one of:

- **`published`**: the page is written to `epiaku-docs` and to `output/`, the original is in `archive/`, and the document is gone from `inbox/`.
- **`would_publish`**: the same, in a `--dry-run`. Nothing was written.
- **`deferred`**: not done, but not lost. The working copy stays in `output/` with `stage: deferred` and the reason. To retry, move the file from `archive/` back into `inbox/`. Typical reasons: a provider is down, a rate limit, a used-up budget, YouTube facts not available, or a missing model setting.
- **`failed`**: could not be processed for good, for example invalid LLM output twice or an invalid page. The note moves to `failed/` with a `.error.txt` that says why.
- **`duplicate`**: an earlier snapshot of a longer clip. Moved to `duplicates/`, no LLM call.
- **`skipped`**: left for the next run because of `--limit`.

The last line shows the counts, and whether both repos were committed and pushed.

**Exit code:** `0` when nothing failed. `1` when a note failed or a file could not be read, so scripts can notice.

**The log** goes to the terminal (and to `LOG_FILE` if set), one line per file and step, with progress like `(2/15)`, and a final `processed 15/15` line. Errors are always logged.

## Recipes

### Check the setup for free

```bash
uv run catcher run pipeline --profile fake --no-review --dry-run
```

### See what is in the inbox

```bash
uv run catcher scan
```

### Try one note for real

```bash
uv run catcher reason "../idea-bucket/inbox/notes/Hello world.md" --profile notes
```

### Test one specific document

```bash
uv run catcher run pipeline --file "Hello world" --profile fake --dry-run     # free
uv run catcher run pipeline --file "Hello world"                              # for real
```

### A small first run, then publish

```bash
uv run catcher run pipeline --limit 3             # commits locally, pushes nothing
git -C ../epiaku-docs log --stat -1               # look at what it wrote
uv run catcher run pipeline --push                # process the rest and push
```

### Run on copies, so the real repos are safe

```bash
git clone --no-hardlinks ../idea-bucket /tmp/ic/idea-bucket
git clone --no-hardlinks ../epiaku-docs /tmp/ic/epiaku-docs
git -C /tmp/ic/idea-bucket remote remove origin
git -C /tmp/ic/epiaku-docs remote remove origin
uv run catcher run pipeline --ideas /tmp/ic/idea-bucket --docs /tmp/ic/epiaku-docs --profile fake
```

Without a remote, nothing can be pulled or pushed by mistake.

### Retry a stalled note

A note that stalled (`stage: deferred` in `output/`) is retried by moving its original from `archive/` back into `inbox/`. It has the same file name in both folders, and it keeps that name, so the next run overwrites the stalled copy.

```bash
mv "../idea-bucket/archive/clippings/20260925-a1b2c3-sell-bundles.md" "../idea-bucket/inbox/clippings/"
uv run catcher run pipeline --file "New chat"      # the original name works too
```

**Possible later:** a helper such as `catcher requeue`, which would move all stalled notes back in one go. It is not built yet.

### Retry a failed note

Find it in `failed/`, read its `.error.txt`, fix the cause, then move the `.md` file back into `inbox/` (same subfolder) and run again.

```bash
mv "../idea-bucket/failed/clippings/20260925-a1b2c3-new-chat.md" "../idea-bucket/inbox/clippings/"
rm "../idea-bucket/failed/clippings/20260925-a1b2c3-new-chat.error.txt"
uv run catcher run pipeline
```

### Redo a note with another model

Copy its file from `archive/` into `inbox/`, then run with the profile you want.

```bash
cp "../idea-bucket/archive/notes/20260928-51bcb0-hello-world.md" "../idea-bucket/inbox/notes/"
uv run catcher run pipeline --profile notes
```

### Bring back a file from `duplicates/`

```bash
mv "../idea-bucket/duplicates/clippings/20260925-a1b2c3-new-chat.md" "../idea-bucket/inbox/clippings/"
```

## Where things end up

- **`inbox/`**: new captures. The only place a run looks for work. A document leaves it when work on it starts.
- **`archive/`**: the original of every document that was worked on, under its calculated name. The text is as captured; two lines were added to its frontmatter (`original_filename`, `calculated_filename`).
- **`output/`**: the working copy (`stage: analyzed` or `deferred`) while it is worked on or stalled, then the final page, the same text as in `epiaku-docs`. A run never reads it.
- **`failed/`**: files that could not be processed, with the reason.
- **`duplicates/`**: earlier snapshots of a longer clip.
- **`epiaku-docs`**: the published pages, under `hugo/content/en/docs/idea-bucket/`. The site still needs your manual `deploy.sh`.

## When something is off

- **Many notes `deferred` with `OPENAI_API_KEY is not set`:** add the key to `.env`.
- **`deferred` with `openai budget reached`:** the key's budget is used up. Raise it, or point the profile at another provider in `profiles.yaml`. The working copies stall in `output/`; move the files from `archive/` back into `inbox/` once the budget is back.
- **`profile problem: unknown LLM profile`:** use `notes`, `clippings`, `youtube` or `fake`. Old names like `claude-sub-now` no longer exist.
- **`deferred` for a YouTube note with `facts unavailable`:** YouTube did not answer from this network. Try again later. You can test one video with `youtube facts`.
- **`not-found  no document named ...`:** the name matches no file in `inbox/`. Check the spelling, or move the file into `inbox/`.
- **A note in `failed/`:** read the `.error.txt` next to it.
- **You want more detail:** run with `--log-level DEBUG`.
