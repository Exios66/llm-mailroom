#!/usr/bin/env python3
"""PostToolUse: syntax-check a just-edited .py file.

This repo deliberately has no linter (AGENTS.md: "don't invent one"), so this only
catches outright syntax errors by compiling the file in memory. Exit 2 returns the
error to Claude so it can fix the file in the same turn.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        path = (payload.get("tool_input") or {}).get("file_path") or ""
    except Exception:
        return 0
    if not path.endswith(".py"):
        return 0
    try:
        with open(path, encoding="utf-8") as fh:
            compile(fh.read(), path, "exec")
    except SyntaxError as exc:
        print(f"SyntaxError in {path}:{exc.lineno}: {exc.msg}", file=sys.stderr)
        return 2
    except OSError:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
