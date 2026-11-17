"""Whole-document coverage for the judge and the arbiters (#277).

A verifier that reads an excerpt cannot vouch for a document: text past the cut
can contradict what it accepts. These helpers split the source into the same
overlapping, paragraph-aware windows the sorter and intake agents use
(``agents.intake.sliding_windows``) so every character is read by some call.

The window size is the calling agent's ``agents.<name>.max_input_chars`` and the
overlap is ``chunking.overlap_chars`` (at most an eighth of the window, as the
specialists clamp it), both from ``taxonomy.yaml``. A document that fits in one
window is sent whole, exactly as before.
"""

from __future__ import annotations

import math

from agents.intake import sliding_windows

#: An overlap larger than this share of the window would starve the span budget.
_MAX_OVERLAP_SHARE = 8
_DEFAULT_OVERLAP_CHARS = 8_000


def window_overlap(budget: int) -> int:
    """``chunking.overlap_chars`` from the taxonomy, clamped to an eighth of ``budget``."""
    try:
        from pipeline.config import load_config

        configured = int((load_config().get("chunking") or {}).get("overlap_chars", _DEFAULT_OVERLAP_CHARS))
    except Exception:  # noqa: BLE001 - config trouble must not change what a verifier reads
        configured = _DEFAULT_OVERLAP_CHARS
    return max(0, min(configured, budget // _MAX_OVERLAP_SHARE))


def windows_for_budget(doc_text: str, budget: int) -> list[str]:
    """Every character of ``doc_text`` in windows of at most ``budget`` characters.

    Always returns at least one window (an empty document is one empty window), so
    a caller that checks "every window" never vacuously verifies nothing.
    """
    budget = max(1, int(budget))
    windows = [w for w, _start in sliding_windows(doc_text or "", budget, window_overlap(budget))]
    return windows or [doc_text or ""]


def source_windows(agent, doc_text: str) -> list[str]:
    """``doc_text`` in windows of ``agent``'s configured ``max_input_chars``."""
    return windows_for_budget(doc_text, agent._configured_max_input_chars())


def window_label(index: int, total: int) -> str:
    """``window 2 of 5`` (1-based), for findings and error messages."""
    return f"window {index} of {total}"


def valid_unit(value: object) -> float | None:
    """``value`` as a float in [0, 1], else None (bools, NaN, inf and out-of-range are invalid)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and 0.0 <= number <= 1.0 else None
