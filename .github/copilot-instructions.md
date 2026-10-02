# Project instructions

Guidance for the AI coding agents (Claude Code, GitHub Copilot, Cline, Roo Code) working in this repository.

## AI agents, skills and instructions

Claude Code, GitHub Copilot, Cline and Roo Code share one set of instructions, agents and skills. The single source of truth is `.github/` (`copilot-instructions.md`, `agents/`, `skills/`); the other agents get symlinks and copies of it.

- **Edit only in `.github/`**, never in `CLAUDE.md`, `.claude/`, `.cline/`, `.clinerules/`, `.roo/` or `.roomodes`.
- **After changing or pulling anything in `.github/`, run `bash .github/skills/epi-setup-ai/scripts/setup-ai.sh`.**
- Only `.github/` is committed. The full explanation is in [.github/skills/epi-setup-ai/references/ai-agents-layout.md](.github/skills/epi-setup-ai/references/ai-agents-layout.md).
