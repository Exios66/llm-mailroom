#!/bin/bash
# Sync wiki pages to the GitHub wiki repository.
#
# Usage:
#   ./wiki/sync-wiki.sh
#
# Prerequisites:
#   - The repo must have a wiki enabled on GitHub
#   - You must have push access to the repo
#
# GitHub wikis are separate git repos at:
#   git@github.com:<user>/<repo>.wiki.git
#
# This script clones the wiki repo, copies these pages in,
# and commits + pushes.

set -e

REPO_URL="${1:-}"
WIKI_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -z "$REPO_URL" ]; then
    # Try to determine from git remote
    REPO_URL=$(cd "$WIKI_DIR/.." && git remote get-url origin 2>/dev/null || echo "")
    if [ -z "$REPO_URL" ]; then
        echo "Usage: $0 <repo-url>  (e.g., git@github.com:user/llm-mailroom.wiki.git)"
        echo "Or run from within a git repo with a configured origin remote."
        exit 1
    fi
fi

# Convert main repo URL to wiki repo URL
WIKI_REPO_URL="${REPO_URL%.git}.wiki.git"

TEMP_DIR=$(mktemp -d)
echo "Cloning wiki from: $WIKI_REPO_URL"
git clone "$WIKI_REPO_URL" "$TEMP_DIR" 2>/dev/null || {
    echo "Wiki repo not found. Create it first on GitHub:"
    echo "  Go to your repo -> Wiki tab -> Create the first page"
    echo "  Or: git clone $WIKI_REPO_URL (after enabling wiki in repo settings)"
    rm -rf "$TEMP_DIR"
    exit 1
}

# Copy all .md files from wiki/ to the wiki repo
echo "Copying wiki pages..."
cp "$WIKI_DIR"/*.md "$TEMP_DIR/"

cd "$TEMP_DIR"
git add -A
git commit -m "Sync wiki pages from main repo" || echo "No changes to commit"
git push origin master

cd /
rm -rf "$TEMP_DIR"
echo "Wiki synced successfully to $WIKI_REPO_URL"
echo "View it at: ${REPO_URL%.git}/wiki"
