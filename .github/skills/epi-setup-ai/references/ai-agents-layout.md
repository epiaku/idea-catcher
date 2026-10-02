# AI agents, skills and instructions: the shared layout

Several AI coding agents work in this repo: Claude Code, GitHub Copilot, Cline and Roo Code. They **share one set of general instructions, custom agents and agent skills**. All of it lives in the `.github/` folder, following the GitHub Copilot layout:

| What | Where (the single source of truth) |
| --- | --- |
| General instructions | `.github/copilot-instructions.md` (also `CLAUDE.md`) |
| Custom agents | `.github/agents/<name>.md`: markdown with YAML frontmatter (`name`, `description`) |
| Agent skills | `.github/skills/<name>/SKILL.md`, with optional `scripts/` and `references/` folders |

For the other agents we created **symlinks and copies**, so each one finds the same files in the place it expects them. The skill `epi-setup-ai` builds them, with `scripts/setup-ai.sh` (macOS and Linux) or `scripts/setup-ai.ps1` (Windows):

| Agent | General instructions | Skills | Custom agents |
| --- | --- | --- | --- |
| **Claude Code** | `CLAUDE.md` in the repo root is a **symlink** to `.github/copilot-instructions.md` | `.claude/skills` is a symlink to `.github/skills` | `.claude/agents/<name>.md` are symlinks to `.github/agents/<name>.md` |
| **Cline** | `.clinerules/copilot-instructions.md`, a **copy** | `.cline/skills` is a symlink to `.github/skills` | `.clinerules/<name>.md`, copies |
| **Roo Code** | `.roo/rules/copilot-instructions.md`, a **copy** | `.roo/skills` is a symlink to `.github/skills` | `.roo/rules-<name>/`, a copy of the agent text without its frontmatter, plus a `.roomodes` file generated from the agents' frontmatter |

Cline and Roo Code get copies, not symlinks, because they do not reliably follow symlinks for rule files (see the comments in the script).

## Rules

- **Edit only in `.github/`.** The copies are overwritten and the symlinks point there. Never edit `CLAUDE.md`, `.claude/`, `.cline/`, `.clinerules/`, `.roo/` or `.roomodes` directly.
- **After changing or pulling anything in `.github/`, run `bash .github/skills/epi-setup-ai/scripts/setup-ai.sh`** (or `setup-ai.ps1` in the same folder on Windows). It works from any folder in the repo. The symlinks follow the source on their own, but the copies and `.roomodes` do not.
- **Only `.github/` is committed.** `CLAUDE.md`, `.claude/`, `.cline/`, `.clinerules/`, `.roo/` and `.roomodes` are in `.gitignore`, so a fresh clone needs the setup script once.
