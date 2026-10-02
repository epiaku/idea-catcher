---
name: epi-setup-ai
description: "Set up the AI coding agents (Claude Code, Cline, Roo Code) to follow the GitHub Copilot layout, so every agent definition, skill and general instruction lives in one place: the .github folder. Runs scripts/setup-ai.sh (macOS and Linux) or scripts/setup-ai.ps1 (Windows) to create the symlinks and copies each agent expects. Use after cloning the repo, after adding, changing or removing an agent or copilot-instructions.md, when an agent does not see a skill or an agent, or when someone asks why CLAUDE.md is a symlink."
metadata:
  version: "1.0.0"
  source: "epiaku"
  author: "rob.de.beir@epiaku.com"
---

# epi-setup-ai

One script that lets **several AI coding agents share the same agents, skills and instructions**. We keep every definition in **one place**, the `.github/` folder, laid out the way **GitHub Copilot** expects. The script then builds what Claude Code, Cline and Roo Code need, so we never keep a second copy by hand.

## Why we do it

The short layout overview, with the rules, is in [references/ai-agents-layout.md](references/ai-agents-layout.md). That file is also linked from the general instructions.

Each tool looks for its files in a different place:

| Tool | Looks for instructions in | Looks for skills in | Looks for agents in |
| --- | --- | --- | --- |
| GitHub Copilot | `.github/copilot-instructions.md` | `.github/skills/` | `.github/agents/` |
| Claude Code | `CLAUDE.md` | `.claude/skills/` | `.claude/agents/` |
| Cline | `.clinerules/` | `.cline/skills/` | `.clinerules/` |
| Roo Code | `.roo/rules/` | `.roo/skills/` | `.roomodes` and `.roo/rules-<agent>/` |

Without this skill we would write and update the same text in four places, and they would drift apart. Instead **`.github/` is the single source of truth**, and the script makes everything else a link to it, or a copy of it.

## When to use it

- **After cloning the repo.** The links and copies are git-ignored (see below), so a fresh clone does not have them.
- **After you add, change or remove an agent** in `.github/agents/`, or **edit `.github/copilot-instructions.md`**. The copies do not follow the source on their own.
- **When an agent does not see a skill or an agent**, or Cline or Roo Code still show old instructions.
- **When someone asks why `CLAUDE.md` is a symlink**, or why `.claude/`, `.cline/`, `.clinerules/` and `.roo/` are not in git.

You do **not** need it after editing the *content of an existing skill*: the skills folders are symlinks, so every agent already sees the change.

## How to run it

From **anywhere inside the repository**. It finds the project root itself.

```bash
bash .github/skills/epi-setup-ai/scripts/setup-ai.sh       # macOS and Linux
```

```powershell
.\.github\skills\epi-setup-ai\scripts\setup-ai.ps1          # Windows (see the note below)
```

It is **idempotent**: running it again rebuilds the same result. It needs no `sudo`. Tell the user what it will do before you run it (see "What it deletes or replaces").

## What it creates

(The step numbers below are for reading; the comments in the script are numbered differently.)

| Step | Result | Kind |
| --- | --- | --- |
| 1 | Makes `.github/`, `.github/skills/`, and a placeholder `.github/copilot-instructions.md` **only if it is missing** | folders and a file |
| 2 | `.claude/skills`, `.cline/skills` and `.roo/skills` point to `.github/skills` | **symlinks** |
| 3 | `CLAUDE.md` in the repo root points to `.github/copilot-instructions.md` | **symlink** |
| 4 | `.clinerules/copilot-instructions.md` and `.roo/rules/copilot-instructions.md` | **copies** |
| 5 | First **removes what an earlier run generated for agents** (see "What it deletes or replaces"), so a removed agent leaves nothing behind. Then, for every agent in `.github/agents/` (nested folders too): `.claude/agents/<agent>.md` points to the source | **symlinks** |
| 6 | For every agent: `.clinerules/<agent>.md` is a copy for Cline | **copy** |
| 7 | For every agent: `.roo/rules-<agent>/<agent>.md` holds the agent text **without its frontmatter** | **copy** |
| 8 | `.roomodes` is **generated** from the agents' frontmatter: one Roo custom mode per agent | generated file |
| 9 | Adds `.cline/`, `.claude/`, `.clinerules/`, `.roo/`, `.roomodes` and `CLAUDE.md` to `.gitignore` | edit |
| 10 | Runs `git rm --cached` on those paths, in case they were tracked | git index only |

**Why copies for Cline and Roo Code:** they do not reliably follow symlinks for rule files, so the script writes real files for them (the comments in the script explain this). The skills folders can be symlinks because those tools read them without trouble.

## What goes into git

Only **`.github/`** is committed: the source, and this skill with its scripts. Everything the script builds is in `.gitignore`, so it never appears in a pull request and every developer builds their own with one command.

## How to add a new agent or skill

1. **A skill:** create `.github/skills/<name>/SKILL.md` (frontmatter with `name` and `description`, optional `scripts/` and `references/` folders). Every agent sees it **at once**, through the symlinks.
2. **An agent:** create `.github/agents/<name>.md`. The frontmatter needs a `name` and a **single-line, double-quoted** `description`, because the script reads it to build the Roo custom mode:

   ```markdown
   ---
   name: my-agent
   description: "One line that says what the agent is for and when to use it."
   ---

   You are ...
   ```

3. **Run the script** (the agents and the instructions need it; a skill does not).
4. **Check:** `ls -l .claude/agents` and `.roomodes` should list the new agent.

## What it deletes or replaces

Read this before running it, and tell the user:

- **`.claude/skills`, `.cline/skills` and `.roo/skills`:** if one of them is a **real folder** (not a symlink), the script **deletes it with `rm -rf`** and puts the symlink there. Move anything you want to keep into `.github/skills/` first.
- **`CLAUDE.md`:** if it is a **regular file**, the script **deletes it without copying it** and makes the symlink. Its text is lost unless it is already in `.github/copilot-instructions.md`. If `CLAUDE.md` was tracked by git, its deletion is also **staged** (it shows as a change to commit).
- **The copies** (`.clinerules/`, `.roo/rules/`, `.roo/rules-<agent>/`) are **overwritten**. Edit the source in `.github/`, never the copy.
- **Stale agent files are removed on every run.** Before it rebuilds the agents, the script deletes the symlinks in `.claude/agents/` (real files there stay), everything in `.clinerules/` except `copilot-instructions.md`, and every `.roo/rules-*/` folder. That is how a removed or renamed agent disappears. Do not keep hand-written files in those places.
- **Two agents with the same file name** (for example `a/foo.md` and `b/foo.md`) would share one Roo slug. The script warns and skips Roo for the second one. Give agents unique file names.
- **`.roomodes`** is rewritten on every run. Hand edits are lost. A permanent change belongs in the agent file or in the script.
- **`.gitignore`** gets up to six lines added, so it will show as modified.
- If `.github/copilot-instructions.md` does not exist, a **placeholder** is created. Write the real instructions there.

## Notes

- **Windows PowerShell 5.1 and links.** The `.ps1` removes links without following them (a recursive delete of a directory link can wipe the target in PowerShell 5.1), but it is untested, so run it on a copy of the repo the first time or use PowerShell 7+.
- **Windows needs symlink permission.** Creating symlinks needs PowerShell **as Administrator**, or **Developer Mode** turned on. The `.ps1` copy of the script has **not been tested** by the author of this skill (no PowerShell was available), so check its result.
- **The script finds the project root by itself.** It lives three levels below the root (`.github/skills/epi-setup-ai/scripts/`), so it asks git for the repository root, and falls back to going four folders up. That is why it works from any working directory, and through the `.claude/skills` symlink. The scripts live **only in this skill folder**.
- **Roo Code on macOS** may also need `epi-fix-roo-code`: a separate problem with its ripgrep path, unrelated to this layout.

## Quick check that it worked

```bash
ls -l CLAUDE.md .claude/skills .cline/skills .roo/skills     # four symlinks
ls .claude/agents .clinerules .roo                           # your agents are there
cat .roomodes                                                # one custom mode per agent
git status --short                                           # only .gitignore (and .github/) should show
```
