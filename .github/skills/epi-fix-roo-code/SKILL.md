---
name: epi-fix-roo-code
description: Fix Roo Code (VS Code extension rooveterinaryinc.roo-cline) hanging forever on "API Request" with no error, caused by "Could not find ripgrep binary" after a VS Code update. Explains the cause and runs the repair script that re-creates the ripgrep symlink. Use when Roo Code hangs, shows a spinning API Request, logs "ripgrep not found", or after a VS Code update breaks Roo Code. Keywords: Roo Code, roo-cline, ripgrep, rg, API Request hang, VS Code update.
metadata:
  version: "1.0.0"
  source: "epiaku"
  author: "rob.de.beir@epiaku.com"
---

# epi-fix-roo-code

Repairs Roo Code when every chat request hangs on a spinning **API Request**
with no error.

## How to start this skill

Any of these phrases in the chat triggers it:

- "fix roo code" / "fix roo-code" / "repair roo code"
- "roo code is hanging on API Request"
- "roo code can't find ripgrep" / "ripgrep not found"
- "roo code stopped working after the VS Code update"
- "run the roo ripgrep fix"

Other ways to invoke it:

- **Claude Code:** type `/epi-fix-roo-code` or just describe the problem.
- **Cline / Roo Code:** ask for the skill by name ("use the epi-fix-roo-code
  skill"); see `docs/invoking-skills-cline-claude.md`.
- **No agent, run the script directly** from the repo root:

  ```bash
  bash .github/skills/epi-fix-roo-code/scripts/fix-roo-ripgrep.sh
  ```

If the user only wants to understand the problem (not fix it), explain from
the reference doc and skip the script.

## Why it happens

Roo Code (3.54.0) looks for `rg` (ripgrep) only in the old
`@vscode/ripgrep/bin/` locations under VS Code's `appRoot`. Recent VS Code
builds (1.138.0+) ship it at
`@vscode/ripgrep-universal/bin/<platform-arch>/rg`, so Roo Code never finds
it. The lookup runs before every API call and its rejection is never caught,
so the task loop dies before any HTTP request is made.

The fix is a symlink at the path Roo Code expects, pointing to the real
binary VS Code already ships. **VS Code updates wipe the symlink**, so the
fix must be re-applied after each update.

Full investigation, stack trace, ruled-out causes and lessons learned:
[references/roo-code-ripgrep-hang-fix.md](references/roo-code-ripgrep-hang-fix.md).
Read it when the user asks for background or when the script's fix doesn't
resolve the problem.

## Workflow

1. Briefly tell the user what the problem is and what the script will do
   (summarise "Why it happens" above).
2. Run the script from the repo root:

   ```bash
   bash .github/skills/epi-fix-roo-code/scripts/fix-roo-ripgrep.sh
   ```

3. Interpret the result:
   - `Already fixed: ...` — symlink is correct; nothing changed. The hang has
     another cause; see the reference doc's "What this was *not*" section and
     check the Webview Developer Tools console.
   - `Missing: ... Creating it now` / `Created: ...` — the fix was just
     (re)applied, most likely after a VS Code update.
   - `Found something unexpected ...` — the script refuses to overwrite it;
     show the user the path and let them decide.
   - `Expected VS Code ripgrep binary not found` — VS Code's layout changed
     again; inspect the `ripgrep-universal/bin` directory it names.
4. Tell the user to reload VS Code: `Cmd+Shift+P` → `Developer: Reload
   Window` → Enter.

## Preconditions & safety

- macOS only (the script aborts on other platforms), with VS Code at
  `/Applications/Visual Studio Code.app`.
- Creating the symlink uses `sudo` (the user's Mac login password). `sudo`
  needs an interactive terminal, so if the Bash tool can't prompt, ask the
  user to run the command themselves (e.g. with the `!` prefix in the
  prompt).
- The script is idempotent and never overwrites an existing non-matching file.
- This is a workaround for a Roo Code bug, not a permanent fix.
