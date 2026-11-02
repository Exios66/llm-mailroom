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


def _repo_relative(path: str, root: str) -> str:
    """Return ``path`` relative to ``root`` as a normalized POSIX path.

    Resolves ``.``/``..`` segments and symlinks first, so ``<root>/./data/x`` or
    ``<root>/src/../data/x`` cannot slip past the ``^data/`` rule. A path outside
    the root is returned normalized but absolute (only the basename rules apply).
    """
    path = path.replace("\\", "/")
    root = root.replace("\\", "/")
    full = os.path.realpath(os.path.join(root, path))
    real_root = os.path.realpath(root)
    try:
        rel = os.path.relpath(full, real_root)
    except ValueError:  # different drive on Windows
        return full.replace("\\", "/")
    if rel == ".." or rel.startswith(("../", "..\\")):
        return full.replace("\\", "/")
    return rel.replace("\\", "/")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        tool_input = payload.get("tool_input") or {}
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    except Exception:
        return 0
    root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    rel = _repo_relative(path, root)
    for pattern, reason in RULES:
        if re.search(pattern, rel):
            print(f"Blocked edit to {path}: {reason}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
