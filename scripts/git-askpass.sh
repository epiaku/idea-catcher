#!/bin/sh
# git calls this with the prompt as $1; answers from GITHUB_TOKEN. Never prints the token to stderr.
[ -n "${GITHUB_TOKEN:-}" ] || exit 1
case "$1" in
  Username*) printf '%s\n' "x-access-token" ;;
  Password*) printf '%s\n' "$GITHUB_TOKEN" ;;
  *) exit 1 ;;
esac
