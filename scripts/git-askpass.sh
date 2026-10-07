#!/bin/sh
# git calls this with the prompt as $1; answers from GITHUB_TOKEN. Never prints the token to stderr.
# Only the two exact prompts git shows for https://github.com (the host quoted in full, so
# github.com.evil.example or another host gets nothing): the user name, then the token as its password.
[ -n "${GITHUB_TOKEN:-}" ] || exit 1
case "${1:-}" in
  "Username for 'https://github.com': "*) printf '%s\n' "x-access-token" ;;
  "Password for 'https://x-access-token@github.com': "*) printf '%s\n' "$GITHUB_TOKEN" ;;
  *) exit 1 ;;
esac
