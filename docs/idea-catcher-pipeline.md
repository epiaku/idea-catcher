---
title: "Idea Catcher: From Phone Note to Docs Page"
linkTitle: "Idea Catcher"
description: "High-level design of the idea-capture pipeline: Obsidian captures, the idea-bucket GitHub repo, a Python + LLM summarizer and publishing into epiaku-docs, with options and strategies for each stage."
---

A structured summary of a long Gemini brainstorm (137 messages, September 2026) on how to catch ideas before they disappear and turn them into readable pages on this site. It was later refined with our own decisions. The raw conversation had many detours, repeated answers and step-by-step click guides. This page keeps the decisions, the options for each part of the workflow and the strategies for building the pipeline. It ends with an [Implementation Reference](#implementation) for building the pipeline.

## 📝 Summary {#summary}

- **Problem:** Ideas for apps, SaaS products and YouTube videos come up on the go and get lost. They need to be captured with near-zero friction and then end up as structured, searchable pages.
- **Chosen workflow:** Capture in **Obsidian** (phone or Mac) → **Obsidian Git** plugin pushes notes to the **idea-bucket** GitHub repo → the **Idea Catcher Service** on Proxmox picks up new notes and summarizes them through an LLM **API with an API key**, using one of three **profiles**: `notes` (FreeLLMApi), `clippings` (OpenAI API, for AI chats) and `youtube` (OpenAI API, for both YouTube classes), with a **reviewer** checking YouTube summaries → writes Hugo pages straight to `main` of `epiaku-docs`, into `hugo/content/en/docs/idea-bucket/<type>/` → you deploy the site to the home Proxmox web server with `deploy.sh`.
- **Key design principles:**
  - **Raw first, process later.** The captured text is never changed. The pipeline only adds metadata and context around it, and an untouched copy of every capture stays in `archive/`, so the pipeline can be replayed, tested and improved later.
  - **One stable ID per capture.** Every note carries an `id` in its frontmatter. The output page is named after it, so re-processing a note **overwrites** its page instead of creating a duplicate.
  - **Asynchronous, no real-time need.** A few notes a day, so an hourly or a few-times-a-day batch is enough. This keeps the design simple and robust.
  - **Metadata steers the pipeline.** A `type` field in the note frontmatter, set by Obsidian templates, decides the prompt, the target folder and the archive folder.
  - **Tags automated, links by hand.** The pipeline tags every summary from a fixed tag list. Hugo builds a page per tag. Curated pages link to the tag pages, and to individual ideas that matter.
  - **Python first, AI last.** Deterministic code handles files, names, frontmatter and paths. The LLM only does what needs language understanding: titles, tags, summaries and structure.
- **Chosen build strategy: the Idea Catcher Service, MVP first.** A small Python service on Proxmox (Docker Compose in one LXC) with a Postgres job queue, a worker and a minimal API. The scheduler or an API call starts a run. A run only looks at `inbox/`: a document leaves it when work starts (untouched copy to `archive/`, working copy to `output/`), and each note gets an **LLM job** whose options (a named profile) set the API provider and the model: `notes` on FreeLLMApi, `clippings` and `youtube` on the OpenAI API. Every call goes to an API with a key, so the service can run 24/7, on the Mac, on Proxmox or in the cloud. Failed LLM jobs retry on the next run, with no fallback to another provider. Every run and note is stored as metrics. See [Idea Catcher Service: Architecture](../idea-catcher-service-architecture/). This replaces the earlier two-stage rocket with a GitHub Action (see [Strategies](#strategies)).
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
| LLM                           | **Three profiles, one per kind of capture, set per job message** ([details](../idea-catcher-service-architecture/#mvp-llm)): `notes` → **FreeLLMApi**, `clippings` (AI chats) → **OpenAI API**, `youtube` (both YouTube classes, and the reviewer) → **OpenAI API**. Every profile is an API with a key, no CLI and no subscription (`claude -p` was unreliable and is removed). The provider of each profile is a config change. **No fallback between providers.** Failed jobs retry on the next run. The [test suite](#test-suite) confirms the profiles. |
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
 │  • a run only looks at /inbox/           │
 │  • a document leaves /inbox/ when work   │
 │      starts: original → /archive/,       │
 │      working copy → /output/             │
 │  • earlier snapshots → /duplicates/      │
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
 │  • the page → /output/ (over the working │
 │      copy); failures → /failed/;         │
 │      errors stall it in /output/         │
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
| Web article / blog post      | Obsidian Web Clipper                             | `web-clip`                                      | **MVP** |

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
2. The domain of `source`: youtube.com or youtu.be with a video → `youtube`; gemini.google.com → `ai-chat` (Gemini); claude.ai → `ai-chat` (Claude).
3. Any other web address in `source` → `web-clip` (an article, blog post or tutorial clipped with the Web Clipper).
4. No `source` → `note`.

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

This is the first part of the `pipeline.run` job in the [Idea Catcher Service](../idea-catcher-service-architecture/#mvp-flow), running on Proxmox. It reads the documents in `inbox/`, works out each one's class and unique `id`, and hands them to the LLM step.

**One rule: a run only looks at `inbox/` to find work.** As soon as a document is worked on, it **leaves `inbox/`**: the original goes to `archive/` (renamed, see [File names](#file-names)) and a working copy goes to `output/`. So a document can never be started twice, and what is still in `inbox/` is exactly what still has to be done. The pipeline never scans `output/` for work. If something goes wrong, the working copy stays in `output/` with a status in its frontmatter. **To retry, move the file from `archive/` back into `inbox/`.** The next run archives it again and overwrites the stalled copy in `output/`.

### Repo layout (latest version) {#repo-layout}

The layout has **five main folders (plus `facts/` and `llm/`) and one rule**: the `notes/` and `clippings/` subfolders of `inbox/` are repeated in every other folder, and a document gets **one calculated file name** that it keeps in every folder from then on (see [File names](#file-names)).

```text
idea-bucket/
├── README.md
├── _templates/            ← Obsidian templates
├── inbox/                 ← phone / Mac write here; the only place a run looks for work
│   ├── notes/             ← dictated notes (Obsidian's default new-note location)
│   └── clippings/         ← Web Clipper's own default folder — everything it captures:
│                             AI chats, YouTube links, web articles (classified by `source`)
├── archive/               ← the original, under its calculated name, for reference and replay
│   ├── notes/
│   ├── clippings/
│   └── artifacts/         ← files that are not markdown (PDFs, images), renamed
├── output/                ← the working copy while it is worked on, and the final result
│   ├── notes/
│   └── clippings/
├── facts/                 ← YouTube facts, one file per video
├── llm/                   ← the trace of each LLM call, same subfolders and names as archive/
│   ├── notes/
│   └── clippings/
├── failed/                ← documents that could not be processed, with the reason
│   ├── notes/
│   └── clippings/
└── duplicates/            ← earlier snapshots of a longer clip, moved out of the pipeline
    ├── notes/
    └── clippings/
```

- **`inbox/`**: new documents. A document leaves it the moment work on it starts.
- **`archive/`**: the original, made when work starts, under its **calculated file name**. The text is exactly as captured. The only change is two lines added to its frontmatter: `original_filename` and `calculated_filename`. Never deleted by the pipeline.
- **`output/`**: the working copy (with a `stage` in its frontmatter) while it is worked on, and the **final page** when it is ready: identical to the page written to `epiaku-docs`. For YouTube, the counts and transcript the summary was checked against are saved **once per video** in `facts/<video id>.json` at the root of `idea-bucket` (not next to the page), so a retry, a requeue or a rerun never calls YouTube again.
- **`llm/`**: one trace per document, `llm/<subfolder>/<calculated name>.json`: the raw reply of each LLM attempt, tokens, outcome and the validated output. A run reads a good trace before it calls the model, so a retry or a requeue does not pay twice. See [Saved LLM replies](../idea-catcher-how-to-run/#saved-llm-replies).
- **`failed/`**: the document, next to `<name>.error.txt` with the reason. See [Failures](#failed-folder).
- **`duplicates/`**: an earlier snapshot of a longer clip, with `duplicate_of: <subfolder>/<longest file name>` in its frontmatter. See [Duplicates](#duplicates-folder).

### Artifacts: files that are not markdown {#artifacts}

A file in `inbox/` that is **not** `.md` (a PDF, an image, a `.txt`, a `.csv`) is an **artifact**. We know nothing about it, so there is no LLM step, no working copy and no page. It is only renamed and copied:

- **Name:** `YYYYMMDD-<short guid>-<original file name>`, for example `20260928-a1b2c3-Quarterly report (final).pdf`. The original name and extension stay as they are, except for characters that are illegal in file names (`/ \ : * ? " < > |` become `_`), and the whole name is cut to 128 characters, keeping the extension. The date is the day of processing.
- **Where it goes:** a copy to `archive/artifacts/` (flat: artifacts arrive in `inbox/`, not in `notes/` or `clippings/`), and a copy to the **root of the epiaku-docs repo**, in `idea-bucket/artifacts/`. Not to the Hugo content folders: Hugo cannot turn a PDF or an image into a page, and the pipeline does not know whether it should be on the site. If you want one on the site, copy it to Hugo's `static/` folder yourself.
- **Then it leaves `inbox/`.** Nothing is written to `output/`.
- **A name that already starts with `YYYYMMDD-<guid>-`** (8 digits, 6 hex characters) is **not renamed**. So a file you move from `archive/artifacts/` back into `inbox/` keeps its name and overwrites the same files in `archive/artifacts/` and `idea-bucket/artifacts/`. That is the retry.
- **Size limit:** a file over **25 MB** (`ARTIFACT_MAX_MB`) is skipped with a warning and stays in `inbox/`, because epiaku-docs is a Git repo and GitHub rejects files over 100 MB.
- **`--limit` does not apply**, because artifacts cost nothing. `--file` finds an artifact by its file name or by the name it was captured under. `scan` lists them (`would copy`, or `would skip` when too large).
- **Hidden files** (`.DS_Store`) and `Thumbs.db` / `desktop.ini` are ignored.

### File names {#file-names}

**One name, everywhere.** When work on a document starts, it gets a **calculated file name** that never changes again:

```text
YYYYMMDD-<short guid>-<title>.md
e.g. 20260925-a1b2c3-how-do-i-sell-digital-bundles-on-systeme-io.md
```

The **same name** is used for the copy in `archive/`, the working copy and final page in `output/`, the page in `epiaku-docs`, and, if it comes to that, the file in `failed/` or `duplicates/`. Two documents with the same original name (two different chats that were both called `New chat.md`) can therefore never overwrite each other.

- **Date:** the capture date (`created` or `captured` in the frontmatter), or the day of processing when there is none.
- **Short guid:** 6 random characters. The pipeline checks that the name is not used yet in `archive/`, `output/`, `failed/` or `duplicates/`, and picks another guid if it is.
- **Title:** the clip's `title`. If it is missing or generic (`New chat`), an AI chat uses the **first words of your first message** (8 words, without links). Everything else uses the file name it was captured under. The title is turned into plain lower-case letters, digits and hyphens.
- **Length:** at most **128 characters**, counted with room for a 13-character suffix after the name (for example `.error.txt`), so the title part is cut short when needed.
- **Stored in the file:** the frontmatter of the archive copy and of the working copy has `original_filename` (the name it was captured under) and `calculated_filename`. The final page also has `source_file: <subfolder>/<calculated name>`, so from a published page you can find the archive file, and its own `original_filename`, so you know what the document was called when it first entered `inbox/` without having to look it up in the archive.
- **Language of a note:** a dictated note may be Dutch. The LLM translates it to English first, so the page is always English, and says which language the note was written in. The page gets `language: nl` (a two-letter code) in its frontmatter. It is left out when the LLM gives none or an invalid value, and only notes have it.
- **The frontmatter is edited as text**: the two lines are inserted into the existing frontmatter and everything else stays byte for byte. A file without frontmatter gets a small block, and its text is unchanged.
- **An unreadable file** (its frontmatter cannot be parsed) cannot be edited. It is only renamed: archived and moved to `failed/` under a calculated name with its bytes unchanged. The `.error.txt` records the original and the calculated name.
- **The `id` is not the file name.** The `id` (from the chat or video address, or the note) identifies the *content*, so a re-clip overwrites the page. Five clips of one chat share one `id` and have five different file names.
- **Requeue keeps the name.** `catcher run pipeline --requeue NAME` moves the archived original back into `inbox/` (and clears the stale `output/` copy) and runs it again; moving it back by hand works too. A file moved from `archive/` back into `inbox/` already has `calculated_filename` in its frontmatter, so it keeps its name. It overwrites the same-named files in `archive/` and `output/` (a stalled working copy).
- **`--file` finds a document by either name**: the calculated name or the original name.

The body of a document is never changed. Only the archive copy's frontmatter gets the two lines.

### The stage of a document {#file-stage}

Until Postgres arrives in [Stage B](../idea-catcher-service-architecture/#mvp-stage-b), the state is **the folder the file is in, plus a `stage` field in the frontmatter of the working copy**:

- **In `inbox/`**: waiting. Nothing has touched it.
- **`output/`, `stage: analyzed`**: work has started. `id` and `class` are added. This is also what a crashed run leaves behind.
- **`output/`, `stage: deferred`**: an error stalled it (LLM down, budget used up, YouTube facts unavailable). The frontmatter also has `deferred_reason` and `deferred_at`.
- **`output/`, no `stage`**: **final.** The file is now exactly the page written to `epiaku-docs`.
- **`failed/`** (with `.error.txt`): failed for good.
- **`duplicates/`**: an earlier snapshot of a longer clip. Never processed.

A run **never reads `output/`**, so `analyzed` and `deferred` copies just wait there until you retry them.

### The steps of one document {#capture-steps}

1. **Read.** Scan `inbox/` (or only the documents named with `--file`). Work out the class and `id` in memory. Nothing is written yet. A file that cannot be read is archived and moved to `failed/`.
2. **Compare.** Among the documents with one `id`, only the longest goes on. Earlier snapshots of it are archived and moved to `duplicates/`. See [Duplicates](#duplicates-folder).
3. **Start work**, right before the document's own LLM step. Give it its calculated name (or keep it, if it has one). Write the original to `archive/<sub>/<calculated name>` (with the two frontmatter lines), write the working copy to `output/<sub>/<calculated name>` (overwriting a stalled copy from an earlier run) with `stage: analyzed`, and remove the document from `inbox/`. With `--limit 3` or `--file`, only those documents leave `inbox/`.
4. **Reason.** One LLM call returns validated JSON. For a direct YouTube clip, free Python checks (no second LLM call) then compare the summary with the facts fetched from YouTube, and a failed transient call (a 5xx, a timeout) is tried again, up to 5 calls.
5. **Ready.** Render and validate the page, write it to `epiaku-docs` **under the calculated name** (removing an older page with the same `id`), and write the same page over the working copy in `output/`. The `stage` is gone.
6. **Failure.** If the LLM output is invalid twice, or the page is invalid: move the working copy to `failed/<sub>/`, with `<name>.error.txt`.
7. **Temporary error.** The working copy stays in `output/`, and its `stage` becomes `deferred` with the reason.

### Retrying {#retry}

A run does not retry stalled documents by itself. To retry:

- **A stalled or crashed document** (`output/`, `analyzed` or `deferred`): find its file in `archive/<sub>/` (same name as in `output/`, and `calculated_filename` in its frontmatter) and move it back into `inbox/<sub>/`. The file keeps its calculated name, so the next run overwrites the same files in `archive/` and `output/`, and the stalled copy is replaced.
- **A failed document:** move it from `failed/<sub>/` back into `inbox/<sub>/` and delete the `.error.txt`. (If it was unreadable, fix its frontmatter first.)
- **A finished document, with a new prompt or model:** copy it from `archive/<sub>/` into `inbox/<sub>/`. It keeps its name, and the page in `epiaku-docs` is replaced. A dictated note that had no `id` gets a **new** one on a replay, which makes a second page, so give it an `id` first.
- **From `duplicates/`:** move the file back into `inbox/`.

**Possible later:** a helper command such as `catcher requeue` that moves every stalled document (`stage: deferred` or `analyzed`) from `archive/` back into `inbox/` in one go. It is not built yet. It would be useful after a budget outage, when many documents stall at once. For now you move the files yourself.

### Duplicates {#duplicates-folder}

**Growing snapshots of one conversation cost one LLM call.** A Gemini chat is often clipped several times while it grows, under different file names, because Gemini renames the tab as the conversation moves on. All clips share one `id` (from the chat's address). Only the longest goes to the LLM:

- A clip is an **earlier snapshot** of a longer clip when every message in it is identical to the longer clip's. Messages are compared one by one (a message starts at a `**You**` line). Its last message may differ, because it is often cut off while the conversation is still being written. Text without messages must be an exact prefix.
- A snapshot is **kept, not deleted**. It is archived, then moved from `inbox/` to `duplicates/<sub>/<same file name>`, and gets `duplicate_of: <subfolder>/<longest file name>` in its frontmatter. It is never sent to the LLM. To undo it, move the file from `duplicates/` back into `inbox/`.
- **Clips with one `id` but different content** are not snapshots of each other. All are processed, and the later page overwrites the earlier one in `epiaku-docs` (overwrite by `id`).
- The comparison only looks at the documents **in `inbox/` in this run**. A longer clip that arrives in a later run is processed and overwrites the page.
- With `--file`, only the named documents are compared. Name a short clip and not its longer copy, and the short clip is processed on its own.

Example: five clips of the "Idea catcher" chat (44 to 68 messages) become one LLM call, on `obsidian github link.md`.

### Failures {#failed-folder}

Only **permanent** problems move a document to `failed/`. **Temporary** ones stall the working copy in `output/` (`stage: deferred`).

- **Frontmatter cannot be parsed, or the file cannot be read:** archived, then moved from `inbox/` to `failed/<sub>/` with an `.error.txt`.
- **LLM output invalid twice** (after the one immediate retry): the working copy moves to `failed/<sub>/`.
- **The rendered page fails validation** (frontmatter, unknown shortcode, tags): the working copy moves to `failed/<sub>/`.
- **Not a failure, the working copy stalls in `output/`:** API rate limit or **budget reached**, provider down (FreeLLMApi or OpenAI), YouTube facts unavailable, a missing model setting. These are logged as `deferred`. A used-up budget is logged as one `ERROR` per run.

The original is always in `archive/`, whatever happens. See [Retrying](#retry).

### What we do not track yet {#no-state}

Which LLM and profile made a page, whether it was sent to `epiaku-docs`, and any error will be kept **per document in Postgres from Stage B on**. In Stage A there is no database, and that is fine: the folder a file is in and its `stage` show where it stands, and the **Python log** shows what happened: one line per file and step with a progress counter (`(2/15) note 6b4d2e: published …`), a final `processed 15/15: 13 published, 2 failed` line, and the reason for every `deferred` (WARNING) and `failed` (ERROR, with a traceback for unexpected errors). Set `LOG_LEVEL` and `LOG_FILE` in `.env`, or pass `--log-level`. There are no metrics, no `stuck` flag and no retry counter until Stage B.

### ID & naming {#id-naming}

To overwrite the right page, every capture needs an ID that **never changes**, whether the note is renamed, edited, re-clipped or re-processed. The page file name is the [calculated file name](#file-names) (`YYYYMMDD-<short guid>-<title>.md`), which is **different for every clip**. That is why the pipeline **finds the existing page by the `id` in its frontmatter, not by the file name**: it removes an older page with the same `id` and writes the new file. The `id` says which content it is, and the file name says which file it came from.

Where the ID comes from, per input type:

| Input                                    | Best ID source                                                                                                                 | Can Obsidian generate it?                                                                                                                                                                                                                                                      |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Short note                               | Generated at creation time and stored in the frontmatter `id`                                                                  | **Yes.** The core _Templates_ plugin can insert a timestamp such as `{{date:YYYYMMDDHHmmss}}`, which is unique enough for one person. The _Templater_ community plugin can also add a random short ID. The core _Unique note creator_ plugin names new notes with a timestamp. |
| Gemini chat                              | The **conversation ID in the chat URL** (for example `gemini.google.com/app/2446cd9c762c9cc9`, with the query string stripped) | Not needed. The Web Clipper stores the URL in `source`, and the pipeline derives the ID from it. This is the only option that makes a **re-clipped, longer version of the same chat overwrite** its earlier page. A clip-time ID would create a new page instead.              |
| YouTube video                            | The **video ID** in the URL (e.g. `Q0pAWZiV2GU`)                                                                               | Not needed. Derived from `source`. Clipping the same video again overwrites its page.                                                                                                                                                                                          |
| YouTube summary made in the Gemini chat  | **`<video-id>-gemini`**, from the YouTube URL in the chat's first message                                                      | Not needed. Derived during ingest. Kept **next to** the directly clipped page of the same video, so the two can be compared.                                                                                                                                                  |
| Note without an ID (forgot the template) | The pipeline generates one when it reads the inbox (in memory: the `archive/` copy stays untouched)                                                   | n/a. A replay from `archive/` derives the same ID only if it comes from the `source`; a generated ID is new, so it would create a second page.                                                                                                                                                                                                                            |

Does this always find the matching page? Yes, as long as the ID is created once and never regenerated. The remaining edge cases are:

- A note that was **copied** in Obsidian keeps the same ID and will overwrite the original's page. Clear the `id` when duplicating a note.
- A Gemini chat exported **without** a URL has no stable key, so it falls back to a generated ID and becomes a new page.

The `output/` page and the `epiaku-docs` page are the same text, so they carry the same `id` in their frontmatter, while the file keeps its inbox name. The `archive/` copy has an `id` only if the note already had one.

### Processing-state options

- **Five folders, a run only reads `inbox/`, a `stage` field on the working copy, then Postgres** (chosen). Visible at a glance, a document can never be started twice, what is left in `inbox/` is exactly what is left to do, the untouched original is always kept, and a retry is a file move.
- **Work in place in `inbox/`, move only when done.** Tried and dropped: a document could be started twice, and there was no place to see that it stalled and why.
- **One folder per stage** (`staging/`, `analyzed/`, `published/`). The state is the folder, but there are many Git moves per note and many folders.
- **`processed: true` flag in frontmatter.** No file moves, but files pile up on the phone.
- **GitHub Issues** (open = inbox, closed = done). Needs a connection at capture time and leaves no files to replay.

---

## 🧠 Stage 4: Process & Summarize {#process}

### Document classes & models {#doc-classes}

Captures fall into a few clear classes. Each gets its own prompt, target folder and default LLM profile (a job message can override the profile):

| Class                | `type`                                                     | What it is                                                                                  | Processing                                                                                                                                                                                                                                                                             | Default profile      | Target in epiaku-docs                          | Folder in `inbox/` `archive/` `output/` `failed/` |
| -------------------- | ---------------------------------------------------------- | ------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------- | ---------------------------------------------- | -------------------- |
| **Notes**            | `note`                                                     | Dictated on the phone. Very short: a reminder or an idea captured before it's forgotten.    | Clean up speech fillers, keep the tone, add a title and tags. Don't pad a two-line idea into a long page.                                                                                                                                                                              | `notes`          | `idea-bucket/notes/`                           | `notes/` |
| **AI chats**         | `ai-chat` (from a gemini.google.com or claude.ai `source`) | Web-clipped Gemini or Claude conversations. Can be very large (100+ messages, ~50K tokens). | Condense: drop fluff and detours, keep decisions and options, keep only the latest version of any code. Output a structured page like this one.                                                                                                                                        | `clippings` | `idea-bucket/clippings/` | `clippings/` |
| **YouTube**          | `youtube`                                                  | A link to a video.                                                                          | Analyse with the [YouTube summary prompt](/docs/products/youtube/youtube-tech-stack/youtube-summary/#example-prompt-1-best--most-precise): purpose, examples, action plan, tools, tips and how to apply it to the channel. See [YouTube videos](#youtube-videos). One call, on Python's fetched transcript and facts. | `youtube` | `idea-bucket/youtube/`                         | `clippings/` |
| **YouTube (Gemini)** | `youtube-gemini`                                           | A Gemini web chat that ran the YouTube summary prompt, clipped in Obsidian.                 | One call, on its own prompt: reformats Gemini's answer into the YouTube page format as it is. No YouTube API call of any kind — not `yt-dlp`, not the transcript endpoint.                    | `youtube` | `idea-bucket/youtube/`                         | `clippings/` |
| _(missing)_          | –                                                          | A note without a template                                                                   | Treated as a short note, so nothing is dropped.                                                                                                                                                                                                                                        | `notes`          | `idea-bucket/notes/`                           | `notes/` |
| **Web clips** | `web-clip` (any other `source` address) | An article, blog post, documentation page or tutorial clipped with the Web Clipper. | Condense: ignore menus, cookie notices and ads, keep the facts and steps, give key points and ideas to use it. Output: summary, key points, ideas to use it, details. | `clippings` | `idea-bucket/web-clips/` | `clippings/` |

The volume is small: a few short notes a day and a few chats and videos a week. Processing them is a bit like a code review of the new files in a repo, which is what a single API call per note handles well.

### One class, one prompt, one call {#one-prompt-per-class}

Every class works the same way: one detection rule picks the class, the class names its own prompt file and its own profile, and `reason()` makes exactly **one** LLM call. There is no separate "reviewer" step and no second call for any class — the design used to run a second check call for `youtube`, but that call was checking a summary already built from the real transcript, so it added cost without adding a source of truth.

| Class            | Detected from                                    | Prompt              | Provider/model (profile) | LLM calls |
| ---------------- | ------------------------------------------------- | -------------------- | ------------------------- | --------- |
| `note`           | no `source`, or an unrecognised one                | `note.md`             | `notes` (FreeLLMApi)       | 1         |
| `ai-chat`        | Gemini/Claude chat, not about a video              | `ai-chat.md`          | `clippings` (OpenAI)       | 1         |
| `web-clip`       | any other clipped web page                         | `web-clip.md`         | `clippings` (OpenAI)       | 1         |
| `youtube`        | a direct YouTube clip                              | `youtube.md`, given Python's transcript, title, counts | `youtube` (OpenAI) | 1         |
| `youtube-gemini` | a Gemini chat that summarized a video              | `youtube-gemini.md`, given only Gemini's own answer — no facts, no transcript | `youtube` (OpenAI) | 1         |

**Free checks apply only to `youtube`.** For that class, Python checks the finished summary against the facts it was given — a timestamp later than the end of the video, or a tool named that isn't in the transcript, title or description — and reports them as warnings in the page's frontmatter. Nothing is "fixed" by a second model call; the warning just tells you where to look.

**`youtube-gemini` makes no YouTube API call at all.** It never fetches a transcript or video counts — not even in the background — so it is unaffected by YouTube blocking or rate-limiting that endpoint, and its page has no real metrics table. If Gemini wrote one in its chat, the page shows those numbers as written, under the same `## 📊 Metrics` heading. They are copied by the LLM, never computed or rounded, and a value Gemini marks "Not available" stays "Not available". The only thing Python does locally is parse the video ID out of the YouTube URL already in Gemini's own chat text (no network call). The page is Gemini's own summary, reformatted into our schema; there is nothing to check it against, so no free checks run for this class.

### Web clips {#web-clips}

A page clipped with the Web Clipper from a site that is not a chat and not a YouTube video is a `web-clip`.

- **Prompt:** its own, [`web-clip.md`](../../src/catcher/modules/llm/prompts/web-clip.md). It ignores page furniture (menus, cookie notices, ads, comments), keeps facts and steps, and does not copy long passages.
- **Output:** a `WebClipSummary`: title, description, summary bullets, key points, ideas to use it, and a detailed body. The page has the sections *Summary*, *Key Points* and *Ideas to Use It* (empty ones are left out), the details, and a link to the source.
- **Profile:** `clippings` (the OpenAI profile), like AI chats.
- **Where the page goes:** `hugo/content/en/docs/idea-bucket/web-clips/`. That folder needs an `_index.md` (a section page, like the other folders have) in the epiaku-docs repo.
- **Id:** the same page clipped again gets the same `id`, so its page is replaced. The id is a short hash of the address without tracking parameters (`utm_*`, `gclid`, `fbclid` and so on), without the fragment, `www.` or a trailing slash. A query that names the page (`?id=42`) is part of it.
- **The link on the page** is the address without the tracking parameters and the fragment.
- **A channel or playlist page on YouTube** (a YouTube address without a video) is also a web clip.

### YouTube videos {#youtube-videos}

The video doesn't need to be watched. **Python fetches the video's data as text**, and the `youtube` profile's model runs the [YouTube summary prompt](/docs/products/youtube/youtube-tech-stack/youtube-summary/#example-prompt-1-best--most-precise) on that text. The Gemini chat app does something equivalent with a YouTube link, but Google has not published exactly how, so we treat it as unverified rather than assume it works the same way.

**The clip already contains part of the data**, so Python only has to fetch the rest:

| Prompt needs                        | Where it comes from                                                                                       |
| ----------------------------------- | --------------------------------------------------------------------------------------------------------- |
| Title                               | Clip frontmatter `title`                                                                                  |
| Creator                             | Clip frontmatter `author` (strip the `[[ ]]` Obsidian link brackets)                                      |
| Publish date                        | Clip frontmatter `published`                                                                              |
| Description                         | **Clip body.** The frontmatter `description` is cut off, but the body holds the full text.                |
| Video ID                            | From the `source` URL. Also used as the stable page ID.                                                   |
| Views, likes, subscribers, duration | **Fetched by Python** (`yt-dlp` or the Data API)                                                          |
| Transcript                          | **Fetched by Python** (`yt-dlp`, from the same extraction as the counts)                                  |
| Why you clipped it                  | Optional: type a line at the top of the body when clipping. The prompt uses it for "Channel Application". |

In Python, the fetched data comes from these sources:

| Data                                                                  | Source                                                                                              | Notes                                                                                                                                                                            |
| --------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Title, channel, thumbnail (only for links pasted into a plain note)   | **YouTube oEmbed API** (`https://www.youtube.com/oembed?url=…&format=json`)                         | No key. Already used by the `epi-hugo-youtube` scripts. Not needed for Web Clipper clips, which already have title and author. Returns **no** transcript, description or counts. |
| Description, views, likes, channel subscribers, upload date, duration | **`yt-dlp`** (as a Python library, without downloading the video)                                   | No key, one call. Unofficial: it reads YouTube's web pages, so it can break when YouTube changes and needs regular updates.                                                      |
| Same, official route                                                  | **YouTube Data API v3**: `videos.list` (`snippet`, `statistics`) and `channels.list` (`statistics`) | Free API key, 10,000 quota units a day (one lookup costs about 1 unit). Stable, works from anywhere. It can't download transcripts of other people's videos.                     |
| Transcript                                                            | **`youtube-transcript-api`**                                                                        | No key. Unofficial as well. Some videos have no transcript.                                                                                                                      |

> **Updated 2026-10-01.** Stage A fetches the info **and** the transcript with **one yt-dlp extraction** (about 3 paced requests), and `youtube-transcript-api` was dropped: it cannot be paced, it repeats requests yt-dlp already makes, and it shares the same IP. See [YouTube IP bans and the queue](../idea-catcher-youtube-bans-and-queue-options/).

Tested from the home network (September 2026) on two videos, including the clip above: `yt-dlp` returned the title, channel, subscribers (1.56M), views (1.4M), likes (46K), upload date, duration and description. The transcript library returned the full English transcript (2,374 words; about 7,900 for a 37-minute video).

```python
import yt_dlp  # pip install yt-dlp
from youtube_transcript_api import YouTubeTranscriptApi  # pip install youtube-transcript-api (v1.x)


def fetch_youtube(video_id: str) -> dict:
    url = f"https://www.youtube.com/watch?v={video_id}"
    with yt_dlp.YoutubeDL({"skip_download": True, "quiet": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    try:
        snippets = YouTubeTranscriptApi().fetch(video_id, languages=["en"])
        transcript = " ".join(s.text for s in snippets)
    except Exception:
        transcript = None  # no transcript: summarize from the description
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
- The counts are a snapshot. Store the fetch date with them (prompt step 4, "Metrics As Of"). The metrics table also shows the video's upload date ("Published"), because a tutorial can be out of date.
- **Chapters and links (`youtube` class):** the page has a `## 🗂️ Chapters` outline made by code from the facts (start time and title), when the video has chapters. It also has a `## 🔗 Links` section with the code, sample-data and documentation links the video points to. The LLM picks them from the description, and code keeps only URLs that are literally in the description (at most 6), so an invented link can never reach a page. The `youtube-gemini` class has no links, because it has no description to check them against. It does keep the chapters that Gemini lists (the Gemini prompt asks for them as item 5), as `time` and `title`. A chapter without a clock time or a title is dropped, not an error. They cannot be checked against YouTube, so treat them as Gemini's word.

Also:

- **Metrics come from Python, never from the LLM.** An LLM would make up views, likes and subscriber counts. Python fills them into the template's metrics table. The one exception is the `youtube-gemini` class, which has no facts: it only shows what Gemini itself wrote.
- **Embed with the site's shortcode.** Prompt step 11 asks for a thumbnail link. On this site the template renders the video with the `youtube-lite` shortcode instead, following the `epi-hugo-youtube` conventions.
- **No transcript available:** the document defers rather than calling the LLM. A title-and-description-only summary isn't worth paying for, so `youtube` waits (`stage: deferred`) until a transcript can be fetched, rather than publishing a weak page.
- **Where to fetch: at home.** `yt-dlp` and transcript requests work from the home network (tested), but YouTube often blocks them from cloud IPs such as GitHub Actions runners. So the worker on Proxmox fetches them during ingest and writes them into a facts file next to the final page in `output/` (see [Processing flow](../idea-catcher-service-architecture/#mvp-flow)).

### Manual route until the pipeline runs {#manual-route}

The pipeline above is designed, not running yet. Until it is, a YouTube summary in this format has to be produced by hand. This happened once (September 2026, on the [Justin Sung productivity video](../../youtube/productivity-is-hard-until-you-build-systems-like-this/)) using Claude Code directly, and it took about **17 minutes** — worth recording why, since Gemini's own web chat does the same thing in about 10 seconds.

**Why the difference:** Gemini's web chat has **native video understanding** — it can ingest a YouTube URL directly and watch/process the video server-side in one model call. Claude Code has no equivalent, so getting the same grounding meant assembling the content piece by piece through browser automation instead: an oEmbed call for title/author, navigating the live YouTube page to scrape the description and metrics, a failed attempt at the on-page transcript panel (stuck loading spinner), a fallback to inspecting network requests for YouTube's internal caption API and fetching that directly, and only then writing the file. Each of those is a separate tool round-trip; none of it is inherent to the task, it's overhead from not having a direct video-understanding pipeline.

**Three options going forward:**

| Option                                              | What it needs                                                                                                                                                                                                                                          | Notes                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| --------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **1. Gemini API, called directly**                  | A Gemini API key, the `google-genai` SDK. A small script (~20 lines): pass the YouTube URL + the [YouTube summary prompt](/docs/products/youtube/youtube-tech-stack/youtube-summary/#example-prompt-1-best--most-precise), save the returned markdown. | Same native video understanding as the web chat, so no scraping needed and no 17-minute detour for the _content_ analysis. **Cannot replace `yt-dlp`**, though: it only sees the video stream (audio + frames), never the surrounding webpage, so it has no access to view/like/subscriber counts at all — those have to keep coming from `yt-dlp`/the Data API regardless of which summarizer is used. See [Gemini API video understanding](#gemini-video-api) below for the verified details, and the comparison right after it. |
| **2. Automate the Gemini web chat via browser**     | Browser automation, a signed-in Gemini session.                                                                                                                                                                                                        | Not recommended: fragile against UI changes, likely against Gemini's consumer ToS for automated use, and no real speed win over calling the API directly.                                                                                                                                                                                                                                                                                                                                                                          |
| **3. Paste Gemini's output, have Claude format it** | Nothing new — works today.                                                                                                                                                                                                                             | Run the prompt in Gemini web chat yourself (fast), paste the markdown output to Claude Code, which saves it to the right file with correct frontmatter and rebuilds the site to verify. Useful right now, but redundant with the capture flow below once that's the habit.                                                                                                                                                                                                                                                         |

**The Obsidian route we can already use today**, without waiting for the pipeline or a Gemini API integration: run the video through Gemini's web chat with the YouTube summary prompt, then clip that Gemini chat page with the **Obsidian Web Clipper** into `inbox/clippings/`, like any other AI-chat capture. The service detects it as a **`youtube-gemini`** doc (a Gemini `source` whose first message contains a YouTube URL): its own `youtube-gemini.md` prompt reformats Gemini's answer into the page format in one call — no YouTube API call at all (see [One class, one prompt, one call](../idea-catcher-service-architecture/#mvp-one-prompt-per-class)). This gets the summary into the idea-bucket vault immediately, using infrastructure that's already set up, even though nothing will _process_ it into a docs page until Stage 3/4 actually runs. It also means the manual step (option 3) is only needed for a summary you want published on the site **right now**, before the pipeline exists — otherwise, clip and let it sit in the inbox for the pipeline to catch up to later.

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
      model="gemini-3.8-flash",
      input=[
          {"type": "text", "text": "Please summarize the video in 3 sentences."},
          {"type": "video", "uri": ""},
      ],
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
| Transcript                          | **Deterministic.** Pulls YouTube's own stored captions file verbatim — the exact same text every run.                                                                        | **Unverified.** Google has not published how Gemini reads a YouTube link internally, so we don't know whether it reads the same captions file or something else, and whether it varies run to run. Useful as a fallback when a video has no captions at all. |
| Summary, purpose, tips, action plan | Needs a **separate LLM call** on top of the fetched transcript/description (Claude, Gemini text, FreeLLMApi — any of the already-compared [LLM options](#claude-vs-openai)). | **One call does it all** — video in, structured summary out. Fewer moving parts, but the summary's factual grounding is only as good as the model's own video understanding, not a checkable source text.                                            |

**Conclusion: keep the already-decided design** — `yt-dlp` for metrics (the only reliable source for those, full stop) and `youtube-transcript-api` for the transcript (_since 2026-10-01 the transcript also comes from `yt-dlp`_; exact, reproducible, and lets a human or a test suite check the summary against the real source text) — and only fall back to Gemini's video understanding for videos that have **no captions available**, where there's no deterministic transcript to fall back on anyway. Gemini (or any other LLM) still does the actual summarizing step in both cases, per [Document classes](#doc-classes).

### Division of work: Python vs. LLM

| Strategy                                | How it works                                                                                                                                                         | Reliability                                        |
| --------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------- |
| AI does everything                      | Send the raw note, get back a complete Hugo page including frontmatter and path.                                                                                     | Risk of invalid frontmatter or made-up paths.      |
| **Python first, AI last** (recommended) | Python handles the ID, filename, paths and frontmatter. The LLM returns only structured JSON (title, tags, summary, body). Python validates it and renders the page. | High: code guarantees valid Hugo syntax and paths. |

The small local `needle` model is **no longer an option**. [cactus-compute/needle](https://github.com/cactus-compute/needle) is a tiny edge model (Needle 3: 121M parameters, 8–29 MB) for tool calling, structured extraction and embeddings on devices. It gives up general reasoning, has no server or API and no stated context length, so it cannot summarize a 100-message Gemini chat. Classification is already handled by the `type` field anyway.

### LLM options

The ideas are not sensitive, so privacy is not a deciding factor. **Decision: every LLM call is an API call with an API key.** There is no CLI and no subscription route. The first setup has three profiles:

| Profile     | Used for                                         | Provider                                       | Why                                                                                       |
| ----------- | ------------------------------------------------ | ---------------------------------------------- | ----------------------------------------------------------------------------------------- |
| `notes`     | Short dictated notes                             | **FreeLLMApi** (home proxy to free tiers)      | Free, and short notes are easy work.                                                      |
| `clippings` | AI chats (Gemini, Claude) that are not YouTube   | **OpenAI API**                                 | Long inputs (up to ~50K tokens) need a large context and reliable JSON. Pay per token.    |
| `youtube`   | `youtube` and `youtube-gemini` | **OpenAI API**                                 | Long transcripts for `youtube`. Own profile so the model can differ from `clippings`. |

**Why the subscription route (`claude -p`) is gone:** it was unreliable (the CLI hangs, changes output and hits usage limits at unpredictable times), it only worked while a Claude login was valid, it shared the weekly limit with coding, and it could not run in the cloud. An API with a key works the same at 3 a.m. on the Mac, on Proxmox or in a cloud container. See [the removed option](#leftover-usage).

| Option                                                         | How you pay                                            | Where it can run                     | Fit                                                                                                                                                   |
| -------------------------------------------------------------- | ------------------------------------------------------ | ------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| **FreeLLMApi** (home proxy to free tiers), **`notes`**         | Free                                                   | Home LAN only                        | Fine for short notes. Long chats are the risk (rate limits, context size, uneven quality), so it is not used for them.                                |
| **OpenAI API**, **`clippings` and `youtube`**                  | Pay per token, one key                                 | Anywhere                             | Same client as FreeLLMApi (OpenAI-compatible), JSON mode, 1M context. At a few chats and videos a week the bill stays small.          |
| Claude API (Anthropic Console)                                 | Pay per token, separate from any subscription          | Anywhere                             | Later, as another `backend` for a profile. Needs its own client (the Anthropic SDK).                                                                  |
| Gemini API                                                     | Pay per token                                          | Anywhere                             | Later. Very large context windows suit long chat exports.                                                                                             |

The LLM call sits behind one small interface, so the provider and model are chosen **per profile** in `profiles.yaml`. Moving `youtube` to another provider, or `notes` to a paid one, is a config change and not a rewrite. For the paid options, see [LLM Choice: Claude API vs. OpenAI API](#claude-vs-openai). See [LLM backends](#llm-backends) for the switchable backend (`freellmapi` / `openai` now; `anthropic-api` / `gemini-api` later).

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
- **Per page:** **at most one idea-type tag** (optional: the LLM adds one only when it clearly fits, and a page without one is published normally), plus **1–4 topic tags**, plus optionally a **project tag**. Tags are a convenience, not a requirement: most pages on the site have none, so a missing tag never fails a page. More than one idea-type tag is still rejected.
- **Capture nudge:** a note may already contain `tags:` from Obsidian. These go through the same validation.

#### Starter tag list

Derived from the current sections and pages of epiaku-docs.

**Idea type** (at most one):

At most one idea type. If a model returns several, the one that is higher in this table wins (the order is the order of `idea_types` in `tags.yaml`); `tech-note` is the catch-all and always loses.

| Tag                    | Meaning                                              | Curated page that links to the tag page |
| ---------------------- | ---------------------------------------------------- | --------------------------------------- |
| `youtube-idea`         | Video idea for one of the channels                   | `youtube-ideas/`                        |
| `saas-idea`            | SaaS service idea                                    | `saas/ideas/`                           |
| `app-idea`             | Phone or web app idea                                | `apps/ideas/`                           |
| `digital-product-idea` | Course, template, e-book or other digital product    | `digital-products/`                     |
| `ai-influencer-idea`   | AI persona or influencer concept                     | `ai-influencers/`                       |
| `todo`                 | Something to do, not an idea                         | `todo/`                                 |
| `strategy`             | Business direction, positioning, freelancing         | `strategy/`                             |
| `tech-note`            | Learning, tool or architecture note (like this page) | `tech-stack/`                           |

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

These are the two main paid options. **OpenAI is the first choice** for `clippings` and `youtube` (see [LLM options](#llm-options)); Claude is the alternative to test. Prices are per 1 million tokens, standard tier, as of September 2026. Check the pricing pages before deciding.

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
| Fit with FreeLLMApi    | Needs its own client (the official Anthropic SDK). The pipeline would need a second client next to the OpenAI-style one.                                                       | FreeLLMApi is OpenAI-compatible, so the **same client works for both**. Switching is only a change of base URL, key and model name. |
| House style            | Claude Code can reuse the repo's `epi-hugo-*` skills when running as an agent (strategy B).                                                                              | Style has to be in the prompt.                                                                                                      |

### Recommendation

1. **Short notes:** stay on FreeLLMApi (`notes`). If it is not good enough, the cheap tier is almost free: GPT-6 Luna, or Haiku 4.5.
2. **Long chats and YouTube (`clippings`, `youtube`):** start with **OpenAI GPT-6 Sol**. Claude Sonnet 5 costs the same, so run the same ~10 real exports through both and compare the pages before switching.
3. **Easiest code path:** OpenAI, because it shares the client with FreeLLMApi. Both are one `openai`-style backend with a different base URL, key and model.
4. **Try the top tier only if** the mid-tier output misses important decisions.
5. **API keys only.** No subscription login is used anywhere: a key works in any tool, on any machine, at any hour, can be rotated and has its own limits.

### Making free models work (FreeLLMApi)

Free models (larger Llama, Qwen, DeepSeek, gpt-oss, Gemini Flash) are usually good enough for summarizing, if you:

1. **Pin a model** (or a short fallback chain) instead of `auto`, for a consistent style. The service starts with `auto` and makes the model an env var (`FREELLMAPI_MODEL`), so pinning later is a config change.
2. **Validate the JSON with pydantic** and retry once, adding the validation error to the prompt.
3. **Split long documents** into chunks when a free tier's context or tokens-per-minute limit is too small for a long Gemini chat.
4. Remember that some free tiers train on prompts. That's fine for these ideas, which are not sensitive.

### Removed: leftover Claude subscription usage {#leftover-usage}

**This option was dropped.** The idea was to run summaries with `claude -p` at the end of the coding day on subscription usage that would otherwise be lost. In practice the CLI route was unreliable, depended on a valid login and on how much you coded that day, shared the weekly limit with coding, and could only run where the `claude` binary is logged in. Everything it gave us is now done by an API key:

| What `claude -p` was for       | What replaces it                                                                                                                                              |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| No per-token bill              | A small pay-per-token bill for `clippings` and `youtube`. The volume is a few chats and videos a week.                                                        |
| Evening window (`when: evening`) | Not needed. An API can be called 24/7, so there is no `when` option and no evening window. Runs happen on the normal schedule.                              |
| Stop the run at the usage limit | The same rule for **any provider's rate limit or budget**: stop calling that provider for the rest of the run, defer its notes, and finish the other providers. A **used-up budget** is reported as its own `ERROR` and retried less often (`budget_retry_delay`, 6 hours) than a rate limit (`retry_delay`, 60 minutes). |
| `CLAUDE_CODE_OAUTH_TOKEN`, `claude setup-token`, Claude Code CLI in the container | `OPENAI_API_KEY` (and `FREELLMAPI_*`) in `.env` or the container's secrets. No CLI in the image. |
| Backlog alarm                  | Unchanged: a note that stays stalled (`stage: deferred`) for 3 days is flagged `stuck` (Stage B).                                                        |

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
| FreeLLMApi, 1–2 pinned models (e.g. a large Llama or Qwen, Gemini Flash) | `freellmapi` backend (the `notes` profile)              |
| GPT-6 Sol                                                                | `openai` backend (the `clippings` and `youtube` profiles) |
| Claude Sonnet 5                                                          | Claude API, as the alternative to compare               |
| Optional: GPT-6 Luna                                                     | `openai` backend, for cheap notes if FreeLLMApi disappoints |

Use the same prompt, schema and templates for every candidate.

### What to measure

| Automatic checks (per run)                                    | Human review (per summary, score 1–5)                                   |
| ------------------------------------------------------------- | ----------------------------------------------------------------------- |
| Valid JSON on the first try, and after one retry              | **Completeness:** are the decisions and options all there?              |
| Schema and frontmatter valid, Hugo build passes               | **Accuracy:** nothing made up or wrongly merged                         |
| Tags only from the allowed list                               | **Clean-up:** detours and duplicates removed, only the latest code kept |
| Long chats handled without hitting context or rate limits     | **Readability:** structure and grammar, fits the site's style           |
| Time per note, tokens used, cost | **Usefulness:** would you link to this page from a curated section?     |

For a fair human review, put the outputs side by side **without the model name** (blind), and score them. An LLM can pre-score against the same rubric to save time, but the final call stays with a person.

### Decision rules

Agree on these before looking at the results:

- **FreeLLMApi scores ≥ 4 on average and ≥ 90% valid JSON for short notes** → keep `notes` on FreeLLMApi.
- **FreeLLMApi is not good enough for short notes** → point `notes` at the cheap OpenAI tier (a config change).
- **GPT-6 Sol scores clearly below Claude Sonnet 5 on long chats or transcripts** → point `clippings` or `youtube` at the Claude API (a config change).
- **The cheap tier is about as good as the mid tier for chats** → use the cheap tier and save money.

### Keeping it useful

- Keep the test set in the pipeline repo, and re-run it whenever the prompt, template or model changes. That catches regressions.
- Save the results (scores, token counts, a few sample outputs) in a small report, so the decision is documented.
- Every candidate is an API call and costs tokens, so keep the test set small (10–15 documents).

---

## 🏗️ Strategies to Build the Pipeline {#strategies}

{{% alert title="Updated: the service replaces the two-stage rocket" color="info" %}}
The earlier choice was a **two-stage rocket**: Python on Proxmox prepared notes, and a GitHub Action ran Claude in the evening. New requirements changed that: start runs from an API, summarize a YouTube URL on demand, store metrics per run, and show a React dashboard. The pipeline now runs as the **Idea Catcher Service** at home, built as an MVP first. The full design is on [Idea Catcher Service: Architecture](../idea-catcher-service-architecture/). This section keeps the options and records why the choice changed.
{{% /alert %}}

### Where the processing job runs

**Decision: strategy F, the Idea Catcher Service on Proxmox.** It is A (Python at home) grown into a service. The GitHub Action (B) and the evening run on leftover Claude usage (E) are dropped: every LLM call is an API call with a key, which needs no Claude login and no evening window.

| Strategy                                | Description                                                                                                                                                                                                 | Pros                                                                                                       | Cons                                                                                                                             |
| --------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| A. Python on Proxmox                    | One Python script in a small LXC, run on a schedule. It calls the chosen LLM API.                                                                                                                           | Full control, easy to test and replay, can deploy the site directly, no inbound ports.                     | No API, no metrics, no on-demand runs.                                                                                           |
| B. GitHub Action + Claude subscription (dropped) | A workflow in idea-bucket, triggered on a schedule. Claude Code does the summarizing and commits to epiaku-docs `main`.                                                                                     | Nothing to host. Uses the existing subscription. Logs in GitHub.                                           | Can't answer an API call within a minute. YouTube fetches are often blocked from cloud IPs. Metrics are spread over Action logs. |
| C. Hybrid                               | GitHub Action does steps 3–5 (Python + LLM API). The home server only pulls and deploys.                                                                                                                    | Clean split between processing (cloud) and hosting (home).                                                 | Two places to look when something breaks.                                                                                        |
| D. n8n / Windmill on Proxmox            | A visual workflow tool.                                                                                                                                                                                     | Visual flows, run history in a UI.                                                                         | Heavy always-on server. The YouTube and LLM logic is still Python.                                                               |
| E. Evening run on leftover Claude usage (dropped) | The `claude -p` CLI, run after coding hours. | No extra cost. | Unreliable, tied to a login and to how much you coded that day, cannot run in the cloud. Replaced by API keys (see [the removed option](#leftover-usage)). |
| **F. Idea Catcher Service** (chosen)    | API + Postgres job queue + worker in one LXC with Docker Compose. Every LLM job carries its own options: the profile (provider and model) ([details](../idea-catcher-service-architecture/#mvp-llm)). | API triggers, metrics per run and per note. Later: dashboards and on-demand YouTube summaries. | More to build and run than a script.                                                                                             |

### Orchestration: Proxmox, GitHub, or both {#orchestration}

|                                    | 1. All on Proxmox      | 2. Proxmox starts the Action and waits | 3. All in GitHub                     | 4. Two-stage rocket (previous choice) | **5. Idea Catcher Service (chosen)**                      |
| ---------------------------------- | ---------------------- | -------------------------------------- | ------------------------------------ | ------------------------------------- | --------------------------------------------------------- |
| Cleaning, IDs, adding context      | Proxmox                | Proxmox                                | GitHub                               | Proxmox                               | **worker (Proxmox)**                                      |
| YouTube fetch (transcript, counts) | Proxmox (works)        | Proxmox (works)                        | GitHub (often blocked for cloud IPs) | Proxmox (works)                       | **worker (works)**                                        |
| LLM step                           | API call from the LXC  | In the Action                          | In the Action                        | In the Action                         | **`llm.reason` job: profile per message (provider + model)** |
| Writing pages, archiving           | Proxmox                | Proxmox                                | GitHub                               | GitHub                                | **worker (`pipeline.publish`)**                           |
| Deploy to the home web server      | Directly               | Directly                               | Cron pull at home                    | Cron pull at home                     | **Manual `deploy.sh`, as today**                          |
| Trigger                            | Timer                  | Timer                                  | Action schedule                      | Timer + Action schedule               | **Scheduler or API: both add a job to the queue**         |
| Metrics                            | Logs                   | Logs                                   | Action logs                          | Logs in two places                    | **Postgres, per run and per note**                        |

Setup 5 is setup 1 grown into a service. Moving the LLM step home is what makes API triggers and on-demand YouTube summaries possible. The evening-subscription idea from setup 4 is dropped: the profiles use API keys, so a job can run at any hour.

### What stays from the two-stage rocket {#split-setup}

- **`inbox/` is where a run finds work, and `output/` holds what was started.** A document leaves `inbox/` when work on it starts, so nothing is started twice. The working copy in `output/` (`stage: analyzed`, or `stage: deferred` with the reason after an error) waits there, and the untouched original is in `archive/`.
- **Every step can be re-run.** Publishing overwrites pages by ID. **Replay** a note, or retry a stalled one, by moving its `archive/` file into `inbox/`, or with `POST /items/{doc_id}/replay`. A failed note is moved from `failed/` back to `inbox/` the same way. In Stage A this is a manual file move (see [Retrying](#retry)).
- **If the LLM is unavailable** (quota or rate limit reached, provider down), the working copy stalls in `output/` with `stage: deferred`. The LLM job is logged and becomes `deferred`. There is no fallback from one provider to another. After 3 failed days the note is flagged `stuck`. In Stage A (no database) this is a log line and the status in the file, and you retry by moving the file back into `inbox/`.
- **Prompts are built in Python**, with a template per class. The LLM is a plain API call with a key: no tools, no skills, no CLI.
- **What is gone:** the GitHub Action, `CLAUDE_CODE_OAUTH_TOKEN` and `PIPELINE_TOKEN` as Action secrets, the Claude Code CLI, and the evening run. There is now only one Git writer (the worker), so the two stages can no longer race each other.

### Scheduling on Proxmox {#scheduling}

**Decision: the scheduler runs as a loop inside the service's worker container.** In the MVP the schedule is a cron expression in `.env` (later editable from the dashboard). A due schedule adds a `pipeline.run` job to the queue, exactly like an API call. See [Scheduling](../idea-catcher-service-architecture/#mvp-scheduling) on the service page.

Options that were considered: a **systemd timer** inside the LXC (earlier recommendation: survives reboots, logs to the journal), **cron** in the LXC or in a container (`supercronic`), a **Python scheduler library** (APScheduler), and the Proxmox host starting and stopping the LXC (zero idle resources, more moving parts). A timer that runs `curl -X POST …/pipeline/runs` still works as a fallback trigger, because the API accepts it like any other client.

### Container layout

**Decision: one unprivileged LXC with Docker Compose** (`db`, `api`, `worker` in the MVP), the same pattern as the FreeLLMApi LXC. The same `compose.yaml` runs on the Mac for testing. See [Hosting & Deploy](../idea-catcher-service-architecture/#mvp-hosting).

---

## 🛠️ Implementation Reference {#implementation}

The pipeline's Python code, based on the decisions on this page. It lives in the service's `pipeline`, `llm` and `youtube` modules. The [service page](../idea-catcher-service-architecture/) covers the queue, the API, the database and hosting.

**Principle:** Python does everything that needs no judgement (ingest, IDs, routing, rendering, Git, deploy). The LLM only turns one note into validated JSON, through one generic `reason()` function.

### Layout

```text
src/catcher/modules/
├── pipeline/
│   ├── doctypes.py         # registry: type → schema, prompt, template, default LLM profile, folders
│   ├── jobs.py             # pipeline.run, pipeline.publish, pipeline.replay_item
│   ├── tags.yaml           # allowed tag list (or read from a Hugo data file)
│   ├── glossary.yaml       # often-misheard words for the note prompt (dictated notes)
│   └── templates/
│       ├── note.md.j2
│       ├── ai-chat.md.j2
│       └── youtube.md.j2   # adds metrics table + youtube-lite embed
├── llm/
│   ├── service.py          # reason(): prompt → backend → validated pydantic object + usage
│   ├── backends/           # MVP: freellmapi, openai, fake (later: anthropic_api, gemini_api)
│   └── prompts/
│       ├── note.md
│       ├── ai-chat.md
│       └── youtube-summary.md   # based on the YouTube summary prompt
├── youtube/                # facts (yt-dlp, transcript), verify, render
└── core/github_auth.py     # GitHub App (or fine-grained PAT) tokens
```

### Document-type registry

The class comes from the domain of the Web Clipper's `source` URL (a page on any other site is a `web-clip`), or from an explicit `type` set by an Obsidian template. No folder or filename guessing is needed.

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


class YoutubeSummary(BaseModel):  # sections of the YouTube summary prompt
    title: str
    creator: str
    summary: str
    main_purpose: str
    key_examples: list[str]
    action_plan: list[str]
    tools: list[str]
    tips: list[dict]  # tip, explanation, how_to_apply
    channel_application: str
    tags: list[str]
    # metrics and the video embed are added by Python, not by the LLM


@dataclass
class DocType:
    name: str
    type: str  # explicit frontmatter `type`
    hosts: tuple[str, ...]  # domains of the `source` URL that map to this class
    schema: type[BaseModel]
    prompt: str  # file in prompts/
    template: str  # file in templates/
    out_dir: str  # folder in epiaku-docs
    llm_profile: str  # default LLM profile, overridable per job message
    review: bool = False  # has a review prompt; the message's review.enabled (default true) applies


DOCS = "hugo/content/en/docs/idea-bucket"
DOC_TYPES = [
    DocType("note", "note", (), NoteSummary, "note.md", "note.md.j2", f"{DOCS}/notes", llm_profile="notes"),
    DocType(
        "ai-chat",
        "ai-chat",
        ("gemini.google.com", "claude.ai"),
        ChatSummary,
        "ai-chat.md",
        "ai-chat.md.j2",
        f"{DOCS}/clippings",
        llm_profile="clippings",
    ),
    DocType(
        "web-clip",
        "web-clip",
        (),  # any other http(s) `source`, detected in detect()
        WebClipSummary,
        "web-clip.md",
        "web-clip.md.j2",
        f"{DOCS}/web-clips",
        llm_profile="clippings",
    ),
    DocType(
        "youtube",
        "youtube",
        ("youtube.com", "youtu.be"),
        YoutubeSummary,
        "youtube.md",
        "youtube.md.j2",
        f"{DOCS}/youtube",
        llm_profile="youtube",
        review=True,
    ),
    DocType(
        "youtube-gemini",
        "youtube-gemini",
        (),
        YoutubeSummary,
        "youtube-from-gemini.md",
        "youtube.md.j2",
        f"{DOCS}/youtube",
        llm_profile="youtube",
        review=True,
    ),
    # detected in detect(): gemini.google.com source + a YouTube URL in the first user message
]


def detect(fm: dict) -> DocType:
    host = urlparse(fm.get("source") or "").netloc.removeprefix("www.").removeprefix("m.")
    wanted = fm.get("type")
    if wanted:  # 1. explicit `type` from an Obsidian template
        matches = [t for t in DOC_TYPES if t.type == wanted]
        for t in matches:  #    e.g. ai-chat: pick Gemini or Claude by domain
            if host in t.hosts:
                return t
        if matches:
            return matches[0]
    for t in DOC_TYPES:  # 2. domain of the Web Clipper `source` URL
        if host and host in t.hosts:
            return t
    if host:  # 3. any other web address: a page clipped with the Web Clipper
        return by_name("web-clip")
    return DOC_TYPES[0]  # 4. no source: treat as a note
```

Adding a type (like `web-clip`, added later) = one entry here, one prompt, one template, one output schema. `prompt_version` (not shown) is stored with every result, so replays are traceable. For `youtube`, Python first fetches the transcript and the counts; title, author and description come from the clip.

### LLM backends {#llm-backends}

The generic `reason(request)` picks the provider and model from the job's **LLM options**. The job message's own options come first, then the document class default (the `llm_profile` in the registry above), then the global default. See [LLM step & profiles](../idea-catcher-service-architecture/#mvp-llm) on the service page for the profiles and the retry rules.

The three starting profiles (`profiles.yaml`):

```yaml
default: notes
retry_delay: 60m                   # a deferred job is tried again after this (Stage B)
stuck_after_days: 3
profiles:
  notes:     { backend: freellmapi, model: "${FREELLMAPI_MODEL:-auto}" }
  clippings: { backend: openai,     model: "${OPENAI_MODEL_CLIPPINGS}" }   # e.g. gpt-6-sol, check the exact id
  youtube:   { backend: openai,     model: "${OPENAI_MODEL_YOUTUBE}" }
  fake:      { backend: fake }       # tests and local development only
```

| Backend                     | Client                                                               | Notes                                                                                                                                              |
| --------------------------- | -------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `freellmapi`                | OpenAI client, `base_url=FREELLMAPI_URL` (`http://<h4-ip>:3001/v1`)   | **MVP.** The `notes` profile. Prompt for JSON, validate with pydantic, retry once with the error.                                                   |
| `openai`                    | OpenAI client, `base_url=OPENAI_BASE_URL`, `api_key=OPENAI_API_KEY`  | **MVP.** The `clippings` and `youtube` profiles. Uses JSON mode (`response_format={"type": "json_object"}`), validates with pydantic and retries once. Native schema-enforced outputs can come later. |
| `fake`                      | Canned JSON per schema                                               | **MVP.** Tests and local development.                                                                                                              |
| `anthropic-api`, `gemini-api` | Official SDKs                                                      | **Future**, selected explicitly by a profile, never as an automatic fallback.                                                                       |

`freellmapi` and `openai` are the **same OpenAI-compatible client** with a different base URL, key and model, so a third provider that speaks the same protocol (another proxy, a local server) is one more profile line.

**No fallback between backends.** A temporary failure (rate limit, quota, timeout, provider down) is logged and the note is retried on the **next run**. After 3 failed days the note is flagged `stuck` and keeps retrying. Invalid JSON is retried once at once, on the same profile. A provider that reports a **rate limit or a used-up budget stops all further calls to that provider for the rest of the run**, and the other providers carry on.

**The API key's budget can run out.** The keys are capped at a budget, so the `openai` profiles can stop working until the budget is raised or renewed. That is not a rate limit, so it is handled separately: the backend is blocked for the run, the log gets one `ERROR` per backend (`openai budget reached: 4 note(s) waiting; raise the key's budget or point the profile at another provider`), the documents stall in `output/` (`stage: deferred`) until you move them back into `inbox/` (in Stage B the queue retries them every `budget_retry_delay`). With no fallback, the two ways out are to raise the budget or to change the profile's provider. See [Risks](#risks).

```python
# llm/backends/openai_compatible.py  (used by both `freellmapi` and `openai`)
from openai import APIConnectionError, APITimeoutError, OpenAI, RateLimitError
from pydantic import BaseModel


def run(client: OpenAI, prompt: str, model: str, schema: type[BaseModel]) -> tuple[BaseModel, Usage]:
    try:
        reply = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},  # JSON mode; the freellmapi backend omits this
        )
    except RateLimitError as e:  # includes "insufficient_quota"
        raise UsageLimitReached(str(e)) from e  # job → deferred, provider blocked for this run
    except (APIConnectionError, APITimeoutError) as e:
        raise BackendUnavailable(str(e)) from e  # job → deferred
    obj = schema.model_validate_json(reply.choices[0].message.content)  # invalid → one immediate retry
    return obj, Usage.from_openai(reply.usage)  # tokens for the metrics
```

- One message, no tools: a single transformation.
- The API key comes from the environment (`OPENAI_API_KEY`), is never logged and never written to a page or a sidecar.

### Main flow

A pipeline run is three kinds of jobs: one `pipeline.run`, one `llm.reason` per note, and a `pipeline.publish` that collects every note whose LLM result is ready.

```python
# pipeline/jobs.py – pipeline.run  (default queue; from the scheduler or the API)
@job("pipeline.run", queue="default", unique=True)
async def run(ctx: JobContext, p: RunParams) -> None:
    pull(ideas_repo)
    notes = scan_inbox(ideas_repo, only=p.only)  # read-only: class + id in memory, nothing moves yet
    for rel, reason in notes.errors.items():  # unreadable → archive + failed/ with an .error.txt
        move_to_failed(ideas_repo, ideas_repo / rel, reason)
    winners, duplicates = split_duplicates(notes.notes)  # longest per id; earlier snapshots
    for dup, winner in duplicates:
        move_to_duplicates(ideas_repo, dup, winner)  # archive + duplicates/, no LLM call
    for note in winners:  # only what we are about to work on leaves inbox/ (--limit, --file)
        start_work(ideas_repo, note)  # original → archive/, working copy (stage: analyzed) → output/
        await ctx.enqueue_or_update(  # one open llm.reason per document
            "llm.reason",
            key=note.rel.as_posix(),
            request=LlmRequest(
                task=note.doctype.prompt,
                prompt_version=note.doctype.prompt_version,
                input=prompt_input(note),
                schema_name=note.doctype.schema.__name__,
                llm=ctx.llm_options(default_profile=note.doctype.llm_profile),
            ),  # message > class default
        )
        ctx.item(note.doc_id, note.doctype.name, status="waiting_llm")
    push(ideas_repo)  # pull --rebase first


# pipeline/jobs.py – pipeline.publish  (default queue; folded, so one publish serves many notes)
@job("pipeline.publish", queue="default", fold=True)
async def publish(ctx: JobContext, p: PublishParams) -> None:
    pull(ideas_repo)
    pull(docs_repo)
    for item in ctx.ready_items():  # LLM result stored, not yet published
        doctype = DOC_TYPES_BY_NAME[item.doc_class]
        summary = item.llm_result(doctype.schema)
        summary.tags = keep_allowed(summary.tags)  # unknown tags → tag_suggestion event
        page = render_page(summary, item.frontmatter, doctype.template)
        if problems := validate_page(page):  # frontmatter, required fields, tags, shortcodes
            ctx.item_failed(item, problems)  # output/<sub>/<name>.md → failed/<sub>/ + .error.txt
            continue
        write_page(docs_repo / doctype.out_dir, item.doc_id, page)
        finish(ideas_repo, item, page)  # the working copy in output/ becomes the final page (no stage)
        ctx.item(item.doc_id, item.doc_class, status="published")
    push(docs_repo)
    push(ideas_repo)  # straight to main, docs first
```

- **`write_page` overwrites by ID:** delete any existing page in the folder whose frontmatter `id` matches, then write the page under its calculated name (`YYYYMMDD-<short guid>-<title>.md`). The file name differs for every clip, but the ID never does.
- **Python validation instead of a Hugo build (MVP):** YAML frontmatter parses, `title`, `description`, `weight` and `type: docs` are present, tags come from the allowed list, only known shortcodes are used. Your manual `deploy.sh` build catches anything else.
- **One Git writer.** Only the worker touches the repos, one job at a time. The phone may still push to idea-bucket during a run, so use `pull --rebase` before pushing, and retry once.
- **Per-note errors don't fail the run.** A bad note is marked `failed` (moved to `failed/`, with an `.error.txt`) or `deferred` (stalls in `output/`) in the metrics, and the log says which.
- **No hand-maintained index:** Hugo's section list pages and tag pages build the overviews from frontmatter, so there are no merge conflicts on index files.
- **Follow the site conventions** from the `epi-hugo-docs` and `epi-hugo-youtube` skills in prompts and templates: frontmatter with `title`, `description`, `weight`, `type: docs`, emoji on `##` headers and `youtube-lite` embeds.

### Running on the H4

Everything runs in the service's Compose stack in one LXC (see [Hosting & Deploy](../idea-catcher-service-architecture/#mvp-hosting)). The same `compose.yaml` runs on the Mac for testing.

| Container | Does                                                                                | Needs                                                                                                                                                           |
| --------- | ----------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `worker`  | Scheduler loop, `pipeline.run`, `llm.reason`, `pipeline.publish`, one job at a time | Python, Git, `yt-dlp` (with Deno), GitHub PAT, `OPENAI_API_KEY`, FreeLLMApi URL |
| `api`     | Start runs, list runs and items                                                     | DB, API keys                                                                                                                                                    |
| `db`      | Postgres: the queue and the metrics                                                 | A volume                                                                                                                                                        |

The starting schedule is `pipeline.run` at 08:00, 12:00, 17:00 and 21:00. Every capture is published within one run, because the APIs can be called at any hour. **The docs site deploy stays manual:** pull `epiaku-docs` on the Mac and run `./.github/workflows/deploy.sh`.

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
    return gi.get_access_token(inst.id).token  # valid ~1 hour
```

- Clone into a fresh temporary directory each run (`https://x-access-token:<token>@github.com/...`), so the token never stays in `.git/config`.
- Commit as the bot: `git -c user.name="epiaku-docs-bot[bot]" -c user.email="<app-id>+epiaku-docs-bot[bot]@users.noreply.github.com" commit ...`.
- **Quick start:** a fine-grained PAT (owner `epiaku`, only the two repos, Contents read/write) works immediately. Only `github_auth.py` changes when you switch to the App.
- If `main` ever gets a ruleset that requires pull requests, the bot needs a bypass, or the pipeline has to go back to PRs.

### Next steps

The service is built as an MVP first (see [Build steps](../idea-catcher-service-architecture/#mvp-steps) on the service page):

1. ✅ **Set up capture** — done on both devices: Obsidian + Git plugin on the [iPhone](../obsidian-git-iphone-setup/) and [Mac](../obsidian-git-macbook-setup/), each syncing independently with the `idea-bucket` repo; the Web Clipper saves directly into `inbox/clippings/`.
2. **Stage A, the Python pipeline, run locally:** ingest, `reason()` through the API profiles, rendering, the free YouTube checks and publishing, as plain functions behind a CLI. **Done (2026-10-01).** What was built, what changed from the plan and what we learned: [Stage A: what we built and what we learned](../idea-catcher-stage-a-lessons-learned/). See also [Stage A](../idea-catcher-service-architecture/#mvp-stage-a).
3. **Stage B, Postgres + the queue, locally:** the same functions wrapped in jobs, with next-day deferral, stuck rules, metrics and the scheduler (see [Stage B](../idea-catcher-service-architecture/#mvp-stage-b)).
4. **Stage C, the API, locally:** start runs and read jobs and items over HTTP. The full Compose stack runs on the Mac (see [Stage C](../idea-catcher-service-architecture/#mvp-stage-c)).
5. **Proxmox:** the LXC, `deploy.sh`, the schedule, and a fine-grained PAT for the real repos (later the GitHub App).
6. **Build and run the [model test suite](#test-suite)** to confirm the profiles per class.
7. **Later:** the [future features](../idea-catcher-service-architecture/#future) (React dashboards, YouTube endpoint, more backends).

---

## ⚠️ Risks & Blind Spots {#risks}

- **iCloud and Git on the same vault.** Solved by option A. Don't put the idea-bucket vault back into iCloud. See [iCloud and Git on the same vault](#icloud-git).
- **API budget.** The OpenAI key has a budget cap. When it is used up, `clippings` and `youtube` documents stall in `output/` with `stage: deferred` (nothing is lost, nothing is published). Raise the budget or point the profile at another provider, then move the files from `archive/` back into `inbox/`. The log says so in one `ERROR` line. Keep an eye on `tokens_in` and `tokens_out`, and set the budget with some headroom.
- **LLM limits and cost.** FreeLLMApi may hit rate limits (that is why it only serves short notes), and the OpenAI API has its own rate limit and a budget. A deferred note stalls in `output/` until you move it back into `inbox/` (Stage A), with no fallback to another provider. After 3 days it is flagged `stuck`. The OpenAI key has a **budget cap** that stops the profile when reached (see [Risks](#risks)), so pick a budget with headroom.
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
| **LLM**            | Paid API with a key               | Paid API with logging and alerts     | Central LLM proxy with per-user token budgets                 |
| **Output**         | Hugo on the home LAN behind VPN   | Cloudflare Pages or similar, private | Per-tenant sites on subdomains or custom domains              |
| **Auth & billing** | None                              | None                                 | e.g. Supabase Auth or Clerk + Stripe                          |
| **Focus**          | Prompt quality and pipeline logic | Reliability and cloud APIs           | UX, scaling and monetization                                  |

SaaS ideas from the session, for later:

- **Bring your own repo:** the user connects GitHub through a **GitHub App** (not a pasted token). The service processes their repo and hosts the generated site.
- **Managed tier:** the service also hosts the repo. For non-technical users, offer a pre-configured Obsidian vault, a simple capture app or a personal ingest email address.
- **Access control:** private, team-only or public sites, enforced at the edge (e.g. Cloudflare Access).

---

## ❓ Open Questions {#open-questions}

1. **Which LLM:** decided by the [model test suite](#test-suite). Decided for the start: FreeLLMApi for notes, the OpenAI API for clippings and YouTube. The suite tells us whether to change a profile's provider or model.

### Reminders

- **Move old Gemini web clips into the pipeline.** Two pages were clipped by hand before the pipeline existed and still have the title "New chat":
  - `ai-influencers/crm/systemeio-vs-stanstore.md`
  - `ai-influencers/instagram/instagram-influencers/persona.md`

  Once the pipeline works, put the raw clips in idea-bucket `inbox/` so they get a proper summary in `idea-bucket/gemini/` with tags (`ai-influencer-idea`, `crm`, `instagram`). Then replace the old pages with links from `ai-influencers/`, and add Hugo `aliases` for the old URLs so existing links keep working.
