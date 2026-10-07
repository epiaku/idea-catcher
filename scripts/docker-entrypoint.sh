#!/usr/bin/env bash
# Container entrypoint: clone the ideas and docs repos on first start, then exec the command.
# Never run with set -x or an error trap: a remote URL may carry credentials.
set -euo pipefail

# git must never prompt (no terminal in a container); the token comes from GIT_ASKPASS.
export GIT_TERMINAL_PROMPT=0

if [ "$#" -eq 0 ]; then
  echo "entrypoint: no command given" >&2
  exit 2
fi

# Hide the user:pass part of any URL in text we print.
mask() { sed -E 's#://[^/@[:space:]]*@#://***@#g'; }

ensure_clone() {
  local repo_var="$1" remote_var="$2"
  local repo="${!repo_var:-}" remote="${!remote_var:-}"

  if [ -z "$repo" ]; then
    echo "entrypoint: $repo_var is not set" >&2
    exit 2
  fi
  if [ -e "$repo/.git" ]; then
    return 0
  fi
  if [ -d "$repo" ] && [ -n "$(ls -A "$repo")" ]; then
    # To re-clone, delete the folder (or the volume) yourself; this script never does.
    echo "entrypoint: $repo has no .git and is not empty; leaving it untouched ($remote_var is not used)" >&2
    exit 2
  fi
  if [ -e "$repo" ] && [ ! -d "$repo" ]; then
    echo "entrypoint: $repo exists and is not a directory" >&2
    exit 2
  fi
  if [ -z "$remote" ]; then
    echo "entrypoint: $repo is not a git checkout and $remote_var is not set" >&2
    exit 2
  fi

  if printf '%s' "$remote" | grep -Eq '^[A-Za-z][A-Za-z0-9+.-]*://[^/]*@'; then
    echo "entrypoint: $remote_var ($(printf '%s' "$remote" | mask)) carries credentials; put the token in GITHUB_TOKEN, not in the URL" >&2
    exit 2
  fi

  local created=0 out rc=0
  [ -d "$repo" ] || created=1
  out="$(git clone -- "$remote" "$repo" 2>&1)" || rc=$?
  if [ "$rc" -ne 0 ]; then
    printf '%s\n' "$out" | mask >&2
    echo "entrypoint: cloning $remote_var ($(printf '%s' "$remote" | mask)) failed with exit code $rc" >&2
    # Only an empty directory we created ourselves; rmdir refuses anything else.
    if [ "$created" = 1 ]; then rmdir "$repo" 2>/dev/null || true; fi
    exit "$rc"
  fi
  printf '%s\n' "$out" | mask
}

ensure_clone IDEAS_REPO IDEAS_REMOTE
ensure_clone DOCS_REPO DOCS_REMOTE

exec "$@"
