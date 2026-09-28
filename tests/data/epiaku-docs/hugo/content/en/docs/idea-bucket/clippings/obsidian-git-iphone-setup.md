---
title: "Obsidian Git Plugin: iPhone Setup"
linkTitle: "iPhone Git Setup"
description: "Step-by-step guide to installing and configuring the Obsidian Git plugin on iPhone, from a fresh install to automatic two-way sync with GitHub, plus why the vault should not also live in iCloud."
type: docs
---

A click-by-click guide for setting up the **Obsidian Git** plugin on iPhone, extracted and structured from a Gemini brainstorm (September 2026). Follow it top to bottom for a fresh install, or jump to a step if you're only fixing one part. It also explains why this vault should **not** be synced through iCloud at the same time (moved here from the [idea catcher pipeline](../idea-catcher-pipeline/) page).

This is the device-setup companion to the [idea catcher pipeline](../idea-catcher-pipeline/#chosen-setup), which explains _why_ the phone syncs this way. This page only covers _how_, on the iPhone.

## 📑 Table of Contents {#toc}

1. [Before You Start](#prerequisites)
2. [Step 1: Create a GitHub Token](#pat)
3. [Step 2: Create the Vault (Not in iCloud)](#vault)
4. [Step 3: Install the Git Plugin](#install-plugin)
5. [Step 4: Sign the Plugin Into GitHub](#authenticate)
6. [Step 5: Connect the Vault to the Repo](#connect)
7. [Step 6: First Pull and Push](#first-sync)
8. [Step 7: Turn On Automatic Sync](#auto-sync)
9. [Step 8: Route New Notes Into `inbox/notes/`](#inbox-routing)
10. [Step 9: Keep Devices From Fighting Each Other](#gitignore)
11. [Troubleshooting](#troubleshooting)
12. [Why Not Also Sync This Vault Through iCloud?](#icloud-git)

---

## ✅ Before You Start {#prerequisites}

- A **GitHub account**, and the target repository already created (e.g. `epiaku/idea-bucket`). It doesn't need to be empty — an existing `README.md` is fine.
- The **Obsidian** app, from the [App Store](https://apps.apple.com/us/app/obsidian/id1557175442) or [obsidian.md](https://obsidian.md/).
- Five to ten minutes, and access to a browser to generate a GitHub token (steps 2–4 of this guide).

You do **not** need Git, a terminal, or SSH keys on the phone. The Obsidian Git plugin uses `isomorphic-git`, a JavaScript implementation of Git that runs inside the app, so it talks to GitHub over plain HTTPS with a token instead.

---

## 🔑 Step 1: Create a GitHub Token {#pat}

The plugin authenticates as you, using a **fine-grained personal access token** scoped to one repository.

1. On your phone or a computer, open GitHub → **Settings → Developer settings → Personal access tokens → Fine-grained tokens**.
2. Tap **Generate new token**.
3. **Token name:** something recognizable, e.g. `Obsidian Idea Bucket`. Description `Sync Obsidian on iPhone and macbook air with idea-bucket repo`.
4. **Expiration:** set a date (fine-grained tokens max out at one year). Put a reminder to renew it before then.
5. **Repository access:** _Only select repositories_ → pick the idea-bucket repo (e.g. `epiaku/idea-bucket`). Never choose "All repositories" for this token.
6. **Repository permissions**, set only:
   - **Contents: Read and write** — allows cloning, committing and pushing files.
   - **Metadata: Read-only** — GitHub sets this automatically; it's required just to see the repo exists.
   - Leave every other permission (Pull requests, Issues, Actions, …) and every **Organization permission** / **Account permission** at **No access**.
7. Scroll down and click **Generate token**.
8. **Copy the token immediately** (it starts with `github_pat_`) — GitHub only shows it once. Paste it somewhere safe for the next steps (a password manager, not a note in this vault). Safe the token inthe notes of the lastpass item `github.com epiaku`

---

## 📱 Step 2: Create the Vault (Not in iCloud) {#vault}

Create a **new, empty vault stored locally on the phone**, not in iCloud Drive. (See [why](#icloud-git) at the bottom of this page.)

1. Open **Obsidian**. If a vault opens automatically, tap the **vault name** at the top of the left sidebar, or the vault icon in the bottom-left corner, to open the **Vault Switcher**.
2. Tap **Create new vault**.
3. Give it a name matching the repo: `idea-bucket`. (Using the same name on every device and repo avoids Web Clipper "vault not found" errors, which are matched by exact vault name.)
4. Leave **Store in iCloud** turned **off**.
5. Tap **Create** in the top-right corner.

You now have an empty local vault. The next steps connect it to the GitHub repo.

---

## 🧩 Step 3: Install the Git Plugin {#install-plugin}

1. In Obsidian, open **Settings** (gear icon, bottom-left).
2. Go to **Community plugins**. If prompted, turn off **Restricted/Safe mode** and confirm.
3. Tap **Browse**, search for **Git**.
4. Install the plugin `Git` by **Vinzent03** (package `obsidian-git`) — it's the standard, most-downloaded one.
5. Tap **Enable**.

A new **Git** entry now appears in Settings, under Community plugins.

---

## 🔐 Step 4: Sign the Plugin Into GitHub {#authenticate}

1. Open **Settings → Community plugins → Git**.
2. Scroll to the **Authentication** section.
3. **Username:** your GitHub username `epiaku`.
4. **Password / Personal Access Token:** paste the token from [Step 1](#pat). (There is no separate "password" — the token _is_ the password field's value.)

Nothing syncs yet — this just stores the credentials the plugin will use. **Author name and email are set in the next step**, after the repo is connected — the plugin only shows/writes that setting once there is a `.git` folder in the vault, and a `Pull` will fail before it's set.

---

## 🔗 Step 5: Connect the Vault to the Repo {#connect}

Open the **Command Palette** first: tap the **`>_`** icon in the left ribbon (or swipe down on a note). Every command below is run from there.

Which path to follow depends on the vault you created in [Step 2](#vault):

### If the vault is empty (most common for a fresh install)

1. In the Command Palette, run **`Git: Clone an existing remote repo`**.
2. Paste the repository's HTTPS URL, e.g. `https://github.com/epiaku/idea-bucket.git`.
3. When asked for the **vault root**, leave it **blank** and confirm. (Blank clones directly into the vault you're already in. A non-blank value would create a subfolder — not what you want.)
4. Enter your GitHub **username**, then paste the **token** again when prompted.
5. When asked **"Does your remote repo contain a `.obsidian` directory?"**:
   - **NO**, if this is the first device connecting, or you're not sure. This is the safer default — it keeps the phone's own plugin settings and avoids overwriting them with someone else's.
   - **YES**, only if the repo already has a `.obsidian/` folder (committed from the Mac) that you deliberately want to pull down.
6. When asked for **clone depth**, leave it **blank** (or `0`) for a full clone with complete history. Only use a shallow depth (e.g. `1`) on a very large repo where history doesn't matter — it can complicate later pushes.
7. Obsidian downloads the repo. Wait for the success notification.

### If you already have notes in this vault you want to keep

1. In the Command Palette, run **`Obsidian Git: Initialize a new repository`**. This creates the hidden `.git` folder, and the "Git is not ready" message disappears.
2. Go to **Settings → Community plugins → Git**, scroll to **Specify custom remote URL**, and enter the repo's HTTPS URL.
3. Set the **Default branch** to `main`.
4. In the Command Palette, run **`Obsidian Git: Commit all changes`**, then **`Obsidian Git: Push`**.

### Now set your Git identity (both paths)

Only once the vault has a `.git` folder — i.e. right after cloning or initializing above — does the plugin have a repo to attach an identity to:

1. Go to **Settings → Community plugins → Git**, scroll to **Commit Author** (or **Advanced**, depending on plugin version).
2. **Author name:** e.g. `rob de beir`.
3. **Author email:** e.g. `rob.de.beir@epiaku.com`.

**Do this before running `Pull`** — the plugin needs this identity to complete a sync (a plain fetch may work, but `Pull`/`Commit`/`Push` will fail or prompt for it otherwise).

The vault's root now matches the repo's root, tracking `main`.

---

## ✅ Step 6: First Pull and Push {#first-sync}

Verify the connection works end to end.

1. Open the Command Palette (`>_` icon) and run **`Obsidian Git: Pull`**. You should see _"Everything up to date"_ or a list of pulled files (e.g. `README.md` appearing in the file explorer).
2. Create a **test note** anywhere in the vault.
3. Open the Command Palette, run **`Obsidian Git: Commit all changes`**.
4. Open the Command Palette again, run **`Obsidian Git: Push`**.
5. Open the repo on GitHub in a browser and confirm the test note is there.

If push fails with a 401 or 403 error, see [Troubleshooting](#troubleshooting).

---

## 🔁 Step 7: Turn On Automatic Sync {#auto-sync}

Go to **Settings → Community plugins → Git** and set:

- **Auto commit-and-sync interval:** `2`–`10` minutes. On the current iOS build of the plugin this single setting handles commit, pull _and_ push together — there is no separate "auto push interval" to configure (it's greyed out, because it's redundant once this is on).
  - Use a **short interval (2–5 min)** if the phone is mainly used to **capture** new dictated notes — you want a note pushed while you're still in the app.
  - A longer interval (10–15 min) saves a little battery if you use the vault for longer editing sessions, since `isomorphic-git` runs the sync in JavaScript.
- **Auto pull on startup:** **ON**. This only fires on a **cold start** — when Obsidian launches from having been fully closed (you'll see the "Loading vault…" splash). Switching briefly to another app and back does **not** trigger it, because iOS just resumes the app in memory.

### One-tap sync (recommended)

Since the phone is mainly used to dictate a quick note and leave, add a manual trigger so a sync doesn't have to wait for the interval or a cold start:

1. Go to **Settings → Interface → Mobile → Configure mobile toolbar** (or the equivalent toolbar-configuration screen) - Command palette.
2. Add **`Git: Commit and sync`** to the mobile toolbar, and move it to the top.
3. After dictating a note, tap it once before switching away. This is the single most reliable habit for this vault.

A missed tap is not a lost note — it stays on the phone and goes out with the next interval sync or the next cold start.
---

## 📥 Step 8: Route New Notes Into `inbox/notes/` {#inbox-routing}

There are two `inbox/` subfolders, one per **capture mechanism** — not per content type: `inbox/notes/` for phone dictation, and `inbox/clippings/` for the Web Clipper, which uses that one folder for **everything** it captures regardless of content (Gemini, Claude, YouTube links, web articles — confirmed by testing a YouTube clip). What kind of content it is comes later, from the domain of `source` in each note, not from which folder it landed in. Only the dictation folder needs manual setup:

1. In the file explorer, create the folder **`inbox/notes`** (if the repo doesn't already have it).
2. Go to **Settings → Files and links**.
3. Set **Default location for new notes** to **In the folder specified below**, and set the folder to **`inbox/notes`**.

Every new dictated note now saves straight into `inbox/notes`. Stage one scans `inbox/` **recursively**, so it picks up all three subfolders; the `type`/`source` in the note frontmatter decides how it's processed, not which subfolder it's in — see the [pipeline doc's device setup](../idea-catcher-pipeline/#chosen-setup).

> The Git plugin has **no setting** to map the vault root to a subfolder on GitHub — wherever the `.git` folder is, that's what maps 1:1 to the repo root. Routing captures into `inbox/<type>/` inside Obsidian only organizes the vault; it doesn't change where the repo root itself lives on GitHub, which still also holds files like `README.md`.

---

## 🧹 Step 9: Keep Devices From Fighting Each Other {#gitignore}

If more than one device (phone and Mac) will run the Git plugin against this repo, exclude Obsidian's per-device window state so the devices don't create sync conflicts over files nobody cares about:

Add to (or create) `.gitignore` at the vault root:

```gitignore
.obsidian/workspace.json
.obsidian/workspace-mobile.json
```

Everything else in `.obsidian/` (plugin settings, hotkeys, templates configuration) is fine to keep in Git — it's what lets a new device pick up the same setup automatically.

**If these files were already committed** (e.g. from before adding them to `.gitignore`), adding the rule alone doesn't remove them — Git keeps tracking a file it already knows about. Untrack them, on the Mac (there's no terminal on the phone for this):

```bash
git rm --cached .obsidian/workspace.json .obsidian/workspace-mobile.json
git commit -m "Stop tracking per-device workspace state"
git push
```

`--cached` removes the files from Git's tracking and from the next commit, but **leaves the local copy on disk**. That's fine — there's no reason to also delete them, since Obsidian just regenerates whichever one it needs the moment it opens (`workspace.json` on desktop, `workspace-mobile.json` on the phone), so a plain `rm` wouldn't stick. Untracking is what actually matters: it's what stops Git from pushing and pulling a file that keeps changing on every launch. After this push, pull on the phone once to pick up the updated `.gitignore`.

---

## 🛠️ Troubleshooting {#troubleshooting}

**"Git is not ready"**
The plugin can't find a hidden `.git` folder in the currently open vault. Fix depending on your situation:

- **Repo exists on GitHub, vault is empty:** run `Obsidian Git: Clone an existing remote repo` (see [Step 5](#connect)).
- **You already have local notes:** run `Obsidian Git: Initialize a new repository`, then set the remote URL in the plugin settings (see [Step 5](#connect)).
- **You expected this to already be connected:** double-check Obsidian actually opened the vault folder that contains `.git`, not a parent folder (e.g. `Documents` instead of the vault itself).

**401 or 403 error on push**

- Confirm the fine-grained token has **Contents: Read and write** (not just Read).
- Confirm the token's repository selection still includes this exact repo — a renamed repo or a token scoped to the wrong repo both fail silently until you push.
- Confirm the remote URL in the plugin settings matches the repo exactly (`https://github.com/<owner>/<repo>.git`).

**Can't find the Command Palette**
Tap the **`>_`** icon in the left ribbon menu (open the ribbon with the menu icon if it's hidden), or swipe down while a note is open. Every `Obsidian Git: …` command in this guide is run from there.

**Auto-sync doesn't seem to run**
Remember it only fires while Obsidian is in the **foreground**. iOS suspends background timers when the app isn't active, so a fixed interval and "auto pull on startup" (cold start only) are the two triggers available — see [Step 7](#auto-sync). Use the one-tap toolbar button when you need a push to happen immediately.

---

## ☁️ Why Not Also Sync This Vault Through iCloud? {#icloud-git}

It's tempting to also put this vault in iCloud Drive so it appears instantly on a Mac too — but running **iCloud sync and the Git plugin on the same vault** causes real problems.

**The issue:** iCloud would sync the hidden `.git` folder along with everything else. That folder holds the repo's entire history as thousands of small files that all change together on every commit. iCloud syncs them one by one, can create "conflict copies" when two devices touch the folder near-simultaneously, and can offload files from the phone to save space. Any of these can leave the local Git repository corrupted, at which point Obsidian Git needs to be reconnected from scratch.

**Can `.git` just be excluded from iCloud?** Not reliably. iCloud Drive has no ignore-file mechanism. The macOS trick of renaming a folder with a `.nosync` suffix only works on the Mac side, and the Git plugin needs a real, unrenamed `.git` folder sitting in the vault root to function. So the practical fix is to keep **iCloud and Git off the same folder** rather than trying to make them coexist.

### Options

| Option                                   | How it works                                                                                                                                       | Pros                                                                                                                                                           | Cons                                                                                                                                                   |
| ---------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **A. GitHub is the only sync** (chosen)  | The iPhone vault lives on local storage (this guide). The Mac clones the same repo into its own local folder. Both run Obsidian Git independently. | One sync mechanism, no `.git` in iCloud, full history. Templates and plugin settings sync via Git too — a new device just needs Obsidian, a clone and a token. | A sync delay of a few minutes (the interval). A token to manage per device.                                                                            |
| B. Keep iCloud, only one device runs Git | Keep the vault in iCloud Drive for iPhone ↔ Mac sync. Turn off Obsidian Git on the Mac; only the iPhone commits and pushes.                        | Minimal change if the vault is already in iCloud. The Mac sees new notes instantly via iCloud.                                                                 | `.git` still lives inside the iCloud-synced folder. Lower risk with a single writer, but not eliminated.                                               |
| C. Keep iCloud, only the Mac runs Git    | The iPhone only uses iCloud to write notes. The Mac runs Obsidian Git and does all the pushing.                                                    | No Git plugin needed on the phone at all.                                                                                                                      | Notes only reach GitHub when the Mac is on, Obsidian is open, and iCloud has finished syncing from the phone. `.git` still lives in iCloud on the Mac. |

**Decision: option A.** This vault is a one-way outbox for capturing ideas, and the Mac is mainly used to edit templates, so the small sync delay of running Git on both devices costs very little compared to the risk of a corrupted repo.

This only applies to a vault that is **also a Git repository**. Any other Obsidian vault — personal notes, general clippings, anything that doesn't feed the pipeline — can keep using iCloud normally. Obsidian supports multiple vaults side by side, each with its own storage location, so there's no need to migrate everything.

**Related reading:** the [idea catcher pipeline](../idea-catcher-pipeline/#device-setup) page has the matching Mac-side setup and the full checklist for moving an existing iCloud vault over to this GitHub-only pattern.
