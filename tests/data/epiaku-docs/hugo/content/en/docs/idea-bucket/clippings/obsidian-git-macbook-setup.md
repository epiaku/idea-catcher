---
title: "Obsidian Git Plugin: MacBook Air Setup"
linkTitle: "Mac Git Setup"
description: "Step-by-step guide to setting up the Obsidian Git plugin on the MacBook Air, using the idea-bucket repo already cloned locally, with the system git and GitHub CLI credentials the Mac already has."
type: docs
---

A click-by-click guide for setting up the **Obsidian Git** plugin on the **MacBook Air**, matching the [iPhone setup](../obsidian-git-iphone-setup/) but for desktop. It documents the state of the local clone at `/Users/robdebeir/Documents/dev/epiaku/idea-bucket` as of writing, so some steps below are "already done" checks rather than new actions.

This is the Mac-side companion to the [idea catcher pipeline](../idea-catcher-pipeline/#device-setup) page, which explains *why* the vault syncs this way, and to the [iPhone guide](../obsidian-git-iphone-setup/), which covers the same setup on mobile (where the plugin has to authenticate differently — see [Step 4](#authenticate)).

## 📑 Table of Contents {#toc}

1. [Before You Start](#prerequisites)
2. [Step 1: Confirm the Local Clone](#clone)
3. [Step 2: Open the Folder as a Vault](#vault)
4. [Step 3: Enable the Git Plugin](#install-plugin)
5. [Step 4: How Desktop Authentication Differs From iPhone](#authenticate)
6. [Step 5: Check the Commit Identity](#identity)
7. [Step 6: First Pull and Push](#first-sync)
8. [Step 7: Turn On Automatic Sync](#auto-sync)
9. [Automating Commit & Push](#automating-commit-push)
10. [Step 8: Set the Default Note Location](#inbox-routing)
11. [Step 9: Untrack the Leftover Workspace File](#gitignore)
12. [Test Plan Executed on This Mac](#test-plan)
13. [Troubleshooting](#troubleshooting)

---

## ✅ Before You Start {#prerequisites}

Already true for this Mac and repo, checked while writing this guide:

- **The repo is already cloned** at `/Users/robdebeir/Documents/dev/epiaku/idea-bucket`, with `origin` pointing at `https://github.com/epiaku/idea-bucket.git`, on branch `main`, up to date.
- **GitHub CLI is already authenticated** (`gh auth status` shows the `epiaku` account logged in over HTTPS), and Git is configured to use the macOS Keychain for credentials (`credential.helper = osxkeychain`). This is what makes [Step 4](#authenticate) simpler than on the iPhone.
- **A global Git identity is already set** (`git config --global user.name` / `user.email`) — see [Step 5](#identity).
- **Obsidian.app is already installed** in `/Applications`.
- **The Git plugin's files are already present** in `.obsidian/plugins/obsidian-git/` (version 2.40.0), because this folder's `.obsidian/` came from the repo. It still needs to be **enabled inside Obsidian** the first time this folder is opened as a vault — see [Step 3](#install-plugin).

If any of the above isn't true on a different Mac (or after a clean reinstall), the steps below note what to do instead.

{{% alert title="Watch out: a stray empty vault" color="warning" %}}
Obsidian's **"Create new vault"** button, if used without browsing to a specific folder, creates the vault as a subfolder of the default `~/Documents/Obsidian Vault/`. On this Mac that happened once with a vault also named `idea-bucket`, ending up at `~/Documents/Obsidian Vault/idea-bucket` — an empty vault with no Git connection at all, completely separate from the real git clone at `~/Documents/dev/epiaku/idea-bucket`. It's easy to end up editing in the wrong one without noticing, since both are named the same in the Vault Switcher.

**Check for this before Step 2:** open the Vault Switcher and look at the *path* under each `idea-bucket` entry, not just the name. If a stray one exists, click its **`···`** menu → **Remove from list** (this only unregisters it from Obsidian, it does not delete files) — then optionally delete the empty folder in Finder, since it holds nothing but a default `.obsidian/` and `Welcome.md`.
{{% /alert %}}

---

## 📥 Step 1: Confirm the Local Clone {#clone}

If the repo isn't cloned yet, do it once in Terminal:

```bash
git clone https://github.com/epiaku/idea-bucket.git ~/Documents/dev/epiaku/idea-bucket
```

To verify an existing clone (this is already true for the current one):

```bash
cd ~/Documents/dev/epiaku/idea-bucket
git remote -v      # should show origin → epiaku/idea-bucket
git status         # should say "up to date" with no changes needed to push
```

If `git status` or a `git pull` here fails with an authentication error, fix that in Terminal first (see [Step 4](#authenticate)) — it's simpler to resolve there than inside Obsidian.

---

## 🗂️ Step 2: Open the Folder as a Vault {#vault}

Unlike the iPhone (which creates an empty vault and then clones into it), on desktop you point Obsidian directly at the already-cloned folder.

1. Open **Obsidian**.
2. Open the **Vault Switcher** (click the vault name at the top of the left sidebar, or the vault icon in the bottom-left corner).
3. Click **Open folder as vault**.
4. Navigate to and select `~/Documents/dev/epiaku/idea-bucket`.
5. Click **Open**.

Obsidian opens the folder as a vault named after the folder — `idea-bucket`, matching the repo and the iPhone vault (see [why that matters](../obsidian-git-iphone-setup/#vault)). **Done on this Mac** — confirmed by the window title reading "idea-bucket - Obsidian" while this vault, and not the stray empty one (see the warning above), is open.

---

## 🧩 Step 3: Enable the Git Plugin {#install-plugin}

Because this vault's `.obsidian/` folder came from the repo, the **Git** plugin's files and its listing in `community-plugins.json` are already there — but Obsidian still needs you to confirm it's allowed to run community plugins the first time this vault opens on a new machine.

1. If a **"Restricted Mode"** banner appears, go to **Settings → Community plugins** and turn **Restricted Mode off** (confirm the prompt). This is a one-time, per-vault-on-this-device setting; it isn't stored in the repo.
2. Under **Installed plugins**, confirm **Git** (by Vinzent03) shows as installed and toggled **on**. If it isn't installed at all (e.g. a fresh vault with no `.obsidian/plugins/`), use **Browse**, search **Git**, and install it the same way as the [iPhone guide, Step 3](../obsidian-git-iphone-setup/#install-plugin).

A **Git** entry now appears in **Settings → Community plugins**. **Done on this Mac** — Restricted mode is off (the button reads "Turn on and reload," meaning it's currently off), and **Git v2.40.0 by Vinzent** shows installed with its toggle on.

---

## 🔐 Step 4: How Desktop Authentication Differs From iPhone {#authenticate}

On the iPhone the plugin runs a JavaScript-only Git implementation (`isomorphic-git`) and needs a **username + Personal Access Token** entered directly into its settings, because there's no OS-level Git on iOS.

**On desktop, the plugin shells out to the Mac's own `git` command** instead. That means it reuses whatever credentials `git` (and by extension `gh`) already has configured — which, on this Mac, is the `gh auth login` session stored via the macOS Keychain. In practice this means:

- **No token needs to be pasted into the plugin's settings on this Mac.** If `git pull` / `git push` already work in Terminal for this repo (they do — see [Step 1](#clone)), the Git plugin will use the same credentials automatically.
- **Leave the plugin's Authentication / token fields empty** on desktop unless the fallback below is needed.

**Fallback, only if the plugin can't find or use system Git:**

1. Confirm the path to Git: `which git` in Terminal (currently `/usr/bin/git` on this Mac).
2. In **Settings → Community plugins → Git → Advanced**, set **Path to Git executable** to that path.
3. If authentication *still* fails from inside Obsidian even though Terminal works, fall back to the same token-based method as the iPhone: generate (or reuse) a fine-grained PAT scoped to `epiaku/idea-bucket` with **Contents: Read and write** (see the [iPhone guide, Step 1](../obsidian-git-iphone-setup/#pat)), and enter your GitHub **username** and that **token** in the plugin's **Authentication** section.

---

## 👤 Step 5: Check the Commit Identity {#identity}

Desktop commits use the Mac's regular Git identity, not a per-plugin setting like on mobile. Verify it's set:

```bash
git config --global user.name
git config --global user.email
```

If either is empty, set it once (this Mac already has both set):

```bash
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
```

---

## ✅ Step 6: First Pull and Push {#first-sync}

Since the clone already exists and is up to date, this is a quick sanity check rather than a real first sync:

1. Open the **Command Palette** (`Cmd+P`), run **`Git: Pull`**. Expect *"Everything up to date"*.
2. Create a **test note** anywhere in the vault.
3. Run **`Git: Commit all changes`**, then **`Git: Push`**.
4. Confirm the note appears on [github.com/epiaku/idea-bucket](https://github.com/epiaku/idea-bucket) in a browser.
5. Delete the test note and push again, to leave the repo clean.

**Done on this Mac** — a test note pushed and showed up in the remote repo as expected.

---

## 🔁 Step 7: Turn On Automatic Sync {#auto-sync}

Go to **Settings → Community plugins → Git** and set:

- **Pull on startup:** **ON** — **done on this Mac**, so opening Obsidian always starts from the latest state, including anything pushed from the phone. (This was found *off* by default and had to be switched on; check it explicitly rather than assuming.)
- **Commit-and-sync interval** (under the "Commit-and-sync" section further down): about **5–10 minutes** is fine as a fallback. This vault's interval was already set to `10` minutes — no change needed.

This Mac also receives **Web Clipper** captures now, not just template edits, so a faster, event-driven sync is worth adding — see the next section.

---

## ⚡ Automating Commit & Push {#automating-commit-push}

Two settings cover this without any external tooling — a responsive trigger for after a clip lands, and a one-key manual trigger for everything else.

### Automatic: sync shortly after a Web Clipper capture

In **Settings → Community plugins → Git**, find **"Auto commit-and-sync after stopping file edits"** and set it to a short delay, e.g. **1–2 minutes**. This fires shortly after *any* file in the vault changes — including a new Web Clipper capture landing in `inbox/clippings/` — instead of waiting for the fixed interval above. Keep both settings on: the interval is the fallback, this one is what makes a fresh clip show up in the remote repo within a minute or two without you doing anything.

### Manual: a quick "commit and push now"

For any other case where you don't want to wait even a minute — pick whichever fits:

- **Hotkey (recommended):** **Settings → Hotkeys**, search `commit-and-sync`, assign a shortcut (e.g. **⌘⇧S**). One keypress from anywhere in Obsidian.
- **Ribbon icon:** the Git plugin adds an icon in the left sidebar ribbon — click it to commit-and-sync immediately.
- **Command Palette:** `⌘P` → type `Git: Commit-and-sync` → Enter. Works with no setup.

Unlike the phone (where a one-tap toolbar button is the practical option — see the [iPhone guide](../obsidian-git-iphone-setup/#auto-sync)), the Mac's full keyboard makes a hotkey the equivalent shortcut here.

---

## 📥 Step 8: Set the Default Note Location {#inbox-routing}

Captures go into just two subfolders under `inbox/`, not a flat `inbox/` (see [Input types](../idea-catcher-pipeline/#input-types) and the [repo layout](../idea-catcher-pipeline/#repo-layout-latest-version)): `inbox/notes/` for dictated notes, and `inbox/clippings/` for **everything** the Web Clipper captures — Gemini, Claude, YouTube links, web articles alike (confirmed by testing a YouTube clip; there is no separate `inbox/youtube/`). Stage one scans `inbox/` **recursively**, so this organizes the vault without affecting how notes are processed — that's decided by the domain of `source` (or an explicit `type` for notes), not by which subfolder it's in.

This vault's **`.obsidian/app.json`** already has **`newFileFolderPath: "inbox/notes"`** — confirmed correct, no change needed. To verify or change it: **Settings → Files and links → Default location for new notes**.

Also check the **Web Clipper** extension's settings (if used on this Mac): **Vault** should be `idea-bucket`, and **Note location** should be `inbox/clippings` (its own default — leave it as-is rather than redirecting it).

---

## 🧹 Step 9: Untrack the Leftover Workspace File {#gitignore}

`.gitignore` at the vault root already excludes both device-state files:

```gitignore
.obsidian/workspace.json
.obsidian/workspace-mobile.json
```

**Done:** both `workspace.json` and `workspace-mobile.json` are now untracked (`git ls-files | grep workspace` returns nothing) via:

```bash
cd ~/Documents/dev/epiaku/idea-bucket
git rm --cached .obsidian/workspace-mobile.json
git commit -m "Stop tracking mobile workspace state"
git push
```

The local file stayed on disk (Obsidian mobile still uses it on the phone) — this only stopped Git from syncing it. If a fresh clone or a different device ever shows one of these files tracked again, run the same command for it. See the [iPhone guide's version of this step](../obsidian-git-iphone-setup/#gitignore) for why deleting the file isn't useful on its own (it just gets regenerated — untracking is what actually matters).

---

## 🧪 Test Plan Executed on This Mac {#test-plan}

The actual sequence run to bring this Mac's vault up and confirm it, in order:

| # | Check | How | Result |
| --- | --- | --- | --- |
| 1 | Local clone healthy | `git remote -v`, `git status` in `~/Documents/dev/epiaku/idea-bucket` | ✅ `origin` set to `epiaku/idea-bucket`, on `main`, up to date |
| 2 | GitHub auth already works | `gh auth status` | ✅ Logged in as `epiaku` over HTTPS, `osxkeychain` credential helper |
| 3 | Git identity already set | `git config --global user.name` / `user.email` | ✅ Both set |
| 4 | Which vault is actually registered | Opened the Vault Switcher, checked the **path** under every `idea-bucket` entry | ❌ Found a stray **empty** vault at `~/Documents/Obsidian Vault/idea-bucket` (`.obsidian/` + `Welcome.md` only, no Git) — this was the most recently opened one |
| 5 | Remove the stray vault | Vault Switcher → `···` on the stray entry → **Remove from list** | ✅ Unregistered (files left untouched) |
| 6 | Open the real vault | **Open folder as vault**, ⌘⇧G to paste `/Users/robdebeir/Documents/dev/epiaku/idea-bucket` directly instead of clicking through Finder | ✅ Window title read "idea-bucket - Obsidian" |
| 7 | Plugin state in the correct vault | **Settings → Community plugins** | ✅ Restricted mode off, **Git v2.40.0 by Vinzent** installed and toggled on |
| 8 | Auto-sync settings | **Settings → Community plugins → Git** | ❌→✅ **Pull on startup** was off by default — turned on. Commit-and-sync interval already `10` minutes — left as-is |
| 9 | End-to-end sync test | Command Palette → `Git: Pull` (up to date) → created a test note → `Git: Commit all changes` → `Git: Push` | ✅ Test note appeared in the remote `idea-bucket` repo on GitHub |
| 10 | Untrack leftover workspace file | `git rm --cached .obsidian/workspace-mobile.json`, commit, push (see [Step 9](#gitignore)) | ✅ `git ls-files \| grep workspace` returns nothing on `origin/main` |

**Takeaway:** steps 1–3 were already true and needed no action. The one real gotcha was step 4 — a same-named but disconnected vault created by "Create new vault" instead of "Open folder as vault" — worth checking for on any device before assuming Step 2 just works.

---

## 🛠️ Troubleshooting {#troubleshooting}

**Restricted Mode won't turn off / plugin doesn't appear**
Confirm you're in **Settings → Community plugins** (not core plugins), and that the vault opened is the actual repo folder, not a parent directory.

**Plugin says it can't find Git, or push/pull hangs**
Set **Path to Git executable** explicitly to the output of `which git` in the plugin's **Advanced** settings (see [Step 4](#authenticate)).

**Push works in Terminal but fails from the plugin**
Usually a stale credential cache specific to the plugin's child process environment. Quit and reopen Obsidian after confirming `git push` works standalone in Terminal; if it persists, use the token fallback in [Step 4](#authenticate) instead of relying on system Git.

**Conflicting changes from the phone**
Run **`Git: Pull`** before making local edits, and resolve any merge conflict Obsidian reports directly in the file — conflicts are rare here since both devices mostly *add* new files rather than edit the same one (see the [pipeline doc](../idea-catcher-pipeline/#device-setup)).
