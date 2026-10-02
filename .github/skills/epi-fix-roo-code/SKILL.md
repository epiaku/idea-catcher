---
name: epi-fix-roo-code
description: "Fix Roo Code (rooveterinaryinc.roo-cline) failing every chat request with 'Could not find ripgrep binary' and hanging on 'API Request'. Explains why the ripgrep path has to be added to the VS Code install on macOS, and runs the script that creates it. Use when Roo Code never answers, after a VS Code update broke it again, or when someone asks why this symlink exists."
metadata:
  version: "1.0.0"
  source: "epiaku"
  author: "rob.de.beir@epiaku.com"
---

# epi-fix-roo-code

Roo Code cannot find the `ripgrep` (`rg`) program that VS Code ships, so every chat request fails. This skill explains why, and fixes it with one script.

## When to use

Use it when **any** of these is true:

- Roo Code shows **`Could not find ripgrep binary`**, or its chat is stuck on **`API Request`** forever with no visible error.
- Roo Code worked before and stopped after a **VS Code update**.
- Someone asks **why there is a symlink inside the VS Code app** (the explanation is below).

It does not apply to Claude Code, Cline or Copilot. It is only about Roo Code, and only about VS Code on **macOS**.

## Why the ripgrep path has to be added

Roo Code needs `ripgrep` before **every** API request (it searches the workspace to build the context). It does not bring its own copy. It looks for the one inside VS Code, and it only checks a **fixed list of folders** under VS Code's app folder (`vscode.env.appRoot`):

```text
node_modules.asar.unpacked/@vscode/ripgrep/bin/
node_modules.asar.unpacked/vscode-ripgrep/bin/
node_modules/@vscode/ripgrep/bin/
node_modules/vscode-ripgrep/bin
```

Recent VS Code builds (for example **1.138.0**) moved `ripgrep` to a **new layout** that is not on that list:

```text
node_modules.asar.unpacked/@vscode/ripgrep-universal/bin/<platform-arch>/rg
```

Roo Code's lookup was written before this rename, so it **can never find the program**. The error is thrown in a step that runs before every API call, and nothing handles it. The chat window therefore just hangs, with no error shown. That is why it looks like Roo Code is "thinking" when it has actually failed.

## What the fix does

The script creates the folder and file Roo Code expects, as a **symlink to the real binary VS Code already ships**:

```text
<VS Code>/Contents/Resources/app/node_modules.asar.unpacked/@vscode/ripgrep/bin/rg
    -> .../@vscode/ripgrep-universal/bin/<darwin-arm64 or darwin-x64>/rg
```

- It adds **no new program**. It only points the old path at the binary that is already there.
- It is **idempotent**: if the right symlink exists, it says "Already fixed" and changes nothing.
- It **refuses to touch** anything else at that path (a real file, or a link to somewhere else) and tells you to remove it by hand.
- It needs **`sudo`**, because the folder is inside `/Applications/Visual Studio Code.app`.

## How to run it

**Needs the user.** The script asks for the administrator password (`sudo`), and an agent cannot type it. Do **not** run it silently. Tell the user what it does, and ask them to run it in their **own terminal**, from the project root:

```bash
bash .github/skills/epi-fix-roo-code/scripts/fix-roo-ripgrep.sh
```

Then they must **reload VS Code**: `Cmd+Shift+P`, type `Developer: Reload Window`, press Enter.

### Check first, without sudo

To see whether the fix is already in place, or whether the problem is something else:

```bash
RG="/Applications/Visual Studio Code.app/Contents/Resources/app/node_modules.asar.unpacked/@vscode/ripgrep/bin/rg"
ls -l "$RG"                     # a symlink to .../ripgrep-universal/bin/<platform>/rg means it is fixed
"$RG" --version                 # prints the ripgrep version when the link works
```

## What the script can say

| Output | Meaning | What to do |
| --- | --- | --- |
| `Already fixed: ... -> ...` | The right symlink exists | Nothing. If Roo Code still hangs, the cause is something else (an API key or model setting) |
| `Created: ... -> ...` | The symlink was made | Reload the VS Code window |
| `This script only supports macOS VS Code installs` | Linux or Windows | The same cause may exist there, but this script does not cover it |
| `Unrecognized architecture` | Not `arm64` or `x86_64` | Stop and look at the machine |
| `Expected VS Code ripgrep binary not found at: ...` | VS Code **changed the layout again** | List `.../@vscode/ripgrep-universal/bin/` by hand, find where `rg` is now, and update `UNIVERSAL_RG_DIR` in the script |
| `Found something unexpected at ... -- leaving it alone` | Something else is at the path | Look at it. Remove it by hand only if you are sure, then run the script again |

## When to run it again

- **After every VS Code update.** An update can replace the `node_modules.asar.unpacked` folder and delete the symlink. Run the script again, then reload the window.
- If the script stops working, VS Code moved `ripgrep` again. Also check whether a **newer Roo Code** now finds it by itself: after updating Roo Code, test it **without** the symlink before keeping the workaround.

## Limits and safety

- **macOS only.** The script uses `uname`, and the fixed app path `/Applications/Visual Studio Code.app`. A VS Code installed somewhere else, or VS Code Insiders, is not found and the script stops with a message.
- **It changes the VS Code installation, not the project.** It writes inside `/Applications`, with `sudo`. Tell the user before running it.
- It is a **workaround for Roo Code's lookup**, not a change to VS Code or Roo Code themselves.
