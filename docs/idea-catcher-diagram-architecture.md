---
title: "Idea Catcher Diagram: The Architecture"
linkTitle: "Diagram: Architecture"
description: "The Idea Catcher in pictures: what runs today (Stage A, one process on the Mac), what will run in the containers on the Proxmox LXC (Stage B and C), the code modules, and the external services that are called."
weight: 61
type: docs
---

This page shows **what runs where** and **what it talks to**. There are three pictures: the setup that exists today, the planned setup in containers, and the code modules inside the program. The design reasons are on [the service architecture page](../idea-catcher-service-architecture/). The path of one document is on [the document flow page](../idea-catcher-diagram-document-flow/), and what happens inside one document on [the processing loop page](../idea-catcher-diagram-processing-loop/).

## 1. As built today (Stage A): one process on the Mac, no containers

```mermaid
flowchart LR
  subgraph devices["Capture devices"]
    phone["iPhone<br/>Obsidian with the Git plugin"]
    macobs["Mac<br/>Obsidian, Git plugin, Web Clipper"]
  end

  subgraph github["GitHub"]
    r_bucket[("idea-bucket<br/>the captures")]
    r_docs[("epiaku-docs<br/>the docs repo")]
  end

  subgraph mac["Your Mac: one command at a time, started by hand"]
    cli["catcher CLI<br/>uv run catcher run pipeline"]
    subgraph code["The Python code"]
      pipeline["pipeline<br/>scan, process, render,<br/>validate, publish, run"]
      llmmod["llm<br/>prompts, profiles, schemas,<br/>one OpenAI-compatible backend"]
      ytmod["youtube<br/>facts, saved facts,<br/>the gap and breaker, checks"]
    end
    clone_b["working copy of idea-bucket<br/>inbox, archive, output, failed, facts"]
    clone_d["working copy of epiaku-docs"]
    state[("Postgres (DATABASE_URL)<br/>YouTube gate and the run lock")]
    cfg[".env and profiles.yaml<br/>keys and settings"]
  end

  subgraph lan["Home network"]
    fla{{"FreeLLMApi<br/>profile notes"}}
  end
  openai{{"OpenAI API<br/>profiles clippings and youtube"}}
  yt{{"YouTube<br/>asked through yt-dlp"}}
  site["Hugo site<br/>you run deploy.sh"]

  phone -->|"push"| r_bucket
  macobs -->|"push"| r_bucket
  r_bucket <-->|"git pull and push"| clone_b
  r_docs <-->|"git pull and push"| clone_d

  cli --> pipeline
  cfg -.-> cli
  pipeline <--> clone_b
  pipeline --> clone_d
  pipeline --> llmmod
  pipeline --> ytmod
  ytmod <--> state
  ytmod <--> clone_b

  llmmod -->|"HTTP, no key"| fla
  llmmod -->|"HTTPS, API key"| openai
  ytmod -->|"about 3 paced requests per video"| yt
  clone_d -.->|"by hand"| site
```

Nothing is installed on a server in Stage A. You start `catcher` yourself, and it runs, commits and stops.

## 2. Planned (Stage B and C): the containers on the Proxmox LXC

```mermaid
flowchart TB
  user["You<br/>curl, scripts, Swagger UI<br/>LAN or VPN, API key"]

  subgraph devices["Capture devices"]
    obs["Obsidian on the phone and the Mac<br/>with the Git plugin"]
  end

  subgraph lxc["Proxmox LXC idea-catcher: Docker Compose, the same file as on the Mac"]
    api["api container<br/>FastAPI: start a run,<br/>read jobs and items"]
    db[("db container<br/>PostgreSQL 17<br/>jobs: the queue<br/>job_items: state per document<br/>job_events: the log<br/>resource_state: YouTube gate")]
    worker["worker container<br/>scheduler loop, pipeline.run,<br/>llm.reason, pipeline.publish<br/>one job at a time"]
    repos[["volume repos<br/>clones of idea-bucket<br/>and epiaku-docs"]]
  end

  github[("GitHub<br/>idea-bucket and epiaku-docs")]

  subgraph lan["Home network"]
    fla{{"FreeLLMApi<br/>its own LXC"}}
  end
  openai{{"OpenAI API"}}
  yt{{"YouTube<br/>yt-dlp"}}
  deploy["Your Mac<br/>deploy.sh builds and publishes the Hugo site"]
  backup["Backups<br/>nightly pg_dump and Proxmox vzdump"]

  obs -->|"push"| github
  user -->|"port 8000"| api
  api -->|"INSERT job and NOTIFY"| db
  worker -->|"claim the next due job<br/>FOR UPDATE SKIP LOCKED"| db
  worker --> repos
  worker -->|"scheduled: pull idea-bucket,<br/>publish: commit and push"| github
  worker -->|"HTTP"| fla
  worker -->|"HTTPS"| openai
  worker -->|"paced, behind the gap and breaker"| yt
  github -->|"you pull epiaku-docs"| deploy
  db -.-> backup
  lxc -.-> backup
```

Both the `api` and the `worker` container use the same image: Python 3.12 with `uv`, Git, and `yt-dlp` with Deno. There is no Node, no Hugo and no Claude Code CLI in the image, because the docs site is still deployed by hand.

**What is new compared with Stage A:** Postgres holds the state of every document and the queue (so the dashboard can show what is waiting, for how long, and what goes first), a worker runs the schedules (pull, process, publish), and an API starts runs and reads results. The YouTube gate state is in Postgres (since 2026-10-04 also for the CLI: there is no gate file any more).

## 3. The code inside the program

```mermaid
flowchart LR
  cli["cli.py<br/>the commands"]

  subgraph pipeline["modules/pipeline"]
    run["run.py<br/>the loop over the inbox"]
    inbox["inbox.py<br/>scan, names, archive,<br/>output, failed, requeue"]
    doctypes["doctypes.py<br/>which class is it,<br/>its default destination"]
    process["process.py<br/>one document, per class"]
    inputs["inputs.py<br/>the LLM input"]
    ctx["tags, glossary, context<br/>the lists and files you edit"]
    render["render.py and templates<br/>the page"]
    validate["validate.py<br/>checks the page"]
    publish["publish.py<br/>writes the page into<br/>notes/ youtube/ or web-clips/"]
  end

  subgraph llm["modules/llm"]
    service["service.py<br/>reason: one call, one retry for bad JSON"]
    backend["backends<br/>OpenAI-compatible client,<br/>retries, budget and limit errors"]
    prompts["prompts and schemas<br/>one prompt per class"]
    profiles["profiles.py<br/>notes, clippings, youtube, fake"]
  end

  subgraph youtube["modules/youtube"]
    access["access.py<br/>saved facts, gate, fetch"]
    gate["gate.py<br/>the gap and the breaker"]
    cache["cache.py<br/>facts per video"]
    facts["facts.py<br/>one yt-dlp extraction"]
    checks["checks.py<br/>free checks, link check"]
  end

  core["core<br/>config, git, files (atomic writes, lock), frontmatter, log, test data"]

  cli --> run
  cli --> process
  run --> inbox
  run --> process
  run --> publish
  run --> core
  run --> access
  inbox --> doctypes
  process --> inputs
  inputs --> ctx
  process --> render
  process --> validate
  process --> service
  process --> access
  process --> checks
  service --> backend
  service --> prompts
  service --> profiles
  access --> gate
  access --> cache
  access --> facts
  facts --> gate
```

## What runs where

| Part | Where | State it keeps |
| --- | --- | --- |
| `catcher` CLI (Stage A) | Your Mac, started by hand | Nothing between runs, except the files in the two repos; the YouTube gate and the run lock are in Postgres (since 2026-10-04) |
| `worker` (Stage B) | LXC, a container | The queue and the document state, in Postgres |
| `api` (Stage C) | LXC, a container | None. It writes jobs into Postgres and reads results from it |
| `db` | LXC, a container | Jobs, items, events, the YouTube gate. Backed up nightly |
| Clones of the two repos | The Mac now, the `repos` volume later | The captures, the archive, the output, the saved facts, the pages |
| FreeLLMApi | Its own LXC on the home network | A pool of free models (it picks a provider per call) |

## The external services

| Service | Used for | How we call it | When it fails |
| --- | --- | --- | --- |
| **GitHub** | The two repos: captures in, pages out | `git pull`, commit, `git push` (one rebase retry on a rejected push) | A failed pull or push is reported; the files are safe on disk |
| **FreeLLMApi** | The `notes` profile: short dictated notes | OpenAI-compatible HTTP, no key needed, at the URL in `FREELLMAPI_URL` | A 5xx, a timeout or a dropped connection is retried up to 5 calls with a growing wait (the provider behind it changes per call); then the document is **deferred** |
| **OpenAI API** | The `clippings` and `youtube` profiles: chats, web clips, YouTube | HTTPS with `OPENAI_API_KEY`, JSON mode | A used-up budget **blocks the backend for the rest of the run** and defers the documents. A 429 or a bad key does the same. A 5xx or a timeout is retried like above |
| **YouTube** (through `yt-dlp`) | The facts of a video: title, description, chapters, counts, transcript | About 3 requests per video, 10 seconds apart, at least 2 minutes (plus jitter) between videos | A 429 or a bot check opens a **breaker** for 6, then 12, then 24 hours. The clips wait in the inbox |
| **Obsidian** (phone and Mac) | Capturing: notes, clips, files | The Git plugin pushes the vault, which is the `idea-bucket` repo | Not our code. A capture is on GitHub as soon as it is pushed |
| **Hugo site** | The published docs site | You run `deploy.sh` by hand | Not part of the service |
