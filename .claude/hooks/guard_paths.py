#!/usr/bin/env python3
"""PreToolUse guard: refuse Edit/Write/NotebookEdit on files Claude must not hand-edit.

Reads the hook JSON on stdin. Exit 2 (with a reason on stderr) blocks the call and
shows the reason to Claude; any other exit lets it through. Fails open on malformed
input so a hook bug can never wedge a session.
"""

from __future__ import annotations

import json
import os
import re
import sys

# (regex searched against the repo-relative POSIX path, explanation shown to Claude)
RULES = [
    (
        r"(^|/)\.env(\.(?!example$)[^/]+)?$",
        "Secrets file. Only .env.example is tracked; ask the user to edit .env themselves.",
    ),
    (
        r"^data/(?!README)",
        "Runtime data (bins, SQLite catalog, caches) is git-ignored operator state, not source.",
    ),
    (
        r"(^|/)docs/changelog/",
        "Generated from CHANGELOG.md. Edit CHANGELOG.md, then run "
        "`PYTHONPATH=src python src/scripts/sync_gitbook_changelog.py`.",
    ),
    (
        r"(^|/)docs/superpowers/baselines/",
        "Approved regression baseline. Re-approve only via `run_experiment.py --write-baseline`, "
        "and only when the user asks.",
    ),
]


def _candidates(path: str, root: str) -> tuple[list[str], bool]:
    """Return the repo-relative POSIX paths to check, and whether any is inside root.

    Two views are checked: the lexical one (``.``/``..`` folded, symlinks kept) and
    the resolved one (symlinks followed). So ``<root>/./data/x`` and
    ``<root>/src/../data/x`` hit ``^data/``, and so does ``data/x`` when ``data`` is
    a symlink to a directory outside the repo. A path outside the root in both views
    is returned absolute, and only the ``.env`` basename rule applies to it.
    """
    path = path.replace("\\", "/")
    root = root.replace("\\", "/")
    joined = os.path.join(root, path)
    views = [
        (os.path.normpath(joined), os.path.normpath(os.path.abspath(root))),
        (os.path.realpath(joined), os.path.realpath(root)),
    ]
    inside: list[str] = []
    for full, base in views:
        try:
            rel = os.path.relpath(full, base).replace("\\", "/")
        except ValueError:  # different drive on Windows
            continue
        if rel != ".." and not rel.startswith("../"):
            inside.append(rel)
    if inside:
        return inside, True
    return [views[1][0].replace("\\", "/")], False


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        tool_input = payload.get("tool_input") or {}
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    except Exception:
        return 0
    root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    rels, inside = _candidates(path, root)
    rules = RULES if inside else RULES[:1]  # outside the repo: only the .env rule
    for rel in rels:
        for pattern, reason in rules:
            if re.search(pattern, rel):
                print(f"Blocked edit to {path}: {reason}", file=sys.stderr)
                return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
