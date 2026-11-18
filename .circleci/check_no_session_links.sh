#!/usr/bin/env bash
# Fails CI when a commit message or the pull request description carries a
# claude.ai link, a Claude-Session trailer or a "Generated with Claude Code"
# footer. Run by the `no-session-links` CircleCI job.
#
#   On main: checks the commit at HEAD (the squash-merge message).
#   On a branch: checks every commit not yet on origin/main, and the open
#   pull request's description when CircleCI reports one.
set -euo pipefail

PATTERN='https?://(www\.)?claude\.ai|claude\.(ai|com)/(code|project|chat|artifact|share)|^[[:space:]]*Claude-Session:|Generated (with|by) \[Claude Code\]|^[[:space:]>*_]*(🤖[[:space:]]*)?Generated (with|by) Claude Code'
status=0

if [ "${CIRCLE_BRANCH:-}" = "main" ]; then
  range="HEAD -1"
else
  git fetch -q origin main:refs/remotes/origin/main
  range="origin/main..HEAD"
fi

if ! commits=$(git rev-list $range); then
  echo "error: cannot read commits in $range" >&2
  exit 1
fi
bad=()
for c in $commits; do
  if git log -1 --format=%B "$c" | grep -Ei "$PATTERN" >/dev/null; then bad+=("$(git log -1 --format='%h' "$c")"); fi
done
if [ ${#bad[@]} -gt 0 ]; then
  # Name the commits only: printing the matched text would leak it into public CI logs.
  echo "error: ${#bad[@]} commit message(s) in $range carry a claude.ai link, session trailer or Claude Code footer:" >&2
  printf '  %s\n' "${bad[@]}" >&2
  echo "Reword them before they are pushed (see .gitmessage); for pushed commits, ask the owner." >&2
  status=1
else
  echo "ok: no session links or footers in commits ($range)"
fi

# Pull request description: it becomes the squash-merge body.
pr_url="${CIRCLE_PULL_REQUEST:-}"
if [ -n "$pr_url" ]; then
  api="https://api.github.com/repos/${pr_url#https://github.com/}"
  api="${api/\/pull\//\/pulls\/}"
  auth=()
  [ -n "${GITHUB_TOKEN:-}" ] && auth=(-H "Authorization: Bearer $GITHUB_TOKEN")
  if body=$(curl -fsSL "${auth[@]}" -H "Accept: application/vnd.github+json" "$api" \
      | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("title") or ""); print(d.get("body") or "")'); then
    if printf '%s\n' "$body" | grep -Ei "$PATTERN" >/dev/null; then
      echo "error: the pull request title or description carries a claude.ai link or a Claude Code footer. Edit it, then re-run this job." >&2
      status=1
    else
      echo "ok: no session links or footers in the pull request description"
    fi
  else
    echo "warning: could not read $pr_url from the GitHub API; description not checked" >&2
  fi
else
  echo "note: no pull request for this build yet; description not checked"
fi

exit $status
