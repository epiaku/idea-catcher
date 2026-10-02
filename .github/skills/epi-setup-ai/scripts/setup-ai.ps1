# Requires -Version 5.0
# Note on Windows Symlinks: Creating symbolic links in Windows 
# requires either running PowerShell as an Administrator 
# or having Developer Mode enabled in Windows settings.
#
# Run from anywhere inside the repository:
#
# .\.github\skills\epi-setup-ai\scripts\setup-ai.ps1

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Dynamically resolve repository root. This script lives in .github\skills\epi-setup-ai\scripts\, so
# the folder it sits in is NOT the root. Order: the git repository it is in, then four folders up from
# here (scripts, epi-setup-ai, skills, .github).
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$GitRoot = $null
try { $GitRoot = (& git -C $ScriptDir rev-parse --show-toplevel 2>$null) } catch { $GitRoot = $null }
if ($GitRoot) {
    $RepoRoot = "$GitRoot"
} else {
    $RepoRoot = $ScriptDir
    1..4 | ForEach-Object { $RepoRoot = Split-Path -Parent $RepoRoot }
}

# Remove a file, folder or link. A symlink is removed WITHOUT following it: Remove-Item -Recurse on a
# directory symlink can delete the contents of the target in Windows PowerShell 5.1, and that would
# destroy .github\skills. Test-Path is not used, because it is false for a dangling link.
function Remove-PathSafe([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if (-not $item) { return }
    if ($item.LinkType) {
        try { [System.IO.File]::Delete($item.FullName) } catch { [System.IO.Directory]::Delete($item.FullName) }
        if (Test-Path -LiteralPath $item.FullName) { [System.IO.Directory]::Delete($item.FullName) }
    } elseif ($item.PSIsContainer) {
        Remove-Item -LiteralPath $Path -Recurse -Force
    } else {
        Remove-Item -LiteralPath $Path -Force
    }
}

Set-Location $RepoRoot
Write-Host "Working in repository root: $RepoRoot" -ForegroundColor Cyan

# 1. Check and create .github folder and copilot-instructions.md placeholder if missing
if (!(Test-Path ".github")) {
    New-Item -ItemType Directory -Path ".github" | Out-Null
}
$CopilotPath = ".github\copilot-instructions.md"
if (!(Test-Path $CopilotPath)) {
    Write-Host "Creating default $CopilotPath..."
    @"
# Project Instructions

## Overview
Add your core workspace guidelines, coding standards, and architectural patterns here.
"@ | Set-Content -Path $CopilotPath -Encoding utf8
}

# Ensure .github\skills exists
if (!(Test-Path ".github\skills")) {
    New-Item -ItemType Directory -Path ".github\skills" | Out-Null
}

# 2. Ensure .cline, .claude, and .roo directories exist
if (!(Test-Path ".cline")) { New-Item -ItemType Directory -Path ".cline" | Out-Null }
if (!(Test-Path ".claude")) { New-Item -ItemType Directory -Path ".claude" | Out-Null }
if (!(Test-Path ".roo")) { New-Item -ItemType Directory -Path ".roo" | Out-Null }

# 3. Create skills folder symlinks
$ToolsToLink = @(".cline\skills", ".claude\skills", ".roo\skills")
foreach ($LinkPath in $ToolsToLink) {
    Remove-PathSafe $LinkPath
    New-Item -ItemType SymbolicLink -Path $LinkPath -Target "..\.github\skills" | Out-Null
    Write-Host "Created symlink: $LinkPath -> .github\skills" -ForegroundColor Green
}

# 4. Create root CLAUDE.md file symlink pointing to copilot-instructions.md
$ClaudeMd = "CLAUDE.md"
Remove-PathSafe $ClaudeMd
New-Item -ItemType SymbolicLink -Path $ClaudeMd -Target ".github\copilot-instructions.md" | Out-Null
Write-Host "Created symlink: CLAUDE.md -> .github/copilot-instructions.md" -ForegroundColor Green

# 5. Ensure .clinerules directory exists and physically copy/overwrite copilot-instructions.md
$ClinerulesDir = ".clinerules"
$TargetRule = Join-Path $ClinerulesDir "copilot-instructions.md"
$SourceInstructions = ".github/copilot-instructions.md"

if (!(Test-Path $ClinerulesDir)) {
    New-Item -ItemType Directory -Path $ClinerulesDir | Out-Null
}

if (Test-Path $SourceInstructions) {
    # Force copy and overwrite the file directly
    Copy-Item $SourceInstructions -Destination $TargetRule -Force
    Write-Host "Copied and updated $TargetRule for Cline." -ForegroundColor Green
} else {
    Write-Host "Warning: $SourceInstructions not found." -ForegroundColor Yellow
}

# 5b. Ensure .roo\rules directory exists and physically copy copilot-instructions.md
# (Roo Code inherited a lot of Cline's file-loading code; copying defensively
# here for the same reason .clinerules\ uses copies, not symlinks — see
# ..\references\ai-agents-layout.md.)
$RooRulesDir = ".roo\rules"
$TargetRooRule = Join-Path $RooRulesDir "copilot-instructions.md"

if (!(Test-Path $RooRulesDir)) { New-Item -ItemType Directory -Path $RooRulesDir | Out-Null }
if (Test-Path $TargetRooRule) { Remove-Item -Force $TargetRooRule }

if (Test-Path $SourceInstructions) {
    Copy-Item $SourceInstructions -Destination $TargetRooRule -Force
    Write-Host "Copied and updated $TargetRooRule for Roo Code." -ForegroundColor Green
} else {
    Write-Host "Warning: $SourceInstructions not found." -ForegroundColor Yellow
}

# 6. Process custom agents from .github/agents (supporting nested subfolders)
# Claude Code auto-discovers selectable subagent types from .claude/agents/*.md,
# so agents are symlinked there (not flat under .claude/) to enable native
# subagent selection instead of requiring manual @-mention/prompting.
$AgentsSrc = ".github/agents"
$ClinerulesDir = ".clinerules"
$ClaudeDir = ".claude/agents"

# 6a. Remove what an earlier run generated for agents, so a removed or renamed agent does not leave
# stale files behind. These paths are generated and git-ignored (edit only in .github\):
#   - symlinks in .claude\agents (real files there are left alone)
#   - everything in .clinerules except the instructions copy made in step 5
#   - the .roo\rules-<agent>\ folders
if (Test-Path $ClaudeDir) {
    Get-ChildItem -Path $ClaudeDir -Recurse -Force | Where-Object { $_.LinkType } | ForEach-Object { Remove-PathSafe $_.FullName }
}
if (Test-Path $ClinerulesDir) {
    Get-ChildItem -Path $ClinerulesDir -Force | Where-Object { $_.Name -ne "copilot-instructions.md" } | ForEach-Object { Remove-PathSafe $_.FullName }
}
if (Test-Path ".roo") {
    Get-ChildItem -Path ".roo" -Directory -Filter "rules-*" -Force | ForEach-Object { Remove-PathSafe $_.FullName }
}

if (Test-Path $AgentsSrc) {
    Write-Host "Processing custom agents..." -ForegroundColor Cyan

    if (!(Test-Path $ClinerulesDir)) { New-Item -ItemType Directory -Path $ClinerulesDir | Out-Null }
    if (!(Test-Path $ClaudeDir)) { New-Item -ItemType Directory -Path $ClaudeDir | Out-Null }

    # Roo custom modes (.roomodes) are generated fresh from each agent file's
    # frontmatter every run — .github/agents/*.md stays the only source of
    # truth, nothing Roo-specific needs hand-maintaining alongside it.
    $RoomodesTmp = [System.IO.Path]::GetTempFileName()
    "customModes:" | Set-Content -Path $RoomodesTmp -Encoding utf8

    # Recursively process all markdown files inside .github/agents
    Get-ChildItem -Path $AgentsSrc -Recurse -File -Filter "*.md" | ForEach-Object {
        $relPath = $_.FullName.Substring((Resolve-Path $AgentsSrc).Path.Length + 1)
        $targetClinerules = Join-Path $ClinerulesDir $relPath
        $targetClaude = Join-Path $ClaudeDir $relPath
        
        # Ensure parent directories exist for nested structures
        $clinerulesParent = Split-Path $targetClinerules -Parent
        $claudeParent = Split-Path $targetClaude -Parent
        if (!(Test-Path $clinerulesParent)) { New-Item -ItemType Directory -Path $clinerulesParent | Out-Null }
        if (!(Test-Path $claudeParent)) { New-Item -ItemType Directory -Path $claudeParent | Out-Null }
        
        # 1. Physically copy to .clinerules
        if (Test-Path $targetClinerules) { Remove-Item -Force $targetClinerules }
        Copy-Item $_.FullName -Destination $targetClinerules -Force
        Write-Host "Copied agent to $targetClinerules" -ForegroundColor Green
        
        # 2. Create symbolic link in .claude pointing to the agent file
        # Relative target, like the .sh: one "..\" per folder level of the link's own folder
        Remove-PathSafe $targetClaude
        $depth = ($claudeParent -split '[\\/]').Count
        $relTarget = ("..\" * $depth) + ".github\agents\$relPath"
        New-Item -ItemType SymbolicLink -Path $targetClaude -Target $relTarget | Out-Null
        Write-Host "Created symlink at $targetClaude" -ForegroundColor Green

        # 3. Roo Code has no "drop a markdown file in" agent mechanism (unlike
        # Cline's .clinerules\). Its mode-specific rules folder convention is
        # .roo\rules-<slug>\, and it doesn't expect YAML frontmatter in there,
        # so strip it before copying. The slug is just the file's basename
        # (matches Claude Code's subagent `name` convention already enforced
        # for files in this folder).
        $slug = $_.BaseName
        $targetRooRulesDir = ".roo\rules-$slug"
        if (Test-Path $targetRooRulesDir) {
            Write-Host "Warning: another agent already uses the Roo slug '$slug' ($($_.Name)); skipping Roo for this one. Give the agents different file names." -ForegroundColor Yellow
            return
        }
        New-Item -ItemType Directory -Path $targetRooRulesDir | Out-Null
        $targetRooRule = Join-Path $targetRooRulesDir $_.Name
        if (Test-Path $targetRooRule) { Remove-Item -Force $targetRooRule }

        $lines = Get-Content $_.FullName
        $delimiterIndices = @()
        for ($i = 0; $i -lt $lines.Count; $i++) {
            if ($lines[$i] -match '^---\s*$') { $delimiterIndices += $i }
        }
        if ($delimiterIndices.Count -ge 2) {
            $bodyStart = $delimiterIndices[1] + 1
            if ($bodyStart -lt $lines.Count) {
                $lines[$bodyStart..($lines.Count - 1)] | Set-Content -Path $targetRooRule -Encoding utf8
            } else {
                Set-Content -Path $targetRooRule -Value "" -Encoding utf8
            }
        } else {
            $lines | Set-Content -Path $targetRooRule -Encoding utf8
        }
        Write-Host "Copied stripped agent body to $targetRooRule for Roo Code." -ForegroundColor Green

        # 4. Append this agent as a Roo custom mode. `roleDefinition` comes
        # straight from the frontmatter `description` (must be a single-line
        # double-quoted string, same as the existing convention). `groups`
        # has no frontmatter equivalent to derive from, so every generated
        # mode gets the full set (read/edit/browser/command/mcp) — parity
        # with the "All tools" access Claude Code subagents get, not a new
        # grant. Narrow a specific mode's `groups`/`fileRegex` by hand-editing
        # .roomodes locally if needed; it'll be overwritten on the next run,
        # so a permanent narrowing belongs as a script change instead.
        $descLine = $lines | Where-Object { $_ -match '^description:\s*"(.*)"\s*$' } | Select-Object -First 1
        if ($descLine -match '^description:\s*"(.*)"\s*$') {
            $roleDefinition = $Matches[1]
            $displayName = ($slug -split '-' | ForEach-Object { $_.Substring(0,1).ToUpper() + $_.Substring(1) }) -join ' '
            Add-Content -Path $RoomodesTmp -Value "  - slug: $slug"
            Add-Content -Path $RoomodesTmp -Value "    name: $displayName"
            Add-Content -Path $RoomodesTmp -Value "    roleDefinition: `"$roleDefinition`""
            Add-Content -Path $RoomodesTmp -Value "    groups:"
            Add-Content -Path $RoomodesTmp -Value "      - read"
            Add-Content -Path $RoomodesTmp -Value "      - edit"
            Add-Content -Path $RoomodesTmp -Value "      - browser"
            Add-Content -Path $RoomodesTmp -Value "      - command"
            Add-Content -Path $RoomodesTmp -Value "      - mcp"
            Add-Content -Path $RoomodesTmp -Value "    source: project"
            Write-Host "Added Roo custom mode '$slug' from $($_.Name)." -ForegroundColor Green
        } else {
            Write-Host "Warning: no single-line frontmatter 'description' in $($_.Name); skipping Roo custom mode." -ForegroundColor Yellow
        }
    }

    if ((Get-Content $RoomodesTmp).Count -gt 1) {
        if (Test-Path ".roomodes") { Remove-Item -Force ".roomodes" }
        Copy-Item $RoomodesTmp -Destination ".roomodes" -Force
        Write-Host "Generated .roomodes from .github/agents/*.md for Roo Code." -ForegroundColor Green
    } elseif (Test-Path ".roomodes") {
        Remove-Item -Force ".roomodes"
    }
    Remove-Item -Force $RoomodesTmp
} else {
    Write-Host "Note: $AgentsSrc directory not found, skipping agent synchronization." -ForegroundColor Yellow
}

# 7. Check and update .gitignore
if (!(Test-Path ".gitignore")) {
    New-Item -ItemType File -Path ".gitignore" | Out-Null
    Write-Host "Created .gitignore file." -ForegroundColor Green
}
# A .gitignore that does not end in a newline would get the first new entry glued to its last line
$GitIgnoreRaw = Get-Content ".gitignore" -Raw -ErrorAction SilentlyContinue
if ($GitIgnoreRaw -and -not $GitIgnoreRaw.EndsWith("`n")) { Add-Content -Path ".gitignore" -Value "" }
$GitIgnoreContent = Get-Content ".gitignore" -ErrorAction SilentlyContinue
if ($GitIgnoreContent -notcontains ".cline/") {
    Add-Content -Path ".gitignore" -Value ".cline/"
}
if ($GitIgnoreContent -notcontains ".claude/") {
    Add-Content -Path ".gitignore" -Value ".claude/"
}
if ($GitIgnoreContent -notcontains ".clinerules/") {
    Add-Content -Path ".gitignore" -Value ".clinerules/"
}
if ($GitIgnoreContent -notcontains ".roo/") {
    Add-Content -Path ".gitignore" -Value ".roo/"
}
if ($GitIgnoreContent -notcontains ".roomodes") {
    Add-Content -Path ".gitignore" -Value ".roomodes"
}
if ($GitIgnoreContent -notcontains "CLAUDE.md") {
    Add-Content -Path ".gitignore" -Value "CLAUDE.md"
}
Write-Host "Updated .gitignore" -ForegroundColor Green

# 8. Untrack folders and root CLAUDE.md if they were previously tracked by Git
git rm -r --cached .cline 2>$null
git rm -r --cached .claude 2>$null
git rm -r --cached .clinerules 2>$null
git rm -r --cached .roo 2>$null
git rm --cached .roomodes 2>$null
git rm --cached CLAUDE.md 2>$null

Write-Host "AI tool environment setup completed successfully!" -ForegroundColor Green