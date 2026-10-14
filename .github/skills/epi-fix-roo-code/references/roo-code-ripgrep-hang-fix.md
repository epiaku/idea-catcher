# Roo Code hangs on "API Request" with no error (freellmapi provider)

## Quick fix (do this after every VS Code update)

VS Code's updater wipes and recreates the symlink this fix relies on, so Roo
Code will start hanging again after any VS Code update. Run this from the
repo root, then reload the window:

```bash
bash .github/skills/epi-fix-roo-code/scripts/fix-roo-ripgrep.sh
```

If it needs to (re)create the symlink, it'll prompt for your `sudo`
password — that's the same login password you use to unlock this MacBook
Air.

Then reload VS Code: `Cmd+Shift+P` → `Developer: Reload Window` → Enter.

The script is idempotent — if the symlink is already correct it says so and
exits without changing anything; if it had to recreate it, that's the
confirmation you needed to re-apply the fix. Full background below.

## Symptom

Roo Code (VS Code extension, `rooveterinaryinc.roo-cline`) was configured with an
OpenAI-compatible provider pointed at a local LLM router
(`http://10.10.60.12:3001/v1`). Sending any chat message produced no response:
the UI showed a permanently spinning **API Request** with no text, no error,
and no timeout. The "Roo Code" output channel showed only checkpoint/shadow-git
setup logs and then nothing:

```
[createTask] parent task ...36fb81a3 instantiated
[Task#getCheckpointService] initializing shadow git
[t#getNestedGitRepository] failed to check for nested git repos: ripgrep not found: undefined
[t#create] git = 2.39.5
[t#initShadowGit] creating shadow git repo at .../checkpoints
[t#initShadowGit] initialized shadow repo with base commit ... in 1761ms
[Task#getCheckpointService] service initialized
```

Nothing else was ever logged. No network request of any kind was made.

## Root cause

Roo Code's internal ripgrep locator only checks for the binary at these paths,
relative to `vscode.env.appRoot`:

```
node_modules.asar.unpacked/@vscode/ripgrep/bin/
node_modules.asar.unpacked/vscode-ripgrep/bin/
node_modules/@vscode/ripgrep/bin/
node_modules/vscode-ripgrep/bin
```

Recent VS Code builds (confirmed on 1.138.0) ship ripgrep under a different,
newer layout instead — a renamed package with a per-architecture subfolder:

```
node_modules.asar.unpacked/@vscode/ripgrep-universal/bin/darwin-arm64/rg
node_modules.asar.unpacked/@vscode/ripgrep-universal/bin/darwin-x64/rg
```

Roo Code 3.54.0's lookup logic predates this rename/relayout, so on any VS
Code install using the newer layout it can **never** find `rg`. This isn't
cosmetic: the lookup runs inside the pre-request step that executes before
every single API call (stack trace below), and the resulting promise
rejection is never caught by Roo Code — VS Code's own log even flags it
explicitly: `rejected promise not handled within 1 second`. That leaves the
task loop dead in the water before it ever builds an HTTP request, with the
chat UI stuck showing "API Request" forever and nothing surfaced to the user.

Confirmed via the Webview Developer Tools console (`Developer: Open Webview
Developer Tools`, then Console tab), which showed the real stack trace that
the checkpoint-scoped log line hid:

```
Error: Could not find ripgrep binary
    at _4a (.../rooveterinaryinc.roo-cline-3.54.0/dist/extension.js:853:12306)
    at async xO (.../dist/extension.js:853:10986)
    at async f$ (.../dist/extension.js:5201:265)
    at async t.recursivelyMakeClineRequests (.../dist/extension.js:5279:5521)
    at async t.initiateTaskLoop (.../dist/extension.js:5279:3923)
    at async t.startTask (.../dist/extension.js:5275:12037)
```

`recursivelyMakeClineRequests` is the function responsible for actually
dispatching each API call — confirming the ripgrep lookup runs, and fails,
before any network request is attempted.

### What this was *not*

Worth recording because each of these looked plausible early on and had to be
ruled out one at a time:

- **Not an auth problem.** The freellmapi server does require an API key
  (`curl` without one returns `401 Invalid API key`), but the key was already
  correctly configured in Roo Code's provider settings.
- **Not a broken server.** Direct `curl` calls to `/v1/models`,
  non-streaming `/v1/chat/completions`, streaming `/v1/chat/completions`, and
  streaming with a `tools` array (mimicking Roo's function-calling requests)
  all succeeded quickly with correctly formed SSE output including a
  trailing `data: [DONE]`.
- **Not stale task config.** Roo Code snapshots provider config per task, so
  changing settings mid-task doesn't apply — but the hang reproduced
  identically on a brand-new task too.
- **Not a CSP block in the webview.** This was the leading hypothesis right
  before the real cause was found (a silent hang with zero network attempts
  is the classic signature of a webview `connect-src` CSP rejection). Opening
  the webview devtools ruled it out and surfaced the real stack trace
  instead.
- **Reinstalling the extension did not help**, because Roo Code doesn't
  bundle its own `rg` binary at all (its `dist/bin/` only contains
  `esbuild`) — it was always relying on VS Code's copy, and VS Code's copy
  had simply moved.
- **A system-wide `sudo ln -s $(which rg) /usr/local/bin/rg` did not help**,
  because Roo Code's locator resolves an absolute path under
  `vscode.env.appRoot` — it never consults `$PATH`.

## How the debug proxy helped narrow it down

Before the ripgrep stack trace was found, a small local HTTP proxy
(`local-buffer-proxy/local-buffer-proxy.js`)
was used to sit between Roo Code and the freellmapi server, logging every
request/response that passed through it (method, headers with the API key
redacted, body, upstream status code, time-to-first-byte, total bytes).

Pointing Roo Code's Base URL at the proxy (`http://127.0.0.1:4000/v1`
instead of `http://10.10.60.12:3001/v1`) and reproducing the hang showed
**zero requests reaching the proxy** for an actual chat send — only a
`GET /v1/models` call from the settings screen ever showed up. That single
observation was the pivot point of the whole investigation: it proved the
hang happened entirely inside Roo Code, before any network I/O, which ruled
out the server, the network path, and the API key in one step and pointed
the search at Roo Code's own pre-request pipeline instead. Without that
proxy, it would have been easy to keep suspecting the server or the network
indefinitely, since nothing about the symptom (silent hang, no error)
distinguishes "request never sent" from "request sent and never answered."

The original version of the proxy only logged `method + url`; it was
extended mid-investigation to also log headers, request body, upstream
response status/headers, and byte-level timing, which is what made the
"zero requests arrived" observation legible in the first place. That
enhanced version is what's committed.

## Fix

[scripts/fix-roo-ripgrep.sh](../scripts/fix-roo-ripgrep.sh) creates the path Roo
Code's locator expects, as a symlink to the real binary VS Code already
ships:

```bash
bash .github/skills/epi-fix-roo-code/scripts/fix-roo-ripgrep.sh
```

It is idempotent and safe to re-run:

- If the correct symlink already exists, it reports that and exits without
  changing anything.
- If something unexpected already exists at that path, it refuses to touch
  it rather than clobbering it.
- If it has to create the symlink, it says so explicitly — which doubles as
  a signal that a VS Code update just wiped it (see Caveats).

After running it, reload VS Code (`Cmd+Shift+P` → `Developer: Reload Window`
→ Enter) for Roo Code to pick up the change.

### Caveats

- The symlink lives inside `/Applications/Visual Studio Code.app`, which VS
  Code's auto-updater replaces wholesale (`node_modules.asar.unpacked/` and
  everything under it) on every update. **Expect this fix to need
  re-applying after VS Code updates** — just rerun the script; it'll report
  "missing, creating" instead of "already fixed" when that happens.
- This is a workaround for a Roo Code bug, not a proper fix. The real fix is
  for Roo Code to update its ripgrep-locator to also check the
  `@vscode/ripgrep-universal/bin/<platform>-<arch>/rg` layout. Worth checking
  the Roo Code GitHub issues for an existing report, or filing one, before
  assuming this workaround will be needed indefinitely.

## Lessons learned

- **A hang with zero logged error is a stronger clue than it looks.** The
  instinct was to suspect the server/network first (wrong provider key,
  broken streaming, blocked connection) because that's the visible half of
  the system. But "nothing happened, not even an error" almost always means
  the failure is upstream of where you're looking — in this case, before the
  HTTP client was even invoked.
- **Rule out the far side before debugging the near side.** A handful of
  direct `curl` calls (auth check, non-streaming, streaming, streaming with
  tool-calls) fully validated the server in a few minutes and eliminated an
  entire branch of hypotheses, before touching the VS Code extension at all.
- **A single log line can be a red herring even when it looks exactly like
  the bug.** The `ripgrep not found: undefined` line was visible from the
  very first paste, but it appeared in a code path
  (`getNestedGitRepository`, part of checkpoint setup) that completed
  successfully anyway — so it was initially dismissed as unrelated, and a
  `sudo ln -s` fix aimed at it accomplished nothing. It turned out to be the
  right suspect, just encountered in the wrong place first; the same failure
  (missing `rg`) recurs in a second, unrelated code path
  (`recursivelyMakeClineRequests`) where it isn't caught.
- **A minimal debug proxy is worth building the moment "is the request even
  being sent?" becomes a real question.** It's a ~50-line script with no
  dependencies beyond Node's built-in `http` module, and it turned a
  guessing game between "client bug," "network," and "server bug" into a
  single yes/no observation (request arrived or it didn't).
- **The VS Code Extension Host output channel is not the full picture for a
  webview-based extension.** The real stack trace only appeared in the
  *Webview Developer Tools* console (`Developer: Open Webview Developer
  Tools`), not in the regular "Roo Code" output channel. When an extension
  has a chat-panel UI, that's a second place to check before concluding
  "nothing was logged."
- **Don't assume reinstalling an extension fixes a missing-dependency
  error.** It only helps if the extension is supposed to bundle the
  dependency itself and the install was corrupted. Here Roo Code was never
  bundling `rg` — it depends on the host application (VS Code) for it — so
  reinstalling Roo Code changed nothing, while confirming that fact (`find
  ... -iname rg` inside the extension directory coming back empty, twice)
  is what redirected the investigation toward a host/extension version
  mismatch instead.
