"""The `.claude/settings.json` deny list must stop the paid / publishing scripts however the
interpreter is spelled. The docs teach `PYTHONPATH=src .venv/bin/python ...`; a rule that
names only `PYTHONPATH=src python ...` lets that spelling through."""

from __future__ import annotations

import fnmatch
import json
import re
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
    "cd src/scripts && python {cmd}",
    "PYTHONPATH=src python -m scripts.{mod}",
)
FORBIDDEN = (
    "sync_prompts.py",
    "sync_prompts.py --agent sorter",
    "sync_dataset.py",
    "sync_dashboards.py",
    "sync_evaluators.py",
    "sync_hf_ground_truth.py --real",
    "sync_hf_ground_truth.py --push",
    "sync_hf_ground_truth.py --pu",       # argparse abbreviations of --push / --real
    "sync_hf_ground_truth.py --rea",
    "sync_hf_ground_truth.py --mock --push --limit 5",
)


def _subcommands(command: str) -> list[str]:
    """Split like Claude Code does: each part of a compound command is matched alone."""
    return [part.strip() for part in re.split(r"&&|\|\||;|\|", command) if part.strip()]


def _denied(command: str) -> bool:
    """True when a deny rule matches a subcommand; ``*`` is a glob matching any text."""
    return any(fnmatch.fnmatchcase(part, rule[:-2] + " *" if rule.endswith(":*") else rule)
               for part in _subcommands(command) for rule in DENY)


def _render(spelling: str, cmd: str) -> str:
    script, _, args = cmd.partition(" ")
    return spelling.format(cmd=cmd, mod=script[:-3] + (" " + args if args else ""))


@pytest.mark.parametrize("cmd", FORBIDDEN)
@pytest.mark.parametrize("spelling", SPELLINGS)
def test_paid_and_publishing_scripts_are_denied_in_every_spelling(spelling, cmd):
    """Every interpreter spelling of a forbidden command hits a deny rule."""
    assert _denied(_render(spelling, cmd))


@pytest.mark.parametrize("command", [
    "PYTHONPATH=src .venv/bin/python src/scripts/sync_hf_ground_truth.py --check",
    "PYTHONPATH=src .venv/bin/python src/scripts/sync_hf_ground_truth.py --dry-run",
    "PYTHONPATH=src .venv/bin/python -m scripts.sync_hf_ground_truth --check",
    "PYTHONPATH=src .venv/bin/python src/scripts/sync_gitbook_changelog.py --check",
    ".venv/bin/python -m pytest src/tests/test_sync_dashboards.py -q",
    ".venv/bin/python -m pytest src/tests/test_sync_dataset_hf.py -q",
    "PYTHONPATH=src .venv/bin/python src/scripts/sync_hf_ground_truth.py --check && git status",
])
def test_network_free_checks_and_tests_are_not_denied(command):
    """The read-only gates and the sync scripts' own tests stay usable."""
    assert not _denied(command)
