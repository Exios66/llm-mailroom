"""The `.claude/settings.json` deny list must stop the paid / publishing scripts however the
interpreter is spelled. The docs teach `PYTHONPATH=src .venv/bin/python ...`; a rule that
names only `PYTHONPATH=src python ...` lets that spelling through."""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DENY = [r[len("Bash("):-1] for r in
        json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))["permissions"]["deny"]
        if r.startswith("Bash(")]

SPELLINGS = (
    "PYTHONPATH=src python src/scripts/{cmd}",
    "PYTHONPATH=src .venv/bin/python src/scripts/{cmd}",
    "PYTHONPATH=src python3 src/scripts/{cmd}",
    "uv run python src/scripts/{cmd}",
    "cd src && python scripts/{cmd}",
)
FORBIDDEN = (
    "sync_prompts.py",
    "sync_prompts.py --agent sorter",
    "sync_dataset.py",
    "sync_dashboards.py",
    "sync_evaluators.py",
    "sync_hf_ground_truth.py --real",
    "sync_hf_ground_truth.py --push",
    "sync_hf_ground_truth.py --real --push --limit 5",
)


def _denied(command: str) -> bool:
    """True when a deny rule matches; ``:*`` is a trailing prefix wildcard, ``*`` a glob."""
    return any(fnmatch.fnmatchcase(command, rule[:-2] + "*" if rule.endswith(":*") else rule)
               for rule in DENY)


@pytest.mark.parametrize("cmd", FORBIDDEN)
@pytest.mark.parametrize("spelling", SPELLINGS)
def test_paid_and_publishing_scripts_are_denied_in_every_spelling(spelling, cmd):
    """Every interpreter spelling of a forbidden command hits a deny rule."""
    assert _denied(spelling.format(cmd=cmd))


@pytest.mark.parametrize("cmd", [
    "sync_hf_ground_truth.py --check",
    "sync_hf_ground_truth.py --dry-run",
    "sync_gitbook_changelog.py --check",
])
def test_network_free_checks_are_not_denied(cmd):
    """The read-only gates stay usable."""
    assert not _denied(f"PYTHONPATH=src .venv/bin/python src/scripts/{cmd}")
