#!/usr/bin/env bash
#
# Run from anywhere inside the repository:
#
# bash .github/skills/epi-setup-ai/scripts/setup-ai.sh

set -e

# Dynamically resolve repository root. This script lives in .github/skills/epi-setup-ai/scripts/, so
# the folder it sits in is NOT the root. Order: the git repository it is in (works through a symlink
# and from any working directory), then four folders up from here (scripts, epi-setup-ai, skills, .github).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)"; then
    :
else
    REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
fi

cd "$REPO_ROOT"
echo "Working in repository root: $REPO_ROOT"

# 1. Check and create .github folder and copilot-instructions.md placeholder if missing
mkdir -p .github
if [ ! -f ".github/copilot-instructions.md" ]; then
    echo "Creating default .github/copilot-instructions.md..."
    cat << 'EOF' > .github/copilot-instructions.md
# Project Instructions

## Overview
Add your core workspace guidelines, coding standards, and architectural patterns here.
EOF
fi

# Ensure .github/skills directory exists
mkdir -p .github/skills

# 2. Ensure .cline, .claude, and .roo directories exist
mkdir -p .cline .claude .roo

# 3. Create skills folder symlinks (.cline/skills, .claude/skills, .roo/skills -> .github/skills)
for TARGET_DIR in ".cline" ".claude" ".roo"; do
    SKILLS_LINK="$TARGET_DIR/skills"
    if [ -L "$SKILLS_LINK" ]; then
        rm "$SKILLS_LINK"
    elif [ -d "$SKILLS_LINK" ]; then
        rm -rf "$SKILLS_LINK"
    fi
    ln -s ../.github/skills "$SKILLS_LINK"
    echo "Created symlink: $SKILLS_LINK -> .github/skills"
done

# 4. Create root CLAUDE.md file symlink pointing to copilot-instructions.md
if [ -L "CLAUDE.md" ] || [ -f "CLAUDE.md" ]; then
    rm -f "CLAUDE.md"
fi
ln -s .github/copilot-instructions.md CLAUDE.md
echo "Created symlink: CLAUDE.md -> .github/copilot-instructions.md"

# 5. Ensure .clinerules directory exists and physically copy/overwrite copilot-instructions.md
CLINERULES_DIR=".clinerules"
TARGET_RULE="$CLINERULES_DIR/copilot-instructions.md"
SOURCE_INSTRUCTIONS=".github/copilot-instructions.md"

# Create directory if it doesn't exist
mkdir -p "$CLINERULES_DIR"

# Force remove any existing symlink, file, or stub to prevent link persistence
rm -rf "$TARGET_RULE"

if [ -f "$SOURCE_INSTRUCTIONS" ]; then
    # Physically copy the file over (ensures it's a real file, not a symlink)
    cp "$SOURCE_INSTRUCTIONS" "$TARGET_RULE"
    echo "Copied and updated $TARGET_RULE for Cline."
else
    echo "Warning: $SOURCE_INSTRUCTIONS not found."
fi

# 5b. Ensure .roo/rules directory exists and physically copy copilot-instructions.md
# (Roo Code inherited a lot of Cline's file-loading code; copying defensively
# here for the same reason .clinerules/ uses copies, not symlinks — see
# ../references/ai-agents-layout.md.)
ROORULES_DIR=".roo/rules"
TARGET_ROORULE="$ROORULES_DIR/copilot-instructions.md"

mkdir -p "$ROORULES_DIR"
rm -rf "$TARGET_ROORULE"

if [ -f "$SOURCE_INSTRUCTIONS" ]; then
    cp "$SOURCE_INSTRUCTIONS" "$TARGET_ROORULE"
    echo "Copied and updated $TARGET_ROORULE for Roo Code."
else
    echo "Warning: $SOURCE_INSTRUCTIONS not found."
fi

# 6. Process custom agents from .github/agents (supporting nested subfolders)
# Claude Code auto-discovers selectable subagent types from .claude/agents/*.md,
# so agents are symlinked there (not flat under .claude/) to enable native
# subagent selection instead of requiring manual @-mention/prompting.
AGENTS_SRC=".github/agents"
CLINERULES_DIR=".clinerules"
CLAUDE_DIR=".claude/agents"

# 6a. Remove what an earlier run generated for agents, so a removed or renamed agent does not leave
# stale files behind. These paths are generated and git-ignored (edit only in .github/):
#   - symlinks in .claude/agents (real files there are left alone)
#   - everything in .clinerules except the instructions copy made in step 5
#   - the .roo/rules-<agent>/ folders
if [ -d "$CLAUDE_DIR" ]; then
    find "$CLAUDE_DIR" -type l -exec rm -f {} +
fi
if [ -d "$CLINERULES_DIR" ]; then
    find "$CLINERULES_DIR" -depth -mindepth 1 ! -path "$CLINERULES_DIR/copilot-instructions.md" -delete
fi
for stale_dir in .roo/rules-*; do
    if [ -d "$stale_dir" ]; then
        rm -rf "$stale_dir"
    fi
done

if [ -d "$AGENTS_SRC" ]; then
    echo "Processing custom agents..."

    # Ensure root destination directories exist
    mkdir -p "$CLINERULES_DIR"
    mkdir -p "$CLAUDE_DIR"

    # Roo custom modes (.roomodes) are generated fresh from each agent file's
    # frontmatter every run — .github/agents/*.md stays the only source of
    # truth, nothing Roo-specific needs hand-maintaining alongside it.
    ROOMODES_TMP="$(mktemp)"
    echo "customModes:" > "$ROOMODES_TMP"

    # Recursively find all markdown files in .github/agents
    find "$AGENTS_SRC" -type f -name "*.md" | while read -r agent_file; do
        # Calculate relative path to preserve folder structure
        rel_path="${agent_file#$AGENTS_SRC/}"
        target_clinerules="$CLINERULES_DIR/$rel_path"
        target_claude="$CLAUDE_DIR/$rel_path"

        # Create nested subdirectories if multiple agent folders exist
        mkdir -p "$(dirname "$target_clinerules")"
        mkdir -p "$(dirname "$target_claude")"

        # 1. Physically copy to .clinerules folder for Cline
        rm -f "$target_clinerules"
        cp -L "$agent_file" "$target_clinerules"
        echo "Copied agent to $target_clinerules"

        # 2. Create symbolic link in .claude/agents pointing to the source agent file
        # (relative "../" depth computed from target_claude's folder depth so
        # nested .github/agents/ subfolders resolve correctly too)
        rm -f "$target_claude"
        target_claude_dir="$(dirname "$target_claude")"
        depth="$(awk -F'/' '{print NF}' <<< "$target_claude_dir")"
        rel_prefix=""
        for ((i = 0; i < depth; i++)); do
            rel_prefix="../$rel_prefix"
        done
        ln -s "${rel_prefix}${agent_file}" "$target_claude" 2>/dev/null || ln -s "$PWD/$agent_file" "$target_claude"
        echo "Created symlink at $target_claude"

        # 3. Roo Code has no "drop a markdown file in" agent mechanism (unlike
        # Cline's .clinerules/). Its mode-specific rules folder convention is
        # .roo/rules-<slug>/, and it doesn't expect YAML frontmatter in there,
        # so strip it before copying. The slug is just the file's basename
        # (matches Claude Code's subagent `name` convention already enforced
        # for files in this folder).
        slug="$(basename "$agent_file" .md)"
        target_roo_rules_dir=".roo/rules-$slug"
        if [ -e "$target_roo_rules_dir" ]; then
            echo "Warning: another agent already uses the Roo slug '$slug' ($agent_file); skipping Roo for this one. Give the agents different file names."
            continue
        fi
        mkdir -p "$target_roo_rules_dir"
        target_roo_rule="$target_roo_rules_dir/$(basename "$agent_file")"
        rm -f "$target_roo_rule"
        awk 'NR==1 && !/^---/{d=2} d>=2{print; next} /^---[ \t\r]*$/{d++}' "$agent_file" > "$target_roo_rule"
        echo "Copied stripped agent body to $target_roo_rule for Roo Code."

        # 4. Append this agent as a Roo custom mode. `roleDefinition` comes
        # straight from the frontmatter `description` (must be a single-line
        # double-quoted string, same as the existing convention). `groups`
        # has no frontmatter equivalent to derive from, so every generated
        # mode gets the full set (read/edit/browser/command/mcp) — parity
        # with the "All tools" access Claude Code subagents get, not a new
        # grant. Narrow a specific mode's `groups`/`fileRegex` by hand-editing
        # .roomodes locally if needed; it'll be overwritten on the next run,
        # so a permanent narrowing belongs as a script change instead.
        description="$(tr -d '\r' < "$agent_file" | sed -n 's/^description: *"\(.*\)" *$/\1/p' | head -n1)"
        if [ -n "$description" ]; then
            display_name="$(echo "$slug" | awk -F'-' '{for(i=1;i<=NF;i++){$i=toupper(substr($i,1,1)) substr($i,2)}; print}' OFS=' ')"
            {
                echo "  - slug: $slug"
                echo "    name: $display_name"
                echo "    roleDefinition: \"$description\""
                echo "    groups:"
                echo "      - read"
                echo "      - edit"
                echo "      - browser"
                echo "      - command"
                echo "      - mcp"
                echo "    source: project"
            } >> "$ROOMODES_TMP"
            echo "Added Roo custom mode '$slug' from $agent_file."
        else
            echo "Warning: no single-line frontmatter 'description' in $agent_file; skipping Roo custom mode."
        fi
    done

    if [ "$(wc -l < "$ROOMODES_TMP")" -gt 1 ]; then
        rm -f ".roomodes"
        cp "$ROOMODES_TMP" ".roomodes"
        echo "Generated .roomodes from .github/agents/*.md for Roo Code."
    else
        rm -f ".roomodes"
    fi
    rm -f "$ROOMODES_TMP"
else
    echo "Note: $AGENTS_SRC directory not found, skipping agent synchronization."
fi

# 7. Check and update .gitignore for folders and root CLAUDE.md
if [ ! -f ".gitignore" ]; then
    touch .gitignore
    echo "Created .gitignore file."
fi

# A .gitignore that does not end in a newline would get the first new entry glued to its last line
if [ -s .gitignore ] && [ -n "$(tail -c1 .gitignore)" ]; then
    echo >> .gitignore
fi

grep -qxF ".cline/" .gitignore || echo ".cline/" >> .gitignore
grep -qxF ".claude/" .gitignore || echo ".claude/" >> .gitignore
grep -qxF ".clinerules/" .gitignore || echo ".clinerules/" >> .gitignore
grep -qxF ".roo/" .gitignore || echo ".roo/" >> .gitignore
grep -qxF ".roomodes" .gitignore || echo ".roomodes" >> .gitignore
grep -qxF "CLAUDE.md" .gitignore || echo "CLAUDE.md" >> .gitignore
echo "Updated .gitignore with .cline/, .claude/, .clinerules/, .roo/, .roomodes, and CLAUDE.md"

# 8. Untrack folders and file if they were previously tracked by Git
git rm -r --cached .cline 2>/dev/null || true
git rm -r --cached .claude 2>/dev/null || true
git rm -r --cached .clinerules 2>/dev/null || true
git rm -r --cached .roo 2>/dev/null || true
git rm --cached .roomodes 2>/dev/null || true
git rm --cached CLAUDE.md 2>/dev/null || true

echo "AI tool environment setup completed successfully!"