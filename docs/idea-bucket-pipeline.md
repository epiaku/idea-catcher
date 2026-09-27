---
title: "Idea Bucket: Folder Structure & Processing Pipeline"
linkTitle: "Idea Bucket Pipeline"
description: "How idea-bucket organizes notes from inbox to archive, and how each pipeline stage transforms them."
---

This page explains how the `idea-bucket` GitHub repo is structured and how a capture moves through the pipeline from the phone inbox to the final docs page. It covers every folder the pipeline touches, what happens in each stage, and how IDs and paths are computed.

## Repo layout

```text
idea-bucket/
├── README.md
├── _templates/              ← Obsidian note templates
├── inbox/                   ← Phone / Mac writes here
│   ├── notes/               ← Obsidian's "Default location for new notes"
│   └── clippings/           ← Web Clipper's default — everything it captures
├── staging/                 ← Cleaned + enriched, waiting for LLM (flat files)
│   └── <doc_id>.md          ← One file per capture, plus .youtube.json sidecar
├── archive/
│   ├── notes/
│   ├── clippings/
│   └── youtube/
│       ├── <video_id>.md    ← direct YouTube clips
│       ├── <video_id>.youtube.json
│       ├── <video_id>-gemini.md  ← Gemini chat about that video
│       └── <video_id>-gemini.youtube.json
└── <doctype>/archive/superseded/  ← older versions of the same capture
```

Staging is **flat** — no subfolders — because a unique `id` makes every capture findable. Archive mirrors the capture class (not the input subfolder), keeping the vault tidy while grouping related content together.

### Subfolder discipline in `inbox/`

| Subfolder          | What goes in                                                                 | Controlled by                                         |
| ------------------ | ---------------------------------------------------------------------------- | ----------------------------------------------------- |
| `inbox/notes/`     | Short dictated or typed notes                                                | Obsidian's **Default location for new notes** setting |
| `inbox/clippings/` | Web Clipper output (Gemini chats, Claude chats, YouTube links, web articles) | Web Clipper's own **note location** setting           |

The pipeline scans `inbox/` **recursively** so the subfolder only organizes the vault for capture — it does not affect classification. Doc class is determined from the `source` URL domain and (for notes) the explicit `type` frontmatter field.

---

## Pipeline stages

```
┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│   INBOX      │───▶│   STAGING    │───▶│    LLM       │───▶│   PUBLISH    │
│  .md files   │    │  .md files   │    │   reason()   │    │   Hugo pages │
└──────────────┘    └──────────────┘    └──────────────┘    └──────────────┘
      │                   │                   │                    │
  Obsidian Git        derive id           JSON output          Write to
  pushes to           detect class        + validate            epiaku-docs
  GitHub              clean frontmatter   + render markdown
                      add youtube facts
```

Each stage is implemented in a separate module under [`src/catcher/modules/pipeline/`](../../src/catcher/modules/pipeline/):

| Stage          | Module                                                          | Key responsibility                                                |
| -------------- | --------------------------------------------------------------- | ----------------------------------------------------------------- |
| Ingest & Stage | [`staging.py`](../../src/catcher/modules/pipeline/staging.py)   | Scan `inbox/`, derive `id`, classify, enrich, write to `staging/` |
| Process        | [`process.py`](../../src/catcher/modules/pipeline/process.py)   | Run LLM, validate JSON, render Markdown page                      |
| Publish        | [`publish.py`](../../src/catcher/modules/pipeline/publish.py)   | Write page to `epiaku-docs`, move staged file to `archive/`       |
| Orchestration  | [`run.py`](../../src/catcher/modules/pipeline/run.py)           | Coordinate the three stages in one run                            |
| Doc types      | [`doctypes.py`](../../src/catcher/modules/pipeline/doctypes.py) | Registry of document classes, ID derivation, detection            |
| Render         | [`render.py`](../../src/catcher/modules/pipeline/render.py)     | Frontmatter assembly, template rendering, slug generation         |
| Tags           | [`tags.py`](../../src/catcher/modules/pipeline/tags.py)         | Allowed-tag list, normalization, dropped-tag tracking             |
| Validate       | [`validate.py`](../../src/catcher/modules/pipeline/validate.py) | Post-render checks on frontmatter and body                        |

---

## Stage 1 — Ingest & Stage

**Module:** [`staging.py`](../../src/catcher/modules/pipeline/staging.py)  
**Entry point:** `stage_inbox(ideas_repo)`

### What it does

1. **Scans** `inbox/` recursively for `*.md` files (skips dotfiles).
2. **Loads** each file's frontmatter and body.
3. **Detects** the doc type using [`detect()`](../../src/catcher/modules/pipeline/doctypes.py) from [`doctypes.py`](../../src/catcher/modules/pipeline/doctypes.py).
4. **Derives** a stable `id` using [`derive_id()`](../../src/catcher/modules/pipeline/doctypes.py) — falls back to a random hex token if none is present.
5. **Groups** candidates by `id` (handles duplicate captures of the same source).
6. **Wins** the largest body per group (avoids clipped-but-incomplete duplicates).
7. **Writes** a cleaned copy to `staging/<doc_id>.md` with enriched frontmatter:

   ```yaml
   id: Q0pAWZiV2GU
   class: youtube
   captured: "2026-09-24"
   source_file: "inbox/clippings/Some Note.md"
   staged_at: "2026-09-27T21:00:00+02:00"
   # ... original frontmatter fields preserved
   ```

8. **Moves** the original inbox file to `inbox/` (deleted) and any superseded copies to `archive/<doctype>/superseded/`.

### YouTube sidecar

For `youtube` and `youtube-gemini` classes, staging also writes a `.youtube.json` sidecar next to the staged note:

```json
{
  "title": "The Only 7 Books You Need…",
  "channel": "Sandeep Swadia",
  "views": 1400000,
  "likes": 46000,
  "subscribers": 1560000,
  "duration": "PT10M32S",
  "upload_date": "2026-03-18",
  "description": "...",
  "transcript": "...",
  "fetched_at": "2026-09-27T21:00:00+02:00"
}
```

This is fetched by [`fetch_facts()`](../../src/catcher/modules/youtube/facts.py) and cached so subsequent runs don't re-fetch.

### ID derivation rules

| Doc type         | ID source                                   | Example              |
| ---------------- | ------------------------------------------- | -------------------- |
| `note`           | `frontmatter.id` (set by Obsidian template) | `20260924103015`     |
| `ai-chat`        | Last path segment of `source` URL           | `2446cd9c762c9cc9`   |
| `youtube`        | YouTube video ID from `source`              | `Q0pAWZiV2GU`        |
| `youtube-gemini` | `<video-id>-gemini` from first user message | `Q0pAWZiV2GU-gemini` |
| _(missing)_      | Random hex token generated by `new_id()`    | `a7b2c9`             |

The ID is **stable**: re-processing the same capture always produces the same ID, so the pipeline can overwrite the existing page instead of creating a duplicate.

---

## Stage 2 — Process (LLM)

**Module:** [`process.py`](../../src/catcher/modules/pipeline/process.py)  
**Entry point:** `process_note(note, svc, opts)`

### What it does

1. **Resolves the LLM profile** from `opts.profile` → doc type default → global default.
2. **Calls the LLM** with a class-specific prompt and schema via [`reason()`](../../src/catcher/modules/llm/service.py).
3. **Validates** the LLM output against the expected Pydantic schema.
4. **Normalizes tags** against the allowed tag list from [`tags.yaml`](../../src/catcher/modules/pipeline/tags.yaml).
5. **Renders** a Hugo-compatible Markdown page using the class-specific Jinja2 template.
6. **Validates** the rendered page via [`validate_page()`](../../src/catcher/modules/pipeline/validate.py).

### LLM backends

| Backend       | Use case                         | How it runs                                      |
| ------------- | -------------------------------- | ------------------------------------------------ |
| `freellmapi`  | Short notes (free)               | OpenAI-compatible HTTP call to home proxy        |
| `claude-code` | AI chats, YouTube (subscription) | `claude -p --model <model> --output-format json` |
| `fake`        | Tests, local dev                 | Returns canned JSON                              |

The backend is selected per profile (see [`profiles.py`](../../src/catcher/modules/llm/profiles.py)). A failed LLM call leaves the note in `staging/` for retry on the next run.

### YouTube processing

YouTube docs go through an extra path (`_process_youtube`) because they need:

1. **Facts enrichment** — views, likes, subscribers, transcript (written to `.youtube.json` sidecar during staging).
2. **Review** (optional) — an LLM reviewer checks the summary against the fetched facts for accuracy.
3. **Embed** — the rendered page includes a `{{< youtube-lite >}}` shortcode with the video ID.
4. **Sibling link** — if a related capture exists (e.g., a direct YouTube clip and a Gemini chat about the same video), a cross-reference is added.

---

## Stage 3 — Publish

**Module:** [`publish.py`](../../src/catcher/modules/pipeline/publish.py)

### What it does

1. **Writes** the rendered page to the `epiaku-docs` repo at `hugo/content/en/docs/idea-bucket/<doctype>/`.
2. **Overwrites** any existing page with the same `id` (found via [`find_pages_by_id()`](../../src/catcher/modules/pipeline/publish.py)).
3. **Archives** the staged note (and its `.youtube.json` sidecar, if present) to `idea-bucket/archive/<doctype>/`.

### Page naming

Pages are named using a deterministic convention:

```
YYYYMMDD_<doc_id>_<slugified-title>.md
```

Example:

```
20260924_Q0pAWZiV2GU_the-only-7-books-you-need-to-educate-yourself-like-the-top-1.md
```

The **date** and **ID** are fixed. The **slug** comes from the LLM-generated title and can change on re-processing — but since the pipeline finds existing pages by `id` (not by filename), this is harmless.

### Frontmatter assembled by [`render.py`](../../src/catcher/modules/pipeline/render.py)

```yaml
title: "The Only 7 Books You Need to Educate Yourself Like the Top 1%"
description: "A curated list of seven books…"
date: "2026-09-24"
weight: 100
type: docs
id: Q0pAWZiV2GU
tags:
  - tech-note
  - self-education
source: "https://www.youtube.com/watch?v=Q0pAWZiV2GU"
llm:
  profile: claude-sub-evening
  backend: claude-code
  model: claude-sonnet-5
  prompt_version: "2026-09-27"
video_id: Q0pAWZiV2GU
review:
  verified: true
  issues: []
```

---

## Complete transformation diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              IDEA BUCKET REPO                                │
│                                                                              │
│  inbox/                                                                     │
│  ├── notes/                                                                  │
│  │   └── 20260924103015_idea-catcher.md   ← Obsidian template note          │
│  └── clippings/                                                              │
│      └── New chat.md                        ← Web Clipper YouTube export    │
│                                                                              │
│                        ▼  stage_inbox()                                      │
│                                                                              │
│  staging/                                                                   │
│  ├── 20260924103015_idea-catcher.md         ← cleaned, enriched frontmatter  │
│  └── Q0pAWZiV2GU.md                         ← with .youtube.json sidecar     │
│                                                                              │
│                        ▼  process_note()                                     │
│                        ▼  LLM reason()                                       │
│                                                                              │
│  hugo/content/en/docs/idea-bucket/    (epiaku-docs repo)                    │
│  ├── notes/                                                                  │
│  │   └── 20260924_20260924103015_idea-catcher.md                             │
│  └── youtube/                                                                │
│      └── 20260924_Q0pAWZiV2GU_the-only-7-books...md                          │
│                                                                              │
│                        ▼  archive_staged()                                   │
│                                                                              │
│  archive/                                                                   │
│  ├── notes/                                                                  │
│  │   └── 20260924103015_idea-catcher.md                                     │
│  └── youtube/                                                                │
│      ├── Q0pAWZiV2GU.md                                                      │
│      └── Q0pAWZiV2GU.youtube.json                                           │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Doc type registry

The four supported classes are defined in [`doctypes.py`](../../src/catcher/modules/pipeline/doctypes.py):

| Class            | `type` field     | Source hosts                     | Default profile      | Archive folder      | Hugo output folder                          |
| ---------------- | ---------------- | -------------------------------- | -------------------- | ------------------- | ------------------------------------------- |
| `note`           | `note`           | _(any)_                          | `free-fast`          | `archive/notes`     | `hugo/content/en/docs/idea-bucket/notes`    |
| `ai-chat`        | `ai-chat`        | `gemini.google.com`, `claude.ai` | `claude-sub-evening` | `archive/clippings` | `hugo/content/en/docs/idea-bucket/clipping` |
| `youtube`        | `youtube`        | `youtube.com`, `youtu.be`        | `claude-sub-evening` | `archive/youtube`   | `hugo/content/en/docs/idea-bucket/youtube`  |
| `youtube-gemini` | `youtube-gemini` | _(detected from body)_           | `claude-sub-evening` | `archive/youtube`   | `hugo/content/en/docs/idea-bucket/youtube`  |

Detection order (first match wins):

1. Explicit `type` or `class` in frontmatter (from Obsidian template).
2. Domain of `source` URL against known hosts.
3. For Gemini chats: if the first user message contains a YouTube URL → `youtube-gemini`.
4. Fallback → `note`.

---

## Processing flow in one run

[`run.py`](../../src/catcher/modules/pipeline/run.py) orchestrates everything in a single `run_pipeline()` call:

```
run_pipeline(ideas_repo, docs_repo, opts, services)
│
├── stage_inbox(ideas_repo)           # Stage 1: scan → staging
│
├── for each staged note:
│   ├── process_note(note, svc, opts) # Stage 2: LLM → rendered page
│   │
│   ├── write_page(docs_repo, ...)    # Stage 3a: commit to epiaku-docs
│   └── archive_staged(ideas_repo, ..) # Stage 3b: move to archive
│
├── commit_paths(docs_repo, ...)      # Git commit to epiaku-docs main
└── commit_paths(ideas_repo, ...)     # Git commit to idea-bucket main
```

Errors are handled per-note: a failed LLM call or validation error marks the item as `deferred` or `failed` and leaves it in `staging/` for the next run. The run itself continues with the remaining notes.

---

## Files touched per note

| File                        | Source                  | Destination                                                   | Stage   |
| --------------------------- | ----------------------- | ------------------------------------------------------------- | ------- |
| `inbox/notes/<name>.md`     | —                       | `staging/<id>.md`                                             | Stage 1 |
| `inbox/clippings/<name>.md` | —                       | `staging/<id>.md`                                             | Stage 1 |
| `staging/<id>.md`           | —                       | `archive/<doctype>/<id>.md`                                   | Stage 3 |
| `staging/<id>.youtube.json` | —                       | `archive/<doctype>/<id>.youtube.json`                         | Stage 3 |
| —                           | `staging/<id>.md` + LLM | `epiaku-docs/.../idea-bucket/<doctype>/<date>_<id>_<slug>.md` | Stage 3 |

---

## Templates

Each doc type has a Jinja2 template in [`src/catcher/modules/pipeline/templates/`](../../src/catcher/modules/pipeline/templates/):

| Template                                                                      | Class                       | What it renders                                              |
| ----------------------------------------------------------------------------- | --------------------------- | ------------------------------------------------------------ |
| [`note.md.j2`](../../src/catcher/modules/pipeline/templates/note.md.j2)       | `note`                      | Title, description, tags, body                               |
| [`ai-chat.md.j2`](../../src/catcher/modules/pipeline/templates/ai-chat.md.j2) | `ai-chat`                   | Structured sections (decisions, options, open questions)     |
| [`youtube.md.j2`](../../src/catcher/modules/pipeline/templates/youtube.md.j2) | `youtube`, `youtube-gemini` | Video embed, metrics table, summary sections, review verdict |

The templates receive the LLM output as `s` (a Pydantic model), the original `source` URL, and class-specific variables (`facts`, `review`, `sibling`, `embed`).

---

## Proposed improved design: dual-flow folder structure

The current design uses a single flat `staging/` folder and only tracks one version of each file. The improved design below introduces **two parallel folder flows** — one preserving the unmodified original, and one tracking the processed version through each stage. This makes it easier to audit what changed, replay processing, and keep a clean history.

**`processing/` and `archive/` use the same two-subfolder structure as `inbox/`** (`notes/`, `clippings/`) because at ingest time the capture type is unknown — only classification (via frontmatter `type` or `source` domain) determines whether it is a note, clipping, or YouTube video. **The processed stages (`analyzed/`, `staging/`, `published/`) use three subfolders** (`notes/`, `clippings/`, `youtube/`) since the type is known by then. This keeps the original capture flow faithful to the inbox layout while still giving clean type-based grouping for processed files.

### Folder layout (proposed)

```text
idea-bucket/
├── README.md
├── _templates/
├── inbox/                              ← Phone / Mac write here (only 2 subfolders)
│   ├── notes/                          ← Obsidian default new-note location
│   └── clippings/                      ← Web Clipper default
├── processing/                         ← Files currently being worked on (unmodified originals)
│   ├── notes/                            ← same subfolders as inbox — type unknown at ingest
│   └── clippings/
├── archive/                            ← Unmodified originals, retained forever
│   ├── notes/
│   └── clippings/
├── analyzed/                           ← Python enrichment complete (fetched facts, cleaned FM)
│   ├── notes/
│   ├── clippings/
│   └── youtube/
├── staging/                            ← LLM reasoning complete, page ready to publish
│   ├── notes/
│   ├── clippings/
│   └── youtube/
└── published/                          ← Successfully written to epiaku-docs
    ├── notes/
    ├── clippings/
    └── youtube/
```

### Two independent flows from inbox

Both flows start from the same `inbox/` files but diverge immediately — they are completely independent paths.

```
  INBOX                              Processing (read-only)             INBOX
  ──────────                          ──────────────────────            ──────────
  inbox/notes/<file>.md    1. Read   processing/notes/<file>.md   2. Write   analyzed/notes/<file>.md
  inbox/clippings/<file>.md           (don't touch)                           (enriched copy)
        │                                                                  │
        │ 1. Move to processing                                            │
        ▼                                                                  │
  processing/notes/<file>.md                                               │
  processing/clippings/<file>.md                                           │
        │                                                                  │
        │ 2. Move to archive (final)                                       │
        ▼                                                                  │
  archive/notes/<file>.md                                                  │
  archive/clippings/<file>.md                                              │
                                                                           │
                        (processed flow continues independently)           │
                                                                           │ 3. LLM reasoning complete
                                                                           ▼
                                                                     staging/notes/<file>.md
                                                                     staging/youtube/<file>.md
                                                                           │
                                                                           │
                                                                           │ 4. Publish to epiaku-docs
                                                                           ▼
                                                                     published/youtube/<file>.md
```

| Flow                                                         | Purpose                              | What changes                                            | Destination                             |
| ------------------------------------------------------------ | ------------------------------------ | ------------------------------------------------------- | --------------------------------------- |
| **Original**: `inbox → processing → archive`                 | Preserve the exact capture, no edits | None — file is copied then moved                        | `archive/<subfolder>/<file>`            |
| **Processed**: `processing → analyzed → staging → published` | Track enrichment and LLM results     | Frontmatter enriched, facts added, LLM summary rendered | `published/<type>/<file>` + epiaku-docs |

| Flow                                            | Purpose                              | What changes                                            | Moved by                        |
| ----------------------------------------------- | ------------------------------------ | ------------------------------------------------------- | ------------------------------- |
| **Original**: `inbox → processing → archive`    | Preserve the exact capture, no edits | None — file is copied then moved                        | Pipeline on ingest / completion |
| **Processed**: `analyzed → staging → published` | Track enrichment and LLM results     | Frontmatter enriched, facts added, LLM summary rendered | Pipeline at each stage boundary |

### Stage-by-stage movement

| Stage           | Original flow                                                | Processed flow                                                         | What happens                                                                                             |
| --------------- | ------------------------------------------------------------ | ---------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| **Ingest**      | `inbox/<subfolder>/<file>` → `processing/<subfolder>/<file>` | —                                                                      | File moves from inbox to processing; type is unknown until classification                                |
| **Analyze**     | —                                                            | Reads from `processing/<type>/<file>`, writes `analyzed/<type>/<file>` | Python enriches: derives `id`, fetches YouTube facts, cleans frontmatter, writes sidecar `.youtube.json` |
| **LLM process** | —                                                            | `staging/<type>/<file>`                                                | LLM generates title, tags, summary; page is rendered                                                     |
| **Publish**     | —                                                            | `published/<type>/<file>`                                              | Page written to `epiaku-docs`; on success, mark as published                                             |
| **Done**        | Stays in `archive/<subfolder>/`                              | Stays in `published/<type>/`                                           | Both flows are final                                                                                     |

### Benefits

- **Replayability**: Every stage has its own folder. If the LLM output is bad, move the file from `archive/` back to `processing/` and re-run — the original is untouched.
- **Audit trail**: Compare `archive/<type>/<file>` (original) with `staging/<type>/<file>` (what was sent to LLM) to see exactly what changed.
- **Recovery**: If publish fails, `staging/` still has the processed file ready to retry. No data loss.
- **Clear semantics**: Each folder name describes the file's state — no need to guess whether a file in `staging/` is waiting for LLM or already processed.

### Decisions

| #   | Question                                                                          | Decision                                                                                                                                                                      |
| --- | --------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Should `published/` use the Hugo filename convention (`YYYYMMDD_<id>_<slug>.md`)? | **Yes — start using it in `analyzed/`** so the name stays consistent through the entire processed flow.                                                                       |
| 2   | Do we need a `failed/` folder for notes that couldn't be processed?               | **Yes, but one per stage**: `failed-analyzed/`, `failed-staging/`, `failed-published/`. This tells us exactly which stage failed.                                             |
| 3   | What about superseded duplicates?                                                 | **Not needed in original flow** — inbox never has two files with the same name, so no deduplication required there.                                                           |
| 4   | How to handle partial runs (crashes mid-stage)?                                   | **Manual check for now**. A future automation can scan `processing/`, `analyzed/`, `staging/` for files older than 1 day and move them to the appropriate `failed-*/` folder. |
| 5   | Future cleanup for `published/`?                                                  | **Add later** — a maintenance job that reviews published files for staleness or duplicates.                                                                                   |
| 6   | Recovery process for stuck files?                                                 | **Later**: a scheduled job that identifies files in `analyzed/` or `staging/` older than a day (everything should normally reach `published/`) and flags them.                |

```

```
