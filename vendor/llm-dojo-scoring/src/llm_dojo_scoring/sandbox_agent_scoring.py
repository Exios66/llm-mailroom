"""Sandbox deterministic isolation rubrics, preserving historical populations.

Producer adapters select fields/serialize values. These literal compatibility
rubrics are separate from GT-supported pipeline and generic field scoring.
"""
from __future__ import annotations


def label_match(expected, predicted, *, lowercase=False):
    """Literal equality, with optional lowercase only (no whitespace stripping)."""
    if lowercase:
        expected, predicted = expected.lower(), predicted.lower()
    return float(expected == predicted)


def contains_match(needle, haystack):
    """Case-insensitive substring; absent needle tests prediction truthiness."""
    return float(needle.lower() in haystack.lower() if needle else bool(haystack))


def reporter_keys(predicted, required):
    """Legacy all-key rubric, including independent diagnostic missing-key rules.

    Keys inside values count. Diagnostic matching is case-sensitive with the
    underscore fallback; the actual verdict searches the lowercase repr. The
    resulting apparent contradictions are preserved, not redefined as schema QA.
    """
    missing = [k for k in required if k not in predicted and f"_{k}" not in str(predicted)]
    if not predicted:
        return {"match": 0.0, "missing": required}
    text = str(predicted)
    present = all(str(k) in predicted or str(k) in text.lower() for k in required)
    return {"match": float(present), "missing": missing, "keys": list(predicted)}
