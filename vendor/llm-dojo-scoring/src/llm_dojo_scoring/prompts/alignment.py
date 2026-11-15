"""Do the specialist prompts ask for the fields the scoring schema scores?

The registry (``schemas.doc_classes``) says which fields a live document type is scored on.
Each specialist prompt carries its own field block (``- name (type): description``). This
module reads that block and reports, per production specialist, the schema fields the
prompt never asks for and the prompt keys the schema does not score. Known gaps are
recorded with a reason in ``data/alignment_gaps.json`` (empty since contracts and merger
production v2); the guard test fails on any change in either direction, so a gap can neither
appear nor be fixed unnoticed.

Imported from ``llm_dojo_scoring.prompts.alignment``; not exported from the package root.
The sorter's class list is not compared: its template only holds a
``{{doc_type_descriptions}}`` placeholder that llm-mailroom fills at run time.
"""

from __future__ import annotations

import json
import re
from importlib import resources

from ..schemas.doc_classes import LIVE_DOC_TYPES, field_types
from . import get_prompt, iter_prompts

__all__ = [
    "META_KEYS",
    "SIDES",
    "alignment_report",
    "compare_to_recorded",
    "prompt_fields",
    "recorded_gaps",
    "report_gaps",
]

#: Keys a prompt may ask for that the schema never scores.
META_KEYS = frozenset({"confidence", "reasoning"})
SIDES = ("missing_from_prompt", "extra_in_prompt")
_FIELD_LINE = re.compile(r"^- ([a-z_][a-z0-9_]*) \(([^)]*)\):", re.MULTILINE)


def prompt_fields(agent: str, family: str = "production") -> dict[str, str]:
    """``{field name: declared type}`` parsed from the field block of a prompt template."""
    return dict(_FIELD_LINE.findall(get_prompt(agent, family).text))


def alignment_report() -> dict[str, dict]:
    """Per production specialist with a live document type: what the prompt and schema disagree on.

    ``{agent: {"doc_type", "missing_from_prompt", "extra_in_prompt"}}``, lists sorted.
    ``extra_in_prompt`` leaves out ``META_KEYS``.
    """
    report: dict[str, dict] = {}
    for rec in iter_prompts():
        if rec.family != "production" or rec.kind != "llm" or not rec.agent.endswith("_specialist"):
            continue
        if rec.doc_bundle not in LIVE_DOC_TYPES:
            continue
        asked = set(prompt_fields(rec.agent, rec.family))
        scored = set(field_types(rec.doc_bundle))
        report[rec.agent] = {
            "doc_type": rec.doc_bundle,
            "missing_from_prompt": sorted(scored - asked),
            "extra_in_prompt": sorted(asked - scored - META_KEYS),
        }
    return report


def report_gaps(report: dict[str, dict] | None = None) -> set[tuple[str, str, str]]:
    """The report flattened to ``{(agent, field, side)}``."""
    report = alignment_report() if report is None else report
    return {(agent, name, side) for agent, row in report.items() for side in SIDES for name in row[side]}


def recorded_gaps() -> list[dict]:
    """The committed gaps (``data/alignment_gaps.json``): ``{agent, field, side, reason}`` rows."""
    raw = resources.files("llm_dojo_scoring.prompts").joinpath("data", "alignment_gaps.json")
    return json.loads(raw.read_text(encoding="utf-8"))["gaps"]


def compare_to_recorded(report: dict[str, dict] | None = None) -> tuple[list[tuple], list[tuple]]:
    """``(new, fixed)``: gaps the prompts have but the record lacks, and recorded gaps now gone."""
    live = report_gaps(report)
    recorded = {(g["agent"], g["field"], g["side"]) for g in recorded_gaps()}
    return sorted(live - recorded), sorted(recorded - live)
