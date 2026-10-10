---
title: "Idea Catcher: The First Local Test with the API"
linkTitle: "Idea Catcher: First Local Test"
description: "The first end-to-end test on your Mac: reset the test folders in tmp/ic, start the whole stack with one docker compose command, and call every API endpoint from a VS Code page."
weight: 43
type: docs
---

The first end-to-end test on your own machine, with the API. **Two commands, then click through a page in VS Code** (or follow the [recipe with `curl`](#recipe) below). Your real `idea-bucket`, your real `epiaku-docs` and your `catcher-db` container are not touched. No LLM and no YouTube call is made.

## Recipe: The test {#recipe}

**Once:** install the VS Code extension **REST Client** (id `humao.rest-client`) and make sure Docker is running.

```bash
# 1. fresh test repos in tmp/ic (deletes tmp/ic first: see the note below)
uv run catcher testdata reset

# 2. build and start the whole stack: database, migrations, worker, API
docker compose -f compose.yaml -f compose.local-test.yaml up --build
```

Wait until the log says the API is listening and `docker compose -f compose.yaml -f compose.local-test.yaml ps` (in another terminal) shows the worker as `healthy`. The first build takes a few minutes.

3. Open **`tests/manual/catcher-recipe.http`** in VS Code and click **Send Request** above each request, **in order**: health, start a run (request 3, with the `fake` profile, so no LLM and no YouTube), count the jobs (4a to 4d), read the run (5) and the items (6a, 6b). Request 5 uses the job id that request 3 returned, so send 3 first. There is nothing to set up: the page holds the public test key that only works with `compose.local-test.yaml`.

4. **Stop the stack** when you are done. Press `Ctrl-C` in the terminal where it runs (skip this if you started it with `-d`), then:

```bash
docker compose -f compose.yaml -f compose.local-test.yaml down -v
```

`down -v` removes the containers **and** the scratch database. Use `down` without `-v` to keep the database (the jobs and items stay for the next start). The results in `tmp/ic` stay either way. To test again from the start, run the two commands of this recipe again.

To try **every** endpoint, with the error cases (no key, wrong key, bad values, a read-only key, publish, requeue), use the longer page **`tests/manual/catcher-api.http`** instead; it is described under "What you should see" below.

> **`testdata reset` deletes `tmp/ic`** and makes it again from the committed test data in `tests/data`. It only deletes a `tmp/ic` that a previous reset made. To keep your current `tmp/ic`, run `uv run catcher testdata reset --target tmp/ic2` and start the stack with `LOCAL_TEST_DIR=./tmp/ic2` in front of the `docker compose` command. Do not use `--fresh-llm-and-youtube`: it removes the saved LLM replies and YouTube facts.

## Why two compose files {#two-files}

The command has two `-f` options on purpose: `-f compose.yaml -f compose.local-test.yaml`. Docker Compose **merges** the files from left to right, and a later file changes what an earlier one says.

- **`compose.yaml`** is the real stack: `db`, `migrate`, `worker` and `api`. It is what you run for real: it reads your `.env`, clones your repos from GitHub, and publishes the database on port 5432.
- **`compose.local-test.yaml`** is a small **override** with only the differences the test needs (listed in the next section). Everything it does not mention (the image, the services, the healthchecks, the start order) comes unchanged from `compose.yaml`.

So the test runs the same image and the same services as the real stack, and only the data and the settings differ. A single copy of `compose.yaml` for the test would drift away from the real one.

You must give **both** files every time you start, stop or look at the test stack (`up`, `stop`, `logs`, `down -v`, `ps`, `run`). With only `compose.yaml` you would get the real stack with its own settings and keys, not the test: that is why a `401` on every request usually means one of the commands was typed without the second file.

## What compose.local-test.yaml does {#what}

- The worker works **directly on the repos in `tmp/ic`** (mounted into the container), so you see the pages appear in your own folders.
- Your real `.env` is **not passed to the containers**: no LLM keys, no GitHub token. (Compose still reads `.env` for the `${...}` values in `compose.yaml`; the file blanks the token and the remotes, so nothing from it reaches a container.) A run uses the `fake` profile (request 6a), and YouTube is offline (the test data has saved facts and saved LLM replies).
- **No schedule is on.** Nothing runs until you start a run from the page.
- The database has **no port on your Mac**, so it never collides with a Postgres on 5432.
- The API is at `http://127.0.0.1:8000` (change the port with `API_PORT=8001` in front of the `docker compose` command, and `@baseUrl` at the top of the page) with two **fixed test keys**: one with `read` and `run`, one with `read` only. They are public, so use this file only for this test.

## What you should see {#expect}

Checked on 2026-10-09 against the committed test data (43 documents and one PDF in the inbox).

| Request in the page              | What you should see                                                                                                                                                                                                                                                                                                                                |
| -------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1 Health                         | `200`, `{"api": "ok", "database": "ok", "worker": "running"}`                                                                                                                                                                                                                                                                                      |
| 2a, 2b No key, wrong key         | `401` `not authenticated` for both                                                                                                                                                                                                                                                                                                                 |
| 3a, 3b Jobs                      | `200` and an empty list (`"total": 0`)                                                                                                                                                                                                                                                                                                             |
| 3c, 3d Bad query values          | `422` with a message naming the field, never a `500`                                                                                                                                                                                                                                                                                               |
| 4 YouTube gate                   | `200`, state `open`                                                                                                                                                                                                                                                                                                                                |
| 5a Dry run                       | `202` with a `job_id` and `"existing": false` (sent again while it waits: `200`, `"existing": true`, the same id)                                                                                                                                                                                                                                  |
| 5b Read the preview job          | type `pipeline.preview`, status `succeeded` within a few seconds; `result.report.counts` is `{"would_publish": 43, "would_copy": 1}` and `result.report.names.items` lists each document with status `would_publish` and `llm_saved: true`                                                                                                         |
| 6a Real run, profile `fake`      | `202`, a `job_id`, `"existing": false`                                                                                                                                                                                                                                                                                                             |
| 6b The same request again        | `200`, `"existing": true` and **the same** `job_id`, if you send it right after 6a: the run takes only a few seconds. Once it has finished you get a new run: `202`                                                                                                                                                                                |
| 6c Read the run                  | `succeeded` (you may never see `queued` or `running`, it is that quick); `result` has `"staged": 43`, `"artifacts": 1`, `"errors": 0`; `item_counts` is `{"ai-chat": {"published": 4}, "note": {"published": 28}, "web-clip": {"published": 1}, "youtube": {"published": 2}, "youtube-gemini": {"published": 8}}`; one event per item state change |
| 6d, 6e Bad options               | `422`: `extra_forbidden` for `colour`; `limit must be a whole number of 0 or more, not -1`                                                                                                                                                                                                                                                         |
| 7a Jobs again                    | 45 jobs: the `pipeline.run`, one `llm.reason` job per document (43) and the `pipeline.preview`, all `succeeded`                                                                                                                                                                                                                                    |
| 7b, 7c The newest run            | the run of 6a with its events and `item_counts`                                                                                                                                                                                                                                                                                                    |
| 7d Unknown job id                | `404` `job not found`                                                                                                                                                                                                                                                                                                                              |
| 8a Items                         | `"total": 43`, every item `published`, with profile, backend and model `fake`                                                                                                                                                                                                                                                                      |
| 8b Stuck items                   | an empty list                                                                                                                                                                                                                                                                                                                                      |
| 8c One class (`youtube`)         | 2 items                                                                                                                                                                                                                                                                                                                                            |
| 9a Publish                       | `202`, then job `9b` ends **`failed`** with `"error": "no git remote to push to in /data/repos/idea-bucket, /data/repos/epiaku-docs: ..."`. That is expected: the test repos have no remote. Nothing is committed                                                                                                                                  |
| 10a Requeue the first item of 8a | `202`, `"existing": false`                                                                                                                                                                                                                                                                                                                         |
| 10b Read the requeue job         | a `pipeline.run` with `"requeue": ["notes/..."]`, `succeeded`. A requeue has no profile option, so it uses the document's normal profile (`notes`, backend `freellmapi`); on the test data that answer comes from the saved LLM reply, so no call is made                                                                                          |
| 10c Requeue an unknown item      | `404` `item not found`                                                                                                                                                                                                                                                                                                                             |
| 11 Read-only key                 | `403` `this key does not have the run scope`                                                                                                                                                                                                                                                                                                       |
| 12 OpenAPI                       | `200`, the description of every endpoint                                                                                                                                                                                                                                                                                                           |

You can also open `http://127.0.0.1:8000/docs` in a browser (Swagger UI): click **Authorize**, paste the key `local-test-key-not-a-secret-0001` and try the endpoints there.

## What this test does not cover {#limits}

- **Real LLMs, real YouTube and a git remote.** Those are your own hand tests, with your `.env`, one step at a time.
- **Schedules.** They are off here on purpose. See [How to Run Stage B](../idea-catcher-how-to-run-stage-b/).
- **Running the worker and the API on the host** instead of in Docker. See [How to Run the API](../idea-catcher-how-to-run-api/#start).

The API itself, with every endpoint and status code, is described in [How to Run the API](../idea-catcher-how-to-run-api/).

## Recipe: test the local ic data using curl {#curl}

The same test with only a terminal and `curl`, so you can run it by hand, step by step, without VS Code. Nothing here calls an LLM or YouTube: the stack is offline, and the run uses the `fake` profile. Use two terminals: **A** for the stack, **B** for the commands.

The same calls are in the VS Code page `tests/manual/catcher-recipe.http`. Here they are with `curl`. Set these once in terminal B (the key and address are the public test ones of `compose.local-test.yaml`):

```bash
export KEY=local-test-key-not-a-secret-0001
export URL=http://127.0.0.1:8000
ic() { docker compose -f compose.yaml -f compose.local-test.yaml "$@"; }
```

`ic` is a short name for the compose command with both files (a function, so it also works in zsh): `ic up --build` means `docker compose -f compose.yaml -f compose.local-test.yaml up --build`. It exists only in this terminal.

### 1. Reset the test data

```bash
uv run catcher testdata reset
```

Makes fresh test repos in `tmp/ic` (see the note above: it deletes the old `tmp/ic`). The inbox now holds 43 documents and one PDF, and none is processed.

### 2. Run the docker compose

Terminal A:

```bash
ic up --build
```

(Or `ic up -d --build` to run it in the background.) Wait until the worker is healthy, then check in terminal B:

```bash
ic ps                                  # db, worker and api: "healthy"
curl -s $URL/health                          # {"api":"ok","database":"ok","worker":"running"}
```

### 3. Queue the data, without the LLM and without YouTube

**The API call to make is `POST /api/v1/pipeline/runs` with the body `{"profile": "fake"}`.** It queues one `pipeline.run` job. The worker picks it up and processes every document in the inbox of `tmp/ic`:

- `"profile": "fake"` makes the LLM step use the fake backend, so no model is called.
- YouTube is switched off in the stack (`YOUTUBE_OFFLINE=1`) and the test data holds the saved YouTube facts, so no YouTube request is made.

```bash
curl -s -X POST $URL/api/v1/pipeline/runs \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"profile": "fake"}'
# {"job_id":"<id>","existing":false}     (status 202)
```

Keep the `job_id`. If you send the same request again while the run is still queued or running, you get `200`, `"existing": true` and the same `job_id`: no second run is queued.

To look first at what a run would do, without processing anything, queue a read-only preview: `-d '{"dry_run": true}'`. It makes a `pipeline.preview` job and changes no file.

### 4. See how many jobs are queued or finished

**The API call is `GET /api/v1/jobs?status=<status>&limit=1`.** Its answer has a `total` with the number of jobs in that status; `limit=1` keeps the answer short. The statuses are `queued`, `running`, `succeeded`, `failed` and `cancelled`. One line for all of them:

```bash
for s in queued running succeeded failed; do
  printf "%-10s" $s
  curl -s -H "Authorization: Bearer $KEY" "$URL/api/v1/jobs?status=$s&limit=1" \
    | python3 -c 'import sys, json; print(json.load(sys.stdin)["total"])'
done
```

The run needs only a few seconds on the test data. When it has finished you should see about `queued 0`, `running 0`, `succeeded 44` (the `pipeline.run` plus one `llm.reason` job per document: 43) and `failed 0`. Run the loop again while it works to watch `queued` go down and `succeeded` go up.

More detail:

```bash
# this run: status, result with the counts, item_counts per class and status
curl -s -H "Authorization: Bearer $KEY" $URL/api/v1/jobs/<job_id>

# the documents and their status ("total": 43; every item "published")
curl -s -H "Authorization: Bearer $KEY" "$URL/api/v1/items?limit=1"
curl -s -H "Authorization: Bearer $KEY" "$URL/api/v1/items?status=published&limit=1"
```

The pages themselves are in `tmp/ic/epiaku-docs` (see "Look at the result in the files" below).

### 5. Stop the docker compose

In terminal A press `Ctrl-C` (if you started it with `-d`, skip that). Then:

```bash
ic down -v        # removes the containers AND the scratch database
```

Use `ic down` (without `-v`) to keep the database, so the jobs and items stay for the next start. The results in `tmp/ic` stay either way. To run the recipe again from the start, begin at step 1: `testdata reset` gives fresh folders and `down -v` gives a fresh database. (Running step 3 a second time on the same data finds an empty inbox and processes nothing.)

## Look at the result in the files {#files}

The run wrote its result into `tmp/ic`, on your Mac:

```bash
git -C tmp/ic/epiaku-docs status --short       # 43 new pages and the PDF, not yet committed
ls tmp/ic/epiaku-docs/hugo/content/en/docs/idea-bucket/
git -C tmp/ic/idea-bucket status --short       # inbox/ emptied; archive/, llm/ and output/ filled
```

The files are owned by you on the Mac (Docker Desktop maps them to your user), so you can read, change and delete them, and `git` works on them without any setting.

The pages are **committed by the publish job**. The publish over the API fails (no remote), so commit by hand: stop the worker and the API first (only one worker may run, and a by-hand run also runs any waiting job), then publish without a push.

```bash
docker compose -f compose.yaml -f compose.local-test.yaml stop worker api
docker compose -f compose.yaml -f compose.local-test.yaml run --rm worker catcher publish
git -C tmp/ic/epiaku-docs log --stat -1        # the pages in one commit
git -C tmp/ic/idea-bucket log --stat -1        # the emptied inbox and the filled archive in one commit
```

`catcher publish` exits 0 and prints `committed: ideas yes, docs yes` and `pushed: no (without --push)`. It commits **both** test repos (as `idea-catcher-local-test`) and pushes nothing. `run --rm` runs the migrations again first (they are already done, so that is quick) and removes its own container afterwards. Then start the worker and the API again with `docker compose -f compose.yaml -f compose.local-test.yaml start worker api`, or stop everything with `down -v`.

## If something does not work {#problems}

| Symptom                                                                                | Cause and fix                                                                                                                                                                                                                                         |
| -------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The worker stops at start with `... is not a git checkout and IDEAS_REMOTE is not set` | `tmp/ic` was missing when you started the stack. Docker then makes empty folders there, and `testdata reset` refuses to delete a `tmp/ic` it did not make. Run `down -v`, `rm -rf tmp/ic`, then the two commands again.                               |
| `docker compose` says port 8000 is in use                                              | Another program uses it. Start with `API_PORT=8001` and change `@baseUrl` at the top of the page to port 8001.                                                                                                                                        |
| Every request is `401`                                                                 | The page holds the keys of `compose.local-test.yaml`. You started a different stack (for example without `-f compose.local-test.yaml`), which has other keys.                                                                                         |
| `/health` is `503` with `"worker": "none"`                                             | The worker is still starting, or it stopped. Check `docker compose -f compose.yaml -f compose.local-test.yaml logs worker`.                                                                                                                           |
| A job stays `queued`                                                                   | The worker is busy with another job or not running. Follow it with `logs -f worker`.                                                                                                                                                                  |
| The page shows `{{...}}` unresolved                                                    | A chained request ran before the one it needs (for example 6c before 6a, or 10a before 8a). Send the earlier request first.                                                                                                                           |
| 7c or 10a works on the wrong job or item                                               | Its source request (7b, 8a) was sent before the run. Send 7b or 8a again, then 7c or 10a.                                                                                                                                                             |
| Git complains about "dubious ownership" on `tmp/ic`                                    | Not on Docker Desktop for Mac (the files are yours). On Linux the container writes as user 1000: run `sudo chown -R "$(id -u):$(id -g)" tmp/ic`, or trust only that folder with `git config --global --add safe.directory "$PWD/tmp/ic/epiaku-docs"`. |
