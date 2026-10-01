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
- **To try things without any risk, first make test repos:** `uv run catcher testdata reset` (see [Recipes on the test data](#recipes-on-the-test-data)). It makes fresh copies in `tmp/ic`, from test data that is committed in this repo, and you run the Idea Catcher on them.
- **zsh and `# comments`:** many commands below have a `# comment` after them. A default interactive zsh does not treat `#` as a comment, so the words after it are passed to the command as extra arguments (and a `;` in a comment starts a new command, giving errors like `zsh: command not found: the`). Fix it once by adding `setopt interactive_comments` to `~/.zshrc`, then open a new terminal (or run that line once in the current one). In this page, keep any `;` out of the inline comments.
- Every command below starts with `uv run catcher`. `uv run catcher --help` lists all commands.

## The usual order

0. **Try it on the test data** (whenever you change something, or just want to see it work). Reset the test repos in `tmp/ic`, then run on them. Your real repos are not touched.
1. **Free check.** See what would happen, with no LLM call and no change.
2. **Small real run.** Process three notes for real, and look at the result.
3. **Full run.** Process everything, then push when you are happy.

```bash
uv run catcher testdata reset                                        # 0. fresh test repos in tmp/ic
uv run catcher run pipeline --ideas tmp/ic/idea-bucket --docs tmp/ic/epiaku-docs --profile fake   # 0. run on them
uv run catcher run pipeline --profile fake --dry-run                 # 1. free check (real repos)
uv run catcher run pipeline --limit 3                                # 2. small real run
uv run catcher run pipeline --push                                   # 3. full run and push
```

## The commands

- **`run pipeline`**: the whole flow, in one go. This is the one you use.
- **`scan`**: lists what is in `inbox/` and what a run would do with it: markdown documents and artifacts. It changes nothing.
- **`reason`**: sends one document to the LLM and prints the answer. Nothing is written.
- **`render`**: makes the page for one document and writes it into `epiaku-docs`. No commit.
- **`youtube facts`**: prints the counts and transcript of one YouTube video.
- **`version`**: prints the version.

**A run only looks at `inbox/` to find work.** When work on a document starts, it gets a **calculated file name** (`YYYYMMDD-<short guid>-<title>.md`) and leaves `inbox/`: the original goes to `archive/` and a working copy to `output/`, both under that name. If something temporary goes wrong (the LLM is down, a budget is used up), the working copy stays in `output/` with `stage: deferred` and the reason. The run never reads `output/`, so **to retry, use `--requeue NAME`** (it moves the original from `archive/` back into `inbox/`, clears the stale working copy in `output/`, and runs it again), or move the file back by hand. It keeps its calculated name, so the next run overwrites the stalled copy.

## `run pipeline`: the whole flow

```bash
uv run catcher run pipeline [OPTIONS]
```

What one run does, in order:

1. Reads the documents in `inbox/` (nothing is written yet).
2. Moves earlier snapshots of a longer clip to `duplicates/`. They get no LLM call.
3. For each remaining document, right before its LLM step: gives it its calculated name, copies the original to `archive/` (with two frontmatter lines added), writes a working copy to `output/`, and removes it from `inbox/`. With `--limit` or `--file`, only those documents leave `inbox/`.
4. Sends the document to the LLM.
5. When it is ready, writes the page to `epiaku-docs` and over the working copy in `output/`, under the same calculated name.
6. Moves a document to `failed/` if it cannot be processed for good.
   Files that are not markdown (PDFs, images) are **artifacts**: they are renamed `YYYYMMDD-<guid>-<original name>`, copied to `archive/artifacts/` and to `idea-bucket/artifacts/` in the root of epiaku-docs, and removed from `inbox/`. No LLM is used, and `--limit` does not apply.
7. Leaves the working copy in `output/` with `stage: deferred` if the problem is temporary.
8. Commits both repos. It pushes only when you ask.

### Options

- **`--profile NAME`**: which LLM profile to use for every note. Without it, each kind of note uses its own default (`notes`, `clippings` or `youtube`). Use `--profile fake` for a free run.

  ```bash
  uv run catcher run pipeline --profile fake         # canned answers, costs nothing
  uv run catcher run pipeline --profile clippings    # force the clippings profile for every note
  ```

- **`--limit N`**: process at most N notes. The rest wait for the next run. Use it to control spend.

  ```bash
  uv run catcher run pipeline --limit 3
  ```

- **`--file NAME`** (or `-f`): process **only the named document**. Use it to test one or a few documents. Repeat it for more.

  ```bash
  uv run catcher run pipeline --file "New chat 2"
  uv run catcher run pipeline --file "clippings/New chat.md" --file "YouTube walks" --profile fake
  ```

  - The name can be the file name (`New chat.md`), the name without `.md` (`New chat`), or the path with the subfolder (`clippings/New chat.md`). Upper and lower case do not matter. A file you moved back from `archive/` is found by its calculated name **or** by the name it was captured under.
  - Only those documents are processed. The other files stay in `inbox/`, untouched.
  - **Only `inbox/` is searched.** Files in `output/`, `archive/`, `failed/` and `duplicates/` are never looked at. To run one of them again, move the file into `inbox/` first.
  - **If no document has that name, you get a warning** and nothing is processed for that name: `no document named "typo" found in inbox/`. The command then ends with exit code 1.
  - It works together with `--limit`, which then applies to the named documents.
  - The duplicate check only compares the documents you named. If you name a short clip and not its longer copy, the short clip is processed on its own.

- **`--requeue NAME`**: run a document **again**. It **moves** the archived original from `archive/` back into `inbox/` (same subfolder, same calculated name), deletes everything the earlier run left behind (the working copy in `output/` with its `.youtube.json` facts file, and for a failed note the copy in `failed/` with its `.error.txt`), then processes only that document. So the document is in one place only, `inbox/`, until the run starts on it. Use it to retry a stalled (`deferred`) note, or to redo a published one, for example with another model. Repeat it for more.

  ```bash
  uv run catcher run pipeline --requeue "YouTube walks"
  uv run catcher run pipeline --requeue "YouTube walks" --profile notes
  ```

  - The name forms are the same as for `--file`: the name it was captured under, or the calculated name (`notes/20260930-1f8b47-youtube-walks.md`).
  - It looks in `archive/`, the almost untouched original (only the two file name fields were added). `output/` and `archive/` use the same file name, so either finds the same note.
  - The run then writes `archive/` and `output/` again under the same name, and overwrites the page in `epiaku-docs` (not duplicated). If the run stalls again, the working copy in `output/` is `deferred` again. The page in `epiaku-docs` is only replaced when the new run succeeds.
  - If `inbox/` already holds a file with that name, nothing is moved or deleted: it is reported as `skipped` and the file that is there is processed.
  - An unknown name gives `no document named "..." in archive/` and exit code 1.
  - With `--dry-run` nothing is moved or deleted, and you only see `would_requeue`.
  - It also works for a **failed** note: its original is in `archive/` too, and the copy in `failed/` and the `.error.txt` are removed, so no error message is left behind. Fix the cause first (the `.error.txt` says what it was), or the note fails again.
  - Only `archive/` is searched. A capture that could not even be read (`unreadable`) and `duplicates/` are not found: move those back by hand (see the recipes).

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
  uv run catcher run pipeline --ideas tmp/copy/idea-bucket --docs tmp/copy/epiaku-docs --profile fake
  ```

- **`--log-level LEVEL`**: how much to log. It goes **before** the command name.

  ```bash
  uv run catcher --log-level DEBUG run pipeline --profile fake --dry-run
  ```

### What your example means

```bash
uv run catcher run pipeline --profile fake --dry-run
```

- `--profile fake`: use the fake profile, so no LLM is called.
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
uv run catcher reason "../idea-bucket/inbox/notes/YouTube walks.md" --profile notes
```

This is the best first real test of a profile, because it is one small call.

### `render`

```bash
uv run catcher render DOCUMENT [--docs PATH] [--profile NAME]
```

Makes the page for one document and writes it into `epiaku-docs`. It does not touch the document in `inbox/`, and it does not commit. You can look at the result before you commit anything.

```bash
uv run catcher render "../idea-bucket/inbox/clippings/New chat.md" --profile clippings
uv run catcher render "../idea-bucket/inbox/clippings/A video.md"
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
- **`requeued`** / **`would_requeue`**: `--requeue` moved the original from `archive/` back into `inbox/` (`would_requeue` in a `--dry-run`: nothing moved).
- **`deferred`**: not done, but not lost. The working copy stays in `output/` with `stage: deferred` and the reason. To retry, run it again with `--requeue NAME`, or move the file from `archive/` back into `inbox/`. Typical reasons: a provider is down, a rate limit, a used-up budget, YouTube facts not available, or a missing model setting.
- **`failed`**: could not be processed for good, for example invalid LLM output twice or an invalid page. The note moves to `failed/` with a `.error.txt` that says why. To try it again after fixing the cause, use `--requeue NAME`.
- **`duplicate`**: an earlier snapshot of a longer clip. Moved to `duplicates/`, no LLM call.
- **`skipped`**: left for the next run because of `--limit`, or an artifact over the size limit.
- **`artifact`**: a file that is not markdown was renamed, archived and copied to epiaku-docs (`would_copy` in a `--dry-run`).

The last line shows the counts, and whether both repos were committed and pushed.

**Exit code:** `0` when nothing failed. `1` when a note failed or a file could not be read, so scripts can notice.

**The log** goes to the terminal (and to `LOG_FILE` if set), one line per file and step, with progress like `(2/15)`, and a final `processed 15/15` line. Errors are always logged.

**The version** of the Idea Catcher is the first thing after `run started:` in the log (`version=0.1.0`), and `uv run catcher version` prints it. It is one string, `__version__` in `src/catcher/__init__.py`. Change it there when you release; `pyproject.toml` reads it from that file, so there is nothing else to update.

## Using the test data

Start here. Everything in this section runs on fresh test repos in the `tmp/ic` folder of this project, made from test data that is committed in this repo. Your real `idea-bucket` and `epiaku-docs` are never touched, and the data is always the same, even when your real repos change.

### Set up the test repos

```bash
uv run catcher testdata reset
export IDEAS_REPO=tmp/ic/idea-bucket
export DOCS_REPO=tmp/ic/epiaku-docs
```

`testdata reset`:

1. Deletes `tmp/ic` if it is there. It only deletes a folder that a previous `testdata reset` made. If `tmp/ic` came from somewhere else (for example an old `git clone`), it stops and tells you to remove it yourself: `rm -rf tmp/ic`.
2. Copies the test data into `tmp/ic/idea-bucket` and `tmp/ic/epiaku-docs`.
3. Turns each into a git repo with one commit and **no remote**, so nothing can be pulled or pushed by mistake.

Run it again whenever you want to start over. `--target PATH` makes the repos somewhere else. The default, `tmp/ic`, is in the project root whatever folder you run the command from, and `tmp/` is not tracked by Git.

The two `export` lines make every command in this terminal use the test repos, so the recipes below need no `--ideas`/`--docs`. They win over `.env`. Run `unset IDEAS_REPO DOCS_REPO` when you are done, so a later command does not run on the test repos by accident. `catcher reason` and `catcher render` take a document path directly, so those always need the full `tmp/ic/...` path.

### What the test data is

It lives in `tests/data/` in the idea-catcher repo and holds only the folders the Idea Catcher reads and writes, not the full repos:

- `tests/data/idea-bucket/inbox/`: the captures (notes, clippings, Gemini video chats, a web article, YouTube clips) and one tiny fake PDF (`sample-report.pdf`) to try artifacts.
- `tests/data/epiaku-docs/hugo/content/en/docs/idea-bucket/`: the output folders (`notes/`, `clippings/`, `youtube/`, `web-clips/`). They start empty, each with a `.gitkeep` file because Git does not keep empty folders. The pages you make in `tmp/ic` appear here; the files in `tests/data/` are not needed for a run.
- `tests/data/epiaku-docs/idea-bucket/artifacts/`: the folder in the root of epiaku-docs where artifacts (files that are not markdown) are sent. Empty apart from a `.gitkeep`.

Nothing else from the real repos is needed: no Hugo theme or site config, no `README`, no templates, no `.obsidian`, and none of the result folders (`archive/`, `output/`, `failed/`, `duplicates/`), which the run creates.

**Refreshing it.** Add, remove or replace captures in `tests/data/idea-bucket/inbox/` and commit. The test `test_testdata.py` does not hardcode what is in there: it scans `tests/data/` when it runs and checks that `testdata reset` copies exactly that, so changing the captures or folders does not break it. Keep the `.gitkeep` files, or the empty folders are lost. To keep a set of real data for later, copy the folders you need into `tests/data/` this way.

**The test suite uses the same data.** `test_testdata_run.py` runs the whole pipeline on it with the fake LLM, so a change that breaks the flow on real-looking captures is caught.

### Fill `tmp/ic` from your real repos instead (more or newer data)

The committed test data is a fixed snapshot. When your real `idea-bucket` and `epiaku-docs` hold more data that you want to test with, you can fill `tmp/ic` with clean clones of the real repos instead. Both ways give you the same folders (`tmp/ic/idea-bucket` and `tmp/ic/epiaku-docs`), so every command on this page works the same on either:

```bash
# 1. Remove the earlier test repos (only this folder, nothing else)
rm -rf tmp/ic

# 2. Get the latest from GitHub into your real repos
git -C ../idea-bucket pull
git -C ../epiaku-docs pull

# 3. Make clean clones of the full repos
git clone --no-hardlinks ../idea-bucket tmp/ic/idea-bucket
git clone --no-hardlinks ../epiaku-docs tmp/ic/epiaku-docs

# 4. Cut the clones off from GitHub, so nothing can be pulled or pushed by mistake
git -C tmp/ic/idea-bucket remote remove origin
git -C tmp/ic/epiaku-docs remote remove origin

# 5. Run on them
uv run catcher run pipeline --ideas tmp/ic/idea-bucket --docs tmp/ic/epiaku-docs --profile fake
```

Good to know:

- **Which one to use.** Use the test data (`testdata reset`) for everyday tries: it is small, always the same, and needs no `pull`. Use the clones when you need what is in your real repos today.
- **Only committed files are cloned.** A note that Obsidian created but Git has not committed yet is missing. Commit it first, or copy it into `tmp/ic/idea-bucket/inbox/` by hand.
- **The `pull` in step 2 matters.** The clones come from your local folders, not from GitHub.
- **Step 4 is what makes it safe.** Without a remote, nothing can be pushed by mistake.
- **Switching between the two.** `testdata reset` only replaces a folder it made itself. A folder made with `git clone` is not recognised, so the command stops and tells you to run `rm -rf tmp/ic` first. That is step 1 above, so switching in either direction is one `rm -rf tmp/ic` followed by the other set of commands.
- **Everything in the real repos is copied**, including any results of earlier real runs (`archive/`, `output/` and so on). Look in `tmp/ic/idea-bucket/inbox/` to see what will be processed.
- **To keep a set of real data for later:** copy what you need into `tests/data/` as described under "Refreshing it" in "What the test data is", and commit it.

### Recipes on test data

#### Set up the test repos

```bash
uv run catcher testdata reset
export IDEAS_REPO=tmp/ic/idea-bucket
export DOCS_REPO=tmp/ic/epiaku-docs
```

Each one says what it tests.

#### Check the setup for free

```bash
uv run catcher run pipeline --profile fake --dry-run
```

#### See what is in the inbox

```bash
uv run catcher scan
```

#### Try only the LLM step on one note (nothing is written)

Tests just the LLM call and its JSON check: reads the note, calls the real `notes` profile and prints the JSON. It writes no page, archives nothing, commits nothing and copies nothing to `epiaku-docs`. It works for every class except `youtube`, which needs facts first (use `render`).

```bash
uv run catcher reason "tmp/ic/idea-bucket/inbox/notes/YouTube walks.md" --profile notes
```

#### Run the whole pipeline on one document

Tests the full flow for one document: read it, fetch YouTube facts (`youtube` class only), call the LLM, write the page to `tmp/ic/epiaku-docs`, archive the note in `tmp/ic/idea-bucket` and commit both repos locally. Nothing is pushed.

```bash
uv run catcher run pipeline --file "YouTube walks" --profile fake --dry-run     # free
uv run catcher run pipeline --file "YouTube walks"                              # for real
uv run catcher run pipeline --requeue "YouTube walks"                           # requeue if it failed

uv run catcher run pipeline --file "Start up brain"                             # bigger note
uv run catcher run pipeline --requeue "Start up brain"

uv run catcher run pipeline --file "Notes in het Nederlands"                    # Dutch note
uv run catcher run pipeline --requeue "Notes in het Nederlands


```

#### Try a direct YouTube clip: facts, LLM and page, no commit

A `youtube` document needs its facts (transcript, counts) before the LLM step, so use `render`, not `reason`. `render` fetches the facts, calls the LLM and writes the page into `epiaku-docs`, but leaves the inbox note alone and makes no commit. This calls the real `youtube` profile (OpenAI) and fetches the real transcript from YouTube — it is not free and not a dry run.

```bash
uv run catcher render "tmp/ic/idea-bucket/inbox/clippings/RAG + Langchain Python Project Easy AIChat For Your Docs.md" --docs tmp/ic/epiaku-docs
```

#### Try a Gemini video chat: LLM and page, no commit

Same idea, for the `youtube-gemini` class: Gemini's answer is reformatted into our page format on the `youtube` profile — nothing else. This class makes no YouTube API call at all (no `yt-dlp`, no transcript fetch), so it always works, whatever the state of YouTube's endpoints. Because it needs no facts first, `reason` works for it too, not just `render`.

```bash
uv run catcher render "tmp/ic/idea-bucket/inbox/clippings/RAG + Langchain Python Project Easy AIChat For Your Docs 1.md" --docs tmp/ic/epiaku-docs
```

Both clips are about the same video, so after running both you can open the two pages in `tmp/ic/epiaku-docs/hugo/content/en/docs/idea-bucket/youtube/` and compare them — each links to the other.

#### A small first run, then publish

```bash
uv run catcher run pipeline --limit 3             # commits locally, pushes nothing
git -C tmp/ic/epiaku-docs log --stat -1           # look at what it wrote
uv run catcher run pipeline --push                # process the rest; --push is a no-op here, tmp/ic has no remote
```

#### Send a PDF or an image to epiaku-docs

The test data already has one: `tmp/ic/idea-bucket/inbox/sample-report.pdf`. Just run the pipeline and look for it in `tmp/ic/idea-bucket/archive/artifacts/` and `tmp/ic/epiaku-docs/idea-bucket/artifacts/`. To try your own file, copy it into `tmp/ic/idea-bucket/inbox/` first.

```bash
uv run catcher run pipeline --file "sample-report.pdf"
```

#### Retry a stalled note

```bash
uv run catcher run pipeline                                  # let something stall (e.g. a YouTube video)
ls tmp/ic/idea-bucket/output/clippings/                      # see the stalled file (stage: deferred)
uv run catcher run pipeline --requeue "<the-file>"           # the original name works, or the calculated name
```

#### Retry a failed note

Read the `.error.txt` and fix the cause first, then requeue. The copy in `failed/` and the error file are removed for you.

```bash
ls tmp/ic/idea-bucket/failed/notes/                                 # find it and its .error.txt
uv run catcher run pipeline --requeue "<the-file>"                  # the original name works, or the calculated name
```

#### Redo a note with another model

```bash
uv run catcher run pipeline --requeue "YouTube walks" --profile notes
```

#### Bring back a file from `duplicates/`

```bash
ls tmp/ic/idea-bucket/duplicates/clippings/                          # find it
mv tmp/ic/idea-bucket/duplicates/clippings/<the-file>.md tmp/ic/idea-bucket/inbox/clippings/
```

## Recipes on real repos

The same recipes on your real `idea-bucket` and `epiaku-docs` (siblings of this project). The test-data section above explains each one; only the paths differ. A real run changes your real repos, so do the free `--dry-run` first.

First switch back to the real repos. If you exported the test repos earlier in this terminal, remove those variables so the paths in `.env` are used again (or close the terminal):

```bash
unset IDEAS_REPO DOCS_REPO
printenv IDEAS_REPO DOCS_REPO    # prints nothing when unset; the paths then come from .env
```

Then the recipes:

```bash
# Check the setup for free, then see what is in the inbox
uv run catcher run pipeline --profile fake --dry-run
uv run catcher scan

# Only the LLM step on one note (writes nothing)
uv run catcher reason "../idea-bucket/inbox/notes/YouTube walks.md" --profile notes

# The whole pipeline on one document
uv run catcher run pipeline --file "YouTube walks" --profile fake --dry-run     # free
uv run catcher run pipeline --file "YouTube walks"                              # for real

# A small first run, then publish
uv run catcher run pipeline --limit 3             # commits locally, pushes nothing
git -C ../epiaku-docs log --stat -1               # look at what it wrote
uv run catcher run pipeline --push                # process the rest and push
```

**Send a PDF or an image to epiaku-docs.** Drop the file in `inbox/` (not in `notes/` or `clippings/`) and run the pipeline. It is renamed `YYYYMMDD-<guid>-<original name>` and copied to `archive/artifacts/` and to `idea-bucket/artifacts/` in the root of epiaku-docs. To do it again, move it from `archive/artifacts/` back into `inbox/`: it keeps its name and overwrites the same files. Files over 25 MB (`ARTIFACT_MAX_MB`) stay in `inbox/` with a warning.

**Retry a stalled note** (`stage: deferred` in `output/`) **or redo one with another model:** `--requeue` moves the original from `archive/` back into `inbox/`, clears the stale working copy in `output/`, and runs it again.

```bash
uv run catcher run pipeline --requeue "YouTube walks"                    # stalled or published: run it again
uv run catcher run pipeline --requeue "YouTube walks" --profile notes    # ... with another profile
```

**A failed note:** `--requeue` works for it too. Read the `.error.txt` in `failed/` and fix the cause first. The copy in `failed/` and the error file are removed for you.

```bash
uv run catcher run pipeline --requeue "<the-file>"
```

**A duplicate, or a capture that could not be read:** these are not found by `--requeue`. Move the file back into `inbox/` (same subfolder) and run again. The name is a random guid, so `ls` the folder to find it.

```bash
mv "../idea-bucket/duplicates/clippings/<the-file>.md" "../idea-bucket/inbox/clippings/"
uv run catcher run pipeline --file "<the-file>"
```

## Where things end up

- **`inbox/`**: new captures. The only place a run looks for work. A document leaves it when work on it starts.
- **`archive/artifacts/`**: files that are not markdown, renamed, and the same files are in `idea-bucket/artifacts/` in the root of epiaku-docs.
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
