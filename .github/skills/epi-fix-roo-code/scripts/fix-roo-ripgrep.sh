#!/usr/bin/env bash
#
# Fixes Roo Code (rooveterinaryinc.roo-cline) failing every chat request with
# "Could not find ripgrep binary" and hanging on "API Request" forever.
#
# Root cause: Roo Code's bundled ripgrep-locator only checks these paths
# under vscode.env.appRoot:
#   node_modules.asar.unpacked/@vscode/ripgrep/bin/
#   node_modules.asar.unpacked/vscode-ripgrep/bin/
#   node_modules/@vscode/ripgrep/bin/
#   node_modules/vscode-ripgrep/bin
# Recent VS Code builds (e.g. 1.138.0) ship ripgrep under a different,
# newer layout instead:
#   node_modules.asar.unpacked/@vscode/ripgrep-universal/bin/<platform-arch>/rg
# Roo Code's lookup logic predates this rename/relayout, so it can never
# find the binary, throws inside the pre-request step that runs before
# every API call, and the unhandled rejection leaves the chat UI stuck
# with no visible error.
#
# This script creates the path Roo Code expects as a symlink to the real
# binary VS Code already ships. It's idempotent: safe to re-run any time,
# including after a VS Code update wipes node_modules.asar.unpacked/ and
# the symlink needs recreating.
#
# Run from anywhere (it does not depend on the project):
#
# bash .github/skills/epi-fix-roo-code/scripts/fix-roo-ripgrep.sh

set -e

VSCODE_APP_ROOT="/Applications/Visual Studio Code.app/Contents/Resources/app"
UNIVERSAL_RG_DIR="$VSCODE_APP_ROOT/node_modules.asar.unpacked/@vscode/ripgrep-universal/bin"
EXPECTED_RG_DIR="$VSCODE_APP_ROOT/node_modules.asar.unpacked/@vscode/ripgrep/bin"
EXPECTED_RG_LINK="$EXPECTED_RG_DIR/rg"

if [[ "$(uname)" != "Darwin" ]]; then
    echo "This script only supports macOS VS Code installs. Aborting." >&2
    exit 1
fi

ARCH="$(uname -m)"
case "$ARCH" in
    arm64) PLATFORM_ARCH="darwin-arm64" ;;
    x86_64) PLATFORM_ARCH="darwin-x64" ;;
    *)
        echo "Unrecognized architecture '$ARCH'. Aborting." >&2
        exit 1
        ;;
esac

REAL_RG_BIN="$UNIVERSAL_RG_DIR/$PLATFORM_ARCH/rg"

if [[ ! -f "$REAL_RG_BIN" ]]; then
    echo "Expected VS Code ripgrep binary not found at:" >&2
    echo "  $REAL_RG_BIN" >&2
    echo "VS Code's ripgrep bundling may have changed again; inspect $UNIVERSAL_RG_DIR manually." >&2
    exit 1
fi

if [[ -L "$EXPECTED_RG_LINK" && "$(readlink "$EXPECTED_RG_LINK")" == "$REAL_RG_BIN" ]]; then
    echo "Already fixed: $EXPECTED_RG_LINK -> $REAL_RG_BIN"
    echo "(symlink already existed and points to the correct binary; nothing to do)"
    exit 0
fi

if [[ -e "$EXPECTED_RG_LINK" || -L "$EXPECTED_RG_LINK" ]]; then
    echo "Found something unexpected at $EXPECTED_RG_LINK (not our symlink) -- leaving it alone." >&2
    echo "Remove it manually if you want this script to replace it." >&2
    exit 1
fi

echo "Missing: $EXPECTED_RG_LINK"
echo "This is likely missing because of a recent VS Code update. Creating it now (requires sudo)..."

sudo mkdir -p "$EXPECTED_RG_DIR"
sudo ln -s "$REAL_RG_BIN" "$EXPECTED_RG_LINK"

echo "Created: $EXPECTED_RG_LINK -> $REAL_RG_BIN"
echo ""
echo "Now reload VS Code so Roo Code picks this up:"
echo "  1. Press Cmd+Shift+P"
echo "  2. Type: Developer: Reload Window"
echo "  3. Press Enter"
