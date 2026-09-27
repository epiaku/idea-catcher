---
title: "Idea Catcher: From Phone Note to Docs Page"
linkTitle: "Idea Catcher"
description: "High-level design of the idea-capture pipeline: Obsidian captures, the idea-bucket GitHub repo, a Python + LLM summarizer and publishing into epiaku-docs, with options and strategies for each stage."
---

A structured summary of a long Gemini brainstorm (137 messages, September 2026) on how to catch ideas before they disappear and turn them into readable pages on this site. It was later refined with our own decisions. The raw conversation had many detours, repeated answers and step-by-step click guides. This page keeps the decisions, the options for each part of the workflow and the strategies for building the pipeline. It ends with an [Implementation Reference](#implementation) for building the pipeline.

## 📝 Summary {#summary}

- **Problem:** Ideas for apps, SaaS products and YouTube videos come up on the go and get lost. They need to be captured with near-zero friction and then end up as structured, searchable pages.
- **Chosen workflow:** Capture in **Obsidian** (phone or Mac) → **Obsidian Git** plugin pushes notes to the **idea-bucket** GitHub repo → the **Idea Catcher Service** on Proxmox picks up new notes and summarizes them with an LLM **profile per class** (FreeLLMApi for short notes, Claude Sonnet via `claude -p` in the evening for AI chats and YouTube), with a **reviewer** checking YouTube summaries → writes Hugo pages straight to `main` of `epiaku-docs`, into `hugo/content/en/docs/idea-bucket/<type>/` → you deploy the site to the home Proxmox web server with `deploy.sh`.
- **Key design principles:**
  - **Raw first, process later.** The captured text is never changed. Staging only adds metadata and context around it, so the pipeline can be replayed, tested and improved later.
  - **One stable ID per capture.** Every note carries an `id` in its frontmatter. The output page is named after it, so re-processing a note **overwrites** its page instead of creating a duplicate.
  - **Asynchronous, no real-time need.** A few notes a day, so an hourly or a few-times-a-day batch is enough. This keeps the design simple and robust.
  - **Metadata steers the pipeline.** A `type` field in the note frontmatter, set by Obsidian templates, decides the prompt, the target folder and the archive folder.
  - **Tags automated, links by hand.** The pipeline tags every summary from a fixed tag list. Hugo builds a page per tag. Curated pages link to the tag pages, and to individual ideas that matter.
  - **Python first, AI last.** Deterministic code handles files, names, frontmatter and paths. The LLM only does what needs language understanding: titles, tags, summaries and structure.
- **Chosen build strategy: the Idea Catcher Service, MVP first.** A small Python service on Proxmox (Docker Compose in one LXC) with a Postgres job queue, a worker and a minimal API. The scheduler or an API call starts a run. Notes go from `inbox/` to `staging/`, then each note gets an **LLM job** whose options (a named profile) set the backend and the timing: FreeLLMApi now for short notes, `claude -p` on the subscription in the evening for AI chats and YouTube. Failed LLM jobs retry the next day, with no fallback to a paid API. Every run and note is stored as metrics. See [Idea Catcher Service: Architecture](../idea-catcher-service-architecture/). This replaces the earlier two-stage rocket with a GitHub Action (see [Strategies](#strategies)).
- **Related page:** hosting FreeLLMApi itself (home LXC, cloud options, persistence) is covered in [FreeLLMApi Hosting](../../claude/freellmapi-hosting/). This page covers the whole docs pipeline.
- **Future:** the same pipeline could grow into a cloud-hosted and later a multi-tenant SaaS offering (see [Growth Path](#growth-path)).

## 📑 Table of Contents {#toc}

1. [Summary](#summary)
2. [Goals & Requirements](#goals)
3. [Decisions](#decisions)
4. [End-to-End Workflow](#workflow)
5. [Stage 1: Capture](#capture)
6. [Stage 2: Sync to GitHub](#sync)
7. [Stage 3: Ingest & Stage](#ingest)
8. [Stage 4: Process & Summarize](#process)
9. [Stage 5: Publish to epiaku-docs](#publish)
10. [LLM Choice: Claude API vs. OpenAI API](#claude-vs-openai)
11. [Model Test Suite](#test-suite)
12. [Strategies to Build the Pipeline](#strategies)
13. [Implementation Reference](#implementation)
14. [Risks & Blind Spots](#risks)
15. [Growth Path: From POC to SaaS](#growth-path)
16. [Open Questions](#open-questions)

---

## 🎯 Goals & Requirements {#goals}

| Requirement              | What it means for the design                                                                                            |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------------- |
| **Low-friction capture** | Dictate or type on the phone in seconds. No filenames, tags or folders to think about at capture time.                  |
| **No data loss**         | A note must survive network errors, VPN drops and failed processing runs.                                               |
| **Multiple input types** | Short dictated notes and Gemini chat exports now. Other AI chats, web articles and YouTube links later.                 |
| **Replayable**           | The raw text is always kept, so prompts can be improved and old captures re-processed.                                  |
| **No duplicates**        | Re-processing a capture replaces its page. It does not add a second one.                                                |
| **Correct placement**    | Output lands in the right `idea-bucket/` subfolder for its type.                                                        |
| **Not real-time**        | Processing once an hour or a few times a day is fine.                                                                   |
| **Cheap**                | Runs on the existing home lab (ODROID H4 with Proxmox). Free LLMs first; a small paid LLM bill is acceptable if needed. |
| **Text only (for now)**  | No images or audio attachments in the first version.                                                                    |

The ideas are not sensitive, so sending them to a cloud LLM is fine.

---

## ✅ Decisions {#decisions}

| Topic                         | Decision                                                                                                                                                                                                                                                                                                                                                                             |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Capture tool                  | **Done:** Obsidian with templates, set up and syncing on both the iPhone and the MacBook Air (see the [iPhone](../obsidian-git-iphone-setup/) and [Mac](../obsidian-git-macbook-setup/) setup guides).                                                                                                                                                                               |
| Vault sync                    | **Option A: GitHub is the only sync** for the idea-bucket vault. **Done:** both the iPhone and MacBook Air vaults are on local storage (no iCloud) and each clones/syncs the `idea-bucket` repo independently. Other vaults can stay in iCloud (see [iCloud and Git](#icloud-git)).                                                                                                  |
| Sync to GitHub                | **Done:** Obsidian Git plugin on both devices, into `inbox/notes/` and `inbox/clippings/` of the idea-bucket repo — the Web Clipper saves everything (Gemini, Claude, YouTube, web) into `inbox/clippings/`. Automatic and one-key manual sync configured on both.                                                                                                                   |
| Input types                   | **Notes**: short notes (dictated on the phone), **AI chats** from Gemini or Claude (Web Clipper) and **YouTube links** (share sheet). Web articles later.                                                                                                                                                                                                                            |
| Existing pages                | **Only create new pages or overwrite a page** generated from the same capture. No appending to curated pages.                                                                                                                                                                                                                                                                        |
| Matching a page to its source | A stable `id` in the note frontmatter, reused in the output filename and frontmatter (see [ID & naming](#id-naming))                                                                                                                                                                                                                                                                 |
| LLM                           | **Profiles per class, set per job message** ([details](../idea-catcher-service-architecture/#mvp-llm)): **FreeLLMApi** (now) for short notes, **Claude subscription** via `claude -p` (Sonnet, evening) for AI chats and YouTube. No paid API in the MVP and **no fallback from free to paid**. Failed jobs retry the next day. The [test suite](#test-suite) confirms the profiles. |
| Publishing                    | **Commit straight to `main`** of epiaku-docs                                                                                                                                                                                                                                                                                                                                         |
| Promotion                     | Summaries **stay in `idea-bucket/`** in the Hugo docs. Other pages (`apps/`, `saas/`, `youtube-ideas/`, …) link to them instead of copying them.                                                                                                                                                                                                                                     |
| Linking                       | **Both:** the pipeline adds **tags automatically** from a fixed list. Important ideas are also **linked by hand** from curated pages (see [Tags & links](#tags-links)).                                                                                                                                                                                                              |
| Orchestration                 | **Idea Catcher Service** ([details](../idea-catcher-service-architecture/)): scheduler and API enqueue jobs in Postgres, a worker runs them (stage → LLM job → publish). Replaces the two-stage rocket with a GitHub Action. The docs site deploy stays **manual** with `deploy.sh`.                                                                                                 |

---

## 🔄 End-to-End Workflow {#workflow}

```text
 ┌─────────────────────────────────────────┐
 │ STAGE 1 · CAPTURE                        │
 │ Obsidian (iPhone / Mac)                  │
 │  • dictated or typed notes               │
 │  • Gemini chats via Web Clipper          │
 │  • template adds `type:` and `id:`       │
 └───────────────────┬─────────────────────┘
                     │ saved to vault /inbox/
                     ▼
 ┌─────────────────────────────────────────┐
 │ STAGE 2 · SYNC                           │
 │ Obsidian Git plugin: commit + push       │
 │  → GitHub repo idea-bucket /inbox/       │
 └───────────────────┬─────────────────────┘
                     │ scheduled run (e.g. hourly)
                     ▼
 ┌─────────────────────────────────────────┐
 │ STAGE 3 · INGEST & STAGE                 │
 │ Python job pulls idea-bucket             │
 │  • inbox empty → stop                    │
 │  • make sure every note has an `id`      │
 │  • move /inbox/ → /staging/              │
 └───────────────────┬─────────────────────┘
                     ▼
 ┌─────────────────────────────────────────┐
 │ STAGE 4 · PROCESS & SUMMARIZE            │
 │  • read `type` → choose prompt           │
 │  • LLM: title, tags, summary, structure  │
 │  • Python: frontmatter, path, layout     │
 └───────────────────┬─────────────────────┘
                     ▼
 ┌─────────────────────────────────────────┐
 │ STAGE 5 · PUBLISH & ARCHIVE              │
 │  • write or overwrite page by `id` →     │
 │      epiaku-docs (main)                  │
 │      hugo/content/en/docs/idea-bucket/   │
 │      <type>/                             │
 │  • move /staging/ → /archive/<type>/     │
 │  • push both repos                       │
 │  • rebuild + deploy Hugo site            │
 └─────────────────────────────────────────┘
```

The phone only ever writes to `inbox/`. Because the pipeline moves files out of `inbox/` on GitHub, the next pull in Obsidian clears them from the phone as well. The phone behaves like a one-way outbox.

---

## 📱 Stage 1: Capture {#capture}

### Input types

| Input                        | How it is captured                               | `type` value                                    | Status |
| ---------------------------- | ------------------------------------------------ | ----------------------------------------------- | ------ |
| Short dictated or typed note | New note in Obsidian (iOS dictation or keyboard) | `note`                                          | Now    |
| Gemini chat                  | Obsidian Web Clipper, default template           | Detected from `source` (gemini.google.com)      | Now    |
| Claude chat                  | Obsidian Web Clipper, default template           | Detected from `source` (claude.ai)              | Now    |
| YouTube video                | Obsidian Web Clipper, default template           | Detected from `source` (youtube.com / youtu.be) | Now    |
| Web article / blog post      | Obsidian Web Clipper                             | `web-clip`                                      | Later  |

### Capture tool options

| Option                            | Friction   | Offline         | Notes                                                                                               |
| --------------------------------- | ---------- | --------------- | --------------------------------------------------------------------------------------------------- |
| **Obsidian + templates** (chosen) | Low–medium | ✅ Yes          | Free app, plain Markdown, templates for metadata, Web Clipper for chats. Phone and Mac.             |
| iOS Shortcut → HTTP POST          | Very low   | ❌ No           | One tap or "Hey Siri". Text is lost on network errors unless the shortcut first saves a local copy. |
| iOS Shortcut → GitHub Issue       | Very low   | ❌ No           | Issue = inbox item, closed = archived. Needs a token on the phone and a connection at capture time. |
| Email or Slack message            | Medium     | ✅ Yes (drafts) | Needs a mail/Slack listener with its own auth and maintenance. The original idea; dropped.          |
| Working Copy (iOS Git client)     | Medium     | ✅ Yes          | Paid unlock needed to push.                                                                         |

### Steering the pipeline ("nudges")

<div class="resource-video-grid" style="width: 81%; max-width: 100%; margin: 0;">
	<div class="resource-video-item">
		{{< youtube-lite Q0pAWZiV2GU "The Only 7 Books You Need to Educate Yourself Like the Top 1%" >}}
		<p>🎬 <strong>The Only 7 Books You Need to Educate Yourself Like the Top 1%</strong> — A curated list of seven books that teach how to self-educate with the depth and discipline of the top 1%, used here as inspiration for steering the pipeline.</p>
	</div>
</div>

The pipeline needs to know what a note is. From least to most effort at capture time:

1. **No hint:** the note is treated as `type: note` (fallback).
2. **Spoken prefix:** start the dictation with a keyword, such as "YouTube: …" or "App idea: …". The LLM can use it for tags and title.
3. **Obsidian template (recommended for phone notes):** a template inserts frontmatter with `type` and `id`. Web clips need nothing: the class is detected from their `source` URL.

**Web clips need no custom template.** The Web Clipper's default template already writes the URL into `source`, and the pipeline detects the class from its domain. A real YouTube clip looks like this:

```yaml
---
title: "The Only 7 Books You Need to Educate Yourself Like the Top 1%"
source: ""
author:
  - "[[Sandeep Swadia]]"
published: 2026-03-18
created: 2026-09-24
description: "Subscribe to my newsletter → https://sandeepswadia.beehiiv.com/Take us on…" # cut off
tags:
  - "clippings"
---
Subscribe to my newsletter → …            ← the full video description is in the body
```

**How the pipeline picks the class** (first match wins):

1. An explicit `type` in the frontmatter (from an Obsidian template), if present.
2. The domain of `source`: youtube.com or youtu.be → `youtube`; gemini.google.com → `ai-chat` (Gemini); claude.ai → `ai-chat` (Claude).
3. Anything else → `note`.

Only phone notes need a template. It sets `type: note` and an `id`:

```yaml
---
id: 20260924103015 # {{date:YYYYMMDDHHmmss}}, stable capture ID (see Stage 3)
type: note
created: "{{date}}"
---
```

The `clippings` tag from the Web Clipper is ignored. The pipeline sets its own tags from the fixed list.

Recommended Web Clipper setting: save to `inbox/` with a timestamped note name, because Gemini chat pages often have the tab title "New chat".

---

## 🔁 Stage 2: Sync to GitHub {#sync}

### Chosen setup

- The Obsidian vault is a clone of the **idea-bucket** repo. (In the Gemini session this repo was created as `epiaku/idea-bucket`.)
- The **Git** community plugin by Vinzent03 (`obsidian-git`) commits and pushes. On iOS it uses a built-in JavaScript Git (isomorphic-git), so there is no Git install on the phone and no SSH. It authenticates over HTTPS with a token.
- Use a **fine-grained personal access token** for this single repo with only **Contents: Read and write**.
- New notes and clips go into just **two** `inbox/` subfolders, not the flat `inbox/` root: `inbox/notes/` (dictated notes, Obsidian's _Default location for new notes_) and `inbox/clippings/` (Web Clipper's own default folder — used for **everything** it captures: Gemini, Claude, YouTube, web articles, confirmed by testing a YouTube clip). There is no `inbox/youtube/` — the Web Clipper doesn't route by content type, only by its own default. Stage one scans `inbox/` recursively, so the subfolder only keeps the vault tidy — classification is entirely by the **domain of `source`** (see [Document classes](#doc-classes)), never by which subfolder a file is in. The plugin cannot map the vault root to a subfolder on GitHub, and the repo root also holds files like `README.md`.
- On iOS, set **Auto commit-and-sync interval** to about 10 minutes. It commits, pulls and pushes in one go. Timers only run while Obsidian is in the foreground, so run "Commit-and-sync" by hand when you want a note sent right away.

### Setting up the devices {#device-setup}

**Done:** each device now has its own local (non-iCloud) `idea-bucket` vault, cloned from and synced with GitHub through the Git plugin. iCloud is no longer involved in this vault (see [option A](#icloud-git)). The Mac syncs in the background while Obsidian is running.

**For the exact button-by-button setup** — generating the token, installing the plugin, connecting the vault, the clone/initialize prompts, and turning on auto-sync — see [Obsidian Git Plugin: iPhone Setup](../obsidian-git-iphone-setup/) and [Obsidian Git Plugin: MacBook Air Setup](../obsidian-git-macbook-setup/). This section only covers the overall architecture and the parts specific to this pipeline.

**The phone is a capture device.** It's mostly used to dictate new ideas, so the app is always open when a note is created. It rarely edits existing notes, so the only thing that matters is that **every new note gets committed and pushed** before the app goes to the background — see the one-tap toolbar setup in the [iPhone guide](../obsidian-git-iphone-setup/#auto-sync). A missed push is not lost: the note stays on the phone and goes out with the next sync. Pulls keep the phone tidy too, since stage one moves processed notes out of `inbox/`.

**Setting up a device from scratch** (for reference — this has already been done on both current devices; use it if a device is replaced or a vault needs to be re-created)

On the Mac:

1. **Clone the repo to a local folder:**

   ```bash
   git clone https://github.com/epiaku/idea-bucket.git ~/Obsidian/idea-bucket
   ```

   On desktop the Git plugin uses the Mac's own Git. Log in once with `gh auth login` (or an SSH key), so the credentials are stored in the macOS Keychain and no token is needed in the plugin.

2. **Open that folder as a vault** (_Open folder as vault_). Install and enable the **Git** plugin (Vinzent03). Set the auto commit-and-sync interval to about 5 minutes, and turn on auto pull on startup.
3. **Update the Web Clipper.** Set the vault name to `idea-bucket` (the vault's name, matching the repo and the iPhone vault), and the note location to `inbox/clippings` — its own default folder, kept as-is rather than redirected to a flat `inbox/`.
4. **Check** that a note made on the phone shows up on the Mac after both sync, and the other way round.

If a vault still lives in iCloud, push everything from the iPhone first, then follow the steps above on the Mac, re-clone the vault on the iPhone into local storage (both in the [iPhone guide](../obsidian-git-iphone-setup/)), and only then remove the old iCloud vault from that device's vault list.

**Set up once**

- **Keep device-specific files out of Git.** Obsidian writes window and layout state to `.obsidian/workspace.json` and `.obsidian/workspace-mobile.json`, which change all the time. Add them to `.gitignore`, or the devices will keep conflicting. Templates and plugin settings stay in Git, so they are shared and backed up.
- **Conflicts are rare.** Both devices mostly add _new_ files to `inbox/`, and the pipeline moves them away. A conflict only happens when the _same_ note is edited on both devices before they sync. If the plugin reports one, resolve it on the Mac.
- **Other vaults** (personal notes, Clippings) can stay in iCloud. Only this pipeline vault is out of iCloud.

### iCloud and Git on the same vault {#icloud-git}

Running iCloud sync and the Git plugin on the **same** vault is risky: iCloud also syncs the hidden `.git` folder, which can corrupt the local repo. **Decision: option A — GitHub is the only sync** for the idea-bucket vault. The iPhone vault lives on local storage, and the Mac clones the repo into its own local folder; both run Obsidian Git independently. Other vaults (personal notes, general clippings) can keep using iCloud normally.

Full explanation, the options considered and the trade-offs are now on [Obsidian Git Plugin: iPhone Setup](../obsidian-git-iphone-setup/#icloud-git), together with the iPhone setup steps.

### Sync options compared

| Option                    | Cost        | Feeds the pipeline directly | Notes                                                             |
| ------------------------- | ----------- | --------------------------- | ----------------------------------------------------------------- |
| **Obsidian Git** (chosen) | Free        | ✅ Yes                      | Full version history. Needs a token per device.                   |
| iCloud Drive              | Free        | ❌ No (a device must push)  | Easiest iPhone ↔ Mac sync, but see the `.git` issue above.        |
| Obsidian Sync             | Paid add-on | ❌ No                       | Official, end-to-end encrypted. Still needs a Git push somewhere. |
| Remotely Save plugin      | Free        | ❌ No                       | Syncs to S3, WebDAV, Dropbox or OneDrive, not to Git.             |

### Alternatives: getting the vault to a server or an agent {#vault-access}

The pipeline needs the vault on a **headless home server** (the service's worker), and it helps if **cloud agents** can read it too. These are the options besides the Git plugin (researched September 2026).

**Can agents talk to Obsidian?** Yes, in three ways:

| Method                                                                                 | How it works                                                                                                                                                                                      | Headless Linux server?                                                   |
| -------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| **Plain files**                                                                        | A vault is a folder of Markdown files. Any agent with file access (Claude Code, a Python job) can read and write it.                                                                              | ✅ Yes, once the vault is synced there                                   |
| **[Local REST API plugin](https://github.com/coddingtonbear/obsidian-local-rest-api)** | Community plugin that serves the vault over HTTPS on `127.0.0.1:27124`, with a **built-in MCP server** at `/mcp/`. Agents can list, read, search, write and patch notes, including live metadata. | ❌ Needs the Obsidian desktop app running, and listens on localhost only |
| **[Official Obsidian CLI](https://obsidian.md/help/cli)**                              | Built into Obsidian since 1.12 (generally available February 2026), about 100 commands, meant for scripting and agents.                                                                           | ❌ Controls the desktop app, which must be running                       |

The REST API and the CLI suit an agent working **next to Obsidian on the Mac**. For a server, only the plain-files route is practical.

**Ways to get the vault onto a home server**

| Option                                                                                                                             | Cost                                    | Maturity                                                                                                                                                                                                                | Notes                                                                                                                                                                                                                    |
| ---------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Obsidian Git → GitHub** (chosen)                                                                                                 | Free                                    | Established                                                                                                                                                                                                             | Full history. **Cloud agents can read it natively.** On the phone it syncs only while the app is open.                                                                                                                   |
| **[Obsidian Sync](https://obsidian.md/help/sync/headless) + [Obsidian Headless](https://github.com/obsidianmd/obsidian-headless)** | Paid Sync subscription                  | Official, open beta since February 2026                                                                                                                                                                                 | Command-line sync client (Node 22+) that runs as a background service on Linux, in Docker or on a Raspberry Pi. End-to-end encrypted, no Git or token on the phone. Meant for pipelines, agents and automated workflows. |
| **[Self-hosted LiveSync](https://github.com/vrtmrz/obsidian-livesync)** + CouchDB on Proxmox                                       | Free                                    | Plugin mature; headless server tools are community projects ([livesync-headless](https://github.com/MrYadro/livesync-headless), [obsidian-livesync-headless](https://github.com/tgmstudios/obsidian-livesync-headless)) | Real-time and fully self-hosted, but the most moving parts.                                                                                                                                                              |
| **Remotely Save** plugin → MinIO/S3 or WebDAV on Proxmox                                                                           | Free                                    | Established                                                                                                                                                                                                             | The agent reads the files from the bucket or share. Syncs on a timer or when the app opens.                                                                                                                              |
| **Syncthing**                                                                                                                      | Free on Mac and server, paid app on iOS | Established                                                                                                                                                                                                             | iOS limits background syncing, so it's weak for the phone.                                                                                                                                                               |

**Reading the vault from iCloud?** Only with difficulty. Apple has no iCloud client for Linux.

- **[rclone's `iclouddrive` backend](https://rclone.org/iclouddrive/)** works on Linux through Apple's unofficial web API. The login token keeps asking for two-factor approval again ([rclone #9966](https://github.com/rclone/rclone/issues/9966)), and accounts with Advanced Data Protection have reported errors ([rclone #9658](https://github.com/rclone/rclone/issues/9658)). Too fragile for an unattended server.
- **A Mac as the bridge:** a signed-in Mac always has the vault locally (`~/Library/Mobile Documents/iCloud~md~obsidian/`). An agent on the Mac can read it, or a job can copy it to the server. But the MacBook Air is not always on.

So iCloud works for iPhone ↔ Mac, but not as a server source. That matches [option A](#icloud-git).

**Access from cloud agents**

- **GitHub:** Claude in the cloud reads it natively: Claude Code on the web, scheduled routines and GitHub Actions. The earlier [two-stage rocket](#split-setup) relied on this. The service at home reads it with a plain `git pull`.
- **Obsidian Headless** could also run inside a GitHub Action or cloud container to pull the vault, using the Sync login.
- **MCP from the cloud to a vault at home** would require making home reachable from the internet (e.g. a Cloudflare Tunnel). That conflicts with keeping the home network closed.

**Conclusion**

1. **Keep Obsidian Git → GitHub.** It's free, already set up, has full history, and both the H4 and cloud agents can reach it.
2. **If syncing Git on the phone gets annoying** (tokens, sync only while the app is open): switch the phone to **Obsidian Sync** and run **Obsidian Headless on the H4**. The worker then reads the vault from there, and the rest of the pipeline is unchanged. It costs the Sync subscription and relies on a beta client.
3. **For hands-on work with Claude on your notes** (outside the pipeline): the **Local REST API MCP** or the official CLI on the Mac lets Claude Code read and edit the live vault.
4. **Don't use iCloud as a server source.** Consider Self-hosted LiveSync only for free real-time sync, if running CouchDB is acceptable.

### Templates and settings backup

Keep `_templates/` and the `.obsidian/` settings folder in the idea-bucket repo (make sure `.gitignore` does not exclude `.obsidian/`). A new device then only needs Obsidian, a clone of the repo and a fresh token.

---

## 📥 Stage 3: Ingest & Stage {#ingest}

This is the first part of the `pipeline.run` job in the [Idea Catcher Service](../idea-catcher-service-architecture/#mvp-flow), running on Proxmox. It turns loose inbox files into uniquely identified, cleaned records with context added, in `staging/`, before any AI runs.

### Repo layout (latest version)

```text
idea-bucket/
├── README.md
├── _templates/            ← Obsidian templates
├── inbox/                 ← phone / Mac write here, in one of two subfolders
│   ├── notes/             ← dictated notes (Obsidian's default new-note location)
│   └── clippings/         ← Web Clipper's own default folder — everything it captures:
│                             AI chats, YouTube links, web articles (classified by `source`)
├── staging/               ← cleaned + context added, waiting for their LLM job (flat, unique IDs)
└── archive/
    ├── notes/
    ├── clippings/          ← AI chats (renamed from ai-chats/ to match inbox/clippings/)
    └── youtube/
```

Staging scans `inbox/` **recursively** and flattens everything into `staging/` with a unique ID, so the subfolder only organizes the vault for capture — it doesn't need to mirror the output structure.

### ID & naming {#id-naming}

To overwrite the right page, every capture needs an ID that **never changes**, whether the note is renamed, edited, re-clipped or re-processed. The page filename follows the agreed convention:

```text
YYYYMMDD_<short-id>_<friendly-name>.md
e.g. 20260924_a7b2c9_idea-catcher-pipeline.md
```

The **date** and **short ID** are fixed per capture. The **friendly name** comes from the LLM-generated title, so it can change on a re-run. That is why the pipeline must **find the existing page by the ID, not by the full filename**. It looks for `_<short-id>_` in the filename, or better, an `id` field in the page frontmatter. On a re-run it removes the old file and writes the new one.

Where the ID comes from, per input type:

| Input                                    | Best ID source                                                                                                                 | Can Obsidian generate it?                                                                                                                                                                                                                                                      |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Short note                               | Generated at creation time and stored in the frontmatter `id`                                                                  | **Yes.** The core _Templates_ plugin can insert a timestamp such as `{{date:YYYYMMDDHHmmss}}`, which is unique enough for one person. The _Templater_ community plugin can also add a random short ID. The core _Unique note creator_ plugin names new notes with a timestamp. |
| Gemini chat                              | The **conversation ID in the chat URL** (for example `gemini.google.com/app/2446cd9c762c9cc9`, with the query string stripped) | Not needed. The Web Clipper stores the URL in `source`, and the pipeline derives the ID from it. This is the only option that makes a **re-clipped, longer version of the same chat overwrite** its earlier page. A clip-time ID would create a new page instead.              |
| YouTube video                            | The **video ID** in the URL (e.g. `Q0pAWZiV2GU`)                                                                               | Not needed. Derived from `source`. Clipping the same video again overwrites its page.                                                                                                                                                                                          |
| YouTube summary made in the Gemini chat  | **`<video-id>-gemini`**, from the YouTube URL in the chat's first message                                                      | Not needed. Derived during staging. Kept **next to** the directly clipped page of the same video, so the two can be compared.                                                                                                                                                  |
| Note without an ID (forgot the template) | The pipeline generates one during staging and writes it into the staged copy                                                   | n/a. Replays from `archive/` then keep the same ID.                                                                                                                                                                                                                            |

Does this always find the matching page? Yes, as long as the ID is created once and never regenerated. The remaining edge cases are:

- A note that was **copied** in Obsidian keeps the same ID and will overwrite the original's page. Clear the `id` when duplicating a note.
- A Gemini chat exported **without** a URL has no stable key, so it falls back to a generated ID and becomes a new page.

Staged files in `staging/` and `archive/` use the same ID (for example `20260924-103015_a7b2c9.md`). The archive, the staged file and the published page then always match.

### Processing-state options

| Option                                          | Pros                                                                                | Cons                                                    |
| ----------------------------------------------- | ----------------------------------------------------------------------------------- | ------------------------------------------------------- |
| **Folders: inbox → staging → archive** (chosen) | Visible at a glance, clears the phone inbox, easy replay (move back to `staging/`). | More Git moves per note.                                |
| `processed: true` flag in frontmatter           | No file moves.                                                                      | Files pile up on the phone.                             |
| GitHub Issues (open = inbox, closed = done)     | Native state, event-driven webhooks.                                                | Needs a connection at capture time. No files to replay. |

---

## 🧠 Stage 4: Process & Summarize {#process}

### Document classes & models {#doc-classes}

Captures fall into a few clear classes. Each gets its own prompt, target folder and default LLM profile (a job message can override the profile):

| Class                | `type`                                                     | What it is                                                                                  | Processing                                                                                                                                                                                                                                                                             | Default profile      | Target in epiaku-docs                          | Archive              |
| -------------------- | ---------------------------------------------------------- | ------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------- | ---------------------------------------------- | -------------------- |
| **Notes**            | `note`                                                     | Dictated on the phone. Very short: a reminder or an idea captured before it's forgotten.    | Clean up speech fillers, keep the tone, add a title and tags. Don't pad a two-line idea into a long page.                                                                                                                                                                              | `free-fast`          | `idea-bucket/notes/`                           | `archive/notes/`     |
| **AI chats**         | `ai-chat` (from a gemini.google.com or claude.ai `source`) | Web-clipped Gemini or Claude conversations. Can be very large (100+ messages, ~50K tokens). | Condense: drop fluff and detours, keep decisions and options, keep only the latest version of any code. Output a structured page like this one.                                                                                                                                        | `claude-sub-evening` | `idea-bucket/gemini/` or `idea-bucket/claude/` | `archive/clippings/`  |
| **YouTube**          | `youtube`                                                  | A link to a video.                                                                          | Analyse with the [YouTube summary prompt](/docs/products/youtube/youtube-tech-stack/youtube-summary/#example-prompt-1-best--most-precise): purpose, examples, action plan, tools, tips and how to apply it to the channel. See [YouTube videos](#youtube-videos). Reviewed by default. | `claude-sub-evening` | `idea-bucket/youtube/`                         | `archive/youtube/`   |
| **YouTube (Gemini)** | `youtube-gemini`                                           | A Gemini web chat that ran the YouTube summary prompt, clipped in Obsidian.                 | Python fetches the transcript and counts. The **reviewer** checks Gemini's answer against them and returns it in the YouTube page format ([Reviewer](../idea-catcher-service-architecture/#mvp-review)).                                                                               | `claude-sub-evening` | `idea-bucket/youtube/`                         | `archive/youtube/`   |
| _(missing)_          | –                                                          | A note without a template                                                                   | Treated as a short note, so nothing is dropped.                                                                                                                                                                                                                                        | `free-fast`          | `idea-bucket/notes/`                           | `archive/notes/`     |
| Later: web articles  | `web-clip`                                                 | Web Clipper                                                                                 | Own prompt                                                                                                                                                                                                                                                                             | tbd                  | new folder                                     | `archive/web-clips/` |

The volume is small: a few short notes a day and a few chats and videos a week. Processing them is a bit like a code review of the new files in a repo, which is what a single `claude -p` call on the subscription handles well.

### YouTube videos {#youtube-videos}

The video doesn't need to be watched. **Python fetches the video's data as text**, and Sonnet runs the [YouTube summary prompt](/docs/products/youtube/youtube-tech-stack/youtube-summary/#example-prompt-1-best--most-precise) on that text. The Gemini chat app does this with Google's own internal access to YouTube.

**The clip already contains part of the data**, so Python only has to fetch the rest:

| Prompt needs                        | Where it comes from                                                                                       |
| ----------------------------------- | --------------------------------------------------------------------------------------------------------- |
| Title                               | Clip frontmatter `title`                                                                                  |
| Creator                             | Clip frontmatter `author` (strip the `[[ ]]` Obsidian link brackets)                                      |
| Publish date                        | Clip frontmatter `published`                                                                              |
| Description                         | **Clip body.** The frontmatter `description` is cut off, but the body holds the full text.                |
| Video ID                            | From the `source` URL. Also used as the stable page ID.                                                   |
| Views, likes, subscribers, duration | **Fetched by Python** (`yt-dlp` or the Data API)                                                          |
| Transcript                          | **Fetched by Python** (`youtube-transcript-api`)                                                          |
| Why you clipped it                  | Optional: type a line at the top of the body when clipping. The prompt uses it for "Channel Application". |

In Python, the fetched data comes from these sources:

| Data                                                                  | Source                                                                                              | Notes                                                                                                                                                                            |
| --------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Title, channel, thumbnail (only for links pasted into a plain note)   | **YouTube oEmbed API** (`https://www.youtube.com/oembed?url=…&format=json`)                         | No key. Already used by the `epi-hugo-youtube` scripts. Not needed for Web Clipper clips, which already have title and author. Returns **no** transcript, description or counts. |
| Description, views, likes, channel subscribers, upload date, duration | **`yt-dlp`** (as a Python library, without downloading the video)                                   | No key, one call. Unofficial: it reads YouTube's web pages, so it can break when YouTube changes and needs regular updates.                                                      |
| Same, official route                                                  | **YouTube Data API v3**: `videos.list` (`snippet`, `statistics`) and `channels.list` (`statistics`) | Free API key, 10,000 quota units a day (one lookup costs about 1 unit). Stable, works from anywhere. It can't download transcripts of other people's videos.                     |
| Transcript                                                            | **`youtube-transcript-api`**                                                                        | No key. Unofficial as well. Some videos have no transcript.                                                                                                                      |

Tested from the home network (September 2026) on two videos, including the clip above: `yt-dlp` returned the title, channel, subscribers (1.56M), views (1.4M), likes (46K), upload date, duration and description. The transcript library returned the full English transcript (2,374 words; about 7,900 for a 37-minute video).

```python
import yt_dlp                                   # pip install yt-dlp
from youtube_transcript_api import YouTubeTranscriptApi   # pip install youtube-transcript-api (v1.x)

def fetch_youtube(video_id: str) -> dict:
    url = f"https://www.youtube.com/watch?v={video_id}"
    with yt_dlp.YoutubeDL({"skip_download": True, "quiet": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    try:
        snippets = YouTubeTranscriptApi().fetch(video_id, languages=["en"])
        transcript = " ".join(s.text for s in snippets)
    except Exception:
        transcript = None                       # no transcript: summarize from the description
    return {
        "title": info.get("title"),
        "channel": info.get("channel"),
        "subscribers": info.get("channel_follower_count"),
        "views": info.get("view_count"),
        "likes": info.get("like_count"),
        "upload_date": info.get("upload_date"),
        "description": info.get("description"),
        "transcript": transcript,
    }
```

- `yt-dlp` warns when no JavaScript runtime is installed, and says some formats may be missing. Metadata still works, but installing **Deno** in the container keeps it reliable.
- The counts are a snapshot. Store the fetch date with them (prompt step 4, "Metrics As Of").

Also:

- **Metrics come from Python, never from the LLM.** An LLM would make up views, likes and subscriber counts. Python fills them into the template's metrics table.
- **Embed with the site's shortcode.** Prompt step 11 asks for a thumbnail link. On this site the template renders the video with the `youtube-lite` shortcode instead, following the `epi-hugo-youtube` conventions.
- **No transcript available:** summarize from the title and description only, and mark the page "no transcript".
- **Where to fetch: at home.** `yt-dlp` and transcript requests work from the home network (tested), but YouTube often blocks them from cloud IPs such as GitHub Actions runners. So the worker on Proxmox fetches them during staging and writes them into the staged note (see [Processing flow](../idea-catcher-service-architecture/#mvp-flow)).

### Manual route until the pipeline runs {#manual-route}

The pipeline above is designed, not running yet. Until it is, a YouTube summary in this format has to be produced by hand. This happened once (September 2026, on the [Justin Sung productivity video](../../youtube/productivity-is-hard-until-you-build-systems-like-this/)) using Claude Code directly, and it took about **17 minutes** — worth recording why, since Gemini's own web chat does the same thing in about 10 seconds.

**Why the difference:** Gemini's web chat has **native video understanding** — it can ingest a YouTube URL directly and watch/process the video server-side in one model call. Claude Code has no equivalent, so getting the same grounding meant assembling the content piece by piece through browser automation instead: an oEmbed call for title/author, navigating the live YouTube page to scrape the description and metrics, a failed attempt at the on-page transcript panel (stuck loading spinner), a fallback to inspecting network requests for YouTube's internal caption API and fetching that directly, and only then writing the file. Each of those is a separate tool round-trip; none of it is inherent to the task, it's overhead from not having a direct video-understanding pipeline.

**Three options going forward:**

| Option                                              | What it needs                                                                                                                                                                                                                                          | Notes                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| --------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **1. Gemini API, called directly**                  | A Gemini API key, the `google-genai` SDK. A small script (~20 lines): pass the YouTube URL + the [YouTube summary prompt](/docs/products/youtube/youtube-tech-stack/youtube-summary/#example-prompt-1-best--most-precise), save the returned markdown. | Same native video understanding as the web chat, so no scraping needed and no 17-minute detour for the _content_ analysis. **Cannot replace `yt-dlp`**, though: it only sees the video stream (audio + frames), never the surrounding webpage, so it has no access to view/like/subscriber counts at all — those have to keep coming from `yt-dlp`/the Data API regardless of which summarizer is used. See [Gemini API video understanding](#gemini-video-api) below for the verified details, and the comparison right after it. |
| **2. Automate the Gemini web chat via browser**     | Browser automation, a signed-in Gemini session.                                                                                                                                                                                                        | Not recommended: fragile against UI changes, likely against Gemini's consumer ToS for automated use, and no real speed win over calling the API directly.                                                                                                                                                                                                                                                                                                                                                                          |
| **3. Paste Gemini's output, have Claude format it** | Nothing new — works today.                                                                                                                                                                                                                             | Run the prompt in Gemini web chat yourself (fast), paste the markdown output to Claude Code, which saves it to the right file with correct frontmatter and rebuilds the site to verify. Useful right now, but redundant with the capture flow below once that's the habit.                                                                                                                                                                                                                                                         |

**The Obsidian route we can already use today**, without waiting for the pipeline or a Gemini API integration: run the video through Gemini's web chat with the YouTube summary prompt, then clip that Gemini chat page with the **Obsidian Web Clipper** into `inbox/clippings/`, like any other AI-chat capture. The service detects it as a **`youtube-gemini`** doc (a Gemini `source` whose first message contains a YouTube URL): it fetches the real transcript and counts, and the **reviewer** checks Gemini's answer against them before publishing (see [Reviewer](../idea-catcher-service-architecture/#mvp-review)). This gets the summary into the idea-bucket vault immediately, using infrastructure that's already set up, even though nothing will _process_ it into a docs page until Stage 3/4 actually runs. It also means the manual step (option 3) is only needed for a summary you want published on the site **right now**, before the pipeline exists — otherwise, clip and let it sit in the inbox for the pipeline to catch up to later.

#### Gemini API video understanding, verified {#gemini-video-api}

<div class="resource-video-grid" style="width: 81%; max-width: 100%; margin: 0;">
	<div class="resource-video-item">
		{{< youtube-lite 9hE5-98ZeCg "Building with Gemini 2.0: Multimodal live streaming" >}}
		<p>🎬 <strong>Building with Gemini 2.0: Multimodal live streaming</strong> — A live demo of Gemini 2.0's multimodal capabilities, showing how the model can understand and respond to real-time audio, video, and text streams — the capability under test for the idea-catcher pipeline's video understanding stage.</p>
	</div>
</div>

Checked directly against the live docs (September 2026), since this is the option future automation would build on:

- **Docs:** [ai.google.dev/gemini-api/docs/video-understanding](https://ai.google.dev/gemini-api/docs/video-understanding)
- **How it works:** the YouTube URL is passed directly as part of the request content next to the text prompt — Gemini fetches and processes the video **server-side**, no separate download or transcript-fetch step:

  ```python
  from google import genai

  client = genai.Client()
  interaction = client.interactions.create(
      model='gemini-3.8-flash',
      input=[
          {"type": "text", "text": "Please summarize the video in 3 sentences."},
          {"type": "video", "uri": ""}
      ]
  )
  print(interaction.output_text)
  ```

  This is a newer API surface (`client.interactions.create`) than what older references describe (`client.models.generate_content`) — check the docs page directly before writing real code, since it's marked preview.

- **Does it return a transcript? Not as a dedicated feature.** There is no separate "get transcript" call. But since the model processes the video's actual audio (not a fetched captions file), it can produce a full transcript **on request in the prompt** — e.g. "give me the full transcript of spoken words, with timestamps" — as part of the same call that also summarizes it. This is one call doing what our current design splits across `yt-dlp` + `youtube-transcript-api` + a separate summarizing LLM call.
- **Capabilities:** summarize, answer content questions, describe visual and audio events (not just speech), reference specific moments by `MM:SS` timestamp, multi-turn follow-ups with the video context preserved.
- **Limits:**
  - **Public videos only** — no private or unlisted videos.
  - **Free tier:** capped at 8 hours of YouTube video processed per day. **Paid tier:** no length cap.
  - **Videos per request:** 1 on models before Gemini 2.5; up to 10 per request on Gemini 2.5+.
  - **Still in preview** — currently free on any tier, but Google states pricing and rate limits are "likely to change."

#### Gemini API vs. `yt-dlp` + `youtube-transcript-api`: which is actually more deterministic {#deterministic-comparison}

These solve different parts of the job, and mixing them up is the mistake to avoid:

| Need                                | `yt-dlp` + `youtube-transcript-api` (chosen, [above](#youtube-videos))                                                                                                       | Gemini video-understanding API                                                                                                                                                                                                                       |
| ----------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Views, likes, subscribers           | **Deterministic.** Exact numbers straight from YouTube's page/API, same input → same output.                                                                                 | **Cannot get these at all.** The model only sees the video stream (audio + frames), never the surrounding webpage — no amount of prompting recovers a subscriber count that isn't in the video itself.                                               |
| Transcript                          | **Deterministic.** Pulls YouTube's own stored captions file verbatim — the exact same text every run.                                                                        | **Non-deterministic.** Regenerates speech-to-text from the audio itself on each call; wording, punctuation and minor details can vary run to run, and it can hallucinate on unclear audio. Useful as a fallback when a video has no captions at all. |
| Summary, purpose, tips, action plan | Needs a **separate LLM call** on top of the fetched transcript/description (Claude, Gemini text, FreeLLMApi — any of the already-compared [LLM options](#claude-vs-openai)). | **One call does it all** — video in, structured summary out. Fewer moving parts, but the summary's factual grounding is only as good as the model's own video understanding, not a checkable source text.                                            |

**Conclusion: keep the already-decided design** — `yt-dlp` for metrics (the only reliable source for those, full stop) and `youtube-transcript-api` for the transcript (exact, reproducible, and lets a human or a test suite check the summary against the real source text) — and only fall back to Gemini's video understanding for videos that have **no captions available**, where there's no deterministic transcript to fall back on anyway. Gemini (or any other LLM) still does the actual summarizing step in both cases, per [Document classes](#doc-classes).

### Division of work: Python vs. LLM

| Strategy                                | How it works                                                                                                                                                         | Reliability                                        |
| --------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------- |
| AI does everything                      | Send the raw note, get back a complete Hugo page including frontmatter and path.                                                                                     | Risk of invalid frontmatter or made-up paths.      |
| **Python first, AI last** (recommended) | Python handles the ID, filename, paths and frontmatter. The LLM returns only structured JSON (title, tags, summary, body). Python validates it and renders the page. | High: code guarantees valid Hugo syntax and paths. |

The small local `needle` model is **no longer an option**. [cactus-compute/needle](https://github.com/cactus-compute/needle) is a tiny edge model (Needle 3: 121M parameters, 8–29 MB) for tool calling, structured extraction and embeddings on devices. It gives up general reasoning, has no server or API and no stated context length, so it cannot summarize a 100-message Gemini chat. Classification is already handled by the `type` field anyway.

### LLM options

The ideas are not sensitive, so privacy is not a deciding factor. **Decision: start with FreeLLMApi**, which already runs on the H4. The Gemini chats are long, so the main risk is that free models lack the context window or summarizing quality. If so, move to a paid model.

| Option                                                         | How you pay                                            | Where it can run                                                                                                                        | Fit                                                                                                                                 |
| -------------------------------------------------------------- | ------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| **Claude via subscription (Pro/Max)**                          | Monthly subscription, usage counts toward its limits   | **GitHub Action** (official Claude Code action, which accepts a subscription token), or Claude Code in headless mode on the Proxmox LXC | Strong at long summaries. No per-token bill. Unattended, scheduled use via `claude setup-token` is officially supported ([verified](#leftover-usage)), as long as only the official `claude` binary calls it. |
| **Claude API** (Anthropic Console)                             | Pay per token, billed separately from any subscription | Anywhere: Python on Proxmox, GitHub Action                                                                                              | Most flexible. At a few notes a day the cost stays small. A cheaper model can handle short notes and a stronger one the long chats. |
| OpenAI / Gemini API                                            | Pay per token                                          | Anywhere                                                                                                                                | Comparable. Gemini has very large context windows, which suits long chat exports.                                                   |
| **FreeLLMApi** (home proxy to free tiers), **starting choice** | Free                                                   | Home LAN only                                                                                                                           | Fine for short notes. Test with real long Gemini chats: rate limits, context size and uneven quality are the risks.                 |

Keep the LLM call behind one small interface in the Python code, so the provider and model can be chosen **per type** in config. Switching from FreeLLMApi to a paid API is then a config change, not a rewrite. For the paid options, see [LLM Choice: Claude API vs. OpenAI API](#claude-vs-openai). See [LLM backends](#llm-backends) for the switchable backend (`freellmapi` / `openai-api` / `anthropic-api` / `claude-code`).

---

## 🚀 Stage 5: Publish to epiaku-docs {#publish}

### Writing to the docs repo

**Decision: commit straight to `main`.** It is the simplest option, and a bad page can be fixed by re-running the note (thanks to overwrite-by-ID) or with a Git revert.

| Option                         | Pros                                                | Cons                                            |
| ------------------------------ | --------------------------------------------------- | ----------------------------------------------- |
| **Commit to `main`** (chosen)  | Simplest. Pages appear on the next build.           | A bad LLM output lands on the site until fixed. |
| Branch + PR with auto-merge    | Hugo build check before merge. Audit trail per run. | Needs a CI workflow and a bot identity.         |
| Branch + PR with manual review | Full control.                                       | Adds friction, and ideas pile up unreviewed.    |

A cheap safety net without PRs: the MVP **validates each page in Python** (frontmatter, required fields, allowed tags, known shortcodes) before writing it. A full Hugo build check before pushing is a [future addition](../idea-catcher-service-architecture/#f-other).

### Tags & links {#tags-links}

Every summary is connected to the rest of the site in two ways:

- **Tags (automatic).** The pipeline adds `tags` to the page frontmatter. Hugo builds a list page for each tag (e.g. `/tags/app-idea/`) that always shows every page with that tag, and shows the tags on each page. This already works on the site: pages tagged `clippings` appear on `/tags/clippings/`.
- **Links (by hand).** Each curated section links **once** to its tag page ("📥 All captured app ideas"). When an idea becomes real work, link that summary directly, with a sentence of context.

#### Tag rules

- **Fixed list.** The LLM may only pick tags from the list below. Python drops anything else and logs it as a _suggestion_ to review. That prevents near-duplicates such as `app-idea`, `app-ideas` and `apps`, which would each get their own page.
- **One source of truth.** Keep the list in one file (for example a Hugo data file in epiaku-docs) that both the pipeline prompt and the validation read. Adding a tag = one line in that file.
- **Format:** lowercase, kebab-case, singular (`app-idea`, not `App Ideas`).
- **Per page:** exactly **one idea-type tag**, plus **1–4 topic tags**, plus optionally a **project tag**.
- **Capture nudge:** a note may already contain `tags:` from Obsidian. These go through the same validation.

#### Starter tag list

Derived from the current sections and pages of epiaku-docs.

**Idea type** (exactly one):

| Tag                    | Meaning                                              | Curated page that links to the tag page |
| ---------------------- | ---------------------------------------------------- | --------------------------------------- |
| `app-idea`             | Phone or web app idea                                | `apps/ideas/`                           |
| `saas-idea`            | SaaS service idea                                    | `saas/ideas/`                           |
| `youtube-idea`         | Video idea for one of the channels                   | `youtube-ideas/`                        |
| `digital-product-idea` | Course, template, e-book or other digital product    | `digital-products/`                     |
| `ai-influencer-idea`   | AI persona or influencer concept                     | `ai-influencers/`                       |
| `todo`                 | Something to do, not an idea                         | `todo/`                                 |
| `tech-note`            | Learning, tool or architecture note (like this page) | `tech-stack/`                           |
| `strategy`             | Business direction, positioning, freelancing         | `strategy/`                             |

**Topics** (1–4):

| Group               | Tags                                                                                                                                       |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| AI & coding         | `ai-agents`, `llm-models`, `freellmapi`, `claude-code`, `codex`, `harnesses`, `skills`, `token-usage`, `vibe-coding`, `ai-coding-workflow` |
| Knowledge & tooling | `second-brain`, `obsidian`, `hugo`, `automation`, `home-lab`                                                                               |
| YouTube             | `tech-channel`, `senior-wisdom`, `content-creation`, `script-writing`, `video-production`, `youtube-growth`                                |
| Business            | `marketing`, `monetization`, `online-courses`, `crm`, `instagram`, `freelancing`, `bookkeeping`, `administration`                          |

**Projects** (optional, 0–1): names of concrete ideas that keep coming back, for example `idea-catcher` or `yummystream`. The list starts small. A new project tag is added by hand when a name shows up in the suggestions log a few times.

### Rebuild and deploy

| Option                                         | Latency                 | Notes                                                                                                    |
| ---------------------------------------------- | ----------------------- | -------------------------------------------------------------------------------------------------------- |
| **Manual `deploy.sh`, as today** (chosen)      | When you run it         | Pull `epiaku-docs` on the Mac, then run `./.github/workflows/deploy.sh`. The service only commits pages. |
| Pipeline runs the deploy at the end of its run | Right after processing  | The job already has the repo. Needs Hugo, PostCSS and an SSH key in the service. Not chosen.             |
| Cron `git pull` + `hugo` on the web server     | Up to the cron interval | Works for every strategy, including GitHub Actions.                                                      |
| GitHub webhook → local listener                | Seconds                 | Needs an endpoint reachable from GitHub, which conflicts with the closed home network.                   |

---

## 🤖 LLM Choice: Claude API vs. OpenAI API {#claude-vs-openai}

If FreeLLMApi is not good enough, these are the two main paid options. Prices are per 1 million tokens, standard tier, as of September 2026. Check the pricing pages before deciding.

### Models that fit this use case

| Tier                         | Claude (Anthropic)              | Input / output | OpenAI                   | Input / output |
| ---------------------------- | ------------------------------- | -------------- | ------------------------ | -------------- |
| Cheap and fast (short notes) | Claude Haiku 4.5 (200K context) | $1 / $5        | GPT-6 Luna (1M context)  | $0.10 / $0.50  |
| Mid-tier (long Gemini chats) | Claude Sonnet 5 (1M context)    | $2 / $10       | GPT-6 Sol (1M context)   | $2 / $10       |
| Top tier                     | Claude Opus 5.5 (1M context)    | $4 / $20       | GPT-6 Astra (1M context) | $10 / $50      |

Both offer a **Batch API at 50% off**. Batch jobs finish within hours instead of seconds, which suits this pipeline because nothing is urgent.

### Estimated monthly cost

Assumptions: the raw Gemini export this page was built from is about 190 KB, roughly 50–55K input tokens, producing a page of about 10K output tokens. Monthly volume is estimated at 2 Gemini chats a week (~9 a month) and 3 short notes a day (~90 a month at ~2K input and 0.5K output each). That totals about 0.7M input and 0.14M output tokens a month.

| Setup                                      | Claude             | OpenAI          |
| ------------------------------------------ | ------------------ | --------------- |
| Cheap tier for everything                  | ~$1.35 (Haiku 4.5) | ~$0.15 (Luna)   |
| Mid-tier for everything                    | ~$2.70 (Sonnet 5)  | ~$2.70 (Sol)    |
| Top tier for everything                    | ~$5.40 (Opus 5.5)  | ~$13.50 (Astra) |
| Mixed: cheap for notes, mid-tier for chats | ~$2.40             | ~$2.00          |

Models that "think" before answering bill that reasoning as output tokens, so real costs can be somewhat higher. Halve the numbers with the Batch API. At this volume, **price is not the deciding factor** for any setup except the top tiers.

### Comparison

| Criterion              | Claude API                                                                                                                                                               | OpenAI API                                                                                                                          |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------- |
| Price                  | Same as OpenAI at mid-tier. Cheaper at the top tier (Opus 5.5 vs Astra).                                                                                                 | Much cheaper at the low tier (Luna).                                                                                                |
| Speed                  | Not relevant here: the job runs in the background every hour or few hours.                                                                                               | Same.                                                                                                                               |
| Context size           | 1M tokens (Haiku 4.5: 200K). All easily fit a full Gemini export.                                                                                                        | 1M tokens on all GPT-6 models. Requests above 272K input tokens cost double, which is not a concern here.                           |
| Structured JSON output | Yes: schema-enforced structured outputs                                                                                                                                  | Yes: schema-enforced structured outputs                                                                                             |
| Summary quality        | Both mid-tier models are strong at long-document summarization. Quality differences depend on the task, so **test on our own exports** instead of relying on benchmarks. | Same.                                                                                                                               |
| Fit with FreeLLMApi    | Needs its own client (the official Anthropic SDK). The pipeline needs a second backend next to the FreeLLMApi one.                                                       | FreeLLMApi is OpenAI-compatible, so the **same client works for both**. Switching is only a change of base URL, key and model name. |
| Subscription route     | The Claude Code GitHub Action can run on a Pro/Max subscription (strategy B), so there is no per-token bill.                                                             | The ChatGPT subscription and the API are billed separately.                                                                         |
| House style            | Claude Code can reuse the repo's `epi-hugo-*` skills when running as an agent (strategy B).                                                                              | Style has to be in the prompt.                                                                                                      |

### Recommendation

1. **Short notes:** use the cheap tier. GPT-6 Luna is almost free. Haiku 4.5 is fine too.
2. **Long Gemini chats:** Claude Sonnet 5 and GPT-6 Sol cost the same, so pick on quality. Run the same ~10 real exports through both, next to FreeLLMApi, and compare the pages.
3. **Easiest code path:** OpenAI, because it shares the client with FreeLLMApi.
4. **Best route if you want to keep a Claude subscription and use the repo's skills:** Claude, through the GitHub Action (strategy B).
5. **Try the top tier only if** the mid-tier output misses important decisions. Opus 5.5 costs about $5 a month at this volume.

### Claude subscription vs. Claude API

|                   | Subscription token                                     | API key                                                                                |
| ----------------- | ------------------------------------------------------ | -------------------------------------------------------------------------------------- |
| How to get it     | `claude setup-token` (Pro/Max/Team login)              | [console.anthropic.com](https://console.anthropic.com), separate pay-as-you-go billing |
| Works in          | **Claude Code only**: CLI, GitHub Action, routines     | Any tool: Python SDK, apps                                                             |
| Extra cost        | $0, within the subscription's usage limits             | Per token (see the estimate above)                                                     |
| Limits            | Shared with personal use                               | Separate                                                                               |
| Efficiency        | Each call also carries Claude Code's own system prompt | Only your prompt and the note                                                          |
| Structured output | Parse the JSON from the reply                          | Native structured outputs                                                              |
| Batch discount    | No                                                     | 50%                                                                                    |
| Owner             | One person's account and token                         | Organization key, can be rotated                                                       |

Using a subscription login in third-party tools is not allowed, so a subscription only works through Claude Code (the `claude-code` backend or the GitHub Action).

### Making free models work (FreeLLMApi)

Free models (larger Llama, Qwen, DeepSeek, gpt-oss, Gemini Flash) are usually good enough for summarizing, if you:

1. **Pin a model** (or a short fallback chain) instead of `auto`, for a consistent style. The service starts with `auto` and makes the model an env var (`FREELLMAPI_MODEL`), so pinning later is a config change.
2. **Validate the JSON with pydantic** and retry once, adding the validation error to the prompt.
3. **Split long documents** into chunks when a free tier's context or tokens-per-minute limit is too small for a long Gemini chat.
4. Remember that some free tiers train on prompts. That's fine for these ideas, which are not sensitive.

### Option: use leftover Claude subscription usage {#leftover-usage}

We already pay for a Claude subscription for daily coding. Its usage comes in **session windows that reset after a few hours**, plus a **weekly limit**. Usage left in a window just before it resets is lost. The idea: run the summaries at the **end of the coding day**, on usage that would otherwise go unused. Nothing is urgent, so a day without leftover usage just means the notes wait until the next day. Only if that happens for days in a row do we need the paid API.

**Is it feasible? Yes, with some care:**

| Point                          | How to handle it                                                                                                                                                                                                                                                                                  |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Running Claude headless        | `claude setup-token` creates a long-lived subscription token. With it, the `claude-code` backend (`claude -p`) runs on the H4 LXC or in a GitHub Action without a login screen.                                                                                                                   |
| Knowing how much usage is left | As far as we know, remaining usage cannot be read reliably from a script. Claude Code shows it interactively (`/usage`). So don't try to measure it: **try, and stop when the limit is hit.**                                                                                                     |
| Hitting the limit mid-run      | When `claude -p` fails with a usage-limit error, the job **stops the whole run** (the next notes would fail too), finishes what is done, and pushes. Unprocessed notes stay in `staging/` and are picked up next time. The pipeline already works this way.                                       |
| Timing                         | Session windows start at your first message, so the reset time moves from day to day. Keep it simple: an **evening window** (for example 21:00–06:00) for `claude-sub-evening` jobs, plus `POST /pipeline/runs` with the `claude-sub-now` profile when you stop coding early with usage to spare. |
| The weekly limit               | Summaries also count toward the weekly limit, which is shared with coding. Cap each run (e.g. at most 3 long chats), and use **Sonnet** rather than Opus. A 50K-token chat is small compared with a coding session.                                                                               |
| Backlog alarm                  | If notes wait in `staging/` for more than a few days (e.g. 3), send a notification. In the service these notes are flagged `stuck` after 3 days. That's the signal to pick another profile for them.                                                                                              |
| Terms                          | **Verified (September 2026):** scheduled, unattended `claude -p` via `claude setup-token` is Anthropic's documented use case for CI pipelines and background services, and headless mode explicitly shares the same Max subscription weekly limit as interactive use ([Claude Code authentication](https://code.claude.com/docs/en/authentication), [Scheduled tasks](https://code.claude.com/docs/en/scheduled-tasks)). The one hard rule: only call the official `claude` binary — the OAuth token must never be extracted and used against Anthropic's API directly (blocked since January 2026). Our design already only shells out to `claude -p`, so it's compliant. The token is annual and needs manual renewal. |

**Suggested routing with this option:**

1. **Notes** → FreeLLMApi, every run. They're free and simple.
2. **Long AI chats** → Claude subscription (Sonnet) in the evening run, on leftover usage.
3. **No automatic fallback.** A job that fails is retried the next day. After 3 days the note is flagged `stuck`, and you choose another profile yourself.

In the service this is the `claude-sub-evening` profile (see [Orchestration](#orchestration)). The [test suite](#test-suite) shows whether the subscription models are actually better enough than FreeLLMApi to be worth it.

### Sources

- [OpenAI API pricing](https://developers.openai.com/api/docs/pricing)
- [Introducing GPT-6 Sol and Luna (OpenAI)](https://openai.com/index/introducing-gpt-6-sol-and-luna/)
- [OpenRouter: GPT-6 Astra](https://openrouter.ai/openai/gpt-6-astra)
- Claude prices: Anthropic model list (June 2026). Check [Anthropic pricing](https://www.anthropic.com/pricing) before deciding.

---

## 🧪 Model Test Suite {#test-suite}

Before choosing an LLM, build a small, fixed **test suite** and run every candidate on it. That turns "which model?" into a measured decision instead of a guess. The test also exercises the pipeline code itself.

### Test set

- **10–15 real captures**, frozen in a `testdata/` folder so every run uses exactly the same input:
  - 5–6 short dictated notes (clean, messy, with a spoken prefix, without a template)
  - 3–4 medium Gemini chats
  - 2–3 long Gemini chats, including the ~190 KB export this page was built from
- **Reference output:** this page is a hand-reviewed summary of that long export. It shows what a good result looks like: decisions kept, detours removed, only the latest code.

### Candidates

| Candidate                                                                | Route                                                   |
| ------------------------------------------------------------------------ | ------------------------------------------------------- |
| FreeLLMApi, 1–2 pinned models (e.g. a large Llama or Qwen, Gemini Flash) | `freellmapi` backend                                    |
| Claude Sonnet 5                                                          | Subscription (`claude-code`, run in the evening) or API |
| Claude Opus                                                              | Subscription, as the quality ceiling                    |
| Optional: GPT-6 Luna / Sol                                               | `openai-api` backend, only if the others disappoint     |

Use the same prompt, schema and templates for every candidate.

### What to measure

| Automatic checks (per run)                                    | Human review (per summary, score 1–5)                                   |
| ------------------------------------------------------------- | ----------------------------------------------------------------------- |
| Valid JSON on the first try, and after one retry              | **Completeness:** are the decisions and options all there?              |
| Schema and frontmatter valid, Hugo build passes               | **Accuracy:** nothing made up or wrongly merged                         |
| Tags only from the allowed list                               | **Clean-up:** detours and duplicates removed, only the latest code kept |
| Long chats handled without hitting context or rate limits     | **Readability:** structure and grammar, fits the site's style           |
| Time per note, tokens used, cost or share of the subscription | **Usefulness:** would you link to this page from a curated section?     |

For a fair human review, put the outputs side by side **without the model name** (blind), and score them. An LLM can pre-score against the same rubric to save time, but the final call stays with a person.

### Decision rules

Agree on these before looking at the results:

- **FreeLLMApi scores ≥ 4 on average and ≥ 90% valid JSON for all types** → use FreeLLMApi for everything. No subscription or API needed.
- **FreeLLMApi is good for short notes but not for long chats** → short notes on FreeLLMApi, long chats on [leftover Claude usage](#leftover-usage), with the paid API as backlog fallback.
- **Sonnet is about as good as Opus** → use Sonnet, which saves subscription usage.
- **Nothing free or subscription-based is good enough** → paid API with the best-scoring model (see [LLM Choice](#claude-vs-openai)).

### Keeping it useful

- Keep the test set in the pipeline repo, and re-run it whenever the prompt, template or model changes. That catches regressions.
- Save the results (scores, token counts, a few sample outputs) in a small report, so the decision is documented.
- Running Claude candidates on the subscription uses usage too. Run those tests in the evening as well.

---

## 🏗️ Strategies to Build the Pipeline {#strategies}

{{% alert title="Updated: the service replaces the two-stage rocket" color="info" %}}
The earlier choice was a **two-stage rocket**: Python on Proxmox prepared notes, and a GitHub Action ran Claude in the evening. New requirements changed that: start runs from an API, summarize a YouTube URL on demand, store metrics per run, and show a React dashboard. The pipeline now runs as the **Idea Catcher Service** at home, built as an MVP first. The full design is on [Idea Catcher Service: Architecture](../idea-catcher-service-architecture/). This section keeps the options and records why the choice changed.
{{% /alert %}}

### Where the processing job runs

**Decision: strategy F, the Idea Catcher Service on Proxmox.** It includes A (Python at home) and E (evening runs on leftover Claude usage). E is now an **option on each LLM job** rather than a separate system. The GitHub Action (B) is no longer needed.

| Strategy                                | Description                                                                                                                                                                                                 | Pros                                                                                                       | Cons                                                                                                                             |
| --------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| A. Python on Proxmox                    | One Python script in a small LXC, run on a schedule. It calls the chosen LLM API.                                                                                                                           | Full control, easy to test and replay, can deploy the site directly, no inbound ports.                     | No API, no metrics, no on-demand runs.                                                                                           |
| B. GitHub Action + Claude subscription  | A workflow in idea-bucket, triggered on a schedule. Claude Code does the summarizing and commits to epiaku-docs `main`.                                                                                     | Nothing to host. Uses the existing subscription. Logs in GitHub.                                           | Can't answer an API call within a minute. YouTube fetches are often blocked from cloud IPs. Metrics are spread over Action logs. |
| C. Hybrid                               | GitHub Action does steps 3–5 (Python + LLM API). The home server only pulls and deploys.                                                                                                                    | Clean split between processing (cloud) and hosting (home).                                                 | Two places to look when something breaks.                                                                                        |
| D. n8n / Windmill on Proxmox            | A visual workflow tool.                                                                                                                                                                                     | Visual flows, run history in a UI.                                                                         | Heavy always-on server. The YouTube and LLM logic is still Python.                                                               |
| E. Evening run on leftover Claude usage | The `claude-code` backend, run after coding hours. It stops when the subscription limit is hit and continues the next day (see [leftover usage](#leftover-usage)).                                          | No extra cost, good summary quality, uses usage that would otherwise go unused.                            | Processing depends on how much you coded that day. Shares the weekly limit.                                                      |
| **F. Idea Catcher Service** (chosen)    | API + Postgres job queue + worker in one LXC with Docker Compose. Every LLM job carries its own options: backend, model, and `now` or `evening` ([details](../idea-catcher-service-architecture/#mvp-llm)). | API triggers, metrics per run and per note, E included. Later: dashboards and on-demand YouTube summaries. | More to build and run than a script.                                                                                             |

### Orchestration: Proxmox, GitHub, or both {#orchestration}

|                                    | 1. All on Proxmox      | 2. Proxmox starts the Action and waits | 3. All in GitHub                     | 4. Two-stage rocket (previous choice) | **5. Idea Catcher Service (chosen)**                      |
| ---------------------------------- | ---------------------- | -------------------------------------- | ------------------------------------ | ------------------------------------- | --------------------------------------------------------- |
| Cleaning, IDs, adding context      | Proxmox                | Proxmox                                | GitHub                               | Proxmox                               | **worker (Proxmox)**                                      |
| YouTube fetch (transcript, counts) | Proxmox (works)        | Proxmox (works)                        | GitHub (often blocked for cloud IPs) | Proxmox (works)                       | **worker (works)**                                        |
| LLM step                           | `claude -p` in the LXC | In the Action                          | In the Action                        | In the Action                         | **`llm.reason` job: profile per message, now or evening** |
| Writing pages, archiving           | Proxmox                | Proxmox                                | GitHub                               | GitHub                                | **worker (`pipeline.publish`)**                           |
| Deploy to the home web server      | Directly               | Directly                               | Cron pull at home                    | Cron pull at home                     | **Manual `deploy.sh`, as today**                          |
| Trigger                            | Timer                  | Timer                                  | Action schedule                      | Timer + Action schedule               | **Scheduler or API: both add a job to the queue**         |
| Metrics                            | Logs                   | Logs                                   | Action logs                          | Logs in two places                    | **Postgres, per run and per note**                        |

Setup 5 is setup 1 grown into a service. Moving the LLM step home is what makes API triggers and on-demand YouTube summaries possible. The evening-subscription idea from setup 4 survives as the `when: evening` job option.

### What stays from the two-stage rocket {#split-setup}

- **`staging/` is still the checkpoint.** A note in `staging/` is complete: ID, class, cleaned frontmatter and added context (for YouTube, the transcript and counts with the fetch date). It waits there for its LLM job, which may be hours away for evening jobs.
- **Every step can be re-run.** Staging only touches `inbox/`. Publishing overwrites pages by ID. **Replay** a note by moving it from `archive/` back to `staging/` (new prompt or model), or with `POST /items/{doc_id}/replay`.
- **If the LLM is unavailable** (usage limit, FreeLLMApi down), notes simply wait in `staging/`. The LLM job is logged, becomes `deferred` and retries the **next day**. There is no fallback from free to paid. After 3 failed days the note is flagged `stuck`.
- **Prompts are built in Python**, with a template per class. Claude Code runs as a plain `claude -p` call, without tools or skills.
- **What is gone:** the GitHub Action, `CLAUDE_CODE_OAUTH_TOKEN` and `PIPELINE_TOKEN` as Action secrets, and pausing stage one around the evening run. There is now only one Git writer (the worker), so the two stages can no longer race each other.

### Scheduling on Proxmox {#scheduling}

**Decision: the scheduler runs as a loop inside the service's worker container.** In the MVP the schedule is a cron expression in `.env` (later editable from the dashboard). A due schedule adds a `pipeline.run` job to the queue, exactly like an API call. See [Scheduling](../idea-catcher-service-architecture/#mvp-scheduling) on the service page.

Options that were considered: a **systemd timer** inside the LXC (earlier recommendation: survives reboots, logs to the journal), **cron** in the LXC or in a container (`supercronic`), a **Python scheduler library** (APScheduler), and the Proxmox host starting and stopping the LXC (zero idle resources, more moving parts). A timer that runs `curl -X POST …/pipeline/runs` still works as a fallback trigger, because the API accepts it like any other client.

### Container layout

**Decision: one unprivileged LXC with Docker Compose** (`db`, `api`, `worker` in the MVP), the same pattern as the FreeLLMApi LXC. The same `compose.yaml` runs on the Mac for testing. See [Hosting & Deploy](../idea-catcher-service-architecture/#mvp-hosting).

---

## 🛠️ Implementation Reference {#implementation}

The pipeline's Python code, based on the decisions on this page. It lives in the service's `pipeline`, `llm` and `youtube` modules. The [service page](../idea-catcher-service-architecture/) covers the queue, the API, the database and hosting.

**Principle:** Python does everything that needs no judgement (staging, IDs, routing, rendering, Git, deploy). The LLM only turns one note into validated JSON, through one generic `reason()` function.

### Layout

```text
src/catcher/modules/
├── pipeline/
│   ├── doctypes.py         # registry: type → schema, prompt, template, default LLM profile, folders
│   ├── jobs.py             # pipeline.run, pipeline.publish, pipeline.replay_item
│   ├── tags.yaml           # allowed tag list (or read from a Hugo data file)
│   └── templates/
│       ├── note.md.j2
│       ├── ai-chat.md.j2
│       └── youtube.md.j2   # adds metrics table + youtube-lite embed
├── llm/
│   ├── service.py          # reason(): prompt → backend → validated pydantic object + usage
│   ├── backends/           # MVP: freellmapi, claude_code, fake (later: anthropic_api, gemini_api, openai_api)
│   └── prompts/
│       ├── note.md
│       ├── ai-chat.md
│       └── youtube-summary.md   # based on the YouTube summary prompt
├── youtube/                # facts (yt-dlp, transcript), verify, render
└── core/github_auth.py     # GitHub App (or fine-grained PAT) tokens
```

### Document-type registry

The class comes from the domain of the Web Clipper's `source` URL, or from an explicit `type` set by an Obsidian template. No folder or filename guessing is needed.

```python
# doctypes.py
from dataclasses import dataclass
from urllib.parse import urlparse
from pydantic import BaseModel

class NoteSummary(BaseModel):
    title: str
    description: str
    body: str
    tags: list[str]

class ChatSummary(BaseModel):
    title: str
    description: str
    summary: list[str]
    decisions: list[str]
    options: list[str]
    open_questions: list[str]
    body: str
    tags: list[str]

class YoutubeSummary(BaseModel):       # sections of the YouTube summary prompt
    title: str
    creator: str
    summary: str
    main_purpose: str
    key_examples: list[str]
    action_plan: list[str]
    tools: list[str]
    tips: list[dict]                   # tip, explanation, how_to_apply
    channel_application: str
    tags: list[str]
    # metrics and the video embed are added by Python, not by the LLM

@dataclass
class DocType:
    name: str
    type: str                  # explicit frontmatter `type`
    hosts: tuple[str, ...]     # domains of the `source` URL that map to this class
    schema: type[BaseModel]
    prompt: str                # file in prompts/
    template: str              # file in templates/
    out_dir: str               # folder in epiaku-docs
    archive_dir: str           # folder in idea-bucket
    llm_profile: str           # default LLM profile, overridable per job message
    review: bool = False       # has a review prompt; the message's review.enabled (default true) applies

DOCS = "hugo/content/en/docs/idea-bucket"
DOC_TYPES = [
    DocType("note", "note", (), NoteSummary, "note.md", "note.md.j2",
            f"{DOCS}/notes", "archive/notes", llm_profile="free-fast"),
    DocType("gemini-chat", "ai-chat", ("gemini.google.com",), ChatSummary, "ai-chat.md", "ai-chat.md.j2",
            f"{DOCS}/gemini", "archive/clippings", llm_profile="claude-sub-evening"),
    DocType("claude-chat", "ai-chat", ("claude.ai",), ChatSummary, "ai-chat.md", "ai-chat.md.j2",
            f"{DOCS}/claude", "archive/clippings", llm_profile="claude-sub-evening"),
    DocType("youtube", "youtube", ("youtube.com", "youtu.be"), YoutubeSummary, "youtube.md", "youtube.md.j2",
            f"{DOCS}/youtube", "archive/youtube", llm_profile="claude-sub-evening", review=True),
    DocType("youtube-gemini", "youtube-gemini", (), YoutubeSummary, "youtube-from-gemini.md", "youtube.md.j2",
            f"{DOCS}/youtube", "archive/youtube", llm_profile="claude-sub-evening", review=True),
            # detected in detect(): gemini.google.com source + a YouTube URL in the first user message
]

def detect(fm: dict) -> DocType:
    host = urlparse(fm.get("source") or "").netloc.removeprefix("www.").removeprefix("m.")
    wanted = fm.get("type")
    if wanted:                 # 1. explicit `type` from an Obsidian template
        matches = [t for t in DOC_TYPES if t.type == wanted]
        for t in matches:      #    e.g. ai-chat: pick Gemini or Claude by domain
            if host in t.hosts:
                return t
        if matches:
            return matches[0]
    for t in DOC_TYPES:        # 2. domain of the Web Clipper `source` URL
        if host and host in t.hosts:
            return t
    return DOC_TYPES[0]        # 3. fallback: treat as a note
```

Adding a type later (web clips) = one entry here, one prompt, one template. `prompt_version` (not shown) is stored with every result, so replays are traceable. For `youtube`, Python first fetches the transcript and the counts; title, author and description come from the clip.

### LLM backends {#llm-backends}

The generic `reason(request)` picks the backend and model from the job's **LLM options**. The job message's own options come first, then the document class default (the `llm_profile` in the registry above), then the global default. See [LLM step & profiles](../idea-catcher-service-architecture/#mvp-llm) on the service page for the profiles, the evening window and the retry rules.

| Backend                                     | Client                                           | Notes                                                                                                                  |
| ------------------------------------------- | ------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------- |
| `freellmapi`                                | OpenAI client, `base_url=http://<h4-ip>:3001/v1` | **MVP.** Default for **notes** (`free-fast`, now). Prompt for JSON, validate with pydantic, retry once with the error. |
| `claude-code`                               | `claude -p` CLI on the subscription              | **MVP.** Default for **AI chats and YouTube clips** (`claude-sub-evening`). Snippet below.                             |
| `fake`                                      | Canned JSON per schema                           | **MVP.** Tests and local development.                                                                                  |
| `anthropic-api`, `gemini-api`, `openai-api` | Official SDKs                                    | **Future**, selected explicitly by a profile, never as an automatic fallback.                                          |

**No fallback between backends.** A failed LLM job is logged and re-queued for the **next day**. After 3 failed days the note is flagged `stuck` and keeps retrying daily.

```python
# llm/backends/claude_code.py
import json, subprocess
from pydantic import BaseModel

def run(prompt: str, model: str, schema: type[BaseModel]) -> tuple[BaseModel, Usage]:
    full = f"{prompt}\n\nReturn ONLY JSON matching this schema:\n{json.dumps(schema.model_json_schema())}"
    out = subprocess.run(
        ["claude", "-p", "--model", model, "--output-format", "json", "--max-turns", "1"],
        input=full, capture_output=True, text=True, cwd="/tmp",
    )
    if is_usage_limit(out):
        raise UsageLimitReached                     # job → deferred to the next evening window
    out.check_returncode()
    data = json.loads(out.stdout)
    result = data["result"]
    obj = schema.model_validate_json(result[result.find("{"): result.rfind("}") + 1])
    return obj, Usage.from_claude_json(data)        # tokens for the metrics
```

- `--max-turns 1` and no tools: a single transformation. `cwd="/tmp"` avoids loading a repo `CLAUDE.md`.
- If the installed CLI supports `--json-schema`, use it instead of trimming to the braces.

### Main flow

A pipeline run is three kinds of jobs: one `pipeline.run`, one `llm.reason` per note, and a `pipeline.publish` that collects every note whose LLM result is ready.

```python
# pipeline/jobs.py – pipeline.run  (default queue; from the scheduler or the API)
@job("pipeline.run", queue="default", unique=True)
async def run(ctx: JobContext, p: RunParams) -> None:
    pull(ideas_repo)
    for note in sorted((ideas_repo / "inbox").rglob("*.md")):   # recursive: notes/, clippings/
        fm, body = load_frontmatter(note)
        doctype = detect(fm)
        fm = clean(fm, doctype)                         # id, class, strip [[ ]]; body stays as captured
        if doctype.name == "youtube":
            fm |= fetch_youtube(video_id(fm["source"])) # counts + transcript, with fetch date
        write_staged(ideas_repo / "staging", fm, body)  # replaces a staged copy with the same id
        note.unlink()
        ctx.item(fm["id"], doctype.name, status="staged")
    push(ideas_repo)                                    # pull --rebase first

    for staged in sorted((ideas_repo / "staging").glob("*.md")):  # also notes left from earlier runs
        fm, body = load_frontmatter(staged)
        doctype = DOC_TYPES_BY_NAME[fm["class"]]
        await ctx.enqueue_or_update(                    # one open llm.reason per doc id
            "llm.reason", key=fm["id"],
            request=LlmRequest(task=doctype.prompt, prompt_version=doctype.prompt_version,
                               input=prompt_input(doctype, fm, body), schema_name=doctype.schema.__name__,
                               llm=ctx.llm_options(default_profile=doctype.llm_profile)),  # message > class default
        )
        ctx.item(fm["id"], doctype.name, status="waiting_llm")
```

```python
# pipeline/jobs.py – pipeline.publish  (default queue; folded, so one publish serves many notes)
@job("pipeline.publish", queue="default", fold=True)
async def publish(ctx: JobContext, p: PublishParams) -> None:
    pull(ideas_repo); pull(docs_repo)
    for item in ctx.ready_items():                      # LLM result stored, not yet published
        doctype = DOC_TYPES_BY_NAME[item.doc_class]
        summary = item.llm_result(doctype.schema)
        summary.tags = keep_allowed(summary.tags)       # unknown tags → tag_suggestion event
        page = render_page(summary, item.frontmatter, doctype.template)
        if problems := validate_page(page):             # frontmatter, required fields, tags, shortcodes
            ctx.item_failed(item, problems); continue
        write_page(docs_repo / doctype.out_dir, item.doc_id, page)
        archive(item.staged_path, ideas_repo / doctype.archive_dir)
        ctx.item(item.doc_id, item.doc_class, status="published")
    push(docs_repo); push(ideas_repo)                   # straight to main, docs first
```

- **`write_page` overwrites by ID:** delete any existing `*_<id>_*.md` in the folder, then write `YYYYMMDD_<id>_<slug>.md`. The friendly name may change between runs, but the ID never does.
- **Python validation instead of a Hugo build (MVP):** YAML frontmatter parses, `title`, `description`, `weight` and `type: docs` are present, tags come from the allowed list, only known shortcodes are used. Your manual `deploy.sh` build catches anything else.
- **One Git writer.** Only the worker touches the repos, one job at a time. The phone may still push to idea-bucket during a run, so use `pull --rebase` before pushing, and retry once.
- **Per-note errors don't fail the run.** A bad note is marked `failed` or `deferred` in the metrics and stays in `staging/`.
- **No hand-maintained index:** Hugo's section list pages and tag pages build the overviews from frontmatter, so there are no merge conflicts on index files.
- **Follow the site conventions** from the `epi-hugo-docs` and `epi-hugo-youtube` skills in prompts and templates: frontmatter with `title`, `description`, `weight`, `type: docs`, emoji on `##` headers and `youtube-lite` embeds.

### Running on the H4

Everything runs in the service's Compose stack in one LXC (see [Hosting & Deploy](../idea-catcher-service-architecture/#mvp-hosting)). The same `compose.yaml` runs on the Mac for testing.

| Container | Does                                                                                | Needs                                                                                                                                                           |
| --------- | ----------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `worker`  | Scheduler loop, `pipeline.run`, `llm.reason`, `pipeline.publish`, one job at a time | Python, Git, `yt-dlp` (with Deno), `youtube-transcript-api`, Claude Code CLI, GitHub PAT, `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`), FreeLLMApi URL |
| `api`     | Start runs, list runs and items                                                     | DB, API keys                                                                                                                                                    |
| `db`      | Postgres: the queue and the metrics                                                 | A volume                                                                                                                                                        |

The starting schedule is `pipeline.run` at 08:00, 12:00, 17:00 and 21:00. Short notes are published within hours. AI chats and YouTube clips wait for the evening window of their `claude-code` jobs. **The docs site deploy stays manual:** pull `epiaku-docs` on the Mac and run `./.github/workflows/deploy.sh`.

### GitHub authentication for the pipeline

The devices (iPhone, Mac) use fine-grained tokens (see [Stage 2](#sync)). The pipeline needs its own identity:

| Method           | Clone and push        | Tied to a person?           | Fit                        |
| ---------------- | --------------------- | --------------------------- | -------------------------- |
| **GitHub App**   | ✅                    | No (`epiaku-docs-bot[bot]`) | **Best for automation**    |
| Fine-grained PAT | ✅                    | Yes                         | Quick start (max 1 year)   |
| SSH deploy key   | ✅ (one repo per key) | No                          | Git only, one key per repo |
| Classic PAT      | ✅                    | Yes                         | ❌ Too broad               |

**GitHub App setup**

1. **Org settings → Developer settings → GitHub Apps → New**. Name `epiaku-docs-bot`, webhooks **off**.
2. Repository permissions: **Contents** read & write, **Metadata** read. Pull request access is no longer needed, because the pipeline pushes straight to `main`.
3. "Only on this account". Download the **private key** (`.pem`) and note the **App ID**.
4. Install it on **only** `idea-bucket` and `epiaku-docs`.
5. On the H4: store the key as `/etc/epiaku-pipeline/app.pem`, `chmod 600`, owned by the pipeline user.

```python
# github_auth.py
import os
from github import Auth, GithubIntegration

APP_ID = int(os.environ["GH_APP_ID"])
PRIVATE_KEY = open(os.environ["GH_APP_KEY_PATH"]).read()

def installation_token(owner: str = "epiaku", repo: str = "epiaku-docs") -> str:
    gi = GithubIntegration(auth=Auth.AppAuth(APP_ID, PRIVATE_KEY))
    inst = gi.get_repo_installation(owner, repo)
    return gi.get_access_token(inst.id).token          # valid ~1 hour
```

- Clone into a fresh temporary directory each run (`https://x-access-token:<token>@github.com/...`), so the token never stays in `.git/config`.
- Commit as the bot: `git -c user.name="epiaku-docs-bot[bot]" -c user.email="<app-id>+epiaku-docs-bot[bot]@users.noreply.github.com" commit ...`.
- **Quick start:** a fine-grained PAT (owner `epiaku`, only the two repos, Contents read/write) works immediately. Only `github_auth.py` changes when you switch to the App.
- If `main` ever gets a ruleset that requires pull requests, the bot needs a bypass, or the pipeline has to go back to PRs.

### Next steps

The service is built as an MVP first (see [Build steps](../idea-catcher-service-architecture/#mvp-steps) on the service page):

1. ✅ **Set up capture** — done on both devices: Obsidian + Git plugin on the [iPhone](../obsidian-git-iphone-setup/) and [Mac](../obsidian-git-macbook-setup/), each syncing independently with the `idea-bucket` repo; the Web Clipper saves directly into `inbox/clippings/`.
2. **Stage A, the Python pipeline, run locally:** staging, `reason()` with `claude -p`, rendering, the reviewer and publishing, as plain functions behind a CLI. Tested step by step on copies of the repos, then against GitHub (see [Stage A](../idea-catcher-service-architecture/#mvp-stage-a)).
3. **Stage B, Postgres + the queue, locally:** the same functions wrapped in jobs, with next-day deferral, stuck rules, metrics and the scheduler (see [Stage B](../idea-catcher-service-architecture/#mvp-stage-b)).
4. **Stage C, the API, locally:** start runs and read jobs and items over HTTP. The full Compose stack runs on the Mac (see [Stage C](../idea-catcher-service-architecture/#mvp-stage-c)).
5. **Proxmox:** the LXC, `deploy.sh`, the schedule, and a fine-grained PAT for the real repos (later the GitHub App).
6. **Build and run the [model test suite](#test-suite)** to confirm the profiles per class.
7. **Later:** the [future features](../idea-catcher-service-architecture/#future) (React dashboards, YouTube endpoint, more backends).

---

## ⚠️ Risks & Blind Spots {#risks}

- **iCloud and Git on the same vault.** Solved by option A. Don't put the idea-bucket vault back into iCloud. See [iCloud and Git on the same vault](#icloud-git).
- **Free LLM limits.** FreeLLMApi may hit rate limits or context limits on long Gemini chats. A failed note stays in `staging/` and is retried the next day, with no fallback to a paid model. After 3 days it is flagged `stuck`.
- **Mobile sync only runs in the foreground.** A note typed just before locking the phone may wait until the next time Obsidian is opened. That is fine for this use case.
- **Tokens on devices and on the server.** Keep tokens fine-grained (one repo, Contents only) with an expiry, and plan the renewal.
- **Unstable IDs.** Regenerating or copying an `id` breaks overwrite-by-ID. Create it once, and clear it when duplicating a note.
- **LLM output quality.** Invalid JSON, wrong structure or lost details. Mitigate with schema validation, a Hugo build before pushing to `main` and the raw archive for replay.
- **Partial runs.** A crash between "write page" and "move to archive" is harmless thanks to overwrite-by-ID. The next run just redoes the note.
- **Home lab as a single point of failure.** If the ODROID is down, captures still queue safely on GitHub. Nothing is lost, only delayed.
- **Tag sprawl.** Without a fixed list, the LLM invents slightly different tags and the tag pages become useless. Enforce the list in Python and review the suggestions log now and then.
- **Web Clipper quirks.** The vault name must match exactly (case-sensitive), and Gemini tab titles are often "New chat". Use timestamped note names.

---

## 📈 Growth Path: From POC to SaaS {#growth-path}

|                    | Step 1: Local POC (now)           | Step 2: Single-user cloud            | Step 3: Multi-tenant SaaS                                     |
| ------------------ | --------------------------------- | ------------------------------------ | ------------------------------------------------------------- |
| **Capture**        | Obsidian + Git                    | Obsidian + Git, webhooks, email      | Own web/mobile app, Obsidian plugin, email or chat-bot ingest |
| **Engine**         | Python job on Proxmox             | GitHub Action or cloud container job | Queue-based workers, per-tenant isolation                     |
| **LLM**            | Paid API or subscription          | Paid API with logging and alerts     | Central LLM proxy with per-user token budgets                 |
| **Output**         | Hugo on the home LAN behind VPN   | Cloudflare Pages or similar, private | Per-tenant sites on subdomains or custom domains              |
| **Auth & billing** | None                              | None                                 | e.g. Supabase Auth or Clerk + Stripe                          |
| **Focus**          | Prompt quality and pipeline logic | Reliability and cloud APIs           | UX, scaling and monetization                                  |

SaaS ideas from the session, for later:

- **Bring your own repo:** the user connects GitHub through a **GitHub App** (not a pasted token). The service processes their repo and hosts the generated site.
- **Managed tier:** the service also hosts the repo. For non-technical users, offer a pre-configured Obsidian vault, a simple capture app or a personal ingest email address.
- **Access control:** private, team-only or public sites, enforced at the edge (e.g. Cloudflare Access).

---

## ❓ Open Questions {#open-questions}

1. **Which LLM:** decided by the [model test suite](#test-suite). The options are FreeLLMApi only, FreeLLMApi for notes plus [leftover Claude usage](#leftover-usage) for long chats, or a paid API.

### Reminders

- **Move old Gemini web clips into the pipeline.** Two pages were clipped by hand before the pipeline existed and still have the title "New chat":
  - `ai-influencers/crm/systemeio-vs-stanstore.md`
  - `ai-influencers/instagram/instagram-influencers/persona.md`

  Once the pipeline works, put the raw clips in idea-bucket `inbox/` so they get a proper summary in `idea-bucket/gemini/` with tags (`ai-influencer-idea`, `crm`, `instagram`). Then replace the old pages with links from `ai-influencers/`, and add Hugo `aliases` for the old URLs so existing links keep working.
